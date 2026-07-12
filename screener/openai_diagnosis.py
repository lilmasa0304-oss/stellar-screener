"""OpenAI API による統合AI診断（テクニカル + ファンダメンタルズ）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from openai import APIError

from screener.fundamentals import format_fundamentals_lines
from screener.openai_client import (
    OPENAI_MAX_ATTEMPTS,
    RETRYABLE_EXCEPTIONS,
    create_chat_completion,
    create_openai_client,
    get_active_openai_base_url,
    get_openai_api_key,
    get_openai_base_url,
    get_openai_fallback_base_url,
    get_openai_model,
    is_openai_configured,
    log_openai_exception,
    probe_openai_connection,
)

logger = logging.getLogger(__name__)

# web_app 等からの既存 import 互換
__all__ = [
    "build_diagnosis_user_message",
    "call_openai_diagnosis",
    "create_openai_client",
    "get_openai_api_key",
    "get_openai_base_url",
    "get_openai_fallback_base_url",
    "get_openai_model",
    "probe_openai_connection",
    "is_openai_configured",
]

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

    try:
        response = create_chat_completion(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
        )
    except RETRYABLE_EXCEPTIONS as exc:
        log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(
            f"OpenAI API 接続エラー（{OPENAI_MAX_ATTEMPTS}回再試行後）: {exc}"
        ) from exc
    except APIError as exc:
        log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(f"OpenAI API エラー: {exc}") from exc
    except Exception as exc:
        log_openai_exception(exc, model=model, stage="chat.completions.create")
        raise RuntimeError(f"OpenAI API 予期しないエラー: {exc}") from exc

    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        raise RuntimeError("OpenAI から空の応答が返されました。")
    return answer
