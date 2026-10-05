"""[WO-PUB-ZOMBIE-2026-08-04] 代发僵尸 pending 永久锁死 + 资金滞留 —— 逐格锁。

工单每一件都给了「必须命中 / 必须不命中」对照表,本文件按那张表逐格落断言。
反向格(必须不命中)和正向格同等重要:只锁正向的话,"全都拦" / "全都退" 这种
把功能做死的实现照样能全绿。
"""
import functools
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import item_row, make_item, make_order, make_synced  # noqa: F401  (同目录 conftest)

REPO = Path(__file__).resolve().parents[2]


def _strip_py_noise(text: str) -> str:
    """先剥 docstring 和 # 注释再做源码断言。

    不剥的话,"注释里解释了这段逻辑" 就能让断言通过 —— 把真代码删光照样绿。

    🔴 用 ast 精确定位 docstring,**不能**拿 `\"\"\"[\\s\\S]*?\"\"\"` 一把梭:
    仓里的 SQL 全写在三引号里,一把梭会把要断言的 SQL 连同注释一起剥掉,
    于是"SQL 里必须有这条 WHERE"的断言变成恒假 —— 恒假和恒真一样废。
    """
    import ast

    lines = text.splitlines()
    drop: set[int] = set()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                drop.add(ln)
    kept = [("" if i + 1 in drop else ln) for i, ln in enumerate(lines)]
    out = "\n".join(kept)
    # 行内 # 注释。SQL 注释是 `--` 不是 `#`,不会误伤要断言的 SQL。
    return re.sub(r"#.*", "", out)


# ============================================================
# R1b · 三分支纯判定
# ============================================================
from services.publish_orphan_settlement import (  # noqa: E402
    ACTION_MANUAL_REVIEW, ACTION_REFUND, SWEEPABLE_STATUSES,
    decide_orphan_action, mirror_is_fresh, sync_config_key_for,
)


def test_r1b_external_not_found_refunds():
    """必须命中:外部镜像查不到 → 退款。"""
    assert decide_orphan_action([]) == ACTION_REFUND
    assert decide_orphan_action(None) == ACTION_REFUND


def test_r1b_external_found_but_claimed_refunds():
    """必须命中:查到但已被别的本地条目认领(386 型本地重复下单)→ 退款。"""
    rows = [{"order_sn": "112026073017505510910457795", "claimed_by": 432}]
    assert decide_orphan_action(rows) == ACTION_REFUND


def test_r1b_external_found_unclaimed_must_not_refund():
    """🔴 必须不命中:查到且**未被认领** → 禁止自动退款,转人工。

    这是"对方收了、我们丢了回执"那种,自动退款 = 既退钱又发稿。
    """
    rows = [{"order_sn": "112026080300000000000000001", "claimed_by": None}]
    assert decide_orphan_action(rows) == ACTION_MANUAL_REVIEW


def test_r1b_any_unclaimed_row_wins():
    """混合时以**未认领**那行为准 —— 不许被已认领的行盖过去。"""
    rows = [
        {"order_sn": "A", "claimed_by": 100},
        {"order_sn": "B", "claimed_by": None},
        {"order_sn": "C", "claimed_by": 101},
    ]
    assert decide_orphan_action(rows) == ACTION_MANUAL_REVIEW


# ============================================================
# R1b · 镜像新鲜度闸(fail-closed)
# ============================================================

def test_freshness_gate_cells():
    now = datetime(2026, 8, 4, 12, 0, 0)
    fresh = (now - timedelta(minutes=5)).isoformat()
    stale = (now - timedelta(minutes=90)).isoformat()

    # 必须命中:新鲜 → 放行
    assert mirror_is_fresh(fresh, max_age_minutes=60, now=now) is True
    # 必须不命中:超龄 → 拦(整轮跳过,一分不退)
    assert mirror_is_fresh(stale, max_age_minutes=60, now=now) is False
    # 必须不命中:时间戳缺失 → 拦(fail-closed,不能当成新鲜)
    assert mirror_is_fresh(None, max_age_minutes=60, now=now) is False
    assert mirror_is_fresh("", max_age_minutes=60, now=now) is False
    # 必须不命中:解析不了 → 拦
    assert mirror_is_fresh("昨天下午", max_age_minutes=60, now=now) is False
    # 必须不命中:时间戳在未来(时钟/写入有问题)→ 拦
    future = (now + timedelta(minutes=30)).isoformat()
    assert mirror_is_fresh(future, max_age_minutes=60, now=now) is False
    # 边界:正好卡在阈值上仍算新鲜
    edge = (now - timedelta(minutes=60)).isoformat()
    assert mirror_is_fresh(edge, max_age_minutes=60, now=now) is True


def test_sync_key_per_channel():
    """软文和自媒体各查各的同步时间戳 —— 一个通道挂了不该冻住另一个。"""
    assert sync_config_key_for("mhz") == "last_status_sync"
    assert sync_config_key_for("") == "last_status_sync"
    assert sync_config_key_for(None) == "last_status_sync"
    assert sync_config_key_for("wemedia") == "last_toutiao_status_sync"
    # 必须不命中:自媒体不能回落到软文那条
    assert sync_config_key_for("wemedia") != "last_status_sync"


# ============================================================
# R1b · 判别力自检(真库)
# ============================================================

def test_discriminating_check_true_when_mirror_complete(db):
    from services.publish_orphan_settlement import mirror_lookup_is_discriminating

    make_order(db, order_id=1, user_id=9, article_id=100, title="T1")
    make_item(db, item_id=1, order_id=1, user_id=9, media_id=500,
              media_name="博客园", status="published", mhz_order_id="SN-1")
    make_synced(db, sid="s1", order_sn="SN-1", title="T1", media_name="博客园", resource_id=500)
    assert mirror_lookup_is_discriminating(sample_size=6) is True


def test_discriminating_check_false_when_mirror_incomplete(db):
    """必须不命中:已知有单号的条目在镜像里查不到 → 镜像不全,整轮作废。"""
    from services.publish_orphan_settlement import mirror_lookup_is_discriminating

    make_order(db, order_id=1, user_id=9, article_id=100, title="T1")
    make_item(db, item_id=1, order_id=1, user_id=9, media_id=500,
              media_name="博客园", status="published", mhz_order_id="SN-MISSING")
    assert mirror_lookup_is_discriminating(sample_size=6) is False


def test_discriminating_check_false_with_no_sample(db):
    """没有任何带单号的条目 → 无从证明查法有效,同样判否(不能默认放行)。"""
    from services.publish_orphan_settlement import mirror_lookup_is_discriminating

    assert mirror_lookup_is_discriminating(sample_size=6) is False


# ============================================================
# R2 · 候选查询(红线:有单号的一条都不许进来)
# ============================================================

def _seed_candidates(db):
    make_order(db, order_id=10, user_id=24, article_id=200, title="老僵尸", age_hours=100)
    make_order(db, order_id=11, user_id=24, article_id=201, title="有单号在跑", age_hours=100)
    make_order(db, order_id=12, user_id=24, article_id=202, title="还没到阈值", age_hours=0.5)
    make_order(db, order_id=13, user_id=24, article_id=203, title="已终态", age_hours=100)
    make_order(db, order_id=14, user_id=24, article_id=204, title="短视频", age_hours=100)
    make_item(db, item_id=100, order_id=10, user_id=24, media_id=500,
              media_name="博客园", status="pending", mhz_order_id=None)
    make_item(db, item_id=101, order_id=11, user_id=24, media_id=500,
              media_name="博客园", status="pending", mhz_order_id="SN-RUNNING")
    make_item(db, item_id=102, order_id=12, user_id=24, media_id=500,
              media_name="博客园", status="pending", mhz_order_id=None)
    make_item(db, item_id=103, order_id=13, user_id=24, media_id=500,
              media_name="博客园", status="failed", mhz_order_id=None)
    make_item(db, item_id=104, order_id=14, user_id=24, media_id=500,
              media_name="抖音", status="pending", mhz_order_id=None, media_type="svideo")


def test_r2_candidate_cells(db):
    from db.meijiehezi_db import find_orphan_publish_items

    _seed_candidates(db)
    got = {r["id"] for r in find_orphan_publish_items(
        min_age_hours=3, statuses=list(SWEEPABLE_STATUSES), limit=50)}

    # 必须命中:无单号 + 超阈值
    assert 100 in got
    # 🔴 必须不命中:有单号的 pending(不管多久)—— 清了等于凭空退款
    assert 101 not in got
    # 必须不命中:无单号但未到阈值(给正常提交/既有重试留时间)
    assert 102 not in got
    # 必须不命中:已是终态的不重复处理
    assert 103 not in got
    # 必须不命中:短视频有自己的出口,不越界
    assert 104 not in got


def test_r2_includes_paused_admin_dedupe_but_not_awaiting_states(db):
    """98 天的 paused_admin_dedupe 要收口;awaiting_* 各有主人,不许抢。"""
    from db.meijiehezi_db import find_orphan_publish_items

    make_order(db, order_id=20, user_id=15, article_id=300, title="98天", age_hours=2400)
    make_item(db, item_id=200, order_id=20, user_id=15, media_id=600,
              media_name="搜狐网", status="paused_admin_dedupe")
    make_item(db, item_id=201, order_id=20, user_id=15, media_id=601,
              media_name="咸宁", status="awaiting_sync")
    make_item(db, item_id=202, order_id=20, user_id=15, media_id=602,
              media_name="正见", status="awaiting_action")
    make_item(db, item_id=203, order_id=20, user_id=15, media_id=603,
              media_name="某某", status="awaiting_confirmation")
    got = {r["id"] for r in find_orphan_publish_items(
        min_age_hours=3, statuses=list(SWEEPABLE_STATUSES), limit=50)}
    assert got == {200}


# ============================================================
# R2 + R1b · 清扫器端到端(真库 + 退款打桩)
# ============================================================

@pytest.fixture
def refund_spy(monkeypatch):
    calls = []

    def _fake_refund(*, user_id, amount, refund_key, reason):
        calls.append({"user_id": user_id, "amount": amount,
                      "refund_key": refund_key, "reason": reason})
        return {"success": True, "refunded": amount, "skipped": False, "reason": reason}

    import db.meijiehezi_db as mdb
    monkeypatch.setattr(mdb, "refund_for_publish_order", _fake_refund)
    return calls


def _fresh_sync(db, minutes_ago: int = 1):
    from db.meijiehezi_db import set_config
    set_config("last_status_sync",
               (datetime.now() - timedelta(minutes=minutes_ago)).isoformat())
    set_config("last_toutiao_status_sync",
               (datetime.now() - timedelta(minutes=minutes_ago)).isoformat())


def _make_mirror_discriminating(db):
    """给判别力自检喂一个"有单号且镜像命中"的样本,否则清扫器整轮跳过。"""
    make_order(db, order_id=90, user_id=1, article_id=900, title="判别力样本")
    make_item(db, item_id=900, order_id=90, user_id=1, media_id=999,
              media_name="样本媒体", status="published", mhz_order_id="SN-SAMPLE")
    make_synced(db, sid="s-sample", order_sn="SN-SAMPLE", title="判别力样本",
                media_name="样本媒体", resource_id=999)


def test_sweep_refunds_when_external_absent(db, refund_spy):
    """必须命中:外部查不到 → 终态 + 退款,幂等键必须是 item:{id}。"""
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _make_mirror_discriminating(db)
    _fresh_sync(db)
    make_order(db, order_id=30, user_id=24, article_id=400, title="外部没有这单",
               age_hours=100, deducted=780)
    make_item(db, item_id=300, order_id=30, user_id=24, media_id=500,
              media_name="博客园", status="pending", cost_points=780)

    res = sweep_orphan_publish_items()

    assert res.refunded_items == 1
    assert item_row(db, 300)["status"] == "failed"
    assert refund_spy == [{
        "user_id": 24, "amount": 780,
        "refund_key": "item:300", "reason": "超时未取得外部单号,自动退回",
    }]


def test_sweep_refunds_when_external_claimed_by_other_item(db, refund_spy):
    """必须命中:386 型 —— 外部有单但已被别的本地条目认领 → 属重复收费,该退。"""
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _make_mirror_discriminating(db)
    _fresh_sync(db)
    make_order(db, order_id=31, user_id=24, article_id=401, title="重复下单标题",
               age_hours=100, deducted=780)
    # 383 那条:同标题同媒体,已经拿着外部单号
    make_item(db, item_id=310, order_id=31, user_id=24, media_id=500,
              media_name="博客园", status="published", mhz_order_id="SN-DUP")
    # 386 那条:同标题同媒体,永远拿不到单号
    make_item(db, item_id=311, order_id=31, user_id=24, media_id=500,
              media_name="博客园", status="pending")
    make_synced(db, sid="s-dup", order_sn="SN-DUP", title="重复下单标题",
                media_name="博客园", resource_id=500)

    res = sweep_orphan_publish_items()

    assert res.refunded_items == 1
    assert res.manual_review_items == 0
    assert [c["refund_key"] for c in refund_spy] == ["item:311"]
    # 必须不命中:拿到单号的那条一分钱不许退、状态一格不许动
    assert item_row(db, 310)["status"] == "published"
    assert "item:310" not in [c["refund_key"] for c in refund_spy]


def test_sweep_must_not_refund_when_external_unclaimed(db, refund_spy):
    """🔴 必须不命中:外部有单且未被任何本地条目认领 → 一分不退,转人工。"""
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _make_mirror_discriminating(db)
    _fresh_sync(db)
    make_order(db, order_id=32, user_id=24, article_id=402, title="对方收了回执丢了",
               age_hours=100, deducted=780)
    make_item(db, item_id=320, order_id=32, user_id=24, media_id=500,
              media_name="博客园", status="pending")
    make_synced(db, sid="s-orphan", order_sn="SN-UNCLAIMED", title="对方收了回执丢了",
                media_name="博客园", resource_id=500)

    res = sweep_orphan_publish_items()

    assert refund_spy == [], "外部有未被认领的单还退款 = 既退钱又发稿"
    assert res.refunded_items == 0
    assert res.manual_review_items == 1
    row = item_row(db, 320)
    assert row["status"] == "awaiting_sync"
    assert row["manual_review_required"] is True
    # 必须不命中:绝不能被推进终态(终态就再也没人回来核对了)
    assert row["status"] != "failed"


def test_sweep_skips_entire_round_when_sync_stale(db, refund_spy):
    """🔴 必须不命中:同步不新鲜时"外部查不到"是假结论 → 整轮一分不退。"""
    from db.meijiehezi_db import set_config
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _make_mirror_discriminating(db)
    set_config("last_status_sync", (datetime.now() - timedelta(hours=5)).isoformat())
    make_order(db, order_id=33, user_id=24, article_id=403, title="同步挂了",
               age_hours=100, deducted=780)
    make_item(db, item_id=330, order_id=33, user_id=24, media_id=500,
              media_name="博客园", status="pending")

    res = sweep_orphan_publish_items()

    assert refund_spy == []
    assert res.refunded_items == 0
    assert item_row(db, 330)["status"] == "pending"
    assert "不新鲜" in res.skipped_reason


def test_sweep_skips_round_when_mirror_not_discriminating(db, refund_spy):
    """🔴 必须不命中:判别力自检没过 → 整轮跳过,不许"查不到就退"。"""
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _fresh_sync(db)
    # 故意不喂判别力样本:有单号的条目在镜像里查不到
    make_order(db, order_id=91, user_id=1, article_id=901, title="坏样本")
    make_item(db, item_id=910, order_id=91, user_id=1, media_id=999,
              media_name="X", status="published", mhz_order_id="SN-NOT-IN-MIRROR")
    make_order(db, order_id=34, user_id=24, article_id=404, title="候选", age_hours=100)
    make_item(db, item_id=340, order_id=34, user_id=24, media_id=500,
              media_name="博客园", status="pending")

    res = sweep_orphan_publish_items()

    assert refund_spy == []
    assert item_row(db, 340)["status"] == "pending"
    assert res.skipped_reason == "镜像判别力自检未通过"


def test_sweep_never_touches_items_with_order_id(db, refund_spy):
    """🔴 红线:有 mhz_order_id 的 submitted 必须原样不动(生产现有 7 条 19,110 分)。"""
    from services.publish_orphan_settlement import sweep_orphan_publish_items

    _make_mirror_discriminating(db)
    _fresh_sync(db)
    make_order(db, order_id=35, user_id=24, article_id=405, title="在跑的单", age_hours=1000)
    make_item(db, item_id=350, order_id=35, user_id=24, media_id=500,
              media_name="博客园", status="submitted", mhz_order_id="SN-INFLIGHT",
              cost_points=2730)

    before = item_row(db, 350)
    sweep_orphan_publish_items()
    after = item_row(db, 350)

    assert refund_spy == []
    assert after["status"] == before["status"] == "submitted"
    assert after["mhz_order_id"] == "SN-INFLIGHT"


# ============================================================
# R3 · 去重判据拆语义
# ============================================================

def test_r3_dedupe_cells(db):
    from db.meijiehezi_db import (
        BLOCK_REASON_ALREADY_PUBLISHED, BLOCK_REASON_IN_FLIGHT,
        find_active_orders_for_media,
    )

    make_order(db, order_id=40, user_id=24, article_id=500, title="A")
    # 501 已发过 → 该拦,理由 ALREADY_PUBLISHED
    make_item(db, item_id=400, order_id=40, user_id=24, media_id=501,
              media_name="已发过", status="published", mhz_order_id="SN-P")
    # 502 有单号在跑 → 该拦,理由 IN_FLIGHT
    make_item(db, item_id=401, order_id=40, user_id=24, media_id=502,
              media_name="在发", status="pending", mhz_order_id="SN-R")
    # 503 无单号 pending → 🔴 不该拦(本次修复点)
    make_item(db, item_id=402, order_id=40, user_id=24, media_id=503,
              media_name="僵尸", status="pending", mhz_order_id=None)
    # 504~507 终态 → 不该拦(老行为不许回退)
    for i, st in enumerate(("failed", "rejected", "withdrawn", "cancelled")):
        make_item(db, item_id=410 + i, order_id=40, user_id=24, media_id=504 + i,
                  media_name=st, status=st)
    # 508 awaiting_confirmation 无单号 → 仍该拦(等用户确认,放行会重复扣费)
    make_item(db, item_id=420, order_id=40, user_id=24, media_id=508,
              media_name="待确认", status="awaiting_confirmation")

    got = {d["media_id"]: d["reason_code"] for d in find_active_orders_for_media(
        500, [501, 502, 503, 504, 505, 506, 507, 508])}

    # 必须命中
    assert got[501] == BLOCK_REASON_ALREADY_PUBLISHED
    assert got[502] == BLOCK_REASON_IN_FLIGHT
    assert got[508] == BLOCK_REASON_IN_FLIGHT
    # 🔴 必须不命中
    assert 503 not in got, "无外部单号的 pending 不该再拦人 —— 那是永久锁死的根因"
    for mid in (504, 505, 506, 507):
        assert mid not in got, f"终态 media {mid} 被拦 = 老行为回退"


def test_r3_published_still_blocks_even_when_ancient(db):
    """不回退:published 的重复拦截仍然生效(别把防重发拆没了)。"""
    from db.meijiehezi_db import BLOCK_REASON_ALREADY_PUBLISHED, find_active_orders_for_media

    make_order(db, order_id=41, user_id=24, article_id=501, title="B", age_hours=95 * 24)
    make_item(db, item_id=430, order_id=41, user_id=24, media_id=600,
              media_name="搜狐", status="published", mhz_order_id="SN-OLD")
    got = find_active_orders_for_media(501, [600])
    assert [d["reason_code"] for d in got] == [BLOCK_REASON_ALREADY_PUBLISHED]


def test_r3_published_wins_over_inflight_on_same_media(db):
    """同一媒体既有 published 又有在途时,理由码取"已发过"(更重要的那条)。"""
    from db.meijiehezi_db import BLOCK_REASON_ALREADY_PUBLISHED, find_active_orders_for_media

    make_order(db, order_id=42, user_id=24, article_id=502, title="C")
    make_item(db, item_id=440, order_id=42, user_id=24, media_id=700,
              media_name="M", status="published", mhz_order_id="SN-1")
    make_item(db, item_id=441, order_id=42, user_id=24, media_id=700,
              media_name="M", status="pending", mhz_order_id="SN-2")
    got = find_active_orders_for_media(502, [700])
    assert len(got) == 1
    assert got[0]["reason_code"] == BLOCK_REASON_ALREADY_PUBLISHED


def test_r3_submit_time_dedupe_also_exempts_old_zombies(db):
    """🔴 第二道闸也得放行陈年僵尸,否则锁只是从"扣费前"挪到"提交时"。

    扣费前闸放行 → 建单扣费 → 提交时闸判重复 → mark_failed_duplicate + 退款,
    用户点了不报错、钱也退了,**文章还是发不出去**。两道闸口径必须一致。
    """
    from db.meijiehezi_db import check_duplicate_submission

    # 陈年僵尸(4 天前 · pending · 无单号)
    make_order(db, order_id=60, user_id=24, article_id=700, title="E", age_hours=96)
    make_item(db, item_id=600, order_id=60, user_id=24, media_id=900,
              media_name="博客园", status="pending")
    # 用户重新提交产生的新 item
    make_order(db, order_id=61, user_id=24, article_id=700, title="E", age_hours=0)
    make_item(db, item_id=601, order_id=61, user_id=24, media_id=900,
              media_name="博客园", status="submitting")

    # 必须不命中:陈年僵尸不算占用
    assert check_duplicate_submission(700, 900, exclude_item_id=601) is None

    # 必须命中:**同批刚下**的无单号 pending 仍算在途(生产 386 型本地重复下单)
    make_order(db, order_id=62, user_id=24, article_id=701, title="F", age_hours=0)
    make_item(db, item_id=610, order_id=62, user_id=24, media_id=901,
              media_name="博客园", status="pending")
    make_item(db, item_id=611, order_id=62, user_id=24, media_id=901,
              media_name="博客园", status="submitting")
    assert check_duplicate_submission(701, 901, exclude_item_id=611) == 610

    # 必须命中:陈年但**有单号**的照旧算占用(真在外部通道跑)
    make_order(db, order_id=63, user_id=24, article_id=702, title="G", age_hours=96)
    make_item(db, item_id=620, order_id=63, user_id=24, media_id=902,
              media_name="博客园", status="pending", mhz_order_id="SN-OLD-RUNNING")
    assert check_duplicate_submission(702, 902, exclude_item_id=999) == 620

    # 必须命中:陈年 published 照旧算占用(防重发不许拆没)
    make_order(db, order_id=64, user_id=24, article_id=703, title="H", age_hours=96)
    make_item(db, item_id=630, order_id=64, user_id=24, media_id=903,
              media_name="博客园", status="published", mhz_order_id="SN-PUB")
    assert check_duplicate_submission(703, 903, exclude_item_id=999) == 630


# ============================================================
# R1 · update_order_item_submitted fail-closed
# ============================================================

def test_r1_submitted_guard_cells(db):
    from db.meijiehezi_db import update_order_item_submitted

    make_order(db, order_id=50, user_id=24, article_id=600, title="D")
    make_item(db, item_id=500, order_id=50, user_id=24, media_id=800,
              media_name="M", status="submitting")
    make_item(db, item_id=501, order_id=50, user_id=24, media_id=801,
              media_name="M", status="submitting")

    # 必须命中:有单号 → 落库
    assert update_order_item_submitted(500, "SN-OK") is True
    assert item_row(db, 500)["status"] == "submitted"
    assert item_row(db, 500)["mhz_order_id"] == "SN-OK"

    # 🔴 必须不命中:空单号一律拒绝,且**什么都不写**
    for empty in (None, "", "   "):
        assert update_order_item_submitted(501, empty) is False
        row = item_row(db, 501)
        assert row["status"] == "submitting", "空单号不许把状态推成 submitted"
        assert row["mhz_order_id"] is None


# ============================================================
# R4 · 文案
# ============================================================

def test_r4_message_cells():
    from api.meijiehezi_api import build_duplicate_block_message, shorten_title_for_message
    from db.meijiehezi_db import BLOCK_REASON_ALREADY_PUBLISHED, BLOCK_REASON_IN_FLIGHT

    published_only = build_duplicate_block_message(
        [{"reason_code": BLOCK_REASON_ALREADY_PUBLISHED, "label": "文章×博客园"}])
    # 必须命中:已发过要说"已经发过"
    assert "已经发过" in published_only
    # 🔴 必须不命中:published 说成"进行中"正是 235 条老单误导用户的原文案
    assert "进行中" not in published_only
    assert "正在发布中" not in published_only

    inflight_only = build_duplicate_block_message(
        [{"reason_code": BLOCK_REASON_IN_FLIGHT, "label": "文章×博客园"}])
    assert "正在发布中" in inflight_only
    assert "已经发过" not in inflight_only

    # 两种理由并存时分开说,不混成一句
    both = build_duplicate_block_message([
        {"reason_code": BLOCK_REASON_ALREADY_PUBLISHED, "label": "甲×博客园"},
        {"reason_code": BLOCK_REASON_IN_FLIGHT, "label": "乙×搜狐"},
    ])
    assert "已经发过" in both and "正在发布中" in both
    assert both.index("已经发过") < both.index("正在发布中")


def test_r4_overflow_is_not_silent():
    from api.meijiehezi_api import build_duplicate_block_message
    from db.meijiehezi_db import BLOCK_REASON_IN_FLIGHT

    entries = [{"reason_code": BLOCK_REASON_IN_FLIGHT, "label": f"文章{i}×博客园"}
               for i in range(9)]
    msg = build_duplicate_block_message(entries)
    # 必须命中:超出部分要说明还有几条
    assert "共 9 条" in msg
    # 必须不命中:第 6 条之后不再逐条罗列(仍然截断,只是不静默)
    assert "文章6×博客园" not in msg

    exactly_five = [{"reason_code": BLOCK_REASON_IN_FLIGHT, "label": f"文章{i}×博客园"}
                    for i in range(5)]
    msg5 = build_duplicate_block_message(exactly_five)
    # 必须不命中:正好 5 条时不该冒出"等共 N 条"
    assert "共" not in msg5


def test_r4_title_not_cut_mid_sentence():
    from api.meijiehezi_api import shorten_title_for_message

    long_title = "福田区全屋定制：香蜜湖等片区老房改造该怎么选服务商？2026 版避坑指南"
    out = shorten_title_for_message(long_title)
    # 必须命中:保留头也保留尾,中间省略
    assert out.startswith("福田区全屋定制")
    assert out.endswith(long_title[-3:])
    assert "…" in out
    # 🔴 必须不命中:不再是老的硬截 15 字(那截出"福田区全屋定制：香蜜湖等片区老"这种半句话)
    assert out != long_title[:15]

    short = "短标题"
    # 必须不命中:短标题不该被加省略号
    assert shorten_title_for_message(short) == short
    assert "…" not in shorten_title_for_message(short)


# ============================================================
# 结构守卫 + 接线(剥注释/docstring 后匹配)
# ============================================================

def _src(rel: str) -> str:
    return _strip_py_noise((REPO / rel).read_text(encoding="utf-8"))


def test_no_second_refund_path():
    """🔴 红线:本模块的退款只许走既有 refund_for_publish_order + item:{id}。"""
    body = _src("services/publish_orphan_settlement.py")
    assert "refund_for_publish_order(" in body
    assert 'refund_key=f"item:{item_id}"' in body
    # 必须不命中:不许自己动钱包/自己写流水
    for forbidden in ("user_wallets", "insert_transaction", "paid_points",
                      "customer_credit_transactions", "refund_credit("):
        assert forbidden not in body, f"清扫器不许自造资金路径: {forbidden}"


def test_orphan_query_pins_null_order_id_in_sql():
    """红线落在 SQL 里,不靠调用方自觉。"""
    body = _src("db/meijiehezi_db.py")
    head = body.index("def find_orphan_publish_items(")
    tail = body.index("def mark_orphan_needs_manual_review(", head)
    sql = body[head:tail]
    assert "mhz_order_id IS NULL OR i.mhz_order_id = ''" in sql
    assert "<> 'svideo'" in sql


def test_new_functions_have_real_call_sites():
    """新增函数必须 grep 到 ≥1 个真实调用点(测试不算)。"""
    api = _src("api/meijiehezi_api.py")
    sched = _src("api/scheduler.py")
    svc = _src("services/publish_orphan_settlement.py")

    # R2 清扫器 → 被 scheduler job 调用,且 job 真的注册了
    assert "sweep_orphan_publish_items" in sched
    assert "_mhz_orphan_zombie_sweep" in sched
    assert "id='mhz_orphan_zombie_sweep'" in sched
    # R1b 三件套 → 被清扫器调用
    for fn in ("mirror_lookup_is_discriminating", "classify_orphan_item",
               "decide_orphan_action", "mirror_is_fresh", "sync_config_key_for"):
        assert svc.count(fn) >= 2, f"{fn} 只有定义没有调用点"
    # DB 侧新函数 → 被 service 调用
    for fn in ("find_orphan_publish_items", "mark_orphan_needs_manual_review",
               "find_mirror_rows_for_orphan", "count_mirror_hits_for_known_orders"):
        assert fn in svc, f"{fn} 是死函数(service 里没有调用点)"
    # R4 文案 → 两个端点都在用
    assert api.count("build_duplicate_block_message") >= 3
    assert api.count("shorten_title_for_message") >= 2


#: 被守卫的那个 sink 的符号原名。别名(`import ... as X`)与模块属性调用都由
#: 检测器现场解析,不写死在这里。
_SINK_NAME = "update_order_item_submitted"

#: 允许「裸调用」(不套空单号收口)的豁免标记。写在调用行尾,强制新增 sink 的人
#: 显式说明为什么这条不需要收口 —— 而不是默默混进去。
_SN_EXEMPT_MARK = "WO-PUB-ZOMBIE-sn-nonempty"

#: 🔴 [返修 2026-08-20 · WO-C] 两个数都是**跑出来的**,不是手调到"正好对上"的。
#:
#: 交付(7f3ecbe53)那天是 6 = 批量软文 1 + 批量自媒体 1 + 调度器重试软文 1 +
#: 调度器重试自媒体 1 + 单发 1 + 短视频 1。今天是 5,差额**不是丢守卫**:
#: 上面四段里的前四段被 [WO-KYB-ROUTING D1] 合并进了共用件
#: `settle_group_order_sns`(api/meijiehezi_api.py),4 个各自带守卫的调用点变成
#: 1 个带守卫的调用点 + 4 个调用共用件的地方 —— 守卫一处没少,按调用点数的棘轮
#: 却掉了 3。同时本次把检测器的盲区(别名 `_mark_submitted` 那两处裸 sink,
#: 交付时就在,只是检测器只认符号原名所以没被看见)补进了作用域并补了守卫,+2。
#: 6 - 3(合并) + 2(补回盲区) = 5。
#:
#: 结论:这个棘轮挡的是"sink 被搬走 / 被贴豁免条",**不挡合法的合并重构** ——
#: 合并会让它红,改这一行是有意识的动作,但改之前必须先做一遍上面这种逐点考古。
_KNOWN_GUARDED_SINKS = 5

#: 豁免条的上限用 `<=` 钉:光钉守卫数下限挡不住"给裸 sink 贴张豁免条"
#: (那种改法 unguarded 空、guarded 不变,断言照绿)。
_KNOWN_EXEMPT_SINKS = 1


@functools.lru_cache(maxsize=1)
def _sink_scope_files() -> tuple[str, ...]:
    """轴的作用域 = 全仓(除 tests/)所有点到这个 sink 的 .py —— 机械枚举,不手写清单。

    🔴 原实现把作用域写死成 ("api/meijiehezi_api.py", "api/scheduler.py")。
    手写分母挡不住「把 sink 连同守卫一起搬去第三个文件」:那两个文件里
    unguarded 仍是空、guarded 也不掉,断言全绿而轴上多了个没人看的口子。
    任何调用形态(直呼 / `import ... as 别名` / `模块.属性`)都必须先在源码里
    写出这个符号名,所以"按符号名筛文件"对调用点是完备的。
    tests/ 排除:测试要直接调它断言 fail-closed 行为,那不是生产 sink。
    """
    files = []
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        try:
            # 按字节筛,不解码 —— 全仓扫一遍要快到能常驻在测试里
            raw = path.read_bytes()
        except OSError:
            continue
        if _SINK_NAME.encode() in raw:
            files.append(rel)
    return tuple(files)


def _submitted_sink_report(rel: str) -> tuple[list, list, list]:
    """走 AST 而不是文本窗口。返回 (guarded, unguarded, exempt)。

    🔴 文本窗口版本被变异实测打穿过:把 `if not f(x):` 改成
    `if False and not f(x):`,收口那几个词**还在窗口里**,断言照绿而行为已经废了
    ——「断言的词在死分支里也有」是弱锁的典型形态。
    改成结构判定:那个调用必须**整个**就是某个 if 的条件取反,
    条件里不许再 and 别的东西进去(那等于给收口装了个可以关掉的开关)。

    🔴 [返修 2026-08-20] 再补一个盲区:原版只认 `ast.Name.id == _SINK_NAME`,
    于是 `from db.meijiehezi_db import update_order_item_submitted as _mark_submitted`
    之后的 `_mark_submitted(...)` 全部隐身 —— 交付那天 api 里就有两处这样的裸
    sink 没被看见。现在现场解析本文件的 import 别名,并认 `模块.属性` 调用形态。
    """
    import ast

    raw = (REPO / rel).read_text(encoding="utf-8")
    tree = ast.parse(raw)
    lines = raw.splitlines()

    # 本文件内这个 sink 的所有可调用名字:符号原名 + 所有 import 别名
    names = {_SINK_NAME}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] == _SINK_NAME and alias.asname:
                    names.add(alias.asname)

    settle_names = {"set_item_awaiting_sync", "_set_awaiting_sync"}

    def _called_name(node):
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return None

    def _is_target(node) -> bool:
        n = _called_name(node)
        return n is not None and (n in names or n == _SINK_NAME)

    guarded, unguarded, exempt = [], [], []

    # 先收集所有"合规守卫"里的那个调用对象
    ok_calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and _is_target(test.operand)):
            continue
        settles = any(
            _called_name(n) in settle_names
            for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        )
        if settles:
            ok_calls.add(id(test.operand))

    for node in ast.walk(tree):
        if not _is_target(node):
            continue
        line = lines[node.lineno - 1]
        if id(node) in ok_calls:
            guarded.append(node.lineno)
        elif _SN_EXEMPT_MARK in line:
            exempt.append(node.lineno)   # 显式豁免:单号来源已证明非空
        else:
            unguarded.append(node.lineno)
    return guarded, unguarded, exempt


def test_r1_all_submitted_sinks_settle_on_empty_sn():
    """R1 接线:每个 update_order_item_submitted 调用点都必须带空单号收口。

    全仓多处 sink,少接一处就会重新长出无单号的在途条目 = 僵尸复发。
    """
    scope = _sink_scope_files()
    # 🔴 枚举器活性自证:分母算错成空集时,下面每一条断言都恒绿。
    #    这两个文件是轴上已知必然在场的,枚举不到它俩 = 枚举器本身坏了。
    for must in ("api/meijiehezi_api.py", "api/scheduler.py"):
        assert must in scope, f"作用域枚举器没扫到 {must},分母是坏的: {scope}"

    total_guarded = 0
    total_exempt = 0
    for rel in scope:
        guarded, unguarded, exempt = _submitted_sink_report(rel)
        assert not unguarded, (
            f"{rel}:{unguarded} 处 {_SINK_NAME} 没有空单号收口守卫"
            f"(守卫必须形如 `if not update_order_item_submitted(...):` 且体内调"
            f" set_item_awaiting_sync;确证单号非空的请在行尾标 {_SN_EXEMPT_MARK})")
        total_guarded += len(guarded)
        total_exempt += len(exempt)
    # 🔴 棘轮:光查"没有裸调用"挡不住"把守卫连同 sink 一起搬走 / 贴豁免条溜号"
    #   —— 那种改法 unguarded 仍是空,断言照绿。所以再钉一个下限。
    #   合法新增 sink 会把这个数顶上去,顺手改这行是**有意识**的动作,正是要的。
    assert total_guarded >= _KNOWN_GUARDED_SINKS, (
        f"收口守卫只剩 {total_guarded} 处(已知 {_KNOWN_GUARDED_SINKS} 处)"
        f" —— 有 sink 被搬走或被贴了豁免条")
    assert total_exempt <= _KNOWN_EXEMPT_SINKS, (
        f"豁免条涨到 {total_exempt} 处(已知 {_KNOWN_EXEMPT_SINKS} 处)"
        f" —— 新贴的豁免必须先证明单号来源非空,再改这行")


def test_r1_resubmit_alias_sinks_are_guarded():
    """返修点定桩:用户确认后重投那两条 sink(走 `_mark_submitted` 别名)必须带收口。

    这两处从交付起就是裸调用,而本函数成功路径**不**再 release_submit_lock ——
    fail-closed 的 sink 返 False 时 item 会带着提交锁永久停在 pending,
    正是 WO-PUB-ZOMBIE 那个僵尸形状。上面那条按数量的棘轮抓不到"具体是哪两处",
    所以在这里按行为形状单独钉一次。
    """
    guarded, unguarded, _exempt = _submitted_sink_report("api/meijiehezi_api.py")
    body = _src("api/meijiehezi_api.py")
    start = body.index("async def _resubmit_item_after_confirm(")
    end = body.index("def _fetch_article_content(", start)
    # 别名在这个函数里被 import,函数体内两处调用都必须落在 guarded 里
    first_line = body[:start].count(chr(10)) + 1
    last_line = body[:end].count(chr(10)) + 1
    in_region = [ln for ln in guarded if first_line <= ln <= last_line]
    assert len(in_region) >= 2, (
        f"_resubmit_item_after_confirm 里带收口的 sink 只有 {len(in_region)} 处(应 ≥2);"
        f" unguarded={unguarded}")
