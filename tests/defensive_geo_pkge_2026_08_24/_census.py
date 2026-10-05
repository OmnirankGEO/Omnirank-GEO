"""调用者 census —— **一次扫描、全名共用、结果缓存**。

═══════════════════════════════════════════════════════════════════════
🔴 为什么抽成一个模块(两个理由,都是记过的形态)
═══════════════════════════════════════════════════════════════════════
1. **同一谓词写两处 ⇒ 必有一处没人验**。
   ``test_p1_2_*`` 与 ``test_wiring_lock_runtime`` 原本各写了一份
   ``_production_callers``,两份逐字相同 —— 哪天有人只改一份,
   另一份会安静地继续用旧口径。

2. **内存**。生产扫描面是 1032 个文件 / 22.3 MB 源码;每调一次就
   全量 AST 一遍,一个 pytest 进程里调了 3 次(test_00 / test_00b / test_06),
   实测直接 ``MemoryError`` 炸在 ``db/diagnosis_db.py``(358 KB)上。
   🔴 那种红**与被测代码无关** —— 它会把"接线没接上"和"进程内存不够"
   混成一团,判读当场失效(本仓记过:两边都红先比错误签名)。
   现在一次扫描把**所有**关心的函数名一起收了,结果缓存复用。

🔴 只认 ``ast.Call``,不用裸符号名 grep:import 语句、注释、字符串都会命中裸名,
   而"import 了"不等于"调用了"(本仓记过:census 裸符号名 = 把 import 当调用)。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 普查面 = **生产**代码目录。不含 ``tests/`` ——
#: 「只有测试在调」正是死函数的标准形态(本仓记过:仅测试调用 = NO-GO)。
PRODUCTION_DIRS = ("db", "api", "services", "workflows", "agents", "tools")

_CACHE: dict[str, set[str]] | None = None


def _scan() -> dict[str, set[str]]:
    """一次遍历,收全部被调函数名 → 调用者文件集合。

    🔴 定义处不算调用者:同一文件里 ``def f(...)`` 与 ``f(...)`` 并存时
       (递归、内部复用),把它算成"有人调"会让死函数看起来是活的。
       判法与两个旧实现逐字一致:文件里出现 ``def <name>(`` 就不计这个文件。
    """
    out: dict[str, set[str]] = {}
    for d in PRODUCTION_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, MemoryError):                 # pragma: no cover - 环境问题
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = path.relative_to(ROOT).as_posix()
            names: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name:
                    names.add(name)
            del tree
            for name in names:
                if f"def {name}(" in src:
                    continue                              # 定义处所在文件不算调用者
                out.setdefault(name, set()).add(rel)
            del src
    return out


def production_callers(function_name: str) -> set[str]:
    """全仓(排 tests)真**调用**了这个函数的文件集合。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _scan()
    return set(_CACHE.get(function_name, ()))


def scanned_file_count() -> int:
    """分母自证用:扫了多少个文件。0 = 探测器根本没跑,后面全是假绿。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _scan()
    return sum(len(v) for v in _CACHE.values())
