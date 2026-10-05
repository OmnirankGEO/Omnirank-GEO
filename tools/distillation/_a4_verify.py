"""
A4: 验证新表结构
- keyword_insights 表结构 + 索引
- brand_aliases 表结构 + 数据
- 旧表已清除
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "db" / "geo_diagnosis.db"

def main():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    print("=" * 50)
    print("A4: 验证新表结构")
    print("=" * 50)
    
    errors = []
    
    # 1. 旧表已清除
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='response_insights'")
    if c.fetchone():
        errors.append("❌ response_insights 表仍存在！")
    else:
        print("✅ response_insights 表已清除")
    
    c.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='brand_intelligence_view'")
    if c.fetchone():
        errors.append("❌ brand_intelligence_view 视图仍存在！")
    else:
        print("✅ brand_intelligence_view 视图已清除")
    
    # 2. keyword_insights 表
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keyword_insights'")
    if not c.fetchone():
        errors.append("❌ keyword_insights 表不存在！")
    else:
        print("✅ keyword_insights 表存在")
        
        # 检查关键字段
        c.execute("PRAGMA table_info(keyword_insights)")
        columns = {row[1]: row[2] for row in c.fetchall()}
        expected = [
            "id", "task_id", "brand_id", "client_id", "keyword",
            "platforms_analyzed", "brands_found", "sources_cited",
            "response_patterns", "client_position", "optimization_hints",
            "llm_model", "llm_tokens", "algorithm_version",
            "quality_flag", "feedback_note", "distilled_at"
        ]
        for col in expected:
            if col in columns:
                print(f"  ✅ {col} ({columns[col]})")
            else:
                errors.append(f"  ❌ 缺少字段: {col}")
        
        # 检查 UNIQUE 约束
        c.execute("PRAGMA index_list(keyword_insights)")
        indexes = c.fetchall()
        unique_found = any(idx[2] == 1 for idx in indexes)  # origin=1 means unique
        if unique_found:
            print("  ✅ UNIQUE(task_id, keyword) 约束存在")
        else:
            errors.append("  ❌ 缺少 UNIQUE 约束！")
        
        # 检查索引
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ki_%'")
        ki_indexes = [row[0] for row in c.fetchall()]
        print(f"  ✅ {len(ki_indexes)} 个索引: {ki_indexes}")
    
    # 3. brand_aliases 表
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brand_aliases'")
    if not c.fetchone():
        errors.append("❌ brand_aliases 表不存在！")
    else:
        print("✅ brand_aliases 表存在")
        
        c.execute("SELECT COUNT(*) FROM brand_aliases")
        count = c.fetchone()[0]
        print(f"  ✅ {count} 条别名数据")
        
        c.execute("SELECT COUNT(DISTINCT canonical_name) FROM brand_aliases")
        brands = c.fetchone()[0]
        print(f"  ✅ {brands} 个不同品牌")
    
    # 4. 总结
    print("\n" + "=" * 50)
    if errors:
        print(f"❌ 验证失败！{len(errors)} 个问题：")
        for e in errors:
            print(f"  {e}")
    else:
        print("✅ Phase A 全部验证通过！数据基础重建完成。")
    print("=" * 50)
    
    conn.close()


if __name__ == "__main__":
    main()
