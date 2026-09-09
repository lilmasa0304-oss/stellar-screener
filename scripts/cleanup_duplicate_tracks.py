#!/usr/bin/env python3
"""signal_tracks の銘柄コードゆれ（3465 / 3465.T）による重複を削除する。"""

from __future__ import annotations

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
logger = logging.getLogger("cleanup_duplicate_tracks")


def main() -> int:
    from screener import storage
    from screener.database import get_db_init_error

    if not storage.init_db():
        logger.error("DB 初期化に失敗しました: %s", get_db_init_error() or "unknown")
        return 1

    result = storage.cleanup_duplicate_signal_tracks()
    logger.info("cleanup result: %s", json.dumps(result, ensure_ascii=False))
    print(json.dumps({"status": "success", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
