"""Public white-label lookup shared by report pages and PDF footers."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from db.connection import get_connection
from schemas.public_contracts import PublicBrandDTO, PublicBrandingDTO
from utils.masking import mask_email, mask_phone, mask_wechat

logger = logging.getLogger("GEO-PublicWhitelabel")


def _row_to_dict(row: Any) -> dict | None:
    if not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return None


def _resolve_owner_from_brand_id(brand_id: int | None) -> int | None:
    if not brand_id:
        return None
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT owner_user_id FROM brands WHERE id = %s", (brand_id,))
        row = _row_to_dict(cursor.fetchone())
        return int(row["owner_user_id"]) if row and row.get("owner_user_id") else None
    finally:
        conn.close()


def get_public_whitelabel_data(
    quote_id: int | None = None,
    *,
    brand_owner_user_id: int | None = None,
    brand_id: int | None = None,
) -> dict:
    """Return an allowlisted public brand payload.

    Account identity is resolved only from a server-trusted business object.
    """
    resolved_uid = None
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if (not resolved_uid or resolved_uid == 0) and quote_id:
            cursor.execute(
                """
                SELECT b.owner_user_id
                FROM quotes q JOIN brands b ON q.brand_id = b.id
                WHERE q.id = %s
                """,
                (quote_id,),
            )
            row = _row_to_dict(cursor.fetchone())
            if row and row.get("owner_user_id"):
                resolved_uid = int(row["owner_user_id"])

        if not resolved_uid and brand_owner_user_id:
            resolved_uid = int(brand_owner_user_id)

        if not resolved_uid and brand_id:
            resolved_uid = _resolve_owner_from_brand_id(int(brand_id))

        if not resolved_uid:
            public = public_branding_from_record(None, surface="customer")
            return {**public, "whitelabel": None}

        cursor.execute(
            """
            SELECT company_name, logo_url, slogan,
                   contact_name, contact_phone, contact_wechat, contact_email,
                   brand_color, product_name, favicon_url,
                   whitelabel_mode, whitelabel_status, unlocked_by_admin
            FROM whitelabel_settings WHERE user_id = %s
            """,
            (resolved_uid,),
        )
        wl_row = _row_to_dict(cursor.fetchone())
        public = public_branding_from_record(wl_row, surface="customer")
        return {
            **public,
            "whitelabel": public["brand"] if public["display_scope"] == "approved_whitelabel" else None,
        }
    except Exception as exc:
        logger.warning("[public_whitelabel] lookup failed: %s", exc)
        public = public_branding_from_record(None, surface="customer")
        return {**public, "whitelabel": None}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== v3.6 双档白标统一解析层（SSOT · T2）====================
# 平台默认品牌：非白标 / admin / 未授权 时的兜底身份（合法平台标识，非泄漏）
_PLATFORM_BRAND = {
    "company_name": "OmniRank · 全域上榜",
    "product_name": "OmniRank",
    "logo_url": "/logo-192.png",
    "favicon_url": None,
    "slogan": None,
    "brand_color": None,
    "contact_name": None,
    "contact_phone": None,
    "contact_wechat": None,
    "contact_email": None,
}

_BRAND_FIELDS = (
    "company_name", "product_name", "logo_url", "favicon_url",
    "slogan", "brand_color",
    "contact_name", "contact_phone", "contact_wechat", "contact_email",
)

_LEGACY_ACCOUNT_LOGO_RE = re.compile(
    r"(?:^|/)uploads/whitelabel-logos/\d+(?:/|$)", re.IGNORECASE
)
_SENSITIVE_URL_KEYS = {
    "user_id", "agent_user_id", "upstream_user_id", "resolved_user_id",
    "shared_by", "service_account_code", "channel_account_code",
}


def is_safe_public_whitelabel_logo_url(value: Any) -> bool:
    """Reject logo URLs whose path/query discloses an internal account identity.

    Historical uploads used ``.../whitelabel-logos/{user_id}/...``. They remain
    available to administrators for remediation, but cannot activate a public or
    service-provider-facing brand until the logo is re-uploaded to an opaque path.
    """

    url = str(value or "").strip()
    if not url or _LEGACY_ACCOUNT_LOGO_RE.search(url.replace("\\", "/")):
        return False
    try:
        query_keys = {key.lower() for key, _ in parse_qsl(urlsplit(url).query)}
    except ValueError:
        return False
    return not query_keys.intersection(_SENSITIVE_URL_KEYS)


def _mask_brand_contacts(brand: dict) -> dict:
    b = dict(brand)
    b["contact_phone"] = mask_phone(b.get("contact_phone"))
    b["contact_wechat"] = mask_wechat(b.get("contact_wechat"))
    b["contact_email"] = mask_email(b.get("contact_email"))
    return b


def _brand_from_row(row: dict, *, surface: str) -> dict:
    """已授权白标行 → 对外品牌 dict。

    ⚠️ 合并基必须是全 None 中性基,绝不能是 _PLATFORM_BRAND:本函数只在
    effective/approved 分支被调用,产出的是「服务商自己的品牌」;若以平台品牌为基,
    服务商未填的字段会继承平台值 —— 2026-08-05 #133 实锤:product_name=NULL 继承
    "OmniRank",客户打开白标报告顶部 = 服务商 logo + "OmniRank" 四个字。
    缺省字段留 None,由前端/渲染层按 product_name→company_name 顺序自行回落。
    """
    overrides = {k: row.get(k) for k in _BRAND_FIELDS if row.get(k) is not None}
    if not is_safe_public_whitelabel_logo_url(overrides.get("logo_url")):
        overrides.pop("logo_url", None)
    brand: dict = {k: None for k in _BRAND_FIELDS}
    brand.update(overrides)
    return _mask_brand_contacts(brand) if surface == "customer" else brand


def _brand_is_complete(row: dict | None) -> bool:
    """A display name and logo are the minimum complete public brand profile."""

    if not row:
        return False
    return bool(
        (row.get("company_name") or "").strip()
        and is_safe_public_whitelabel_logo_url(row.get("logo_url"))
    )


# 板块 C（Owner 2026-07-22 裁决 D1/D2）· 内部后台换肤总闸（合同 §7 · 默认 false）。
# false → 所有内部后台一律平台标识（fail-closed）；
# true  → 仅 backoffice_brand_unlocked 且 status=active 的账号显示自有品牌。
_BACKOFFICE_BRAND_FLAG = "WHITELABEL_BACKOFFICE_BRAND_ENABLED"
_FLAG_TRUE = {"1", "true", "yes", "on"}


def is_backoffice_brand_enabled() -> bool:
    """板块 C 内部后台换肤总闸（env · 默认 false · fail-closed）。"""

    return os.getenv(_BACKOFFICE_BRAND_FLAG, "false").strip().lower() in _FLAG_TRUE


def is_backoffice_brand_allowed(row: dict | None) -> bool:
    """D2（Owner 2026-07-22）后台换肤独立授权判定（fail-closed）。

    后台换肤不再由 whitelabel_mode='oem' 隐含，改读独立授权位：
    总闸开 ∧ backoffice_brand_unlocked IS TRUE ∧ whitelabel_status='active'。
    空值/旧脏值/无法证明档位 → False（内部显示 OmniRank）。
    """

    if not row or not is_backoffice_brand_enabled():
        return False
    return bool(
        row.get("backoffice_brand_unlocked") is True
        and (row.get("whitelabel_status") or "") == "active"
    )


def _agent_surface_brand_ready(row: dict | None) -> bool:
    """agent surface 展示代理品牌的完整判定（D2 · 与 mode 解耦）。

    = backoffice 独立授权 ∧ 品牌资料可展示（company_name + 安全 logo）。
    status='active' 已在 is_backoffice_brand_allowed 内含。
    """

    return bool(
        row
        and is_backoffice_brand_allowed(row)
        and (row.get("company_name") or "").strip()
        and is_safe_public_whitelabel_logo_url(row.get("logo_url"))
    )


def public_branding_from_record(row: dict | None, *, surface: str) -> dict:
    """Serialize a public white-label record without authorization/account metadata."""

    mode = (row or {}).get("whitelabel_mode") or "none"
    if row and surface == "agent":
        # D2（Owner 2026-07-22）：后台换肤只看 backoffice 独立授权，与 mode 解耦。
        effective = _agent_surface_brand_ready(row)
    else:
        effective = bool(
            row
            and _is_brand_effective(
                mode,
                row.get("whitelabel_status"),
                row.get("unlocked_by_admin"),
                row.get("company_name"),
                row.get("logo_url"),
            )
            and _can_show_brand(surface, mode)
        )
    source = _brand_from_row(row or {}, surface=surface) if effective else dict(_PLATFORM_BRAND)
    brand = PublicBrandDTO(**{key: source.get(key) for key in PublicBrandDTO.model_fields}).model_dump()
    return PublicBrandingDTO(
        display_scope="approved_whitelabel" if effective else "platform",
        brand=PublicBrandDTO(**brand),
    ).model_dump()


def _can_show_brand(surface: str, mode: str, backoffice_allowed: bool = False) -> bool:
    """surface × 授权 是否允许展示代理品牌（admin / 未知 surface → 永远平台）。

    customer:external_only 或 oem 都可见；
    agent:仅 backoffice_brand_unlocked 独立授权可见（D2 · Owner 2026-07-22，
          mode='oem' 不再自动隐含后台换肤；无法证明档位 → fail-closed False）；
    admin:永不。
    snapshot 分支与 live 分支都必须走此函数（修 Codex P1：snapshot 曾绕过 surface 矩阵）。
    """
    if surface == "customer":
        return mode in ("external_only", "oem")
    if surface == "agent":
        return backoffice_allowed is True
    return False


# 平台默认品牌标识集合（P0 · 识别 resolve_branding_context 对无白标返回的 _PLATFORM_BRAND dict）
_PLATFORM_BRAND_IDENTIFIERS = {
    s for s in (
        (_PLATFORM_BRAND.get("company_name") or "").strip(),
        (_PLATFORM_BRAND.get("product_name") or "").strip(),
        "OmniRank", "全域上榜", "OmniRank · 全域上榜",
    ) if s
}


def is_real_agent_brand(branding) -> bool:
    """branding 是否为「真实代理白标」（而非 _PLATFORM_BRAND 平台默认 / 空）。

    ⚠️ P0 防穿帮（2026-06-06 Codex）：resolve_branding_context 对无白标**永远返回
    _PLATFORM_BRAND dict（company_name='OmniRank · 全域上榜'）**。客户面 renderer/footer 若仅凭
    `branding 非空 / company_name 非空` 判定，就会把平台默认 dict 当代理白标显示 → 露平台名。
    所有客户面渲染层必须用本函数区分（platform_default dict → 视作无白标留白）。
    """
    if not branding or not isinstance(branding, dict):
        return False
    name = (branding.get("company_name") or "").strip()
    if not name:
        return False
    return name not in _PLATFORM_BRAND_IDENTIFIERS


def _is_brand_effective(mode, status, unlocked, company_name, logo_url=None) -> bool:
    """Customer branding is self-serve; OEM back-office branding remains governed."""

    complete = bool(
        status == "active"
        and (company_name or "").strip()
        and is_safe_public_whitelabel_logo_url(logo_url)
    )
    if mode == "external_only":
        return complete
    if mode == "oem":
        return complete and unlocked is True
    return False


def is_whitelabel_active_for_customer(wl_row) -> bool:
    """customer surface 展示代理品牌判定（SSOT · 走 _is_brand_effective）。

    external_only 由服务商完善资料后自助生效；oem 后台换肤仍需管理员解锁。
    供 share_api（海报/客户报告）、get_public_whitelabel_data、generate-quote 共用，
    入参为 whitelabel_settings 整行（SELECT * · 含 mode/status/unlocked）。
    """
    if not wl_row:
        return False
    return _is_brand_effective(
        wl_row.get("whitelabel_mode"),
        wl_row.get("whitelabel_status"),
        wl_row.get("unlocked_by_admin"),
        wl_row.get("company_name"),
        wl_row.get("logo_url"),
    )


def _load_live_whitelabel_row_for_snapshot(
    *,
    owner_user_id: int | None = None,
    quote_id: int | None = None,
    brand_id: int | None = None,
) -> dict | None:
    """快照分支实时核验（D3 emergency suppression + D2 backoffice 授权 · 2026-07-22）。

    返回：
      dict  → 已核验到的实时 whitelabel_settings 行（含空 dict=已核验但无此行）；
      None  → 无法核验（未提供任何属主线索 / 查询异常），调用方按冻结快照策略兜底。
    只读不写；任何异常不外抛（快照展示是降级路径，不能因核验失败打挂客户材料）。
    """

    if not owner_user_id and not quote_id and not brand_id:
        return None
    conn = None
    try:
        resolved_uid = int(owner_user_id) if owner_user_id else None
        conn = get_connection()
        cursor = conn.cursor()
        if (not resolved_uid or resolved_uid == 0) and quote_id:
            cursor.execute(
                "SELECT b.owner_user_id FROM quotes q JOIN brands b ON q.brand_id = b.id WHERE q.id = %s",
                (quote_id,),
            )
            row = _row_to_dict(cursor.fetchone())
            if row and row.get("owner_user_id"):
                resolved_uid = int(row["owner_user_id"])
        if not resolved_uid and brand_id:
            resolved_uid = _resolve_owner_from_brand_id(int(brand_id))
        if not resolved_uid:
            return {}  # 已尝试核验但无法定位属主 → 视为无实时证据
        cursor.execute(
            "SELECT whitelabel_status, backoffice_brand_unlocked "
            "FROM whitelabel_settings WHERE user_id = %s",
            (resolved_uid,),
        )
        return _row_to_dict(cursor.fetchone()) or {}
    except Exception as exc:
        logger.warning("[branding] snapshot live-check failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_branding_context(
    *,
    owner_user_id: int | None = None,
    quote_id: int | None = None,
    brand_id: int | None = None,
    shared_by: int | None = None,
    surface: str = "customer",
    snapshot: dict | None = None,
) -> dict:
    """v3.6 双档白标统一解析层（SSOT）。账号解析只在函数内部存在。

    ⚠️ 约束 1：`surface` 必须由调用方 endpoint 在服务端写死，**禁止**取自请求体/query
       （customer=客户公开页 / agent=代理工作台 / admin=后台）。
    surface 生效矩阵（Owner 2026-07-22 裁决 D1/D2 更新）：
      admin    → 永远平台品牌（OmniRank）
      agent    → 仅 backoffice_brand_unlocked 独立授权且 status=active 才出代理品牌
                 （mode='oem' 不再自动隐含后台换肤；无法证明档位 → fail-closed 平台）
      customer → mode in (external_only, oem) 且已授权才出代理品牌，否则平台
                 （D1：客户触达页面服务商自设免审批，行为不变）
    有效白标(P1 2026-06-06 · 走 _is_brand_effective):
      external_only 须 status='active' 且资料完整；oem 另须 unlocked_by_admin=true。
    约束 2：传入 `snapshot`（agent_quotes.whitelabel 冻结快照）→ source='agent_quote_snapshot'
       （防代理改名后历史客户材料漂移）；否则读实时 whitelabel_settings → source='whitelabel_settings'。
    D3（Owner 2026-07-22）：正常到期/撤权 → 快照保留签发时冻结 Logo（快照数据不动）；
       emergency suspension（实时 whitelabel_status='suspended'）→ 快照展示层立即压制
       回落平台标识；新报告永远使用当前有效授权。
    brand 永远有值（最差平台默认）。本函数为 T2 新增能力，调用方接入在阶段 1/T4。
    """
    if surface == "admin":
        return {"mode": "none", "status": "locked", "brand": dict(_PLATFORM_BRAND),
                "source": "platform_default", "display_scope": "platform"}

    # 约束 2：快照优先（已生成材料用冻结品牌）· 必须同样过 surface 矩阵（修 Codex P1）
    if snapshot is not None:
        snap_mode = snapshot.get("whitelabel_mode") or "none"
        snapshot_approved = _is_brand_effective(
            snap_mode,
            snapshot.get("whitelabel_status"),
            snapshot.get("unlocked_by_admin"),
            snapshot.get("company_name"),
            snapshot.get("logo_url"),
        )
        live_row = None
        backoffice_allowed = False
        if snapshot_approved:
            # D3：快照分支实时核验（仅在能提供属主线索时查库；查不到=无法核验，走冻结策略）
            live_row = _load_live_whitelabel_row_for_snapshot(
                owner_user_id=shared_by or owner_user_id, quote_id=quote_id, brand_id=brand_id,
            )
            if live_row is not None:
                # emergency suspension（冒用/安全/违法）→ 立即压制自定义品牌回落平台标识
                if (live_row.get("whitelabel_status") or "") == "suspended":
                    return {"mode": "none", "status": "suspended",
                            "brand": dict(_PLATFORM_BRAND),
                            "source": "platform_default",
                            "display_scope": "platform",
                            "suppression": "emergency_suspension"}
                # D2：agent surface 以实时 backoffice 授权为准（撤权即失效）
                backoffice_allowed = is_backoffice_brand_allowed(live_row)
            else:
                # 无法实时核验（未提供属主线索）→ agent surface 仅信快照自带冻结授权位
                # （新快照才有）；旧快照无该位 → fail-closed（D2「无法证明档位 → 平台」）。
                # customer surface 保持 D3 冻结策略：正常到期/撤权不影响已签发材料。
                backoffice_allowed = bool(
                    snapshot.get("backoffice_brand_unlocked") is True
                    and is_backoffice_brand_enabled()
                )
        if snapshot_approved and _can_show_brand(surface, snap_mode, backoffice_allowed=backoffice_allowed):
            return {"mode": snap_mode, "status": "active",
                    "brand": _brand_from_row(snapshot, surface=surface),
                    "source": "agent_quote_snapshot",
                    "display_scope": "approved_whitelabel"}
        return {"mode": "none", "status": "locked", "brand": dict(_PLATFORM_BRAND),
                "source": "platform_default",
                "display_scope": "platform"}

    resolved_uid = shared_by or owner_user_id
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if (not resolved_uid or resolved_uid == 0) and quote_id:
            cursor.execute(
                "SELECT b.owner_user_id FROM quotes q JOIN brands b ON q.brand_id = b.id WHERE q.id = %s",
                (quote_id,),
            )
            row = _row_to_dict(cursor.fetchone())
            if row and row.get("owner_user_id"):
                resolved_uid = int(row["owner_user_id"])
        if not resolved_uid and brand_id:
            resolved_uid = _resolve_owner_from_brand_id(int(brand_id))
        if not resolved_uid:
            return {"mode": "none", "status": "locked", "brand": dict(_PLATFORM_BRAND),
                    "source": "platform_default", "display_scope": "platform"}

        cursor.execute(
            """
            SELECT company_name, product_name, logo_url, favicon_url, slogan, brand_color,
                   contact_name, contact_phone, contact_wechat, contact_email,
                   whitelabel_mode, whitelabel_status, unlocked_by_admin, hide_platform_branding,
                   backoffice_brand_unlocked
            FROM whitelabel_settings WHERE user_id = %s
            """,
            (resolved_uid,),
        )
        row = _row_to_dict(cursor.fetchone()) or {}
        mode = row.get("whitelabel_mode") or "none"
        status = row.get("whitelabel_status") or "locked"
        if surface == "agent":
            # D2（Owner 2026-07-22）：后台换肤只看 backoffice 独立授权 + 资料完整，与 mode 解耦。
            show_brand = _agent_surface_brand_ready(row)
        else:
            # 客户侧品牌自助生效（D1：自设免审批 · 行为不变）；OEM 客户侧仍需解锁。
            effective = _is_brand_effective(
                mode, status, row.get("unlocked_by_admin"), row.get("company_name"), row.get("logo_url")
            )
            show_brand = effective and _can_show_brand(surface, mode)
        if not show_brand:
            return {"mode": mode, "status": status, "brand": dict(_PLATFORM_BRAND),
                    "source": "platform_default",
                    "display_scope": "platform"}
        return {"mode": mode, "status": status, "brand": _brand_from_row(row, surface=surface),
                "source": "whitelabel_settings",
                "display_scope": "approved_whitelabel"}
    except Exception as exc:
        logger.warning("[branding] resolve_branding_context failed: %s", exc)
        return {"mode": "none", "status": "locked", "brand": dict(_PLATFORM_BRAND),
                "source": "platform_default", "display_scope": "platform"}
    finally:
        try:
            conn.close()
        except Exception:
            pass
