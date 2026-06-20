"""Sync config.yaml tickers from screener/jpx400.py static list."""
from pathlib import Path

import yaml

from screener.jpx400 import get_jpx400_tickers


def sync_config(config_path: Path | None = None) -> int:
    path = config_path or Path("config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    tickers = get_jpx400_tickers()
    data["universe"] = "jpx400"
    data["tickers"] = tickers
    data.setdefault("settings", {})
    data["settings"].setdefault("delay_seconds", 0.5)
    data["settings"].setdefault("history_period", "6mo")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    print(f"Synced {len(tickers)} JPX400 tickers to {path}")
    return len(tickers)


if __name__ == "__main__":
    sync_config()
