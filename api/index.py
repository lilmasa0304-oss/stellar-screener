"""
Vercel サーバーレス用エントリーポイント。
ローカル開発は web_app.py のまま、Vercel ではこのファイル経由で FastAPI を起動する。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_app import app  # noqa: E402
