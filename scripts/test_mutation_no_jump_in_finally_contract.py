# -*- coding: utf-8 -*-
"""`finally` 体内禁 `break` / `continue` / `return`(Review 照准 · 2026-08-28)。

═══ 为什么这条值钱 ═══
`finally` 里的跳转会**丢弃正在传播的异常**。`return` 众所周知,
`break` / `continue` **完全一样**,而后两者容易被漏掉 —— 本仓就有现成一例:

    tests/defensive_geo_pkgh_2026_08_23/_mutation_runner.py:131
    finally:
        …还原…
        if len(back) != len(original):
            # 🔴 不在 finally 里 return(会吞掉异常)。标记一下,循环外处理。
            restore_broken = True
            break                      # ← 作者知道规矩,只是漏了半边

独立复现(不碰工作树):
    finally 里 break     -> 循环正常结束(异常被吞)
    finally 里 continue  -> 循环正常结束(异常被吞)
    finally 里不跳转     -> 异常正常抛出

**对变异 runner 尤其贵**:变异臂里 pytest 子进程炸、还原失败抛出的异常会被这一跳转
吞掉,循环正常退出 ⇒ 走到汇总 ⇒ 可能 `return 0`。
于是「跑挂了」与「跑完了零存活」**同形** —— 本仓假绿家族的新成员。
(Python 3.14 才为 `break`/`continue` 加了 SyntaxWarning;`return` 至今不警告。)

═══ 分母 ═══
`scripts/` + `tests/` 下**全部** `.py`,机械枚举,不是手写清单。
🔴 读文件用 `utf-8-sig`:普查第一版用 `utf-8`,一个带 BOM 的文件解析失败被跳过 ——
   **解析失败 = 分母洞,而分母洞不会让任何判据变红**。现在解析失败必须为 0。

跑法::

    python -m pytest scripts/test_no_jump_in_finally_contract.py -q
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOTS = ("scripts", "tests")

#: 冻结豁免集(**钉大小**)。空 = 一处都不豁免。
#: 加一项就要同时改大小,并写清楚为什么那一处的异常吞掉是可接受的。
FROZEN_EXEMPTIONS: frozenset[str] = frozenset()
FROZEN_EXEMPTIONS_SIZE = 0


def _py_files() -> list[pathlib.Path]:
    return sorted({p for r in ROOTS for p in (ROOT / r).rglob("*.py")})


def _jumps_in_finally(tree: ast.AST) -> list[tuple[int, str]]:
    """`finally` 体内的跳转。

    只看**直接属于这个 finally** 的语句子树;
    嵌套函数体内的 `return` 不算(它 return 的是那个函数,不吞外层异常)。
    """
    out: list[tuple[int, str]] = []

    def scan(node: ast.AST) -> None:
        """自己走,不用 `ast.walk` —— 它会把**嵌套函数体**一起展平。

        🔴 第一版就是用 `ast.walk` + 对 FunctionDef 节点 `continue`,
           而那只跳过了函数节点本身、没跳过它的子树 ⇒ 嵌套函数里的 `return`
           被误算成"finally 里的跳转"。**是这条判据自己的阴性对照抓到的。**
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue          # 它 return 的是那个函数,不吞外层异常
            if isinstance(child, (ast.Break, ast.Continue, ast.Return)):
                out.append((child.lineno, type(child).__name__))
            scan(child)

    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.finalbody:
            for stmt in node.finalbody:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(stmt, (ast.Break, ast.Continue, ast.Return)):
                    out.append((stmt.lineno, type(stmt).__name__))
                scan(stmt)
    return out


def test_finally_00_the_probe_actually_detects_the_shape():
    """🔴 正样本先跑:探针抓不到坏形态的话,下面那条「全仓干净」没有任何含义。"""
    src = ("def f():\n"
           "    for _ in range(1):\n"
           "        try:\n"
           "            raise RuntimeError('x')\n"
           "        finally:\n"
           "            break\n")
    assert _jumps_in_finally(ast.parse(src)) == [(6, "Break")]
    # 阴性对照:嵌套函数里的 return 不该被算进来
    ok = ("def f():\n"
          "    try:\n"
          "        pass\n"
          "    finally:\n"
          "        def g():\n"
          "            return 1\n"
          "        g()\n")
    assert _jumps_in_finally(ast.parse(ok)) == []


def test_finally_01_every_file_in_the_denominator_parses():
    """分母不许带洞:解析失败的文件**不会让任何判据变红**。

    第一版普查用 `utf-8` 读,一个带 BOM 的文件解析失败被静默跳过 ——
    那就是一个安静的少数。现在用 `utf-8-sig`,并要求失败数为 0。
    """
    files = _py_files()
    assert len(files) > 500, f"分母只扫到 {len(files)} 个文件 —— 探针写废了"
    unparsed = []
    for p in files:
        try:
            ast.parse(p.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            unparsed.append((p.relative_to(ROOT).as_posix(), type(exc).__name__))
    assert not unparsed, f"这些文件解析不了 ⇒ 分母洞:{unparsed}"


def test_finally_02_no_jump_in_any_finally_block():
    """全仓 `finally` 体内不许有 break / continue / return。"""
    offenders = []
    for p in _py_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in FROZEN_EXEMPTIONS:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue          # 由 test_finally_01 单独判红,这里不重复
        for lineno, kind in _jumps_in_finally(tree):
            offenders.append(f"{rel}:{lineno} {kind}")
    assert not offenders, (
        "这些 `finally` 体内有跳转,会**丢弃正在传播的异常**:\n  "
        + "\n  ".join(offenders)
        + "\n  改法:标志位 + 循环外处理,或把跳转挪出 finally。"
        "\n  (对 runner 尤其贵:异常被吞 ⇒「跑挂了」与「跑完零存活」同形。)")


def test_finally_03_the_exemption_set_is_frozen_by_size():
    """豁免集钉大小 —— 加一项必须同时改大小,不许悄悄放行。"""
    assert len(FROZEN_EXEMPTIONS) == FROZEN_EXEMPTIONS_SIZE, (
        f"豁免集 {len(FROZEN_EXEMPTIONS)} 项 ≠ 钉死的 {FROZEN_EXEMPTIONS_SIZE} 项")
