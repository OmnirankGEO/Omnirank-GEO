def test_package_tables_exist(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE 'defgeo_publish%'")
        assert cur.fetchone()["c"] >= 4
