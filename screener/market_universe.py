"""
日本株スクリーニング対象ユニバース（4桁コード 1000〜4999 ≒ 4000銘柄）。
Yahoo Finance に存在しないコードはスキャン時にスキップされる。
"""


def get_market_universe_tickers() -> list[str]:
    """全市場スキャン用ティッカーリスト（例: 1306.T, 7203.T）。"""
    return [f"{code:04d}.T" for code in range(1000, 5000)]


def get_market_universe_count() -> int:
    """スキャン対象銘柄数（約4000）。"""
    return len(get_market_universe_tickers())
