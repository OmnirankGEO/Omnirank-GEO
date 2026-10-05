"""Admin-only user identity and relationship governance service.

All mutations use one PostgreSQL transaction for row locks, expected-version CAS,
business data, the dedicated evidence ledger, and the existing generic audit log.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from db.connection import get_connection, get_db


SCOPES = (
    "business_identity", "commercial_binding", "channel_relationship",
    "platform_access", "password_security", "wallet_adjustment",
)
_SCOPE_LABELS = {
    "business_identity": "调整业务身份",
    "commercial_binding": "调整商业服务归属",
    "channel_relationship": "调整服务商渠道关系",
    "platform_access": "调整平台管理员权限",
    "password_security": "重置账号密码",
    "wallet_adjustment": "校正用户算力",
}
_BINDING_SOURCE_LABELS = {
    "qrcode": "服务二维码绑定",
    "ref_link": "服务链接绑定",
    "invite_code": "服务邀请码绑定",
    "historical_referral_backfill": "历史邀请记录核验补齐",
    "admin_manual": "管理员人工设置",
}
_LEGACY_ROLE_LABELS = {
    "social_ops": ("普通用户兼容角色", "active_compatibility"),
    "geo_writer": ("GEO 编辑（历史兼容）", "read_only_legacy"),
    "sales": ("销售顾问（历史兼容）", "read_only_legacy"),
    "geo_user_basic": ("GEO 用户（历史兼容）", "read_only_legacy"),
    "geo_agent_full": ("GEO 服务商（历史兼容）", "read_only_legacy"),
    "finance_reviewer": ("财务审核员（内部兼容）", "active_compatibility"),
}


class GovernanceNotFound(Exception):
    pass


class GovernanceVersionConflict(Exception):
    def __init__(self, scope: str, current_version: int):
        super().__init__(f"{scope} version conflict: {current_version}")
        self.scope = scope
        self.current_version = int(current_version)


class GovernanceValidationError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        # Optional machine-readable payload so a refusal can name the exact
        # non-zero obstacle and where it is handled.  A refusal that only says
        # "there are dependencies" is a dead end for the operator.
        self.details: Dict[str, Any] = dict(details or {})


def _value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _is_active(value: Any) -> bool:
    if value is None:
        return True
    return bool(int(value)) if isinstance(value, (int, str)) else bool(value)


def _identity(agent_level: Any) -> str:
    return "service_provider" if int(agent_level or 0) >= 1 else "ordinary_user"


def is_platform_direct_service_user(user_id: int) -> bool:
    """Single source for terminal protection of the platform-direct principal."""
    configured = (os.getenv("PLATFORM_DIRECT_SERVICE_USER_ID") or "").strip()
    try:
        return bool(configured) and int(configured) == int(user_id)
    except (TypeError, ValueError):
        return False


def _enqueue_account_change(
    cur,
    *,
    user_id: int,
    event_type: str,
    scope: str,
    version: int,
    status: str,
    summary: str,
) -> None:
    """Append the account notification to the caller's governance transaction."""
    from services.notification_events import RecipientKind
    from services.notification_outbox import enqueue_notification_event

    enqueue_notification_event(
        cur,
        event_type=event_type,
        business_id=f"user:{int(user_id)}:{scope}:{int(version)}",
        terminal_state="changed",
        recipient_user_id=int(user_id),
        recipient_kind=RecipientKind.USER,
        facts={
            "business_no": f"ACCOUNT-{int(version)}",
            "status": status,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": summary,
        },
    )


def _assert_identity_ssot(cur, user_id: Optional[int] = None) -> None:
    params: List[Any] = []
    predicate = ""
    if user_id is not None:
        predicate = "AND u.id=%s"
        params.append(int(user_id))
    cur.execute(
        f"""SELECT u.id FROM users u
            LEFT JOIN user_wallets w ON w.user_id=u.id
            WHERE (w.user_id IS NULL OR w.agent_level IS NULL) {predicate}
            ORDER BY u.id LIMIT 1""",
        params,
    )
    missing = cur.fetchone()
    if missing:
        missing_id = int(missing["id"] if isinstance(missing, dict) else missing[0])
        raise GovernanceValidationError(
            "BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
            f"用户 #{missing_id} 的业务身份真相缺失，已停止读取，需先完成数据核验",
        )


def _ensure_subject_wallet_row(cur, user_id: int) -> int:
    """[#139 返修三] **被治理主体**的钱包行:没有就补,然后返回 agent_level。

    🔴 一处实现。`change_business_identity` 本来就是这么做的
       (补行 + `FOR UPDATE` 读),`change_commercial_binding` 却在同样的输入上
       **直接拒**。同样是治理写动作、同样的主体,处置相反 —— 而两边都没错到
       会有人发现的程度,因为各自的判据都是绿的。

    🔴 为什么补行是对的,而**读**路径不许猜:
       读的时候把缺行显示成「普通用户」是**猜**(可能只是钱包没建出来),
       所以列表/详情标 `unverified`;
       写的时候补出来的 `agent_level=0` 不是猜,是**把默认值落成事实** ——
       新账号本来就是普通用户,直到被提升。
       两者不矛盾:读不许替系统回答,写负责把答案确定下来。

    🔴 counterparty(关系对手方)**不适用**本函数,保持严格:
       没有钱包行的人本来就不可能是服务商,那一侧的拒绝是对的。
    """
    cur.execute(
        "INSERT INTO user_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
        (int(user_id),),
    )
    cur.execute(
        "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets"
        " WHERE user_id=%s FOR UPDATE",
        (int(user_id),),
    )
    row = cur.fetchone()
    if not row:
        # 补了还读不到 = 真异常(并发删?),仍然 fail-closed —— 不猜。
        raise GovernanceValidationError(
            "BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
            "用户业务身份暂时无法确认,未执行治理动作",
        )
    return int(row["agent_level"] or 0)

def _actor(cur, user_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    _assert_identity_ssot(cur, int(user_id))
    cur.execute(
        """
        SELECT u.id, u.username, u.display_name, u.is_active,
               to_jsonb(u)->>'company' AS company,
               COALESCE(w.agent_level, 0) AS agent_level,
               pac.service_account_code, pac.channel_account_code
        FROM users u
        LEFT JOIN user_wallets w ON w.user_id = u.id
        LEFT JOIN public_account_codes pac ON pac.user_id = u.id
        WHERE u.id = %s
        """,
        (int(user_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "user_id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "is_active": _is_active(row.get("is_active")),
        "company": row.get("company"),
        "business_identity": _identity(row.get("agent_level")),
        "service_code": row.get("service_account_code"),
        "channel_code": row.get("channel_account_code"),
    }


def _version_map(cur, user_id: int) -> Dict[str, int]:
    versions = {scope: 1 for scope in SCOPES}
    versions["account_status"] = 1
    cur.execute(
        """SELECT scope, version FROM admin_user_governance_versions
           WHERE subject_user_id = %s""",
        (int(user_id),),
    )
    for row in cur.fetchall() or []:
        if row["scope"] in versions:
            versions[row["scope"]] = int(row["version"])
    cur.execute(
        """SELECT version FROM admin_governance_subject_versions
           WHERE subject_kind='user' AND subject_id=%s AND capability='account_status'""",
        (int(user_id),),
    )
    status_version = cur.fetchone()
    if status_version:
        versions["account_status"] = int(status_version["version"])
    return versions


def _lock_version(cur, user_id: int, scope: str, expected_version: int) -> int:
    cur.execute(
        """
        INSERT INTO admin_user_governance_versions(subject_user_id, scope, version)
        VALUES (%s, %s, 1)
        ON CONFLICT(subject_user_id, scope) DO NOTHING
        """,
        (int(user_id), scope),
    )
    cur.execute(
        """SELECT version FROM admin_user_governance_versions
           WHERE subject_user_id=%s AND scope=%s FOR UPDATE""",
        (int(user_id), scope),
    )
    row = cur.fetchone()
    current = int(row["version"])
    if current != int(expected_version):
        raise GovernanceVersionConflict(scope, current)
    return current


def _advance_version(cur, user_id: int, scope: str, current: int) -> int:
    next_version = current + 1
    cur.execute(
        """
        UPDATE admin_user_governance_versions
        SET version=%s, updated_at=NOW()
        WHERE subject_user_id=%s AND scope=%s AND version=%s
        """,
        (next_version, int(user_id), scope, current),
    )
    if cur.rowcount != 1:
        raise GovernanceVersionConflict(scope, current)
    return next_version


def _snapshot_summary(scope: str, snapshot: Dict[str, Any]) -> str:
    if scope == "business_identity":
        return "服务商" if snapshot.get("business_identity") == "service_provider" else "普通用户"
    if scope == "commercial_binding":
        provider = snapshot.get("commercial_provider_user_id")
        return f"服务商用户 #{provider}" if provider else "平台直营"
    if scope == "channel_relationship":
        upstream = snapshot.get("channel_upstream_user_id")
        if not upstream:
            return "平台根渠道"
        multiplier = int(snapshot.get("cost_multiplier_bps") or 10000)
        return f"上游服务商 #{upstream}，进货系数 {Decimal(multiplier) / Decimal(10000):.4f}"
    if scope == "password_security":
        return "下次登录需修改密码" if snapshot.get("password_state") == "reset_required" else "密码正常"
    if scope == "wallet_adjustment":
        return (
            f"付费算力 {int(snapshot.get('paid_points') or 0)}，"
            f"赠送算力 {int(snapshot.get('bonus_points') or 0)}"
        )
    return "内部管理员" if snapshot.get("platform_access") == "administrator" else "普通平台权限"


def _write_audits(
    cur,
    *,
    subject_user_id: int,
    scope: str,
    operator_user_id: int,
    operator_username: Optional[str],
    request_id: str,
    reason: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    version_before: int,
    version_after: int,
    ip_address: Optional[str],
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    cur.execute(
        """
        INSERT INTO admin_user_governance_audits(
            subject_user_id, scope, operator_user_id, operator_username,
            request_id, reason, before_snapshot, after_snapshot, evidence_jsonb,
            version_before, version_after, ip_address
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            int(subject_user_id), scope, int(operator_user_id), operator_username,
            request_id, reason, Json(before), Json(after), Json(evidence or {}),
            version_before, version_after, ip_address,
        ),
    )
    cur.execute(
        """
        INSERT INTO audit_logs(
            user_id, username, action, module, entity_type, entity_id,
            summary, before_snapshot, after_snapshot, ip_address
        ) VALUES (%s,%s,'update','admin_user_governance','user',%s,%s,%s,%s,%s)
        """,
        (
            int(operator_user_id), operator_username, int(subject_user_id),
            f"{_SCOPE_LABELS[scope]}：{_snapshot_summary(scope, before)} → "
            f"{_snapshot_summary(scope, after)}；原因：{reason}",
            json.dumps(before, ensure_ascii=False, default=str),
            json.dumps(after, ensure_ascii=False, default=str),
            ip_address,
        ),
    )


def record_external_commercial_binding_change(
    cur,
    *,
    subject_user_id: int,
    operator_user_id: int,
    operator_username: Optional[str],
    request_id: str,
    reason: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    ip_address: Optional[str],
    evidence: Optional[Dict[str, Any]] = None,
) -> int:
    """Advance the shared CAS and audit an already-locked relationship writer.

    The caller must hold the commercial subject advisory lock and keep this call in
    the same transaction as its relationship mutation.
    """
    cur.execute(
        """INSERT INTO admin_user_governance_versions(subject_user_id,scope,version)
           VALUES (%s,'commercial_binding',1)
           ON CONFLICT(subject_user_id,scope) DO NOTHING""",
        (int(subject_user_id),),
    )
    cur.execute(
        """SELECT version FROM admin_user_governance_versions
           WHERE subject_user_id=%s AND scope='commercial_binding' FOR UPDATE""",
        (int(subject_user_id),),
    )
    version_row = cur.fetchone()
    current = int(version_row["version"] if isinstance(version_row, dict) else version_row[0])
    next_version = _advance_version(cur, subject_user_id, "commercial_binding", current)
    _write_audits(
        cur,
        subject_user_id=subject_user_id,
        scope="commercial_binding",
        operator_user_id=operator_user_id,
        operator_username=operator_username,
        request_id=request_id,
        reason=reason,
        before=before,
        after=after,
        version_before=current,
        version_after=next_version,
        ip_address=ip_address,
        evidence=evidence,
    )
    return next_version


# 补录审计的 `original_bound_at` 口径 —— 写入侧与判定侧必须用**同一个** to_char 格式,
# 因此只留这一份常量。🔴 故意用文本比较而不是 ::timestamp 强转:
# 强转遇到一条手写的畸形 evidence 会让整个管理员用户列表查询报错,
# 判据本身不该成为可被一行脏数据打爆的东西。
BINDING_BACKFILL_TIME_FORMAT = 'YYYY-MM-DD HH24:MI:SS.US'


def _relation_attention_sql() -> str:
    """「这条 admin_manual 归属没有审计凭证」的判定。

    两条豁免分支,都要求审计**指名道姓**对上这条绑定
    (`evidence_jsonb->>'binding_id'` + `after_snapshot->>'commercial_provider_user_id'`):

      1. **原生**:审计与绑定同期(±5 秒)—— 走
         `PUT /users/{id}/commercial-service-binding` 自然满足。
      2. **补录**:审计显式带 `backfill=true` + `original_bound_at`,且
         `original_bound_at` 与该绑定当前的 `bound_at` **逐字符相等**。

    🔴 为什么要第 2 条(工单 P1-5):历史绑定的 `bound_at` 在 06-06 / 07-11 / 07-15,
       今天补录的审计**永远**落在 ±5 秒窗外 —— 只有第 1 条时,
       「补录后灯灭」与「数据里能看出是补录」在现判据下无法同时成立。

    🔴 判别标准(红线):**裸插一条无审计的 admin_manual 绑定,仍然亮灯**。
       两条分支都要求存在配对审计行,没有任何一条是"按 id / 时间加白名单"。
       且 `original_bound_at` 锚死当前 `bound_at`:绑定一旦被重建(bound_at 变),
       旧的补录豁免立即失效,灯重新亮起来。
    """
    return f"""
        cab.binding_source = 'admin_manual'
        AND NOT EXISTS (
            SELECT 1 FROM admin_user_governance_audits uga
            WHERE uga.subject_user_id=u.id AND uga.scope='commercial_binding'
              AND uga.evidence_jsonb->>'binding_id' = cab.id::text
              AND uga.after_snapshot->>'commercial_provider_user_id' = cab.agent_user_id::text
              AND uga.created_at BETWEEN cab.bound_at - INTERVAL '5 seconds'
                                     AND cab.bound_at + INTERVAL '5 seconds'
        )
        AND NOT EXISTS (
            SELECT 1 FROM admin_user_governance_audits uga
            WHERE uga.subject_user_id=u.id AND uga.scope='commercial_binding'
              AND uga.evidence_jsonb->>'binding_id' = cab.id::text
              AND uga.after_snapshot->>'commercial_provider_user_id' = cab.agent_user_id::text
              AND uga.evidence_jsonb->>'backfill' = 'true'
              AND uga.evidence_jsonb->>'original_bound_at'
                  = to_char(cab.bound_at, '{BINDING_BACKFILL_TIME_FORMAT}')
        )
    """


def list_admin_users(
    *, page: int = 1, page_size: int = 30, search: Optional[str] = None,
    identity: Optional[str] = None, attention_only: bool = False,
) -> Dict[str, Any]:
    page = max(1, int(page))
    page_size = min(100, max(10, int(page_size)))
    where: List[str] = []
    params: List[Any] = []
    if search:
        where.append(
            "(u.username ILIKE %s OR u.display_name ILIKE %s OR COALESCE(u.phone,'') ILIKE %s)"
        )
        needle = f"%{search.strip()}%"
        params.extend([needle, needle, needle])
    if identity == "ordinary_user":
        where.append("COALESCE(w.agent_level,0)=0")
    elif identity == "service_provider":
        where.append("COALESCE(w.agent_level,0)>=1")
    if attention_only:
        where.append(f"({_relation_attention_sql()})")
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 [#139 · 2026-09-07] 这里原来是 `_assert_identity_ssot(cur)` ——
        #    **不带 user_id 就是全表扫描**,任一用户没有 `user_wallets` 行就
        #    `raise` 掉整个列表。生产现象:Owner 2026-09-07 打开管理端用户设置,
        #    整页报「用户 #174 的业务身份真相缺失,已停止读取」。
        #
        #    🔴 一个坏行拖垮整页 —— 而列表本身根本不需要那个前提:
        #    下面的查询已经是 `LEFT JOIN user_wallets` + `COALESCE(w.agent_level,0)`,
        #    没有钱包行的用户照样能渲染。守卫挡的是**它自己不需要的东西**。
        #
        #    改成**按行降级**:缺行的那一行标 unverified + attention,列表照常 200。
        #    🔴 写动作仍然 fail-closed(`_actor` :152 与详情 :985 的按用户守卫
        #    原样保留)—— 读可以降级,改身份/改绑定不行。
        base = f"""
            FROM users u
            LEFT JOIN user_wallets w ON w.user_id=u.id
            LEFT JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
            {where_sql}
        """
        cur.execute(f"SELECT COUNT(*) AS count {base}", params)
        total = int(cur.fetchone()["count"])
        cur.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.phone, u.is_active,
                   u.created_at, u.last_active_at,
                   COALESCE(w.agent_level,0) AS agent_level,
                   COALESCE(w.paid_points,0) + COALESCE(w.bonus_points,0) AS total_points,
                   EXISTS(
                       SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                       WHERE ur.user_id=u.id AND r.name='admin'
                   ) AS is_admin,
                   (SELECT COUNT(*) FROM customer_agent_bindings bc WHERE bc.agent_user_id=u.id) AS customer_count,
                   (SELECT COUNT(*) FROM brands b WHERE b.owner_user_id=u.id) AS brand_count,
                   ({_relation_attention_sql()}) AS needs_attention,
                   (w.user_id IS NULL) AS wallet_missing,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='business_identity'),1) AS identity_version,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='commercial_binding'),1) AS binding_version,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='channel_relationship'),1) AS channel_version,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='platform_access'),1) AS access_version,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='password_security'),1) AS password_version,
                   COALESCE((SELECT version FROM admin_user_governance_versions
                             WHERE subject_user_id=u.id AND scope='wallet_adjustment'),1) AS wallet_version
                   ,COALESCE((SELECT version FROM admin_governance_subject_versions
                             WHERE subject_kind='user' AND subject_id=u.id
                               AND capability='account_status'),1) AS account_status_version
            {base}
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        users = []
        for row in cur.fetchall() or []:
            # 🔴 [#139] 没有钱包行 ⇒ 这一行的业务身份**是未知,不是普通用户**。
            #    `COALESCE(w.agent_level,0)` 会把缺行读成 0 = 普通用户 ——
            #    那是**具体而错误**的答案:管理员会以为已经核过了。
            #    标成 unverified 并进 attention 分母,让它在列表里显眼。
            wallet_missing = bool(row.get("wallet_missing"))
            business_identity = ("unverified" if wallet_missing
                                 else _identity(row["agent_level"]))
            attention = bool(row["needs_attention"]) or wallet_missing
            users.append({
                "user_id": int(row["id"]),
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "phone": row.get("phone"),
                "is_active": _is_active(row.get("is_active")),
                "business_identity": business_identity,
                "platform_access": "administrator" if row["is_admin"] else "standard",
                "total_points": int(row["total_points"] or 0),
                "customer_count": int(row["customer_count"] or 0),
                "brand_count": int(row["brand_count"] or 0),
                # The list label is an operational summary, not a second relationship
                # resolver. Service providers belong to the channel supply chain even
                # when they do not also have an ordinary-customer binding of their own.
                "service_mode": (
                    "service_provider" if business_identity == "service_provider"
                    else "platform_direct"
                ),
                "needs_attention": attention,
                # 🔴 [#139] 报文要告诉管理员**去哪核验**。原来整页那句
                #    「需先完成数据核验」具体、却没说核什么、在哪核。
                "attention_label": (
                    "该账号由组织邀请或管理员创建,尚未初始化钱包 —— "
                    "本人登录一次即自动补齐,或由平台执行钱包初始化"
                    if wallet_missing else
                    ("旧人工归属缺少直接审计" if attention else None)
                ),
                "created_at": row.get("created_at"),
                "last_active_at": row.get("last_active_at"),
                "versions": {
                    "business_identity": int(row["identity_version"]),
                    "commercial_binding": int(row["binding_version"]),
                    "channel_relationship": int(row["channel_version"]),
                    "platform_access": int(row["access_version"]),
                    "password_security": int(row["password_version"]),
                    "wallet_adjustment": int(row["wallet_version"]),
                    "account_status": int(row["account_status_version"]),
                },
            })
        # Fill service mode without exposing provider identities in the list DTO.
        user_ids = [item["user_id"] for item in users]
        if user_ids:
            cur.execute(
                "SELECT customer_user_id FROM customer_agent_bindings WHERE customer_user_id=ANY(%s)",
                (user_ids,),
            )
            bound_ids = {int(r["customer_user_id"]) for r in cur.fetchall() or []}
            for item in users:
                if item["business_identity"] == "ordinary_user":
                    item["service_mode"] = (
                        "service_provider" if item["user_id"] in bound_ids else "platform_direct"
                    )
            # [补充工单 2026-08-06] 账号来源要在**列表行**上也看得出来。
            # Owner 原话:「就是没有显示是否是用户在团队与席位里面自建的账号」——
            # 他看的是列表,不是详情页;详情页修好之后两处仍然不一致:
            # 点进去写着「组织操作员」,退出来又变回一个看不出来历的普通用户。
            # 🔴 走集合版,与上面 bound_ids 同样是一次查询;判定口径与详情页同源。
            origins = _account_origins_bulk(cur, user_ids)
            for item in users:
                origin = origins.get(item["user_id"]) or {"kind": "self_signup", "organization": None}
                item["account_origin"] = origin["kind"]
                # 列表行只需要团队名(§4.1);完整 organization 对象留给详情页。
                item["organization_name"] = (origin["organization"] or {}).get("name")
        else:
            # 空页也要把字段带上,前端不必判 undefined。
            for item in users:
                item["account_origin"] = "self_signup"
                item["organization_name"] = None
        return {
            "success": True,
            "users": users,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }
    finally:
        conn.close()


def _evidence_for_binding(cur, user_id: int, binding: Dict[str, Any]) -> Dict[str, Any]:
    if binding.get("binding_source") != "admin_manual":
        return {
            "status": "complete",
            "label": "绑定来源记录已留存",
            "happened_at": binding.get("bound_at"),
        }
    cur.execute(
        """
        SELECT uga.*, COALESCE(u.display_name, uga.operator_username) AS operator_name
        FROM admin_user_governance_audits uga
        LEFT JOIN users u ON u.id=uga.operator_user_id
        WHERE uga.subject_user_id=%s AND uga.scope='commercial_binding'
          AND uga.evidence_jsonb->>'binding_id'=%s
          AND uga.after_snapshot->>'commercial_provider_user_id'=%s
          AND uga.created_at BETWEEN %s::timestamptz - INTERVAL '5 seconds'
                                 AND %s::timestamptz + INTERVAL '5 seconds'
        ORDER BY uga.created_at DESC LIMIT 1
        """,
        (
            int(user_id), str(binding["id"]), str(binding["agent_user_id"]),
            binding.get("bound_at"), binding.get("bound_at"),
        ),
    )
    direct = cur.fetchone()
    if direct:
        return {
            "status": "complete", "label": "管理员操作证据完整",
            "operator_user_id": int(direct["operator_user_id"]),
            "operator_name": direct.get("operator_name"), "reason": direct["reason"],
            "request_id": direct["request_id"], "happened_at": direct["created_at"],
        }
    cur.execute(
        """
        SELECT al.id, al.user_id, al.username, al.summary, al.created_at,
               COALESCE(u.display_name, al.username) AS operator_name
        FROM audit_logs al LEFT JOIN users u ON u.id=al.user_id
        WHERE al.entity_id=%s
          AND COALESCE(al.module,'') <> 'admin_user_governance'
          AND al.created_at BETWEEN %s::timestamp - INTERVAL '10 minutes'
                                AND %s::timestamp + INTERVAL '10 minutes'
          AND (al.summary ILIKE '%%归属%%' OR al.summary ILIKE '%%绑定%%')
        ORDER BY ABS(EXTRACT(EPOCH FROM (al.created_at - %s::timestamp))) ASC
        LIMIT 1
        """,
        (int(user_id), binding.get("bound_at"), binding.get("bound_at"), binding.get("bound_at")),
    )
    related = cur.fetchone()
    if related:
        return {
            "status": "related", "label": "发现时间关联审计（非直接外键证据）",
            "operator_user_id": related.get("user_id"),
            "operator_name": related.get("operator_name"),
            "reason": related.get("summary"), "happened_at": related.get("created_at"),
        }
    if binding.get("admin_override_user_id") and binding.get("admin_override_at"):
        operator = _actor(cur, int(binding["admin_override_user_id"]))
        return {
            "status": "related", "label": "操作人与时间已留存，但缺少直接原因证据",
            "operator_user_id": int(binding["admin_override_user_id"]),
            "operator_name": operator["display_name"] if operator else None,
            "happened_at": binding["admin_override_at"],
        }
    return {
        "status": "incomplete", "label": "旧人工归属缺少直接操作凭证",
        "happened_at": binding.get("bound_at"),
    }


# [工单 2026-08-06 §3] 注册必须带邀请码这道闸的上线日期。
# 在它之前建的号本来就不可能有注册邀请归属,把它们标成「需核实」是纯噪声
# (实测 63 个老号)—— 那会让真正需要核实的那一个淹没在里面。
# 🔴 这个日期来自工单 §3.1 的取证,不是猜的;闸的开关是 SIGNUP_REQUIRE_REFERRAL(api/auth_api.py)。
_SIGNUP_REFERRAL_GATE_SINCE = "2026-06-10"


# 🔴 账号来源判定的**唯一口径**。详情页(_account_origin)与用户列表(_account_origins_bulk)
#   都只能通过这里判,不许各写一套 —— 两处分叉就是下一次「点进去说是组织操作员、
#   退出来又变回普通用户」的事故(补充工单 2026-08-06 §3 点名)。
#
# 取哪一行 membership:同一个人可能有多条(换过团队 / 被移除过)。
#   口径 = **优先 active,其次最早加入**。单个版用 ORDER BY ... LIMIT 1,
#   集合版用 DISTINCT ON 配同样的 ORDER BY —— 二者语义等价,这是它们同源的关键。
_MEMBERSHIP_PICK_ORDER = "(m.status = 'active') DESC, m.joined_at ASC"


def _classify_membership_row(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把一行 membership(可能为 None)判成账号来源。**纯函数,不碰数据库。**

    纯函数是有意的:它让 detail / list 两条路的判定可以被同一组测试钉死,
    也让「组织所有者不算操作员」这条边界只存在于一个地方。
    """
    if not row:
        return {"kind": "self_signup", "organization": None}
    row = dict(row)
    # 🔴 组织**所有者**是自己注册后建的团队,不是被邀请进来的 —— 他仍走自助注册那条口径,
    #    否则会把「老板自己没有邀请归属」这个真事实一起藏掉。
    if row.get("is_owner"):
        return {"kind": "self_signup", "organization": None}
    return {
        "kind": "organization_member",
        "organization": {
            "organization_id": int(row["organization_id"]) if row.get("organization_id") else None,
            "name": row.get("org_name"),
            "owner_user_id": int(row["owner_user_id"]) if row.get("owner_user_id") else None,
            "membership_status": row.get("status"),
            "role_id": int(row["role_id"]) if row.get("role_id") is not None else None,
            "joined_at": row.get("joined_at"),
        },
    }


def _account_origin(cur, user_id: int) -> Dict[str, Any]:
    """判断这个账号是**怎么来的**(详情页用 · 单个)。

    🔴 工单 §3 的真 bug:admin 用户治理页把**组织操作员账号**也塞进「注册邀请归属」面板,
    显示「无邀请记录 / 没有邀请归属记录」却不说明这是操作员账号、本来就不该有注册归属。
    结果一个完全正常的账号看起来像注册闸失守,直接触发了一次误报 P0。

    全系统只有三处 `INSERT INTO users`:db/auth_db.py 的注册与 admin 建号,
    以及 services/organization_onboarding.py 的**组织邀请接受**流程。
    第三条路进来的人不经过注册,自然没有 referral_links —— 这是设计,不是异常。
    """
    cur.execute(
        f"""
        SELECT m.organization_id, m.status, m.is_owner, m.role_id, m.joined_at,
               o.name AS org_name, o.owner_user_id
        FROM organization_memberships m
        LEFT JOIN organizations o ON o.id = m.organization_id
        WHERE m.user_id = %s
        ORDER BY {_MEMBERSHIP_PICK_ORDER}
        LIMIT 1
        """,
        (user_id,),
    )
    origin = _classify_membership_row(cur.fetchone())
    org = origin["organization"]
    if org:
        # 详情页要展示邀请人(团队所有者)本人,这一次额外查询只发生在单个用户上。
        # 🔴 列表版**故意不查**它 —— 列表行只需要团队名,查了就变成 N+1(补充工单 §3)。
        owner_id = org.pop("owner_user_id", None)
        org["owner"] = _actor(cur, int(owner_id)) if owner_id else None
    return origin


def _account_origins_bulk(cur, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """一次查出一批用户的账号来源(用户列表用 · **O(1) 次查询**)。

    🔴 补充工单 2026-08-06 §3 的硬约束:列表页一屏几十行,逐行调 `_account_origin`
    就是 N+1,用户治理页会明显变慢。所以这里用 `= ANY(%s)` 一次取回,
    再交给**同一个** `_classify_membership_row` 判定。

    🔴 `DISTINCT ON (m.user_id)` 配 `ORDER BY m.user_id, {_MEMBERSHIP_PICK_ORDER}`,
    与单个版的 `ORDER BY {_MEMBERSHIP_PICK_ORDER} LIMIT 1` 语义等价 ——
    多条 membership 时两边挑中的是同一行,这是「同源」的另一半。

    空入参直接返回空字典:🔴 不许退化成「不带 WHERE 全表扫」,那会把所有人算成组织成员。
    """
    if not user_ids:
        return {}
    cur.execute(
        f"""
        SELECT DISTINCT ON (m.user_id)
               m.user_id, m.organization_id, m.status, m.is_owner, m.role_id, m.joined_at,
               o.name AS org_name, o.owner_user_id
        FROM organization_memberships m
        LEFT JOIN organizations o ON o.id = m.organization_id
        WHERE m.user_id = ANY(%s)
        ORDER BY m.user_id, {_MEMBERSHIP_PICK_ORDER}
        """,
        ([int(u) for u in user_ids],),
    )
    rows = {int(r["user_id"]): dict(r) for r in (cur.fetchall() or [])}
    return {int(uid): _classify_membership_row(rows.get(int(uid))) for uid in user_ids}


def _relationships(cur, user: Dict[str, Any], versions: Dict[str, int]) -> Dict[str, Any]:
    user_id = int(user["id"])
    notices: List[Dict[str, str]] = []
    origin = _account_origin(cur, user_id)
    cur.execute(
        """
        SELECT referrer_id, created_at FROM referral_links
        WHERE referred_id=%s AND level=1
        ORDER BY created_at ASC, referrer_id ASC LIMIT 2
        """,
        (user_id,),
    )
    referral_rows = list(cur.fetchall() or [])
    legacy_pointer = user.get("referred_by_agent_id")
    if referral_rows:
        primary = referral_rows[0]
        inviter = _actor(cur, primary["referrer_id"])
        registration = {
            "present": True, "inviter": inviter,
            "source": "referral_links", "registered_at": primary.get("created_at"),
            "legacy_pointer_user_id": int(legacy_pointer) if legacy_pointer else None,
            "evidence": {"status": "complete", "label": "注册邀请记录已留存", "happened_at": primary.get("created_at")},
        }
        if not inviter:
            notices.append({
                "code": "REGISTRATION_ACTOR_MISSING", "severity": "critical",
                "title": "邀请来源账号不存在", "detail": "邀请记录仍保留；系统未猜测或替换邀请人。",
            })
        if len(referral_rows) > 1:
            notices.append({
                "code": "MULTIPLE_REGISTRATION_ATTRIBUTIONS", "severity": "critical",
                "title": "注册邀请归属存在多条现役记录", "detail": "需要人工核验；系统未自动选择或订正。",
            })
        if legacy_pointer and int(legacy_pointer) != int(primary["referrer_id"]):
            notices.append({
                "code": "LEGACY_REFERRAL_POINTER_MISMATCH", "severity": "warning",
                "title": "历史邀请快查字段与邀请记录不一致", "detail": "页面以邀请记录展示事实，不自动改写历史字段。",
            })
    elif legacy_pointer:
        inviter = _actor(cur, int(legacy_pointer))
        registration = {
            "present": True, "inviter": inviter,
            "source": "legacy_user_pointer", "registered_at": None,
            "legacy_pointer_user_id": int(legacy_pointer),
            "evidence": {"status": "related", "label": "仅有历史快查字段，缺少邀请记录时间线"},
        }
        notices.append({
            "code": "LEGACY_REFERRAL_ONLY", "severity": "warning",
            "title": "邀请归属仅有历史兼容证据",
            "detail": "缺少完整注册邀请时间线，系统不会据此自动改写当前商业服务关系。",
        })
        if not inviter:
            notices.append({
                "code": "LEGACY_REGISTRATION_ACTOR_MISSING", "severity": "critical",
                "title": "历史邀请账号不存在", "detail": "仅展示原始用户 ID，不自动订正。",
            })
    elif origin["kind"] == "organization_member":
        # 三态之一:组织操作员账号。它**本来就不该有**注册邀请归属 —— 从组织邀请那条路进来的。
        org = origin["organization"]
        owner = (org or {}).get("owner")
        who = f"{owner['display_name']}（用户 #{owner['user_id']}）" if owner else "团队所有者"
        registration = {
            "present": False, "inviter": None, "source": "organization_invite",
            "registered_at": (org or {}).get("joined_at"),
            "legacy_pointer_user_id": None,
            "evidence": {
                "status": "not_required",
                "label": f"组织操作员账号 · 由团队「{(org or {}).get('name') or '—'}」邀请加入,不经过注册流程",
                "happened_at": (org or {}).get("joined_at"),
            },
        }
        notices.append({
            "code": "ACCOUNT_FROM_ORGANIZATION_INVITE", "severity": "info",
            "title": "组织操作员账号 · 无注册邀请归属属正常",
            "detail": f"该账号由团队「{(org or {}).get('name') or '—'}」的 {who} 邀请创建,"
                      "走的是组织邀请流程,不经过注册闸,因此没有注册邀请归属记录。",
        })
    else:
        # 三态之三:自助注册但**真的**没有邀请归属。
        # 🔴 这一类必须仍然看得出是需要关注的 —— 工单 §3.4 明确要求别把真信号一起吞掉。
        #    但闸上线前建的老号不算异常(那时本来就不要求),否则 63 个老号会把唯一那个真的淹掉。
        created_at = user.get("created_at")
        created_str = str(created_at)[:10] if created_at else ""
        pre_gate = bool(created_str and created_str < _SIGNUP_REFERRAL_GATE_SINCE)
        if pre_gate:
            registration = {
                "present": False, "inviter": None, "source": "none", "registered_at": None,
                "legacy_pointer_user_id": None,
                "evidence": {
                    "status": "not_required",
                    "label": f"注册邀请闸({_SIGNUP_REFERRAL_GATE_SINCE})上线前建的老账号,当时不要求邀请归属",
                },
            }
        else:
            registration = {
                "present": False, "inviter": None, "source": "none", "registered_at": None,
                "legacy_pointer_user_id": None,
                "evidence": {
                    "status": "incomplete",
                    "label": "自助注册账号却没有邀请归属记录 —— 需人工核实来历",
                },
            }
            notices.append({
                "code": "SELF_SIGNUP_WITHOUT_REFERRAL", "severity": "warning",
                "title": "自助注册账号没有邀请归属",
                "detail": f"该账号建于注册邀请闸({_SIGNUP_REFERRAL_GATE_SINCE})上线之后,"
                          "却查不到任何邀请归属记录。可能是运营手工建号或后台建号;"
                          "系统不猜测、不自动补记,请人工核实来历。",
            })
    registration["account_origin"] = origin["kind"]
    registration["organization"] = origin["organization"]

    cur.execute(
        """
        SELECT id, customer_user_id, agent_user_id, binding_source, source_token, bound_at,
               dispute_status, dispute_note, admin_override_user_id, admin_override_at
        FROM customer_agent_bindings WHERE customer_user_id=%s
        """,
        (user_id,),
    )
    binding_row = cur.fetchone()
    if binding_row:
        binding = dict(binding_row)
        evidence = _evidence_for_binding(cur, user_id, binding)
        provider_actor = _actor(cur, binding["agent_user_id"])
        commercial = {
            "mode": "service_provider", "provider": provider_actor,
            "binding_id": int(binding["id"]), "binding_source": binding["binding_source"],
            "binding_source_label": _BINDING_SOURCE_LABELS.get(binding["binding_source"], "历史来源"),
            "bound_at": binding.get("bound_at"), "relationship_version": versions["commercial_binding"],
            "dispute_status": binding.get("dispute_status"), "evidence": evidence,
        }
        if not provider_actor:
            notices.append({
                "code": "COMMERCIAL_PROVIDER_MISSING", "severity": "critical",
                "title": "商业服务账号不存在", "detail": "绑定记录仍保留；系统已停止推断承接方，请人工核验。",
            })
        elif not provider_actor["is_active"] or provider_actor["business_identity"] != "service_provider":
            notices.append({
                "code": "COMMERCIAL_PROVIDER_INVALID", "severity": "critical",
                "title": "商业服务账号状态异常",
                "detail": "当前承接账号已停用或不再是服务商；系统未自动改绑。",
            })
        if evidence["status"] != "complete":
            notices.append({
                "code": "ADMIN_MANUAL_EVIDENCE_INCOMPLETE", "severity": "warning",
                "title": "历史人工绑定证据不完整",
                "detail": "缺少同时直接证明操作人和原因的记录；关联审计仅作线索，不得补造。",
            })
        if binding.get("dispute_status") == "pending":
            notices.append({
                "code": "COMMERCIAL_BINDING_DISPUTED", "severity": "critical",
                "title": "商业服务归属正在争议处理中", "detail": "本页面拒绝覆盖，请先走专用争议裁决流程。",
            })
    else:
        commercial = {
            "mode": "platform_direct", "provider": None, "binding_id": None,
            "binding_source": None, "binding_source_label": "平台直营",
            "bound_at": None, "relationship_version": versions["commercial_binding"],
            "dispute_status": None,
            "evidence": {"status": "not_required", "label": "无商业绑定，由平台直营承接"},
        }

    level = int(user.get("agent_level") or 0)
    if level < 1:
        channel = {"mode": "not_applicable", "upstream": None, "relationship_version": None,
                   "cost_multiplier_bps": None, "effective_from": None, "reason": None}
    else:
        cur.execute(
            """
            SELECT upstream_channel_account_id, relationship_version, cost_multiplier_bps,
                   effective_from, reason
            FROM channel_pricing_relationships
            WHERE buyer_dealer_id=%s AND status='active' AND effective_to IS NULL LIMIT 1
            """,
            (user_id,),
        )
        rel = cur.fetchone()
        upstream_actor = _actor(cur, rel["upstream_channel_account_id"]) if rel else None
        channel = ({
            "mode": "upstream_channel", "upstream": upstream_actor,
            "relationship_version": rel["relationship_version"],
            "cost_multiplier_bps": int(rel["cost_multiplier_bps"]),
            "effective_from": rel.get("effective_from"), "reason": rel.get("reason"),
        } if rel else {
            "mode": "platform_root", "upstream": None, "relationship_version": None,
            "cost_multiplier_bps": None, "effective_from": None, "reason": None,
        })
        if rel and (
            not upstream_actor or not upstream_actor["is_active"]
            or upstream_actor["business_identity"] != "service_provider"
        ):
            notices.append({
                "code": "CHANNEL_UPSTREAM_INVALID", "severity": "critical",
                "title": "渠道上游账号状态异常",
                "detail": f"关系记录指向用户 #{rel['upstream_channel_account_id']}；未自动选择替代上游。",
            })

    dual = bool(registration["present"] and binding_row)
    if dual:
        notices.append({
            "code": "REGISTRATION_AND_SERVICE_RECORDED", "severity": "info",
            "title": "邀请来源与当前服务关系均已留痕",
            "detail": "合格服务商邀请可建立首次服务关系；后续管理员调整只改变当前服务关系，不改写注册来源。",
        })
    return {
        "registration": registration, "commercial": commercial, "channel": channel,
        "dual_relationships_present": dual,
        "dual_relationships_label": "来源与当前服务关系均已记录" if dual else None,
        "notices": notices,
    }


def _pricing_summary(cur, user: Dict[str, Any], relationships: Dict[str, Any]) -> Dict[str, Any]:
    identity = _identity(user.get("agent_level"))
    commercial = relationships["commercial"]
    cur.execute(
        """SELECT quote_markup_override, sku_markup_override, wholesale_numer, wholesale_denom
           FROM agent_pricing_overrides WHERE agent_user_id=%s""",
        (int(user["id"]),),
    )
    override = cur.fetchone()
    notes = []
    if override:
        if override.get("quote_markup_override") is not None or override.get("sku_markup_override") is not None:
            notes.append("存在服务商专属销售规则")
        if override.get("wholesale_numer") is not None and override.get("wholesale_denom") is not None:
            notes.append("存在服务商专属进货规则")
    if identity == "service_provider":
        return {
            "customer_pricing_route": "该服务商面向客户的现役销售价目",
            "procurement_pricing_route": "服务商进货价目表",
            "settlement_route": "服务商库存与渠道结算链",
            "pricing_source": "平台定价中心 + 服务商专属规则（如有）",
            "special_pricing_note": "；".join(notes) if notes else None,
        }
    if commercial["mode"] == "service_provider":
        provider = commercial.get("provider") or {}
        route = f"当前服务商 {provider.get('display_name', '')} 的客户价目"
        settlement = "当前服务商商业服务与结算链"
    else:
        route = "平台直营客户价目"
        settlement = "平台直营结算链"
    return {
        "customer_pricing_route": route,
        "procurement_pricing_route": "不适用（普通用户不进货）",
        "settlement_route": settlement,
        "pricing_source": "现役定价中心",
        "special_pricing_note": None,
    }


def get_admin_user_detail(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 [#139 · 2026-09-07 返修] 详情页也按行降级。
        #    原来这里对缺钱包的用户 raise ⇒ 管理员**打不开 #174 的详情**,
        #    也就到不了那个能补齐钱包的写动作(`change_business_identity` 里
        #    本来就有 `INSERT INTO user_wallets ... ON CONFLICT DO NOTHING`)。
        #    ⇒ 守卫把人挡在了修复入口之外 —— 拒读没有让任何人更安全。
        #    `_actor` 对**操作者**的守卫保留(:152),那条守的是另一件事。
        cur.execute(
            """
            SELECT u.id, u.username, u.display_name, u.phone, u.is_active,
                   u.permission_version, u.must_change_password, u.created_at,
                   u.last_login_at, u.last_active_at,
                   to_jsonb(u)->>'company' AS company,
                   NULLIF(to_jsonb(u)->>'referred_by_agent_id','')::integer AS referred_by_agent_id,
                   COALESCE(w.agent_level,0) AS agent_level,
                   COALESCE(w.paid_points,0) AS paid_points,
                   COALESCE(w.bonus_points,0) AS bonus_points,
                   COALESCE(w.total_recharged,0) AS total_recharged,
                   EXISTS(SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                          WHERE ur.user_id=u.id AND r.name='admin') AS is_admin,
                   (w.user_id IS NULL) AS wallet_missing
            FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id
            WHERE u.id=%s
            """,
            (int(user_id),),
        )
        row = cur.fetchone()
        if not row:
            raise GovernanceNotFound(f"user {user_id}")
        user = dict(row)
        versions = _version_map(cur, int(user_id))
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE agent_user_id=%s", (int(user_id),))
        customer_count = int(cur.fetchone()["c"])
        cur.execute("SELECT COUNT(*) AS c FROM brands WHERE owner_user_id=%s", (int(user_id),))
        brand_count = int(cur.fetchone()["c"])
        # 🔴 [#139] 缺钱包 ⇒ 身份是**未知**,不是 COALESCE 出来的「普通用户」。
        wallet_missing = bool(user.get("wallet_missing"))
        identity = "unverified" if wallet_missing else _identity(user["agent_level"])
        relationships = _relationships(cur, user, versions)

        cur.execute(
            """
            SELECT cab.customer_user_id, cab.bound_at, u.username, u.display_name
            FROM customer_agent_bindings cab JOIN users u ON u.id=cab.customer_user_id
            WHERE cab.agent_user_id=%s ORDER BY cab.bound_at DESC LIMIT 100
            """,
            (int(user_id),),
        )
        clients = [{
            "customer_user_id": int(r["customer_user_id"]), "username": r["username"],
            "display_name": r["display_name"] or r["username"], "bound_at": r.get("bound_at"),
        } for r in cur.fetchall() or []]
        cur.execute(
            """
            SELECT id, name, to_jsonb(brands)->>'industry' AS industry,
                   to_jsonb(brands)->>'status' AS status
            FROM brands WHERE owner_user_id=%s ORDER BY id DESC LIMIT 100
            """,
            (int(user_id),),
        )
        brands = [{"brand_id": int(r["id"]), "name": r["name"], "industry": r.get("industry"),
                   "status": r.get("status")} for r in cur.fetchall() or []]

        cur.execute(
            """SELECT id, amount_cents, payment_status, created_at, paid_at
               FROM recharge_orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 20""",
            (int(user_id),),
        )
        status_labels = {"pending": "待支付", "paid": "已支付", "failed": "失败", "refunded": "已退款"}
        orders = [{
            "order_id": str(r["id"]),
            "amount_yuan": format(Decimal(int(r["amount_cents"] or 0)) / Decimal(100), ".2f"),
            "status_label": status_labels.get(r.get("payment_status"), r.get("payment_status") or "未知"),
            "created_at": r.get("created_at"), "paid_at": r.get("paid_at"),
        } for r in cur.fetchall() or []]
        cur.execute(
            """SELECT id, type, amount, description, created_at
               FROM point_transactions WHERE user_id=%s ORDER BY created_at DESC LIMIT 30""",
            (int(user_id),),
        )
        transactions = [{
            "transaction_id": int(r["id"]),
            "direction_label": "支出" if int(r["amount"] or 0) < 0 or r.get("type") == "consume" else "收入",
            "points": int(r["amount"] or 0), "description": r.get("description"),
            "created_at": r.get("created_at"),
        } for r in cur.fetchall() or []]

        cur.execute(
            """SELECT r.id, r.name, r.display_name FROM roles r
               JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=%s ORDER BY r.id""",
            (int(user_id),),
        )
        legacy_roles = []
        for role in cur.fetchall() or []:
            if role["name"] == "admin":
                continue
            label, status = _LEGACY_ROLE_LABELS.get(
                role["name"], (f"{role['display_name']}（历史兼容）", "read_only_legacy")
            )
            legacy_roles.append({
                "role_id": int(role["id"]), "internal_name": role["name"],
                "historical_label": label, "compatibility_status": status,
            })
        cur.execute(
            """
            SELECT uga.id, uga.scope, uga.operator_user_id,
                   COALESCE(u.display_name, uga.operator_username) AS operator_name,
                   uga.request_id, uga.reason, uga.before_snapshot, uga.after_snapshot,
                   uga.version_before, uga.version_after, uga.created_at
            FROM admin_user_governance_audits uga
            LEFT JOIN users u ON u.id=uga.operator_user_id
            WHERE uga.subject_user_id=%s ORDER BY uga.created_at DESC LIMIT 100
            """,
            (int(user_id),),
        )
        logs = []
        for audit in cur.fetchall() or []:
            before = dict(audit["before_snapshot"] or {})
            after = dict(audit["after_snapshot"] or {})
            logs.append({
                "audit_id": int(audit["id"]), "scope": audit["scope"],
                "action_label": _SCOPE_LABELS[audit["scope"]],
                "operator_user_id": int(audit["operator_user_id"]),
                "operator_name": audit.get("operator_name"), "request_id": audit["request_id"],
                "reason": audit["reason"], "before_summary": _snapshot_summary(audit["scope"], before),
                "after_summary": _snapshot_summary(audit["scope"], after),
                "version_before": int(audit["version_before"]),
                "version_after": int(audit["version_after"]), "created_at": audit["created_at"],
            })

        # 🔴 [P0-C · WO_INVREL_P0_HOTFIX §9] 详情页也要知道这条归属在不在亮灯 ——
        #    列表页早就有(list_users:498),详情页没有,于是"补录凭证"按钮在详情页
        #    根本无从判断该不该出现。这里用**判定侧同一段 SQL**,不另写第二套口径:
        #    判据、处置(backfill_commercial_binding_audit)、这里,三处共用一份。
        cur.execute(
            f"""SELECT ({_relation_attention_sql()}) AS needs_attention
                FROM users u JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
                WHERE u.id=%s""",
            (int(user_id),),
        )
        attention_row = cur.fetchone()
        needs_attention = bool(attention_row and attention_row["needs_attention"]) or wallet_missing

        overview = {
            "needs_attention": needs_attention,
            "attention_label": (
                "该账号由组织邀请或管理员创建,尚未初始化钱包 —— "
                "本人登录一次即自动补齐,或在本页执行任一身份/绑定变更时自动补齐"
                if wallet_missing else
                ("旧人工归属缺少直接审计" if needs_attention else None)
            ),
            "user_id": int(user["id"]), "username": user["username"],
            "display_name": user["display_name"] or user["username"], "phone": user.get("phone"),
            "company": user.get("company"), "is_active": _is_active(user.get("is_active")),
            "business_identity": identity,
            "business_identity_label": (
                "尚未初始化钱包(身份待核)" if identity == "unverified"
                else ("服务商" if identity == "service_provider" else "普通用户")
            ),
            "platform_access": "administrator" if user["is_admin"] else "standard",
            "platform_access_label": "内部管理员" if user["is_admin"] else "无管理员权限",
            "total_points": int(user["paid_points"] or 0) + int(user["bonus_points"] or 0),
            "paid_points": int(user["paid_points"] or 0), "bonus_points": int(user["bonus_points"] or 0),
            "total_recharged_points": int(user["total_recharged"] or 0),
            "customer_count": customer_count, "brand_count": brand_count,
            "created_at": user.get("created_at"), "last_login_at": user.get("last_login_at"),
            "last_active_at": user.get("last_active_at"), "versions": versions,
        }
        return {
            "success": True, "overview": overview, "relationships": relationships,
            "pricing_and_settlement": _pricing_summary(cur, user, relationships),
            "clients_and_brands": {"clients": clients, "brands": brands},
            "wallet_and_billing": {
                "paid_points": overview["paid_points"], "bonus_points": overview["bonus_points"],
                "total_points": overview["total_points"],
                "total_recharged_points": overview["total_recharged_points"],
                "recent_orders": orders, "recent_transactions": transactions,
            },
            "permissions_and_security": {
                "platform_access": overview["platform_access"],
                "account_status_label": "正常" if overview["is_active"] else "已停用",
                "must_change_password": bool(user.get("must_change_password")),
                "permission_version": int(user.get("permission_version") or 1),
                "legacy_roles": legacy_roles,
            },
            "operation_logs": logs,
        }
    finally:
        conn.close()


def get_platform_direct_readiness() -> Dict[str, Any]:
    """平台直营就绪度 + **全平台**零售目录零条目体检。

    两件事合在一个响应里,是因为管理员真正要回答的是同一个问题:"现在有没有人买不了东西"。
    平台直营空掉只是其中最严重的一种(它是所有无归属客户的唯一售卖通道),
    但任何服务方空掉都等于那批客户断供 —— 07-27 那次断了 47 个客户 2 天,零告警。
    """
    from services.commercial_service_routing import platform_direct_readiness
    from services.retail_catalog_health import (
        empty_published_retail_scopes,
        empty_retail_catalog_alert,
    )

    readiness = dict(platform_direct_readiness())
    try:
        with get_db() as conn:
            scopes = empty_published_retail_scopes(conn.cursor())
    except Exception:  # noqa: BLE001 — 体检失败不得把治理页打成 500
        scopes = []
    readiness["empty_retail_scopes"] = scopes
    readiness["empty_retail_alert"] = empty_retail_catalog_alert(scopes)
    return readiness


def _lock_dealer_resale_downgrade_dependencies(cur, provider_id: int) -> Dict[str, int]:
    """Lock every live resale/refund obligation before a provider downgrade."""
    required = {
        "dealer_resale_global_settings", "dealer_inventory_lots", "dealer_resale_orders",
        "dealer_consumer_sales", "dealer_resale_profit_ledger", "consumer_refund_cases",
        "refund_work_orders", "dealer_resale_fulfillment_plans",
        "dealer_resale_fulfillment_hops", "dealer_resale_fulfillment_allocations",
        "dealer_resale_hop_profit_ledger",
        "service_refund_reserve_accounts", "service_refund_liability_ledger",
        "service_refund_funding_work_orders", "service_refund_cash_jobs",
    }
    cur.execute(
        "SELECT name,to_regclass('public.' || name) AS relation FROM unnest(%s::text[]) AS name",
        (sorted(required),),
    )
    missing = [row["name"] for row in cur.fetchall() if row.get("relation") is None]
    if missing:
        raise GovernanceValidationError(
            "PROVIDER_DEPENDENCY_SCHEMA_NOT_READY",
            "无法证明服务商资金责任已结清，缺少关系表：" + ",".join(missing),
        )
    cur.execute(
        "SELECT platform_seller_user_id FROM dealer_resale_global_settings "
        "WHERE singleton_id=1 FOR UPDATE"
    )
    settings = cur.fetchone() or {}
    is_manufacturer = int(settings.get("platform_seller_user_id") or 0) == int(provider_id)

    queries = {
        "live_lots": (
            "SELECT lot_id FROM dealer_inventory_lots WHERE owner_agent_user_id=%s "
            "AND (remaining_points>0 OR reserved_points>0) FOR UPDATE", (provider_id,),
        ),
        "open_b2b_sales": (
            "SELECT order_id FROM dealer_resale_orders WHERE "
            "(seller_user_id=%s OR refund_responsible_user_id=%s) "
            "AND state NOT IN ('refunded','cancelled') FOR UPDATE", (provider_id, provider_id),
        ),
        "open_consumer_sales": (
            "SELECT order_id FROM dealer_consumer_sales WHERE "
            "(seller_user_id=%s OR refund_responsible_user_id=%s) "
            "AND state NOT IN ('refunded','cancelled') FOR UPDATE", (provider_id, provider_id),
        ),
        "unsettled_resale_profit": (
            "SELECT profit_id FROM dealer_resale_profit_ledger WHERE seller_user_id=%s "
            "AND (status='pending' OR (status='available' AND revenue_ledger_id IS NULL)) FOR UPDATE", (provider_id,),
        ),
        "open_consumer_refunds": (
            "SELECT case_id FROM consumer_refund_cases WHERE responsible_service_user_id=%s "
            "AND status NOT IN ('completed','rejected') FOR UPDATE", (provider_id,),
        ),
        "open_legacy_refund_work_orders": (
            "SELECT id FROM refund_work_orders WHERE agent_user_id=%s "
            "AND status IN ('draft','submitted','approved','payout_pending') FOR UPDATE", (provider_id,),
        ),
        "open_refund_liabilities": (
            "SELECT liability_id FROM service_refund_liability_ledger WHERE service_user_id=%s "
            "AND status<>'closed' FOR UPDATE", (provider_id,),
        ),
        "open_refund_work_orders": (
            "SELECT work_order_id FROM service_refund_funding_work_orders WHERE service_user_id=%s "
            "AND status<>'closed' FOR UPDATE", (provider_id,),
        ),
        "open_cash_jobs": (
            "SELECT cash_job_id FROM service_refund_cash_jobs WHERE responsible_service_user_id=%s "
            "AND status<>'completed' FOR UPDATE", (provider_id,),
        ),
        "refund_reserve_cents": (
            "SELECT service_user_id FROM service_refund_reserve_accounts WHERE service_user_id=%s "
            "AND available_cents>0 FOR UPDATE", (provider_id,),
        ),
        "open_jit_plans": (
            "SELECT p.plan_id FROM dealer_resale_fulfillment_plans p WHERE "
            "p.state NOT IN ('settled','cancelled') AND (p.final_seller_user_id=%s OR "
            "p.final_buyer_user_id=%s OR EXISTS (SELECT 1 FROM dealer_resale_fulfillment_hops h "
            "WHERE h.plan_id=p.plan_id AND (h.seller_user_id=%s OR h.buyer_user_id=%s))) "
            "FOR UPDATE OF p", (provider_id, provider_id, provider_id, provider_id),
        ),
        "open_jit_hops": (
            "SELECT plan_id,hop_seq FROM dealer_resale_fulfillment_hops WHERE "
            "state NOT IN ('settled','cancelled') AND (seller_user_id=%s OR buyer_user_id=%s) "
            "FOR UPDATE", (provider_id, provider_id),
        ),
        "reserved_jit_allocations": (
            "SELECT a.allocation_id FROM dealer_resale_fulfillment_allocations a "
            "JOIN dealer_resale_fulfillment_hops h ON h.plan_id=a.plan_id AND h.hop_seq=a.hop_seq "
            "WHERE a.status='reserved' AND (h.seller_user_id=%s OR h.buyer_user_id=%s) "
            "FOR UPDATE OF a", (provider_id, provider_id),
        ),
        "unsettled_jit_hop_payables": (
            "SELECT profit_id FROM dealer_resale_hop_profit_ledger WHERE seller_user_id=%s "
            "AND (status='pending' OR (status='available' AND revenue_ledger_id IS NULL)) "
            "AND agent_payable_cents>0 FOR UPDATE", (provider_id,),
        ),
    }
    result = {"platform_manufacturer": int(is_manufacturer)}
    for key, (sql, params) in queries.items():
        cur.execute(sql, params)
        result[key] = len(cur.fetchall() or [])
    return result


# Every downgrade obstacle carries the place it is actually resolved.  A refusal
# that lists twelve counters — eleven of them zero — and names no next step is
# the reason ADMIN had to ask engineering to run the change by hand.
#   entry="channel_relationship" / "commercial_binding" -> the wizard can drive it
#   entry=None                                          -> handled in its own domain flow
_DOWNGRADE_BLOCKER_SPECS: tuple = (
    ("bound_customers", "clients", "个商业绑定客户", "rows",
     "逐个把客户转交给其他服务商或转为平台直营，再回到本向导", "commercial_binding"),
    ("active_channel_relations", "channel_relations", "条现役渠道关系", "rows",
     "先在本向导终结该服务商的现役上下游渠道关系（降级后就再也改不了，必须先做）",
     "channel_relationship"),
    ("inventory_points", "inventory", "点服务商库存", "points",
     "使用现有库存退回、转售或结清原语归零；向导不没收库存", None),
    ("pending_purchase_orders", "orders", "笔待支付进货订单", "rows",
     "等待进货订单进入不可变终态（支付成功或关闭）", None),
    ("pending_customer_orders", "orders", "笔待支付客户订单", "rows",
     "等待客户订单进入不可变终态（支付成功或关闭）", None),
    ("pending_disputes", "responsibilities", "个待裁决客户归属争议", "rows",
     "走客户归属争议裁决流程结案", None),
    ("held_escrows", "responsibilities", "笔在途争议托管", "rows",
     "走争议托管释放流程结清", None),
    ("unsettled_revenue_cents", "earnings", "分未结服务商收益", "cents",
     "完成结算或提现流程把收益清零；向导不覆盖收益", None),
    ("unsettled_channel_revenue", "earnings", "笔未结渠道收益", "rows",
     "完成渠道收益结算流程", None),
    ("pending_settlements", "earnings", "笔在途结算申请", "rows",
     "先处理完在途结算申请（通过或驳回）", None),
    ("pending_withdrawals", "earnings", "笔在途提现申请", "rows",
     "先处理完在途提现申请（通过或驳回）", None),
    ("live_lots", "inventory", "个逐级转售库存批次", "rows",
     "退回、转售或结清逐级库存批次", None),
    ("open_b2b_sales", "orders", "笔未终态转售订单", "rows",
     "等待转售订单进入不可变终态", None),
    ("open_consumer_sales", "orders", "笔未终态消费者订单", "rows",
     "等待消费者订单进入不可变终态", None),
    ("unsettled_resale_profit", "earnings", "笔未入台账的逐级收益", "rows",
     "把逐级收益结入收益台账", None),
    ("open_consumer_refunds", "responsibilities", "个未结消费者退款案件", "rows",
     "完成消费者退款案件；责任不得转嫁给平台", None),
    ("open_legacy_refund_work_orders", "responsibilities", "个未结退款工单", "rows",
     "完成退款工单（admin 工单流程）", None),
    ("open_refund_liabilities", "responsibilities", "笔未结退款负债", "rows",
     "结清退款负债台账", None),
    ("open_refund_work_orders", "responsibilities", "个未结退款资金工单", "rows",
     "完成退款资金工单", None),
    ("open_cash_jobs", "responsibilities", "个未完成退款现金任务", "rows",
     "完成退款现金任务", None),
    ("refund_reserve_cents", "inventory", "个非零退款准备金账户", "rows",
     "结清退款准备金账户余额", None),
    ("open_jit_plans", "orders", "个在途 JIT 履约计划", "rows",
     "等待 JIT 履约计划结算或取消", None),
    ("open_jit_hops", "orders", "个在途 JIT 履约节点", "rows",
     "等待 JIT 履约节点结算或取消", None),
    ("reserved_jit_allocations", "inventory", "笔 JIT 预留分配", "rows",
     "释放或消费 JIT 预留分配", None),
    ("unsettled_jit_hop_payables", "earnings", "笔 JIT 节点未结应付", "rows",
     "把 JIT 节点应付结入收益台账", None),
    ("platform_manufacturer", "platform_manufacturer", "项平台生产主体配置", "rows",
     "先把平台生产主体配置迁移到别的账号", None),
    ("platform_direct_service_identity", "platform_manufacturer", "项平台直营专用服务身份", "rows",
     "先把平台直营专用服务身份迁移到别的账号", None),
)


def _provider_downgrade_blocker_items(counters: Dict[str, int]) -> List[Dict[str, Any]]:
    """Render ONLY the non-zero obstacles, each with where it is handled."""
    items: List[Dict[str, Any]] = []
    for key, category, noun, unit, handling, entry in _DOWNGRADE_BLOCKER_SPECS:
        value = int(counters.get(key) or 0)
        if value <= 0:
            continue
        items.append({
            "key": key, "category": category, "unit": unit, "value": value,
            "label": f"{value} {noun}", "handling": handling, "entry": entry,
        })
    return items


def complete_provider_downgrade_in_transaction(
    cur,
    user_id: int,
    *,
    expected_version: int,
    reason: str,
    operator_user_id: int,
    operator_username: Optional[str],
    request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """Apply the identity terminal write inside an already-fenced downgrade tx.

    The caller must hold the commercial provider lifecycle fence and locked,
    freshly rechecked dependency census.  Keeping this authoritative identity,
    permission-version, audit and outbox writer cursor-based lets the downgrade
    plan and identity transition commit or roll back together.
    """
    cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
    if not cur.fetchone():
        raise GovernanceNotFound(f"user {user_id}")
    current_version = _lock_version(cur, user_id, "business_identity", expected_version)
    cur.execute(
        "INSERT INTO user_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
        (int(user_id),),
    )
    cur.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s FOR UPDATE", (int(user_id),))
    level = int(cur.fetchone()["agent_level"] or 0)
    if level < 1:
        raise GovernanceValidationError("SUBJECT_NOT_PROVIDER", "该账号当前不是服务商")
    if is_platform_direct_service_user(user_id):
        raise GovernanceValidationError(
            "PLATFORM_DIRECT_PROVIDER_PROTECTED",
            "平台直营专用服务账号不能降级为普通用户",
        )
    cur.execute(
        "UPDATE user_wallets SET agent_level=0,updated_at=NOW() WHERE user_id=%s",
        (int(user_id),),
    )
    cur.execute(
        """UPDATE users SET permission_version=COALESCE(permission_version,1)+1
           WHERE id=%s RETURNING permission_version""",
        (int(user_id),),
    )
    permission_version = int(cur.fetchone()["permission_version"])
    before = {"business_identity": _identity(level)}
    after = {"business_identity": "ordinary_user"}
    next_version = _advance_version(cur, user_id, "business_identity", current_version)
    _write_audits(
        cur, subject_user_id=user_id, scope="business_identity",
        operator_user_id=operator_user_id, operator_username=operator_username,
        request_id=request_id, reason=reason, before=before, after=after,
        version_before=current_version, version_after=next_version, ip_address=ip_address,
        evidence={
            "agent_level_before": level,
            "agent_level_after": 0,
            "permission_version_incremented": True,
            "atomic_provider_downgrade": True,
        },
    )
    _enqueue_account_change(
        cur, user_id=user_id, event_type="account.identity_changed",
        scope="business_identity", version=next_version,
        status="账号身份已变更",
        summary="账号可用功能可能发生变化，请重新进入相应页面查看。",
    )
    return {
        "success": True, "scope": "business_identity", "version": next_version,
        "request_id": request_id, "before": before, "after": after,
        "permission_version": permission_version,
    }


def _convert_customer_binding_to_channel_on_upgrade(
    cur,
    *,
    new_provider_user_id: int,
    operator_user_id: int,
    reason: str,
    request_id: str,
) -> Optional[Dict[str, Any]]:
    """Re-express a promoted user's inbound customer binding as an upstream
    channel relationship (SSOT business-governance-master §4.1).

    When ordinary account B — currently a *customer* of provider A — is promoted
    to service_provider, the old commercial-service attribution (A serves B) is
    converted, in the SAME transaction, into a channel/procurement relationship
    (A becomes B's upstream channel account). The caller MUST have already set B's
    agent_level to 1 so the channel actor validation (active + agent_level>=1)
    passes for B.

    Returns audit evidence, or None when B has no inbound customer binding.
    Historical orders, settlements and commissions are never rewritten (§4.1.6);
    only the current-state projection is closed (source fact preserved in
    customer_agent_binding_history).
    """
    from services.channel_pricing import (
        ChannelError,
        MIN_COST_MULTIPLIER_BPS,
        save_relationships_cur,
    )
    from services.commercial_binding_history import close_current_version

    provider_id = int(new_provider_user_id)
    cur.execute(
        """SELECT id, customer_user_id, agent_user_id, binding_source, source_token,
                  bound_at, dispute_status, dispute_note
           FROM customer_agent_bindings
           WHERE customer_user_id=%s FOR UPDATE""",
        (provider_id,),
    )
    binding = cur.fetchone()
    if not binding:
        return None
    binding = dict(binding)
    upstream_user_id = int(binding["agent_user_id"])

    # Preserve the source fact, then drop the current-state customer projection:
    # B is no longer a customer of A.
    binding_history_id = close_current_version(
        cur, customer_user_id=provider_id, projection=binding,
        operator_user_id=int(operator_user_id), reason=reason, request_id=request_id,
    )
    cur.execute(
        "DELETE FROM customer_agent_bindings WHERE customer_user_id=%s", (provider_id,)
    )

    if upstream_user_id == provider_id:
        # Defensive: a self-referential legacy binding must never become a self
        # channel edge. The projection is closed above; no channel relationship.
        return {
            "converted_inbound_binding": True,
            "upstream_provider_user_id": None,
            "converted_binding_id": int(binding["id"]),
            "binding_history_id": binding_history_id,
            "channel_relationship_id": None,
            "self_referential_binding_dropped": True,
        }

    try:
        saved = save_relationships_cur(
            cur,
            [{
                "buyer_dealer_id": provider_id,
                "upstream_channel_account_id": upstream_user_id,
                "expected_relationship_version": None,
                "new_relationship_version": f"identity-upgrade-{request_id}"[:180],
                # Inherit the procurement COST relationship at cost-passthrough
                # (§5.2). The upstream's end-customer retail markup is NOT
                # inherited; ADMIN may set a real channel multiplier later (§5.4).
                "cost_multiplier_bps": MIN_COST_MULTIPLIER_BPS,
                "reason": reason or "普通用户升级为下级服务商：商业归属转渠道进货关系",
            }],
            approved_by=int(operator_user_id),
            created_by=int(operator_user_id),
        )
    except ChannelError as exc:
        raise GovernanceValidationError("CHANNEL_CONVERSION_INVALID", str(exc)) from exc

    channel_row = saved["relationships"][0]
    return {
        "converted_inbound_binding": True,
        "upstream_provider_user_id": upstream_user_id,
        "converted_binding_id": int(binding["id"]),
        "binding_history_id": binding_history_id,
        "channel_relationship_id": int(channel_row["id"]),
        "channel_cost_multiplier_bps": int(MIN_COST_MULTIPLIER_BPS),
        "historical_orders_untouched": True,
        "pricing_snapshots_untouched": True,
    }


def change_business_identity(
    user_id: int, business_identity: str, *, expected_version: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        provider_binding_ids: List[int] = []
        if business_identity == "service_provider":
            # Promotion may re-express an inbound customer binding as an upstream
            # channel relationship. Take the channel-graph advisory lock BEFORE any
            # actor row lock so the promotion path shares the channel writers' lock
            # order (graph -> rows) and cannot deadlock with change_channel_relationship.
            from services.channel_pricing import lock_channel_relationship_graph

            lock_channel_relationship_graph(cur)
        if business_identity == "ordinary_user":
            from services.commercial_service_routing import lock_commercial_provider_lifecycle

            # Every operation that can change or invalidate a commercial
            # provider starts with this fence.  It keeps downgrade, W4
            # arbitration and admin grants on one provider->binding->user order.
            lock_commercial_provider_lifecycle(cur, int(user_id))
        # Payment callbacks lock recharge_orders before taking the provider advisory
        # lock.  Pre-lock the same pending rows first so a cash-success callback can
        # never deadlock with a concurrent downgrade (order -> advisory everywhere).
        # Purchase writers take the advisory before INSERT; the dependency queries
        # below deliberately run again after this lock and therefore see any order
        # committed while this transaction was waiting.
        cur.execute(
            """SELECT id FROM recharge_orders
               WHERE payment_status='pending' AND (
                   (user_id=%s AND order_type='agent_inventory_purchase')
                   OR (
                       agent_user_id=%s
                       AND (order_type IS NULL OR order_type='customer_recharge')
                   )
               )
               ORDER BY id FOR UPDATE""",
            (int(user_id), int(user_id)),
        )
        cur.fetchall()
        cur.execute("SELECT pg_advisory_xact_lock(920714, %s)", (int(user_id),))
        if business_identity == "ordinary_user":
            cur.execute(
                """SELECT id FROM customer_agent_bindings
                   WHERE agent_user_id=%s ORDER BY id FOR UPDATE""",
                (int(user_id),),
            )
            provider_binding_ids = [int(row["id"]) for row in cur.fetchall() or []]
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
        if not cur.fetchone():
            raise GovernanceNotFound(f"user {user_id}")
        current_version = _lock_version(cur, user_id, "business_identity", expected_version)
        level = _ensure_subject_wallet_row(cur, int(user_id))
        before_identity = _identity(level)
        if business_identity == before_identity:
            raise GovernanceValidationError("NO_CHANGE", "业务身份没有变化")
        if business_identity == "ordinary_user":
            provider_id = int(user_id)
            is_platform_direct = is_platform_direct_service_user(provider_id)

            bound_customers = len(provider_binding_ids)
            cur.execute(
                """SELECT id FROM channel_pricing_relationships
                   WHERE status='active' AND effective_to IS NULL
                     AND (buyer_dealer_id=%s OR upstream_channel_account_id=%s)
                   FOR UPDATE""",
                (provider_id, provider_id),
            )
            active_channel_relations = len(cur.fetchall() or [])
            cur.execute(
                """SELECT paid_inventory_points, bonus_inventory_points, frozen_inventory_points
                   FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
                (provider_id,),
            )
            inventory = cur.fetchone() or {}
            inventory_points = sum(
                int(inventory.get(column) or 0)
                for column in (
                    "paid_inventory_points", "bonus_inventory_points", "frozen_inventory_points"
                )
            )
            cur.execute(
                """SELECT id FROM recharge_orders
                   WHERE user_id=%s AND order_type='agent_inventory_purchase'
                     AND payment_status='pending'
                   FOR UPDATE""",
                (provider_id,),
            )
            pending_purchase_orders = len(cur.fetchall() or [])

            cur.execute(
                """SELECT id FROM recharge_orders
                   WHERE agent_user_id=%s
                     AND (order_type IS NULL OR order_type='customer_recharge')
                     AND payment_status='pending'
                   FOR UPDATE""",
                (provider_id,),
            )
            pending_customer_orders = len(cur.fetchall() or [])
            cur.execute(
                """SELECT id FROM customer_agent_binding_disputes
                   WHERE status='pending'
                     AND (old_agent_user_id=%s OR new_agent_user_id=%s)
                   FOR UPDATE""",
                (provider_id, provider_id),
            )
            pending_disputes = len(cur.fetchall() or [])
            cur.execute(
                """SELECT id FROM dispute_escrow
                   WHERE status='held'
                     AND (order_agent_user_id=%s OR bound_agent_user_id=%s)
                   FOR UPDATE""",
                (provider_id, provider_id),
            )
            held_escrows = len(cur.fetchall() or [])

            # Reuse the earnings SSOT so paid withdrawals and redeemed inventory
            # are not mistaken for forever-unsettled revenue.
            from services.agent_revenue import get_agent_balance

            revenue_balance = get_agent_balance(cur, provider_id)
            unsettled_revenue_cents = sum(
                int(revenue_balance.get(key) or 0)
                for key in ("frozen_cents", "available_cents", "pending_payout_cents")
            )
            cur.execute(
                """SELECT id FROM agent_settlement_requests
                   WHERE agent_user_id=%s AND status IN ('pending','approved')
                   FOR UPDATE""",
                (provider_id,),
            )
            pending_settlements = len(cur.fetchall() or [])
            cur.execute(
                """SELECT id FROM withdrawal_requests
                   WHERE user_id=%s AND status IN ('pending','approved')
                   FOR UPDATE""",
                (provider_id,),
            )
            pending_withdrawals = len(cur.fetchall() or [])
            cur.execute(
                """SELECT id FROM channel_revenue_ledger
                   WHERE channel_beneficiary_user_id=%s AND status='recorded'
                     AND channel_revenue_cents > 0
                   FOR UPDATE""",
                (provider_id,),
            )
            unsettled_channel_revenue = len(cur.fetchall() or [])
            resale_dependencies = _lock_dealer_resale_downgrade_dependencies(cur, provider_id)
            resale_blockers = sum(resale_dependencies.values())

            blockers = (
                is_platform_direct or bound_customers or active_channel_relations
                or inventory_points or pending_purchase_orders or pending_customer_orders
                or pending_disputes or held_escrows or unsettled_revenue_cents
                or unsettled_channel_revenue or pending_settlements or pending_withdrawals
                or resale_blockers
            )
            if blockers:
                # The gate expression above is authoritative and unchanged; this
                # only decides how the refusal is WORDED.  Zero-valued counters
                # are never listed, and every listed one names its next step.
                items = _provider_downgrade_blocker_items({
                    "bound_customers": bound_customers,
                    "active_channel_relations": active_channel_relations,
                    "inventory_points": inventory_points,
                    "pending_purchase_orders": pending_purchase_orders,
                    "pending_customer_orders": pending_customer_orders,
                    "pending_disputes": pending_disputes,
                    "held_escrows": held_escrows,
                    "unsettled_revenue_cents": unsettled_revenue_cents,
                    "unsettled_channel_revenue": unsettled_channel_revenue,
                    "pending_settlements": pending_settlements,
                    "pending_withdrawals": pending_withdrawals,
                    "platform_direct_service_identity": int(bool(is_platform_direct)),
                    **{key: int(value) for key, value in resale_dependencies.items()},
                })
                summary = "、".join(item["label"] for item in items)
                message = (
                    f"仍有 {summary}，不能直接改为普通用户"
                    if summary
                    # Fail-closed: the gate fired but nothing itemised.  Refuse
                    # anyway rather than let an un-narratable state through.
                    else "该服务商仍有未结清的经营依赖，不能直接改为普通用户"
                )
                raise GovernanceValidationError(
                    "ACTIVE_PROVIDER_DEPENDENCIES", message,
                    {"blockers": items, "is_platform_direct": bool(is_platform_direct)},
                )
            next_level = 0
        else:
            # SSOT business-governance-master §4.1: an ordinary account that is
            # currently a customer of an upstream provider MUST be promotable
            # atomically. The former ACTIVE_CUSTOMER_COMMERCIAL_BINDING hard block
            # is removed; the inbound customer binding is instead converted into an
            # upstream channel/procurement relationship right after the agent_level
            # flip below (so the channel actor validation observes the new provider).
            next_level = 1
        cur.execute(
            "UPDATE user_wallets SET agent_level=%s, updated_at=NOW() WHERE user_id=%s",
            (next_level, int(user_id)),
        )
        inbound_binding_conversion = None
        if business_identity == "service_provider":
            # B is now a service provider (agent_level=1). Re-express any inbound
            # customer binding (A serves B) as an upstream channel relationship
            # (A becomes B's upstream). Atomic, history-preserving, funds untouched.
            inbound_binding_conversion = _convert_customer_binding_to_channel_on_upgrade(
                cur, new_provider_user_id=int(user_id),
                operator_user_id=operator_user_id, reason=reason, request_id=request_id,
            )
        cur.execute(
            """UPDATE users SET permission_version=COALESCE(permission_version,1)+1
               WHERE id=%s RETURNING permission_version""",
            (int(user_id),),
        )
        permission_version = int(cur.fetchone()["permission_version"])
        before = {"business_identity": before_identity}
        after = {"business_identity": business_identity}
        next_version = _advance_version(cur, user_id, "business_identity", current_version)
        identity_evidence = {
            "agent_level_before": level,
            "agent_level_after": next_level,
            "permission_version_incremented": True,
        }
        if inbound_binding_conversion:
            identity_evidence["inbound_binding_conversion"] = inbound_binding_conversion
        _write_audits(
            cur, subject_user_id=user_id, scope="business_identity",
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, reason=reason, before=before, after=after,
            version_before=current_version, version_after=next_version, ip_address=ip_address,
            evidence=identity_evidence,
        )
        # 🔴 [P0-C 根治 · WO_INVREL_P0_HOTFIX §9.4.3] scope 缺口:
        #    「把用户升级成服务商 / 设上级」走的是 `business_identity` scope,
        #    而"归属证据不完整"那盏灯认的是 `commercial_binding` scope ——
        #    **两条路径写不同 scope,灯只认后者,所以每次这样设置都必然亮灯**。
        #    u133 / u163 就是这么亮的(生产实证:它们的审计里一条 commercial_binding 都没有)。
        #    这里在**同一事务**里把缺的那条凭证一并补上,形状复用唯一实现,
        #    下次这样设置就不会再亮。灯已灭的、非 admin_manual 的一律跳过,不刷审计。
        _autofill_commercial_binding_evidence(
            cur, user_id=user_id, operator_user_id=operator_user_id,
            operator_username=operator_username, request_id=request_id,
            reason=f"随业务身份变更同事务补录归属凭证 · {reason}"[:500],
            ip_address=ip_address,
        )
        _enqueue_account_change(
            cur,
            user_id=user_id,
            event_type="account.identity_changed",
            scope="business_identity",
            version=next_version,
            status="账号身份已变更",
            summary="账号可用功能可能发生变化，请重新进入相应页面查看。",
        )
        return {"success": True, "scope": "business_identity", "version": next_version,
                "request_id": request_id, "before": before, "after": after,
                "permission_version": permission_version}


def _autofill_commercial_binding_evidence(
    cur, *, user_id: int, operator_user_id: int, operator_username: Optional[str],
    request_id: str, reason: str, ip_address: Optional[str],
) -> Optional[int]:
    """身份治理动作发生时,顺带补齐该用户 admin_manual 归属缺失的凭证(§9.4.3 根治)。

    🔴 **不是无条件刷审计**:三道闸,任何一道不满足就原样返回 None ——
       ① 必须存在 `admin_manual` 归属;② 必须**当前确实在亮灯**
       (用判定侧同一段 SQL 复核,不另写口径);③ 必须能拿到版本位。
       灯已灭的不会被再补一条,非人工归属的不碰。

    返回新的 commercial_binding 版本号,或 None(什么也没做)。
    """
    cur.execute(
        f"""SELECT id, agent_user_id, binding_source, bound_at,
                   to_char(bound_at, '{BINDING_BACKFILL_TIME_FORMAT}') AS bound_at_key
            FROM customer_agent_bindings WHERE customer_user_id=%s FOR UPDATE""",
        (int(user_id),),
    )
    binding = cur.fetchone()
    if not binding or str(binding["binding_source"]) != "admin_manual":
        return None
    cur.execute(
        f"""SELECT ({_relation_attention_sql()}) AS needs_attention
            FROM users u JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
            WHERE u.id=%s""",
        (int(user_id),),
    )
    row = cur.fetchone()
    if not row or not bool(row["needs_attention"]):
        return None  # 已有凭证 —— 不重复补,避免刷审计
    # 🔴 列名是 `subject_user_id` 不是 `user_id`(照抄 `_lock_version` 的真实 SQL,不自己发明)。
    #    这里不能用 `_lock_version`:那个要求调用方给 expected_version 做 CAS,
    #    而本函数是身份变更的**顺带**动作,没有独立的 expected 值。
    #    行锁仍然要拿 —— 与 `_lock_version` 同一把 FOR UPDATE,避免并发双写版本位。
    cur.execute(
        """INSERT INTO admin_user_governance_versions(subject_user_id, scope, version)
           VALUES (%s,'commercial_binding',1)
           ON CONFLICT(subject_user_id, scope) DO NOTHING""",
        (int(user_id),),
    )
    cur.execute(
        """SELECT version FROM admin_user_governance_versions
           WHERE subject_user_id=%s AND scope='commercial_binding' FOR UPDATE""",
        (int(user_id),),
    )
    version_row = cur.fetchone()
    current_version = int(version_row["version"]) if version_row else 1
    # 🔴 `uq_admin_user_governance_audits_request_id` 是**唯一**约束。
    #    本函数是顺带动作,主动作已经用掉了调用方那个 request_id ——
    #    直接复用会 UniqueViolation,而它发生在同一事务里 → **整个治理动作失败**。
    #    (判别锁 `test_setting_upstream_backfills_lingering_binding_evidence` 抓到的正是这个:
    #     不加派生后缀的话,"设上级"在生产上会直接 500。)
    derived_request_id = f"{request_id}:binding-evidence"[:128]
    result = _write_binding_backfill_audit(
        cur, user_id=user_id, binding=binding, current_version=current_version,
        operator_user_id=operator_user_id, operator_username=operator_username,
        request_id=derived_request_id, reason=reason, ip_address=ip_address,
    )
    # 不打日志:本模块没有 module-level logger,而且**审计行本身就是记录** ——
    # 补了什么、给谁补的、什么理由,都在 `admin_user_governance_audits` 里可查。
    return int(result["version"])


def change_commercial_binding(
    user_id: int, provider_user_id: Optional[int], *, expected_version: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """连接自持版本(HTTP 端点用)· 返回即已提交。

    🔴 唯一实现在 `change_commercial_binding_cur`,本函数只是薄壳。
       拆开的原因:库存代划拨要求「绑定缺失时同事务内建绑定,失败整体回滚」
       (工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12 §P0-1),
       而自持连接的函数没法加入调用方的事务。
       **不是第二套写路径** —— 两个入口共用同一段实现,审计/CAS/版本位一并继承。
    """
    with get_db() as conn:
        cur = conn.cursor()
        return change_commercial_binding_cur(
            cur, user_id, provider_user_id,
            expected_version=expected_version, reason=reason,
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, ip_address=ip_address,
        )


def change_commercial_binding_cur(
    cur, user_id: int, provider_user_id: Optional[int], *, expected_version: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """主事务内版本 · 调用方负责 commit/rollback。"""
    from services.commercial_service_routing import (
        RelationshipConflict,
        lock_commercial_binding_subject,
        lock_commercial_provider_lifecycle,
        lock_commercial_provider_for_assignment,
        lock_pending_commercial_orders,
    )
    from services.commercial_binding_history import (
        append_current_version,
        close_current_version,
    )

    # Provider eligibility changes share this first lock.  It prevents a
    # stale SELECT snapshot from admitting both an admin grant and a new
    # commercial assignment.
    if provider_user_id is not None:
        lock_commercial_provider_lifecycle(cur, int(provider_user_id))
    # Payment completion locks the pending order before resolving (and thus
    # locking) the commercial subject.  Use the same order -> subject order
    # here to avoid a callback/rebind deadlock.  The query is repeated after
    # the subject lock so a quote-less writer that held the subject lock and
    # inserted while we waited is still observed fail-closed.
    lock_pending_commercial_orders(cur, int(user_id))
    lock_commercial_binding_subject(cur, int(user_id))
    cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
    if not cur.fetchone():
        raise GovernanceNotFound(f"user {user_id}")
    # 🔴 [#139 返修三] 原来这里「主体没有钱包行 ⇒ 直接拒」。
    #    而 `change_business_identity` 对同样的主体是**补行然后继续** ——
    #    同样的治理写动作、同样的输入,处置相反,两边判据各自都绿。
    #    裁定(2026-09-07):**主体补行**,对手方保持严格。
    #    ⇒ 管理员终于能修那些「邀请进来还没初始化钱包」的账号,
    #      而拒绝原本也没保护任何东西:它要求存在的那一行,补一下就有了。
    subject_level = _ensure_subject_wallet_row(cur, int(user_id))
    if subject_level >= 1:
        raise GovernanceValidationError(
            "COMMERCIAL_BINDING_SUBJECT_MUST_BE_ORDINARY",
            "商业服务归属只适用于普通用户；服务商上下游必须使用渠道关系治理",
        )
    current_version = _lock_version(cur, user_id, "commercial_binding", expected_version)
    cur.execute(
        """SELECT * FROM customer_agent_bindings WHERE customer_user_id=%s FOR UPDATE""",
        (int(user_id),),
    )
    old = cur.fetchone()
    old_provider = int(old["agent_user_id"]) if old else None
    if old and old.get("dispute_status") == "pending":
        raise GovernanceValidationError(
            "BINDING_DISPUTE_PENDING", "商业服务归属正在争议处理中，请使用专用争议裁决流程"
        )
    if provider_user_id is not None:
        provider_user_id = int(provider_user_id)
        if provider_user_id == int(user_id):
            raise GovernanceValidationError("SELF_BINDING", "不能将用户绑定给自己")
        try:
            lock_commercial_provider_for_assignment(cur, provider_user_id)
        except RelationshipConflict as exc:
            raise GovernanceValidationError(
                "PROVIDER_UNAVAILABLE",
                "目标承接方未通过服务商身份、平台权限或价目范围检查",
            ) from exc
    if old_provider == provider_user_id:
        raise GovernanceValidationError("NO_CHANGE", "商业服务归属没有变化")
    revoked_permission_versions: List[Dict[str, int]] = []
    if old_provider is not None and old_provider != provider_user_id:
        # Ending or transferring the commercial service relationship also
        # ends the old provider's legacy access assignments. Organization
        # members remain governed by organization_brand_assignments; this
        # only removes their compatibility projection.
        cur.execute(
            """
            DELETE FROM user_clients uc USING brands b
            WHERE uc.user_id=%s AND uc.brand_id=b.id AND b.owner_user_id=%s
            """,
            (old_provider, int(user_id)),
        )
        if cur.rowcount:
            cur.execute(
                """
                UPDATE users SET permission_version=permission_version+1
                WHERE id=%s RETURNING permission_version
                """,
                (old_provider,),
            )
            revoked_permission_versions.append(
                {
                    "user_id": old_provider,
                    "permission_version": int(cur.fetchone()["permission_version"]),
                }
            )
    # Missing projection row means platform direct, not "no provider".
    # Every pending customer order already pins an immutable service principal.
    pending_locked_orders = lock_pending_commercial_orders(cur, int(user_id))
    if pending_locked_orders:
        raise GovernanceValidationError(
            "BINDING_PENDING_ORDERS",
            f"仍有 {pending_locked_orders} 笔待支付订单锁定当前服务方；请等待支付终态后再修改",
        )
    before = {
        "commercial_provider_user_id": old_provider,
        "commercial_mode": "service_provider" if old_provider else "platform_direct",
    }
    ended_history_id = close_current_version(
        cur,
        customer_user_id=int(user_id),
        projection=dict(old) if old else None,
        operator_user_id=int(operator_user_id),
        reason=reason,
        request_id=request_id,
    )
    if provider_user_id is None:
        # The legacy table is a current-state projection only.  Its complete
        # source/dispute facts were durably versioned above before clearing.
        cur.execute("DELETE FROM customer_agent_bindings WHERE customer_user_id=%s", (int(user_id),))
        binding_id = None
    else:
        cur.execute(
            """
            INSERT INTO customer_agent_bindings(
                customer_user_id, agent_user_id, binding_source, source_token, bound_at,
                dispute_status, dispute_note, admin_override_user_id, admin_override_at
            ) VALUES (%s,%s,'admin_manual',NULL,NOW(),NULL,NULL,%s,NOW())
            ON CONFLICT(customer_user_id) DO UPDATE SET
                agent_user_id=EXCLUDED.agent_user_id,
                binding_source='admin_manual', source_token=NULL, bound_at=NOW(),
                dispute_status=NULL, dispute_note=NULL,
                admin_override_user_id=EXCLUDED.admin_override_user_id,
                admin_override_at=NOW()
            RETURNING id
            """,
            (int(user_id), provider_user_id, int(operator_user_id)),
        )
        binding_id = int(cur.fetchone()["id"])
    new_history_id = append_current_version(
        cur,
        customer_user_id=int(user_id),
        provider_user_id=provider_user_id,
        source_binding_id=binding_id,
        operator_user_id=int(operator_user_id),
        reason=reason,
        request_id=request_id,
    )
    # Existing credit balances are a current projection, not immutable
    # transaction history.  Keep their service principal synchronized with
    # the relationship SSOT in this same CAS transaction.
    wallet_provider_id = provider_user_id
    if wallet_provider_id is None:
        from services.commercial_service_routing import platform_direct_readiness

        direct = platform_direct_readiness(cur=cur)
        if not direct.get("ready") or not (direct.get("service_user") or {}).get("user_id"):
            raise GovernanceValidationError(
                "PLATFORM_DIRECT_NOT_READY",
                "平台直营服务尚未就绪，不能结束当前商业绑定",
            )
        wallet_provider_id = int(direct["service_user"]["user_id"])
    # [历史账本只读 · 2026-08-17] 该表已于 2026-07-29 停写(三池清零 · 行保留)。
    # 本 UPDATE 只同步**归属投影**(agent_user_id),不动任何金额,且 rowcount 进审计 evidence。
    cur.execute(
        """UPDATE customer_agent_credit_wallets
           SET agent_user_id=%s, updated_at=NOW()
           WHERE customer_user_id=%s""",
        (int(wallet_provider_id), int(user_id)),
    )
    wallet_projection_updated = int(cur.rowcount or 0)
    after = {
        "commercial_provider_user_id": provider_user_id,
        "commercial_mode": "service_provider" if provider_user_id else "platform_direct",
    }
    next_version = _advance_version(cur, user_id, "commercial_binding", current_version)
    _write_audits(
        cur, subject_user_id=user_id, scope="commercial_binding",
        operator_user_id=operator_user_id, operator_username=operator_username,
        request_id=request_id, reason=reason, before=before, after=after,
        version_before=current_version, version_after=next_version, ip_address=ip_address,
        evidence={
            "binding_id": binding_id,
            "ended_history_id": ended_history_id,
            "new_history_id": new_history_id,
            "registration_attribution_untouched": True,
            "credit_wallet_projection_updated": wallet_projection_updated,
            "credit_wallet_service_user_id": int(wallet_provider_id),
        },
    )
    return {"success": True, "scope": "commercial_binding", "version": next_version,
            "request_id": request_id, "before": before, "after": after,
            "_permission_invalidations": revoked_permission_versions}


def backfill_commercial_binding_audit(
    user_id: int, *, expected_version: int, reason: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """给一条**已存在**的 admin_manual 归属补录审计凭证(工单 P1-5 方案 a)。

    🔴 这不是"建绑定的第二条路":本函数对 `customer_agent_bindings`
       **只读**,一个字段都不写(判别锁 `test_backfill_never_writes_binding` 钉这条)。
       它补的是「当时这条归属为什么这么定」的凭证,不是归属本身。

    🔴 也不是"伪装成当时的审计":写进去的是
         backfill=true · original_bound_at(锚死当时的绑定时间)· backfilled_at(现在)
       `created_at` 照实落在今天,与 `original_bound_at` 差多久一目了然,
       任何人都能**机械区分**补录与原生(判别锁 `test_backfill_is_mechanically_distinguishable`)。

    前置条件(任一不满足即拒,不静默成功):
      - 该用户当前有 `binding_source='admin_manual'` 的归属行
      - 该归属**当前确实在亮灯**(已有凭证的不许重复补,避免刷审计)
    """
    with get_db() as conn:
        cur = conn.cursor()
        from services.commercial_service_routing import lock_commercial_binding_subject

        lock_commercial_binding_subject(cur, int(user_id))
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
        if not cur.fetchone():
            raise GovernanceNotFound(f"user {user_id}")
        cur.execute(
            f"""SELECT id, agent_user_id, binding_source, bound_at,
                       to_char(bound_at, '{BINDING_BACKFILL_TIME_FORMAT}') AS bound_at_key
                FROM customer_agent_bindings WHERE customer_user_id=%s FOR UPDATE""",
            (int(user_id),),
        )
        binding = cur.fetchone()
        if not binding:
            raise GovernanceValidationError(
                "BINDING_NOT_FOUND", "该用户当前没有商业服务归属,无从补录凭证"
            )
        if str(binding["binding_source"]) != "admin_manual":
            raise GovernanceValidationError(
                "BINDING_NOT_ADMIN_MANUAL",
                f"只有 admin_manual 归属需要补录凭证;当前来源是 {binding['binding_source']}",
            )
        # 用**判定侧同一段 SQL** 复核是否真的在亮灯 —— 判据和处置共用一份口径,
        # 不能一边改判据一边让处置按另一套走。
        cur.execute(
            f"""SELECT ({_relation_attention_sql()}) AS needs_attention
                FROM users u JOIN customer_agent_bindings cab ON cab.customer_user_id=u.id
                WHERE u.id=%s""",
            (int(user_id),),
        )
        row = cur.fetchone()
        if not row or not bool(row["needs_attention"]):
            raise GovernanceValidationError(
                "NO_CHANGE", "该归属已有审计凭证(未亮灯),无需补录"
            )

        current_version = _lock_version(cur, user_id, "commercial_binding", expected_version)
        result = _write_binding_backfill_audit(
            cur, user_id=user_id, binding=binding, current_version=current_version,
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, reason=reason, ip_address=ip_address,
        )
        # 🔴 [P0-1 · WO_RESPONSE_MODEL_CONTRACT_GATE §2] 这里原本还返回 `"backfill": True`,
        #    而 `GovernanceMutationResponse` 是 extra="forbid" 且未声明它
        #    → 该端点**每次必 500**(2026-08-14 Owner 在生产点按钮撞到)。
        #    修法取"不返回"而不是"往模型加字段":
        #      · 该模型被 7 条路由共用,加字段会让另外 6 条平白多回一个 `backfill:false`;
        #      · 这个键**没有任何消费方**(前端不读、测试不断言),端点名本身已表达语义;
        #      · 该端点从未成功返回过(100% 500),因此移除它不存在"打破既有消费者"的风险。
        #    审计侧的凭证仍带 `backfill:"true"`(见 `_write_binding_backfill_audit`),不受影响。
        return {"success": True, "scope": "commercial_binding", **result}


def _write_binding_backfill_audit(
    cur, *, user_id: int, binding: Dict[str, Any], current_version: int,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    reason: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """写一条**补录形态**的 `commercial_binding` 审计(唯一实现)。

    🔴 [P0-C §9] 抽出来是因为有**两个**合法调用点:
         1. `backfill_commercial_binding_audit` —— 管理员手动补历史;
         2. `change_business_identity` —— 升级服务商时**同事务**顺带补上。
       两处必须写出**逐字段相同**的形状,否则灯的判据只认其中一种,
       另一种照样亮 —— 那就是"根治了但没治好"。写两遍必然漂移,所以只留一份。

    形状要点(与 `_relation_attention_sql()` 第 2 条豁免逐字对应):
      · `backfill='true'` + `original_bound_at` 逐字符等于当前 `bound_at`;
      · `created_at` 照实落今天 → 与原生审计**机械可分**;
      · before / after 同一状态 —— 只补凭证,**不改归属**。
    """
    provider_user_id = int(binding["agent_user_id"])
    snapshot = {
        "commercial_provider_user_id": provider_user_id,
        "commercial_mode": "service_provider",
    }
    next_version = _advance_version(cur, user_id, "commercial_binding", current_version)
    cur.execute("SELECT clock_timestamp() AS now_at")
    backfilled_at = cur.fetchone()["now_at"]
    _write_audits(
        cur, subject_user_id=user_id, scope="commercial_binding",
        operator_user_id=operator_user_id, operator_username=operator_username,
        request_id=request_id, reason=reason,
        # 补录不改变归属 —— before / after 是同一状态,这正是"只补凭证"的形状。
        before=snapshot, after=snapshot,
        version_before=current_version, version_after=next_version,
        ip_address=ip_address,
        evidence={
            "binding_id": int(binding["id"]),
            "backfill": "true",
            "original_bound_at": str(binding["bound_at_key"]),
            "backfilled_at": backfilled_at.isoformat(),
            "binding_untouched": True,
        },
    )
    return {
        "version": next_version, "request_id": request_id,
        "before": snapshot, "after": snapshot,
    }


def change_platform_access(
    user_id: int, administrator: bool, *, expected_version: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        # Share the same transaction-scoped census lock used by account-status
        # governance.  Without it, two administrators removing each other's
        # role can lock opposite user rows and deadlock while each scans the
        # remaining active administrators.
        cur.execute("SELECT pg_advisory_xact_lock(920716,1)")
        # Commercial writers lock the current binding projection before the
        # provider user row.  Preserve that order here: otherwise an admin
        # grant racing a rebind can either deadlock or make a committed service
        # principal immediately ineligible in the canonical resolver.
        provider_binding_ids: List[int] = []
        if administrator:
            from services.commercial_service_routing import lock_commercial_provider_lifecycle

            lock_commercial_provider_lifecycle(cur, int(user_id))
            cur.execute(
                """SELECT id FROM customer_agent_bindings
                   WHERE agent_user_id=%s ORDER BY id FOR UPDATE""",
                (int(user_id),),
            )
            provider_binding_ids = [int(row["id"]) for row in cur.fetchall() or []]
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
        if not cur.fetchone():
            raise GovernanceNotFound(f"user {user_id}")
        current_version = _lock_version(cur, user_id, "platform_access", expected_version)
        cur.execute("SELECT id FROM roles WHERE name='admin' FOR UPDATE")
        role = cur.fetchone()
        if not role:
            raise GovernanceValidationError("ADMIN_ROLE_MISSING", "管理员权限模板不存在，已拒绝修改")
        role_id = int(role["id"])
        cur.execute("SELECT 1 FROM user_roles WHERE user_id=%s AND role_id=%s", (int(user_id), role_id))
        current = bool(cur.fetchone())
        if current == bool(administrator):
            raise GovernanceValidationError("NO_CHANGE", "平台管理员权限没有变化")
        if administrator:
            if is_platform_direct_service_user(user_id):
                raise GovernanceValidationError(
                    "ACTIVE_COMMERCIAL_PROVIDER",
                    "该账号是平台直营承接账号；授予管理员权限会中断客户购买，已拒绝修改",
                )
            # Repeat after the provider row lock.  A concurrent binding writer
            # that committed while the first scan waited is now visible; a
            # writer still in flight must validate the newly granted role after
            # waiting for this same user row.
            cur.execute(
                """SELECT id FROM customer_agent_bindings
                   WHERE agent_user_id=%s ORDER BY id FOR UPDATE""",
                (int(user_id),),
            )
            provider_binding_ids.extend(
                int(row["id"]) for row in cur.fetchall() or []
            )
            if provider_binding_ids:
                raise GovernanceValidationError(
                    "ACTIVE_COMMERCIAL_PROVIDER",
                    "该服务商仍承接客户；授予管理员权限会使现有商业服务关系失效，已拒绝修改",
                )
        if current and not administrator:
            if int(user_id) == int(operator_user_id):
                raise GovernanceValidationError("SELF_ADMIN_REMOVAL", "不能移除自己的管理员权限")
            cur.execute(
                """SELECT u.id FROM users u JOIN user_roles ur ON ur.user_id=u.id
                   WHERE ur.role_id=%s AND COALESCE(u.is_active,1)=1 FOR UPDATE OF u""",
                (role_id,),
            )
            admin_ids = [int(r["id"]) for r in cur.fetchall() or []]
            if len(admin_ids) <= 1:
                raise GovernanceValidationError("LAST_ADMIN", "不能移除最后一个有效管理员")
        if administrator:
            cur.execute(
                "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (int(user_id), role_id),
            )
        else:
            cur.execute("DELETE FROM user_roles WHERE user_id=%s AND role_id=%s", (int(user_id), role_id))
        cur.execute(
            """UPDATE users SET permission_version=COALESCE(permission_version,1)+1
               WHERE id=%s RETURNING permission_version""",
            (int(user_id),),
        )
        permission_version = int(cur.fetchone()["permission_version"])
        before = {"platform_access": "administrator" if current else "standard"}
        after = {"platform_access": "administrator" if administrator else "standard"}
        next_version = _advance_version(cur, user_id, "platform_access", current_version)
        _write_audits(
            cur, subject_user_id=user_id, scope="platform_access",
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, reason=reason, before=before, after=after,
            version_before=current_version, version_after=next_version, ip_address=ip_address,
            evidence={"legacy_roles_preserved": True, "permission_version_incremented": True},
        )
        _enqueue_account_change(
            cur,
            user_id=user_id,
            event_type="account.permissions_changed",
            scope="platform_access",
            version=next_version,
            status="账号权限已变更",
            summary="账号可用功能可能发生变化，请重新登录后查看。",
        )
        return {"success": True, "scope": "platform_access", "version": next_version,
                "request_id": request_id, "before": before, "after": after,
                "permission_version": permission_version}


def change_channel_relationship(
    user_id: int, upstream_user_id: Optional[int], cost_multiplier_bps: int, *,
    expected_version: int, reason: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """Change one service provider's immediate upstream with CAS and audit.

    🔴 唯一实现在 `change_channel_relationship_cur`,本函数只是薄壳。
       拆开的原因:渠道合作申请的「审批」要把
       「批准请求」与「落渠道关系」放进同一个事务(工单 §P0-2),
       而自持连接的函数没法加入调用方的事务。**不是第二套写路径**。
    """
    with get_db() as conn:
        cur = conn.cursor()
        return change_channel_relationship_cur(
            cur, user_id, upstream_user_id, cost_multiplier_bps,
            expected_version=expected_version, reason=reason,
            operator_user_id=operator_user_id, operator_username=operator_username,
            request_id=request_id, ip_address=ip_address,
        )


def change_channel_relationship_cur(
    cur, user_id: int, upstream_user_id: Optional[int], cost_multiplier_bps: int, *,
    expected_version: int, reason: str, operator_user_id: int,
    operator_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> Dict[str, Any]:
    """主事务内版本 · 调用方负责 commit/rollback。"""
    from services.channel_pricing import (
        ChannelError,
        archive_relationship_cur,
        get_active_relationship,
        lock_channel_relationship_graph,
        save_relationships_cur,
    )

    # Existing relationship writers acquire the graph lock before actor-row
    # locks. Preserve that order to avoid deadlocks with pricing admin APIs.
    lock_channel_relationship_graph(cur)
    cur.execute(
        """SELECT u.id, COALESCE(w.agent_level,0) AS agent_level
           FROM users u JOIN user_wallets w ON w.user_id=u.id
           WHERE u.id=%s AND COALESCE(u.is_active,1)=1
           FOR UPDATE OF u, w""",
        (int(user_id),),
    )
    subject = cur.fetchone()
    if not subject:
        raise GovernanceNotFound(f"user {user_id}")
    if int(subject.get("agent_level") or 0) < 1:
        raise GovernanceValidationError(
            "CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER",
            "只有服务商账号可以设置渠道上游",
        )
    current_version = _lock_version(cur, user_id, "channel_relationship", expected_version)
    current = get_active_relationship(int(user_id), cur=cur)
    old_upstream = int(current["upstream_channel_account_id"]) if current else None
    old_multiplier = int(current["cost_multiplier_bps"]) if current else None
    upstream_user_id = int(upstream_user_id) if upstream_user_id is not None else None
    if upstream_user_id == int(user_id):
        raise GovernanceValidationError("SELF_CHANNEL_RELATIONSHIP", "不能将服务商归属给自己")
    if upstream_user_id is None and current is None:
        raise GovernanceValidationError("NO_CHANGE", "该服务商当前已经是平台根渠道")
    if (
        current is not None
        and old_upstream == upstream_user_id
        and old_multiplier == int(cost_multiplier_bps)
    ):
        raise GovernanceValidationError("NO_CHANGE", "渠道关系和进货系数没有变化")

    before = {
        "channel_upstream_user_id": old_upstream,
        "channel_mode": "upstream_channel" if old_upstream else "platform_root",
        "cost_multiplier_bps": old_multiplier,
    }
    try:
        if upstream_user_id is None:
            changed = archive_relationship_cur(
                cur,
                int(user_id),
                expected_relationship_version=str(current["relationship_version"]),
            )
            relationship_id = int(changed["id"]) if changed else None
            relationship_version = None
        else:
            relationship_version = f"admin-{request_id}"[:180]
            saved = save_relationships_cur(
                cur,
                [{
                    "buyer_dealer_id": int(user_id),
                    "upstream_channel_account_id": upstream_user_id,
                    "expected_relationship_version": (
                        str(current["relationship_version"]) if current else None
                    ),
                    "new_relationship_version": relationship_version,
                    "cost_multiplier_bps": int(cost_multiplier_bps),
                    "reason": reason,
                }],
                approved_by=int(operator_user_id),
                created_by=int(operator_user_id),
            )
            relationship_id = int(saved["relationships"][0]["id"])
    except ChannelError as exc:
        raise GovernanceValidationError("CHANNEL_RELATIONSHIP_INVALID", str(exc)) from exc

    after = {
        "channel_upstream_user_id": upstream_user_id,
        "channel_mode": "upstream_channel" if upstream_user_id else "platform_root",
        "cost_multiplier_bps": int(cost_multiplier_bps) if upstream_user_id else None,
    }
    next_version = _advance_version(cur, user_id, "channel_relationship", current_version)
    _write_audits(
        cur,
        subject_user_id=user_id,
        scope="channel_relationship",
        operator_user_id=operator_user_id,
        operator_username=operator_username,
        request_id=request_id,
        reason=reason,
        before=before,
        after=after,
        version_before=current_version,
        version_after=next_version,
        ip_address=ip_address,
        evidence={
            "relationship_id": relationship_id,
            "relationship_version": relationship_version,
            "historical_orders_untouched": True,
            "pricing_snapshots_untouched": True,
        },
    )
    # 🔴 [P0-C 根治 · §9.4.3] 「设上级」是 Owner 口述的另一条路径
    #    (「我手动设了上级、设成服务商」)。这条路径**不会**移除已有的人工归属,
    #    于是那条归属继续缺 `commercial_binding` 凭证 → 灯一直亮。
    #    u133 / u163 的渠道关系版本号(`identity-upgrade-…`)就是走这条来的。
    #    这里同事务补齐;灯已灭的、非人工归属的会被 `_autofill…` 内部的闸跳过。
    _autofill_commercial_binding_evidence(
        cur, user_id=user_id, operator_user_id=operator_user_id,
        operator_username=operator_username, request_id=request_id,
        reason=f"随直属上游变更同事务补录归属凭证 · {reason}"[:500],
        ip_address=ip_address,
    )
    return {
        "success": True,
        "scope": "channel_relationship",
        "version": next_version,
        "request_id": request_id,
        "before": before,
        "after": after,
    }


def adjust_user_wallet(
    user_id: int,
    *,
    point_type: str,
    operation: str,
    amount: int,
    expected_version: int,
    reason: str,
    operator_user_id: int,
    operator_username: Optional[str],
    request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """Apply an audited non-revenue wallet correction in one transaction."""
    if point_type not in {"paid", "bonus"}:
        raise GovernanceValidationError("WALLET_POINT_TYPE_INVALID", "算力类型无效")
    if operation not in {"add", "deduct", "set"}:
        raise GovernanceValidationError("WALLET_OPERATION_INVALID", "校正方式无效")
    amount = int(amount)
    if amount < 0 or amount > 1_000_000_000_000_000:
        raise GovernanceValidationError("WALLET_AMOUNT_INVALID", "算力数值超出允许范围")
    if operation in {"add", "deduct"} and amount == 0:
        raise GovernanceValidationError("WALLET_AMOUNT_INVALID", "增加或扣减的算力必须大于 0")

    column = "paid_points" if point_type == "paid" else "bonus_points"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(user_id),))
        if not cur.fetchone():
            raise GovernanceNotFound(f"user {user_id}")
        current_version = _lock_version(cur, user_id, "wallet_adjustment", expected_version)
        cur.execute(
            "INSERT INTO user_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (int(user_id),),
        )
        cur.execute(
            "SELECT paid_points,bonus_points,total_recharged FROM user_wallets "
            "WHERE user_id=%s FOR UPDATE",
            (int(user_id),),
        )
        wallet = cur.fetchone()
        if not wallet:
            raise GovernanceValidationError("WALLET_UNAVAILABLE", "用户钱包不可用")
        before = {
            "paid_points": int(wallet.get("paid_points") or 0),
            "bonus_points": int(wallet.get("bonus_points") or 0),
        }
        current = int(before[f"{point_type}_points"])
        if operation == "add":
            target = current + amount
        elif operation == "deduct":
            target = current - amount
        else:
            target = amount
        if target < 0:
            raise GovernanceValidationError("WALLET_BALANCE_INSUFFICIENT", "扣减后算力不能小于 0")
        if target > 9_223_372_036_854_775_807:
            raise GovernanceValidationError("WALLET_AMOUNT_INVALID", "校正后算力超出账本范围")
        if target == current:
            raise GovernanceValidationError("NO_CHANGE", "钱包算力没有变化")

        cur.execute(
            f"UPDATE user_wallets SET {column}=%s,updated_at=NOW() "
            "WHERE user_id=%s RETURNING paid_points,bonus_points,total_recharged",
            (target, int(user_id)),
        )
        updated = cur.fetchone()
        delta = target - current
        from db.wallet_db import insert_transaction

        transaction_id = insert_transaction(
            cur,
            int(user_id),
            "admin_adjust",
            point_type,
            delta,
            target,
            description=f"最高管理员算力校正：{reason}",
            order_id=f"admin-governance:{request_id}",
            source="admin_governance",
        )
        after = {
            "paid_points": int(updated.get("paid_points") or 0),
            "bonus_points": int(updated.get("bonus_points") or 0),
        }
        next_version = _advance_version(cur, user_id, "wallet_adjustment", current_version)
        _write_audits(
            cur,
            subject_user_id=user_id,
            scope="wallet_adjustment",
            operator_user_id=operator_user_id,
            operator_username=operator_username,
            request_id=request_id,
            reason=reason,
            before=before,
            after=after,
            version_before=current_version,
            version_after=next_version,
            ip_address=ip_address,
            evidence={
                "operation": operation,
                "point_type": point_type,
                "delta_points": delta,
                "point_transaction_id": transaction_id,
                "accounting_class": "non_revenue_admin_correction",
                "total_recharged_unchanged": (
                    int(updated.get("total_recharged") or 0)
                    == int(wallet.get("total_recharged") or 0)
                ),
            },
        )
        return {
            "success": True,
            "scope": "wallet_adjustment",
            "version": next_version,
            "request_id": request_id,
            "before": before,
            "after": after,
        }


def reset_user_password(
    user_id: int, new_password: str, *, expected_version: int, reason: str,
    operator_user_id: int, operator_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> Dict[str, Any]:
    """Reset a password without returning or auditing password material."""
    from db.auth_db import hash_password

    if (
        len(new_password) < 8
        or new_password != new_password.strip()
        or len(new_password.encode("utf-8")) > 72
    ):
        raise GovernanceValidationError(
            "PASSWORD_POLICY_INVALID",
            "密码必须至少 8 个字符、首尾无空格且编码后不超过 72 字节",
        )
    password_hash = hash_password(new_password)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, must_change_password FROM users WHERE id=%s FOR UPDATE""",
            (int(user_id),),
        )
        user = cur.fetchone()
        if not user:
            raise GovernanceNotFound(f"user {user_id}")
        current_version = _lock_version(cur, user_id, "password_security", expected_version)
        before = {
            "password_state": (
                "reset_required" if bool(user.get("must_change_password")) else "active"
            )
        }
        cur.execute(
            """UPDATE users
               SET password_hash=%s, must_change_password=1,
                   permission_version=COALESCE(permission_version,1)+1
               WHERE id=%s RETURNING permission_version""",
            (password_hash, int(user_id)),
        )
        permission_version = int(cur.fetchone()["permission_version"])
        after = {"password_state": "reset_required"}
        next_version = _advance_version(cur, user_id, "password_security", current_version)
        _write_audits(
            cur,
            subject_user_id=user_id,
            scope="password_security",
            operator_user_id=operator_user_id,
            operator_username=operator_username,
            request_id=request_id,
            reason=reason,
            before=before,
            after=after,
            version_before=current_version,
            version_after=next_version,
            ip_address=ip_address,
            evidence={
                "password_material_logged": False,
                "must_change_password": True,
                "permission_version_incremented": True,
            },
        )
        _enqueue_account_change(
            cur,
            user_id=user_id,
            event_type="account.password_changed",
            scope="password_security",
            version=next_version,
            status="管理员已重置登录密码",
            summary="请使用新密码登录，并按提示完成密码更新。",
        )
        return {
            "success": True,
            "scope": "password_security",
            "version": next_version,
            "request_id": request_id,
            "before": before,
            "after": after,
            "permission_version": permission_version,
        }
