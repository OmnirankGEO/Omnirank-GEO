"""
Migration 009 user seed: 老用户迁移到 free 套餐 (V3.1 P1-1)

来源: SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md §22
计划: .planning/phases/07-social-studio-subscription/PLAN.md B07

执行前置:
  - migration_009_subscription_v2.py 必须已跑(6 表 + 5 套餐 seed 全建)

执行内容:
  1. 所有 active users(无订阅)默认建 free 套餐 + 配套 entitlements
  2. admin / is_test 用户跳过
  3. 幂等(LEFT JOIN 跳过已有订阅的用户 + ON CONFLICT)

执行后置:
  - 老用户登录后看到的 /api/subscription/me 返 free 套餐
  - 30 天回馈窗口由前端 banner 自行处理(不在本 migration)
"""

import logging

logger = logging.getLogger("GEO-Migration-009-UserSeed")


def run_migration():
    from db.connection import get_connection

    conn = get_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        steps = [
            # ============================================================
            # 1. 创建 free subscription 给所有无订阅的真实用户
            # ============================================================
            (
                "1.1 给所有 active 真实用户建 free 套餐",
                # B20 修: users 表没有 is_admin 字段, OmniRank 通过 user_roles+roles 识别 admin
                """
                INSERT INTO user_social_subscriptions
                    (user_id, plan_id, channel, started_at, expires_at,
                     auto_renew, status, price_locked_yuan)
                SELECT
                    u.id, 'free', 'migration',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP + INTERVAL '3650 days',
                    FALSE, 'active', 0
                FROM users u
                LEFT JOIN user_social_subscriptions s
                    ON s.user_id = u.id AND s.status = 'active'
                LEFT JOIN user_roles ur ON ur.user_id = u.id
                LEFT JOIN roles r ON r.id = ur.role_id AND r.name = 'admin'
                WHERE s.id IS NULL
                  AND r.id IS NULL  -- 不是 admin
                  AND u.is_active = 1
                  AND NOT EXISTS (
                    SELECT 1 FROM brands b
                    WHERE b.owner_user_id = u.id AND b.is_test = TRUE
                  )
                """
            ),
            # ============================================================
            # 2. 给每个 free subscription 建 entitlements (13 维 free 数字)
            # ============================================================
            (
                "1.2 给 free 订阅建 entitlements",
                """
                INSERT INTO user_social_entitlements
                    (subscription_id, user_id, period_start, period_end,
                     light_chat_limit, pro_write_limit, super_write_limit, web_search_limit,
                     video_minutes_limit, video_single_minutes_cap, rewrite_limit,
                     video_breakdown_limit, author_breakdown_limit, review_limit,
                     monthly_plan_limit, team_profile_limit, status)
                SELECT
                    s.id, s.user_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '30 days',
                    5, 1, 0, 1,
                    3, 1, 1,
                    0, 0, 0,
                    0, 0, 'active'
                FROM user_social_subscriptions s
                LEFT JOIN user_social_entitlements e ON e.subscription_id = s.id AND e.status='active'
                WHERE s.plan_id = 'free' AND s.channel = 'migration' AND e.id IS NULL
                """
            ),
        ]

        success_count = 0
        affected_total = 0
        for desc, sql in steps:
            try:
                cur.execute(sql)
                affected = cur.rowcount
                affected_total += affected
                logger.info(f"✅ {desc}: {affected} rows")
                success_count += 1
            except Exception as e:
                logger.error(f"❌ {desc}: {e}")
                conn.rollback()
                raise

        conn.commit()

        # 验证
        cur.execute("""
            SELECT COUNT(*) AS subs FROM user_social_subscriptions
            WHERE plan_id = 'free' AND channel = 'migration'
        """)
        free_subs = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) AS ents FROM user_social_entitlements e
            JOIN user_social_subscriptions s ON s.id = e.subscription_id
            WHERE s.channel = 'migration'
        """)
        free_ents = cur.fetchone()

        logger.info(f"  迁移结果: {free_subs} free 订阅 + {free_ents} entitlements")
        return {
            "success": True,
            "free_subscriptions": dict(free_subs),
            "free_entitlements": dict(free_ents),
            "affected_total": affected_total,
        }

    except Exception:
        conn.rollback()
        raise
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
