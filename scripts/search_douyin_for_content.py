"""
临时脚本：搜抖音相关科普视频，给老板的脚本助理 AI 做对标素材。
关键词围绕 OmniRank 的降维打击主题。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from tools.tikhub.tikhub_tools import search_douyin_videos


KEYWORDS = [
    "AI搜索优化",
    "DeepSeek推荐",
    "GEO优化",
    "AI代运营",
    "SEO已死",
    "AI搜索时代",
    "让AI推荐你",
    "AI搜索引擎",
]


async def main():
    all_results = {}
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
                    "id": v["id"],
                    "desc": v["desc"][:200] if v.get("desc") else "",
                    "author": v["author"]["nickname"],
                    "follower": v["author"].get("follower_count", 0),
                    "digg": v["stats"]["digg"],
                    "comment": v["stats"]["comment"],
                    "share": v["stats"]["share"],
                    "url": v["url"],
                }
                for v in videos
                if v.get("stats", {}).get("digg", 0) >= 500
            ]
            filtered.sort(key=lambda x: x["digg"], reverse=True)
            all_results[kw] = filtered[:5]
            print(f"[{kw}] {len(filtered)} hits (top digg {filtered[0]['digg'] if filtered else 0})")
        except Exception as e:
            print(f"[{kw}] ERROR: {e}")
            all_results[kw] = {"error": str(e)}

    out = ROOT / "scripts" / "douyin_search_results.json"
    out.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
