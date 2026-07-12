import time
import logging
import pandas as pd
from typing import Dict, Optional, Tuple

from screener.yahoo_chart import fetch_history_with_fallback
from screener.yahoo_session import create_yfinance_ticker

logger = logging.getLogger(__name__)

class DataFetcher:
    """Fetches stock historical data and info from Yahoo Finance with rate limiting."""

    def __init__(self, delay_seconds: float = 1.0, history_period: str = "3mo"):
        self.delay_seconds = delay_seconds
        self.history_period = history_period

    def fetch_ticker_data(self, ticker_symbol: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        Fetches historical data and the company name for a given ticker.
        
        Args:
            ticker_symbol: The ticker symbol (e.g., 'AAPL', '7203.T').
            
        Returns:
            A tuple of (historical_dataframe, company_name). Both can be None if fetching fails.
        """
        logger.info(f"Fetching data for {ticker_symbol}...")
        company_name = ticker_symbol
        df: Optional[pd.DataFrame] = None

        try:
            ticker = create_yfinance_ticker(ticker_symbol)
            df = ticker.history(period=self.history_period)
            if df is not None and not df.empty:
                try:
                    if hasattr(ticker, "fast_info") and ticker.fast_info.get("name"):
                        company_name = ticker.fast_info["name"]
                    elif ticker.info.get("longName"):
                        company_name = ticker.info["longName"]
                    elif ticker.info.get("shortName"):
                        company_name = ticker.info["shortName"]
                except Exception as info_err:
                    logger.debug(
                        "Could not retrieve company name for %s: %s",
                        ticker_symbol,
                        info_err,
                    )
        except Exception as e:
            logger.warning(f"yfinance 取得失敗 ({ticker_symbol}): {e}")

        if df is None or df.empty:
            logger.info("yfinance 代替ルートを試行: %s", ticker_symbol)
            df, fallback_name, source = fetch_history_with_fallback(
                ticker_symbol,
                period=self.history_period,
            )
            if df is None or df.empty:
                logger.error(f"No historical data found for {ticker_symbol}.")
                return None, None
            if fallback_name:
                company_name = fallback_name
            logger.info("代替データソースで取得成功: %s via %s", ticker_symbol, source)

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        return df, company_name
            
    def fetch_all(self, tickers: list) -> Dict[str, Tuple[pd.DataFrame, str]]:
        """
        Fetches data for a list of tickers.
        
        Args:
            tickers: A list of ticker symbols.
            
        Returns:
            A dictionary mapping ticker symbols to tuples of (dataframe, company_name).
        """
        results = {}
        for ticker in tickers:
            df, name = self.fetch_ticker_data(ticker)
            if df is not None:
                results[ticker] = (df, name or ticker)
        return results
