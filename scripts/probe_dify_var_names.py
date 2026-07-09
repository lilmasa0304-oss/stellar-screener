"""Dify ワークフローが参照する inputs 変数名を推定する。"""
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

from web_app import DIFY_API_KEY, DIFY_BASE_URL  # noqa: E402

BASE = {
    "query": "285A",
    "response_mode": "blocking",
    "user": "probe-var-names",
}

CANDIDATES = {
    "pattern_a": {
        "code": "285A",
        "mode": "堅実",
        "rsi": "47.9",
        "current_price": "77860",
        "ma25_deviation_pct": "-9.88",
        "volume_ratio": "0.85",
        "name": "KIOXIA HOLDINGS CORPORATION",
    },
    "pattern_b": {
        "code": "285A",
        "mode": "堅実",
        "rsi1": "47.9",
        "price": "77860",
        "ma25_divergence": "-9.88",
        "volume_ratio1": "0.85",
        "stock_name": "KIOXIA HOLDINGS CORPORATION",
    },
    "pattern_c": {
        "code": "285A",
        "mode": "堅実",
        "RSI": "47.9",
        "price1": "77860",
        "ma25_divergence_pct": "-9.88",
        "volume_ratio": "0.85",
        "company_name": "KIOXIA HOLDINGS CORPORATION",
    },
}

url = f"{DIFY_BASE_URL.rstrip('/')}/chat-messages"
headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}


def extract_rsi(text: str) -> list[str]:
    return re.findall(r"RSI[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)


for label, inputs in CANDIDATES.items():
    resp = requests.post(url, headers=headers, json={**BASE, "inputs": inputs}, timeout=120)
    print(f"\n=== {label} status={resp.status_code} ===")
    if not resp.ok:
        print(resp.text[:300])
        continue
    answer = resp.json().get("answer", "")
    rsi_hits = extract_rsi(answer)
    print("RSI values in answer:", rsi_hits)
    print("contains 47.9:", "47.9" in answer or "47.90" in answer)
    print("contains 20.0:", "20.0" in answer or "20.00" in answer)
    print("preview:", answer[:180].replace("\n", " / "))
