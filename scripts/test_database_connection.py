#!/usr/bin/env python3
"""DATABASE_URL 接続テスト（Supabase PostgreSQL / ローカル SQLite）。"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _set_raw_mode(enabled: bool) -> None:
    if enabled:
        os.environ["DATABASE_URL_RAW"] = "1"
    else:
        os.environ.pop("DATABASE_URL_RAW", None)


def _run_connection_steps() -> None:
    from screener.database import (
        connect,
        fetchone,
        get_database_url_mode,
        get_db_init_error,
        get_storage_info,
        mask_database_url,
        reset_engine,
        resolve_database_url,
    )
    from screener import storage

    reset_engine()
    storage.refresh_db_path()

    url, backend = resolve_database_url()
    info = get_storage_info()
    print(f"[1/4] backend={backend}")
    print(f"      mode={get_database_url_mode()}")
    print(f"      persistent={info.get('db_persistent')}")
    print(f"      url={info.get('database_url_masked') or mask_database_url(url)}")

    print("[2/4] 接続確認...")
    with connect() as conn:
        row = fetchone(conn, "SELECT 1 AS ok")
        if not row or row.get("ok") != 1:
            raise RuntimeError("SELECT 1 が失敗しました")
    print("      OK")

    print("[3/4] スキーマ初期化...")
    if not storage.init_db():
        detail = get_db_init_error() or "unknown"
        raise RuntimeError(f"スキーマ初期化に失敗しました: {detail}")
    print("      OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="DATABASE_URL 接続・スキーマ・書き込みテスト")
    parser.add_argument(
        "--url",
        help="テストする DATABASE_URL（未指定時は環境変数 DATABASE_URL）",
    )
    parser.add_argument(
        "--skip-write-test",
        action="store_true",
        help="書き込みテスト（検証リスト登録）をスキップ",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="DATABASE_URL をそのまま使用（Supabase 自動変換をスキップ）",
    )
    parser.add_argument(
        "--fallback-rewrite",
        action="store_true",
        help="raw モード失敗時に Supabase URL 自動変換で再試行",
    )
    args = parser.parse_args()

    database_url = (args.url or os.getenv("DATABASE_URL", "")).strip().strip('"').strip("'")
    if not database_url:
        print("ERROR: DATABASE_URL が未設定です。", file=sys.stderr)
        return 1

    os.environ["DATABASE_URL"] = database_url
    use_raw = args.raw or _env_truthy("DATABASE_URL_RAW") or _env_truthy("SUPABASE_SKIP_URL_REWRITE")
    _set_raw_mode(use_raw)

    try:
        _run_connection_steps()
    except Exception as first_exc:
        print(f"ERROR: DATABASE_URL 接続テスト中に例外: {first_exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        allow_fallback = args.fallback_rewrite or _env_truthy("SUPABASE_URL_FALLBACK")
        if use_raw and allow_fallback:
            print(
                "WARN: raw モードで失敗したため Supabase URL 自動変換で再試行します...",
                file=sys.stderr,
            )
            _set_raw_mode(False)
            try:
                _run_connection_steps()
            except Exception as retry_exc:
                print(f"ERROR: フォールバック接続も失敗: {retry_exc}", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)
                return 1
        else:
            return 1

    if args.skip_write_test:
        print("[4/4] 書き込みテスト: スキップ")
        print("\nSUCCESS: DATABASE_URL 接続は正常です。")
        return 0

    from screener.database import connect, execute, fetchone
    from screener import storage

    try:
        print("[4/4] 書き込みテスト（signal_tracks）...")
        track_id = storage.register_signal_track(
            scan_id="connection_test",
            ticker="0000.T",
            name="接続テスト",
            signal_date="2099-01-01",
            entry_price=1.0,
            risk_mode="堅実",
        )
        if track_id is None:
            raise RuntimeError("register_signal_track が None を返しました")

        row = None
        with connect() as conn:
            row = fetchone(
                conn,
                "SELECT track_id, ticker FROM signal_tracks WHERE track_id=?",
                (track_id,),
            )
        if not row:
            raise RuntimeError("登録した track が読み取れません")

        with connect() as conn:
            execute(conn, "DELETE FROM signal_track_outcomes WHERE track_id=?", (track_id,))
            execute(conn, "DELETE FROM signal_tracks WHERE track_id=?", (track_id,))

        print(f"      OK (track_id={track_id}, cleaned up)")
        print("\nSUCCESS: DATABASE_URL 接続・スキーマ・読み書きすべて正常です。")
        return 0
    except Exception as exc:
        print(f"ERROR: 書き込みテスト中に例外: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
