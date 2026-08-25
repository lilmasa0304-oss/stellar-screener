"""ストレージ永続化（DB パス解決・signal_tracks 保存）のユニットテスト。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from screener.database import is_persistent_storage, reset_engine, resolve_database_url
from screener import storage


@contextmanager
def env_override(**kwargs):
    saved = {}
    for key, value in kwargs.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        reset_engine()
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        reset_engine()


def test_resolve_database_url_explicit_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "custom.db"
        with env_override(DATABASE_URL=f"sqlite:///{db_file.as_posix()}", RENDER=None):
            url, backend = resolve_database_url()
            assert backend == "sqlite"
            assert url.startswith("sqlite:///")


def test_resolve_database_url_local_fallback():
    with env_override(DATABASE_URL=None, RENDER=None):
        url, backend = resolve_database_url()
        assert backend == "sqlite"
        assert url.startswith("sqlite:///")


def test_external_database_is_persistent():
    with env_override(DATABASE_URL="postgresql://user:pass@host/db", RENDER="true"):
        assert is_persistent_storage() is True


def test_render_without_database_url_not_persistent():
    with env_override(DATABASE_URL=None, RENDER="true"):
        assert is_persistent_storage() is False


def test_signal_tracks_persist_across_reconnect():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_file = Path(tmp) / "test_signals.db"
        with env_override(
            DATABASE_URL=f"sqlite:///{db_file.as_posix()}",
            RENDER=None,
            SQLITE_TEST_MODE="1",
        ):
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

        with env_override(DATABASE_URL=None, RENDER=None, SQLITE_TEST_MODE=None):
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


def test_storage_info_local():
    with env_override(DATABASE_URL=None, RENDER=None):
        info = storage.get_storage_info()
        assert info["backend"] == "sqlite"
        assert info["db_persistent"] is True
        assert info["database_url_set"] is False


if __name__ == "__main__":
    test_resolve_database_url_explicit_sqlite()
    test_resolve_database_url_local_fallback()
    test_external_database_is_persistent()
    test_render_without_database_url_not_persistent()
    test_signal_tracks_persist_across_reconnect()
    test_storage_info_local()
    print("ok")
