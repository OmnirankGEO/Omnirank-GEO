"""包E 夹具种子 —— **只造前置事实,不替被测代码干活**。

🔴 规矩三条(每条都对应一种记过的假绿):
  1. 种子只写**现役表**里的真行(品牌 / 钱包 / 目录 / 报价快照 / 文章),
     绝不直接写 ``defgeo_provider_execution_budgets`` ——
     那张表由被测的物化器签发,种子替它写 = 判据恒绿;
  2. id 用的时候现要(``MAX(id)+1``),不写死常量:撕锁 runner 会把同一套判据
     跑很多遍,写死常量第 2 发就撞 UniqueViolation,而那种红与被测代码无关;
  3. 每条判据一把新格(新 accepted snapshot / 新 plan_item_key),
     上一条留下的 command 不许变成下一条的前置状态。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from services.defensive_geo.publish import body_hash as _bh

from tests.defensive_geo_pkge_2026_08_24.conftest import connect

TENANT_A = 9701
TENANT_B = 9702
BRAND_A = 9801
BRAND_B = 9802

# ── [R2 · 平台成本腿] ────────────────────────────────────────────────────
#: 🔴 **带 admin 角色**的品牌主。物化器判付款方时是**现查角色**
#:    (``users`` 表没有 ``is_admin`` 列),所以这里必须真写 ``user_roles`` 行 ——
#:    只在 ``identity()`` 里塞 ``is_admin=True`` 只骗得过端点,骗不过 worker。
TENANT_ADMIN = 9704
BRAND_ADMIN = 9803
#: 🔴 平台直营服务账号:冻结**落到它头上**,且它**必须不是 admin**
#:    (是 admin 就撞 billing 免单旁路 ⇒ ``publish_funding`` 拒零句柄)。
PLATFORM_ACCOUNT = 9705

#: 🔴 [R2 返修] 本包用到的**全部**测试身份。``conftest._clean_queue`` 拿它
#:    在 session 开局清角色行 —— 身份轴必须由本包自己重建,不能继承库里攒的。
#:    判据 ``test_r2_42`` 机械对账:这张表必须覆盖 ``install_base_rows`` 真写的每一个 uid。
TEST_USER_IDS: tuple[int, ...] = (TENANT_A, TENANT_B, 9703, TENANT_ADMIN, PLATFORM_ACCOUNT)

#: 目录里的媒体单价(算力)。最贵的那一条决定 ``execution_budget_policy``
#: 推出来的天花板,所以判据能拿它做**算术对账**,而不是"看着差不多"。
MEDIA_SEED: tuple[tuple[int, str, str, int], ...] = (
    (991001, "包E 判据日报", "pkge-daily.com.cn", 130),
    (991002, "包E 行业网", "pkge-trade.com", 90),
    (991003, "包E 资讯站", "news.pkge-trade.com", 50),
)
MAX_UNIT_POINTS = max(p for *_x, p in MEDIA_SEED)


def identity(user_id: int, *, is_admin: bool = False) -> dict[str, Any]:
    """🔴 键名逐字照抄 ``auth/middleware.py`` 的 ``user_data_source`` 字面量。

    2026-08-20 的事故形态:夹具写 ``{"id": ...}``、生产中间件写 ``{"user_id": ...}``
    ⇒ 整片端点生产必 500 而判据全绿。
    """
    now = int(time.time())
    return {
        "iat": now,
        "exp": now + 7200,
        "user_id": user_id,
        "username": f"pkge_test_{user_id}",
        "display_name": f"pkge {user_id}",
        "is_admin": is_admin,
        "roles": [],
        "permissions": [],
        "client_brand_ids": [],
        "perm_version": 1,
        "must_change_password": 0,
        "team_context": None,
    }


IDENTITIES: dict[str, dict[str, Any]] = {
    "a": identity(TENANT_A),
    "b": identity(TENANT_B),
    "admin": identity(9703, is_admin=True),
    #: [R2] 平台腿那一臂:**既是 admin、又是品牌主**(端点侧与 worker 侧同一个人)。
    "adminowner": identity(TENANT_ADMIN, is_admin=True),
}


def install_base_rows() -> None:
    """用户 / 钱包 / 品牌 / 媒体目录 / 发布 SKU。幂等。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            # 🔴 列名以真库为准(SQL 四维度核验第 1 条:肉眼核对 \d feature_pricing)。
            #    这张表**没有** cost_yuan —— 照抄别处的 INSERT 会当场 UndefinedColumn。
            # [合流 2026-08-24 Review-CTO] 计费代号已按包G 段二③(Owner 批)统一为
            # media_proxy_publish(publish_funding.PUBLISH_FEATURE_CODE),种子跟码走。
            # cost_points=0 是生产真值(动态定价 SKU,Review 生产只读实证 08-24;
            # 别拉平成媒体价 —— 夹具替被测代码干活 ⇒ 判据恒绿,包G conftest 有全注)。
            "INSERT INTO feature_pricing (feature_code, feature_name, cost_points, "
            "is_active) VALUES ('media_proxy_publish','媒体代发(动态定价)',0,TRUE) "
            "ON CONFLICT (feature_code) DO NOTHING")
        for uid, name in ((TENANT_A, "包E 服务商甲"), (TENANT_B, "包E 服务商乙"),
                          (9703, "包E 管理员"),
                          (TENANT_ADMIN, "包E 管理员品牌主"),
                          (PLATFORM_ACCOUNT, "包E 平台直营服务账号")):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name) "
                "VALUES (%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                (uid, f"pkge_test_{uid}", name))
            cur.execute(
                "INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points) "
                "VALUES (%s, 500000, 0, 0) ON CONFLICT (user_id) DO NOTHING", (uid,))
        # 🔴 [R2] admin **角色行**必须真写:worker 侧判身份是现查 roles,
        #    不看 identity() 里那个 is_admin(那只影响端点侧)。
        cur.execute(
            "INSERT INTO roles (name, display_name, is_system) "
            "VALUES ('admin','管理员',1) ON CONFLICT (name) DO NOTHING")
        cur.execute(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT %s, id FROM roles WHERE name='admin' "
            "ON CONFLICT (user_id, role_id) DO NOTHING", (TENANT_ADMIN,))
        for bid, owner in ((BRAND_A, TENANT_A), (BRAND_B, TENANT_B),
                           (BRAND_ADMIN, TENANT_ADMIN)):
            cur.execute(
                "INSERT INTO brands (id, name, owner_user_id, industry) "
                "VALUES (%s,%s,%s,'本地生活服务') ON CONFLICT (id) DO NOTHING",
                (bid, f"包E 判据品牌 {bid}", owner))
        for mid, mname, domain, points in MEDIA_SEED:
            cur.execute(
                "INSERT INTO mhz_media (id, media_name, source_domain, our_price_points, "
                "is_active, provider, provider_media_id) "
                "VALUES (%s,%s,%s,%s,TRUE,'mhz',%s) ON CONFLICT (id) DO NOTHING",
                (mid, mname, domain, points, mid + 500000))
        # 🔴 [工单B B-3 · 2026-08-25 回归夹具追加] 外发通道**已配置**这个前置事实。
        #    confirm 侧的 ``capability_available`` 从写死 True 改成真实
        #    ``provider_transport.readiness()`` 之后,这一行不在 ⇒ readiness False
        #    ⇒ 每一条 confirm 都 503(ADMISSION_UNAVAILABLE)。
        #    那不是"判据被削",是生产语义真的多了一个前置:通道从没配过就不许冻钱。
        #    所以夹具必须**真造这个前置事实**,而不是把探针 patch 掉
        #    (patch 掉 = 夹具替被测代码干活 ⇒ 那条闸恒绿)。
        #    反向对照判据(删掉这一行 ⇒ confirm 必 503 且零副作用)在
        #    tests/defgeo_wob_publish_funding_2026_08_25/ 里。
        cur.execute(
            "INSERT INTO mhz_config (key, value) VALUES ('phpsessid','pkge-fixture-session') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.commit()
    finally:
        conn.close()


def _next_ids() -> tuple[int, int]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(id), 780000) + 1 AS n FROM quote_pricing_snapshots")
        accepted = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COALESCE(MAX(quote_id), 670000) + 1 AS n FROM keyword_selection_sessions")
        quote = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    return accepted, quote


def delivery_plan(*, publications: int) -> dict[str, Any]:
    """一份最小但**合法**的 ``geo-delivery-plan-v1``。

    ``contract_minimums.publications`` 是 ``execution_budget_policy`` 唯一读的那个数,
    所以判据可以让它变化,再对账推出来的 cap —— 一个真正有判别力的乘数。
    """
    return {
        "schema_version": "geo-delivery-plan-v1",
        "campaign_mode": "defensive",
        "contract_minimums": {
            "questions": publications,
            "articles": publications,
            "publications": publications,
            "distinct_public_media": 1,
            "distinct_root_domains": 1,
        },
        "items": [
            {
                "plan_item_key": f"item_{i + 1}",
                "objective_mode": "defensive",
                "brand_exposure": "named",
                "commitment_kind": "contract",
                "question_identity_key": f"qid_{i + 1}",
                "question_revision": 1,
                "global_ordinal": i + 1,
                "authorized_article_capacity": 1,
                "minimum_articles": 1,
                "minimum_publications": 1,
            }
            for i in range(publications)
        ],
    }


def seed_accepted_snapshot(
    *, tenant: int = TENANT_A, brand: int = BRAND_A,
    publications: int = 2, activate: bool = True,
) -> dict[str, Any]:
    """造一份**客户已接受**的报价快照 + 会话 + (可选)activation 入队行。

    🔴 ``activate=True`` 时只入队 ``pending`` —— **不写 materialized**。
       物化是被测行为(``activation_materializer``),夹具替它写 = 判据恒绿。
       窗C 那份 helper 直接写 ``status='materialized'`` 是对的(它测的是别的),
       本包不能照抄。
    """
    accepted, quote = _next_ids()
    snapshot_hash = uuid.uuid4().hex + uuid.uuid4().hex
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO quotes (id, brand_id, service_days) VALUES (%s,%s,90) "
                    "ON CONFLICT (id) DO NOTHING", (quote, brand))
        cur.execute(
            "INSERT INTO keyword_selection_sessions "
            "(token, quote_id, brand_id, keywords_snapshot, expires_at, status) "
            "VALUES (%s,%s,%s,'[]','2099-01-01','active') RETURNING id",
            (f"tok_pkge_{accepted}", quote, brand))
        session_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quote_pricing_snapshots "
            "(id, quote_id, brand_id, selection_session_id, version, reason, "
            " calculation_version, pricing_snapshot, snapshot_hash) "
            "VALUES (%s,%s,%s,%s,1,'包E 判据夹具','v1',%s,%s)",
            (accepted, quote, brand, session_id,
             json.dumps({"delivery_plan": delivery_plan(publications=publications)}),
             snapshot_hash))
        cur.execute(
            "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id = %s, "
            "customer_confirmed_snapshot_hash = %s, customer_confirmed_at = NOW() "
            "WHERE id = %s", (accepted, snapshot_hash, session_id))
        if activate:
            cur.execute(
                # 🔴 [工单 C-1 · 迁移 052] 冻结 payer 三件套**必须由夹具供**——
                #    生产入队(`selection_api._commit_frozen_quote_confirmation`)
                #    从这一版起一定写它们,而 `activation_materializer.frozen_identity`
                #    缺任一格就转人工。夹具不供 = 夹具供了一份生产不会发的行,
                #    整包物化判据会以"转人工"的形态红掉(本仓记过这个形态)。
                # 🔴 判别位走生产同一个函数,不在夹具里手抄两个字面量:
                #    手抄的那份会在判别位改口径时静默漂移,而判据照样全绿。
                "INSERT INTO defgeo_activation_outbox "
                "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
                " accepted_snapshot_hash, status, tenant_owner_id, "
                " payer_user_id, payer_funding_policy, payer_principal_kind) "
                "VALUES (%s,%s,%s,'commercial_basis_established',%s,'pending',%s,%s,%s,%s)",
                (accepted, quote, brand, snapshot_hash, tenant,
                 tenant, _payer_of(cur, tenant).funding_policy,
                 _payer_of(cur, tenant).principal_kind))
        conn.commit()
    finally:
        conn.close()
    return {
        "acceptedSnapshotId": accepted,
        "quoteId": quote,
        "brandId": brand,
        "tenantOwnerId": tenant,
        "snapshotHash": snapshot_hash,
        "publications": publications,
    }


def mark_activation_materialized(accepted_snapshot_id: int) -> None:
    """把 activation 推到终态(043 的 CHECK 要求终态带 materialized_at)。

    只在**不测物化器**的那些判据里用 —— 例如真链判据只想要一个已激活的服务。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE defgeo_activation_outbox SET status='materialized', materialized_at=NOW() "
            "WHERE accepted_snapshot_id = %s AND status <> 'materialized'",
            (int(accepted_snapshot_id),))
        conn.commit()
    finally:
        conn.close()


def seed_article(*, brand: int = BRAND_A, body: str) -> tuple[str, str]:
    """造一篇真文章,返回 ``(articleRevisionId, articleHash)``。

    🔴 hash 由**真正落库的那段正文**算出来,不是另写一个常量:
       两处各算一遍时,worker 里那条「正文与冻结指纹对不上就不发」的守卫
       会被夹具自己的不一致触发,而那与被测行为无关。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        # 🔴 ``articles.topic_id`` 是 NOT NULL 且**无默认值**(真库实测)。
        #    articles 对 topics 没有 FK,但列还是得给 —— 所以顺手造一条 topic,
        #    而不是塞一个编出来的数字:编的数字在别处被 join 时会静默取不到行。
        cur.execute("SELECT COALESCE(MAX(id), 900000) + 1 AS n FROM topics")
        topic_id = int(cur.fetchone()["n"])
        cur.execute(
            "INSERT INTO topics (id, original_keyword, optimized_title, status) "
            "VALUES (%s,'包E 判据词','包E 判据稿件','confirmed')", (topic_id,))
        cur.execute(
            "INSERT INTO articles (topic_id, brand_id, title, content, status) "
            "VALUES (%s,%s,%s,%s,'draft') RETURNING id",
            (topic_id, brand, "包E 判据稿件", body))
        article_id = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()
    return f"article:{article_id}", _bh.body_hash(body)


def wallet(user_id: int) -> dict[str, int]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT paid_points, bonus_points, frozen_points FROM user_wallets "
                    "WHERE user_id = %s", (int(user_id),))
        row = cur.fetchone() or {}
        conn.rollback()
    finally:
        conn.close()
    return {k: int(row.get(k) or 0) for k in ("paid_points", "bonus_points", "frozen_points")}


def counts() -> dict[str, int]:
    """副作用分母。短连接、立刻关 —— 不把事务跨到 HTTP 调用上。"""
    from services.defensive_geo.publish import store as _store

    conn = connect()
    try:
        cur = conn.cursor()
        out: dict[str, int] = {}
        for t in (_store.COMMAND_TABLE, _store.OUTBOX_TABLE, _store.SNAPSHOT_TABLE,
                  _store.BUDGET_TABLE):
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COUNT(*) AS n FROM point_freezes WHERE task_ref LIKE 'defgeo_publish_%%'")
        out["point_freezes"] = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    return out


def command_row(publish_command_id: str) -> dict[str, Any]:
    from services.defensive_geo.publish import store as _store

    conn = connect()
    try:
        cur = conn.cursor()
        row = _store.get_command_any_tenant(cur, publish_command_id=publish_command_id)
        conn.rollback()
    finally:
        conn.close()
    assert row is not None, f"命令 {publish_command_id} 不存在"
    return dict(row)


def outbox_rows(publish_command_id: str) -> list[dict[str, Any]]:
    from services.defensive_geo.publish import store as _store

    conn = connect()
    try:
        cur = conn.cursor()
        rows = _store.outbox_rows(cur, publish_command_id=publish_command_id)
        conn.rollback()
    finally:
        conn.close()
    return rows


def _payer_of(cur, tenant: int):
    """夹具侧的付款方判别 —— 走**生产同一个**判别位。

    [工单 C-1] 生产入队在客户确认事务里调 `payer_classification.classify_user_id`
    把结果冻进 outbox 行。夹具照做,而不是硬编 "personal_wallet":
    本包有一臂是 admin 身份(TENANT_ADMIN),硬编会让那一臂的冻结值
    与生产判出来的不同,于是"平台腿"判据测的是夹具而不是被测代码。
    """
    from services.defensive_geo import payer_classification as _pc

    return _pc.classify_user_id(cur, int(tenant))
