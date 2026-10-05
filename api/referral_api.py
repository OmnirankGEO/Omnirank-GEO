"""
推荐/代理 API
- 推荐码生成与管理
- 佣金统计与明细
- 佣金转积分（三轨切割）
- 推荐网络查询
- 白标设置 CRUD
"""

import http.client
import ipaddress
import logging
import re
import shortuuid
import uuid
from urllib.parse import urljoin, urlsplit
from fastapi import APIRouter, Request, HTTPException, Depends
from auth.agreement_gate import require_signed_agreement
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Optional
from db.connection import get_db, get_connection
from db.wallet_db import insert_transaction, get_or_create_wallet
from services.public_whitelabel import (
    is_backoffice_brand_allowed,
    is_safe_public_whitelabel_logo_url,
    is_whitelabel_active_for_customer,
)

logger = logging.getLogger("GEO-Referral-API")

router = APIRouter(prefix="/api/referral", tags=["推荐/代理"])
public_router = APIRouter(tags=["推荐/代理公开"])


def _get_user(request: Request) -> dict:
    """统一认证检查，避免 request.state.user 为 None 时 AttributeError"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _to_quote_number(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        number = float(value)
        if number != number:  # NaN
            return float(default)
        return number
    except Exception:
        return float(default)


class PublicQuoteServiceDTO(BaseModel):
    """Anonymous quote surface. No internal service JSON key is permitted."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1_000_000)
    unit_price: float = Field(ge=0, le=2_000_000_000)
    subtotal: float = Field(ge=0, le=2_000_000_000)


def _normalize_quote_services(services, *, reject_internal_fields: bool = False) -> list[dict]:
    """Serialize quote lines through the anonymous-surface allowlist."""
    normalized = []
    if not isinstance(services, list):
        return normalized
    for service in services:
        if not isinstance(service, dict):
            continue
        allowed_input = {"name", "quantity", "unit_price", "subtotal", "price", "total"}
        if reject_internal_fields and (set(service) - allowed_input):
            raise HTTPException(422, detail="报价明细包含不允许公开的内部字段")
        quantity = _to_quote_number(service.get("quantity"), 1.0)
        if quantity <= 0:
            quantity = 1.0
        unit_price = _to_quote_number(
            service.get("unit_price", service.get("price")),
            0.0,
        )
        calculated_subtotal = quantity * unit_price
        subtotal = service.get("subtotal", service.get("total"))
        subtotal_value = _to_quote_number(subtotal, calculated_subtotal)
        if subtotal_value <= 0 < calculated_subtotal:
            subtotal_value = calculated_subtotal
        try:
            item = PublicQuoteServiceDTO.model_validate({
                "name": str(service.get("name") or "").strip(),
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": subtotal_value,
            })
        except ValidationError:
            if reject_internal_fields:
                raise HTTPException(422, detail="报价明细格式无效")
            continue
        normalized.append(item.model_dump())
    return normalized


def _public_quote_whitelabel(value) -> dict:
    """Read-time privacy serializer for both new and historical quote snapshots."""
    if not isinstance(value, dict):
        return {}
    logo_url = str(value.get("logo_url") or "").strip()
    return {
        "company_name": str(value.get("company_name") or "").strip(),
        "logo_url": logo_url if is_safe_public_whitelabel_logo_url(logo_url) else "",
        "slogan": str(value.get("slogan") or "").strip(),
    }

COMMISSION_RATE_L1 = 0.12   # L1 直推 12%（v3.1 legacy）
COMMISSION_RATE_L2 = 0.03   # L2 二级 3%（v3.1 legacy）
EXPANSION_RATE = 0.20        # 佣金膨胀系数
POINTS_PER_YUAN = 130        # 1元 = 130积分

# v3.2 新佣金率（全员对称 2 级模型）
COMMISSION_RATE_L1_V32 = 0.18   # 直推 18%(示例值)
COMMISSION_RATE_L2_V32 = 0.03   # 间推 3%(示例值)
COMMISSION_OBSERVATION_DAYS = 3  # T+3 观察期（对齐 3 天退款窗口）

# v3.4 普通用户推广返利率（不走 T+3，即时 bonus_points 到账）
FREE_USER_REFERRAL_RATE = 0.15  # 15%


# ==================== 数据库初始化 ====================

def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    """安全添加列：先检查再 ALTER，避免不必要的排他锁（复用 db/diagnosis_db.py 同款幂等模式）"""
    if not _column_exists(cursor, table, column):
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # 并发竞态：另一个 worker 已经添加了


# v3.6 白标：whitelabel_settings 正本清源（基表只声明 7 列，历史靠手工 ALTER 加了 4 列）
# + 双档授权 / OEM 列。全部走 _safe_add_column 幂等兜底，既有 DB / fresh DB 都可重建。
_WHITELABEL_V36_COLUMNS = [
    # —— 漂移列正本清源（与生产手工 ALTER 对齐：brand_color 是 varchar(20)）——
    ("logo_url", "TEXT"),
    ("contact_wechat", "TEXT"),
    ("slogan", "TEXT"),
    ("brand_color", "VARCHAR(20)"),
    # —— v3.6 授权位（只能 admin 写 · 见 T3）——
    ("whitelabel_mode", "TEXT DEFAULT 'none'"),       # none | external_only | oem
    ("whitelabel_status", "TEXT DEFAULT 'locked'"),   # locked | active | suspended
    ("unlocked_by_admin", "BOOLEAN DEFAULT FALSE"),
    ("approved_by", "INTEGER"),
    ("approved_at", "TIMESTAMP"),
    # —— OEM 档品牌字段 ——
    ("product_name", "TEXT"),
    ("favicon_url", "TEXT"),
    ("hide_platform_branding", "BOOLEAN DEFAULT FALSE"),
    ("custom_domain", "TEXT"),  # 决策点 D：建列不实装（自有域名留 v3.7）
    # —— P1（2026-06-06）轻量白牌条款:operator 首次用基础白标须同意《对外品牌使用条款》(非代理协议)——
    ("brand_terms_accepted_at", "TIMESTAMP"),
]


# 板块 C（2026-07-22 · Owner 裁决 D1/D2）：backoffice 独立授权位 + 品牌版本号。
# 与 scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql 一致（启动自检兜底，
# 防 migration 未跑环境生产 SELECT 报错 · 元指令 17）。
_WHITELABEL_BACKOFFICE_COLUMNS = [
    ("backoffice_brand_unlocked", "BOOLEAN NOT NULL DEFAULT FALSE"),  # D2 后台换肤独立授权（仅 admin 写）
    ("backoffice_brand_granted_by", "INTEGER"),                       # admin 授权痕迹
    ("backoffice_brand_granted_at", "TIMESTAMPTZ"),
    ("brand_version", "INTEGER NOT NULL DEFAULT 1"),                  # D1 版本化：品牌字段/授权位变更 +1
]

# [客户反馈④ 2026-08-09] 后台换肤被 D2 裁决**钉死为 customer-only** 的账号。
#
# 🔴 这里**不是**第二份授权口径 —— 授权口径的权威在
#   `scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql`:
#     §4  每次部署把这些账号的 backoffice_brand_unlocked 重置为 FALSE;
#     §6  `whitelabel_backoffice_schema_blockers()` 把它们为 TRUE 定义成 blocker,
#         prestart / 运行时自检 / release readiness 三处 fail-closed。
#   本常量的唯一作用是让**写入侧提前拒绝并说人话**,而不是让 admin 写进去、
#   库进入 readiness 合同定义的非法态、再被下一次部署无声抹掉。
#   `tests/custfb_2026_08_09/test_backoffice_pin_guard_2026_08_09.py`
#   有一条一致性锁:本集合必须与迁移文件里被点名的 id 集合逐字相同。
BACKOFFICE_BRAND_PINNED_USER_IDS = frozenset({132})


# whitelabel_settings 全 25 列预期集（7 基表列 + 14 v3.6 列 + 4 板块C列）·
# 供 _safe_add_column 收敛循环的测试断言用；readiness 判定统一走
# services/whitelabel_backoffice_schema_contract（单一合同函数，不写第二份口径）。
_WHITELABEL_BASE_COLUMNS = {
    "user_id", "company_name", "company_logo_url",
    "contact_name", "contact_phone", "contact_email", "updated_at",
}
_WHITELABEL_EXPECTED_COLUMNS = (
    _WHITELABEL_BASE_COLUMNS
    | {c for c, _ in _WHITELABEL_V36_COLUMNS}
    | {c for c, _ in _WHITELABEL_BACKOFFICE_COLUMNS}
)


def _migrate_whitelabel_settings_v36():
    """既有 DB 补齐 whitelabel_settings 的漂移列 + v3.6 授权/OEM 列。

    用独立 autocommit 连接：每条 ALTER 独立事务，单条失败不污染后续
    （对齐 db/diagnosis_db.init_db 的 autocommit=True 模式）。

    post-migration 校验（统一 R3 §七 收敛）：收敛完成后调**同一 readiness 合同函数**
    public.whitelabel_backoffice_schema_blockers（经薄调用方
    services/whitelabel_backoffice_schema_contract），与 migration 自验块 /
    prestart / 统一 release readiness 同一口径（列 type/null/default + audit
    完整列/PK + 索引归属/列序/定义/三状态 + append-only trigger/function + ID132
    不变式）；drift 即 fail —— 不靠 try/except pass 静默吞，也不再维护第二份
    仅列名的校验口径。
    """
    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()
        for col, col_type in _WHITELABEL_V36_COLUMNS:
            _safe_add_column(cursor, "whitelabel_settings", col, col_type)
        # 板块 C（2026-07-22）：backoffice 授权位 + brand_version 同款幂等兜底
        for col, col_type in _WHITELABEL_BACKOFFICE_COLUMNS:
            _safe_add_column(cursor, "whitelabel_settings", col, col_type)

        # 单一 readiness 合同（统一 R3 §七）：基表与 audit/触发器已由
        # init_referral_tables 建齐（含 append-only 自愈），此处必须全量就绪。
        from services.whitelabel_backoffice_schema_contract import (
            assert_whitelabel_backoffice_schema_ready,
        )

        assert_whitelabel_backoffice_schema_ready(cursor, require_settings=True)
        logger.info("[whitelabel-v36] whitelabel schema 合同校验通过（同一 readiness 合同函数）")
    finally:
        conn.close()


def _get_whitelabel_mode(user_id: int) -> str:
    """读用户 whitelabel_mode（none/external_only/oem）。无记录 → 'none'。

    v3.6 白标功能访问 gate 用此替代 agent_level>=1（白标授权由 admin 控）。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT whitelabel_mode FROM whitelabel_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return "none"
        return (row.get("whitelabel_mode") if isinstance(row, dict) else row[0]) or "none"
    finally:
        conn.close()


# 板块 C（2026-07-22 R3 · P2）：whitelabel_audit append-only DB 层强制。
# 对标 db/monitoring_db.py 的 monitoring_identity_decision_events append-only 模式
# （reject_monitoring_identity_event_mutation 函数 + BEFORE UPDATE OR DELETE 触发器）:
# 应用层约定（只 INSERT/SELECT）之外，DB 触发器硬强制 UPDATE/DELETE 直接 RAISE。
_WHITELABEL_AUDIT_APPEND_ONLY_NAME = "trg_whitelabel_audit_append_only"

_WHITELABEL_AUDIT_APPEND_ONLY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.trg_whitelabel_audit_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'whitelabel_audit is append-only';
END;
$$;
"""

_WHITELABEL_AUDIT_APPEND_ONLY_TRIGGER_SQL = """
CREATE TRIGGER trg_whitelabel_audit_append_only
BEFORE UPDATE OR DELETE ON public.whitelabel_audit
FOR EACH ROW EXECUTE FUNCTION public.trg_whitelabel_audit_append_only();
"""

_WHITELABEL_AUDIT_TRIGGER_EXISTS_SQL = """
SELECT t.tgname
  FROM pg_catalog.pg_trigger t
  JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname = 'whitelabel_audit'
   AND t.tgname = %s
   AND NOT t.tgisinternal
"""


def _whitelabel_audit_trigger_present(cursor) -> bool:
    cursor.execute(_WHITELABEL_AUDIT_TRIGGER_EXISTS_SQL, (_WHITELABEL_AUDIT_APPEND_ONLY_NAME,))
    return cursor.fetchone() is not None


def _ensure_whitelabel_audit_append_only(cursor) -> None:
    """whitelabel_audit append-only 触发器 drift 自检（启动自检 · fail-closed）。

    - 触发器存在 → no-op；
    - 缺失 → 幂等自建（CREATE OR REPLACE FUNCTION + DROP IF EXISTS + CREATE TRIGGER，
      与 migration_whitelabel_backoffice_scope_2026_07_22.sql §5 同文）；
    - 自建后复查仍缺 → raise（fail-closed：drift 即部署事故，不静默降级；
      与 monitoring_db.assert_monitoring_identity_review_ready 同口径）。
    """
    if _whitelabel_audit_trigger_present(cursor):
        return
    logger.warning("[whitelabel-audit] append-only 触发器缺失，启动自检尝试幂等自建")
    cursor.execute(_WHITELABEL_AUDIT_APPEND_ONLY_FUNCTION_SQL)
    cursor.execute(
        "DROP TRIGGER IF EXISTS trg_whitelabel_audit_append_only ON public.whitelabel_audit"
    )
    cursor.execute(_WHITELABEL_AUDIT_APPEND_ONLY_TRIGGER_SQL)
    if not _whitelabel_audit_trigger_present(cursor):
        raise RuntimeError(
            "[whitelabel-audit] append-only 触发器自建失败（fail-closed）："
            "whitelabel_audit 失去 DB 层 UPDATE/DELETE 强制，拒绝静默继续"
        )
    logger.info("[whitelabel-audit] append-only 触发器自建完成")


def init_referral_tables():
    """创建推荐/白标相关表（幂等）"""
    with get_db() as conn:
        cursor = conn.cursor()

        # 推荐码表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referral_codes (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                code TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 佣金记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS commission_records (
                id BIGSERIAL PRIMARY KEY,
                referrer_id INTEGER NOT NULL REFERENCES users(id),
                referred_id INTEGER NOT NULL REFERENCES users(id),
                level INTEGER NOT NULL,
                trigger_amount_cents INTEGER NOT NULL,
                commission_rate NUMERIC(4,3) NOT NULL,
                commission_yuan NUMERIC(10,2) NOT NULL,
                status TEXT DEFAULT 'pending',
                converted_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_comm_referrer
            ON commission_records(referrer_id, created_at DESC)
        """)

        # 白标设置表（v3.6 正本清源：CREATE 声明全 20 列，fresh DB 直接齐；
        # 既有 DB 由下方 _migrate_whitelabel_settings_v36() 用 _safe_add_column 补齐）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whitelabel_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                company_name TEXT,
                company_logo_url TEXT,
                logo_url TEXT,
                slogan TEXT,
                brand_color VARCHAR(20),
                contact_name TEXT,
                contact_phone TEXT,
                contact_wechat TEXT,
                contact_email TEXT,
                -- v3.6 授权位（只能 admin 写）
                whitelabel_mode TEXT DEFAULT 'none',
                whitelabel_status TEXT DEFAULT 'locked',
                unlocked_by_admin BOOLEAN DEFAULT FALSE,
                approved_by INTEGER,
                approved_at TIMESTAMP,
                -- v3.6 OEM 档品牌字段
                product_name TEXT,
                favicon_url TEXT,
                hide_platform_branding BOOLEAN DEFAULT FALSE,
                custom_domain TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 板块 C（2026-07-22 · Owner 裁决 D1）：whitelabel_audit 审计表（append-only）。
        # ⚠️ APPEND-ONLY：禁止 UPDATE / DELETE；任何更正以新行追加。应用代码只允许 INSERT/SELECT。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whitelabel_audit (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                actor_user_id INTEGER,
                actor_role TEXT NOT NULL DEFAULT 'agent',
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                request_id TEXT,
                ip TEXT,
                reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_whitelabel_audit_user_created
            ON whitelabel_audit (user_id, created_at DESC)
        """)
        # 板块 C（2026-07-22 R3 · P2）：append-only DB 层强制 drift 自检 ——
        # 触发器缺失则幂等自建，自建失败 fail-closed（不静默降级）。
        _ensure_whitelabel_audit_append_only(cursor)

        # 代理报价单表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_quotes (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                services JSONB NOT NULL,
                total_price NUMERIC(10,2),
                whitelabel JSONB,
                share_code TEXT UNIQUE,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aq_user ON agent_quotes(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aq_share ON agent_quotes(share_code) WHERE share_code IS NOT NULL")

        # 2026-04-18 v3.4: 普通用户 15% 推荐返利明细表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referral_bonus_records (
                id BIGSERIAL PRIMARY KEY,
                referrer_id INTEGER NOT NULL REFERENCES users(id),
                referred_id INTEGER NOT NULL REFERENCES users(id),
                order_id TEXT NOT NULL,
                recharge_yuan NUMERIC(10, 2) NOT NULL,
                rate NUMERIC(4, 3) NOT NULL DEFAULT 0.15,
                bonus_points INTEGER NOT NULL,
                fraud_flag BOOLEAN NOT NULL DEFAULT FALSE,
                refund_linked_order_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rbr_referrer ON referral_bonus_records(referrer_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rbr_order ON referral_bonus_records(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rbr_fraud ON referral_bonus_records(fraud_flag) WHERE fraud_flag = TRUE")

        logger.info("[Referral] 推荐/白标/报价/返利表初始化完成")

    # v3.6 白标：既有 DB 正本清源（补 4 漂移列）+ 授权/OEM 列（autocommit 隔离每条 ALTER）
    _migrate_whitelabel_settings_v36()
    # Existing records are intentionally not grandfathered. Only an explicit
    # admin grant may activate a public brand.


# ==================== 请求模型 ====================

class ConvertCommissionRequest(BaseModel):
    commission_ids: list[int] = Field(default=[], description="要转换的佣金记录ID，空=全部pending")

class WhitelabelUpdateRequest(BaseModel):
    company_name: Optional[str] = None
    company_logo_url: Optional[str] = None
    logo_url: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    contact_wechat: Optional[str] = None
    slogan: Optional[str] = None
    brand_color: Optional[str] = None
    product_name: Optional[str] = None   # v3.6 OEM:产品名（document.title / 工作台品牌）
    favicon_url: Optional[str] = None    # v3.6 OEM:浏览器标签图标
    accept_brand_terms: Optional[bool] = None  # P1:首次同意《对外品牌使用条款》(非授权位·不入 hard-403)


# v3.6 admin 白标授权请求（只能 admin 调 · 写授权位 · 见 T3 约束 4）
# ⚠️ unlocked_by_admin 不接受请求体决定，由 _normalize_whitelabel_grant 强制派生（修 Codex P1：
#    防 admin 写出 status=active + unlocked=false 的矛盾态）
class WhitelabelGrantRequest(BaseModel):
    whitelabel_mode: str = Field(..., description="none | external_only | oem")
    whitelabel_status: str = Field("active", description="active | locked | suspended")
    # 板块 C（Owner 2026-07-22 裁决 D1/D2）：后台换肤独立授权位。
    # None=不改动；True=授予 backoffice 换肤；False=收回。与 mode（客户触达档位）解耦。
    backoffice_brand_unlocked: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=500, description="操作理由（写审计）")


# v3.6 授权/策略字段（代理不可写 · 仅 admin · update_whitelabel hard 403 检测用）
_WL_AUTHZ_FIELDS = frozenset({
    "whitelabel_mode", "whitelabel_status", "unlocked_by_admin",
    "approved_by", "approved_at", "hide_platform_branding",
    # 板块 C（Owner 2026-07-22 D1）：backoffice 授权位 + 版本号同属 admin/system 边界
    "backoffice_brand_unlocked", "backoffice_brand_granted_by",
    "backoffice_brand_granted_at", "brand_version",
})


def _forbidden_whitelabel_authz_fields(body_keys) -> set:
    """代理请求体里命中的授权字段（非空 = 必须 hard 403）。"""
    return set(_WL_AUTHZ_FIELDS) & set(body_keys or ())


def _normalize_whitelabel_grant(mode, status):
    """admin 授权规范化：校验 mode/status + 强制派生 unlocked（不信请求体 · 修 Codex P1）。

    返回 (mode, status, unlocked)；非法 mode/status 抛 ValueError（端点映射 400）。
    - mode='none'（撤销）→ ('none', 'locked', False)
    - mode in (external_only, oem) → unlocked=True（admin 设非 none mode 即授权解锁），
      消除 'status=active 但 unlocked_by_admin=false' 矛盾态。
    """
    mode = (mode or "none").strip()
    status = (status or "locked").strip()
    if mode not in ("none", "external_only", "oem"):
        raise ValueError("whitelabel_mode 非法（none/external_only/oem）")
    if status not in ("locked", "active", "suspended"):
        raise ValueError("whitelabel_status 非法（locked/active/suspended）")
    if mode == "none":
        return "none", "locked", False
    return mode, status, True


# ==================== 板块 C（Owner 2026-07-22 裁决 D1）· 品牌字段校验 + 审计 + 版本化 ====================

_BRAND_TEXT_LIMITS = {
    "company_name": ("公司名称", 100),
    "product_name": ("产品名", 100),
    "contact_name": ("联系人", 50),
    "contact_phone": ("联系电话", 50),
    "contact_email": ("联系邮箱", 254),
    "contact_wechat": ("微信号", 100),
    "slogan": ("品牌标语", 200),
}
# 危险字符：尖/花括号（防 HTML 注入进报告页）、控制符、脚本协议
_BRAND_TEXT_FORBIDDEN_RE = re.compile(
    r"[<>{}\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|javascript:|data:", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BRAND_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_BRAND_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"}
_BRAND_URL_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
    re.IGNORECASE,
)
_WHITELABEL_LOGO_URL_MAX_BYTES = 2 * 1024 * 1024  # 外链图片上限 · 与 _WHITELABEL_LOGO_MAX_BYTES 对齐


def _validate_brand_text(value, field: str):
    """品牌文本字段校验（D1 校验）。None/空串 → None（允许清空）；超限/危险字符 → 400 人话。"""
    if value is None:
        return None
    label, max_len = _BRAND_TEXT_LIMITS[field]
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_len:
        raise HTTPException(400, detail=f"{label}过长（最多 {max_len} 字）")
    if _BRAND_TEXT_FORBIDDEN_RE.search(text):
        raise HTTPException(400, detail=f"{label}包含不允许的字符（请勿使用 <> 括号、控制符或脚本协议）")
    if field == "contact_email" and not _EMAIL_RE.match(text):
        raise HTTPException(400, detail="联系邮箱格式不正确")
    return text


def _validate_brand_color(value):
    """品牌主色校验：#RGB/#RGBA/#RRGGBB/#RRGGBBAA 或空。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _BRAND_COLOR_RE.match(text):
        raise HTTPException(400, detail="品牌主色须为 #RGB 或 #RRGGBB 格式色值")
    return text


def _validate_brand_image_url(value, field_label: str):
    """Logo/Favicon URL 校验（D1 校验 · Owner 2026-07-22）。

    - 空 → None（允许清空）；
    - 本站上传相对路径 /uploads/whitelabel-logos/...（上传时已做魔数+大小校验）→ 放行；
    - 外链仅 https + 域名合法 + 扩展名白名单 + HEAD 探测（图片类型 · ≤2MB · 5s 超时）。
    """
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "file:", "ftp:")):
        raise HTTPException(400, detail=f"{field_label}仅支持 https 图片地址，脚本/数据协议已被拦截")
    if url.startswith("/uploads/"):
        # 本站上传路径：仍须过公开安全路径检查（拦截历史 account-derived 路径）
        if not is_safe_public_whitelabel_logo_url(url):
            raise HTTPException(400, detail=f"{field_label}不合规，请重新上传 Logo")
        return url
    if not lowered.startswith("https://"):
        raise HTTPException(400, detail=f"{field_label}仅支持 https:// 安全链接（也可点「上传 Logo」使用本地上传）")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise HTTPException(400, detail=f"{field_label}格式不正确")
    host = parts.hostname or ""
    if "@" in parts.netloc or not _BRAND_URL_HOST_RE.match(host):
        raise HTTPException(400, detail=f"{field_label}域名不合法")
    path = parts.path or ""
    dot = path.rfind(".")
    ext = path[dot:].lower() if dot >= 0 else ""
    if ext not in _BRAND_IMAGE_EXTS:
        raise HTTPException(400, detail=f"{field_label}仅支持 {' / '.join(sorted(_BRAND_IMAGE_EXTS))} 图片后缀")
    if not is_safe_public_whitelabel_logo_url(url):
        raise HTTPException(400, detail=f"{field_label}含不允许的参数或路径")
    _probe_brand_image_url(url, field_label)
    return url


# [集中严审 R1 · P1-2 → R4 · P1-3] HEAD 探测 SSRF 防线：
# 不再 allow_redirects=True 盲跟 302（可跳到 http://169.254.169.254 / 内网任意 host），
# 改手工逐跳重定向（最多 3 跳），每跳强制 scheme==https + 443 端口 + DNS 解析 IP 必须为公网。
# R4 修复 DNS rebinding：校验阶段解析出的公网 IP 即本跳唯一拨号目标（pinned IP），
# 连接阶段绝不二次解析——攻击者让校验解析返回公网、连接解析返回内网的竞态无处生效；
# TLS SNI / 证书 hostname 校验 / HTTP Host 头仍使用原始域名，证书验证不关闭。
_BRAND_IMAGE_PROBE_MAX_REDIRECTS = 3
_BRAND_IMAGE_PROBE_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# 400 文案统一人话：不回显对端状态码/Content-Type（防内网存活 oracle），
# 解析失败/连接失败/重定向超标同样只给这一句。
_BRAND_IMAGE_PROBE_FAIL_HINT = "不可用或不支持，请更换 https 图片链接"

# [2026-08-19 搬家] 下面这套 SSRF 防线(逐跳校验 + pinned IP 拨号 + 证书不放松)
# 已整块抽到 `services/safe_https_probe.py`,供文章域「发布公开 URL 服务端核实」
# 复用同一条出站防线。本文件按同名 import 回来,行为不变;既有三份白标锁仍
# monkeypatch `api.referral_api._pinned_https_roundtrip`,patch 目标就是下面这个名字。
from services.safe_https_probe import (  # noqa: E402
    _V6_PROBE_DENIED_NETWORKS,
    _V6_COMPAT_ZERO80_NETWORK,
    _V6_NAT64_WKP_NETWORK,
    _V6_6TO4_NETWORK,
    _assert_public_probe_host,
    _assert_safe_probe_hop,
    _PinnedIPHTTPSConnection,
    _pinned_https_roundtrip,
)


def _probe_brand_image_url(url: str, field_label: str) -> None:
    """HEAD 探测外链图片（SSRF 安全版 · R4 · P1-3 DNS rebinding 修复）。

    - 手工逐跳重定向（最多 3 跳），每跳强制 https + 443 + 公网 IP；
    - 校验通过的公网 IP 即本跳唯一拨号目标（pinned），连接阶段绝不二次 DNS，
      彻底关闭"校验解析=公网、连接解析=内网"的 rebinding 竞态；
    - TLS SNI / 证书 hostname / HTTP Host 仍为原始域名，证书验证不关闭；
    - HEAD 405/501 降级流式 GET 只读头，复用同一跳 pinned IP；
    - 保持既有约束：5s 超时 · Content-Type 图片白名单 · ≤2MB；
    - 所有失败统一 400 人话，不回显对端状态码/Content-Type（防内网存活 oracle）。

    测试可 monkeypatch _pinned_https_roundtrip 跳过网络（网络判活不属于纯逻辑断言范围）。
    """
    try:
        current = url
        status = None
        ctype = ""
        clen_raw = None
        for _hop in range(_BRAND_IMAGE_PROBE_MAX_REDIRECTS + 1):
            host, pinned_ips, target = _assert_safe_probe_hop(current)
            status, headers = _pinned_https_roundtrip(host, pinned_ips, "HEAD", target)
            if status in (405, 501):  # 对方不支持 HEAD → 退化 GET 只读头（同一 pinned IP）
                status, headers = _pinned_https_roundtrip(host, pinned_ips, "GET", target)
            if status in _BRAND_IMAGE_PROBE_REDIRECT_STATUSES:
                location = str(headers.get("location") or "").strip()
                if not location:
                    raise ValueError("重定向缺少 Location")
                current = urljoin(current, location)
                continue
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            clen_raw = headers.get("content-length")
            break
        else:
            raise ValueError("重定向次数超限")
        if status is None or status >= 400:
            raise ValueError("对端不可访问")
        if ctype and not ctype.startswith("image/"):
            raise ValueError("对端不是图片")
        if clen_raw:
            try:
                declared_size = int(clen_raw)
            except (TypeError, ValueError):
                declared_size = None  # Content-Length 非数字时不据此拦截
            if declared_size is not None and declared_size > _WHITELABEL_LOGO_URL_MAX_BYTES:
                raise ValueError("图片超过 2MB")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, detail=f"{field_label}{_BRAND_IMAGE_PROBE_FAIL_HINT}") from None


def _validate_whitelabel_update_payload(req: "WhitelabelUpdateRequest") -> dict:
    """对已发送字段做 D1 校验，返回 {字段: 清洗后值}（只含 model_fields_set 字段）。"""
    sent = req.model_fields_set
    cleaned = {}
    for field in _BRAND_TEXT_LIMITS:
        if field in sent:
            cleaned[field] = _validate_brand_text(getattr(req, field), field)
    if "brand_color" in sent:
        cleaned["brand_color"] = _validate_brand_color(req.brand_color)
    if "logo_url" in sent or "company_logo_url" in sent:
        cleaned_logo = _validate_brand_image_url(req.logo_url or req.company_logo_url, "Logo 地址")
        cleaned["logo_url"] = cleaned_logo
        cleaned["company_logo_url"] = cleaned_logo
    if "favicon_url" in sent:
        cleaned["favicon_url"] = _validate_brand_image_url(req.favicon_url, "网站图标地址")
    return cleaned


def _whitelabel_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID")
    return str(rid)[:128] if rid else uuid.uuid4().hex


def _whitelabel_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _write_whitelabel_audit(cursor, *, user_id, actor_user_id, actor_role, changes,
                            request_id=None, ip=None, reason=None):
    """写 whitelabel_audit（D1 审计 · append-only · 每变更字段一行 · old==new 跳过）。

    与品牌 UPDATE 同事务：审计失败即整体回滚（D1 要求审计为硬约束，不静默跳过）。
    """
    for field, old, new in changes:
        if old == new:
            continue
        cursor.execute(
            """
            INSERT INTO whitelabel_audit
                (user_id, actor_user_id, actor_role, field, old_value, new_value, request_id, ip, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(user_id),
                int(actor_user_id) if actor_user_id is not None else None,
                actor_role,
                str(field),
                None if old is None else str(old),
                None if new is None else str(new),
                request_id,
                ip,
                reason,
            ),
        )


def _bump_brand_version(cursor, user_id, current_version=None) -> int:
    """D1 版本化：品牌有效载荷每次变更 +1（缓存键 (owner, surface, brand_version) 的失效维度）。"""
    cursor.execute(
        "UPDATE whitelabel_settings SET brand_version = COALESCE(brand_version, 1) + 1 WHERE user_id = %s",
        (int(user_id),),
    )
    try:
        return int(1 if current_version is None else current_version) + 1
    except (TypeError, ValueError):
        return 2


# ==================== 推荐码 ====================

@router.get("/code")
async def get_my_referral_code(request: Request):
    """获取或生成我的推荐码"""
    user = _get_user(request)
    wallet = get_or_create_wallet(user["user_id"])

    # 注册即给推荐码，不限制 agent_level
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user["user_id"],))
        row = cursor.fetchone()
        if row:
            return {"success": True, "data": {"code": row["code"]}}

        # 生成推荐码
        code = f"OR-{shortuuid.uuid()[:8].upper()}"
        cursor.execute(
            "INSERT INTO referral_codes (user_id, code) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING code",
            (user["user_id"], code)
        )
        conn.commit()
        result = cursor.fetchone()
        return {"success": True, "data": {"code": result["code"] if result else code}}
    finally:
        conn.close()


@router.get("/stats")
async def get_referral_stats(request: Request):
    """推荐统计（人数、佣金、等级）"""
    user = _get_user(request)
    wallet = get_or_create_wallet(user["user_id"])

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 推荐人数
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE level = 1) AS l1_count,
                COUNT(*) FILTER (WHERE level = 2) AS l2_count
            FROM referral_links WHERE referrer_id = %s
        """, (user["user_id"],))
        counts = cursor.fetchone()

        # 佣金统计
        cursor.execute("""
            SELECT
                COALESCE(SUM(commission_yuan), 0) AS total_commission,
                COALESCE(SUM(commission_yuan) FILTER (WHERE created_at >= date_trunc('month', CURRENT_DATE)), 0) AS month_commission,
                COALESCE(SUM(commission_yuan) FILTER (WHERE status = 'pending'), 0) AS pending_commission
            FROM commission_records WHERE referrer_id = %s
        """, (user["user_id"],))
        commission = cursor.fetchone()

        return {"success": True, "data": {
            "agent_level": wallet["agent_level"],
            "total_recharged_yuan": wallet["total_recharged"] / 100,
            "l1_count": counts["l1_count"] or 0,
            "l2_count": counts["l2_count"] or 0,
            "total_commission": float(commission["total_commission"]),
            "month_commission": float(commission["month_commission"]),
            "pending_commission": float(commission["pending_commission"]),
        }}
    finally:
        conn.close()


@router.get("/commissions")
async def list_commissions(request: Request, status: str = None, limit: int = 50, offset: int = 0):
    """佣金明细"""
    user = _get_user(request)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT cr.*, u.display_name AS referred_name
                FROM commission_records cr
                LEFT JOIN users u ON u.id = cr.referred_id
                WHERE cr.referrer_id = %s AND cr.status = %s
                ORDER BY cr.created_at DESC LIMIT %s OFFSET %s
            """, (user["user_id"], status, limit, offset))
        else:
            cursor.execute("""
                SELECT cr.*, u.display_name AS referred_name
                FROM commission_records cr
                LEFT JOIN users u ON u.id = cr.referred_id
                WHERE cr.referrer_id = %s
                ORDER BY cr.created_at DESC LIMIT %s OFFSET %s
            """, (user["user_id"], limit, offset))
        return {"success": True, "data": [dict(r) for r in cursor.fetchall()]}
    finally:
        conn.close()


@router.get("/bonus-history")
async def get_referral_bonus_history(request: Request, limit: int = 20, offset: int = 0):
    """普通用户推广返利明细（15% 即时 bonus）

    返回可信记录列表 + 可疑记录数（后者不展示详情只报总数给运营看）。
    """
    user = _get_user(request)
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT rbr.id, rbr.recharge_yuan, rbr.rate, rbr.bonus_points,
                   rbr.fraud_flag, rbr.created_at,
                   u.display_name AS referred_name, u.username AS referred_username
            FROM referral_bonus_records rbr
            LEFT JOIN users u ON u.id = rbr.referred_id
            WHERE rbr.referrer_id = %s
              AND rbr.fraud_flag = FALSE
            ORDER BY rbr.created_at DESC LIMIT %s OFFSET %s
        """, (user["user_id"], limit, offset))
        rows = cursor.fetchall()

        cursor.execute("""
            SELECT
                COALESCE(SUM(bonus_points) FILTER (WHERE fraud_flag = FALSE), 0) AS total_bonus,
                COUNT(*) FILTER (WHERE fraud_flag = FALSE) AS valid_count,
                COUNT(*) FILTER (WHERE fraud_flag = TRUE)  AS suspicious_count
            FROM referral_bonus_records WHERE referrer_id = %s
        """, (user["user_id"],))
        stats = cursor.fetchone()

        def _iso(d):
            return d.isoformat() if hasattr(d, "isoformat") else str(d) if d else None

        return {
            "success": True,
            "data": {
                "records": [{
                    "id": r["id"],
                    "recharge_yuan": float(r["recharge_yuan"]),
                    "rate": float(r["rate"]),
                    "bonus_points": int(r["bonus_points"]),
                    "referred_name": r.get("referred_name") or r.get("referred_username") or "朋友",
                    "created_at": _iso(r["created_at"]),
                } for r in rows],
                "total_bonus_points": int(stats["total_bonus"]),
                "total_count": int(stats["valid_count"]),
                "suspicious_count": int(stats["suspicious_count"]),
            }
        }
    finally:
        conn.close()


@router.post("/convert")
async def convert_commission_to_points(req: ConvertCommissionRequest, request: Request):
    """
    佣金转积分（2026-04-18 v3.4 三轨切割）
    本金(100%) → commission_points （可消费，可提现，不能原路退款）
    膨胀(20%) → bonus_points         （仅消费）
    """
    user = _get_user(request)

    with get_db() as conn:
        cursor = conn.cursor()

        if req.commission_ids:
            cursor.execute("""
                SELECT id, commission_yuan FROM commission_records
                WHERE referrer_id = %s AND status = 'pending' AND id = ANY(%s)
                FOR UPDATE
            """, (user["user_id"], req.commission_ids))
        else:
            cursor.execute("""
                SELECT id, commission_yuan FROM commission_records
                WHERE referrer_id = %s AND status = 'pending'
                FOR UPDATE
            """, (user["user_id"],))

        rows = cursor.fetchall()
        if not rows:
            raise HTTPException(400, detail="没有可转换的佣金")

        total_yuan = sum(float(r["commission_yuan"]) for r in rows)
        ids = [r["id"] for r in rows]

        principal_points = int(total_yuan * POINTS_PER_YUAN)
        expansion_points = int(total_yuan * EXPANSION_RATE * POINTS_PER_YUAN)

        # v3.4 入账：本金入 commission_points，膨胀入 bonus_points
        cursor.execute("""
            UPDATE user_wallets
            SET commission_points = commission_points + %s,
                bonus_points = bonus_points + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING commission_points, bonus_points
        """, (principal_points, expansion_points, user["user_id"]))
        wallet = cursor.fetchone()

        # 流水：本金 point_type='commission'，膨胀 point_type='bonus'
        insert_transaction(cursor, user["user_id"], "commission", "commission",
                          principal_points, wallet["commission_points"],
                          description=f"佣金本金 ¥{total_yuan:.2f}")
        insert_transaction(cursor, user["user_id"], "commission", "bonus",
                          expansion_points, wallet["bonus_points"],
                          description=f"佣金膨胀 20%")

        # 标记佣金为已转换
        cursor.execute("""
            UPDATE commission_records
            SET status = 'converted', converted_at = CURRENT_TIMESTAMP
            WHERE id = ANY(%s)
        """, (ids,))

    return {"success": True, "data": {
        "converted_count": len(ids),
        "total_yuan": total_yuan,
        "principal_commission_points": principal_points,
        "expansion_bonus_points": expansion_points,
    }}


@router.get("/team")
async def get_referral_network(request: Request):
    """推荐网络（L1 + L2）"""
    user = _get_user(request)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT rl.referred_id, rl.level, rl.commission_rate, rl.created_at,
                   u.display_name, u.username,
                   COALESCE(w.total_recharged, 0) AS total_recharged
            FROM referral_links rl
            LEFT JOIN users u ON u.id = rl.referred_id
            LEFT JOIN user_wallets w ON w.user_id = rl.referred_id
            WHERE rl.referrer_id = %s
            ORDER BY rl.level, rl.created_at DESC
        """, (user["user_id"],))
        return {"success": True, "data": [dict(r) for r in cursor.fetchall()]}
    finally:
        conn.close()


# ==================== 利润概览 ====================

@router.get("/profit-summary")
async def get_profit_summary(request: Request):
    """代理利润概览（ProfitDashboard 调用）"""
    user = _get_user(request)
    wallet = get_or_create_wallet(user["user_id"])
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 佣金汇总
        cursor.execute("""
            SELECT
                COALESCE(SUM(commission_yuan), 0) AS total_commission_paid,
                COUNT(DISTINCT referred_id) AS total_referred,
                COALESCE(SUM(commission_yuan) FILTER (WHERE created_at >= date_trunc('month', CURRENT_DATE)), 0) AS month_commission
            FROM commission_records WHERE referrer_id = %s
        """, (user["user_id"],))
        stats = cursor.fetchone()
        # [P0 2026-08-05] 「累计推荐」= 推荐关系数,不是佣金记录数
        cursor.execute(
            "SELECT COUNT(DISTINCT referred_id) AS n FROM referral_links WHERE referrer_id = %s",
            (user["user_id"],))
        _rc = cursor.fetchone()
        _referred_count = (_rc["n"] if _rc else 0) or 0
        # 月度数据
        cursor.execute("""
            SELECT
                to_char(created_at, 'YYYY-MM') AS month,
                COALESCE(SUM(commission_yuan), 0) AS commission,
                COUNT(DISTINCT referred_id) AS referred_count
            FROM commission_records WHERE referrer_id = %s
            GROUP BY to_char(created_at, 'YYYY-MM')
            ORDER BY month DESC LIMIT 12
        """, (user["user_id"],))
        monthly = [dict(r) for r in cursor.fetchall()]
        # 膨胀佣金（bonus 部分）
        total_bonus = float(stats["total_commission_paid"]) * EXPANSION_RATE
        return {"success": True, "data": {
            "total_commission_paid": float(stats["total_commission_paid"]),
            "total_commission_bonus": total_bonus,
            # [P0 2026-08-05] 原来取自 commission_records(=产生过佣金的人数)却叫「累计推荐」。
            #   #133 名下真有 3 人(referral_links 3 行 · /team 与 /stats 都返 3),唯独这里 0 ——
            #   同一事实两个端点给相反答案,用户读到的是「我推的人不见了」。口径统一到 referral_links。
            "total_referred": _referred_count,
            "month_commission": float(stats["month_commission"]),
            "agent_level": wallet["agent_level"],
            "monthly_data": monthly,
        }}
    finally:
        conn.close()


# ==================== 代理报价 ====================

@router.post("/generate-quote")
async def generate_agent_quote(request: Request):
    """生成白标报价单 — 存入 DB，返回 quote_id"""
    user = _get_user(request)
    wallet = get_or_create_wallet(user["user_id"])
    if wallet["agent_level"] < 1:
        raise HTTPException(403, detail="需要服务方权限（累计充值达标后自动升级）")

    body = await request.json()
    services = body.get("services", [])
    total_price = body.get("total_price", 0)
    if not services:
        raise HTTPException(400, detail="请选择至少一项服务")
    services = _normalize_quote_services(services, reject_internal_fields=True)
    if not services:
        raise HTTPException(400, detail="报价明细格式无效")

    quote_id = shortuuid.uuid()[:12]
    user_id = user["user_id"]

    # [白标继承] 报价快照是**冻结**的:这里存错,客户看到的 H5/PDF 就永久错。
    # 员工代做的报价必须冻团队长的品牌与主体 —— 冻员工自己的空行会落回平台默认。
    from auth.principal_identity import resolve_branding_principal_user_id
    quote_owner_user_id = resolve_branding_principal_user_id(request, fallback_user_id=int(user_id))

    # 读取白标设置
    import json
    whitelabel = {}
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM whitelabel_settings WHERE user_id = %s", (quote_owner_user_id,))
        wl_row = cursor.fetchone()
        # v3.6 决策 B：报价访问仍按 agent_level（上方 gate 不变），但快照品牌走 customer-surface 授权判定
        # —— 未授权 → 空快照（客户看平台）；授权也只保存明确批准的品牌展示字段。
        if wl_row and is_whitelabel_active_for_customer(wl_row):
            whitelabel = _public_quote_whitelabel({
                "company_name": wl_row.get("company_name", ""),
                "logo_url": wl_row.get("logo_url") or wl_row.get("company_logo_url", ""),
                "slogan": wl_row.get("slogan", ""),
            })

        # 存报价单
        cursor.execute("""
            INSERT INTO agent_quotes (id, user_id, services, total_price, whitelabel)
            VALUES (%s, %s, %s, %s, %s)
        """, (quote_id, quote_owner_user_id, json.dumps(services, ensure_ascii=False), total_price,
              json.dumps(whitelabel, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "data": {"quote_id": quote_id, "services": services}}


@router.get("/quote/{quote_id}")
async def get_agent_quote(quote_id: str, request: Request):
    """获取已保存的报价单"""
    user = _get_user(request)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_quotes WHERE id = %s AND user_id = %s", (quote_id, user["user_id"]))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="报价单不存在")

    import json
    row = dict(row)
    services = row["services"] if isinstance(row["services"], list) else json.loads(row["services"])
    services = _normalize_quote_services(services)
    whitelabel_raw = row["whitelabel"] if isinstance(row["whitelabel"], dict) else json.loads(row.get("whitelabel") or "{}")
    whitelabel = _public_quote_whitelabel(whitelabel_raw)

    return {
        "success": True,
        "quote_id": row["id"],
        "services": services,
        "total_price": float(row.get("total_price") or 0),
        "whitelabel": whitelabel,
        "share_code": row.get("share_code"),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
    }


@router.post("/export-quote")
async def export_quote_pdf(request: Request):
    """导出报价 PDF — 使用浏览器打印"""
    return {"success": True, "method": "print", "message": "请使用浏览器打印功能保存 PDF（Ctrl+P）"}


@router.post("/share-quote")
async def share_quote_link(request: Request):
    """生成白标报价分享链接"""
    user = _get_user(request)
    body = await request.json()
    quote_id = body.get("quote_id")
    if not quote_id:
        raise HTTPException(400, detail="缺少 quote_id")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, share_code FROM agent_quotes WHERE id = %s AND user_id = %s",
                       (quote_id, user["user_id"]))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, detail="报价单不存在")

        share_code = row.get("share_code")
        if not share_code:
            share_code = shortuuid.uuid()[:8].lower()
            cursor.execute("UPDATE agent_quotes SET share_code = %s WHERE id = %s", (share_code, quote_id))
            conn.commit()
    finally:
        conn.close()

    origin = str(request.base_url).rstrip("/")
    forwarded_host = request.headers.get("X-Forwarded-Host")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
    if forwarded_host:
        origin = f"{forwarded_proto}://{forwarded_host}"

    url = f"{origin}/q/{share_code}"
    return {"success": True, "url": url, "share_code": share_code}


# ==================== 公开报价单（无需登录） ====================

@public_router.get("/api/public/quote/{share_code}")
@router.get("/api/public/quote/{share_code}", include_in_schema=False)
async def get_public_quote(share_code: str):
    """公开报价单 — 客户无需登录查看"""
    import json
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, services, total_price, whitelabel, created_at FROM agent_quotes WHERE share_code = %s AND is_active = true",
            (share_code,)
        )
        row = cursor.fetchone()
        if row:
            # D3（Owner 2026-07-22）：emergency suspension → 快照展示层实时压制回落平台
            # （快照数据不动；正常到期/撤权 locked/none → 保留签发时冻结 Logo）。
            # 核验失败不阻断报价展示（冻结快照兜底 · D3 正常路径）。
            try:
                _owner = row.get("user_id") if isinstance(row, dict) else None
                if _owner:
                    cursor.execute(
                        "SELECT whitelabel_status FROM whitelabel_settings WHERE user_id = %s",
                        (_owner,),
                    )
                    _live = cursor.fetchone()
                    _live_status = (_live.get("whitelabel_status") if isinstance(_live, dict) else None) if _live else None
                    if _live_status == "suspended":
                        row = {**dict(row), "whitelabel": None}
            except Exception as _d3_err:
                logger.warning("[whitelabel-d3] quote snapshot live-check failed: %s", _d3_err)
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="报价单不存在或已失效")

    row = dict(row)
    services = row["services"] if isinstance(row["services"], list) else json.loads(row["services"])
    services = _normalize_quote_services(services)
    whitelabel_raw = row["whitelabel"] if isinstance(row["whitelabel"], dict) else json.loads(row.get("whitelabel") or "{}")
    whitelabel = _public_quote_whitelabel(whitelabel_raw)

    return {
        "status": "success",
        "quote": {
            "services": services,
            "total_price": float(row.get("total_price") or 0),
            "whitelabel": whitelabel,
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        },
    }


# ==================== 公开邀请落地页（无需登录 · B 方案 2026-05-30）====================

@public_router.get("/api/public/invite/{code}")
@router.get("/api/public/invite/{code}", include_in_schema=False)
async def get_public_invite(code: str):
    """公开邀请落地页信息 · 按推广码查代理品牌名(白标)· 无需登录(新客户扫码时还没账号)。
    只返展示用品牌名 · 不泄露代理 user_id / 手机等敏感。"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # [P0 2026-08-05] 公开校验端点同口径 —— 分享出去的码一被手抄就废,修一处不够
        from services.referral_code_normalize import fold_sql, normalize_code
        cursor.execute(
            f"SELECT user_id FROM referral_codes WHERE {fold_sql('code')} = {fold_sql('%s')}",
            (normalize_code(code),))
        row = cursor.fetchone()
        if not row:
            return {"valid": False}
        agent_id = row["user_id"] if isinstance(row, dict) else row[0]
        cursor.execute("SELECT * FROM whitelabel_settings WHERE user_id = %s", (agent_id,))
        wl = cursor.fetchone()
        brand = None
        # [P1 Codex sweep] 落地页品牌走 customer 授权判定:无授权/suspended → 不返代理品牌名;
        #   且不 fallback 平台名(无白标 operator 邀请页不露 OmniRank · 前端 brand=null 显中性)
        if wl and is_whitelabel_active_for_customer(wl):
            brand = (wl.get("company_name") if hasattr(wl, "get") else wl[0]) or None
    finally:
        conn.close()
    return {"valid": True, "service_brand": brand}


# ==================== 佣金引擎（被充值回调调用） ====================

# ---------- 2026-04-18 v3.4 普通用户推广返利 ----------

def _apply_free_user_referral_bonus(cursor, referrer_id: int, referred_id: int,
                                      amount_yuan: float, order_id: str):
    """L0 普通用户推广 15% 返利（即时到账 bonus_points）

    规则:
    - 即时到账（无 T+3 观察期）
    - 入账类型：bonus_points（不可提现不可退款）
    - 流水 type='referral_bonus' 便于前端分类展示
    - referral_bonus_records 落明细（含 fraud_flag 列，退款时标记）
    """
    bonus_points = int(amount_yuan * FREE_USER_REFERRAL_RATE * POINTS_PER_YUAN)
    if bonus_points <= 0:
        return

    # 更新钱包
    cursor.execute("""
        UPDATE user_wallets
        SET bonus_points = bonus_points + %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = %s
        RETURNING bonus_points
    """, (bonus_points, referrer_id))
    row = cursor.fetchone()
    balance_after = row["bonus_points"] if row else bonus_points

    # 流水
    insert_transaction(
        cursor, referrer_id,
        "referral_bonus", "bonus",
        bonus_points, balance_after,
        description=f"推荐返利 15% · 朋友充值 ¥{amount_yuan:.0f}"
    )

    # 明细表（带反作弊字段）
    try:
        cursor.execute("""
            INSERT INTO referral_bonus_records
                (referrer_id, referred_id, order_id, recharge_yuan,
                 rate, bonus_points, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (referrer_id, referred_id, order_id, amount_yuan,
              FREE_USER_REFERRAL_RATE, bonus_points))
    except Exception as e:
        logger.warning(f"[ReferralBonus] 明细表写入失败（可忽略）: {e}")

    logger.info(
        f"[ReferralBonus/L0] referrer={referrer_id} referred={referred_id} "
        f"recharge=¥{amount_yuan:.0f} bonus=+{bonus_points}"
    )


def on_recharge_refund(original_order_id: str, refund_order_id: str = None, refund_reason: str = "user_request"):
    """充值被退款时触发 — V3.5 工厂模式 + V3.2 老分润 + V3.3.1 三轨退款编排

    [V3.5 工厂模式 2026-05-26] 退款 4 状态机(Codex r1 P0)
    根据原订单 settlement_mode 路由:
      - v35_inventory_settlement / v35_platform_direct_settlement
                                    → V3.5 退款 4 状态机
      - v32_legacy               → V3.2 老 fraud_flag 路径
      - direct                   → 仅平台 wallet 退积分(无分润 clawback)

    V3.5 退款 4 状态(Codex r2):
    A · T+3 内 + 客户 0 消费 → ledger frozen 直接反向冲销 + 客户额度全退
    B · T+3 内 + 已消费     → 按比例冲销 + manual_review_required
    C · T+3 已过 settled 未提现 → INSERT 'refund_clawback' 负数 ledger · 代理钱包减
    D · T+3 已过 settled 已提走 → clawback 协议 v2.1 §6.2.1 三级追索

    Args:
        original_order_id: 原充值订单号
        refund_order_id:   退款单号;未提供时填原订单号
        refund_reason:     退款原因
    """
    # 1. 查原订单 settlement_mode 路由
    settlement_mode = "direct"
    order_amount_cents = 0
    order_user_id = None
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT settlement_mode, user_id, amount_cents, agent_user_id
                FROM recharge_orders WHERE id = %s
            """, (original_order_id,))
            row = cursor.fetchone()
            if row:
                settlement_mode = (row["settlement_mode"] if isinstance(row, dict) else row[0]) or "direct"
                order_user_id = row["user_id"] if isinstance(row, dict) else row[1]
                order_amount_cents = row["amount_cents"] if isinstance(row, dict) else row[2]
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[Refund] 查 recharge_orders.settlement_mode 失败 order={original_order_id}: {e}")

    logger.info(f"[Refund] on_recharge_refund order={original_order_id} mode={settlement_mode} reason={refund_reason}")

    # 2. V3.5 工厂路径(主事务内 4 状态机)
    if settlement_mode in (
        "v35_inventory_settlement",
        "v35_platform_direct_settlement",
        "dealer_consumer_resale",
    ):
        try:
            _handle_v35_factory_refund(original_order_id, refund_order_id, refund_reason)
        except Exception as e:
            logger.exception(f"[Refund v35] order={original_order_id} 失败: {e}")
        return

    # 3. V3.2 老逻辑(过渡期 30 天保留)
    if settlement_mode == "v32_legacy":
        try:
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE referral_bonus_records
                    SET fraud_flag = TRUE,
                        refund_linked_order_id = %s
                    WHERE order_id = %s
                    RETURNING id, referrer_id, bonus_points
                """, (refund_order_id or original_order_id, original_order_id))
                rows = cursor.fetchall()
                conn.commit()
                if rows:
                    logger.warning(
                        f"[ReferralBonus v32] 退款 fraud_flag: order={original_order_id} 影响 {len(rows)} 条"
                    )
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"[Refund v32] on_recharge_refund 失败 order={original_order_id}: {e}")

    # 4. V3.3.1 hook · 仅 v32_legacy 路径触发(v35 不走 service_fee_engine)
    if settlement_mode == "v32_legacy":
        try:
            from config.v3_3_1_flags import is_v3_3_1_enabled
            if is_v3_3_1_enabled():
                from services.service_fee_engine import process_recharge_refund
                v3_result = process_recharge_refund(original_order_id, refund_reason=refund_reason)
                logger.info("[V3.3.1] on_recharge_refund order=%s reason=%s result=%s",
                            original_order_id, refund_reason, v3_result)
        except Exception as exc:
            logger.exception("[V3.3.1] process_recharge_refund hook failed: %s", exc)


def _revoke_customer_and_sync_grants(
    cursor,
    *,
    customer_user_id: int,
    agent_user_id,
    paid_points: int,
    bonus_points: int,
    related_order_id: str,
    description: str,
) -> dict:
    """[单账本接线 2026-08-17 · C-2] 客户侧回收 = 扣 user_wallets + 同步 bonus_grants 台账。

    替代原 `services.customer_credit.revoke_credit`。原函数的两条边车逻辑逐项定性:

    ① `consume_bonus_fifo`(bonus_grants FIFO 消耗)—— **搬过来**。
       理由:`bonus_grants` 已随单账本收敛(`_freeze_customer_bonus` 现在扣的就是
       `user_wallets.bonus_points`),它是**独立于停写表**的赠送算力台账。
       回收了 bonus 却不消耗对应 grant 行 → grant 台账虚高、到期冻结会重复扣。
    ② `_log_bonus_grant_reconcile`(守恒漂移告警)—— **不搬**。
       理由:它读视图 `v_bonus_grant_reconcile`,该视图 customer 分支的 pool_balance
       取自 `customer_agent_credit_wallets.bonus_credit_points`(见
       `scripts/migration_channel_tier_2026_06_28.sql:187-192`)—— 停写表,现恒 0。
       搬过来只会在每次退款刷一条**必然为真的假漂移**告警。
       这条是【停写表专属】。视图返修不属本包(需改 SQL 视图定义,另出工单)。

    SAVEPOINT 包裹保留:边车失败绝不能打废调用方的退款主事务。
    """
    from services.customer_entitlement import revoke_from_customer as _revoke_wallet

    result = _revoke_wallet(
        cursor,
        customer_user_id=customer_user_id,
        agent_user_id=agent_user_id,
        paid_points=paid_points,
        bonus_points=bonus_points,
        related_order_id=related_order_id,
        source="refund_revoke",
        description=description,
    )
    revoked_paid = int(result.get("revoked_paid") or 0)
    revoked_bonus = int(result.get("revoked_bonus") or 0)

    if revoked_bonus > 0:
        cursor.execute("SAVEPOINT v35_refund_grant_sync")
        try:
            from services.bonus_grants import consume_bonus_fifo
            from services.channel_tier import is_channel_tier_enabled

            if is_channel_tier_enabled(cursor):
                consume_bonus_fifo(cursor, "customer", customer_user_id, revoked_bonus)
            cursor.execute("RELEASE SAVEPOINT v35_refund_grant_sync")
        except Exception as exc:
            cursor.execute("ROLLBACK TO SAVEPOINT v35_refund_grant_sync")
            cursor.execute("RELEASE SAVEPOINT v35_refund_grant_sync")
            logger.warning(
                "[Refund v35] bonus grant 同步失败(不阻断退款)· customer=%s order=%s bonus=%s: %s",
                customer_user_id, related_order_id, revoked_bonus, exc,
            )

    return {"revoked_paid": revoked_paid, "revoked_bonus": revoked_bonus,
            "revoked_total": revoked_paid + revoked_bonus}


def _handle_v35_factory_refund(original_order_id: str, refund_order_id: str = None,
                               refund_reason: str = "user_request"):
    """
    V3.5 工厂模式退款 4 状态机(主事务内)
    Codex r1 P0 + r2 修正:
      - 严格区分 4 状态 · 不同处置
      - revoke 上限 = min(余额, 未消费)
      - clawback 负数 ledger · 不动原条目
    """
    # 🔴 本函数里的 revoke_from_customer = **服务商库存**回收(把额度退回代理库存)。
    #    客户侧钱包回收走 _revoke_customer_and_sync_grants(内部用
    #    services.customer_entitlement.revoke_from_customer,**同名不同义**)——
    #    两者绝不可混用,故客户侧一律经上面那个 helper,不在本函数直接 import,
    #    从根上杜绝同名遮蔽(裸 import 会让下面三处库存回流静默调错函数)。
    from services.agent_inventory import revoke_from_customer
    # compute_unspent_from_order 仍读 customer_credit_transactions:那是【历史订单】的
    # 划拨/消费流水,对 07-29 之前建的单是正确的 FIFO 依据(见交付单「已知缺口」一节)。
    from services.customer_credit import compute_unspent_from_order
    from services.agent_revenue import (
        insert_revenue_clawback, cancel_frozen_ledger, insert_revenue_replacement,
    )

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. 读 ledger(主条目 · source='recharge'· 含全字段)
        # [Codex r4 P1-2] 加 gateway_fee_bps / settlement_service_fee_bps / tax_mode · 给 replacement 复制
        cursor.execute("""
            SELECT l.id, l.agent_user_id, l.customer_user_id, l.agent_settlement_cents,
                   l.status, l.settle_at, l.settled_at, l.reversed_at,
                   l.customer_paid_cents, l.factory_cents,
                   l.gateway_fee_bps, l.gateway_fee_cents,
                   l.settlement_service_fee_bps, l.settlement_service_fee_cents,
                   l.agent_margin_before_tax_cents,
                   l.tax_rate_bps, l.tax_mode, l.tax_withholding_cents
            FROM agent_revenue_ledger l
            WHERE l.recharge_order_id = %s AND l.source = 'recharge'
            ORDER BY l.id ASC LIMIT 1 FOR UPDATE
        """, (original_order_id,))
        ledger_row = cursor.fetchone()
        if not ledger_row:
            logger.warning(f"[Refund v35] order={original_order_id} 无 v35 ledger · skip")
            return

        ledger = dict(ledger_row) if isinstance(ledger_row, dict) else {
            "id": ledger_row[0], "agent_user_id": ledger_row[1],
            "customer_user_id": ledger_row[2], "agent_settlement_cents": ledger_row[3],
            "status": ledger_row[4], "settle_at": ledger_row[5], "settled_at": ledger_row[6],
            "reversed_at": ledger_row[7],
            "customer_paid_cents": ledger_row[8], "factory_cents": ledger_row[9],
            "gateway_fee_bps": ledger_row[10], "gateway_fee_cents": ledger_row[11],
            "settlement_service_fee_bps": ledger_row[12], "settlement_service_fee_cents": ledger_row[13],
            "agent_margin_before_tax_cents": ledger_row[14],
            "tax_rate_bps": ledger_row[15], "tax_mode": ledger_row[16], "tax_withholding_cents": ledger_row[17],
        }

        if ledger["reversed_at"] is not None:
            logger.warning(f"[Refund v35] order={original_order_id} 已 reversed · 跳过重复退款")
            return

        # [2A-2 对抗审 wmlgiobzc P0] 统一幂等闸:A/B(frozen)靠上方 reversed_at 挡;
        # C/D(settled)走 insert_revenue_clawback 不设原 ledger reversed_at → 上方挡不住。
        # 加 refund_status='processed' 检查(配合本函数 994 行 FOR UPDATE ledger serialize 并发)·
        # 防 settled 单二次提交/并发穿透 → 重复负 clawback 双扣代理收益。
        # Re-check cash-refund evidence while holding the order lock in the same
        # transaction as the irreversible ledger work. This is unconditional so
        # no current or future caller can bypass the RD/CD evidence contract.
        cursor.execute("""
            SELECT refund_status, refund_completed_at
            FROM recharge_orders
            WHERE id = %s
            FOR UPDATE
        """, (original_order_id,))
        _rs_row = cursor.fetchone()
        _rs = (_rs_row["refund_status"] if isinstance(_rs_row, dict) else (_rs_row[0] if _rs_row else None))
        if not _rs_row:
            raise ValueError("退款订单不存在")
        from db.refund_work_order_db import assert_external_refund_evidence_cur
        _refund_completed_at = (
            _rs_row["refund_completed_at"] if isinstance(_rs_row, dict) else _rs_row[1]
        )
        assert_external_refund_evidence_cur(
            cursor,
            original_order_id,
            _rs,
            _refund_completed_at,
        )
        if _rs == "processed":
            logger.warning(f"[Refund v35] order={original_order_id} refund_status 已 processed · 跳过重复退款(幂等闸)")
            return

        # [BUG-P1] 库存回流闸:线上结算(allocate_to_customer · 净0双流水 purchase_auto+N→allocate-N)
        # 代理库存从未真扣 → revoke_from_customer 会给代理 paid_inventory 凭空 +N。
        # 仅线下分配(allocate_to_customer_offline · 库存真减)退款才需回流库存。
        # ⚠️ 本函数只处理 source='recharge' 线上净0单 → is_offline_alloc 在此结构上【恒 False】
        # (线下分配不写 recharge ledger·进不来本函数;且 allocate_offline 不带 related_order_id)。
        # 即 gate 仅作【防御性断言】· 本函数内 revoke 永不触发。勿误依赖"未来线下能从这里回流"——
        # 线下回流应在各自线下退款路径处理,不走本函数。
        cursor.execute("""
            SELECT EXISTS(
                SELECT 1 FROM agent_inventory_transactions
                WHERE related_order_id = %s AND type = 'allocate_to_customer_offline'
            ) AS is_offline
        """, (original_order_id,))
        _off_row = cursor.fetchone()
        is_offline_alloc = bool(_off_row["is_offline"] if isinstance(_off_row, dict) else _off_row[0])
        if not is_offline_alloc:
            logger.info(
                f"[Refund v35] order={original_order_id} 线上净0分配 · "
                f"跳过库存回流(防凭空注入代理库存)"
            )

        # 2. [Codex r2 P0-2] 按订单 FIFO 算未消费(不用 total_consumed 比例)
        #
        # [单账本 FIFO 换底座 2026-08-17 · Owner 授权 A] 新旧**按有无老台账 allocate 历史**分流,
        # **不按日期硬切**(日期切在边界上两头不靠;按"谁有账用哪本账"天然无缝):
        #   · 有老台账历史 → 走老 compute_unspent_from_order(在册 7 张切换前的单不换轨);
        #   · 无           → 走 customer_entitlement.compute_unspent_fifo(point_transactions 重放)。
        # 老路径读的 customer_credit_transactions 自 07-29 停写,对切换后新建的订单恒空
        # → 未消费恒 0 → 撤回恒 0 且 clawback 比例恒 0(ec11dffe 交付单自曝的缺口)。
        from services.customer_entitlement import (
            compute_unspent_fifo,
            has_legacy_credit_ledger_history,
        )
        _customer_uid = ledger["customer_user_id"]
        _use_legacy_ledger = has_legacy_credit_ledger_history(
            cursor, _customer_uid, original_order_id)

        if _use_legacy_ledger:
            order_unspent = compute_unspent_from_order(cursor, _customer_uid, original_order_id)
            paid_unspent = int(order_unspent.get("tool_unspent", 0)) + \
                int(order_unspent.get("publish_unspent", 0))
            bonus_unspent = int(order_unspent.get("bonus_unspent", 0))
            # 划拨总量:老台账口径(与老未消费同源,比例才自洽)
            cursor.execute("""
                SELECT pool, SUM(points) AS alloc_total
                FROM customer_credit_transactions
                WHERE customer_user_id = %s AND related_order_id = %s AND type = 'allocate'
                GROUP BY pool
            """, (_customer_uid, original_order_id))
            order_total_by_pool = {}
            for r in cursor.fetchall():
                p = r["pool"] if isinstance(r, dict) else r[0]
                t = r["alloc_total"] if isinstance(r, dict) else r[1]
                order_total_by_pool[p] = int(t or 0)
            order_total_alloc = sum(order_total_by_pool.values())
        else:
            _fifo = compute_unspent_fifo(cursor, _customer_uid, original_order_id)
            paid_unspent = int(_fifo.get("paid_unspent", 0))
            bonus_unspent = int(_fifo.get("bonus_unspent", 0))
            # 划拨总量:订单**不可变快照**(08_billing:退款按原订单快照单跳反转,
            # 不读当前价格/关系/倍率重算)。老台账对新单为空,不能拿它当分母 ——
            # 否则 order_total_alloc=0 → unconsumed_ratio 恒 0 → 代理 clawback 恒 0。
            cursor.execute(
                "SELECT COALESCE(base_points,0) AS bp, COALESCE(bonus_points,0) AS bnp "
                "FROM recharge_orders WHERE id = %s", (original_order_id,))
            _ord = cursor.fetchone() or {}
            _bp = int((_ord["bp"] if isinstance(_ord, dict) else _ord[0]) or 0) if _ord else 0
            _bnp = int((_ord["bnp"] if isinstance(_ord, dict) else _ord[1]) or 0) if _ord else 0
            order_total_alloc = _bp + _bnp

        total_unspent = paid_unspent + bonus_unspent
        logger.info(
            "[Refund v35] order=%s 未消费口径=%s · paid_unspent=%s bonus_unspent=%s / alloc=%s",
            original_order_id, "legacy_ledger" if _use_legacy_ledger else "single_ledger_fifo",
            paid_unspent, bonus_unspent, order_total_alloc,
        )

        # [Codex r2 P0-3] 0 消费精准判断:本订单 FIFO unspent == 本订单 allocated
        is_zero_consumed = (total_unspent == order_total_alloc) and (order_total_alloc > 0)

        # 3. 路由 4 状态
        from datetime import datetime
        now = datetime.utcnow()
        is_frozen = ledger["status"] == "frozen"
        is_settled = ledger["status"] == "settled"

        manual_review = False

        if is_frozen and is_zero_consumed:
            # 状态 A · T+3 内 · 0 消费
            # [Codex r3 P0 修正]:只 cancel 原 frozen · ❌ 不写 negative clawback
            #                     防代理余额变成 -100 (应该 0)
            logger.info(f"[Refund v35-A] order={original_order_id} 全退 + cancel frozen · 无 clawback")

            # [Codex r3 P1-1] 用实际撤回额 · 不用 unspent 预估值
            # [单账本接线 2026-08-17] tool+publish → paid_points(与 grant_to_customer 及
            # 迁移脚本 wallet_credit_merge_migrate 同口径);bonus → bonus_points。
            credit_result = _revoke_customer_and_sync_grants(
                cursor,
                customer_user_id=ledger["customer_user_id"],
                agent_user_id=ledger["agent_user_id"],
                paid_points=paid_unspent,
                bonus_points=bonus_unspent,
                related_order_id=original_order_id,
                description=f"退款全退 · reason={refund_reason}",
            )
            if is_offline_alloc:  # [BUG-P1] 仅线下真扣库存才回流(线上净0不调,防凭空注入)
                revoke_from_customer(
                    cursor,
                    agent_user_id=ledger["agent_user_id"],
                    customer_user_id=ledger["customer_user_id"],
                    paid_points_to_revoke=credit_result["revoked_paid"],
                    bonus_points_to_revoke=credit_result["revoked_bonus"],
                    description=f"退款回库存 · order={original_order_id}",
                )
            # cancel 原 frozen · 无 clawback_ledger_id(无负数 clawback)
            cancelled_rows = cancel_frozen_ledger(
                cursor,
                ledger_id=ledger["id"],
                clawback_ledger_id=None,
                reason=f"refund_full_{refund_reason}",
            )
            if cancelled_rows == 0:
                logger.error(f"[Refund v35-A] order={original_order_id} cancel_frozen_ledger 0 rows · race")

        elif is_frozen and not is_zero_consumed:
            # 状态 B · T+3 内 · 部分消费
            # [Codex r3 P0 修正]:cancel 原 frozen + 写正数 replacement(已消费部分对应收益)
            #                     不写 negative clawback(防代理余额错)
            consumed_amount = order_total_alloc - total_unspent
            consumed_ratio = consumed_amount / max(order_total_alloc, 1)
            unconsumed_ratio = total_unspent / max(order_total_alloc, 1)
            logger.warning(
                f"[Refund v35-B] order={original_order_id} 部分消费 · "
                f"未消费 {total_unspent}/{order_total_alloc} 撤 · "
                f"已消费 {consumed_amount} 留代理收益(写 replacement frozen)"
            )
            manual_review = True

            # [Codex r3 P1-1] revoke 用实际撤回额
            credit_result = _revoke_customer_and_sync_grants(
                cursor,
                customer_user_id=ledger["customer_user_id"],
                agent_user_id=ledger["agent_user_id"],
                paid_points=paid_unspent,
                bonus_points=bonus_unspent,
                related_order_id=original_order_id,
                description=f"退款按订单FIFO · reason={refund_reason}",
            )
            if is_offline_alloc:  # [BUG-P1] 仅线下真扣库存才回流(线上净0不调,防凭空注入)
                revoke_from_customer(
                    cursor,
                    agent_user_id=ledger["agent_user_id"],
                    customer_user_id=ledger["customer_user_id"],
                    paid_points_to_revoke=credit_result["revoked_paid"],
                    bonus_points_to_revoke=credit_result["revoked_bonus"],
                    description=f"退款按比例回库存 · order={original_order_id}",
                )

            # 算已消费部分对应的代理收益(按比例)
            replacement_settlement = int(ledger["agent_settlement_cents"] * consumed_ratio)
            replacement_paid = int(ledger["customer_paid_cents"] * consumed_ratio)
            replacement_factory = int(ledger["factory_cents"] * consumed_ratio)
            replacement_gateway = int(ledger["gateway_fee_cents"] * consumed_ratio)
            replacement_service = int(ledger["settlement_service_fee_cents"] * consumed_ratio)
            replacement_margin = int(ledger["agent_margin_before_tax_cents"] * consumed_ratio)
            replacement_tax = int(ledger["tax_withholding_cents"] * consumed_ratio)

            replacement_id = None
            if replacement_settlement > 0:
                # [Codex r4 P1-2] bps + tax_mode 从原 ledger 复制 · 不再硬编码
                replacement_id = insert_revenue_replacement(
                    cursor,
                    original_ledger_id=ledger["id"],
                    agent_user_id=ledger["agent_user_id"],
                    recharge_order_id=original_order_id,
                    customer_user_id=ledger["customer_user_id"],
                    customer_paid_cents=replacement_paid,
                    factory_cents=replacement_factory,
                    gateway_fee_bps=ledger["gateway_fee_bps"],
                    gateway_fee_cents=replacement_gateway,
                    settlement_service_fee_bps=ledger["settlement_service_fee_bps"],
                    settlement_service_fee_cents=replacement_service,
                    agent_margin_before_tax_cents=replacement_margin,
                    tax_rate_bps=ledger["tax_rate_bps"],
                    tax_mode=ledger["tax_mode"],
                    tax_withholding_cents=replacement_tax,
                    agent_settlement_cents=replacement_settlement,
                    settle_at_orig=ledger["settle_at"],
                    note=f"replacement consumed_ratio={consumed_ratio:.2%} reason={refund_reason}",
                )
                # [BUG-P2] 部分退款 replacement frozen 不再打 manual_review_required:
                # 它是按 consumed_ratio 确定性算出的【服务商应得收益】(客户已消费部分对应),无需人工审核。
                # 原打 TRUE → settle daily cron(WHERE NOT manual_review_required)永久跳过 → 既不 settled
                # 也不可提现/换算力,全仓无代码清回 FALSE = 服务商应得收益永久卡死。
                # 改为不打标,随 settle_at 正常进 T+3 settle。(C/D clawback 的已消费损耗承接仍保留 manual_review)
                pass

            # cancel 原 frozen · 引用 replacement_id 作为 reversed_by
            cancel_frozen_ledger(
                cursor,
                ledger_id=ledger["id"],
                clawback_ledger_id=replacement_id,
                reason=f"refund_partial_{refund_reason}",
            )

        elif is_settled:
            # 状态 C/D · T+3 settled(未/已提走皆此分支)
            # [Codex r4 P1-1 修正] clawback 按 unspent_ratio · 不再全额
            # 原:clawback = settlement 全额 → 客户消费 80% 时代理被扣 100% 收益(错)
            # 新:clawback = settlement × unspent_ratio · 代理保留已消费部分对应收益
            # 撤客户 wallet(用 actually · P1-1)+ 回库存
            credit_result = _revoke_customer_and_sync_grants(
                cursor,
                customer_user_id=ledger["customer_user_id"],
                agent_user_id=ledger["agent_user_id"],
                paid_points=paid_unspent,
                bonus_points=bonus_unspent,
                related_order_id=original_order_id,
                description=f"退款撤未消费 · reason={refund_reason}",
            )
            actually_total = credit_result["revoked_total"]
            # [BUG-P1] 仅线下真扣库存才回流(线上净0不调,防凭空注入代理库存)
            if actually_total > 0 and is_offline_alloc:
                revoke_from_customer(
                    cursor,
                    agent_user_id=ledger["agent_user_id"],
                    customer_user_id=ledger["customer_user_id"],
                    paid_points_to_revoke=credit_result["revoked_paid"],
                    bonus_points_to_revoke=credit_result["revoked_bonus"],
                    description=f"退款回库存(settled 阶段)· order={original_order_id}",
                )

            # [P1-1] clawback 按实际撤回比例(unspent_ratio)
            unspent_ratio = actually_total / max(order_total_alloc, 1)
            clawback_amount = int(ledger["agent_settlement_cents"] * unspent_ratio)
            consumed_amount_settled = order_total_alloc - actually_total
            logger.warning(
                f"[Refund v35-C/D] order={original_order_id} settled · "
                f"actually_revoked={actually_total}/{order_total_alloc} unspent_ratio={unspent_ratio:.2%} · "
                f"clawback={clawback_amount}/全额{ledger['agent_settlement_cents']}"
            )

            # [Codex r5 P1-3] 已消费部分必须人工处理
            # actually_total < order_total_alloc 表示客户已消费了部分(甚至全部)
            # 现金已退给客户 · 但消费损耗需要财务/客服判定如何承接
            if consumed_amount_settled > 0:
                logger.warning(
                    f"[Refund v35-C/D] order={original_order_id} 已消费 {consumed_amount_settled} 积分 · "
                    f"现金已退但消费损耗需人工承接 · manual_review_required=TRUE"
                )
                manual_review = True

            # 检查是否有 paid items(已提走 · D 状态 · 比 P1-3 优先级高)
            cursor.execute("""
                SELECT COUNT(*) AS c FROM agent_settlement_request_items i
                JOIN agent_settlement_requests r ON r.id = i.settlement_request_id
                WHERE i.ledger_id = %s AND r.status = 'paid'
            """, (ledger["id"],))
            paid_count = cursor.fetchone()
            paid_count = paid_count["c"] if isinstance(paid_count, dict) else paid_count[0]
            if paid_count > 0:
                logger.warning(
                    f"[Refund v35-D] order={original_order_id} 已提走 · 协议 §6.2.1 三级追索"
                )
                manual_review = True

            # 写 negative clawback ledger(settled 状态才合适 · 金额按比例)
            if clawback_amount > 0:
                clawback_id = insert_revenue_clawback(
                    cursor,
                    agent_user_id=ledger["agent_user_id"],
                    original_ledger_id=ledger["id"],
                    clawback_amount_cents=clawback_amount,
                    note=f"退款 clawback · order={original_order_id} · ratio={unspent_ratio:.2%} · reason={refund_reason}",
                )
                if manual_review:
                    cursor.execute("""
                        UPDATE agent_revenue_ledger SET manual_review_required=TRUE WHERE id=%s
                    """, (clawback_id,))

        else:
            logger.warning(f"[Refund v35-?] order={original_order_id} 未知 ledger status={ledger['status']} · skip")
            return

        # 5. 标记 recharge_orders.refund_status = 'processed'(退款生效)
        # [审核 P1 · P0-2 快照不可变] 退款元信息写 settlement_snapshot_jsonb,不改 pricing_snapshot_jsonb
        #   (原始定价证据 write-once · §9.3)。
        cursor.execute("""
            UPDATE recharge_orders
            SET refund_status = 'processed',
                settlement_snapshot_jsonb = COALESCE(settlement_snapshot_jsonb, '{}'::jsonb)
                    || jsonb_build_object('refund_at', NOW()::text, 'refund_reason', %s, 'manual_review', %s)
            WHERE id = %s
        """, (refund_reason, manual_review, original_order_id))

        # [v10 item6] 退款【已生效】(processed)→ 统一 canonical 冲销渠道收益。
        #   替代 v9 手工 SAVEPOINT+reverse+enqueue:由 services.channel_revenue_lifecycle 单点收口
        #   (FOR UPDATE + refund_status 阶段判定 + 幂等 reverse + SAVEPOINT + 失败同事务耐久工单)。
        #   ⚠️ 必须在 SET refund_status='processed' 之后调(canonical 按当前 refund_status 判定是否已生效)。
        from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
        reverse_channel_revenue_on_refund(cursor, original_order_id)

        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------- 反作弊检查 ----------

def _is_suspicious_referral(source_user_id: int, receiver_user_id: int, cursor) -> tuple[bool, str]:
    """检查两个用户是否可能是刷单关系（同设备 / 同 IP）

    返回: (是否可疑, 原因描述)
    """
    # 查两个用户的设备指纹和 IP
    cursor.execute("""
        SELECT id, register_device_fingerprint, register_ip, last_login_ip
        FROM users
        WHERE id IN (%s, %s)
    """, (source_user_id, receiver_user_id))
    rows = {r["id"]: r for r in cursor.fetchall()}

    source = rows.get(source_user_id)
    receiver = rows.get(receiver_user_id)
    if not source or not receiver:
        return False, ""

    # 层 1: 同设备指纹
    src_fp = source.get("register_device_fingerprint")
    rcv_fp = receiver.get("register_device_fingerprint")
    if src_fp and rcv_fp and src_fp == rcv_fp:
        return True, "same_device_fingerprint"

    # 层 2: 同注册 IP（最严格）
    src_ip = source.get("register_ip")
    rcv_ip = receiver.get("register_ip")
    if src_ip and rcv_ip and src_ip == rcv_ip:
        return True, "same_register_ip"

    # 层 2b: 最近登录 IP 交集
    src_login_ip = source.get("last_login_ip")
    rcv_login_ip = receiver.get("last_login_ip")
    if src_login_ip and rcv_login_ip and src_login_ip == rcv_login_ip:
        return True, "same_recent_login_ip"

    return False, ""


# ---------- v3.2 新佣金引擎（充值 → T+0 pending → T+3 settled） ----------

def process_recharge_commission_v3(
    referred_user_id: int,
    recharge_amount_cents: int,
    order_id: str,
    order_extras: Optional[dict] = None,
):
    """
    v3.2 + v3.3.1: 充值成功后生成 pending_commissions

    V3.3.1(2026-05-12):总开关 V3_3_1_ENABLED 启用时由
    services/service_fee_calculator.record_v3_3_1_rewards 接管 ·
    写 service_fee_records / pending_bonus_records 新表 ·
    根据 V3_3_1_DUAL_WRITE_OLD_TABLE 决定是否同时双写老 pending_commissions。

    总开关关时(默认 OFF · 生产现状)继续走原 V3.2 阶梯 28%/5% 逻辑。

    规则(V3.2 老逻辑):
    - L1 直推: 18%, T+3 自动结算
    - L2 间推: 3%, T+3 自动结算
    - 反作弊可疑 → status='frozen' 人工审核
    - 大额首充 ≥ ¥2000 → status='frozen' 人工审核

    Args:
        order_extras: V3.3.1 净现金基数所需的扣项明细
            {refund_amount_yuan, media_cost_yuan, coupon_yuan,
             bonus_points_used, granted_points_deducted_yuan,
             gateway_fee_yuan, payment_method, source, order_type}

    [V3.5 W3 2026-05-26] 防御性 gate:
    - 主控:wallet_db.py:763 仅在 settlement_mode='v32_legacy' 调本函数(mutex)
    - 防御:本函数额外检查 recharge_orders.settlement_mode · 若为任一 V3.5 客户结算模式
      或 order_type='agent_inventory_purchase' 立刻 return 0(防直接调用绕过 mutex)
    """
    from datetime import datetime, timedelta
    # [W3 防御] 读 order 检查 settlement_mode · V3.5 路径立即 return
    try:
        from db.connection import get_db as _gdb
        with _gdb() as _c:
            _cur = _c.cursor()
            _cur.execute(
                "SELECT settlement_mode, order_type FROM recharge_orders WHERE id = %s",
                (order_id,),
            )
            _ord = _cur.fetchone()
            if _ord:
                _sm = _ord["settlement_mode"] if isinstance(_ord, dict) else _ord[0]
                _ot = _ord["order_type"] if isinstance(_ord, dict) else _ord[1]
                if _sm in (
                    "v35_inventory_settlement", "v35_platform_direct_settlement",
                ) or _ot == "agent_inventory_purchase":
                    logger.info(
                        f"[Commission v3.4] 拦截 V3.5 路径调用 · order={order_id} "
                        f"settlement_mode={_sm} order_type={_ot} · 不写老分润"
                    )
                    return
    except Exception as _e:
        logger.warning(f"[Commission v3.4] V3.5 防御 gate 检查失败(允许继续 · 由 mutex 兜底): {_e}")

    # ============ V3.3.1 hook(总开关启用时接管)============
    # Codex 二审 P0-4:source 必须 fail-close · 不能默认 'external_cash_payment'
    # Codex 二审 P0-5:V3.3.1 hook 异常不再退化 V3.2 · 报警 + skip 防绕过净现金/source 规则
    try:
        from config.v3_3_1_flags import is_v3_3_1_enabled
    except Exception:  # 极罕见 · 仅 import 失败兜底
        is_v3_3_1_enabled = lambda: False

    if is_v3_3_1_enabled():
        from services.service_fee_calculator import record_v3_3_1_rewards
        extras = order_extras or {}
        # source 必须显式传入 · 否则 fail-close 走 service_fee_calculator 内部判断
        # complete_recharge / wxpay callback 等真实充值入口已改为显式传 'external_cash_payment'
        order_source = extras.get("source")
        if order_source is None:
            logger.warning(
                "[V3.3.1] referred=%s order=%s · source missing in extras · "
                "fail-close(不触发返佣)· 调用方应显式传 source",
                referred_user_id, order_id,
            )
            return  # 不退化 V3.2 · 直接 skip
        order_dict = {
            "id": order_id,
            "user_id": referred_user_id,
            "paid_amount_yuan": recharge_amount_cents / 100,
            "paid_amount_cents": recharge_amount_cents,
            "source": order_source,
            "order_type": extras.get("order_type", "recharge"),
            "payment_method": extras.get("payment_method"),
            **{k: extras.get(k) for k in (
                "refund_amount_yuan", "media_cost_yuan", "coupon_yuan",
                "bonus_points_used", "granted_points_deducted_yuan",
                "gateway_fee_yuan",
            )},
        }
        try:
            result = record_v3_3_1_rewards(order_dict)
            logger.info("[V3.3.1] referred=%s order=%s result=%s",
                        referred_user_id, order_id, result)
        except Exception as exc:
            # Codex 三审 P1-4:hook 异常 持久化到 failed_service_fee_jobs 补偿队列
            # 充值已 commit · 返佣链路失败由 cron 自动重试 · 多次失败转 abandoned 由财务手动处理
            logger.exception(
                "[V3.3.1] CRITICAL: record_v3_3_1_rewards FAILED for order=%s · "
                "充值已成功 · 写入 failed_service_fee_jobs 队列",
                order_id,
            )
            try:
                import json as _json
                _conn = get_connection()
                try:
                    _cur = _conn.cursor()
                    _cur.execute(
                        """
                        INSERT INTO failed_service_fee_jobs
                            (source_order_id, user_id, amount_cents, order_extras,
                             error_message, error_class, status)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s, 'pending')
                        ON CONFLICT (source_order_id) DO UPDATE
                          SET retry_count = failed_service_fee_jobs.retry_count + 1,
                              error_message = EXCLUDED.error_message,
                              last_retry_at = NOW()
                        """,
                        (
                            order_id, referred_user_id, recharge_amount_cents,
                            _json.dumps({"source": order_source, **(order_extras or {})}),
                            str(exc)[:1000], type(exc).__name__,
                        ),
                    )
                    _conn.commit()
                finally:
                    _conn.close()
            except Exception as persist_exc:
                # 连补偿表都写不了 · 至少日志已有 · 这是最后兜底
                logger.exception(
                    "[V3.3.1] CRITICAL^2: failed_service_fee_jobs insert FAILED order=%s: %s",
                    order_id, persist_exc,
                )
        return  # 总开关 ON 时永不走 V3.2 老路径

    # ============ V3.2 原逻辑(总开关 OFF 才走)============
    conn = get_connection()
    try:
        cursor = conn.cursor()
        amount_yuan = recharge_amount_cents / 100
        available_at = datetime.utcnow() + timedelta(days=COMMISSION_OBSERVATION_DAYS)

        # 查直推人（L1）
        cursor.execute("""
            SELECT referrer_id FROM referral_links
            WHERE referred_id = %s AND level = 1
        """, (referred_user_id,))
        l1 = cursor.fetchone()

        if not l1:
            return  # 无推荐人，不返佣

        l1_user_id = l1["referrer_id"]

        # 2026-04-18 v3.4: 按推荐人当前身份分流
        #   L0（普通用户）→ 15% 赠送积分（即时到账，不走 pending）
        #   L1+（代理）→ 18%/3% 佣金（原 T+3 结算逻辑保留）
        l1_wallet = get_or_create_wallet(l1_user_id)
        l1_current_level = l1_wallet.get("agent_level", 0) or 0

        if l1_current_level == 0:
            _apply_free_user_referral_bonus(
                cursor, l1_user_id, referred_user_id, amount_yuan, order_id
            )
            conn.commit()
            logger.info(
                f"[Commission v3.4] L0 返利：referrer={l1_user_id} referred={referred_user_id} "
                f"recharge=¥{amount_yuan} → 15% bonus"
            )
            return

        l1_commission = round(amount_yuan * COMMISSION_RATE_L1_V32, 2)

        # 反作弊检查（L1）— v3.2 用户决策：检测但不拦截
        # 35% 营销成本已留出薅羊毛空间，代理帮客户注册是业务常态
        is_suspicious, reason = _is_suspicious_referral(referred_user_id, l1_user_id, cursor)

        # 大额首充（≥ ¥2000）— 记录标记便于运营后台查，不冻结
        large_first_charge = amount_yuan >= 2000

        l1_status = 'pending'
        l1_frozen_reason = None

        # 可疑仅记录日志 + 打标记（frozen_reason 字段），不改变 pending 状态
        # 运营可通过后台查 frozen_reason 非空的记录做抽查，但不影响正常结算
        if is_suspicious:
            l1_frozen_reason = f"flag_only_suspicious:{reason}"
            logger.warning(
                f"[Commission v3.2] 标记可疑但正常结算 source={referred_user_id} "
                f"target={l1_user_id} reason={reason}"
            )
        elif large_first_charge:
            l1_frozen_reason = f"flag_only_large:¥{amount_yuan:.0f}"

        if l1_commission > 0:
            cursor.execute("""
                INSERT INTO pending_commissions
                    (user_id, source_user_id, order_id, amount_yuan,
                     level, commission_rate, status, available_at, frozen_reason)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s)
            """, (l1_user_id, referred_user_id, order_id, l1_commission,
                  COMMISSION_RATE_L1_V32, l1_status, available_at, l1_frozen_reason))

        # 查间推人（L2）
        cursor.execute("""
            SELECT referrer_id FROM referral_links
            WHERE referred_id = %s AND level = 2
        """, (referred_user_id,))
        l2 = cursor.fetchone()

        if l2 and l2["referrer_id"] != referred_user_id:
            l2_user_id = l2["referrer_id"]
            l2_commission = round(amount_yuan * COMMISSION_RATE_L2_V32, 2)

            # L2 也反作弊
            # L2 同样只标记不冻结（v3.2 用户决策）
            l2_suspicious, l2_reason = _is_suspicious_referral(referred_user_id, l2_user_id, cursor)
            l2_status = 'pending'
            l2_frozen_reason = None
            if l2_suspicious:
                l2_frozen_reason = f"flag_only_suspicious:{l2_reason}"
            elif large_first_charge:
                l2_frozen_reason = f"flag_only_large:¥{amount_yuan:.0f}"

            if l2_commission > 0:
                cursor.execute("""
                    INSERT INTO pending_commissions
                        (user_id, source_user_id, order_id, amount_yuan,
                         level, commission_rate, status, available_at, frozen_reason)
                    VALUES (%s, %s, %s, %s, 2, %s, %s, %s, %s)
                """, (l2_user_id, referred_user_id, order_id, l2_commission,
                      COMMISSION_RATE_L2_V32, l2_status, available_at, l2_frozen_reason))

        conn.commit()
        logger.info(
            f"[Commission v3.2] referred={referred_user_id} ¥{amount_yuan} "
            f"L1=¥{l1_commission} ({l1_status})"
        )
    except Exception as e:
        conn.rollback()
        logger.error(f"[Commission v3.2] 失败: {e}")
        raise
    finally:
        conn.close()


# ---------- 结算定时任务（T+3 后自动转 settled） ----------

def settle_due_commissions(dry_run: bool = False) -> dict:
    """T+3 到期的 pending 佣金自动结算

    退款状态分流(2026-04-26 P1-5 修复):
      - refund_status IN ('approved', 'completed') → refunded_cancelled(退款已生效 · 取消佣金)
      - refund_status IN ('pending', 'failed', 其他未知) → 跳过本轮 · 等下次 cron(状态未稳定)
      - refund_status IN (NULL, 'rejected') → 正常 settled(无退款 / 退款已拒)

    入账规则:
      - 已结算的佣金 → user_wallets.commission_points(可提现 · 不可退款)
      - 不写 paid_points(老板拍板)

    参数:
        dry_run: True 时只统计不实际操作(用于调试)
    """
    from datetime import datetime

    conn = get_connection()
    settled_count = 0
    cancelled_count = 0
    skipped_count = 0  # 2026-04-26 P1-5: refund_status pending/failed 跳过本轮 · 等下次再来
    try:
        cursor = conn.cursor()
        now = datetime.utcnow()

        # 锁定到期的 pending 记录
        cursor.execute("""
            SELECT pc.id, pc.user_id, pc.amount_yuan, pc.order_id,
                   pc.level, pc.source_user_id, ro.refund_status, ro.payment_status
            FROM pending_commissions pc
            LEFT JOIN recharge_orders ro ON ro.id = pc.order_id
            WHERE pc.status = 'pending'
              AND pc.available_at <= %s
            FOR UPDATE OF pc
        """, (now,))
        due_records = cursor.fetchall()

        for rec in due_records:
            refund_status = rec.get("refund_status")  # NULL / pending / approved / completed / rejected / failed

            # ---- 1) 退款已生效 → 取消佣金 ----
            if refund_status in ('approved', 'completed'):
                if not dry_run:
                    cursor.execute("""
                        UPDATE pending_commissions
                        SET status = 'refunded_cancelled', settled_at = %s
                        WHERE id = %s
                    """, (now, rec["id"]))

                    # 退款终态与奖励取消通知同事务；不披露被推荐人账号/手机号。
                    from services.notification_events import NotificationEventType, RecipientKind
                    from services.notification_outbox import enqueue_notification_event
                    enqueue_notification_event(
                        cursor,
                        event_type=NotificationEventType.REFERRAL_REWARD_REVERSED,
                        business_id=f"pending_commission:{int(rec['id'])}",
                        terminal_state="reversed",
                        recipient_user_id=int(rec["user_id"]),
                        recipient_kind=RecipientKind.USER,
                        facts={
                            "business_no": str(rec.get("order_id") or f"COMMISSION-{int(rec['id'])}"),
                            "amount": f"{rec['amount_yuan']} 元",
                            "status": "对应订单已退款，推荐奖励已取消",
                            "occurred_at": now.isoformat(timespec="seconds"),
                            "summary": "本次调整不影响其他已生效的推荐奖励。",
                        },
                    )

                cancelled_count += 1
                continue

            # ---- 2) 退款进行中 / 失败但状态未稳定 → 跳过本轮(老板 P1-5: 不结算也不取消) ----
            if refund_status not in (None, 'rejected'):
                # 包含 'pending' / 'failed' / 任意未知值 · 等下次 cron 再判
                logger.info(
                    f"[Commission Settle] skip pending refund: pc_id={rec['id']} "
                    f"order={rec['order_id']} refund_status={refund_status}"
                )
                skipped_count += 1
                continue

            # ---- 3) refund_status NULL / rejected → 正常结算 ----
            points = int(float(rec["amount_yuan"]) * POINTS_PER_YUAN)
            if not dry_run:
                # 更新钱包
                cursor.execute("""
                    UPDATE user_wallets
                    SET commission_points = commission_points + %s, updated_at = NOW()
                    WHERE user_id = %s
                    RETURNING commission_points
                """, (points, rec["user_id"]))
                wallet_row = cursor.fetchone()
                new_balance = wallet_row["commission_points"] if wallet_row else 0

                # 写流水
                insert_transaction(
                    cursor, rec["user_id"], "commission_settlement", "commission",
                    points, new_balance,
                    description=f"推广费结算 L{rec['level']} ¥{rec['amount_yuan']}"
                )

                # 标记为 settled
                cursor.execute("""
                    UPDATE pending_commissions
                    SET status = 'settled', settled_at = %s
                    WHERE id = %s
                """, (now, rec["id"]))
            settled_count += 1

        if not dry_run:
            conn.commit()
        logger.info(
            f"[Commission Settle] settled={settled_count} cancelled={cancelled_count} "
            f"skipped={skipped_count} dry_run={dry_run}"
        )
        return {"settled": settled_count, "cancelled": cancelled_count, "skipped": skipped_count}
    except Exception as e:
        conn.rollback()
        logger.error(f"[Commission Settle] 失败: {e}")
        raise
    finally:
        conn.close()


# ---------- 老版本佣金引擎（v3.1 legacy，deprecated 但保留） ----------

# [2026-05-30 D4] process_commission(v3.1 legacy 12%/3%)已删除 · 全仓 0 调用死代码
# 佣金统一由 process_recharge_commission_v3 负责(已随工厂模式 + LEGACY_REFERRAL_V32_ENABLED=disabled 下线)
# commission_records 历史表 + commission_points 提现通道保留(老余额可提现)


def bind_referral(referred_user_id: int, referral_code: str):
    """
    注册时绑定推荐关系
    被 auth_api 注册逻辑调用
    """
    if not referral_code:
        return

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 查推荐码对应的用户
        # [P0 2026-08-05] 绑定链路同口径 —— 校验放行而绑定不认 = 「注册成功但没归属」
        from services.referral_code_normalize import fold_sql, normalize_code
        cursor.execute(
            f"SELECT user_id FROM referral_codes WHERE {fold_sql('code')} = {fold_sql('%s')}",
            (normalize_code(referral_code),))
        row = cursor.fetchone()
        if not row:
            raise ValueError("registration referral attribution is unavailable")

        referrer_id = row["user_id"]
        if referrer_id == referred_user_id:
            raise ValueError("registration referral cannot target the same account")

        # 绑定 L1 直推关系
        cursor.execute("""
            INSERT INTO referral_links (referrer_id, referred_id, level, commission_rate)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT DO NOTHING
        """, (referrer_id, referred_user_id, COMMISSION_RATE_L1))

        from services.commercial_service_routing import solidify_service_provider_invitation

        commercial_result = solidify_service_provider_invitation(
            cursor, int(referred_user_id), int(referrer_id), str(referral_code)
        )

        conn.commit()
        logger.info("[Referral] promotional attribution recorded")
        return {"commercial_binding_action": commercial_result["action"]}
    except Exception as e:
        conn.rollback()
        logger.error("[Referral] 注册归因事务失败 type=%s", type(e).__name__)
        raise
    finally:
        conn.close()


# ==================== 白标设置 ====================

_WHITELABEL_LOGO_MAX_BYTES = 2 * 1024 * 1024

_AGENT_WHITELABEL_FIELDS = (
    "company_name", "company_logo_url", "logo_url", "product_name", "favicon_url",
    "contact_name", "contact_phone", "contact_email", "contact_wechat", "slogan",
    "brand_color", "brand_terms_accepted_at",
)


def _serialize_agent_whitelabel_config(row) -> dict:
    """Return editable own-brand data without internal authorization evidence."""

    if not row:
        return {"configuration_status": "draft", "display_scope": "platform"}
    data = dict(row)
    configured_logo = data.get("logo_url") or data.get("company_logo_url")
    safe_logo = configured_logo if is_safe_public_whitelabel_logo_url(configured_logo) else None
    data["logo_url"] = safe_logo
    data["company_logo_url"] = safe_logo
    active = is_whitelabel_active_for_customer(
        {**data, "logo_url": safe_logo}
    )
    if data.get("whitelabel_status") == "suspended":
        configuration_status = "suspended"
    elif active:
        configuration_status = "approved"
    else:
        configuration_status = "draft"
    # D2（Owner 2026-07-22）：后台换肤改读 backoffice_brand_unlocked 独立授权位（fail-closed），
    # mode='oem' 不再自动隐含后台换肤；迁移脚本已把既有 oem+active+unlocked 授权映射到该位
    # （不无差别收回真正已授权者）；空值/旧脏值 → False 显示平台。
    backoffice_branding_active = bool(
        is_backoffice_brand_allowed(data)
        and (data.get("company_name") or "").strip()
        and safe_logo
    )
    return {
        **{field: data.get(field) for field in _AGENT_WHITELABEL_FIELDS},
        "configuration_status": configuration_status,
        "display_scope": (
            "approved_whitelabel"
            if backoffice_branding_active
            else "platform"
        ),
        "customer_branding_active": active,
        "backoffice_branding_active": backoffice_branding_active,
        # D1/D2（Owner 2026-07-22）：下发后台换肤权威判定 + 授权位当前态 + 品牌版本号（只读）
        "backoffice_brand_allowed": backoffice_branding_active,
        "backoffice_brand_unlocked": data.get("backoffice_brand_unlocked") is True,
        "brand_version": int(data.get("brand_version") or 1),
    }


def _detect_whitelabel_logo_ext(content: bytes) -> Optional[str]:
    """Return a safe logo extension for supported raster formats."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def _save_whitelabel_logo_for_user(target_user_id: int, content: bytes) -> str:
    """Validate and store a logo below an opaque, non-account-derived path."""
    if not content:
        raise HTTPException(status_code=400, detail="Logo 图片为空")
    if len(content) > _WHITELABEL_LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Logo 图片不能超过 2MB")

    ext = _detect_whitelabel_logo_ext(content)
    if not ext:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、WebP 格式 Logo")

    import secrets
    import time
    from pathlib import Path

    # ``target_user_id`` remains an internal authorization/audit input only. It
    # must never become part of a customer/agent-visible asset URL.
    int(target_user_id)
    opaque_bucket = secrets.token_urlsafe(18).replace("-", "").replace("_", "")
    upload_dir = Path("uploads/whitelabel-logos") / opaque_bucket
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"logo_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    filepath = upload_dir / filename
    filepath.write_bytes(content)
    return f"/uploads/whitelabel-logos/{opaque_bucket}/{filename}"


@router.get("/whitelabel")
async def get_whitelabel(request: Request):
    """获取白标设置（P1 2026-06-06：基础白标对所有 operator 放开 · 去掉 admin 授权 gate）。

    新用户无记录 → 返回 {}（前端进自助可编辑空表单态）。
    OEM / 暂停等授权位仍由 admin 经 /admin/whitelabel 控制（不在本端点）。
    """
    user = _get_user(request)
    # [白标继承] 对外品牌属于**商业主体**:团队长设置的品牌覆盖名下全部子账号。
    # 员工席位读到的是团队长的品牌(不是自己的空行 —— 那会落回平台默认 = 露馅),
    # 但只读:编辑权是 owner 专属(whitelabel.manage 在 OWNER_ONLY_CAPABILITIES)。
    from auth.principal_identity import resolve_branding_principal_user_id
    _brand_owner_id = resolve_branding_principal_user_id(request, fallback_user_id=int(user["user_id"]))
    _is_seat_member = _brand_owner_id != int(user["user_id"])
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM whitelabel_settings WHERE user_id = %s", (_brand_owner_id,))
        row = cursor.fetchone()
        data = _serialize_agent_whitelabel_config(row)
        if isinstance(data, dict) and _is_seat_member:
            # 让前端能把设置页切成只读态,而不是给员工一个改了保存不了的表单
            data["managed_by_owner"] = True
            data["editable"] = False
        if row:
            # D1（Owner 2026-07-22）：最近审计时间（只读展示 · 审计表缺失时降级为 None 不阻断读取）
            try:
                cursor.execute(
                    "SELECT MAX(created_at) AS last_audit_at FROM whitelabel_audit WHERE user_id = %s",
                    (_brand_owner_id,),
                )
                audit_row = cursor.fetchone()
                last_audit_at = (audit_row or {}).get("last_audit_at") if isinstance(audit_row, dict) or audit_row else None
                data["last_audit_at"] = last_audit_at.isoformat() if hasattr(last_audit_at, "isoformat") else (str(last_audit_at) if last_audit_at else None)
            except Exception as audit_err:
                logger.warning("[whitelabel-audit] last_audit_at 查询失败: %s", audit_err)
                data["last_audit_at"] = None
        return {"success": True, "data": data}
    finally:
        conn.close()


@router.post("/whitelabel/logo")
async def upload_whitelabel_logo(request: Request):
    """上传白标 Logo,只返回图片地址;是否生效仍由 PUT /whitelabel 保存决定。"""
    user = _get_user(request)
    # [白标继承] 编辑权是团队长专属(whitelabel.manage 属 OWNER_ONLY_CAPABILITIES)。
    # 不挡的话员工会在**自己的 uid 下建出第二份品牌行**,既覆盖不了子账号,
    # 又让"对外品牌"出现两个真相。§13 合同:给出下一步而不是干拒。
    from auth.principal_identity import resolve_branding_principal_user_id
    _brand_owner_id = resolve_branding_principal_user_id(request, fallback_user_id=int(user["user_id"]))
    if _brand_owner_id != int(user["user_id"]):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "WHITELABEL_OWNER_ONLY",
                "message": "对外品牌由团队长统一设置,员工账号不可修改。",
                "reason": "对外品牌属于服务商主体,统一设置才能保证客户看到的一致。",
                "impact": "本次修改没有保存。",
                "repair_hint": "如需调整品牌资料,请联系团队长在其账号中修改;修改后会自动覆盖全部员工账号。",
                "actions": [
                    {"id": "contact_owner", "label": "联系团队长", "type": "hint"},
                    {"id": "view_brand", "label": "查看当前对外品牌", "type": "nav", "target": "/agent/whitelabel"},
                ],
                "rule_version": "whitelabel-owner-only-v1",
            },
        )

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        raise HTTPException(status_code=400, detail="请选择 Logo 图片")

    content = await file.read()
    logo_url = _save_whitelabel_logo_for_user(int(user["user_id"]), content)
    return {
        "success": True,
        "data": {"logo_url": logo_url, "url": logo_url},
        "logo_url": logo_url,
    }


@router.post("/admin/whitelabel/{target_user_id}/logo")
async def admin_upload_whitelabel_logo(target_user_id: int, request: Request):
    """仅管理员可上传对外品牌 Logo;是否生效仍由保存品牌资料决定。"""
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, detail="仅管理员可上传对外品牌 Logo")

    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        raise HTTPException(status_code=400, detail="请选择 Logo 图片")

    content = await file.read()
    logo_url = _save_whitelabel_logo_for_user(target_user_id, content)
    logger.info("[whitelabel-v36] admin %s 上传 user %s 对外品牌 Logo", user.get("user_id"), target_user_id)
    return {
        "success": True,
        "data": {"logo_url": logo_url, "url": logo_url},
        "logo_url": logo_url,
    }


# [P1 2026-06-06] 去掉代理协议门(require_signed_agreement 会挡 agent_level>=1 未签协议者)·
#   基础白标对所有 operator 放开 · 改用下方轻量《对外品牌使用条款》gate(人人可签)。
@router.put("/whitelabel")
async def update_whitelabel(req: WhitelabelUpdateRequest, request: Request):
    """更新白标设置（operator 自填品牌资料 · 首次须同意《对外品牌使用条款》）"""
    user = _get_user(request)
    # T3 约束 4:授权/策略字段是安全边界,代理不可改 → hard 403(不静默忽略 · 走 admin 端点)
    try:
        _raw_body = await request.json()
    except Exception:
        _raw_body = {}
    _forbidden = _forbidden_whitelabel_authz_fields(_raw_body)
    if _forbidden:
        logger.warning(
            "[whitelabel-v36] 代理 user %s 越权尝试改授权字段 %s → 403",
            user.get("user_id"), sorted(_forbidden),
        )
        raise HTTPException(403, detail=f"以下字段仅管理员可设置: {sorted(_forbidden)}")
    # P1（2026-06-06）：基础白标对所有 operator 放开 · 去掉 mode=none 授权 gate。
    #   上方请求体授权字段 hard-403 保留（代理不能自设 mode/status/unlocked → 死守不能自升 OEM）。

    # D1（Owner 2026-07-22）：品牌字段入库前强制校验（长度/危险字符/URL 协议/域名/后缀/HEAD 探测）。
    cleaned = _validate_whitelabel_update_payload(req)
    _request_id = _whitelabel_request_id(request)
    _client_ip = _whitelabel_client_ip(request)

    logo = cleaned.get("logo_url") if ("logo_url" in cleaned or "company_logo_url" in cleaned) else (req.logo_url or req.company_logo_url)
    # 用 model_fields_set 区分"未传"和"显式传 null"
    # 未传的字段保留旧值（COALESCE），显式传的字段直接覆盖（含 null 清空）
    sent = req.model_fields_set
    # logo_url 和 company_logo_url 联动：任一传入则两个都更新
    logo_sent = "logo_url" in sent or "company_logo_url" in sent

    # 字段名 → (SQL 列名, 值) 映射（D1：用清洗后的值）
    field_map = {
        "company_name": ("company_name", cleaned.get("company_name", req.company_name)),
        "product_name": ("product_name", cleaned.get("product_name", req.product_name)),
        "favicon_url": ("favicon_url", cleaned.get("favicon_url", req.favicon_url)),
        "contact_name": ("contact_name", cleaned.get("contact_name", req.contact_name)),
        "contact_phone": ("contact_phone", cleaned.get("contact_phone", req.contact_phone)),
        "contact_email": ("contact_email", cleaned.get("contact_email", req.contact_email)),
        "contact_wechat": ("contact_wechat", cleaned.get("contact_wechat", req.contact_wechat)),
        "slogan": ("slogan", cleaned.get("slogan", req.slogan)),
        "brand_color": ("brand_color", cleaned.get("brand_color", req.brand_color)),
    }

    set_clauses = []
    update_params = []
    for field_name, (col, val) in field_map.items():
        if field_name in sent:
            set_clauses.append(f"{col} = %s")
            update_params.append(val)
        else:
            set_clauses.append(f"{col} = COALESCE(EXCLUDED.{col}, whitelabel_settings.{col})")
    # logo 字段联动处理
    if logo_sent:
        set_clauses.append("logo_url = %s")
        set_clauses.append("company_logo_url = %s")
        update_params.extend([logo, logo])
    else:
        set_clauses.append("logo_url = COALESCE(EXCLUDED.logo_url, whitelabel_settings.logo_url)")
        set_clauses.append("company_logo_url = COALESCE(EXCLUDED.company_logo_url, whitelabel_settings.company_logo_url)")
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    # [白标继承] 编辑权是团队长专属(whitelabel.manage 属 OWNER_ONLY_CAPABILITIES)。
    # 不挡的话员工会在**自己的 uid 下建出第二份品牌行**,既覆盖不了子账号,
    # 又让"对外品牌"出现两个真相。§13 合同:给出下一步而不是干拒。
    from auth.principal_identity import resolve_branding_principal_user_id
    _brand_owner_id = resolve_branding_principal_user_id(request, fallback_user_id=int(user["user_id"]))
    if _brand_owner_id != int(user["user_id"]):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "WHITELABEL_OWNER_ONLY",
                "message": "对外品牌由团队长统一设置,员工账号不可修改。",
                "reason": "对外品牌属于服务商主体,统一设置才能保证客户看到的一致。",
                "impact": "本次修改没有保存。",
                "repair_hint": "如需调整品牌资料,请联系团队长在其账号中修改;修改后会自动覆盖全部员工账号。",
                "actions": [
                    {"id": "contact_owner", "label": "联系团队长", "type": "hint"},
                    {"id": "view_brand", "label": "查看当前对外品牌", "type": "nav", "target": "/agent/whitelabel"},
                ],
                "rule_version": "whitelabel-owner-only-v1",
            },
        )

    with get_db() as conn:
        cursor = conn.cursor()
        # D1 审计：SELECT * 取变更前快照（before 值）
        cursor.execute(
            "SELECT * FROM whitelabel_settings WHERE user_id = %s",
            (user["user_id"],),
        )
        _cur = cursor.fetchone()
        _before = dict(_cur) if isinstance(_cur, dict) else (_cur or {})
        _cur_status = ((_cur.get("whitelabel_status") if isinstance(_cur, dict) else None) if _cur else None) or "none"
        # [P1 Codex finding3] 暂停=冻结资料:suspended 时非 admin 不可改品牌(防绕过只读 UI 直接调 API)
        if _cur_status == "suspended" and not user.get("is_admin"):
            raise HTTPException(403, detail="白标已被平台暂停,资料暂不可编辑,请联系平台恢复")
        # [P1 finding1] 轻量白牌条款:首次用基础白标须同意《对外品牌使用条款》(非代理协议·人人可签)
        _terms_at = ((_cur.get("brand_terms_accepted_at") if isinstance(_cur, dict) else None) if _cur else None)
        if not _terms_at and not bool(req.accept_brand_terms):
            raise HTTPException(403, detail={"code": "BRAND_TERMS_REQUIRED",
                "message": "首次使用对外品牌(白标)需先同意《对外品牌使用条款》"})
        cursor.execute(f"""
            INSERT INTO whitelabel_settings
                (user_id, company_name, logo_url, company_logo_url,
                 contact_name, contact_phone, contact_email, contact_wechat,
                 slogan, brand_color, product_name, favicon_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                {', '.join(set_clauses)}
            RETURNING *
        """, [user["user_id"], field_map["company_name"][1], logo, logo,
              field_map["contact_name"][1], field_map["contact_phone"][1],
              field_map["contact_email"][1], field_map["contact_wechat"][1],
              field_map["slogan"][1], field_map["brand_color"][1],
              field_map["product_name"][1], field_map["favicon_url"][1]] + update_params)
        result = dict(cursor.fetchone())
        # [P1 finding1] 首次同意 → 落《对外品牌使用条款》同意时间(已同意则不覆盖)
        if bool(req.accept_brand_terms) and not _terms_at:
            cursor.execute(
                "UPDATE whitelabel_settings SET brand_terms_accepted_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s AND brand_terms_accepted_at IS NULL", (user["user_id"],))
            result["brand_terms_accepted_at"] = "accepted"

        # 客户可见品牌是服务商自助能力：资料完整并同意条款后立即生效。
        # OEM 后台换肤仍由管理员授权，本端点不会把 external_only 静默升级为 oem。
        current_mode = str(result.get("whitelabel_mode") or "none")
        current_status = str(result.get("whitelabel_status") or "locked")
        terms_ready = bool(_terms_at or req.accept_brand_terms)
        complete = bool(
            str(result.get("company_name") or "").strip()
            and is_safe_public_whitelabel_logo_url(result.get("logo_url"))
            and terms_ready
        )
        if current_status != "suspended" and current_mode != "oem":
            next_status = "active" if complete else "locked"
            cursor.execute(
                "UPDATE whitelabel_settings "
                "SET whitelabel_mode = 'external_only', whitelabel_status = %s, "
                "unlocked_by_admin = FALSE, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s AND whitelabel_status <> 'suspended'",
                (next_status, user["user_id"]),
            )
            result.update({
                "whitelabel_mode": "external_only",
                "whitelabel_status": next_status,
                "unlocked_by_admin": False,
            })

        # D1（Owner 2026-07-22）：审计（全字段 before/after）+ 品牌版本号 +1。
        # 审计与品牌 UPDATE 同事务：审计写失败整体回滚，不留无痕覆盖。
        _audit_changes = []
        for _field, (_col, _val) in field_map.items():
            if _field in sent:
                _audit_changes.append((_col, _before.get(_col), result.get(_col)))
        if logo_sent:
            _audit_changes.append(("logo_url", _before.get("logo_url"), result.get("logo_url")))
            _audit_changes.append(("company_logo_url", _before.get("company_logo_url"), result.get("company_logo_url")))
        for _authz_col in ("whitelabel_mode", "whitelabel_status", "unlocked_by_admin"):
            _audit_changes.append((_authz_col, _before.get(_authz_col), result.get(_authz_col)))
        if bool(req.accept_brand_terms) and not _terms_at:
            _audit_changes.append(("brand_terms_accepted_at", None, "accepted"))
        _write_whitelabel_audit(
            cursor,
            user_id=user["user_id"],
            actor_user_id=user["user_id"],
            actor_role="admin" if user.get("is_admin") else "agent",
            changes=_audit_changes,
            request_id=_request_id,
            ip=_client_ip,
            reason=None,
        )
        result["brand_version"] = _bump_brand_version(
            cursor,
            user["user_id"],
            result.get("brand_version"),
        )
    return {"success": True, "data": _serialize_agent_whitelabel_config(result)}


@router.put("/admin/whitelabel/{target_user_id}")
async def admin_set_whitelabel(target_user_id: int, req: WhitelabelGrantRequest, request: Request):
    """admin 授予/暂停代理白标授权位（whitelabel_mode/status/unlocked）。仅 admin。

    与代理自填资料分离（T3 约束 4）：代理走 PUT /whitelabel 只能改品牌资料；
    授权位只能 admin 经此端点写。
    决策 C：激活 external_only/oem 前校验 company_name 已填，缺失返 400，
    不允许客户面 fallback OmniRank。
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, detail="仅管理员可授权白标")

    # [客户反馈④ 2026-08-09] 后台换肤被裁决钉死的账号:写入侧当场拒绝并说清楚。
    #
    # 🔴 这不是"收紧权限",是堵一条**能把生产部署卡死**的路:
    #   `scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql` §6 的
    #   `whitelabel_backoffice_schema_blockers()` 把"user_id=132 的
    #   backoffice_brand_unlocked 为 TRUE"定义成一条 blocker,而 prestart /
    #   运行时启动自检 / release readiness 三处都读它、fail-closed。
    #   同一份迁移的 §4 每次部署又会把它无声改回 FALSE。
    #   于是修复前的实际行为是:admin 点了 → 库里真写成 TRUE(**此刻起该库处于
    #   readiness 合同定义的非法态**)→ 下次部署被 §4 静默改回 → 没人知道发生过什么。
    #   生产实证:whitelabel_audit id=21(2026-07-31 19:57:43,admin 把 132 置 True),
    #   而今天该行是 FALSE、授权戳还留着、审计里没有任何一条改回去的记录。
    #   **工单说的"管理员漏点第三个开关"不成立 —— 他点了,是系统改回去的。**
    #
    # 要不要真放开 132 是 D2 裁决的变更,归 Owner,不在本次修复范围。
    if (
        req.backoffice_brand_unlocked is True
        and int(target_user_id) in BACKOFFICE_BRAND_PINNED_USER_IDS
    ):
        raise HTTPException(
            400,
            detail={
                "code": "BACKOFFICE_BRAND_PINNED_CUSTOMER_ONLY",
                "message": "该服务商按 2026-07-22 白标作用域裁决固定为「仅客户页面」"
                           "· 后台换肤不能在这里授予(授予了也会被系统还原)。"
                           "确需放开请走裁决变更,由平台侧改合同后再操作。",
            },
        )

    try:
        mode, status, unlocked = _normalize_whitelabel_grant(req.whitelabel_mode, req.whitelabel_status)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    # Active white label requires an explicit admin grant and complete brand data.
    if mode in ("external_only", "oem") and status == "active":
        conn0 = get_connection()
        try:
            cur0 = conn0.cursor()
            cur0.execute(
                "SELECT company_name, COALESCE(logo_url, company_logo_url) AS logo_url "
                "FROM whitelabel_settings WHERE user_id = %s",
                (target_user_id,),
            )
            row0 = cur0.fetchone()
            company = (row0.get("company_name") if isinstance(row0, dict) else (row0[0] if row0 else None)) if row0 else None
            logo_url = (row0.get("logo_url") if isinstance(row0, dict) else (row0[1] if row0 else None)) if row0 else None
            if (
                not company
                or not str(company).strip()
                or not is_safe_public_whitelabel_logo_url(logo_url)
            ):
                raise HTTPException(400, detail="白标资料不完整：需先填写公司名称和 Logo")
        finally:
            conn0.close()

    with get_db() as conn:
        cursor = conn.cursor()
        # D1 审计：变更前快照
        cursor.execute(
            "SELECT whitelabel_mode, whitelabel_status, unlocked_by_admin, backoffice_brand_unlocked "
            "FROM whitelabel_settings WHERE user_id = %s",
            (target_user_id,),
        )
        _before_row = cursor.fetchone()
        _before = dict(_before_row) if isinstance(_before_row, dict) else (_before_row or {})
        cursor.execute("""
            INSERT INTO whitelabel_settings
                (user_id, whitelabel_mode, whitelabel_status, unlocked_by_admin,
                 approved_by, approved_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                whitelabel_mode = EXCLUDED.whitelabel_mode,
                whitelabel_status = EXCLUDED.whitelabel_status,
                unlocked_by_admin = EXCLUDED.unlocked_by_admin,
                approved_by = EXCLUDED.approved_by,
                approved_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            RETURNING user_id, whitelabel_mode, whitelabel_status,
                      unlocked_by_admin, approved_by, approved_at, brand_version
        """, (target_user_id, mode, status, unlocked, user["user_id"]))
        _result_row = cursor.fetchone()
        result = dict(_result_row) if _result_row else None

        # D1/D2（Owner 2026-07-22）：backoffice 后台换肤独立授权/撤权（与 mode 解耦）。
        # suspend（whitelabel_status='suspended'）语义 = emergency suppression（D3）：
        # 快照展示层实时压制由 services/public_whitelabel.resolve_branding_context 承担。
        _audit_changes = [
            ("whitelabel_mode", _before.get("whitelabel_mode"), mode),
            ("whitelabel_status", _before.get("whitelabel_status"), status),
            ("unlocked_by_admin", _before.get("unlocked_by_admin"), unlocked),
        ]
        if req.backoffice_brand_unlocked is not None:
            if req.backoffice_brand_unlocked:
                cursor.execute(
                    "UPDATE whitelabel_settings SET backoffice_brand_unlocked = TRUE, "
                    "backoffice_brand_granted_by = %s, backoffice_brand_granted_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = %s",
                    (user["user_id"], target_user_id),
                )
            else:
                cursor.execute(
                    "UPDATE whitelabel_settings SET backoffice_brand_unlocked = FALSE, "
                    "backoffice_brand_granted_by = NULL, backoffice_brand_granted_at = NULL "
                    "WHERE user_id = %s",
                    (target_user_id,),
                )
            _audit_changes.append((
                "backoffice_brand_unlocked",
                _before.get("backoffice_brand_unlocked"),
                bool(req.backoffice_brand_unlocked),
            ))
        # D1 审计 + 版本化：授权位/后台换肤任何变更 → 审计 + brand_version+1（缓存失效维度）
        _write_whitelabel_audit(
            cursor,
            user_id=target_user_id,
            actor_user_id=user["user_id"],
            actor_role="admin",
            changes=_audit_changes,
            request_id=_whitelabel_request_id(request),
            ip=_whitelabel_client_ip(request),
            reason=(req.reason or None),
        )
        if result is not None:
            result["brand_version"] = _bump_brand_version(
                cursor,
                target_user_id,
                result.get("brand_version"),
            )
        else:
            _bump_brand_version(cursor, target_user_id)

    logger.info(
        "[whitelabel-v36] admin %s 设 user %s 白标 → mode=%s status=%s unlocked=%s backoffice=%s",
        user.get("user_id"), target_user_id, mode, status, unlocked, req.backoffice_brand_unlocked,
    )
    return {"success": True, "data": result}


@router.get("/admin/whitelabel")
async def admin_list_whitelabel(request: Request, q: Optional[str] = None):
    """admin 列白标授权状态(配 /admin/whitelabel 授权页)。仅 admin。

    - q 空:列所有已有 whitelabel_settings 行的代理(已授权 / 进行中)
    - q 非空:按 username / display_name / user_id 搜 users(用于授权新代理)· LEFT JOIN 状态
    每行返回:user_id / username / display_name / whitelabel_mode / whitelabel_status /
              company_name / has_company_name(激活前置校验用) / approved_at
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, detail="仅管理员可查看白标授权")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        q_str = (q or "").strip()
        if q_str:
            like = f"%{q_str}%"
            uid_match = int(q_str) if q_str.isdigit() else -1
            cursor.execute("""
                SELECT u.id AS user_id, u.username, u.display_name,
                       w.whitelabel_mode, w.whitelabel_status, w.company_name, w.approved_at,
                       w.logo_url, w.slogan, w.brand_color, w.contact_name, w.contact_phone,
                       w.contact_wechat, w.contact_email, w.product_name, w.favicon_url,
                       w.backoffice_brand_unlocked, w.brand_version
                FROM users u
                LEFT JOIN whitelabel_settings w ON w.user_id = u.id
                WHERE u.username ILIKE %s OR u.display_name ILIKE %s OR u.id = %s
                ORDER BY u.id DESC
                LIMIT 50
            """, (like, like, uid_match))
        else:
            cursor.execute("""
                SELECT u.id AS user_id, u.username, u.display_name,
                       w.whitelabel_mode, w.whitelabel_status, w.company_name, w.approved_at,
                       w.logo_url, w.slogan, w.brand_color, w.contact_name, w.contact_phone,
                       w.contact_wechat, w.contact_email, w.product_name, w.favicon_url,
                       w.backoffice_brand_unlocked, w.brand_version
                FROM whitelabel_settings w
                JOIN users u ON u.id = w.user_id
                ORDER BY w.updated_at DESC NULLS LAST
                LIMIT 200
            """)
        items = []
        for row in cursor.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            company = d.get("company_name")
            items.append({
                "user_id": d.get("user_id"),
                "username": d.get("username"),
                "display_name": d.get("display_name"),
                "whitelabel_mode": d.get("whitelabel_mode") or "none",
                "whitelabel_status": d.get("whitelabel_status") or "locked",
                "company_name": company,
                "has_company_name": bool(company and str(company).strip()),
                "approved_at": d.get("approved_at").isoformat() if d.get("approved_at") else None,
                # [V3.6 2026-05-30] admin 代填弹窗预填用 · 品牌字段
                "logo_url": d.get("logo_url"),
                "slogan": d.get("slogan"),
                "brand_color": d.get("brand_color"),
                "contact_name": d.get("contact_name"),
                "contact_phone": d.get("contact_phone"),
                "contact_wechat": d.get("contact_wechat"),
                "contact_email": d.get("contact_email"),
                "product_name": d.get("product_name"),
                "favicon_url": d.get("favicon_url"),
                # 板块 C（Owner 2026-07-22 D1/D2）：后台换肤授权位 + 品牌版本号（admin 列表展示用）
                "backoffice_brand_unlocked": d.get("backoffice_brand_unlocked") is True,
                "brand_version": int(d.get("brand_version") or 1),
                # [客户反馈④ 2026-08-09] 该账号是否被 D2 裁决钉死为 customer-only。
                #   管理页据此置灰第三个下拉 —— 修复前它可点、点了没用、没人告诉他。
                "backoffice_brand_pinned": int(d.get("user_id") or 0) in BACKOFFICE_BRAND_PINNED_USER_IDS,
            })
        # [客户反馈④ 2026-08-09] 把**总闸**的运行时真值一起下发。
        #   2026-08-09 生产实测:omnirank-blue / omnirank-green 两槽
        #   `printenv WHITELABEL_BACKOFFICE_BRAND_ENABLED` 都是空 →
        #   `is_backoffice_brand_enabled()` 恒 False → **不管管理员怎么点这三个开关,
        #   全体账户的后台换肤都不生效**(125/133/46 三户三条件齐全,照样不生效)。
        #   不下发这个值,管理员只能看着"已授权"却发现没效果,然后来报"授权失效"。
        try:
            from services.public_whitelabel import is_backoffice_brand_enabled
            _flag = bool(is_backoffice_brand_enabled())
        except Exception:
            _flag = False
        return {
            "success": True,
            "items": items,
            "backoffice_brand_flag_enabled": _flag,
        }
    finally:
        conn.close()


@router.put("/admin/whitelabel/{target_user_id}/brand")
async def admin_set_whitelabel_brand(target_user_id: int, req: WhitelabelUpdateRequest, request: Request):
    """admin 代填代理品牌资料(company_name/logo/联系方式/oem 字段)。仅 admin。

    与授权位端点分离(职责清晰):本端点**只写品牌内容,不碰 mode/status/unlocked**
    (授权位仍走 PUT /admin/whitelabel/{id})。用途:admin onboard 代理 /
    代理迟迟不填 company_name 导致激活被阻时,admin 代填解锁。
    前端弹窗已用 GET 列表的品牌字段预填全部值,故此处直接覆盖写(WYSIWYG · 无意外清空)。
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, detail="仅管理员可代填白标品牌")
    # D1（Owner 2026-07-22）：admin 代填同样强制校验 + 审计 + 版本化（与代理自填同标准）
    cleaned = _validate_whitelabel_update_payload(req)
    _request_id = _whitelabel_request_id(request)
    _client_ip = _whitelabel_client_ip(request)
    logo = cleaned.get("logo_url") if ("logo_url" in cleaned or "company_logo_url" in cleaned) else (req.logo_url or req.company_logo_url)
    _val = lambda f: cleaned.get(f, getattr(req, f))  # noqa: E731 · 清洗后值（未发送字段取原值）
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM whitelabel_settings WHERE user_id = %s", (target_user_id,))
        _before_row = cursor.fetchone()
        _before = dict(_before_row) if isinstance(_before_row, dict) else (_before_row or {})
        cursor.execute("""
            INSERT INTO whitelabel_settings
                (user_id, company_name, logo_url, company_logo_url,
                 contact_name, contact_phone, contact_email, contact_wechat,
                 slogan, brand_color, product_name, favicon_url, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                logo_url = EXCLUDED.logo_url,
                company_logo_url = EXCLUDED.company_logo_url,
                contact_name = EXCLUDED.contact_name,
                contact_phone = EXCLUDED.contact_phone,
                contact_email = EXCLUDED.contact_email,
                contact_wechat = EXCLUDED.contact_wechat,
                slogan = EXCLUDED.slogan,
                brand_color = EXCLUDED.brand_color,
                product_name = EXCLUDED.product_name,
                favicon_url = EXCLUDED.favicon_url,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *
        """, (target_user_id, _val("company_name"), logo, logo,
              _val("contact_name"), _val("contact_phone"), _val("contact_email"), _val("contact_wechat"),
              _val("slogan"), _val("brand_color"), _val("product_name"), _val("favicon_url")))
        _result_row = cursor.fetchone()
        result = dict(_result_row) if _result_row else None
        _after = result or {}
        # D1 审计（品牌字段 before/after）+ 版本号 +1
        _write_whitelabel_audit(
            cursor,
            user_id=target_user_id,
            actor_user_id=user["user_id"],
            actor_role="admin",
            changes=[
                (_col, _before.get(_col), _after.get(_col))
                for _col in (
                    "company_name", "logo_url", "company_logo_url",
                    "contact_name", "contact_phone", "contact_email", "contact_wechat",
                    "slogan", "brand_color", "product_name", "favicon_url",
                )
            ],
            request_id=_request_id,
            ip=_client_ip,
            reason="admin 代填品牌资料",
        )
        if result is not None:
            result["brand_version"] = _bump_brand_version(
                cursor,
                target_user_id,
                result.get("brand_version"),
            )
        else:
            _bump_brand_version(cursor, target_user_id)

    logger.info("[whitelabel-v36] admin %s 代填 user %s 品牌资料", user.get("user_id"), target_user_id)
    return {"success": True, "data": result}
