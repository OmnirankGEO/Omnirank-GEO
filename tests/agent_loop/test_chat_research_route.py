"""老板 A+ 2026-05-19 v1.5 · /api/content/chat 调研类接入 agent_loop · 测试

测试目标:
  1. `_is_research_intent` keyword router 命中(应进 agent_loop)
  2. `_is_research_intent` 不命中(应走老 14 route)
  3. self-data 排除("我抖音粉丝最近涨得慢"应走数据复盘不进 research;复盘模块已随开源 E3 B2 删)
  4. write_verb 排除("帮我把抖音那条仿写"应走 rewrite_reference)
  5. _run_chat_research_agent_loop · admin_setting flag 默认开
  6. _run_chat_research_agent_loop · DATA_TOOLS 命中护栏(防"假查真猜")
  7. Data SSOT 边界:helper 工具白名单不含 internal_memory_query / internal_profile_get / write tools
  8. 文档:docs/AI-CONTEXT/CHAT_AGENT_LOOP_RESEARCH_PLAN_2026-05-19.md
"""

from __future__ import annotations


# ─── 1. _is_research_intent 命中 case(19/19 实测)─────────────────────────────

# [开源 E3 · B2 · 2026-09-28] 对话编排器(社媒包内)随包删除,守它意图路由 / LLM 分类的格退役;
#   下面只剩守 content_api 调研 helper 本体的格。


# ─── 1b. Codex 第 6 轮 P1 反馈 · 高频真实问法补漏(2026-05-19)──────────────────
#       前 5 verb 漏 · 用户说"找高赞/高播放/热度高/热视频/头部账号"等高频句式
#       会掉回老 orchestrator · 出现"反问按钮/不查数据"旧体验


# ─── 2. _is_research_intent 不命中 case ───────────────────────────────────────


# ─── 3. self-data 排除 case(老板 §4.2 红线)───────────────────────────────────


# ─── 4. write_verb 排除 case ──────────────────────────────────────────────────


# ─── 5. admin_setting flag 默认开 ─────────────────────────────────────────────

def test_admin_setting_default_enabled():
    """chat_agent_loop_research_enabled default 'true' · 关闭时退回老 orchestrator."""
    from db.social_preferences_db import ADMIN_SETTINGS_DEFAULTS
    assert "chat_agent_loop_research_enabled" in ADMIN_SETTINGS_DEFAULTS
    value, typ, desc = ADMIN_SETTINGS_DEFAULTS["chat_agent_loop_research_enabled"]
    assert value == "true", "default 应灰度开启"
    assert typ == "bool"


# ─── 6. helper 白名单 tool 不含 write / memory / profile ───────────────────────
#       Data SSOT §1.1.4 红线:防 internal_memory_query track_access=True 写
#       profile_memory_events.access_count

def test_helper_signature_callable_present():
    """_run_chat_research_agent_loop helper 存在 · 防文档/代码漂移."""
    from api.content_api import _run_chat_research_agent_loop
    import inspect
    sig = inspect.signature(_run_chat_research_agent_loop)
    params = list(sig.parameters.keys())
    assert "user_message" in params
    assert "event_sink" in params, "Codex v1.2 反馈 · 必须支持 event_sink 桥 SSE"


def test_helper_summarize_profile_for_research_safe():
    """_summarize_profile_for_research · 空 dict 不炸."""
    from api.content_api import _summarize_profile_for_research
    assert _summarize_profile_for_research({}) == "(资料较少)"
    summary = _summarize_profile_for_research({"industry": "家居", "cities": ["广州", "深圳"]})
    assert "家居" in summary
    assert "广州" in summary or "深圳" in summary


def test_helper_source_contains_no_forbidden_tools():
    """Data SSOT §1.1.4 · helper 源码不暴露 internal_memory_query / internal_profile_get / write tools.

    实证 read · v1.5 砍 internal_memory_query 防 track_access=True UPDATE profile_memory_events.
    """
    import inspect
    from api import content_api
    src = inspect.getsource(content_api._run_chat_research_agent_loop)

    # tools=[ ... ] 块只允许出现 8 个 read tool
    # 禁用名单(违反 Data SSOT §1.1):
    forbidden_in_tools_list = [
        '"internal_memory_query"',
        '"internal_profile_get"',
        '"update_profile_taboo"',
        '"update_profile_memory"',
        '"update_profile_field"',
        '"confirm_memory_conflict"',
        '"archive_memory_event"',
        '"cost_estimate_and_confirm"',
        '"archive_plan"',
        '"update_plan"',
    ]
    # 从源码中提取 tools=[...] block
    tools_block_start = src.find("tools=[")
    tools_block_end = src.find("]", tools_block_start)
    assert tools_block_start > 0, "helper 必须有 tools=[ 白名单"
    tools_block = src[tools_block_start:tools_block_end + 1]

    for tool_str in forbidden_in_tools_list:
        assert tool_str not in tools_block, (
            f"Data SSOT 违约 · helper 白名单不应含 {tool_str}(违 §1.1)"
        )

    # 8 个允许 read tool 必须在
    allowed_in_tools_list = [
        '"industry_knowledge_query"',
        '"metaso_web_search"',
        '"tikhub_search_topics"',
        '"tikhub_get_account"',
        '"tikhub_parse_video"',
        '"keyword_explore"',
        '"web_visit"',
        '"time_now"',
    ]
    for tool_str in allowed_in_tools_list:
        assert tool_str in tools_block, f"v1.5 白名单 8 read 必含 {tool_str}"


def test_helper_source_has_data_tools_guard():
    """v1.3 Codex 第 3 轮护栏 · DATA_TOOLS 命中检查必须在源码里(防"假查真猜")."""
    import inspect
    from api import content_api
    src = inspect.getsource(content_api._run_chat_research_agent_loop)
    assert "DATA_TOOLS" in src, "v1.3 必须有 DATA_TOOLS 集合"
    assert "has_data_tool_ok" in src, "v1.3 必须有 has_data_tool_ok 判定"


# ─── 7. caller 接入位点验证(防文档漂移)──────────────────────────────────────

# ─── 8. 老板 C 方案 2026-05-19 · keyword fast-path + LLM fallback · 30+ 压测 ──
#       9 类真实用户语境覆盖:
#       A 强信号 research(0 LLM) · B 弱信号 research(走 LLM) · C 写稿
#       D 仿写(强排除 · 0 LLM) · E 自己数据(强排除 · 0 LLM) · F 补资料
#       G 闲聊 · H 模糊表达 · I 多平台 alias

import json


def _mock_llm_response(route: str, reason: str = "") -> str:
    """生成 LLM classifier 期望的 JSON 输出"""
    return json.dumps({"route": route, "reason": reason or f"mock_{route}"}, ensure_ascii=False)


# A · 强信号 research(0 LLM · keyword_strong 命中)


# B · 弱信号 research(走 LLM · LLM 判 research)


# C · 写稿(含"写一条"等 → keyword_block · 0 LLM · 直接 other)


# D · 仿写(强排除 · 0 LLM · 直接 other)


# E · 自己账号数据复盘(强排除 · 0 LLM · 直接 other)


# F · 补资料(LLM 判 profile_update → other)


# G · 闲聊(LLM 判 other)


# H · 模糊表达(LLM 判)


# I · 多平台 alias 覆盖


# 失败兜底:LLM 抛错 / 输出非 JSON · 必须 fallback "other"(不能崩)


# ─── 9. 老板 2026-05-19 第 2 轮压测抓 4 误接管(P0 修)──────────────────────────
#       4 case 含强信号 keyword(高赞/爆款)但有 write/self_data 隐藏信号
#       原 v1 v2 拦不下来 → 误进 research · 这轮 P0 修


# ─── 10. 老板 2026-05-20 真机抓:无平台名的调研 query 走 LLM 应判 research ──────
#        prompt 修前 "research" 严格要求"用户问外部平台(抖音/B站/...)"
#        prod 真机 "帮我找一下最近 GEO 这个概念比较火的视频" 没平台名 →
#        LLM 判 other → 走老 14 route 出 4 按钮 clarify(老板:还是这么傻)
#        修法 prompt 放宽 research:不一定要有平台名 · 加 5 few-shot 例


# ─── 11. 老板 2026-05-20 真机第 2 贴 P0:接错入口 · 主对话框走 /quick-generate-stream
#        不是 /api/content/chat · 7 commit 全跑错路径 · 修补到正确入口

# ─── 12. 老板 + Codex Phase 1(2026-05-20)调研结果变可继续工作的对象 ──
#        4 改:数据层链接 / prompt 加链接要求 / 半动态 suggested_prompts / 动名词按钮

def test_phase1_tikhub_summary_markdown_contains_links():
    """tikhub _summary_markdown 必须把标题渲成 [text](url) markdown 链接."""
    import inspect
    from tools.agent_loop.adapters import tikhub
    src = inspect.getsource(tikhub._summary_markdown)
    # 关键 anchor:[{}]({})  / url.startswith http
    assert "url.startswith" in src
    assert "[" in src and "](" in src, "_summary_markdown 必须用 markdown 链接形式"


def test_phase1_helper_prompt_requires_links():
    """_run_chat_research_agent_loop system prompt 必含 \"必须保留 3-5 个可点击 markdown 链接\"."""
    import inspect
    from api import content_api
    src = inspect.getsource(content_api._run_chat_research_agent_loop)
    assert "markdown 链接" in src or "可点击" in src
    assert "3-5" in src or "3 到 5" in src


def test_phase1_helper_returns_suggested_prompts():
    """_run_chat_research_agent_loop decision 必含 suggested_prompts 字段."""
    import inspect
    from api import content_api
    src = inspect.getsource(content_api._run_chat_research_agent_loop)
    assert '"suggested_prompts": suggested' in src or "'suggested_prompts'" in src


def test_phase1_build_suggested_prompts_with_top_items():
    """top items 存在时 · 半动态生成带《标题》+ URL 的按钮(2026-05-21 P1)."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True, "tool": "tikhub_search_topics",
        "result": {"items": [
            {"title": "大白话讲透GEO", "url": "https://douyin.com/v/123"},
            {"title": "315 AI投毒揭秘", "url": "https://b23.tv/456"},
        ]}
    }]
    s = _build_research_suggested_prompts(tr)
    assert len(s) == 4
    # 2026-05-21 Codex P1 · 按钮必须带 URL · 防后续 14 route 拿不到可定位对象
    assert "拆解这条" in s[0] and "大白话讲透GEO" in s[0] and "https://douyin.com/v/123" in s[0]
    assert "仿写这条" in s[1] and "315 AI投毒揭秘" in s[1] and "https://b23.tv/456" in s[1]
    assert "10 个选题" in s[2]
    assert "整理成素材包" in s[3]


def test_phase1_build_suggested_prompts_no_url_only_title():
    """item 缺 URL 时 · 只带标题不崩(不拼 None / 不拼 'http://None')."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True, "tool": "tikhub_search_topics",
        "result": {"items": [{"title": "无 URL 视频", "url": ""}]}
    }]
    s = _build_research_suggested_prompts(tr)
    assert len(s) == 4
    assert "无 URL 视频" in s[0]
    # 不应含任何 url 残片
    assert "http" not in s[0] and "None" not in s[0]


def test_phase1_build_suggested_prompts_invalid_url_skipped():
    """url 不是 http(s) 开头时(如 javascript:)· 只带标题不带 URL · 防 XSS-like 注入."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True, "tool": "tikhub_search_topics",
        "result": {"items": [{"title": "可疑 url 视频", "url": "javascript:alert(1)"}]}
    }]
    s = _build_research_suggested_prompts(tr)
    assert "可疑 url 视频" in s[0]
    assert "javascript:" not in s[0]


def test_phase1_build_suggested_prompts_from_research_table():
    """items 缺失但 research_table 有标题/链接时 · 仍必须生成具体可点击二跳按钮."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True,
        "tool": "tikhub_search_topics",
        "result": {
            "research_table": [
                {"视频/笔记": "315 AI投毒揭秘", "链接": "https://b23.tv/geo315"},
                {"标题": "GEO 8 分钟入门", "url": "https://xhslink.com/geo8"},
            ]
        },
    }]
    s = _build_research_suggested_prompts(tr)
    assert "拆解这条" in s[0] and "315 AI投毒揭秘" in s[0] and "https://b23.tv/geo315" in s[0]
    assert "仿写这条" in s[1] and "GEO 8 分钟入门" in s[1] and "https://xhslink.com/geo8" in s[1]


def test_phase1_build_suggested_prompts_from_summary_markdown():
    """items/research_table 都缺失但 summary_markdown 有 markdown 链接时 · 不应退回无对象按钮."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True,
        "tool": "tikhub_search_topics",
        "result": {
            "summary_markdown": "- [大白话讲透 GEO](https://douyin.com/v/geo123) · 1.1 万赞\n"
                                "- [315 AI 投毒揭秘](https://b23.tv/geo315) · 109 万播放"
        },
    }]
    s = _build_research_suggested_prompts(tr)
    assert "大白话讲透 GEO" in s[0] and "https://douyin.com/v/geo123" in s[0]
    assert "315 AI 投毒揭秘" in s[1] and "https://b23.tv/geo315" in s[1]
    assert "其中一条" not in "\n".join(s[:2])


# ─── 13. 老板 2026-05-21 P0 闭环 · follow-up 按钮二跳必走 other · 防绕圈 ──
#        修前 4/6 误判 research:"拆解这条" / "按这些样本" / "整理成素材" / "拆解其中一条"
#        修后:_has_research_follow_up_signal 强排除 · 6/6 keyword_block other


def test_phase1_build_suggested_prompts_fallback():
    """tool 全失败 / items 异常 · 兜底按钮不能假装能直接拆某条无对象视频."""
    from api.content_api import _build_research_suggested_prompts
    s = _build_research_suggested_prompts([{"ok": False}])
    assert len(s) == 4
    assert "拆解其中一条" not in "\n".join(s)
    assert "仿写其中一条" not in "\n".join(s)
    assert "列成清单" in s[0] or "选一条" in s[0]
    assert "10 个选题" in s[2]
    assert "整理成素材包" in s[3]


def test_phase1_build_suggested_prompts_single_item():
    """只 1 个 top item · 拆解 + 仿写都用 title_1 · 不崩."""
    from api.content_api import _build_research_suggested_prompts
    tr = [{
        "ok": True, "tool": "tikhub_search_topics",
        "result": {"items": [{"title": "孤独 GEO 视频", "url": "https://x"}]}
    }]
    s = _build_research_suggested_prompts(tr)
    assert len(s) == 4
    assert "拆解这条" in s[0] and "孤独 GEO 视频" in s[0]
    assert "仿写这条" in s[1] and "孤独 GEO 视频" in s[1]


def test_phase1_build_suggested_prompts_long_title_truncate():
    """超长标题截到 30 字 · 防按钮文案爆."""
    from api.content_api import _build_research_suggested_prompts
    long_title = "超超超超超长的标题" * 10  # 80 字
    tr = [{
        "ok": True, "tool": "tikhub_search_topics",
        "result": {"items": [{"title": long_title, "url": "https://x"}]}
    }]
    s = _build_research_suggested_prompts(tr)
    # 拆解按钮里 title 截到 30 字 · 格式 "拆解这条:《<title>》 https://..."
    button_text = s[0]
    # 抽《》中间的 title(不含 URL 后缀)
    title_in_button = button_text.split("《", 1)[1].split("》", 1)[0]
    assert len(title_in_button) <= 30
    # URL 仍带上(2026-05-21 P1 修)
    assert "https://x" in button_text
