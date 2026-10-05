"""包 C③ · 动态事实只读适配层。

工单 §8 S09:「询问当前价格/算力 → 从当前动态真值读取;源不可用给 O1 重试/人工。
**明确禁止** 固定 130/300 等常量兜底」。

这里最要紧的一条判据是 `test_source_failure_never_falls_back_to_a_constant`:
回退常量是最坏的一种失败 —— 用户拿到一个**看起来对**的错数字,没有任何人会发现。
"""

from __future__ import annotations

import io
import pathlib

import pytest

from services import xiaobang_dynamic_facts as dyn


def test_live_value_is_read_from_the_existing_readonly_function(monkeypatch):
    """必须命中:能读到就返回真值,并标明这是当前值。"""
    monkeypatch.setattr(
        "db.wallet_db.get_feature_pricing",
        lambda code, cursor=None: {
            "feature_code": code, "feature_name": "GEO 专项诊断",
            "cost_compute": 777, "is_active": True,
        },
    )
    fact = dyn.feature_price("geo_diagnosis")
    assert fact.available is True
    assert fact.amount == 777
    assert fact.unit == "算力"
    text = dyn.describe(fact)
    assert "777" in text and "算力" in text
    assert "当前" in text or "以下单前确认页显示的为准" in text


def test_legacy_cost_column_is_still_read(monkeypatch):
    """反向对照:旧列 `cost_points` 也要能读到,否则「取不到」会是假的。"""
    monkeypatch.setattr(
        "db.wallet_db.get_feature_pricing",
        lambda code, cursor=None: {"feature_code": code, "feature_name": "选题生成",
                                   "cost_points": 88},
    )
    assert dyn.feature_price("topic_gen").amount == 88


@pytest.mark.parametrize("boom", [
    ValueError("未知的功能编码: x"),
    RuntimeError("connection refused"),
    Exception("relation \"feature_pricing\" does not exist"),
])
def test_source_failure_never_falls_back_to_a_constant(monkeypatch, boom):
    """🔴 主锁:取数失败 ⇒ 不可用 + 没有任何数字,**绝不**兜底常量。

    给 `feature_price` 加任何一句 `return DynamicFact(..., amount=<常量>)`,本条必红。
    """
    def _raise(code, cursor=None):
        raise boom
    monkeypatch.setattr("db.wallet_db.get_feature_pricing", _raise)

    fact = dyn.feature_price("geo_diagnosis")
    assert fact.available is False
    assert fact.amount is None, "不可用却带了数字 = 常量兜底"

    text = dyn.describe(fact)
    # 必须不命中:一个数字都不许出现在给用户的话里
    assert not any(ch.isdigit() for ch in text), text
    # 必须命中:O1 出口(确认页 / 人工)俱在
    assert "确认页" in text
    assert "工作人员" in text


def test_missing_cost_column_is_unavailable_not_zero(monkeypatch):
    """行在但没有消耗列 ⇒ 不可用。**不许**当成 0 —— 0 会被用户读成「免费」。"""
    monkeypatch.setattr(
        "db.wallet_db.get_feature_pricing",
        lambda code, cursor=None: {"feature_code": code, "feature_name": "x"},
    )
    fact = dyn.feature_price("x")
    assert fact.available is False
    assert fact.amount is None


def test_an_unavailable_fact_carrying_a_number_is_rejected_by_construction():
    """把「不可用 + 带数字」这种半吊子状态在**类型层**堵死。"""
    with pytest.raises(ValueError):
        dyn.DynamicFact(key="x", available=False, amount=130)


def _executable_source(path) -> str:
    """只取**会执行的**代码,剥掉注释与文档串。

    🔴 第一版直接扫全文,当场被自己的禁令原文判红 ——
       模块 docstring 里写着「禁止固定 130/300 等常量兜底」。
       裸串结构锚会把**引用禁令**当成**违反禁令**(本仓记过同一种病:
       `feedback_prose_mention_trips_string_anchored_locks`)。
       「不许有第二出口」型的锁,正确做法是**收紧匹配面**到真正承重的那一层,
       而不是往白名单里加一条把自己放过去。
    """
    import ast
    src = io.open(pathlib.Path(path), encoding="utf-8").read()
    tree = ast.parse(src)
    # 把所有文档串节点替换掉:剩下的就是真会跑的表达式
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body[0].value.value = ""
    return ast.unparse(tree)


def test_module_contains_no_price_constants():
    """🔴 结构锚:本模块**可执行代码**里不许出现任何看起来像价格的常量。

    扫的是源码形态而不是「跑一遍看返回值」—— 后者挡不住一条没被判据驱动到的
    兜底分支(本仓记过「变异存活常因判据自己构造了中间值」)。
    """
    code = _executable_source(dyn.__file__)
    for bad in ("130", "300", "650", "260", "150", "1200"):
        assert bad not in code, "可执行代码里出现了价格常量 %s" % bad


def test_the_price_constant_anchor_actually_bites():
    """🔴 给上面那把锁注毒:剥完文档串后,真常量必须还看得见。

    不注毒的锁不许当证据 —— 万一 `_executable_source` 把整个模块剥空了,
    上面那条会永远绿。
    """
    import tempfile, os
    src = "'''禁止写 130 这种常量'''\n\n\ndef f():\n    '''也不许 300'''\n    return 650\n"
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(src)
        code = _executable_source(path)
        assert "650" in code, "真常量被剥没了 —— 锁是空的"
        assert "130" not in code and "300" not in code, "文档串没被剥干净"
    finally:
        os.unlink(path)


def test_the_adapter_never_writes(monkeypatch):
    """只读自证:适配层不许碰任何写入口。"""
    src = io.open(pathlib.Path(dyn.__file__), encoding="utf-8").read().upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "COMMIT", "FREEZE", "CHARGE"):
        assert verb not in src, verb
