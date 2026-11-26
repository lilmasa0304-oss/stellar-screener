"""共有モード（2026-11-27）のユニットテスト。

検証リストをユーザー別に分けず全員で共有する変更の要：
- 重複判定（register_signal_track）がユーザーを横断すること
- user_id=None の読み取りが全ユーザーの行を返すこと
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock

try:
    import tenacity  # noqa: F401
except ModuleNotFoundError:
    sys.modules["tenacity"] = MagicMock()

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from screener.database import reset_engine

reset_engine()

from screener import storage

storage.init_db()


def _register(ticker, *, scan_id, mode, user_id, date="2026-11-26"):
    return storage.register_signal_track(
        scan_id=scan_id,
        ticker=ticker,
        name=ticker,
        signal_date=date,
        entry_price=1000.0,
        preset_matched="junbari",
        risk_mode=mode,
        user_id=user_id,
    )


def test_dedupe_crosses_users():
    first = _register("7846", scan_id="s1", mode="標準", user_id="masa-phone")
    assert first is not None
    # 別ユーザー（共有ID）・別モード・別スキャンでも同じ銘柄/同日なら1件に統合
    second = _register("7846", scan_id="s2", mode="積極", user_id="shared")
    assert second == first
    tracks = storage.list_signal_tracks(user_id=None)
    assert len(tracks) == 1
    assert tracks[0]["detected_modes"] == "標準,積極"


def test_unfiltered_read_sees_all_users():
    _register("9302", scan_id="s3", mode="積極", user_id="shared")
    all_tracks = storage.list_signal_tracks(user_id=None)
    assert {t["ticker"] for t in all_tracks} == {"7846.T", "9302.T"}
    # 参考：特定ユーザーで絞ると、そのユーザーの行だけに絞られる（＝Web層は None を使う）
    assert len(storage.list_signal_tracks(user_id="masa-phone")) == 1
    assert len(storage.list_signal_tracks(user_id="shared")) == 1


def test_different_date_is_a_separate_track():
    _register("7846", scan_id="s4", mode="標準", user_id="shared", date="2026-11-27")
    tracks = storage.list_signal_tracks(user_id=None)
    assert len([t for t in tracks if t["ticker"] == "7846.T"]) == 2
