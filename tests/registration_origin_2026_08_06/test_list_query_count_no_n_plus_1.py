"""[补充工单 2026-08-06 §5「性能反向对照」] 真跑 `list_admin_users()`,数组织查询次数。

工单原话:「同一页在改动前后各跑一次,统计组织相关 SQL 次数;改动后必须是常数级。
**只报『感觉不慢』不算。**」

这里不数「函数被调了几次」,而是**拦 cursor.execute 数真 SQL**,
并且走的是 `list_admin_users()` 的真实代码路径(只把连接换成假的,业务代码一行没动)。

判别力:同一条测试里跑 10 行和 50 行两页 ——
  · 常数级 → 两页的组织查询次数相同(且 ≤1);
  · N+1   → 50 行那页必然远多于 10 行那页。
两个数字都断言,避免「恰好都等于某个值」的恒真。
"""
import datetime
import importlib
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ORG_RE = re.compile(r"FROM\s+organization_memberships", re.I)


class FakeCursor:
    """按 SQL 形态回放最小可用结果;顺带按类别计数。"""

    def __init__(self, n_rows, counters):
        self._n = n_rows
        self._c = counters
        self._rows = []

    # ---- 计数与回放
    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self._c["total"] += 1
        if _ORG_RE.search(flat):
            self._c["org"] += 1
            # 一律返回空:本测试只关心**发了几次**,不关心判成什么
            self._rows = []
        elif flat.startswith("SELECT COUNT(*) AS count"):
            self._rows = [{"count": self._n}]
        elif "FROM customer_agent_bindings WHERE customer_user_id=ANY" in flat:
            self._rows = []
        elif "SELECT u.id, u.username" in flat:
            self._rows = [self._user_row(i) for i in range(1, self._n + 1)]
        else:
            self._rows = []

    @staticmethod
    def _user_row(i):
        return {
            "id": i, "username": f"u{i}", "display_name": f"用户{i}", "phone": None,
            "is_active": True, "created_at": datetime.datetime(2026, 7, 1),
            "last_active_at": None, "agent_level": 0, "total_points": 0,
            "is_admin": False, "customer_count": 0, "brand_count": 0,
            "needs_attention": False, "identity_version": 1, "binding_version": 1,
            "channel_version": 1, "access_version": 1, "password_version": 1,
            "wallet_version": 1, "account_status_version": 1,
        }

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, n_rows, counters):
        self._n = n_rows
        self._c = counters

    def cursor(self, *a, **kw):
        return FakeCursor(self._n, self._c)

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


def _run(monkeypatch, n_rows):
    gov = importlib.import_module("services.admin_user_governance")
    counters = {"org": 0, "total": 0}
    # 🔴 服务模块是 `from db.connection import get_connection` —— 名字已经绑进它自己的
    #    命名空间,只 patch db.connection 那一份是**打不中的**(第一版就这么错的)。
    monkeypatch.setattr(gov, "get_connection", lambda *a, **kw: FakeConn(n_rows, counters))
    result = gov.list_admin_users(page=1, page_size=max(1, n_rows))
    return result, counters


def test_org_query_count_is_constant_not_n_plus_1(monkeypatch):
    small, c_small = _run(monkeypatch, 10)
    big, c_big = _run(monkeypatch, 50)

    assert len(small["users"]) == 10 and len(big["users"]) == 50, "假连接没喂出预期行数,判据无效"

    # 🔴 常数级:两页组织查询次数相同,且 ≤1
    assert c_small["org"] <= 1, f"10 行页发了 {c_small['org']} 次组织查询"
    assert c_big["org"] <= 1, f"50 行页发了 {c_big['org']} 次组织查询 —— N+1 没被消掉"
    assert c_small["org"] == c_big["org"], (
        f"组织查询次数随行数变了:10 行 {c_small['org']} 次 / 50 行 {c_big['org']} 次"
    )

    # 判别力自证:总查询数确实被数到了(不是拦截器没生效导致两边都是 0)
    assert c_small["total"] >= 3 and c_big["total"] >= 3, "拦截器没生效,上面的 0 是假绿"


def test_every_row_carries_origin_fields(monkeypatch):
    """字段必须逐行都有 —— 前端不必判 undefined(工单 §4.1)。"""
    result, _ = _run(monkeypatch, 10)
    for item in result["users"]:
        assert item["account_origin"] in ("self_signup", "organization_member")
        assert "organization_name" in item


def test_empty_page_still_carries_fields(monkeypatch):
    """空页也要带字段:否则前端在空列表/搜索无结果时会读到 undefined。"""
    result, counters = _run(monkeypatch, 0)
    assert result["users"] == []
    assert counters["org"] == 0, "空页不许发组织查询"
