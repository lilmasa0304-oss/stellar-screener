"""日本株向け営業日（JST）ユーティリティ。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional

JST = timezone(timedelta(hours=9))

# 主要な JPX 休場日（不足時は平日近似。必要に応じて拡張）
JPX_HOLIDAYS = {
    date(2026, 1, 1),
    date(2026, 1, 2),
    date(2026, 1, 3),
    date(2026, 1, 12),
    date(2026, 2, 11),
    date(2026, 2, 23),
    date(2026, 3, 20),
    date(2026, 4, 29),
    date(2026, 5, 3),
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 5, 6),
    date(2026, 7, 20),
    date(2026, 8, 10),
    date(2026, 8, 11),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 11, 3),
    date(2026, 11, 23),
}


def today_jst(reference: Optional[date] = None) -> date:
    if reference is not None:
        return reference
    return datetime.now(JST).date()


def is_jp_business_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    return day not in JPX_HOLIDAYS


def next_jp_business_day(day: date) -> date:
    current = day
    while not is_jp_business_day(current):
        current += timedelta(days=1)
    return current


def add_jp_business_days(start: date, business_days: int) -> date:
    """登録日を0日目とし、N 営業日後の日付を返す。"""
    if business_days <= 0:
        return next_jp_business_day(start)
    current = next_jp_business_day(start)
    added = 0
    while added < business_days:
        current += timedelta(days=1)
        if is_jp_business_day(current):
            added += 1
    return current


def business_days_between(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if is_jp_business_day(current):
            count += 1
    return count


def iter_business_days(start: date, end: date) -> List[date]:
    days: List[date] = []
    current = start
    while current <= end:
        if is_jp_business_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def parse_signal_date(value: Optional[str]) -> date:
    if not value:
        return today_jst()
    text = value.strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text[:10])
