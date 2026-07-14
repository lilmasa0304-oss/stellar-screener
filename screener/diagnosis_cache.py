"""銘柄診断向け TTL キャッシュ（重複 Yahoo 取得の短時間抑制）。"""

from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd

DEFAULT_TTL_SEC = float(os.environ.get("DIAGNOSIS_CACHE_TTL_SEC", "300"))
DEFAULT_HISTORY_DAYS = int(os.environ.get("DIAGNOSIS_HISTORY_DAYS", "120"))
DIAGNOSIS_TIMEOUT_SEC = float(os.environ.get("DIAGNOSIS_TIMEOUT_SEC", "28"))
MIN_HISTORY_ROWS = 75
TRIM_HISTORY_ROWS = 90


class _TtlCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._store[key] = (value, expires_at)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_ticker_bundles = _TtlCache(DEFAULT_TTL_SEC)
_diagnosis_results = _TtlCache(DEFAULT_TTL_SEC)


def cache_key_ticker(symbol: str) -> str:
    return symbol.strip().upper()


def cache_key_diagnosis(code: str, mode: Optional[str]) -> str:
    normalized_mode = (mode or "default").strip()
    return f"{code.strip().upper()}:{normalized_mode}"


def get_ticker_bundle(
    symbol: str,
) -> Optional[Tuple[pd.DataFrame, str, Dict[str, Any]]]:
    cached = _ticker_bundles.get(cache_key_ticker(symbol))
    if cached is None:
        return None
    df, name, info = cached
    return df.copy(), name, copy.deepcopy(info)


def put_ticker_bundle(
    symbol: str,
    df: pd.DataFrame,
    name: str,
    info: Dict[str, Any],
) -> None:
    _ticker_bundles.set(
        cache_key_ticker(symbol),
        (df.copy(), name, copy.deepcopy(info)),
    )


def get_diagnosis(code: str, mode: Optional[str]) -> Optional[Dict[str, Any]]:
    cached = _diagnosis_results.get(cache_key_diagnosis(code, mode))
    if cached is None:
        return None
    return copy.deepcopy(cached)


def put_diagnosis(code: str, mode: Optional[str], result: Dict[str, Any]) -> None:
    _diagnosis_results.set(cache_key_diagnosis(code, mode), copy.deepcopy(result))


def trim_history_df(df: pd.DataFrame, *, max_rows: int = TRIM_HISTORY_ROWS) -> pd.DataFrame:
    """テクニカル計算に必要な行数だけ残し、処理量を抑える。"""
    if df is None or df.empty:
        return df
    if len(df) <= max_rows:
        return df
    return df.iloc[-max_rows:].copy()
