"""`set_snapshot_sync` 的三态返回值不许被当布尔用。

为什么要这把锁
--------------
P2-RUNTIME-1 的地基是把 `set_snapshot_sync` 从 `bool` 改成三态
(`SYNC_SET_ACCEPTED` / `SYNC_SET_REJECTED` / `None`)。
但三态一旦落进**布尔上下文**就又被压回两态,而且是**最坏的那种压法**:

    SYNC_SET_REJECTED = 0   → falsy   ← "Redis 拒了"
    None                    → falsy   ← "Redis 不可达"
    SYNC_SET_ACCEPTED = 1   → truthy

`if set_snapshot_sync(...)` 会把「拒绝」和「不可达」并成一档 —— 而这两档的正确处置
**恰好相反**(拒绝 ⇒ 绝不写本地;不可达 ⇒ 由调用方按 `driven_in_process` 决定)。
把它们并档,就是把这条修复原样退回去,而且退得悄无声息:代码读起来完全正常。

所以锁的形态不是"别写某个词",是**数据流**:
  ① 任何调用点都不许出现在布尔上下文里(if/while/not/and/or/bool()/三元的 test);
  ② 凡是把返回值**接住**的地方,必须拿它跟三态常量(或 `None`)**比较**过。

自证不靠改真文件:检查器抽成纯函数,拿**合成的坏代码**喂它,必须逐条报出来。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FN = "set_snapshot_sync"
#: 三态常量名(比较对象白名单)。`None` 单独判。
TRISTATE_NAMES = ("SYNC_SET_ACCEPTED", "SYNC_SET_REJECTED")
#: 生产侧扫描面。判据自己不在分母里(判据本来就会拿它做各种断言)。
PROD_FILES = ("server.py", "cache/progress_bus.py", "api/defensive_geo_api.py")


def _parents(tree):
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _is_our_call(node):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr == FN) or \
           (isinstance(f, ast.Name) and f.id == FN)


def _compared_against_tristate(func_node, name: str) -> bool:
    """函数体里,`name` 有没有被拿去跟三态常量 / None 比较过。"""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Compare):
            continue
        parts = [node.left] + list(node.comparators)
        uses_name = any(isinstance(p, ast.Name) and p.id == name for p in parts)
        if not uses_name:
            continue
        for p in parts:
            if isinstance(p, ast.Constant) and p.value is None:
                return True
            if isinstance(p, ast.Name) and p.id in TRISTATE_NAMES:
                return True
            if isinstance(p, ast.Attribute) and p.attr in TRISTATE_NAMES:
                return True
    return False


def scan_source(src: str, label: str) -> list[str]:
    """返回违规列表。**纯函数** —— 所以能拿合成的坏代码给它自证。"""
    tree = ast.parse(src)
    parents = _parents(tree)
    problems = []

    def _enclosing_func(node):
        cur = parents.get(node)
        while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cur = parents.get(cur)
        return cur

    for node in ast.walk(tree):
        if not _is_our_call(node):
            continue
        line = getattr(node, "lineno", "?")
        p = parents.get(node)
        # ① 布尔上下文
        if isinstance(p, (ast.BoolOp,)) or \
                (isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not)) or \
                (isinstance(p, (ast.If, ast.While, ast.IfExp)) and p.test is node) or \
                (isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "bool"):
            problems.append(
                f"{label}:{line} {FN}() 的返回值被当布尔用了 —— "
                "「拒绝」(0)和「不可达」(None)会并成同一档,而这两档的处置恰好相反")
            continue
        # ②' 整个丢弃 —— 守卫出了裁定却没人看
        #
        # 🔴 [fof8 P1-3 2026-08-31 收紧] 这一条原来在"合法写法"里放行,理由写的是
        #    "总线已知不可用那一支"。Codex 的 P1-3 正是从这个豁免里长出来的:
        #    `send_message` 丢掉同步返回值,于是 `SYNC_SET_REJECTED` 照样记本地 + dispatch。
        #    修完之后生产侧**零个**丢弃点,豁免同步取消 —— 否则旧锁会反过来给新缺陷背书。
        if isinstance(p, ast.Expr):
            problems.append(
                f"{label}:{line} {FN}() 的返回值被整个丢弃 —— "
                "守卫出了裁定却没人看,`REJECTED` 与 `ACCEPTED` 在调用点没有区别")
            continue
        # ② 接住了就必须比较过
        if isinstance(p, ast.Assign) and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
            fn = _enclosing_func(node)
            target = p.targets[0].id
            if fn is not None and not _compared_against_tristate(fn, target):
                problems.append(
                    f"{label}:{line} 接住了 {FN}() 的返回值(`{target}`)却从没拿它跟三态比较 —— "
                    "接住不用等于又把它压回两态")
    return problems


def test_denominator_is_alive():
    """判据可用性:分母非空 + 三态常量真的在。分母塌了这把锁就是空断言。"""
    import cache.progress_bus as pb

    for name in TRISTATE_NAMES:
        assert hasattr(pb, name), f"三态常量 {name} 不在了 —— 锚点过期"
    total = 0
    for rel in PROD_FILES:
        src = (REPO / rel).read_text(encoding="utf-8")
        total += sum(1 for n in ast.walk(ast.parse(src)) if _is_our_call(n))
    assert total >= 2, f"全生产面只扫到 {total} 个 {FN} 调用点 —— 分母塌了"


@pytest.mark.parametrize("rel", PROD_FILES)
def test_production_never_uses_the_tristate_as_a_boolean(rel):
    """🔴 主锁:生产侧没有任何一处把三态当布尔用 / 接住不比较。"""
    problems = scan_source((REPO / rel).read_text(encoding="utf-8"), rel)
    assert not problems, "\n".join(problems)


#: 自证用的**合成坏代码**。不改真文件 —— 检查器是纯函数,喂它就行。
BAD_SNIPPETS = {
    "if 里直接判": "def f():\n    if pb.set_snapshot_sync(s, m):\n        pass\n",
    "not 取反": "def f():\n    if not pb.set_snapshot_sync(s, m):\n        pass\n",
    "and 短路": "def f():\n    x = pb.set_snapshot_sync(s, m) and 1\n",
    "bool() 包": "def f():\n    x = bool(pb.set_snapshot_sync(s, m))\n",
    "三元的 test": "def f():\n    x = 1 if pb.set_snapshot_sync(s, m) else 2\n",
    "接住了不比较": "def f():\n    r = pb.set_snapshot_sync(s, m)\n    return r\n",
    # [fof8 P1-3] 从"合法写法"挪进来的:丢弃返回值就是 P1-3 那条缺陷的形状。
    "整个丢弃返回值": "def f():\n    pb.set_snapshot_sync(s, m)\n",
}


@pytest.mark.parametrize("why,src", sorted(BAD_SNIPPETS.items()))
def test_the_lock_catches_each_bad_shape(why, src):
    """判别力:六种坏形态逐个必须被抓到。"""
    assert scan_source(src, "<合成>"), f"这把锁抓不到「{why}」—— 它挡不住真正会发生的退化"


def test_the_lock_does_not_flag_the_legitimate_shapes():
    """配对的必须不命中:合法写法不许误伤。

    没有这条,把检查器写成"见到 set_snapshot_sync 就红"也能让上面六条全过 ——
    那样它就不是锁,是禁用令。
    """
    ok = {
        "接住并与常量比较": (
            "def f():\n    r = pb.set_snapshot_sync(s, m)\n"
            "    if r == pb.SYNC_SET_REJECTED:\n        return\n"),
        "接住并与 None 比较": (
            "def f():\n    r = pb.set_snapshot_sync(s, m)\n"
            "    if r is None:\n        return\n"),
    }
    for why, src in ok.items():
        assert not scan_source(src, "<合成>"), f"合法写法「{why}」被误伤了"
