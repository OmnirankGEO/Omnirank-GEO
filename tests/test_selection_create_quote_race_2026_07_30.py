"""P1-A 锁 · 报价选词链接创建在「同 quote_id 并发/重复提交」下不得回 500。

生产实证(2026-07-30 17:55:19-24 · omnirank.top 宿主 nginx 访问日志):
  同一 6 秒窗口 7 次 POST /api/keyword-selection/create,3 次 200(1 次新建 123B +
  2 次幂等命中 134B)、**4 次 500 且响应体恒为 21 字节**。21 字节 =
  Starlette ServerErrorMiddleware 的 PlainTextResponse("Internal Server Error"),
  即未捕获异常(不是 HTTPException)。落库侧:该窗口只新建了 1 条 session
  (id=208 / quote_id=409),而 keyword_selection_sessions 上有
  UNIQUE CONSTRAINT keyword_selection_sessions_quote_id_key (quote_id)。

根因:方式 1(带 quote_id)是 check-then-insert,且 check 与 insert 之间夹了一次
LLM await(extract_business_lines,秒级)—— 同一批并发全部通过前置检查,
只有第一个 INSERT 成功,其余 UniqueViolation 直接漏成 500。

本文件锁住两件事:
  1. 输给并发的那几个请求,必须回幂等的 already_exists 200(赢家的 token),
     不得回 500;
  2. **不得把唯一冲突一律吞掉** —— 只有"该 quote 确实已有 session"才折叠;
     token 撞车之类的唯一冲突必须原样抛出。

判别力反向对照(不是只证明会过):
  - test_*_reraised_when_no_winner_row / test_*_token_collision_* 反证不是无脑吞;
  - test_archived_quote_still_409 / test_unknown_value_error_propagates 反证
    没顺手改坏既有分支。
"""

import asyncio
import types

import pytest
from fastapi import HTTPException
from psycopg2.errors import UniqueViolation

from api import selection_api
from db import diagnosis_db


# ----------------------------------------------------------------------------
# 第一层:db.diagnosis_db.create_selection_session 把 UniqueViolation 翻成幂等信号
# ----------------------------------------------------------------------------

class _FakeCursor:
    """按调用顺序回放:SELECT FOR UPDATE → INSERT(抛) → SELECT winner。"""

    def __init__(self, *, insert_raises, winner_row):
        self._insert_raises = insert_raises
        self._winner_row = winner_row
        self._last = None
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        head = sql.strip().split()[0].upper()
        if head == "SELECT" and "FOR UPDATE" in sql:
            self._last = {"id": 409}
            return
        if head == "INSERT":
            if self._insert_raises is not None:
                raise self._insert_raises
            self._last = {"id": 9001}
            return
        if head == "SELECT":
            self._last = self._winner_row
            return
        raise AssertionError(f"未预期的语句: {sql}")

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False
        self.committed = False
        self.closed = 0

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def close(self):
        self.closed += 1


def _run_create(monkeypatch, *, insert_raises, winner_row):
    cursor = _FakeCursor(insert_raises=insert_raises, winner_row=winner_row)
    conn = _FakeConn(cursor)
    monkeypatch.setattr(diagnosis_db, "get_connection", lambda: conn)
    return conn, cursor


def test_unique_violation_with_existing_session_becomes_idempotent_signal(monkeypatch):
    conn, _ = _run_create(
        monkeypatch, insert_raises=UniqueViolation("dup"), winner_row={"id": 208}
    )
    with pytest.raises(ValueError) as err:
        diagnosis_db.create_selection_session(
            token="loser-token",
            quote_id=409,
            brand_id=706,
            created_by=1,
            keywords_snapshot="[]",
            expires_at="2026-08-06 17:55:19",
        )
    assert str(err.value) == "SESSION_EXISTS_FOR_QUOTE"
    # 中止的事务必须显式回滚,否则同一连接后面那条 SELECT 会 25P02
    assert conn.rolled_back is True


def test_unique_violation_reraised_when_no_winner_row(monkeypatch):
    """判别力:该 quote 并没有已存在的 session → 不许吞,原样抛 UniqueViolation。"""
    _run_create(monkeypatch, insert_raises=UniqueViolation("dup"), winner_row=None)
    with pytest.raises(UniqueViolation):
        diagnosis_db.create_selection_session(
            token="loser-token",
            quote_id=409,
            brand_id=706,
            created_by=1,
            keywords_snapshot="[]",
            expires_at="2026-08-06 17:55:19",
        )


def test_happy_path_still_returns_new_session_id(monkeypatch):
    """判别力:没有唯一冲突时,行为与改动前完全一致(照常 commit 并返新 id)。"""
    conn, _ = _run_create(monkeypatch, insert_raises=None, winner_row=None)
    session_id = diagnosis_db.create_selection_session(
        token="fresh-token",
        quote_id=410,
        brand_id=706,
        created_by=1,
        keywords_snapshot="[]",
        expires_at="2026-08-06 17:55:19",
    )
    assert session_id == 9001
    assert conn.committed is True
    assert conn.rolled_back is False


def test_archived_quote_signal_is_untouched(monkeypatch):
    """判别力:既有的 QUOTE_ARCHIVED_OR_SCOPE_CHANGED 分支没被改动。"""
    cursor = _FakeCursor(insert_raises=None, winner_row=None)

    def _no_quote(sql, params=None):
        cursor.statements.append(" ".join(sql.split()))
        cursor._last = None

    conn = _FakeConn(cursor)
    monkeypatch.setattr(diagnosis_db, "get_connection", lambda: conn)
    monkeypatch.setattr(cursor, "execute", _no_quote)
    with pytest.raises(ValueError) as err:
        diagnosis_db.create_selection_session(
            token="t",
            quote_id=999999,
            brand_id=706,
            created_by=1,
            keywords_snapshot="[]",
            expires_at="2026-08-06 17:55:19",
        )
    assert str(err.value) == "QUOTE_ARCHIVED_OR_SCOPE_CHANGED"


# ----------------------------------------------------------------------------
# 第二层:api/selection_api.create_selection_link 把幂等信号翻回 200
# ----------------------------------------------------------------------------

WINNER = {
    "token": "sdJAQt3ez_4ZCTP9",
    "expires_at": "2026-08-06 17:55:19",
    "status": "selecting",
}


def _wire_endpoint(monkeypatch, *, create_raises, session_by_quote):
    """把方式 1 那条链路上的外部依赖全部替掉,只留被测的异常翻译逻辑。"""
    import auth.brand_access as brand_access
    import tools.keyword_cluster as keyword_cluster

    monkeypatch.setattr(
        brand_access, "require_quote_access", lambda *a, **k: {"brand_id": 706}
    )
    monkeypatch.setattr(
        selection_api, "get_quote",
        lambda qid: {"brand_id": 706, "brand_name": "杭州聆溪信息科技有限公司",
                     "industry": "信息技术", "city": "全国"},
    )
    monkeypatch.setattr(
        selection_api, "get_keywords_by_quote",
        lambda qid: [{"id": 1, "keyword": "杭州语音识别厂商哪家好",
                      "category": "general", "intent": "commercial"}],
    )

    async def _no_business_lines(**kwargs):
        return []

    monkeypatch.setattr(keyword_cluster, "extract_business_lines", _no_business_lines)

    calls = {"by_quote": 0}

    def _by_quote(qid):
        calls["by_quote"] += 1
        return session_by_quote(calls["by_quote"])

    monkeypatch.setattr(selection_api, "get_session_by_quote", _by_quote)

    def _create(**kwargs):
        raise create_raises

    monkeypatch.setattr(selection_api, "create_selection_session", _create)
    return calls


def _call_endpoint():
    req = selection_api.CreateSessionRequest(quote_id=409)
    request = types.SimpleNamespace(state=types.SimpleNamespace(user={"user_id": 1}))
    return asyncio.run(selection_api.create_selection_link(req, request))


def test_race_loser_gets_idempotent_200_not_500(monkeypatch):
    """核心锁:输给并发的请求回赢家 token 的 already_exists,而不是 500。"""
    _wire_endpoint(
        monkeypatch,
        create_raises=ValueError("SESSION_EXISTS_FOR_QUOTE"),
        # 第 1 次(前置检查)看不到 → 才会走到 INSERT;第 2 次(冲突后重读)看到赢家
        session_by_quote=lambda n: None if n == 1 else dict(WINNER),
    )
    result = _call_endpoint()
    assert result["already_exists"] is True
    assert result["token"] == WINNER["token"]
    assert result["url"] == f"/s/{WINNER['token']}"
    assert result["status"] == "selecting"
    assert result["quote_id"] == 409


def test_race_signal_without_winner_propagates(monkeypatch):
    """判别力:冲突后重读仍查不到赢家 → 不许编造 200,原样抛。"""
    _wire_endpoint(
        monkeypatch,
        create_raises=ValueError("SESSION_EXISTS_FOR_QUOTE"),
        session_by_quote=lambda n: None,
    )
    with pytest.raises(ValueError):
        _call_endpoint()


def test_archived_quote_still_409(monkeypatch):
    """判别力:既有 409 契约(code/priority/recovery)没被改动。"""
    _wire_endpoint(
        monkeypatch,
        create_raises=ValueError("QUOTE_ARCHIVED_OR_SCOPE_CHANGED"),
        session_by_quote=lambda n: None,
    )
    with pytest.raises(HTTPException) as err:
        _call_endpoint()
    assert err.value.status_code == 409
    assert err.value.detail["code"] == "QUOTE_ARCHIVED_OR_SCOPE_CHANGED"


def test_unknown_value_error_propagates(monkeypatch):
    """判别力:没把所有 ValueError 都当成幂等信号。"""
    _wire_endpoint(
        monkeypatch,
        create_raises=ValueError("SOMETHING_ELSE_ENTIRELY"),
        session_by_quote=lambda n: None,
    )
    with pytest.raises(ValueError) as err:
        _call_endpoint()
    assert str(err.value) == "SOMETHING_ELSE_ENTIRELY"
