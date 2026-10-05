"""[v5 req5 · Deploy-CTO 2026-07-13] 测试库安全护栏 · DB 行为测试统一入口。

铁律(防止破坏性测试误打生产库):
  1. DB 行为测试【只读 TEST_DATABASE_URL】· 禁止回退 DATABASE_URL
     (app init 需要 DATABASE_URL,但那是"指向哪个库跑 app",不能拿它当"测试库=可清可 DROP"的依据)。
  2. URL 必须指向本机(localhost / 127.0.0.1 / ::1)【且】库名含 test/throwaway。
     两条都满足才允许连接;否则 raise(拒绝),不静默降级。
  3. 清表 / DROP / TRUNCATE 等破坏性操作前,必须显式 ALLOW_DESTRUCTIVE_TEST_DB=1。

用法:
  from tests.regression._dbsafe import resolve_test_db_url, require_destructive_allowed
  DB = resolve_test_db_url()                 # None → 测试各自 skip(pytest.mark.skipif(not DB))
  ...
  def _clean():
      require_destructive_allowed()          # 清表前
      _assert_safe_test_db(DB)               # 双保险:清表目标也必须是安全测试库
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}  # "" = unix socket / 省略 host(本机)


def _parse(url: str):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    dbname = (p.path or "").lstrip("/").lower()
    return host, dbname


def is_safe_test_db(url: str | None) -> bool:
    """URL 是否是"本机 + 库名含 test/throwaway"的安全测试库。None/空 → False。"""
    if not url:
        return False
    host, dbname = _parse(url)
    if host not in _LOCAL_HOSTS:
        return False
    if not ("test" in dbname or "throwaway" in dbname):
        return False
    return True


def _assert_safe_test_db(url: str | None) -> None:
    """不安全则 raise(拒绝),不静默降级。"""
    host, dbname = _parse(url or "")
    if not url:
        raise RuntimeError("[dbsafe] 未提供测试库 URL")
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(
            f"[dbsafe] 拒绝:测试库 host 必须本机(localhost/127.0.0.1/::1),实际 {host!r} · "
            f"绝不对远程/生产库跑破坏性测试"
        )
    if not ("test" in dbname or "throwaway" in dbname):
        raise RuntimeError(
            f"[dbsafe] 拒绝:测试库名必须含 test/throwaway,实际 {dbname!r} · 防误打生产库"
        )


def resolve_test_db_url() -> str | None:
    """DB 行为测试唯一取库入口:【只读 TEST_DATABASE_URL】· 禁止回退 DATABASE_URL。

    - 未设 TEST_DATABASE_URL → 返 None(调用方 skipif(not DB) 跳过);
    - 设了但不安全(非本机 / 库名不含 test/throwaway)→ raise(拒绝,不降级)。
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return None
    _assert_safe_test_db(url)
    return url


def require_destructive_allowed() -> None:
    """破坏性操作(清表/DROP/TRUNCATE)前调:未显式 ALLOW_DESTRUCTIVE_TEST_DB=1 则 raise。"""
    if os.environ.get("ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        raise RuntimeError(
            "[dbsafe] 破坏性测试操作(清表/DROP/TRUNCATE)被拒:需显式 ALLOW_DESTRUCTIVE_TEST_DB=1 · "
            "防止误在非预期库上清数据"
        )
