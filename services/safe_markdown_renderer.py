"""Canonical safe Markdown-to-HTML renderer for customer-visible content.

SSOT 口径（census MARKDOWN_SINK_CENSUS_2026-07-22 · Review-CTO §13.3）：

- markdown-it-py 负责解析（commonmark + table + strikethrough，``breaks=True``
  保留软换行）；不再用正则假渲染重建表格/段落。
- ``html=True`` 仅让报告生产端写入的 ``details``/``summary`` 块级结构存活；
  bleach 显式标签+属性白名单是最终权威，其余一切标签/属性/协议被剥离。
- 链接仅 http/https 且无 userinfo；非法链接去 href 保留可见文本；
  合法外链 ``target="_blank" rel="noopener noreferrer"``。
- 单行被压平的历史表格数据不逆向重建：标注 ``data-markdown-anomaly`` 后
  按原文转义展示。
- 与前端 ``SafeMarkdown.tsx`` 语义对齐（同一结构化约定、同一链接策略、
  同一 ``.safe-markdown`` CSS 类名）。
"""

from __future__ import annotations

from html import escape
import re
from urllib.parse import urlsplit

import bleach
from bleach.linkifier import Linker
from markdown_it import MarkdownIt

from services.owned_image_policy import is_owned_image_url


_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    # [返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §4] img 从"无条件剥离"
    # 改为"进白名单但由 _allow_attribute 逐条判 src" —— 只有自有图库
    # (同源 + /uploads/article-images/<brand_id>/…)的 src 能活下来,
    # 其余一切 src 被 bleach 摘掉属性后成为无源 <img>,再由下面的
    # _drop_sourceless_img 整块删除。口径与前端读同一份
    # config/owned_image_asset_policy.json,不许两边各写一份。
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "summary",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_SAFE_CLASSES = {"safe-markdown-table", "safe-markdown-anomaly"}
_FLAT_TABLE_RE = re.compile(r"\|\s*:?-{3,}:?\s*\|")
#: bleach 摘掉不合规 src 之后剩下的无源 <img>（前端等价形态是文字占位）。
#: 前瞻里的 src= 前面要求空白，避免 data-src= 之类被当成有源。
_SOURCELESS_IMG_RE = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")


def _is_http_url(value: str) -> bool:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        return False
    try:
        parsed = urlsplit(value.strip())
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def _link_open(tokens, idx, options, env) -> str:
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if _is_http_url(href):
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
    else:
        # bleach 会在下面移除空 href。用 attrSet 兼容 markdown-it-py 的
        # Token.attrs 为 dict 或 pair-list 两种形态。
        token.attrSet("href", "")
    return _MD.renderer.renderToken(tokens, idx, options, env)


def _allow_attribute(tag: str, name: str, value: str) -> bool:
    if tag == "img":
        # 🔴 只放行自有图库的 src —— 不是"只要 http(s) 就放行"。
        # 跟踪像素风险不因本单消失(返修单 §3 硬性约束 3)。
        if name == "src":
            return is_owned_image_url(value)
        if name == "alt":
            return True
        if name == "loading":
            return value == "lazy"
        if name == "referrerpolicy":
            return value == "no-referrer"
        return False
    if tag == "a":
        if name == "href":
            return _is_http_url(value)
        if name == "target":
            return value == "_blank"
        if name == "rel":
            return value == "noopener noreferrer"
        if name == "title":
            return True
    if tag == "div" and name == "class":
        return value in _SAFE_CLASSES
    return False


_MD = MarkdownIt(
    "commonmark",
    {
        "html": True,
        "breaks": True,
        "linkify": False,
        "typographer": False,
    },
).enable("table").enable("strikethrough")
# 即使目标协议不安全也解析链接语法：renderer 保可见文本、去危险目标。
# 否则 markdown-it 会把完整 ``[label](javascript:...)`` 源码留在客户可见文本里。
_MD.validateLink = lambda _url: True
_MD.renderer.rules["link_open"] = _link_open
_MD.renderer.rules["table_open"] = lambda *_args: '<div class="safe-markdown-table"><table>\n'
_MD.renderer.rules["table_close"] = lambda *_args: "</table></div>\n"
_MD.renderer.rules["s_open"] = lambda *_args: "<del>"
_MD.renderer.rules["s_close"] = lambda *_args: "</del>"


def _harden_link(attrs, _new=False):
    # 与前端语义对齐：Markdown 链接与 CommonMark autolink 支持；
    # 裸散文 URL 保持散文，不主动 linkify。
    if _new:
        return None
    href = attrs.get((None, "href"), "")
    if not _is_http_url(href):
        return None
    attrs[(None, "target")] = "_blank"
    attrs[(None, "rel")] = "noopener noreferrer"
    return attrs


_LINKER = Linker(callbacks=[_harden_link], parse_email=False)


def markdown_structure_anomaly(value: object) -> str | None:
    """仅当原始块边界已不可恢复时返回 anomaly 码。"""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or "\n" in text:
        return None
    if text.count("|") >= 4 and _FLAT_TABLE_RE.search(text):
        return "flattened_table"
    return None


def render_safe_markdown(value: object, *, annotate_anomaly: bool = True) -> str:
    """Render Markdown with GFM tables and a tightly controlled HTML subset.

    不截断任何输入。历史值若已不可逆压平，则按转义可读文本展示并显式标注
    anomaly，绝不臆造表格行或换行。
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""

    anomaly = markdown_structure_anomaly(text)
    if anomaly:
        readable = f"<p>{escape(text)}</p>"
        if not annotate_anomaly:
            return readable
        return (
            f'<div class="safe-markdown-anomaly" data-markdown-anomaly="{anomaly}">'
            "历史内容的换行结构已缺失，以下按原文安全展示。"
            "</div>"
            f"{readable}"
        )

    rendered = _MD.render(text)
    cleaned = bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_allow_attribute,
        protocols={"http", "https"},
        strip=True,
        strip_comments=True,
    )
    # [返修单 §4] bleach 摘掉不合规 src 后会留下无源 <img>（浏览器显示碎图标）。
    # 前端对同一情况给的是文字占位，两边语义必须一致 → 无源 img 整块删除。
    cleaned = _SOURCELESS_IMG_RE.sub("", cleaned)
    # Linker 回调加固未经过 markdown-it link_open 规则的裸 anchor；
    # 刻意不把裸散文 URL 变成链接，与 React 端一致。
    return _LINKER.linkify(cleaned)


SAFE_MARKDOWN_CSS = """
.safe-markdown{line-height:1.75;overflow-wrap:anywhere;word-break:break-word;white-space:normal}
.safe-markdown h1,.safe-markdown h2,.safe-markdown h3,.safe-markdown h4{margin:1.1em 0 .55em;font-weight:700;line-height:1.35}
.safe-markdown p{margin:.65em 0}.safe-markdown ul,.safe-markdown ol{margin:.65em 0;padding-left:1.5em}
.safe-markdown blockquote{margin:.8em 0;padding:.2em .9em;border-left:3px solid #94a3b8;color:#475569}
.safe-markdown pre{max-width:100%;overflow:auto;padding:12px;border-radius:6px;background:#0f172a;color:#e2e8f0;white-space:pre}
.safe-markdown code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.safe-markdown :not(pre)>code{padding:.1em .3em;border-radius:4px;background:#e2e8f0;color:#0f172a}
.safe-markdown .safe-markdown-table{max-width:100%;overflow-x:auto;margin:.8em 0;-webkit-overflow-scrolling:touch}
.safe-markdown table{width:100%;min-width:560px;border-collapse:collapse}
.safe-markdown th,.safe-markdown td{padding:8px 10px;border:1px solid #cbd5e1;text-align:left;vertical-align:top}
.safe-markdown th{background:#f1f5f9;font-weight:700}
.safe-markdown details{margin:.75em 0;border:1px solid #cbd5e1;border-radius:6px;padding:.55em .7em}
.safe-markdown summary{cursor:pointer;font-weight:650}
.safe-markdown a{color:#0369a1;text-decoration:underline;text-underline-offset:2px}
.safe-markdown .safe-markdown-anomaly{margin:.65em 0;padding:.55em .7em;border-left:3px solid #d97706;background:#fffbeb;color:#92400e;font-size:.9em}
@media print{.safe-markdown .safe-markdown-table{overflow:visible}.safe-markdown table{min-width:0;font-size:9pt}.safe-markdown details{break-inside:avoid}.safe-markdown details>*{display:block!important}}
"""
