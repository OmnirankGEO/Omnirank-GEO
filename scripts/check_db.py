import sqlite3

conn = sqlite3.connect('db/geo_diagnosis.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

# 检查client_profiles档案
print("=== 客户档案表 (client_profiles) ===")
c.execute("SELECT id, name, brand_id FROM client_profiles LIMIT 5")
for r in c.fetchall():
    bid = r[2][:8] if r[2] else 'None'
    print(f"ID: {r[0][:8]}... | 名称: {r[1]} | brand_id: {bid}")

# 检查client_materials
print("\n=== 客户资料表 (client_materials) ===")
c.execute("SELECT brand_id, company_intro, unique_value FROM client_materials LIMIT 3")
for r in c.fetchall():
    intro = (r[1] or '')[:40]
    bid = r[0][:8] if r[0] else 'None'
    print(f"brand_id: {bid}... | 公司简介: {intro}...")

conn.close()
