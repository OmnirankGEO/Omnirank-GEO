# -*- coding: utf-8 -*-
"""「虚构竞品」不可复活锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3 §6.2

2026-08-06 之前 `ranking_prompt_v9.COMPETITOR_SELF_ASSESSMENT_PROMPT` 写着
「半透明行业:中小型/区域性竞品**可虚构**」「低透明行业:竞品**可全部使用虚构名称**」。
它被 `GEO_EVIDENCE_FIRST_ENABLED`(默认 true)挡在回落分支里 —— 但那个 flag 的
docstring 自己写着「false is the rollback」,**关掉即引爆**:系统会合法地编造公司名做榜单。
与 Owner 亲裁治理 SSOT D12②「位次必须有可核验依据、禁自创评分体系」正面冲突。

🔴 判据必须**先剥注释与 docstring** —— 拆除说明里逐字引用了「可虚构」「虚构名称」,
不剥就会撞到我自己写的注释,锁永远绿(这与"锚点没命中"表现相反、危害相同)。
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
RANKING_V9 = REPO / "writing" / "ranking_prompt_v9.py"


def _code_only(path: pathlib.Path) -> str:
    """只留**会进 prompt 的字符串字面量与代码**,剥掉注释和 docstring。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts: list[str] = []
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            parts.append(node.value)
    return "\n".join(parts)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return _code_only(RANKING_V9)


def test_stripper_itself_has_discriminative_power():
    """判据自检:剥注释这一步必须真的在剥,否则后面所有断言都是恒真的。"""
    src = RANKING_V9.read_text(encoding="utf-8")
    assert "可虚构" in src, "源文件里连拆除说明都没有 —— 锚点错了"
    assert "可虚构" not in _code_only(RANKING_V9), "剥注释没生效,判据恒真"


@pytest.mark.parametrize("phrase", [
    "可虚构", "可全部使用虚构名称", "中小型/区域性竞品可虚构",
    "虚构名", "化名", "占位品牌",
])
def test_no_fabrication_permission_survives_in_any_prompt_literal(prompt_text, phrase):
    """进 prompt 的字面量里不得再有"允许虚构"的口径。

    ⚠️ 「禁止虚构公司、化名、占位品牌」这类**禁止句**是允许的 —— 见下一条反向对照。
    """
    if phrase in prompt_text:
        # 只有出现在明确的禁止句里才放行
        for line in prompt_text.splitlines():
            if phrase in line:
                assert any(neg in line for neg in ("禁止", "不得", "严禁", "不要", "不能")), \
                    f"出现了非禁止语境的「{phrase}」:{line.strip()[:90]}"


def test_prohibition_sentence_is_still_there(prompt_text):
    """反向对照:禁止虚构的那句**必须在**,否则上一条可以靠"把整段删光"通过。"""
    assert "禁止虚构公司" in prompt_text


def test_constant_is_gone():
    assert "COMPETITOR_SELF_ASSESSMENT_PROMPT" not in RANKING_V9.read_text(encoding="utf-8")


def test_get_competitor_instruction_has_no_branch():
    """🔴 函数体内不得有任何 if/条件表达式 —— 有分支就等于留着回滚引爆路径。"""
    tree = ast.parse(RANKING_V9.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_competitor_instruction")
    branches = [n for n in ast.walk(fn) if isinstance(n, (ast.If, ast.IfExp, ast.Match))]
    assert not branches, "get_competitor_instruction 又有分支了"
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 1, f"应只有一个 return,实际 {len(returns)}"


@pytest.mark.parametrize("flag", ["true", "false", "0", "off", "no", ""])
def test_rollback_flag_cannot_bring_fabrication_back(flag, monkeypatch):
    """🔴 行为锁:任何 flag 取值下产出都必须一致且不含虚构许可。

    这是本文件最重要的一条 —— 源码串锁可以被"换个写法"绕过,行为锁不能。
    """
    monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", flag)
    from writing.ranking_prompt_v9 import get_competitor_instruction
    text = get_competitor_instruction()
    assert "禁止虚构公司" in text
    for bad in ("可虚构", "可全部使用虚构名称", "中小型/区域性竞品可虚构"):
        assert bad not in text, f"flag={flag!r} 时虚构口径复活:{bad}"


def test_all_flag_values_produce_identical_text(monkeypatch):
    """反向对照:不是"每个取值都不含虚构",而是"**根本没有分支**"——产出必须逐字相同。"""
    from writing.ranking_prompt_v9 import get_competitor_instruction
    seen = set()
    for flag in ("true", "false", "0", "off", "", "anything"):
        monkeypatch.setenv("GEO_EVIDENCE_FIRST_ENABLED", flag)
        seen.add(get_competitor_instruction())
    assert len(seen) == 1, f"不同 flag 产出了 {len(seen)} 种文本 —— 还有分支"
