import pandas as pd
from typing import Dict, List, Any, Optional
from .indicators import (
    calculate_rsi,
    calculate_sma,
    calculate_bollinger_bands,
    calculate_macd,
    detect_macd_crossover,
)


class StrategyEvaluator:
    """
    日本株スイングトレード専用のテクニカル分析エンジン。

    ▸ 従来シグナル: RSI売られすぎ/MAクロス/BB逸脱/急騰急落
    ▸ 新規スイング BUY SIGNAL (3条件 AND 判定):
        ①  25日SMAが上向き（前日比上昇）
        ②  RSI(14) が 35〜45 の低値圏反発帯
        ③  MACD ゴールデンクロス or ゴールデンクロス直前（ヒスト収束）
    """

    def __init__(self, config_data: Dict[str, Any]):
        self.strategies   = config_data.get("strategies",    {})
        self.swing_cfg    = config_data.get("swing_signal",  {})

    # ─────────────────────────────────────────────────────────────────
    def evaluate(self, ticker: str, name: str, df: pd.DataFrame) -> Dict[str, Any]:
        """
        全テクニカル指標を計算し、各シグナル・メタデータを返す。

        Returns dict with:
          ticker, name, current_price, change_percent,
          triggered, signals, is_prime_entry,
          buy_signal (新規スイングシグナル),
          rsi, ma5, ma25, ma75, bb_upper, bb_lower,
          ma25_deviation_pct, ma25_uptrend, price_above_ma25, bb_lower_touch,
          macd, macd_signal, macd_hist, macd_crossover, macd_pre_crossover
        """
        if len(df) < 2:
            return self._empty_result(ticker, name, "データ数が足りません")

        close         = df['Close']
        current_price = float(close.iloc[-1])
        prev_price    = float(close.iloc[-2])
        change_pct    = ((current_price - prev_price) / prev_price) * 100.0

        # ── 1. RSI ────────────────────────────────────────────────
        rsi_cfg    = self.strategies.get("rsi", {})
        rsi_period = int(rsi_cfg.get("period", 14))
        oversold   = float(rsi_cfg.get("oversold_threshold",  30.0))
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

        # ── 2. 移動平均線 5 / 25 / 75 ────────────────────────────
        ma_cfg = self.strategies.get("ma_trend", {})
        ma5_p  = int(ma_cfg.get("ma5_period",  5))
        ma25_p = int(ma_cfg.get("ma25_period", 25))
        ma75_p = int(ma_cfg.get("ma75_period", 75))

        sma5  = calculate_sma(df, ma5_p)
        sma25 = calculate_sma(df, ma25_p)
        sma75 = calculate_sma(df, ma75_p)

        last_ma5  = float(sma5.iloc[-1])  if pd.notna(sma5.iloc[-1])  else None
        last_ma25 = float(sma25.iloc[-1]) if pd.notna(sma25.iloc[-1]) else None
        last_ma75 = float(sma75.iloc[-1]) if pd.notna(sma75.iloc[-1]) else None

        # 25日線トレンド判定（前日比で上昇していれば "上向き"）
        ma25_uptrend     = False
        price_above_ma25 = False
        ma25_deviation   = None

        if last_ma25 is not None and len(sma25) >= 2 and pd.notna(sma25.iloc[-2]):
            prev_ma25        = float(sma25.iloc[-2])
            ma25_uptrend     = last_ma25 > prev_ma25
            price_above_ma25 = current_price > last_ma25
            ma25_deviation   = ((current_price - last_ma25) / last_ma25) * 100.0

        # 5日線リバウンド判定
        ma5_rebound = False
        if last_ma5 is not None and len(sma5) >= 2 and pd.notna(sma5.iloc[-2]):
            prev_ma5    = float(sma5.iloc[-2])
            ma5_rebound = last_ma5 > prev_ma5

        # ── 3. ボリンジャーバンド ─────────────────────────────────
        bb_cfg = self.strategies.get("bollinger", {})
        bb_p   = int(bb_cfg.get("period", 20))
        bb_std = float(bb_cfg.get("std_dev", 2.0))

        bb_upper_s, bb_lower_s = calculate_bollinger_bands(df, bb_p, bb_std)
        last_bb_upper  = float(bb_upper_s.iloc[-1]) if pd.notna(bb_upper_s.iloc[-1]) else None
        last_bb_lower  = float(bb_lower_s.iloc[-1]) if pd.notna(bb_lower_s.iloc[-1]) else None
        bb_lower_touch = (last_bb_lower is not None) and (current_price <= last_bb_lower)

        # ── 4. MACD ───────────────────────────────────────────────
        macd_line, macd_signal_line, macd_hist = calculate_macd(df)
        last_macd        = float(macd_line.iloc[-1])        if pd.notna(macd_line.iloc[-1])        else None
        last_macd_signal = float(macd_signal_line.iloc[-1]) if pd.notna(macd_signal_line.iloc[-1]) else None
        last_macd_hist   = float(macd_hist.iloc[-1])        if pd.notna(macd_hist.iloc[-1])        else None

        macd_crossover, macd_pre_crossover = detect_macd_crossover(macd_hist)

        # ── 5. 個別シグナル収集（従来ロジック） ──────────────────
        signals: List[str] = []

        # RSI シグナル
        if rsi_cfg.get("enabled", True) and last_rsi is not None:
            if last_rsi <= oversold:
                signals.append(f"RSI売られすぎ: {last_rsi:.1f}（基準 {oversold}以下）")
            elif last_rsi >= overbought:
                signals.append(f"RSI買われすぎ: {last_rsi:.1f}（基準 {overbought}以上）")

        # MA クロスオーバー
        cross_cfg = self.strategies.get("crossover", {})
        if cross_cfg.get("enabled", True) and len(df) >= ma25_p + 1:
            s5_y  = float(sma5.iloc[-2])  if pd.notna(sma5.iloc[-2])  else None
            s25_y = float(sma25.iloc[-2]) if pd.notna(sma25.iloc[-2]) else None
            if None not in (last_ma5, last_ma25, s5_y, s25_y):
                if s5_y <= s25_y and last_ma5 > last_ma25:
                    signals.append("ゴールデンクロス（SMA5がSMA25を上抜け）")
                elif s5_y >= s25_y and last_ma5 < last_ma25:
                    signals.append("デッドクロス（SMA5がSMA25を下抜け）")

        # MA トレンドフィルター
        if ma_cfg.get("enabled", True):
            if ma25_uptrend and price_above_ma25:
                signals.append("25日線上向き＆株価が25日線の上（押し目候補）")
            elif not ma25_uptrend and not price_above_ma25:
                signals.append("25日線下向き＆株価が25日線の下（下落トレンド）")

        # MACD シグナル
        if macd_crossover:
            signals.append(
                f"MACDゴールデンクロス（MACD:{last_macd:.2f} / Signal:{last_macd_signal:.2f}）"
            )
        elif macd_pre_crossover:
            signals.append(
                f"MACD GC直前・収束中（ヒスト: {last_macd_hist:.2f}）"
            )

        # BB シグナル
        if bb_cfg.get("enabled", True):
            if bb_lower_touch:
                signals.append(
                    f"ボリンジャーバンド −2σ タッチ（{current_price:,.1f}円 ≤ {last_bb_lower:,.1f}円）"
                )
            elif last_bb_upper is not None and current_price >= last_bb_upper:
                signals.append(
                    f"ボリンジャーバンド +2σ タッチ（{current_price:,.1f}円 ≥ {last_bb_upper:,.1f}円）"
                )

        # 前日比急騰/急落
        dc_cfg = self.strategies.get("daily_change", {})
        if dc_cfg.get("enabled", True):
            threshold = float(dc_cfg.get("threshold_percent", 3.0))
            if abs(change_pct) >= threshold:
                direction = "急騰" if change_pct > 0 else "急落"
                signals.append(f"前日比{direction}: {change_pct:+.1f}%（基準 ±{threshold}%）")

        # ── 6. 従来複合エントリーシグナル判定 ───────────────────
        is_prime_entry   = False
        comp_cfg         = self.strategies.get("composite_entry", {})
        rsi_oversold_hit = (last_rsi is not None) and (last_rsi <= oversold)

        require_ma25_up  = ma_cfg.get("require_ma25_uptrend",      True)
        require_bb_touch = bb_cfg.get("require_lower_band_touch",  False)

        if comp_cfg.get("enabled", True) and rsi_oversold_hit:
            conditions_met = [True]  # RSI 条件はベース
            if require_ma25_up:
                conditions_met.append(ma25_uptrend)
            if require_bb_touch:
                conditions_met.append(bb_lower_touch)
            if all(conditions_met):
                is_prime_entry = True
                signals.append("⭐ 絶好のエントリータイミング（複合条件クリア）")

        # ── 7. 新規スイング BUY SIGNAL 判定 ──────────────────────
        # 設定から閾値を取得
        swing_rsi_min = float(self.swing_cfg.get("rsi_min", 35.0))
        swing_rsi_max = float(self.swing_cfg.get("rsi_max", 45.0))
        req_ma25      = self.swing_cfg.get("require_ma25_uptrend", True)
        req_macd      = self.swing_cfg.get("require_macd_cross",   True)

        # 条件評価
        cond_rsi  = (last_rsi is not None) and (swing_rsi_min <= last_rsi <= swing_rsi_max)
        cond_ma25 = ma25_uptrend if req_ma25 else True
        cond_macd = (macd_crossover or macd_pre_crossover) if req_macd else True

        buy_signal = cond_rsi and cond_ma25 and cond_macd

        if buy_signal:
            signals.append(
                f"🚀 [BUY SIGNAL] スイング買い条件クリア"
                f"（RSI:{last_rsi:.1f} / MA25↑ / MACD{'GC' if macd_crossover else 'GC接近'}）"
            )

        return {
            "ticker":               ticker,
            "name":                 name,
            "current_price":        current_price,
            "change_percent":       change_pct,
            "triggered":            len(signals) > 0,
            "signals":              signals,
            "is_prime_entry":       is_prime_entry,
            "buy_signal":           buy_signal,
            # テクニカル指標値
            "rsi":                  round(last_rsi,   1) if last_rsi   is not None else None,
            "ma5":                  round(last_ma5,   1) if last_ma5   is not None else None,
            "ma25":                 round(last_ma25,  1) if last_ma25  is not None else None,
            "ma75":                 round(last_ma75,  1) if last_ma75  is not None else None,
            "bb_upper":             round(last_bb_upper, 1) if last_bb_upper is not None else None,
            "bb_lower":             round(last_bb_lower, 1) if last_bb_lower is not None else None,
            "ma25_deviation_pct":   round(ma25_deviation, 2) if ma25_deviation is not None else None,
            "ma25_uptrend":         ma25_uptrend,
            "price_above_ma25":     price_above_ma25,
            "bb_lower_touch":       bb_lower_touch,
            "ma5_rebound":          ma5_rebound,
            # MACD 指標
            "macd":                 round(last_macd,        2) if last_macd        is not None else None,
            "macd_signal":          round(last_macd_signal, 2) if last_macd_signal is not None else None,
            "macd_hist":            round(last_macd_hist,   3) if last_macd_hist   is not None else None,
            "macd_crossover":       macd_crossover,
            "macd_pre_crossover":   macd_pre_crossover,
            # BUY SIGNAL サブ条件（デバッグ/UI用）
            "swing_cond_rsi":       cond_rsi,
            "swing_cond_ma25":      cond_ma25,
            "swing_cond_macd":      cond_macd,
        }

    # ─────────────────────────────────────────────────────────────────
    def _empty_result(self, ticker: str, name: str, reason: str) -> Dict[str, Any]:
        return {
            "ticker": ticker, "name": name,
            "current_price": None, "change_percent": 0.0,
            "triggered": False, "signals": [reason],
            "is_prime_entry": False, "buy_signal": False,
            "rsi": None, "ma5": None, "ma25": None, "ma75": None,
            "bb_upper": None, "bb_lower": None,
            "ma25_deviation_pct": None, "ma25_uptrend": False,
            "price_above_ma25": False, "bb_lower_touch": False, "ma5_rebound": False,
            "macd": None, "macd_signal": None, "macd_hist": None,
            "macd_crossover": False, "macd_pre_crossover": False,
            "swing_cond_rsi": False, "swing_cond_ma25": False, "swing_cond_macd": False,
        }
