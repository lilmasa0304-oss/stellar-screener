import os
import yaml
from pathlib import Path
from typing import List

from dotenv import load_dotenv


class Config:
    """Loads and validates configuration from config.yaml and environment variables (.env)."""

    def __init__(self, config_path="config.yaml"):
        load_dotenv()

        self.line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        self.line_user_id = os.getenv("LINE_USER_ID")

        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(path, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

        self.universe = self.data.get("universe", "")
        self.tickers = self.resolve_tickers()
        self.strategies = self.data.get("strategies", {})
        self.settings = self.data.get("settings", {})

        self.delay_seconds = float(self.settings.get("delay_seconds", 0.5))
        self.history_period = self.settings.get("history_period", "6mo")

    def resolve_tickers(self) -> List[str]:
        """
        config.yaml の tickers を返す。
        universe: jpx400 または tickers が空のときは JPX400 静的リストを使用する。
        """
        raw = self.data.get("tickers") or []
        if self.data.get("universe") == "jpx400" and not raw:
            return self._load_jpx400_tickers()
        if len(raw) == 1 and str(raw[0]).lower() in ("jpx400", "auto"):
            return self._load_jpx400_tickers()
        if raw:
            return list(raw)
        if self.data.get("universe") == "jpx400":
            return self._load_jpx400_tickers()
        return []

    @staticmethod
    def _load_jpx400_tickers() -> List[str]:
        from screener.jpx400 import get_jpx400_tickers

        return get_jpx400_tickers()

    def is_jpx400_universe(self) -> bool:
        return self.data.get("universe") == "jpx400" or len(self.tickers) >= 350

    def validate_line_credentials(self) -> bool:
        """Checks if the LINE access token and user ID are configured correctly."""
        if not self.line_token or self.line_token == "your_channel_access_token_here":
            return False
        if not self.line_user_id or self.line_user_id == "your_user_id_here":
            return False
        return True
