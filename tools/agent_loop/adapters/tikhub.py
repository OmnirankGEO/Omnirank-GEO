from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from config.model_config import TIKHUB_CONFIG
from tools.tikhub.tikhub_tools import (
    get_user_info,
    search_douyin_videos,
    search_wechat_channels,
    search_xiaohongshu_notes,
)
from tools.tikhub_cost_tracking import tracked_tikhub_get


async def search_topics(
    industry: str,
    platform: str,
    market: str = "",
    language: str = "",
    limit: int = 10,
) -> dict:
    keyword = industry
    platform_key = str(platform or "").lower()
    if platform_key in ("douyin", "抖音"):
        raw = await _search_douyin_videos(keyword, limit=_safe_limit(limit))
    elif platform_key in ("douyin_hot", "抖音热榜", "hot", "hotspot", "热点", "热榜"):
        raw = await _search_douyin_hot()
        data = _tool_response_to_data(raw)
        items = _normalize_hot_items(data)[: _safe_limit(limit)]
        return {
            "source": "tikhub",
            "status": "success" if items else "empty",
            "platform": platform or "douyin_hot",
            "market": market,
            "language": language,
            "items": items,
            "research_table": _research_table(items),
            "insights": _insights(items),
            "summary_markdown": _summary_markdown("抖音热榜", platform or "douyin_hot", items),
            "raw_count": _raw_count(data, items),
        }
    elif platform_key in ("xiaohongshu", "xhs", "小红书"):
        raw = await search_xiaohongshu_notes(keyword, page=1)
    elif platform_key in ("wechat_channels", "shipinhao", "视频号"):
        raw = await search_wechat_channels(keyword)
    elif platform_key in ("tiktok", "tik tok"):
        raw = await _search_tiktok_videos(keyword, market=market, limit=_safe_limit(limit))
    elif platform_key in ("bilibili", "b站", "bili", "哔哩哔哩"):
        raw = await _search_bilibili_videos(keyword, limit=_safe_limit(limit))
    else:
        return {
            "source": "tikhub",
            "platform": platform,
            "market": market,
            "language": language,
            "status": "unsupported_platform",
            "items": [],
            "research_table": [],
            "insights": {
                "top_sample_count": 0,
                "note": "当前 TikHub adapter 暂未接入该平台搜索；请换抖音、小红书或视频号，或改走网页调研。",
            },
            "summary_markdown": "暂未获取到该平台的视频数据。",
        }
    data = _tool_response_to_data(raw)
    items = _normalize_items(_extract_items(data), platform=platform)[: _safe_limit(limit)]
    return {
        "source": "tikhub",
        "status": _status_from_data(data, items),
        "platform": platform,
        "market": market,
        "language": language,
        "items": items,
        "research_table": _research_table(items),
        "insights": _insights(items),
        "summary_markdown": _summary_markdown(industry, platform, items),
        "raw_count": _raw_count(data, items),
    }


async def get_account(platform: str, account_id: str = "", profile_url: str = "") -> dict:
    raw = await get_user_info(platform=platform, user_id=account_id or profile_url)
    return {"source": "tikhub", "platform": platform, "account_id": account_id, "raw": raw}


async def _search_tiktok_videos(keyword: str, *, market: str = "", limit: int = 10) -> dict:
    api_key = _tikhub_api_key()
    if not api_key:
        return {"status": "error", "error": "TIKHUB_API_KEY not configured"}
    count = _safe_limit(limit)
    params = {
        "keyword": keyword,
        "count": count,
        "offset": 0,
        "sort_type": 0,
        "publish_time": 0,
    }
    region = _market_to_tiktok_region(market)
    if region:
        params["region"] = region
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await tracked_tikhub_get(
            client,
            "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_video_search_result",
            caller="tikhub_tiktok_video_search",
            model="search",
            metadata={"count": count, "region": region or ""},
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if int(getattr(response, "status_code", 0) or 0) != 200:
        return {
            "status": "error",
            "error": f"TikHub TikTok search HTTP {getattr(response, 'status_code', '')}",
            "detail": str(getattr(response, "text", ""))[:300],
        }
    data = response.json()
    return data if isinstance(data, dict) else {"items": data}


async def _search_douyin_videos(keyword: str, *, limit: int = 10) -> dict:
    try:
        from tools.social.tikhub_mcp import douyin_search_videos as douyin_search_v2

        raw = await douyin_search_v2(keyword, count=_safe_limit(limit), sort_type="1", publish_time="180")
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return await search_douyin_videos(keyword, page=1)


async def _search_bilibili_videos(keyword: str, *, limit: int = 10) -> dict:
    api_key = _tikhub_api_key()
    if not api_key:
        return {"status": "error", "error": "TIKHUB_API_KEY not configured"}
    count = _safe_limit(limit)
    params = {
        "keyword": keyword,
        "search_type": "video",
        "page": 1,
        "page_size": count,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await tracked_tikhub_get(
            client,
            "https://api.tikhub.io/api/v1/bilibili/app/fetch_search_by_type",
            caller="tikhub_bilibili_video_search",
            model="search",
            metadata={"count": count},
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if int(getattr(response, "status_code", 0) or 0) != 200:
        return {
            "status": "error",
            "error": f"TikHub Bilibili search HTTP {getattr(response, 'status_code', '')}",
            "detail": str(getattr(response, "text", ""))[:300],
        }
    data = response.json()
    return data if isinstance(data, dict) else {"items": data}


async def _search_douyin_hot() -> dict:
    from tools.social.tikhub_mcp import douyin_get_hot_search

    raw = await douyin_get_hot_search()
    return raw if isinstance(raw, dict) else {"items": raw}


def _tikhub_api_key() -> str:
    return str(TIKHUB_CONFIG.get("api_key") or os.environ.get("TIKHUB_API_KEY") or "").strip()


def _market_to_tiktok_region(market: str) -> str:
    text = str(market or "").strip().lower()
    mapping = {
        "美国": "US",
        "usa": "US",
        "us": "US",
        "united states": "US",
        "德国": "DE",
        "germany": "DE",
        "de": "DE",
        "印尼": "ID",
        "印度尼西亚": "ID",
        "indonesia": "ID",
        "id": "ID",
        "加拿大": "CA",
        "canada": "CA",
        "英国": "GB",
        "uk": "GB",
        "gb": "GB",
    }
    return mapping.get(text, "")


def _tool_response_to_data(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, list) and content:
        text = ""
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                text = str(item.get("text") or "")
                break
            if isinstance(item, str):
                text = item
                break
        if text:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"items": parsed}
            except Exception:
                return {"status": "error", "text": text}
    return {"raw_text": str(raw)}


def _extract_items(raw: Any) -> list:
    if isinstance(raw, dict):
        direct = (
            raw.get("list")
            or raw.get("items")
            or raw.get("notes")
            or raw.get("aweme_list")
            or raw.get("item_list")
            or raw.get("video_list")
            or raw.get("videos")
            or raw.get("result")
            or raw.get("results")
            or raw.get("search_item_list")
            or raw.get("business_data")
        )
        if isinstance(direct, list) and direct:
            return direct
        for key in ("items", "list", "data"):
            value = raw.get(key)
            if isinstance(value, list) and value:
                return value
        nested = raw.get("data")
        if isinstance(nested, dict):
            for key in ("items", "list", "aweme_list", "notes", "item_list", "video_list", "videos", "result", "results", "search_item_list", "business_data"):
                value = nested.get(key)
                if isinstance(value, list) and value:
                    return value
            nested2 = nested.get("data")
            if isinstance(nested2, dict):
                for key in ("items", "list", "aweme_list", "notes", "item_list", "video_list", "videos", "result", "results", "search_item_list", "business_data"):
                    value = nested2.get(key)
                    if isinstance(value, list) and value:
                        return value
    content = getattr(raw, "content", None)
    if isinstance(content, list):
        return content
    return []


def _normalize_items(items: list[Any], *, platform: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            continue
        item = raw_item.get("note") if isinstance(raw_item.get("note"), dict) else raw_item
        if isinstance(item.get("data"), dict) and isinstance(item.get("data", {}).get("aweme_info"), dict):
            item = item.get("data", {}).get("aweme_info") or {}
        if isinstance(item.get("aweme_info"), dict):
            item = item.get("aweme_info") or {}
        if isinstance(item.get("av"), dict):
            av = item.get("av") or {}
            merged = dict(item)
            merged.update(
                {
                    "title": av.get("title") or item.get("title"),
                    "author": av.get("author") or item.get("author"),
                    "play": av.get("play") or item.get("play"),
                    "danmaku": av.get("danmaku") or item.get("danmaku"),
                    "duration": av.get("duration") or item.get("duration"),
                }
            )
            item = merged
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        if not stats and isinstance(item.get("statistics"), dict):
            stats = item.get("statistics") or {}
        author = item.get("author") or item.get("user") or {}
        if not isinstance(author, dict):
            author = {"nickname": str(author)}
        title = _first_text(
            item.get("title"),
            item.get("display_title"),
            item.get("desc"),
            item.get("text"),
            item.get("content"),
        )
        title = _compact_text(_strip_html(title), 180)
        if not title:
            continue
        likes = _first_int(
            stats.get("digg"),
            stats.get("digg_count"),
            stats.get("likes"),
            stats.get("like_count"),
            item.get("like_count"),
            item.get("liked_count"),
            item.get("digg_count"),
            item.get("likes"),
            item.get("like"),
        )
        comments = _first_int(
            stats.get("comment"),
            stats.get("comment_count"),
            stats.get("comments"),
            item.get("comment_count"),
            item.get("comments_count"),
            item.get("comments"),
            item.get("review"),
            item.get("danmaku"),
        )
        shares = _first_int(
            stats.get("share"),
            stats.get("share_count"),
            stats.get("shares"),
            item.get("share_count"),
            item.get("shared_count"),
        )
        collects = _first_int(
            stats.get("collects"),
            stats.get("favorite"),
            stats.get("favorite_count"),
            item.get("collect_count"),
            item.get("collected_count"),
            item.get("favorites"),
            item.get("favorite"),
            item.get("favorite_count"),
        )
        views = _first_int(
            stats.get("play"),
            stats.get("play_count"),
            stats.get("view"),
            stats.get("view_count"),
            item.get("play"),
            item.get("play_count"),
            item.get("view"),
            item.get("view_count"),
        )
        normalized.append(
            {
                "rank": index,
                "id": item.get("id") or item.get("note_id") or item.get("aweme_id") or item.get("bvid"),
                "title": title,
                "description": _first_text(item.get("desc"), item.get("text"), item.get("content")),
                "author": author.get("nickname") or author.get("name") or "",
                "platform": platform,
                "url": _video_url(item),
                "metrics": {
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "collects": collects,
                    "engagement_score": likes + comments * 3 + shares * 4 + collects * 2 + views // 10,
                },
                "signal_hint": _signal_hint(title),
            }
        )
    normalized.sort(key=lambda item: int(item.get("metrics", {}).get("engagement_score") or 0), reverse=True)
    for index, item in enumerate(normalized, start=1):
        item["rank"] = index
    return normalized


def _normalize_hot_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = data.get("trending") or data.get("data") or data.get("items") or data.get("word_list") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("word_list") or raw_items.get("list") or []
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return normalized
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            title = _first_text(item.get("word"), item.get("sentence"), item.get("title"), item.get("keyword"))
            hot_value = _first_int(item.get("hot_value"), item.get("hot"), item.get("score"))
            rank = index
        else:
            title = str(item or "").strip()
            hot_value = 0
            rank = index
        if not title:
            continue
        normalized.append(
            {
                "rank": rank,
                "id": f"hot-{rank}",
                "title": title,
                "description": title,
                "author": "抖音热榜",
                "platform": "douyin_hot",
                "url": "",
                "metrics": {
                    "hot_value": hot_value,
                    "hot_rank": rank,
                    "views": hot_value,
                    "likes": 0,
                    "comments": 0,
                    "shares": 0,
                    "collects": 0,
                    "engagement_score": hot_value,
                },
                "signal_hint": "平台热点/可借势话题",
            }
        )
    normalized.sort(key=lambda item: int(item.get("rank") or 9999))
    return normalized


def _research_table(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for item in items:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        table.append(
            {
                "排名": item.get("rank"),
                "视频/笔记": item.get("title"),
                "作者": item.get("author") or "未知",
                "互动数据": _format_metrics(metrics),
                "可观察信号": item.get("signal_hint") or "待结合客户资料判断",
                "链接": item.get("url") or "",
            }
        )
    return table


def _format_metrics(metrics: dict[str, Any]) -> str:
    parts: list[str] = []
    hot_value = int(metrics.get("hot_value") or 0)
    hot_rank = int(metrics.get("hot_rank") or 0)
    views = int(metrics.get("views") or 0)
    likes = int(metrics.get("likes") or 0)
    comments = int(metrics.get("comments") or 0)
    collects_shares = int(metrics.get("collects") or 0) + int(metrics.get("shares") or 0)
    if hot_value:
        parts.append(f"热度 {hot_value}")
    elif hot_rank:
        parts.append(f"热榜排名 {hot_rank}")
    elif views:
        parts.append(f"播 {views}")
    parts.append(f"赞 {likes}")
    parts.append(f"评 {comments}")
    parts.append(f"藏转 {collects_shares}")
    return " / ".join(parts)


def _insights(items: list[dict[str, Any]]) -> dict[str, Any]:
    top_titles = [str(item.get("title") or "") for item in items[:5] if item.get("title")]
    signals = []
    for item in items:
        signal = str(item.get("signal_hint") or "").strip()
        if signal and signal not in signals:
            signals.append(signal)
        if len(signals) >= 4:
            break
    return {
        "top_sample_count": len(items),
        "top_titles": top_titles,
        "observed_signals": signals,
        "quality_note": "这是平台公开样本整理，不等于最终选题结论；写稿前应结合客户卖点、目标人群和禁忌表达再判断。",
    }


def _summary_markdown(industry: str, platform: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return f"未从 {platform} 获取到「{industry}」的有效视频样本。"
    lines = [
        f"### {platform}「{industry}」视频样本整理",
        "",
        "| 排名 | 视频/笔记 | 作者 | 互动数据 | 可观察信号 |",
        "|---:|---|---|---|---|",
    ]
    for row in _research_table(items[:8]):
        # 2026-05-20 老板 + Codex Phase 1 · 数据层带链接 · 防 LLM 偷懒只总结标题
        title_text = _escape_pipe(str(row.get("视频/笔记") or ""))
        url = str(row.get("链接") or "").strip()
        if url and url.startswith(("http://", "https://")):
            title = f"[{title_text}]({url})"
        else:
            title = title_text
        author = _escape_pipe(str(row.get("作者") or "未知"))
        metrics = _escape_pipe(str(row.get("互动数据") or ""))
        signal = _escape_pipe(str(row.get("可观察信号") or ""))
        lines.append(f"| {row.get('排名')} | {title} | {author} | {metrics} | {signal} |")
    lines.extend(
        [
            "",
            "整理建议：先看高互动样本共同触发了什么情绪/疑问，再结合客户真实资料决定是否写稿；缺少事实依据时先追问，不要直接编案例。",
        ]
    )
    return "\n".join(lines)


def _safe_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), 20))
    except Exception:
        return 10


def _status_from_data(data: dict, items: list[dict[str, Any]]) -> str:
    if data.get("error") or data.get("status") == "error":
        return "error"
    if items:
        return "success"
    return str(data.get("status") or "empty")


def _raw_count(data: dict, items: list[dict[str, Any]]) -> int:
    for key in ("count", "total_raw", "total"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    return len(items)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _compact_text(text: str, max_chars: int = 180) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "…"


def _video_url(item: dict[str, Any]) -> str:
    explicit = item.get("url") or item.get("share_url") or item.get("arcurl")
    if explicit:
        text = str(explicit)
        if text.startswith("http://") or text.startswith("https://"):
            return text
    bvid = item.get("bvid")
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    note_id = item.get("note_id")
    if note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    param = item.get("param")
    if param:
        return f"https://www.bilibili.com/video/av{param}"
    return ""


def _first_int(*values: Any) -> int:
    for value in values:
        parsed = _to_int(value)
        if parsed:
            return parsed
    return 0


def _to_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10000
        text = text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except Exception:
        return 0


def _signal_hint(title: str) -> str:
    text = title or ""
    if any(token in text for token in ("价格", "报价", "多少钱", "便宜", "贵", "避坑")):
        return "价格/决策疑虑"
    if any(token in text for token in ("对比", "前后", "案例", "真实", "体验")):
        return "真实案例/前后对比"
    if any(token in text for token in ("步骤", "流程", "怎么", "如何", "教程")):
        return "流程解释/教程"
    if any(token in text for token in ("测评", "推荐", "榜单", "排名")):
        return "测评推荐/榜单"
    return "高互动话题样本"


def _escape_pipe(text: str) -> str:
    return text.replace("|", "｜").replace("\n", " ").strip()
