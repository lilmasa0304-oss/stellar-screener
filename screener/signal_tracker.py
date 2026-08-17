"""BUY SIGNAL 追跡（最大10営業日・3/5/10日目の成績集計）。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from screener.jp_business_days import (
    add_jp_business_days,
    is_jp_business_day,
    next_jp_business_day,
    today_jst,
)
from screener import storage
from screener.yahoo_session import create_yfinance_ticker

logger = logging.getLogger(__name__)

TRACKING_HORIZONS = (3, 5, 10)
HORIZON_LABELS = {
    3: "3日目",
    5: "5日目（1週間）",
    10: "10日目（2週間）",
}
MAX_TRACKING_BUSINESS_DAYS = 10


def register_track_from_scan(
    scan_id: str,
    ev: Dict[str, Any],
    *,
    risk_mode: Optional[str] = None,
    scan_result_id: Optional[int] = None,
) -> Optional[int]:
    """BUY SIGNAL 銘柄を追跡登録する。"""
    if not ev.get("buy_signal"):
        return None
    entry_price = ev.get("current_price") or ev.get("close_price")
    if entry_price is None:
        return None
    signal_date = today_jst()
    preset = ev.get("preset_matched")
    if preset in (None, "", "none"):
        preset = None
    return storage.register_signal_track(
        scan_id=scan_id,
        scan_result_id=scan_result_id,
        ticker=ev["ticker"],
        name=ev.get("name") or ev["ticker"],
        signal_date=signal_date.isoformat(),
        entry_price=float(entry_price),
        preset_matched=preset,
        risk_mode=risk_mode,
    )


def _fetch_history_df(ticker: str, start: date, end: date) -> pd.DataFrame:
    symbol = ticker.strip().upper()
    if not symbol.endswith(".T") and symbol[:-1].isdigit():
        symbol = f"{symbol}.T"
    fetch_end = end + timedelta(days=5)
    df = create_yfinance_ticker(symbol).history(
        start=start.isoformat(),
        end=fetch_end.isoformat(),
        interval="1d",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()


def _window_metrics(df: pd.DataFrame, entry_price: float, eval_date: date) -> Optional[Dict[str, float]]:
    if df.empty or entry_price <= 0:
        return None
    ts_eval = pd.Timestamp(eval_date)
    available = df[df.index <= ts_eval]
    if available.empty:
        return None
    exit_row = available.iloc[-1]
    exit_price = float(exit_row["Close"])
    high_max = float(available["High"].max())
    return_pct = ((exit_price - entry_price) / entry_price) * 100.0
    max_return_pct = ((high_max - entry_price) / entry_price) * 100.0
    return {
        "exit_price": exit_price,
        "return_pct": round(return_pct, 4),
        "max_return_pct": round(max_return_pct, 4),
        "is_win": return_pct > 0,
    }


def evaluate_track(track: Dict[str, Any]) -> int:
    """1件の追跡レコードについて到達可能な horizon を評価する。"""
    signal_date = date.fromisoformat(track["signal_date"])
    entry_price = float(track["entry_price"])
    ticker = track["ticker"]
    today = today_jst()
    updated = 0

    try:
        history_start = next_jp_business_day(signal_date)
        history_end = min(today, add_jp_business_days(signal_date, MAX_TRACKING_BUSINESS_DAYS))
        df = _fetch_history_df(ticker, history_start, history_end)
    except Exception as exc:
        logger.warning("追跡評価: 株価取得失敗 (%s): %s", ticker, exc)
        df = pd.DataFrame()

    for horizon in TRACKING_HORIZONS:
        existing = storage.get_track_outcome(track["track_id"], horizon)
        if existing and existing.get("status") == "complete":
            continue

        eval_date = add_jp_business_days(signal_date, horizon)
        if today < eval_date:
            storage.upsert_track_outcome(
                track_id=track["track_id"],
                horizon_days=horizon,
                horizon_label=HORIZON_LABELS[horizon],
                eval_date=eval_date.isoformat(),
                status="pending",
            )
            continue

        metrics = _window_metrics(df, entry_price, eval_date)
        if metrics is None:
            storage.upsert_track_outcome(
                track_id=track["track_id"],
                horizon_days=horizon,
                horizon_label=HORIZON_LABELS[horizon],
                eval_date=eval_date.isoformat(),
                status="insufficient_data",
            )
            continue

        storage.upsert_track_outcome(
            track_id=track["track_id"],
            horizon_days=horizon,
            horizon_label=HORIZON_LABELS[horizon],
            eval_date=eval_date.isoformat(),
            exit_price=metrics["exit_price"],
            return_pct=metrics["return_pct"],
            max_return_pct=metrics["max_return_pct"],
            is_win=metrics["is_win"],
            status="complete",
        )
        updated += 1

    if today >= add_jp_business_days(signal_date, MAX_TRACKING_BUSINESS_DAYS):
        storage.mark_track_completed(track["track_id"])

    return updated


def evaluate_pending_tracks(limit: int = 100) -> Dict[str, int]:
    """未評価の追跡レコードを評価する。"""
    tracks = storage.list_active_signal_tracks(limit=limit)
    updated = 0
    for track in tracks:
        updated += evaluate_track(track)
    return {"tracks_checked": len(tracks), "outcomes_updated": updated}


def _aggregate_horizon(outcomes: List[Dict[str, Any]], horizon: int) -> Dict[str, Any]:
    rows = [o for o in outcomes if o["horizon_days"] == horizon]
    completed = [r for r in rows if r.get("status") == "complete"]
    pending = [r for r in rows if r.get("status") == "pending"]
    insufficient = [r for r in rows if r.get("status") == "insufficient_data"]

    if not completed:
        return {
            "horizon_days": horizon,
            "label": HORIZON_LABELS[horizon],
            "registered_count": len(rows),
            "evaluated_count": 0,
            "pending_count": len(pending),
            "insufficient_data_count": len(insufficient),
            "win_rate_pct": None,
            "avg_return_pct": None,
            "max_return_achievement_rate_pct": None,
        }

    wins = sum(1 for r in completed if r.get("is_win"))
    avg_return = sum(float(r["return_pct"]) for r in completed) / len(completed)
    avg_max_return = sum(float(r["max_return_pct"]) for r in completed) / len(completed)
    max_positive_rate = (
        sum(1 for r in completed if float(r.get("max_return_pct") or 0) > 0) / len(completed) * 100.0
    )

    return {
        "horizon_days": horizon,
        "label": HORIZON_LABELS[horizon],
        "registered_count": len(rows),
        "evaluated_count": len(completed),
        "pending_count": len(pending),
        "insufficient_data_count": len(insufficient),
        "win_rate_pct": round(wins / len(completed) * 100.0, 2),
        "avg_return_pct": round(avg_return, 2),
        "max_return_achievement_rate_pct": round(avg_max_return, 2),
        "max_return_positive_rate_pct": round(max_positive_rate, 2),
    }


def build_tracking_summary(
    *,
    risk_mode: Optional[str] = None,
    preset_matched: Optional[str] = None,
    auto_evaluate: bool = True,
) -> Dict[str, Any]:
    """3/5/10日目の勝率・平均損益率・最高益達成率を比較集計する。"""
    if auto_evaluate:
        evaluate_pending_tracks()

    outcomes = storage.list_track_outcomes(
        risk_mode=risk_mode,
        preset_matched=preset_matched,
    )
    horizons = [_aggregate_horizon(outcomes, h) for h in TRACKING_HORIZONS]
    total_registered = storage.count_signal_tracks(
        risk_mode=risk_mode,
        preset_matched=preset_matched,
    )

    return {
        "status": "success",
        "tracking_period_business_days": MAX_TRACKING_BUSINESS_DAYS,
        "checkpoints": ["3日目", "5日目（1週間）", "10日目（2週間）"],
        "total_registered": total_registered,
        "filters": {
            "risk_mode": risk_mode,
            "preset_matched": preset_matched,
        },
        "horizons": horizons,
        "notes": {
            "win_rate_pct": "各時点の終値ベース損益がプラスの比率",
            "avg_return_pct": "各時点の平均損益率（終値ベース）",
            "max_return_achievement_rate_pct": "登録から各時点までの最高値ベース平均最高益率",
        },
    }
