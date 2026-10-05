"""拿 3 条抖音目标视频的 play_url:直接调 search REST API 从 raw aweme_list 里挖。"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

TIKHUB_KEY = os.getenv("TIKHUB_API_KEY")


TARGETS = [
    {"id": "7543565048950689058", "author": "徐老师AI", "digg": 65212, "kw": "AI赚钱",
     "title": "邪修一人用AI搞定电商月赚6位数"},
    {"id": "7619406490651466985", "author": "露露在干嘛", "digg": 84640, "kw": "AI副业",
     "title": "用Ai打三份工的日常"},
    {"id": "7594783507453398299", "author": "AI冷科长", "digg": 38933, "kw": "AI赚钱",
     "title": "挑战一人一手机六小时用AI赚第一笔钱"},
]


async def raw_search(keyword: str, page: int = 1) -> dict:
    """对等 _do_search_douyin 但保留 raw 返回"""
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2",
            headers={
                "Authorization": f"Bearer {TIKHUB_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "keyword": keyword,
                "sortType": "_1",
                "publishTime": "_180",
                "duration": "_0",
                "page": page,
                "searchId": "",
            },
        )
        r.raise_for_status()
        return r.json()


def walk_aweme_list(data):
    """V2 返回结构:data.aweme_list 或 data.business_data[i].data.aweme_info"""
    if not data:
        return []
    root = data.get("data") or data
    aweme_list = root.get("aweme_list") or []
    if aweme_list:
        return aweme_list
    # business_data 结构
    business_data = root.get("business_data") or []
    collected = []
    for item in business_data:
        info = (item.get("data") or {}).get("aweme_info")
        if info:
            if "aweme_id" not in info and "id" in info:
                info["aweme_id"] = info["id"]
            collected.append(info)
    return collected


def extract_play_url(aweme):
    video = aweme.get("video", {}) or {}
    play_addr = video.get("play_addr", {}) or {}
    url_list = play_addr.get("url_list", []) or []
    download_addr = video.get("download_addr", {}) or {}
    download_url = (download_addr.get("url_list") or [None])[0]
    return {
        "play_url": url_list[0] if url_list else None,
        "play_url_list": url_list,
        "download_url": download_url,
        "duration_ms": video.get("duration"),
        "ratio": video.get("ratio"),
        "width": video.get("width"),
        "height": video.get("height"),
    }


async def main():
    # 3 个目标分属 AI赚钱 / AI副业 两个 kw,各搜多页
    # 额外加 kw 提升露露命中率
    keyword_pages = {"AI赚钱": {1, 2}, "AI副业": {1, 2, 3}, "Ai打三份工": {1}, "用AI打工": {1}}

    pool = {}  # aweme_id -> aweme raw
    for kw, pages in keyword_pages.items():
        for page in sorted(pages):
            try:
                raw = await raw_search(kw, page=page)
                aweme_list = walk_aweme_list(raw)
                for a in aweme_list:
                    aid = a.get("aweme_id") or a.get("id")
                    if aid:
                        pool[str(aid)] = a
                print(f"[{kw} p{page}] got {len(aweme_list)} aweme, pool size={len(pool)}")
            except Exception as e:
                print(f"[{kw} p{page}] ERROR: {e}")

    results = []
    for t in TARGETS:
        aweme = pool.get(t["id"])
        if not aweme:
            results.append({"target": t, "error": "not found in search pool"})
            print(f"[{t['author']}] MISS in pool")
            continue
        info = extract_play_url(aweme)
        duration_sec = round(info["duration_ms"] / 1000, 1) if info.get("duration_ms") else None
        results.append({
            "target": t,
            "duration_sec": duration_sec,
            "play_url": info["play_url"],
            "download_url": info["download_url"],
            "play_url_list": info["play_url_list"],
        })
        print(f"[{t['author']}] dur={duration_sec}s, url={'yes' if info['play_url'] else 'NO'}")
        if info["play_url"]:
            print(f"  {info['play_url'][:150]}...")

    out = ROOT / "scripts" / "video_detail.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
