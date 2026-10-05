"""第 2 轮复检返修判别锁(2026-07-27)。

复检 R2 抓出两条退回级 + 四条建议:
  🔴 飞轮换官方后 `is_answer_cited` 会**静默归零**(旧路径 69.4%,四家最高)——
     官方响应既无原生 annotations、正文也无 `[n]` 角标,解析器一条都认不出。
     这是 2026-07-16「kimi/doubao 采纳恒 0」的重演。
  🔴 飞轮 docstring 声称"软重试 + 兜底,与监测/诊断同口径",**实现却是单发**。
  · system prompt 有三处非检索内容,直接诱导 GEO 的被测指标(品牌提及数 / 引用数)。
  · `tracker.record(success=True)` 在形状自检之前 —— 形状不符那次仍会记成功。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services.research_monitor import platforms as fw


# ---------------------------------------------------------------------------
# ① system prompt 砍到只剩检索指令
# ---------------------------------------------------------------------------
FORBIDDEN = ["市场调研", "服务商", "品牌", "说明你参考", "哪些"]


def test_system_prompt_has_no_metric_leading_content():
    """🔒 system 只准说"先检索再作答",不许描述问题类型、不许诱导枚举品牌/来源。

    GEO 的两个核心指标就是**品牌提及数**与**引用数** —— 在 prompt 里提"有哪些服务商或品牌"
    "说明你参考了哪些来源",等于在诱导被测指标本身。而且这条 system 现在 100% 调用都带,
    带偏就是全量带偏(改前只在重试时发,17–29%)。
    """
    # 运行期读常量(不在收集期取值),两侧都查。
    from tools.ai_visibility.ai_tester import DEEPSEEK_ZERO_SEARCH_SYSTEM

    for name, prompt_const in (("飞轮", fw.DEEPSEEK_OFFICIAL_FORCE_SEARCH_SYSTEM),
                               ("监测/诊断", DEEPSEEK_ZERO_SEARCH_SYSTEM)):
        for word in FORBIDDEN:
            assert word not in prompt_const,                 f"{name} system prompt 含诱导性内容:{word!r} → {prompt_const}"
        assert "web_search" in prompt_const, f"{name} 的检索指令丢了"


def test_both_sides_share_the_same_lean_prompt():
    """监测/诊断与飞轮必须同口径,不能一边精简一边啰嗦。"""
    from tools.ai_visibility.ai_tester import DEEPSEEK_ZERO_SEARCH_SYSTEM

    assert DEEPSEEK_ZERO_SEARCH_SYSTEM == fw.DEEPSEEK_OFFICIAL_FORCE_SEARCH_SYSTEM


# ---------------------------------------------------------------------------
# ② 采纳率:必须给 [n] 角标指令
# ---------------------------------------------------------------------------
def test_flywheel_sends_inline_marker_instruction():
    """🔒 退回级锁:没有角标指令 → 采纳率 69.4% → 0(静默,不报错不告警)。"""
    assert "[n]" in fw.DEEPSEEK_INLINE_MARKER_SUFFIX
    assert "序号" in fw.DEEPSEEK_INLINE_MARKER_SUFFIX
    src = inspect.getsource(fw.query_deepseek)
    assert "prompt + DEEPSEEK_INLINE_MARKER_SUFFIX" in src, \
        "角标指令没挂到 user prompt 上 → 模型不会输出 [n] → is_answer_cited 全 False"


def test_inline_marker_follows_proven_doubao_pattern():
    """照搬 2026-07-16 实测服从的豆包那条(12/12 + 14/14),编号口径必须一致。"""
    assert "内联标注" in fw.DEEPSEEK_INLINE_MARKER_SUFFIX
    assert "不要省略标注" in fw.DEEPSEEK_INLINE_MARKER_SUFFIX
    # 编号 = 原始流序(含重复不去重),与我方 rank 分配一致
    assert "本次搜索结果中的序号" in fw.DEEPSEEK_INLINE_MARKER_SUFFIX


def test_adoption_marking_recognises_inline_markers():
    """端到端:正文带 [2] → 第 2 条来源判为已采纳。"""
    cits = [
        {"url": "https://a/1", "title": "A", "rank": 1, "answer_ranks": [1]},
        {"url": "https://b/2", "title": "B", "rank": 2, "answer_ranks": [2]},
    ]
    marked = fw.mark_answer_cited_sources("结论如下[2]。", cits, raw={})
    assert marked[0]["is_answer_cited"] is False
    assert marked[1]["is_answer_cited"] is True


def test_adoption_is_zero_without_markers_proving_the_risk():
    """反证复检的判断:没有角标也没有 annotations → 采纳全 False(就是会归零)。"""
    cits = [{"url": "https://a/1", "title": "A", "rank": 1, "answer_ranks": [1]}]
    marked = fw.mark_answer_cited_sources("我参考了界面新闻和腾讯新闻。", cits, raw={})
    assert marked[0]["is_answer_cited"] is False


# ---------------------------------------------------------------------------
# ③ 飞轮零检索:重试 + 兜底
#
# 🔴 [R2 复核返修] 这一节原本有三条**源码 grep**,复检 AI 用三种方式打残实现后它们全绿:
#      整段删重试 / 只把 `> 0` 改成 `< 0`(源码文本一字未少)/ 关掉兜底。
#    grep 对无害重构脆、对真实破坏钝 —— 两头不讨好。已整体迁到
#    `test_flywheel_zero_search_behaviour.py` 的**行为级 harness**(照搬监测侧那套),
#    四种破坏方式实测全部转红。此处只保留"旧百炼实现仍在且仅作兜底"这一条结构性断言。
# ---------------------------------------------------------------------------
def test_flywheel_keeps_dashscope_impl_for_fallback_only():
    """旧百炼实现保留为兜底,但不再是默认通道。"""
    assert hasattr(fw, "_query_deepseek_via_dashscope")
    src = inspect.getsource(fw._query_deepseek_via_dashscope)
    assert "dashscope.aliyuncs.com" in src and "forced_search" in src
    assert "仅作**零检索兜底**保留" in src


# ---------------------------------------------------------------------------
# ④ 自检必须先于 record(success=True)
# ---------------------------------------------------------------------------
def test_shape_check_runs_before_success_record():
    """🔒 形状不符 = 根本不是官方通道,那一次不该以 deepseek_official 记成功。"""
    src = inspect.getsource(fw.query_deepseek)
    assert "_assert_official_deepseek_shape(data)" in src
    # 锚点必须是 "success=True,"(带逗号):
    #   · 不能用 "success=True" —— 上方注释里的 record(success=True) 会误命中;
    #   · 也不能用 "tracker.record(" —— HTTP>=400 分支里还有个 record(success=False) 在更前面。
    assert src.index("_assert_official_deepseek_shape(data)") < src.index("success=True,"), \
        "自检还在 record(success=True) 之后 —— 形状不符那次会被以 deepseek_official 记成功,污染成本账本"
