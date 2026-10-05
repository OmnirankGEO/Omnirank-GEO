import sqlite3, os

db_dir = os.path.join(os.path.dirname(__file__), '..', 'db')
db_file = os.path.join(db_dir, 'geo_diagnosis.db')

conn = sqlite3.connect(db_file)
c = conn.cursor()

# Check if brands table exists
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brands'")
result = c.fetchone()
print(f"brands table exists: {result is not None}")

if result:
    c.execute("SELECT COUNT(*) FROM brands")
    print(f"brands row count: {c.fetchone()[0]}")
    c.execute("SELECT * FROM brands LIMIT 3")
    cols = [d[0] for d in c.description]
    print(f"brands columns: {cols}")
    for row in c.fetchall():
        print(dict(zip(cols, row)))

# List all tables with row counts
print("\n--- All tables with counts ---")
c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for (name,) in c.fetchall():
    if name == 'sqlite_sequence':
        continue
    c.execute(f'SELECT COUNT(*) FROM [{name}]')
    cnt = c.fetchone()[0]
    if 'brand' in name.lower() or 'client' in name.lower():
        print(f"  *** {name}: {cnt}")
    elif cnt > 0:
        print(f"  {name}: {cnt}")

conn.close()
