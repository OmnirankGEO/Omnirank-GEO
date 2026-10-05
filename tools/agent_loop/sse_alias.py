"""供应商名 alias 层 · 仅 user-facing SSE event 出口处翻译.

2026-05-16 老板反馈 · 用户可见层不能露 tikhub / metaso / 5118 等供应商品牌名(商业机密)。

设计原则:
- LLM tool definitions / tool_router / internal logging 保留 internal 名(LLM 已学过 · 不能改)
- SSE event 推到 FE 时 · `tool` 字段 + `used_tools` 数组通过本 alias 翻译
- TOOL_LABELS 文案层仍键 internal 名(BE 内部 lookup · 标签本身已是中文中性词)

只翻含供应商名的几个 · 其他工具(internal_*/web_visit/time_now/keyword_explore/...)已中性 · 不改:
- tikhub_*  → platform_*
- metaso_web_search → web_research_realtime
"""

from __future__ import annotations


# 内部 internal name → user-facing alias(只有露供应商名的需要翻)
SUPPLIER_TOOL_ALIAS_FOR_FE: dict[str, str] = {
    "tikhub_search_topics": "platform_topic_research",
    "tikhub_get_account": "platform_account_sample",
    "tikhub_parse_video": "platform_video_parse",
    "metaso_web_search": "web_research_realtime",
}


def to_external_tool_name(internal: str) -> str:
    """把 internal tool 名翻译成 FE 可见的中性 alias · 没在 map 里就 passthrough."""
    if not internal:
        return ""
    return SUPPLIER_TOOL_ALIAS_FOR_FE.get(str(internal), str(internal))


def to_external_tool_names(internal_names: list[str] | None) -> list[str]:
    """批量翻译 used_tools 数组."""
    if not internal_names:
        return []
    return [to_external_tool_name(name) for name in internal_names]
