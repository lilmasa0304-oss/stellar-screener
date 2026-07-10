import asyncio
import math
import os
import re
import uuid
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from screener.config import Config
from screener.data_fetcher import DataFetcher
from screener.fundamentals import fetch_fundamentals
from screener.openai_diagnosis import (
    OPENAI_MODEL,
    build_diagnosis_user_message,
    call_openai_diagnosis,
    is_openai_configured,
)
from screener.jp_stock_code import (
    extract_jp_stock_code,
    find_jp_stock_code_in_text,
    normalize_jp_stock_code,
    normalize_stock_codes_param,
    split_stock_codes,
)
from screener.jp_stock_names import resolve_jp_display_name
from screener.strategy import StrategyEvaluator
from screener import storage
from screener.scheduler import start_scheduler, stop_scheduler, get_next_run_time

# ── ロギング設定 ───────────────────────────────────────────────────────────
def _configure_stdout_utf8() -> None:
    """Windows ターミナル等で日本語ログが化けないよう stdout/stderr を UTF-8 に統一する。"""
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdout_utf8()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger(__name__)

load_dotenv()

# ── JPX400 リアルタイムスキャン（インメモリ・直近結果キャッシュ） ─────────────
_jpx400_progress: Dict[str, Any] = {
    "status":    "idle",   # idle | running | completed | failed
    "scan_id":   None,
    "mode":      None,
    "processed": 0,
    "total":     0,
    "buy_count": 0,
    "buy_signals": [],
    "error":     None,
    "elapsed_seconds": None,
}


# ── FastAPI lifespan（起動/終了フック） ───────────────────────────────────
IS_VERCEL = os.getenv("VERCEL") == "1"
IS_RENDER = os.getenv("RENDER") == "true"
DISABLE_SCHEDULER = os.getenv("DISABLE_SCHEDULER", "0").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時
    storage.init_db()
    logger.info("SQLite DB を初期化しました。")

    # Vercel サーバーレス / DISABLE_SCHEDULER=1 では常駐スケジューラーを動かさない
    if not IS_VERCEL and not DISABLE_SCHEDULER:
        start_scheduler(hour=7, minute=0, timezone="UTC")
        logger.info("定時スキャンスケジューラーを起動しました（平日 16:00 JST）。")
    elif DISABLE_SCHEDULER:
        logger.info("DISABLE_SCHEDULER=1 のためスケジューラーは無効です。")

    yield

    # 終了時
    if not IS_VERCEL and not DISABLE_SCHEDULER:
        stop_scheduler()


# ── FastAPI アプリ ────────────────────────────────────────────────────────
app = FastAPI(
    title="STELLAR SCREENER API",
    description="JPX400 日本株スイングトレード・スクリーナー（クラウド対応）",
    lifespan=lifespan,
)


# ── YAML ヘルパー ─────────────────────────────────────────────────────────
def read_config_yaml() -> Dict[str, Any]:
    path = Path("config.yaml")
    if not path.exists():
        raise FileNotFoundError("config.yaml not found.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_config_yaml(data: Dict[str, Any]):
    path = Path("config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# ── Pydantic モデル ───────────────────────────────────────────────────────
class RsiConfigModel(BaseModel):
    enabled:              bool
    period:               int   = Field(..., ge=2,   le=50)
    oversold_threshold:   float = Field(..., ge=5,   le=50)
    overbought_threshold: float = Field(..., ge=50,  le=95)

class CrossoverConfigModel(BaseModel):
    enabled:      bool
    short_period: int = Field(..., ge=2,  le=100)
    long_period:  int = Field(..., ge=5,  le=200)

class DailyChangeConfigModel(BaseModel):
    enabled:           bool
    threshold_percent: float = Field(..., ge=0.1, le=20.0)

class MaTrendConfigModel(BaseModel):
    enabled:              bool
    ma5_period:           int  = Field(5,    ge=2,  le=20)
    ma25_period:          int  = Field(25,   ge=10, le=60)
    ma75_period:          int  = Field(75,   ge=30, le=120)
    require_ma25_uptrend: bool = True

class BollingerConfigModel(BaseModel):
    enabled:                  bool
    period:                   int   = Field(20,  ge=5,  le=50)
    std_dev:                  float = Field(2.0, ge=1.0, le=3.0)
    require_lower_band_touch: bool  = False

class CompositeEntryConfigModel(BaseModel):
    enabled: bool

class SwingSignalConfigModel(BaseModel):
    rsi_min:              float = Field(35.0, ge=20.0, le=50.0)
    rsi_max:              float = Field(45.0, ge=30.0, le=60.0)
    require_ma25_uptrend: bool  = True
    require_macd_cross:   bool  = True

class UpdateConfigPayload(BaseModel):
    tickers:         List[str]
    rsi:             RsiConfigModel
    crossover:       CrossoverConfigModel
    daily_change:    DailyChangeConfigModel
    ma_trend:        MaTrendConfigModel
    bollinger:       BollingerConfigModel
    composite_entry: CompositeEntryConfigModel
    swing_signal:    Optional[SwingSignalConfigModel] = None


class TickerDiagnosisPayload(BaseModel):
    ticker: Optional[str] = Field(None, description="日本株銘柄コード（例: 1605, 7203, 285A）")
    code:   Optional[str] = Field(None, description="ticker の別名（Dify HTTP ノード互換）")

    def resolved_ticker(self) -> str:
        raw = (self.ticker or self.code or "").strip()
        if not raw:
            raise HTTPException(status_code=422, detail="ticker または code が必要です。")
        normalized = normalize_jp_stock_code(raw)
        return normalized or raw.upper().removesuffix(".T")


class ScreenPayload(BaseModel):
    code:       Optional[str]  = Field(None, description="Dify から送る銘柄コード（例: 1605）")
    ticker:     Optional[str]  = Field(None, description="銘柄コード（code の別名）")
    mode:       Optional[str]  = Field(None, description="リスクモード（堅実 / 標準 / 積極）")
    full_scan:  Optional[bool] = Field(False, description="true のときのみ JPX400 一括スキャンを実行")


class WatchlistPayload(BaseModel):
    tickers: List[str] = Field(default_factory=list, description="ウォッチリスト銘柄コード一覧")


class MarketScanPayload(BaseModel):
    mode: Optional[str] = Field("堅実", description="リスクモード（堅実 / 標準 / 積極）")


class ChatPayload(BaseModel):
    query:            str = Field(..., description="ユーザー入力（銘柄コードなど。例: 7203 / 1605,7203）")
    code:             Optional[str] = Field(
        None,
        description="銘柄コード（query と同値でよい。例: 7203 / 1605,7203）",
    )
    mode:             Optional[str] = Field(
        None,
        description="リスクモード（堅実 / 標準 / 積極）",
    )
    screen_data:      Optional[Dict[str, Any]] = Field(
        None,
        description="フロント/スキャン結果の実診断データ。未指定時はサーバーで取得。",
    )
    conversation_id:  Optional[str] = Field(None, description="後方互換用（未使用）")


# 並列リクエスト上限（Yahoo Finance API 制限対策・Vercel 向けに最適化）
MAX_CONCURRENT_REQUESTS = 40
_scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
_scan_lock = asyncio.Lock()

# リスク許容度ごとの StrategyEvaluator 閾値マッピング
RISK_MODES: Dict[str, Dict[str, float]] = {
    "堅実": {
        "oshieme_rsi_limit": 23,
        "volume_spike_threshold": 1.7,
        "ma25_divergence_cap": 4.0,
    },
    "標準": {
        "oshieme_rsi_limit": 30,
        "volume_spike_threshold": 1.3,
        "ma25_divergence_cap": 7.0,
    },
    "積極": {
        "oshieme_rsi_limit": 35,
        "volume_spike_threshold": 1.0,
        "ma25_divergence_cap": 12.0,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# ルート定義
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """メインダッシュボード HTML を返す。"""
    html_path = Path("templates/index.html")
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )


# ── 設定 API ──────────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    """現在の設定一式を返す。"""
    try:
        cfg = read_config_yaml()
        cfg["next_scan_time"]  = get_next_run_time()
        return cfg
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"設定の読み込みに失敗: {e}")


@app.post("/api/config")
def update_config(payload: UpdateConfigPayload):
    """UI からの設定変更を config.yaml に書き込む。"""
    try:
        current = read_config_yaml()
        current["tickers"] = payload.tickers
        s = current.setdefault("strategies", {})
        s["rsi"]             = payload.rsi.model_dump()
        s["crossover"]       = payload.crossover.model_dump()
        s["daily_change"]    = payload.daily_change.model_dump()
        s["ma_trend"]        = payload.ma_trend.model_dump()
        s["bollinger"]       = payload.bollinger.model_dump()
        s["composite_entry"] = payload.composite_entry.model_dump()
        if payload.swing_signal:
            current["swing_signal"] = payload.swing_signal.model_dump()
        write_config_yaml(current)
        return {"status": "success", "message": "設定を更新しました。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"設定の保存に失敗: {e}")


# ── ウォッチリスト API（後方互換: フロントは localStorage を使用） ───────
@app.get("/api/watchlist")
def get_watchlist():
    """ウォッチリスト銘柄一覧を返す（配布版は常に空。各ユーザーの保存はブラウザ側）。"""
    return {"tickers": []}


@app.put("/api/watchlist")
def update_watchlist(payload: WatchlistPayload):
    """後方互換用。サーバー側には保存しない。"""
    return {"status": "success", "tickers": payload.tickers}


@app.get("/api/stocks")
def get_stocks_data():
    """
    ウォッチリスト銘柄の最新株価・テクニカル指標・スパークラインを返す。
    MACD / BUY SIGNAL も含む。
    """
    try:
        config      = Config("config.yaml")
        fetcher     = DataFetcher(delay_seconds=0.2, history_period="6mo")
        ticker_data = fetcher.fetch_all(config.tickers)
        evaluator   = StrategyEvaluator(config.data)
        results     = []

        for ticker, (df, name) in ticker_data.items():
            if df.empty:
                continue

            sparkline = [float(p) for p in df["Close"].tail(15).tolist()]
            ev        = evaluator.evaluate(ticker, name, df)

            results.append({
                "ticker":              ticker,
                "name":                name,
                "current_price":       float(ev["current_price"]) if ev["current_price"] else None,
                "change_percent":      float(ev["change_percent"]),
                "triggered":           ev["triggered"],
                "is_prime_entry":      ev["is_prime_entry"],
                "buy_signal":          ev["buy_signal"],
                "signals":             ev["signals"],
                "sparkline":           sparkline,
                # テクニカル指標
                "rsi":                 ev["rsi"],
                "ma5":                 ev["ma5"],
                "ma25":                ev["ma25"],
                "ma75":                ev["ma75"],
                "bb_upper":            ev["bb_upper"],
                "bb_lower":            ev["bb_lower"],
                "ma25_deviation_pct":  ev["ma25_deviation_pct"],
                "ma25_uptrend":        ev["ma25_uptrend"],
                "price_above_ma25":    ev["price_above_ma25"],
                "bb_lower_touch":      ev["bb_lower_touch"],
                "ma5_rebound":         ev["ma5_rebound"],
                # MACD
                "macd":                ev["macd"],
                "macd_signal":         ev["macd_signal"],
                "macd_hist":           ev["macd_hist"],
                "macd_crossover":      ev["macd_crossover"],
                "macd_pre_crossover":  ev["macd_pre_crossover"],
                # BUY SIGNAL サブ条件
                "swing_cond_rsi":      ev["swing_cond_rsi"],
                "swing_cond_ma25":     ev["swing_cond_ma25"],
                "swing_cond_macd":     ev["swing_cond_macd"],
            })

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"株価データ取得に失敗: {e}")


# ── 即時スクリーニング（ウォッチリスト対象） ──────────────────────────────
@app.post("/api/run")
def run_screener():
    """ウォッチリスト対象の即時スクリーニングを実行する。"""
    try:
        config      = Config("config.yaml")
        fetcher     = DataFetcher(
            delay_seconds=config.delay_seconds,
            history_period=config.history_period,
        )
        ticker_data = fetcher.fetch_all(config.tickers)
        evaluator   = StrategyEvaluator(config.data)
        matched     = []
        buy_signals = []

        for ticker, (df, name) in ticker_data.items():
            res = evaluator.evaluate(ticker, name, df)
            for k in ("current_price", "change_percent", "rsi",
                      "ma5", "ma25", "ma75", "bb_upper", "bb_lower",
                      "ma25_deviation_pct", "macd", "macd_signal", "macd_hist"):
                if res.get(k) is not None:
                    res[k] = float(res[k])
            if res["triggered"]:
                matched.append(res)
            if res["buy_signal"]:
                buy_signals.append(res)

        return {
            "status":           "success",
            "matched_count":    len(matched),
            "buy_signal_count": len(buy_signals),
            "matched_results":  matched,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"スクリーニング実行に失敗: {e}")


# ── JPX400 リアルタイムスキャン（Vercel 完結型） ─────────────────────────

def _signal_label(preset_matched: Optional[str]) -> str:
    if preset_matched == "oshieme":
        return "押し目シグナル"
    if preset_matched == "junbari":
        return "順張りブレイク"
    return "シグナル"


def _enrich_scan_result(ev: Dict[str, Any]) -> Dict[str, Any]:
    """スキャン結果を DB 保存・API 返却用に正規化する。"""
    preset = ev.get("preset_matched", "none")
    ev["current_price"] = ev.get("current_price") or ev.get("close_price")
    ev["signals"] = [{
        "preset_matched": preset,
        "signal_label":   _signal_label(preset),
        "reason":         ev.get("reason", ""),
    }]
    return ev


async def _localize_stock_names(items: List[Dict[str, Any]]) -> None:
    """BUY SIGNAL 銘柄の表示名を日本語に置き換える。"""
    async def _one(item: Dict[str, Any]) -> None:
        ticker = item.get("ticker", "")
        fallback = item.get("name", ticker)
        item["name"] = await asyncio.to_thread(
            resolve_jp_display_name, ticker, fallback,
        )

    if items:
        await asyncio.gather(*(_one(item) for item in items))


def _decorate_buy_signal_rows(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DB から読んだ buy_signals に preset / reason を付与する。"""
    decorated: List[Dict[str, Any]] = []
    for row in results:
        item = dict(row)
        meta = (item.get("signals") or [{}])[0] if item.get("signals") else {}
        preset = meta.get("preset_matched", "none")
        item["preset_matched"] = preset
        item["signal_label"] = meta.get("signal_label") or _signal_label(preset)
        item["reason"] = meta.get("reason", "")
        decorated.append(item)
    return decorated


async def _execute_jpx400_realtime_scan(selected_mode: str = "堅実") -> Dict[str, Any]:
    """
    JPX400（約400銘柄）を非同期並列スクリーニングし、結果を即時返却する。
    Vercel 上でボタン押下 → 同一リクエストで完結する想定。
    """
    import time
    from datetime import datetime
    from screener.jpx400 import get_jpx400_tickers

    safe_mode = selected_mode if selected_mode in RISK_MODES else "堅実"
    mode_config = RISK_MODES[safe_mode]
    tickers = get_jpx400_tickers()
    total = len(tickers)

    scan_id = (
        f"jpx400_{safe_mode}_"
        f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:6]}"
    )

    global _jpx400_progress
    _jpx400_progress.update({
        "status":          "running",
        "scan_id":         scan_id,
        "mode":            safe_mode,
        "processed":       0,
        "total":           total,
        "buy_count":       0,
        "buy_signals":     [],
        "error":           None,
        "elapsed_seconds": None,
    })

    storage.create_session(
        scan_id=scan_id,
        scan_type=f"jpx400_{safe_mode}",
        total_tickers=total,
    )

    started = time.monotonic()
    config = read_config_yaml()
    fetcher = DataFetcher(delay_seconds=0, history_period="6mo")
    evaluator = StrategyEvaluator(config)

    tasks = [
        _fetch_and_evaluate_single_stock(ticker, mode_config, evaluator, fetcher)
        for ticker in tickers
    ]
    results = await asyncio.gather(*tasks)

    buy_signals: List[Dict[str, Any]] = []
    for stock in results:
        if stock is None:
            continue
        ev = dict(stock["metrics"])
        ev["ticker"] = stock["ticker"]
        ev["name"] = stock["name"]
        ev = _enrich_scan_result(ev)
        for k in ("current_price", "rsi"):
            if ev.get(k) is not None:
                ev[k] = float(ev[k])
        storage.save_result(scan_id, ev)
        buy_signals.append(ev)

    elapsed = round(time.monotonic() - started, 2)
    await _localize_stock_names(buy_signals)
    decorated = _decorate_buy_signal_rows(buy_signals)

    storage.complete_session(
        scan_id=scan_id,
        buy_signal_count=len(buy_signals),
        sent_line=False,
    )

    payload = {
        "status":          "completed",
        "scan_id":         scan_id,
        "mode":            safe_mode,
        "total":           total,
        "total_tickers":   total,
        "processed":       total,
        "buy_count":       len(buy_signals),
        "buy_signals":     decorated,
        "elapsed_seconds": elapsed,
        "message":         (
            f"【{safe_mode}】JPX400 スキャン完了: "
            f"BUY SIGNAL {len(buy_signals)} 件 / {total} 銘柄（{elapsed}秒）"
        ),
    }
    _jpx400_progress.update(payload)
    logger.info(payload["message"])
    return payload


@app.post("/api/jpx400/scan")
async def start_jpx400_scan(payload: MarketScanPayload):
    """JPX400 約400銘柄をリアルタイム並列スキャンし、結果を同一レスポンスで返す。"""
    if _scan_lock.locked():
        raise HTTPException(
            status_code=409,
            detail=(
                f"スキャンがすでに実行中です。"
                f"（{_jpx400_progress.get('processed', 0)}/"
                f"{_jpx400_progress.get('total', 0)} 処理済み）"
            ),
        )

    safe_mode = payload.mode if payload.mode in RISK_MODES else "堅実"
    try:
        async with _scan_lock:
            return await _execute_jpx400_realtime_scan(safe_mode)
    except Exception as e:
        logger.exception(f"[JPX400 scan] 致命的エラー: {e}")
        _jpx400_progress.update({"status": "failed", "error": str(e)})
        raise HTTPException(status_code=500, detail=f"JPX400 スキャンに失敗: {e}")


@app.post("/api/market/scan")
async def start_market_scan(payload: MarketScanPayload):
    """後方互換: JPX400 リアルタイムスキャンへ委譲。"""
    return await start_jpx400_scan(payload)


@app.get("/api/jpx400/status")
def get_jpx400_status():
    """直近の JPX400 スキャン結果（インメモリキャッシュ）を返す。"""
    return get_market_scan_status()


@app.get("/api/market/status")
def get_market_scan_status():
    """直近の JPX400 スキャン結果を返す（後方互換エンドポイント）。"""
    prog = dict(_jpx400_progress)
    scan_id = prog.get("scan_id")
    session_info = storage.get_session(scan_id) if scan_id else None
    return {
        **prog,
        "session":        session_info,
        "next_scan_time": get_next_run_time(),
    }


# ── 履歴 API ─────────────────────────────────────────────────────────────
@app.get("/api/history/sessions")
def get_scan_history(limit: int = 20):
    """過去のスキャンセッション一覧を返す。"""
    try:
        return storage.list_sessions(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"履歴の取得に失敗: {e}")


@app.get("/api/history/buy-signals")
def get_buy_signal_history(limit: int = 50):
    """過去のスキャンで BUY SIGNAL が出た銘柄を新しい順に返す。"""
    try:
        return storage.get_history_buy_signals(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"BUY SIGNAL 履歴の取得に失敗: {e}")


@app.get("/api/history/results/{scan_id}")
def get_scan_results(scan_id: str, buy_signal_only: bool = False):
    """特定スキャンの全銘柄結果を返す。"""
    try:
        session = storage.get_session(scan_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"scan_id '{scan_id}' が見つかりません。")
        results = storage.get_results(scan_id, buy_signal_only=buy_signal_only)
        return {"session": session, "results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"スキャン結果の取得に失敗: {e}")


# ── 銘柄診断 API（外部連携・後方互換） ───────────────────────────────────
def _normalize_ticker_code(raw: str) -> str:
    """銘柄コード (7203, 285A 等) を Yahoo Finance 用ティッカーに正規化する。"""
    normalized = normalize_jp_stock_code(raw)
    if not normalized:
        code = raw.strip()
        if not code:
            raise HTTPException(status_code=422, detail="銘柄コードが空です。")
        if code.upper().endswith(".T"):
            return code.upper()
        return f"{code.upper()}.T"
    return f"{normalized}.T"


def _safe_float(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    """NaN / Inf を除去した float を返す。"""
    if value is None:
        return fallback
    try:
        if hasattr(value, "item"):
            value = value.item()
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return num
    except (TypeError, ValueError):
        return fallback


def _last_valid_close(df) -> Optional[float]:
    """直近の有効な終値（当日 NaN 行をスキップ）。"""
    if df is None or df.empty or "Close" not in df.columns:
        return None
    closes = df["Close"].dropna()
    if closes.empty:
        return None
    return _safe_float(closes.iloc[-1])


def _compute_technical_metrics(df) -> Dict[str, float]:
    """診断レポート表示用の補助指標を算出する。"""
    close_p = _last_valid_close(df) or 0.0
    ma25 = df["Close"].rolling(window=25).mean().iloc[-1]
    ma25_val = _safe_float(ma25)
    ma25_divergence = ((close_p - ma25_val) / ma25_val) * 100 if ma25_val else 0.0
    avg_volume_5d = df["Volume"].iloc[-6:-1].mean()
    latest = df.iloc[-1]
    current_volume = _safe_float(latest["Volume"], 0.0) or 0.0
    avg_vol = _safe_float(avg_volume_5d, 0.0) or 0.0
    volume_ratio = current_volume / avg_vol if avg_vol > 0 else 1.0
    return {
        "ma25_divergence_pct": round(_safe_float(ma25_divergence, 0.0) or 0.0, 2),
        "volume_ratio":        round(_safe_float(volume_ratio, 1.0) or 1.0, 2),
    }


def _diagnose_ticker(raw_code: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """株価取得 → テクニカル分析 → OpenAI 診断向けレスポンスを組み立てる。"""
    ticker_code = raw_code.strip()
    yahoo_ticker = _normalize_ticker_code(ticker_code)

    fetcher = DataFetcher(delay_seconds=0.2, history_period="6mo")
    df, company_name = fetcher.fetch_ticker_data(yahoo_ticker)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"銘柄 {ticker_code} のデータが取得できませんでした。",
        )

    name = resolve_jp_display_name(
        yahoo_ticker,
        company_name or f"銘柄コード:{ticker_code}",
    )
    config = read_config_yaml()
    evaluator = StrategyEvaluator(config)
    safe_mode = mode if mode in RISK_MODES else None
    if safe_mode:
        _apply_risk_mode(evaluator, RISK_MODES[safe_mode])
    result = evaluator.evaluate(yahoo_ticker, name, df)
    metrics = _compute_technical_metrics(df)
    fundamentals = fetch_fundamentals(yahoo_ticker)

    display_code = ticker_code.removesuffix(".T")
    close_fallback = _last_valid_close(df)
    current_price = _safe_float(
        result.get("current_price") or result.get("close_price"),
        close_fallback,
    )
    return {
        "status":              "success",
        "code":                display_code,
        "ticker":              display_code,
        "name":                name,
        "mode":                safe_mode or "堅実",
        "current_price":       current_price,
        "rsi":                 _safe_float(result.get("rsi")),
        "ma25":                _safe_float(result.get("ma25")),
        "ma25_uptrend":        result.get("ma25_uptrend"),
        "ma25_deviation_pct":  metrics["ma25_divergence_pct"],
        "volume_ratio":        metrics["volume_ratio"],
        "buy_signal":          result.get("buy_signal", False),
        "reason":              result.get("reason"),
        "preset_matched":      result.get("preset_matched"),
        "preset_evaluations":  result.get("preset_evaluations"),
        "sector":              fundamentals.get("sector") or result.get("sector"),
        "trend_status":        _derive_trend_status(result),
        "fundamentals":        fundamentals,
        "fundamental_grade":   (fundamentals.get("assessment") or {}).get("grade"),
        "fundamental_score":   (fundamentals.get("assessment") or {}).get("score"),
    }


def _apply_risk_mode(evaluator: StrategyEvaluator, mode_config: Dict[str, float]) -> None:
    """RISK_MODES の値を既存 StrategyEvaluator.settings に反映する。"""
    evaluator.settings["rsi_oshieme_max"] = mode_config["oshieme_rsi_limit"]
    evaluator.settings["volume_growth_ratio"] = mode_config["volume_spike_threshold"]
    evaluator.settings["max_ma25_divergence"] = mode_config["ma25_divergence_cap"]


def _derive_trend_status(result: Dict[str, Any]) -> str:
    """evaluate() 結果からトレンド判定ラベルを導出する。"""
    if not result.get("buy_signal"):
        return "WAIT"
    preset = result.get("preset_matched")
    if preset == "oshieme":
        return "HOLD"
    if preset == "junbari":
        return "ENTRY_OK"
    return "MONITOR"


async def _fetch_and_evaluate_single_stock(
    ticker: str,
    mode_config: Dict[str, float],
    evaluator: StrategyEvaluator,
    fetcher: DataFetcher,
) -> Optional[Dict[str, Any]]:
    """1銘柄を非同期ワーカーで取得・評価する。"""
    async with _scan_semaphore:
        try:
            df, name = await asyncio.to_thread(fetcher.fetch_ticker_data, ticker)
            if df is None or df.empty:
                return None

            stock_evaluator = StrategyEvaluator(evaluator.config)
            _apply_risk_mode(stock_evaluator, mode_config)
            metrics = stock_evaluator.evaluate(ticker, name or ticker, df)

            if not metrics.get("buy_signal"):
                return None

            return {
                "ticker": ticker,
                "name": name or ticker,
                "metrics": metrics,
                "trend_status": _derive_trend_status(metrics),
            }
        except Exception as e:
            logger.error(f"銘柄 {ticker} の処理中にエラーが発生: {e}")
            return None


async def _execute_bulk_screener(selected_mode: str) -> Dict[str, Any]:
    """JPX400 銘柄を非同期並列でスクリーニングする（/api/v1/screen 用）。"""
    result = await _execute_jpx400_realtime_scan(selected_mode)
    buy_signals = result.get("buy_signals") or []
    if not buy_signals:
        return {
            "status": "NO_TRADE",
            "mode": result.get("mode", selected_mode),
            "message": (
                f"本日、{selected_mode}モードの超厳格基準を突破できた銘柄は0件でした。"
                "完璧な資本防衛が遂行されました。"
            ),
            "stocks": [],
            "count": 0,
        }
    stocks = [
        {
            "ticker": row.get("ticker"),
            "name": row.get("name"),
            "metrics": row,
            "trend_status": _derive_trend_status(row),
        }
        for row in buy_signals
    ]
    return {
        "status": "SUCCESS",
        "mode": result.get("mode", selected_mode),
        "count": len(stocks),
        "stocks": stocks,
        "elapsed_seconds": result.get("elapsed_seconds"),
    }


@app.post("/api/v1/screen")
async def execute_screener(payload: ScreenPayload):
    """
    スクリーニング API。
    - code / ticker 指定時: 単一銘柄の実診断（Yahoo Finance）
    - full_scan 指定時: JPX400 一括スキャン
    """
    if payload.code is not None:
        code = payload.code.strip()
        if code:
            normalized = normalize_stock_codes_param(code)
            if normalized and "," in normalized:
                logger.info(f"/api/v1/screen 複数銘柄実診断: codes={normalized}")
                stocks = []
                for single_code in normalized.split(","):
                    try:
                        stocks.append(
                            await asyncio.to_thread(_diagnose_ticker, single_code, payload.mode)
                        )
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.error(f"/api/v1/screen 銘柄 {single_code} エラー: {e}")
                return {
                    "status": "success",
                    "count":  len(stocks),
                    "stocks": stocks,
                }
            single = normalized or extract_jp_stock_code(code)
            if single:
                logger.info(f"/api/v1/screen 単一銘柄実診断: code={single}")
                return await asyncio.to_thread(_diagnose_ticker, single, payload.mode)
            raise HTTPException(
                status_code=422,
                detail=f"銘柄コード '{code}' を解析できませんでした。",
            )

    ticker_code = (payload.ticker or "").strip()
    if ticker_code:
        try:
            return await asyncio.to_thread(_diagnose_ticker, ticker_code)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"/api/v1/screen 単一銘柄エラー (ticker={ticker_code}): {e}")
            raise HTTPException(status_code=500, detail=f"銘柄診断中にエラーが発生しました: {e}")

    selected_mode = payload.mode or "堅実"
    if not payload.full_scan:
        raise HTTPException(
            status_code=422,
            detail="code または ticker を指定してください（mode のみでは実データを返しません）。",
        )

    try:
        return await _execute_bulk_screener(selected_mode)
    except Exception as e:
        logger.exception(f"/api/v1/screen 一括スキャンエラー (mode={selected_mode}): {e}")
        raise HTTPException(status_code=500, detail=f"一括スクリーニング中にエラーが発生しました: {e}")


@app.post("/api/bot/diagnose")
def diagnose_ticker_for_bot(payload: TickerDiagnosisPayload):
    """Dify の AI ボットから呼び出され、特定銘柄のテクニカル分析結果を返す。"""
    try:
        return _diagnose_ticker(payload.resolved_ticker())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"/api/bot/diagnose エラー (ticker={payload.resolved_ticker()}): {e}")
        raise HTTPException(status_code=500, detail=f"銘柄診断中にエラーが発生しました: {e}")


@app.post("/api/diagnose")
@app.post("/api/v1/diagnose")
def diagnose_ticker_legacy(payload: TickerDiagnosisPayload):
    """Dify ワークフロー互換エイリアス（旧 URL → /api/bot/diagnose）。"""
    return diagnose_ticker_for_bot(payload)


@app.post("/api/screen")
async def execute_screener_legacy(payload: ScreenPayload):
    """Dify ワークフロー互換エイリアス（旧 URL → /api/v1/screen）。"""
    return await execute_screener(payload)


# ── 統合AI診断（OpenAI 直接呼び出し） ─────────────────────────────────────


def _extract_single_ticker_from_query(query: str) -> Optional[str]:
    """単一銘柄の診断依頼かどうかを判定し、銘柄コードを返す。"""
    q = query.strip()
    if not q:
        return None
    if "ウォッチリスト" in q and "スクリーニング診断" in q:
        return None
    return extract_jp_stock_code(q)


def _parse_query_for_screen(query: str) -> Dict[str, Any]:
    """チャット入力から銘柄コードまたはリスクモードを推定する。"""
    q = query.strip()
    if q in RISK_MODES:
        return {"mode": q}
    for mode in RISK_MODES:
        if mode in q:
            code = find_jp_stock_code_in_text(q)
            if code:
                return {"mode": mode, "code": code}
            return {"mode": mode}
    code = find_jp_stock_code_in_text(q)
    if code:
        return {"code": code}
    return {"mode": "堅実"}


def _resolve_chat_code(explicit_code: Optional[str], query: str) -> Optional[str]:
    """フロントから渡された code を優先し、なければ query から推定する。"""
    normalized = normalize_stock_codes_param(explicit_code)
    if normalized:
        return normalized
    normalized = normalize_stock_codes_param(query)
    if normalized:
        return normalized
    single = _extract_single_ticker_from_query(query)
    return normalize_stock_codes_param(single) if single else None


def _resolve_chat_codes(code: Optional[str], query: str) -> List[str]:
    """チャット診断対象の銘柄コード一覧を解決する。"""
    normalized = _resolve_chat_code(code, query)
    if normalized:
        return split_stock_codes(normalized) or [c.strip() for c in normalized.split(",") if c.strip()]
    single = extract_jp_stock_code(query) or _extract_single_ticker_from_query(query)
    return [single] if single else []


def _lookup_jpx400_scan_hit(code: str) -> Optional[Dict[str, Any]]:
    """直近 JPX400 スキャン結果から銘柄ヒットを検索する。"""
    target = (normalize_jp_stock_code(code) or code.strip()).removesuffix(".T").upper()
    if not target:
        return None
    for row in _jpx400_progress.get("buy_signals") or []:
        ticker = (row.get("ticker") or "").removesuffix(".T").upper()
        if ticker == target:
            return dict(row)
    return None


def _merge_screen_data(
    primary: Dict[str, Any],
    secondary: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """診断 dict をマージする（primary を優先、欠損のみ secondary で補完）。"""
    merged = dict(primary)
    if not secondary:
        return merged
    for key, value in secondary.items():
        if value is None:
            continue
        if merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def _fetch_live_screen_data(
    code: str,
    mode: Optional[str] = None,
    hint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Yahoo Finance 実診断データを取得する（OpenAI 診断用）。"""
    normalized = normalize_stock_codes_param(code)
    if not normalized or "," in normalized:
        raise HTTPException(status_code=422, detail="単一銘柄コードが必要です。")

    single_code = normalized.split(",")[0]
    live = _diagnose_ticker(single_code, mode)
    cached = _lookup_jpx400_scan_hit(single_code)
    if cached:
        live = _merge_screen_data(live, cached)
    if hint and not _is_dummy_screen_data(hint):
        live = _merge_screen_data(live, hint)
    return live


def _is_dummy_screen_data(screen_data: Optional[Dict[str, Any]]) -> bool:
    """フロント hint が空またはダミーかどうか。"""
    if not screen_data:
        return True
    if screen_data.get("fast_response"):
        return True
    rsi = screen_data.get("rsi")
    divergence = screen_data.get("ma25_deviation_pct", screen_data.get("ma25_divergence_pct"))
    if rsi == 20.0 and divergence == -5.0 and screen_data.get("current_price") is None:
        return True
    return False


async def _gather_screen_data_for_chat(
    codes: List[str],
    mode: Optional[str],
    hint: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """複数銘柄の実診断データを収集する。"""
    results: List[Dict[str, Any]] = []
    for index, code in enumerate(codes):
        use_hint = hint if len(codes) == 1 and index == 0 else None
        data = await asyncio.to_thread(_fetch_live_screen_data, code, mode, use_hint)
        results.append(data)
    return results


async def _run_openai_diagnosis(
    query: str,
    codes: List[str],
    mode: Optional[str],
    hint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """OpenAI API で統合AI診断を実行する。"""
    if not is_openai_configured():
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY が未設定です。環境変数に API キーを設定してください。",
        )

    screen_data_list = await _gather_screen_data_for_chat(codes, mode, hint)
    user_message = build_diagnosis_user_message(screen_data_list, query)
    try:
        answer = await asyncio.to_thread(call_openai_diagnosis, user_message)
    except RuntimeError as exc:
        logger.error("OpenAI 診断失敗: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "answer": answer,
        "source": "openai",
        "model":  OPENAI_MODEL,
    }


@app.post("/api/chat")
async def stellar_chat(payload: ChatPayload):
    """統合AI診断チャット（OpenAI 直接呼び出し）。"""
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query が空です。")

    chat_mode = payload.mode if payload.mode in RISK_MODES else None
    codes = _resolve_chat_codes(payload.code, query)

    if not codes:
        parsed = _parse_query_for_screen(query)
        if "code" in parsed:
            codes = [parsed["code"]]
            if parsed.get("mode") in RISK_MODES:
                chat_mode = parsed["mode"]

    if not codes:
        raise HTTPException(
            status_code=422,
            detail="銘柄コードを入力してください（例: 7203 / 285A）。",
        )

    hint = payload.screen_data if not _is_dummy_screen_data(payload.screen_data) else None
    logger.info(
        "OpenAI 診断: query=%r codes=%s mode=%s",
        query,
        ",".join(codes),
        chat_mode or "(なし)",
    )
    return await _run_openai_diagnosis(query, codes, chat_mode, hint)


@app.post("/api/dify/chat")
async def dify_chat_legacy(payload: ChatPayload):
    """後方互換エイリアス（/api/chat へ委譲）。"""
    return await stellar_chat(payload)


@app.get("/api/chat/status")
@app.get("/api/dify/status")
def chat_status(probe: bool = False):
    """OpenAI 診断チャットの設定状態を返す（APIキー本体は返さない）。"""
    configured = is_openai_configured()
    result: Dict[str, Any] = {
        "configured": configured,
        "provider":   "openai",
        "model":      OPENAI_MODEL,
        "mode":       "openai" if configured else "unconfigured",
    }
    if probe and configured:
        result["probe"] = {"reachable": True, "provider": "openai"}
    return result


# ── ヘルスチェック ────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    """Render / UptimeRobot 等のヘルスチェック用。"""
    cfg = Config("config.yaml")
    return {
        "status":          "ok",
        "platform":        "render" if IS_RENDER else ("vercel" if IS_VERCEL else "local"),
        "openai_configured": is_openai_configured(),
        "chat_mode":       "openai" if is_openai_configured() else "unconfigured",
        "openai_model":    OPENAI_MODEL,
        "ticker_count":    len(cfg.tickers),
        "universe":        cfg.universe or "custom",
        "scheduler":       "disabled" if DISABLE_SCHEDULER else "active",
        "next_scan_time":  None if DISABLE_SCHEDULER else get_next_run_time(),
        "db_path":         str(storage.DB_PATH.resolve()),
    }


if __name__ == "__main__":
    import uvicorn

    _configure_stdout_utf8()
    uvicorn.run("web_app:app", host="0.0.0.0", port=8000, reload=True)