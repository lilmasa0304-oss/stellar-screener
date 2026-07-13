"""Dify HTTP ノード向けの銘柄コード解決ヘルパー。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from screener.jp_stock_code import extract_jp_stock_code, normalize_jp_stock_code


def resolve_dify_stock_code(*candidates: Optional[str]) -> Optional[str]:
    """code / ticker / query / input_value などから銘柄コードを解決する。"""
    for raw in candidates:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        normalized = normalize_jp_stock_code(text) or extract_jp_stock_code(text)
        if normalized:
            return normalized
    return None


def resolve_code_from_mapping(data: Dict[str, Any]) -> Optional[str]:
    """JSON ボディから Dify 互換フィールドを読み取って銘柄コードを返す。"""
    return resolve_dify_stock_code(
        data.get("code"),
        data.get("ticker"),
        data.get("query"),
        data.get("input_value"),
        data.get("input"),
    )
