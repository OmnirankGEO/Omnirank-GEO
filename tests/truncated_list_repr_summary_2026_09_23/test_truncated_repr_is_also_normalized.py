# -*- coding: utf-8 -*-
"""WO_265 · 存量 summary 是**被截断的**列表 repr 时,WO_262 的归一化不生效。

WO_262 我把 `looks_like_list_repr` 写成"**两端**都是括号才算"。
而存量那批 repr 不是 `str(list)` 原样落库,是又经过**旧** `clip_text`
按句边界截断过的 —— **闭合的 `]` 被切掉了**。于是:

  谓词 False → `flatten_text` 原样返回 → 整串当一行"必须逐字出现" →
  OCR 缺失项 = 整串 → 客户看到 `['…', '…'`;
  **出口锁用的也是这个谓词,所以连一句 warning 都不出**;
  `build_closing_prompt` 同源,重抽会把 repr 再印一次上图。

生产 8 条存量里 **3 条**是这种(43 客户 / 41 / 40 测试),
也就是说 Owner 对客户说的「修复完让他重新抽就行」在 post 43 上**落不了地**。

🔴 教训写在这儿:**谓词只认"完整形状"时,残缺的同类反而全部漏网** ——
   而残缺恰恰是"被别的环节处理过"的痕迹,比完整形状更常见。
"""
from __future__ import annotations

import base64
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.geo_douyin.card_templates import (  # noqa: E402
    StyleTokens, build_closing_prompt, clip_text, flatten_text,
    frozen_text_lines, looks_like_list_repr,
)

#: 生产 post 40 的**原串**(Review 从车尖导出,base64 见 WO_265)。
#: 用 base64 是为了让夹具**逐字节**等于生产那一条 —— 手抄会掉标点。
POST40 = base64.b64decode(
    "WyfmsqHmnInnu5/kuIDku7cs5L2G5ouG5oiQ6K+K5pat44CB5YaF5a6544CB5Y+R5biD44CB"
    "55uR5rWL5Zub5Z2X6ZeuLOaKpeS7t+WwseiXj+S4jeS9j+S6hicsICflho3pl67nm5HmtYvl"
    "gZrlpJrkuYXjgIHlpJrkuYXlpI3nm5jkuIDmrKEs5rKh55uR5rWL55qE562J5LqO5Y+q5Lmw"
    "5LiA5qyh5b+r54WnJw=="
).decode("utf-8")

#: post 41 **同形**(尾巴被切在元素中间、连收尾引号都没有)。
#: 🔴 按工单要求**自造**,不抄客户原文 —— 判据里不留真客户的正文。
POST41_SHAPED = ("['先问清楚开票方式,别等交付完才发现开不了专票', "
                 "'再确认发票抬头和税号能不能改,有的家只能开发票")

#: post 43 同形(尾巴有收尾引号,和 40 一样但更短)。同样自造。
POST43_SHAPED = "['先看采光和朝向,样板间的灯光会骗人', '再问清楚公摊怎么算,别只听套内采光'"

CLOSED = "['① 先列预算表:装修/设备各多少', '② 再按需求筛:先看资质再看案例']"

TRUNCATED = [
    pytest.param(POST40, id="post40-生产原串"),
    pytest.param(POST41_SHAPED, id="post41同形-尾切在元素中间"),
    pytest.param(POST43_SHAPED, id="post43同形-尾有引号"),
]


# ══════════════════════════════════════════════════════════════════
# 一、谓词:认得出残缺,且仍然窄
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw", TRUNCATED)
def test_a_truncated_repr_is_recognised(raw):
    """🔴 本单的全部意义:**没有闭合括号**的那一类也要认出来。"""
    assert looks_like_list_repr(raw) is True


def test_the_closed_shape_still_recognised():
    """🔴 既有形状零变化 —— 扩谓词不许把老的那类漏掉。"""
    assert looks_like_list_repr(CLOSED) is True


@pytest.mark.parametrize("raw", [
    "[重要] 请注意", "(含税)", "[1] 第一步", "(含税, 含运费)", "[见下表]", "普通一行",
    # 🔴 上面六条**都靠"尾巴不是引号"那一半**挡住 —— 也就是说
    #    「括号后必须紧跟引号」那一半一格都没测到(注毒把 HEAD 正则放宽时读数 ALIVE)。
    #    这一条专门隔离它:开头是括号、里面**有** `', '` 分隔,但括号后跟的是字。
    "[备注] '含税', '含运' 见下表",
])
def test_normal_text_is_still_not_touched(raw):
    """🔴 反臂(工单点名三条 + 我 WO_262 那两条):括号后**紧跟引号**才算。

    放宽成"开头是括号就算"会吃掉「[1] 第一步」这种真文案 ——
    而一个开始乱叫的判据,下一个人只会把它整条关掉。
    """
    assert looks_like_list_repr(raw) is False
    assert flatten_text(raw) == raw


# ══════════════════════════════════════════════════════════════════
# 二、归一化:切得开,且尾部半句留着
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw", TRUNCATED)
def test_a_truncated_repr_splits_into_lines_without_brackets(raw):
    got = flatten_text(raw)
    assert got != raw, "没归一"
    assert "[" not in got and "'" not in got, got
    assert got.count("\n") == 1, "应当两行:%r" % got


def test_the_half_cut_tail_is_kept_as_its_own_line():
    """🔴 尾巴被切在元素中间时**保留成一行**,不丢。

    它本来就会被印上图 —— 半句话比一整串带方括号的 repr 强得多。
    少了这一条,把归一化写成"丢掉最后一个不完整元素"也能让上面那些绿。
    """
    got = flatten_text(POST41_SHAPED)
    assert got.endswith("有的家只能开发票"), got


def test_commas_inside_a_sentence_are_not_split_points():
    """🔴 **不能只按逗号切**:中文正文里本来就有「,」。

    post 40 两条元素里各有一个中文逗号,按逗号切会切成四行。
    """
    got = flatten_text(POST40)
    assert got.count("\n") == 1, got
    assert "报价就藏不住了" in got.split("\n")[0]


# ══════════════════════════════════════════════════════════════════
# 三、两个出口:OCR 答案 与 出图 prompt(同源)
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw", TRUNCATED)
def test_frozen_lines_have_no_list_head(raw):
    """🔴 工单验收点:三条各得 ≥2 行,且**没有一行以 `['` / `["` 开头**。"""
    lines = frozen_text_lines("closing", {"headline": "选购总结", "summary": raw})
    assert len(lines) >= 2, lines
    assert not any(x.startswith(("['", '["')) for x in lines), lines
    assert not any(looks_like_list_repr(x) for x in lines), lines


def test_the_redraw_prompt_and_the_ocr_answer_come_from_the_same_place():
    """🔴 出图侧与 OCR 侧**同源**:一条锁证明两边走的是同一个 `flatten_text`,
    别各写一套(各写一套就会有一天只修好一边,而两边各自看都"对")。
    """
    prompt = build_closing_prompt(headline="选购总结", summary=POST40,
                                  style=StyleTokens(), caveat="以实际沟通为准")
    assert "['" not in prompt and '["' not in prompt, prompt[-260:]
    first_line = flatten_text(POST40).split("\n")[0]
    assert first_line[:12] in prompt, "prompt 里没有归一化之后的正文"
    # 同源自证:prompt 里那段正文与 frozen 行取自同一次归一化
    lines = frozen_text_lines("closing", {"headline": "选购总结", "summary": POST40})
    assert any(first_line[:12] in x for x in lines)


def test_clip_text_is_still_the_single_entrance():
    """🔴 WO_262 的结构不变:改动仍然只落在**唯一截断入口**那一层。"""
    got = clip_text(POST40, 200)
    assert "[" not in got and "'" not in got, got
