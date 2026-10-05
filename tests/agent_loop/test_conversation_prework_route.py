"""Prework route regression(老板 2026-05-16 截图反馈)·

修前(截图症状):
  用户输入 "我想先梳理再写。请先帮我拆清楚目标人群、痛点、内容方向和需要补的信息，确认后再写。"
  → 路由进 content_planning auto_generate_plan
  → AI 回 "可以，我会直接按当前客户资料进入内容规划，排出本月内容结构和拍摄安排" + "打开内容规划" 按钮
  → 用户感觉 AI 没听懂 · 又被推去工具 · 不是真在帮思考

根因:PLANNING_INTENT_MARKERS 含"内容方向" + DIRECT_PLANNING_WORDS 含"先帮"
  → _has_planning_language(compact)=True 且 _has_direct_content_planning_intent(compact)=True
  → 命中 line 1460 的 content_planning auto_generate_plan 分支

修法:加 _has_prework_intake_intent 优先级高于 _has_planning_language
  → 检测到"先梳理 / 先拆 / 确认后再写 / 需要补的信息" → 走 prework_analysis 路由
  → reply 是结构化 4 维拆解承诺(人群/痛点/方向/缺口)+ 让用户确认再写 · 不 auto_generate_plan
"""

from __future__ import annotations


# ─── _has_prework_intake_intent 单元 ──────────────────────────────────────────

# [开源 E3 · B2 · 2026-09-28] 对话编排器(社媒包内)随包删除,守 prework 三路由的 20 格退役;
#   只剩管理开关默认值那一格(开关在 db/social_preferences_db.py,在役)。


# ─── deterministic_social_chat_decision 端到端 ────────────────────────────────


def test_admin_setting_default_chat_llm_first_is_true():
    """2026-05-18 老板拍 GO · default 必须是 true · 默认 LLM-first."""
    from db.social_preferences_db import ADMIN_SETTINGS_DEFAULTS
    default_val, typ, _desc = ADMIN_SETTINGS_DEFAULTS["chat_llm_first_enabled"]
    assert default_val == "true", f"default 必须是 'true' · 实际 {default_val!r}"
    assert typ == "bool"


