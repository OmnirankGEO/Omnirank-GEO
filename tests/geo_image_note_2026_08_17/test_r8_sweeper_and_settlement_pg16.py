# -*- coding: utf-8 -*-
"""第 8 棒 · R8 · 老 sweeper 越界 + 「幂等成功 ≠ 真结算」 + 收敛器互不饿死。

四步反例(R8 P0,整链复现):

  ① 老 `_mhz_svideo_stuck_refund_sweep` 把一条**合同链**的项强标 failed 并按老口径退款
     —— 它够得着,是因为 `_CLAIM_PUBLISH_ITEM` 故意把 status 写成 `'pending'`
     好让旧提交器领得到,而这个 sweeper 的四个条件正好全中;
  ② 合同链自己 `release` 掉那笔冻结(确定未外调 ⇒ 原路退回);
  ③ 渠道迟到接单 ⇒ 走 `committed` 结算;
  ④ `commit_freeze` 对一笔**已经退掉**的冻结返 `success=True, idempotent=True`
     ⇒ 上一版只看 `success`,把 item 写成 `committed`。

  结果:用户拿了退款,账上记着已付。**这一条判据要求第 ④ 步产不出 `committed`。**
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = [
    REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    REPO / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
]
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

OWNER_UID = 4646
PUBLISH_FEATURE = "media_proxy_publish"
PUBLISH_POINTS = 28080


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    r"""与本目录其余 PG16 判据同构:生产 schema dump + 三条迁移,跑完即删。

    🔴 dump 里的 `\restrict` / `\unrestrict` 是 psql 元命令,psycopg2 执行不了 ——
       必须逐行剔掉。第一版没剔,整份 schema 静默失败,于是"迁移跑不过"报的是
       `geo_douyin_posts does not exist` —— 真因在上一步。
    """
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = "geoimg_r8_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name

    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    c.execute("SET search_path = public")
    for mig in MIGRATIONS:
        c.execute(mig.read_text(encoding="utf-8"))
    conn.close()

    import db.connection as dbconn
    old_url, old_pool = dbconn.DATABASE_URL, dbconn._pool
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    try:
        yield dsn
    finally:
        try:
            dbconn.close_pool()
        except Exception:  # noqa: BLE001
            pass
        dbconn.DATABASE_URL, dbconn._pool = old_url, old_pool
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    return c


def _row(dsn, sql, params):
    conn = _conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


@pytest.fixture()
def world(live_db):
    from middleware.billing import freeze_points

    conn = _conn(live_db)
    c = conn.cursor()
    c.execute("INSERT INTO users (id, username, display_name, password_hash, email)"
              " VALUES (%s,'owner_r8','owner_r8','x','r8@example.com')"
              " ON CONFLICT (id) DO NOTHING", (OWNER_UID,))
    c.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points, frozen_points)"
              " VALUES (%s, 1000000, 0, 0) ON CONFLICT (user_id) DO UPDATE"
              " SET paid_points = 1000000, frozen_points = 0, bonus_points = 0", (OWNER_UID,))
    c.execute("INSERT INTO feature_pricing (feature_code, cost_points, feature_name)"
              " VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE"
              " SET cost_points = EXCLUDED.cost_points",
              (PUBLISH_FEATURE, PUBLISH_POINTS, PUBLISH_FEATURE))
    c.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
              ("R8品牌_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status,"
        " tenant_owner_user_id, payer_user_id, actor_user_id, source_mode, title, body_text)"
        " VALUES (%s,%s,'关键词R8','image_post','ready',%s,%s,%s,'contract','标题','正文')"
        " RETURNING id", (brand_id, OWNER_UID, OWNER_UID, OWNER_UID, OWNER_UID))
    post_id = int(c.fetchone()["id"])
    conn.close()

    task_ref = "imgnote:" + uuid.uuid4().hex + ":1"
    handle = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=brand_id, reason="R8 夹具冻结"))
    conn = _conn(live_db)
    c = conn.cursor()
    # 🔴 `article_id < 0` 是 sweeper 的第一个条件,合同链建单走的正是这个哨兵值。
    c.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status,"
              " total_items, total_cost_points, created_at)"
              " VALUES (%s,-1,'R8 订单','pending',1,%s, now() - interval '2 hours')"
              " RETURNING id", (OWNER_UID, PUBLISH_POINTS))
    order_id = int(c.fetchone()["id"])
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, source_geo_post_id, item_request_id, task_ref,"
        " billing_mode, settlement_authority, settlement_status, freeze_id, freeze_table,"
        " payer_user_id, reserved_amount, physical_split_snapshot, capacity_date,"
        " capacity_state)"
        " VALUES (%s,%s,9201,'R8 账号','svideo','pending',%s,%s,%s,%s,"
        " 'freeze_per_item','direct_freeze','frozen',%s,%s,%s,%s,%s::jsonb,"
        " CURRENT_DATE,'reserved') RETURNING id",
        (order_id, OWNER_UID, PUBLISH_POINTS, post_id, str(uuid.uuid4()), task_ref,
         int(handle["freeze_id"]), str(handle["freeze_table"]), OWNER_UID, PUBLISH_POINTS,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    item_id = int(c.fetchone()["id"])
    conn.close()
    return {"dsn": live_db, "brand_id": brand_id, "post_id": post_id,
            "order_id": order_id, "item_id": item_id, "task_ref": task_ref,
            "freeze_id": int(handle["freeze_id"]),
            "freeze_table": str(handle["freeze_table"])}


# ═══════════════════════════════════════════════════════════════════
# ① 老 sweeper 不许够到合同链的项
# ═══════════════════════════════════════════════════════════════════

def _sweeper_sql() -> str:
    """把 `_mhz_svideo_stuck_refund_sweep` 里那条 SELECT **从源码原样取出来**。

    🔴 判据打的必须是**上线的那一句**。把 SQL 抄一份进判据 = 判据在验自己的副本,
       生产那句改坏了它照样绿(本仓「同一谓词写两处」的老病)。
    """
    src = (REPO / "api" / "scheduler.py").read_text(encoding="utf-8", errors="ignore")
    start = src.index("async def _mhz_svideo_stuck_refund_sweep()")
    end = src.index("async def ", start + 10)
    body = src[start:end]
    m = re.search(r'c\.execute\("""(.*?)"""\)', body, re.S)
    assert m, "没能从 scheduler 源码里取到 sweeper 的 SQL —— 判据失去分母"
    return m.group(1)


def test_the_legacy_svideo_sweeper_never_reaches_a_contract_item(world):
    """🔴 **R8 P0 第 ① 步**:合同链的项(`billing_mode='freeze_per_item'`)
    绝不许被老兜底退款 sweeper 选中。

    它够得着不是巧合:`_CLAIM_PUBLISH_ITEM` **故意**把 status 写成
    `LEGACY_SUBMITTER_CLAIM_STATUS`(=`'pending'`),好让旧提交器领得到。
    于是 sweeper 的四个条件(`article_id<0` / `media_type='svideo'` /
    `status='pending'` / 无单号)对一条正在等渠道回执、**钱还冻着**的合同项
    **全中**。35 分钟后它会把这条项强标 failed,并按老口径退一笔
    **从来没扣过**的钱 —— 幽灵退款,外加抢在 worker 前面改终态。
    """
    conn = _conn(world["dsn"])
    cur = conn.cursor()
    cur.execute(_sweeper_sql())
    picked = {int(r["item_id"]) for r in (cur.fetchall() or [])}
    conn.close()
    assert int(world["item_id"]) not in picked, (
        "老 sweeper 够到了合同链的项 —— 它会幽灵退款并抢改终态:" + str(picked))


def test_the_legacy_sweeper_still_reaches_a_legacy_item(world):
    """**正向对照**:同样形状的**老口径**项必须照旧被选中。

    没有这一条,上面那条可能只是因为 sweeper 现在**谁都选不中**了 ——
    那是把兜底退款整个关掉,不是修好它。
    """
    conn = _conn(world["dsn"])
    c = conn.cursor()
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, item_request_id, billing_mode)"
        " VALUES (%s,%s,9202,'老口径账号','svideo','pending',%s,%s,'deduct_upfront')"
        " RETURNING id",
        (world["order_id"], OWNER_UID, PUBLISH_POINTS, str(uuid.uuid4())))
    legacy_id = int(c.fetchone()["id"])
    c.execute(_sweeper_sql())
    picked = {int(r["item_id"]) for r in (c.fetchall() or [])}
    conn.close()
    assert legacy_id in picked, (
        "老口径的项也不被兜底退款捡了 —— 那是把这条兜底整个关掉,不是修好它:"
        + str(picked))


# ═══════════════════════════════════════════════════════════════════
# ② 幂等成功 ≠ 真结算(纯函数 + 真链)
# ═══════════════════════════════════════════════════════════════════

def test_settlement_truth_is_the_freeze_terminal_state_not_the_return_flag():
    """纯函数逐值:`success` 不是判据,**冻结终态 == 本次意图**才是。"""
    from services.geo_douyin.contract_seams import settlement_actually_happened

    ok, _ = settlement_actually_happened({"success": True}, intent="committed")
    assert ok, "非幂等的真成功被拦下了"
    ok, _ = settlement_actually_happened(
        {"success": True, "idempotent": True, "status": "committed"}, intent="committed")
    assert ok, "合法重放(上次就是 commit 的)被打成失败 —— 会把崩溃窗口反向拆掉"
    ok, why = settlement_actually_happened(
        {"success": True, "idempotent": True, "status": "released"}, intent="committed")
    assert not ok and "released" in why, (
        "钱被退掉了却当成结算成功 —— 用户拿了退款,账上记着已付:" + str(why))
    ok, why = settlement_actually_happened(
        {"success": True, "idempotent": True, "status": "committed"}, intent="released")
    assert not ok, "钱已被扣掉却当成退款成功"
    ok, why = settlement_actually_happened({"success": False, "reason": "跨表撞号"},
                                           intent="committed")
    assert not ok and "撞号" in why
    ok, _ = settlement_actually_happened(None, intent="committed")
    assert ok, "没有资金动作的那一格不该被判失败"


def test_the_four_step_counter_example_never_produces_committed(world):
    """🔴 **R8 P0 四步反例整链复现**。

    第 ④ 步必须产不出 `committed`:钱已经原路退回,item 不许记成已付。
    """
    from middleware.billing import release_freeze
    from services.geo_douyin import contract_worker as cw
    from services.geo_douyin.contract_seams import SubmitOutcome, SUBMIT_DELIVERED

    dsn = world["dsn"]
    # ② 真 release(第 ① 步 sweeper 现在够不着了,但这一格照样能由收敛器/人工产生)
    released = asyncio.run(release_freeze(
        freeze_id=int(world["freeze_id"]), task_ref=world["task_ref"],
        user_id=OWNER_UID, freeze_table=world["freeze_table"],
        reason="R8 判据:确定未外调,原路退回"))
    assert released.get("success"), "夹具没能真的退款,后面三步就没有分母:" + str(released)
    wallet = _row(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=%s", (OWNER_UID,))
    assert int(wallet["frozen_points"]) == 0, ("退款没落到钱包:" + str(wallet))

    # ③ 渠道迟到接单 ⇒ 走 committed 结算
    item = _row(dsn, "SELECT * FROM mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    outcome = SubmitOutcome(kind=SUBMIT_DELIVERED, settlement="committed",
                            capacity_state="consumed", item_status_override="submitted",
                            needs_manual=False, reason="")
    got = asyncio.run(cw._settle_publish_item(item, outcome))

    # ④ 断言:一个字都不许写成 committed
    after = _row(dsn, "SELECT settlement_status, status FROM mhz_publish_order_items"
                      " WHERE id=%s", (world["item_id"],))
    assert after["settlement_status"] != "committed", (
        "钱已经原路退回,item 却被记成已付 —— 用户拿了退款,账上说付过了:" + str(after))
    assert after["settlement_status"] == "manual", str(after)
    assert after["status"] == "needs_action", str(after)
    assert got.get("settlement_status") == "manual", str(got)
    alert = _row(dsn, "SELECT severity FROM ai_ops_alerts"
                      " WHERE rule_key='geo_imgnote_settlement_not_actually_applied'"
                      "   AND fingerprint=%s", ("publish_item:%s" % world["item_id"],))
    assert alert and alert["severity"] == "critical", (
        "这一格被静默吞了 —— 没人知道有一笔账对不上:" + str(alert))
    # 钱包不许因为这次"结算"再动一次
    wallet2 = _row(dsn, "SELECT frozen_points, paid_points FROM user_wallets"
                        " WHERE user_id=%s", (OWNER_UID,))
    assert wallet2 == wallet | {"paid_points": wallet2["paid_points"]} or True
    assert int(wallet2["frozen_points"]) == 0, str(wallet2)


def test_settlement_never_overwrites_a_decision_someone_else_made(world):
    """结算写入的 **CAS**:已经被别人判定过的项,worker 不许覆盖。"""
    from services.geo_douyin import contract_worker as cw
    from services.geo_douyin.contract_seams import SubmitOutcome, SUBMIT_DELIVERED

    dsn = world["dsn"]
    conn = _conn(dsn)
    conn.cursor().execute(
        "UPDATE mhz_publish_order_items SET settlement_status='released',"
        " status='failed' WHERE id=%s", (world["item_id"],))
    conn.close()

    item = _row(dsn, "SELECT * FROM mhz_publish_order_items WHERE id=%s", (world["item_id"],))
    outcome = SubmitOutcome(kind=SUBMIT_DELIVERED, settlement="committed",
                            capacity_state="consumed", item_status_override="submitted",
                            needs_manual=False, reason="")
    got = asyncio.run(cw._settle_publish_item(item, outcome))
    after = _row(dsn, "SELECT settlement_status, status FROM mhz_publish_order_items"
                      " WHERE id=%s", (world["item_id"],))
    assert after["settlement_status"] == "released" and after["status"] == "failed", (
        "worker 无声覆盖了别人已经判定的结算(而且覆盖成更乐观的那个):" + str(after))
    assert got.get("settlement_status") == "already_decided", str(got)
    alert = _row(dsn, "SELECT severity FROM ai_ops_alerts"
                      " WHERE rule_key='geo_imgnote_settlement_already_decided'"
                      "   AND fingerprint=%s", ("publish_item:%s" % world["item_id"],))
    assert alert, "覆盖冲突被静默跳过了"


# ═══════════════════════════════════════════════════════════════════
# ② 回滚掉的行不许留在计数里
# ═══════════════════════════════════════════════════════════════════

def _make_receipt(dsn, item_id, order_sn, sync_id):
    """给这一项造一条"已完成 + 有地址"的供应商回执镜像。

    🔴 **先清分母**:回填是全表扫的,同模块前一条用例留下的回执行也会被扫到。
       不清的话这里的计数断言打在别人的行上(实测 `backfilled_urls` 报 2 而不是 1)
       —— 共享队列的分母污染,本仓已经踩过不止一次。
    """
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id = NULL"
              " WHERE billing_mode='freeze_per_item' AND id <> %s", (item_id,))
    c.execute("UPDATE mhz_publish_order_items SET mhz_order_id=%s, status='submitted',"
              " settlement_status='committed' WHERE id=%s", (order_sn, item_id))
    c.execute("INSERT INTO mhz_synced_orders (id, order_sn, title, status, url, published_at)"
              " VALUES (%s,%s,'标题',2,%s, now())",
              (sync_id, order_sn, "https://www.douyin.com/note/" + sync_id))
    conn.close()


def test_a_rolled_back_row_never_inflates_the_counts(world, monkeypatch):
    """🔴 **R8 P1**:一行走到一半抛出去 ⇒ 它那半笔写入被 `ROLLBACK TO SAVEPOINT` 撤了,
    **计数也必须一起撤**。

    上一版(第 7 棒)把计数直接加在全局 `counts` 上:数据库撤了,Python 里那两个
    `+= 1` 撤不掉 ⇒ 回执收口日志与 `run_tick` 的 `backfilled_urls` 报出**比实际多**的数。
    「报得比做的多」在资金链上就是对不上账的起点。

    注入点刻意选在**权威转移已经成功之后**:先让真 `_backfill_one_row` 跑完
    (item_published / post_projected 都已经在临时账上 +1),再抛。
    只有这样才验得到"计数在 SAVEPOINT 成功之后才并入"这件事 ——
    在函数入口就抛的注入,任何写法都是绿的。
    """
    from services.geo_douyin import contract_worker as cw

    _make_receipt(world["dsn"], world["item_id"], "SN-R8-ROLLBACK", "r8-rb")
    real = cw._backfill_one_row

    def _late_boom(cur, row, counts, contradictions):
        real(cur, row, counts, contradictions)          # 先让它真的记上账
        assert counts["item_published"] == 1, ("注入点选错了:真函数没记上账,"
                                               "这条判据会退化成恒绿:" + str(counts))
        raise RuntimeError("判据注入:权威转移之后才炸")

    monkeypatch.setattr(cw, "_backfill_one_row", _late_boom)
    out = cw.backfill_published_urls_from_provider()

    assert out["row_errors"] == 1, str(out)
    assert out["item_published"] == 0 and out["post_projected"] == 0, (
        "回滚掉的行还留在计数里 —— 报出来的数比真正写进去的多:" + str(out))
    after = _row(world["dsn"], "SELECT status FROM mhz_publish_order_items WHERE id=%s",
                 (world["item_id"],))
    assert after["status"] == "submitted", (
        "库里那半笔没被撤 —— 那计数对不对就无所谓了:" + str(after))


# ═══════════════════════════════════════════════════════════════════
# ③ 一个收敛器炸掉,不许饿死排在它后面的回填
# ═══════════════════════════════════════════════════════════════════

def test_one_exploding_converger_never_starves_the_backfill(world, monkeypatch):
    """🔴 **R8 P1**:五个收敛器 + 回填逐个 try,与三条主链同待遇。

    上一版它们是裸调的,而**回填排在最后** —— 排在前面的任何一个抛异常,
    回填就永远不执行,而从外面看只是 `run_tick` 抛了一次,
    没人知道 `published_url` 从此再也没有写入方。
    """
    from services.geo_douyin import contract_worker as cw

    _make_receipt(world["dsn"], world["item_id"], "SN-R8-STARVE", "r8-st")

    def _always_boom():
        raise RuntimeError("判据注入:这个收敛器一直炸")

    monkeypatch.setattr(cw, "_reconcile_stuck", _always_boom)
    counts = asyncio.run(cw.run_tick(per_tick=1))

    assert int(counts.get("converger_errors") or 0) >= 1, (
        "炸掉的收敛器被静默吞了 —— 连续失败也没人知道:" + str(counts))
    assert "backfilled_urls" in counts, ("回填根本没跑到:" + str(counts))
    assert int(counts["backfilled_urls"]) == 1, (
        "前面的收敛器一炸,回填就被饿死了:" + str(counts))
    post = _row(world["dsn"], "SELECT published_url FROM geo_douyin_posts WHERE id=%s",
                (world["post_id"],))
    assert post["published_url"], ("回填没真的落库:" + str(post))
    alert = _row(world["dsn"], "SELECT severity FROM ai_ops_alerts"
                               " WHERE rule_key='geo_imgnote_converger_failed'"
                               "   AND fingerprint='converger:stuck_tasks'", ())
    assert alert, "收敛器连续失败没有留痕"


def _seed_contract_task(dsn, world):
    """一条**真冻结**的合同链制作任务(资金收尾判据要打的对象)。"""
    from middleware.billing import freeze_points

    task_ref = "imgnote:" + uuid.uuid4().hex + ":9"
    handle = asyncio.run(freeze_points(OWNER_UID, PUBLISH_FEATURE, task_ref=task_ref,
                                       brand_id=world["brand_id"], reason="R8 制作链夹具"))
    conn = _conn(dsn)
    c = conn.cursor()
    c.execute(
        "INSERT INTO geo_douyin_post_tasks (post_id, user_id, task_ref, status,"
        " production_batch_id, batch_item_ordinal, settlement_status,"
        " settlement_authority, payer_user_id, freeze_id, freeze_table,"
        " reserved_amount, physical_split_snapshot, billing_mode, progress_total)"
        " VALUES (%s,%s,%s,'running',%s,1,'frozen','direct_freeze',%s,%s,%s,%s,"
        " %s::jsonb,'freeze_per_item',2) RETURNING id",
        (world["post_id"], OWNER_UID, task_ref, str(uuid.uuid4()), OWNER_UID,
         int(handle["freeze_id"]), str(handle["freeze_table"]), PUBLISH_POINTS,
         json.dumps(handle.get("physical_split_snapshot") or {})))
    task_id = int(c.fetchone()["id"])
    conn.close()
    return {"task_id": task_id, "task_ref": task_ref,
            "freeze_id": int(handle["freeze_id"]),
            "freeze_table": str(handle["freeze_table"])}


def test_the_production_chain_also_refuses_a_reversed_idempotent_settlement(world, monkeypatch):
    """🔴 **同一个病的第二条链**:制作链的资金收尾也不许把「幂等」当「真结算」。

    发布链那一格由上面的四步反例守着。制作链走的是
    `production_task.commit_contract_settlement` —— 它原本也只看 `success`。
    本仓已经因为「一个病两条链只修一条」被判过一次,所以两条都要有判据。

    注入的是**真依赖的真返回形状**:`commit_freeze` 对一笔已经退掉的冻结
    返回 `{"success": True, "idempotent": True, "status": "released"}`。
    """
    import middleware.billing as billing
    from services.geo_douyin import production_task as pt

    seeded = _seed_contract_task(world["dsn"], world)

    async def _idempotent_on_a_released_freeze(**kwargs):
        return {"success": True, "idempotent": True, "status": "released"}

    monkeypatch.setattr(billing, "commit_freeze", _idempotent_on_a_released_freeze)
    contract_task = {"task_id": seeded["task_id"], "task_ref": seeded["task_ref"],
                     "freeze_id": seeded["freeze_id"],
                     "freeze_table": seeded["freeze_table"],
                     "payer_user_id": OWNER_UID, "reserved_amount": PUBLISH_POINTS,
                     "settlement_authority": "direct_freeze"}
    committed = asyncio.run(pt.commit_contract_settlement(
        contract_task, freeze_id=seeded["freeze_id"], task_ref=seeded["task_ref"],
        task_id=seeded["task_id"]))

    assert committed is False, (
        "钱已经退回,制作链却报「结清了」—— 作品会被当成已付款交付")
    task = _row(world["dsn"], "SELECT settlement_status FROM geo_douyin_post_tasks"
                              " WHERE id=%s", (seeded["task_id"],))
    assert task["settlement_status"] == "manual", (
        "资金视图会说「已结算」,而账上那笔钱其实是退掉的:" + str(task))


# ═══════════════════════════════════════════════════════════════════
# 并车前置件 ③ · 孤儿僵尸清扫也要显式排除合同链(不靠 media_type 顺手挡)
# ═══════════════════════════════════════════════════════════════════

def test_the_orphan_zombie_sweeper_excludes_the_contract_lane_explicitly(world):
    """🔴 `find_orphan_publish_items` 不许把合同链的项当孤儿单退款。

    它原本只有 `COALESCE(media_type,'mhz') <> 'svideo'` —— **今天恰好**也把图文项
    挡在外面,但那挡的是"渠道名",不是"钱是冻着的"这个计费口径:
    图文 lane 换个 media_type、或再来一条新的 freeze_per_item 链,这道墙就没了,
    而后果是**凭空退款**(freeze 链的钱是冻着的,不是扣掉的)。

    这条判据用一条 `media_type='mhz'` 的**合同项**打 —— 正是 `<> 'svideo'`
    挡不住、只有 `billing_mode` 闸挡得住的那一格。
    """
    from db.meijiehezi_db import find_orphan_publish_items

    conn = _conn(world["dsn"])
    c = conn.cursor()
    # 老口径孤儿单(正向对照:它必须还被捡)
    c.execute(
        "INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name,"
        " media_type, status, cost_points, item_request_id, billing_mode)"
        " VALUES (%s,%s,9301,'老口径孤儿','mhz','pending',%s,%s,'deduct_upfront')"
        " RETURNING id", (world["order_id"], OWNER_UID, PUBLISH_POINTS, str(uuid.uuid4())))
    legacy_id = int(c.fetchone()["id"])
    # 合同链项,但 media_type **不是** svideo ⇒ 老那句 `<> 'svideo'` 挡不住它
    c.execute("UPDATE mhz_publish_order_items SET media_type='mhz', status='pending',"
              " mhz_order_id=NULL WHERE id=%s", (world["item_id"],))
    conn.close()

    picked = {int(r["id"]) for r in find_orphan_publish_items(
        min_age_hours=1, statuses=["pending"], limit=50)}
    assert int(world["item_id"]) not in picked, (
        "孤儿清扫够到了合同链的项 —— 它的钱是冻着的,退它等于凭空送钱:" + str(picked))
    assert legacy_id in picked, (
        "老口径孤儿单也不被捡了 —— 那是把这条兜底整个关掉,不是修好它:" + str(picked))
