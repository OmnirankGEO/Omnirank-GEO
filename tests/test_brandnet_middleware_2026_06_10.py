# -*- coding: utf-8 -*-
"""#12 全局品牌隔离安全网(audit P2 · 红线 auth/middleware.py · 老板已批 · 2026-06-10):
旧网两缺陷:①空 client_brand_ids 跳过(owner 直连用户=多数代理 → 网形同虚设)②有分配用户访问自己
owner 品牌被误 403。新网 = 与 require_brand_access 同语义(分配 OR owner·owner 60s 缓存·异常 fail-open)
+ _extract_brand_id 扩 query quote_id/client_id 反查(永久缓存·fail-open)。"""
import time
from pathlib import Path
from unittest.mock import patch

import auth.middleware as mw

ROOT = Path(__file__).resolve().parents[1]


def _clear_caches():
    mw._quote_brand_cache.clear()
    mw._owner_net_cache.clear()


# ---------- owner 判定矩阵(值级) ----------

def test_owner_check_owner_passes():
    _clear_caches()
    with patch.object(mw, "_resolve_quote_brand_cached"):
        pass
    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return {"owner_user_id": 46}
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    with patch("db.connection.get_connection", return_value=_Conn()):
        assert mw._is_brand_owner_cached_failopen(46, 99) is True   # owner 放行
    _clear_caches()
    with patch("db.connection.get_connection", return_value=_Conn()):
        assert mw._is_brand_owner_cached_failopen(7, 99) is False   # 非 owner 拒


def test_owner_check_failopen_on_db_error():
    _clear_caches()
    with patch("db.connection.get_connection", side_effect=RuntimeError("db down")):
        assert mw._is_brand_owner_cached_failopen(7, 99) is True, "DB 抖动必须 fail-open(网是第二道·不可炸全站)"


def test_owner_check_missing_info_failopen():
    assert mw._is_brand_owner_cached_failopen(None, 99) is True
    assert mw._is_brand_owner_cached_failopen(7, None) is True


def test_owner_check_uses_ttl_cache():
    _clear_caches()
    calls = {"n": 0}
    class _Cur:
        def execute(self, *a): calls["n"] += 1
        def fetchone(self): return {"owner_user_id": 46}
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    with patch("db.connection.get_connection", return_value=_Conn()):
        mw._is_brand_owner_cached_failopen(46, 88)
        mw._is_brand_owner_cached_failopen(46, 88)
    assert calls["n"] == 1, "60s 内第二次必须命中缓存(全局中间件不能每请求查 DB)"


# ---------- quote→brand 反查(值级) ----------

def test_quote_brand_resolve_and_permanent_cache():
    _clear_caches()
    calls = {"n": 0}
    class _Cur:
        def execute(self, *a): calls["n"] += 1
        def fetchone(self): return {"brand_id": 31}
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    with patch("db.connection.get_connection", return_value=_Conn()):
        assert mw._resolve_quote_brand_cached(282) == 31
        assert mw._resolve_quote_brand_cached(282) == 31
    assert calls["n"] == 1, "quote→brand 不可变 · 必须永久缓存"
    _clear_caches()
    with patch("db.connection.get_connection", side_effect=RuntimeError("db down")):
        assert mw._resolve_quote_brand_cached(283) is None  # fail-open


# ---------- _extract_brand_id 扩展(值级·mock Request) ----------

class _FakeRequest:
    def __init__(self, query=None, path="/api/x"):
        self.query_params = query or {}
        class _U:
            pass
        self.url = _U()
        self.url.path = path


def test_extract_query_brand_id_first():
    assert mw._extract_brand_id(_FakeRequest({"brand_id": "12"})) == 12


def test_extract_quote_and_client_id_resolve():
    _clear_caches()
    with patch.object(mw, "_resolve_quote_brand_cached", return_value=31) as rq:
        assert mw._extract_brand_id(_FakeRequest({"quote_id": "282"})) == 31
        assert mw._extract_brand_id(_FakeRequest({"client_id": "282"})) == 31
    assert rq.call_count == 2
    # 非数字 client_id(legacy 字符串)跳过不反查
    with patch.object(mw, "_resolve_quote_brand_cached", return_value=31) as rq2:
        assert mw._extract_brand_id(_FakeRequest({"client_id": "abc"})) is None
    assert rq2.call_count == 0


def test_extract_client_context_path_kept():
    assert mw._extract_brand_id(_FakeRequest({}, path="/api/client-context/55")) == 55


# ---------- 安全网源码契约 ----------

def test_net_source_contract():
    src = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")
    # 空分配不再整段跳过(旧 `if allowed_brands:` 包住网的写法必须移除)
    i = src.find("# 8. 非管理员的 brand_id 数据隔离")
    blk = src[i:i + 3200]
    # [audit #6 返修] 调用改 await asyncio.to_thread(_is_brand_owner_cached_failopen, ...)(逗号形式)
    assert "_is_brand_owner_cached_failopen" in blk, "网必须带 owner 语义(分配 OR owner)"
    assert "if allowed_brands:  # 有分配限制才检查" not in blk, "空分配跳过的旧写法必须移除"
    assert "BRAND_ACCESS_DENIED" in blk


# ---------- #6 Fable 返修(2026-06-10)----------

def test_6_malformed_user_id_failopen_not_500():
    """[#6 返修 行为] 畸形 user_id/brand_id(非数字 JWT subject)→ int() 不再在 try 外抛 500,
    fail-open 返 True(与本函数'一切异常 fail-open'承诺一致)。"""
    assert mw._is_brand_owner_cached_failopen("not-a-number", 99) is True
    assert mw._is_brand_owner_cached_failopen(7, "xyz") is True
    assert mw._is_brand_owner_cached_failopen("abc", "def") is True


def test_6_claim_correction_documented():
    """[#6 返修] claim 过誉更正:注释须明示本网只覆盖 query 向量,不覆盖 path-id / POST body,
    路径越权靠端点级(#2 已收紧 distill/publications fail-closed)。"""
    src = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")
    i = src.find("# 8. 非管理员的 brand_id 数据隔离")
    blk = src[i:i + 2400]
    assert "path-id" in blk, "须标注不覆盖 path-id"
    assert "POST body" in blk
    assert "#2" in blk, "须指明路径越权由 #2 端点级兜底"


def test_6_sync_helpers_wrapped_to_thread():
    """[#6 返修] 两个 sync DB helper 在 async 中间件里包 asyncio.to_thread(WORKERS=1 防阻塞 loop)。"""
    src = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")
    assert "import asyncio" in src
    assert "await asyncio.to_thread(_extract_brand_id, request)" in src
    assert "await asyncio.to_thread(_is_brand_owner_cached_failopen," in src
