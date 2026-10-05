"""T3 判别锁 —— 通知系统(工单 §3.5)。

7 条锁,全部行为级(真调 db.team_db 的 feed 函数 / 真调 monitoring_db.update_task_status,
真读真库),不做源码字符串断言:
  1. 列表项点击 → 弹窗正文**完整**(与库中原文逐字一致)
  2. 弹窗内跳转可用,且跳到正确目标页
  3. 点击后未读消失,**刷新后仍为已读**
  4. 两类通知分别可筛选,未读数各自正确
  5. 历史页可翻页,能取到超出首屏的旧通知
  6. 同一业务单号的终态通知**不并列展示两条矛盾状态**
  7. 扣费通知渲染路径**不触发任何扣费/退费写操作**

锁 1/2 的"弹窗"部分:后端负责的是"完整正文 + 正确 link 出得来";弹窗本身
是否截断由前端 `NotificationDetailDialog` 的 whitespace-pre-wrap 承担,
本文件用 `test_lock1_*` 断言后端**不截断**,前端渲染契约另有 DOM 级断言在
tests/portal_notify_2026_07_29/test_t3_frontend_contract.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db import monitoring_db, team_db

LONG_CONTENT = (
    "业务单号 MONITOR-1507；最终状态：监测已完成；时间：2026-07-29T11:37:24。"
    "请在效果监测页面查看结果。本段刻意写得很长,用来验证正文不会在任何一层被截断——"
    "旧实现在列表里用 line-clamp-3 砍掉了后半段,Owner 截图里看不到的就是这里。"
)


def _mk_notification(db, user_id, *, title, content, link, level="gentle", event_key=None, minutes_ago=0):
    db.execute(
        """INSERT INTO user_notifications (user_id, type, title, content, link, level, event_key, created_at)
           VALUES (%s, 'system', %s, %s, %s, %s, %s, %s) RETURNING id""",
        (user_id, title, content, link, level, event_key,
         datetime.now() - timedelta(minutes=minutes_ago)),
    )
    return db.fetchone()["id"]


def _mk_tx(db, user_id, *, tx_type="consume", amount=-650, balance=1000, desc="监测扣费"):
    db.execute(
        """INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, description)
           VALUES (%s, %s, 'paid', %s, %s, %s) RETURNING id""",
        (user_id, tx_type, amount, balance, desc),
    )
    return db.fetchone()["id"]


@pytest.fixture
def user(db):
    db.execute("INSERT INTO users (username) VALUES ('agent-notify') RETURNING id")
    return db.fetchone()["id"]


# ---------------------------------------------------------------------- 锁 1
def test_lock1_feed_content_is_verbatim_and_untruncated(db, user):
    """锁 1:feed 返回的正文与库中原文**逐字一致**,一个字符都不许少。"""
    _mk_notification(db, user, title="效果监测已完成", content=LONG_CONTENT, link="/monitoring")

    page = team_db.get_notification_feed(user, category="system", limit=10)

    assert len(page["items"]) == 1
    assert page["items"][0]["content"] == LONG_CONTENT


# ---------------------------------------------------------------------- 锁 2
def test_lock2_feed_carries_correct_jump_target(db, user):
    """锁 2:每条都带正确的跳转目标(业务状态→业务页 / 扣费→消费明细)。"""
    _mk_notification(db, user, title="效果监测已完成", content="x", link="/monitoring")
    _mk_tx(db, user)

    page = team_db.get_notification_feed(user, category="all", limit=10)
    by_cat = {item["category"]: item for item in page["items"]}

    assert by_cat["system"]["link"] == "/monitoring"
    assert by_cat["billing"]["link"] == "/wallet"


# ---------------------------------------------------------------------- 锁 3
def test_lock3_read_state_persists_across_reload(db, user):
    """锁 3:标已读后重新拉 feed(= 刷新页面)仍是已读,不回退。"""
    nid = _mk_notification(db, user, title="报告已生成", content="x", link="/reports")
    tx_id = _mk_tx(db, user)

    assert team_db.mark_feed_item_read(user, f"sys:{nid}") is True
    assert team_db.mark_feed_item_read(user, f"bill:{tx_id}") is True

    reloaded = team_db.get_notification_feed(user, category="all", limit=10)
    assert all(item["is_read"] for item in reloaded["items"]), reloaded["items"]
    counts = team_db.get_notification_unread_counts(user)
    assert counts["system"] == 0 and counts["billing"] == 0


# ---------------------------------------------------------------------- 锁 4
def test_lock4_two_categories_filter_and_count_independently(db, user):
    """锁 4:两类可分别筛选,未读数各自正确。"""
    _mk_notification(db, user, title="效果监测已完成", content="a", link="/monitoring")
    _mk_notification(db, user, title="报告已生成", content="b", link="/reports")
    _mk_tx(db, user)
    _mk_tx(db, user, tx_type="refund", amount=650)
    _mk_tx(db, user, tx_type="release", amount=100)

    system_page = team_db.get_notification_feed(user, category="system", limit=50)
    billing_page = team_db.get_notification_feed(user, category="billing", limit=50)
    counts = team_db.get_notification_unread_counts(user)

    assert {item["category"] for item in system_page["items"]} == {"system"}
    assert {item["category"] for item in billing_page["items"]} == {"billing"}
    assert system_page["total"] == 2
    assert billing_page["total"] == 3
    assert counts["system"] == 2
    assert counts["billing"] == 3
    assert counts["total"] == 5


def test_lock4b_marking_one_category_read_leaves_the_other_alone(db, user):
    _mk_notification(db, user, title="效果监测已完成", content="a", link="/monitoring")
    _mk_tx(db, user)

    team_db.mark_feed_all_read(user, category="billing")

    counts = team_db.get_notification_unread_counts(user)
    assert counts["billing"] == 0
    assert counts["system"] == 1, "只清扣费类不许把系统消息也标已读"


# ---------------------------------------------------------------------- 锁 5
def test_lock5_history_pagination_reaches_older_items(db, user):
    """锁 5:历史页能翻到首屏之外的旧通知(不是只保留最近 N 条)。"""
    for i in range(25):
        _mk_notification(
            db, user, title=f"通知 {i}", content=f"正文 {i}", link="/monitoring",
            minutes_ago=i,
        )

    first = team_db.get_notification_feed(user, category="system", limit=10, offset=0)
    second = team_db.get_notification_feed(user, category="system", limit=10, offset=10)
    third = team_db.get_notification_feed(user, category="system", limit=10, offset=20)

    assert first["total"] == 25
    assert first["has_more"] is True
    assert len(second["items"]) == 10
    assert len(third["items"]) == 5
    ids = [i["id"] for i in first["items"] + second["items"] + third["items"]]
    assert len(set(ids)) == 25, "翻页不许重复/漏条"
    assert "通知 24" in {i["title"] for i in third["items"]}


# ---------------------------------------------------------------------- 锁 6
def test_lock6_conflicting_terminal_states_are_not_shown_side_by_side(db, user):
    """锁 6:同一业务单号 MONITOR-1507 的两条矛盾终态,只展示最后一条。"""
    _mk_notification(
        db, user, title="效果监测已完成", content="11:37:24", link="/monitoring",
        event_key="monitoring.completed:1507:completed:%d" % user, minutes_ago=10,
    )
    _mk_notification(
        db, user, title="效果监测未完成", content="11:37:34", link="/monitoring",
        event_key="monitoring.failed:1507:failed:%d" % user, minutes_ago=9,
    )

    page = team_db.get_notification_feed(user, category="system", limit=10)

    titles = [item["title"] for item in page["items"]]
    assert titles == ["效果监测未完成"], titles
    assert page["total"] == 1


def test_lock6b_different_business_numbers_are_not_collapsed(db, user):
    """反向:不同业务单号不许被误合并(否则就是"把症状盖住"了)。"""
    _mk_notification(
        db, user, title="效果监测已完成 A", content="a", link="/monitoring",
        event_key="monitoring.completed:1507:completed:%d" % user, minutes_ago=10,
    )
    _mk_notification(
        db, user, title="效果监测已完成 B", content="b", link="/monitoring",
        event_key="monitoring.completed:1508:completed:%d" % user, minutes_ago=9,
    )
    page = team_db.get_notification_feed(user, category="system", limit=10)
    assert page["total"] == 2


def test_lock6c_refund_notification_is_never_superseded(db, user):
    """资金类事实不许被后到的终态盖掉 —— 退款通知永远看得见。"""
    _mk_notification(
        db, user, title="发布费用已退回", content="退款", link="/publish",
        event_key="publication.refunded:77:refunded:%d" % user, minutes_ago=10,
    )
    _mk_notification(
        db, user, title="发布任务已完成", content="完成", link="/publish",
        event_key="publication.completed:77:completed:%d" % user, minutes_ago=9,
    )
    page = team_db.get_notification_feed(user, category="system", limit=10)
    titles = {item["title"] for item in page["items"]}
    assert "发布费用已退回" in titles, "退款通知被覆盖了 —— 这是把资金事实藏起来"


# ---------------------------------------------------------------------- 锁 7
def test_lock7_billing_render_path_performs_zero_fund_writes(db, user):
    """锁 7:扣费通知从拉取到标已读,对资金表**零写入**(真调 handler 后逐表核对)。"""
    tx_id = _mk_tx(db, user)
    db.execute("SELECT id, type, amount, balance_after FROM point_transactions ORDER BY id")
    before = [dict(r) for r in db.fetchall()]

    team_db.get_notification_feed(user, category="billing", limit=10)
    team_db.get_notification_unread_counts(user)
    team_db.mark_feed_item_read(user, f"bill:{tx_id}")
    team_db.mark_feed_all_read(user, category="billing")

    db.execute("SELECT id, type, amount, balance_after FROM point_transactions ORDER BY id")
    after = [dict(r) for r in db.fetchall()]
    assert after == before, "扣费通知路径改动了资金流水"
    db.execute("SELECT COUNT(*) AS c FROM point_transactions")
    assert db.fetchone()["c"] == 1, "扣费通知路径新增了资金流水行"


# --------------------------------- §3.4 根因锁:终态 completed 不许被降级成 failed
def test_root_cause_completed_task_is_not_demoted_by_post_terminal_error(db):
    """真因锁:监测已到 completed 之后,附属段异常不许把它改写成 failed。

    真调 `api.monitoring_api._run_monitoring_stream` 那段的等效路径不现实
    (它是 SSE 生成器 + 真跑引擎),所以这里锁的是**可被单独验证的不变量**:
    终态判定发生在 `_sse_completed` 之后就不许再写 failed。
    做法:用真 update_task_status 把任务打到 completed,再模拟附属段异常路径
    (旧代码会无条件 update_task_status(task_id,'failed')),断言两条矛盾终态
    通知不会并列出现在 feed 里 —— 这是用户实际看到的那一层。
    """
    db.execute("INSERT INTO users (username) VALUES ('agent-root') RETURNING id")
    user_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO brands (name, owner_user_id) VALUES ('根因品牌', %s) RETURNING id",
        (user_id,),
    )
    brand_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO quotes (brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, 'paid', NOW(), CURRENT_DATE, 180) RETURNING id",
        (brand_id,),
    )
    quote_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (quote_id, str(quote_id), brand_id),
    )
    task_id = db.fetchone()["id"]

    monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 50})
    # 旧 bug 的复现动作:终态之后再写一次 failed
    monitoring_db.update_task_status(task_id, "failed")

    # outbox → user_notifications 派发(真调派发器)
    from services import notification_outbox
    notification_outbox.dispatch_notification_outbox()

    page = team_db.get_notification_feed(user_id, category="system", limit=20)
    monitoring_titles = [i["title"] for i in page["items"] if "效果监测" in i["title"]]
    assert len(monitoring_titles) == 1, (
        f"同一业务单号并列展示了矛盾终态: {monitoring_titles}"
    )


def test_root_cause_db_level_guard_refuses_completed_to_failed(db):
    """根因锁(库级):已 completed 的监测任务不许被写成 failed。

    这是 MONITOR-1507 的写入侧不变量。真调 update_task_status 两次,
    直接看 monitoring_tasks.status 与 outbox 里发了几条终态通知。
    """
    db.execute("INSERT INTO users (username) VALUES ('agent-guard') RETURNING id")
    user_id = db.fetchone()["id"]
    db.execute("INSERT INTO brands (name, owner_user_id) VALUES ('守卫品牌', %s) RETURNING id", (user_id,))
    brand_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO quotes (brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, 'paid', NOW(), CURRENT_DATE, 180) RETURNING id", (brand_id,),
    )
    quote_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (quote_id, str(quote_id), brand_id),
    )
    task_id = db.fetchone()["id"]

    monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 50})
    demoted = monitoring_db.update_task_status(task_id, "failed")

    assert demoted is False, "已完成任务被降级成 failed"
    db.execute("SELECT status FROM monitoring_tasks WHERE id=%s", (task_id,))
    assert db.fetchone()["status"] == "completed"
    db.execute("SELECT event_type FROM notification_outbox ORDER BY id")
    events = [r["event_type"] for r in db.fetchall()]
    assert events == ["monitoring.completed"], events


def test_guard_interception_leaves_a_trace(db, caplog):
    """🔴 守卫拦截必须留痕(Deploy-CTO 部署前小修 · Review 2026-07-29 要求)。

    本包修的就是静默失败。守卫默默吞掉一次 completed→failed,等于用一个新的静默
    失败替换旧的 —— 而且更难查,因为它长得像"正常工作"。

    **真捕获日志**(caplog),不是断言源码里有 logger —— 后者换皮即绕。
    四要素逐个断:task_id / 尝试写入的状态 / 当前实际状态 / 调用点。
    """
    import logging

    db.execute("INSERT INTO users (username) VALUES ('agent-guard-log') RETURNING id")
    user_id = db.fetchone()["id"]
    db.execute("INSERT INTO brands (name, owner_user_id) VALUES ('留痕品牌', %s) RETURNING id", (user_id,))
    brand_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO quotes (brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, 'paid', NOW(), CURRENT_DATE, 180) RETURNING id", (brand_id,),
    )
    quote_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (quote_id, str(quote_id), brand_id),
    )
    task_id = db.fetchone()["id"]

    monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 50})

    with caplog.at_level(logging.WARNING, logger="GEO-Monitoring"):
        demoted = monitoring_db.update_task_status(task_id, "failed")

    # ① 状态没被改动
    assert demoted is False
    db.execute("SELECT status FROM monitoring_tasks WHERE id=%s", (task_id,))
    assert db.fetchone()["status"] == "completed"

    # ② 日志里出现该 task_id 的拦截记录
    # 认**带方括号的标签** `[终态守卫拦截]`,不是裸字符串 —— 裸串会被别处的否定说法误命中
    # (第一版就踩了:另一条日志写"(不是终态守卫拦截)",裸串匹配照样命中)。
    hits = [r.getMessage() for r in caplog.records if "[终态守卫拦截]" in r.getMessage()]
    assert hits, f"守卫拦截没有留痕(捕获到的 WARNING: {[r.getMessage() for r in caplog.records]})"
    msg = hits[0]
    assert f"task_id={task_id}" in msg, msg
    assert "尝试写入状态=failed" in msg, msg
    assert "当前实际状态=completed" in msg, msg
    assert "调用点=" in msg and "<unknown>" not in msg, msg


def test_guard_log_does_not_mislabel_a_missing_task(db, caplog):
    """配对锁:task_id 不存在时,UPDATE 同样不命中 —— 但**不许**记成"终态守卫拦截"。

    不分清就把"根本没这行"记成"被守卫拦下",是在制造假线索;
    真出事时会顺着错误方向查半天。
    """
    import logging

    db.execute("SELECT COALESCE(MAX(id),0)+9999 AS ghost FROM monitoring_tasks")
    ghost_id = db.fetchone()["ghost"]

    with caplog.at_level(logging.WARNING, logger="GEO-Monitoring"):
        assert monitoring_db.update_task_status(ghost_id, "failed") is False

    msgs = [r.getMessage() for r in caplog.records if f"task_id={ghost_id}" in r.getMessage()]
    assert msgs, "不存在的 task_id 也应留痕(只是不能记成守卫拦截)"
    # 连"终态守卫拦截"这几个字都不许出现 —— 运维 grep 日志时会误命中,
    # 把"任务不存在"当成"被守卫拦下"顺着错方向查(第一版我在这条里写了
    # "(不是终态守卫拦截)",结果自己的 grep 就命中了自己)。
    assert not any("终态守卫拦截" in m for m in msgs), f"该分支不许出现拦截字样: {msgs}"


def test_failed_to_completed_retry_still_allowed(db):
    """反向不许拦:先失败后重试成功,状态要能改回 completed。"""
    db.execute("INSERT INTO users (username) VALUES ('agent-retry') RETURNING id")
    user_id = db.fetchone()["id"]
    db.execute("INSERT INTO brands (name, owner_user_id) VALUES ('重试品牌', %s) RETURNING id", (user_id,))
    brand_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO quotes (brand_id, status, paid_at, service_start_date, service_days) "
        "VALUES (%s, 'paid', NOW(), CURRENT_DATE, 180) RETURNING id", (brand_id,),
    )
    quote_id = db.fetchone()["id"]
    db.execute(
        "INSERT INTO monitoring_tasks (quote_id, client_id, brand_id, status, total_tests, completed_tests, trigger_type) "
        "VALUES (%s, %s, %s, 'running', 4, 4, 'manual') RETURNING id",
        (quote_id, str(quote_id), brand_id),
    )
    task_id = db.fetchone()["id"]

    assert monitoring_db.update_task_status(task_id, "failed") is True
    assert monitoring_db.update_task_status(task_id, "completed", 4, {"detection_rate": 10}) is True
    db.execute("SELECT status FROM monitoring_tasks WHERE id=%s", (task_id,))
    assert db.fetchone()["status"] == "completed"
