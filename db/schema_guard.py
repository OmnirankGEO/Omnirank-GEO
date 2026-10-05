"""请求路径上的「自愈 DDL」只在对象真缺失时才执行(WO_285b)。

为什么要有这个模块:
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` 在 PostgreSQL 里是**先拿 ACCESS EXCLUSIVE 锁、再判断 IF NOT EXISTS**。
  列早就存在也照样要锁。发车备份的 pg_dump 对每张表持 ACCESS SHARE ⇒ 这类语句排在 dump 后面;
  lock_timeout=0 ⇒ 一直等;同步 psycopg2 卡住事件循环;WORKERS=1 ⇒ 整个应用停(0913AA 上线 8 条 504,WO_284/285)。
  本机实测复现:会话持 ACCESS SHARE 时,`ADD COLUMN IF NOT EXISTS`(列已存在)被挡。

做法:先读系统目录(普通 SELECT,不与 dump 冲突),对象真缺失才执行 DDL。
  生产上列早已存在 ⇒ 每个请求 0 条 DDL;干净库 / 测试库仍能自愈。
  目录查询走 `to_regclass` + `pg_attribute`,跟随当前 search_path(与 db/brands_schema._column_exists 同一写法)。

🔴 请求路径可达的函数里,不许再写无条件的 `ALTER TABLE …`:
   要么经本模块的函数,要么放在进程级 once-guard 函数里,要么走迁移。
   判据:tests/request_path_ddl_2026_09_24。
"""
from __future__ import annotations

import logging

__all__ = ["column_exists", "add_column_if_missing", "constraint_def", "replace_in_list_check_if_changed",
           "column_type", "alter_column_type_if_changed", "run_ddl_with_lock_timeout"]

logger = logging.getLogger("GEO-SchemaGuard")

#: 真要跑 DDL 时最多等多久拿锁。对象缺失本就罕见,但赶上备份窗口时宁可这次不补、下次再补,
#: 也不能排在 pg_dump 后面把整个应用卡住(与 api/advisor_api.py 的 WO_285 执行器同一取值)。
DDL_LOCK_TIMEOUT = "2s"
_SAVEPOINT = "schema_guard_ddl"


def _is_lock_timeout(exc: Exception) -> bool:
    return getattr(exc, "pgcode", None) == "55P03"  # lock_not_available


def run_ddl_with_lock_timeout(cursor, statements, label: str) -> bool:
    """在调用方的连接上跑 DDL,拿锁最多等 DDL_LOCK_TIMEOUT;拿不到锁 ⇒ 撤销这几条、记 warning、返回 False,不抛。

    🔴 这里跑在**调用方的**事务里,不能像 advisor 执行器那样整笔回滚(会把调用方前面的写一起丢掉):
       - 调用方在事务里 ⇒ 用 SAVEPOINT 包住「SET LOCAL lock_timeout + DDL」,失败只退到保存点;
         成功后把 lock_timeout 恢复成调用前的值,不改变调用方后续语句的等锁行为。
       - 调用方是 autocommit 连接 ⇒ 自己 BEGIN … COMMIT,SET LOCAL 随事务结束失效。
       同一组语句要么全生效、要么全不生效(DROP + ADD CHECK 不会只剩一半)。
       拿锁以外的错误照旧抛出(先退回保存点,调用方事务仍可用)。
    """
    conn = getattr(cursor, "connection", None)
    autocommit = bool(getattr(conn, "autocommit", False))
    if autocommit:
        cursor.execute("BEGIN")
    else:
        cursor.execute("SELECT current_setting('lock_timeout')")
        previous = _one(cursor)
        cursor.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        cursor.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
        for sql in statements:
            cursor.execute(sql)
    except Exception as exc:
        if autocommit:
            cursor.execute("ROLLBACK")
        else:
            cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        if _is_lock_timeout(exc):
            logger.warning("[schema_guard] %s:%s 内拿不到锁,本次不补,下次再试", label, DDL_LOCK_TIMEOUT)
            return False
        raise
    if autocommit:
        cursor.execute("COMMIT")
    else:
        cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        if previous is not None:
            cursor.execute("SELECT set_config('lock_timeout', %s, true)", (str(previous),))
    return True


def _one(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def column_exists(cursor, table: str, column: str) -> bool:
    """当前 search_path 实际解析到的表上是否有这一列(表不存在 ⇒ False)。"""
    cursor.execute(
        """
        SELECT 1
          FROM pg_attribute
         WHERE attrelid = to_regclass(%s)
           AND attname = %s
           AND attnum > 0
           AND NOT attisdropped
        """,
        (table, column),
    )
    return cursor.fetchone() is not None


def add_column_if_missing(cursor, table: str, column: str, ddl_type: str) -> bool:
    """列缺失才 `ALTER TABLE … ADD COLUMN`;返回是否真的执行了 DDL。

    table / column / ddl_type 都来自调用方写死的字面量(不是用户输入),拼进 DDL 与原写法一致。
    仍写 IF NOT EXISTS:两个进程同时自愈时,后到的那个不报 duplicate_column。
    """
    if column_exists(cursor, table, column):
        return False
    return run_ddl_with_lock_timeout(
        cursor, [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"], f"{table}.{column} 加列")


def constraint_def(cursor, table: str, name: str):
    """约束的规范定义文本(pg_get_constraintdef);不存在 ⇒ None。"""
    cursor.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
          FROM pg_constraint c
         WHERE c.conrelid = to_regclass(%s)
           AND c.conname = %s
        """,
        (table, name),
    )
    return _one(cursor)


def replace_in_list_check_if_changed(cursor, table: str, name: str, column: str, values) -> bool:
    """`CHECK (column IN (...))` 这一类约束:允许值集合与期望不同(或约束不存在)才 DROP + ADD;返回是否执行了 DDL。

    🔴 不能拿规范化文本逐字比:PostgreSQL 把 `status IN ('a','b')` 存成
       `CHECK ((status = ANY (ARRAY['a'::text, 'b'::text])))`,逐字比永远「不同」,
       那就又退回每个请求都 DROP + ADD。这里比的是**定义里出现的引号字面量集合**。
    """
    import re

    current = constraint_def(cursor, table, name)
    want = {str(v) for v in values}
    if current is not None and set(re.findall(r"'([^']*)'", current)) == want:
        return False
    in_list = ",".join("'" + v.replace("'", "''") + "'" for v in values)
    return run_ddl_with_lock_timeout(cursor, [
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}",
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({in_list}))",
    ], f"{table}.{name} 约束")


def column_type(cursor, table: str, column: str):
    """列的类型文本(format_type,如 `character varying(240)`);列不存在 ⇒ None。"""
    cursor.execute(
        """
        SELECT format_type(atttypid, atttypmod)
          FROM pg_attribute
         WHERE attrelid = to_regclass(%s)
           AND attname = %s
           AND attnum > 0
           AND NOT attisdropped
        """,
        (table, column),
    )
    return _one(cursor)


def alter_column_type_if_changed(cursor, table: str, column: str, ddl_type: str, expected_format: str) -> bool:
    """列类型不是 expected_format(format_type 的写法)才 ALTER COLUMN TYPE;返回是否真的执行了 DDL。"""
    current = column_type(cursor, table, column)
    if current is None or current == expected_format:
        return False
    return run_ddl_with_lock_timeout(
        cursor, [f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {ddl_type}"], f"{table}.{column} 改类型")

