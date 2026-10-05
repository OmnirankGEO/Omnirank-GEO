from __future__ import annotations

import logging
import re
from typing import Any, Optional

from services.video_link import (
    detect_platform,
    extract_bilibili_bv_id,
    extract_share_url,
    extract_video_id_douyin,
    extract_wechat_video_id,
    extract_xhs_note_id,
    fetch_bilibili_video_detail,
    fetch_video_detail_douyin,
    fetch_wechat_video_detail,
    fetch_xhs_note_detail,
)

logger = logging.getLogger("GEO-AgentLoop-Video")


async def parse_video(url: str, need_asr: bool = True, language: str = "") -> dict:
    """视频 URL 解析 —— 只取平台详情,不做改写(E0b · 2026-09-26 起)

    🔴 E0b 行为变化(Review 09-26 定 (b+)):
       旧版走社媒一键仿写模块的 analyze_video_link(= one_click_rewrite:取详情 → ASR → LLM 拆解 / 仿写;已随开源 E3 删除),
       现在只调 `services/video_link.py` 里上提的取数函数,**不做 ASR、不调 LLM**:
         · 四个平台(抖音 / 小红书 / B 站 / 视频号)返回标题、作者、互动数;小红书带首图作封面;
         · 文案:小红书 = 正文 desc · B 站 = 中文字幕(没有则简介)· 视频号 = 描述 · 抖音 = 空(原来是 ASR 转写);
         · 旧版这四项元数据其实一直是空的:改写流水线把它们放在 `original` 子字典里,
           下面的 _normalize_* 只读顶层 —— 旧版只有 transcript(ASR 文本)有值。
       `need_asr` / `language` 参数保留(调用方和工具定义还在传),本版不再做 ASR。
       `raw` 里的平台详情放在 `raw["detail"]` 下一层,**顶层不出现 duration 类字段**:
       chat_attachments 按 raw 顶层时长计 video_asr 分钟,旧版恒为 1 分钟,这里保持不变(计费口径另议)。

    返稳定契约(所有 key 必有 · 缺数据允许空):
      url:        str (原始 URL)
      platform:   str | None ("douyin" / "xiaohongshu" / "bilibili" / "wechat_channels" / "tiktok")
      title:      str
      author:     str
      thumbnail:  str (cover URL)
      transcript: str (字幕 / 文案)
      metrics:    dict {"likes": int, "views": int, "comments": int, "shares": int}
      raw:        dict ({"detail": 平台详情} · 兼容老代码)
      source:     str ("video_link")
      need_asr:   bool
      language:   str
    """
    platform = _normalize_platform(url)
    try:
        detail = await _fetch_detail(url)
    except Exception as exc:
        logger.warning(f"[VideoAdapter] fetch detail failed url={url}: {exc}")
        return _error_result(url, platform, need_asr, language, str(exc))
    if not isinstance(detail, dict) or detail.get("status") != "success":
        err = (detail or {}).get("error") if isinstance(detail, dict) else "invalid_detail"
        return _error_result(url, platform, need_asr, language, str(err or "fetch_failed"))

    raw_dict = _basic_raw(detail)
    return {
        "source": "video_link",
        "url": url,
        "platform": platform,
        "title": _normalize_title(raw_dict),
        "author": _normalize_author(raw_dict),
        "thumbnail": _normalize_thumbnail(raw_dict),
        "transcript": _normalize_transcript(raw_dict),
        "metrics": _normalize_metrics(raw_dict),
        # 2026-05-22 v1.3.3 老板 B 拍 · 小红书 web 链触发 unauth 防御 · 给 App deep link
        # 移动端 <a href="xhsdiscover://..."> 唤起 App · 桌面端浏览器无响应(改用 web link)
        # 平台支持矩阵:小红书 ✅ · 抖音 ✅ · B 站 ✅ · 视频号/TikTok 暂无标准 deep link
        "deep_link": _normalize_deep_link(url, _deep_link_ids(detail), platform),
        "raw": {"detail": detail},
        "need_asr": need_asr,
        "language": language,
        "status": "ok",
    }


def _error_result(url: str, platform: Optional[str], need_asr: bool, language: str, error: str) -> dict:
    return {
        "source": "video_link",
        "url": url,
        "platform": platform,
        "title": "",
        "author": "",
        "thumbnail": "",
        "transcript": "",
        "metrics": {"likes": 0, "views": 0, "comments": 0, "shares": 0},
        "deep_link": "",  # error path 也保留 key · 防 caller 假设字段不存在崩
        "raw": {},
        "need_asr": need_asr,
        "language": language,
        "status": "error",
        "error": str(error)[:200],
    }


async def _fetch_detail(url: str) -> dict:
    """按平台调上提后的取数函数(与旧 one_click_rewrite 的取数步骤同序同参),返回平台详情 dict。"""
    share_url = extract_share_url(url) or (url or "").strip()
    src = detect_platform(share_url)
    if src == "douyin":
        id_result = await extract_video_id_douyin(share_url)
        if id_result.get("status") != "success":
            return {"status": "error", "error": f"提取视频ID失败: {id_result.get('error')}"}
        return await fetch_video_detail_douyin(id_result.get("video_id"))
    if src == "xiaohongshu":
        id_result = await extract_xhs_note_id(share_url)
        if id_result.get("status") != "success":
            return {"status": "error", "error": f"提取笔记ID失败: {id_result.get('error')}"}
        return await fetch_xhs_note_detail(id_result["note_id"], share_text=id_result.get("share_text", ""))
    if src == "weixin":
        id_result = await extract_wechat_video_id(share_url)
        if id_result.get("status") != "success":
            return {"status": "error", "error": f"提取视频号 ID 失败: {id_result.get('error')}"}
        return await fetch_wechat_video_detail(id_result["video_id"], id_result.get("id_type", "exportId"))
    if src == "bilibili":
        bv = extract_bilibili_bv_id(share_url)
        if bv.get("status") != "success":
            return {"status": "error", "error": bv.get("error", "B 站 BV id 提取失败")}
        return await fetch_bilibili_video_detail(bv["bv_id"])
    return {"status": "error", "error": f"暂不支持平台: {src}"}


def _first_image(images: Any) -> str:
    for im in images or []:
        if isinstance(im, str) and im.strip():
            return im.strip()
        if isinstance(im, dict):
            u = _first_text(im.get("url"), im.get("url_default"), im.get("original"))
            if u:
                return u
    return ""


def _basic_raw(detail: dict) -> dict:
    """把各平台详情显式映射到 _normalize_* 认的顶层键(不靠猜键名)。"""
    stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
    return {
        "title": detail.get("title") or "",
        "author": detail.get("author") if isinstance(detail.get("author"), dict) else {},
        "cover": _first_image(detail.get("images")),
        "transcript": _first_text(detail.get("desc"), detail.get("subtitle_text"), detail.get("description")),
        "metrics": {
            "likes": _first_int(stats.get("digg"), stats.get("likes"), stats.get("like")),
            "views": _first_int(stats.get("play"), stats.get("view")),
            "comments": _first_int(stats.get("comment"), stats.get("comments"), stats.get("reply")),
            "shares": _first_int(stats.get("share"), stats.get("shares")),
        },
    }


def _deep_link_ids(detail: dict) -> dict:
    """deep link 用到的平台 id(键名沿用 _normalize_deep_link 原来认的 note_id / aweme_id / bvid)。"""
    return {
        "note_id": detail.get("note_id") or "",
        "aweme_id": detail.get("video_id") or "",
        "bvid": detail.get("bv_id") or "",
    }


def _normalize_platform(url: str) -> Optional[str]:
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return None
    if "douyin.com" in low or "iesdouyin.com" in low:
        return "douyin"
    if "xiaohongshu.com" in low or "xhslink.com" in low:
        return "xiaohongshu"
    if "bilibili.com" in low or "b23.tv" in low:
        return "bilibili"
    if "weixin.qq.com" in low and "finder" in low:
        return "wechat_channels"
    if "tiktok.com" in low:
        return "tiktok"
    return None


def _first_text(*candidates) -> str:
    """取第一个非空 string · 用于多 key 候选"""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _first_int(*candidates) -> int:
    for c in candidates:
        try:
            v = int(c)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0


def _normalize_title(raw: dict) -> str:
    return _first_text(
        raw.get("title"),
        raw.get("video_title"),
        raw.get("name"),
        raw.get("desc"),  # tikhub 抖音文案
        raw.get("description"),
    )


def _normalize_author(raw: dict) -> str:
    # nested author dict 优先
    author = raw.get("author")
    if isinstance(author, dict):
        nick = _first_text(author.get("nickname"), author.get("name"), author.get("unique_id"))
        if nick:
            return nick
    return _first_text(
        raw.get("nickname"),
        raw.get("author_name"),
        author if isinstance(author, str) else "",
        raw.get("user_name"),
    )


def _normalize_thumbnail(raw: dict) -> str:
    return _first_text(
        raw.get("cover"),
        raw.get("cover_url"),
        raw.get("thumbnail"),
        raw.get("pic"),
        raw.get("dynamic_cover"),
    )


def _normalize_transcript(raw: dict) -> str:
    return _first_text(
        raw.get("transcript"),
        raw.get("asr_text"),
        raw.get("subtitle"),
        raw.get("captions"),
    )


def _normalize_deep_link(url: str, raw: dict, platform: Optional[str]) -> str:
    """生成移动端 App deep link · 唤起原生 App 打开内容

    2026-05-22 老板拍 B · 小红书 web 链触发"App 内打开"防御页 · 给 deep link 唤起 App
    返空 str 表示不支持(视频号/TikTok 暂无标准 deep link · 前端 fallback web URL)

    Args:
        url: 用户原始 URL(www.xiaohongshu.com/explore/xxx · b23.tv/xxx 等)
        raw: tikhub 返的原始 dict(可能含 note_id / aweme_id / bvid)
        platform: 已 normalize 的平台名

    Returns:
        deep_link str · 形如 "xhsdiscover://item/xxx" · 失败返 ""
    """
    if not platform:
        return ""
    try:
        if platform == "xiaohongshu":
            # 小红书:xhsdiscover://item/{note_id}
            # note_id 优先取 raw.note_id · 否则从 url 正则提
            note_id = _first_text(
                raw.get("note_id") if isinstance(raw, dict) else "",
                _re_extract(url, r"/explore/([A-Za-z0-9]+)"),
                _re_extract(url, r"/discovery/item/([A-Za-z0-9]+)"),
            )
            if note_id:
                return f"xhsdiscover://item/{note_id}"

        elif platform == "douyin":
            # 抖音:snssdk1128://aweme/detail/{aweme_id}
            aweme_id = _first_text(
                raw.get("aweme_id") if isinstance(raw, dict) else "",
                raw.get("id") if isinstance(raw, dict) else "",
                _re_extract(url, r"/video/(\d+)"),
            )
            if aweme_id and str(aweme_id).isdigit():
                return f"snssdk1128://aweme/detail/{aweme_id}"

        elif platform == "bilibili":
            # B 站:bilibili://video/BV{bvid}
            bvid = _first_text(
                raw.get("bvid") if isinstance(raw, dict) else "",
                _re_extract(url, r"/video/(BV[A-Za-z0-9]+)"),
            )
            if bvid:
                return f"bilibili://video/{bvid}"

        # 视频号 / TikTok / 未知平台 · 暂无标准 deep link · 前端用 web URL fallback
        return ""
    except Exception as exc:
        logger.warning(f"[VideoAdapter] _normalize_deep_link failed url={url}: {exc}")
        return ""


def _re_extract(text: str, pattern: str) -> str:
    """正则提取 group 1 · 失败返空"""
    if not text:
        return ""
    try:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _normalize_metrics(raw: dict) -> dict:
    """统一互动数据 · 抖音/小红书/B 站字段不同 · 全部 fallback 到 0"""
    statistics = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
    metrics_src = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    return {
        "likes": _first_int(
            statistics.get("digg_count"),
            metrics_src.get("likes"),
            raw.get("likes"),
            raw.get("digg_count"),
            raw.get("liked_count"),
        ),
        "views": _first_int(
            statistics.get("play_count"),
            metrics_src.get("views"),
            metrics_src.get("play"),
            raw.get("views"),
            raw.get("play_count"),
            raw.get("view_count"),
        ),
        "comments": _first_int(
            statistics.get("comment_count"),
            metrics_src.get("comments"),
            raw.get("comments"),
            raw.get("comment_count"),
        ),
        "shares": _first_int(
            statistics.get("share_count"),
            metrics_src.get("shares"),
            raw.get("shares"),
            raw.get("share_count"),
        ),
    }
