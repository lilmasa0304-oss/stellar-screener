"""Dify ワークフロー inputs へのマッピング（診断データを inputs 内にフラット展開）。"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

_INTERNAL_KEYS = frozenset({"fast_response", "local_diagnosis", "source"})

# Dify プロンプト {x} body が参照する inputs 内の集約 JSON 用キー
DIFY_BODY_INPUT_KEY = "body"

_DIFY_INPUT_FIELDS = (
    "status",
    "code",
    "ticker",
    "mode",
    "name",
    "current_price",
    "rsi",
    "ma25",
    "ma25_uptrend",
    "ma25_deviation_pct",
    "ma25_divergence_pct",
    "volume_ratio",
    "buy_signal",
    "trend_status",
    "preset_matched",
    "reason",
    "sector",
    "fundamental_grade",
    "fundamental_score",
    "preset_evaluations",
    "fundamentals",
)


def _is_bad_float(value: float) -> bool:
    return math.isnan(value) or math.isinf(value)


def _to_native_scalar(value: Any) -> Any:
    """numpy / pandas スカラーを Python ネイティブ型へ。"""
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return None
    return value


def sanitize_value(value: Any) -> Any:
    """JSON シリアライズ可能な値へ正規化（NaN / Inf を除去）。"""
    if value is None:
        return None

    value = _to_native_scalar(value)

    if isinstance(value, float):
        if _is_bad_float(value):
            return None
        return round(value, 4)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        text = value.strip()
        return text if text else None

    if isinstance(value, dict):
        return sanitize_dict(value)

    if isinstance(value, (list, tuple)):
        return sanitize_list(value)

    text = str(value).strip()
    return text if text else None


def sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """dict を再帰的に JSON 安全化する。"""
    cleaned: Dict[str, Any] = {}
    for key, value in data.items():
        if key in _INTERNAL_KEYS:
            continue
        sanitized = sanitize_value(value)
        if sanitized is None:
            continue
        if isinstance(sanitized, dict) and not sanitized:
            continue
        if isinstance(sanitized, list) and not sanitized:
            continue
        cleaned[str(key)] = sanitized
    return cleaned


def sanitize_list(items: list) -> list:
    """list を再帰的に JSON 安全化する。"""
    cleaned: list = []
    for item in items:
        sanitized = sanitize_value(item)
        if sanitized is None:
            continue
        cleaned.append(sanitized)
    return cleaned


def format_buy_signal(value: Any) -> str:
    """Dify 向け buy_signal 文言。"""
    if isinstance(value, bool):
        return "買いシグナル点灯" if value else "買いシグナルなし"
    if value in (None, ""):
        return "買いシグナルなし"
    return str(value)


def to_dify_input_string(key: str, value: Any) -> Optional[str]:
    """
    Dify Chat-App API 用 inputs 値（すべて文字列）。
    Dify API は inputs の値型に string を要求するため、最終段階で統一する。
    """
    if key == "buy_signal":
        return format_buy_signal(value)

    sanitized = sanitize_value(value)
    if sanitized is None:
        return None

    if isinstance(sanitized, bool):
        return "true" if sanitized else "false"

    if isinstance(sanitized, (dict, list)):
        return json.dumps(sanitized, ensure_ascii=False)

    if isinstance(sanitized, float):
        text = f"{sanitized:.4f}".rstrip("0").rstrip(".")
        return text or "0"

    if isinstance(sanitized, int):
        return str(sanitized)

    return str(sanitized)


def prepare_dify_body_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Dify inputs 用の診断 dict を組み立てる。"""
    payload = sanitize_dict(dict(data))

    if "ma25_deviation_pct" not in payload and payload.get("ma25_divergence_pct") is not None:
        payload["ma25_deviation_pct"] = payload["ma25_divergence_pct"]

    if "code" not in payload and payload.get("ticker"):
        payload["code"] = payload["ticker"]
    if "ticker" not in payload and payload.get("code"):
        payload["ticker"] = payload["code"]
    payload.setdefault("status", "success")
    return payload


def build_dify_inputs(
    screen_data: Optional[Dict[str, Any]],
    code: Optional[str] = None,
    mode: Optional[str] = None,
) -> Dict[str, str]:
    """
    Dify Chat-App API の inputs を組み立てる。
    各変数を inputs 内にフラット展開（すべて string）し、body に JSON スナップショットも格納。
    """
    if screen_data:
        payload = prepare_dify_body_payload(screen_data)
    else:
        display_code = (code or "").strip().removesuffix(".T").upper() or None
        payload = sanitize_dict({
            "status": "success",
            "code": display_code,
            "ticker": display_code,
            "mode": mode,
        })

    inputs: Dict[str, str] = {}
    for key in _DIFY_INPUT_FIELDS:
        if key not in payload:
            continue
        text = to_dify_input_string(key, payload[key])
        if text is not None and text != "":
            inputs[key] = text

    for key, value in payload.items():
        if key in inputs:
            continue
        text = to_dify_input_string(key, value)
        if text is not None and text != "":
            inputs[key] = text

    inputs[DIFY_BODY_INPUT_KEY] = json.dumps(payload, ensure_ascii=False)
    return inputs
