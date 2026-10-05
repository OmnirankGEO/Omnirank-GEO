"""
代理申请审核制（Partner Application Review）Schema

v1.1 新增三张表 + user_wallets.agent_verified 字段:
- agent_applications:  代理申请记录（身份证 AES 加密 + HMAC 指纹 + mask 三字段）
- agent_agreements:    协议签署记录（电子签证据链 + 文本哈希防篡改）
- agent_violations:    代理违规处理记录（三级制）

幂等: 通过 _migration_markers.marker='v1_1_partner_review_system' 标记。
启动时由 server.py 调用 init_partner_tables()。
"""

import logging
from db.connection import get_db

logger = logging.getLogger("GEO-Partner-Schema")


MIGRATION_MARKER = "v1_1_partner_review_system"


def init_partner_tables():
    """创建代理申请审核制相关表（幂等，可重复调用）"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _migration_markers (
                marker TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                note TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_applications (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),

                real_name TEXT NOT NULL,
                id_card_no_encrypted TEXT NOT NULL,
                id_card_no_hmac TEXT NOT NULL,
                id_card_no_mask TEXT NOT NULL,
                id_card_front_key TEXT NOT NULL,
                id_card_back_key TEXT NOT NULL,
                id_card_selfie_key TEXT,

                ocr_result JSONB,
                ocr_confidence NUMERIC(4,3),

                promotion_scenes JSONB,
                expected_monthly_customers TEXT,
                remark TEXT,

                status TEXT NOT NULL DEFAULT 'pending',
                rejection_reason TEXT,
                ai_risk_flags JSONB,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,

                ip_address TEXT,
                device_fingerprint TEXT,
                user_agent TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_app_user
            ON agent_applications(user_id, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_app_status_pending
            ON agent_applications(status)
            WHERE status IN ('pending', 'manual_review')
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_app_idcard_hmac
            ON agent_applications(id_card_no_hmac)
            WHERE status = 'approved'
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_agreements (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                application_id BIGINT REFERENCES agent_applications(id),
                version TEXT NOT NULL,
                signed_name TEXT NOT NULL,
                signed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                device_fingerprint TEXT,
                user_agent TEXT,
                agreement_text_hash TEXT NOT NULL,
                pdf_key TEXT,
                UNIQUE(user_id, version)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_agree_user
            ON agent_agreements(user_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_violations (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                severity TEXT NOT NULL,
                violation_type TEXT NOT NULL,
                evidence JSONB NOT NULL,
                action_taken TEXT NOT NULL,
                commission_frozen_amount BIGINT DEFAULT 0,
                commission_clawback_amount BIGINT DEFAULT 0,
                operator_id INTEGER,
                operator_notes TEXT,
                appeal_deadline TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_viol_user
            ON agent_violations(user_id, created_at DESC)
        """)

        cursor.execute("""
            ALTER TABLE user_wallets
            ADD COLUMN IF NOT EXISTS agent_verified BOOLEAN DEFAULT FALSE
        """)

        cursor.execute(
            "SELECT 1 FROM _migration_markers WHERE marker = %s",
            (MIGRATION_MARKER,),
        )
        already_marked = cursor.fetchone()

        if not already_marked:
            cursor.execute(
                """
                INSERT INTO _migration_markers (marker, note)
                VALUES (%s, %s)
                ON CONFLICT (marker) DO NOTHING
                """,
                (
                    MIGRATION_MARKER,
                    "agent_applications + agent_agreements + agent_violations "
                    "+ user_wallets.agent_verified (v1.1 partner review system)",
                ),
            )
            logger.info(
                "[Partner-Schema] v1.1 initial migration applied: "
                "3 tables + user_wallets.agent_verified"
            )
        else:
            logger.info("[Partner-Schema] v1.1 migration already applied, skip")
