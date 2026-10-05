"""
P0.1b 禁词 SSOT(CTO-15.7 2026-04-24)

与 P0.1a 配对:
- P0.1a 改 prompt 指令 LLM 不输出运营话术(强制在 prompt 层)
- P0.1b(本)加 lint 兜底 · LLM 仍复读时打标 · 为 retry/人工审核提供钩子

范围:
- 运营话术(稳步提升/有望/持续沉淀/迭代优化/持续迭代等)
- 广告法敏感(唯一/第一/最佳 · Agent 06 0424 发现 v9/service/cleaner 3 处冲突词表)

使用方:
- server.py ai_write_report:LLM 返回后跑 lint_report_text · 命中则 log WARN + 可选 retry
- 未来 M1b:周报审核闸门 · 命中即 block

老板批:P0.1b SSOT 上 P1(本 PLAN 只做 seed + log 集成 · 不做 block 逻辑)
"""
from __future__ import annotations
import logging
from typing import NamedTuple

logger = logging.getLogger("GEO-BannedWords")


# ============ 运营话术 · 报告禁用(强命中 · 不允许)============
_OPS_PHRASES = [
    "稳步提升",
    "有望提升",
    "有望突破",
    "持续沉淀",
    "持续迭代",
    "迭代优化",
    "大有可为",
    "指日可待",
    "前景广阔",
    "长期来看",
    "未来可期",
    "蒸蒸日上",
    "蓬勃发展",
    "势头强劲",
    "屡创新高",
]

# ============ 广告法敏感词(软命中 · 触警 · 需结合上下文)============
# Agent 06 2026-04-24 · v9/service/cleaner 3 处词表冲突 · 统一放此
_ADVERTISING_LAW_RISKY = [
    "最佳",
    "最强",
    "最好",
    "第一品牌",
    "国家级",
    "世界级",
    "绝对",
    "100%",
]

# ============ Evidence 无支撑的结论词(必须搭配数据 · 空口不允许)============
_UNSUPPORTED_TREND = [
    "行业领先",
    "遥遥领先",
    "碾压",
    "断层",
    "垄断",
]

# ============ 自我宣传段(CTO-B 2026-04-26 W2 · 老板硬要求 D)============
# 客户版报告禁出现 · 除非证据表里有来源
_SELF_PROMO_CLAIMS = [
    "500+客户",
    "500 + 客户",
    "1000+ 品牌",
    "1000+客户",
    "平均提升 40 分",
    "平均提升40分",
    "市场规模 320 亿",
    "320 亿",
    "320亿",
    "服务上千",
    "覆盖全行业",
    "我们已服务",
    "累计帮助",
]


class LintResult(NamedTuple):
    """lint 结果 · 调用方决定 block/retry/warn"""
    text: str
    ops_hits: list[str]       # 命中的运营话术
    ad_law_hits: list[str]    # 命中的广告法词
    unsupported_hits: list[str]  # 命中的无支撑趋势词
    self_promo_hits: list[str]   # 自我宣传段(W2 老板硬要求 D · 客户版必删)
    has_any: bool             # 是否任一命中


def find_ops_phrases(text: str) -> list[str]:
    """返命中的运营话术(去重 · 保留出现顺序)"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for phrase in _OPS_PHRASES:
        if phrase in text and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def find_advertising_law_risky(text: str) -> list[str]:
    """返命中的广告法敏感词"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for phrase in _ADVERTISING_LAW_RISKY:
        if phrase in text and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def find_unsupported_trend(text: str) -> list[str]:
    """返命中的无支撑趋势词"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for phrase in _UNSUPPORTED_TREND:
        if phrase in text and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


# CTO-15.16 round2 Task C · 报告 v2 lint 误报根因:
#   evidence 引用块/第三方文章标题/客户原问 都在 ``...`` 或 「...」 等引用容器里 ·
#   命中"最好/最佳/绝对"等词时不该判我们违法 · 这是用户/第三方内容的引文
#   strip_quoted=True 在 lint 前剥离这些容器 · 只对"我们的叙述"判罚
import re as _re_strip
_QUOTED_BLOCKS = [
    _re_strip.compile(r"`[^`]*`"),       # markdown 行内代码 / 引用 span
    _re_strip.compile(r"「[^」]*」"),     # 中文引号 · evidence snippet 用
    _re_strip.compile(r"“[^”]*”"),  # English curly double quote
    _re_strip.compile(r"‘[^’]*’"),  # English curly single quote
]


def _strip_quoted_blocks(text: str) -> str:
    """剥离引用容器 · 仅供 lint · 容器内是用户/第三方原文 不属本系统主张"""
    if not text:
        return ""
    out = text
    for pat in _QUOTED_BLOCKS:
        out = pat.sub(" ", out)
    return out


def find_self_promo(text: str) -> list[str]:
    """返命中的自我宣传段(W2 CTO-B · 决策点 D · 客户版禁出现)"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for phrase in _SELF_PROMO_CLAIMS:
        if phrase in text and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return out


def lint_report_text(text: str, *, strip_quoted: bool = False) -> LintResult:
    """统一入口 · 调用方决定是否 block

    strip_quoted=True · 报告 v2 装配后调用 · 跳过 ``引用块`` / 「引用块」 · 只对叙述部分判
    """
    target = _strip_quoted_blocks(text) if strip_quoted else text
    ops = find_ops_phrases(target)
    ad = find_advertising_law_risky(target)
    unsup = find_unsupported_trend(target)
    promo = find_self_promo(target)
    return LintResult(
        text=text,
        ops_hits=ops,
        ad_law_hits=ad,
        unsupported_hits=unsup,
        self_promo_hits=promo,
        has_any=bool(ops or ad or unsup or promo),
    )


def format_lint_warning(result: LintResult) -> str:
    """生成人类可读的 lint 警告字符串(供 log/回写 report.warnings)"""
    if not result.has_any:
        return ""
    parts = []
    if result.ops_hits:
        parts.append(f"运营话术 {len(result.ops_hits)}: {'/'.join(result.ops_hits)}")
    if result.ad_law_hits:
        parts.append(f"广告法敏感 {len(result.ad_law_hits)}: {'/'.join(result.ad_law_hits)}")
    if result.unsupported_hits:
        parts.append(f"无支撑趋势 {len(result.unsupported_hits)}: {'/'.join(result.unsupported_hits)}")
    if result.self_promo_hits:
        parts.append(f"自我宣传 {len(result.self_promo_hits)}: {'/'.join(result.self_promo_hits)}")
    return " | ".join(parts)


# ============ Debug helpers ============
def get_all_banned_phrases() -> dict[str, list[str]]:
    """返所有词表(供运营/管理后台展示)"""
    return {
        "ops_phrases": list(_OPS_PHRASES),
        "advertising_law": list(_ADVERTISING_LAW_RISKY),
        "unsupported_trend": list(_UNSUPPORTED_TREND),
        "self_promo_claims": list(_SELF_PROMO_CLAIMS),
    }
