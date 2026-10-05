# -*- coding: utf-8 -*-
"""#53 未消费表登记合同 —— 给 `DOC_ONLY_TABLES` 配上它承诺过、却从没写的判据。

被守文件 `mutation_runner_extsel_v2_2026_08_27.py` 的注释(205-219)自己判过这个病,
措辞很准:

> 一张自称「带证据才允许改终态」的表,如果没人读它,它改不了任何终态,
> 却让读代码的人**以为**终态可以被它改。**这是仪器在说谎。**

处方也对:未消费的裁定类表**显式登记**进 `DOC_ONLY_TABLES` **并配判据**。
登记表建了 —— **判据从没写**。2026-09-04 全仓 AST 普查:
```
DOC_ONLY_TABLES            零读点(只有注释与定义)
ZERO_DISCRIMINATION        零读点 · 已登记 ✅
DENOMINATOR_ADJUDICATIONS  零读点 · **未登记** 🔴  ← 正是 218 行写着「不许」的那件事
```
⇒ **治「只声明不消费」的那张表,自己就是只声明不消费。**
   而它的存在会让人**停止检查** —— 读到「凡未消费的表必须登记在此」,
   自然默认「没登记的就是没有」,真相却是「没人在查」。

🔴 **分母绝不能是 `DOC_ONLY_TABLES` 自身。**
   拿它当分母 ⇒ 未登记的违规者永远在分母之外 ⇒ 恒绿。这正是它现在的处境。
   本文件的分母 = **AST 机械枚举出的、零读点的 id 索引表全集**,登记表只是**豁免名单**。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import contextlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARDED = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"
SKIP_DIRS = ("__pycache__", "venv", "node_modules", ".git")


def _load():
    spec = importlib.util.spec_from_file_location("_m53", GUARDED)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


def id_keyed_tables(src: str) -> dict[str, int]:
    out = {}
    for node in ast.parse(src).body:
        tgt = val = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            tgt, val = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            tgt, val = node.targets[0].id, node.value
        if tgt and isinstance(val, ast.Dict) and any(
                isinstance(k, ast.Constant) and isinstance(k.value, str)
                and k.value.startswith("MUT-") for k in val.keys):
            out[tgt] = node.lineno
    return out


_READ_INDEX: dict[str, list[str]] | None = None


def _build_read_index(names: set[str]) -> dict[str, list[str]]:
    """全仓 `.py` **一次遍历**,建 `名字 -> 真读点` 索引。

    🔴 **不数字符串字面量。** 第一版数了,结果散文提及(注释、docstring、
       本文件自己的说明)把两张零读点的表全淹没 —— 分母变空、锁恒绿。
       这正是本仓的老病「散文触发裸串锁」,而我转头把它建进了检测器。
    也不数 `Store`(定义本身不是读点)。

    ⚠️ 一次遍历不是优化洁癖:第一版按「每张表扫一遍全仓」写,6 张 × 3000 文件,
       **判据 2 分钟跑不完** —— 一条跑不完的判据在汇总里跟「还没跑」同形。
    """
    idx = {n: [] for n in names}
    for p in sorted(ROOT.rglob("*.py")):
        if any(s in str(p) for s in SKIP_DIRS):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id in idx and isinstance(n.ctx, ast.Load):
                idx[n.id].append(f"{p.name}:{n.lineno}")
            elif isinstance(n, ast.Attribute) and n.attr in idx and isinstance(n.ctx, ast.Load):
                idx[n.attr].append(f"{p.name}:{n.lineno}")
    return idx


def zero_read_tables() -> list[str]:
    global _READ_INDEX
    tables = id_keyed_tables(GUARDED.read_text(encoding="utf-8-sig"))
    if _READ_INDEX is None or set(tables) - set(_READ_INDEX):
        _READ_INDEX = _build_read_index(set(tables))
    return sorted(t for t in tables if not _READ_INDEX[t])


# ══ ① 分母自证:零读点集合必须非空,否则下面全是恒真 ═══════════════
def test_w53_01_the_denominator_is_not_empty():
    """🔴 若哪天所有表都有读点,本文件的其余断言会**全部恒真**。

    那时正确的动作是**退役本文件**并写明继任者,不是让它绿着当装饰。
    这条就是那个提醒 —— 它红的时候,说明「已经没有未消费的表了」,是好消息,
    但**必须有人来读到这句话**。
    """
    z = zero_read_tables()
    assert z, ("零读点的 id 索引表**一张都没有** —— 好消息,但本文件其余判据从此恒真。"
               "请退役本文件并在 post-train 台账里写明继任者(别让它绿着当装饰)。")


# ══ ② 每张零读点表必须登记且写理由 ═══════════════════════════════════
def test_w53_02_every_zero_read_table_is_registered_with_a_reason():
    m = _load()
    reg = getattr(m, "DOC_ONLY_TABLES", {})
    bad_missing, bad_empty = [], []
    for t in zero_read_tables():
        if t not in reg:
            bad_missing.append(t)
        elif not str(reg[t]).strip():
            bad_empty.append(t)
    assert not bad_missing, (
        f"这些表**零读点却没登记** {bad_missing} —— 本文件 218 行原话「不许无声躺着冒充可执行」。\n"
        f"    一张没人读的表改不了任何终态,却让读代码的人以为终态可以被它改。\n"
        f"    处置二选一:① 删掉它;② 登记进 `DOC_ONLY_TABLES` 并写清**留档价值**。\n"
        f"    (若它本该被消费 —— 那缺的是消费点,不是登记项。)")
    assert not bad_empty, f"这些登记项**理由为空** {bad_empty} —— 空理由等于没登记"


def test_w53_02b_the_detector_would_flag_an_unregistered_table():
    """反向臂:零读点检测器必须真的能分辨「有读点」与「零读点」。

    喂合成源码,不碰真文件 —— 用真文件做反向臂等于把树弄脏(今天已有前例)。
    """
    src = ('A: dict[str, int] = {"MUT-X-01": 1}\n'
           'B: dict[str, int] = {"MUT-Y-01": 1}\n'
           'def f():\n    return B["MUT-Y-01"]\n')
    tables = id_keyed_tables(src)
    assert sorted(tables) == ["A", "B"], f"枚举器返回 {sorted(tables)},应为 ['A','B']"
    # 在这段合成源码里数读点(独立小实现,证明「Load 才算」这条判别是对的)
    tree = ast.parse(src)
    loads = {t: sum(1 for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and n.id == t and isinstance(n.ctx, ast.Load))
             for t in tables}
    assert loads == {"A": 0, "B": 1}, (
        f"读点判别错了:{loads} —— A 无人读该为 0,B 被 f() 读该为 1。"
        f"若把 Store(定义)也算进去,两张都会 >0,零读点集合永远为空 ⇒ 锁②恒绿")


# ══ ③ 登记项的键必须仍在本轮清单里(不许悄悄过期)═════════════════
def test_w53_03_registered_tables_keys_are_still_in_the_current_roster():
    """`FLAKE_ADJUDICATIONS` 当年被删,理由之一就是它唯一那条 entry 的键
    `MUT-EXTE3-15` 已重锚为 `15b`、**不在本轮清单里** ——
    「接线它等于把一次改判应用到一个不存在的发上」。

    🔴 三态兼容:重锚可能**尚未生效**(见 `_apply_reanchors` 578-590),
       所以旧 id 或其继任者**任一**在清单里都算数。
    """
    m = _load()
    with contextlib.redirect_stdout(io.StringIO()):
        roster = {x["id"] for x in m.load_v2()}
    succ = {old: ra["new_id"] for old, ra in m.V2_REANCHORS.items()}
    stale = []
    for t in getattr(m, "DOC_ONLY_TABLES", {}):
        table = getattr(m, t, None)
        if not isinstance(table, dict):
            continue
        for k in table:
            if k in roster or succ.get(k) in roster:
                continue
            stale.append(f"{t}[{k}]")
    assert not stale, (
        f"这些登记表的键**已不在本轮清单里** {stale} —— 锚过期。\n"
        f"    留档一个不存在的发,等于把一次定性挂在空处;要么改键,要么整张退役。")
