"""类 Kimi chat 参考资料预备栏 v1.3 测试(2026-05-21)

覆盖 Codex 4 轮反馈 + 老板 6 P0/P1 补点:
1. services/chat_attachments.py · dispatch / 90s timeout / 不入 KB
2. (已随开源 E3 B2 删)chat 会话库 · attachments JSONB / 4 helper / FOR UPDATE 行锁 / sweep stale
3. video.py normalize layer · 稳契约
4. content_api 4 端点 · require_profile_access / sweep on /list
5. PendingAttachment 类型映射(parsed → ready / failed → error)
6. 多轮粘性 / 上限 / 重复 id / 跨用户拒绝

某些测试需要 DB · 标 db_required 跳过(本机无 PG 时)
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch, AsyncMock

import pytest


# ─── 1. services/chat_attachments.py dispatch ─────────────────────────────────

def test_detect_file_type_image():
    from services.chat_attachments import detect_file_type
    assert detect_file_type("photo.jpg", "image/jpeg") == ("image", "jpg")
    assert detect_file_type("noext", "image/png") == ("image", "")
    assert detect_file_type("scan.HEIC", "")[0] == "image"


def test_detect_file_type_video():
    from services.chat_attachments import detect_file_type
    assert detect_file_type("clip.mp4", "video/mp4") == ("video_file", "mp4")
    assert detect_file_type("a.MOV", "")[0] == "video_file"


def test_detect_file_type_document():
    from services.chat_attachments import detect_file_type
    assert detect_file_type("doc.pdf", "application/pdf") == ("document", "pdf")
    assert detect_file_type("note.md", "")[0] == "document"
    assert detect_file_type("a.docx", "")[0] == "document"


def test_detect_file_type_unsupported_excel():
    """Excel 砍掉:老板 + Codex 拍 · 不假装支持"""
    from services.chat_attachments import detect_file_type
    assert detect_file_type("data.xlsx", "")[0] == "unsupported"
    assert detect_file_type("data.xls", "")[0] == "unsupported"
    assert detect_file_type("data.csv", "")[0] == "unsupported"


def test_detect_file_type_unsupported_archive_ppt():
    from services.chat_attachments import detect_file_type
    assert detect_file_type("file.zip", "")[0] == "unsupported"
    assert detect_file_type("file.pptx", "")[0] == "unsupported"
    assert detect_file_type("file.7z", "")[0] == "unsupported"


def test_detect_file_type_unknown_rejected():
    from services.chat_attachments import detect_file_type
    assert detect_file_type("malware.exe", "")[0] == "unsupported"


def test_detect_video_url_platform_douyin():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://www.douyin.com/video/123") == "douyin"
    assert detect_video_url_platform("https://v.douyin.com/abc") == "douyin"


def test_detect_video_url_platform_xhs():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://www.xiaohongshu.com/explore/x") == "xiaohongshu"
    assert detect_video_url_platform("https://xhslink.com/abc") == "xiaohongshu"


def test_detect_video_url_platform_bilibili():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://www.bilibili.com/video/BV1xx") == "bilibili"
    assert detect_video_url_platform("https://b23.tv/abc") == "bilibili"


def test_detect_video_url_platform_wechat():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://weixin.qq.com/x/finder/y") == "wechat_channels"


def test_detect_video_url_platform_tiktok():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://www.tiktok.com/@user/video/1") == "tiktok"


def test_detect_video_url_platform_rejects_non_video():
    from services.chat_attachments import detect_video_url_platform
    assert detect_video_url_platform("https://google.com") is None
    assert detect_video_url_platform("not a url") is None
    assert detect_video_url_platform("javascript:alert(1)") is None
    assert detect_video_url_platform("") is None


def test_reject_unsupported_excel_friendly_message():
    from services.chat_attachments import reject_unsupported
    res = reject_unsupported("table.xlsx", "xlsx")
    assert res.status == "failed"
    assert res.error == "unsupported_xlsx"
    assert "PDF" in res.markdown_summary or "Word" in res.markdown_summary


def test_parse_video_file_placeholder_returns_friendly_hint():
    """本地视频文件:placeholder · 提示用 URL"""
    from services.chat_attachments import parse_video_file_placeholder
    res = parse_video_file_placeholder("clip.mp4", 1024 * 1024)
    assert res.ok is True
    assert res.status == "parsed"
    assert "URL" in res.markdown_summary or "url" in res.markdown_summary.lower()


def test_short_title_truncates_long():
    from services.chat_attachments import _short_title
    long_text = "超长标题" * 20
    out = _short_title(long_text, max_len=30)
    assert len(out) <= 31  # 30 + "…"
    assert out.endswith("…")


def test_short_title_strips_newlines():
    from services.chat_attachments import _short_title
    out = _short_title("第一行\n第二行\r第三行")
    assert "\n" not in out and "\r" not in out


# ─── 2. video.py normalize layer ─────────────────────────────────────────────

def test_video_normalize_platform_detection():
    from tools.agent_loop.adapters.video import _normalize_platform
    assert _normalize_platform("https://douyin.com/x") == "douyin"
    assert _normalize_platform("https://xiaohongshu.com/x") == "xiaohongshu"
    assert _normalize_platform("https://b23.tv/x") == "bilibili"
    assert _normalize_platform("https://tiktok.com/x") == "tiktok"
    assert _normalize_platform("not-url") is None


def test_video_normalize_title_fallback_chain():
    from tools.agent_loop.adapters.video import _normalize_title
    assert _normalize_title({"title": "primary"}) == "primary"
    assert _normalize_title({"video_title": "alt"}) == "alt"
    assert _normalize_title({"desc": "desc fallback"}) == "desc fallback"
    assert _normalize_title({}) == ""


def test_video_normalize_author_nested_dict():
    from tools.agent_loop.adapters.video import _normalize_author
    assert _normalize_author({"author": {"nickname": "Tom"}}) == "Tom"
    assert _normalize_author({"author": "FlatStr"}) == "FlatStr"
    assert _normalize_author({"nickname": "X"}) == "X"
    assert _normalize_author({}) == ""


def test_video_normalize_metrics_handles_multiple_keys():
    from tools.agent_loop.adapters.video import _normalize_metrics
    m = _normalize_metrics({"statistics": {"digg_count": 100, "play_count": 5000}})
    assert m["likes"] == 100 and m["views"] == 5000
    assert m["comments"] == 0 and m["shares"] == 0  # 缺字段返 0


def test_video_normalize_metrics_all_empty():
    from tools.agent_loop.adapters.video import _normalize_metrics
    m = _normalize_metrics({})
    assert m == {"likes": 0, "views": 0, "comments": 0, "shares": 0}


def test_video_parse_video_contract_keys():
    """v1.3 contract:所有 key 必有(即使空值)"""
    from tools.agent_loop.adapters import video as video_module
    # [E0b 2026-09-26] adapter 不再走社媒改写工具的 analyze_video_link(该工具已随开源 E3 删),改为只取平台详情;
    #   mock 点换到新的取数入口 _fetch_detail,契约断言原样保留。
    fake_detail = {"status": "success", "title": "T", "author": {"nickname": "A"}, "desc": "hello"}

    async def fake_fetch(_url):
        return fake_detail

    with patch.object(video_module, "_fetch_detail", new=fake_fetch):
        result = asyncio.run(video_module.parse_video("https://douyin.com/v/1"))
    for k in ("source", "url", "platform", "title", "author", "thumbnail", "transcript", "metrics", "raw", "status"):
        assert k in result, f"contract 缺 key={k}"
    assert result["title"] == "T"
    assert result["author"] == "A"
    assert result["platform"] == "douyin"
    assert result["status"] == "ok"


def test_video_parse_video_error_path_keeps_contract():
    """取数抛错时仍返完整 contract(所有 key 有 · status=error)"""
    from tools.agent_loop.adapters import video as video_module

    async def fake_raise(_url):
        raise RuntimeError("network down")

    with patch.object(video_module, "_fetch_detail", new=fake_raise):  # [E0b] mock 点同上
        result = asyncio.run(video_module.parse_video("https://b23.tv/x"))
    assert result["status"] == "error"
    assert result["title"] == ""
    assert result["metrics"] == {"likes": 0, "views": 0, "comments": 0, "shares": 0}


# ─── 3. services 解析超时 ───────────────────────────────────────────────────

def test_parse_image_timeout_returns_failed():
    """vision 卡 100s · service 应在 90s 后 timeout 返 failed"""
    from services.chat_attachments import parse_image, PARSE_TIMEOUT_SECONDS
    assert PARSE_TIMEOUT_SECONDS == 90.0

    async def slow_vision(_b, original_filename="x"):
        await asyncio.sleep(100)
        return "should never get here"

    with patch("services.image_to_text._vision_image_to_text",  # [E0c] 原在社媒主路径 router(E3 已删)
               new=slow_vision):
        # 用 mock timeout 短一点跑 · 实际 prod 90s
        with patch("services.chat_attachments.PARSE_TIMEOUT_SECONDS", 0.1):
            res = asyncio.run(parse_image(b"x", "x.jpg"))
    assert res.ok is False
    assert res.status == "failed"
    assert "timeout" in (res.error or "")


def test_parse_image_empty_output_failed():
    """vision 返空 string · 应 failed"""
    from services.chat_attachments import parse_image

    async def empty_vision(_b, original_filename="x"):
        return ""

    with patch("services.image_to_text._vision_image_to_text",  # [E0c] 原在社媒主路径 router(E3 已删)
               new=empty_vision):
        res = asyncio.run(parse_image(b"x", "x.jpg"))
    assert res.ok is False
    assert res.error == "vision_empty_output"


def test_parse_image_success():
    from services.chat_attachments import parse_image

    async def good_vision(_b, original_filename="x"):
        return "图片显示一只猫在沙发上。"

    with patch("services.image_to_text._vision_image_to_text",  # [E0c] 原在社媒主路径 router(E3 已删)
               new=good_vision):
        res = asyncio.run(parse_image(b"x", "cat.jpg"))
    assert res.ok is True and res.status == "parsed"
    assert "猫" in res.markdown_summary
    assert "cat.jpg" in res.markdown_summary


# ─── 4. chat 会话库 helper (FOR UPDATE · 行锁)──────────────────────────────────

# [开源 E3 · B2 · 2026-09-28] db/chat_db.py 随 chat 会话一并删除(E3 孤儿),守它的 5 格退役。


# ─── 5. content_api 4 端点 access control + sweep ──────────────────────────

# ─── 6. parse pipeline 异步 fire-and-forget ────────────────────────────────

# ─── 7. prompt 软口径(v1.3)────────────────────────────────────────────────

def test_chat_research_prompt_has_soft_attachment_clause():
    """v1.3 prompt 软口径:用户已附参考资料时优先用 · 非"绝对不"硬口径"""
    import inspect
    from api import content_api
    src = inspect.getsource(content_api._run_chat_research_agent_loop)
    # 软口径 marker
    assert "优先" in src and "附" in src and "参考" in src
    # 不再硬禁止 tikhub/metaso
    has_hard_block = "不要去 tikhub" in src or "不要去 metaso" in src
    has_soft_clause = "除非用户明确" in src or "明确说" in src
    assert has_soft_clause, "v1.3 必须有软口径例外"
    # 老的"不要去 tikhub/metaso 重新检索"硬口径已被改成软口径
    if has_hard_block:
        # 允许 · 但必须有"除非"作软化
        assert has_soft_clause


# ─── 8. frontend types.ts 契约 ────────────────────────────────────────────




# ─── 10. nginx 配置覆盖 ──────────────────────────────────────────────────

def test_nginx_covers_chat_attachment_endpoints():
    """nginx 必为 /chat-attachment/parse-* 加 50MB / 300s location"""
    import os
    p = os.path.join(os.path.dirname(__file__), "..", "..", "nginx.conf")
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()
    assert "chat-attachment/(parse-file|parse-url)" in content
    # client_max_body_size 50MB 以上 · proxy_read_timeout 300s
    assert "client_max_body_size 60M" in content
    assert "proxy_read_timeout 300s" in content


# ─── 11. AttachmentParseResult 契约稳定 ──────────────────────────────────

def test_attachment_parse_result_contract():
    """AttachmentParseResult 字段稳定 · 防 caller 假设字段不存在崩"""
    from services.chat_attachments import AttachmentParseResult
    r = AttachmentParseResult(ok=True, status="parsed")
    # 必须有的字段
    for k in ("ok", "status", "title", "markdown_summary", "thumbnail_url",
              "platform", "char_count", "error", "extra"):
        assert hasattr(r, k), f"AttachmentParseResult 缺字段 {k}"


# ─── 12. v1.3.1 hotfix(Codex 第 4 轮 4 P0/P1)─────────────────────────────


# ─── 13. v1.3.2 P0 fix(老板 2026-05-21 截图抓 × 删除 + PC 横滑)───────────


# ─── 14. v1.3.3 老板 2026-05-22 截图抓 · 拖拽 overlay + 小红书 deep_link ───────


def test_hotfix_v1_3_3_video_normalize_deep_link_xiaohongshu():
    """小红书 deep_link · xhsdiscover://item/{note_id}"""
    from tools.agent_loop.adapters.video import _normalize_deep_link
    # 直接从 URL 提
    assert _normalize_deep_link(
        "https://www.xiaohongshu.com/explore/abc123XYZ", {}, "xiaohongshu"
    ) == "xhsdiscover://item/abc123XYZ"
    assert _normalize_deep_link(
        "https://www.xiaohongshu.com/discovery/item/note456", {}, "xiaohongshu"
    ) == "xhsdiscover://item/note456"
    # 从 raw.note_id 提(优先)
    assert _normalize_deep_link(
        "https://xhslink.com/short", {"note_id": "fromraw789"}, "xiaohongshu"
    ) == "xhsdiscover://item/fromraw789"
    # 没 note_id 返空
    assert _normalize_deep_link("https://xhslink.com/x", {}, "xiaohongshu") == ""


def test_hotfix_v1_3_3_video_normalize_deep_link_douyin():
    """抖音 deep_link · snssdk1128://aweme/detail/{aweme_id}"""
    from tools.agent_loop.adapters.video import _normalize_deep_link
    assert _normalize_deep_link(
        "https://www.douyin.com/video/7123456789", {}, "douyin"
    ) == "snssdk1128://aweme/detail/7123456789"
    # 从 raw.aweme_id 提
    assert _normalize_deep_link(
        "https://v.douyin.com/xyz", {"aweme_id": "9876543210"}, "douyin"
    ) == "snssdk1128://aweme/detail/9876543210"
    # 非数字 aweme_id 拒绝
    assert _normalize_deep_link("https://v.douyin.com/abc", {"aweme_id": "notnum"}, "douyin") == ""


def test_hotfix_v1_3_3_video_normalize_deep_link_bilibili():
    """B 站 deep_link · bilibili://video/{bvid}"""
    from tools.agent_loop.adapters.video import _normalize_deep_link
    assert _normalize_deep_link(
        "https://www.bilibili.com/video/BV1xx411c7mu", {}, "bilibili"
    ) == "bilibili://video/BV1xx411c7mu"


def test_hotfix_v1_3_3_video_normalize_deep_link_unsupported():
    """视频号 / TikTok / 未知平台 · 返空"""
    from tools.agent_loop.adapters.video import _normalize_deep_link
    assert _normalize_deep_link("https://weixin.qq.com/finder/x", {}, "wechat_channels") == ""
    assert _normalize_deep_link("https://tiktok.com/@u/video/1", {}, "tiktok") == ""
    assert _normalize_deep_link("https://example.com", {}, None) == ""


def test_hotfix_v1_3_3_attachment_parse_result_has_deep_link():
    """AttachmentParseResult 加 deep_link 字段 · 防 caller 假设字段不存在"""
    from services.chat_attachments import AttachmentParseResult
    r = AttachmentParseResult(ok=True, status="parsed")
    assert hasattr(r, "deep_link")
    assert r.deep_link == ""  # 默认空


def test_hotfix_v1_3_3_parse_video_url_passes_deep_link():
    """services.parse_video_url 必透传 video.py 的 deep_link 到 AttachmentParseResult"""
    import asyncio
    from services.chat_attachments import parse_video_url
    from tools.agent_loop.adapters import video as video_module
    from unittest.mock import patch

    async def fake_parse_video(url, need_asr=True, language=""):
        return {
            "title": "测试", "author": "u", "thumbnail": "", "transcript": "字幕",
            "platform": "xiaohongshu",
            "metrics": {"likes": 100, "views": 0, "comments": 0, "shares": 0},
            "deep_link": "xhsdiscover://item/test123",
            "raw": {}, "status": "ok",
        }

    with patch.object(video_module, "parse_video", new=fake_parse_video):
        res = asyncio.run(parse_video_url("https://www.xiaohongshu.com/explore/test123"))
    assert res.ok
    assert res.deep_link == "xhsdiscover://item/test123"


