"""类 Kimi chat 参考资料解析 service(2026-05-21 v1.3)

设计原则:
1. **不入 KB**:聊天临时附件 ≠ 客户知识库 · 不写 `social_kb_documents` 等表
   · 图片走 _vision_image_to_text(原始字节级 · 不留盘)
   · PDF 走 _extract_pdf_with_vision(同 /flywheel/attach · 已验证不入 KB)
   · Word/TXT/MD 走 _extract_upload_text(同 /flywheel/attach)
2. **90s timeout**:防 fire-and-forget 永久 parsing · asyncio.wait_for 兜底
3. **稳定契约**:返 AttachmentParseResult dataclass · 字段缺失允许空但 key 必有
4. **不假装支持**:Excel / 视频文件本地 ASR 返 unsupported · 让前端友好提示

文档:docs/AI-CONTEXT/CHAT_ATTACHMENT_V1.3.md
红线:
· 不调 /api/social/profile/knowledge/upload(那会入 KB · 红线 1.1.3)
· 不污染 client_profiles / profile_memory_events
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("GEO-ChatAttachments")

# 单次解析硬超时 · 防 fire-and-forget 永久 parsing
PARSE_TIMEOUT_SECONDS = 90.0

# 文件大小上限 · 沿用 nginx + /flywheel/attach 50MB
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# 文档类后缀(不入 KB · 走 _extract_upload_text)
DOC_EXTENSIONS = {"pdf", "doc", "docx", "txt", "md", "markdown"}
# 图片后缀(走 vision · 不入 KB)
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"}
# 视频文件后缀(暂不做本地 ASR · placeholder)
VIDEO_FILE_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
# 明确拒绝 · 友好提示(老板 + Codex 拍:Excel 砍 · 不假装支持)
UNSUPPORTED_EXTENSIONS = {"xlsx", "xls", "csv", "zip", "rar", "7z", "pptx", "ppt"}


@dataclass
class AttachmentParseResult:
    """统一契约 · 所有 dispatch 分支返回此结构"""
    ok: bool
    status: str  # "parsed" | "failed" | "unsupported"
    title: str = ""
    markdown_summary: str = ""
    thumbnail_url: str = ""
    platform: Optional[str] = None
    # 2026-05-22 v1.3.3 老板 B · App deep link · 仅 video_url 类型有 · 用户点 "在 App 中打开"
    deep_link: str = ""
    char_count: int = 0
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)
    # 2026-05-21 v1.5 chat 附件计费 · bg task 用 incurred_cost 决定是否真扣
    # cost_feature_code: SOCIAL_FEATURES 白名单内的 feature(profile_polish / video_asr · 决定扣什么配额)
    # cost_video_minutes: 仅 video_asr 用 · 真实视频分钟数(向上取整 · 最小 1)
    incurred_cost: bool = False
    cost_feature_code: Optional[str] = None
    cost_video_minutes: int = 0


def _short_title(text: str, max_len: int = 40) -> str:
    """统一标题截断 · 防 chip 文案爆 · 单行"""
    t = (text or "").strip().replace("\n", " ").replace("\r", " ")
    return t[:max_len] + ("…" if len(t) > max_len else "")


# ============================================================================
# Dispatch
# ============================================================================

async def parse_image(file_bytes: bytes, filename: str) -> AttachmentParseResult:
    """图片 → qwen3.6-plus vision(同 /flywheel/attach · 不入 KB)

    v1.5 计费:图片必扣 profile_polish(light_chat 30 积分 ≈ ¥0.23 fallback)
    业务失败(vision empty / timeout / 异常)不扣
    """
    try:
        from services.image_to_text import _vision_image_to_text  # [E0c] 原在社媒主路径 router(E3 已删)
        # 90s timeout 兜底(防 LLM 挂)
        text = await asyncio.wait_for(
            _vision_image_to_text(file_bytes, original_filename=filename),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
        text = (text or "").strip()
        if not text:
            return AttachmentParseResult(
                ok=False, status="failed",
                error="vision_empty_output",
            )
        return AttachmentParseResult(
            ok=True, status="parsed",
            title=_short_title(filename or "图片"),
            markdown_summary=f"### 图片参考:{filename}\n\n{text[:2000]}",
            char_count=len(text),
            # v1.5 计费 · 业务成功 = 真调了 qwen vision · 扣 profile_polish
            incurred_cost=True,
            cost_feature_code="profile_polish",
        )
    except asyncio.TimeoutError:
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"timeout_{int(PARSE_TIMEOUT_SECONDS)}s_image_vision",
        )
    except Exception as exc:
        logger.warning(f"[ChatAttachments] image parse failed file={filename}: {exc}")
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"image_vision_error: {str(exc)[:150]}",
        )


async def parse_document(file_bytes: bytes, filename: str, content_type: str = "") -> AttachmentParseResult:
    """PDF/Word/TXT/MD → _extract_upload_text(同 /flywheel/attach · 不入 KB)

    Excel 在 caller 已拒绝 · 这里只处理 doc 类

    v1.5 计费 · Codex 2-审 P0 #2 实证(当时在社媒主路径文件里;现 `services/upload_text._extract_upload_text`):
      - .pdf → `_extract_pdf_with_vision`(混合 pdf_reader 文字层 + qwen vision 嵌入图)→ **扣 profile_polish**
      - .txt / .md / .csv / .json → 纯本地解码 → **0 成本不扣**
      - .docx / .doc → file_analyzer(python-docx)→ **0 成本不扣**
    """
    ext_lower = ""
    if "." in (filename or ""):
        ext_lower = filename.rsplit(".", 1)[-1].lower()
    pdf_incurred_cost = ext_lower == "pdf"  # 仅 PDF 走 vision · 其他 doc 类型本地解析

    try:
        from services.upload_text import _extract_upload_text  # [B1b-1] 原在社媒主路径 router(E3 已删)
        from io import BytesIO
        # 包一层 UploadFile-like 对象
        class _BytesUpload:
            def __init__(self, b, n, ct):
                self.filename = n
                self.content_type = ct
                self._b = b
                self.file = BytesIO(b)
            async def read(self):
                return self._b
            async def seek(self, pos):
                self.file.seek(pos)

        text = await asyncio.wait_for(
            _extract_upload_text(_BytesUpload(file_bytes, filename, content_type)),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
        text = (text or "").strip()
        if not text:
            return AttachmentParseResult(
                ok=False, status="failed",
                error="empty_content",
            )
        return AttachmentParseResult(
            ok=True, status="parsed",
            title=_short_title(filename or "文档"),
            markdown_summary=f"### 文档参考:{filename}\n\n{text[:3000]}",
            char_count=len(text),
            # v1.5 计费 · PDF 走 vision 真扣 · doc/txt/md 0 成本不扣
            incurred_cost=pdf_incurred_cost,
            cost_feature_code="profile_polish" if pdf_incurred_cost else None,
        )
    except asyncio.TimeoutError:
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"timeout_{int(PARSE_TIMEOUT_SECONDS)}s_doc_extract",
        )
    except Exception as exc:
        logger.warning(f"[ChatAttachments] doc parse failed file={filename}: {exc}")
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"text_extract_error: {str(exc)[:150]}",
        )


async def parse_video_url(url: str) -> AttachmentParseResult:
    """视频 URL → tools/agent_loop/adapters/video.py(normalize 后稳定契约)"""
    try:
        from tools.agent_loop.adapters.video import parse_video
        result = await asyncio.wait_for(
            parse_video(url, need_asr=True),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict):
            return AttachmentParseResult(
                ok=False, status="failed",
                error="parse_video_invalid_return",
            )
        # video.py 已加 normalize layer · 直接读字段
        title = str(result.get("title") or "短视频").strip()
        author = str(result.get("author") or "").strip()
        thumbnail = str(result.get("thumbnail") or "").strip()
        transcript = str(result.get("transcript") or "").strip()
        platform = result.get("platform") or None
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        likes = int(metrics.get("likes") or 0)
        views = int(metrics.get("views") or metrics.get("play") or 0)

        # 至少要有 title 或 transcript · 否则视为 fail(防 video.py 全空返"短视频")
        if not transcript and title == "短视频":
            return AttachmentParseResult(
                ok=False, status="failed",
                error="parse_video_empty_content",
            )

        md_lines = [f"### 视频参考:{title}"]
        if author:
            md_lines.append(f"作者:{author}")
        if likes or views:
            md_lines.append(f"互动:赞 {likes} / 播 {views}")
        if transcript:
            md_lines.append(f"\n字幕摘要:\n{transcript[:2000]}")
        md_lines.append(f"\n原链接:{url}")
        summary = "\n".join(md_lines)

        # v1.3.3 · 透传 video.py normalize 的 deep_link
        deep_link = str(result.get("deep_link") or "").strip()

        return AttachmentParseResult(
            ok=True, status="parsed",
            title=_short_title(title),
            markdown_summary=summary,
            thumbnail_url=thumbnail,
            platform=platform,
            deep_link=deep_link,
            char_count=len(summary),
            extra={"transcript_length": len(transcript), "metrics": metrics},
            # [E0b 2026-09-26 · Review 定] 视频链接附件只取基础信息、不做转写 ⇒ 不收转写费:
            #   不再扣 video_asr,三个计费字段置空(调用方 content_api 见 incurred_cost=False 直接暴露)。
            #   锁 tests/e0b_lift_scoring_video_2026_09_26::test_video_link_attachment_carries_no_charge
            incurred_cost=False,
            cost_feature_code=None,
            cost_video_minutes=0,
        )
    except asyncio.TimeoutError:
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"timeout_{int(PARSE_TIMEOUT_SECONDS)}s_video_url",
        )
    except Exception as exc:
        logger.warning(f"[ChatAttachments] video_url parse failed url={url}: {exc}")
        return AttachmentParseResult(
            ok=False, status="failed",
            error=f"video_url_error: {str(exc)[:150]}",
        )


def parse_video_file_placeholder(filename: str, size_bytes: int) -> AttachmentParseResult:
    """本地视频文件 · 老板 + Codex 拍:第一版 placeholder · 不做本地 ASR

    用户可在 chat 里用 URL 形式补传 · 或基于文件名 + 用户描述参考
    """
    return AttachmentParseResult(
        ok=True, status="parsed",
        title=_short_title(filename or "视频"),
        markdown_summary=(
            f"### 视频文件参考:{filename}\n\n"
            f"(本地视频文件已上传 · 暂未做本地 ASR 解析。"
            f"如需逐字稿 · 请将视频上传到抖音/B 站/小红书等平台后用 URL 形式再附一次。"
            f"或在对话中描述视频核心内容 · AI 会综合参考。)"
        ),
        char_count=size_bytes,
    )


def reject_unsupported(filename: str, ext: str) -> AttachmentParseResult:
    """Excel / 压缩包 等友好拒绝 · 不假装支持"""
    suggestion_map = {
        "xlsx": "请将表格内容复制成 Word / PDF / 纯文本后上传",
        "xls": "请将表格内容复制成 Word / PDF / 纯文本后上传",
        "csv": "请将 CSV 转换成 PDF / TXT 后上传",
        "pptx": "请将 PPT 导出为 PDF 后上传",
        "ppt": "请将 PPT 导出为 PDF 后上传",
        "zip": "请解压后逐个上传文件",
        "rar": "请解压后逐个上传文件",
        "7z": "请解压后逐个上传文件",
    }
    hint = suggestion_map.get(ext, "请转 PDF / Word / 图片后上传")
    return AttachmentParseResult(
        ok=False, status="failed",
        error=f"unsupported_{ext}",
        title=_short_title(filename or f"文件.{ext}"),
        markdown_summary=f"(暂不支持 .{ext} 文件 · {hint})",
    )


def detect_file_type(filename: str, content_type: str = "") -> tuple[str, str]:
    """返 (kind, ext)
    kind: 'image' | 'document' | 'video_file' | 'unsupported'
    ext:  小写后缀(无 .)
    """
    ext = ""
    if "." in (filename or ""):
        ext = filename.rsplit(".", 1)[-1].lower().strip()
    ctype = (content_type or "").lower()

    if ext in UNSUPPORTED_EXTENSIONS:
        return ("unsupported", ext)
    if ext in IMAGE_EXTENSIONS or ctype.startswith("image/"):
        return ("image", ext)
    if ext in VIDEO_FILE_EXTENSIONS or ctype.startswith("video/"):
        return ("video_file", ext)
    if ext in DOC_EXTENSIONS:
        return ("document", ext)
    # 未知类型 · 拒绝
    return ("unsupported", ext or "unknown")


def detect_video_url_platform(url: str) -> Optional[str]:
    """从 URL 识别平台 · 返 None 不是支持的视频 URL"""
    low = (url or "").lower().strip()
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
