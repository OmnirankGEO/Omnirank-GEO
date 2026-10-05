"""
Migration 012: chat 附件计费 soft log 表(v1.5)

来源: 老板拍 v1.5 chat 附件计费方案(2026-05-21)
作者: Social-CTO-13.0

背景:
  类 Kimi chat 附件预备栏 v1.3 已上线 prod main(ea552487)· 但 4 endpoint 全 0 计费白嫖:
    /chat-attachment/parse-file · /chat-attachment/parse-url
    /chat-attachment/list       · /chat-attachment/delete

v1.5 计费策略(Codex 5 阻断收尾):
  - 图片 vision → 扣 profile_polish(light_chat 30 积分 ≈ ¥0.23)
  - 视频 URL → 扣 video_asr(按真实分钟数 · 估 1 分钟)
  - PDF(走 _extract_pdf_with_vision)→ 扣 profile_polish
  - doc/txt/md/local-video → 0 成本不扣
  - endpoint 入口预检查(fail-closed)+ bg task charge-on-actual + race-safe(用户 × 删不扣)

本 migration 加一张 cost_events soft log 表:
  - 每次扣费(成功/失败)写一条 · 供运营 / 财务复盘真实成本分布
  - 不影响业务(写失败 logger.warning · 不抛异常)
  - 不做硬 daily quota 拦截(老板 #7:不能中断附件体验 · 极端滥用 ≥ 100/天才拦)

幂等: 可重复跑 · 走 IF NOT EXISTS
"""

import logging

logger = logging.getLogger("GEO-Migration-012")


def run_migration():
    from db.connection import get_connection

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()

        steps = [
            # ============================================================
            # chat_attachment_cost_events — 软 log · 不硬拦
            # ============================================================
            (
                "1.1 chat_attachment_cost_events 表",
                """
                CREATE TABLE IF NOT EXISTS chat_attachment_cost_events (
                    id              BIGSERIAL    PRIMARY KEY,
                    user_id         INTEGER      NOT NULL,
                    session_id      VARCHAR(64)  NOT NULL,
                    attachment_id   VARCHAR(64)  NOT NULL,
                    feature_code    VARCHAR(64)  NOT NULL,
                    video_minutes   INTEGER      DEFAULT 0,
                    deducted_amount INTEGER      DEFAULT 0,
                    deduct_source   VARCHAR(32),
                    error           TEXT,
                    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                )
                """
            ),
            (
                "1.2 idx_chat_att_cost_user_created",
                "CREATE INDEX IF NOT EXISTS idx_chat_att_cost_user_created ON chat_attachment_cost_events(user_id, created_at DESC)"
            ),
            (
                "1.3 idx_chat_att_cost_session",
                "CREATE INDEX IF NOT EXISTS idx_chat_att_cost_session ON chat_attachment_cost_events(session_id, created_at DESC)"
            ),
            (
                "1.4 idx_chat_att_cost_feature",
                "CREATE INDEX IF NOT EXISTS idx_chat_att_cost_feature ON chat_attachment_cost_events(feature_code, created_at DESC)"
            ),
        ]

        success_count = 0
        for desc, sql in steps:
            try:
                cur.execute(sql)
                logger.info(f"✅ {desc}: OK")
                success_count += 1
            except Exception as e:
                logger.error(f"❌ {desc}: {e}")
                raise

        # 验证
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_name='chat_attachment_cost_events'"
        )
        row = cur.fetchone()
        table_exists = bool(row and (row["cnt"] if isinstance(row, dict) else row[0]))

        logger.info(f"Migration 012 完成 · steps_ok={success_count}/{len(steps)} · table_exists={table_exists}")
        return {
            "success": True,
            "steps_ok": success_count,
            "steps_total": len(steps),
            "table_exists": table_exists,
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
