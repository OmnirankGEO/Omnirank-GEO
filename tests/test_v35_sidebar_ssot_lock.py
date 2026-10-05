# -*- coding: utf-8 -*-
"""#91 · 侧栏标签必须走术语 SSOT —— `test_w5_8_sidebar_rename` 的**重锚**继任者。

## 为什么原锁必须换掉,而不是收紧阈值

原锁问「八个字面串在不在 `AppSidebar.tsx` 里」。而正确做法是标签从 SSOT
(`v35Terminology.ts` 的 `SIDEBAR_LABELS` + `sidebarLabel()`)取 ——
**做对了字面量就不在那个文件**。于是:

    基线(没做)          2/8  → 红
    接完 SSOT(做对了)   1/8  → **更红**

锁与正确做法**互斥**:它奖励硬编码、惩罚 SSOT。这不是阈值松紧问题,是锚错了。
另外原锁 `found == 0 ⇒ pytest.skip`,让「**完全没做**」与「**做对了**」同落 skip,
只罚「做一半」—— 三档压成一档,而且压掉的正是两个极端。

## 🔴 期望值**从模块读**,判据里不留第二份

A 刚栽在这儿:D4 那条读的是**判据内的拷贝**,于是 SSOT 改成禁词照样绿。
本文件所有「八个现行名」一律现场解析 `v35Terminology.ts` 得到,不手写清单。

代价要说清:名字**内容**对不对,这把锁答不了(它没有独立事实源可比)。
它锁的是**结构**:数量、禁词、每条都有消费方、侧栏里零硬编码。
内容正确性由 A 的实现与 Review 的文案裁定负责。这条边界写出来,
免得下一个人以为「锁绿了 = 名字都对」。

## 四道闸(Review 2026-09-06 重锚裁定)

① `SIDEBAR_LABELS` 值集合:数量 == 8,每个值不含 `额度|积分|代理`;
② `AppSidebar.tsx` 真**接线**:import 了 `sidebarLabel`,且**每个** key 都被
   `sidebarLabel('<key>')` 调用(不是 import 顶住 —— 只查 import 的话,
   「import 了但一处没调」照样绿);
③ `AppSidebar.tsx` 去注释源里 `label: '<名>'` 形态**零硬编码** ——
   新名旧名都禁。只禁旧名会被「把新名直写进侧栏、SSOT 没人用」骗过;
④ 找不到 SSOT 文件、或 `SIDEBAR_LABELS` 解析为空 ⇒ **红**,不 skip。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.v35_visible_copy import strip_comments

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"
SSOT = FE / "lib" / "v35Terminology.ts"
SIDEBAR = FE / "components" / "layout" / "AppSidebar.tsx"

#: 禁词。「代理」是 2026-09-06 补裁(对外统一称「服务商」);
#: 「额度 / 积分」是全站「算力」口径(CLAUDE.md · AppSidebar 头注释都写着)。
FORBIDDEN = re.compile(r"额度|积分|代理")

#: 🔴 **历史**名单 —— 只有这一份是手写的,而且它不会烂:
#:    这些名字已经退役,不会再变。现行名一律从模块读。
LEGACY_LABELS = (
    # 批 3 之前的旧名
    "算力库存", "客户售价", "提现结算", "推广获客",
    # 批 3 名单里已被后来的「算力」口径废掉的两条(锚过期,不是缺陷)
    "额度包管理", "资金与额度对账",
)

_DECL = re.compile(r"export\s+const\s+SIDEBAR_LABELS\s*:[^=]*=\s*\{")
_ENTRY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'")


def load_sidebar_labels() -> dict[str, str]:
    """现场解析 SSOT 里的 `SIDEBAR_LABELS` —— 闸④ 长在这里。

    先剥注释再解析:那个字面表里夹着大段 `/** ... */` 说明,
    不剥的话注释里举例的名字会被当成条目(「提到」不等于「在用」,本班第四次)。
    剥注释复用 `tests/v35_visible_copy.strip_comments`,不再写第二份。
    """
    assert SSOT.is_file(), (
        f"找不到术语 SSOT:{SSOT}。这不是「跳过」的理由 —— 文件没了意味着侧栏标签"
        f"失去唯一事实源,本锁的全部前提消失,必须响。")
    src = strip_comments(SSOT.read_text(encoding="utf-8"))
    m = _DECL.search(src)
    assert m, (
        "SSOT 里找不到 `export const SIDEBAR_LABELS` —— 被改名/删除了。"
        "改名要同笔改本文件;删除意味着侧栏又回到硬编码。")
    i = m.end() - 1
    depth = 0
    end = -1
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    assert end > i, "SIDEBAR_LABELS 的花括号没配上 —— 解析器读到文件尾了"
    labels = dict(_ENTRY.findall(src[i + 1:end]))
    assert labels, (
        "SIDEBAR_LABELS 解析为空 ⇒ **红**,不 skip。"
        "空分母的绿是「什么都没量到」被读成「没有违规」。")
    return labels


# ══ ① 值集合:数量 + 禁词 ═══════════════════════════════════════════
def test_ssot_has_exactly_eight_labels():
    labels = load_sidebar_labels()
    assert len(labels) == 8, (
        f"SIDEBAR_LABELS 有 {len(labels)} 条(期望 8):{sorted(labels)}\n"
        f"    多了:新增侧栏入口要同笔在这里定名,并回 Review 过文案;\n"
        f"    少了:有入口退回硬编码,或条目被删而侧栏还在调它。")


def test_ssot_labels_contain_no_forbidden_terms():
    """禁词打在**值**上(不是打在侧栏源码上)—— 值才是用户看到的字。"""
    bad = {k: v for k, v in load_sidebar_labels().items() if FORBIDDEN.search(v)}
    assert not bad, (
        f"侧栏标签含禁词 额度/积分/代理:{bad}\n"
        f"    全站文案统一「算力」;对外统一称「服务商」,不称「代理」。")


def test_ssot_labels_are_nonempty_chinese():
    """分母自证:解析出来的必须是**真名字**,不是空串。

    少了这条,`{'a': '', ...}` 8 条会让上面两条一起绿 —— 数量对、也不含禁词。
    """
    labels = load_sidebar_labels()
    bad = {k: v for k, v in labels.items() if not re.search(r"[一-鿿]", v)}
    assert not bad, f"这些标签不是中文文案(解析可能读错了位置):{bad}"


# ══ ② 真接线:每个 key 都有消费方 ═══════════════════════════════════
def test_sidebar_imports_the_ssot_accessor():
    src = SIDEBAR.read_text(encoding="utf-8")
    assert re.search(r"import\s*\{[^}]*\bsidebarLabel\b[^}]*\}\s*from\s*['\"]@/lib/v35Terminology['\"]",
                     src), "AppSidebar 没有 import sidebarLabel —— 标签必然又是硬编码的"


def test_every_ssot_key_is_actually_called_in_the_sidebar():
    """🔴 接线锁:**每个** key 都要被 `sidebarLabel('<key>')` 调到。

    只查 import 会被 import 行顶住(本班在 #95 栽过同一形:
    「文件里含 unevaluated」靠 import 行恒真)。只查调用**总数**也不够 ——
    8 次调用可能全指向同一个 key,而另外 7 条 SSOT 条目无人消费。
    """
    src = strip_comments(SIDEBAR.read_text(encoding="utf-8"))
    called = set(re.findall(r"sidebarLabel\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)", src))
    labels = load_sidebar_labels()
    orphan = sorted(set(labels) - called)
    assert not orphan, (
        f"这些 SSOT 条目在侧栏里**没有消费方**:{orphan}\n"
        f"    定了名却没人用 = SSOT 是摆设,侧栏那一项仍是硬编码或已消失。")
    unknown = sorted(called - set(labels))
    assert not unknown, (
        f"侧栏调了 SSOT 里没有的 key:{unknown} —— `sidebarLabel()` 取不到会回落成"
        f"**key 本身**(`SIDEBAR_LABELS[key] ?? key`),用户会看到 `admin_xxx` 这种原文。")
    assert len(called) >= len(labels), f"调用 {len(called)} 个 key < SSOT {len(labels)} 条"


# ══ ③ 侧栏零硬编码:新名旧名都禁 ═════════════════════════════════════
def test_sidebar_hardcodes_no_label_literal():
    """新名也禁 —— 只禁旧名会被「把新名直写进侧栏、SSOT 没人用」骗过。"""
    src = strip_comments(SIDEBAR.read_text(encoding="utf-8"))
    watched = list(load_sidebar_labels().values()) + list(LEGACY_LABELS)
    hits = []
    for name in watched:
        for m in re.finditer(r"label\s*:\s*['\"]" + re.escape(name) + r"['\"]", src):
            hits.append((name, src[:m.start()].count("\n") + 1))
    assert not hits, (
        f"AppSidebar 里有**硬编码**的侧栏标签(name, 行号):{hits}\n"
        f"    标签唯一事实源是 v35Terminology.SIDEBAR_LABELS,侧栏只许写"
        f" `label: sidebarLabel('<key>')`。\n"
        f"    (本锁只管这 {len(watched)} 个被治理的名字;其它侧栏 label 字面不在范围内。)")


# ══ ④ 前提缺失 = 红,不是 skip ═══════════════════════════════════════
def test_missing_ssot_file_is_red_not_skip(tmp_path, monkeypatch):
    """🔴 毒:SSOT 文件不在 ⇒ 必须抛,不许 skip。

    原锁 `if not sb.exists(): pytest.skip(...)` 正是这一形:文件改名之后
    判据什么都不检查,而 `-q` 汇总里 skip 不进 failed 计数,没人会发现。
    """
    import tests.test_v35_sidebar_ssot_lock as mod
    monkeypatch.setattr(mod, "SSOT", tmp_path / "__never_exists__.ts")
    with pytest.raises(AssertionError, match="找不到术语 SSOT"):
        mod.load_sidebar_labels()


def test_empty_ssot_table_is_red_not_skip(tmp_path, monkeypatch):
    """毒:表在但是空的 ⇒ 必须红。空分母的绿是本仓头号假绿形态。"""
    import tests.test_v35_sidebar_ssot_lock as mod
    fake = tmp_path / "v35Terminology.ts"
    fake.write_text("export const SIDEBAR_LABELS: Record<string, string> = {\n};\n",
                    encoding="utf-8")
    monkeypatch.setattr(mod, "SSOT", fake)
    with pytest.raises(AssertionError, match="解析为空"):
        mod.load_sidebar_labels()


def test_parser_ignores_names_mentioned_only_in_comments(tmp_path, monkeypatch):
    """毒:注释里举例的名字不许被当成条目。

    那个字面表里夹着大段 `/** ... */` 说明,里面就写着「额度包管理」等**禁词**名字。
    不剥注释的话,禁词那条会因为一段**解释为什么不用这个名**的注释而变红 ——
    带证据的错红,比分母为空更难发现。
    """
    import tests.test_v35_sidebar_ssot_lock as mod
    fake = tmp_path / "v35Terminology.ts"
    fake.write_text(
        "export const SIDEBAR_LABELS: Record<string, string> = {\n"
        "    /** 旧名 old_key: '额度包管理' 已废 */\n"
        "    agent_inventory: '我的库存',\n"
        "};\n", encoding="utf-8")
    monkeypatch.setattr(mod, "SSOT", fake)
    labels = mod.load_sidebar_labels()
    assert labels == {"agent_inventory": "我的库存"}, f"注释被当成条目了:{labels}"
