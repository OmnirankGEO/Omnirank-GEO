"""安全 fast-follow 2026-05-29 · 4 个 P2 认证态写越权 RBAC · 真执行测试

主批(fix/security-batch-2026-05-29)关 P0/P1 资金+IDOR 后,对抗复审遗留的 4 个 P2
authenticated-write 越权,单开本分支(老板:不塞进资金 PR · 避免扩大发布风险):
  add_monitoring_keyword / batch_add_monitoring_keywords  · _require_add_keyword_access(brand/quote 归属)
  api_generate_topic_for_keyword / api_research_competitors · +require_quote_access(quote_id)

server.py 无法整体 import(import 连 DB)→ AST 抽源码 exec(stub lazy import)· 对真实源码真执行。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types
import typing

import pytest
from fastapi import HTTPException

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_LOG = types.SimpleNamespace(
    error=lambda *a, **k: None, warning=lambda *a, **k: None,
    info=lambda *a, **k: None, critical=lambda *a, **k: None,
)

_SERVER_CACHE = None


def _extract_server_func(name):
    global _SERVER_CACHE
    if _SERVER_CACHE is None:
        with open(os.path.join(_ROOT, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        _SERVER_CACHE = (src, src.splitlines(), ast.parse(src))
    src, lines, tree = _SERVER_CACHE
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg_lines = lines[node.lineno - 1: node.end_lineno]
            while seg_lines and seg_lines[0].lstrip().startswith("@"):
                seg_lines = seg_lines[1:]
            seg = "\n".join(seg_lines)
            assert seg.lstrip().startswith(("def ", "async def "))
            return seg
    raise AssertionError(f"server.py 未找到顶层函数 {name}")


def _load(name, g):
    ns = {"__name__": "srv_ff"}
    ns.update(g)
    exec(compile(_extract_server_func(name), f"<srv:{name}>", "exec"), ns)
    return ns[name]


def _install(monkeypatch, modname, **attrs):
    m = types.ModuleType(modname)
    for k, v in attrs.items():
        setattr(m, k, v)
    monkeypatch.setitem(sys.modules, modname, m)


class _Req:
    def __init__(self, user=None):
        self.state = types.SimpleNamespace(user=user)


def _kw_req(brand_id=None, quote_id=None, client_id=None):
    return types.SimpleNamespace(brand_id=brand_id, quote_id=quote_id, client_id=client_id)


# ============================================================
# _require_add_keyword_access · 归属解析
# ============================================================
def test_add_access_brand_id_uses_brand_access(monkeypatch):
    rba, rqa = [], []
    _install(monkeypatch, "auth.brand_access",
             require_brand_access=lambda req, bid, **k: rba.append(bid),
             require_quote_access=lambda req, qid: rqa.append(qid))
    fn = _load("_require_add_keyword_access", {"HTTPException": HTTPException, "Request": object})
    fn(_Req(), brand_id=7, quote_id=None, client_id=None)
    assert rba == [7] and rqa == []


def test_add_access_quote_id_uses_quote_access(monkeypatch):
    rqa = []
    _install(monkeypatch, "auth.brand_access",
             require_brand_access=lambda *a, **k: None,
             require_quote_access=lambda req, qid: rqa.append(qid))
    fn = _load("_require_add_keyword_access", {"HTTPException": HTTPException, "Request": object})
    fn(_Req(), brand_id=None, quote_id=88, client_id=None)
    assert rqa == [88]


def test_add_access_client_id_str_uses_quote_access(monkeypatch):
    rqa = []
    _install(monkeypatch, "auth.brand_access",
             require_brand_access=lambda *a, **k: None,
             require_quote_access=lambda req, qid: rqa.append(qid))
    fn = _load("_require_add_keyword_access", {"HTTPException": HTTPException, "Request": object})
    fn(_Req(), brand_id=None, quote_id=None, client_id="55")
    assert rqa == [55]                 # client_id(实为 quote_id 字符串)→ int 化后 require_quote_access


def test_add_access_none_400(monkeypatch):
    _install(monkeypatch, "auth.brand_access",
             require_brand_access=lambda *a, **k: None, require_quote_access=lambda *a, **k: None)
    fn = _load("_require_add_keyword_access", {"HTTPException": HTTPException, "Request": object})
    with pytest.raises(HTTPException) as ei:
        fn(_Req(), None, None, None)
    assert ei.value.status_code == 400


def test_add_access_brand_id_priority_over_quote(monkeypatch):
    rba, rqa = [], []
    _install(monkeypatch, "auth.brand_access",
             require_brand_access=lambda req, bid, **k: rba.append(bid),
             require_quote_access=lambda req, qid: rqa.append(qid))
    fn = _load("_require_add_keyword_access", {"HTTPException": HTTPException, "Request": object})
    fn(_Req(), brand_id=7, quote_id=88, client_id="55")
    assert rba == [7] and rqa == []    # brand_id 优先 · 不重复校验


# ============================================================
# add_monitoring_keyword / batch 路由
# ============================================================
def test_add_route_403_propagates(monkeypatch):
    def _gate(http_request, brand_id=None, quote_id=None, client_id=None):
        raise HTTPException(status_code=403, detail="no")
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "KeywordAddRequest": object,
         "_require_add_keyword_access": _gate,
         "api_add_keyword": lambda req: {"status": "success"}}
    fn = _load("add_monitoring_keyword", g)
    with pytest.raises(HTTPException) as ei:
        fn(_kw_req(brand_id=9), _Req())
    assert ei.value.status_code == 403            # 不被吞成 {"status":"error"}


def test_add_route_happy_calls_api(monkeypatch):
    seen = []
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "KeywordAddRequest": object,
         "_require_add_keyword_access": lambda *a, **k: seen.append("gated"),
         "api_add_keyword": lambda req: {"status": "success", "keyword_id": 1}}
    fn = _load("add_monitoring_keyword", g)
    out = fn(_kw_req(brand_id=9), _Req())
    assert out["status"] == "success" and seen == ["gated"]   # 先校验 · 再落库


def test_batch_route_403_propagates(monkeypatch):
    def _gate(http_request, brand_id=None, quote_id=None, client_id=None):
        raise HTTPException(status_code=403, detail="no")
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "KeywordBatchAddRequest": object,
         "_require_add_keyword_access": _gate,
         "api_batch_add_keywords": lambda req: {"status": "success"}}
    fn = _load("batch_add_monitoring_keywords", g)
    with pytest.raises(HTTPException) as ei:
        fn(_kw_req(brand_id=9), _Req())
    assert ei.value.status_code == 403


# ============================================================
# generate-topic / research-competitors · 守卫在 LLM 之前
# ============================================================
def test_generate_topic_guard_before_llm(monkeypatch):
    rqa = []

    def _rqa(req, qid):
        rqa.append(qid)
        raise HTTPException(status_code=403, detail="no")
    _install(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object,
         "Optional": typing.Optional, "GenerateTopicForKwRequest": object}
    fn = _load("api_generate_topic_for_keyword", g)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(7, 99, _Req(), None))      # quote_id=7, keyword_id=99, http_request, body=None
    assert ei.value.status_code == 403 and rqa == [7]


def test_research_competitors_guard_before_llm(monkeypatch):
    rqa = []

    def _rqa(req, qid):
        rqa.append(qid)
        raise HTTPException(status_code=403, detail="no")
    _install(monkeypatch, "auth.brand_access", require_quote_access=_rqa)
    g = {"HTTPException": HTTPException, "logger": _LOG, "Request": object}
    fn = _load("api_research_competitors", g)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fn(7, _Req()))
    assert ei.value.status_code == 403 and rqa == [7]


# ============================================================
# P1 followup · quote_id 归属:鉴权放行的表 == 实际写入的表
# ============================================================
def test_ff_api_add_keyword_normalizes_quote_id(monkeypatch):
    """只传 quote_id(brand_id/client_id 皆空)→ add_keyword 必须收到 client_id=str(quote_id),
    否则写孤儿 extra_keywords(鉴权按 quote_id 放行但归属丢失)。"""
    from api import monitoring_api as mod
    captured = {}
    monkeypatch.setattr(mod, "add_keyword", lambda **k: captured.update(k) or 1)
    req = types.SimpleNamespace(brand_id=None, quote_id=88, client_id=None,
                                keyword="k", target_brand="b", difficulty="中等",
                                target_rate=60, platforms="x", monitoring_query=None)
    mod.api_add_keyword(req)         # 后续 log_operation/_resolve_id 失败不影响:add_keyword 已先被调用
    assert captured.get("client_id") == "88"
    assert captured.get("brand_id") is None


def test_ff_api_add_keyword_explicit_client_id_priority(monkeypatch):
    """显式 client_id 优先于 quote_id 兜底(向后兼容)。"""
    from api import monitoring_api as mod
    captured = {}
    monkeypatch.setattr(mod, "add_keyword", lambda **k: captured.update(k) or 1)
    req = types.SimpleNamespace(brand_id=None, quote_id=88, client_id="77",
                                keyword="k", target_brand="b", difficulty="中等",
                                target_rate=60, platforms="x", monitoring_query=None)
    mod.api_add_keyword(req)
    assert captured.get("client_id") == "77"


def test_ff_db_add_keyword_writes_quote_id_from_client(monkeypatch):
    """db.add_keyword(client_id='88') → extra_keywords INSERT 的 quote_id == 88(老板指定用例)。"""
    from db import monitoring_db as mdb
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            if "INSERT INTO extra_keywords" in sql:
                captured["insert_params"] = params

        def fetchone(self):
            return {"id": 1, "brand_id": 5, "seed_keywords": None}

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(mdb, "get_connection", lambda: _Conn())
    mdb.add_keyword(brand_id=None, client_id="88", keyword="k", target_brand="b", platforms="x")
    assert captured["insert_params"][0] == 88     # extra_keywords INSERT 第 1 占位 = quote_id
