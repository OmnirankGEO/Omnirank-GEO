from starlette.requests import Request

from api import meijiehezi_api
from db import meijiehezi_db


class _StatsCursor:
    def __init__(self):
        self.query = ""
        self.params = []

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchone(self):
        return {
            "total": 68,
            "completed": 17,
            "in_progress": 0,
            "pending": 0,
            "rejected": 25,
            "withdrawn": 26,
            "refunded": 0,
        }


class _StatsConnection:
    def __init__(self):
        self.cursor_instance = _StatsCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def _request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def test_synced_order_stats_are_one_user_scoped_aggregate(monkeypatch):
    conn = _StatsConnection()
    monkeypatch.setattr(meijiehezi_db, "_get_conn", lambda: conn)

    result = meijiehezi_db.get_mhz_synced_order_stats(user_id=24)

    assert result == {
        "total": 68,
        "completed": 17,
        "in_progress": 0,
        "pending": 0,
        "rejected": 25,
        "withdrawn": 26,
        "refunded": 0,
    }
    assert "WHERE user_id = %s" in conn.cursor_instance.query
    assert conn.cursor_instance.params == [24]
    assert conn.closed is True


async def test_mhz_orders_returns_list_and_full_stats_in_one_response(monkeypatch):
    list_calls = []
    stats_calls = []

    monkeypatch.setattr(
        meijiehezi_api,
        "_get_user",
        lambda request: {"user_id": 24, "is_admin": False},
    )
    monkeypatch.setattr(
        meijiehezi_api,
        "list_mhz_synced_orders",
        lambda **kwargs: list_calls.append(kwargs) or {
            "orders": [{"id": "order-1", "user_id": 24}],
            "total": 68,
            "page": 1,
            "pages": 4,
        },
    )
    monkeypatch.setattr(
        meijiehezi_api,
        "get_mhz_synced_order_stats",
        lambda user_id: stats_calls.append(user_id) or {
            "total": 68,
            "completed": 17,
            "in_progress": 0,
            "pending": 0,
            "rejected": 25,
            "withdrawn": 26,
            "refunded": 0,
        },
    )

    result = await meijiehezi_api.api_mhz_orders(_request(), page=1, limit=20)

    assert result["status"] == "success"
    assert result["total"] == 68
    assert result["stats"]["total"] == 68
    assert list_calls == [{
        "user_id": 24,
        "page": 1,
        "limit": 20,
        "status": "",
        "brand_id": None,
        "search": "",
        "media_type": "",
    }]
    assert stats_calls == [24]
