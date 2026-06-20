import json
import logging
import requests
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

PRESET_LABELS = {
    "oshieme": "押し目シグナル",
    "junbari": "順張りブレイク",
}


class LineNotifier:
    """LINE Messaging API を使った通知クライアント。"""

    API_URL = "https://api.line.me/v2/bot/message/push"

    def __init__(self, token: str, user_id: str):
        self.token   = token
        self.user_id = user_id

    def build_message(self, matched_results: List[Dict[str, Any]]) -> str:
        """汎用スクリーニング結果テキストを生成する（従来フォーマット）。"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            "【📈 株価スクリーニング通知】",
            f"判定日時: {now_str}",
            "",
            "条件に合致した銘柄が検出されました：",
            "",
        ]

        for item in matched_results:
            ticker = item["ticker"]
            name   = item["name"]
            price  = item.get("current_price") or item.get("close_price")
            change = item.get("change_percent", 0.0)
            signals = item.get("signals") or []

            price_str = f"{price:,.1f}円" if (price and ticker.endswith(".T")) else (
                f"${price:,.2f}" if price else "--"
            )

            lines.append(f"■ {name} ({ticker})")
            lines.append(f"  ・現在値: {price_str} (前日比 {change:+.2f}%)")
            lines.append("  ・シグナル:")
            for sig in signals:
                if isinstance(sig, dict):
                    lines.append(f"    - {sig.get('reason') or sig.get('signal_label', '')}")
                else:
                    lines.append(f"    - {sig}")
            lines.append("")

        lines.append("--------------------")
        lines.append("※この通知は自動配信されています。")

        return "\n".join(lines)

    def build_buy_signal_message(self, buy_signal_results: List[Dict[str, Any]]) -> str:
        """[BUY SIGNAL] 専用の LINE メッセージを生成する。"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        count   = len(buy_signal_results)

        lines = [
            "🌟【BUY SIGNAL 検出】STELLAR SCREENER",
            "━━━━━━━━━━━━━━━━",
            f"📅 検知日時: {now_str}",
            f"🔍 検出銘柄数: {count}件",
            "",
        ]

        for item in buy_signal_results:
            ticker  = item["ticker"]
            name    = item.get("name", ticker)
            price   = item.get("current_price") or item.get("close_price")
            change  = item.get("change_percent", 0.0)
            rsi     = item.get("rsi")
            ma25    = item.get("ma25")
            preset  = item.get("preset_matched", "none")
            reason  = item.get("reason", "")
            uptrend = item.get("ma25_uptrend", False)

            price_str  = f"{price:,.1f}円" if (price and ticker.endswith(".T")) else (
                f"${price:,.2f}" if price else "--"
            )
            change_str = f"{change:+.2f}%"
            trend_str  = "↑ 上向き" if uptrend else "↓ 下向き"
            rsi_str    = f"{rsi:.1f}" if rsi is not None else "--"
            ma25_str   = f"{ma25:,.1f}円" if ma25 else "--"
            preset_label = PRESET_LABELS.get(preset, "BUY SIGNAL")

            lines.append(f"▶ {name} ({ticker})")
            lines.append(f"  💴 株価: {price_str}（前日比 {change_str}）")
            lines.append(f"  🎯 シグナル種別: {preset_label}")
            lines.append(f"  📊 RSI(14): {rsi_str}  /  MA25: {ma25_str} ({trend_str})")

            if reason:
                lines.append(f"  📝 {reason}")
            elif preset == "none":
                macd_v = item.get("macd")
                macd_s = item.get("macd_signal")
                macd_h = item.get("macd_hist")
                is_gc  = item.get("macd_crossover", False)
                macd_label = "✅ ゴールデンクロス" if is_gc else "⚡ GC直前（収束中）"
                lines.append(f"  ✅ MACD: {macd_label}")
                if macd_v is not None and macd_s is not None:
                    lines.append(f"     MACD:{macd_v:.2f} / Signal:{macd_s:.2f}")
                if macd_h is not None:
                    lines.append(f"     ヒスト:{macd_h:.3f}")

            lines.append("━━━━━━━━━━━━━━━━")

        lines.append("⚠ 投資判断は自己責任でお願いします。")
        lines.append("※ STELLAR SCREENER による自動通知")

        text = "\n".join(lines)
        if len(text) > 4900:
            text = text[:4900] + "\n…（メッセージが長いため省略）"
        return text

    def send_notification(self, message: str) -> bool:
        """LINE Push Messaging API でメッセージを送信する。"""
        if not self.token or not self.user_id:
            logger.error("LINE credentials not configured. Cannot send message.")
            return False

        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        payload = {
            "to": self.user_id,
            "messages": [{"type": "text", "text": message}],
        }

        try:
            logger.info("Sending LINE BUY SIGNAL notification...")
            response = requests.post(
                self.API_URL,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=15,
            )
            if response.status_code == 200:
                logger.info("LINE notification sent successfully.")
                return True
            logger.error(
                "LINE notification failed. Status: %s, Body: %s",
                response.status_code,
                response.text,
            )
            return False

        except Exception as e:
            logger.error("Error sending LINE notification: %s", e)
            return False
