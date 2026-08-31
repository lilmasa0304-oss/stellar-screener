#!/usr/bin/env python3
"""DATABASE_URL 接続テスト（Supabase PostgreSQL / ローカル SQLite）。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
    args = parser.parse_args()

    database_url = (args.url or os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        print("ERROR: DATABASE_URL が未設定です。", file=sys.stderr)
        return 1

    os.environ["DATABASE_URL"] = database_url

    from screener.database import (
        connect,
        fetchone,
        get_backend,
        get_storage_info,
        reset_engine,
        resolve_database_url,
    )
    from screener import storage

    reset_engine()
    storage.refresh_db_path()

    url, backend = resolve_database_url()
    info = get_storage_info()
    print(f"[1/4] backend={backend}")
    print(f"      persistent={info.get('db_persistent')}")
    print(f"      url={info.get('database_url_masked') or url}")

    print("[2/4] 接続確認...")
    with connect() as conn:
        row = fetchone(conn, "SELECT 1 AS ok")
        if not row or row.get("ok") != 1:
            print("ERROR: SELECT 1 が失敗しました。", file=sys.stderr)
            return 1
    print("      OK")

    print("[3/4] スキーマ初期化...")
    storage.init_db()
    print("      OK")

    if args.skip_write_test:
        print("[4/4] 書き込みテスト: スキップ")
        print("\nSUCCESS: DATABASE_URL 接続は正常です。")
        return 0

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
        print("ERROR: register_signal_track が None を返しました。", file=sys.stderr)
        return 1

    row = None
    with connect() as conn:
        row = fetchone(
            conn,
            "SELECT track_id, ticker FROM signal_tracks WHERE track_id=?",
            (track_id,),
        )
    if not row:
        print("ERROR: 登録した track が読み取れません。", file=sys.stderr)
        return 1

    with connect() as conn:
        from screener.database import execute

        execute(conn, "DELETE FROM signal_track_outcomes WHERE track_id=?", (track_id,))
        execute(conn, "DELETE FROM signal_tracks WHERE track_id=?", (track_id,))

    print(f"      OK (track_id={track_id}, cleaned up)")
    print("\nSUCCESS: DATABASE_URL 接続・スキーマ・読み書きすべて正常です。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
