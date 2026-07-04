"""Yahoo Finance からファンダメンタルズ指標を取得し、診断用に整形する。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
        if num != num:  # NaN
            return None
        return num
    except (TypeError, ValueError):
        return None


def _format_yen_amount(value: Optional[float]) -> str:
    if value is None:
        return "—"
    abs_val = abs(value)
    if abs_val >= 1_0000_0000_0000:  # 兆
        return f"約{value / 1_0000_0000_0000:.2f}兆円"
    if abs_val >= 1_0000_0000:  # 億
        return f"約{value / 1_0000_0000:.0f}億円"
    if abs_val >= 1_0000:  # 万
        return f"約{value / 1_0000:.0f}万円"
    return f"{value:,.0f}円"


def _format_pct(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return "—"
    # yfinance は 0.1023 = 10.23% 形式
    if abs(value) <= 1.5:
        return f"{value * 100:.{decimals}f}%"
    return f"{value:.{decimals}f}%"


def _format_ratio(value: Optional[float], suffix: str = "倍", decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}{suffix}"


def fetch_fundamentals(yahoo_ticker: str) -> Dict[str, Any]:
    """銘柄のファンダメンタルズ指標を取得する（診断専用・単銘柄向け）。"""
    result: Dict[str, Any] = {
        "available": False,
        "ticker": yahoo_ticker,
    }
    try:
        info = yf.Ticker(yahoo_ticker).info or {}
    except Exception as exc:
        logger.warning("ファンダメンタルズ取得失敗 (%s): %s", yahoo_ticker, exc)
        result["error"] = str(exc)
        return result

    if not info:
        result["error"] = "データなし"
        return result

    trailing_pe = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
    pbr = _safe_float(info.get("priceToBook"))
    roe = _safe_float(info.get("returnOnEquity"))
    profit_margin = _safe_float(info.get("profitMargins"))
    revenue_growth = _safe_float(info.get("revenueGrowth"))
    earnings_growth = _safe_float(info.get("earningsGrowth"))
    dividend_yield = _safe_float(info.get("dividendYield"))
    market_cap = _safe_float(info.get("marketCap"))
    debt_to_equity = _safe_float(info.get("debtToEquity"))
    current_ratio = _safe_float(info.get("currentRatio"))
    ev_ebitda = _safe_float(info.get("enterpriseToEbitda"))

    result.update({
        "available": True,
        "sector": info.get("sector") or info.get("sectorDisp"),
        "industry": info.get("industry") or info.get("industryDisp"),
        "trailing_pe": trailing_pe,
        "pbr": pbr,
        "roe": roe,
        "profit_margin": profit_margin,
        "revenue_growth": revenue_growth,
        "earnings_growth": earnings_growth,
        "dividend_yield": dividend_yield,
        "market_cap": market_cap,
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio,
        "ev_ebitda": ev_ebitda,
        "book_value": _safe_float(info.get("bookValue")),
    })
    result["assessment"] = assess_fundamentals(result)
    return result


def assess_fundamentals(data: Dict[str, Any]) -> Dict[str, Any]:
    """取得した指標から簡易コメントとスコアを生成する。"""
    if not data.get("available"):
        return {
            "score": None,
            "grade": "データ不足",
            "summary": "ファンダメンタルズデータを取得できませんでした。",
            "points": [],
        }

    points: List[str] = []
    score = 50  # 中立起点

    pe = data.get("trailing_pe")
    if pe is not None:
        if pe < 0:
            points.append("PER: 赤字（マイナス益）で割高感あり")
            score -= 15
        elif pe <= 12:
            points.append("PER: 割安圏（12倍以下）")
            score += 12
        elif pe <= 20:
            points.append("PER: 適正〜やや割安（12〜20倍）")
            score += 5
        elif pe <= 30:
            points.append("PER: やや割高（20〜30倍）")
            score -= 5
        else:
            points.append("PER: 割高（30倍超）")
            score -= 12

    pbr = data.get("pbr")
    if pbr is not None:
        if pbr < 1.0:
            points.append("PBR: 1倍割れ（資産面では割安）")
            score += 10
        elif pbr <= 2.0:
            points.append("PBR: 適正圏（1〜2倍）")
            score += 4
        elif pbr <= 4.0:
            points.append("PBR: やや高め（2〜4倍）")
            score -= 3
        else:
            points.append("PBR: 高PBR（4倍超）")
            score -= 8

    roe = data.get("roe")
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) <= 1.5 else roe
        if roe_pct >= 12:
            points.append(f"ROE: 高収益（{roe_pct:.1f}%）")
            score += 10
        elif roe_pct >= 8:
            points.append(f"ROE: 健全（{roe_pct:.1f}%）")
            score += 5
        elif roe_pct >= 3:
            points.append(f"ROE: 低め（{roe_pct:.1f}%）")
            score -= 3
        else:
            points.append(f"ROE: 弱い（{roe_pct:.1f}%）")
            score -= 8

    rev_g = data.get("revenue_growth")
    if rev_g is not None:
        rev_pct = rev_g * 100 if abs(rev_g) <= 1.5 else rev_g
        if rev_pct >= 10:
            points.append(f"売上成長: 高成長（+{rev_pct:.1f}%）")
            score += 8
        elif rev_pct >= 0:
            points.append(f"売上成長: プラス（+{rev_pct:.1f}%）")
            score += 3
        else:
            points.append(f"売上成長: マイナス（{rev_pct:.1f}%）")
            score -= 8

    earn_g = data.get("earnings_growth")
    if earn_g is not None:
        earn_pct = earn_g * 100 if abs(earn_g) <= 1.5 else earn_g
        if earn_pct >= 15:
            points.append(f"利益成長: 高い（+{earn_pct:.1f}%）")
            score += 6
        elif earn_pct >= 0:
            points.append(f"利益成長: プラス（+{earn_pct:.1f}%）")
            score += 2
        else:
            points.append(f"利益成長: 減益（{earn_pct:.1f}%）")
            score -= 6

    div_y = data.get("dividend_yield")
    if div_y is not None:
        div_pct = div_y * 100 if div_y <= 0.2 else div_y
        if div_pct >= 3:
            points.append(f"配当利回り: 高め（{div_pct:.2f}%）")
            score += 4
        elif div_pct >= 1:
            points.append(f"配当利回り: あり（{div_pct:.2f}%）")
            score += 2

    dte = data.get("debt_to_equity")
    if dte is not None:
        if dte >= 200:
            points.append("財務: D/E 比率が高め（要注意）")
            score -= 8
        elif dte >= 100:
            points.append("財務: D/E 比率はやや高め")
            score -= 3
        else:
            points.append("財務: D/E 比率は比較的安定")
            score += 3

    score = max(0, min(100, score))
    if score >= 70:
        grade = "良好"
        summary = "ファンダメンタルズは総じて良好。中長期の下支え材料あり。"
    elif score >= 55:
        grade = "中立"
        summary = "ファンダメンタルズは中立。テクニカルとセットで判断を。"
    elif score >= 40:
        grade = "注意"
        summary = "ファンダメンタルズに弱い材料あり。エントリーは慎重に。"
    else:
        grade = "弱い"
        summary = "ファンダメンタルズ面のリスクが目立つ。短期テクニカルのみに依存しないこと。"

    return {
        "score": score,
        "grade": grade,
        "summary": summary,
        "points": points,
    }


def format_fundamentals_lines(data: Dict[str, Any]) -> List[str]:
    """診断レポート用のファンダメンタルズ行リストを返す。"""
    if not data.get("available"):
        return ["ファンダメンタルズ: データ取得不可"]

    assessment = data.get("assessment") or {}
    lines = [
        "",
        "【ファンダメンタルズ分析】",
        f"セクター: {data.get('sector') or '—'} / {data.get('industry') or '—'}",
        f"時価総額: {_format_yen_amount(data.get('market_cap'))}",
        f"PER: {_format_ratio(data.get('trailing_pe'))}",
        f"PBR: {_format_ratio(data.get('pbr'))}",
        f"ROE: {_format_pct(data.get('roe'))}",
        f"営業利益率: {_format_pct(data.get('profit_margin'))}",
        f"売上成長率: {_format_pct(data.get('revenue_growth'))}",
        f"利益成長率: {_format_pct(data.get('earnings_growth'))}",
        f"配当利回り: {_format_pct(data.get('dividend_yield'), decimals=2)}",
        f"D/Eレシオ: {_format_ratio(data.get('debt_to_equity'), suffix='', decimals=1) if data.get('debt_to_equity') is not None else '—'}",
        f"EV/EBITDA: {_format_ratio(data.get('ev_ebitda'))}",
        f"総合評価: {assessment.get('grade', '—')}（スコア {assessment.get('score', '—')}/100）",
        f"コメント: {assessment.get('summary', '—')}",
    ]
    for point in assessment.get("points") or []:
        lines.append(f"  - {point}")
    return lines
