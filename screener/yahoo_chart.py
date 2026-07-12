"""Yahoo Chart API / Stooq による株価取得フォールバック（Render 向け）。"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from screener.yahoo_session import get_yahoo_session

logger = logging.getLogger(__name__)

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_PERIOD_MAP = {
    "1mo": "1mo",
    "3mo": "3mo",
    "6mo": "6mo",
    "1y": "1y",
    "2y": "2y",
    "5y": "5y",
}


def _to_stooq_symbol(ticker_symbol: str) -> Optional[str]:
    symbol = (ticker_symbol or "").strip().upper()
    if symbol.endswith(".T"):
        return f"{symbol[:-2]}.jp"
    if symbol.endswith(".JP"):
        return symbol.lower()
    return None


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=6))
def _fetch_yahoo_chart_json(symbol: str, period: str) -> Dict[str, Any]:
    session = get_yahoo_session()
    response = session.get(
        _YAHOO_CHART_URL.format(symbol=symbol),
        params={
            "interval": "1d",
            "range": _PERIOD_MAP.get(period, period),
            "includePrePost": "false",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _chart_json_to_dataframe(payload: Dict[str, Any]) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    chart = (payload.get("chart") or {}).get("result") or []
    if not chart:
        return None, None

    block = chart[0]
    meta = block.get("meta") or {}
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    timestamps = block.get("timestamp") or []
    if not timestamps:
        return None, None

    index = [
        datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        for ts in timestamps
    ]
    df = pd.DataFrame(
        {
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        },
        index=pd.DatetimeIndex(index, name="Date"),
    ).dropna(how="all")
    if df.empty:
        return None, None

    name = (
        meta.get("longName")
        or meta.get("shortName")
        or meta.get("symbol")
    )
    return df, name


@retry(reraise=True, stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
def _fetch_stooq_csv(stooq_symbol: str) -> pd.DataFrame:
    session = get_yahoo_session()
    response = session.get(
        "https://stooq.com/q/d/l/",
        params={"s": stooq_symbol, "i": "d"},
        timeout=20,
    )
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text))
    if df.empty or "Date" not in df.columns:
        raise ValueError("Stooq CSV が空です")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    rename = {col: col.capitalize() for col in df.columns}
    df = df.rename(columns=rename)
    return df


def fetch_history_with_fallback(
    ticker_symbol: str,
    *,
    period: str = "6mo",
) -> Tuple[Optional[pd.DataFrame], Optional[str], str]:
    """yfinance 失敗時に Chart API → Stooq の順で株価履歴を取得する。"""
    symbol = ticker_symbol.strip()

    try:
        payload = _fetch_yahoo_chart_json(symbol, period)
        df, name = _chart_json_to_dataframe(payload)
        if df is not None and not df.empty:
            logger.info("Yahoo Chart API 取得成功: %s (%d rows)", symbol, len(df))
            return df, name, "yahoo_chart"
    except Exception as exc:
        logger.warning("Yahoo Chart API 失敗 (%s): %s", symbol, exc)

    stooq_symbol = _to_stooq_symbol(symbol)
    if stooq_symbol:
        try:
            df = _fetch_stooq_csv(stooq_symbol)
            if df is not None and not df.empty:
                tail = {"1mo": 22, "3mo": 66, "6mo": 132, "1y": 264}.get(period, 132)
                df = df.tail(tail)
                logger.info("Stooq 取得成功: %s (%d rows)", stooq_symbol, len(df))
                return df, symbol, "stooq"
        except Exception as exc:
            logger.warning("Stooq 失敗 (%s): %s", stooq_symbol, exc)

    return None, None, "none"
