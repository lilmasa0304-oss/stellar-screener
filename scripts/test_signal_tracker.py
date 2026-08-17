"""シグナル追跡集計のユニットテスト。"""

from screener.jp_business_days import add_jp_business_days
from screener.signal_tracker import _aggregate_horizon, _window_metrics
import pandas as pd
from datetime import date


def test_add_jp_business_days():
    start = date(2026, 8, 7)  # Fri
    assert add_jp_business_days(start, 3) == date(2026, 8, 14)  # +3 BD (8/10-11 are holidays)


def test_window_metrics():
    idx = pd.to_datetime(["2026-08-07", "2026-08-10", "2026-08-11"])
    df = pd.DataFrame(
        {"Close": [100.0, 105.0, 102.0], "High": [101.0, 110.0, 103.0]},
        index=idx,
    )
    metrics = _window_metrics(df, 100.0, date(2026, 8, 10))
    assert metrics is not None
    assert metrics["return_pct"] == 5.0
    assert metrics["max_return_pct"] == 10.0
    assert metrics["is_win"] is True


def test_aggregate_horizon():
    outcomes = [
        {"horizon_days": 3, "status": "complete", "return_pct": 2.0, "max_return_pct": 4.0, "is_win": True},
        {"horizon_days": 3, "status": "complete", "return_pct": -1.0, "max_return_pct": 1.0, "is_win": False},
        {"horizon_days": 5, "status": "pending"},
    ]
    h3 = _aggregate_horizon(outcomes, 3)
    assert h3["evaluated_count"] == 2
    assert h3["win_rate_pct"] == 50.0
    assert h3["avg_return_pct"] == 0.5
    assert h3["max_return_achievement_rate_pct"] == 2.5


if __name__ == "__main__":
    test_add_jp_business_days()
    test_window_metrics()
    test_aggregate_horizon()
    print("ok")
