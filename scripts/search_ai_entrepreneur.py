"""搜抖音 AI 创业博主,挑 top 爆款给 qwen-omni 拆解。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from tools.tikhub.tikhub_tools import search_douyin_videos


KEYWORDS = [
    "AI创业",
    "AI副业",
    "AI赚钱",
    "AI工具创业",
    "AI创业者",
    "AI创业项目",
    "一个人用AI",
    "AI变现",
]


async def main():
    all_results = {}
    flat_top = []
    for kw in KEYWORDS:
        try:
            resp = await search_douyin_videos(
                keyword=kw, sort_type="_1", publish_time="_180", page=1
            )
            text = resp.content[0]["text"]
            data = json.loads(text)
            videos = data.get("list", [])
            filtered = [
                {
                    "keyword": kw,
                    "id": v["id"],
                    "desc": (v["desc"] or "")[:180],
                    "author": v["author"]["nickname"],
                    "follower": v["author"].get("follower_count", 0),
                    "digg": v["stats"]["digg"],
                    "comment": v["stats"]["comment"],
                    "share": v["stats"]["share"],
                    "url": v["url"],
                }
                for v in videos
                if v.get("stats", {}).get("digg", 0) >= 1000
            ]
            filtered.sort(key=lambda x: x["digg"], reverse=True)
            all_results[kw] = filtered[:5]
            flat_top.extend(filtered[:3])
            print(f"[{kw}] {len(filtered)} hits (top {filtered[0]['digg'] if filtered else 0})")
        except Exception as e:
            print(f"[{kw}] ERROR: {e}")
            all_results[kw] = {"error": str(e)}

    flat_top.sort(key=lambda x: x["digg"], reverse=True)
    out = ROOT / "scripts" / "ai_entrepreneur_results.json"
    out.write_text(
        json.dumps({"by_keyword": all_results, "flat_top": flat_top[:15]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nSaved to {out}, flat_top={len(flat_top[:15])}")


if __name__ == "__main__":
    asyncio.run(main())
