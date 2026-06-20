"""One-off script to rebuild screener/jpx400.py from extracted ticker codes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
lines = (ROOT / "jpx400_codes_temp.txt").read_text(encoding="utf-8").strip().splitlines()
count = int(lines[0])
tickers = lines[1:]
assert len(tickers) == count

header = '''"""
JPX400 (JPX日経インデックス400) 構成銘柄リスト
出典: JPX 公式 PDF (2025年8月29日適用予定)
https://www.jpx.co.jp/markets/indices/line-up/files/mei2_1_jpx400.pdf

get_jpx400_tickers() はキャッシュ付き静的リストを返す。
fetch_jpx400_tickers_from_jpx() で JPX PDF から動的取得も可能（要 pypdf）。
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import List, Optional

logger = logging.getLogger(__name__)

_JPX400_PDF_URL = (
    "https://www.jpx.co.jp/markets/indices/line-up/files/mei2_1_jpx400.pdf"
)

_JPX400_TICKERS: List[str] = [
'''

footer = '''
]

_cached_tickers: Optional[List[str]] = None


def fetch_jpx400_tickers_from_jpx() -> List[str]:
    """JPX 公式 PDF から構成銘柄コードを動的取得する（失敗時は空リスト）。"""
    try:
        import requests
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf / requests が未インストールのため動的取得をスキップします。")
        return []

    try:
        resp = requests.get(
            _JPX400_PDF_URL,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; STELLAR-SCREENER/1.0)"},
        )
        resp.raise_for_status()
        reader = PdfReader(BytesIO(resp.content))
        text = "".join(page.extract_text() or "" for page in reader.pages)
        codes: List[str] = []
        seen: set[str] = set()
        for match in re.findall(r"\\b(\\d{4})\\b", text):
            if 1000 <= int(match) <= 9999 and match not in seen:
                seen.add(match)
                codes.append(f"{match}.T")
        if len(codes) >= 350:
            logger.info("JPX PDF から %d 銘柄を取得しました。", len(codes))
            return codes
        logger.warning("JPX PDF からの取得件数が不足 (%d)。静的リストを使用します。", len(codes))
    except Exception as e:
        logger.warning("JPX PDF 取得失敗: %s", e)
    return []


def get_jpx400_tickers(use_dynamic: bool = False) -> List[str]:
    """
    JPX400 構成銘柄のティッカーシンボルリストを返す。

    Args:
        use_dynamic: True のとき JPX PDF からの動的取得を試みる（失敗時は静的リスト）

    Returns:
        例: ['7203.T', '6758.T', ...]  (Yahoo Finance 形式)
    """
    global _cached_tickers
    if _cached_tickers is not None:
        return list(_cached_tickers)

    if use_dynamic:
        dynamic = fetch_jpx400_tickers_from_jpx()
        if dynamic:
            _cached_tickers = dynamic
            return list(_cached_tickers)

    _cached_tickers = list(_JPX400_TICKERS)
    return list(_cached_tickers)


def get_jpx400_count() -> int:
    """JPX400 銘柄数を返す。"""
    return len(get_jpx400_tickers())
'''

body = "".join(f'    "{t}",\n' for t in tickers)
(ROOT / "screener" / "jpx400.py").write_text(header + body + footer, encoding="utf-8")
print(f"wrote {len(tickers)} tickers to screener/jpx400.py")
