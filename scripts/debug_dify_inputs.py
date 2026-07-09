"""Dify inputs 形式デバッグ（body 丸ごと送信）。"""
import asyncio
import json
import os
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

from web_app import (  # noqa: E402
    DIFY_API_KEY,
    DIFY_BASE_URL,
    _build_dify_inputs,
    _dify_needs_local_enrichment,
    _is_dify_stub_answer,
    _resolve_screen_data_for_dify,
)


async def main() -> None:
    sd = await _resolve_screen_data_for_dify("285A", "堅実", None)
    full_inputs = _build_dify_inputs("285A", "堅実", sd)
    minimal_inputs = _build_dify_inputs("285A", "堅実", None)

    print("=== full inputs ===")
    print(json.dumps(full_inputs, ensure_ascii=False, indent=2)[:2000])
    print("\n=== input keys ===", sorted(k for k in full_inputs if k != "body"))

    params = requests.get(
        f"{DIFY_BASE_URL.rstrip('/')}/parameters",
        headers={"Authorization": f"Bearer {DIFY_API_KEY}"},
        timeout=20,
    )
    print("\n=== GET /parameters ===", params.status_code)
    if params.ok:
        data = params.json()
        print("user_input_form:", json.dumps(data.get("user_input_form"), ensure_ascii=False))

    url = f"{DIFY_BASE_URL.rstrip('/')}/chat-messages"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    for label, inputs in [("minimal", minimal_inputs), ("full", full_inputs)]:
        resp = requests.post(
            url,
            headers=headers,
            json={
                "inputs": inputs,
                "query": "285A",
                "response_mode": "blocking",
                "user": "debug-dify-inputs",
            },
            timeout=120,
        )
        print(f"\n=== POST chat-messages ({label}) === status {resp.status_code}")
        print(resp.text[:600])
        if resp.ok:
            answer = resp.json().get("answer", "")
            print("stub:", _is_dify_stub_answer(answer))
            print("enrichment:", _dify_needs_local_enrichment(answer))
            print("preview:", answer[:220].replace("\n", " / "))


if __name__ == "__main__":
    asyncio.run(main())
