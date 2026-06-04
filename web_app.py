import uuid
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from screener.config import Config
from screener.data_fetcher import DataFetcher
from screener.strategy import StrategyEvaluator
from screener.notifier import LineNotifier
from screener import storage
from screener.scheduler import start_scheduler, stop_scheduler, get_next_run_time

# ── ロギング設定 ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── JPX400 スキャン進捗（インメモリ、セッションIDで DB と紐付け） ─────────────
_jpx400_progress: Dict[str, Any] = {
    "status":    "idle",   # idle | running | completed | failed
    "scan_id":   None,
    "processed": 0,
    "total":     0,
    "buy_count": 0,
    "error":     None,
}


# ── FastAPI lifespan（起動/終了フック） ───────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時
    storage.init_db()
    logger.info("SQLite DB を初期化しました。")

    # スケジューラー起動（平日 07:00 UTC = 16:00 JST）
    start_scheduler(hour=7, minute=0, timezone="UTC")

    yield

    # 終了時
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


# ── ウォッチリスト API ────────────────────────────────────────────────────
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


# ── JPX400 バックグラウンドスキャン ──────────────────────────────────────

def _do_jpx400_scan(scan_id: str):
    """
    JPX400 全銘柄をバックグラウンドでスキャンし、結果を SQLite に保存する。
    BUY SIGNAL が出た銘柄は LINE に通知する。
    """
    from screener.jpx400 import get_jpx400_tickers

    global _jpx400_progress

    tickers = get_jpx400_tickers()
    _jpx400_progress.update({
        "status":    "running",
        "scan_id":   scan_id,
        "processed": 0,
        "total":     len(tickers),
        "buy_count": 0,
        "error":     None,
    })
    storage.create_session(scan_id=scan_id, scan_type="manual", total_tickers=len(tickers))

    try:
        config    = Config("config.yaml")
        fetcher   = DataFetcher(delay_seconds=0.5, history_period="6mo")
        evaluator = StrategyEvaluator(config.data)
        buy_signals = []

        for i, ticker in enumerate(tickers):
            try:
                df, name = fetcher.fetch_ticker_data(ticker)
                if df is None or df.empty:
                    _jpx400_progress["processed"] = i + 1
                    continue

                ev = evaluator.evaluate(ticker, name or ticker, df)

                for k in ("current_price", "change_percent", "rsi",
                          "ma5", "ma25", "ma75", "bb_upper", "bb_lower",
                          "ma25_deviation_pct", "macd", "macd_signal", "macd_hist"):
                    if ev.get(k) is not None:
                        ev[k] = float(ev[k])

                storage.save_result(scan_id, ev)

                if ev["buy_signal"]:
                    buy_signals.append(ev)
                    _jpx400_progress["buy_count"] = len(buy_signals)

            except Exception as e:
                logger.error(f"[JPX400 scan] {ticker} エラー: {e}")

            _jpx400_progress["processed"] = i + 1
            if (i + 1) % 20 == 0:
                storage.update_session_progress(scan_id, i + 1)

        # LINE 通知
        sent_line = False
        if buy_signals and config.validate_line_credentials():
            notifier  = LineNotifier(config.line_token, config.line_user_id)
            message   = notifier.build_buy_signal_message(buy_signals)
            sent_line = notifier.send_notification(message)

        storage.complete_session(
            scan_id=scan_id,
            buy_signal_count=len(buy_signals),
            sent_line=sent_line,
        )
        _jpx400_progress["status"] = "completed"
        logger.info(
            f"[JPX400 scan] 完了: BUY SIGNAL {len(buy_signals)}件 / "
            f"{len(tickers)}銘柄処理"
        )

    except Exception as e:
        logger.exception(f"[JPX400 scan] 致命的エラー: {e}")
        storage.complete_session(
            scan_id=scan_id,
            buy_signal_count=0,
            sent_line=False,
            error_message=str(e),
        )
        _jpx400_progress.update({"status": "failed", "error": str(e)})


@app.post("/api/jpx400/scan")
def start_jpx400_scan(background_tasks: BackgroundTasks):
    """JPX400 全銘柄のバックグラウンドスキャンを開始する。"""
    if _jpx400_progress["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail=f"スキャンがすでに実行中です。"
                   f"（{_jpx400_progress['processed']}/{_jpx400_progress['total']} 処理済み）",
        )
    from screener.jpx400 import get_jpx400_count
    scan_id = f"manual_{__import__('datetime').datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    background_tasks.add_task(_do_jpx400_scan, scan_id)
    return {
        "status":  "started",
        "scan_id": scan_id,
        "total_tickers": get_jpx400_count(),
        "message": "JPX400 スキャンをバックグラウンドで開始しました。",
    }


@app.get("/api/jpx400/status")
def get_jpx400_status():
    """現在のJPX400スキャン進捗と最新結果を返す。"""
    prog  = dict(_jpx400_progress)
    scan_id = prog.get("scan_id")

    results     = []
    session_info = None

    if scan_id:
        session_info = storage.get_session(scan_id)
        if prog["status"] == "completed":
            results = storage.get_results(scan_id, buy_signal_only=True)

    return {
        **prog,
        "session":       session_info,
        "buy_signals":   results,
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


# ── ヘルスチェック ────────────────────────────────────────────────────────
@app.get("/health")
def health_check():
    """Render / UptimeRobot 等のヘルスチェック用。"""
    return {
        "status":         "ok",
        "next_scan_time": get_next_run_time(),
        "db_path":        str(storage.DB_PATH.resolve()),
    }
