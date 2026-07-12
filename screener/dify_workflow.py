"""Dify チャットフロー API クライアント（株価取得・AI診断は Dify 側で完結）。"""

from __future__ import annotations

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
DIFY_CHAT_TIMEOUT_SEC = 120
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
    """銘柄コードを Dify チャットフロー query 形式（例: 7203.T）へ変換する。"""
    normalized = normalize_jp_stock_code(code) or code.strip().upper().removesuffix(".T")
    if not normalized:
        raise ValueError(f"無効な銘柄コード: {code}")
    return f"{normalized}.T"


def _chat_messages_url() -> str:
    return f"{get_dify_api_url()}/chat-messages"


def _request_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_dify_api_key()}",
        "Content-Type": "application/json",
    }


def _extract_chat_answer(payload: Dict[str, Any]) -> str:
    answer = payload.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    raise RuntimeError("Dify チャットフローから空の応答が返されました。")


@retry(
    reraise=True,
    stop=stop_after_attempt(DIFY_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _post_chat_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        _chat_messages_url(),
        headers=_request_headers(),
        json=payload,
        timeout=DIFY_CHAT_TIMEOUT_SEC,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(
            f"Dify API エラー (HTTP {response.status_code}): {detail}"
        )
    return response.json()


def call_dify_workflow(
    keyword: str,
    *,
    user: Optional[str] = None,
) -> str:
    """Dify チャットフロー API を呼び出し、診断テキストを返す。"""
    if not is_dify_configured():
        raise RuntimeError(
            "DIFY_API_KEY が未設定です。環境変数に API キーを設定してください。"
        )

    query = (keyword or "").strip()
    if not query:
        raise ValueError("Dify へ送る query が空です。")

    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "blocking",
        "user": user or get_dify_user(),
    }
    logger.info(
        "Dify チャットフロー呼び出し: query=%s user=%s url=%s",
        query,
        payload["user"],
        _chat_messages_url(),
    )
    try:
        result = _post_chat_message(payload)
        answer = _extract_chat_answer(result)
        logger.info("Dify チャットフロー成功: query=%s chars=%d", query, len(answer))
        return answer
    except Exception as exc:
        logger.error(
            "Dify チャットフロー失敗: query=%s error=%s",
            query,
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
        query = to_dify_input_value(code)
        answer = call_dify_workflow(query)
        display_code = code.removesuffix(".T")
        if len(codes) == 1:
            return answer
        sections.append(f"## {display_code}\n{answer}")
    return "\n\n".join(sections)


def probe_dify_connection(test_input: str = "7203.T") -> Dict[str, Any]:
    """Dify チャットフロー API の到達性を軽量チェックする。"""
    result: Dict[str, Any] = {
        "configured": is_dify_configured(),
        "api_url": get_dify_api_url(),
        "chat_messages_url": _chat_messages_url(),
        "user": get_dify_user(),
        "reachable": False,
        "test_query": test_input,
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
