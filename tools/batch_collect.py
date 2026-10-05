"""

5
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from tools.tikhub import search_douyin_videos, search_xiaohongshu_notes
from utils.state_manager import retry_async, parallel_gather


def calc_video_score(video: dict) -> float:
    """(like + collect*2 + share*3)"""
    return (
        video.get("like_count", 0)
        + video.get("collect_count", 0) * 2
        + video.get("share_count", 0) * 3
    )


def calc_note_score(note: dict) -> float:
    """(like + collect*2)"""
    return note.get("like_count", 0) + note.get("collect_count", 0) * 2


def is_within_half_year(timestamp: Any) -> bool:
    """"""
    if not timestamp:
        return True  #

    try:
        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp)
        elif isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        else:
            return True

        return dt > datetime.now() - timedelta(days=180)
    except:
        return True


async def batch_search_douyin(keywords: list[str], page: int = 1) -> dict:
    """
    5

    Args:
        keywords: 5
        page:

    Returns:
         {videos: list, total_raw: int, keywords: list}
    """
    print(f"   : {len(keywords)} ...")

    #
    tasks = [
        retry_async(search_douyin_videos, keyword=kw, page=page) for kw in keywords
    ]

    #
    results = await parallel_gather(tasks)

    #
    all_videos = []
    total_raw = 0

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"      '{keywords[i]}' : {result}")
            continue

        try:
            data = json.loads(result.content[0]["text"])
            # TikHub  "list"  "videos"
            videos = data.get("list", [])
            total_raw += data.get("total_raw", len(videos))

            #
            for v in videos:
                v["_search_keyword"] = keywords[i]
                #
                if "stats" in v:
                    v["like_count"] = v["stats"].get("digg", 0) or 0
                    v["share_count"] = v["stats"].get("share", 0) or 0
                    v["collect_count"] = 0  #
                v["aweme_id"] = v.get("id")

            all_videos.extend(videos)
        except Exception as e:
            print(f"     : {e}")

    #  id
    seen_ids = set()
    unique_videos = []
    for v in all_videos:
        vid = v.get("id") or v.get("aweme_id") or hash(str(v.get("desc", "")))
        if vid and vid not in seen_ids:
            seen_ids.add(vid)
            unique_videos.append(v)

    #
    recent_videos = [
        v for v in unique_videos if is_within_half_year(v.get("create_time"))
    ]

    #
    recent_videos.sort(key=calc_video_score, reverse=True)

    print(f"   :  {total_raw}  {len(unique_videos)}  {len(recent_videos)} ")

    return {
        "videos": recent_videos,
        "top20": recent_videos[:20],
        "total_raw": total_raw,
        "unique_count": len(unique_videos),
        "recent_count": len(recent_videos),
        "keywords": keywords,
    }


async def batch_search_xiaohongshu(keywords: list[str], page: int = 1) -> dict:
    """
    5

    Args:
        keywords: 5
        page:

    Returns:
         {notes: list, total_raw: int, keywords: list}
    """
    print(f"   : {len(keywords)} ...")

    #
    tasks = [
        retry_async(search_xiaohongshu_notes, keyword=kw, page=page) for kw in keywords
    ]

    #
    results = await parallel_gather(tasks)

    #
    all_notes = []
    total_raw = 0

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"      '{keywords[i]}' : {result}")
            continue

        try:
            data = json.loads(result.content[0]["text"])
            # TikHub  "list"  "notes"
            notes = data.get("list", [])
            total_raw += data.get("total_raw", len(notes))

            #
            for n in notes:
                n["_search_keyword"] = keywords[i]
                n["note_id"] = n.get("id")
                #
                if "stats" in n:
                    n["like_count"] = n["stats"].get("likes", 0) or 0
                    n["collect_count"] = n["stats"].get("collects", 0) or 0

            all_notes.extend(notes)
        except Exception as e:
            print(f"     : {e}")

    #  id
    seen_ids = set()
    unique_notes = []
    for n in all_notes:
        nid = n.get("id") or n.get("note_id") or hash(str(n.get("title", "")))
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            unique_notes.append(n)

    #
    recent_notes = [n for n in unique_notes if is_within_half_year(n.get("time"))]

    #
    recent_notes.sort(key=calc_note_score, reverse=True)

    print(f"   :  {total_raw}  {len(unique_notes)}  {len(recent_notes)} ")

    return {
        "notes": recent_notes,
        "top20": recent_notes[:20],
        "total_raw": total_raw,
        "unique_count": len(unique_notes),
        "recent_count": len(recent_notes),
        "keywords": keywords,
    }


async def batch_collect_all(keywords: list[str]) -> dict:
    """


    Args:
        keywords: 5

    Returns:
        {douyin: {...}, xiaohongshu: {...}}
    """
    print("  ()...")

    #
    douyin_task = batch_search_douyin(keywords)
    xhs_task = batch_search_xiaohongshu(keywords)

    douyin_data, xhs_data = await asyncio.gather(
        douyin_task, xhs_task, return_exceptions=True
    )

    #
    if isinstance(douyin_data, Exception):
        print(f"   : {douyin_data}")
        douyin_data = {"videos": [], "total_raw": 0, "error": str(douyin_data)}

    if isinstance(xhs_data, Exception):
        print(f"   : {xhs_data}")
        xhs_data = {"notes": [], "total_raw": 0, "error": str(xhs_data)}

    total_videos = len(douyin_data.get("videos", []))
    total_notes = len(xhs_data.get("notes", []))

    print(f" :  {total_videos} ,  {total_notes} ")

    return {
        "douyin": douyin_data,
        "xiaohongshu": xhs_data,
        "keywords": keywords,
        "summary": {
            "total_videos": total_videos,
            "total_notes": total_notes,
            "total_items": total_videos + total_notes,
        },
    }


async def filter_relevant_content(
    items: list,
    brand_name: str,
    industry: str,
    platform: str = "douyin",
    top_n: int = 20,
) -> list:
    """
    使用LLM对社媒内容进行相关性过滤，排除与品牌/行业无关的内容。

    解决问题：品牌名含通用词（如"一路顺风"）时，搜索结果中会混入
    大量无关内容（如节日祝福、成语引用、不相关行业的内容）。

    Args:
        items: 社媒内容列表（视频或笔记）
        brand_name: 品牌名称
        industry: 行业描述
        platform: 平台名称 (douyin/xiaohongshu)
        top_n: 最终保留的最大数量

    Returns:
        过滤后的内容列表
    """
    import os
    import httpx

    if not items:
        return []

    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if not dashscope_key:
        print(f"  ⚠️ [相关性过滤] 无DASHSCOPE_API_KEY，跳过过滤")
        return items[:top_n]

    relevant = []
    checked = 0
    # 检查2倍数量以保证过滤后仍有足够内容
    candidates = items[: top_n * 3]

    print(
        f"  🔍 [相关性过滤] 检查{len(candidates)}条{platform}内容与「{brand_name}」({industry})的相关性..."
    )

    for item in candidates:
        if len(relevant) >= top_n:
            break

        # 提取内容摘要
        if platform == "douyin":
            desc = item.get("desc", "") or item.get("title", "")
            author = (
                item.get("author", {}).get("nickname", "")
                if isinstance(item.get("author"), dict)
                else ""
            )
        else:
            desc = item.get("title", "") or item.get("desc", "")
            user = item.get("user", {}) or item.get("author", {})
            author = user.get("nickname", "") if isinstance(user, dict) else ""

        desc_short = desc[:150] if desc else ""
        if not desc_short:
            relevant.append(item)  # 无描述的保留
            continue

        prompt = f"""判断以下社媒内容是否与"{brand_name}"（行业：{industry}）相关。

内容：{desc_short}
作者：{author}

YES = 内容与{industry}行业直接相关（讨论相关产品/服务/案例/评测/行业话题）
NO = 内容与{industry}无关（如节日祝福、成语引用、不相关行业、娱乐短剧等）

只回答YES或NO。"""

        try:
            from tools.llm_call_tracker import llm_track, usage_from_response_payload

            async with httpx.AsyncClient(timeout=10.0) as client:
                async with llm_track(
                    "social_relevance_filter",
                    "dashscope",
                    model="qwen-turbo",
                ) as tracker:
                    response = await client.post(
                        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {dashscope_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "qwen-turbo",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 5,
                            "temperature": 0,
                        },
                    )
                    if response.status_code == 200:
                        data_for_usage = response.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
                        tracker.record(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            success=True,
                        )
                    else:
                        tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
            checked += 1
            if response.status_code == 200:
                data = response.json()
                answer = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                    .upper()
                )
                if answer.startswith("YES"):
                    relevant.append(item)
                # NO → 丢弃
            else:
                relevant.append(item)  # API异常时保留
        except Exception as e:
            relevant.append(item)  # 网络异常时保留

    filtered_count = checked - len(relevant)
    print(
        f"  ✅ [相关性过滤] 检查{checked}条，保留{len(relevant)}条，过滤{filtered_count}条无关内容"
    )

    return relevant
