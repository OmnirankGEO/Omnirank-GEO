"""Smoke test: 5 configs × 1 short prompt · 验证 API 能通 + thinking 语法正确"""
import asyncio
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.deepseek_v4_compare import CONFIGS  # noqa: E402


SHORT_SYSTEM = "你是一位简洁的助手。"
SHORT_USER = "用不超过 40 字介绍'深圳'这座城市。"


async def smoke_one(cfg: dict) -> None:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SHORT_SYSTEM},
            {"role": "user", "content": SHORT_USER},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "stream": False,
    }
    if cfg["thinking"] == "enabled":
        body["thinking"] = {"type": "enabled"}
    elif cfg["thinking"] == "disabled":
        body["thinking"] = {"type": "disabled"}

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                cfg["base_url"],
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            elapsed = time.time() - t0
            if resp.status_code != 200:
                print(f"[{cfg['key']}] FAIL HTTP {resp.status_code}: {resp.text[:300]}")
                return
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content", "")
            reasoning = msg.get("reasoning_content", "")
            usage = data.get("usage", {})
            print(
                f"[{cfg['key']}] OK {elapsed:.1f}s"
                f" · in={usage.get('prompt_tokens', 0)} out={usage.get('completion_tokens', 0)}"
                f" · reason_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}"
            )
            print(f"    content: {content[:120]}")
            if reasoning:
                print(f"    reasoning: {reasoning[:120]}...")
    except Exception as e:
        print(f"[{cfg['key']}] EXCEPTION {type(e).__name__}: {str(e)[:200]}")


async def main():
    print("Smoke test: 5 configs · short prompt")
    print("=" * 60)
    # Run in parallel · 5 独立 endpoint, 无相互阻塞
    await asyncio.gather(*[smoke_one(cfg) for cfg in CONFIGS])


if __name__ == "__main__":
    asyncio.run(main())
