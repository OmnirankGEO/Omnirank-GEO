#!/usr/bin/env python3
"""[补充工单 2026-08-06 §5「性能反向对照」] 数一页列表到底发几次组织查询。

工单原话:「同一页在改动前后各跑一次,统计组织相关 SQL 次数;改动后必须是常数级。
**只报『感觉不慢』不算。**」

做法:拦 psycopg2 cursor 的 execute,按 SQL 文本分类计数,真跑 `list_admin_users()`。
不改任何业务代码,只在探针里包一层。

用法(需要能连库):
    DATABASE_URL=... python scripts/probe_account_origin_query_count_2026_08_06.py [page_size]

输出三个数:本页行数 N · 组织相关查询次数 · 该次数是否随 N 增长(跑两个 page_size 对比)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.admin_user_governance as gov  # noqa: E402

_ORG_RE = re.compile(r"FROM\s+organization_memberships", re.I)


def _run(page_size: int):
    counts = {"org": 0, "total": 0}
    real_connect = gov.get_conn if hasattr(gov, "get_conn") else None

    import db.connection as dbconn

    orig_get = dbconn.get_conn

    class CountingCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            counts["total"] += 1
            if _ORG_RE.search(" ".join(str(sql).split())):
                counts["org"] += 1
            return self._inner.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

    class CountingConn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self, *a, **kw):
            return CountingCursor(self._inner.cursor(*a, **kw))

        def __getattr__(self, item):
            return getattr(self._inner, item)

    def patched(*a, **kw):
        return CountingConn(orig_get(*a, **kw))

    dbconn.get_conn = patched
    try:
        result = gov.list_admin_users(page=1, page_size=page_size)
    finally:
        dbconn.get_conn = orig_get
    return len(result["users"]), counts["org"], counts["total"]


def main() -> int:
    small_n, small_org, small_total = _run(10)
    big_n, big_org, big_total = _run(50)
    print(f"page_size=10 → 行数 {small_n} · 组织查询 {small_org} 次 · 总查询 {small_total} 次")
    print(f"page_size=50 → 行数 {big_n} · 组织查询 {big_org} 次 · 总查询 {big_total} 次")
    # 🔴 判据:组织查询次数不随行数增长。反向对照就是这两行本身 ——
    #    若是 N+1,50 行那次必然远多于 10 行那次。
    ok = small_org <= 1 and big_org <= 1
    print()
    print("✅ 常数级(O(1))" if ok else f"🔴 疑似 N+1:{small_n} 行 {small_org} 次 / {big_n} 行 {big_org} 次")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
