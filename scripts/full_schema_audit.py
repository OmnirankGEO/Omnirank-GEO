"""
全面比对 SQLite 表结构 vs PostgreSQL 表结构
输出：1) 缺失的列 → ALTER TABLE ADD COLUMN SQL
      2) 每张表的行数对比
"""
import sqlite3
import os
import re

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'db', 'geo_diagnosis.db')

# SQLite → PostgreSQL 类型映射
TYPE_MAP = {
    'INTEGER': 'INTEGER',
    'TEXT': 'TEXT',
    'REAL': 'REAL',
    'BLOB': 'BYTEA',
    'BOOLEAN': 'SMALLINT',
    'DATETIME': 'TIMESTAMP',
    'JSON': 'TEXT',
    'FLOAT': 'REAL',
    'VARCHAR': 'TEXT',
    'NUMERIC': 'NUMERIC',
}

def map_type(sqlite_type: str) -> str:
    """将 SQLite 类型转为 PostgreSQL 类型"""
    if not sqlite_type:
        return 'TEXT'
    upper = sqlite_type.upper().strip()
    # 处理 VARCHAR(n) 等带参数的类型
    base = re.sub(r'\(.*\)', '', upper).strip()
    return TYPE_MAP.get(base, 'TEXT')

def get_default_clause(default_value) -> str:
    """生成 DEFAULT 子句"""
    if default_value is None:
        return ''
    val = str(default_value).strip()
    # 处理常见默认值
    if val.upper() in ('CURRENT_TIMESTAMP',):
        return f' DEFAULT CURRENT_TIMESTAMP'
    if val.upper() in ('NULL',):
        return ''
    # 数字
    if re.match(r'^-?\d+\.?\d*$', val):
        return f' DEFAULT {val}'
    # 已有引号的字符串
    if val.startswith("'") and val.endswith("'"):
        return f' DEFAULT {val}'
    # datetime(...) → CURRENT_TIMESTAMP
    if 'datetime' in val.lower():
        return ' DEFAULT CURRENT_TIMESTAMP'
    # 裸字符串
    return f" DEFAULT '{val}'"

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 获取所有表
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' ORDER BY name")
    tables = [row[0] for row in c.fetchall()]
    
    print("=" * 70)
    print("SQLite → PostgreSQL 全面结构审计")
    print("=" * 70)
    
    all_alter_sqls = []
    table_row_counts = {}
    
    for table in tables:
        # 获取列信息
        c.execute(f"PRAGMA table_info([{table}])")
        columns = c.fetchall()
        
        # 获取行数
        c.execute(f"SELECT COUNT(*) FROM [{table}]")
        count = c.fetchone()[0]
        table_row_counts[table] = count
        
        col_info = []
        for col in columns:
            col_name = col['name']
            col_type = col['type'] or 'TEXT'
            notnull = col['notnull']
            default = col['dflt_value']
            pk = col['pk']
            
            pg_type = map_type(col_type)
            default_clause = get_default_clause(default)
            
            col_info.append({
                'name': col_name,
                'pg_type': pg_type,
                'default': default_clause,
                'pk': pk,
            })
        
        # 生成 ALTER TABLE SQL（供手动检查缺失列用）
        for ci in col_info:
            if ci['pk']:
                continue  # 跳过主键（肯定已存在）
            sql = f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{ci["name"]}" {ci["pg_type"]}{ci["default"]};'
            all_alter_sqls.append(sql)
    
    # 输出行数统计
    print(f"\n{'表名':<45} {'SQLite行数':>10}")
    print("-" * 60)
    for table, count in sorted(table_row_counts.items()):
        marker = " ★" if count > 0 else ""
        print(f"  {table:<43} {count:>8}{marker}")
    
    total = sum(table_row_counts.values())
    print(f"\n  总计: {len(tables)} 张表, {total:,} 行")
    
    # 输出 ALTER TABLE SQL
    print(f"\n{'=' * 70}")
    print(f"以下 ALTER TABLE 语句可安全执行（IF NOT EXISTS 不会重复添加）")
    print(f"共 {len(all_alter_sqls)} 条")
    print(f"{'=' * 70}\n")
    
    for sql in all_alter_sqls:
        print(sql)
    
    # 写入文件方便复制
    output_path = os.path.join(os.path.dirname(__file__), 'alter_columns.sql')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("-- 自动生成：补全 PostgreSQL 中可能缺失的列\n")
        f.write("-- 使用 IF NOT EXISTS 安全执行，不会影响已有列\n\n")
        for sql in all_alter_sqls:
            f.write(sql + '\n')
    
    print(f"\n✅ SQL 已保存到: {output_path}")
    print(f"   复制到服务器执行: docker compose exec db psql -U geo_admin -d geo_agentscope -f /path/to/alter_columns.sql")
    
    conn.close()

if __name__ == '__main__':
    main()
