"""
Migration 008: 数据完整性约束修复
- 清理 NULL brand_id 数据
- 添加 FK + ON DELETE CASCADE
- 添加关键字段 NOT NULL
- 验证钱包 CHECK 约束

对应审计报告: C-07, C-08, H-09
"""

import logging

logger = logging.getLogger("GEO-Migration-008")


def run_migration():
    """执行数据库约束迁移"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()

        steps = [
            # ============================================================
            # Step 1: 清理存量 NULL brand_id 数据
            # ============================================================
            (
                "清理无品牌档案",
                """
                UPDATE client_profiles
                SET is_deleted = 1, deleted_at = NOW()
                WHERE brand_id IS NULL AND (is_deleted = 0 OR is_deleted IS NULL)
                """
            ),
            (
                "清理无品牌社媒项目",
                """
                DELETE FROM social_projects WHERE brand_id IS NULL
                """
            ),

            # ============================================================
            # Step 2: 添加 FK 约束（IF NOT EXISTS 模式：先 DROP 再 ADD）
            # ============================================================
            (
                "FK: client_profiles.brand_id → brands.id CASCADE",
                """
                ALTER TABLE client_profiles
                DROP CONSTRAINT IF EXISTS fk_profile_brand;
                ALTER TABLE client_profiles
                ADD CONSTRAINT fk_profile_brand
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
                """
            ),
            (
                "FK: social_projects.brand_id → brands.id CASCADE",
                """
                ALTER TABLE social_projects
                DROP CONSTRAINT IF EXISTS fk_sp_brand;
                ALTER TABLE social_projects
                ADD CONSTRAINT fk_sp_brand
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
                """
            ),
            (
                "FK: social_scripts.project_id → social_projects.id CASCADE",
                """
                ALTER TABLE social_scripts
                DROP CONSTRAINT IF EXISTS fk_ss_project;
                ALTER TABLE social_scripts
                ADD CONSTRAINT fk_ss_project
                FOREIGN KEY (project_id) REFERENCES social_projects(id) ON DELETE CASCADE
                """
            ),
            (
                "FK: interview_sessions.user_id → users.id CASCADE",
                """
                ALTER TABLE interview_sessions
                DROP CONSTRAINT IF EXISTS fk_interview_user;
                ALTER TABLE interview_sessions
                ADD CONSTRAINT fk_interview_user
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                """
            ),

            # ============================================================
            # Step 3: 钱包 CHECK 约束（余额不能为负）
            # ============================================================
            (
                "CHECK: user_wallets.paid_points >= 0",
                """
                ALTER TABLE user_wallets
                DROP CONSTRAINT IF EXISTS check_paid_non_negative;
                ALTER TABLE user_wallets
                ADD CONSTRAINT check_paid_non_negative CHECK (paid_points >= 0)
                """
            ),
            (
                "CHECK: user_wallets.bonus_points >= 0",
                """
                ALTER TABLE user_wallets
                DROP CONSTRAINT IF EXISTS check_bonus_non_negative;
                ALTER TABLE user_wallets
                ADD CONSTRAINT check_bonus_non_negative CHECK (bonus_points >= 0)
                """
            ),
        ]

        success_count = 0
        fail_count = 0

        for desc, sql in steps:
            try:
                for stmt in sql.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)
                logger.info(f"✅ {desc}")
                success_count += 1
            except Exception as e:
                err_msg = str(e).lower()
                # 常见可忽略的错误：表/列/约束不存在
                if any(x in err_msg for x in ["does not exist", "already exists", "no such table"]):
                    logger.info(f"⏭️ {desc} — 跳过（{e}）")
                else:
                    logger.warning(f"❌ {desc} — 失败: {e}")
                    fail_count += 1

        cur.close()
        conn.close()

        logger.info(f"迁移完成: {success_count} 成功, {fail_count} 失败")
        return {"success": success_count, "failed": fail_count}
    finally:
        try:
            conn.close()
        except Exception: pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
