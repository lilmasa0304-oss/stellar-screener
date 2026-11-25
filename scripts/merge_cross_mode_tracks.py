#!/usr/bin/env python3
"""signal_tracks のモード横断の重複（同一ユーザー・同一銘柄・同日）を1件に統合する。

案1（登録のモード横断1件化）に伴う既存データ移行用スクリプト。
PR のマージ・デプロイ後に、手動で1回だけ実行してください。

使い方:
    python scripts/merge_cross_mode_tracks.py --dry-run   # 件数だけ確認（DBは変更しない）
    python scripts/merge_cross_mode_tracks.py             # 実際に統合する

統合ルール:
    - グループ化キー: ユーザー × 銘柄（表記ゆれ正規化） × シグナル日
    - 残すレコード: 評価済み outcomes 件数 → 登録日時 → track_id の優先度
    - 全メンバーの検出モードは detected_modes に統合される
    - 削除側の outcomes は削除され、残す側の outcomes は不足分が補完される
      （pending の評価は次回の自動評価で再計算される）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("merge_cross_mode_tracks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="統合される件数だけを表示し、DBは変更しない",
    )
    args = parser.parse_args()

    from screener import storage
    from screener.database import get_db_init_error

    if not storage.init_db():
        logger.error("DB 初期化に失敗しました: %s", get_db_init_error() or "unknown")
        return 1

    result = storage.merge_cross_mode_signal_tracks(dry_run=args.dry_run)
    logger.info("merge result: %s", json.dumps(result, ensure_ascii=False))
    print(
        json.dumps(
            {"status": "success", "dry_run": args.dry_run, **result},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
