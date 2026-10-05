"""形态锁:测试库的表形态必须与生产一致,否则上面所有锁都跑在一张假表上。

🔴 本仓踩过的坑(2026-08-08 P4 包):测试库缺一列 → 被测函数的 try/except 把它吞成
   False → 锁「全红但看起来很严」,实为零判别力。反过来同理:形态比生产宽松,锁就
   在一张生产不存在的表上全绿。

下面这张期望表是 **2026-08-08 从生产只读取的**
(`information_schema.columns` · omnirank-db · 会话 READ ONLY 且以一条必失败的
 UPDATE 自证),不是照着 DDL 抄的 —— 因为两份 `CREATE TABLE IF NOT EXISTS mhz_media`
并存,谁先跑谁赢,只有生产能回答"实际是哪一份"。
"""

from __future__ import annotations

import pytest

# 生产实测形态(2026-08-08)。只列本次 SQL 真读到的列。
PROD_SHAPE = {
    "media_effective_pool": {
        "media_source": "text",
        "media_id": "bigint",
        "platform_name": "text",
        "industry": "text",
        "effective_score": "numeric",
        "is_recommendable": "boolean",
        "tier": "text",
        "updated_at": "timestamp without time zone",
    },
    "mhz_media": {
        "id": "integer",
        "media_name": "text",
        "our_price_yuan": "numeric",
        "our_price_points": "bigint",
        "price": "numeric",
        "portal_media": "text",
        "resource_type_name": "text",
        # 🔴 这一列是"两份 DDL 谁赢"的判别列:publish 版是 text,meijiehezi 版是
        #    integer。生产是 text → 说明 init_publish_tables 先跑。conftest 的调用
        #    顺序就是照这个结论定的。
        "inclusion_rate": "text",
        "pc_weight": "integer",
        "m_weight": "integer",
        "geo_rank": "integer",
        "authority_media": "integer",
        "is_active": "boolean",
    },
    "mhz_wemedia": {
        "id": "integer",
        "toutiao_name": "text",
        "our_price_yuan": "numeric",
        "our_price_points": "integer",
        "price": "numeric",
        "platform": "text",
        "industry": "text",
        "geo_rank": "integer",
        "authority_media": "integer",
        "is_active": "boolean",
    },
}


@pytest.fixture()
def cols(_schema):
    from db.connection import get_connection

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT table_name, column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = ANY(%s)
        """,
        (list(PROD_SHAPE),),
    )
    out: dict[str, dict[str, str]] = {t: {} for t in PROD_SHAPE}
    for row in cur.fetchall():
        out[row["table_name"]][row["column_name"]] = row["data_type"]
    conn.close()
    return out


@pytest.mark.parametrize("table", sorted(PROD_SHAPE))
def test_test_db_shape_matches_production(cols, table):
    actual = cols[table]
    assert actual, f"{table} 在测试库里根本不存在 —— 初始化器没跑到"
    mismatched = {
        col: (want, actual.get(col))
        for col, want in PROD_SHAPE[table].items()
        if actual.get(col) != want
    }
    assert not mismatched, f"{table} 与 2026-08-08 生产形态不一致(列: (期望, 实际)):{mismatched}"


def test_the_parity_check_can_actually_fail(cols):
    """反向对照:证明上面那三条不是"查不到就当过"。

    往期望表里塞一个生产没有的列,同一套比对逻辑必须报出来。
    """
    fake = dict(PROD_SHAPE["mhz_media"])
    fake["a_column_that_does_not_exist"] = "text"
    actual = cols["mhz_media"]
    mismatched = {c: (w, actual.get(c)) for c, w in fake.items() if actual.get(c) != w}
    assert "a_column_that_does_not_exist" in mismatched
