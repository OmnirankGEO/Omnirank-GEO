"""S1(选题规划)JSON 稳定性追测 · 3 配置各跑 5 次看失败率"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.topic_planning_ab_test import (  # noqa: E402
    CONFIGS, SCENE1_SYSTEM, SCENE1_USER, SCENE1_PARAMS, evaluate_output,
)

N_RUNS = 5


async def one_call(cfg: dict, run_idx: int) -> dict:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SCENE1_SYSTEM},
            {"role": "user", "content": SCENE1_USER},
        ],
        "temperature": SCENE1_PARAMS["temperature"],
        "max_tokens": SCENE1_PARAMS["max_tokens"],
    }
    if cfg["thinking"] == "enabled":
        body["thinking"] = {"type": "enabled"}
    elif cfg["thinking"] == "disabled":
        body["thinking"] = {"type": "disabled"}

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(
                cfg["base_url"],
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json=body,
            )
            elapsed = time.time() - t0
            if r.status_code != 200:
                return {"config_key": cfg["key"], "run": run_idx, "success": False,
                        "error": f"HTTP {r.status_code}", "elapsed_s": round(elapsed, 2)}
            d = r.json()
            content = d["choices"][0]["message"].get("content", "") or ""
            usage = d.get("usage", {})
            ev = evaluate_output("s1_topic_dispatch", content)
            # Further parse test: look for >= 10 topics
            topic_count = 0
            parse_ok = ev["valid_json"]
            try:
                m = re.search(r"(\{[\s\S]*\})", content)
                if m:
                    obj = json.loads(m.group(1))
                    topic_count = len(obj.get("topics", [])) if isinstance(obj, dict) else 0
            except Exception:
                pass
            return {
                "config_key": cfg["key"], "run": run_idx, "success": True,
                "elapsed_s": round(elapsed, 2),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
                "valid_json": parse_ok,
                "topic_count": topic_count,
                "content_len": len(content),
            }
    except Exception as e:
        return {"config_key": cfg["key"], "run": run_idx, "success": False,
                "error": f"{type(e).__name__}: {str(e)[:150]}", "elapsed_s": round(time.time() - t0, 2)}


async def main():
    print(f"S1 稳定性追测 · 3 配置 × {N_RUNS} runs = {3*N_RUNS} calls")
    print(f"开始: {datetime.now().strftime('%H:%M:%S')}")

    # Run 3 configs in parallel, each runs N_RUNS sequentially
    async def run_config_seq(cfg):
        out = []
        for i in range(N_RUNS):
            print(f"  [{cfg['key']}] run {i+1}/{N_RUNS}...", end=" ", flush=True)
            r = await one_call(cfg, i + 1)
            if r["success"]:
                print(f"OK {r['elapsed_s']}s | valid_json={r['valid_json']} | topics={r['topic_count']}")
            else:
                print(f"FAIL {r.get('error','')[:60]}")
            out.append(r)
        return out

    t0 = time.time()
    results_nested = await asyncio.gather(*[run_config_seq(cfg) for cfg in CONFIGS])
    elapsed = time.time() - t0
    print(f"\n[done] {elapsed:.1f}s\n")

    all_results = []
    for rs in results_nested:
        all_results.extend(rs)

    # Stability report
    print("=" * 70)
    print(f"{'config':30s}  success  valid_json  avg_topic_count  avg_elapsed")
    for cfg in CONFIGS:
        rs = [r for r in all_results if r["config_key"] == cfg["key"]]
        succ = [r for r in rs if r["success"]]
        valid = sum(1 for r in succ if r.get("valid_json"))
        topic_avg = sum(r.get("topic_count", 0) for r in succ) / max(1, len(succ))
        elapsed_avg = sum(r["elapsed_s"] for r in succ) / max(1, len(succ))
        print(f"{cfg['label']:30s}  {len(succ)}/{len(rs)}  {valid}/{len(succ)}        {topic_avg:.1f}            {elapsed_avg:.1f}s")

    # Save
    out_path = Path(__file__).parent / "topic_planning_ab_results" / "_s1_stability.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\nRaw: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
