#!/usr/bin/env python3
"""フォワードテスト追跡バッチ（株価更新・horizon 評価・確定）。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# GitHub Actions 等で `python scripts/run_tracking_batch.py` 実行時も
# リポジトリルートを import パスに含める（main.py と同等の挙動）。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("tracking_batch")


def main() -> int:
    parser = argparse.ArgumentParser(description="検証リストの株価更新・成績評価バッチ")
    parser.add_argument("--limit", type=int, default=500, help="評価する最大銘柄数")
    parser.add_argument("--summary", action="store_true", help="評価後にサマリーを出力")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        logger.error(
            "DATABASE_URL が未設定です。"
            " GitHub Repo → Settings → Secrets → Actions に追加してください。"
        )
        return 1

    from screener import storage
    from screener.database import get_db_init_error, mask_database_url
    from screener.signal_tracker import build_tracking_summary, evaluate_pending_tracks

    info = storage.get_storage_info()
    logger.info("storage info: %s", json.dumps(info, ensure_ascii=False))
    logger.info("DATABASE_URL (masked): %s", mask_database_url(database_url))

    if not storage.init_db():
        detail = get_db_init_error() or "unknown"
        logger.error(
            "DB 初期化に失敗しました。DATABASE_URL・Supabase ネットワーク設定を確認してください。"
            " error=%s",
            detail,
        )
        return 1

    result = evaluate_pending_tracks(limit=args.limit)
    logger.info(
        "追跡評価完了: tracks_checked=%s outcomes_updated=%s",
        result.get("tracks_checked"),
        result.get("outcomes_updated"),
    )

    if args.summary:
        summary = build_tracking_summary(auto_evaluate=False)
        logger.info("summary: %s", json.dumps(summary, ensure_ascii=False, indent=2))

    print(json.dumps({"status": "success", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
