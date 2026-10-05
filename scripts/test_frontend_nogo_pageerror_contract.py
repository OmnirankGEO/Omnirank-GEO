# -*- coding: utf-8 -*-
"""#89 · frontend-nogo 每个 spec 都必须接上「页面零崩溃」断言。

## 缺陷是什么(实测三档)

| 档 | 数量 | 说明 |
|---|---|---|
| 装了监听 **且有断言** | 1 | `wallet-auth.spec.ts`(两处 `expect(pageErrors).toEqual([])`) |
| 装了监听 **但无断言** | 1 | `defgeo-five-card-fallback.spec.ts` |
| 没装 | 12 | —— |

🔴 **中间那一档最危险:装了不断言,与根本没装,在报告里完全同形。**
监听把崩溃收下来了,然后没有任何东西去看它 —— 「页面崩了」既没让用例红,
也没出现在任何输出里。

`defgeo-five-card-fallback.spec.ts:58` 自己的注释说得很准:

> 页面崩进错误边界时,后面每一条断言都报「element(s) not found」——
> 与「元素确实没渲染」完全同形,会把人引去改被测组件。

它收集 pageerror 是为了**失败时念出真因**,不是为了让崩溃本身判红。两个目的都要。

## 修法:fixture 而不是逐个用例接线

`tests/frontend-nogo/_fixtures.ts` 用 `auto: true` 的 fixture,
一行 import 覆盖全部 164 个用例;崩溃既**附到报告**也**断言为空**。
所以本门钉的是「**每个 spec 都从 `./_fixtures` 取 test**」——
只要它没从那里取,断言就没接上,而那件事没有任何别的痕迹。
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOGO = ROOT / "frontend" / "tests" / "frontend-nogo"
FIXTURES = NOGO / "_fixtures.ts"

#: 🔴 冻结例外集 —— 大小钉死为 0。
#:    「这个 spec 就是不该接零崩溃断言」是一个**需要理由的主张**,
#:    不许靠「它恰好没 import」这种沉默来表达。
NOT_WIRED_BY_DESIGN: frozenset[str] = frozenset()


def specs() -> list[pathlib.Path]:
    return sorted(p for p in NOGO.glob("*.spec.ts"))


def _read(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8", errors="replace").read()


def test_the_fixture_module_exists_and_asserts_zero_page_errors():
    assert FIXTURES.exists(), f"共享 fixture 不在:{FIXTURES}"
    s = _read(FIXTURES)
    assert "page.on('pageerror'" in s or 'page.on("pageerror"' in s, \
        "fixture 没有装 pageerror 监听 —— 它守不住任何东西"
    assert "auto: true" in s, (
        "fixture 不是 `auto: true` —— 那就要求每个用例显式声明,"
        "而「忘了声明」与「没有崩溃」在报告里同形,等于把这道断言的接线交给人记。")
    assert re.search(r"expect\(\s*errors[\s\S]{0,400}?\)\.toEqual\(\s*\[\s*\]\s*\)", s), (
        "🔴 fixture 收集了 pageerror 却**没有断言它为空** —— "
        "那正是本门要治的那一档(装了不断言 = 没装)。")


def test_every_spec_is_wired_to_the_shared_fixture():
    """分母 = `frontend-nogo/*.spec.ts` 机械枚举,不是手写清单。

    新加一个 spec 而没接 fixture ⇒ 红。这正是「零崩溃」这句话此前**没有执行者**的形态:
    规矩写在别处,而新文件默认不受它约束。
    """
    all_specs = specs()
    assert len(all_specs) >= 10, f"只枚举到 {len(all_specs)} 个 spec —— 分母塌了,先查路径 {NOGO}"
    bad = []
    for p in all_specs:
        s = _read(p)
        m = re.search(r"^import \{[^}]*\btest\b[^}]*\} from '([^']+)';", s, re.M)
        src = m.group(1) if m else None
        if src != "./_fixtures":
            bad.append(f"{p.name}(从 {src!r} 取 test)")
    unexplained = [b for b in bad if b.split("(")[0] not in NOT_WIRED_BY_DESIGN]
    assert not unexplained, (
        "这些 spec **没有**从共享 fixture 取 `test`,零崩溃断言在它们身上没接上:\n    "
        + "\n    ".join(unexplained)
        + "\n    改成 `import { expect, test, … } from './_fixtures';`。\n"
          "    「装了监听不断言」与「根本没装」在报告里同形 —— 所以这里钉的是**接线**,不是「有没有提到 pageerror」。")


def test_no_spec_is_exempted_without_a_written_reason():
    assert NOT_WIRED_BY_DESIGN == frozenset(), (
        f"有人往 `NOT_WIRED_BY_DESIGN` 里加了 {sorted(NOT_WIRED_BY_DESIGN)} 却没改本条。\n"
        f"    要加就同笔写清:这个 spec 为什么不需要零崩溃断言。")


def test_the_exception_annotation_requires_a_reason():
    """例外机制(`allow-pageerror` 标注)本身必须要求写理由。

    否则「关掉这道断言」会变成一个**不留痕迹**的动作。
    """
    s = _read(FIXTURES)
    assert "allow-pageerror" in s, "fixture 没有提供例外机制 —— 故意测错误边界的用例会被迫改写"
    assert "description" in s and "toBeGreaterThan(0)" in s, (
        "🔴 例外没有强制写理由 —— 没有理由的例外等于把断言关掉,而关掉这件事不会留下痕迹。")
