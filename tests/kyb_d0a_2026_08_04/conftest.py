"""[WO-KYB-ROUTING D0-a] 目录接口字段白名单 —— 测试替身。

不连库:用 FakeCursor 模拟 RealDictCursor 的行为(取列查询返回
声明的列集,主查询按 SELECT 里出现的列做投影)。这样"接口返回哪些键"这件事
可以被行为断言直接打到,而不是去比对源码字符串。
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def parse_select_columns(sql: str):
    """从 `SELECT a, b FROM t ...` 里取出列名(去掉双引号)。

    只用于测试替身做投影 —— 主查询永远是 `SELECT <显式列> FROM`,
    如果被测代码退回 `SELECT *`,这里会原样返回 ['*'],
    投影就会命中 KeyError/空集,断言随之转红(这是有意的)。
    """
    flat = " ".join(sql.split())
    upper = flat.upper()
    start = upper.index("SELECT ") + len("SELECT ")
    end = upper.index(" FROM ", start)
    return [c.strip().strip('"') for c in flat[start:end].split(",")]


class FakeCursor:
    def __init__(self, schema_columns, rows):
        self.schema_columns = list(schema_columns)
        self.rows = list(rows)
        self.executed = []          # [(sql, params)] · 供断言 SQL 文本
        self._result = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        flat = " ".join(sql.split())
        if "pg_attribute" in flat:
            self._result = [{"column_name": c} for c in self.schema_columns]
        elif flat.upper().startswith("SELECT COUNT("):
            self._result = [{"total": len(self.rows)}]
        else:
            cols = parse_select_columns(flat)
            if cols == ["*"]:
                # 退回 SELECT * = 把库里所有列原样吐出去(本次要修掉的行为)
                self._result = [dict(r) for r in self.rows]
            else:
                self._result = [{c: r.get(c) for c in cols} for r in self.rows]

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    @property
    def main_query(self):
        """最后一条非 pg_attribute(取列)、非 COUNT 的查询。"""
        for sql, _ in reversed(self.executed):
            flat = " ".join(sql.split())
            if "pg_attribute" in flat:
                continue
            if flat.upper().startswith("SELECT COUNT("):
                continue
            return flat
        raise AssertionError("没有主查询被执行")


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


@pytest.fixture()
def mhz_db(monkeypatch):
    """导入被测模块并清掉进程级列缓存(否则用例之间会串)。"""
    import db.meijiehezi_db as mod
    mod._PUBLIC_COLUMN_CACHE.clear()
    yield mod
    mod._PUBLIC_COLUMN_CACHE.clear()


@pytest.fixture()
def wire(monkeypatch, mhz_db):
    """wire(schema_columns, rows) → FakeCursor;之后调 list_* 即走替身。"""
    def _wire(schema_columns, rows):
        cur = FakeCursor(schema_columns, rows)
        monkeypatch.setattr(mhz_db, "_get_conn", lambda: FakeConn(cur))
        return cur
    return _wire
