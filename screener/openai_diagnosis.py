"""OpenAI API による統合AI診断（テクニカル + ファンダメンタルズ）。"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any, Dict, List, Optional

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

from screener.fundamentals import format_fundamentals_lines

logger = logging.getLogger(__name__)

_OPENAI_TIMEOUT_SEC = 60.0
_OPENAI_CONNECT_TIMEOUT_SEC = 30.0
_OPENAI_MAX_ATTEMPTS = 3
_OPENAI_RETRY_WAIT_MIN_SEC = 2
_OPENAI_RETRY_WAIT_MAX_SEC = 10
_OPENAI_HOST = "api.openai.com"
_RETRYABLE_EXCEPTIONS = (APIConnectionError, APITimeoutError, RateLimitError)

_ipv4_dns_patch_applied = False
_orig_getaddrinfo = socket.getaddrinfo

_PLACEHOLDER_KEYS = frozenset({
    "your_openai_api_key_here",
    "sk-your-key-here",
    "sk-xxxxxxxx",
})

_dotenv_loaded = False


def _ensure_dotenv() -> None:
    """.env と OS 環境変数を読み込む（Render 等の本番 env は上書きしない）。"""
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(override=False)
        _dotenv_loaded = True


def _normalize_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def get_openai_api_key() -> str:
    """OPENAI_API_KEY を実行時に os.environ から取得する。"""
    _ensure_dotenv()
    return _normalize_secret(os.environ.get("OPENAI_API_KEY"))


def get_openai_model() -> str:
    """OPENAI_MODEL を実行時に os.environ から取得する。"""
    _ensure_dotenv()
    model = _normalize_secret(os.environ.get("OPENAI_MODEL")) or "gpt-4o"
    return model


def is_openai_configured() -> bool:
    """OPENAI_API_KEY が有効に設定されているか。"""
    key = get_openai_api_key()
    if not key:
        return False
    if key.lower() in _PLACEHOLDER_KEYS:
        return False
    if key.startswith("sk-"):
        return len(key) > 20
    return len(key) >= 20


def _ipv4_only_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
):
    """DNS 解決を IPv4 (AF_INET) のみに制限する（Render 等の IPv6 不通対策）。"""
    sock_type = type or socket.SOCK_STREAM
    return _orig_getaddrinfo(host, port, socket.AF_INET, sock_type, proto, flags)


def _ensure_ipv4_dns_resolution() -> None:
    """socket.getaddrinfo を一度だけ IPv4 専用に差し替える。"""
    global _ipv4_dns_patch_applied
    if _ipv4_dns_patch_applied:
        return
    socket.getaddrinfo = _ipv4_only_getaddrinfo
    _ipv4_dns_patch_applied = True
    logger.info("OpenAI 通信: socket.getaddrinfo を IPv4 (AF_INET) のみに制限しました")


def _log_openai_network_diagnostics() -> None:
    """接続前に DNS が IPv4 で解決できるかログに残す。"""
    try:
        addresses = _orig_getaddrinfo(
            _OPENAI_HOST,
            443,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        ipv4_list = sorted({item[4][0] for item in addresses})
        logger.info("OpenAI DNS probe (IPv4): %s -> %s", _OPENAI_HOST, ipv4_list)
    except OSError as exc:
        logger.error(
            "OpenAI DNS probe failed (IPv4): host=%s errno=%s message=%s",
            _OPENAI_HOST,
            getattr(exc, "errno", None),
            exc,
        )


def _exception_cause_chain(exc: BaseException) -> List[str]:
    """例外チェーンを文字列リストで返す（根本原因の特定用）。"""
    chain: List[str] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return chain


def _create_ipv4_http_client() -> httpx.Client:
    """IPv4 を強制する httpx クライアント（Render 無料枠の IPv6 遮断対策）。"""
    _ensure_ipv4_dns_resolution()
    _log_openai_network_diagnostics()
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=0)
    timeout = httpx.Timeout(
        _OPENAI_TIMEOUT_SEC,
        connect=_OPENAI_CONNECT_TIMEOUT_SEC,
    )
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        trust_env=False,
        http2=False,
    )


def create_openai_client() -> OpenAI:
    """OpenAI クライアントを生成する（IPv4 強制・タイムアウト付き）。"""
    api_key = get_openai_api_key()
    if not is_openai_configured():
        raise RuntimeError(
            "OPENAI_API_KEY が未設定です。.env または環境変数に設定してください。"
        )
    http_client = _create_ipv4_http_client()
    return OpenAI(
        api_key=api_key,
        http_client=http_client,
        timeout=_OPENAI_TIMEOUT_SEC,
        max_retries=0,
    )


def _log_openai_exception(exc: BaseException, *, model: str, stage: str) -> None:
    """OpenAI 関連エラーの詳細をログに記録する。"""
    details = {
        "stage": stage,
        "model": model,
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


@retry(
    reraise=True,
    stop=stop_after_attempt(_OPENAI_MAX_ATTEMPTS),
    wait=wait_exponential(
        multiplier=2,
        min=_OPENAI_RETRY_WAIT_MIN_SEC,
        max=_OPENAI_RETRY_WAIT_MAX_SEC,
        exp_base=2,
    ),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _create_chat_completion(client: OpenAI, *, model: str, user_message: str):
    """Chat Completions を呼び出す（接続失敗時は 2s→4s の指数バックオフで最大3回再試行）。"""
    return client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.6,
    )


SYSTEM_PROMPT = (
    "あなたは日本の株式市場に精通した精鋭の投資アナリストです。"
    "提供されたテクニカルデータ（RSI、株価、移動平均線、シグナルなど）と、"
    "企業の財務・業績データ（ファンダメンタルズ）を網羅的に分析し、"
    "ユーザーに向けて、非常に具体的でプロフェッショナルな『統合AI診断コメント』を出力してください。"
    "テクニカルの売買サインの背景を客観的に解説しつつ、"
    "特に『ファンダメンタルズ分析』の項目では、企業の財務健全性や成長性を鋭く読み解いた"
    "実践的なアドバイスを必ず盛り込んでください。"
    "口調は、信頼感のあるエリートプロトレーダーのビジネス言語で統一すること。"
    ""
    "【必須出力】テクニカル分析とファンダメンタルズ分析の結論の直後、"
    "各銘柄について『利確・損切り目安』セクションを必ず設け、"
    "データ内の current_price（現在株価）を基準に以下3パターンを逆算し、"
    "具体的な円建て価格を必ず併記すること（端数は1円単位で四捨五入）。"
    "① 短期・値幅取り型：利確目安 +5%（現在株価×1.05円）、損切り目安 -3%（現在株価×0.97円）"
    "② 中期・トレンド追随型：利確目安 +10%（現在株価×1.10円）、損切り目安 -5%（現在株価×0.95円）"
    "③ 長期・資産成長型：利確目安 +30%（現在株価×1.30円）、"
    "損切り目安は数値ではなく『前提の崩壊（業績悪化・成長ストーリー喪失・財務悪化など）』と明記すること。"
    "各パターンでは「利確目安 ○○円（+○%）」「損切り目安 ○○円（-○%）」の形式で示すこと。"
    "③の損切りのみ数値を出さず、定性条件を簡潔に補足すること。"
)


def _serialize_fundamentals(fundamentals: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not fundamentals or not fundamentals.get("available"):
        return {"available": False}
    assessment = fundamentals.get("assessment") or {}
    return {
        "available": True,
        "sector": fundamentals.get("sector"),
        "industry": fundamentals.get("industry"),
        "trailing_pe": fundamentals.get("trailing_pe"),
        "pbr": fundamentals.get("pbr"),
        "roe": fundamentals.get("roe"),
        "profit_margin": fundamentals.get("profit_margin"),
        "revenue_growth": fundamentals.get("revenue_growth"),
        "earnings_growth": fundamentals.get("earnings_growth"),
        "dividend_yield": fundamentals.get("dividend_yield"),
        "market_cap": fundamentals.get("market_cap"),
        "debt_to_equity": fundamentals.get("debt_to_equity"),
        "current_ratio": fundamentals.get("current_ratio"),
        "ev_ebitda": fundamentals.get("ev_ebitda"),
        "grade": assessment.get("grade"),
        "score": assessment.get("score"),
        "summary": assessment.get("summary"),
        "points": assessment.get("points") or [],
    }


def _build_stock_context(screen_data: Dict[str, Any]) -> Dict[str, Any]:
    preset = screen_data.get("preset_matched", "none")
    preset_labels = {
        "oshieme": "押し目シグナル",
        "junbari": "順張りブレイク",
        "none": "該当なし",
    }
    return {
        "code": screen_data.get("code") or screen_data.get("ticker"),
        "name": screen_data.get("name"),
        "mode": screen_data.get("mode", "堅実"),
        "technical": {
            "current_price": screen_data.get("current_price"),
            "rsi": screen_data.get("rsi"),
            "ma25": screen_data.get("ma25"),
            "ma25_uptrend": screen_data.get("ma25_uptrend"),
            "ma25_deviation_pct": screen_data.get(
                "ma25_deviation_pct", screen_data.get("ma25_divergence_pct"),
            ),
            "volume_ratio": screen_data.get("volume_ratio"),
            "buy_signal": screen_data.get("buy_signal"),
            "signal_type": preset_labels.get(preset, preset),
            "trend_status": screen_data.get("trend_status"),
            "technical_comment": screen_data.get("reason"),
            "preset_evaluations": screen_data.get("preset_evaluations"),
        },
        "fundamentals": _serialize_fundamentals(screen_data.get("fundamentals")),
    }


def build_diagnosis_user_message(
    screen_data_list: List[Dict[str, Any]],
    user_query: str,
) -> str:
    """OpenAI へ渡すユーザーメッセージ（実データを構造化して含める）。"""
    contexts = [_build_stock_context(item) for item in screen_data_list]
    lines = [
        f"ユーザー入力: {user_query}",
        "",
        "以下は Yahoo Finance から取得した最新の診断データです。",
        "このデータのみを根拠に、統合AI診断コメントを作成してください。",
        "",
        json.dumps({"stocks": contexts}, ensure_ascii=False, indent=2, default=str),
        "",
    ]

    for item in screen_data_list:
        fundamentals = item.get("fundamentals")
        if fundamentals and fundamentals.get("available"):
            lines.extend(format_fundamentals_lines(fundamentals))

    lines.append("")
    lines.append(
        "出力形式: 見出しを用いた読みやすい日本語。"
        "テクニカル分析とファンダメンタルズ分析を明確に分けた後、"
        "【利確・損切り目安】として以下3パターンを現在株価から逆算した円建て価格付きで必ず出力すること。"
        "①短期・値幅取り型（利確+5%/損切-3%）"
        "②中期・トレンド追随型（利確+10%/損切-5%）"
        "③長期・資産成長型（利確+30%/損切は前提の崩壊）"
        "最後に総合所見とリスク留意点を簡潔に述べてください。"
    )
    return "\n".join(lines)


def call_openai_diagnosis(user_message: str) -> str:
    """OpenAI Chat Completions API で統合診断テキストを生成する。"""
    model = get_openai_model()
    client = create_openai_client()

    try:
        response = _create_chat_completion(client, model=model, user_message=user_message)
    except _RETRYABLE_EXCEPTIONS as exc:
        _log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(
            f"OpenAI API 接続エラー（{_OPENAI_MAX_ATTEMPTS}回再試行後）: {exc}"
        ) from exc
    except APIError as exc:
        _log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(f"OpenAI API エラー: {exc}") from exc
    except Exception as exc:
        _log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(f"OpenAI API 予期しないエラー: {exc}") from exc

    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        raise RuntimeError("OpenAI から空の応答が返されました。")
    return answer
