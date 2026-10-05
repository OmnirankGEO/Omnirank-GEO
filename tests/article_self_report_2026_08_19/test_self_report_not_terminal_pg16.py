"""判据② · 「自报不能直接变 terminal fact」配对判据(WO §3 第 2 条)。

## 打法

伪造一条浏览器 `PUBLISH_PROGRESS` 成功回报(带一个我们完全控制不了的 `publicUrl`),
然后**驱动真实消费方的真实代码路径**去读它:

  · `services.article_data_health.get_article_data_health()` —— 数据健康度分子
  · `services.strict_article_outcomes.load_strict_outcomes()` —— 效果归因/KPI 的发布事实
  · `services.article_delivery_plan._process_publication_locked()` —— 交付槽投影的权威闸

判据是**成对**的,单向的那半没有判别力:

  阴性臂:自报刚落库(pending)→ 三个消费方一个都不认它是发布事实;
  阳性臂:服务端探针核实通过(verified)→ 同样三个消费方**必须**认。

只有阴性臂 = 判据可能恒假(比如夹具压根没插进去、或者 SQL 恒空),那种"全绿"
本仓栽过好几次。阳性臂就是分母自证:证明这三条读取路径确实**够得到**这一行。

🔴 夹具用生产整库 `pg_dump --schema-only`(2026-08-19 现取,490 张表)+ **本包迁移
   真跑一遍**。手搓建表在本仓栽过:类型不同构照样全绿,到生产副本上才炸。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCHEMA_SQL = Path(__file__).resolve().parent / "prod_schema_2026-08-19.sql"
MIGRATION_SQL = ROOT / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql"
#: [WO_301b ②c-1 2026-09-27] 与同包 test_funding_path_excludes_self_report_pg16 同一做法:schema 快照是 08-19 的,
#: 不含图文包迁移;被测的统计 SQL 现在引用 034 的 billing_mode。
#: 夹具要像生产 prestart 一样重放缺的迁移,否则红的是夹具不是代码。三条都 additive / 幂等,重放安全。
GEOIMG_MIGRATIONS = (
    ROOT / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    ROOT / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    ROOT / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
)

ARTICLE_ID = 990101
TOPIC_ID = 990102
QUOTE_ID = 990103
BRAND_ID = 990104
UID = "9901"
OWNER_UID = 9905
SLOT_EVENT_ID = 990106
REVISION_ID = 990107
PLAN_RUN_ID = 990108

ARTICLE_TITLE = "全域上榜自报收口验证专用标题一二三"
ARTICLE_BODY = "正文内容用于计算提交快照哈希。" * 8
#: 落在「今日头条」平台域清单内的自报 URL —— 域在清单内才谈得上内容指纹。
FORGED_PUBLIC_URL = "https://www.toutiao.com/article/990101/"
#: 域**不在**任何平台清单内的自报 URL(连 content_matched 都不给)
OFF_PLATFORM_URL = "https://attacker-mirror.test/a/990101"
#: `geo_article_delivery_slots.delivery_slot_key` 是 uuid 列(生产 schema 实证)
SLOT_KEY = "9901aaaa-0000-4000-8000-000000000001"


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺 schema 快照:{SCHEMA_SQL}"
    assert MIGRATION_SQL.exists(), f"缺本包迁移:{MIGRATION_SQL}"
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"artself_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        # 🔴 pg_dump 的 set_config('search_path','',false) 对**本连接**生效且不还原,
        #    后面全是 "relation does not exist"(本仓踩过)。
        cur.execute("SET search_path TO public")
        for _mig in GEOIMG_MIGRATIONS:
            cur.execute(_mig.read_text(encoding="utf-8"))
        # 迁移在生产 schema 上真跑一遍 —— 「本机能跑」不算数,要跑在目标形状上。
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
        # 幂等自证:prestart 每次部署无条件重放全部迁移,重放必须无害。
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))

    try:
        yield type("DB", (), {"url": url, "conn": conn})
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture(autouse=True)
def seed(db):
    """每个用例前把夹具重置成「文章存在 + 一条浏览器自报成功记录」。"""
    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        # 🔴 清理顺序 = FK 依赖的**逆序**,且 articles↔slots 是环:
        #    先把 articles.delivery_slot_key 置空断环,再从叶子往根删。
        #    顺序写错时"单跑绿、整跑红"(第一版就是这样),很容易误判成用例互相污染。
        cur.execute("UPDATE articles SET delivery_slot_key=NULL WHERE id=%s", (ARTICLE_ID,))
        cur.execute("DELETE FROM publish_record_verification_events WHERE publish_record_id IN "
                    "(SELECT id FROM publish_records WHERE user_id=%s)", (UID,))
        cur.execute("DELETE FROM publish_records WHERE user_id=%s", (UID,))
        cur.execute("DELETE FROM geo_article_plan_outbox WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM geo_article_delivery_slots WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM geo_article_delivery_slot_events WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM geo_article_plan_runs WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM geo_article_contract_revisions WHERE quote_id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM articles WHERE id=%s", (ARTICLE_ID,))
        cur.execute("DELETE FROM quotes WHERE id=%s", (QUOTE_ID,))
        cur.execute("DELETE FROM brands WHERE id=%s", (BRAND_ID,))
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
            (BRAND_ID, "自报收口验证品牌", OWNER_UID),
        )
        cur.execute(
            "INSERT INTO quotes (id, brand_id, service_days, owner_user_id) VALUES (%s,%s,%s,%s)",
            (QUOTE_ID, BRAND_ID, 365, OWNER_UID),
        )
        # 🔴 建序有环:articles.delivery_slot_key → slots → articles.id。
        #    先建无 slot 的 article,最后再回填 delivery_slot_key。
        cur.execute(
            "INSERT INTO articles (id, topic_id, quote_id, title, content, publication_profile) "
            "VALUES (%s,%s,%s,%s,%s,'standard')",
            (ARTICLE_ID, TOPIC_ID, QUOTE_ID, ARTICLE_TITLE, ARTICLE_BODY),
        )
        # 交付槽 sidecar 全链 —— 缺任一环,enqueue 会以 `article_not_sidecar_linked`
        # 早退,「零 outbox」就又变成零分母了。
        cur.execute(
            """
            INSERT INTO geo_article_contract_revisions
            (id, revision_key, source_event_key, source_version, owner_user_id, brand_id,
             quote_id, authority_snapshot, authority_snapshot_hash, delivery_count,
             contract_version)
            VALUES (%s,%s,%s,'artself-test-v1',%s,%s,%s,'{}'::jsonb,%s,1,'v1')
            """,
            (REVISION_ID, "r" * 64, "s" * 64, OWNER_UID, BRAND_ID, QUOTE_ID, "h" * 64),
        )
        cur.execute(
            """
            INSERT INTO geo_article_plan_runs
            (id, run_key, contract_revision_id, owner_user_id, brand_id, quote_id,
             run_mode, compiler_version, input_snapshot, input_snapshot_hash)
            VALUES (%s,%s,%s,%s,%s,%s,'shadow','v1','{}'::jsonb,%s)
            """,
            (PLAN_RUN_ID, "n" * 64, REVISION_ID, OWNER_UID, BRAND_ID, QUOTE_ID, "i" * 64),
        )
        cur.execute(
            """
            INSERT INTO geo_article_delivery_slot_events
            (id, event_key, delivery_slot_key, slot_version, event_kind, target_state,
             contract_revision_id, plan_run_id, owner_user_id, brand_id, quote_id,
             source_version, occurred_at)
            VALUES (%s,%s,%s,1,'created','active',%s,%s,%s,%s,%s,'artself-test-v1',
                    CURRENT_TIMESTAMP)
            """,
            (SLOT_EVENT_ID, "e" * 64, SLOT_KEY, REVISION_ID, PLAN_RUN_ID,
             OWNER_UID, BRAND_ID, QUOTE_ID),
        )
        cur.execute(
            """
            INSERT INTO geo_article_delivery_slots
            (delivery_slot_key, contract_revision_id, contract_ordinal, owner_user_id,
             brand_id, quote_id, current_state, projection_version, current_event_id,
             last_event_key, article_id)
            VALUES (%s,%s,1,%s,%s,%s,'active',1,%s,%s,%s)
            """,
            (SLOT_KEY, REVISION_ID, OWNER_UID, BRAND_ID, QUOTE_ID, SLOT_EVENT_ID,
             "e" * 64, ARTICLE_ID),
        )
        cur.execute("UPDATE articles SET delivery_slot_key=%s WHERE id=%s",
                    (SLOT_KEY, ARTICLE_ID))
        _insert_browser_self_report(cur)
    yield


def _insert_browser_self_report(cur, *, public_url: str = FORGED_PUBLIC_URL,
                                title: str = ARTICLE_TITLE) -> int:
    """逐字复刻插件后端 PUBLISH_PROGRESS 的写入形状。

    🔴 这里**故意**不去 import 那个 WS handler:它带 WebSocket / Redis / 鉴权一整套。
       复刻的是列集合与取值。
    🔴 [WO_273] 那个写入方随插件后端整体删除,原来钉它源码的
       `test_writer_only_writes_pending_or_unverified` 同单肯定式退役(见该处说明)。
       本夹具**照旧有效**:生产里存量的浏览器自报行就是这个形状,核实器 cron 与各读者
       今天读的正是它们 —— 下面各格测的是「存量自报行永远不被当成终态事实」。
    """
    import hashlib

    content_hash = hashlib.sha256(ARTICLE_BODY.encode("utf-8")).hexdigest()
    cur.execute(
        """
        INSERT INTO publish_records
        (user_id, article_title, platform, account_name, status, brand_id,
         article_id, submitted_title_snapshot, submitted_content_snapshot,
         submitted_content_snapshot_hash, submitted_content_snapshot_at,
         submitted_content_snapshot_source, public_url,
         public_url_reported_explicitly, public_url_report_source,
         public_url_verification_state, created_at)
        VALUES (%s,%s,'今日头条','acct-1','success',%s,%s,%s,%s,%s,
                CURRENT_TIMESTAMP,'browser_extension_channel_submit',%s,
                %s,%s,%s, CURRENT_TIMESTAMP - INTERVAL '1 hour')
        RETURNING id
        """,
        (UID, title, BRAND_ID, ARTICLE_ID, title, ARTICLE_BODY, content_hash,
         public_url or None, bool(public_url),
         "extension_explicit_public_url" if public_url else None,
         "pending" if public_url else "unverified"),
    )
    return int(cur.fetchone()["id"])


def _record(db) -> dict:
    with db.conn.cursor() as cur:
        cur.execute("SELECT * FROM publish_records WHERE user_id=%s ORDER BY id DESC LIMIT 1",
                    (UID,))
        return dict(cur.fetchone())


def _page_with_title(title: str = ARTICLE_TITLE) -> dict:
    return {"status": 200, "body": f"<html><h1>{title}</h1>正文…</html>",
            "final_url": FORGED_PUBLIC_URL, "hops": 0}


@contextmanager
def _txn(db):
    """开一条**非 autocommit** 连接跑写路径。

    🔴 夹具那条 `db.conn` 是 autocommit 的,而交付槽 outbox 那步要 `SAVEPOINT` ——
       autocommit 下直接 NoActiveSqlTransaction。也就是说:用 db.conn 跑写路径,
       outbox 那一段**根本没被执行过**,「零 outbox」自然恒真。
       生产路径(API endpoint / cron)都是事务里跑的,这里跟着来。
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SET search_path TO public")
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _probe(db, fetcher):
    """跑服务端探针 —— R2 之后它**最高只能给 content_matched**。"""
    from services.publication_url_verifier import probe_publish_record_with_cursor
    record_id = _record(db)["id"]
    with _txn(db) as cur:
        return probe_publish_record_with_cursor(cur, record_id, fetcher=fetcher)


def _attest(db, *, actor_user_id=7001, evidence="人工核实：已登录后台确认该文在列且账号为我方", note=None):
    """人工核实动作 —— 自助发布链唯一能产生 verified 的入口。"""
    from services.publication_url_verifier import attest_publish_record_with_cursor
    record_id = _record(db)["id"]
    with _txn(db) as cur:
        return attest_publish_record_with_cursor(
            cur, record_id, actor_user_id=actor_user_id, evidence=evidence, note=note)


@pytest.fixture
def outbox_enabled(monkeypatch):
    """🔴 打开 ARTICLE_PLAN_EVENT_OUTBOX_ENABLED。

    不打开的话「零 outbox」是**零分母** —— 闸关着的时候谁都写不进去,
    那条断言恒真、杀不动任何变异。打开之后 0 行才真的意味着「代码没去写」。
    生产该闸当前是 false(2026-08-19 运行时实测),这里打开只为让判据有判别力。
    """
    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    from services.article_closed_loop_contract import feature_flags
    assert feature_flags()["ARTICLE_PLAN_EVENT_OUTBOX_ENABLED"] is True, "闸没打开,分母仍是空的"
    yield


def _outbox_rows(db) -> int:
    """交付槽 outbox 行数 —— 「零 outbox」这条判据的分母。"""
    with db.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_plan_outbox")
        return int(dict(cur.fetchone())["n"])


def _audit_rows(db):
    with db.conn.cursor() as cur:
        cur.execute("SELECT * FROM publish_record_verification_events "
                    "WHERE publish_record_id=%s ORDER BY id", (_record(db)["id"],))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 消费方驱动:打**真实函数**,不重抄 SQL
# ---------------------------------------------------------------------------

@pytest.fixture
def as_app_db(db, monkeypatch):
    """把 `db.connection.get_connection` 指到一次性库,让消费方走自己的真实读取路径。"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    import db.connection as dbconn

    conns = []

    def _get():
        c = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
        with c.cursor() as cur:
            cur.execute("SET search_path TO public")
        conns.append(c)
        return c

    monkeypatch.setattr(dbconn, "get_connection", _get)
    yield
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


def _health_counts() -> dict:
    from services.article_data_health import get_article_data_health
    return get_article_data_health(since_days=365)["counts"]


def _strict_publication_article_ids() -> set[int]:
    """跑 `load_strict_outcomes` 的**真 SQL**,在缝上截下它查出来的 publication_facts。

    🔴 为什么截缝而不是直接断言返回值:`load_strict_outcomes` 最终返回的是归因
       事件,而事件还要 join `monitoring_results`。夹具里没有监测行 → 事件恒空 →
       "自报没进来"会**恒真**,那种绿是假绿(第一版就是这么写的,阳性臂当场把它
       照出来了)。所以判据打在真 SQL 的直接产物上,不打它下游的空集。

    🔴 也不重抄那段 SQL:重抄的话我改没改对 SQL 就没人验了。
    """
    import services.strict_article_outcomes as sao

    captured: list[list[dict]] = []
    original = sao.attribute_observations

    def _spy(publications, results, **kwargs):
        captured.append(list(publications))
        return original(publications, results, **kwargs)

    # 🔴 这里**不用** monkeypatch fixture:它的 undo() 是整体回滚,会把 `as_app_db`
    #    那条 get_connection 补丁一起撤掉(第一版就是这么写的,下一句 DB 调用当场
    #    打到了没建表的基础库)。只还自己这一个属性。
    sao.attribute_observations = _spy
    try:
        sao.load_strict_outcomes(since_days=365)
    finally:
        sao.attribute_observations = original
    assert captured, "load_strict_outcomes 没走到 attribute_observations —— 缝接错了"
    return {
        int(p["article_id"]) for p in captured[0]
        if p.get("article_id") is not None
        and str(p.get("publication_source")) == "publish_records"
    }


def _delivery_authority_verdict(db) -> str:
    """驱动交付槽投影的权威闸;返回 'ok' 或 AuthorityConflict 的原因串。"""
    from services.article_delivery_plan import AuthorityConflict, _process_publication_locked

    record = _record(db)
    # 🔴 这个 outbox 是**照真实行的形状**手搓的:权威闸通过之后流程会继续往下写
    #    slot event,那些字段一个都不能少 —— 少了会以 KeyError 中断,而 KeyError
    #    既不是"通过"也不是"拒绝",会把判据变成一个说不清的结果。
    outbox = {
        "quote_id": QUOTE_ID,
        "owner_user_id": OWNER_UID,
        "brand_id": BRAND_ID,
        "source_version": "artself-test-v1",
        "occurred_at": record["created_at"],
        "event_key": "artself-test-event",
        "authority_snapshot": {
            "publication_source": "publish_records",
            "publication_source_id": int(record["id"]),
            "article_id": ARTICLE_ID,
            "public_url": FORGED_PUBLIC_URL,
            "published_at": record["created_at"].isoformat(),
            # 🔴 真库列类型是 uuid,不是 text —— 随手编个 'artself-test-slot' 会在
            #    权威闸**之后**才炸,把阳性臂的结论污染成一个库错误。
            "delivery_slot_key": SLOT_KEY,
        },
    }
    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        try:
            _process_publication_locked(cur, outbox)
            return "ok"
        except AuthorityConflict as exc:
            return str(exc)


# ===========================================================================
# 阴性臂:自报刚落库 —— 谁都不许认它是发布事实
# ===========================================================================

def test_forged_self_report_lands_pending_not_verified(db):
    record = _record(db)
    assert record["public_url_verification_state"] == "pending"
    assert record["public_url_verified_at"] is None
    # 来源位仍诚实为真(浏览器确实显式回报过) —— 但它不再是权威位。
    assert record["public_url_reported_explicitly"] is True


def test_forged_self_report_absent_from_strict_outcomes(db, as_app_db):
    assert ARTICLE_ID not in _strict_publication_article_ids()


def test_forged_self_report_absent_from_data_health(db, as_app_db):
    health = _health_counts()
    assert int(health["successful_published_articles"]) == 0, health
    assert int(health["publication_snapshots"]) == 0, health


def test_forged_self_report_rejected_by_delivery_authority(db):
    assert _delivery_authority_verdict(db) == "publication_public_url_not_server_verified"


def test_forged_self_report_does_not_burn_the_article_snapshot(db):
    """快照位是**一次性**的:假自报一旦钉死,真发布就再也捕不到。

    收口前这一步在 WS 写入方里就发生了;收口后必须等核实通过才发生。
    """
    with db.conn.cursor() as cur:
        cur.execute("SELECT publication_snapshot_at, publication_snapshot_source "
                    "FROM articles WHERE id=%s", (ARTICLE_ID,))
        row = dict(cur.fetchone())
    assert row["publication_snapshot_at"] is None, row
    assert row["publication_snapshot_source"] is None, row


# ===========================================================================
# 🔴 R2 §① 头号判据:**攻击者同标题页复现**
#    Codex 复审指出 R1 的探针可被「做一个同标题的页面」骗过 —— 那种命中证明的是
#    「某个页面写着这个标题」,不是「这篇发布在该平台该账号下」。
#    这里正面复现那次攻击:指纹**会**命中,但结论只能停在 content_matched,
#    并且必须零快照、零 outbox、严格链一个都不认。
# ===========================================================================

def test_attacker_same_title_page_only_reaches_content_matched(db, as_app_db, outbox_enabled):
    result = _probe(db, lambda url: _page_with_title())   # 页面上有我方标题
    assert result["state"] == "content_matched", result
    assert result["reason"] == "content_fingerprint_matched"

    record = _record(db)
    assert record["public_url_verification_state"] == "content_matched"
    assert record["public_url_verification_source"] == "server_probe"
    # content_matched 不是终态:verified_at 必须还是空
    assert record["public_url_verified_at"] is None

    # 零快照
    with db.conn.cursor() as cur:
        cur.execute("SELECT publication_snapshot_at, publication_snapshot_source "
                    "FROM articles WHERE id=%s", (ARTICLE_ID,))
        art = dict(cur.fetchone())
    assert art["publication_snapshot_at"] is None, art
    assert art["publication_snapshot_source"] is None, art

    # 零 outbox
    assert _outbox_rows(db) == 0

    # 严格链一个都不认
    assert ARTICLE_ID not in _strict_publication_article_ids()
    assert int(_health_counts()["successful_published_articles"]) == 0
    assert _delivery_authority_verdict(db) == "publication_public_url_not_server_verified"

    # 用户面也不许把它念成已发布
    from services.publication_receipt_projection import project

    axes = project(record["status"], record["public_url_verification_state"])
    assert axes["publication"] == "reported_success_unverified"
    assert axes["countsAsPublished"] is False


def test_probe_can_never_write_verified_even_if_code_tries(db):
    """库级锁自证:绕过 service 直写 `verified + server_probe` 必须被库拒绝。

    §① 的规矩不能只靠 Python 的自觉 —— 代码写错、有人直写 SQL,库也得拦住。
    """
    import psycopg2

    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "UPDATE publish_records SET public_url_verification_state='verified',"
                " public_url_verification_source='server_probe' WHERE user_id=%s", (UID,))
    db.conn.rollback()


def test_service_layer_also_refuses_probe_signed_verified(db):
    """Python 侧第一道闸同样在:`_write_state` 断言挡下 server_probe 签 verified。"""
    from services import publication_url_verifier as v

    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        with pytest.raises(AssertionError):
            v._write_state(cur, _record(db)["id"], v.STATE_VERIFIED,
                           source=v.SOURCE_SERVER_PROBE, method="x", detail={})
    db.conn.rollback()


def test_url_outside_platform_domain_gets_nothing(db, as_app_db):
    """§① 「平台域清单外连 content_matched 都不给」。"""
    with db.conn.cursor() as cur:
        cur.execute("UPDATE publish_records SET public_url=%s WHERE user_id=%s",
                    (OFF_PLATFORM_URL, UID))

    def _should_not_be_called(url):
        raise AssertionError("域不在清单内就不该发起出站请求")

    result = _probe(db, _should_not_be_called)
    assert result["state"] == "needs_action"
    assert result["reason"] == "url_outside_platform_domain"
    assert ARTICLE_ID not in _strict_publication_article_ids()


def test_redirect_landing_outside_platform_domain_is_rejected(db, as_app_db):
    """入口域合规但**落地**跳到站外 —— 同样不给 content_matched。

    只校验入口 URL 的话,「站内短链 302 到攻击者站点」就绕过去了。
    """
    result = _probe(db, lambda url: {"status": 200, "body": f"<h1>{ARTICLE_TITLE}</h1>",
                                     "final_url": OFF_PLATFORM_URL, "hops": 1})
    assert result["state"] == "needs_action"
    assert result["reason"] == "final_url_outside_platform_domain"


# ===========================================================================
# 阳性臂(分母自证):**人工核实**通过 —— 同样三条路径必须认
#   没有这一臂,上面所有「都不认」可能只是因为夹具压根够不到(R1 就这么假绿过)。
# ===========================================================================

def test_human_attestation_becomes_terminal_fact(db, as_app_db):
    result = _attest(db)
    assert result["state"] == "verified", result
    record = _record(db)
    assert record["public_url_verification_state"] == "verified"
    assert record["public_url_verification_source"] == "human_attestation"
    assert record["public_url_verified_at"] is not None

    assert ARTICLE_ID in _strict_publication_article_ids()
    assert int(_health_counts()["successful_published_articles"]) == 1
    # 交付槽 sidecar 已在夹具里建全 → 权威闸通过后整条投影**跑到底**('ok')。
    # 这比"停在下一道闸"更强:它证明拒绝确实只来自 URL 权威那一条。
    assert _delivery_authority_verdict(db) == "ok"

    from services.publication_receipt_projection import project

    axes = project(record["status"], record["public_url_verification_state"])
    assert axes["publication"] == "verified_published"
    assert axes["countsAsPublished"] is True


def test_human_attestation_actually_writes_outbox(db, outbox_enabled):
    """分母自证:同一个闸打开时,人工核实**会**写出 outbox 行。

    有它,攻击者那条的「零 outbox」才是"代码没去写",而不是"闸关着写不进"。
    """
    before = _outbox_rows(db)
    result = _attest(db)
    assert result["state"] == "verified"
    assert result["promotion"]["captured"] is True, result
    assert _outbox_rows(db) > before, result["promotion"]


def test_human_attestation_captures_publication_snapshot(db):
    _attest(db)
    with db.conn.cursor() as cur:
        cur.execute("SELECT publication_snapshot_at, publication_snapshot_source "
                    "FROM articles WHERE id=%s", (ARTICLE_ID,))
        row = dict(cur.fetchone())
    assert row["publication_snapshot_source"] == "publish_records", row
    assert row["publication_snapshot_at"] is not None


def test_human_attestation_leaves_actor_evidence_and_audit(db):
    """§① 「actor + 证据 hash + 审计」—— 三样缺一样都不算核实过。"""
    import hashlib

    evidence = "人工核实：后台作品列表可见，账号 ID 一致，截图存档 2026-08-19"
    _attest(db, actor_user_id=7042, evidence=evidence, note="R2 验收")
    rows = _audit_rows(db)
    attests = [r for r in rows if r["verification_source"] == "human_attestation"]
    assert len(attests) == 1, rows
    ev = attests[0]
    assert ev["actor_user_id"] == 7042
    assert ev["actor_kind"] == "human"
    assert ev["to_state"] == "verified"
    assert ev["evidence_sha256"] == hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    assert ev["evidence_note"] == "R2 验收"


def test_attestation_without_real_evidence_is_refused(db):
    """空/过短证据 = 没有证据。宁可拒绝,也不要一条形同虚设的审计。"""
    for bad in ("", "   ", "ok"):
        with pytest.raises(ValueError):
            _attest(db, evidence=bad)
    assert _record(db)["public_url_verification_state"] != "verified"


def test_audit_table_refuses_human_attestation_without_evidence(db):
    """库级锁:human_attestation 缺 actor 或缺证据 hash 直接 23514。"""
    import psycopg2

    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO publish_record_verification_events "
                "(publish_record_id, actor_kind, action, to_state, verification_source) "
                "VALUES (%s,'human','human_attestation','verified','human_attestation')",
                (_record(db)["id"],))
    db.conn.rollback()


# ===========================================================================
# 探针失败的形态:一律 needs_action,一律**不静默**放行
# ===========================================================================

def test_page_without_our_fingerprint_needs_action(db, as_app_db):
    result = _probe(db, lambda url: {"status": 200, "body": "<html>别人的文章</html>",
                                     "final_url": FORGED_PUBLIC_URL, "hops": 0})
    assert result["state"] == "needs_action"
    assert result["reason"] == "fingerprint_absent_on_page"
    assert ARTICLE_ID not in _strict_publication_article_ids()


def test_probe_exception_needs_action_not_silent_pass(db, as_app_db):
    def _boom(url):
        raise ConnectionResetError("对端断开")

    result = _probe(db, _boom)
    assert result["state"] == "needs_action"
    assert result["reason"] == "probe_failed"
    assert "ConnectionResetError" in result["error"]
    assert ARTICLE_ID not in _strict_publication_article_ids()


def test_non_200_needs_action(db):
    result = _probe(db, lambda url: {"status": 404, "body": "",
                                     "final_url": FORGED_PUBLIC_URL, "hops": 0})
    assert result["state"] == "needs_action"
    assert result["reason"] == "probe_non_200"


def test_missing_anchor_cannot_pass(db, as_app_db):
    """没有指纹锚点 ≠ 判据通过。页面 200 也不许给 content_matched。"""
    with db.conn.cursor() as cur:
        cur.execute("UPDATE publish_records SET submitted_title_snapshot='短' WHERE user_id=%s",
                    (UID,))
    result = _probe(db, lambda url: _page_with_title("短"))
    assert result["state"] == "needs_action"
    assert result["reason"] == "no_fingerprint_anchor"
    assert ARTICLE_ID not in _strict_publication_article_ids()


# ===========================================================================
# 写入方 / 探针路径源码锁
# ===========================================================================

# [WO_273 · 2026-09-23 肯定式退役] 原格 `test_writer_only_writes_pending_or_unverified`:
#   钉住浏览器写入方(插件后端的 PUBLISH_PROGRESS)在源码层够不到 verified、也不做终态两步。
#   那个写入方随插件后端**整体删除** —— 自报入口没了,不是换了实现。
#   接替:
#     · 「不许再冒出一条自报写入方」→ test_publish_records_writer_census.py 的
#       `test_declared_writers_match_repo`(仓内写入方集合 == 台账,多一条即红;
#       台账里 browser_self_report 一档同单清零);
#     · 「插件后端不许悄悄回来」→ tests/extension_retirement_2026_09_23 的路由 / 模块缺席锁。
#   对应变异 M19 一并从 run_mutations.py 退役。


def test_probe_path_never_promotes_terminal_fact():
    """探针函数体里不许出现「升终态事实」的调用 —— 只有人工核实那条能调。"""
    import inspect

    from services import publication_url_verifier as v

    probe_src = inspect.getsource(v.probe_publish_record_with_cursor)
    assert "_promote_verified_fact_with_cursor" not in probe_src
    attest_src = inspect.getsource(v.attest_publish_record_with_cursor)
    assert "_promote_verified_fact_with_cursor" in attest_src


def test_verification_state_enum_is_closed_in_db(db):
    """CHECK 是闭集:塞一个枚举外的值必须被库挡下。"""
    import psycopg2

    with db.conn.cursor() as cur:
        cur.execute("SET search_path TO public")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE publish_records SET public_url_verification_state='trust_me' "
                        "WHERE user_id=%s", (UID,))
    db.conn.rollback()


# ===========================================================================
# R3 §① · verified 单调不可降级(P0)+ 独立可达轴
# ===========================================================================
# Codex 复现:人工核实过的记录,管理员再点一次「重核」,探针探不动就把它写回
# needs_action、连 public_url_verified_at 都清成 NULL —— 一次探测抹掉历史事实。
#
# 🔴 这一组判据是**成对**的,单向那半没有判别力:
#      阴性臂:已 verified 的行,同一个失败探针打上去 → 状态与 verified_at 原样;
#      阳性臂:**同一个** fetcher 打在未核实的行上 → 必须真把它打成 needs_action。
#    没有阳性臂,"状态没变"可能只是因为那个 fetcher 根本没被调用、或者探针整个
#    静默失败了 —— 那种绿是本仓栽过好几次的假绿。


def _boom_fetcher(url):
    """探不动。真实世界里就是页面 502 / 平台改版 / 文章被下架。"""
    raise ConnectionError("probe boom")


def test_verified_survives_admin_reprobe(db):
    """阴性臂:已核实的行被重核 —— 核实态与 verified_at 一个字不许变。"""
    _attest(db)
    before = _record(db)
    assert before["public_url_verification_state"] == "verified"
    assert before["public_url_verified_at"] is not None
    assert before["public_url_verification_source"] == "human_attestation"

    result = _probe(db, _boom_fetcher)

    after = _record(db)
    assert after["public_url_verification_state"] == "verified", "P0:探针把权威结论撤销了"
    assert after["public_url_verified_at"] == before["public_url_verified_at"], (
        "P0:verified_at 被重核抹掉/改写了 —— 它记的是当初何时被核实,不是谁最近点过按钮")
    assert after["public_url_verification_source"] == "human_attestation"
    # 探到的失效信息没有被丢掉,只是落在另一根轴上
    assert result["axis"] == "availability"
    assert after["public_url_availability_state"] == "unreachable"
    assert after["public_url_availability_checked_at"] is not None
    audit = [r for r in _audit_rows(db) if r["action"] == "availability_probe"]
    assert len(audit) == 1, "可达轴的探测也要留痕"
    assert audit[0]["to_state"] == "verified", "审计的 to_state 说的是核实轴(没变)"


def test_same_failing_probe_does_downgrade_an_unverified_row(db):
    """阳性臂(分母自证):**同一个** fetcher 打在未核实的行上必须真的降级。

    没有这一臂,上面那条"状态没变"可能只是因为探针压根没跑起来。
    """
    assert _record(db)["public_url_verification_state"] == "pending"
    result = _probe(db, _boom_fetcher)
    assert result["state"] == "needs_action"
    assert _record(db)["public_url_verification_state"] == "needs_action"


def test_write_state_chokepoint_refuses_downgrade(db):
    """守卫钉在唯一写核实轴的收口处 —— 绕过探针直接调它也拦得住。"""
    from services.publication_url_verifier import (
        STATE_NEEDS_ACTION, SOURCE_SERVER_PROBE, TerminalStateDowngradeRefused,
        _write_state,
    )

    _attest(db)
    before = _record(db)
    with pytest.raises(TerminalStateDowngradeRefused):
        with _txn(db) as cur:
            _write_state(cur, int(before["id"]), STATE_NEEDS_ACTION,
                         source=SOURCE_SERVER_PROBE, method="m", detail={})
    after = _record(db)
    assert after["public_url_verification_state"] == "verified"
    assert after["public_url_verified_at"] == before["public_url_verified_at"]


def test_db_trigger_refuses_downgrade_even_from_raw_sql(db):
    """第二道:**库级**触发器。代码写错、有人绕过 service 直写 SQL,库都拒绝。

    🔴 一道 Python 断言 + 一道库约束不是重复劳动:Python 那道保护的是调用路径,
       库这道保护的是"下一个人写了条新的 UPDATE"。本仓的资金链锁一直是两道。
    """
    import psycopg2

    _attest(db)
    before = _record(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _txn(db) as cur:
            cur.execute(
                "UPDATE publish_records SET public_url_verification_state='needs_action' "
                "WHERE id=%s", (int(before["id"]),))
    # 正对照:同一条 raw UPDATE 打在**未核实**的行上必须成功 —— 否则上面那个
    # CheckViolation 可能只是"这张表随便 UPDATE 都会炸"。
    with _txn(db) as cur:
        cur.execute("UPDATE publish_records SET public_url_verification_state='needs_action' "
                    "WHERE id=%s", (_insert_browser_self_report(cur),))


def test_raw_sql_cannot_downgrade_by_clearing_verified_at_too(db):
    """🔴 单调触发器**真正承重**的那一格:一条 UPDATE 同时把两列都改掉。

    变异实验打脸的地方:R4 §A 加了 `verified_at 有值 ⇒ state=verified` 这条 CHECK
    之后,原来那条"只改 state"的判据即使摘掉触发器**也照样红** —— CHECK 替它
    挡住了。于是"库级单调触发器"这一层在判据眼里成了**没人验的纵深防御**
    (M31 存活就是这么暴露的)。

    但两者并不等价:`SET state='needs_action', verified_at=NULL` 一条写完,
    CHECK 的两个析取项都满足,**只有触发器**能拦。而这恰恰是最自然的写法 ——
    一个想"重置这条记录"的人就会这么写。所以判据必须打在这一格上。
    """
    import psycopg2

    _attest(db)
    before = _record(db)
    assert before["public_url_verification_state"] == "verified"
    with pytest.raises(psycopg2.Error):
        with _txn(db) as cur:
            cur.execute(
                "UPDATE publish_records "
                "   SET public_url_verification_state = 'needs_action',"
                "       public_url_verified_at = NULL "
                " WHERE id = %s", (int(before["id"]),))
    after = _record(db)
    assert after["public_url_verification_state"] == "verified", (
        "两列一起改就绕过了单调性 —— 历史发布事实被一条 UPDATE 抹掉")
    assert after["public_url_verified_at"] == before["public_url_verified_at"]


def test_raw_sql_cannot_null_out_verified_at(db):
    """verified_at 是"当初何时被核实",任何后续 UPDATE 都不许改写它。"""
    _attest(db)
    before = _record(db)
    with _txn(db) as cur:
        cur.execute("UPDATE publish_records SET public_url_verified_at=NULL WHERE id=%s",
                    (int(before["id"]),))
    assert _record(db)["public_url_verified_at"] == before["public_url_verified_at"]


def test_human_retraction_lands_on_availability_axis(db):
    """页面被下架/撤回是**人**才能下的结论,而且落可达轴,不动发布事实。"""
    from services.publication_url_verifier import (
        AVAILABILITY_RETRACTED, record_availability_attestation_with_cursor,
    )

    _attest(db)
    before = _record(db)
    with _txn(db) as cur:
        out = record_availability_attestation_with_cursor(
            cur, int(before["id"]), actor_user_id=7002,
            availability=AVAILABILITY_RETRACTED,
            evidence="平台后台显示该文已于今日被下架,截图存档编号 A-0819",
            note="客户要求撤回")
    assert out["availability"] == "retracted"
    after = _record(db)
    assert after["public_url_availability_state"] == "retracted"
    # 🔴 被下架**不能**倒推成"当初没发过":两个命题互不蕴含,而后者已被据以结算。
    assert after["public_url_verification_state"] == "verified"
    assert after["public_url_verified_at"] == before["public_url_verified_at"]
    audit = [r for r in _audit_rows(db) if r["action"] == "availability_attestation"]
    assert len(audit) == 1
    assert audit[0]["actor_user_id"] == 7002
    assert audit[0]["evidence_sha256"]


def test_probe_only_reaches_available_on_the_availability_axis(db):
    """可达轴的正对照:页面还在 → available(否则"unreachable"可能是恒定值)。"""
    _attest(db)
    _probe(db, lambda url: _page_with_title())
    assert _record(db)["public_url_availability_state"] == "available"


def test_reattest_is_idempotent_and_keeps_first_verified_at(db, outbox_enabled):
    """重复人工核实不刷新时间戳、不重复升级发布事实(不多一条 outbox)。"""
    _attest(db)
    before = _record(db)
    outbox_before = _outbox_rows(db)
    out = _attest(db)
    assert out["reason"] == "already_verified"
    assert _record(db)["public_url_verified_at"] == before["public_url_verified_at"]
    assert _outbox_rows(db) == outbox_before


# ===========================================================================
# R4 §A · verified_at 有值 ⇔ state = 'verified'(库级 CHECK,防"首设直写")
# ===========================================================================
# R3 的触发器只在 `OLD.verified_at IS NOT NULL` 时保值 —— **从 NULL 到有值那一跳
# 它是放行的**,而单调守卫看的是 state 不是这一列。于是可以写出
# 「state 还是 needs_action、verified_at 已经有值」的自相矛盾行;
# 下游任何一处按 `verified_at IS NOT NULL` 判"核实过"(很自然的写法)就被绕过了。


def test_verified_at_cannot_be_set_on_an_unverified_row(db):
    """阴性臂:没核实过的行不许被直接写上核实时间。"""
    import psycopg2

    rid = int(_record(db)["id"])
    assert _record(db)["public_url_verification_state"] == "pending"
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _txn(db) as cur:
            cur.execute(
                "UPDATE publish_records SET public_url_verified_at = CURRENT_TIMESTAMP "
                "WHERE id = %s", (rid,))
    assert _record(db)["public_url_verified_at"] is None


def test_verified_row_legitimately_carries_verified_at(db):
    """阳性臂(分母自证):真核实过的行**必须**带得上这一列。

    没有这一臂,上面的 CheckViolation 可能只是"这一列根本写不进去"。
    """
    _attest(db)
    row = _record(db)
    assert row["public_url_verification_state"] == "verified"
    assert row["public_url_verified_at"] is not None


def test_migration_nulls_out_preexisting_contradictory_verified_at(db):
    """迁移里那条清洗 UPDATE 要真能清 —— 否则老库上加 CHECK 会直接失败。

    先造一个"矛盾行"(绕过 CHECK:先建成 verified 再降级是不行的,所以
    直接把约束摘掉造完再装回去 —— 这正是迁移在老库上要面对的局面)。
    """
    rid = int(_record(db)["id"])
    with _txn(db) as cur:
        cur.execute("ALTER TABLE publish_records DROP CONSTRAINT "
                    "publish_records_verified_at_requires_verified_state")
        cur.execute("UPDATE publish_records SET public_url_verified_at = CURRENT_TIMESTAMP "
                    "WHERE id = %s", (rid,))
    assert _record(db)["public_url_verified_at"] is not None, "矛盾行没造出来,后面等于没验"

    with _txn(db) as cur:
        cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
    row = _record(db)
    assert row["public_url_verified_at"] is None, "迁移没把矛盾行清掉"
    assert row["public_url_verification_state"] == "pending", "清洗不该动核实态"


# ===========================================================================
# R4 §B · retracted 的库级后盾(镜像核实轴,不只靠调用方自觉)
# ===========================================================================


def test_probe_source_cannot_write_retracted_at_db_level(db):
    """阴性臂:绕过 Python 直写 SQL,库也必须拒绝 `server_probe` 签的 retracted。"""
    import psycopg2

    rid = int(_record(db)["id"])
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _txn(db) as cur:
            cur.execute(
                "UPDATE publish_records SET public_url_availability_state='retracted',"
                " public_url_availability_source='server_probe' WHERE id=%s", (rid,))


def test_human_source_can_write_retracted_at_db_level(db):
    """阳性臂:同一条 SQL 换成人工来源必须写得进去。

    没有它,上面的 CheckViolation 可能只是"这两列根本写不进去"。
    """
    rid = int(_record(db)["id"])
    with _txn(db) as cur:
        cur.execute(
            "UPDATE publish_records SET public_url_availability_state='retracted',"
            " public_url_availability_source='human_attestation' WHERE id=%s", (rid,))
    assert _record(db)["public_url_availability_state"] == "retracted"


def test_availability_state_without_source_is_refused(db):
    """有可达结论就必须说清是谁下的(与核实轴 source 的存在性口径一致)。"""
    import psycopg2

    rid = int(_record(db)["id"])
    with pytest.raises(psycopg2.errors.CheckViolation):
        with _txn(db) as cur:
            cur.execute("UPDATE publish_records SET public_url_availability_state="
                        "'unreachable' WHERE id=%s", (rid,))


def test_write_availability_chokepoint_refuses_probe_signed_retraction(db):
    """收口处那道(库那道是第二道):`_write_availability` 必须自己也拦住。"""
    from services.publication_url_verifier import (
        AVAILABILITY_RETRACTED, SOURCE_SERVER_PROBE, _write_availability,
    )

    rid = int(_record(db)["id"])
    with pytest.raises(AssertionError):
        with _txn(db) as cur:
            _write_availability(cur, rid, AVAILABILITY_RETRACTED, {},
                                source=SOURCE_SERVER_PROBE)


def test_probe_writes_availability_with_its_own_source(db):
    """探针写可达轴时留的是 `server_probe`,人工留的是 `human_attestation` ——
    两者可分辨,否则"谁下的结论"这件事在库里查不出来。"""
    from services.publication_url_verifier import (
        AVAILABILITY_RETRACTED, record_availability_attestation_with_cursor,
    )

    _attest(db)
    _probe(db, _boom_fetcher)
    assert _record(db)["public_url_availability_source"] == "server_probe"

    with _txn(db) as cur:
        record_availability_attestation_with_cursor(
            cur, int(_record(db)["id"]), actor_user_id=7003,
            availability=AVAILABILITY_RETRACTED,
            evidence="平台后台显示该文已下架,截图存档编号 A-0820")
    assert _record(db)["public_url_availability_source"] == "human_attestation"
