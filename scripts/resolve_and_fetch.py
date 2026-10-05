"""解析抖音短链 → 拿 aweme_id → 通过搜索 pool / user_videos 找 play_url。"""
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
SHORT_URL = "https://v.douyin.com/OF7oPB_5URo/"
SEARCH_KWS = ["geo优化", "陌拜", "地推销售", "AI销售", "第一视角体验ai销售"]


async def resolve_short(url: str) -> dict:
    """追 302 拿到 aweme_id"""
    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0,
                                  headers={"User-Agent": "Mozilla/5.0"}) as client:
        current = url
        for _ in range(5):
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
                # 非重定向,试从 body 里挖
                html = r.text
                m = re.search(r"/video/(\d+)", html) or re.search(r'"aweme_id"\s*:\s*"(\d+)"', html)
                if m:
                    return {"aweme_id": m.group(1), "final_url": str(r.url), "body_matched": True}
                break
        return {"aweme_id": None, "final_url": current}


async def raw_search(keyword: str, page: int = 1) -> dict:
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2",
            headers={
                "Authorization": f"Bearer {TIKHUB_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "keyword": keyword,
                "sortType": "_0",  # 综合
                "publishTime": "_180",
                "duration": "_0",
                "page": page,
                "searchId": "",
            },
        )
        r.raise_for_status()
        return r.json()


def walk_aweme_list(data):
    if not data:
        return []
    root = data.get("data") or data
    aweme_list = root.get("aweme_list") or []
    if aweme_list:
        return aweme_list
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
    print(f"  final = {resolved.get('final_url')[:150]}")

    if not aweme_id:
        print("FAIL: couldn't extract aweme_id")
        return

    # 多关键词搜索 pool 里找
    pool = {}
    for kw in SEARCH_KWS:
        for page in (1, 2):
            try:
                raw = await raw_search(kw, page)
                for a in walk_aweme_list(raw):
                    aid = str(a.get("aweme_id") or a.get("id") or "")
                    if aid:
                        pool[aid] = a
                print(f"  [search {kw} p{page}] pool={len(pool)}")
                if aweme_id in pool:
                    print(f"  HIT at [{kw} p{page}]")
                    break
            except Exception as e:
                print(f"  [search {kw} p{page}] err: {e}")
        if aweme_id in pool:
            break

    if aweme_id not in pool:
        print(f"FAIL: aweme_id {aweme_id} not found in pool of {len(pool)}")
        # save partial data for debug
        out = ROOT / "scripts" / "resolved_target.json"
        out.write_text(json.dumps({
            "resolved": resolved,
            "pool_size": len(pool),
            "pool_ids": list(pool.keys())[:30],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    info = extract_play_url(pool[aweme_id])
    dur_sec = round(info["duration_ms"] / 1000, 1) if info.get("duration_ms") else None
    print(f"\n[{info['author']}] dur={dur_sec}s, digg={info['stats']['digg']}")
    print(f"desc: {(info['desc'] or '')[:200]}")
    print(f"play_url: {(info['play_url'] or '')[:150]}")

    out = ROOT / "scripts" / "resolved_target.json"
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
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
