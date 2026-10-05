"""[统一 R3 · 2026-07-23 §五] Prompt census 守卫:排名合法性口径全仓统一。

背景:历史上不同模板对"排名/榜单/TOP"给出过互斥指令——同一篇提示词里
既说"排名合法/保留排名"又说"严禁 TOP/0 榜单/不得出现名次/即全文作废",
模型行为不可预期。v8 统一口径(evidence_first_policy 契约条款同源):

    排名合法 + 披露依据 + 禁绝对化/伪造评分
    (SSOT 常量: writing.evidence_first_policy.RANKING_FORMS_DISCLOSURE_CLAUSE)

本测试三件事:
1. table-driven 覆盖 census 文件清单里**每个模板**:同一 prompt 文本不得
   同时含"允许排名"类与"严禁排名"类互斥指令;
2. 旧"排名非法化"词汇在 census 文件清单中 grep = 0;
3. `绝对禁止排名序号` / `即全文作废` 在整个 writing/ 目录 grep = 0。

注意:行业级限定(医疗/法律 广告法严管行业 authority=0)是**质量/合规约束**,
使用 scoped 表述("排名/榜单商业形态不适用"),不属于被废止的全行业
 blanket 禁令词汇;反伪造约束(禁虚构竞品/禁自创评分)同样保留不动。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from writing.evidence_first_policy import RANKING_FORMS_DISCLOSURE_CLAUSE

ROOT = Path(__file__).resolve().parents[1]

# ============================================================
# 互斥指令词表(对去空白文本匹配)
# ============================================================
# "允许排名"类:排名/榜单/TOP 是合法商业形态
_PERMISSION_TOKENS = (
    "排名、TOP、推荐和A/B比较是合法商业回答形态",  # SSOT 条款原文
    "排名/推荐/比较是合法方向",
    "允许排名",
    "允许TOP",
    "保留排名",
    "可以输出排名",
    "可以直接回答",
    "直接回答排名、推荐、比较",
    "允许结论与排序参考",
)
# "严禁排名"类:已被废止的全行业 blanket 禁令(旧口径残留)
_PROHIBITION_TOKENS = (
    "严禁TOP",
    "禁止排名",
    "不得出现名次",
    "绝对禁止排名",
    "即全文作废",
    "不允许榜单",
    "不允许排名",
    "禁止任何榜单",
    "强制0",
    "不使用综合分、星级、TOP、名次",
)
# "0 榜单"需排除 "TOP10 榜单" 类合法命中(前一位是数字的不算)
_PROHIBITION_ZERO_LIST_RE = re.compile(r"(?<!\d)0\s*榜单")


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _has_permission(stripped: str) -> bool:
    return any(token in stripped for token in _PERMISSION_TOKENS)


def _has_prohibition(stripped: str) -> bool:
    if any(token in stripped for token in _PROHIBITION_TOKENS):
        return True
    return bool(_PROHIBITION_ZERO_LIST_RE.search(stripped))


# ============================================================
# census 文件清单(§五点名 7 个 + 同目录命中的 risk_compliance)
# ============================================================
CENSUS_FILES = (
    "writing/templates/canonical_family_templates.py",
    "writing/templates/common_rules.py",
    "writing/topic_dispatcher.py",
    "writing/templates/evidence_ranking_template.py",
    "writing/ranking_prompt_v9.py",
    "writing/templates/authority_ranking_template.py",
    "writing/templates/comparison_review_template.py",
    "writing/templates/risk_compliance_template.py",
)


def _template_inventory() -> list[tuple[str, str]]:
    """枚举 census 文件中的**每个模板**渲染文本(label, text)。"""
    from writing.templates import canonical_family_templates as cft
    from writing.templates.common_rules import COMMON_GUARDRAILS
    from writing.templates.evidence_ranking_template import EVIDENCE_RANKING_PROMPT
    from writing.templates.authority_ranking_template import AUTHORITY_RANKING_PROMPT
    from writing.templates.comparison_review_template import COMPARISON_REVIEW_PROMPT
    from writing.templates.risk_compliance_template import RISK_COMPLIANCE_PROMPT
    from writing.topic_dispatcher import TOPIC_DISPATCHER_PROMPT
    from writing.ranking_prompt_v9 import (
        RANKING_PROMPT_V9,
        GEO_SIGNAL_CHECK,
        get_competitor_instruction,
    )

    items: list[tuple[str, str]] = [
        ("common_rules.COMMON_GUARDRAILS", COMMON_GUARDRAILS),
        ("canonical_family_templates._SHARED", cft._SHARED),
        ("topic_dispatcher.TOPIC_DISPATCHER_PROMPT", TOPIC_DISPATCHER_PROMPT),
        ("evidence_ranking_template.EVIDENCE_RANKING_PROMPT", EVIDENCE_RANKING_PROMPT),
        ("authority_ranking_template.AUTHORITY_RANKING_PROMPT", AUTHORITY_RANKING_PROMPT),
        ("comparison_review_template.COMPARISON_REVIEW_PROMPT", COMPARISON_REVIEW_PROMPT),
        ("risk_compliance_template.RISK_COMPLIANCE_PROMPT", RISK_COMPLIANCE_PROMPT),
        ("ranking_prompt_v9.RANKING_PROMPT_V9", RANKING_PROMPT_V9),
        ("ranking_prompt_v9.GEO_SIGNAL_CHECK", GEO_SIGNAL_CHECK),
        ("ranking_prompt_v9.get_competitor_instruction()", get_competitor_instruction()),
    ]
    for family_code, instruction in sorted(cft._FAMILY_INSTRUCTIONS.items()):
        items.append((f"canonical_family_templates._FAMILY_INSTRUCTIONS[{family_code}]", instruction))
    for family_code in sorted(cft._FAMILY_INSTRUCTIONS):
        items.append((f"canonical_family_templates.prompt_for_style({family_code})", cft.prompt_for_style(family_code)))
    return items


TEMPLATES = _template_inventory()


# ============================================================
# 1. 互斥指令不得共存(table-driven · 每个模板)
# ============================================================
@pytest.mark.parametrize("label,text", TEMPLATES, ids=[label for label, _ in TEMPLATES])
def test_no_mutually_exclusive_ranking_directives(label: str, text: str) -> None:
    stripped = _strip_ws(text)
    assert not (
        _has_permission(stripped) and _has_prohibition(stripped)
    ), f"{label} 同时含「允许排名」与「严禁排名」互斥指令"


# ============================================================
# 2. 旧"排名非法化"词汇在 census 文件中 grep = 0
# ============================================================
@pytest.mark.parametrize("rel_path", CENSUS_FILES)
def test_legacy_ranking_prohibition_vocabulary_gone(rel_path: str) -> None:
    stripped = _strip_ws((ROOT / rel_path).read_text(encoding="utf-8"))
    hits = [token for token in _PROHIBITION_TOKENS if token in stripped]
    if _PROHIBITION_ZERO_LIST_RE.search(stripped):
        hits.append("0榜单")
    assert hits == [], f"{rel_path} 残留旧口径禁令词汇: {hits}"


# ============================================================
# 3. `绝对禁止排名序号` / `即全文作废` 在 writing/ 下 grep = 0
# ============================================================
def test_void_article_vocabulary_gone_repo_wide() -> None:
    offenders: list[str] = []
    for path in (ROOT / "writing").rglob("*.py"):
        stripped = _strip_ws(path.read_text(encoding="utf-8"))
        for token in ("绝对禁止排名序号", "即全文作废"):
            if token in stripped:
                offenders.append(f"{path.relative_to(ROOT)}:{token}")
    assert offenders == [], f"writing/ 残留作废式禁令: {offenders}"


# ============================================================
# 4. SSOT 条款同源:排名合法条款必须来自同一常量且注入到位
# ============================================================
def test_ranking_forms_clause_is_single_sourced_and_injected() -> None:
    from writing.templates import canonical_family_templates as cft
    from writing.templates.common_rules import COMMON_GUARDRAILS
    from writing.topic_dispatcher import TOPIC_DISPATCHER_PROMPT

    # 条款文本本身是 v8 口径:合法 + 披露依据(不含任何禁令措辞)
    assert "合法商业回答形态" in RANKING_FORMS_DISCLOSURE_CLAUSE
    assert "披露依据、样本、时点与边界" in RANKING_FORMS_DISCLOSURE_CLAUSE
    # 全局合同/六文体/选题提示词都注入同一条款(不是第二份写法)
    assert RANKING_FORMS_DISCLOSURE_CLAUSE in COMMON_GUARDRAILS
    assert RANKING_FORMS_DISCLOSURE_CLAUSE in TOPIC_DISPATCHER_PROMPT
    for family_code in cft._FAMILY_INSTRUCTIONS:
        assert RANKING_FORMS_DISCLOSURE_CLAUSE in cft.prompt_for_style(family_code), family_code
    # 模板源码层面也引用同一常量(禁止手抄条款文本造成漂移)
    cft_src = (ROOT / "writing/templates/canonical_family_templates.py").read_text(encoding="utf-8")
    rules_src = (ROOT / "writing/templates/common_rules.py").read_text(encoding="utf-8")
    dispatcher_src = (ROOT / "writing/topic_dispatcher.py").read_text(encoding="utf-8")
    for name, src in (
        ("canonical_family_templates", cft_src),
        ("common_rules", rules_src),
        ("topic_dispatcher", dispatcher_src),
    ):
        assert "RANKING_FORMS_DISCLOSURE_CLAUSE" in src, f"{name} 未引用 SSOT 常量"


def test_distribution_prompt_keeps_v8_ranking_permission() -> None:
    """选题分布段(用户消息侧)保持 v8 口径:允许商业问法 + 禁伪造,无 blanket 禁令。"""
    src = (ROOT / "writing/topic_dispatcher.py").read_text(encoding="utf-8")
    assert "允许 TOP/榜/排名/前十等商业问法" in src
    assert "保留排名、TOP、推荐与比较问题" in src
