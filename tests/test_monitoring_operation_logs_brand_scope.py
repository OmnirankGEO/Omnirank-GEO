from db import monitoring_db as mdb


class FakeCursor:
    def __init__(self):
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return {"id": 42}

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_log_operation_stores_brand_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(mdb, "get_connection", lambda: conn)

    log_id = mdb.log_operation(
        operator_id="admin-1",
        brand_id=615,
        action="add_keyword",
        target_type="keyword",
        target_id=7,
        details={"keyword": "test"},
    )

    sql, params = conn.cursor_obj.executed[-1]
    assert log_id == 42
    assert "brand_id" in sql
    assert params[1] == 615
    assert conn.committed is True


def test_get_operation_logs_filters_by_brand_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(mdb, "get_connection", lambda: conn)

    rows = mdb.get_operation_logs(brand_id=615, limit=20)

    sql, params = conn.cursor_obj.executed[-1]
    assert rows == []
    assert "brand_id = %s" in sql
    assert params == (615, 20)


def test_get_operation_logs_multi_filters_by_brand_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(mdb, "get_connection", lambda: conn)

    rows = mdb.get_operation_logs_multi(["user-1", "615"], target_type="keyword", brand_id=615, limit=50)

    sql, params = conn.cursor_obj.executed[-1]
    assert rows == []
    assert "operator_id IN (%s,%s)" in sql
    assert "target_type = %s" in sql
    assert "brand_id = %s" in sql
    assert params == ("user-1", "615", "keyword", 615, 50)
