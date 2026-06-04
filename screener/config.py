import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

class Config:
    """Loads and validates configuration from config.yaml and environment variables (.env)."""
    
    def __init__(self, config_path="config.yaml"):
        # Load environment variables from .env if present
        load_dotenv()
        
        self.line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        self.line_user_id = os.getenv("LINE_USER_ID")
        
        # Load YAML config
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
            
        with open(path, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}
            
        self.tickers = self.data.get("tickers", [])
        self.strategies = self.data.get("strategies", {})
        self.settings = self.data.get("settings", {})
        
        # Load general settings with defaults
        self.delay_seconds = float(self.settings.get("delay_seconds", 1.0))
        self.history_period = self.settings.get("history_period", "3mo")

    def validate_line_credentials(self) -> bool:
        """Checks if the LINE access token and user ID are configured correctly."""
        if not self.line_token or self.line_token == "your_channel_access_token_here":
            return False
        if not self.line_user_id or self.line_user_id == "your_user_id_here":
            return False
        return True
