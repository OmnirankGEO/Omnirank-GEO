"""
Phase 07 单测: subscription_billing 13 维扣减 + cache + clawback

来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
计划: .planning/phases/07-social-studio-subscription/PLAN.md T01

测试场景(8 case):
  1. SOCIAL_FEATURES 白名单约束(扣 GEO feature 应抛 ValueError)
  2. 视频单条 cap 超限抛 QuotaExceededError
  3. 配额够 → 扣 entitlement
  4. 配额不够 + fallback_to_points=True → 走 billing fallback
  5. 配额不够 + fallback_to_points=False → 抛 InsufficientQuotaError
  6. cache 命中 → 0 扣权益,返 cache_hit=True
  7. release_subscription_entitlement 回滚 used 不能 < 0
  8. month_end_reset 幂等(同月跑 2 次只执行 1 次)

注意: 这些测试需要测试 DB(可用 docker exec omnirank-db psql 跑 migration_009)。
本测试用 monkeypatch 模拟 db.connection / billing 子模块,不依赖真 DB。
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


@pytest.fixture
def mock_conn():
    """模拟 db connection 上下文"""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


@pytest.mark.asyncio
async def test_white_list_blocks_geo_features():
    """TC-1: SOCIAL_FEATURES 白名单约束"""
    from middleware.subscription_billing import charge_subscription_entitlement

    with pytest.raises(ValueError, match="SOCIAL_FEATURES 白名单"):
        await charge_subscription_entitlement(
            user_id=1, feature_code="geo_diagnosis"
        )


@pytest.mark.asyncio
async def test_video_single_cap_blocks_long_video(mock_conn):
    """TC-2: 视频单条 60 分钟超 cap=15 抛 QuotaExceededError"""
    from middleware.subscription_billing import charge_subscription_entitlement, QuotaExceededError

    conn, cur = mock_conn
    cur.fetchone.return_value = {
        "subscription_id": 1, "plan_id": "growth", "started_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=30),
        "auto_renew": False, "is_first_month": False, "status": "active",
        "referrer_user_id": None, "indirect_referrer_user_id": None,
        "price_locked_yuan": 99,
        "display_name": "内容增长", "monthly_yuan": 99,
        "entitlement_id": 100,
        "period_start": datetime.utcnow(), "period_end": datetime.utcnow() + timedelta(days=30),
        "video_minutes_limit": 90, "video_minutes_used": 0,
        "video_single_minutes_cap": 15,
        # 其他维度都给 0
        "light_chat_limit": 0, "light_chat_used": 0,
        "pro_write_limit": 0, "pro_write_used": 0,
        "super_write_limit": 0, "super_write_used": 0,
        "web_search_limit": 0, "web_search_used": 0,
        "rewrite_limit": 0, "rewrite_used": 0,
        "video_breakdown_limit": 0, "video_breakdown_used": 0,
        "author_breakdown_limit": 0, "author_breakdown_used": 0,
        "review_limit": 0, "review_used": 0,
        "monthly_plan_limit": 0, "monthly_plan_used": 0,
        "team_profile_limit": 0, "team_profile_used": 0,
        "quota_workspace": 3, "quota_knowledge_mb": 50,
    }

    with patch("middleware.subscription_billing.get_connection", return_value=conn):
        with pytest.raises(QuotaExceededError, match="单条视频"):
            await charge_subscription_entitlement(
                user_id=1, feature_code="video_asr",
                video_minutes=60, fallback_to_points=False
            )


@pytest.mark.asyncio
async def test_in_quota_charges_entitlement(mock_conn):
    """TC-3: 配额够 → 扣 entitlement,返 from='entitlement'"""
    from middleware.subscription_billing import charge_subscription_entitlement

    conn, cur = mock_conn

    # First call: get_active_subscription_full
    sub_row = {
        "subscription_id": 1, "plan_id": "growth", "started_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=30),
        "auto_renew": False, "is_first_month": False, "status": "active",
        "referrer_user_id": None, "indirect_referrer_user_id": None,
        "price_locked_yuan": 99,
        "display_name": "内容增长", "monthly_yuan": 99,
        "entitlement_id": 100,
        "period_start": datetime.utcnow(), "period_end": datetime.utcnow() + timedelta(days=30),
        "rewrite_limit": 15, "rewrite_used": 0,
        "video_minutes_limit": 90, "video_minutes_used": 0,
        "video_single_minutes_cap": 15,
        "light_chat_limit": 300, "light_chat_used": 0,
        "pro_write_limit": 40, "pro_write_used": 0,
        "super_write_limit": 6, "super_write_used": 0,
        "web_search_limit": 80, "web_search_used": 0,
        "video_breakdown_limit": 5, "video_breakdown_used": 0,
        "author_breakdown_limit": 1, "author_breakdown_used": 0,
        "review_limit": 4, "review_used": 0,
        "monthly_plan_limit": 2, "monthly_plan_used": 0,
        "team_profile_limit": 0, "team_profile_used": 0,
        "quota_workspace": 3, "quota_knowledge_mb": 50,
    }

    # _consume_entitlement 的 RETURNING
    consume_returning = {"rewrite_used": 1, "rewrite_limit": 15}

    cur.fetchone.side_effect = [sub_row, consume_returning, None]

    with patch("middleware.subscription_billing.get_connection", return_value=conn):
        result = await charge_subscription_entitlement(
            user_id=1, feature_code="rewrite",
        )

    assert result["ok"] is True
    assert result["from"] == "entitlement"
    assert result["deducted_amount"] == 1
    assert result["cache_hit"] is False


def test_release_does_not_go_below_zero(mock_conn):
    """TC-7: release used -= n 但不能 < 0(GREATEST(used - n, 0))"""
    from middleware.subscription_billing import release_subscription_entitlement

    conn, cur = mock_conn
    cur.fetchone.side_effect = [
        # get_active_subscription
        {
            "subscription_id": 1, "plan_id": "growth", "started_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(days=30),
            "auto_renew": False, "is_first_month": False, "status": "active",
            "referrer_user_id": None, "indirect_referrer_user_id": None,
            "price_locked_yuan": 99, "display_name": "内容增长", "monthly_yuan": 99,
            "entitlement_id": 100,
            "period_start": datetime.utcnow(), "period_end": datetime.utcnow() + timedelta(days=30),
            "rewrite_limit": 15, "rewrite_used": 5,
            "video_minutes_limit": 90, "video_minutes_used": 0,
            "video_single_minutes_cap": 15,
            "light_chat_limit": 300, "light_chat_used": 0,
            "pro_write_limit": 40, "pro_write_used": 0,
            "super_write_limit": 6, "super_write_used": 0,
            "web_search_limit": 80, "web_search_used": 0,
            "video_breakdown_limit": 5, "video_breakdown_used": 0,
            "author_breakdown_limit": 1, "author_breakdown_used": 0,
            "review_limit": 4, "review_used": 0,
            "monthly_plan_limit": 2, "monthly_plan_used": 0,
            "team_profile_limit": 0, "team_profile_used": 0,
            "quota_workspace": 3, "quota_knowledge_mb": 50,
        },
        # release returning - used 应该已经 GREATEST 防 < 0
        {"rewrite_used": 0},
    ]

    with patch("middleware.subscription_billing.get_connection", return_value=conn):
        result = release_subscription_entitlement(
            user_id=1, feature_code="rewrite", video_minutes=0
        )

    assert result["ok"] is True
    # SQL 用 GREATEST(used - n, 0) 保证不会 < 0
    update_sql = cur.execute.call_args_list[1][0][0]
    assert "GREATEST" in update_sql


def test_subscription_overrun_check_returns_hint():
    """TC-5 部分: overrun_check 返结构包含 fallback_points / addon_pack / upgrade_to"""
    from middleware.subscription_billing import subscription_overrun_check

    with patch("middleware.subscription_billing.get_active_subscription_full") as mock_get:
        mock_get.return_value = {
            "subscription_id": 1, "plan_id": "growth",
            "rewrite_limit": 15, "rewrite_used": 15,
            "display_name": "内容增长",
        }
        hint = subscription_overrun_check(user_id=1, feature_code="rewrite")

    assert hint["in_quota"] is False
    assert hint["quota_type"] == "rewrite"
    assert hint["fallback_points"] == 650  # 仿写
    assert hint["fallback_yuan"] == 5.0
    assert hint["addon_pack"] is None  # rewrite 不推荐扩展包
    assert hint["upgrade_to"] == "agency"  # growth → agency
    assert hint["upgrade_yuan"] == 299


def test_no_subscription_returns_no_active():
    """TC-4 部分: 无 active subscription 时 overrun_check 返 no_active_subscription=True"""
    from middleware.subscription_billing import subscription_overrun_check

    with patch("middleware.subscription_billing.get_active_subscription_full") as mock_get:
        mock_get.return_value = None
        hint = subscription_overrun_check(user_id=999, feature_code="rewrite")

    assert hint["in_quota"] is False
    assert hint.get("no_active_subscription") is True
    assert hint["fallback_points"] == 650
    assert hint["upgrade_to"] == "personal"


def test_make_cache_key_returns_md5():
    """cache key 用 video_url md5 / blogger_uid+depth"""
    from middleware.subscription_billing import _make_cache_key

    # video_url
    key = _make_cache_key("single_video", {"video_url": "https://douyin.com/video/123"})
    assert key.startswith("single_video:")
    assert len(key) > 30  # md5

    # blogger_uid + depth
    key = _make_cache_key("author_breakdown", {"blogger_uid": "abc123", "depth": "lite"})
    assert key == "author_breakdown:abc123:lite"

    # 不可缓存(profile_polish 没在 CACHE_TTL_HOURS)
    key = _make_cache_key("profile_polish", {})
    assert key is None


def test_b16_xunhupay_has_notify_url_param():
    """B16 修复: services/xunhupay.create_xunhupay_order 必须接受 notify_url 参数"""
    import inspect
    from services import xunhupay
    sig = inspect.signature(xunhupay.create_xunhupay_order)
    assert "notify_url" in sig.parameters, "B16: xunhupay 必须有 notify_url 参数"


def test_subscription_scheduler_jobs_have_1_entry():
    """B08 原锁「5 个 cron」—— WO_308(2026-09-27)订阅佣金停用、自动续费扣款停用后改成:只剩月度重置,
    3 个佣金 cron 与 03:00 宽限期 / 自动续费 cron 的 id 不许再出现在注册表里
    (行为级的锁见 tests/wo308_retire_subscription_commission_2026_09_27)"""
    import inspect
    from api import subscription_scheduler

    src = inspect.getsource(subscription_scheduler.register_subscription_jobs)
    assert "subscription_monthly_reset" in src
    for retired in ("subscription_commission_settle", "subscription_clawback_resolve",
                    "subscription_commission_reconcile", "subscription_grace_check"):
        assert f'"id": "{retired}"' not in src, f"WO_308:{retired} 已停用,不许再注册"


# ============================================================
# Codex Round 3 反馈:R2 账务黑洞修复回归测试
# ============================================================

def test_bug5_consume_entitlement_with_event_same_transaction():
    """Bug 5 修(Codex R3): _consume_entitlement_with_event 同事务原子提交"""
    import inspect
    from middleware import subscription_billing

    # 新函数必须存在
    assert hasattr(subscription_billing, "_consume_entitlement_with_event"), (
        "Bug 5: _consume_entitlement_with_event 同事务函数必须存在"
    )
    src = inspect.getsource(subscription_billing._consume_entitlement_with_event)
    # 同事务内必须 UPDATE entitlement + INSERT usage event 用同一 cur
    assert "UPDATE user_social_entitlements" in src
    assert "_write_usage_event_inline" in src, "Bug 5: 必须用 inline 版本同事务写"
    # 必须有 conn.rollback() 兜底
    assert "conn.rollback()" in src
    # charge_subscription_entitlement 必须调新函数,不再调旧 _consume_entitlement
    cmain = inspect.getsource(subscription_billing.charge_subscription_entitlement)
    assert "_consume_entitlement_with_event" in cmain, (
        "Bug 5: charge_subscription_entitlement 主路径必须改用同事务函数"
    )


def test_bug6_migration_fail_fast_returns_failed_at():
    """Bug 6 修(Codex R3): migration_009 任意 step 失败立即 return failed_at,不跑后续"""
    import inspect
    from db import migration_009_subscription_v2

    src = inspect.getsource(migration_009_subscription_v2.run_migration)
    assert "Bug 6 修" in src, "Bug 6 修复注释丢失"
    assert "FAIL_FAST" in src, "Bug 6: 必须打 FAIL_FAST 日志"
    assert "failed_at" in src, "Bug 6: 失败必须返 failed_at"
    assert "rollback_hint" in src, "Bug 6: 失败必须返 rollback_hint 提示清场"
    # 不能再有 fail_count += 1 然后继续跑
    assert "fail_count += 1" not in src, "Bug 6: 不能继续跑后续 step"
