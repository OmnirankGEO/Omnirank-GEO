"""
A3: 创建 brand_aliases 表 + 初始化已知品牌别名数据

数据来源：
- extractor.py 的 _BRAND_ALIAS_MAP（10 个品牌 + 别名）
- 常见企业后缀（用于自动生成变体）
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "db" / "geo_diagnosis.db"

CREATE_BRAND_ALIASES = """
CREATE TABLE IF NOT EXISTS brand_aliases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL,              -- 标准名称（如 "全域上榜"）
    alias           TEXT NOT NULL,              -- 变体名称（如 "全域上榜科技有限公司"）
    brand_id        INTEGER,                    -- 关联品牌 ID（可空）
    source          TEXT DEFAULT 'manual'       -- manual / auto_discovered / llm_suggested
                    CHECK(source IN ('manual', 'auto_discovered', 'llm_suggested')),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE(canonical_name, alias)               -- 同一品牌不重复记录同一别名
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ba_canonical ON brand_aliases(canonical_name);",
    "CREATE INDEX IF NOT EXISTS idx_ba_alias ON brand_aliases(alias);",
]

# 从 extractor.py 迁移的已知品牌别名
# 这些是通用品牌，后续实际客户品牌在使用时动态添加
INITIAL_ALIASES = [
    # GEO 行业相关品牌（客户和竞品）
    ("全域上榜", "全域上榜科技", "manual"),
    ("全域上榜", "全域上榜科技有限公司", "manual"),
    ("全域上榜", "Quanyu Shangbang", "manual"),
    ("全域上榜", "quanyushangbang", "manual"),
    
    # 通用互联网品牌别名（用于准确提取）
    ("小米", "Xiaomi", "manual"),
    ("小米", "MI", "manual"),
    ("小米", "Redmi", "manual"),
    ("小米", "红米", "manual"),
    ("华为", "Huawei", "manual"),
    ("华为", "HUAWEI", "manual"),
    ("华为", "荣耀", "manual"),
    ("华为", "Honor", "manual"),
    ("苹果", "Apple", "manual"),
    ("苹果", "iPhone", "manual"),
    ("苹果", "iPad", "manual"),
    ("腾讯", "Tencent", "manual"),
    ("腾讯", "微信", "manual"),
    ("腾讯", "WeChat", "manual"),
    ("腾讯", "QQ", "manual"),
    ("阿里", "Alibaba", "manual"),
    ("阿里", "阿里巴巴", "manual"),
    ("阿里", "淘宝", "manual"),
    ("阿里", "天猫", "manual"),
    ("字节", "ByteDance", "manual"),
    ("字节", "抖音", "manual"),
    ("字节", "TikTok", "manual"),
    ("字节", "字节跳动", "manual"),
    ("百度", "Baidu", "manual"),
    ("百度", "BAIDU", "manual"),
    ("京东", "JD", "manual"),
    ("京东", "JD.com", "manual"),
    ("美团", "Meituan", "manual"),
    ("拼多多", "Pinduoduo", "manual"),
    ("拼多多", "PDD", "manual"),
]


def main():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    print("=" * 50)
    print("A3: 创建 brand_aliases 表")
    print("=" * 50)
    
    # 检查是否已存在
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brand_aliases'")
    if c.fetchone():
        print("⚠️ brand_aliases 表已存在，跳过创建")
        c.execute("SELECT COUNT(*) FROM brand_aliases")
        print(f"   现有 {c.fetchone()[0]} 条别名数据")
        conn.close()
        return
    
    # 建表
    c.execute(CREATE_BRAND_ALIASES)
    print("✅ 已创建 brand_aliases 表")
    
    # 建索引
    for idx_sql in CREATE_INDEXES:
        c.execute(idx_sql)
    print(f"✅ 已创建 {len(CREATE_INDEXES)} 个索引")
    
    # 初始化数据
    inserted = 0
    for canonical, alias, source in INITIAL_ALIASES:
        try:
            c.execute(
                "INSERT INTO brand_aliases (canonical_name, alias, source) VALUES (?, ?, ?)",
                (canonical, alias, source)
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # 跳过重复
    
    conn.commit()
    print(f"✅ 已初始化 {inserted} 条品牌别名")
    
    # 验证
    c.execute("SELECT canonical_name, COUNT(*) FROM brand_aliases GROUP BY canonical_name ORDER BY 2 DESC")
    groups = c.fetchall()
    print(f"\n--- 品牌别名统计 ---")
    for name, cnt in groups:
        print(f"  {name}: {cnt} 个别名")
    
    conn.close()
    print(f"\n✅ A3 完成")


if __name__ == "__main__":
    main()
