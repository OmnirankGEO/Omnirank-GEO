"""
Migration 009: Social Studio 订阅系统 V3.1

来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
计划: .planning/phases/07-social-studio-subscription/PLAN.md
决策锁: .planning/phases/07-social-studio-subscription/PRICING_DECISIONS.md

V3.1 vs V2 改动:
  - 删除 V2 的 user_wallets ADD COLUMN subscription_points(改用 entitlements 独立表)
  - 新增 6 张表:
      subscription_plans                     — 套餐 SSOT(13 维)
      user_social_subscriptions              — 订阅主表
      user_social_entitlements               — 13 维权益账本(周期表)
      subscription_usage_events              — usage ledger 流水(P0-4)
      subscription_referral_records          — 订阅佣金独立 ledger
      commission_clawback_pending            — clawback 待清算(P1-5)
      first_month_special_whitelist          — ¥9.9 反黑产白名单(P1-2)
  - 5 套餐显式列名 seed(P0-5)
  - ¥99 视频分钟 90(Codex 修订,V3 原 120 / V3.1 P0-1 误紧 80 → 取中 90)
  - ¥99 仿写 15 / 拆视频 5(V3.1 P0-1)

幂等(可重复跑),所有 CREATE/ADD 走 IF NOT EXISTS / ON CONFLICT DO NOTHING。
"""

import logging

logger = logging.getLogger("GEO-Migration-009")


def run_migration():
    from db.connection import get_connection

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()

        steps = [
            # ============================================================
            # 1. subscription_plans — SSOT 13 维
            # ============================================================
            (
                "1.1 subscription_plans 套餐 SSOT 表",
                """
                CREATE TABLE IF NOT EXISTS subscription_plans (
                    plan_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    monthly_yuan NUMERIC(8,2) NOT NULL,
                    yearly_yuan NUMERIC(8,2),
                    first_month_yuan NUMERIC(8,2),
                    quota_light_chat INTEGER NOT NULL DEFAULT 0,
                    quota_pro_write INTEGER NOT NULL DEFAULT 0,
                    quota_super_write INTEGER NOT NULL DEFAULT 0,
                    quota_web_search INTEGER NOT NULL DEFAULT 0,
                    quota_video_minutes INTEGER NOT NULL DEFAULT 0,
                    quota_video_single_cap INTEGER NOT NULL DEFAULT 0,
                    quota_rewrite INTEGER NOT NULL DEFAULT 0,
                    quota_video_breakdown INTEGER NOT NULL DEFAULT 0,
                    quota_author_breakdown INTEGER NOT NULL DEFAULT 0,
                    quota_review INTEGER NOT NULL DEFAULT 0,
                    quota_monthly_plan INTEGER NOT NULL DEFAULT 0,
                    quota_team_profile INTEGER NOT NULL DEFAULT 0,
                    quota_workspace INTEGER NOT NULL DEFAULT 0,
                    quota_knowledge_mb INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            # 5 套餐显式列名 seed (P0-5)
            (
                "1.2 seed: free 免费体验",
                """
                INSERT INTO subscription_plans
                    (plan_id, display_name, monthly_yuan, yearly_yuan, first_month_yuan,
                     quota_light_chat, quota_pro_write, quota_super_write, quota_web_search,
                     quota_video_minutes, quota_video_single_cap, quota_rewrite,
                     quota_video_breakdown, quota_author_breakdown, quota_review,
                     quota_monthly_plan, quota_team_profile, quota_workspace, quota_knowledge_mb,
                     is_active, display_order)
                VALUES
                    ('free', '免费体验', 0, NULL, NULL,
                     5, 1, 0, 1,
                     3, 1, 1,
                     0, 0, 0,
                     0, 0, 1, 5,
                     TRUE, 0)
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),
            (
                "1.3 seed: personal ¥49(首月 ¥9.9)",
                """
                INSERT INTO subscription_plans
                    (plan_id, display_name, monthly_yuan, yearly_yuan, first_month_yuan,
                     quota_light_chat, quota_pro_write, quota_super_write, quota_web_search,
                     quota_video_minutes, quota_video_single_cap, quota_rewrite,
                     quota_video_breakdown, quota_author_breakdown, quota_review,
                     quota_monthly_plan, quota_team_profile, quota_workspace, quota_knowledge_mb,
                     is_active, display_order)
                VALUES
                    ('personal', '个人创作者', 49, 499, 9.9,
                     100, 15, 2, 30,
                     30, 5, 8,
                     4, 0, 1,
                     1, 0, 1, 10,
                     TRUE, 1)
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),
            (
                "1.4 seed: growth ¥99 主推(Codex 修订视频 90 分钟)",
                """
                INSERT INTO subscription_plans
                    (plan_id, display_name, monthly_yuan, yearly_yuan, first_month_yuan,
                     quota_light_chat, quota_pro_write, quota_super_write, quota_web_search,
                     quota_video_minutes, quota_video_single_cap, quota_rewrite,
                     quota_video_breakdown, quota_author_breakdown, quota_review,
                     quota_monthly_plan, quota_team_profile, quota_workspace, quota_knowledge_mb,
                     is_active, display_order)
                VALUES
                    ('growth', '内容增长', 99, 990, NULL,
                     300, 40, 6, 80,
                     90, 15, 15,
                     5, 1, 4,
                     2, 0, 3, 50,
                     TRUE, 2)
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),
            (
                "1.5 seed: agency ¥299 代运营",
                """
                INSERT INTO subscription_plans
                    (plan_id, display_name, monthly_yuan, yearly_yuan, first_month_yuan,
                     quota_light_chat, quota_pro_write, quota_super_write, quota_web_search,
                     quota_video_minutes, quota_video_single_cap, quota_rewrite,
                     quota_video_breakdown, quota_author_breakdown, quota_review,
                     quota_monthly_plan, quota_team_profile, quota_workspace, quota_knowledge_mb,
                     is_active, display_order)
                VALUES
                    ('agency', '代运营', 299, 2999, NULL,
                     1000, 130, 25, 300,
                     600, 30, 60,
                     25, 4, 15,
                     7, 3, 20, 200,
                     TRUE, 3)
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),
            (
                "1.6 seed: partner ¥599 起代理团队",
                """
                INSERT INTO subscription_plans
                    (plan_id, display_name, monthly_yuan, yearly_yuan, first_month_yuan,
                     quota_light_chat, quota_pro_write, quota_super_write, quota_web_search,
                     quota_video_minutes, quota_video_single_cap, quota_rewrite,
                     quota_video_breakdown, quota_author_breakdown, quota_review,
                     quota_monthly_plan, quota_team_profile, quota_workspace, quota_knowledge_mb,
                     is_active, display_order)
                VALUES
                    ('partner', '代理团队', 599, 5999, NULL,
                     1500, 200, 40, 500,
                     500, 60, 80,
                     40, 8, 20,
                     8, 3, -1, -1,
                     TRUE, 4)
                ON CONFLICT (plan_id) DO NOTHING
                """
            ),

            # ============================================================
            # 2. user_social_subscriptions — 订阅主表
            # ============================================================
            (
                "2.1 user_social_subscriptions 订阅主表",
                # B29 修: 加 billing_cycle 区分月付/年付,避免年付被当月付续费多扣
                # B30 修: 加 last_transaction_id 存微信/虎皮椒流水号供财务对账
                """
                CREATE TABLE IF NOT EXISTS user_social_subscriptions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    plan_id TEXT NOT NULL REFERENCES subscription_plans(plan_id),
                    channel TEXT NOT NULL DEFAULT 'wechat',
                    billing_cycle TEXT NOT NULL DEFAULT 'monthly',  -- B29: monthly / yearly
                    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    auto_renew BOOLEAN NOT NULL DEFAULT FALSE,
                    is_first_month BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'active',
                    cancelled_at TIMESTAMP,
                    refunded_at TIMESTAMP,
                    refund_amount_yuan NUMERIC(8,2),
                    grace_period_until TIMESTAMP,
                    last_renewal_attempt_at TIMESTAMP,
                    last_renewal_failed_reason TEXT,
                    last_transaction_id TEXT,                        -- B30: 微信/虎皮椒流水号
                    referrer_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    indirect_referrer_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    subscription_agreement_version TEXT,
                    price_locked_yuan NUMERIC(8,2),
                    order_id TEXT UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "2.1.b user_social_subscriptions ADD billing_cycle 兼容旧表",
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='user_social_subscriptions'
                          AND column_name='billing_cycle'
                    ) THEN
                        ALTER TABLE user_social_subscriptions
                        ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT 'monthly';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='user_social_subscriptions'
                          AND column_name='last_transaction_id'
                    ) THEN
                        ALTER TABLE user_social_subscriptions
                        ADD COLUMN last_transaction_id TEXT;
                    END IF;
                END$$;
                """
            ),
            (
                "2.2 idx_subs_user_active",
                "CREATE INDEX IF NOT EXISTS idx_subs_user_active ON user_social_subscriptions(user_id) WHERE status='active'"
            ),
            (
                "2.3 idx_subs_expires",
                "CREATE INDEX IF NOT EXISTS idx_subs_expires ON user_social_subscriptions(expires_at) WHERE status='active'"
            ),
            (
                "2.4 idx_subs_referrer",
                "CREATE INDEX IF NOT EXISTS idx_subs_referrer ON user_social_subscriptions(referrer_user_id) WHERE referrer_user_id IS NOT NULL"
            ),

            # ============================================================
            # 3. user_social_entitlements — 13 维权益账本
            # ============================================================
            (
                "3.1 user_social_entitlements 13 维权益表",
                """
                CREATE TABLE IF NOT EXISTS user_social_entitlements (
                    id BIGSERIAL PRIMARY KEY,
                    subscription_id BIGINT NOT NULL REFERENCES user_social_subscriptions(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    period_start TIMESTAMP NOT NULL,
                    period_end TIMESTAMP NOT NULL,
                    light_chat_limit INTEGER NOT NULL DEFAULT 0,
                    light_chat_used INTEGER NOT NULL DEFAULT 0,
                    pro_write_limit INTEGER NOT NULL DEFAULT 0,
                    pro_write_used INTEGER NOT NULL DEFAULT 0,
                    super_write_limit INTEGER NOT NULL DEFAULT 0,
                    super_write_used INTEGER NOT NULL DEFAULT 0,
                    web_search_limit INTEGER NOT NULL DEFAULT 0,
                    web_search_used INTEGER NOT NULL DEFAULT 0,
                    video_minutes_limit INTEGER NOT NULL DEFAULT 0,
                    video_minutes_used INTEGER NOT NULL DEFAULT 0,
                    video_single_minutes_cap INTEGER NOT NULL DEFAULT 0,
                    rewrite_limit INTEGER NOT NULL DEFAULT 0,
                    rewrite_used INTEGER NOT NULL DEFAULT 0,
                    video_breakdown_limit INTEGER NOT NULL DEFAULT 0,
                    video_breakdown_used INTEGER NOT NULL DEFAULT 0,
                    author_breakdown_limit INTEGER NOT NULL DEFAULT 0,
                    author_breakdown_used INTEGER NOT NULL DEFAULT 0,
                    review_limit INTEGER NOT NULL DEFAULT 0,
                    review_used INTEGER NOT NULL DEFAULT 0,
                    monthly_plan_limit INTEGER NOT NULL DEFAULT 0,
                    monthly_plan_used INTEGER NOT NULL DEFAULT 0,
                    team_profile_limit INTEGER NOT NULL DEFAULT 0,
                    team_profile_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "3.2 idx_ent_user_active",
                "CREATE INDEX IF NOT EXISTS idx_ent_user_active ON user_social_entitlements(user_id) WHERE status='active'"
            ),
            (
                "3.3 idx_ent_period",
                "CREATE INDEX IF NOT EXISTS idx_ent_period ON user_social_entitlements(period_end) WHERE status='active'"
            ),

            # ============================================================
            # 4. subscription_usage_events — usage ledger (P0-4)
            # ============================================================
            (
                "4.1 subscription_usage_events 流水表",
                """
                CREATE TABLE IF NOT EXISTS subscription_usage_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    subscription_id BIGINT REFERENCES user_social_subscriptions(id),
                    entitlement_id BIGINT REFERENCES user_social_entitlements(id),
                    request_id TEXT NOT NULL,
                    feature_code TEXT NOT NULL,
                    consumed_quantity INTEGER NOT NULL DEFAULT 1,
                    consumed_unit TEXT NOT NULL,
                    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                    cache_key TEXT,
                    deduction_source TEXT NOT NULL,
                    deduction_amount BIGINT NOT NULL DEFAULT 0,
                    brand_id INTEGER,
                    metadata JSONB,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "4.2 idx_use_user_time",
                "CREATE INDEX IF NOT EXISTS idx_use_user_time ON subscription_usage_events(user_id, created_at DESC)"
            ),
            (
                "4.3 idx_use_request",
                "CREATE INDEX IF NOT EXISTS idx_use_request ON subscription_usage_events(request_id)"
            ),
            (
                "4.4 idx_use_sub",
                "CREATE INDEX IF NOT EXISTS idx_use_sub ON subscription_usage_events(subscription_id)"
            ),
            (
                "4.5 idx_use_feature_time",
                "CREATE INDEX IF NOT EXISTS idx_use_feature_time ON subscription_usage_events(feature_code, created_at DESC)"
            ),

            # ============================================================
            # 5. subscription_referral_records — 订阅佣金独立 ledger
            # ============================================================
            (
                "5.1 subscription_referral_records 订阅佣金 ledger",
                """
                CREATE TABLE IF NOT EXISTS subscription_referral_records (
                    id BIGSERIAL PRIMARY KEY,
                    subscription_id BIGINT NOT NULL REFERENCES user_social_subscriptions(id) ON DELETE CASCADE,
                    payer_user_id INTEGER NOT NULL REFERENCES users(id),
                    beneficiary_user_id INTEGER NOT NULL REFERENCES users(id),
                    beneficiary_level SMALLINT NOT NULL CHECK (beneficiary_level IN (1, 2)),
                    month_index INTEGER NOT NULL CHECK (month_index >= 1),
                    rate NUMERIC(5,4) NOT NULL,
                    amount_yuan NUMERIC(8,2) NOT NULL,
                    amount_points BIGINT NOT NULL,
                    bonus_inflation_points BIGINT NOT NULL DEFAULT 0,
                    is_self_referral BOOLEAN NOT NULL DEFAULT FALSE,
                    fraud_flag BOOLEAN NOT NULL DEFAULT FALSE,
                    fraud_reason TEXT,
                    settled_at TIMESTAMP,
                    settle_status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "5.2 idx_sub_ref_settle",
                "CREATE INDEX IF NOT EXISTS idx_sub_ref_settle ON subscription_referral_records(beneficiary_user_id, settle_status)"
            ),
            (
                "5.3 idx_sub_ref_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_ref_unique ON subscription_referral_records(subscription_id, beneficiary_level, month_index)"
            ),

            # ============================================================
            # 6. commission_clawback_pending — clawback 待清算 (P1-5)
            # ============================================================
            (
                "6.1 commission_clawback_pending 待清算表",
                """
                CREATE TABLE IF NOT EXISTS commission_clawback_pending (
                    id BIGSERIAL PRIMARY KEY,
                    beneficiary_user_id INTEGER NOT NULL REFERENCES users(id),
                    referral_record_id BIGINT NOT NULL REFERENCES subscription_referral_records(id),
                    amount_points BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolution_note TEXT
                )
                """
            ),
            (
                "6.2 idx_clawback_user",
                "CREATE INDEX IF NOT EXISTS idx_clawback_user ON commission_clawback_pending(beneficiary_user_id) WHERE status='pending'"
            ),

            # ============================================================
            # 7. first_month_special_whitelist — ¥9.9 反黑产白名单 (P1-2)
            # ============================================================
            (
                "7.1 first_month_special_whitelist 白名单",
                """
                CREATE TABLE IF NOT EXISTS first_month_special_whitelist (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    reason TEXT NOT NULL,
                    approved_by_admin_id INTEGER REFERENCES users(id),
                    used_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "7.2 idx_whitelist_user",
                "CREATE INDEX IF NOT EXISTS idx_whitelist_user ON first_month_special_whitelist(user_id) WHERE used_at IS NULL"
            ),
        ]

        # Bug 6 修(Codex Round 3): autocommit=True 配 try/except 吞异常继续跑
        # = 留下半套不可恢复混合状态。改"失败即停":任意 step 失败立即返回 failed_at,
        # 不跑后续 step,把回滚提示丢回调用方让它清场。
        success_count = 0
        for desc, sql in steps:
            try:
                cur.execute(sql)
                logger.info(f"✅ {desc}")
                success_count += 1
            except Exception as e:
                logger.error(f"❌ FAIL_FAST stop at: {desc} | err={e}")
                return {
                    "success": False,
                    "ok": success_count,
                    "failed_at": desc,
                    "error": str(e),
                    "completed_steps": success_count,
                    "total_steps": len(steps),
                    "rollback_hint": (
                        "autocommit=True 模式下已完成的 step 已落地,"
                        "请检查 fail step 类型:"
                        "CREATE TABLE 失败 → 跑前看是否已存在(IF NOT EXISTS 应吞);"
                        "ALTER TABLE 失败 → 列名/类型核对;"
                        "INSERT seed 失败 → 检查 ON CONFLICT;"
                        "整体回滚 DROP 列表见 .planning/phases/07-social-studio-subscription/PLAN.md ROLLBACK"
                    ),
                }

        logger.info(f"Migration 009 V3.1 done: {success_count}/{len(steps)} ok, 0 failed")

        # 验证(走到这说明所有 step 全过)
        verify = [
            ("subscription_plans rows(应为 5)", "SELECT COUNT(*) FROM subscription_plans"),
            ("user_social_subscriptions table", "SELECT to_regclass('user_social_subscriptions')"),
            ("user_social_entitlements table", "SELECT to_regclass('user_social_entitlements')"),
            ("subscription_usage_events table", "SELECT to_regclass('subscription_usage_events')"),
            ("subscription_referral_records table", "SELECT to_regclass('subscription_referral_records')"),
            ("commission_clawback_pending table", "SELECT to_regclass('commission_clawback_pending')"),
            ("first_month_special_whitelist table", "SELECT to_regclass('first_month_special_whitelist')"),
            ("growth(¥99)视频分钟应为 90", "SELECT quota_video_minutes FROM subscription_plans WHERE plan_id='growth'"),
            ("growth(¥99)仿写应为 15", "SELECT quota_rewrite FROM subscription_plans WHERE plan_id='growth'"),
            ("growth(¥99)拆视频应为 5", "SELECT quota_video_breakdown FROM subscription_plans WHERE plan_id='growth'"),
        ]
        for name, sql in verify:
            cur.execute(sql)
            row = cur.fetchone()
            logger.info(f"  verify {name}: {row}")

        return {
            "success": True,
            "ok": success_count,
            "fail": 0,
            "total_steps": len(steps),
        }

    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_migration()
    print(result)
