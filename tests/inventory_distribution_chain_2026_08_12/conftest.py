"""服务商分销链路测试 · 夹具 = **生产整库 schema 快照**。

🔴 为什么不手写 BASE_SCHEMA(本仓连续四次被手写夹具证伪):
    手写的表结构与生产不同构时,测试照样全绿而生产照样炸
    —— 少一条 CHECK、列类型不同、缺一个唯一索引,判据就悄悄失效。
    这里直接灌 `prod_schema_2026-08-12.sql`(生产 `pg_dump --schema-only` 现取),
    再叠本包迁移。fixture 里能跑通 = 生产 schema 上能跑通。

用法:
    TEST_DATABASE_URL=postgresql://geo_admin:testpass@localhost:55812/test_geo_agentscope \\
        python -m pytest tests/inventory_distribution_chain_2026_08_12 -q
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROD_SCHEMA = HERE / "prod_schema_2026-08-12.sql"
PACKAGE_MIGRATION = ROOT / "scripts" / "migration_inventory_distribution_chain_2026_08_12.sql"

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


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
        "WHERE table_schema='public' AND table_name='agent_inventory_admin_actions'"
    )
    if cur.fetchone()[0] == 0:
        cur.execute(PROD_SCHEMA.read_text(encoding="utf-8"))
        cur.execute(PACKAGE_MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    yield


@pytest.fixture
def db():
    """每个用例一条连接 · 用例自己 commit;结束后清掉本包造的数据。"""
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    yield conn
    conn.rollback()
    cur = conn.cursor()
    # 🔴 只清本包用例造的行,且**只按测试用户 id 段清**(id >= 900000)。
    #    绝不 TRUNCATE 全表:本仓有过"套件把测试库跑烂 488→92 表"的事故。
    for table, column in (
        ("agent_inventory_admin_actions", "agent_user_id"),
        ("agent_inventory_transactions", "agent_user_id"),
        ("agent_inventory_wallets", "agent_user_id"),
        ("dealer_inventory_lots", "owner_agent_user_id"),
        ("channel_partner_requests", "requester_user_id"),
        ("customer_agent_bindings", "customer_user_id"),
        ("admin_user_governance_audits", "subject_user_id"),
        ("admin_user_governance_versions", "subject_user_id"),
        ("point_transactions", "user_id"),
        ("user_wallets", "user_id"),
        ("audit_logs", "user_id"),
    ):
        cur.execute(f"DELETE FROM {table} WHERE {column} >= 900000")
    cur.execute("DELETE FROM channel_partner_requests WHERE target_user_id >= 900000")
    cur.execute(
        "DELETE FROM channel_pricing_relationships "
        "WHERE buyer_dealer_id >= 900000 OR upstream_channel_account_id >= 900000"
    )
    cur.execute("DELETE FROM recharge_orders WHERE user_id >= 900000")
    cur.execute("DELETE FROM public_account_codes WHERE user_id >= 900000")
    cur.execute("DELETE FROM customer_agent_bindings WHERE agent_user_id >= 900000")
    cur.execute("DELETE FROM users WHERE id >= 900000")
    conn.commit()
    conn.close()


# ============================================================
# 造数 helper
# ============================================================

# `chk_public_service_code_format` 的字符集:去掉了易混的 0/1/I/O。
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _code_suffix(user_id: int) -> str:
    """把 user_id 映射成 8 位合法编码后缀(确定性,便于复算)。"""
    value = int(user_id)
    chars = []
    for _ in range(8):
        chars.append(_CODE_ALPHABET[value % len(_CODE_ALPHABET)])
        value //= len(_CODE_ALPHABET)
    return "".join(reversed(chars))


def make_user(cur, user_id: int, *, agent_level: int = 0, paid: int = 0, bonus: int = 0,
              is_admin: bool = False, display_name: str = None) -> int:
    cur.execute(
        """INSERT INTO users(id, username, password_hash, display_name, is_active)
           VALUES (%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING""",
        (user_id, f"u{user_id}", display_name or f"用户{user_id}"),
    )
    cur.execute(
        """INSERT INTO user_wallets(user_id, paid_points, bonus_points, agent_level)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET
             paid_points=EXCLUDED.paid_points, bonus_points=EXCLUDED.bonus_points,
             agent_level=EXCLUDED.agent_level""",
        (user_id, paid, bonus, agent_level),
    )
    if agent_level >= 1:
        # 🔴 生产前置条件,不是测试脚手架:`lock_commercial_provider_for_assignment`
        #    要求承接方有 `public_account_codes.service_account_code`,
        #    否则一律 RelationshipConflict("commercial service price scope is not ready")。
        #    这条是被**生产 schema 夹具**逼出来的 —— 手写夹具里没有这张表,
        #    测试会全绿而生产会拒绝建绑定。
        cur.execute(
            """INSERT INTO public_account_codes(user_id, service_account_code, channel_account_code)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                 service_account_code=EXCLUDED.service_account_code""",
            # 编码格式有 CHECK:`^SV-[23456789A-Z(去 IO01)]{8}$` / `^CH-...$`。
            # 用 id 的十进制位映射到允许字符集,保证 8 位且逐账号唯一(两列都有 UNIQUE)。
            (user_id, f"SV-{_code_suffix(user_id)}", f"CH-{_code_suffix(user_id)}"),
        )
    if is_admin:
        cur.execute(
            "INSERT INTO roles(name, display_name) VALUES ('admin','管理员') "
            "ON CONFLICT (name) DO NOTHING"
        )
        cur.execute("SELECT id FROM roles WHERE name='admin'")
        role_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO user_roles(user_id, role_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (user_id, role_id),
        )
    return user_id


def give_inventory(cur, agent_user_id: int, *, paid: int = 0, bonus: int = 0,
                   with_lot: bool = True, lot_suffix: str = "") -> None:
    """给服务商造库存 —— 默认**同时**建对侧 lot(生产上正规链路就是两边都有)。

    `with_lot=False` 用来复现生产已有的漂移形态(钱包有、lot 没有)。
    """
    cur.execute(
        """INSERT INTO agent_inventory_wallets(
               agent_user_id, paid_inventory_points, bonus_inventory_points,
               total_purchased_points)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (agent_user_id) DO UPDATE SET
             paid_inventory_points=EXCLUDED.paid_inventory_points,
             bonus_inventory_points=EXCLUDED.bonus_inventory_points,
             total_purchased_points=EXCLUDED.total_purchased_points""",
        (agent_user_id, paid, bonus, paid + bonus),
    )
    # 流水必须同步造:对账等式是「钱包 vs 流水」,只造钱包会让等式天生不平。
    if paid > 0:
        cur.execute(
            """INSERT INTO agent_inventory_transactions(
                   agent_user_id, type, pool, points, balance_paid_after, balance_bonus_after)
               VALUES (%s,'purchase_prepay','paid',%s,%s,%s)""",
            (agent_user_id, paid, paid, bonus),
        )
    if bonus > 0:
        cur.execute(
            """INSERT INTO agent_inventory_transactions(
                   agent_user_id, type, pool, points, balance_paid_after, balance_bonus_after)
               VALUES (%s,'purchase_prepay','bonus',%s,%s,%s)""",
            (agent_user_id, bonus, paid, bonus),
        )
    if with_lot and paid > 0:
        cur.execute(
            """INSERT INTO dealer_inventory_lots(
                   lot_id, owner_agent_user_id, original_points, remaining_points,
                   reserved_points, acquisition_cost_cents, remaining_cost_cents,
                   reserved_cost_cents, status, source_kind, pricing_version, evidence_jsonb)
               VALUES (%s,%s,%s,%s,0,%s,%s,0,'active','platform_purchase','test-v1','{}'::jsonb)
               ON CONFLICT (lot_id) DO NOTHING""",
            (f"TESTLOT{agent_user_id}{lot_suffix}", agent_user_id, paid, paid,
             max(1, paid), max(1, paid)),
        )


def governance_version(cur, user_id: int, scope: str) -> int:
    cur.execute(
        "SELECT version FROM admin_user_governance_versions "
        "WHERE subject_user_id=%s AND scope=%s",
        (user_id, scope),
    )
    row = cur.fetchone()
    return int(row["version"]) if row else 1


def binding_needs_attention(cur, customer_user_id: int) -> bool:
    """用**判定侧同一段 SQL** 问「这条归属现在亮不亮灯」。

    🔴 刻意直接调 `_relation_attention_sql()` 而不是复制一份 SQL 到测试里:
       复制一份就等于测试和实现各有一套口径,改了实现测试照样绿。
    """
    from services.admin_user_governance import _relation_attention_sql

    cur.execute(
        f"""SELECT ({_relation_attention_sql()}) AS needs_attention
            FROM users u JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
            WHERE u.id=%s""",
        (customer_user_id,),
    )
    row = cur.fetchone()
    return bool(row and row["needs_attention"])
