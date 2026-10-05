"""
Phase 2: SQLite → PostgreSQL 数据迁移
读取 SQLite 全部表数据，批量插入 PostgreSQL

策略：
- 临时禁用外键检查（session_replication_role = replica）
- 批量 INSERT（每 500 行一批）
- 自动重置 SERIAL 序列到正确值
- 跳过 sqlite_sequence 内部表
"""

import sqlite3
import psycopg2
import psycopg2.extras
import os
import sys
import time
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

BATCH_SIZE = 500
SKIP_TABLES = {'sqlite_sequence'}


def get_pg_connection():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in .env")
    return psycopg2.connect(url)


def get_sqlite_connection():
    db_path = os.path.join(os.path.dirname(__file__), "geo_diagnosis.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_table_columns(pg_cursor, table_name):
    """获取 PostgreSQL 表的列名列表"""
    pg_cursor.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    return [row[0] for row in pg_cursor.fetchall()]


def get_serial_columns(pg_cursor, table_name):
    """获取使用 SERIAL 的列（需要重置序列）"""
    pg_cursor.execute("""
        SELECT column_name, pg_get_serial_sequence(%s, column_name) as seq_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_default LIKE 'nextval%%'
    """, (table_name, table_name))
    return [(row[0], row[1]) for row in pg_cursor.fetchall()]


def migrate_table(sqlite_conn, pg_conn, pg_cursor, table_name):
    """迁移单张表的数据"""
    # 1. 获取 PG 列名
    pg_columns = get_table_columns(pg_cursor, table_name)
    if not pg_columns:
        return 0, "表不存在于 PostgreSQL"

    # 2. 读取 SQLite 数据
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute(f'SELECT * FROM [{table_name}]')
    sqlite_columns = [desc[0] for desc in sqlite_cursor.description]

    # 3. 找到两边都有的列（交集，保持 PG 列顺序）
    common_columns = [c for c in pg_columns if c in sqlite_columns]
    if not common_columns:
        return 0, "无公共列"

    # 4. 清空 PG 表（幂等：支持重复执行）
    pg_cursor.execute(f'DELETE FROM "{table_name}"')

    # 5. 构造 INSERT 语句
    col_list = ', '.join(f'"{c}"' for c in common_columns)
    placeholders = ', '.join(['%s'] * len(common_columns))
    insert_sql = f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})'

    # 6. 批量读取并插入
    total = 0
    batch = []

    # 重新查询，只取公共列
    col_select = ', '.join(f'[{c}]' for c in common_columns)
    sqlite_cursor.execute(f'SELECT {col_select} FROM [{table_name}]')

    for row in sqlite_cursor:
        # 将 sqlite3.Row 转为 tuple
        values = tuple(row[c] for c in common_columns)
        batch.append(values)

        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_batch(pg_cursor, insert_sql, batch)
            total += len(batch)
            batch = []

    if batch:
        psycopg2.extras.execute_batch(pg_cursor, insert_sql, batch)
        total += len(batch)

    pg_conn.commit()
    return total, None


def reset_sequences(pg_conn, pg_cursor, table_name):
    """重置 SERIAL 序列到当前最大值 + 1"""
    serial_cols = get_serial_columns(pg_cursor, table_name)
    for col_name, seq_name in serial_cols:
        if seq_name:
            pg_cursor.execute(
                f'SELECT COALESCE(MAX("{col_name}"), 0) FROM "{table_name}"'
            )
            max_val = pg_cursor.fetchone()[0]
            if max_val > 0:
                pg_cursor.execute(
                    f"SELECT setval('{seq_name}', {max_val})"
                )
    pg_conn.commit()


def main():
    print("=" * 60)
    print("Phase 2: SQLite → PostgreSQL 数据迁移")
    print("=" * 60)

    sqlite_conn = get_sqlite_connection()
    pg_conn = get_pg_connection()
    pg_cursor = pg_conn.cursor()

    # 临时禁用外键检查（加速导入，避免顺序问题）
    pg_cursor.execute("SET session_replication_role = replica")
    pg_conn.commit()

    # 获取所有 SQLite 表
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in sqlite_cursor.fetchall() if row[0] not in SKIP_TABLES]

    print(f"\n共 {len(tables)} 张表待迁移\n")

    start_time = time.time()
    total_rows = 0
    results = []

    for table_name in tables:
        t0 = time.time()
        count, error = migrate_table(sqlite_conn, pg_conn, pg_cursor, table_name)
        elapsed = time.time() - t0

        if error:
            print(f"  ✗ {table_name}: {error}")
            results.append((table_name, 0, error))
        else:
            total_rows += count
            # 重置序列
            reset_sequences(pg_conn, pg_cursor, table_name)
            status = f"{count:>6} 行" if count > 0 else "  空表"
            print(f"  ✓ {table_name:<40} {status}  ({elapsed:.1f}s)")
            results.append((table_name, count, None))

    elapsed_total = time.time() - start_time

    # 恢复外键检查
    pg_cursor.execute("SET session_replication_role = DEFAULT")
    pg_conn.commit()

    # ========== 验证 ==========
    print(f"\n{'='*60}")
    print(f"数据验证：逐表比对行数")
    print(f"{'='*60}")

    mismatch = 0
    for table_name, expected, error in results:
        if error:
            continue
        pg_cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        actual = pg_cursor.fetchone()[0]
        if actual != expected:
            print(f"  ⚠ {table_name}: 期望 {expected}, 实际 {actual}")
            mismatch += 1

    if mismatch == 0:
        print(f"  全部 {len(tables)} 张表行数一致 ✓")

    print(f"\n{'='*60}")
    print(f"迁移完成!")
    print(f"  总行数: {total_rows:,}")
    print(f"  耗时: {elapsed_total:.1f}s")
    print(f"  不匹配: {mismatch}")
    print(f"{'='*60}")

    pg_cursor.close()
    pg_conn.close()
    sqlite_conn.close()

    return mismatch == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
