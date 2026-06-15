import pandas as pd
import numpy as np
import logging
from typing import Dict, Any
from screener.indicators import calculate_rsi, calculate_bollinger_bands

logger = logging.getLogger(__name__)

NO_BUY_SIGNAL_REASON = "現在は買い条件を満たしていません。次のサインを待ちましょう。"

class StrategyEvaluator:
    """【超・高値掴み防止モード】初心者のポジポジ病を物理的に止めるスクリーナーロジック"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        # 妥協を一切許さないゴリゴリの厳格パラメータ
        self.settings = {
            "rsi_oshieme_max": 23,       # 押し目買い：本当の底ダマしか見ない
            "rsi_junbari_max": 60,       # 順張り：高値掴み防止のため、過熱感を低めに制限
            "rsi_junbari_min": 52,       # 順張り：中途半端なヨコヨコを弾く
            "max_ma25_divergence": 4.0,  # 25日線から上に4%以上離れたら「高値掴み」として強制除外
            "volume_growth_ratio": 1.7   # 出来高は過去平均の1.7倍以上（大口の参入が確実なものだけ）
        }

    def evaluate(self, ticker: str, name: str, df: pd.DataFrame) -> Dict[str, Any]:
        result = {
            "ticker": ticker, "name": name, "sector": "要確認", "close_price": 0.0,
            "rsi": 0.0, "buy_signal": False, "reason": NO_BUY_SIGNAL_REASON, "preset_matched": "none"
        }

        if df is None or len(df) < 75:
            return result

        try:
            latest = df.iloc[-1]
            close_p = float(latest["Close"])
            high_p = float(latest["High"])
            low_p = float(latest["Low"])
            open_p = float(latest["Open"])
            
            result["close_price"] = close_p

            # 指標計算
            close_series = df["Close"]
            ma5 = close_series.rolling(window=5).mean().iloc[-1]
            ma25_series = close_series.rolling(window=25).mean()
            ma25 = ma25_series.iloc[-1]
            ma75 = close_series.rolling(window=75).mean()
            
            rsi_series = calculate_rsi(df, period=14)
            current_rsi_val = rsi_series.iloc[-1]
            if pd.isna(current_rsi_val) or pd.isna(ma25) or ma25 == 0:
                return result
            current_rsi = float(current_rsi_val)
            result["rsi"] = round(current_rsi, 2)

            bb_upper, bb_lower = calculate_bollinger_bands(df, period=20, std_dev=2.0)

            # 出来高の計算
            avg_volume_5d = df["Volume"].iloc[-6:-1].mean()
            current_volume = df["Volume"].iloc[-1]
            volume_ratio = current_volume / avg_volume_5d if avg_volume_5d > 0 else 1.0

            # 25日線乖離率の計算
            ma25_divergence = ((close_p - ma25) / ma25) * 100

            # ----------------------------------------------------
            # 【鉄壁の共通フィルター（ここで罠を弾く）】
            # ----------------------------------------------------
            # 1. 流動性の低い銘柄は除外
            if current_volume < 100000:
                return result
            
            # 2. 長い「上ヒゲ」は高値掴みの典型（罠と判定して即除外）
            body_size = abs(close_p - open_p)
            upper_shadow = high_p - max(open_p, close_p)
            if upper_shadow > (body_size * 2) and body_size > 0:
                return result

            # ----------------------------------------------------
            # ① 【極上・押し目買い型】
            # ----------------------------------------------------
            ma75_now  = ma75.iloc[-1]
            ma75_prev = ma75.iloc[-5]
            is_long_term_up = (
                not pd.isna(ma75_now) and not pd.isna(ma75_prev) and ma75_now > ma75_prev
            )
            if is_long_term_up and current_rsi <= self.settings["rsi_oshieme_max"]:
                bb_low = bb_lower.iloc[-1]
                if not pd.isna(bb_low) and close_p <= bb_low:
                    result["buy_signal"] = True
                    result["preset_matched"] = "oshieme"
                    result["reason"] = f"【神の押し目】長期上昇中の奇跡的な急落。RSIは{current_rsi:.1f}%と完全に底値圏。高値掴みリスクは極めて低いです。"
                    return result

            # ----------------------------------------------------
            # ② 【初動・順張りトレンド追随型】（高値掴みを絶対させない）
            # ----------------------------------------------------
            ma5_prev = df["Close"].rolling(window=5).mean().iloc[-2]
            ma25_prev = ma25_series.iloc[-2]
            is_gold_cross = (
                not pd.isna(ma5) and not pd.isna(ma25)
                and not pd.isna(ma5_prev) and not pd.isna(ma25_prev)
                and ma5_prev <= ma25_prev and ma5 > ma25
            )
            rsi_in_safe_zone = self.settings["rsi_junbari_min"] <= current_rsi <= self.settings["rsi_junbari_max"]
            volume_ok = volume_ratio >= self.settings["volume_growth_ratio"]

            if is_gold_cross and rsi_in_safe_zone and volume_ok:
                # 【最重要】すでに25日線から上に離れすぎている場合は、イナゴの天井掴みなので弾く！
                if ma25_divergence > self.settings["max_ma25_divergence"]:
                    return result

                result["buy_signal"] = True
                result["preset_matched"] = "junbari"
                result["reason"] = f"【上昇初動】25日線からの乖離率も{ma25_divergence:.1f}%と低く、出来高急増（{volume_ratio:.1f}倍）を伴う本物のクロス。ここからのエントリーなら安全圏です。"
                return result

        except Exception as e:
            logger.error(f"Error: {e}")
            result["reason"] = f"エラー: {str(e)}"

        return result