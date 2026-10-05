# -*- coding: utf-8 -*-
"""#91 · 可见文案抽取器自身的判据。

四条禁词判据现在都靠 `tests/v35_visible_copy` 决定「什么算可见」。
它一旦偏松,四条一起变成恒绿而没人知道 —— 所以它必须**两臂都有**:
**该抓的必须抓到**(正样本),**该滤的必须滤掉**(反样本)。

正样本这一臂尤其不能省:今天修的正是「四条判据把注释当文案」的偏严假阳,
修偏严最容易的失手方式就是**顺手把真阳也滤了**
(同族教训:「修假阳时沿同一根轴滑到另一端,真阳一起没」)。
"""
from __future__ import annotations

import pytest

from tests.v35_visible_copy import (
    blank_interpolations,
    strip_comments,
    visible_hits,
    visible_source,
)

# ── 该**抓到**的:词真的露在用户眼前 ───────────────────────────────────
MUST_CATCH = [
    ("JSX 文本", "<TableHead>active</TableHead>", "active"),
    ("JSX 文本带空格", "<div> slug </div>", "slug"),
    ("中文夹英", "<span>全部 active 行业</span>", "active"),
    ("单引号字面量", "toast.error('SKU 不足');", "SKU"),
    ("双引号字面量", 'toast.error("请检查 SKU 配置");', "SKU"),
    ("模板字面量的固定部分", "toast.error(`SKU 校验失败`);", "SKU"),
    ("中文紧邻(无空格)", "toast.error('您的SKU额度不足');", "SKU"),
    ("placeholder 属性", '<Input placeholder="prompt" />', "prompt"),
]

# ── 该**滤掉**的:只是源码里提到 ───────────────────────────────────────
MUST_IGNORE = [
    ("行注释", "// active 表头已中文化", "active"),
    ("块注释", "/* 这里原来显示 slug */", "slug"),
    ("JSX 注释", "{/* P13-v7: 删 slug 显示 · 改成 active badge */}", "slug"),
    ("JSX 注释里的另一个词", "{/* active → 参与跑批 */}", "active"),
    ("模板插值变量名", "toast.error(`跳过 ${skipped} 失败 ${failed}`);", "failed"),
    ("返回类型注解", "function toQuotedSKU(item: RetailCatalogItem): QuotedSKU {", "SKU"),
    ("函数调用", "const r = rechargeSKUs(list);", "SKU"),
    ("字段访问", "const n = row.active;", "active"),
    ("字段定义", "  active: boolean;", "active"),
    ("类型泛型", "const [v, s] = useState<SKU[]>([]);", "SKU"),
    ("import 行", "import { SKU } from '@/types';", "SKU"),
]


@pytest.mark.parametrize("label,line,word", MUST_CATCH, ids=[c[0] for c in MUST_CATCH])
def test_visible_positions_are_caught(label, line, word):
    """🔴 正样本臂:抽取器**必须**在这些位置抓到词。

    少了这一臂,「四条判据全绿」的最可能解释就是抽取器把什么都滤掉了。
    """
    assert visible_hits(line, word), f"[{label}] 该抓没抓到:{line}"


@pytest.mark.parametrize("label,line,word", MUST_IGNORE, ids=[c[0] for c in MUST_IGNORE])
def test_non_visible_positions_are_ignored(label, line, word):
    """反样本臂:这些位置只是「提到」,判成违规就是假阳(今天四条判据全栽在这)。"""
    assert not visible_hits(line, word), f"[{label}] 不该抓却抓了:{line}"


def test_comment_stripping_preserves_line_and_column():
    """保行保列 —— 否则报出来的行号对不上原文,人按行号去看会看到别的东西。"""
    src = "a\n// 注释\nb /* 块 */ c\n{/* jsx */}\n"
    out = strip_comments(src)
    assert out.split("\n") != src.split("\n"), "什么都没剥 —— 剥离器没工作"
    assert len(out) == len(src), "总长度变了 ⇒ 列位置会漂"
    assert len(out.split("\n")) == len(src.split("\n")), "行数变了 ⇒ 行号会漂"
    for a, b in zip(out.split("\n"), src.split("\n")):
        assert len(a) == len(b), f"该行长度变了:{a!r} vs {b!r}"


def test_string_containing_double_slash_is_not_treated_as_comment():
    """🔴 `'https://x'` 里的 `//` 不是注释起点。

    不跟踪字符串状态的话,整行后半截会被抹掉 —— 判据对那一行就此**失明**,
    而且是**偏松**方向的失明:什么都不报,没人会发现。
    """
    line = "toast.error('打开 https://example.com/active 查看 active 状态');"
    assert "active 状态" in visible_source(line), "字符串里的 // 被当成注释了"
    assert visible_hits(line, "active"), "该行被抹掉后判据对它失明"


def test_interpolation_is_blanked_but_literal_part_survives():
    """挖空的只该是 `${}` 内部,固定文本部分必须留着。"""
    line = "toast.error(`SKU ${skuCode} 不足`);"
    out = blank_interpolations(line)
    assert "skuCode" not in out, "插值内部没挖空"
    assert "SKU" in out and "不足" in out, "把固定文案也挖掉了 —— 那会漏报真违规"


def test_extractor_is_not_vacuous():
    """分母自证:抽取器不能把一切都判成不可见(那样四条判据全恒绿)。"""
    caught = sum(1 for _, line, word in MUST_CATCH if visible_hits(line, word))
    assert caught == len(MUST_CATCH), f"正样本只抓到 {caught}/{len(MUST_CATCH)}"
