"""日本株銘柄コードの正規化・抽出（7203, 285A 等）。"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

# 4文字英数字（7203, 285A 等）— TSE 2024年〜
_JP_STOCK_CODE_ALPHA = r"\d{3}[A-Z]"
_JP_STOCK_CODE_DIGIT = r"\d{4}"
_JP_STOCK_CODE_ANY = r"[0-9A-Z]{4}"


def normalize_jp_stock_code(raw: str) -> Optional[str]:
    """銘柄コード文字列を正規化し、有効なら大文字コードを返す。"""
    token = unicodedata.normalize("NFKC", raw.strip()).upper().removesuffix(".T")
    if re.fullmatch(_JP_STOCK_CODE_ANY, token, re.IGNORECASE) and re.search(r"\d", token):
        return token
    return None


def find_jp_stock_code_in_text(text: str) -> Optional[str]:
    """文中から日本株銘柄コードを抽出する（英字付きコードを数字4桁より優先）。"""
    q = unicodedata.normalize("NFKC", text).upper()
    for pattern in (_JP_STOCK_CODE_ALPHA, _JP_STOCK_CODE_DIGIT):
        code_match = re.search(rf"({pattern})\.T", q) or re.search(rf"({pattern})", q)
        if code_match:
            return code_match.group(1).upper()
    return None


def extract_jp_stock_code(query: str) -> Optional[str]:
    """単一銘柄コードを query から抽出する。"""
    q = query.strip()
    if not q:
        return None
    direct = normalize_jp_stock_code(q)
    if direct:
        return direct
    return find_jp_stock_code_in_text(q)


def normalize_stock_codes_param(raw: Optional[str]) -> Optional[str]:
    """'7203', '285A.T', '7203,285A' などをカンマ区切りコードに正規化する。"""
    if not raw or not raw.strip():
        return None

    codes: List[str] = []
    for part in raw.split(","):
        token = normalize_jp_stock_code(part)
        if token and token not in codes:
            codes.append(token)
    return ",".join(codes) if codes else None


def split_stock_codes(raw: Optional[str]) -> List[str]:
    """カンマ区切り銘柄コードをリストに分解する。"""
    normalized = normalize_stock_codes_param(raw)
    if not normalized:
        return []
    return normalized.split(",")
