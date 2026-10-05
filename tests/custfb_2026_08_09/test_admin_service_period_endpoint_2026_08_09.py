"""客户反馈⑥ · admin 服务期更正端点 —— 资金邻接件行为锁(Review 复交要求)。

被测:`PATCH /api/monitoring/clients/{quote_id}/service-period`(定义在 `server.py`)。

判据全部打在**真跑一遍那个 handler + 真 PostgreSQL** 上:
真库、真 UPDATE、真 audit_log,不做源码字符串断言。

  锁1  403:非 admin 一律拒 —— 且**库里那一行一个字没动**
       (只断 403 不够:先写后判的实现照样能返 403 而数据已改)
  锁2  400 CONFIRM_REQUIRED:没带 confirm=true 不许写 —— 同样要证明没写
  锁3  target 一致:核验的 quote_id = 写入的 quote_id;**同库里的另一张单必须原封不动**
  锁4  审计留痕:audit_logs 落一行 action=quote_service_period_admin_fix,
       before/after 都是真值(不是空壳、不是同值)
  锁5  反向对照:admin + confirm 齐 → 真写成功(否则上面三条可能只是恒拒 = 零判别力)
  锁6  (start,end) **成对**写:service_months 走服务期 SSOT 换算,不许只写一个
  锁7  end < start → 400 SERVICE_PERIOD_INVERTED
  锁8  白名单:请求体里塞别的 quotes 列(status/total_price/service_days)一律被忽略
       —— 资金表邻接件,不许顺手改别的列

🔴 本文件**必须在导入任何项目模块之前**把 DATABASE_URL 指到一次性测试库:
   `server.py` 在 import 期就会建表/初始化连接池,指错就是往别的库里建表。
🔴 导入 server.py 约 23 秒(它 import 整棵路由树),这是本文件跑得比别的慢的原因。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


_skip_unless_throwaway_pg()

# ── 一次性库:必须在 import server 之前建好并钉进 DATABASE_URL ──────────────
import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

_admin_conn = psycopg2.connect(PG_URL)
_admin_conn.autocommit = True
_DBNAME = f"custfb_sp_test_{uuid.uuid4().hex[:10]}"
assert "test" in _DBNAME and "prod" not in _DBNAME
with _admin_conn.cursor() as _c:
    _c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_DBNAME)))
_parsed = urlsplit(PG_URL)
_DBURL = urlunsplit((_parsed.scheme, _parsed.netloc, f"/{_DBNAME}", _parsed.query, _parsed.fragment))
os.environ["DATABASE_URL"] = _DBURL

import server  # noqa: E402  ← 必须在 DATABASE_URL 钉好之后


def _conn():
    return psycopg2.connect(_DBURL, cursor_factory=RealDictCursor)


QUOTE_TARGET = 900001
QUOTE_BYSTANDER = 900002
ORIG_START, ORIG_END = "2026-01-01", "2026-02-01"


@pytest.fixture(scope="module", autouse=True)
def _seed_and_teardown():
    with _conn() as c:
        with c.cursor() as cur:
            # `service_days` 在生产由 migration_028 保证(NOT NULL);
            # 运行时建表兜底不含它 —— 这里补上,让"白名单不许改 service_days"那条锁有东西可断。
            cur.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS service_days INTEGER")
            cur.execute(
                "INSERT INTO quotes (id, brand_name, service_start_date, service_end_date, service_days)"
                " VALUES (%s,'目标单',%s,%s,30), (%s,'旁观单',%s,%s,30)"
                " ON CONFLICT (id) DO NOTHING",
                (QUOTE_TARGET, ORIG_START, ORIG_END, QUOTE_BYSTANDER, ORIG_START, ORIG_END),
            )
        c.commit()
    yield
    with _admin_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (_DBNAME,))
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(_DBNAME)))
    _admin_conn.close()


@pytest.fixture(autouse=True)
def _reset_rows():
    """每条用例前把两张单恢复原状 —— 否则"没被改"这类判据会被上一条用例污染。"""
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE quotes SET service_start_date=%s, service_end_date=%s WHERE id IN (%s,%s)",
                (ORIG_START, ORIG_END, QUOTE_TARGET, QUOTE_BYSTANDER))
            cur.execute("DELETE FROM audit_logs WHERE action='quote_service_period_admin_fix'")
        c.commit()
    yield


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user, body):
        self.state = _FakeState(user)
        self.client = None
        self._body = body

    async def json(self):
        return self._body


def _call(user, body, quote_id=QUOTE_TARGET):
    import asyncio

    return asyncio.run(server.admin_update_service_period(quote_id, _FakeRequest(user, body)))


def _period(quote_id):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT service_start_date, service_end_date FROM quotes WHERE id=%s", (quote_id,))
            r = cur.fetchone()
    return str(r["service_start_date"]), str(r["service_end_date"])


def _audit_rows():
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT user_id, action, entity_type, entity_id, summary, before_snapshot, after_snapshot"
                "  FROM audit_logs WHERE action='quote_service_period_admin_fix' ORDER BY id")
            return [dict(r) for r in cur.fetchall()]


ADMIN = {"user_id": 1, "username": "admin", "is_admin": True}
AGENT = {"user_id": 42, "username": "agent", "is_admin": False}
GOOD_BODY = {"confirm": True, "service_start_date": "2026-03-01", "service_end_date": "2026-09-01"}


# ── 锁1 · 403 且没写 ─────────────────────────────────────────────────────
def test_non_admin_gets_403_and_writes_nothing():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(AGENT, GOOD_BODY)
    assert exc.value.status_code == 403
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END), "403 了,数据却被改了"
    assert _audit_rows() == []


def test_missing_user_gets_403():
    """反向对照的另一半:request.state.user 缺失(未登录)同样拒。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(None, GOOD_BODY)
    assert exc.value.status_code == 403
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END)


# ── 锁2 · confirm 必填 ───────────────────────────────────────────────────
@pytest.mark.parametrize("body_patch", [{}, {"confirm": False}, {"confirm": "true"}])
def test_confirm_required_and_writes_nothing(body_patch):
    """`confirm` 必须是**布尔真**:漏传、传 False、传字符串 "true" 都不算。"""
    from fastapi import HTTPException

    body = dict(GOOD_BODY)
    body.pop("confirm", None)
    body.update(body_patch)
    with pytest.raises(HTTPException) as exc:
        _call(ADMIN, body)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_REQUIRED"
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END), "没确认,数据却被改了"
    assert _audit_rows() == []


# ── 锁5(先证正向,否则上面全可能是恒拒) ────────────────────────────────
def test_admin_with_confirm_actually_writes():
    got = _call(ADMIN, GOOD_BODY)
    assert got["status"] == "success" and got["quote_id"] == QUOTE_TARGET
    assert _period(QUOTE_TARGET) == ("2026-03-01", "2026-09-01")


# ── 锁3 · 核验 target = 写 target ────────────────────────────────────────
def test_only_the_requested_quote_is_written():
    _call(ADMIN, GOOD_BODY)
    assert _period(QUOTE_TARGET) == ("2026-03-01", "2026-09-01")
    assert _period(QUOTE_BYSTANDER) == (ORIG_START, ORIG_END), "写串了:旁观单被改"


def test_body_cannot_redirect_the_write_target():
    """🔴 核验 target = 写 target 的**正面攻击**:body 里塞一个别的 `quote_id`。

    被授权/被核验的是**路径**上的那个 id;写入必须还落在它身上。
    如果实现哪天变成 `WHERE id = body['quote_id']`,就是一条"查 A 写 B"的缝
    —— 而光靠上面那条"旁观单没被改"是抓不到的(那条里 body 根本没有 quote_id,
    变异等于空操作)。这条用例的存在就是为了让那种变异必须转红。
    """
    _call(ADMIN, dict(GOOD_BODY, quote_id=QUOTE_BYSTANDER))
    assert _period(QUOTE_TARGET) == ("2026-03-01", "2026-09-01"), "路径上的那张单没被写"
    assert _period(QUOTE_BYSTANDER) == (ORIG_START, ORIG_END), \
        "body 里的 quote_id 把写入重定向了 —— 查 A 写 B"
    rows = _audit_rows()
    assert len(rows) == 1 and rows[0]["entity_id"] == QUOTE_TARGET, \
        f"审计记的实体与真正被写的那张单不一致:{rows}"


def test_unknown_quote_404_and_writes_nothing():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(ADMIN, GOOD_BODY, quote_id=999999)
    assert exc.value.status_code == 404
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END)


# ── 锁4 · 审计留痕 ───────────────────────────────────────────────────────
def test_audit_trail_records_real_before_and_after():
    _call(ADMIN, dict(GOOD_BODY, reason="线下续费半年"))
    rows = _audit_rows()
    assert len(rows) == 1, f"审计行数不对:{rows}"
    row = rows[0]
    assert row["user_id"] == ADMIN["user_id"]
    assert row["entity_type"] == "quote" and row["entity_id"] == QUOTE_TARGET
    assert "线下续费半年" in (row["summary"] or "")
    # before/after 必须是**真值且不同** —— 同值或空壳等于没留痕
    assert ORIG_END in (row["before_snapshot"] or "")
    assert "2026-09-01" in (row["after_snapshot"] or "")
    assert row["before_snapshot"] != row["after_snapshot"]


# ── 锁15-16 · [Review P1-2] 审计失败必须整体回滚,且不许返成功 ────────────
def test_audit_failure_rolls_back_the_whole_change(monkeypatch):
    """故障注入:审计 INSERT 抛异常 → 服务期**必须没变**,接口**必须不返成功**。

    🔴 这是本端点自己写的"审计留痕:写失败不吞"那句话的判据。
      改前的实现是"先 commit 业务、再单独写审计,审计异常只记日志仍返 success" ——
      资金邻接字段改了却查无对证,而且调用方以为一切正常。
    🔴 注入点打在 `server` 模块**函数内部 import 到的那个名字**上,
      不是打在 `db.auth_db` 上 —— 后者对已经 `from ... import` 进来的引用无效
      (这类"注入没打中、测试照样绿"的假绿本仓踩过)。
    """
    from fastapi import HTTPException

    import db.auth_db as auth_db

    def _boom(*_a, **_kw):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(auth_db, "write_audit_log_with_cursor", _boom)

    with pytest.raises(HTTPException) as exc:
        _call(ADMIN, GOOD_BODY)
    assert exc.value.status_code == 500
    assert exc.value.detail["code"] == "SERVICE_PERIOD_UPDATE_FAILED"
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END), \
        "审计失败了,服务期却已经落库 —— 不是同一个事务"
    assert _audit_rows() == []


def test_audit_injection_control_is_not_always_red(monkeypatch):
    """反向对照:不注入故障时同一条路径必须正常成功。

    没有这条,上面那条可能只是"端点恒 500"而不是"回滚生效"。
    """
    got = _call(ADMIN, GOOD_BODY)
    assert got["status"] == "success"
    assert _period(QUOTE_TARGET) == ("2026-03-01", "2026-09-01")
    assert len(_audit_rows()) == 1


# ── 锁6 · (start,end) 成对 · months 走 SSOT ─────────────────────────────
def test_service_months_goes_through_ssot_and_writes_the_pair():
    got = _call(ADMIN, {"confirm": True, "service_start_date": "2026-03-15", "service_months": 6})
    assert got["service_start_date"] == "2026-03-15"
    assert got["service_end_date"] == "2026-09-15", "months→end 换算没走服务期 SSOT"
    assert _period(QUOTE_TARGET) == ("2026-03-15", "2026-09-15")


def test_invalid_months_is_rejected_and_writes_nothing():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(ADMIN, {"confirm": True, "service_start_date": "2026-03-15", "service_months": 99})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "SERVICE_MONTHS_INVALID"
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END)


# ── 锁7 · 反了不许写 ─────────────────────────────────────────────────────
def test_inverted_period_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _call(ADMIN, {"confirm": True,
                      "service_start_date": "2026-09-01", "service_end_date": "2026-03-01"})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "SERVICE_PERIOD_INVERTED"
    assert _period(QUOTE_TARGET) == (ORIG_START, ORIG_END)


# ── 锁8 · 白名单:资金表其它列不许被顺手改 ───────────────────────────────
def test_other_quote_columns_are_not_touched():
    """请求体里塞 status / service_days,必须被忽略 —— quotes 是资金表。"""
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE quotes SET status='paid', service_days=30 WHERE id=%s",
                        (QUOTE_TARGET,))
        c.commit()
    _call(ADMIN, dict(GOOD_BODY, status="cancelled", service_days=999, total_price=1))
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT status, service_days FROM quotes WHERE id=%s", (QUOTE_TARGET,))
            row = cur.fetchone()
    assert row["status"] == "paid", "端点顺手改了 quotes.status(资金表越权写)"
    assert row["service_days"] == 30, "端点顺手改了达标配额(那是另一个端点的职责)"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
