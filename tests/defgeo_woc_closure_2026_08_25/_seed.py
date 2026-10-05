"""工单C 判据的种子行。**只造被测代码不负责造的东西**。

🔴 纪律:夹具不许替被测代码干活。例如 activation outbox 那一行,
   在测「入队是否冻结 payer」的判据里**必须由生产入队函数写**;
   只有在测「物化器怎么处置存量行」时才由夹具直插(那正是存量形态)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tests.defgeo_woc_closure_2026_08_25.conftest import connect

#: 本包用到的测试身份。session 开局由 conftest 清角色行,这里按意图重建。
#: 哨兵:``new_run_cell`` 缺省从该格自己的品牌取 owner。
_FROM_BRAND = object()

TENANT_A = 990101
TENANT_B = 990102
TENANT_ADMIN = 990103

TEST_USER_IDS = (TENANT_A, TENANT_B, TENANT_ADMIN)


def _exec(cur, sql: str, params: tuple = ()) -> Any:
    cur.execute(sql, params)
    return cur


def install_identities() -> None:
    """三个测试身份 + admin 角色。**幂等**。"""
    conn = connect()
    try:
        cur = conn.cursor()
        for uid in TEST_USER_IDS:
            cur.execute(
                "INSERT INTO users (id, username, display_name, password_hash, email) "
                "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                (uid, f"woc_u{uid}", f"woc_u{uid}", f"woc_u{uid}@example.test"))
        cur.execute("INSERT INTO roles (name, display_name) VALUES ('admin','管理员') "
                    "ON CONFLICT DO NOTHING")
        cur.execute("SELECT id FROM roles WHERE name='admin'")
        row = cur.fetchone()
        if row:
            role_id = row["id"] if not isinstance(row, tuple) else row[0]
            # 只给 ADMIN 那一个身份挂 admin —— 另外两个必须是普通身份,
            # 否则「个人钱包腿」那一臂测的是夹具而不是判别位。
            cur.execute(
                "DELETE FROM user_roles WHERE user_id = ANY(%s)", (list(TEST_USER_IDS),))
            cur.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING", (TENANT_ADMIN, role_id))
        # 🔴 媒体目录:``execution_budget_policy.derive`` 从 ``mhz_media`` 的单价
        #    推执行预算上限,一行都没有时它抛「媒体目录里没有任何可选媒体的单价」。
        #    那种红与本包被测的东西无关(我第一版就是这么红的)——
        #    形态照抄包E 的 ``install_base_rows``,不自创。
        for mid, mname, domain, points in (
            (960001, "工单C 判据媒体甲", "woc-a.example.test", 3000),
            (960002, "工单C 判据媒体乙", "woc-b.example.test", 5000),
        ):
            cur.execute(
                "INSERT INTO mhz_media (id, media_name, source_domain, our_price_points, "
                "is_active, provider, provider_media_id) "
                "VALUES (%s,%s,%s,%s,TRUE,'mhz',%s) ON CONFLICT (id) DO NOTHING",
                (mid, mname, domain, points, mid + 500000))
        conn.commit()
    finally:
        conn.close()


#: 品牌名唯一 —— ``brands_name_owner_key`` 是 (name, owner_user_id) 唯一约束。
#: 用**库里现有最大 id + 计数**派生名字,而不是 uuid/random:
#: 一次性库会被反复复用,随机名在跨 session 里也可能撞;派生名单调递增。
_BRAND_SEQ = [0]


def new_brand(*, owner: int, name: str | None = None) -> int:
    _BRAND_SEQ[0] += 1
    name = name or f"工单C 判据品牌 #{_BRAND_SEQ[0]}"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brands (name, owner_user_id) "
            "VALUES (%s || '·' || (SELECT COALESCE(MAX(id),0)+1 FROM brands)::text, %s) "
            "RETURNING id",
            (name, int(owner)))
        brand_id = cur.fetchone()["id"]
        conn.commit()
        return int(brand_id)
    finally:
        conn.close()


def set_brand_owner(brand_id: int, owner: int) -> None:
    """品牌转移 —— C-1 的被测场景本体。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE brands SET owner_user_id=%s WHERE id=%s",
                    (int(owner), int(brand_id)))
        conn.commit()
    finally:
        conn.close()


def new_quote(*, brand_id: int, created_at: str = "2026-08-25 10:00:00") -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO quotes (brand_id, brand_name, status, created_at) "
            "VALUES (%s,'工单C',%s,%s) RETURNING id",
            (int(brand_id), "quoted", created_at))
        quote_id = cur.fetchone()["id"]
        conn.commit()
        return int(quote_id)
    finally:
        conn.close()


def new_selection_session(*, brand_id: int, quote_id: int, token: str,
                          status: str = "quoted",
                          expires_at: str = "2099-01-01T00:00:00") -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO keyword_selection_sessions "
            "(token, quote_id, brand_id, keywords_snapshot, status, expires_at) "
            "VALUES (%s,%s,%s,'[]',%s,%s) RETURNING id",
            (token, int(quote_id), int(brand_id), status, expires_at))
        sid = cur.fetchone()["id"]
        conn.commit()
        return int(sid)
    finally:
        conn.close()


def delivery_plan(*, publications: int) -> dict[str, Any]:
    """一份最小但**合法**的 ``geo-delivery-plan-v1``。

    🔴 形态逐字取自包E 的同名夹具(``tests/defensive_geo_pkge_2026_08_24/_seed.py``)——
       ``execution_budget_policy.derive`` 只读 ``contract_minimums.publications``,
       自己另编一个形状会让物化器在"承诺篇数为 0"上直接抛,
       而那种红与本包被测的东西毫无关系(我第一版就是这么红的)。
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


def new_pricing_snapshot(*, quote_id: int, session_id: int) -> tuple[int, str]:
    """一份冻结报价快照 + 它的 hash。``enqueue_activation`` 的复合 FK 指向它。"""
    digest = hashlib.sha256(f"woc:{quote_id}:{session_id}".encode()).hexdigest()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO quote_pricing_snapshots "
            "(quote_id, brand_id, selection_session_id, version, reason, "
            " calculation_version, pricing_snapshot, snapshot_hash) "
            "SELECT %s, s.brand_id, %s, 1, '工单C 判据夹具', 'v1', %s, %s "
            "  FROM keyword_selection_sessions s WHERE s.id = %s "
            "RETURNING id",
            (int(quote_id), int(session_id),
             json.dumps({"delivery_plan": delivery_plan(publications=2)}),
             digest, int(session_id)))
        snap_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE keyword_selection_sessions SET active_pricing_snapshot_id=%s "
            "WHERE id=%s", (snap_id, int(session_id)))
        conn.commit()
        return int(snap_id), digest
    finally:
        conn.close()


def new_client_access_token(*, quote_id: int, token: str,
                            is_active: int = 1, expires_at: str | None = None) -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO client_access_tokens (quote_id, brand_name, token, is_active, expires_at) "
            "VALUES (%s,'工单C',%s,%s,%s) RETURNING id",
            (int(quote_id), token, int(is_active), expires_at))
        tid = cur.fetchone()["id"]
        conn.commit()
        return int(tid)
    finally:
        conn.close()


def new_article(*, quote_id: int | None, content: str,
                title: str = "工单C 判据稿件") -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        # 🔴 ``articles.topic_id`` 是 NOT NULL(生产 schema 实核),所以先造一条
        #    topic。夹具照真 schema 建行,不去改被测库的约束 ——
        #    放宽约束的测试库与生产不同构,判据会全绿而生产照炸。
        cur.execute(
            "INSERT INTO topics (quote_id, original_keyword, optimized_title, status) "
            "VALUES (%s,'工单C 判据词',%s,'draft') RETURNING id",
            (quote_id, title))
        topic_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO articles (topic_id, quote_id, title, content) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (topic_id, quote_id, title, content))
        aid = cur.fetchone()["id"]
        conn.commit()
        return int(aid)
    finally:
        conn.close()


def article_content(article_id: int) -> str:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM articles WHERE id=%s", (int(article_id),))
        row = cur.fetchone()
        conn.rollback()
        return str((row or {}).get("content") or "")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 监测侧(C-3)
# ══════════════════════════════════════════════════════════════════════════
def new_monitoring_task(*, brand_id: int, client_id: str = "woc") -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO monitoring_tasks (brand_id, client_id) VALUES (%s,%s) RETURNING id",
            (int(brand_id), client_id))
        tid = cur.fetchone()["id"]
        conn.commit()
        return int(tid)
    finally:
        conn.close()


def new_run_cell(*, task_id: int, brand_id: int, platform: str = "dashscope",
                 state: str = "queued", plan_hash: str | None = None,
                 provider_dispatched: bool = False,
                 fulfillment_state: str = "reserved",
                 tenant_owner_user_id: Any = _FROM_BRAND) -> tuple[int, str]:
    """一个 plan cell。``plan_hash`` 就是账本里的 ``plan_cell_id``。

    [工单 E3-1] ``tenant_owner_user_id`` 缺省从**该格自己的品牌**取 owner ——
    与生产 ``create_monitoring_run_cells`` 那一刻的冻结口径同源。
    🔴 不能缺省成一个固定的 TENANT_A:多租户判据(c3_11)会让 B 的格挂上 A 的租户,
       而那正好是本单要消灭的"归属错人"。显式传 ``None`` = 造一个租户未知的格。
    """
    digest = plan_hash or hashlib.sha256(
        f"woc-cell:{task_id}:{platform}:{state}:{provider_dispatched}".encode()
    ).hexdigest()
    conn = connect()
    try:
        cur = conn.cursor()
        owner = tenant_owner_user_id
        if owner is _FROM_BRAND:
            cur.execute("SELECT owner_user_id FROM brands WHERE id=%s", (int(brand_id),))
            _row = cur.fetchone()
            owner = (_row or {}).get("owner_user_id") if _row else None
        cur.execute(
            """INSERT INTO monitoring_run_cells
                 (task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,
                  question_snapshot, target_brand_snapshot, platform, is_planned,
                  state, entitlement_snapshot, order_snapshot, fulfillment_credential,
                  fulfillment_state, plan_hash, provider_dispatched_at,
                  tenant_owner_user_id)
               VALUES (%s,%s,1,'confirmed','kw','q','b',%s,TRUE,%s,'{}'::jsonb,'{}'::jsonb,
                       gen_random_uuid(), %s, %s, %s, %s)
               RETURNING id""",
            (int(task_id), int(brand_id), platform, state, fulfillment_state, digest,
             "2026-08-25 10:00:00+00" if provider_dispatched else None,
             owner))
        cid = cur.fetchone()["id"]
        conn.commit()
        return int(cid), digest
    finally:
        conn.close()


def attempts_for(plan_cell_id: str) -> list[dict[str, Any]]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT attempt_id, terminal_state, error_code, provider_called, "
            "       tenant_owner_user_id, brand_id, run_authority_id "
            "  FROM defgeo_monitoring_attempts WHERE plan_cell_id=%s "
            " ORDER BY attempt_ordinal", (plan_cell_id,))
        rows = [dict(r) for r in (cur.fetchall() or [])]
        conn.rollback()
        return rows
    finally:
        conn.close()


def outbox_rows(accepted_snapshot_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM defgeo_activation_outbox WHERE accepted_snapshot_id=%s "
            "ORDER BY id", (int(accepted_snapshot_id),))
        rows = [dict(r) for r in (cur.fetchall() or [])]
        conn.rollback()
        return rows
    finally:
        conn.close()
