"""WP5 · 一篇一账号 command / 资格 / 日容量 / 状态投影(03 §8)。

含 Review 终审预告 ① 的**构造违例配对判据**:
「已 external-start 卡死 → needs_action 不 release」正反两条。
"""
from __future__ import annotations

import os
import pathlib
import threading
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin.account_eligibility import (  # noqa: E402
    MEDIA_TYPE_SVIDEO, REASON_BLACKLISTED, REASON_FREQUENCY_FULL, REASON_INACTIVE,
    REASON_NOT_DOUYIN, REASON_NOT_FOUND, REASON_NO_IMAGE_NOTE,
    evaluate_accounts, load_today_counts_scoped,
)
from services.geo_douyin.durable_worker import reconcile_stuck_external_tasks  # noqa: E402
from services.geo_douyin.publish_command import (  # noqa: E402
    CMD_ACCEPTED, CMD_CANCELLED, CMD_COMPLETED, CMD_FAILED, CMD_NEEDS_ACTION,
    CMD_PARTIAL_SUCCESS, CMD_PENDING_APPROVAL, CMD_PROCESSING,
    CapacityExceeded, CapacityLockUnsafe, OneToOneViolation,
    aggregate_account_demand, assert_one_to_one, business_capacity_date,
    is_retryable, normalized_availability, project_command_status, reserve_daily_capacity,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")


def _item(**over):
    base = dict(item_request_id=str(uuid.uuid4()), geo_post_id=101,
                post_revision_id="postrev_7", prepared_artifact_id="artifact_9",
                manifest_hash="sha256_h1", media_id=9001,
                expected_price_fingerprint="publish-price-v1:abc")
    base.update(over)
    return base


# ============================================================
# A. 一篇一账号(不连库)
# ============================================================

def test_three_posts_three_mappings_account_may_repeat():
    """03 §8:3 篇形成 3 项账号映射;账号 ID **可以**重复(A4 已裁)。"""
    items = [_item(post_revision_id="r1", media_id=9001),
             _item(post_revision_id="r2", media_id=9001),
             _item(post_revision_id="r3", media_id=9002)]
    assert_one_to_one(items)                       # 不抛
    assert aggregate_account_demand(items) == {9001: 2, 9002: 1}


def test_same_revision_to_two_accounts_rejects_whole_batch():
    """🔴 同一 active post revision → A/B 两账号:**整批**拒绝。

    整批而不是丢掉冲突项 —— 部分接受会让用户以为提交成功,
    而实际发出去的组合不是他确认的那一组。
    """
    items = [_item(post_revision_id="r1", media_id=9001),
             _item(post_revision_id="r1", media_id=9002)]
    with pytest.raises(OneToOneViolation, match="多个账号"):
        assert_one_to_one(items)


def test_media_ids_array_is_rejected():
    """03 §12「one-to-one mapping」反向变异行:「接受 media_ids[]」。"""
    with pytest.raises(OneToOneViolation, match="多个账号"):
        assert_one_to_one([_item(media_id=[9001, 9002])])


def test_duplicate_item_request_id_rejected():
    key = str(uuid.uuid4())
    with pytest.raises(OneToOneViolation, match="重复的 item_request_id"):
        assert_one_to_one([_item(item_request_id=key, post_revision_id="r1"),
                           _item(item_request_id=key, post_revision_id="r2")])


@pytest.mark.parametrize("field", [
    "geo_post_id", "post_revision_id", "prepared_artifact_id",
    "manifest_hash", "media_id", "expected_price_fingerprint",
])
def test_every_identity_field_is_required(field):
    """缺任一身份字段即拒 —— 少一格就意味着"服务端没冻结那一维"。"""
    with pytest.raises(OneToOneViolation, match=field):
        assert_one_to_one([_item(**{field: None})])


def test_same_post_different_revisions_is_allowed():
    """反向对照:判的是 revision 不是 geo_post_id。

    内容修复产生新 revision → 新的有效发布根(§3.7),拿 geo_post_id 判会把它误拒。
    """
    assert_one_to_one([_item(geo_post_id=101, post_revision_id="r1"),
                       _item(geo_post_id=101, post_revision_id="r2")])


def test_empty_batch_rejected():
    with pytest.raises(ValueError, match="EMPTY_BATCH"):
        assert_one_to_one([])


# ============================================================
# B. Command 状态投影(§7.5 四优先级)
# ============================================================

def _att(state, settlement="frozen", started=False, **over):
    row = {"state": state, "settlement_status": settlement,
           "external_started_at": "2026-08-17T10:00:00+08:00" if started else None}
    row.update(over)
    return row


def test_pending_approval_wins_everything():
    assert project_command_status(root_pending_approval=True, coordination_failed=False,
                                  attempts=[_att("published")]) == CMD_PENDING_APPROVAL


def test_coordination_failed_with_no_items_is_failed():
    assert project_command_status(root_pending_approval=False, coordination_failed=True,
                                  attempts=[]) == CMD_FAILED


@pytest.mark.parametrize("attempts", [
    [_att("queued", settlement="quarantined")],
    [_att("needs_action")],
    [_att("published"), _att("queued", settlement="manual")],
])
def test_needs_action_outranks_everything_below(attempts):
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=attempts) == CMD_NEEDS_ACTION


def test_all_queued_and_none_started_is_accepted():
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=[_att("queued"), _att("queued")]) == CMD_ACCEPTED


@pytest.mark.parametrize("attempts", [
    [_att("published", started=True), _att("queued")],
    [_att("failed", settlement="released"), _att("queued")],
    [_att("cancelled"), _att("queued")],
])
def test_mixed_states_all_land_on_processing(attempts):
    """🔴 §7.5 点名的三组混合态:`published+queued` / `failed+queued` / `cancelled+queued`
    **必须**都是 processing,不能落出枚举。"""
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=attempts) == CMD_PROCESSING


def test_started_but_still_queued_is_not_accepted():
    """已经有 external-start 就不再是"还没开始"。

    只看状态名会把"已发出去一条、另一条排队中"误判成 accepted(= 告诉用户还没开始)。
    """
    assert project_command_status(
        root_pending_approval=False, coordination_failed=False,
        attempts=[_att("queued", started=True), _att("queued")]) == CMD_PROCESSING


@pytest.mark.parametrize("attempts,expected", [
    ([_att("published"), _att("published")], CMD_COMPLETED),
    ([_att("published"), _att("failed")], CMD_PARTIAL_SUCCESS),
    ([_att("cancelled"), _att("withdrawn")], CMD_CANCELLED),
    ([_att("failed"), _att("rejected")], CMD_FAILED),
])
def test_terminal_projection(attempts, expected):
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=attempts) == expected


def test_retry_child_moves_command_back_from_terminal():
    """§7.5 末:retry child 建立后,command 从 failed/partial_success 回到 accepted/processing。"""
    terminal = [_att("failed", settlement="released", started=True)]
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=terminal) == CMD_FAILED
    with_child = terminal + [_att("queued")]
    assert project_command_status(root_pending_approval=False, coordination_failed=False,
                                  attempts=with_child) == CMD_PROCESSING


def test_retryable_only_for_explicit_failure_with_released_funds():
    """结果未知**绝不可**盲重投(供应商那边可能已经发了)。"""
    assert is_retryable(_att("failed", settlement="released")) is True
    assert is_retryable(_att("rejected", settlement="released")) is True
    assert is_retryable(_att("failed", settlement="frozen")) is False, "资金还冻着就允许重试 = 可能双冻"
    assert is_retryable(_att("awaiting_sync", settlement="frozen")) is False, "结果未知被判成可重投"
    assert is_retryable(_att("published", settlement="committed")) is False


def test_availability_axis_is_separate_from_delivery_outcome():
    """§7.5:published 之后下架**不覆写** raw published,另给可用性轴。"""
    assert normalized_availability(_att("published")) == "active"
    assert normalized_availability(_att("published", retracted_at="2026-08-18")) == "retracted"
    assert normalized_availability(
        _att("published", retracted_at="2026-08-18", replaced_by_source_id="x")) == "replaced"
    assert normalized_availability(_att("failed")) == "not_published"


# ============================================================
# C. PG16:资格跨 lane / 日容量原子预占 / external-start 收敛
# ============================================================

def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = f"geoimg_test_wp5_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def txn(db):
    """显式事务连接(**不** autocommit)—— 容量预占必须在事务内,见 CapacityLockUnsafe。"""
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    c.execute("DELETE FROM mhz_publish_order_items")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def cur(db):
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("DELETE FROM mhz_publish_order_items")
    c.execute("DELETE FROM mhz_short_video")
    c.execute("DELETE FROM geo_douyin_post_tasks")
    c.execute("DELETE FROM geo_douyin_posts")
    try:
        yield c
    finally:
        conn.close()


def _account(cur, media_id, *, platform="抖音", can_tuwen=1, active=True, blacklist=0):
    cur.execute(
        "INSERT INTO mhz_short_video (id, media_name, platform, is_active, blacklist, can_tuwen) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (media_id, f"号{media_id}", platform, active, blacklist, can_tuwen))


def _order_item(cur, *, media_id, media_type, status="pending", capacity_state=None,
                capacity_date=None):
    cur.execute(
        "INSERT INTO mhz_publish_order_items (user_id, media_id, media_name, media_type, "
        " status, capacity_state, capacity_date) VALUES (42,%s,'x',%s,%s,%s,%s)",
        (media_id, media_type, status, capacity_state, capacity_date))


def test_today_counts_do_not_cross_lanes(cur):
    """🔴 规格 §6.1 点名的缺陷:现役 load_today_counts **没有** media_type 条件。

    造一个跨 lane 同 ID 的场景:media_id=9001 在 article lane 有 2 单、svideo lane 有 1 单。
    按 lane 限定后必须只数到 1;数到 3 就是串数(明明没发满却判"今天已满")。
    """
    _order_item(cur, media_id=9001, media_type="article")
    _order_item(cur, media_id=9001, media_type="article")
    _order_item(cur, media_id=9001, media_type=MEDIA_TYPE_SVIDEO)

    counts = load_today_counts_scoped(cur, [9001], media_type=MEDIA_TYPE_SVIDEO)
    assert counts.get(9001) == 1, f"跨 lane 串数了:{counts}"
    # 反向对照:另一个 lane 数得到它自己的 2 单,证明数据确实存在(不是查询恒空)
    assert load_today_counts_scoped(cur, [9001], media_type="article").get(9001) == 2


def test_null_status_rows_are_counted(cur):
    """`NOT IN (...)` 对 NULL 恒 UNKNOWN → 整行漏掉 → **少数 → 超发**。

    现役写法就是 `NOT IN`;本模块改成 `status IS NULL OR status <> ALL(...)`。
    """
    _order_item(cur, media_id=9002, media_type=MEDIA_TYPE_SVIDEO, status=None)
    assert load_today_counts_scoped(cur, [9002], media_type=MEDIA_TYPE_SVIDEO).get(9002) == 1, \
        "status 为 NULL 的占用被漏掉了 —— 会超发"


def test_non_consuming_statuses_do_not_consume(cur):
    """反向对照:没发出去的终态不占额度,否则一次失败会惩罚用户一整天。"""
    for st in ("cancelled", "withdrawn", "failed", "rejected"):
        _order_item(cur, media_id=9003, media_type=MEDIA_TYPE_SVIDEO, status=st)
    assert load_today_counts_scoped(cur, [9003], media_type=MEDIA_TYPE_SVIDEO).get(9003) is None


@pytest.mark.parametrize("kwargs,expected_reason", [
    (dict(active=False), REASON_INACTIVE),
    (dict(blacklist=1), REASON_BLACKLISTED),
    (dict(platform="小红书"), REASON_NOT_DOUYIN),
    (dict(can_tuwen=0), REASON_NO_IMAGE_NOTE),
])
def test_eligibility_reason_codes(cur, kwargs, expected_reason):
    _account(cur, 9100, **kwargs)
    result = evaluate_accounts(cur, [9100], daily_limit=3)[9100]
    assert result.available is False and result.reason_code == expected_reason


def test_unknown_account_is_not_found_not_available(cur):
    result = evaluate_accounts(cur, [999999], daily_limit=3)[999999]
    assert result.available is False and result.reason_code == REASON_NOT_FOUND


def test_eligible_account_is_available_with_remaining(cur):
    """反向对照:全部条件满足时必须可用,否则上面四条"能拒"可能是"什么都拒"。"""
    _account(cur, 9101)
    result = evaluate_accounts(cur, [9101], daily_limit=3)[9101]
    assert result.available is True and result.today_remaining == 3
    assert result.capabilities["image_note"] is True


def test_frequency_full_is_reported_after_identity_checks(cur):
    """判定顺序:先身份后频控。不能发图文的号不该显示成"今天满了"(会让人一直等)。"""
    _account(cur, 9102, can_tuwen=0)
    for _ in range(5):
        _order_item(cur, media_id=9102, media_type=MEDIA_TYPE_SVIDEO)
    assert evaluate_accounts(cur, [9102], daily_limit=3)[9102].reason_code == REASON_NO_IMAGE_NOTE

    _account(cur, 9103)
    for _ in range(3):
        _order_item(cur, media_id=9103, media_type=MEDIA_TYPE_SVIDEO)
    assert evaluate_accounts(cur, [9103], daily_limit=3)[9103].reason_code == REASON_FREQUENCY_FULL


# ---- 日容量原子预占 ----

def test_capacity_reservation_refuses_autocommit(cur):
    """🔴 "锁了等于没锁" 的硬闸。

    `pg_advisory_xact_lock` 是事务级锁:autocommit 下取锁那条语句自己就是一个事务,
    语句结束锁立刻释放 ⇒ 取锁/读用量/判断三步之间零互斥 ⇒ 并发双双通过 ⇒ 超发。
    第一版没有这道闸,容量用例在 autocommit fixture 下"通过"了 ——
    而通过的原因正是锁没起作用。静默失效比报错危险得多。
    """
    today = business_capacity_date(cur)
    with pytest.raises(CapacityLockUnsafe, match="显式事务"):
        reserve_daily_capacity(cur, media_id=9199, needed=1, daily_limit=2, capacity_date=today)


def test_capacity_counts_legacy_rows_too(txn):
    """双写期:老单(capacity_state 为 NULL)也是真实占用,漏掉就会超发。"""
    c = txn.cursor()
    today = business_capacity_date(c)
    _order_item(c, media_id=9200, media_type=MEDIA_TYPE_SVIDEO, status="pending")  # legacy
    # 余量 = 2 - 1(老单)= 1;要 1 → 放行,剩 0
    assert reserve_daily_capacity(c, media_id=9200, needed=1, daily_limit=2,
                                  capacity_date=today) == 0, "老单没被算进占用"
    # 要 2 → 拒(证明老单确实吃掉了一格)
    with pytest.raises(CapacityExceeded):
        reserve_daily_capacity(c, media_id=9200, needed=2, daily_limit=2, capacity_date=today)
    txn.rollback()


def test_legacy_usage_is_timezone_correct_across_midnight(txn):
    """🔴 定点回归(2026-08-18 被时钟跨上海午夜顶红的那条)。

    `created_at` 是 timestamp without time zone —— 它存的是哪个墙钟**取决于 DB 时区**:
    生产 omnirank-db 是 Asia/Shanghai,本机夹具是 Etc/UTC。第一版直接拿它跟
    "上海业务日"的字符串边界比,生产碰巧对、UTC 环境下少数 → 超发。

    这条在**任何** DB 时区下都必须成立:刚插入的行,按当前业务日一定算作占用。
    它同时是"不猜机器本地"这条纪律的可执行形式。
    """
    c = txn.cursor()
    c.execute("SHOW timezone")
    db_tz = str(dict(c.fetchone())["TimeZone"])
    today = business_capacity_date(c)
    _order_item(c, media_id=9250, media_type=MEDIA_TYPE_SVIDEO, status="pending")
    # 刚插的这一行必须被算作今天的占用 —— 与 DB 时区是否等于业务时区无关
    with pytest.raises(CapacityExceeded):
        reserve_daily_capacity(c, media_id=9250, needed=1, daily_limit=1, capacity_date=today)
    # 反向对照:限额放宽到 2 就该放行,证明上面的拒绝不是"永远拒绝"
    assert reserve_daily_capacity(c, media_id=9250, needed=1, daily_limit=2,
                                  capacity_date=today) == 0
    # 把 DB 时区与业务时区是否相等**显式记进断言消息**,便于将来换环境时定位
    assert today, f"business date 取不到(db_tz={db_tz})"
    txn.rollback()


def test_capacity_rejects_overbooking(txn):
    c = txn.cursor()
    today = business_capacity_date(c)
    _order_item(c, media_id=9201, media_type=MEDIA_TYPE_SVIDEO,
                capacity_state="reserved", capacity_date=today)
    # 余量 1,要 2 → 拒
    with pytest.raises(CapacityExceeded, match="今天只剩"):
        reserve_daily_capacity(c, media_id=9201, needed=2, daily_limit=2, capacity_date=today)
    # 反向对照:要 1 → 放行
    assert reserve_daily_capacity(c, media_id=9201, needed=1, daily_limit=2,
                                  capacity_date=today) == 0
    txn.rollback()


def test_two_concurrent_batches_never_exceed_today_remaining(db):
    """🔴 03 §8:两 post → 同账号且 today_remaining=1 时,两 command 真并发**不得**超额。"""
    today_holder = {}
    conn0 = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn0.autocommit = True
    c0 = conn0.cursor()
    c0.execute("DELETE FROM mhz_publish_order_items")
    today_holder["d"] = business_capacity_date(c0)
    conn0.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
        try:
            cur_ = conn.cursor()
            barrier.wait(timeout=30)
            try:
                reserve_daily_capacity(cur_, media_id=9300, needed=1, daily_limit=1,
                                       capacity_date=today_holder["d"])
                # 预占成功 → 真插一行占住(与真实协调器同事务语义)
                cur_.execute(
                    "INSERT INTO mhz_publish_order_items (user_id, media_id, media_name, "
                    " media_type, status, capacity_state, capacity_date) "
                    "VALUES (42,9300,'x',%s,'pending','reserved',%s)",
                    (MEDIA_TYPE_SVIDEO, today_holder["d"]))
                conn.commit()
                result = "reserved"
            except CapacityExceeded:
                conn.rollback()
                result = "rejected"
        finally:
            conn.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(outcomes) == 2, f"并发夹具没跑满:{outcomes}"
    assert outcomes.count("reserved") == 1, f"余量 1 却预占了 {outcomes.count('reserved')} 次"
    assert outcomes.count("rejected") == 1


# ---- Review 终审预告 ①:构造违例配对 ----

def _stuck_task(cur, *, external: bool) -> int:
    cur.execute(
        "INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status) "
        "VALUES (36,99,'k','image_post','draft') RETURNING id")
    post_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO geo_douyin_post_tasks (post_id, user_id, task_ref, status) "
        "VALUES (%s,99,%s,'running') RETURNING id", (post_id, f"r_{uuid.uuid4().hex[:10]}"))
    task_id = int(cur.fetchone()["id"])
    cur.execute("UPDATE geo_douyin_post_tasks SET lease_owner='dead', "
                "lease_expires_at = now() - interval '2 hours' WHERE id=%s", (task_id,))
    if external:
        cur.execute("UPDATE geo_douyin_post_tasks SET external_started_at = now() - interval '3 hours' "
                    "WHERE id=%s", (task_id,))
    return task_id


def test_stuck_external_start_converges_to_manual_never_released(cur):
    """🔴 Review 终审预告 ① 的**正向**:已 external-start 卡死 → needs_action + manual。"""
    task_id = _stuck_task(cur, external=True)
    assert task_id in reconcile_stuck_external_tasks(cur, grace_seconds=0)
    cur.execute("SELECT status, settlement_status FROM geo_douyin_post_tasks WHERE id=%s", (task_id,))
    row = cur.fetchone()
    assert row["status"] == "needs_action"
    assert row["settlement_status"] == "manual"
    assert row["settlement_status"] != "released", (
        "结果未知却 release —— release 意味着『确定没发生』,而我们恰恰不确定"
    )


def test_never_started_task_is_not_swept_into_manual(cur):
    """🔴 Review 终审预告 ① 的**配对违例**:**没有** external-start 的卡死任务
    绝不能被这条 reconciler 扫成 needs_action/manual。

    它属于"可安全重跑"那一类,应该留给 claim_next_task 重领。
    被扫进 manual = 明明能自动恢复的任务被打成需人工,用户白等。
    """
    task_id = _stuck_task(cur, external=False)
    swept = reconcile_stuck_external_tasks(cur, grace_seconds=0)
    assert task_id not in swept, "未外调的任务被误扫进人工队列"
    cur.execute("SELECT status, settlement_status FROM geo_douyin_post_tasks WHERE id=%s", (task_id,))
    row = cur.fetchone()
    assert row["status"] == "running", "状态被改了"
    assert row["settlement_status"] is None


def test_grace_window_protects_a_just_expired_lease(cur):
    """配对违例②:租约刚过期那一刻可能只是心跳抖动,grace 内不许判死。"""
    task_id = _stuck_task(cur, external=True)
    assert reconcile_stuck_external_tasks(cur, grace_seconds=86400) == [], \
        "grace 窗口内就把任务判死了"
    # 反向对照:grace 归零后必须扫到,证明上面的空集不是"永远扫不到"
    assert task_id in reconcile_stuck_external_tasks(cur, grace_seconds=0)
