"""
A1: 清理旧蒸馏数据
- 查看 response_insights 表现状
- DROP response_insights 表
- DROP brand_intelligence_view 视图
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "db" / "geo_diagnosis.db"

def main():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    
    # 1. 查看现状
    print("=" * 50)
    print("A1: 清理旧蒸馏数据")
    print("=" * 50)
    
    # 检查 response_insights 表
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='response_insights'")
    if c.fetchone():
        c.execute("SELECT COUNT(*) FROM response_insights")
        count = c.fetchone()[0]
        print(f"\n✅ response_insights 表存在，共 {count} 条数据")
    else:
        print("\n⚠️ response_insights 表不存在")
        conn.close()
        return
    
    # 检查 brand_intelligence_view
    c.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='brand_intelligence_view'")
    has_view = c.fetchone() is not None
    print(f"{'✅' if has_view else '⚠️'} brand_intelligence_view 视图{'存在' if has_view else '不存在'}")
    
    # 2. 删除
    print("\n--- 执行清理 ---")
    
    if has_view:
        c.execute("DROP VIEW IF EXISTS brand_intelligence_view")
        print("🗑️  已删除 brand_intelligence_view 视图")
    
    c.execute("DROP TABLE IF EXISTS response_insights")
    print("🗑️  已删除 response_insights 表")
    
    conn.commit()
    
    # 3. 验证
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='response_insights'")
    still_exists = c.fetchone()
    c.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='brand_intelligence_view'")
    view_still_exists = c.fetchone()
    
    print("\n--- 验证结果 ---")
    print(f"response_insights 表: {'❌ 仍存在!' if still_exists else '✅ 已清除'}")
    print(f"brand_intelligence_view: {'❌ 仍存在!' if view_still_exists else '✅ 已清除'}")
    
    conn.close()
    print("\n✅ A1 完成")

if __name__ == "__main__":
    main()
