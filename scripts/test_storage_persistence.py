"""ストレージ永続化（DB パス解決・signal_tracks 保存）のユニットテスト。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from screener.db_path import is_persistent_storage, resolve_db_path
from screener import storage


@contextmanager
def env_override(**kwargs):
    """環境変数を一時的に上書きする。"""
    saved = {}
    for key, value in kwargs.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def test_resolve_db_path_explicit_env():
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "custom.db"
        with env_override(DB_PATH=str(db_file), RENDER=None):
            assert resolve_db_path() == db_file


def test_resolve_db_path_render_disk():
    with tempfile.TemporaryDirectory() as tmp:
        mount = Path(tmp) / "var_data"
        mount.mkdir()
        with env_override(
            RENDER="true",
            DB_PATH=None,
            RENDER_DISK_MOUNT=str(mount),
        ):
            assert resolve_db_path() == mount / "screener.db"
            assert is_persistent_storage(mount / "screener.db") is True


def test_resolve_db_path_local_default():
    with env_override(DB_PATH=None, RENDER=None):
        path = resolve_db_path()
        assert path.name == "screener.db"
        assert "data" in path.parts
        assert is_persistent_storage(path) is True


def test_signal_tracks_persist_across_reconnect():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_file = Path(tmp) / "test_signals.db"
        with env_override(DB_PATH=str(db_file), RENDER=None, SQLITE_TEST_MODE="1"):
            storage.refresh_db_path()
            storage.init_db()

            track_id = storage.register_signal_track(
                scan_id="test_scan",
                ticker="7203.T",
                name="トヨタ",
                signal_date="2026-08-18",
                entry_price=2500.0,
                risk_mode="堅実",
            )
            assert track_id is not None

        with env_override(DB_PATH=None, RENDER=None, SQLITE_TEST_MODE=None):
            storage.refresh_db_path()

        with sqlite3.connect(str(db_file)) as conn:
            row = conn.execute(
                "SELECT ticker, entry_price, risk_mode FROM signal_tracks WHERE track_id=?",
                (track_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "7203.T"
        assert row[1] == 2500.0
        assert row[2] == "堅実"


def test_ephemeral_render_path_not_persistent():
    with tempfile.TemporaryDirectory() as tmp:
        ephemeral = Path(tmp) / "data" / "screener.db"
        with env_override(
            RENDER="true",
            DB_PATH=str(ephemeral),
            RENDER_DISK_MOUNT="/nonexistent_mount_xyz",
        ):
            assert is_persistent_storage(ephemeral) is False


if __name__ == "__main__":
    test_resolve_db_path_explicit_env()
    test_resolve_db_path_render_disk()
    test_resolve_db_path_local_default()
    test_signal_tracks_persist_across_reconnect()
    test_ephemeral_render_path_not_persistent()
    print("ok")
