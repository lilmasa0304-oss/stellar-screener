import pandas as pd
from typing import Tuple


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculates the Relative Strength Index (RSI) using Wilder's smoothing technique.

    RSI = 100 - (100 / (1 + RS))
    RS = Smooth Gain / Smooth Loss
    """
    if len(df) < period:
        return pd.Series(index=df.index, dtype='float64')

    close = df['Close']
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's smoothing: exponential moving average with alpha = 1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    # Prevent division by zero
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calculate_sma(df: pd.DataFrame, period: int) -> pd.Series:
    """Calculates Simple Moving Average (SMA) for a given period."""
    return df['Close'].rolling(window=period).mean()


def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Calculates Exponential Moving Average (EMA) for a given period."""
    return df['Close'].ewm(span=period, adjust=False).mean()


def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> tuple[pd.Series, pd.Series]:
    """Calculates upper and lower Bollinger Bands for a given period and standard deviation."""
    mean = df['Close'].rolling(window=period).mean()
    std = df['Close'].rolling(window=period).std()
    upper = mean + (std * std_dev)
    lower = mean - (std * std_dev)
    return upper, lower


def calculate_macd(
    df: pd.DataFrame,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculates MACD (Moving Average Convergence Divergence).

    Args:
        df: DataFrame with 'Close' column.
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line EMA period (default 9).

    Returns:
        Tuple of (macd_line, signal_line, histogram) as pd.Series.
        - macd_line  = EMA(fast) - EMA(slow)
        - signal_line = EMA(macd_line, signal_period)
        - histogram   = macd_line - signal_line
    """
    close = df['Close']
    ema_fast = close.ewm(span=fast_period,  adjust=False).mean()
    ema_slow = close.ewm(span=slow_period,  adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_macd_crossover(histogram: pd.Series) -> Tuple[bool, bool]:
    """
    Detects MACD golden cross and pre-cross (convergence) from the histogram.

    Golden Cross (actual): histogram was negative, now non-negative
      → hist[-2] < 0 and hist[-1] >= 0

    Pre-Cross (convergence): histogram is still negative but narrowing
      → hist[-1] < 0 and |hist[-1]| < |hist[-2]|

    Args:
        histogram: MACD histogram series.

    Returns:
        Tuple of (golden_cross: bool, pre_cross: bool).
    """
    if len(histogram) < 2:
        return False, False

    h_now  = float(histogram.iloc[-1]) if pd.notna(histogram.iloc[-1])  else None
    h_prev = float(histogram.iloc[-2]) if pd.notna(histogram.iloc[-2]) else None

    if h_now is None or h_prev is None:
        return False, False

    golden_cross = (h_prev < 0) and (h_now >= 0)
    pre_cross    = (h_now < 0) and (abs(h_now) < abs(h_prev))

    return golden_cross, pre_cross
