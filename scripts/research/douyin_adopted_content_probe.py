"""§6 深挖 · 被采纳抖音条目的内容形态分类器(视频 vs 图文帖)

工单 WORKORDER_GEO_VIDEO_PIPELINE_V1_2026-08-01.md §6 的第一件事:
**先测 视频 / 图文帖 的占比** —— 这个分布直接决定 Phase 2 视频档要不要开。

判据(抖音 aweme_detail 真实字段,不靠 URL 路径猜):
  - `images` 非空          → 图文帖(image_post)
  - `aweme_type` == 68     → 图文帖(抖音图文的官方类型码)
  - 其余且有 video.play_addr → 视频(video)

🔴 为什么不能用 URL 路径判:生产实测 1,677 条被采纳抖音条目 **100% 都是
   `/share/video/{aweme_id}`**,抖音分享链对图文帖不换路径 —— 路径零判别力。

用法:
    python scripts/research/douyin_adopted_content_probe.py \
        --sample .tmp_ro/sample_100.tsv --out .tmp_ro/probe_result.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

AWEME_ID_RE = re.compile(r"/share/(?:video|note)/(\d+)")

TIKHUB_ONE_VIDEO = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video"

# 抖音图文帖的 aweme_type;视频为 0/4/61 等
IMAGE_POST_AWEME_TYPE = 68


def parse_sample(path: Path) -> List[Dict[str, str]]:
    """读取 §6 取样 TSV:source_url \t industry_key \t title \t adoption_rank"""
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("http"):
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        m = AWEME_ID_RE.search(url)
        if not m:
            continue
        aweme_id = m.group(1)
        if aweme_id in seen:
            continue
        seen.add(aweme_id)
        rows.append({
            "aweme_id": aweme_id,
            "source_url": url,
            "industry_key": parts[1].strip() if len(parts) > 1 else "",
            "title_from_signal": parts[2].strip() if len(parts) > 2 else "",
            "adoption_rank": parts[3].strip() if len(parts) > 3 else "",
        })
    return rows


def classify(detail: Dict[str, Any]) -> str:
    """按真实字段分类,不靠 URL。"""
    images = detail.get("images")
    if images:
        return "image_post"
    if detail.get("aweme_type") == IMAGE_POST_AWEME_TYPE:
        return "image_post"
    video = detail.get("video") or {}
    if (video.get("play_addr") or {}).get("url_list"):
        return "video"
    return "unknown"


def _duration_seconds(detail: Dict[str, Any]) -> float:
    raw = detail.get("duration") or 0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # 抖音 duration 单位是毫秒
    return round(val / 1000.0, 1) if val > 1000 else round(val, 1)


def summarize(detail: Dict[str, Any], row: Dict[str, str]) -> Dict[str, Any]:
    images = detail.get("images") or []
    stats = detail.get("statistics") or {}
    author = detail.get("author") or {}
    return {
        **row,
        "kind": classify(detail),
        "aweme_type": detail.get("aweme_type"),
        "image_count": len(images),
        "duration_sec": _duration_seconds(detail),
        "desc": detail.get("desc") or "",
        "author_nickname": author.get("nickname") or "",
        "digg_count": stats.get("digg_count"),
        "comment_count": stats.get("comment_count"),
        "text_extra": [
            t.get("hashtag_name") for t in (detail.get("text_extra") or [])
            if t.get("hashtag_name")
        ],
        "image_urls": [
            ((img.get("url_list") or [None])[0]) for img in images
        ][:12],
        "music_play_url": (
            ((detail.get("music") or {}).get("play_url") or {}).get("url_list") or [None]
        )[0],
    }


async def fetch_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore,
    row: Dict[str, str], api_key: str,
) -> Dict[str, Any]:
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
                        return {**row, "kind": "no_detail", "error": "empty aweme_detail"}
                    return summarize(detail, row)
                if resp.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return {**row, "kind": "http_error", "error": f"HTTP {resp.status_code}"}
            except Exception as exc:  # noqa: BLE001 - 研究脚本,单条失败不中断全批
                if attempt == 2:
                    return {**row, "kind": "exception", "error": str(exc)[:200]}
                await asyncio.sleep(2 * (attempt + 1))
        return {**row, "kind": "exhausted", "error": "retries exhausted"}


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.getenv("TIKHUB_API_KEY")
    if not api_key:
        print("🔴 TIKHUB_API_KEY 未设置", file=sys.stderr)
        return 2

    rows = parse_sample(Path(args.sample))
    if not rows:
        print("🔴 取样文件解析出 0 条", file=sys.stderr)
        return 2
    print(f"[probe] 样本 {len(rows)} 条(已按 aweme_id 去重)")

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(fetch_one(client, sem, r, api_key) for r in rows)
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    total_ok = sum(v for k, v in counts.items() if k in ("video", "image_post"))
    print("\n===== 形态分布(§6 第一问) =====")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:12s} {v:4d}")
    if total_ok:
        vid = counts.get("video", 0)
        img = counts.get("image_post", 0)
        print(f"\n  可判定 {total_ok} 条中: 视频 {vid} ({vid*100//total_ok}%) "
              f"/ 图文帖 {img} ({img*100//total_ok}%)")
    print(f"\n[probe] 明细已写入 {out}")
    # 🔴 别用 ¥ / ≈ 等字符:Windows 控制台默认 GBK,U+00A5 直接 UnicodeEncodeError(实测)
    print(f"[probe] TIKHUB 调用 {len(rows)} 次, 约 CNY {len(rows) * 0.0072:.2f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sample", required=True, help="§6 取样 TSV")
    p.add_argument("--out", required=True, help="输出 JSON 明细")
    p.add_argument("--concurrency", type=int, default=5)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
