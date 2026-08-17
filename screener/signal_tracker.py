"""BUY SIGNAL フォワードテスト追跡（最大10営業日・3/5/10日目の成績集計）。"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from screener.jp_business_days import (
    add_jp_business_days,
    business_days_between,
    next_jp_business_day,
    parse_signal_date,
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
RISK_MODES = ("堅実", "標準", "積極")


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


def register_manual_track(
    *,
    ticker: str,
    name: Optional[str] = None,
    entry_price: float,
    risk_mode: Optional[str] = None,
    preset_matched: Optional[str] = None,
) -> Optional[int]:
    """スキャン結果画面から手動で検証リストへ登録する。"""
    scan_id = f"manual_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    ev = {
        "ticker": ticker,
        "name": name or ticker,
        "current_price": entry_price,
        "buy_signal": True,
        "preset_matched": preset_matched,
    }
    return register_track_from_scan(scan_id, ev, risk_mode=risk_mode)


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
    low_min = float(available["Low"].min())
    return_pct = ((exit_price - entry_price) / entry_price) * 100.0
    max_return_pct = ((high_max - entry_price) / entry_price) * 100.0
    min_return_pct = ((low_min - entry_price) / entry_price) * 100.0
    return {
        "exit_price": exit_price,
        "return_pct": round(return_pct, 4),
        "max_return_pct": round(max_return_pct, 4),
        "min_return_pct": round(min_return_pct, 4),
        "is_win": return_pct > 0,
    }


def _live_snapshot_metrics(
    df: pd.DataFrame,
    entry_price: float,
    as_of_date: date,
) -> Optional[Dict[str, float]]:
    if df.empty or entry_price <= 0:
        return None
    ts_as_of = pd.Timestamp(as_of_date)
    available = df[df.index <= ts_as_of]
    if available.empty:
        return None
    current_row = available.iloc[-1]
    current_price = float(current_row["Close"])
    period_high = float(available["High"].max())
    period_low = float(available["Low"].min())
    current_return_pct = ((current_price - entry_price) / entry_price) * 100.0
    max_return_pct = ((period_high - entry_price) / entry_price) * 100.0
    min_return_pct = ((period_low - entry_price) / entry_price) * 100.0
    return {
        "current_price": round(current_price, 4),
        "current_return_pct": round(current_return_pct, 4),
        "period_high": round(period_high, 4),
        "period_low": round(period_low, 4),
        "max_return_pct": round(max_return_pct, 4),
        "min_return_pct": round(min_return_pct, 4),
    }


def evaluate_track(track: Dict[str, Any]) -> int:
    """1件の追跡レコードについて到達可能な horizon を評価する。"""
    signal_date = parse_signal_date(track["signal_date"])
    entry_price = float(track["entry_price"])
    ticker = track["ticker"]
    today = today_jst()
    updated = 0
    track_end = add_jp_business_days(signal_date, MAX_TRACKING_BUSINESS_DAYS)
    elapsed = business_days_between(signal_date, today)

    try:
        history_start = next_jp_business_day(signal_date)
        history_end = min(today, track_end)
        df = _fetch_history_df(ticker, history_start, history_end)
    except Exception as exc:
        logger.warning("追跡評価: 株価取得失敗 (%s): %s", ticker, exc)
        df = pd.DataFrame()

    live = _live_snapshot_metrics(df, entry_price, min(today, track_end))
    if live:
        storage.update_track_snapshot(
            track["track_id"],
            current_price=live["current_price"],
            current_return_pct=live["current_return_pct"],
            period_high=live["period_high"],
            period_low=live["period_low"],
            max_return_pct=live["max_return_pct"],
            min_return_pct=live["min_return_pct"],
            business_days_elapsed=elapsed,
        )

    final_metrics: Optional[Dict[str, float]] = None
    for horizon in TRACKING_HORIZONS:
        existing = storage.get_track_outcome(track["track_id"], horizon)
        if existing and existing.get("status") == "complete":
            if horizon == MAX_TRACKING_BUSINESS_DAYS:
                final_metrics = {
                    "return_pct": float(existing["return_pct"]),
                    "max_return_pct": float(existing.get("max_return_pct") or 0),
                    "min_return_pct": float(existing.get("min_return_pct") or 0),
                    "is_win": bool(existing.get("is_win")),
                }
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
            min_return_pct=metrics["min_return_pct"],
            is_win=metrics["is_win"],
            status="complete",
        )
        updated += 1
        if horizon == MAX_TRACKING_BUSINESS_DAYS:
            final_metrics = metrics

    if today >= track_end and track.get("status") == "tracking":
        if final_metrics is None and live:
            final_metrics = {
                "return_pct": live["current_return_pct"],
                "max_return_pct": live["max_return_pct"],
                "min_return_pct": live["min_return_pct"],
                "is_win": live["current_return_pct"] > 0,
            }
        if final_metrics:
            storage.mark_track_archived(
                track["track_id"],
                final_return_pct=final_metrics["return_pct"],
                final_is_win=final_metrics["is_win"],
                max_return_pct=final_metrics["max_return_pct"],
                min_return_pct=final_metrics["min_return_pct"],
            )

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


def _mode_stats(mode: str) -> Dict[str, Any]:
    all_tracks = storage.list_signal_tracks(risk_mode=mode, limit=1000)
    archived = [t for t in all_tracks if t.get("status") == "archived"]
    active = [t for t in all_tracks if t.get("status") == "tracking"]
    finalized = [t for t in archived if t.get("final_return_pct") is not None]

    win_rate_pct = None
    avg_return_pct = None
    max_profit_pct = None
    max_loss_pct = None

    if finalized:
        wins = sum(1 for t in finalized if t.get("final_is_win"))
        win_rate_pct = round(wins / len(finalized) * 100.0, 2)
        avg_return_pct = round(
            sum(float(t["final_return_pct"]) for t in finalized) / len(finalized),
            2,
        )
        max_profit_pct = round(
            max(float(t.get("max_return_pct") or t["final_return_pct"]) for t in finalized),
            2,
        )
        max_loss_pct = round(
            min(float(t.get("min_return_pct") or t["final_return_pct"]) for t in finalized),
            2,
        )

    return {
        "risk_mode": mode,
        "registered_count": len(all_tracks),
        "active_count": len(active),
        "finalized_count": len(finalized),
        "win_rate_pct": win_rate_pct,
        "avg_return_pct": avg_return_pct,
        "max_profit_pct": max_profit_pct,
        "max_loss_pct": max_loss_pct,
    }


def build_mode_comparison_summary() -> Dict[str, Any]:
    """堅実・標準・積極モードの成績を比較する。"""
    modes = [_mode_stats(mode) for mode in RISK_MODES]
    best_mode = None
    best_score = -1.0
    for row in modes:
        if row["finalized_count"] == 0 or row["win_rate_pct"] is None:
            continue
        score = row["win_rate_pct"] + (row["avg_return_pct"] or 0) * 0.1
        if score > best_score:
            best_score = score
            best_mode = row["risk_mode"]
    return {
        "modes": modes,
        "best_mode": best_mode,
    }


def _track_to_dashboard_row(track: Dict[str, Any]) -> Dict[str, Any]:
    signal_date = parse_signal_date(track["signal_date"])
    today = today_jst()
    elapsed = track.get("business_days_elapsed")
    if elapsed is None:
        elapsed = business_days_between(signal_date, today)

    status = track.get("status", "tracking")
    is_archived = status == "archived"
    return_pct = track.get("final_return_pct") if is_archived else track.get("current_return_pct")

    return {
        "track_id": track["track_id"],
        "ticker": track["ticker"],
        "name": track["name"],
        "signal_date": track["signal_date"],
        "risk_mode": track.get("risk_mode"),
        "entry_price": track["entry_price"],
        "status": status,
        "elapsed_business_days": elapsed,
        "elapsed_label": "検証完了" if is_archived else f"{elapsed}日目",
        "current_price": track.get("current_price"),
        "return_pct": return_pct,
        "period_high": track.get("period_high"),
        "period_low": track.get("period_low"),
        "max_return_pct": track.get("max_return_pct"),
        "min_return_pct": track.get("min_return_pct"),
        "is_win": track.get("final_is_win") if is_archived else ((return_pct or 0) > 0),
        "archived_at": track.get("archived_at"),
        "preset_matched": track.get("preset_matched"),
    }


def build_forward_test_dashboard(*, auto_evaluate: bool = True) -> Dict[str, Any]:
    """パフォーマンス検証ダッシュボード用データを返す。"""
    if auto_evaluate:
        evaluate_pending_tracks(limit=200)

    active = storage.list_signal_tracks(status="tracking", limit=100)
    archived = storage.list_signal_tracks(status="archived", limit=200)
    tracks = [_track_to_dashboard_row(t) for t in active + archived]

    return {
        "status": "success",
        "tracking_period_business_days": MAX_TRACKING_BUSINESS_DAYS,
        "checkpoints": [HORIZON_LABELS[h] for h in TRACKING_HORIZONS],
        "mode_comparison": build_mode_comparison_summary(),
        "tracks": tracks,
        "active_count": len(active),
        "archived_count": len(archived),
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
        "checkpoints": [HORIZON_LABELS[h] for h in TRACKING_HORIZONS],
        "total_registered": total_registered,
        "filters": {
            "risk_mode": risk_mode,
            "preset_matched": preset_matched,
        },
        "horizons": horizons,
        "mode_comparison": build_mode_comparison_summary(),
        "notes": {
            "win_rate_pct": "各時点の終値ベース損益がプラスの比率",
            "avg_return_pct": "各時点の平均損益率（終値ベース）",
            "max_return_achievement_rate_pct": "登録から各時点までの最高値ベース平均最高益率",
        },
    }
