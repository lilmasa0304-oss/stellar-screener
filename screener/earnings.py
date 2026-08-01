"""決算跨ぎリスク判定（決算発表日前後のエントリー除外）。"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from screener.yahoo_session import create_yfinance_ticker

logger = logging.getLogger(__name__)

DEFAULT_DAYS_BEFORE = int(os.environ.get("EARNINGS_DAYS_BEFORE", "7"))
DEFAULT_DAYS_AFTER = int(os.environ.get("EARNINGS_DAYS_AFTER", "7"))
EARNINGS_CACHE_TTL_SEC = float(os.environ.get("EARNINGS_CACHE_TTL_SEC", "3600"))


@dataclass(frozen=True)
class EarningsSchedule:
    next_earnings_date: Optional[date] = None
    last_earnings_date: Optional[date] = None
    source: str = "unknown"


@dataclass(frozen=True)
class EarningsBlackoutResult:
    is_blackout: bool
    reference_date: Optional[date] = None
    next_earnings_date: Optional[date] = None
    last_earnings_date: Optional[date] = None
    days_before: int = DEFAULT_DAYS_BEFORE
    days_after: int = DEFAULT_DAYS_AFTER
    data_available: bool = True

    def warning_tag(self) -> str:
        if not self.is_blackout or self.reference_date is None:
            return ""
        return f"⚠️ 決算通過直前/直後（決算日: {self.reference_date.strftime('%Y/%m/%d')}）"

    def filter_message(self) -> str:
        base = (
            f"決算発表前後（{self.days_before}日以内）のため、"
            "エントリー対象外としてフィルタリングしました。"
        )
        tag = self.warning_tag()
        return f"{base} {tag}".strip() if tag else base


class _EarningsCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, Tuple[EarningsSchedule, float]] = {}
        self._lock = threading.Lock()

    def get(self, symbol: str) -> Optional[EarningsSchedule]:
        now = time.monotonic()
        key = symbol.strip().upper()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, symbol: str, value: EarningsSchedule) -> None:
        key = symbol.strip().upper()
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)


_earnings_cache = _EarningsCache(EARNINGS_CACHE_TTL_SEC)


def get_earnings_filter_settings(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """config.yaml / 環境変数から決算フィルター設定を読み込む。"""
    settings = (config or {}).get("settings", {})
    earnings_cfg = settings.get("earnings_filter") or {}
    enabled_raw = earnings_cfg.get("enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        enabled = bool(enabled_raw)
    return {
        "enabled": enabled,
        "days_before": int(earnings_cfg.get("days_before", DEFAULT_DAYS_BEFORE)),
        "days_after": int(earnings_cfg.get("days_after", DEFAULT_DAYS_AFTER)),
    }


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _to_date(item)
            if parsed is not None:
                return parsed
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    if numeric > 1e12:
        numeric /= 1000.0
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _unique_dates(values: List[Any]) -> List[date]:
    seen: set[date] = set()
    dates: List[date] = []
    for value in values:
        parsed = _to_date(value)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        dates.append(parsed)
    return sorted(dates)


def _dates_from_info(info: Dict[str, Any]) -> List[date]:
    keys = (
        "earningsDate",
        "earningsTimestamp",
        "earningsTimestampStart",
        "earningsTimestampEnd",
        "earningsCallTimestampStart",
        "earningsCallTimestampEnd",
    )
    raw_values: List[Any] = []
    for key in keys:
        value = info.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            raw_values.extend(value)
        else:
            raw_values.append(value)
    return _unique_dates(raw_values)


def _dates_from_calendar(calendar: Any) -> List[date]:
    if calendar is None:
        return []
    if isinstance(calendar, dict):
        values = calendar.get("Earnings Date") or calendar.get("EarningsDate")
        if values is None:
            return []
        if not isinstance(values, (list, tuple)):
            values = [values]
        return _unique_dates(list(values))
    if hasattr(calendar, "empty") and calendar.empty:
        return []
    if hasattr(calendar, "index"):
        return _unique_dates(list(calendar.index))
    return []


def _dates_from_earnings_dates_table(table: Any) -> List[date]:
    if table is None:
        return []
    if hasattr(table, "empty") and table.empty:
        return []
    if hasattr(table, "index"):
        return _unique_dates(list(table.index))
    return []


def _split_next_and_last(dates: List[date], today: date) -> Tuple[Optional[date], Optional[date]]:
    if not dates:
        return None, None
    past = [d for d in dates if d <= today]
    future = [d for d in dates if d > today]
    last_date = max(past) if past else None
    next_date = min(future) if future else None
    if next_date is None and last_date is None:
        only = dates[-1]
        if only >= today:
            next_date = only
        else:
            last_date = only
    return next_date, last_date


def fetch_earnings_schedule(
    yahoo_ticker: str,
    info: Optional[Dict[str, Any]] = None,
) -> EarningsSchedule:
    """次回・直近の決算発表日を yfinance から取得する。"""
    symbol = yahoo_ticker.strip().upper()
    cached = _earnings_cache.get(symbol)
    if cached is not None:
        return cached

    dates: List[date] = []
    source = "unknown"

    if info:
        info_dates = _dates_from_info(info)
        if info_dates:
            dates.extend(info_dates)
            source = "info"

    try:
        ticker = create_yfinance_ticker(symbol)
        if not dates:
            calendar_dates = _dates_from_calendar(getattr(ticker, "calendar", None))
            if calendar_dates:
                dates.extend(calendar_dates)
                source = "calendar"
        if not dates:
            earnings_dates = _dates_from_earnings_dates_table(
                getattr(ticker, "earnings_dates", None)
            )
            if earnings_dates:
                dates.extend(earnings_dates)
                source = "earnings_dates"
        if not info and not dates:
            info = ticker.info or {}
            info_dates = _dates_from_info(info)
            if info_dates:
                dates.extend(info_dates)
                source = "info_fetch"
    except Exception as exc:
        logger.debug("決算日取得失敗 (%s): %s", symbol, exc)

    unique = _unique_dates(dates)
    today = datetime.now(timezone.utc).date()
    next_date, last_date = _split_next_and_last(unique, today)
    schedule = EarningsSchedule(
        next_earnings_date=next_date,
        last_earnings_date=last_date,
        source=source,
    )
    if unique:
        _earnings_cache.set(symbol, schedule)
    return schedule


def is_within_earnings_blackout(
    earnings_date: date,
    *,
    reference_date: Optional[date] = None,
    days_before: int = DEFAULT_DAYS_BEFORE,
    days_after: int = DEFAULT_DAYS_AFTER,
) -> bool:
    today = reference_date or datetime.now(timezone.utc).date()
    start = earnings_date - timedelta(days=days_before)
    end = earnings_date + timedelta(days=days_after)
    return start <= today <= end


def check_earnings_blackout(
    yahoo_ticker: str,
    *,
    info: Optional[Dict[str, Any]] = None,
    reference_date: Optional[date] = None,
    days_before: int = DEFAULT_DAYS_BEFORE,
    days_after: int = DEFAULT_DAYS_AFTER,
) -> EarningsBlackoutResult:
    """決算日前後ブラックアウト期間か判定する。データ未取得時は除外しない。"""
    schedule = fetch_earnings_schedule(yahoo_ticker, info=info)
    candidates = [
        d for d in (schedule.next_earnings_date, schedule.last_earnings_date) if d is not None
    ]
    if not candidates:
        return EarningsBlackoutResult(
            is_blackout=False,
            next_earnings_date=schedule.next_earnings_date,
            last_earnings_date=schedule.last_earnings_date,
            days_before=days_before,
            days_after=days_after,
            data_available=False,
        )

    today = reference_date or datetime.now(timezone.utc).date()
    for earnings_date in candidates:
        if is_within_earnings_blackout(
            earnings_date,
            reference_date=today,
            days_before=days_before,
            days_after=days_after,
        ):
            return EarningsBlackoutResult(
                is_blackout=True,
                reference_date=earnings_date,
                next_earnings_date=schedule.next_earnings_date,
                last_earnings_date=schedule.last_earnings_date,
                days_before=days_before,
                days_after=days_after,
                data_available=True,
            )

    return EarningsBlackoutResult(
        is_blackout=False,
        next_earnings_date=schedule.next_earnings_date,
        last_earnings_date=schedule.last_earnings_date,
        days_before=days_before,
        days_after=days_after,
        data_available=True,
    )


def apply_earnings_fields(
    payload: Dict[str, Any],
    blackout: EarningsBlackoutResult,
) -> Dict[str, Any]:
    """診断/スキャン結果 dict に決算フィルター情報を付与する。"""
    enriched = dict(payload)
    enriched["earnings_blackout"] = blackout.is_blackout
    enriched["earnings_data_available"] = blackout.data_available
    enriched["entry_eligible"] = not blackout.is_blackout
    enriched["next_earnings_date"] = (
        blackout.next_earnings_date.isoformat() if blackout.next_earnings_date else None
    )
    enriched["last_earnings_date"] = (
        blackout.last_earnings_date.isoformat() if blackout.last_earnings_date else None
    )
    enriched["earnings_reference_date"] = (
        blackout.reference_date.isoformat() if blackout.reference_date else None
    )
    if blackout.is_blackout:
        enriched["earnings_warning"] = blackout.warning_tag()
        enriched["earnings_filter_message"] = blackout.filter_message()
        if enriched.get("buy_signal"):
            enriched["buy_signal_raw"] = True
            enriched["buy_signal"] = False
            enriched["trend_status"] = "EARNINGS_RISK"
            technical_reason = enriched.get("reason") or ""
            enriched["reason"] = (
                f"{blackout.filter_message()} {technical_reason}".strip()
            )
    return enriched
