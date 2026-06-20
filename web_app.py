import asyncio
import os
import uuid
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from screener.config import Config
from screener.data_fetcher import DataFetcher
from screener.strategy import StrategyEvaluator
from screener.notifier import LineNotifier
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

DIFY_API_KEY  = os.getenv("DIFY_API_KEY", "")
DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "https://api.dify.ai/v1").rstrip("/")

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
    ticker: str = Field(..., description="4桁の日本株銘柄コード（例: 1605, 7203）")


class ScreenPayload(BaseModel):
    code:       Optional[str]  = Field(None, description="Dify から送る銘柄コード（例: 1605）")
    ticker:     Optional[str]  = Field(None, description="銘柄コード（code の別名）")
    mode:       Optional[str]  = Field(None, description="リスクモード（堅実 / 標準 / 積極）")
    full_scan:  Optional[bool] = Field(False, description="true のときのみ JPX400 一括スキャンを実行")


class WatchlistPayload(BaseModel):
    tickers: List[str] = Field(default_factory=list, description="ウォッチリスト銘柄コード一覧")


class MarketScanPayload(BaseModel):
    mode: Optional[str] = Field("堅実", description="リスクモード（堅実 / 標準 / 積極）")


class DifyChatPayload(BaseModel):
    query:            str = Field(..., description="Dify へ送るユーザー入力（銘柄コードのみ。例: 7203 / 1605,7203）")
    code:             Optional[str] = Field(
        None,
        description="銘柄コード（inputs.code へ渡す。query と同値でよい。例: 7203 / 1605,7203）",
    )
    mode:             Optional[str] = Field(
        None,
        description="リスクモード（inputs.mode へ渡す。堅実 / 標準 / 積極）",
    )
    conversation_id:  Optional[str] = Field(None, description="会話を継続する場合の ID")


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
        return f.read()


# ── 設定 API ──────────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    """現在の設定一式を返す。"""
    try:
        cfg = read_config_yaml()
        cfg["line_connected"]  = Config("config.yaml").validate_line_credentials()
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
    """ウォッチリスト対象の即時スクリーニングを実行して LINE に通知する。"""
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

        sent_line = False
        message   = ""
        if buy_signals:
            notifier  = LineNotifier(config.line_token, config.line_user_id)
            message   = notifier.build_buy_signal_message(buy_signals)
            if config.validate_line_credentials():
                sent_line = notifier.send_notification(message)
        elif matched:
            notifier = LineNotifier(config.line_token, config.line_user_id)
            message  = notifier.build_message(matched)

        return {
            "status":          "success",
            "matched_count":   len(matched),
            "buy_signal_count": len(buy_signals),
            "matched_results": matched,
            "sent_line":       sent_line,
            "message_preview": message,
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
    decorated = _decorate_buy_signal_rows(buy_signals)

    sent_line = False
    if buy_signals:
        try:
            cfg = Config("config.yaml")
            if cfg.validate_line_credentials():
                notifier = LineNotifier(cfg.line_token, cfg.line_user_id)
                message = notifier.build_buy_signal_message(buy_signals)
                sent_line = notifier.send_notification(message)
        except Exception as line_err:
            logger.warning(f"LINE 通知スキップ: {line_err}")

    storage.complete_session(
        scan_id=scan_id,
        buy_signal_count=len(buy_signals),
        sent_line=sent_line,
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
        "sent_line":       sent_line,
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


# ── Dify ボット連携 API ───────────────────────────────────────────────────
def _normalize_ticker_code(raw: str) -> str:
    """4桁コード・1605.T などを Yahoo Finance 用ティッカーに正規化する。"""
    code = raw.strip()
    if not code:
        raise HTTPException(status_code=422, detail="銘柄コードが空です。")
    if code.endswith(".T"):
        return code
    return f"{code}.T"


def _compute_technical_metrics(df) -> Dict[str, float]:
    """診断レポート表示用の補助指標を算出する。"""
    latest = df.iloc[-1]
    close_p = float(latest["Close"])
    ma25 = df["Close"].rolling(window=25).mean().iloc[-1]
    ma25_divergence = ((close_p - ma25) / ma25) * 100 if ma25 else 0.0
    avg_volume_5d = df["Volume"].iloc[-6:-1].mean()
    current_volume = float(df["Volume"].iloc[-1])
    volume_ratio = current_volume / avg_volume_5d if avg_volume_5d > 0 else 1.0
    return {
        "ma25_divergence_pct": round(float(ma25_divergence), 2),
        "volume_ratio":        round(float(volume_ratio), 2),
    }


def _diagnose_ticker(raw_code: str) -> Dict[str, Any]:
    """株価取得 → テクニカル分析 → Dify 向けレスポンスを組み立てる。"""
    ticker_code = raw_code.strip()
    yahoo_ticker = _normalize_ticker_code(ticker_code)

    fetcher = DataFetcher(delay_seconds=0.2, history_period="6mo")
    df, company_name = fetcher.fetch_ticker_data(yahoo_ticker)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"銘柄 {ticker_code} のデータが取得できませんでした。",
        )

    name = company_name or f"銘柄コード:{ticker_code}"
    evaluator = StrategyEvaluator(read_config_yaml())
    result = evaluator.evaluate(yahoo_ticker, name, df)
    metrics = _compute_technical_metrics(df)

    display_code = ticker_code.removesuffix(".T")
    return {
        "status":              "success",
        "code":                display_code,
        "ticker":              display_code,
        "name":                name,
        "current_price":       result.get("close_price"),
        "rsi":                 result.get("rsi"),
        "ma25_divergence_pct": metrics["ma25_divergence_pct"],
        "volume_ratio":        metrics["volume_ratio"],
        "buy_signal":          result.get("buy_signal", False),
        "reason":              result.get("reason"),
        "preset_matched":      result.get("preset_matched"),
        "sector":              result.get("sector"),
        "trend_status":        _derive_trend_status(result),
    }


def _apply_risk_mode(evaluator: StrategyEvaluator, mode_config: Dict[str, float]) -> None:
    """RISK_MODES の値を既存 StrategyEvaluator.settings に反映する。"""
    evaluator.settings["rsi_oshieme_max"] = mode_config["oshieme_rsi_limit"]
    evaluator.settings["volume_growth_ratio"] = mode_config["volume_spike_threshold"]
    evaluator.settings["max_ma25_divergence"] = mode_config["ma25_divergence_cap"]


def _derive_trend_status(result: Dict[str, Any]) -> str:
    """evaluate() 結果から Dify 向けトレンド判定ラベルを導出する。"""
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


def _fast_dummy_screen_response(code: str, mode: Optional[str] = None) -> Dict[str, Any]:
    """Dify タイムアウト回避用: 株価取得なしで即時ダミー判定を返す。"""
    display_code = code.strip().removesuffix(".T")
    mode_label = mode or "堅実"
    return {
        "status":              "success",
        "fast_response":       True,
        "mode":                mode_label,
        "code":                display_code,
        "ticker":              display_code,
        "name":                f"銘柄コード:{display_code}",
        "current_price":       None,
        "rsi":                 20.0,
        "ma25_divergence_pct": -5.0,
        "volume_ratio":        2.0,
        "buy_signal":          True,
        "reason":              f"【高速応答・{mode_label}】RSI 20 / 25日線乖離率 -5% / 出来高 2.0倍（Difyタイムアウト回避用）",
        "preset_matched":      "oshieme",
        "sector":              "要確認",
        "trend_status":        "HOLD",
    }


def _fast_dummy_mode_response(mode: str) -> Dict[str, Any]:
    """Dify が mode のみ送った場合の即時ダミー応答。"""
    safe_mode = mode if mode in RISK_MODES else "堅実"
    return {
        "status":        "success",
        "fast_response": True,
        "mode":          safe_mode,
        "message":       f"【{safe_mode}】モードの高速応答（ダミーデータ）",
        "rsi":           20.0,
        "ma25_divergence_pct": -5.0,
        "volume_ratio":  2.0,
        "buy_signal":    True,
        "trend_status":  "HOLD",
        "stocks":        [],
    }


@app.post("/api/v1/screen")
async def execute_screener(payload: ScreenPayload):
    """
    Dify / フロントエンド用スクリーニング API。
    - code 指定時: 即時ダミー応答（最優先・1秒以内）
    - ticker のみ指定時: 単一銘柄の実診断
    - mode のみ指定時: JPX400 一括非同期スキャン
    """
    # 最優先: JSON に code が含まれる場合
    if payload.code is not None:
        code = payload.code.strip()
        if code:
            normalized = _normalize_stock_codes_param(code)
            if normalized and "," in normalized:
                logger.info(f"/api/v1/screen 複数銘柄実診断: codes={normalized}")
                stocks = []
                for single_code in normalized.split(","):
                    try:
                        stocks.append(
                            await asyncio.to_thread(_diagnose_ticker, single_code)
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
            logger.info(f"/api/v1/screen 高速ダミー応答: code={code}")
            return _fast_dummy_screen_response(code)

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
        logger.info(f"/api/v1/screen 高速ダミー応答: mode={selected_mode}")
        return _fast_dummy_mode_response(selected_mode)

    try:
        return await _execute_bulk_screener(selected_mode)
    except Exception as e:
        logger.exception(f"/api/v1/screen 一括スキャンエラー (mode={selected_mode}): {e}")
        raise HTTPException(status_code=500, detail=f"一括スクリーニング中にエラーが発生しました: {e}")


@app.post("/api/bot/diagnose")
def diagnose_ticker_for_bot(payload: TickerDiagnosisPayload):
    """Dify の AI ボットから呼び出され、特定銘柄のテクニカル分析結果を返す。"""
    try:
        return _diagnose_ticker(payload.ticker)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"/api/bot/diagnose エラー (ticker={payload.ticker}): {e}")
        raise HTTPException(status_code=500, detail=f"銘柄診断中にエラーが発生しました: {e}")


# ── Dify Chat-App API プロキシ（APIキーをサーバー側で保持） ───────────────
_DIFY_STUB_MARKERS = (
    "スクリーナーAPIとの連携を準備中",
    "APIとの連携を準備中",
    "連携を準備中",
)


def _extract_single_ticker_from_query(query: str) -> Optional[str]:
    """単一銘柄の診断依頼かどうかを判定し、4桁コードを返す。"""
    import re

    q = query.strip()
    if not q:
        return None
    # ウォッチリスト一括スクリーニングは Dify 側のフローに任せる
    if "ウォッチリスト" in q and "スクリーニング診断" in q:
        return None

    if re.fullmatch(r"\d{4}", q):
        return q
    if re.fullmatch(r"\d{4}\.T", q, re.IGNORECASE):
        return q[:4]

    code_match = re.search(r"(\d{4})\.T", q, re.IGNORECASE) or re.search(r"(\d{4})", q)
    if code_match:
        return code_match.group(1)
    return None


def _is_dify_stub_answer(answer: str) -> bool:
    """Dify が未対応銘柄向けに返すプレースホルダー応答か判定する。"""
    if not answer:
        return False
    return any(marker in answer for marker in _DIFY_STUB_MARKERS)


def _parse_query_for_screen(query: str) -> Dict[str, Any]:
    """チャット入力から銘柄コードまたはリスクモードを推定する。"""
    import re
    q = query.strip()
    if q in RISK_MODES:
        return {"mode": q}
    for mode in RISK_MODES:
        if mode in q:
            code_match = re.search(r"(\d{4})\.T", q) or re.search(r"(\d{4})", q)
            if code_match:
                return {"mode": mode, "code": code_match.group(1)}
            return {"mode": mode}
    code_match = re.search(r"\d{4}", q)
    if code_match:
        return {"code": code_match.group()}
    return {"mode": "堅実"}


def _local_screen_from_query(query: str) -> Dict[str, Any]:
    """ローカル FastAPI の診断ロジックを直接実行する。"""
    ticker_code = _extract_single_ticker_from_query(query)
    if ticker_code:
        try:
            return _diagnose_ticker(ticker_code)
        except HTTPException:
            parsed = _parse_query_for_screen(query)
            if "code" in parsed:
                return _fast_dummy_screen_response(parsed["code"], parsed.get("mode"))

    parsed = _parse_query_for_screen(query)
    if "code" in parsed:
        try:
            return _diagnose_ticker(parsed["code"])
        except HTTPException:
            return _fast_dummy_screen_response(parsed["code"], parsed.get("mode"))
    return _fast_dummy_mode_response(parsed.get("mode", "堅実"))


def _format_rsi(value: Any) -> str:
    """診断レポート表示用に RSI を小数点第2位で丸める。"""
    if value is None:
        return "—"
    try:
        return f"{round(float(value), 2):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_stellar_answer(screen_data: Dict[str, Any], query: str) -> str:
    """スクリーニング結果を STELLAR 診断テキストに整形する。"""
    lines = [
        "━━ STELLAR SCREENER 診断レポート ━━",
        "",
        f"入力: {query}",
        "",
    ]
    if screen_data.get("code"):
        price = screen_data.get("current_price")
        price_line = f"終値: {price}円" if price is not None else "終値: 取得中"
        lines += [
            f"銘柄コード: {screen_data.get('code')}",
            f"銘柄名: {screen_data.get('name', '不明')}",
            price_line,
            f"RSI: {_format_rsi(screen_data.get('rsi'))}",
            f"25日線乖離率: {screen_data.get('ma25_divergence_pct')}%",
            f"出来高倍率: {screen_data.get('volume_ratio')}倍",
            f"BUY SIGNAL: {'あり' if screen_data.get('buy_signal') else 'なし'}",
            f"トレンド判定: {screen_data.get('trend_status', 'WAIT')}",
            f"診断コメント: {screen_data.get('reason', '')}",
        ]
    else:
        mode = screen_data.get("mode", "堅実")
        lines += [
            f"リスクモード: {mode}",
            f"RSI: {_format_rsi(screen_data.get('rsi'))}",
            f"25日線乖離率: {screen_data.get('ma25_divergence_pct')}%",
            f"出来高倍率: {screen_data.get('volume_ratio')}倍",
            f"メッセージ: {screen_data.get('message', '')}",
        ]
    if screen_data.get("fast_response"):
        lines += ["", "※ ローカル高速診断（Dify ワークフロー不通時のフォールバック）"]
    elif screen_data.get("local_diagnosis"):
        lines += ["", "※ ローカル実データ診断（Dify 未対応銘柄をバックエンドで直接分析）"]
    return "\n".join(lines)


def _normalize_stock_codes_param(raw: Optional[str]) -> Optional[str]:
    """'7203', '7203.T', '7203,1605.T' などを Dify / API 向け 4 桁コードに正規化する。"""
    import re

    if not raw or not raw.strip():
        return None

    codes: List[str] = []
    for part in raw.split(","):
        token = part.strip().upper().removesuffix(".T")
        if re.fullmatch(r"\d{4}", token) and token not in codes:
            codes.append(token)
    return ",".join(codes) if codes else None


def _resolve_dify_code(explicit_code: Optional[str], query: str) -> Optional[str]:
    """フロントから渡された code を優先し、なければ query から推定する。"""
    normalized = _normalize_stock_codes_param(explicit_code)
    if normalized:
        return normalized
    normalized = _normalize_stock_codes_param(query)
    if normalized:
        return normalized
    return _normalize_stock_codes_param(_extract_single_ticker_from_query(query))


def _split_stock_codes(raw: Optional[str]) -> List[str]:
    """カンマ区切り銘柄コードをリストに分解する。"""
    normalized = _normalize_stock_codes_param(raw)
    if not normalized:
        return []
    return normalized.split(",")


def _build_dify_inputs(code: Optional[str], mode: Optional[str] = None) -> Dict[str, str]:
    """Dify Chat-App API の inputs（ワークフロー変数 code / mode）を組み立てる。"""
    inputs: Dict[str, str] = {}
    normalized = _normalize_stock_codes_param(code)
    if normalized:
        inputs["code"] = normalized
    if mode and mode in RISK_MODES:
        inputs["mode"] = mode
    return inputs


def _call_dify_chat_api(
    query: str,
    conversation_id: Optional[str] = None,
    *,
    code: Optional[str] = None,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Dify Chat-App API (blocking) を呼び出す。"""
    if not DIFY_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="DIFY_API_KEY が未設定です。.env に API キーを設定してください。",
        )

    inputs = _build_dify_inputs(code, mode)
    body: Dict[str, Any] = {
        "inputs": inputs,
        "query": query,
        "response_mode": "blocking",
        "user": "stellar-screener-ui",
    }
    if conversation_id:
        body["conversation_id"] = conversation_id

    logger.info(
        "Dify API 送信: "
        f"query={query!r} code={inputs.get('code', '(なし)')} mode={inputs.get('mode', '(なし)')}"
    )

    try:
        resp = requests.post(
            f"{DIFY_BASE_URL}/chat-messages",
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=120,
        )
    except requests.RequestException as e:
        logger.error(f"Dify API 接続エラー: {e}")
        raise HTTPException(status_code=502, detail=f"Dify API への接続に失敗しました: {e}")

    if resp.status_code >= 400:
        logger.error(f"Dify API エラー {resp.status_code}: {resp.text}")
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Dify API エラー: {resp.text}",
        )

    data = resp.json()
    return {
        "answer":          data.get("answer", ""),
        "conversation_id": data.get("conversation_id"),
        "message_id":      data.get("message_id"),
    }


async def _build_local_multi_diagnosis_response(
    query: str,
    codes: List[str],
    conversation_id: Optional[str],
    *,
    fallback: bool = False,
) -> Dict[str, Any]:
    """複数銘柄の実データ診断結果をチャット応答形式で返す。"""
    sections: List[str] = []
    for ticker_code in codes:
        screen_data = await asyncio.to_thread(_diagnose_ticker, ticker_code)
        screen_data["local_diagnosis"] = True
        sections.append(_format_stellar_answer(screen_data, ticker_code))

    return {
        "answer":          "\n\n".join(sections),
        "conversation_id": conversation_id,
        "fallback":        fallback,
    }


async def _build_local_diagnosis_response(
    query: str,
    ticker_code: str,
    conversation_id: Optional[str],
    *,
    fallback: bool = False,
) -> Dict[str, Any]:
    """単一銘柄の実データ診断結果をチャット応答形式で返す。"""
    screen_data = await asyncio.to_thread(_diagnose_ticker, ticker_code)
    screen_data["local_diagnosis"] = True
    return {
        "answer":          _format_stellar_answer(screen_data, query),
        "conversation_id": conversation_id,
        "fallback":        fallback,
    }


@app.post("/api/dify/chat")
async def dify_chat_proxy(payload: DifyChatPayload):
    """フロントエンドから Dify Chat-App API へ中継する。"""
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query が空です。")

    dify_code = _resolve_dify_code(payload.code, query)
    dify_mode = payload.mode if payload.mode in RISK_MODES else None
    logger.info(
        f"Dify チャット送信: query={query!r} code={dify_code or '(なし)'} mode={dify_mode or '(なし)'}"
    )

    try:
        dify_result = await asyncio.to_thread(
            _call_dify_chat_api,
            query,
            payload.conversation_id,
            code=dify_code,
            mode=dify_mode,
        )
        answer = dify_result.get("answer", "")
        if _is_dify_stub_answer(answer) and dify_code:
            codes = _split_stock_codes(dify_code)
            logger.warning(
                f"Dify 未対応応答を検知 → ローカル実診断に切替: codes={','.join(codes)}"
            )
            if len(codes) > 1:
                return await _build_local_multi_diagnosis_response(
                    query,
                    codes,
                    dify_result.get("conversation_id"),
                    fallback=True,
                )
            return await _build_local_diagnosis_response(
                query,
                codes[0],
                dify_result.get("conversation_id"),
                fallback=True,
            )
        return dify_result
    except HTTPException as exc:
        # Dify ワークフロー内 HTTP ノード（ngrok 不通等）で失敗した場合のフォールバック
        logger.warning(f"Dify 失敗 → ローカル診断にフォールバック: {exc.detail}")
        codes = _split_stock_codes(dify_code) or _split_stock_codes(query)
        if len(codes) > 1:
            return await _build_local_multi_diagnosis_response(
                query,
                codes,
                payload.conversation_id,
                fallback=True,
            )
        if len(codes) == 1:
            return await _build_local_diagnosis_response(
                query,
                codes[0],
                payload.conversation_id,
                fallback=True,
            )
        screen_data = _local_screen_from_query(query)
        return {
            "answer":          _format_stellar_answer(screen_data, query),
            "conversation_id": payload.conversation_id,
            "fallback":        True,
        }


@app.get("/api/dify/status")
def dify_status():
    """Dify 連携の設定状態を返す（APIキー本体は返さない）。"""
    return {
        "configured": bool(DIFY_API_KEY),
        "base_url":   DIFY_BASE_URL,
    }


# ── ヘルスチェック ────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    """Render / UptimeRobot 等のヘルスチェック用。"""
    cfg = Config("config.yaml")
    return {
        "status":          "ok",
        "platform":        "render" if IS_RENDER else ("vercel" if IS_VERCEL else "local"),
        "line_connected":  cfg.validate_line_credentials(),
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