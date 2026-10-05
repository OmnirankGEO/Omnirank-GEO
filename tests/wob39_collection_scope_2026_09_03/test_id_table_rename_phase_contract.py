# -*- coding: utf-8 -*-
"""#51 换 id 全表同步锁 —— 守「重锚时,按 id 索引的表一张都不能漏」。

2026-09-04 我重锚 `MUT-EXTE3-09/10` 时踩到:改了 id 之后
  · `QUICK_SCOPE` 漏了 ⇒ `SystemExit: 这些发没有快速作用域`  **吼**,当场停机
  · `DB_DOUBLE_HIT` 漏了 ⇒ `.get(mid)` 静默返回 `None`        **不吭声**,那一发从此不打真库
**我是被「吼」的那张拦住,才去机械枚举,才发现「不吭声」的那张。**
只靠「红了才查」,后者永远不会被发现 —— 它不会红,只会让产物看起来一切正常。

🔴 **两条设计决定,都是被实证逼出来的:**

**① 分母是 AST 机械枚举的全部顶层 id 索引表,不是 `ID_TABLE_RENAME_PHASE` 自身。**
   拿登记表当分母 ⇒ 没登记的那张永远在分母之外 ⇒ 恒绿。
   同文件里的 `DOC_ONLY_TABLES` 现在就是这个处境:它承诺「凡未消费的表必须登记在此」,
   却**没有任何判据在跑**,于是 `DENOMINATOR_ADJUDICATIONS` 无声躺了进来(见 #53)。

**② post-rename 的铁律是「新旧两把键都要在」,不是「补新 id」。**
   `_apply_reanchors`(578-590)是**三态**的:新锚在本尖不唯一而旧锚唯一时,
   它打印「尚未生效」并 `continue` —— **id 保持旧的**。
   所以 959 返回后 id 可能是新的也可能仍是旧的,取决于本尖合没合过改靶文件那笔。
   谁拿「它是 post-rename」当理由去删旧 id,会在重锚尚未生效的尖上当场炸。
   (这一条不是我原来的判断 —— 是 2026-09-04 的对抗性复核里两个独立 agent 各自提出的。
    我原本的处方是「补新 id」,那会给后人留下删旧 id 的口实。)
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
PHASES = ("pre-rename", "post-rename", "zero-read", "rule-source")


def _load():
    spec = importlib.util.spec_from_file_location("_m51", GUARDED)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


def id_keyed_tables(src: str) -> dict[str, int]:
    """分母:**AST 机械枚举**模块顶层、键以 `MUT-` 开头的 dict。

    不用 grep —— grep 会把注释里、docstring 里提到的表名一起数进去
    (「散文触发裸串锁」在本仓已有前例)。
    同时覆盖 `X: T = {...}`(AnnAssign)与 `X = {...}`(Assign)两种写法。
    """
    out = {}
    for node in ast.parse(src).body:
        tgt = val = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            tgt, val = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            tgt, val = node.targets[0].id, node.value
        if not tgt or not isinstance(val, ast.Dict):
            continue
        if any(isinstance(k, ast.Constant) and isinstance(k.value, str)
               and k.value.startswith("MUT-") for k in val.keys):
            out[tgt] = node.lineno
    return out


# ══ ① 每张 id 索引表都必须被显式分类 ═════════════════════════════════
def test_w51_01_every_id_keyed_table_is_classified():
    m = _load()
    tables = id_keyed_tables(GUARDED.read_text(encoding="utf-8-sig"))
    assert tables, "分母为空 —— AST 枚举坏了,下面的断言会恒真"
    missing = sorted(t for t in tables if t not in m.ID_TABLE_RENAME_PHASE)
    assert not missing, (
        f"这些按 id 索引的表**没有分类** {len(missing)} 张:{missing}"
        f"(定义在 {[tables[t] for t in missing]} 行)。\n"
        f"    换 id 时漏掉它们,后果分两种:读点用 `[...]` 的会**吼**(SystemExit),"
        f"用 `.get()` 的**不吭声** —— 后者永远不会红。\n"
        f"    请在 `ID_TABLE_RENAME_PHASE` 里登记,格式 `<phase>|<理由>`,"
        f"phase ∈ {PHASES}。判定 phase 的方法:找出该表**全部读点**,"
        f"看它们相对 `_apply_reanchors` 唯一调用点(load_v2 内,约 959 行)是前是后;"
        f"函数体内的读点要看**那个函数何时被调用**,不是看它定义在第几行。")
    stale = sorted(t for t in m.ID_TABLE_RENAME_PHASE if t not in tables)
    assert not stale, (
        f"登记表里有**已不存在**的表 {stale} —— 锚过期,删掉或改名")


def test_w51_01b_the_enumerator_would_find_a_planted_table():
    """反向臂:喂合成源码,枚举器必须找到新表 —— 否则锁①的分母可能恒定。"""
    src = ('A: dict[str, int] = {"MUT-X-01": 1}\n'
           'B = {"MUT-Y-02": 2}\n'
           'C = {"not-a-mut": 3}\n'
           'D = "MUT-Z-03 只是散文里提到"\n')
    got = id_keyed_tables(src)
    assert sorted(got) == ["A", "B"], (
        f"枚举器返回 {sorted(got)} —— 应为 ['A','B']:"
        f"C 不是 MUT- 键不该算;D 是字符串不是 dict,更不该算(散文误伤)")


# ══ ② 分类值必须合法 ═════════════════════════════════════════════════
def test_w51_02_every_classification_has_a_legal_phase_and_a_reason():
    m = _load()
    bad = []
    for t, v in m.ID_TABLE_RENAME_PHASE.items():
        head, _, why = str(v).partition("|")
        if head not in PHASES or not why.strip():
            bad.append(f"{t}={str(v)[:40]}")
    assert not bad, (
        f"这些登记项格式不对 {bad} —— 必须是 `<phase>|<理由>`,phase ∈ {PHASES}。"
        f"**理由不许空**:分类本身不解释「为什么」,下一个人只能重做一遍普查。")


# ══ ③ post-rename:重锚的**新旧两把键都要在** ════════════════════════
def _sync_violations(m) -> list[str]:
    bad = []
    for old, ra in m.V2_REANCHORS.items():
        new = ra["new_id"]
        for t, v in m.ID_TABLE_RENAME_PHASE.items():
            if not str(v).startswith("post-rename"):
                continue
            table = getattr(m, t, None)
            if not isinstance(table, dict) or old not in table:
                continue                       # 该表不管这一发,跳过
            if new not in table:
                bad.append(f"{t} 有 {old} 缺 {new}")
    return bad


def test_w51_03_post_rename_tables_carry_both_the_old_and_the_new_id():
    m = _load()
    bad = _sync_violations(m)
    assert not bad, (
        f"post-rename 表漏了重锚继任者 {len(bad)} 处:{bad}。\n"
        f"    🔴 注意是**两把键都要在**,不是「换成新的」——`_apply_reanchors` 是三态的:"
        f"新锚在本尖不唯一而旧锚唯一时它打印「尚未生效」并保持**旧 id** 跑完本轮。"
        f"删旧 id 会在那种尖上当场炸。")


def test_w51_03b_the_sync_detector_would_flag_a_missing_successor():
    """反向臂:合成一张缺继任者的 post-rename 表,检测器必须点名。"""
    class _Fake:
        V2_REANCHORS = {"MUT-X-01": {"new_id": "MUT-X-01b"}}
        ID_TABLE_RENAME_PHASE = {"T_BAD": "post-rename|合成", "T_OK": "post-rename|合成",
                                 "T_PRE": "pre-rename|合成"}
        T_BAD = {"MUT-X-01": 1}                       # 缺 01b ⇒ 该被点名
        T_OK = {"MUT-X-01": 1, "MUT-X-01b": 1}        # 两把都在 ⇒ 不该被点名
        T_PRE = {"MUT-X-01": 1}                       # pre-rename ⇒ 不该被要求补新 id
    bad = _sync_violations(_Fake())
    assert bad == ["T_BAD 有 MUT-X-01 缺 MUT-X-01b"], (
        f"检测器返回 {bad} —— 它要么漏了缺继任者的表(锁③恒绿),"
        f"要么误伤了两把都在的表 / pre-rename 表(锁③会把正确代码判红)")


# ══ ④ pre-rename:**不许**被要求补新 id(防止把正确代码「修」坏)══════
def test_w51_04_pre_rename_tables_are_not_required_to_carry_the_successor():
    """🔴 这条守的是**不要做多余的事**。

    `V2_AMENDMENTS` 的读点在重锚**之前**,拿到的是旧 id ⇒ 它按旧 id 索引是**对的**。
    现状 `MUT-EXTE3-07` 有、`MUT-EXTE3-07b` 没有 —— 正确,别「修」。
    没有这条,下一个人看到锁③会顺手给 pre-rename 表也补新 id,那是往正确代码里加错。
    """
    m = _load()
    pre = [t for t, v in m.ID_TABLE_RENAME_PHASE.items()
           if str(v).startswith("pre-rename")]
    assert pre, "一张 pre-rename 表都没有 ⇒ 本条恒真,先确认分类是否漏了"
    for t in pre:
        table = getattr(m, t, None)
        if not isinstance(table, dict):
            continue
        for old, ra in m.V2_REANCHORS.items():
            if old in table:
                assert ra["new_id"] not in table, (
                    f"{t} 是 pre-rename 却同时有 {old} 与 {ra['new_id']} —— "
                    f"它的读点在重锚前,只会拿到旧 id;新 id 那条永远不会被读到,"
                    f"是**只声明不消费**的死条目(本仓已为此删过 FLAKE_ADJUDICATIONS)")
