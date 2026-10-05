"""标题内部术语黑名单(包③ §3G-3)· 生成侧负面约束 + 机审 advisory。

工单:docs/AI-CONTEXT/WORKORDER_TITLE_NATURALNESS_ADOPTED_PATTERN_2026-08-01.md §1 病 B

**病**:写作系统内部的质量话术被当成标题卖点卖给客户 ——
"证据字段与风险检查""复核清单""证据型问答""情景测算""适用边界说明"。
生产实测(2026-08-01 · 近 30 天 183 条我方标题)**31.7% 命中**;
而真实被引 top25(`signal_tier='answer_adopted'`)**零命中**。
客户搜索时不会说"我要看证据字段" —— 这些词只在我们内部有意义。

🔴 两条设计约束:

1. **黑名单是枚举,拼接感是语义 —— 枚举堵不完。**
   所以黑名单只负责"堵住已知的那批",拼接感交给 AI 质检旁路
   (`services/title_quality_ai.py`,advisory 不阻断)。两者互补,不互相替代。

2. **只做 advisory,绝不硬拦标题。** 与包① §3A 同一口径:
   内容侧一律提示级,发不发/改不改由客户决定。
"""
from __future__ import annotations

import re
from typing import Final

#: 从生产 47 条污染样本归纳。**按"只在内部有意义"筛**,不是按"看起来专业"筛 ——
#: 「选购指南」「实操流程」是客户会说的话,不进表;
#: 「证据字段」「复核清单」客户永远不会搜,进表。
TITLE_JARGON_TERMS: Final = (
    "证据字段",
    "证据型",
    "证据密度",
    "证据清单",
    "核验",
    "复核清单",
    "复核路径",
    "情景测算",
    "同字段",
    "适用场景对比",
    "适用边界",
    "数据口径",
    "验收指南",
    "检查点",
    "风险检查",
    "字段与风险",
    "独立证据",
    "能力边界",
)

_JARGON_RE: Final = re.compile("|".join(re.escape(t) for t in TITLE_JARGON_TERMS))


def scan_title_jargon(title: object) -> list[str]:
    """返回标题里命中的内部术语(去重保序)。空表 = 干净。"""
    text = str(title or "")
    hits: list[str] = []
    for match in _JARGON_RE.finditer(text):
        term = match.group(0)
        if term not in hits:
            hits.append(term)
    return hits


def render_title_jargon_constraint() -> str:
    """生成侧负面约束。**与 `scan_title_jargon` 同源同一份表** ——
    禁止在提示词里另抄一份清单(本仓刚踩过"测试硬编码集合副本"的假绿)。"""
    banned = "、".join(f"「{t}」" for t in TITLE_JARGON_TERMS)
    return (
        "\n标题禁用内部术语:" + banned + " 等。"
        "这些是我们内部的质量话术,客户搜索时不会这么说,写进标题只会显得像模板拼接。"
        "\n标题请对照真实被引用文章的写法:"
        "①直接用用户的问法开头(如「哪家好?」「怎么选?」「要花多少钱?」);"
        "②带具体数字或实体(如「这 3 点」「前五家」);"
        "③点出对读者的好处(如「少走弯路」「一文看懂」「更省心」)。"
    )
