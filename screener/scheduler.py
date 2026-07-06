"""
APScheduler を使った定時スキャンスケジューラー。

・平日 16:00 JST（07:00 UTC）に JPX400 全銘柄を自動スキャン
・結果は SQLite に保存（UI は手動スキャンでその場表示）
・FastAPI の lifespan イベントで起動/停止する
"""

import logging
import uuid
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)

# グローバルスケジューラーインスタンス
_scheduler: BackgroundScheduler | None = None


# ─────────────────────────────────────────────────────────────────────────────
def _run_jpx400_scan_job():
    """
    定時スキャンジョブ本体。
    web_app.py の _do_jpx400_scan() を呼び出す形で実装。
    循環インポートを避けるため、実行時にインポートする。
    """
    from screener.jpx400 import get_jpx400_tickers
    from screener.data_fetcher import DataFetcher
    from screener.strategy import StrategyEvaluator
    from screener.config import Config
    from screener import storage

    scan_id = f"sched_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    logger.info(f"[Scheduler] 定時スキャン開始: scan_id={scan_id}")

    tickers = get_jpx400_tickers()
    storage.create_session(scan_id=scan_id, scan_type="scheduled", total_tickers=len(tickers))

    try:
        config   = Config("config.yaml")
        fetcher  = DataFetcher(delay_seconds=0.5, history_period="6mo")
        evaluator = StrategyEvaluator(config.data)

        buy_signals = []
        processed   = 0

        for ticker in tickers:
            try:
                df, name = fetcher.fetch_ticker_data(ticker)
                if df is None or df.empty:
                    processed += 1
                    continue

                ev = evaluator.evaluate(ticker, name or ticker, df)

                # float 変換（JSON/SQLite 安全化）
                for k in ("current_price", "change_percent", "rsi",
                          "ma5", "ma25", "ma75", "bb_upper", "bb_lower",
                          "ma25_deviation_pct", "macd", "macd_signal", "macd_hist"):
                    if ev.get(k) is not None:
                        ev[k] = float(ev[k])

                storage.save_result(scan_id, ev)

                if ev["buy_signal"]:
                    buy_signals.append(ev)
                    logger.info(
                        f"[Scheduler] BUY SIGNAL: {ev['name']} ({ticker}) "
                        f"RSI={ev['rsi']} MACD={'GC' if ev['macd_crossover'] else 'GC接近'}"
                    )

                processed += 1
                if processed % 20 == 0:
                    storage.update_session_progress(scan_id, processed)
                    logger.info(f"[Scheduler] 進捗: {processed}/{len(tickers)}")

            except Exception as ticker_err:
                logger.error(f"[Scheduler] {ticker} 評価エラー: {ticker_err}")
                processed += 1

        if buy_signals:
            logger.info(f"[Scheduler] BUY SIGNAL {len(buy_signals)} 件を検出。")
        else:
            logger.info("[Scheduler] BUY SIGNAL なし。")

        storage.complete_session(
            scan_id=scan_id,
            buy_signal_count=len(buy_signals),
            sent_line=False,
        )
        logger.info(
            f"[Scheduler] 定時スキャン完了: "
            f"{len(buy_signals)} BUY SIGNAL / {processed} 処理済み"
        )

    except Exception as e:
        logger.exception(f"[Scheduler] 致命的エラー: {e}")
        storage.complete_session(
            scan_id=scan_id,
            buy_signal_count=0,
            sent_line=False,
            error_message=str(e),
        )


# ─────────────────────────────────────────────────────────────────────────────
def start_scheduler(
    hour: int = 7,
    minute: int = 0,
    timezone: str = "UTC",
) -> BackgroundScheduler:
    """
    バックグラウンドスケジューラーを起動する。

    デフォルト: 毎平日 07:00 UTC = 16:00 JST

    Args:
        hour:     実行時刻（時）
        minute:   実行時刻（分）
        timezone: タイムゾーン文字列（例: "UTC", "Asia/Tokyo"）

    Returns:
        起動済みの BackgroundScheduler インスタンス
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.warning("[Scheduler] すでに起動済みです。")
        return _scheduler

    tz = pytz.timezone(timezone)

    _scheduler = BackgroundScheduler(timezone=tz)
    _scheduler.add_job(
        _run_jpx400_scan_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            timezone=tz,
        ),
        id="daily_jpx400_scan",
        name="JPX400 日次スキャン",
        replace_existing=True,
        misfire_grace_time=3600,  # 1時間以内なら遅延起動を許容
    )

    _scheduler.start()
    logger.info(
        f"[Scheduler] 起動完了。毎平日 {hour:02d}:{minute:02d} {timezone} に JPX400 スキャンを実行します。"
    )
    return _scheduler


def stop_scheduler() -> None:
    """スケジューラーを停止する。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] 停止しました。")


def get_next_run_time() -> str | None:
    """次回実行予定時刻（ISO 文字列）を返す。"""
    global _scheduler
    if not _scheduler or not _scheduler.running:
        return None
    job = _scheduler.get_job("daily_jpx400_scan")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def trigger_now() -> None:
    """スケジュールされたジョブを即時実行する（テスト用）。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.get_job("daily_jpx400_scan").modify(next_run_time=datetime.now(pytz.utc))
        logger.info("[Scheduler] 即時実行をトリガーしました。")
