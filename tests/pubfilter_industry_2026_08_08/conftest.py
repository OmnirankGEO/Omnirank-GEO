"""自媒体推荐行业过滤(B 单 2026-08-08)· 真库 fixture。

🔴 schema **不手写**:一律用生产同款初始化器,且按 `server.py` 的真实调用顺序
   (`init_publish_tables()` :1561 → `init_mhz_tables()` :1592)。
   顺序是有意义的 —— 两份 DDL 都 `CREATE TABLE IF NOT EXISTS mhz_media`,
   先跑的那份赢;后跑的那份靠 migration_columns 补列。生产的 mhz_media
   (`inclusion_rate` 是 **text** 而不是 meijiehezi 版的 integer)证明 publish 版先跑。
   反过来跑 = fixture 形态与生产不同 = 锁跑在一张生产不存在的表上。
   test_schema_parity.py 把这件事做成机械断言,不靠人记得。

运行前置:
    TEST_DATABASE_URL=postgresql://geo_admin:pubtest@127.0.0.1:55640/geo_pubfilter_test
(根 tests/conftest.py 会把它顶到 DATABASE_URL;库名必须含 'test' 才放行。)
"""

from __future__ import annotations

import os

import pytest

EXPECTED_DB_SUFFIX = "geo_pubfilter_test"


def _guard_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.rstrip("/").endswith(EXPECTED_DB_SUFFIX):
        raise RuntimeError(
            "本套件会 TRUNCATE 媒体池表,只允许跑在专用测试库上。\n"
            f"当前 DATABASE_URL 尾部不是 {EXPECTED_DB_SUFFIX}:{url!r}\n"
            "请用:TEST_DATABASE_URL=postgresql://geo_admin:pubtest@127.0.0.1:55640/"
            f"{EXPECTED_DB_SUFFIX} python -m pytest tests/pubfilter_industry_2026_08_08/"
        )
    return url


@pytest.fixture(scope="session", autouse=True)
def _schema():
    _guard_database_url()
    from db.auth_db import init_auth_db
    from db.meijiehezi_db import init_mhz_tables
    from db.publish_db import init_publish_tables

    init_auth_db()          # publish_batches/publish_orders 的 users FK
    init_publish_tables()   # ← server.py:1561:先跑,mhz_media 用它那份 DDL
    init_mhz_tables()       # ← server.py:1592:后跑,migration_columns 补齐缺列
    yield


@pytest.fixture()
def pool(_schema):
    """每个用例一张干净的池子。返回一个 seeding helper。"""
    from db.connection import get_connection

    conn = get_connection()
    cur = conn.cursor()
    for table in ("media_effective_pool", "mhz_media", "mhz_wemedia"):
        cur.execute(f"TRUNCATE TABLE {table}")
    conn.commit()

    class _Seeder:
        def __init__(self, conn, cur):
            self.conn = conn
            self.cur = cur

        def media(self, mid, *, name, industry, tier="L2", score=90.0,
                  recommendable=True, resource_type_name=None):
            """一条门户媒体候选(mhz_media 侧 + 池子侧,两边都要有才 JOIN 得上)。

            🔴 信号给得足是**有意的**:`build_recommendation_packages` 不信任池子里
               存的 tier/effective_score,而是拿本函数投影出来的行**重算一遍**
               (services/publish_recommendation.py:436-443)。信号不足 → 重算成 L0
               → 组合包空 → 端到端那几条锁会退化成恒真。
               (另见交付单「第二层发现」:池子里没有 citation_rate 这一列,
                重算时 evidence_score 恒 0,所以这条路径上 L2 永远出不来。)
            """
            self.cur.execute(
                """
                INSERT INTO mhz_media (id, media_name, resource_type_name, portal_media,
                                       our_price_yuan, our_price_points, price,
                                       inclusion_rate, pc_weight, m_weight,
                                       geo_rank, authority_media, is_active)
                VALUES (%s, %s, %s, '门户网站', 50, 6500, 50, '80', 10, 10, 5, 1, TRUE)
                """,
                (mid, name, resource_type_name if resource_type_name is not None else industry),
            )
            self._pool("media", mid, name, industry, tier, score, recommendable)

        def wemedia(self, mid, *, name, industry, tier="L2", score=90.0,
                    recommendable=True, wemedia_industry=None):
            """一条自媒体候选。"""
            self.cur.execute(
                """
                INSERT INTO mhz_wemedia (id, toutiao_name, platform, industry,
                                         our_price_yuan, our_price_points, price,
                                         geo_rank, authority_media, is_active)
                VALUES (%s, %s, '头条号', %s, 30, 3900, 30, 1, 0, TRUE)
                """,
                (mid, name, wemedia_industry if wemedia_industry is not None else industry),
            )
            self._pool("wemedia", mid, name, industry, tier, score, recommendable)

        def _pool(self, source, mid, name, industry, tier, score, recommendable):
            self.cur.execute(
                """
                INSERT INTO media_effective_pool
                    (media_source, media_id, platform_name, industry,
                     quality_score, evidence_score, price_score, noise_score,
                     effective_score, is_recommendable, tier, tags, reasons, updated_at)
                VALUES (%s, %s, %s, %s, 80, 80, 80, 0, %s, %s, %s, '{}'::jsonb, '[]'::jsonb, NOW())
                """,
                (source, mid, name, industry, score, recommendable, tier),
            )

        def commit(self):
            self.conn.commit()

    seeder = _Seeder(conn, cur)
    yield seeder
    conn.rollback()
    conn.close()
