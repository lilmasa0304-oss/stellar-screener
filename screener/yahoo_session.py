"""Yahoo Finance 向け HTTP セッション（クラウド IP ブロック対策）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
import yfinance as yf

logger = logging.getLogger(__name__)

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_YAHOO_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

_session: Optional[requests.Session] = None


def get_yahoo_session() -> requests.Session:
    """ブラウザ風ヘッダー付きの共有 requests.Session を返す。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_YAHOO_BROWSER_HEADERS)
        logger.debug("Yahoo Finance 用ブラウザ風セッションを初期化しました")
    return _session


def create_yfinance_ticker(symbol: str) -> Any:
    """カスタムセッション付き yfinance.Ticker を生成する。"""
    return yf.Ticker(symbol, session=get_yahoo_session())
