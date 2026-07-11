"""OpenAI HTTP クライアント（Render 向け IPv4 安定化・プロキシ対応）。"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_TIMEOUT_SEC = 60.0
OPENAI_CONNECT_TIMEOUT_SEC = 30.0
OPENAI_MAX_ATTEMPTS = 3
OPENAI_RETRY_WAIT_MIN_SEC = 2
OPENAI_RETRY_WAIT_MAX_SEC = 10

RETRYABLE_EXCEPTIONS = (APIConnectionError, APITimeoutError, RateLimitError)

_PLACEHOLDER_KEYS = frozenset({
    "your_openai_api_key_here",
    "sk-your-key-here",
    "sk-xxxxxxxx",
})

_ipv4_dns_patch_applied = False
_orig_getaddrinfo = socket.getaddrinfo
_dotenv_loaded = False
_client_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None
_openai_client: Optional[OpenAI] = None
_cached_client_key: Optional[tuple[str, str]] = None


def _ensure_dotenv() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(override=False)
        _dotenv_loaded = True


def _normalize_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def get_openai_api_key() -> str:
    _ensure_dotenv()
    return _normalize_secret(os.environ.get("OPENAI_API_KEY"))


def get_openai_base_url() -> str:
    """OPENAI_BASE_URL を取得（Cloudflare AI Gateway 等のプロキシ対応）。"""
    _ensure_dotenv()
    base_url = _normalize_secret(os.environ.get("OPENAI_BASE_URL")) or DEFAULT_BASE_URL
    return base_url.rstrip("/")


def get_openai_model() -> str:
    _ensure_dotenv()
    return _normalize_secret(os.environ.get("OPENAI_MODEL")) or "gpt-4o"


def is_openai_configured() -> bool:
    key = get_openai_api_key()
    if not key:
        return False
    if key.lower() in _PLACEHOLDER_KEYS:
        return False
    if key.startswith("sk-"):
        return len(key) > 20
    return len(key) >= 20


def _resolve_api_host(base_url: str) -> str:
    hostname = urlparse(base_url).hostname
    return hostname or "api.openai.com"


def _ipv4_only_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
):
    sock_type = type or socket.SOCK_STREAM
    return _orig_getaddrinfo(host, port, socket.AF_INET, sock_type, proto, flags)


def _ensure_ipv4_dns_resolution() -> None:
    global _ipv4_dns_patch_applied
    if _ipv4_dns_patch_applied:
        return
    socket.getaddrinfo = _ipv4_only_getaddrinfo
    _ipv4_dns_patch_applied = True
    logger.info("OpenAI 通信: socket.getaddrinfo を IPv4 (AF_INET) のみに制限しました")


def _log_openai_network_diagnostics(api_host: str) -> None:
    try:
        addresses = _orig_getaddrinfo(
            api_host,
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        ipv4_list = sorted({item[4][0] for item in addresses})
        logger.info(
            "OpenAI DNS probe (IPv4): host=%s base_url=%s -> %s",
            api_host,
            get_openai_base_url(),
            ipv4_list,
        )
    except OSError as exc:
        logger.error(
            "OpenAI DNS probe failed (IPv4): host=%s errno=%s message=%s",
            api_host,
            getattr(exc, "errno", None),
            exc,
        )


def _exception_cause_chain(exc: BaseException) -> List[str]:
    chain: List[str] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return chain


def log_openai_exception(exc: BaseException, *, model: str, stage: str) -> None:
    details = {
        "stage": stage,
        "model": model,
        "base_url": get_openai_base_url(),
        "exc_type": type(exc).__name__,
        "message": str(exc),
    }
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        details["status_code"] = status_code
    request_id = getattr(exc, "request_id", None)
    if request_id:
        details["request_id"] = request_id
    body = getattr(exc, "body", None)
    if body:
        details["body"] = body
    cause_chain = _exception_cause_chain(exc)
    if cause_chain:
        details["cause_chain"] = cause_chain
        details["root_cause"] = cause_chain[-1]
    logger.error(
        "OpenAI API エラー詳細: %s",
        json.dumps(details, ensure_ascii=False, default=str),
        exc_info=True,
    )


def _build_http_client() -> httpx.Client:
    """IPv4 固定・接続プール制御付き httpx.Client を構築する。"""
    _ensure_ipv4_dns_resolution()
    api_host = _resolve_api_host(get_openai_base_url())
    _log_openai_network_diagnostics(api_host)

    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=0)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=5)
    timeout = httpx.Timeout(
        OPENAI_TIMEOUT_SEC,
        connect=OPENAI_CONNECT_TIMEOUT_SEC,
    )
    return httpx.Client(
        transport=transport,
        limits=limits,
        timeout=timeout,
        trust_env=False,
        http2=False,
    )


def reset_openai_client() -> None:
    """接続エラー後にクライアントを破棄し、次回呼び出しで再構築する。"""
    global _http_client, _openai_client, _cached_client_key
    with _client_lock:
        if _http_client is not None:
            try:
                _http_client.close()
            except Exception:
                pass
        _http_client = None
        _openai_client = None
        _cached_client_key = None


def _before_retry_sleep(retry_state) -> None:
    """リトライ前に待機ログを出し、接続系エラー時は HTTP クライアントを再生成する。"""
    before_sleep_log(logger, logging.WARNING)(retry_state)
    outcome = retry_state.outcome
    if outcome is None or not outcome.failed:
        return
    exc = outcome.exception()
    if isinstance(exc, APIConnectionError):
        logger.warning(
            "OpenAI 接続エラーのため HTTP クライアントをリセットします (attempt=%s)",
            retry_state.attempt_number,
        )
        reset_openai_client()


def create_openai_client(*, force_new: bool = False) -> OpenAI:
    """OpenAI クライアントを生成する（IPv4 強制・base_url 可変・接続プール共有）。"""
    if not is_openai_configured():
        raise RuntimeError(
            "OPENAI_API_KEY が未設定です。.env または環境変数に設定してください。"
        )

    api_key = get_openai_api_key()
    base_url = get_openai_base_url()
    client_key = (api_key, base_url)

    with _client_lock:
        global _http_client, _openai_client, _cached_client_key
        if (
            not force_new
            and _openai_client is not None
            and _cached_client_key == client_key
        ):
            return _openai_client

        if _http_client is not None:
            try:
                _http_client.close()
            except Exception:
                pass

        _http_client = _build_http_client()
        _openai_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=_http_client,
            timeout=OPENAI_TIMEOUT_SEC,
            max_retries=0,
        )
        _cached_client_key = client_key
        logger.info("OpenAI クライアント初期化: base_url=%s", base_url)
        return _openai_client


@retry(
    reraise=True,
    stop=stop_after_attempt(OPENAI_MAX_ATTEMPTS),
    wait=wait_exponential(
        multiplier=2,
        min=OPENAI_RETRY_WAIT_MIN_SEC,
        max=OPENAI_RETRY_WAIT_MAX_SEC,
        exp_base=2,
    ),
    retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
    before_sleep=_before_retry_sleep,
)
def create_chat_completion(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
) -> Any:
    """Chat Completions を呼び出す（APIConnectionError 等は 2s→4s で最大3回再試行）。"""
    client = create_openai_client()
    return client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.6,
    )
