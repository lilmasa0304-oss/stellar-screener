"""OpenAI API による統合AI診断（テクニカル + ファンダメンタルズ）。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from openai import APIError, OpenAI

from screener.fundamentals import format_fundamentals_lines

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYSTEM_PROMPT = (
    "あなたは日本の株式市場に精通した精鋭の投資アナリストです。"
    "提供されたテクニカルデータ（RSI、株価、移動平均線、シグナルなど）と、"
    "企業の財務・業績データ（ファンダメンタルズ）を網羅的に分析し、"
    "ユーザーに向けて、非常に具体的でプロフェッショナルな『統合AI診断コメント』を出力してください。"
    "テクニカルの売買サインの背景を客観的に解説しつつ、"
    "特に『ファンダメンタルズ分析』の項目では、企業の財務健全性や成長性を鋭く読み解いた"
    "実践的なアドバイスを必ず盛り込んでください。"
    "口調は、信頼感のあるエリートプロトレーダーのビジネス言語で統一すること。"
)


def is_openai_configured() -> bool:
    """OPENAI_API_KEY が有効に設定されているか。"""
    if not OPENAI_API_KEY:
        return False
    placeholders = (
        "your_openai_api_key_here",
        "sk-your-key-here",
        "sk-xxxxxxxx",
    )
    return OPENAI_API_KEY not in placeholders and OPENAI_API_KEY.startswith("sk-")


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
        "テクニカル分析とファンダメンタルズ分析を明確に分け、"
        "最後に総合所見とリスク留意点を簡潔に述べてください。"
    )
    return "\n".join(lines)


def call_openai_diagnosis(user_message: str) -> str:
    """OpenAI Chat Completions API で統合診断テキストを生成する。"""
    if not is_openai_configured():
        raise RuntimeError(
            "OPENAI_API_KEY が未設定です。.env または環境変数に設定してください。"
        )

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.6,
        )
    except APIError as exc:
        logger.error("OpenAI API エラー: %s", exc)
        raise RuntimeError(f"OpenAI API エラー: {exc}") from exc

    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        raise RuntimeError("OpenAI から空の応答が返されました。")
    return answer
