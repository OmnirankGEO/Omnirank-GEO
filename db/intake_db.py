"""
intake_db — 客户资料补全邀请 (CTO-E · 2026-04-26)

两张表:
  - intake_tokens: 代理生成的邀请链接 token + 状态机
  - profile_update_submissions: 客户提交的资料 + 代理审核状态机

红线:
  - 客户提交不直接写 client_profiles, 必经代理 approve
  - token 默认 7 天 + 撤销 + 过期硬拦
  - 不引新依赖, 复用 db.connection.get_connection()
  - 不改公开 token 语义 (/q /s /m /portal /public/report)

token 状态机:
  active -> submitted (客户提交) | revoked (代理撤销) | expired (TTL 到)
  active 可 GET / ai-suggest / submit
  其他态全部 410

submission 状态机:
  pending_review -> approved | rejected | partially_approved
  本期不允许同 token 二次提交
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-Intake-DB")

# token 状态
TOKEN_STATUS_ACTIVE = "active"
TOKEN_STATUS_SUBMITTED = "submitted"
TOKEN_STATUS_REVOKED = "revoked"
TOKEN_STATUS_EXPIRED = "expired"

# submission 状态
SUBMISSION_STATUS_PENDING = "pending_review"
SUBMISSION_STATUS_APPROVED = "approved"
SUBMISSION_STATUS_REJECTED = "rejected"
SUBMISSION_STATUS_PARTIAL = "partially_approved"

DEFAULT_TOKEN_TTL_DAYS = 7
MAX_AI_SUGGEST_PER_TOKEN = 5

# P0-7: 社媒采访新增字段集合 (走 client_profiles.social_fields JSONB · 不挤占 canonical 列)
# CTO-13.0 2026-05-10 · 扩 3 个新字段(creator_archetype / preferred_script_structure / disliked_style)
# 原与社媒工具包的字段映射表(SOCIAL_FIELD_KEYS_EXTENDED)保持一致;该包已随开源 E3 删除,本表即为唯一来源
SOCIAL_FIELD_KEYS = (
    "persona_tone",
    "speaking_style",
    "customer_faq",
    "real_cases",
    "content_taboo",
    "target_audience",
    "product_selling_points",
    "closing_method",
    # CTO-13.0 P1 Day 1 (2026-05-10) 新增
    "creator_archetype",
    "preferred_script_structure",
    "disliked_style",
)

APPEND_ONLY_SOCIAL_FIELD_KEYS = {
    "customer_faq",
    "real_cases",
    "product_selling_points",
    "content_taboo",
}


def _social_fields_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_social_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("text") or value.get("content") or value.get("title") or value
    if isinstance(value, (list, tuple)):
        value = "；".join(_normalize_social_value(item) for item in value if item not in (None, "", " "))
    text = str(value or "").strip()
    return " ".join(text.split())[:500]


def _coerce_social_items(value: Any) -> List[Dict[str, Any]]:
    if value in (None, "", [], {}, "null"):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if parsed != value:
                return _coerce_social_items(parsed)
        except Exception:
            pass
        text = _normalize_social_value(value)
        return [{"value": text, "meta": {}}] if text else []
    if isinstance(value, dict):
        if "value" in value:
            text = _normalize_social_value(value)
            return [{"value": text, "meta": value.get("meta") or {}}] if text else []
        rows: List[Dict[str, Any]] = []
        for key, item in value.items():
            text = _normalize_social_value(item)
            if text:
                rows.append({"value": f"{key}: {text}", "meta": {}})
        return rows
    if isinstance(value, list):
        rows = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                text = _normalize_social_value(item)
                if text:
                    rows.append({"value": text, "meta": item.get("meta") or {}})
            else:
                text = _normalize_social_value(item)
                if text:
                    rows.append({"value": text, "meta": {}})
        return rows
    text = _normalize_social_value(value)
    return [{"value": text, "meta": {}}] if text else []


# ==================== 表初始化 ====================

def init_intake_tables() -> None:
    """创建两张表 + 索引(幂等). server.py startup 调用."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS intake_tokens (
                id SERIAL PRIMARY KEY,
                token VARCHAR(48) UNIQUE NOT NULL,
                brand_id INTEGER NOT NULL REFERENCES brands(id),
                diagnosis_id INTEGER,
                quote_id INTEGER,
                inviter_user_id INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                ai_suggest_count INTEGER DEFAULT 0,
                expires_at TIMESTAMPTZ NOT NULL,
                submitted_at TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ,
                revoked_reason TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_tokens_brand "
            "ON intake_tokens(brand_id, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_tokens_inviter "
            "ON intake_tokens(inviter_user_id, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_tokens_status "
            "ON intake_tokens(status, expires_at)"
        )

        # P0-7: 分步采访草稿列 (idempotent · 旧表也能升级)
        cursor.execute(
            "ALTER TABLE intake_tokens "
            "ADD COLUMN IF NOT EXISTS draft_payload_jsonb JSONB DEFAULT '{}'::jsonb"
        )
        cursor.execute(
            "ALTER TABLE intake_tokens "
            "ADD COLUMN IF NOT EXISTS step_index INTEGER DEFAULT 0"
        )
        cursor.execute(
            "ALTER TABLE intake_tokens "
            "ADD COLUMN IF NOT EXISTS answered_fields_jsonb JSONB DEFAULT '[]'::jsonb"
        )
        cursor.execute(
            "ALTER TABLE intake_tokens "
            "ADD COLUMN IF NOT EXISTS draft_updated_at TIMESTAMPTZ"
        )
        # P0-7: 社媒补充资料挂在 client_profiles.social_fields (一份 JSONB · 8 个 key)
        cursor.execute(
            "ALTER TABLE client_profiles "
            "ADD COLUMN IF NOT EXISTS social_fields JSONB DEFAULT '{}'::jsonb"
        )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profile_update_submissions (
                id SERIAL PRIMARY KEY,
                token_id INTEGER NOT NULL REFERENCES intake_tokens(id) ON DELETE CASCADE,
                brand_id INTEGER NOT NULL,
                submitted_by_name TEXT,
                submitted_by_phone TEXT,
                payload_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
                ai_suggested_jsonb JSONB,
                diff_jsonb JSONB,
                status VARCHAR(24) NOT NULL DEFAULT 'pending_review',
                review_notes TEXT,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMPTZ,
                approved_fields_jsonb JSONB,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_submissions_brand "
            "ON profile_update_submissions(brand_id, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_submissions_status "
            "ON profile_update_submissions(status, created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_intake_submissions_token "
            "ON profile_update_submissions(token_id)"
        )
        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM profile_update_submissions
                    GROUP BY token_id
                    HAVING COUNT(*) > 1
                ) THEN
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_submissions_token_unique
                    ON profile_update_submissions(token_id);
                END IF;
            END $$;
        """)

        conn.commit()
    finally:
        conn.close()
    logger.info("[Intake] intake_tokens / profile_update_submissions 表初始化完成")


# ==================== Token 操作 ====================

def _generate_token() -> str:
    """生成不可猜 token (~32 字符 URL-safe)."""
    return secrets.token_urlsafe(24)


def create_intake_token(
    brand_id: int,
    inviter_user_id: int,
    diagnosis_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
) -> Dict[str, Any]:
    """创建邀请 token. 同 brand 允许多个 active token."""
    token = _generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO intake_tokens
              (token, brand_id, diagnosis_id, quote_id, inviter_user_id, status, expires_at)
            VALUES (%s, %s, %s, %s, %s, 'active', %s)
            RETURNING id, token, brand_id, diagnosis_id, quote_id, inviter_user_id,
                      status, ai_suggest_count, expires_at, created_at
        """, (token, brand_id, diagnosis_id, quote_id, inviter_user_id, expires_at))
        row = cursor.fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def get_token_by_string(token: str) -> Optional[Dict[str, Any]]:
    """按 token 字符串查询. 不主动改状态."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM intake_tokens WHERE token = %s", (token,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_token_by_id(token_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM intake_tokens WHERE id = %s", (token_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_tokens_for_brand(brand_id: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM intake_tokens WHERE brand_id = %s ORDER BY created_at DESC
        """, (brand_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def revoke_token(token_id: int, reason: Optional[str] = None) -> bool:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE intake_tokens
            SET status = 'revoked',
                revoked_at = CURRENT_TIMESTAMP,
                revoked_reason = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'active'
        """, (reason, token_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def increment_ai_suggest_count(token_id: int) -> int:
    """原子 +1. 调用方必须先校验当前值 < cap."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE intake_tokens
            SET ai_suggest_count = ai_suggest_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING ai_suggest_count
        """, (token_id,))
        row = cursor.fetchone()
        conn.commit()
        return row["ai_suggest_count"] if row else 0
    finally:
        conn.close()


def save_intake_draft(
    token_id: int,
    step_index: int,
    draft_payload: Dict[str, Any],
    answered_fields: List[str],
) -> None:
    """P0-7: 保存采访进度 · 不写正式 submission · 客户刷新可恢复.
    红线: 只允许 active token 写;非 active 静默忽略让 _public_token_or_410 报 410.
    """
    if not isinstance(draft_payload, dict):
        draft_payload = {}
    if not isinstance(answered_fields, list):
        answered_fields = []
    # 去重 + 限长 (防 client 塞坏值)
    answered = []
    seen: set = set()
    for k in answered_fields:
        if isinstance(k, str) and k and k not in seen and len(k) < 64:
            answered.append(k)
            seen.add(k)
    answered = answered[:64]
    step_index = max(0, min(int(step_index or 0), 64))

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE intake_tokens
            SET draft_payload_jsonb = %s::jsonb,
                step_index = %s,
                answered_fields_jsonb = %s::jsonb,
                draft_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'active'
            """,
            (
                json.dumps(draft_payload, ensure_ascii=False),
                step_index,
                json.dumps(answered, ensure_ascii=False),
                token_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_intake_draft(token_id: int) -> Dict[str, Any]:
    """P0-7: 读采访草稿 · 给前端恢复进度用."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT draft_payload_jsonb, step_index, answered_fields_jsonb, draft_updated_at
            FROM intake_tokens
            WHERE id = %s
            """,
            (token_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {"draft_payload": {}, "step_index": 0, "answered_fields": []}
        draft_payload = row.get("draft_payload_jsonb") or {}
        if isinstance(draft_payload, str):
            try:
                draft_payload = json.loads(draft_payload)
            except Exception:
                draft_payload = {}
        answered = row.get("answered_fields_jsonb") or []
        if isinstance(answered, str):
            try:
                answered = json.loads(answered)
            except Exception:
                answered = []
        if not isinstance(answered, list):
            answered = []
        return {
            "draft_payload": draft_payload if isinstance(draft_payload, dict) else {},
            "step_index": int(row.get("step_index") or 0),
            "answered_fields": [str(x) for x in answered if isinstance(x, str)],
            "draft_updated_at": (
                row["draft_updated_at"].isoformat()
                if row.get("draft_updated_at") else None
            ),
        }
    finally:
        conn.close()


def update_profile_social_fields(profile_id: Any, social_fields: Dict[str, Any]) -> bool:
    """P0-7: 把客户 11 项社媒补充字段 merge 到 client_profiles.social_fields.
    用 JSONB || 操作符 merge · 客户新值覆盖旧值 · 旧 key 保留.
    不删除空值 (避免客户漏填一题就清掉之前的答案).
    返 True 表示有写入.

    复检修(BUG 165): client_profiles.id 真实是 TEXT 类型 · signature 改 int → Any
    SQL `WHERE id = %s` 接受 string/int 都能 match.
    """
    if not profile_id or not isinstance(social_fields, dict):
        return False
    # 只接受 SOCIAL_FIELD_KEYS 范围内非空值
    cleaned: Dict[str, Any] = {}
    append_values: Dict[str, Any] = {}
    for k in SOCIAL_FIELD_KEYS:
        v = social_fields.get(k)
        if v is None:
            continue
        if k in APPEND_ONLY_SOCIAL_FIELD_KEYS:
            append_values[k] = v
            continue
        if isinstance(v, str):
            s = v.strip()
            if s:
                cleaned[k] = s
        elif isinstance(v, list):
            arr = [x for x in v if x not in (None, "", " ")]
            if arr:
                cleaned[k] = arr
        elif isinstance(v, dict):
            if v:
                cleaned[k] = v
        else:
            cleaned[k] = v
    changed = False
    if not cleaned and not append_values:
        return False
    if cleaned:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE client_profiles
                SET social_fields = COALESCE(social_fields, '{}'::jsonb) || %s::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (json.dumps(cleaned, ensure_ascii=False), profile_id),
            )
            conn.commit()
            changed = cursor.rowcount > 0
        except Exception as e:
            conn.rollback()
            logger.warning(f"[Intake] update_profile_social_fields failed: {e}")
            return False
        finally:
            conn.close()
    for key, value in append_values.items():
        changed = append_profile_social_field_items(
            profile_id,
            key,
            _coerce_social_items(value),
            source="update_profile_social_fields",
        ) or changed
    return changed


def append_profile_social_field_items(
    profile_id: Any,
    key: str,
    items: List[Dict[str, Any]] | List[str] | Any,
    *,
    source: str = "unknown",
    limit: int = 30,
) -> bool:
    """Append list-like social_fields values without overwriting prior user facts."""
    if not profile_id or key not in SOCIAL_FIELD_KEYS:
        return False
    incoming = _coerce_social_items(items)
    if not incoming:
        return False
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT social_fields
            FROM client_profiles
            WHERE id = %s
            FOR UPDATE
            """,
            (profile_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False
        social_fields = _social_fields_json(row.get("social_fields"))
        existing_items = _coerce_social_items(social_fields.get(key))
        seen = {_normalize_social_value(item.get("value")) for item in existing_items if isinstance(item, dict)}
        merged = list(existing_items)
        for item in incoming:
            text = _normalize_social_value(item.get("value") if isinstance(item, dict) else item)
            if not text or text in seen:
                continue
            seen.add(text)
            meta = item.get("meta") if isinstance(item, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            merged.append({
                "value": text,
                "meta": {
                    **meta,
                    "source": source,
                    "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
                },
            })
        if len(merged) > max(1, int(limit or 30)):
            merged = merged[-max(1, int(limit or 30)):]
        social_fields[key] = merged
        cursor.execute(
            """
            UPDATE client_profiles
            SET social_fields = %s::jsonb,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (json.dumps(social_fields, ensure_ascii=False), profile_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logger.warning(f"[Intake] append_profile_social_field_items failed: {e}")
        return False
    finally:
        conn.close()


def is_token_actionable(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """判断 token 是否可用. 返 None 表示可用; 否则返 reason 字符串.

    注意: 不主动 update 过期态(避免读端写表), 由 sweep job 或下次 submit 拒绝."""
    if not record:
        return "token_not_found"
    status = record.get("status")
    if status == TOKEN_STATUS_REVOKED:
        return "revoked"
    if status == TOKEN_STATUS_SUBMITTED:
        return "submitted"
    if status == TOKEN_STATUS_EXPIRED:
        return "expired"
    expires_at = record.get("expires_at")
    if expires_at:
        # 兼容 timezone-naive / aware
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            return "expired"
    return None


def mark_token_submitted(token_id: int) -> None:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE intake_tokens
            SET status = 'submitted',
                submitted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (token_id,))
        conn.commit()
    finally:
        conn.close()


def expire_old_tokens() -> int:
    """定时把过期 active token 标 expired. 不依赖此 job 阻塞主流程."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE intake_tokens
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'active' AND expires_at < CURRENT_TIMESTAMP
        """)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ==================== Submission 操作 ====================

def insert_submission(
    token_id: int,
    brand_id: int,
    payload: Dict[str, Any],
    ai_suggested: Optional[Dict[str, Any]],
    diff: Optional[Dict[str, Any]],
    submitted_by_name: Optional[str] = None,
    submitted_by_phone: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status FROM intake_tokens WHERE id = %s FOR UPDATE",
            (token_id,),
        )
        token_row = cursor.fetchone()
        if not token_row:
            raise ValueError("intake token not found")
        if token_row.get("status") != TOKEN_STATUS_ACTIVE:
            raise ValueError("intake token is no longer active")
        cursor.execute(
            "SELECT 1 FROM profile_update_submissions WHERE token_id = %s LIMIT 1",
            (token_id,),
        )
        if cursor.fetchone() is not None:
            raise ValueError("intake token already submitted")
        cursor.execute("""
            INSERT INTO profile_update_submissions
              (token_id, brand_id, payload_jsonb, ai_suggested_jsonb, diff_jsonb,
               submitted_by_name, submitted_by_phone, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending_review')
            RETURNING *
        """, (
            token_id, brand_id,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(ai_suggested, ensure_ascii=False) if ai_suggested else None,
            json.dumps(diff, ensure_ascii=False) if diff else None,
            submitted_by_name, submitted_by_phone,
        ))
        row = cursor.fetchone()
        cursor.execute("""
            UPDATE intake_tokens
            SET status = 'submitted',
                submitted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (token_id,))
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_existing_submission_for_token(token_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM profile_update_submissions WHERE token_id = %s LIMIT 1",
            (token_id,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def get_submission(submission_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM profile_update_submissions WHERE id = %s",
            (submission_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_submissions_for_brand(
    brand_id: int, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT * FROM profile_update_submissions
                WHERE brand_id = %s AND status = %s
                ORDER BY created_at DESC
            """, (brand_id, status))
        else:
            cursor.execute("""
                SELECT * FROM profile_update_submissions
                WHERE brand_id = %s
                ORDER BY created_at DESC
            """, (brand_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def update_submission_review(
    submission_id: int,
    reviewer_id: int,
    status: str,
    approved_fields: Optional[List[str]] = None,
    review_notes: Optional[str] = None,
) -> bool:
    """只允许 pending_review -> approved/rejected/partially_approved. 二次审核被拒绝."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE profile_update_submissions
            SET status = %s,
                reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP,
                approved_fields_jsonb = %s,
                review_notes = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'pending_review'
        """, (
            status, reviewer_id,
            json.dumps(approved_fields, ensure_ascii=False) if approved_fields is not None else None,
            review_notes, submission_id,
        ))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
