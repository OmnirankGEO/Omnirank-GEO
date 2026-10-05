"""
代理合作协议签约服务(当前版本见 CURRENT_VERSION)

状态机:
- unsigned → signed(代理点签)
- unsigned → rejected(代理主动拒签)
- signed → expired(协议版本升级 · 自动迁移)
- rejected → unsigned(30 天冷静期后)· 用户重新点弹窗 fresh INSERT

签约 gate 用 FastAPI Depends 注入代理经营 endpoint · 不改函数体
未签 → 403 + 引导签约页 URL
"""
import hashlib
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger("GEO-V35-W3-Agreement")

# [r16/r4 2026-06-02] 经营功能协议改名「服务商经营功能协议」+ 服务商责任边界定义句 + 通篇服务商
# 属实质变更(协议主体称谓 + 责任定义)→ 升 v2.3 · 不覆盖 v2.2 · 全体服务商需重新签署(判定按 version · 见 is_agent_signed)
CURRENT_VERSION = "v2.4"
CURRENT_CONTENT_HASH = "14cfa7436b1628f9cf421c6805124bc9f2ad0b0b4bc4a46ea7b6d90483abc0a7"
REJECTION_COOLOFF_DAYS = 30

# [报价定价免责协议 2026-06-08] 普通用户(非服务商)开通"自设报价系数 / 单篇成本"功能前须签的免责协议。
#   复用本表 agent_factory_agreements + is_agent_signed/get_agreement_status/sign_agreement,仅 version 不同
#   (普通用户 user_id 存 agent_user_id 列·语义借用·零 schema)。
#   服务商(agent_level>=1)走 v2.3 经营功能协议、不需另签此份;判定见 auth/agreement_gate.require_pricing_authority。
PRICING_DISCLAIMER_VERSION = "pricing-disclaimer-v2"


def get_agreement_status(cursor, agent_user_id: int, version: str = CURRENT_VERSION) -> Dict[str, Any]:
    """读代理协议状态 · 返回 unsigned 占位若未记录"""
    cursor.execute("""
        SELECT id, version, status, signed_at, rejected_at, rejected_reason, created_at, content_hash
        FROM agent_factory_agreements
        WHERE agent_user_id = %s AND version = %s
    """, (agent_user_id, version))
    row = cursor.fetchone()
    if not row:
        return {
            "agent_user_id": agent_user_id,
            "version": version,
            "status": "unsigned",
            "signed_at": None,
            "rejected_at": None,
            "rejected_reason": None,
            "can_show_dialog": True,  # 从未交互过 · 可弹
        }
    d = dict(row) if isinstance(row, dict) else {
        "id": row[0], "version": row[1], "status": row[2],
        "signed_at": row[3], "rejected_at": row[4],
        "rejected_reason": row[5], "created_at": row[6], "content_hash": row[7],
    }
    if (
        version == CURRENT_VERSION
        and d.get("status") == "signed"
        and d.get("content_hash") != CURRENT_CONTENT_HASH
    ):
        d["status"] = "unsigned"
        d["signature_invalid"] = True
    # rejected 30 天内不再弹
    can_show = True
    if d["status"] == "rejected" and d.get("rejected_at"):
        rejected_at = d["rejected_at"]
        if isinstance(rejected_at, datetime):
            if datetime.now() < rejected_at + timedelta(days=REJECTION_COOLOFF_DAYS):
                can_show = False
    elif d["status"] == "signed":
        can_show = False
    d["can_show_dialog"] = can_show
    d["agent_user_id"] = agent_user_id
    return d


def is_agent_signed(cursor, agent_user_id: int, version: str = CURRENT_VERSION) -> bool:
    """快查代理是否已签 · gate 用"""
    if version == CURRENT_VERSION:
        cursor.execute("""
            SELECT 1 FROM agent_factory_agreements
            WHERE agent_user_id = %s AND version = %s AND status = 'signed'
              AND content_hash = %s
            LIMIT 1
        """, (agent_user_id, version, CURRENT_CONTENT_HASH))
    else:
        cursor.execute("""
            SELECT 1 FROM agent_factory_agreements
            WHERE agent_user_id = %s AND version = %s AND status = 'signed'
            LIMIT 1
        """, (agent_user_id, version))
    return cursor.fetchone() is not None


def is_pricing_disclaimer_signed(user_id) -> bool:
    """[报价定价免责协议 2026-06-08] 用户是否已签免责协议(自管连接·供 gate 外的只读判定复用,
    如 GET /quote-markup-preference 算 can_edit、PUT /profile 决定可否存系数)。任何异常一律 False(失败安全)。"""
    if not user_id:
        return False
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            return is_agent_signed(cur, int(user_id), PRICING_DISCLAIMER_VERSION)
    except Exception as exc:
        logger.warning("is_pricing_disclaimer_signed 失败 user=%s: %s", user_id, exc)
        return False


def sign_agreement(
    cursor,
    agent_user_id: int,
    version: str = CURRENT_VERSION,
    signed_ip: Optional[str] = None,
    signed_ua: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """代理签署协议 · UPSERT(rejected/unsigned → signed)"""
    if version not in {CURRENT_VERSION, PRICING_DISCLAIMER_VERSION}:
        raise ValueError("协议版本无效，请刷新页面后重新确认")
    if version == CURRENT_VERSION and content_hash != CURRENT_CONTENT_HASH:
        raise ValueError("协议正文版本已更新,请刷新页面后重新确认")
    if not content_hash:
        content_hash = hashlib.sha256(version.encode("utf-8")).hexdigest()  # 默认 hash version 字符串
    cursor.execute("""
        INSERT INTO agent_factory_agreements
            (agent_user_id, version, status, signed_at, signed_ip, signed_ua, content_hash)
        VALUES (%s, %s, 'signed', NOW(), %s, %s, %s)
        ON CONFLICT (agent_user_id, version) DO UPDATE SET
            status = 'signed',
            signed_at = NOW(),
            signed_ip = EXCLUDED.signed_ip,
            signed_ua = EXCLUDED.signed_ua,
            content_hash = EXCLUDED.content_hash,
            rejected_at = NULL,
            rejected_reason = NULL
        RETURNING id, signed_at
    """, (agent_user_id, version, signed_ip, signed_ua, content_hash))
    row = cursor.fetchone()
    logger.info(f"[agreement] agent={agent_user_id} signed version={version}")
    return {"signed": True, "version": version, "agreement_id": row["id"] if isinstance(row, dict) else row[0]}


def reject_agreement(
    cursor,
    agent_user_id: int,
    version: str = CURRENT_VERSION,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """代理主动拒签 · 30 天冷静期"""
    if version not in {CURRENT_VERSION, PRICING_DISCLAIMER_VERSION}:
        raise ValueError("协议版本无效，请刷新页面后重新确认")
    cursor.execute("""
        INSERT INTO agent_factory_agreements
            (agent_user_id, version, status, rejected_at, rejected_reason)
        VALUES (%s, %s, 'rejected', NOW(), %s)
        ON CONFLICT (agent_user_id, version) DO UPDATE SET
            status = 'rejected',
            rejected_at = NOW(),
            rejected_reason = EXCLUDED.rejected_reason
        RETURNING id
    """, (agent_user_id, version, reason or ""))
    logger.info(f"[agreement] agent={agent_user_id} rejected version={version} reason={reason}")
    return {"rejected": True, "cooloff_days": REJECTION_COOLOFF_DAYS}
