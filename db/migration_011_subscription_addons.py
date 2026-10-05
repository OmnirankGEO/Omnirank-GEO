"""
Migration 011: Social Studio 增量包(Addon)优化 V1

来源: .planning/phases/07-social-studio-subscription/ADDON_OPTIMIZATION_V1.md
日期: 2026-05-13
作者: Social-CTO-13.0

V3.1 旧增量包问题:
  - ADDON_PACKS 写死 api/subscription_api.py:740 Python dict
  - 只 3 SKU 覆盖 2 维度 (video_minutes + author_breakdown)
  - author_breakdown_lite vs _pro 同额度不同价 (¥99 vs ¥199)
  - 无 max_per_month 限制 / 无 admin 改价能力

本 migration:
  1. 新建 subscription_addons SSOT 表 (admin 后台可改价 / 加新包不发版)
  2. 新建 subscription_addon_purchases 购买记录表 (月度限购校验依据)
  3. seed 9 个新 SKU (覆盖 6 维 quotas)
  4. seed 3 个旧 SKU 兼容版 (is_active=false · 保留历史 reference)

幂等: 可重复跑 · 走 IF NOT EXISTS / ON CONFLICT DO NOTHING
"""

import logging

logger = logging.getLogger("GEO-Migration-011")


def run_migration():
    from db.connection import get_connection

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()

        steps = [
            # ============================================================
            # 1. subscription_addons — SSOT 表
            # ============================================================
            (
                "1.1 subscription_addons SSOT 表",
                """
                CREATE TABLE IF NOT EXISTS subscription_addons (
                    id              SERIAL PRIMARY KEY,
                    addon_id        VARCHAR(64)  UNIQUE NOT NULL,
                    target_quota    VARCHAR(64)  NOT NULL,
                    amount          INTEGER      NOT NULL,
                    yuan_price      NUMERIC(8,2) NOT NULL,
                    points_price    INTEGER      NOT NULL,
                    display_name    VARCHAR(128) NOT NULL,
                    description     TEXT,
                    best_for        TEXT,
                    min_plan_id     VARCHAR(32),
                    max_per_month   INTEGER      DEFAULT 5,
                    sort_order      INTEGER      DEFAULT 100,
                    is_active       BOOLEAN      DEFAULT TRUE,
                    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT addon_yuan_positive CHECK (yuan_price > 0),
                    CONSTRAINT addon_amount_positive CHECK (amount > 0)
                )
                """
            ),
            (
                "1.2 idx_addons_active_sort",
                "CREATE INDEX IF NOT EXISTS idx_addons_active_sort ON subscription_addons(is_active, sort_order)"
            ),
            (
                "1.3 idx_addons_target_quota",
                "CREATE INDEX IF NOT EXISTS idx_addons_target_quota ON subscription_addons(target_quota)"
            ),

            # ============================================================
            # 2. subscription_addon_purchases — 购买记录(月度限购依据)
            # ============================================================
            (
                "2.1 subscription_addon_purchases 购买记录表",
                """
                CREATE TABLE IF NOT EXISTS subscription_addon_purchases (
                    id              SERIAL PRIMARY KEY,
                    user_id         INTEGER      NOT NULL,
                    addon_id        VARCHAR(64)  NOT NULL,
                    subscription_id BIGINT       NOT NULL,
                    entitlement_id  BIGINT       NOT NULL,
                    yuan_paid       NUMERIC(8,2) NOT NULL,
                    points_deducted INTEGER      NOT NULL,
                    payment_method  VARCHAR(16)  NOT NULL DEFAULT 'points',
                    order_id        VARCHAR(128) UNIQUE NOT NULL,
                    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "2.2 idx_addon_purchases_user_month",
                "CREATE INDEX IF NOT EXISTS idx_addon_purchases_user_month ON subscription_addon_purchases(user_id, created_at)"
            ),
            (
                "2.3 idx_addon_purchases_addon_id",
                "CREATE INDEX IF NOT EXISTS idx_addon_purchases_addon_id ON subscription_addon_purchases(addon_id)"
            ),

            # ============================================================
            # 3. seed 9 个新 SKU (覆盖 6 维 quotas)
            # 价格规则: 单包 < 升级月卡差价 · 累加 ≥ 升级差价 (自然引导)
            # ============================================================

            # 3.1 video_minutes 2 包
            (
                "3.1 seed: video_minutes_60 (¥39)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('video_minutes_60', 'video_minutes', 60, 39, 5070,
                     '视频时长包 +60 分钟', '加 60 分钟视频处理时长 · 本月有效不滚存',
                     'personal 偶尔超视频时长 · 比升 growth (¥99) 划算',
                     'personal', 5, 110, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
            (
                "3.2 seed: video_minutes_240 (¥129)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('video_minutes_240', 'video_minutes', 240, 129, 16770,
                     '视频时长大包 +240 分钟', '加 240 分钟视频处理 · 适合视频密集创作者',
                     'growth↔agency 中间用户 · 比升 agency (¥299) 划算',
                     'personal', 3, 120, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # 3.3 pro_write 1 包
            (
                "3.3 seed: pro_write_30 (¥99)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('pro_write_30', 'pro_write', 30, 99, 12870,
                     '专业写稿包 +30 条', '加 30 条专业写稿额度 · 本月有效不滚存',
                     '写稿大户 · 月卡用完仍要发稿',
                     'personal', 3, 200, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # 3.4 rewrite 1 包
            (
                "3.4 seed: rewrite_15 (¥79)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('rewrite_15', 'rewrite', 15, 79, 10270,
                     '仿写包 +15 次', '加 15 次仿写额度 · 本月有效不滚存',
                     '仿写大户 · 频繁参考爆款改写',
                     'personal', 3, 300, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # 3.5 video_breakdown 1 包
            (
                "3.5 seed: video_breakdown_10 (¥59)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('video_breakdown_10', 'video_breakdown', 10, 59, 7670,
                     '拆视频包 +10 次', '加 10 次拆单视频额度 · 本月有效不滚存',
                     '选题密集 · 需要多参考样本',
                     'personal', 5, 400, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # 3.6 author_breakdown 2 包
            (
                "3.6 seed: author_breakdown_3 (¥99)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('author_breakdown_3', 'author_breakdown', 3, 99, 12870,
                     '拆博主包 +3 次', '加 3 次拆博主深度分析 · 本月有效不滚存',
                     'personal 想拆 1-3 个对标博主 · 比升 growth 划算',
                     'personal', 3, 500, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
            (
                "3.7 seed: author_breakdown_10 (¥259)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('author_breakdown_10', 'author_breakdown', 10, 259, 33670,
                     '拆博主大包 +10 次', '加 10 次拆博主深度分析 · 适合调研期密集使用',
                     '调研大户 · 比升 agency (¥299) 划算',
                     'personal', 2, 510, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # 3.8 review 2 包
            (
                "3.8 seed: review_5 (¥69)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('review_5', 'review', 5, 69, 8970,
                     '复盘包 +5 次', '加 5 次内容复盘 · 本月有效不滚存',
                     '复盘大户 · 月底数据回顾',
                     'personal', 3, 600, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
            (
                "3.9 seed: review_15 (¥179)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('review_15', 'review', 15, 179, 23270,
                     '复盘大包 +15 次', '加 15 次内容复盘 · 数据/复盘密集',
                     '数据驱动代理 · 比升 agency (¥299) 划算',
                     'personal', 2, 610, TRUE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),

            # ============================================================
            # 4. seed 3 个旧 SKU (is_active=false · 保留历史 reference)
            # 旧订单的 addon_id 仍在 subscription_usage_events 里 · 不删
            # ============================================================
            (
                "4.1 seed legacy: video_minutes_pack (¥49 / 100min · 已下架)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('video_minutes_pack', 'video_minutes', 100, 49, 6370,
                     '视频时长包 +100 分钟 (旧版)', '旧版包 · 已替换为 video_minutes_60 + _240',
                     NULL, NULL, 5, 9990, FALSE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
            (
                "4.2 seed legacy: author_breakdown_lite (¥99 / 5次 · 已下架)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('author_breakdown_lite', 'author_breakdown', 5, 99, 12870,
                     '拆博主 Lite 包 (旧版)', '旧版包 · 已替换为 author_breakdown_3',
                     NULL, NULL, 5, 9991, FALSE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
            (
                "4.3 seed legacy: author_breakdown_pro (¥199 / 5次 · 已下架)",
                """
                INSERT INTO subscription_addons
                    (addon_id, target_quota, amount, yuan_price, points_price,
                     display_name, description, best_for,
                     min_plan_id, max_per_month, sort_order, is_active)
                VALUES
                    ('author_breakdown_pro', 'author_breakdown', 5, 199, 25870,
                     '拆博主 Pro 包 (旧版)', '旧版包 · Pro vs Lite 同额度被废 · 替换为 author_breakdown_10',
                     NULL, NULL, 5, 9992, FALSE)
                ON CONFLICT (addon_id) DO NOTHING
                """
            ),
        ]

        success_count = 0
        for name, sql in steps:
            try:
                cur.execute(sql)
                logger.info(f"  ✓ {name}")
                success_count += 1
            except Exception as e:
                logger.error(f"  ✗ {name}: {e}")
                raise

        # 验证 seed 数量
        cur.execute("SELECT COUNT(*) AS n FROM subscription_addons")
        row = cur.fetchone()
        count = int(row["n"]) if row else 0
        logger.info(f"\n  📊 subscription_addons 表共 {count} 行 (期望 ≥ 12: 9 新 + 3 旧)")

        cur.execute("SELECT COUNT(*) AS n FROM subscription_addons WHERE is_active=TRUE")
        row = cur.fetchone()
        active_count = int(row["n"]) if row else 0
        logger.info(f"  📊 is_active=TRUE 共 {active_count} 行 (期望 = 9)")

        return {
            "success": True,
            "migration": "011_subscription_addons",
            "steps_passed": success_count,
            "total_rows": count,
            "active_rows": active_count,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = run_migration()
    print(result)
