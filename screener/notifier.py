import json
import logging
import requests
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class LineNotifier:
    """LINE Messaging API を使った通知クライアント。"""

    API_URL = "https://api.line.me/v2/bot/message/push"

    def __init__(self, token: str, user_id: str):
        self.token   = token
        self.user_id = user_id

    # ─────────────────────────────────────────────────────────────────
    # 従来の汎用メッセージ（後方互換）
    # ─────────────────────────────────────────────────────────────────
    def build_message(self, matched_results: List[Dict[str, Any]]) -> str:
        """
        汎用スクリーニング結果テキストを生成する（従来フォーマット）。
        """
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
            price  = item["current_price"]
            change = item["change_percent"]
            signals = item["signals"]

            price_str = f"{price:,.1f}円" if ticker.endswith(".T") else f"${price:,.2f}"

            lines.append(f"■ {name} ({ticker})")
            lines.append(f"  ・現在値: {price_str} (前日比 {change:+.2f}%)")
            lines.append("  ・シグナル:")
            for sig in signals:
                lines.append(f"    - {sig}")
            lines.append("")

        lines.append("--------------------")
        lines.append("※この通知は自動配信されています。")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────
    # 新規: BUY SIGNAL 専用フォーマット
    # ─────────────────────────────────────────────────────────────────
    def build_buy_signal_message(self, buy_signal_results: List[Dict[str, Any]]) -> str:
        """
        [BUY SIGNAL] 専用の LINE メッセージを生成する。

        3条件（MA25上向き / RSI35〜45 / MACDゴールデンクロス）を
        個別に明示したリッチなフォーマット。
        """
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
            ticker = item["ticker"]
            name   = item.get("name", ticker)
            price  = item.get("current_price")
            change = item.get("change_percent", 0.0)
            rsi    = item.get("rsi")
            ma25   = item.get("ma25")
            macd_v = item.get("macd")
            macd_s = item.get("macd_signal")
            macd_h = item.get("macd_hist")
            is_gc  = item.get("macd_crossover", False)
            uptrend = item.get("ma25_uptrend", False)

            price_str  = f"{price:,.1f}円" if (price and ticker.endswith(".T")) else (f"${price:,.2f}" if price else "--")
            change_str = f"{change:+.2f}%"
            trend_str  = "↑ 上向き" if uptrend else "↓ 下向き"
            macd_label = "✅ ゴールデンクロス" if is_gc else "⚡ GC直前（収束中）"

            lines.append(f"▶ {name} ({ticker})")
            lines.append(f"  💴 株価: {price_str}（前日比 {change_str}）")
            lines.append("")

            # 条件①: MA25
            ma25_str = f"{ma25:,.1f}円" if ma25 else "--"
            lines.append(f"  ✅ 条件①: 25日移動平均線 {trend_str}")
            lines.append(f"     MA25: {ma25_str}")

            # 条件②: RSI
            rsi_str = f"{rsi:.1f}" if rsi is not None else "--"
            lines.append(f"  ✅ 条件②: RSI(14) = {rsi_str}（低値圏反発帯 35〜45）")

            # 条件③: MACD
            macd_val_str = f"MACD:{macd_v:.2f} / Signal:{macd_s:.2f}" if (macd_v is not None and macd_s is not None) else "--"
            lines.append(f"  ✅ 条件③: {macd_label}")
            if macd_h is not None:
                lines.append(f"     {macd_val_str} / ヒスト:{macd_h:.3f}")

            lines.append("━━━━━━━━━━━━━━━━")

        lines.append("⚠ 投資判断は自己責任でお願いします。")
        lines.append("※ STELLAR SCREENER による自動通知")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────
    # 送信
    # ─────────────────────────────────────────────────────────────────
    def send_notification(self, message: str) -> bool:
        """
        LINE Push Messaging API でメッセージを送信する。

        Returns:
            True if successful, False otherwise.
        """
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
            logger.info(f"Sending LINE message to user {self.user_id}...")
            response = requests.post(
                self.API_URL,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("LINE notification sent successfully.")
                return True
            else:
                logger.error(
                    f"LINE notification failed. "
                    f"Status: {response.status_code}, Body: {response.text}"
                )
                return False

        except Exception as e:
            logger.error(f"Error sending LINE notification: {e}")
            return False
