"""[ADVISOR-OWNERSHIP] 测试辅助 —— 模块名唯一。

不放 conftest:多个测试包的 conftest 同名,各自把自己目录塞进 sys.path 后
`from conftest import x` 会解析到别人那份,几个套件合起来跑就 ImportError
(Review 正是合起来跑的)。[D0-b 2026-08-05 拆出]
"""
import types


class FakeCursor:
    """记录所有执行过的 (sql, params),按队列吐 row。"""

    def __init__(self, rows_queue=None):
        self.executed = []          # [(sql, params)]
        self._rows = list(rows_queue or [])

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def _next(self):
        return self._rows.pop(0) if self._rows else None

    def fetchone(self):
        return self._next()

    def fetchall(self):
        nxt = self._next()
        return nxt if isinstance(nxt, list) else ([] if nxt is None else [nxt])

    @property
    def rowcount(self):
        return 1

    # ---- 判据辅助 ----
    def sql_texts(self):
        return [s for (s, _) in self.executed]

    def ran(self, needle):
        """执行过的 SQL 里有没有包含 needle 的(大小写不敏感)。"""
        return any(needle.lower() in s.lower() for s in self.sql_texts())


class FakeConn:
    def __init__(self, rows_queue=None):
        self.cursor_obj = FakeCursor(rows_queue)
        self.committed = 0
        self.closed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed += 1


def make_request(user):
    """造一个只有 .state.user 的 stub request。user=None 表示未登录。"""
    st = types.SimpleNamespace(user=user)
    return types.SimpleNamespace(state=st)


ADMIN = {"user_id": 1, "is_admin": True}
ALICE = {"user_id": 42, "is_admin": False}
BOB = {"user_id": 99, "is_admin": False}


