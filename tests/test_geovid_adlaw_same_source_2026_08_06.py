# -*- coding: utf-8 -*-
"""广告法同源锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3 · P1-1

背景(实测):图文链原来自带 15 词表,与 Owner 签发目录**不是同一份**;
`_PROMPT` 里还硬编码了**第三份**。1,164 条真实语料上签发目录命中 85 条、
本地词表只命中 12 条 —— **漏检率 95%**。

治理依据:治理 SSOT **v2.5**(Owner 2026-08-06 明确适用于抖音图文链)——
目录同源 → ①写作侧 prompt 约束(主防) ②生成后自检 ③提示+一键修复,**全链零硬拦**。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import content_generator as cg


REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "services" / "geo_douyin" / "content_generator.py"


# ===========================================================================
# 同源
# ===========================================================================

def test_no_local_wordlist_left_in_module():
    """🔴 模块内不得再有自建的广告法词表常量(判据先剥注释,别撞拆除说明)。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            continue
        lits = {e.value for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        overlap = lits & {"国家级", "最高级", "最佳", "顶级", "第一品牌", "百分百"}
        assert len(overlap) < 3, f"又出现自建广告法词表:{overlap}"


def test_terms_come_from_the_signed_pack():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_ad_law_terms")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "legal_pack" in names, "词表没走签发目录"


def test_scan_uses_context_matcher_not_plain_substring():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "scan_ad_law")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "absolute_law_hits" in names, "退回纯子串匹配了"


@pytest.mark.parametrize("text,word", [
    ("行业最好的选择", "最好"), ("全国第一的服务商", "全国第一"),
    ("国内首选品牌", "首选"), ("世界级团队", "世界级"),
    ("最强方案", "最强"), ("史上最全整理", "史上最"),
    ("最优解", "最优"), ("最先进工艺", "最先进"),
])
def test_terms_the_old_15_word_list_missed_are_now_caught(text, word):
    """旧词表全漏的那批 —— 漏检率 95% 的来源。"""
    assert word in cg.scan_ad_law(text), f"仍然漏检:{text}"


@pytest.mark.parametrize("text", [
    "这是我第一次做GEO", "第一步先看诊断", "进入第一梯队",
    "第一眼看上去", "最大化收益", "最大限度降低风险",
])
def test_benign_context_is_not_flagged(text):
    """🔴 反向对照:纯子串会在这批上误报 28%,语境匹配必须放行。"""
    assert cg.scan_ad_law(text) == [], f"误报:{text}"


# ===========================================================================
# ① 写作侧 prompt 约束(主防)
# ===========================================================================

def test_prompt_block_is_generated_from_the_pack_not_hardcoded():
    block = cg.ad_law_prompt_block()
    terms = cg._ad_law_terms()
    assert terms, "签发目录取不到词"
    assert any(t in block for t in terms), "prompt 块没带目录里的词"


def test_prompt_has_a_placeholder_not_a_frozen_list():
    """🔴 `_PROMPT` 里必须是占位符,不能是第三份硬编码词表。"""
    assert "{ad_law_block}" in cg._PROMPT
    # 反向面:模板里不许再出现整串写死的词表
    assert "国家级/最高级/最佳/第一/顶级" not in cg._PROMPT


def test_prompt_block_is_actually_wired_into_format():
    src = SRC.read_text(encoding="utf-8")
    i = src.find("prompt = _PROMPT.format(")
    assert i > 0
    assert "ad_law_block=" in src[i: i + 1200], "占位符没接线,format 会 KeyError"


# ===========================================================================
# ③ 提示 + 一键修复 · 零硬拦
# ===========================================================================

def test_notice_is_advisory_never_blocking():
    n = cg.ad_law_notice(["第一", "最好"])
    assert n is not None
    assert n["blocking"] is False
    assert n["overridable"] is True
    for k in ("eligible", "rejected", "blocked_by"):
        assert k not in n, f"通知里出现阻断语义 {k}"


def test_notice_offers_a_repair_exit_and_a_publish_anyway_exit():
    n = cg.ad_law_notice(["第一"])
    ids = {a["id"] for a in n["actions"]}
    assert "ai_repair" in ids, "没给一键修复出口"
    assert "publish_anyway" in ids, "没给'先发'出口 —— 那就是变相硬拦"


def test_notice_is_none_when_clean():
    assert cg.ad_law_notice([]) is None
    assert cg.ad_law_notice(None) is None


def test_notice_carries_rule_version_for_audit():
    assert cg.ad_law_notice(["第一"])["rule_version"], "没带目录版本,事后审不了"


def test_notice_is_exposed_in_to_dict():
    """🔴 flags 落库但零展示是原来的病 —— 通知必须真的出现在对外结构里。"""
    c = cg.GeneratedContent(body="x", ad_law_flags=["第一"])
    d = c.to_dict()
    assert "ad_law_notice" in d and d["ad_law_notice"] is not None


def test_scan_never_raises_even_if_pack_is_unavailable(monkeypatch):
    """反向对照:目录取不到时**降级返空**,不抛 —— 永不中断。"""
    import services.marketing.guards as g
    monkeypatch.setattr(g, "legal_pack", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert cg._ad_law_terms() == ()
    assert "禁一切绝对化用语" in cg.ad_law_prompt_block()


def test_module_has_no_hard_block_on_ad_law():
    """形态锁:全模块不得因广告法命中而判废(零硬拦)。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "GeneratedContent"):
            continue
        for kw in node.keywords:
            if kw.arg == "error" and isinstance(kw.value, ast.Constant):
                assert "ad_law" not in str(kw.value.value), "广告法被做成了判废理由"
