"""決算ブラックアウト判定のユニットテスト。"""

from datetime import date
from unittest.mock import patch

from screener.earnings import (
    EarningsBlackoutResult,
    EarningsSchedule,
    _split_next_and_last,
    apply_earnings_fields,
    check_earnings_blackout,
    is_within_earnings_blackout,
    normalize_yahoo_ticker,
    should_fail_closed_for_mode,
)


def test_normalize_yahoo_ticker():
    assert normalize_yahoo_ticker("7906") == "7906.T"
    assert normalize_yahoo_ticker("7906.T") == "7906.T"
    assert normalize_yahoo_ticker("285A") == "285A.T"


def test_is_within_earnings_blackout():
    earnings = date(2026, 8, 10)
    assert is_within_earnings_blackout(
        earnings,
        reference_date=date(2026, 8, 10),
        days_before=7,
        days_after=7,
    )
    assert is_within_earnings_blackout(
        earnings,
        reference_date=date(2026, 8, 3),
        days_before=7,
        days_after=7,
    )
    assert is_within_earnings_blackout(
        earnings,
        reference_date=date(2026, 8, 17),
        days_before=7,
        days_after=7,
    )
    assert not is_within_earnings_blackout(
        earnings,
        reference_date=date(2026, 8, 18),
        days_before=7,
        days_after=7,
    )


def test_split_next_and_last():
    today = date(2026, 8, 10)
    next_date, last_date = _split_next_and_last(
        [date(2026, 5, 1), date(2026, 8, 10), date(2026, 11, 1)],
        today,
    )
    assert last_date == date(2026, 8, 10)
    assert next_date == date(2026, 11, 1)


def test_yonex_7906_blackout_on_earnings_day():
    schedule = EarningsSchedule(
        all_dates=(date(2026, 8, 10),),
        next_earnings_date=None,
        last_earnings_date=date(2026, 8, 10),
        source="calendar",
    )
    with patch("screener.earnings.fetch_earnings_schedule", return_value=schedule):
        result = check_earnings_blackout(
            "7906.T",
            reference_date=date(2026, 8, 10),
            fail_closed=True,
        )
    assert result.is_blackout is True
    assert result.reference_date == date(2026, 8, 10)
    assert result.data_available is True


def test_yonex_7906_without_suffix_uses_normalized_ticker():
    schedule = EarningsSchedule(
        all_dates=(date(2026, 8, 10),),
        last_earnings_date=date(2026, 8, 10),
        source="calendar",
    )

    def _fetch(symbol, info=None):
        assert symbol == "7906.T"
        return schedule

    with patch("screener.earnings.fetch_earnings_schedule", side_effect=_fetch):
        result = check_earnings_blackout(
            "7906",
            reference_date=date(2026, 8, 10),
            fail_closed=True,
        )
    assert result.is_blackout is True


def test_fail_closed_when_earnings_data_missing():
    empty = EarningsSchedule(all_dates=(), source="unknown")
    with patch("screener.earnings.fetch_earnings_schedule", return_value=empty):
        result = check_earnings_blackout(
            "7906.T",
            reference_date=date(2026, 8, 10),
            fail_closed=True,
        )
    assert result.is_blackout is True
    assert result.data_available is False
    assert result.fail_closed is True


def test_fail_open_when_earnings_data_missing_and_not_strict_mode():
    empty = EarningsSchedule(all_dates=(), source="unknown")
    with patch("screener.earnings.fetch_earnings_schedule", return_value=empty):
        result = check_earnings_blackout(
            "7906.T",
            reference_date=date(2026, 8, 10),
            fail_closed=False,
        )
    assert result.is_blackout is False
    assert result.data_available is False


def test_should_fail_closed_for_mode():
    config = {"settings": {"earnings_filter": {"fail_closed_modes": ["堅実"]}}}
    assert should_fail_closed_for_mode("堅実", config) is True
    assert should_fail_closed_for_mode("標準", config) is False


def test_apply_earnings_fields_overrides_buy_signal():
    blackout = EarningsBlackoutResult(
        is_blackout=True,
        reference_date=date(2026, 8, 10),
        next_earnings_date=date(2026, 8, 10),
        days_before=7,
        days_after=7,
    )
    result = apply_earnings_fields(
        {"buy_signal": True, "reason": "テクニカル条件クリア", "trend_status": "ENTRY_OK"},
        blackout,
    )
    assert result["buy_signal"] is False
    assert result["buy_signal_raw"] is True
    assert result["entry_eligible"] is False
    assert "決算発表前後" in result["earnings_filter_message"]
    assert result["trend_status"] == "EARNINGS_RISK"


if __name__ == "__main__":
    test_normalize_yahoo_ticker()
    test_is_within_earnings_blackout()
    test_split_next_and_last()
    test_yonex_7906_blackout_on_earnings_day()
    test_yonex_7906_without_suffix_uses_normalized_ticker()
    test_fail_closed_when_earnings_data_missing()
    test_fail_open_when_earnings_data_missing_and_not_strict_mode()
    test_should_fail_closed_for_mode()
    test_apply_earnings_fields_overrides_buy_signal()
    print("ok")
