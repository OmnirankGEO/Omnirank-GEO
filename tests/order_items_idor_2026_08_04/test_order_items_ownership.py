"""[FIND-MHZ-ORDERITEMS-2026-08-04 · S1] `/orders/{order_id}/items` 归属守卫行为锁。

已实证的洞:QA 号 113 带 token 调 admin 的 order 441 → HTTP 200 · 40 个字段,
含 `cost_yuan`(我方付上游的钱)与 `submitted_content_snapshot`(别人的稿件正文)。

锁的形状按"每条必须命中配一条必须不命中"写,并且额外锁两件容易在重构中丢的性质:
  ① **防枚举** —— "不存在"与"不是你的"必须返回**完全相同**的响应
  ② **先判后取** —— 拒绝时绝不许已经把别人的数据从库里取出来
"""
import asyncio

import pytest
from fastapi import HTTPException

OWNER = 113
OTHER = 999
ITEMS = [{"id": 1, "order_id": 441, "cost_yuan": 4.0, "submitted_content_snapshot": "别人的正文"}]


def _call(mod, request):
    return asyncio.run(mod.api_order_items(441, request))


# ============================================================================
# [WO-KYB D0-b 2026-08-05] 端点级用例整体移除 —— 因为端点本身被删了。
#
# `GET /api/meijiehezi/orders/{order_id}/items` 前端零调用(Review 复审亲核过),
# Owner 2026-08-05 拍板"死端点删"。端点没了,守卫失去被测对象,
# 原 6 个 def / 7 个用例(owner 放行 / admin 放行 / 别人 404 / 不存在 404 /
# 拒绝不碰数据 / 两种拒绝不可区分 / 匿名 401)一并移除。
#
# 🔴 不是静默删:换成下面这条"必须保持已删除"的锁,把删除本身钉住 ——
#    否则将来谁把端点加回来,就又是一个无守卫的 IDOR。
# 🔴 保留下方 db 层 get_publish_order_owner 的用例:那是可复用的**归属原语**,
#    S4 通用资源级归属中间件正要用它,不是冗余。
# ============================================================================

def test_dead_endpoint_stays_deleted():
    import io as _io
    import os as _os
    p = _os.path.join(_os.path.dirname(__file__), "..", "..", "api", "meijiehezi_api.py")
    src = _io.open(_os.path.abspath(p), encoding="utf-8", newline="").read()
    assert '@router.get("/orders/{order_id}/items")' not in src,         "死端点被加回来了 —— 加回来必须同时带归属守卫,并恢复原来那 7 条锁"
    # 反向对照:同文件别的路由必须仍在,证明不是整份文件读空了
    assert '@router.get("/mhz-orders")' in src


def test_ownership_read_from_orders_table(monkeypatch):
    """`mhz_publish_order_items` 自己也有 user_id 列(冗余副本)。

    权威源必须是 `mhz_publish_orders.user_id` —— 否则一旦冗余列写歪,
    归属判定跟着歪,而且歪的方向是"放行"。
    """
    import ast
    import inspect

    import db.meijiehezi_db as dbmod

    src = inspect.getsource(dbmod.get_publish_order_owner)
    fn = ast.parse(src).body[0]

    # 🔴 必须先把 docstring 摘掉再断言:这个函数的 docstring 里逐字写着
    # "mhz_publish_order_items 自己也有 user_id 列",不摘掉的话
    # 「SQL 里不许出现 items 表」这条断言会被自己的说明文字打成恒假。
    doc = ast.get_docstring(fn, clean=False)
    literals = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc
    ]
    sql = " ".join(literals)

    assert "mhz_publish_orders" in sql
    assert "mhz_publish_order_items" not in sql
    # 反向对照两条:①确实取到了 SQL 而不是空串 ②docstring 真的被摘掉了
    assert "user_id" in sql and "WHERE" in sql.upper()
    assert doc and "mhz_publish_order_items" in doc, (
        "docstring 里本该有那个词;它没有的话,上面那条否定断言就成了恒真"
    )


def test_owner_helper_returns_none_for_missing(monkeypatch):
    """订单不存在 / user_id 为空 → None(端点据此 404)。非法入参也不许抛。"""
    import db.meijiehezi_db as dbmod

    class _C:
        def __init__(self, row):
            self._row = row

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self, row):
            self._row = row

        def cursor(self):
            return _C(self._row)

        def close(self):
            pass

    monkeypatch.setattr(dbmod, "_get_conn", lambda: _Conn(None))
    assert dbmod.get_publish_order_owner(441) is None

    monkeypatch.setattr(dbmod, "_get_conn", lambda: _Conn({"user_id": None}))
    assert dbmod.get_publish_order_owner(441) is None

    monkeypatch.setattr(dbmod, "_get_conn", lambda: _Conn({"user_id": 113}))
    assert dbmod.get_publish_order_owner(441) == 113
    # 非法 order_id 不许把异常抛到端点(端点会变 500 而不是 404)
    assert dbmod.get_publish_order_owner("不是数字") is None
