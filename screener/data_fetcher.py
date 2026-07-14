import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from screener.diagnosis_cache import (
    DEFAULT_HISTORY_DAYS,
    MIN_HISTORY_ROWS,
    get_ticker_bundle,
    put_ticker_bundle,
    trim_history_df,
)
from screener.yahoo_chart import fetch_history_with_fallback
from screener.yahoo_session import create_yfinance_ticker

logger = logging.getLogger(__name__)


class DataFetcher:
    """Fetches stock historical data and info from Yahoo Finance with rate limiting."""

    def __init__(
        self,
        delay_seconds: float = 1.0,
        history_period: str = "3mo",
        *,
        history_days: Optional[int] = None,
        use_cache: bool = False,
    ):
        self.delay_seconds = delay_seconds
        self.history_period = history_period
        self.history_days = history_days
        self.use_cache = use_cache

    def fetch_ticker_data(self, ticker_symbol: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """後方互換: 履歴と銘柄名のみ返す。"""
        df, name, _info = self.fetch_ticker_bundle(ticker_symbol)
        return df, name

    def fetch_ticker_bundle(
        self,
        ticker_symbol: str,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], Dict[str, Any]]:
        """
        履歴・銘柄名・info を最小限の Yahoo 往復で取得する。
        use_cache=True のとき TTL キャッシュを参照する。
        """
        symbol = ticker_symbol.strip()
        if self.use_cache:
            cached = get_ticker_bundle(symbol)
            if cached is not None:
                logger.info("キャッシュヒット (OHLCV): %s", symbol)
                return cached

        logger.info("Fetching data for %s...", symbol)
        company_name = symbol
        info: Dict[str, Any] = {}
        df: Optional[pd.DataFrame] = None

        try:
            ticker = create_yfinance_ticker(symbol)
            df = self._fetch_history(ticker)
            if df is not None and not df.empty:
                info = self._fetch_info(ticker)
                company_name = self._name_from_info(info, symbol)
        except Exception as exc:
            logger.warning("yfinance 取得失敗 (%s): %s", symbol, exc)

        if df is None or df.empty:
            logger.info("yfinance 代替ルートを試行: %s", symbol)
            period = self._fallback_period()
            df, fallback_name, source = fetch_history_with_fallback(symbol, period=period)
            if df is None or df.empty:
                logger.error("No historical data found for %s.", symbol)
                return None, None, {}
            if fallback_name:
                company_name = fallback_name
            logger.info("代替データソースで取得成功: %s via %s", symbol, source)

        df = trim_history_df(df)
        if df is not None and len(df) < MIN_HISTORY_ROWS:
            logger.warning(
                "履歴行数が不足 (%s: %d rows, need %d)",
                symbol,
                len(df),
                MIN_HISTORY_ROWS,
            )

        if self.use_cache and df is not None and not df.empty:
            put_ticker_bundle(symbol, df, company_name, info)

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        return df, company_name, info

    def _fallback_period(self) -> str:
        if self.history_days is not None:
            return f"{self.history_days}d"
        return self.history_period

    def _fetch_history(self, ticker: Any) -> Optional[pd.DataFrame]:
        if self.history_days is not None:
            end = datetime.now()
            start = end - timedelta(days=self.history_days)
            return ticker.history(start=start, end=end, interval="1d")
        return ticker.history(period=self.history_period)

    @staticmethod
    def _fetch_info(ticker: Any) -> Dict[str, Any]:
        try:
            return ticker.info or {}
        except Exception as exc:
            logger.debug("ticker.info 取得失敗: %s", exc)
            return {}

    @staticmethod
    def _name_from_info(info: Dict[str, Any], fallback: str) -> str:
        return (
            info.get("longName")
            or info.get("shortName")
            or fallback
        )

    def fetch_all(self, tickers: list) -> Dict[str, Tuple[pd.DataFrame, str]]:
        """Fetches data for a list of tickers."""
        results: Dict[str, Tuple[pd.DataFrame, str]] = {}
        for ticker in tickers:
            df, name, _info = self.fetch_ticker_bundle(ticker)
            if df is not None:
                results[ticker] = (df, name or ticker)
        return results


def create_diagnosis_fetcher() -> DataFetcher:
    """単銘柄診断向け: 短い履歴・遅延なし・キャッシュ有効。"""
    return DataFetcher(
        delay_seconds=0,
        history_days=DEFAULT_HISTORY_DAYS,
        use_cache=True,
    )
