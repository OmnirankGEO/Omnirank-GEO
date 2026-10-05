"""[WO-D-R2 ①] 自举 schema 保真锁:`init_mhz_tables()` 建出来的表必须能被生产 SQL 读。

## 这条锁在守什么(2026-08-20 实证)

`billing_mode` 由迁移 `db/migration_034_geo_image_note_contract_2026_08_17.sql:409`
加到 `mhz_publish_order_items`,但**没进 `init_mhz_tables()` 的自举兜底**。后果不对称:

- **生产没事** —— prestart 每次部署无条件重放全部迁移,034 早跑过;
- **任何只靠 `init_mhz_tables()` 自举的新库**(CI / 新 staging / 重建预演 / 一次性
  测试库)拿到的表**没有这一列**,而生产 SQL 在读它 ⇒
  `tests/publish_zombie_2026_08_04` 6 条判据 **双臂恒红**:
  `column i.billing_mode does not exist` —— 红因与被测代码零关系。

「双臂恒红」是最坏的一种红:A/B 两边都红,比较器只会说「无新增红」,
于是它**永远不会被任何人当成缺陷看见**。

## 判据成对

- **必须命中** —— 自举后列在、CHECK 约束在,且**真的拒**非法值
  (只断言"列存在"验不出约束漏掉;只断言"约束存在"验不出它有没有生效);
- **必须不命中** —— 把 `db/meijiehezi_db.py` 里那两句兜底删掉,本文件立刻红,
  且红在「自举缺列」这个真因上,而不是散成 6 条看不懂的 UndefinedColumn。
"""
import psycopg2
import pytest

from db.connection import get_connection


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name=%s", (table,))
    return {r["column_name"] for r in cur.fetchall()}


def test_bootstrap_gives_billing_mode_column(_schema):
    """自举路径必须产出 `billing_mode` —— 生产 SQL 读它。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cols = _cols(cur, "mhz_publish_order_items")
    finally:
        conn.close()
    assert "billing_mode" in cols, (
        "init_mhz_tables() 自举出来的 mhz_publish_order_items 没有 billing_mode —— "
        "迁移 034 加过它,但自举兜底漏了。生产靠 prestart 重放迁移看不出来,"
        "新库/一次性测试库会 6 条判据双臂恒红。"
    )


def test_bootstrap_billing_mode_check_actually_rejects(_schema, db):
    """CHECK 必须**真的拒** —— 夹具比生产宽就是假绿。

    🔴 反向对照在同一条用例里:合法值必须**写得进去**。
       少了这半,把约束写成 `CHECK (false)` 也能让上半条通过。
    """
    cur = db.cursor()
    cur.execute(
        "INSERT INTO mhz_publish_orders (id, user_id, article_id, article_title,"
        " total_cost_points) VALUES (990001, 1, 1, 't', 0)")
    db.commit()

    # 合法值:写得进
    cur.execute(
        "INSERT INTO mhz_publish_order_items (id, order_id, user_id, media_id,"
        " media_name, status, billing_mode) VALUES (990101,990001,1,1,'m','pending',%s)",
        ("freeze_per_item",))
    db.commit()

    # 非法值:必须被库拒掉
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO mhz_publish_order_items (id, order_id, user_id, media_id,"
            " media_name, status, billing_mode) VALUES (990102,990001,1,1,'m','pending',%s)",
            ("nonsense_mode",))
    db.rollback()
