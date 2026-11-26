"""スキャン結果の表示重複排除（dedupe_scan_hits_for_display）のユニットテスト。"""

import sys
from unittest.mock import MagicMock

try:
    import tenacity  # noqa: F401
except ModuleNotFoundError:
    sys.modules["tenacity"] = MagicMock()

from screener.signal_tracker import dedupe_scan_hits_for_display


def _row(ticker, mode):
    return {"ticker": ticker, "risk_mode": mode, "name": ticker}


def test_cross_mode_hit_shows_only_in_strictest_mode():
    # 2026-11-26 夜の実例：パイロットが標準・積極の両方、三井倉庫が積極のみ
    rows = {
        "堅実": [],
        "標準": [_row("7846", "標準")],
        "積極": [_row("7846", "積極"), _row("9302", "積極")],
    }
    moved = dedupe_scan_hits_for_display(rows)
    assert [r["ticker"] for r in rows["標準"]] == ["7846"]
    assert rows["標準"][0]["detected_modes"] == ["標準", "積極"]
    assert [r["ticker"] for r in rows["積極"]] == ["9302"]
    assert rows["積極"][0]["detected_modes"] == ["積極"]
    assert moved == {"堅実": 0, "標準": 0, "積極": 1}


def test_strictest_mode_wins_over_looser_modes():
    rows = {
        "堅実": [_row("1111", "堅実")],
        "標準": [_row("1111", "標準")],
        "積極": [_row("1111", "積極")],
    }
    moved = dedupe_scan_hits_for_display(rows)
    assert [r["ticker"] for r in rows["堅実"]] == ["1111"]
    assert rows["堅実"][0]["detected_modes"] == ["堅実", "標準", "積極"]
    assert rows["標準"] == [] and rows["積極"] == []
    assert moved["標準"] == 1 and moved["積極"] == 1


def test_single_mode_and_no_hits_are_unchanged():
    rows = {"標準": [_row("2222", "標準")], "積極": []}
    moved = dedupe_scan_hits_for_display(rows)
    assert [r["ticker"] for r in rows["標準"]] == ["2222"]
    assert rows["標準"][0]["detected_modes"] == ["標準"]
    assert moved == {"標準": 0, "積極": 0}


def test_distinct_tickers_are_all_kept():
    rows = {
        "標準": [_row("3333", "標準"), _row("4444", "標準")],
        "積極": [_row("5555", "積極")],
    }
    moved = dedupe_scan_hits_for_display(rows)
    assert [r["ticker"] for r in rows["標準"]] == ["3333", "4444"]
    assert [r["ticker"] for r in rows["積極"]] == ["5555"]
    assert moved == {"標準": 0, "積極": 0}
