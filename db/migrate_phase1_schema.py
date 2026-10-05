"""
Phase 1: SQLite → PostgreSQL 表结构迁移
自动读取 SQLite 全部表结构，转写为 PostgreSQL DDL 并创建

转换规则：
- INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
- BOOLEAN → SMALLINT (代码兼容 =0 / =1 比较)
- datetime('now','localtime') → CURRENT_TIMESTAMP
- JSON → TEXT (保持现有 json.loads 代码兼容)
- DATETIME → TIMESTAMP
- 跳过 sqlite_sequence (PG 自动管理序列)
- 剥离 FOREIGN KEY 约束，全部表创建后再添加（解决依赖顺序问题）
- 修复未加引号的字符串默认值
"""

import sqlite3
import psycopg2
import re
import os
import sys
from dotenv import load_dotenv

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def get_pg_connection():
    """连接 PostgreSQL"""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in .env")
    return psycopg2.connect(url)


def get_sqlite_connection():
    """连接 SQLite"""
    db_path = os.path.join(os.path.dirname(__file__), "geo_diagnosis.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def strip_foreign_keys(sql: str) -> tuple:
    """
    从 CREATE TABLE 中剥离 FOREIGN KEY 约束，返回 (clean_sql, [fk_clauses])
    逐行处理，避免注释中的 ) 被误匹配
    """
    fk_pattern = re.compile(
        r'^\s*,?\s*FOREIGN\s+KEY\s*\(.+$',
        re.IGNORECASE
    )

    lines = sql.split('\n')
    clean_lines = []
    fk_lines = []

    for line in lines:
        if fk_pattern.match(line):
            fk_lines.append(line.strip().lstrip(',').strip())
        else:
            clean_lines.append(line)

    # 找到最后一个非空、非注释、非闭合括号的行，去掉尾部逗号
    for i in range(len(clean_lines) - 1, -1, -1):
        stripped = clean_lines[i].strip()
        if stripped and stripped != ')' and not stripped.startswith('--'):
            # 移除行尾逗号（可能在注释之前）
            clean_lines[i] = re.sub(r',(\s*--.*)?$', r'\1', clean_lines[i])
            break

    clean_sql = '\n'.join(clean_lines)
    return clean_sql, fk_lines


def fix_unquoted_defaults(sql: str) -> str:
    """修复 SQLite 中未加引号的字符串默认值，如 DEFAULT draft → DEFAULT 'draft'"""
    # 匹配 DEFAULT 后面跟着的裸字符串（不是数字、不是引号、不是关键字）
    keywords = {
        'CURRENT_TIMESTAMP', 'CURRENT_DATE', 'CURRENT_TIME',
        'NULL', 'TRUE', 'FALSE',
    }

    def fix_default(match):
        val = match.group(1)
        if val.upper() in keywords:
            return f"DEFAULT {val}"
        # 纯数字或负数
        if re.match(r'^-?\d+\.?\d*$', val):
            return f"DEFAULT {val}"
        # 函数调用 (如 CURRENT_TIMESTAMP)
        if '(' in val:
            return f"DEFAULT {val}"
        # 已有引号
        if val.startswith("'") or val.startswith('"'):
            return f"DEFAULT {val}"
        # 裸字符串 → 加引号
        return f"DEFAULT '{val}'"

    sql = re.sub(
        r"DEFAULT\s+([^\s,)]+)",
        lambda m: fix_default(m),
        sql,
        flags=re.IGNORECASE
    )
    return sql


def convert_ddl(table_name: str, sqlite_sql: str) -> tuple:
    """
    将 SQLite CREATE TABLE 转为 PostgreSQL DDL
    返回 (pg_ddl, fk_clauses)
    """
    sql = sqlite_sql

    # 1. INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
    sql = re.sub(
        r'(\w+)\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
        r'\1 SERIAL PRIMARY KEY',
        sql,
        flags=re.IGNORECASE
    )

    # 2. BOOLEAN → SMALLINT (保持 =0/=1 代码兼容)
    sql = re.sub(r'\bBOOLEAN\b', 'SMALLINT', sql, flags=re.IGNORECASE)

    # 3. datetime('now', 'localtime') 和 datetime('now') → CURRENT_TIMESTAMP
    sql = re.sub(
        r"DEFAULT\s*\(\s*datetime\s*\(\s*'now'\s*(?:,\s*'localtime'\s*)?\)\s*\)",
        "DEFAULT CURRENT_TIMESTAMP",
        sql,
        flags=re.IGNORECASE
    )

    # 4. JSON → TEXT
    sql = re.sub(r'\bJSON\b', 'TEXT', sql, flags=re.IGNORECASE)

    # 5. DATETIME → TIMESTAMP
    sql = re.sub(r'\bDATETIME\b', 'TIMESTAMP', sql, flags=re.IGNORECASE)

    # 6. 移除 SQLite 特有的 AUTOINCREMENT（兜底）
    sql = re.sub(r'\bAUTOINCREMENT\b', '', sql, flags=re.IGNORECASE)

    # 7. 修复未加引号的默认值
    sql = fix_unquoted_defaults(sql)

    # 8. 剥离 FOREIGN KEY 约束
    sql, fks = strip_foreign_keys(sql)

    # 9. 解析 FK 供后续 ALTER TABLE 使用
    fk_alters = []
    for fk_clause in fks:
        fk_clean = fk_clause.strip().lstrip(',').strip()
        fk_alters.append(f"ALTER TABLE {table_name} ADD {fk_clean}")

    return sql, fk_alters


def convert_index(sqlite_sql: str) -> str:
    """将 SQLite CREATE INDEX 转为 PostgreSQL"""
    sql = re.sub(r'\s+', ' ', sqlite_sql.strip())
    sql = sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
    sql = sql.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
    return sql


def main():
    print("=" * 60)
    print("Phase 1: SQLite → PostgreSQL 表结构迁移")
    print("=" * 60)

    # 连接数据库
    sqlite_conn = get_sqlite_connection()
    pg_conn = get_pg_connection()
    pg_cursor = pg_conn.cursor()

    # ========== 0. 清理已存在的表（全新创建）==========
    pg_cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
    """)
    existing = [row[0] for row in pg_cursor.fetchall()]
    if existing:
        print(f"\n清理 PostgreSQL 中已有的 {len(existing)} 张表...")
        # 用 CASCADE 删除所有表和依赖
        for t in existing:
            pg_cursor.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
        pg_conn.commit()
        print("  清理完成")

    # ========== 1. 提取 SQLite 表结构 ==========
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
    )
    tables = sqlite_cursor.fetchall()

    skip_tables = {'sqlite_sequence'}

    # ========== 2. 创建表（不含外键）==========
    print(f"\n阶段 A：创建表结构（{len(tables) - len(skip_tables)} 张，无外键）")
    created = 0
    skipped = 0
    errors = []
    all_fk_alters = []

    for name, sql in tables:
        if name in skip_tables:
            skipped += 1
            continue

        pg_ddl, fk_alters = convert_ddl(name, sql)
        all_fk_alters.extend(fk_alters)

        # 不需要 IF NOT EXISTS（已清理过）
        try:
            pg_cursor.execute(pg_ddl)
            pg_conn.commit()
            created += 1
            print(f"  ✓ {name}")
        except Exception as e:
            pg_conn.rollback()
            err_msg = str(e).split('\n')[0]
            errors.append((name, err_msg, pg_ddl))
            print(f"  ✗ {name}: {err_msg}")

    print(f"\n  结果: {created} 成功, {skipped} 跳过, {len(errors)} 失败")

    # ========== 3. 添加外键约束 ==========
    print(f"\n阶段 B：添加外键约束（{len(all_fk_alters)} 条）")
    fk_ok = 0
    fk_skip = []

    for alter_sql in all_fk_alters:
        try:
            pg_cursor.execute(alter_sql)
            pg_conn.commit()
            fk_ok += 1
        except Exception as e:
            pg_conn.rollback()
            err_msg = str(e).split('\n')[0]
            fk_skip.append((alter_sql, err_msg))

    print(f"  结果: {fk_ok} 成功, {len(fk_skip)} 跳过（引用不存在的表）")
    for sql, err in fk_skip:
        print(f"    跳过: {sql[:80]}... — {err}")

    # ========== 4. 创建索引 ==========
    sqlite_cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    )
    indexes = sqlite_cursor.fetchall()
    print(f"\n阶段 C：创建索引（{len(indexes)} 个）")

    idx_created = 0
    idx_errors = []

    for name, sql in indexes:
        pg_idx = convert_index(sql)
        try:
            pg_cursor.execute(pg_idx)
            pg_conn.commit()
            idx_created += 1
            print(f"  ✓ {name}")
        except Exception as e:
            pg_conn.rollback()
            err_msg = str(e).split('\n')[0]
            idx_errors.append((name, err_msg))
            print(f"  ✗ {name}: {err_msg}")

    print(f"\n  结果: {idx_created} 成功, {len(idx_errors)} 失败")

    # ========== 5. 验证 ==========
    pg_cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    pg_tables = [row[0] for row in pg_cursor.fetchall()]

    pg_cursor.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public' ORDER BY indexname
    """)
    pg_indexes = [row[0] for row in pg_cursor.fetchall()]

    print(f"\n{'='*60}")
    print(f"PostgreSQL 最终验证:")
    print(f"  表: {len(pg_tables)} 张")
    print(f"  索引: {len(pg_indexes)} 个 (含主键/唯一约束自动索引)")
    print(f"{'='*60}")

    if errors:
        print(f"\n⚠ 表创建失败详情:")
        for name, err, ddl in errors:
            print(f"\n  [{name}]")
            print(f"  错误: {err}")
            print(f"  DDL: {ddl[:200]}...")

    # 清理
    pg_cursor.close()
    pg_conn.close()
    sqlite_conn.close()

    return len(errors) == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
