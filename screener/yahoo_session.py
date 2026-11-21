"""Yahoo Finance 向け HTTP セッション（クラウド IP ブロック対策）。"""

from __future__ import annotations

import logging
import threading
from typing import Any

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

_thread_local = threading.local()


def get_yahoo_session() -> requests.Session:
    """スレッドごとの requests.Session を返す。共有 Session は並列取得で壊れる。"""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(_YAHOO_BROWSER_HEADERS)
        _thread_local.session = session
        logger.debug("Yahoo Finance 用セッションをこのスレッドで初期化しました")
    return session


def create_yfinance_ticker(symbol: str) -> Any:
    """カスタムセッション付き yfinance.Ticker を生成する。"""
    return yf.Ticker(symbol, session=get_yahoo_session())
