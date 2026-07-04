"""日本語の銘柄表示名を Yahoo Finance 検索 API から解決する。"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_USER_AGENT = "Mozilla/5.0 (compatible; STELLAR-SCREENER/3.0)"
_ASCII_NAME = re.compile(r"^[A-Za-z0-9\s\.,&'\-()/]+$")


def _normalize_ticker(ticker: str) -> str:
    code = (ticker or "").strip().upper()
    if not code:
        return ""
    if not code.endswith(".T"):
        code = f"{code}.T" if re.match(r"^\d{3}[A-Z0-9]$", code) else code
    return code


def _looks_english(name: Optional[str]) -> bool:
    if not name or not name.strip():
        return True
    if re.search(r"[\u3040-\u9fff\u30a0-\u30ff]", name):
        return False
    return bool(_ASCII_NAME.match(name.strip()))


@lru_cache(maxsize=2048)
def resolve_jp_display_name(ticker: str, fallback: str = "") -> str:
    """
    ティッカーに対応する日本語表示名を返す。
    取得失敗時は fallback、なければ ticker を返す。
    """
    symbol = _normalize_ticker(ticker)
    if not symbol:
        return fallback or ticker

    if fallback and not _looks_english(fallback):
        return fallback.strip()

    try:
        resp = requests.get(
            _YAHOO_SEARCH_URL,
            params={
                "q": symbol,
                "quotesCount": 1,
                "lang": "ja-JP",
                "region": "JP",
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=12,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or []
        if quotes:
            name = (quotes[0].get("longname") or quotes[0].get("shortname") or "").strip()
            if name and not _looks_english(name):
                return name
            if name and not fallback:
                return name
    except Exception as exc:
        logger.debug("日本語銘柄名取得失敗 (%s): %s", symbol, exc)

    return (fallback or symbol).strip()
