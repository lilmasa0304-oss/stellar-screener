"""Dify ワークフロー API クライアント（株価取得・AI診断は Dify 側で完結）。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from screener.jp_stock_code import normalize_jp_stock_code

logger = logging.getLogger(__name__)

DEFAULT_DIFY_API_URL = "https://api.dify.ai/v1"
DEFAULT_DIFY_USER = "render_user"
DIFY_INPUT_KEY = "input_value"
DIFY_WORKFLOW_TIMEOUT_SEC = 120
DIFY_MAX_ATTEMPTS = 3

_PLACEHOLDER_KEYS = frozenset({
    "your_dify_api_key_here",
    "app-xxxxxxxx",
})

_dotenv_loaded = False


def _ensure_dotenv() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(override=False)
        _dotenv_loaded = True


def _normalize_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def get_dify_api_key() -> str:
    _ensure_dotenv()
    return _normalize_secret(os.environ.get("DIFY_API_KEY"))


def get_dify_api_url() -> str:
    """Dify API のベース URL（DIFY_API_URL / DIFY_BASE_URL 互換）。"""
    _ensure_dotenv()
    url = (
        _normalize_secret(os.environ.get("DIFY_API_URL"))
        or _normalize_secret(os.environ.get("DIFY_BASE_URL"))
        or DEFAULT_DIFY_API_URL
    )
    return url.rstrip("/")


def get_dify_user() -> str:
    _ensure_dotenv()
    return _normalize_secret(os.environ.get("DIFY_USER")) or DEFAULT_DIFY_USER


def is_dify_configured() -> bool:
    key = get_dify_api_key()
    if not key:
        return False
    if key.lower() in _PLACEHOLDER_KEYS:
        return False
    return len(key) >= 10


def to_dify_input_value(code: str) -> str:
    """銘柄コードを Dify ワークフロー input_value 形式（例: 7203.T）へ変換する。"""
    normalized = normalize_jp_stock_code(code) or code.strip().upper().removesuffix(".T")
    if not normalized:
        raise ValueError(f"無効な銘柄コード: {code}")
    return f"{normalized}.T"


def _workflow_run_url() -> str:
    return f"{get_dify_api_url()}/workflows/run"


def _request_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_dify_api_key()}",
        "Content-Type": "application/json",
    }


def _extract_workflow_answer(payload: Dict[str, Any]) -> str:
    data = payload.get("data") or {}
    status = data.get("status")
    if status == "failed":
        raise RuntimeError(data.get("error") or "Dify ワークフローが failed になりました。")

    outputs = data.get("outputs") or {}
    for key in ("text", "result", "answer", "output", "response", "diagnosis"):
        value = outputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    if outputs:
        for value in outputs.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and not isinstance(value, (dict, list)):
                text = str(value).strip()
                if text:
                    return text

    raise RuntimeError("Dify ワークフローから空の応答が返されました。")


@retry(
    reraise=True,
    stop=stop_after_attempt(DIFY_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _post_workflow(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        _workflow_run_url(),
        headers=_request_headers(),
        json=payload,
        timeout=DIFY_WORKFLOW_TIMEOUT_SEC,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(
            f"Dify API エラー (HTTP {response.status_code}): {detail}"
        )
    return response.json()


def call_dify_workflow(
    input_value: str,
    *,
    user: Optional[str] = None,
) -> str:
    """Dify ワークフロー API を呼び出し、診断テキストを返す。"""
    if not is_dify_configured():
        raise RuntimeError(
            "DIFY_API_KEY が未設定です。環境変数に API キーを設定してください。"
        )

    payload = {
        "inputs": {DIFY_INPUT_KEY: input_value},
        "response_mode": "blocking",
        "user": user or get_dify_user(),
    }
    logger.info(
        "Dify ワークフロー呼び出し: input_value=%s user=%s url=%s",
        input_value,
        payload["user"],
        _workflow_run_url(),
    )
    try:
        result = _post_workflow(payload)
        answer = _extract_workflow_answer(result)
        logger.info("Dify ワークフロー成功: input_value=%s chars=%d", input_value, len(answer))
        return answer
    except Exception as exc:
        logger.error(
            "Dify ワークフロー失敗: input_value=%s error=%s",
            input_value,
            exc,
            exc_info=True,
        )
        raise


def call_dify_workflow_for_codes(codes: List[str]) -> str:
    """複数銘柄を順に Dify へ送り、診断結果を結合する。"""
    if not codes:
        raise ValueError("銘柄コードが空です。")

    sections: List[str] = []
    for code in codes:
        input_value = to_dify_input_value(code)
        answer = call_dify_workflow(input_value)
        display_code = code.removesuffix(".T")
        if len(codes) == 1:
            return answer
        sections.append(f"## {display_code}\n{answer}")
    return "\n\n".join(sections)


def probe_dify_connection(test_input: str = "7203.T") -> Dict[str, Any]:
    """Dify ワークフロー API の到達性を軽量チェックする。"""
    result: Dict[str, Any] = {
        "configured": is_dify_configured(),
        "api_url": get_dify_api_url(),
        "workflow_url": _workflow_run_url(),
        "user": get_dify_user(),
        "reachable": False,
        "test_input": test_input,
    }
    if not is_dify_configured():
        result["error"] = "DIFY_API_KEY が未設定です。"
        return result

    try:
        parameters = requests.get(
            f"{get_dify_api_url()}/parameters",
            headers=_request_headers(),
            timeout=20,
        )
        result["parameters_status"] = parameters.status_code
        if parameters.ok:
            result["parameters_ok"] = True
    except Exception as exc:
        result["parameters_ok"] = False
        result["parameters_error"] = str(exc)

    try:
        answer = call_dify_workflow(test_input)
        result["reachable"] = True
        result["answer_preview"] = answer[:200]
    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return result
