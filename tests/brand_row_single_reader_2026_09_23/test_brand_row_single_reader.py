"""WO_267 拆格 · geo_douyin 读 brands 行只有一处(`_fetch_brand_row`)。

本格原住在 `tests/test_geo_one_pipeline_v5_locks.py::test_only_one_place_reads_the_brand_row`。
那个文件整体在冻结名单(17 条存量红,没有运行者),住在里面的锁等于没人跑 ——
WO_267 下单冻结行业时新加了一处直读 brands 行,就是在这种状态下破的「只有一处」。
拆成独立包、登记运行者,原格退役(tests/RETIRED_TESTS.txt)。

守什么:原来四个调用点各写一遍 `get_brand_by_id(...).get("brand_name")`,同一个错抄了四份,
修的时候漏一份就是「修了但还是坏」。读 brands 行收口到一个函数,名称与冻结两个消费方都经它。

判据落在 AST 上:本仓真正的写法是 `asyncio.to_thread(get_brand_by_id, ...)`,函数名是**实参**
不是被调者 —— 只认 Call.func 的第一版扫出 0 个调用点。所以按「函数体里出现 get_brand_by_id 这个名字」数。
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
API = ROOT / "api" / "geo_douyin_api.py"

READER = "_fetch_brand_row"
CONSUMERS = {"fetch_brand_display_name", "_create_and_dispatch_one"}


def _functions(tree: ast.AST):
    return [fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _mentions(fn: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(fn))


def brand_row_readers(source: str) -> tuple[list[str], set[str]]:
    """返回 (直接读 brands 行的函数名, 经 _fetch_brand_row 读的函数名)。"""
    fns = _functions(ast.parse(source))
    callers = [fn.name for fn in fns if _mentions(fn, "get_brand_by_id")]
    users = {fn.name for fn in fns if fn.name != READER and _mentions(fn, READER)}
    return callers, users


def problems(source: str) -> list[str]:
    callers, users = brand_row_readers(source)
    out = []
    if callers != [READER]:
        out.append(f"读 brands 行的地方不是只有 {READER}:{callers}")
    missing = CONSUMERS - users
    if missing:
        out.append(f"这些消费方没经 {READER} 读品牌行:{sorted(missing)}")
    return out


def test_only_one_place_reads_the_brand_row():
    assert problems(API.read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------- 牙证 / 对照(同一个 problems())

_GOOD = '''
import asyncio
async def _fetch_brand_row(brand_id, purpose):
    from db.diagnosis_db import get_brand_by_id
    return await asyncio.to_thread(get_brand_by_id, int(brand_id))
async def fetch_brand_display_name(brand_id):
    return await _fetch_brand_row(brand_id, "name")
async def _create_and_dispatch_one(req):
    return await _fetch_brand_row(req.brand_id, "freeze")
'''


def test_control_the_well_formed_shape_is_green():
    assert problems(_GOOD) == []


def test_tooth_a_second_direct_reader_is_red():
    """WO_267 破的正是这个形状:下单路径自己 to_thread(get_brand_by_id, …)。"""
    bad = _GOOD.replace(
        'return await _fetch_brand_row(req.brand_id, "freeze")',
        "from db.diagnosis_db import get_brand_by_id\n"
        "    return await asyncio.to_thread(get_brand_by_id, req.brand_id)")
    assert bad != _GOOD
    assert any("不是只有" in p for p in problems(bad))


def test_tooth_a_consumer_that_bypasses_the_reader_is_red():
    """「谁都不读」也能满足「只有一处」—— 所以消费方必须真经过它。"""
    bad = _GOOD.replace('return await _fetch_brand_row(brand_id, "name")', "return None")
    assert bad != _GOOD
    assert any("没经" in p for p in problems(bad))
