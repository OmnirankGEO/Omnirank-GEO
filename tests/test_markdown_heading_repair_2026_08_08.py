"""W1 返工:粗体伪标题 → `##` 局部修复器的行为锁。

这一份只锁**修复器自己**的行为。它是否真的接在了落库路径上,由
`tests/test_spec_card_real_h2_2026_08_08.py` 走真入口锁 —— 两份分工不要混:
函数对了但没接线,是这个仓库里已经犯过三次的错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writing.article_writer import H2_PATTERN, MIN_H2_HEADINGS  # noqa: E402
from writing.markdown_heading_repair import (  # noqa: E402
    HEADING_REPAIR_VERSION,
    MAX_HEADING_CHARS,
    count_real_h2,
    heading_repair_note,
    repair_pseudo_headings,
)


def _doc(*body_lines: str) -> str:
    """构造一篇「有 H1、正文里是伪标题」的文章。"""
    return "# 某行业服务商怎么选\n\n导语。\n\n" + "\n\n".join(body_lines) + "\n"


# --- 核心行为 -------------------------------------------------------------

def test_bold_only_line_becomes_real_h2():
    out, repaired = repair_pseudo_headings(_doc("**怎么核验资质**", "正文一段。"))
    assert repaired == ["怎么核验资质"]
    assert "## 怎么核验资质" in out
    assert "**怎么核验资质**" not in out


def test_repaired_body_is_countable_by_the_same_regex_h4_uses():
    """修完必须能被 H4 那条正则数到 —— 这才是修复的**目的**。"""
    body = [f"**第 {i} 节标题**" for i in range(MIN_H2_HEADINGS)]
    raw = _doc(*body)
    assert len(H2_PATTERN.findall(raw)) == 0
    out, _ = repair_pseudo_headings(raw)
    assert len(H2_PATTERN.findall(out)) == MIN_H2_HEADINGS


def test_threshold_is_imported_not_redefined():
    """门槛必须是 H4 那一份。写第二个 6 就是第二套数字逻辑。"""
    import writing.article_writer as aw
    import writing.markdown_heading_repair as mhr

    assert mhr.MIN_H2_HEADINGS is aw.MIN_H2_HEADINGS
    src = Path(mhr.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]  # 去掉模块 docstring(里面允许出现说明性数字)
    assert "MIN_H2_HEADINGS: " not in body, "修复器自己重新定义了门槛"


def test_h2_pattern_is_shared_with_the_validator():
    """正则也必须是 H4 那一份。

    🔴 只写 `mhr.H2_PATTERN is aw.H2_PATTERN` 是**零判别力**的:`re.compile` 自带缓存,
    同 pattern 同 flags 编译两次返回的就是同一个对象,`is` 恒真。
    (变异 W08「修复器自己另写一份 H2 正则」当场存活,才逼出这条。)
    真正能判别的是源码级:本模块除了那行 import 之外,不许出现第二处 H2_PATTERN 定义。
    """
    import writing.article_writer as aw
    import writing.markdown_heading_repair as mhr

    assert mhr.H2_PATTERN is aw.H2_PATTERN
    src = Path(mhr.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]
    assert "H2_PATTERN = " not in body, "修复器自己又编了一份 H2 正则"
    assert "from .article_writer import" in body and "H2_PATTERN" in body


# --- 「局部」闸:已经有真小标题的文章一律不碰 ------------------------------

def test_article_with_enough_real_headings_is_left_untouched():
    rich = "# T\n\n" + "\n\n".join(
        f"## 真小标题 {i}\n\n**这是正常的行内强调**" for i in range(MIN_H2_HEADINGS)
    )
    assert count_real_h2(rich) >= MIN_H2_HEADINGS
    out, repaired = repair_pseudo_headings(rich)
    assert repaired == []
    assert out is rich, "未修改时应原样返回同一对象,便于调用方判断有没有真的动过"


def test_just_below_threshold_still_repairs():
    """门槛的反向对照:差一个就该修 —— 证明这条闸是按门槛判的,不是恒不修。"""
    body = "# T\n\n" + "\n\n".join(f"## 真标题 {i}" for i in range(MIN_H2_HEADINGS - 1))
    body += "\n\n**再补一个伪标题**\n"
    out, repaired = repair_pseudo_headings(body)
    assert repaired == ["再补一个伪标题"]
    assert count_real_h2(out) == MIN_H2_HEADINGS


# --- 保守约束:宁可漏修不可错改 -------------------------------------------

@pytest.mark.parametrize(
    "line, why",
    [
        ("- **要点**:这是列表项", "列表项里的加粗不是标题"),
        ("正文里出现 **强调词** 不该被动", "行内强调"),
        ("**结论:**", "冒号结尾是引导语"),
        ("**这句话是完整的一句陈述。**", "句号结尾是句子"),
        ("**" + "长" * (MAX_HEADING_CHARS + 1) + "**", "超长更像被整行加粗的句子"),
        ("| **表格里的粗体** |", "表格行"),
        ("**带*星号*的**", "嵌套星号形态不确定"),
    ],
)
def test_conservative_cases_are_not_touched(line, why):
    out, repaired = repair_pseudo_headings(_doc(line, "正文。"))
    assert repaired == [], f"不该改:{why}"
    assert out is not None


def test_code_fence_contents_are_never_touched():
    raw = _doc("```", "**围栏里的粗体**", "```", "正文。")
    out, repaired = repair_pseudo_headings(raw)
    assert repaired == []
    assert "**围栏里的粗体**" in out


def test_first_nonblank_line_is_title_territory():
    """第一个非空行归 `_normalize_article_title_and_h1`,两边抢会写出重复标题。"""
    raw = "**这看起来像标题但它是第一行**\n\n正文。\n"
    out, repaired = repair_pseudo_headings(raw)
    assert repaired == []
    assert out is raw


# --- 留痕 -----------------------------------------------------------------

def test_repair_note_records_every_converted_title():
    out, repaired = repair_pseudo_headings(_doc("**甲节**", "文。", "**乙节**", "文。"))
    note = heading_repair_note(repaired)
    assert note["version"] == HEADING_REPAIR_VERSION
    assert note["converted_count"] == 2
    assert note["converted_titles"] == ["甲节", "乙节"]
    assert "## 甲节" in out and "## 乙节" in out


# --- 「整篇只有 ###」那一种(真跑实证逼出来的) --------------------------

def test_orphan_h3_layer_is_promoted_to_h2():
    """不带格式要求的那一臂真跑出来的形态:规规矩矩的 `### 一、…`,但 H4 一条数不到。"""
    from writing.markdown_heading_repair import promote_orphan_h3

    raw = "## 文章标题\n\n导语。\n\n" + "\n\n".join(
        f"### {name}\n\n正文。" for name in [f"第 {i} 节" for i in range(MIN_H2_HEADINGS)]
    )
    assert count_real_h2("\n".join(raw.splitlines()[1:])) == 0
    out, promoted = promote_orphan_h3(raw)
    assert promoted == MIN_H2_HEADINGS
    assert count_real_h2("\n".join(out.splitlines()[1:])) == MIN_H2_HEADINGS
    assert "### " not in out


def test_mixed_heading_hierarchy_is_never_flattened():
    """只要正文里已经有 `##`,层级就是作者的选择,一个 `###` 都不许提级。"""
    from writing.markdown_heading_repair import promote_orphan_h3

    raw = "# 标题\n\n## 第一章\n\n" + "\n\n".join(f"### 小节 {i}" for i in range(10))
    out, promoted = promote_orphan_h3(raw)
    assert promoted == 0
    assert out is raw


def test_too_few_h3_is_left_alone():
    """`###` 条数不够门槛时,它更可能只是几个注解,不是整层结构。"""
    from writing.markdown_heading_repair import promote_orphan_h3

    raw = "# 标题\n\n正文。\n\n" + "\n\n".join(
        f"### 注 {i}" for i in range(MIN_H2_HEADINGS - 1)
    )
    out, promoted = promote_orphan_h3(raw)
    assert promoted == 0
    assert out is raw


def test_save_chain_records_h3_promotion(monkeypatch):
    from writing.markdown_heading_repair import repair_article_for_save

    body = "## 标题\n\n导语。\n\n" + "\n\n".join(
        f"### 第 {i} 节\n\n正文。" for i in range(MIN_H2_HEADINGS)
    )
    art = {"content": body}
    repair_article_for_save(art)
    note = art["quality_warning"]["heading_repair"]
    assert note["promoted_h3_count"] == MIN_H2_HEADINGS
    assert "### " not in art["content"]


def test_empty_and_none_are_safe():
    assert repair_pseudo_headings("") == ("", [])
    assert repair_pseudo_headings(None) == (None, [])


def test_trailing_newline_preserved():
    raw = _doc("**甲节**", "文。")
    assert raw.endswith("\n")
    out, repaired = repair_pseudo_headings(raw)
    assert repaired
    assert out.endswith("\n")
