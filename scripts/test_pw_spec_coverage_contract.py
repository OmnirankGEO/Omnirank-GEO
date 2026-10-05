# -*- coding: utf-8 -*-
"""#88 · 每个 Playwright spec 都必须被至少一个 config 收集。

## 缺陷是什么

`frontend/tests/**` 下有 spec **没有任何 config 会收它** ⇒ 它永远不跑,
而「没跑」与「跑了且通过」在报告里**长得一模一样**。
实测孤儿 1 个:`tests/demo-live-projection/demo-monitoring-live.spec.ts`(6 个用例)。

它的 commit message 写着「已写未跑 · 需集成环境」—— 也就是说**意图存在**,
但**只活在 commit message 里**。树上看它和其他 92 个 spec 长得一样,
`git log` 不是任何人跑判据时会看的东西。本条把那个意图搬到会被执行的地方。

## 🔴 分母的轴

第一版我数的是「testDir 里的 spec 数 > testMatch 点名数」，得 6 个「问题配置」——
**错的**:那 5 个是单 spec 专用配置(`testDir: './tests'` 只是根),其余 spec 由各自配置收。
正确的轴是**每个 spec 被几个 config 收**:0 个才是缺陷。
换轴后 93 个 spec 里孤儿恰好 1 个。

## 重复(≥2 个 config 收同一个 spec)只报不判

已核一例是**有意**的:`demo-customer.spec.ts` 被 `demo-wysiwyg`(Vite dev,验
StrictMode setup→cleanup→setup 生命周期)与 `organization-seats`(7 档视口)各收一次,
用途不同。所以「恰归一个 project」这条口径不成立,改钉「≥1」。
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FE = ROOT / "frontend"
TESTS = FE / "tests"

#: 🔴 冻结例外集 —— 「这个 spec 就是故意不跑的」是一个**需要理由的主张**。
#:    分母仍是机械枚举出的全部 spec;本表只是豁免名单,不是分母。
#:    新增 spec 若没被任何 config 收、又不在这里 ⇒ 红,并要求二选一:接进 config,或登记+写理由。
DELIBERATELY_UNRUN: dict[str, str] = {
    "tests/demo-live-projection/demo-monitoring-live.spec.ts":
        "D10 e2e:已写未跑,需要集成环境(真监测投影链)。"
        "原意图只写在 commit 40b49d32a 的 message 里 —— 搬到这里才有人执行。"
        "接进 CI 的条件:有可用集成环境后建 playwright.demo-live-projection.config.ts 并从本表移除。",
}

SPEC_RE = re.compile(r"\.(spec|test)\.(ts|tsx|js|cjs|mjs)$")
CFG_RE = re.compile(r"^playwright.*\.config\.(ts|cjs|mjs|js)$")


def _read(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8", errors="replace").read()


def all_specs() -> list[str]:
    out = []
    for p in TESTS.rglob("*"):
        if p.is_file() and SPEC_RE.search(p.name):
            out.append(p.relative_to(FE).as_posix())
    return sorted(out)


def coverage() -> dict[str, list[str]]:
    """每个 spec 被哪些 config 收集。

    判定复刻 playwright 的口径(够用即可,不求完整):
      testDir 圈定目录;有**字符串字面量** testMatch ⇒ 只收点名的那个;否则该目录下全收。
    正则形态的 testMatch 视作全收 —— 会**高估**覆盖,所以本条只用来找「0 覆盖」,
    高估只会让它漏报、不会误报(方向安全)。
    """
    specs = all_specs()
    cover: dict[str, list[str]] = {s: [] for s in specs}
    cfgs = sorted(p.name for p in FE.iterdir() if p.is_file() and CFG_RE.match(p.name))
    assert cfgs, f"一个 playwright config 都没找到 —— 分母塌了,先查路径:{FE}"
    for c in cfgs:
        src = _read(FE / c)
        dirs = re.findall(r"testDir:\s*['\"`]([^'\"`]+)['\"`]", src)
        lits = re.findall(r"testMatch:\s*['\"`]([^'\"`]+)['\"`]", src)
        for d in dirs:
            rel = d.lstrip("./").rstrip("/")
            for s in specs:
                if not s.startswith(rel + "/"):
                    continue
                if not lits or any(s.endswith("/" + m) or s.split("/")[-1] == m for m in lits):
                    cover[s].append(c)
    return cover


# ══ ① 主锁:没有孤儿 spec ═══════════════════════════════════════════
def test_every_spec_is_collected_by_at_least_one_config():
    cov = coverage()
    assert cov, "spec 分母为空 —— 不是「没有孤儿」,是什么都没量到"
    orphans = sorted(s for s, v in cov.items() if not v)
    unexplained = [s for s in orphans if s not in DELIBERATELY_UNRUN]
    assert not unexplained, (
        "这些 spec **没有任何 config 会收集它们**,永远不跑:\n    "
        + "\n    ".join(unexplained)
        + "\n    「没跑」与「跑了且通过」在报告里长得一模一样。\n"
          "    二选一:①接进某个 config 的 testDir/testMatch;\n"
          "            ②登记进 `DELIBERATELY_UNRUN` 并写清**为什么不跑、什么条件下接进来**。\n"
          "    不许沉默留着。")


def test_the_exemption_list_has_no_stale_entries():
    """🔴 反向:登记表里的 spec 若已经被 config 收了,或文件已删,必须清理。

    留一条指向「其实已经在跑」的豁免 = 登记表在说谎;
    留一条指向不存在文件的豁免 = 下一个人以为还有个 spec 待接。
    """
    cov = coverage()
    for s, why in DELIBERATELY_UNRUN.items():
        assert (FE / s).exists(), f"豁免登记指向不存在的文件:{s}"
        assert not cov.get(s), (
            f"{s} 已经被 config 收集了({cov[s]}),但还留在 `DELIBERATELY_UNRUN` 里 —— "
            f"同笔删掉这条登记。")
        assert len(why) > 20, f"{s} 的豁免理由太短,等于没写"


# ══ ② 分母自证:轴对不对 ═══════════════════════════════════════════
def test_the_denominator_is_the_spec_set_not_the_config_set():
    """🔴 第一版我数的是「testDir 里 spec 数 > testMatch 点名数」,得 6 个「问题配置」——

    那 5 个是单 spec 专用配置(`testDir: './tests'` 是根目录),完全正常。
    本条钉住分母的**量纲**:分母是 spec,不是 config;数出来的量级要对得上盘。
    """
    specs = all_specs()
    assert len(specs) > 50, f"只枚举到 {len(specs)} 个 spec —— 分母太小,先查 rglob 路径"
    cfgs = [p.name for p in FE.iterdir() if p.is_file() and CFG_RE.match(p.name)]
    assert len(cfgs) > 20, f"只枚举到 {len(cfgs)} 个 config"
    cov = coverage()
    assert set(cov) == set(specs), "覆盖表的键集与 spec 全集不等 —— 有 spec 没进分母"


def test_duplicate_collection_is_reported_not_failed():
    """重复收集只观察不判红 —— 已核一例是**有意**的。

    `demo-customer.spec.ts` 被 `demo-wysiwyg`(Vite dev · 验 StrictMode 生命周期)
    与 `organization-seats`(7 档视口)各收一次,用途不同。
    所以「每个 spec 恰归一个 project」这条口径**不成立**,本组钉的是「≥1」。
    这条只保证:真出现重复时,数据拿得到、不是隐形的。
    """
    dup = {s: v for s, v in coverage().items() if len(v) > 1}
    assert isinstance(dup, dict)      # 不判红,只确保这条信息可得
    for s, v in dup.items():
        assert len(set(v)) == len(v), f"{s} 被同一个 config 重复计了:{v}"
