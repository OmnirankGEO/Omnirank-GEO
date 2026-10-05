"""抖音「被采纳内容」语料采集 · 灌 `douyin_adopted_corpus`

选题蒸馏器的 few-shot 来源。**离线运维脚本**,不在任何请求链上。

为什么必须单独采一遍(而不是直接查 geo_research_source_signals):
  那张表里没有形态字段,而 Review 硬要求「few-shot 优先同行业**图文帖**」。
  生产实测 1,677 条被采纳抖音条目 **100% 都是 `/share/video/{id}` 路径** ——
  URL 对图文/视频零判别力,只能问抖音(TIKHUB `aweme_detail`)。

用法(在**生产容器内**跑,它要连生产库):
    docker exec -i omnirank-blue python scripts/research/douyin_corpus_ingest.py \
        --limit 400 --concurrency 5

成本:TIKHUB 约 ¥0.0072/条。400 条 ≈ ¥2.9。
幂等:同一 aweme_id 重复采集走 upsert 覆盖,可反复跑。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

AWEME_ID_RE = re.compile(r"/share/(?:video|note)/([0-9]+)")
TIKHUB_ONE_VIDEO = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video"
IMAGE_POST_AWEME_TYPE = 68


def load_candidates(limit: int) -> List[Dict[str, str]]:
    """从飞轮信号表取「被采纳」的抖音条目。已在库里的跳过(省钱)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT ON (s.source_url)
                      s.source_url, s.industry_key
                 FROM geo_research_source_signals s
                WHERE s.domain = 'iesdouyin.com'
                  AND s.signal_tier = 'answer_adopted'
                  AND s.source_url LIKE '%%/share/video/%%'
                ORDER BY s.source_url, s.observed_at DESC
                LIMIT %s""",
            (max(1, int(limit)) * 3,),   # 多取一些,去掉已采过的还够
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT aweme_id FROM douyin_adopted_corpus")
        known = {str(dict(r)["aweme_id"]) for r in cur.fetchall()}
    finally:
        conn.close()

    out: List[Dict[str, str]] = []
    for r in rows:
        m = AWEME_ID_RE.search(str(r.get("source_url") or ""))
        if not m:
            continue
        aid = m.group(1)
        if aid in known:
            continue
        out.append({"aweme_id": aid,
                    "industry_key": str(r.get("industry_key") or "general"),
                    "source_url": str(r.get("source_url") or "")})
        if len(out) >= limit:
            break
    return out


async def fetch_one(client, sem, row: Dict[str, str], api_key: str) -> Dict[str, Any]:
    async with sem:
        for attempt in range(3):
            try:
                resp = await client.get(
                    TIKHUB_ONE_VIDEO,
                    params={"aweme_id": row["aweme_id"]},
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=45.0,
                )
                if resp.status_code == 200:
                    detail = (resp.json().get("data") or {}).get("aweme_detail") or {}
                    if not detail:
                        return {**row, "skip": "empty_detail"}
                    images = detail.get("images") or []
                    aweme_type = detail.get("aweme_type")
                    stats = detail.get("statistics") or {}
                    return {
                        **row,
                        "aweme_type": aweme_type,
                        "is_image_post": bool(images) or aweme_type == IMAGE_POST_AWEME_TYPE,
                        "image_count": len(images),
                        "caption": detail.get("desc") or "",
                        "hashtags": [t.get("hashtag_name")
                                     for t in (detail.get("text_extra") or [])
                                     if t.get("hashtag_name")],
                        "digg_count": stats.get("digg_count"),
                    }
                if resp.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return {**row, "skip": f"http_{resp.status_code}"}
            except Exception as exc:  # noqa: BLE001 - 单条失败不中断全批
                if attempt == 2:
                    return {**row, "skip": f"exc_{type(exc).__name__}"}
                await asyncio.sleep(2 * (attempt + 1))
        return {**row, "skip": "exhausted"}


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    from db.douyin_corpus_db import upsert_corpus_row

    api_key = os.getenv("TIKHUB_API_KEY")
    if not api_key:
        print("[ingest] TIKHUB_API_KEY 未设置", file=sys.stderr)
        return 2

    rows = load_candidates(args.limit)
    if not rows:
        print("[ingest] 没有待采集的条目(可能都采过了)")
        return 0
    print(f"[ingest] 待采集 {len(rows)} 条")

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(fetch_one(client, sem, r, api_key) for r in rows))

    saved = 0
    skipped = 0
    for r in results:
        if r.get("skip") or not str(r.get("caption") or "").strip():
            skipped += 1
            continue
        upsert_corpus_row(
            aweme_id=r["aweme_id"], industry_key=r["industry_key"],
            aweme_type=r.get("aweme_type"),
            is_image_post=bool(r.get("is_image_post")),
            image_count=int(r.get("image_count") or 0),
            caption=str(r.get("caption") or ""),
            hashtags=list(r.get("hashtags") or []),
            digg_count=r.get("digg_count"),
            source_url=r.get("source_url") or "")
        saved += 1

    from db.douyin_corpus_db import corpus_stats
    print(f"[ingest] 入库 {saved} 条, 跳过 {skipped} 条")
    print(f"[ingest] 现有语料: {corpus_stats()}")
    # 不用 CNY 符号:Windows 控制台默认 GBK,U+00A5 直接 UnicodeEncodeError(实测)
    print(f"[ingest] TIKHUB 调用 {len(rows)} 次, 约 CNY {len(rows) * 0.0072:.2f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=5)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
