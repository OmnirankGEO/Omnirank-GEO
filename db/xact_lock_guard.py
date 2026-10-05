"""事务级 advisory lock 的显式硬闸(WO-LATENT-TRAPS §1 · 2026-08-17)

## 它挡的是什么

`pg_advisory_xact_lock` 是**事务级**锁:锁在**事务结束**时释放。
如果拿锁用的连接是 `autocommit=True`,那条 `SELECT pg_advisory_xact_lock(...)`
**自成一个事务** —— 语句一返回,事务就结束,锁**当场释放**。
后面所有"我已持锁,可以安全读改写"的代码全部裸奔:

    conn.autocommit = True
    cur.execute("SELECT pg_advisory_xact_lock(920512, %s)", (uid,))   # 锁 → 立刻放
    cur.execute("SELECT balance FROM ... WHERE user_id=%s", (uid,))   # 已无保护
    cur.execute("UPDATE ... SET balance=%s", (new,))                  # 并发超发

**症状是"锁了等于没锁",不报错、不留痕**,只在并发下变成超发 / 双花 / 孤儿写入。
图文包一周内三次撞上同一个坑;2026-08-17 全仓普查发现既有代码 87 处在用该锁,
其中 28 处的连接来源**静态追不出来**(cursor 由调用方传入、调用方在仓外或跨多层)。
追不出来 ≠ 安全 —— 所以那 28 处改由本模块在**运行时**判,判不过就**响亮失败**。

## 为什么是"响亮失败"而不是"帮你开事务"

静默把 autocommit 关掉会让调用方以为自己在 autocommit 语义下工作
(每条语句独立提交),而实际所有写入压在一个没人 commit 的事务里 —— 换一种更难查的错。
锁失效是**调用方的架构错误**,必须在调用方那里改,所以这里只诊断、只抛,不代偿。

## 用法

    from db.xact_lock_guard import require_xact_scope

    def _do_something(cur, user_id):
        require_xact_scope(cur, where="agent_pricing.apply_markup_for_agent")
        cur.execute("SELECT pg_advisory_xact_lock(920512, %s)", (user_id,))
        ...

反向对照(证明本闸不是恒红):事务连接上调用 `require_xact_scope` 必须**静默通过**。
配套判据 `tests/test_advisory_xact_lock_gate_2026_08_17.py`。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-XactLockGuard")

__all__ = [
    "XactLockScopeError",
    "require_xact_scope",
    "assert_lock_scope_effective",
    "connection_of",
]


class XactLockScopeError(RuntimeError):
    """在 autocommit(或已损坏)连接上试图取事务级 advisory lock。

    继承 RuntimeError 而不是自定义基类:调用链上游普遍 `except Exception` 落日志后
    继续走,继承 RuntimeError 让它在那些地方仍旧是"未预期异常"而非被当成业务错误吞掉。
    """


def connection_of(cur: Any) -> Any:
    """从 cursor 拿到底层 psycopg2 连接。

    注意 `db/connection.py::_PooledConnection.cursor()` 直接把 **raw** psycopg2 cursor
    透传出来,所以 `cur.connection` 拿到的是 raw connection(不是池包装),
    `autocommit` / `get_transaction_status()` 都可直接用。
    传进来的若本身就是连接(少数调用点),原样返回。
    """
    conn = getattr(cur, "connection", None)
    if conn is not None:
        return conn
    if hasattr(cur, "autocommit"):
        return cur
    return None


def require_xact_scope(cur: Any, where: str = "") -> None:
    """取事务级 advisory lock **之前**调用:确认这把锁能活过本语句。

    抛 `XactLockScopeError` 的唯一条件 = 连接处于 autocommit —— 那正是锁当场失效的形态。
    拿不到连接对象时**放行**(判据不可用不等于判据不通过,但也不许在这里假装能判):
    这种情况只出现在测试替身 / mock cursor 上,真 psycopg2 cursor 一定有 `.connection`。
    """
    conn = connection_of(cur)
    if conn is None:
        return

    autocommit = getattr(conn, "autocommit", None)
    if autocommit is True:
        raise XactLockScopeError(
            "事务级 advisory lock 取在 autocommit 连接上 —— 锁会在本语句结束即释放,"
            "等于没锁(并发下超发/双花/孤儿写入)。"
            "修法是在调用方开显式事务(get_db() / autocommit=False)后再进来,"
            "不是在这里把 autocommit 关掉。"
            + (f" 位置: {where}" if where else "")
        )


def assert_lock_scope_effective(cur: Any, where: str = "") -> None:
    """取锁**之后**调用:向 PostgreSQL 求证"我现在真的在一个事务块里"。

    比 `require_xact_scope` 的标志位检查更硬 —— 它打的是**行为**不是**属性**:
      autocommit=False + 已执行过语句 → TRANSACTION_STATUS_INTRANS
      autocommit=True                 → TRANSACTION_STATUS_IDLE(语句已自成事务并结束)
    因此只能在取锁之后打(取锁之前 psycopg2 尚未发 BEGIN,两种情形都是 IDLE,零判别力)。
    """
    conn = connection_of(cur)
    if conn is None:
        return
    try:
        from psycopg2 import extensions as _ext
    except Exception:  # psycopg2 不在(纯单测环境)→ 判据不可用,放行
        return
    getter = getattr(conn, "get_transaction_status", None)
    if getter is None:
        return
    status = getter()
    if status in (_ext.TRANSACTION_STATUS_INTRANS, _ext.TRANSACTION_STATUS_INERROR):
        return
    raise XactLockScopeError(
        "取完事务级 advisory lock 后连接不在事务块内"
        f"(get_transaction_status()={status})—— 锁已随语句结束释放,等于没锁。"
        + (f" 位置: {where}" if where else "")
    )


def xact_lock(cur: Any, sql: str, params: Optional[Any] = None, where: str = "") -> None:
    """取锁 + 前后双向自证 的一体化写法(新代码首选)。

    既有 28 处走的是 `require_xact_scope` 单行插入(改动面最小、diff 可逐行读);
    新写的锁建议直接用这个,前后两道判据都拿到。
    """
    require_xact_scope(cur, where=where)
    cur.execute(sql, params)
    assert_lock_scope_effective(cur, where=where)
