import time
import logging
import pandas as pd
import yfinance as yf
from typing import Dict, Optional, Tuple

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
        try:
            ticker = yf.Ticker(ticker_symbol)
            
            # Fetch history
            df = ticker.history(period=self.history_period)
            if df.empty:
                logger.warning(f"No historical data found for {ticker_symbol}.")
                return None, None
                
            # Fetch company name (gracefully fall back to ticker symbol if it fails or is slow)
            company_name = ticker_symbol
            try:
                # We fetch fast_info or info. Fast_info is much faster than info.
                if hasattr(ticker, 'fast_info') and 'name' in ticker.fast_info:
                    company_name = ticker.fast_info['name']
                elif 'longName' in ticker.info:
                    company_name = ticker.info['longName']
                elif 'shortName' in ticker.info:
                    company_name = ticker.info['shortName']
            except Exception as info_err:
                logger.debug(f"Could not retrieve company name for {ticker_symbol}: {info_err}")
                # Fallback to symbol is already set
                
            # Sleep to prevent rate limit blocks
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
                
            return df, company_name
            
        except Exception as e:
            logger.error(f"Error fetching data for {ticker_symbol}: {e}")
            return None, None
            
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
