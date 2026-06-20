import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from screener.indicators import (
    calculate_bollinger_bands,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
    detect_macd_crossover,
)

logger = logging.getLogger(__name__)

NO_BUY_SIGNAL_REASON = "現在は買い条件を満たしていません。次のサインを待ちましょう。"


class StrategyEvaluator:
    """
    日本株スイングトレード用テクニカル分析エンジン。

    - 全指標（RSI / MA / BB / MACD / ma25_uptrend 等）を常に算出し DB・UI・main.py へ返す
    - BUY SIGNAL は「押し目買い / 順張り初動」プリセット判定（RISK_MODES 対応）
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.strategies = self.config.get("strategies", {})
        self.swing_cfg = self.config.get("swing_signal", {})
        self.settings = {
            "rsi_oshieme_max": 23,
            "rsi_junbari_max": 60,
            "rsi_junbari_min": 52,
            "max_ma25_divergence": 4.0,
            "volume_growth_ratio": 1.7,
        }

    def evaluate(self, ticker: str, name: str, df: pd.DataFrame) -> Dict[str, Any]:
        if df is None or len(df) < 2:
            return self._empty_result(ticker, name, "データ数が足りません")

        close = df["Close"]
        current_price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2])
        change_pct = ((current_price - prev_price) / prev_price) * 100.0

        result = self._compute_indicators(ticker, name, df, current_price, change_pct)
        result.update(self._evaluate_preset_buy(df, current_price, result))
        return result

    def _compute_indicators(
        self,
        ticker: str,
        name: str,
        df: pd.DataFrame,
        current_price: float,
        change_pct: float,
    ) -> Dict[str, Any]:
        rsi_cfg = self.strategies.get("rsi", {})
        rsi_period = int(rsi_cfg.get("period", 14))
        oversold = float(rsi_cfg.get("oversold_threshold", 30.0))
        overbought = float(rsi_cfg.get("overbought_threshold", 70.0))

        rsi_series = (
            calculate_rsi(df, rsi_period)
            if rsi_cfg.get("enabled", True)
            else pd.Series(dtype=float)
        )
        last_rsi = (
            float(rsi_series.iloc[-1])
            if (not rsi_series.empty and pd.notna(rsi_series.iloc[-1]))
            else None
        )

        ma_cfg = self.strategies.get("ma_trend", {})
        ma5_p = int(ma_cfg.get("ma5_period", 5))
        ma25_p = int(ma_cfg.get("ma25_period", 25))
        ma75_p = int(ma_cfg.get("ma75_period", 75))

        sma5 = calculate_sma(df, ma5_p)
        sma25 = calculate_sma(df, ma25_p)
        sma75 = calculate_sma(df, ma75_p)

        last_ma5 = float(sma5.iloc[-1]) if pd.notna(sma5.iloc[-1]) else None
        last_ma25 = float(sma25.iloc[-1]) if pd.notna(sma25.iloc[-1]) else None
        last_ma75 = float(sma75.iloc[-1]) if pd.notna(sma75.iloc[-1]) else None

        ma25_uptrend = False
        price_above_ma25 = False
        ma25_deviation = None
        if last_ma25 is not None and len(sma25) >= 2 and pd.notna(sma25.iloc[-2]):
            prev_ma25 = float(sma25.iloc[-2])
            ma25_uptrend = last_ma25 > prev_ma25
            price_above_ma25 = current_price > last_ma25
            ma25_deviation = ((current_price - last_ma25) / last_ma25) * 100.0

        ma5_rebound = False
        if last_ma5 is not None and len(sma5) >= 2 and pd.notna(sma5.iloc[-2]):
            prev_ma5 = float(sma5.iloc[-2])
            ma5_rebound = last_ma5 > prev_ma5

        bb_cfg = self.strategies.get("bollinger", {})
        bb_p = int(bb_cfg.get("period", 20))
        bb_std = float(bb_cfg.get("std_dev", 2.0))

        bb_upper_s, bb_lower_s = calculate_bollinger_bands(df, bb_p, bb_std)
        last_bb_upper = float(bb_upper_s.iloc[-1]) if pd.notna(bb_upper_s.iloc[-1]) else None
        last_bb_lower = float(bb_lower_s.iloc[-1]) if pd.notna(bb_lower_s.iloc[-1]) else None
        bb_lower_touch = (last_bb_lower is not None) and (current_price <= last_bb_lower)

        macd_line, macd_signal_line, macd_hist = calculate_macd(df)
        last_macd = float(macd_line.iloc[-1]) if pd.notna(macd_line.iloc[-1]) else None
        last_macd_signal = (
            float(macd_signal_line.iloc[-1]) if pd.notna(macd_signal_line.iloc[-1]) else None
        )
        last_macd_hist = float(macd_hist.iloc[-1]) if pd.notna(macd_hist.iloc[-1]) else None
        macd_crossover, macd_pre_crossover = detect_macd_crossover(macd_hist)

        signals: List[str] = []

        if rsi_cfg.get("enabled", True) and last_rsi is not None:
            if last_rsi <= oversold:
                signals.append(f"RSI売られすぎ: {last_rsi:.1f}（基準≤{oversold}）")
            elif last_rsi >= overbought:
                signals.append(f"RSI買われすぎ: {last_rsi:.1f}（基準≥{overbought}）")

        cross_cfg = self.strategies.get("crossover", {})
        if cross_cfg.get("enabled", True) and len(df) >= ma25_p + 1:
            s5_y = float(sma5.iloc[-2]) if pd.notna(sma5.iloc[-2]) else None
            s25_y = float(sma25.iloc[-2]) if pd.notna(sma25.iloc[-2]) else None
            if None not in (last_ma5, last_ma25, s5_y, s25_y):
                if s5_y <= s25_y and last_ma5 > last_ma25:
                    signals.append("ゴールデンクロス（MA5がMA25を上抜け）")
                elif s5_y >= s25_y and last_ma5 < last_ma25:
                    signals.append("デッドクロス（MA5がMA25を下抜け）")

        if ma_cfg.get("enabled", True):
            if ma25_uptrend and price_above_ma25:
                signals.append("25日線上昇中（株価が25日線の上・上昇目線）")
            elif not ma25_uptrend and not price_above_ma25:
                signals.append("25日線下降中（株価が25日線の下・下降トレンド）")

        if macd_crossover:
            signals.append(
                f"MACDゴールデンクロス（MACD:{last_macd:.2f} / Signal:{last_macd_signal:.2f}）"
            )
        elif macd_pre_crossover:
            signals.append(f"MACD GC直前の収束中（ヒスト: {last_macd_hist:.2f}）")

        if bb_cfg.get("enabled", True):
            if bb_lower_touch:
                signals.append(
                    f"ボリンジャーバンド-2σタッチ（{current_price:,.1f}円 ≤ {last_bb_lower:,.1f}円）"
                )
            elif last_bb_upper is not None and current_price >= last_bb_upper:
                signals.append(
                    f"ボリンジャーバンド+2σタッチ（{current_price:,.1f}円 ≥ {last_bb_upper:,.1f}円）"
                )

        dc_cfg = self.strategies.get("daily_change", {})
        if dc_cfg.get("enabled", True):
            threshold = float(dc_cfg.get("threshold_percent", 3.0))
            if abs(change_pct) >= threshold:
                direction = "急騰" if change_pct > 0 else "急落"
                signals.append(f"前日比{direction}: {change_pct:+.1f}%（基準±{threshold}%）")

        is_prime_entry = False
        comp_cfg = self.strategies.get("composite_entry", {})
        rsi_oversold_hit = (last_rsi is not None) and (last_rsi <= oversold)
        require_ma25_up = ma_cfg.get("require_ma25_uptrend", True)
        require_bb_touch = bb_cfg.get("require_lower_band_touch", False)

        if comp_cfg.get("enabled", True) and rsi_oversold_hit:
            conditions_met = [True]
            if require_ma25_up:
                conditions_met.append(ma25_uptrend)
            if require_bb_touch:
                conditions_met.append(bb_lower_touch)
            if all(conditions_met):
                is_prime_entry = True
                signals.append("★絶好のエントリータイミング（複合条件クリア）")

        swing_rsi_min = float(self.swing_cfg.get("rsi_min", 35.0))
        swing_rsi_max = float(self.swing_cfg.get("rsi_max", 45.0))
        req_ma25 = self.swing_cfg.get("require_ma25_uptrend", True)
        req_macd = self.swing_cfg.get("require_macd_cross", True)

        cond_rsi = (last_rsi is not None) and (swing_rsi_min <= last_rsi <= swing_rsi_max)
        cond_ma25 = ma25_uptrend if req_ma25 else True
        cond_macd = (macd_crossover or macd_pre_crossover) if req_macd else True
        swing_buy = cond_rsi and cond_ma25 and cond_macd

        if swing_buy:
            signals.append(
                f"🚀 [BUY SIGNAL] スイング買い条件クリア"
                f"（RSI:{last_rsi:.1f} / MA25↑ / MACD{'GC' if macd_crossover else 'GC接近'}）"
            )

        return {
            "ticker": ticker,
            "name": name,
            "sector": "要確認",
            "current_price": current_price,
            "close_price": current_price,
            "change_percent": change_pct,
            "triggered": len(signals) > 0,
            "signals": signals,
            "is_prime_entry": is_prime_entry,
            "buy_signal": False,
            "reason": NO_BUY_SIGNAL_REASON,
            "preset_matched": "none",
            "rsi": round(last_rsi, 1) if last_rsi is not None else None,
            "ma5": round(last_ma5, 1) if last_ma5 is not None else None,
            "ma25": round(last_ma25, 1) if last_ma25 is not None else None,
            "ma75": round(last_ma75, 1) if last_ma75 is not None else None,
            "bb_upper": round(last_bb_upper, 1) if last_bb_upper is not None else None,
            "bb_lower": round(last_bb_lower, 1) if last_bb_lower is not None else None,
            "ma25_deviation_pct": round(ma25_deviation, 2) if ma25_deviation is not None else None,
            "ma25_uptrend": ma25_uptrend,
            "price_above_ma25": price_above_ma25,
            "bb_lower_touch": bb_lower_touch,
            "ma5_rebound": ma5_rebound,
            "macd": round(last_macd, 2) if last_macd is not None else None,
            "macd_signal": round(last_macd_signal, 2) if last_macd_signal is not None else None,
            "macd_hist": round(last_macd_hist, 3) if last_macd_hist is not None else None,
            "macd_crossover": macd_crossover,
            "macd_pre_crossover": macd_pre_crossover,
            "swing_cond_rsi": cond_rsi,
            "swing_cond_ma25": cond_ma25,
            "swing_cond_macd": cond_macd,
        }

    def _evaluate_preset_buy(
        self,
        df: pd.DataFrame,
        close_p: float,
        base: Dict[str, Any],
    ) -> Dict[str, Any]:
        """押し目買い / 順張り初動プリセットで buy_signal を判定する。"""
        updates: Dict[str, Any] = {}

        if len(df) < 75:
            return updates

        try:
            latest = df.iloc[-1]
            high_p = float(latest["High"])
            low_p = float(latest["Low"])
            open_p = float(latest["Open"])

            close_series = df["Close"]
            ma5 = close_series.rolling(window=5).mean().iloc[-1]
            ma25_series = close_series.rolling(window=25).mean()
            ma25 = ma25_series.iloc[-1]
            ma75 = close_series.rolling(window=75).mean()

            current_rsi = base.get("rsi")
            if current_rsi is None or pd.isna(ma25) or ma25 == 0:
                return updates

            bb_upper, bb_lower = calculate_bollinger_bands(df, period=20, std_dev=2.0)

            avg_volume_5d = df["Volume"].iloc[-6:-1].mean()
            current_volume = df["Volume"].iloc[-1]
            volume_ratio = current_volume / avg_volume_5d if avg_volume_5d > 0 else 1.0
            ma25_divergence = ((close_p - ma25) / ma25) * 100

            if current_volume < 100000:
                return updates

            body_size = abs(close_p - open_p)
            upper_shadow = high_p - max(open_p, close_p)
            if upper_shadow > (body_size * 2) and body_size > 0:
                return updates

            # ① 押し目買い型
            ma75_now = ma75.iloc[-1]
            ma75_prev = ma75.iloc[-5]
            is_long_term_up = (
                not pd.isna(ma75_now) and not pd.isna(ma75_prev) and ma75_now > ma75_prev
            )
            if is_long_term_up and current_rsi <= self.settings["rsi_oshieme_max"]:
                bb_low = bb_lower.iloc[-1]
                if not pd.isna(bb_low) and close_p <= bb_low:
                    updates["buy_signal"] = True
                    updates["preset_matched"] = "oshieme"
                    updates["reason"] = (
                        f"【神の押し目】長期上昇中の奇跡的な急落。"
                        f"RSIは{current_rsi:.1f}%と完全に底値圏。高値掴みリスクは極めて低いです。"
                    )
                    return updates

            # ② 順張り初動型
            ma5_prev = close_series.rolling(window=5).mean().iloc[-2]
            ma25_prev = ma25_series.iloc[-2]
            is_gold_cross = (
                not pd.isna(ma5)
                and not pd.isna(ma25)
                and not pd.isna(ma5_prev)
                and not pd.isna(ma25_prev)
                and ma5_prev <= ma25_prev
                and ma5 > ma25
            )
            rsi_in_safe_zone = (
                self.settings["rsi_junbari_min"] <= current_rsi <= self.settings["rsi_junbari_max"]
            )
            volume_ok = volume_ratio >= self.settings["volume_growth_ratio"]

            if is_gold_cross and rsi_in_safe_zone and volume_ok:
                if ma25_divergence > self.settings["max_ma25_divergence"]:
                    return updates

                updates["buy_signal"] = True
                updates["preset_matched"] = "junbari"
                updates["reason"] = (
                    f"【上昇初動】25日線からの乖離率も{ma25_divergence:.1f}%と低く、"
                    f"出来高急増（{volume_ratio:.1f}倍）を伴う本物のクロス。"
                    f"ここからのエントリーなら安全圏です。"
                )

        except Exception as e:
            logger.error(f"Preset buy evaluation error for {base.get('ticker')}: {e}")
            updates["reason"] = f"エラー: {str(e)}"

        return updates

    def _empty_result(self, ticker: str, name: str, reason: str) -> Dict[str, Any]:
        return {
            "ticker": ticker,
            "name": name,
            "sector": "要確認",
            "current_price": None,
            "close_price": None,
            "change_percent": 0.0,
            "triggered": False,
            "signals": [reason],
            "is_prime_entry": False,
            "buy_signal": False,
            "reason": reason,
            "preset_matched": "none",
            "rsi": None,
            "ma5": None,
            "ma25": None,
            "ma75": None,
            "bb_upper": None,
            "bb_lower": None,
            "ma25_deviation_pct": None,
            "ma25_uptrend": False,
            "price_above_ma25": False,
            "bb_lower_touch": False,
            "ma5_rebound": False,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "macd_crossover": False,
            "macd_pre_crossover": False,
            "swing_cond_rsi": False,
            "swing_cond_ma25": False,
            "swing_cond_macd": False,
        }
