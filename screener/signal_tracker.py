"""BUY SIGNAL フォワードテスト追跡（最大10営業日・3/5/10日目の成績集計）。"""

from __future__ import annotations

import logging
import math
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
from screener.yahoo_chart import fetch_history_with_fallback

logger = logging.getLogger(__name__)

TRACKING_HORIZONS = (3, 5, 10)
HORIZON_LABELS = {
    3: "3日目",
    5: "5日目（1週間）",
    10: "10日目（2週間）",
}
MAX_TRACKING_BUSINESS_DAYS = 10
RISK_MODES = ("堅実", "標準", "積極")


def _safe_num(value: Any, *, digits: Optional[int] = 4) -> Optional[float]:
    """pandas / DB 由来の NaN・Inf を None に正規化する。"""
    if value is None:
        return None
    try:
        if hasattr(value, "item"):
            value = value.item()
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return None
        return round(num, digits) if digits is not None else num
    except (TypeError, ValueError):
        return None


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
    """登録日〜評価日の株価履歴（Chart API / Stooq フォールバック）。"""
    symbol = ticker.strip().upper()
    if not symbol.endswith(".T") and symbol[:-1].isdigit():
        symbol = f"{symbol}.T"

    span_days = max((end - start).days + 10, 30)
    if span_days <= 90:
        period = "3mo"
    elif span_days <= 180:
        period = "6mo"
    else:
        period = "1y"

    df, _, source = fetch_history_with_fallback(symbol, period=period)
    if df is None or df.empty:
        logger.warning("追跡評価: 株価履歴なし (%s, source=%s)", symbol, source)
        return pd.DataFrame()

    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    ts_start = pd.Timestamp(start)
    ts_end = pd.Timestamp(end + timedelta(days=5))
    df = df[(df.index >= ts_start) & (df.index <= ts_end)]
    if df.empty:
        logger.warning(
            "追跡評価: 期間フィルタ後にデータなし (%s %s〜%s)",
            symbol,
            start.isoformat(),
            end.isoformat(),
        )
    else:
        logger.debug("追跡評価: %s 取得 %d 行 (source=%s)", symbol, len(df), source)
    return df.sort_index()


def _window_metrics(df: pd.DataFrame, entry_price: float, eval_date: date) -> Optional[Dict[str, float]]:
    if df.empty or entry_price <= 0:
        return None
    ts_eval = pd.Timestamp(eval_date)
    available = df[df.index <= ts_eval]
    if available.empty:
        return None
    exit_row = available.iloc[-1]
    exit_price = _safe_num(exit_row["Close"])
    high_max = _safe_num(available["High"].max())
    low_min = _safe_num(available["Low"].min())
    if exit_price is None or high_max is None or low_min is None:
        return None
    return_pct = ((exit_price - entry_price) / entry_price) * 100.0
    max_return_pct = ((high_max - entry_price) / entry_price) * 100.0
    min_return_pct = ((low_min - entry_price) / entry_price) * 100.0
    return {
        "exit_price": exit_price,
        "return_pct": _safe_num(return_pct),
        "max_return_pct": _safe_num(max_return_pct),
        "min_return_pct": _safe_num(min_return_pct),
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
    current_price = _safe_num(current_row["Close"])
    period_high = _safe_num(available["High"].max())
    period_low = _safe_num(available["Low"].min())
    if current_price is None or period_high is None or period_low is None:
        return None
    current_return_pct = ((current_price - entry_price) / entry_price) * 100.0
    max_return_pct = ((period_high - entry_price) / entry_price) * 100.0
    min_return_pct = ((period_low - entry_price) / entry_price) * 100.0
    return {
        "current_price": current_price,
        "current_return_pct": _safe_num(current_return_pct),
        "period_high": period_high,
        "period_low": period_low,
        "max_return_pct": _safe_num(max_return_pct),
        "min_return_pct": _safe_num(min_return_pct),
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
    logger.info("追跡評価開始: active_tracks=%d limit=%d", len(tracks), limit)
    updated = 0
    for track in tracks:
        try:
            updated += evaluate_track(track)
        except Exception as exc:
            logger.exception(
                "追跡評価失敗 track_id=%s ticker=%s: %s",
                track.get("track_id"),
                track.get("ticker"),
                exc,
            )
    logger.info(
        "追跡評価完了: tracks_checked=%d outcomes_updated=%d",
        len(tracks),
        updated,
    )
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
    returns = [_safe_num(r.get("return_pct"), digits=2) for r in completed]
    max_returns = [_safe_num(r.get("max_return_pct"), digits=2) for r in completed]
    returns = [v for v in returns if v is not None]
    max_returns = [v for v in max_returns if v is not None]
    avg_return = sum(returns) / len(returns) if returns else None
    avg_max_return = sum(max_returns) / len(max_returns) if max_returns else None
    max_positive_rate = (
        sum(1 for v in max_returns if v > 0) / len(max_returns) * 100.0 if max_returns else None
    )

    return {
        "horizon_days": horizon,
        "label": HORIZON_LABELS[horizon],
        "registered_count": len(rows),
        "evaluated_count": len(completed),
        "pending_count": len(pending),
        "insufficient_data_count": len(insufficient),
        "win_rate_pct": round(wins / len(completed) * 100.0, 2) if completed else None,
        "avg_return_pct": round(avg_return, 2) if avg_return is not None else None,
        "max_return_achievement_rate_pct": round(avg_max_return, 2) if avg_max_return is not None else None,
        "max_return_positive_rate_pct": round(max_positive_rate, 2) if max_positive_rate is not None else None,
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
        final_returns = [_safe_num(t.get("final_return_pct"), digits=2) for t in finalized]
        final_returns = [v for v in final_returns if v is not None]
        if final_returns:
            avg_return_pct = round(sum(final_returns) / len(final_returns), 2)
            profit_candidates = [
                _safe_num(t.get("max_return_pct"), digits=2) or _safe_num(t.get("final_return_pct"), digits=2)
                for t in finalized
            ]
            loss_candidates = [
                _safe_num(t.get("min_return_pct"), digits=2) or _safe_num(t.get("final_return_pct"), digits=2)
                for t in finalized
            ]
            profit_candidates = [v for v in profit_candidates if v is not None]
            loss_candidates = [v for v in loss_candidates if v is not None]
            if profit_candidates:
                max_profit_pct = round(max(profit_candidates), 2)
            if loss_candidates:
                max_loss_pct = round(min(loss_candidates), 2)

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
        "name": track.get("name") or track["ticker"],
        "signal_date": str(track.get("signal_date") or "")[:10],
        "risk_mode": track.get("risk_mode"),
        "entry_price": _safe_num(track.get("entry_price"), digits=None),
        "status": status,
        "elapsed_business_days": elapsed,
        "elapsed_label": "検証完了" if is_archived else f"{elapsed}日目",
        "current_price": _safe_num(track.get("current_price"), digits=None),
        "return_pct": _safe_num(return_pct, digits=2),
        "period_high": _safe_num(track.get("period_high"), digits=None),
        "period_low": _safe_num(track.get("period_low"), digits=None),
        "max_return_pct": _safe_num(track.get("max_return_pct"), digits=2),
        "min_return_pct": _safe_num(track.get("min_return_pct"), digits=2),
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
