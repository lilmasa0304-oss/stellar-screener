"""シグナル追跡集計のユニットテスト。"""

from datetime import date
from unittest.mock import MagicMock
import sys

try:
    import tenacity  # noqa: F401
except ModuleNotFoundError:
    sys.modules["tenacity"] = MagicMock()

import pandas as pd

from screener.jp_business_days import add_jp_business_days
from screener.signal_tracker import (
    MAX_TRACKING_BUSINESS_DAYS,
    TRACKING_HORIZONS,
    _aggregate_horizon,
    _window_metrics,
)


def test_tracking_horizons_are_ten_day_model():
    assert MAX_TRACKING_BUSINESS_DAYS == 10
    assert TRACKING_HORIZONS == (3, 5, 10)


def test_add_jp_business_days():
    start = date(2026, 8, 7)  # Fri
    assert add_jp_business_days(start, 3) == date(2026, 8, 14)  # +3 BD (8/10-11 are holidays)
    assert add_jp_business_days(start, 5) == date(2026, 8, 18)
    assert add_jp_business_days(start, 10) == date(2026, 8, 25)


def test_window_metrics():
    idx = pd.to_datetime(["2026-08-07", "2026-08-10", "2026-08-11"])
    df = pd.DataFrame(
        {"Close": [100.0, 105.0, 102.0], "High": [101.0, 110.0, 103.0], "Low": [99.0, 104.0, 100.0]},
        index=idx,
    )
    metrics = _window_metrics(df, 100.0, date(2026, 8, 10))
    assert metrics is not None
    assert metrics["return_pct"] == 5.0
    assert metrics["max_return_pct"] == 10.0
    assert metrics["min_return_pct"] == -1.0
    assert metrics["is_win"] is True


def test_aggregate_horizon():
    outcomes = [
        {
            "horizon_days": 3,
            "status": "complete",
            "return_pct": 2.0,
            "max_return_pct": 4.0,
            "is_win": True,
        },
        {
            "horizon_days": 3,
            "status": "complete",
            "return_pct": -1.0,
            "max_return_pct": 1.0,
            "is_win": False,
        },
        {"horizon_days": 5, "status": "pending"},
    ]
    h3 = _aggregate_horizon(outcomes, 3)
    assert h3["evaluated_count"] == 2
    assert h3["win_rate_pct"] == 50.0
    assert h3["avg_return_pct"] == 0.5
    assert h3["max_return_achievement_rate_pct"] == 2.5


def test_is_buy_signal_coercion():
    from screener.signal_tracker import _is_buy_signal

    assert _is_buy_signal(True) is True
    assert _is_buy_signal(1) is True
    assert _is_buy_signal("true") is True
    assert _is_buy_signal(False) is False
    assert _is_buy_signal(None) is False


def test_register_track_from_scan_canonicalizes_ticker():
    from unittest.mock import patch

    from screener.signal_tracker import register_track_from_scan

    captured = {}

    def fake_register(**kwargs):
        captured.update(kwargs)
        return 99

    with patch("screener.storage.register_signal_track", side_effect=fake_register):
        track_id = register_track_from_scan(
            "scan_1",
            {"ticker": "3465", "current_price": 1234, "buy_signal": True, "name": "テスト"},
            risk_mode="堅実",
        )
    assert track_id == 99
    assert captured["ticker"] == "3465.T"
    assert captured["risk_mode"] == "堅実"


def test_dedupe_dashboard_tracks_collapses_ticker_variants():
    from screener.signal_tracker import _dedupe_dashboard_tracks

    rows = _dedupe_dashboard_tracks(
        [
            {"ticker": "3465.T", "signal_date": "2026-09-09", "risk_mode": "堅実", "track_id": 2},
            {"ticker": "3465", "signal_date": "2026-09-09", "risk_mode": "堅実", "track_id": 1},
            {"ticker": "3465.T", "signal_date": "2026-09-09", "risk_mode": "積極", "track_id": 3},
        ]
    )
    assert [row["track_id"] for row in rows] == [2, 3]


if __name__ == "__main__":
    test_tracking_horizons_are_ten_day_model()
    test_add_jp_business_days()
    test_window_metrics()
    test_aggregate_horizon()
    test_is_buy_signal_coercion()
    test_register_track_from_scan_canonicalizes_ticker()
    test_dedupe_dashboard_tracks_collapses_ticker_variants()
    print("ok")
