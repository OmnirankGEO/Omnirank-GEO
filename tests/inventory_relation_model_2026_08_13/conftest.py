"""库存与关系模型修订 · 测试夹具 = **生产整库 schema 快照**。

🔴 为什么复用 `tests/inventory_distribution_chain_2026_08_12/prod_schema_2026-08-12.sql`
   而不是手写 BASE_SCHEMA:手写夹具与生产不同构时,判据会**悄悄失效**而测试照样全绿
   (本仓连续踩过:少一条 CHECK / 列类型不同 / 缺唯一索引 → 全绿但生产炸)。
   这份 dump 取自同一个生产尖 `00466fd7`,与本包被测代码同构。

🔴 `search_path` 毒(2026-08-12 实证):pg_dump 头部有 `SELECT pg_catalog.set_config(
   'search_path', '', false)`,它在**同一条连接**上一直生效 → 之后所有不带 schema
   限定的语句在干净库上全部 ERROR,而预灌过的库因为 guard 短路照样全绿。
   所以这里灌完 dump **立刻显式** `SET search_path TO public`,并且本包
   `_schema_ready` 不做"表已存在就跳过"的短路 —— 短路正是上次假绿的成因。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROD_SCHEMA = ROOT / "tests" / "inventory_distribution_chain_2026_08_12" / "prod_schema_2026-08-12.sql"
INVCHAIN_MIGRATION = ROOT / "scripts" / "migration_inventory_distribution_chain_2026_08_12.sql"

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

# 本包所有构造账号都落在这个 id 段,收尾只清这一段(绝不 TRUNCATE 全表 ——
# 本仓有过「套件把测试库跑烂 488→92 表」的事故)。
TEST_ID_BASE = 940000


def _assert_safe_test_database() -> None:
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL is required")
    name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in name or "prod" in name:
        raise RuntimeError(f"unsafe TEST_DATABASE_URL database name: {name!r}")


@pytest.fixture(scope="session", autouse=True)
def _schema_ready():
    _assert_safe_test_database()
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='channel_pricing_relationships'"
    )
    already = cur.fetchone()[0] > 0
    if not already:
        cur.execute(PROD_SCHEMA.read_text(encoding="utf-8"))
        # 🔴 解毒必须紧跟在 dump 之后,且在任何后续迁移之前。
        cur.execute("SET search_path TO public")
        cur.execute(INVCHAIN_MIGRATION.read_text(encoding="utf-8"))
        cur.execute("SET search_path TO public")
    # 反向对照:确认解毒真的生效 —— 不带 schema 限定也能查到表。
    cur.execute("SET search_path TO public")
    cur.execute("SELECT count(*) FROM channel_pricing_relationships")
    conn.close()
    yield


@pytest.fixture
def db():
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()
    cur.execute("SET search_path TO public")
    conn.commit()
    yield conn
    conn.rollback()
    cur = conn.cursor()
    cur.execute("SET search_path TO public")
    # 🔴 顺序即正确性:`dealer_inventory_lots.owner_agent_user_id` 外键指向 users,
    #    不先删 lot 就删不掉 users → 清理静默失败 → 残留的渠道关系撞
    #    `ux_channel_rel_active`,下一个用例莫名其妙红。
    #    (P0 热修新增供货用例后当场暴露 —— 之前没有用例建 lot,所以看不出来。)
    for table, column in (
        ("agent_inventory_transactions", "agent_user_id"),
        ("agent_inventory_transactions", "related_customer_user_id"),
        ("dealer_inventory_lots", "owner_agent_user_id"),
        ("agent_inventory_wallets", "agent_user_id"),
        # [2026-08-17 xfer 包新增] `brands` 与 `point_transactions` 也要清:
        #   · brands —— §R1 判据要给服务商造名下品牌(泄露源),不清会被
        #     `brands_name_owner_key` 唯一索引卡住下一次跑;
        #   · point_transactions —— §R2 落库判据真调 allocate-offline 端点,
        #     它写的 `agent_grant` 流水同时是**幂等标记**,不清 → 第二次跑
        #     直接走重放分支、余额不再变化 = 判据静默失效(不是转红,是变成恒真)。
        ("point_transactions", "user_id"),
        ("brands", "owner_user_id"),
        ("channel_pricing_relationships", "buyer_dealer_id"),
        ("channel_pricing_relationships", "upstream_channel_account_id"),
        ("customer_agent_bindings", "customer_user_id"),
        ("customer_agent_bindings", "agent_user_id"),
        ("admin_user_governance_audits", "subject_user_id"),
        ("admin_user_governance_versions", "subject_user_id"),
        ("customer_agent_binding_history", "customer_user_id"),
        ("user_wallets", "user_id"),
        ("users", "id"),
    ):
        cur.execute(f"DELETE FROM {table} WHERE {column} >= %s", (TEST_ID_BASE,))
    conn.commit()
    conn.close()


# ============================================================
# 构造工具
# ============================================================

def make_user(cur, user_id: int, *, phone: str, name: str, agent_level: int) -> int:
    """建一个账号 + 钱包。`agent_level>=1` = 服务商。"""
    cur.execute(
        """INSERT INTO users (id, username, display_name, phone, password_hash)
           VALUES (%s,%s,%s,%s,'x')
           ON CONFLICT (id) DO UPDATE SET display_name=EXCLUDED.display_name,
                                          phone=EXCLUDED.phone""",
        (user_id, f"u{user_id}", name, phone),
    )
    cur.execute(
        """INSERT INTO user_wallets (user_id, agent_level)
           VALUES (%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level""",
        (user_id, agent_level),
    )
    return user_id


def make_binding(cur, *, customer_user_id: int, agent_user_id: int, source: str = "admin_manual") -> None:
    cur.execute(
        """INSERT INTO customer_agent_bindings (customer_user_id, agent_user_id, binding_source)
           VALUES (%s,%s,%s)
           ON CONFLICT (customer_user_id) DO UPDATE SET agent_user_id=EXCLUDED.agent_user_id""",
        (customer_user_id, agent_user_id, source),
    )


def make_brand(cur, *, owner_user_id: int, name: str) -> int:
    """给账号挂一个名下品牌。

    §R1 的泄露源就是这张表:搜索 SQL 用 `ORDER BY b.id DESC LIMIT 1` 取
    "目标名下最新品牌"当 `brand_name`。目标是服务商时,那是 TA **客户**的名字。
    """
    cur.execute(
        """INSERT INTO brands (name, owner_user_id, user_id, status)
           VALUES (%s,%s,%s,'active') RETURNING id""",
        (name, owner_user_id, owner_user_id),
    )
    return int(cur.fetchone()["id"])


def make_channel(cur, *, buyer_dealer_id: int, upstream_user_id: int, bps: int) -> int:
    cur.execute(
        """INSERT INTO channel_pricing_relationships
               (buyer_dealer_id, upstream_channel_account_id, relationship_version,
                cost_multiplier_bps, status, effective_from)
           VALUES (%s,%s,%s,%s,'active',NOW())
           RETURNING id""",
        (buyer_dealer_id, upstream_user_id, "v1", bps),
    )
    return int(cur.fetchone()["id"])
