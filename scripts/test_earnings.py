"""決算ブラックアウト判定のユニットテスト。"""

from datetime import date

from screener.earnings import (
    EarningsBlackoutResult,
    _split_next_and_last,
    apply_earnings_fields,
    is_within_earnings_blackout,
)


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
    today = date(2026, 8, 1)
    next_date, last_date = _split_next_and_last(
        [date(2026, 5, 1), date(2026, 8, 10), date(2026, 11, 1)],
        today,
    )
    assert last_date == date(2026, 5, 1)
    assert next_date == date(2026, 8, 10)


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
    test_is_within_earnings_blackout()
    test_split_next_and_last()
    test_apply_earnings_fields_overrides_buy_signal()
    print("ok")
