"""[退役记录 · 2026-08-17] 「兜底模板尊重 title_form」六锁的**语义已被 Owner 裁决取代**。

原文件(2026-08-01)锁的是:`_fallback_style_title_map` / `_fallback_form_completion_map`
/ `fallback_templates_for_form` / `pick_diverse_template` 这条**硬编码兜底模板**链
在用户显式指定 `title_form` 时按形态取模板。

Owner 2026-08-17 裁决把硬编码模板从「最后兜底」降为「禁止存在于运行时路径」:
标题必须 AI 生成,产不出来就显式失败。**被锁的那个对象整体不存在了**,
六条锁因此不是"改断言"能救的 —— 它们锁的是一条已经拆掉的路。

逐条交代(旧锁 → 去向):

| 旧锁 | 语义 | 去向 |
|---|---|---|
| ① open 端到端 0 问句(兜底链) | 兜底模板池按 open 过滤 | **退役**(兜底链已无模板池) |
| ①b 同上(主链) | 主链修复走兜底模板时同样按形态 | **退役**(主链修复改走 AI 失败梯) |
| ② question 显式 → 兜底全问句 | 过滤方向不接反 | **退役**(同上) |
| ③ auto 池零变化 | 补全模板不污染 auto 池 | **退役**(两张表都没了) |
| ④ 全问句族 + open 仍产非问句 | 补全模板那条路 | **退役** |
| ⑤ 每族两形态都有模板 | 模板池不变量 | **退役** |
| ⑥ 补全模板保关键词锚 / 年份不作开头 | 模板文案约束 | **搬家**:关键词锚由 `assess_title_keyword_alignment`
  对**每一条 AI 标题**复核(`title_ai_only._aligned`);年份口径仍由
  `title_element_contract`(本包禁区,一字未动)管 |

**没有退役的那条不变式**:用户显式指定形态时,链路不许背着他改形态。
这条与模板无关,搬到下面两条断言里继续受锁。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

RETIRED_SYMBOLS = (
    "_fallback_style_title_map",
    "_fallback_form_completion_map",
    "fallback_templates_for_form",
    "_safe_fallback_title",
    "pick_diverse_template",
    "filter_templates_by_form",
    "template_is_question",
)


def test_retired_template_symbols_are_gone_from_runtime():
    """退役实证:六锁锁的那些符号,运行时模块里一个都不存在。"""
    import writing.keyword_topic_generator as ktg
    import writing.title_batch_dedupe as dedupe

    present = [
        name for name in RETIRED_SYMBOLS
        if hasattr(ktg, name) or hasattr(dedupe, name)
    ]
    assert not present, f"退役符号又回到运行时:{present}"


def test_retirement_check_is_not_vacuous():
    """成对反向:同样的检查对**还活着**的符号必须报 present(防恒真)。"""
    import writing.keyword_topic_generator as ktg

    assert hasattr(ktg, "KeywordTopicGenerator"), "hasattr 检查坏了 → 上面那条恒真"


def test_surviving_invariant_user_locked_form_is_never_flipped():
    """搬家后的不变式:用户锁了形态,去重**不许**把标题换成另一种形态。

    这条原来由「兜底模板按形态过滤」承担;模板没了之后,承担者是
    `dedupe_topic_titles(preserve_form=True)` 的 `_same_title_form` 否决。
    """
    from writing.title_batch_dedupe import dedupe_topic_titles

    dup = "深圳装修公司报价怎么算：三项口径先对齐"   # 陈述式
    topics = [
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 0},
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 1},
    ]
    report = dedupe_topic_titles(
        topics,
        # 唯一候选是**问句式** —— 用户锁了 open,它必须被否决。
        candidates_by_index={1: ["深圳装修公司报价怎么算？三项口径先对齐"]},
        preserve_form=True,
    )
    assert report["form_rejected"] >= 1, report
    assert topics[1]["optimized_title"] == dup, "去重背着用户把陈述式换成了问句式"


def test_surviving_invariant_form_guard_can_accept_a_same_form_candidate():
    """成对反向:同形态的合格候选必须被接受(否则上面那条只是"什么都拒")。"""
    from writing.title_batch_dedupe import dedupe_topic_titles

    dup = "深圳装修公司报价怎么算：三项口径先对齐"
    topics = [
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 0},
        {"optimized_title": dup, "original_keyword": "深圳装修公司",
         "article_style": "选购与多品牌比较", "slot_index": 1},
    ]
    report = dedupe_topic_titles(
        topics,
        candidates_by_index={1: ["深圳装修公司验收要看什么：分区清单与常见争议"]},
        preserve_form=True,
    )
    assert report["regenerated"] == 1, report


def test_no_runtime_file_reintroduces_the_retired_template_tables():
    """接线锁:退役符号在整个运行时零定义、零引用(AST 级)。"""
    skip = ("tests", "scripts", "docs", "node_modules", "frontend", ".git",
            "_archive", "prompt_archive", "agent-test-artifacts",
            "agent-test-artifacts-round3")
    offenders = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if any(rel.startswith(f"{d}/") or rel == d for d in skip):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name.rsplit(".", 1)[-1]
            if name in RETIRED_SYMBOLS:
                offenders.setdefault(rel, set()).add(name)
    assert not offenders, f"退役符号被重新接线:{ {k: sorted(v) for k, v in offenders.items()} }"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
