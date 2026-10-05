# -*- coding: utf-8 -*-
"""WO_262 · 收尾卡 summary 被存成「列表字符串」⇒ 客户看到「第 4 张少了:['①…','②…']」。

链路(全在 a0719f771 上实测):
  `content_generator.py:210` 出参模板写 `"summary": "2-4 行小结"` ⇒ 模型常按"行"回**数组**
  → `card_templates.clip_text` 第一行是 `str(text or "")` ⇒ `"['① …', '② …']"` 落库
  → `build_closing_prompt` 把这串印进出图 prompt
  → `frozen_text_lines("closing")` 把它当"必须逐字出现"的一行
  → OCR 当然找不到(模型不会照印方括号)⇒ 前端把原串回显给**客户**。
生产 41 条图文里 **8 条**中招(含 09-22 正在投诉的 post 43)。

🔴 修在**唯一截断入口** `clip_text` 里,不在 40 个调用点各写一次 ——
   那种写法迟早有一处跟不上,而跟不上的那一处不会报错,只会把方括号送到客户眼前。
🔴 因此归一化天然是**读侧**的:存量那 8 条 meta 里存的仍是旧串,
   重抽时照样从 `clip_text` / `frozen_text_lines` 走一遍 ⇒ 出来的就是干净的。
   **不回写库**(Owner 存量口径)。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.geo_douyin.card_templates import (  # noqa: E402
    CLOSING_TEXT_MAX, StyleTokens, build_closing_prompt, clip_text,
    flatten_text, frozen_text_lines, looks_like_list_repr,
)

#: 生产里真实存着的那种串(**Python repr,单引号** —— 不是合法 JSON,
#: `json.loads` 解不了,所以归一化必须也认 `ast.literal_eval`)。
LEGACY_REPR = "['① 先列预算表:装修/设备各多少', '② 再按需求筛:先看资质再看案例']"
AS_LIST = ["① 先列预算表:装修/设备各多少", "② 再按需求筛:先看资质再看案例"]


# ══════════════════════════════════════════════════════════════════
# 一、归一化:三种形状
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,label", [
    (AS_LIST, "list"),
    ([["① 先列预算表:装修/设备各多少"], ["② 再按需求筛:先看资质再看案例"]], "嵌套 list"),
    (LEGACY_REPR, "存量 repr 串"),
    ('["① 先列预算表:装修/设备各多少", "② 再按需求筛:先看资质再看案例"]', "JSON 串"),
])
def test_all_three_shapes_become_one_multiline_text(raw, label):
    """🔴 三形状(外加 JSON 串)都归一成**按换行拼**的一段。

    用换行不用「、」:小结本来就是多行,拼成一行会挤成一坨上不了版。
    """
    got = flatten_text(raw)
    assert "[" not in got and "'" not in got, "%s 没归一干净:%r" % (label, got)
    assert got.count("\n") == 1, "%s 应当是两行:%r" % (label, got)
    assert got.startswith("① 先列预算表")


@pytest.mark.parametrize("raw", [
    "[重要] 请注意:这不是列表",
    "（含税）",
    "普通一行小结",
    "",
])
def test_normal_text_is_not_touched(raw):
    """🔴 反臂:正常文案**一个字都不许动**。

    少了这一条,把归一化写成"凡是带方括号就砍掉"也能让上面那些绿 ——
    而那会吃掉「[重要] 请注意」这种真文案。
    """
    assert flatten_text(raw) == raw


def test_none_becomes_empty_not_the_string_none():
    assert flatten_text(None) == ""


@pytest.mark.parametrize("raw,expect", [
    (LEGACY_REPR, True),
    ("['a', 'b']", True),
    ('["a", "b"]', True),
    ("[重要] 请注意", False),
    ("（含税）", False),
    ("普通一行", False),
    # 🔴 上面三条负例其实都被**正则**挡在门外(没有以括号收尾 / 是全角括号),
    #    也就是说「里面要有元素分隔符」那一半**一格都没测到** ——
    #    注毒把它改成 `return True` 时读数是 ALIVE,这把尺子当时才露出来。
    #    下面两条**过得了正则**,专门考第二半。
    ("(含税, 含运费)", False),
    ("[见下表]", False),
])
def test_the_exit_predicate_is_narrow_enough(raw, expect):
    """🔴 出口锁的谓词要**收窄**:两端括号 **且** 里面有元素分隔符。

    只看括号会把「[重要] 请注意」误判 —— 一个开始乱叫的判据,
    下一个人只会把它放宽,放宽之后它就再也不会为真原因转红。
    """
    assert looks_like_list_repr(raw) is expect


# ══════════════════════════════════════════════════════════════════
# 二、唯一截断入口
# ══════════════════════════════════════════════════════════════════

def test_clip_text_is_where_it_is_fixed():
    """🔴 修在 `clip_text` 里 —— 它是全链唯一截断入口,40 个调用点全受益。

    注毒把这一行改回裸 `str()` ⇒ 本格与下面几格一起红(工单点名的反臂)。
    """
    got = clip_text(AS_LIST, CLOSING_TEXT_MAX)
    assert "[" not in got, got
    assert "① 先列预算表" in got and "② 再按需求筛" in got


# ══════════════════════════════════════════════════════════════════
# 三、出口:冻结文案与出图 prompt
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("summary,label", [(AS_LIST, "新产出"), (LEGACY_REPR, "存量 meta")])
def test_frozen_lines_have_no_bracket_and_split_per_line(summary, label):
    """🔴 冻结文案是 OCR 的"标准答案":有方括号 = 必然核不到 = 客户看到那串。

    拆成**逐行**是为了「少了哪一行」能逐条说出来,而不是甩一整段回去。
    """
    lines = frozen_text_lines("closing", {
        "headline": "选购总结", "summary": summary, "caveat": "以实际沟通为准",
    })
    assert not any(looks_like_list_repr(x) for x in lines), "%s:%r" % (label, lines)
    assert not any("[" in x for x in lines), "%s:%r" % (label, lines)
    body = [x for x in lines if x.startswith("①") or x.startswith("②")]
    assert len(body) == 2, "%s 应当拆成两行:%r" % (label, lines)


def test_the_redraw_path_on_legacy_meta_is_clean():
    """🔴 [b7 09-22 补] **存量重抽必须走新归一化**。

    Owner 口径是「修复完让他重新抽就行」,而 post 43 的 meta 里存的仍是旧串 ——
    所以归一化必须做在**读侧**(本单正是:`clip_text` / `frozen_text_lines` 入口),
    而不是重抽时把值回写进 `generation_meta`(那算改存量数据,不做)。

    这一格拿**存量形状**的 meta 走一次出图 prompt + 冻结文案,两边都不许有 `[`。
    """
    prompt = build_closing_prompt(
        headline="选购总结", summary=LEGACY_REPR, style=StyleTokens(),
        caveat="以实际沟通为准", contact_line="详询门店",
    )
    assert "[" not in prompt and "']" not in prompt, prompt[-300:]
    assert "① 先列预算表" in prompt, "归一化把正文也弄丢了"

    lines = frozen_text_lines("closing", {"headline": "选购总结", "summary": LEGACY_REPR})
    assert not any("[" in x for x in lines), lines


# ══════════════════════════════════════════════════════════════════
# 四、prompt 说什么、解析就收什么
# ══════════════════════════════════════════════════════════════════

def test_the_output_template_says_the_shape_it_now_accepts():
    """🔴 出参模板与归一化**同批改**:模板要是还写「2-4 行小结」,
    模型就会继续在"一段"和"一个数组"之间摇摆,而我们只是把后者兜住了。
    兜住不等于说清楚 —— 说清楚能少一次摇摆。
    """
    src = (REPO / "services" / "geo_douyin" / "content_generator.py").read_text(encoding="utf-8")
    i = src.index('"closing": {{')
    line = src[i: src.index("\n", i)]
    assert '"summary": ["' in line, "模板没改成数组形状:%s" % line
    assert "2-4 行小结" not in line, "旧的模糊说法还在:%s" % line
