"""真 Redis 目标守卫**只此一份** —— 防它再漂成两份。

背景:那组守卫(loopback / db=1 / 贴标签的一次性容器 / 端口映射对上)原来长在
``tests/test_progress_bus_real_redis.py`` 里,而那个模块 **import 期**就读 env,
跨包复用会变成"谁先设 env"的隐式顺序依赖 —— 于是我上一版**抄了一遍**。
抄一遍就是同一组守卫两处,而漂掉的那一处不会有任何信号:
它照样"绿",只是守的东西已经不一样了。

现在本体在 ``tests/_shared/progress_redis_guard.py``(env 改函数内懒读),
这个文件就是钉住"别再抄第二份"的那把锁。**纯静态,不需要 Redis / PG。**
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SHARED = "tests/_shared/progress_redis_guard.py"
#: 守卫的**特征字面量**:它们只该出现在本体里。
#: 用字面量而不是函数名当分母,是因为"抄一份"最省事的形态就是把这个串复制走。
#: 🔴 只用**标签**这一个串:容器名前缀在每个调用点都会合法出现(各自的默认容器名),
#:    拿它当分母会把合法用法判红 —— 第一版就这么自伤了一次。
GUARD_LITERALS = ("omnirank.progress-bus-test",)
#: 现役两个调用点。新增第三个真 Redis 判据包时,把它加进来(并因此被迫想一次:
#: 你是不是又要抄守卫)。
CALL_SITES = (
    "tests/test_progress_bus_real_redis.py",
    "tests/defgeo_gate8_2026_08_30/conftest.py",
)


def _tests_py_files():
    for path in (REPO / "tests").rglob("*.py"):
        yield path.relative_to(REPO).as_posix(), path


def test_the_shared_guard_module_exists_and_is_lazy():
    """锚 + 根因:本体在,且 env 是**函数内**读的(不是 import 期)。

    如果哪天有人把 `os.getenv` 提回模块级,复用又会变贵,而"变贵"正是当初抄写的根因。
    """
    src = (REPO / SHARED).read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_getenv = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "getenv"
    ]
    assert not top_level_getenv, \
        "共享守卫又在 import 期读 env 了 —— 跨包复用会变回隐式顺序依赖"
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in ("resolve_target", "verify_ephemeral_container", "bind_progress_bus"):
        assert required in names, f"共享守卫缺 {required} —— 锚点过期"


@pytest.mark.parametrize("rel", CALL_SITES)
def test_every_call_site_imports_the_shared_guard(rel):
    """两个调用点都必须 import 同一个模块(AST,不数字符串)。"""
    path = REPO / rel
    assert path.is_file(), f"调用点 {rel} 不在了 —— 锚点过期,本锁失去它守的东西"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "tests._shared.progress_redis_guard" in modules, \
        f"{rel} 没有 import 共享守卫 —— 多半是又抄了一份"


def test_guard_literals_live_in_exactly_one_file():
    """🔴 主锁:守卫的特征字面量在 `tests/**` 里**只许出现在本体**。

    这条比"调用点有没有 import"更硬:import 了照样可以在旁边再抄一份自己的。
    分母是机械扫出来的,不是我手列的。
    """
    for literal in GUARD_LITERALS:
        holders = sorted(rel for rel, path in _tests_py_files()
                         if literal in path.read_text(encoding="utf-8", errors="replace"))
        # 本文件自己也写着这些串(就在 GUARD_LITERALS 里),排除掉自己。
        holders = [h for h in holders if not h.endswith("test_shared_redis_guard_single_source.py")]
        assert holders == [SHARED], \
            (f"守卫特征串 {literal!r} 出现在 {holders} —— 期望只在 {SHARED}。"
             "多一处就是又抄了一份守卫,而抄走的那份迟早跟本体漂。")
