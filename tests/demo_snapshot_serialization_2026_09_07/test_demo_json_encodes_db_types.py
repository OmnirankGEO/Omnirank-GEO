"""#148 · demo 快照出口必须能编码 DB 原生类型。

## 缺陷

`GET /api/portal/tokens/94` 在 demo 的 `frozen_snapshot` 那支 500 ×2。
`snapshot_transport_payload()` 的 payload 里带着 DB 的 `date`
(`trends` 里的 `period_date`,还有 `expires_at` / `archived_at`),
直塞 `JSONResponse(content=payload)` ⇒
`TypeError: Object of type date is not JSON serializable`。

## 分母不是那一处

机械枚举:`services/demo_access.py` 里有 **8 处** `JSONResponse(content=…)`,
**全部**是直塞。只修被报的那一处,其余七处照样是雷,
而且下一个新增的调用点仍然可以直塞 —— **不会有任何东西变红**。

## 判据打行为,不打「不抛」

「不抛异常」太弱:一个把 payload 整个丢掉的实现也不抛。
所以下面断言的是**编码结果**:该字段在响应体里是 ISO 串。
"""

from __future__ import annotations

import ast
import datetime
import decimal
import io
import json
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "services" / "demo_access.py"


def _calls(name):
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]


# ------------------------------------------------------------------ 结构锁

def test_module_has_exactly_one_bare_jsonresponse_and_it_is_the_gate():
    """🔴 承重锁:本模块零裸 `JSONResponse` —— 唯一那处是门自己。

    毒:把任何一个调用点改回 `JSONResponse(...)` ⇒ 红。

    锁「零裸抛」而不是「有 `_demo_json` 调用」:后者在**部分**改造下照样绿
    (8 处里改了 1 处,它仍然"有调用")。
    """
    bare = _calls("JSONResponse")
    gate = _calls("_demo_json")
    assert len(bare) == 1, (
        "本模块有 %d 处裸 JSONResponse(应只剩门自己):行号 %s"
        % (len(bare), [n.lineno for n in bare]))

    # 那唯一一处必须**在门的函数体内**,不是散落在别处
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_demo_json")
    inside = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "JSONResponse"]
    assert len(inside) == 1, "唯一那处裸 JSONResponse 不在门里"
    assert len(gate) >= 7, "调用点没都改走门,只有 %d 处" % len(gate)


def test_the_gate_actually_encodes():
    """结构臂:门里必须真的调 `jsonable_encoder`。

    没有这条,上一条锁可以被一个「改了名字但没编码」的门骗过 ——
    形状对了、缺陷还在(机制存在 ≠ 机制生效)。
    """
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_demo_json")
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "jsonable_encoder"
               for n in ast.walk(fn)), "门没有编码,只是改了个名字"


# ------------------------------------------------------------------ 行为臂

@pytest.mark.parametrize("value,expect", [
    (datetime.date(2026, 9, 7), "2026-09-07"),
    (datetime.datetime(2026, 9, 7, 7, 31, 0), "2026-09-07T07:31:00"),
])
def test_date_like_values_come_out_as_iso_strings(value, expect):
    """🔴 本单那一格:payload 含 date ⇒ 响应体里是 ISO 串。

    打**编码结果**不打「没抛异常」—— 后者一个把 payload 丢空的实现也满足。
    毒:门不再 `jsonable_encoder` ⇒ 红(TypeError)。
    """
    from services.demo_access import _demo_json

    resp = _demo_json(200, {"trends": [{"date": value, "rate": 0.5}]})
    body = json.loads(resp.body.decode("utf-8"))
    assert body["trends"][0]["date"] == expect, body
    assert body["trends"][0]["rate"] == 0.5


@pytest.mark.parametrize("value", [
    decimal.Decimal("75.88"),
    uuid.UUID("12345678-1234-5678-1234-567812345678"),
])
def test_other_unserialisable_db_types_also_survive(value):
    """不是只有 date:`Decimal`(金额)与 `UUID` 一样不可序列化。

    🔴 这条是**在出口编码**而不是「把 date/datetime 归一成 ISO」的理由:
       归一只挡得住今天想到的那几种,而下一种进 payload 时同样整页 500,
       且没有任何东西会提醒。
    """
    from services.demo_access import _demo_json

    resp = _demo_json(200, {"v": value})
    body = json.loads(resp.body.decode("utf-8"))
    assert body["v"] is not None


def test_status_and_headers_are_preserved():
    """门不许改状态码与响应头 —— 它只负责编码。"""
    from services.demo_access import _demo_json

    resp = _demo_json(409, {"detail": "x"},
                      {"X-Demo-Mode": "demo", "Cache-Control": "no-store"})
    assert resp.status_code == 409
    assert resp.headers["x-demo-mode"] == "demo"
    assert resp.headers["cache-control"] == "no-store"


def test_a_plain_payload_is_unchanged():
    """反向臂:本来就能序列化的 payload,编码前后**逐字节相同**。

    没有这条,一个「把所有值转成字符串」的门也能让上面几条变绿,
    而那会悄悄改掉所有 demo 响应的类型(数字变字符串,前端算术全崩)。
    """
    from services.demo_access import _demo_json

    payload = {"n": 3, "f": 1.5, "s": "x", "b": True, "z": None, "arr": [1, 2]}
    body = json.loads(_demo_json(200, payload).body.decode("utf-8"))
    assert body == payload, body
