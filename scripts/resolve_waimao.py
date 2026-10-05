"""解析 4月20日 新对标视频短链 → 拿 aweme_id + play_url。"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

TIKHUB_KEY = os.getenv("TIKHUB_API_KEY")
SHORT_URL = "https://v.douyin.com/WOZvwoa8SCA/"
SEARCH_KWS = ["外贸术语", "外贸新手入门", "外贸怎么做", "外贸出口", "外贸新航道"]


async def resolve_short(url: str) -> dict:
    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0,
                                  headers={"User-Agent": "Mozilla/5.0"}) as client:
        current = url
        for _ in range(6):
            r = await client.get(current)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location") or r.headers.get("Location")
                if not loc:
                    break
                current = loc
                m = re.search(r"/video/(\d+)", current)
                if m:
                    return {"aweme_id": m.group(1), "final_url": current}
            else:
                m = re.search(r"/video/(\d+)", r.text) or re.search(r'"aweme_id"\s*:\s*"(\d+)"', r.text)
                if m:
                    return {"aweme_id": m.group(1), "final_url": str(r.url)}
                break
        return {"aweme_id": None, "final_url": current}


async def raw_search(keyword: str, page: int = 1) -> dict:
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2",
            headers={"Authorization": f"Bearer {TIKHUB_KEY}", "Content-Type": "application/json"},
            json={"keyword": keyword, "sortType": "_0", "publishTime": "_180",
                  "duration": "_0", "page": page, "searchId": ""},
        )
        r.raise_for_status()
        return r.json()


def walk_aweme_list(data):
    if not data:
        return []
    root = data.get("data") or data
    return root.get("aweme_list") or []


def extract_info(aweme):
    video = aweme.get("video", {}) or {}
    play_addr = video.get("play_addr", {}) or {}
    url_list = play_addr.get("url_list", []) or []
    return {
        "play_url": url_list[0] if url_list else None,
        "duration_ms": video.get("duration"),
        "desc": aweme.get("desc"),
        "author": (aweme.get("author", {}) or {}).get("nickname"),
        "follower": (aweme.get("author", {}) or {}).get("follower_count"),
        "stats": {
            "digg": (aweme.get("statistics", {}) or {}).get("digg_count"),
            "comment": (aweme.get("statistics", {}) or {}).get("comment_count"),
            "share": (aweme.get("statistics", {}) or {}).get("share_count"),
        },
    }


async def main():
    print(f"Resolving: {SHORT_URL}")
    resolved = await resolve_short(SHORT_URL)
    aweme_id = resolved.get("aweme_id")
    print(f"  aweme_id = {aweme_id}")
    if not aweme_id:
        print("FAIL")
        return

    pool = {}
    for kw in SEARCH_KWS:
        for page in (1, 2, 3):
            try:
                raw = await raw_search(kw, page)
                for a in walk_aweme_list(raw):
                    aid = str(a.get("aweme_id") or a.get("id") or "")
                    if aid:
                        pool[aid] = a
                print(f"  [{kw} p{page}] pool={len(pool)}")
                if aweme_id in pool:
                    print(f"  HIT at [{kw} p{page}]")
                    break
            except Exception as e:
                print(f"  [{kw} p{page}] err: {e}")
        if aweme_id in pool:
            break

    if aweme_id not in pool:
        print(f"MISS — pool={len(pool)} · not in pool")
        out = ROOT / "scripts" / "resolved_waimao.json"
        out.write_text(json.dumps({"resolved": resolved, "pool_size": len(pool)},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        return

    info = extract_info(pool[aweme_id])
    dur_sec = round(info["duration_ms"] / 1000, 1) if info.get("duration_ms") else None
    print(f"\n[{info['author']}] dur={dur_sec}s, digg={info['stats']['digg']}")
    print(f"desc: {(info['desc'] or '')[:200]}")
    out = ROOT / "scripts" / "resolved_waimao.json"
    out.write_text(json.dumps({
        "resolved": resolved,
        "video": {
            "aweme_id": aweme_id,
            "author": info["author"],
            "follower": info["follower"],
            "digg": info["stats"]["digg"],
            "comment": info["stats"]["comment"],
            "share": info["stats"]["share"],
            "duration_sec": dur_sec,
            "desc": info["desc"],
            "play_url": info["play_url"],
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
