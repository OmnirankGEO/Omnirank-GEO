"""
intake_diff — 客户表单字段映射 + diff + 审核合并

CTO-E 2026-04-26 · 红线:
  - FIELD_MAP 是唯一权威 · 不在 MAP 的客户输入只进 notes 不写正式 profile
  - service_scope 必须 local/national/hybrid (LLM/客户随便写要兜底)
  - JSON list 字段交给 db.profile_db.update_profile 自动处理
  - brand 表只允许补 name / industry / cities / company_name (其他不动)
  - approve 后必须重新算 completeness 并返给前端

数据流:
  client form payload (key: form_key) →
    diff vs current brand+profile →
    agent picks approved_fields →
    merge_to_profile() → 写 client_profiles + brands → recompute completeness
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("GEO-Intake-Diff")

# ==================== FIELD_MAP ====================
# form_key -> {table, column, group, label, type}
#   table: brands | client_profiles
#   group: identity | business | marketing | market_insight (对齐 brand_completeness 5 组)
#   type:  str | json_list | enum_scope
FIELD_MAP: Dict[str, Dict[str, Any]] = {
    # ---- A 基础身份 (brands) ----
    "brand_name": {
        "table": "brands", "column": "name", "group": "identity",
        "label": "品牌/门店名称", "type": "str",
    },
    "company_name": {
        "table": "brands", "column": "company_name", "group": "identity",
        "label": "公司名称", "type": "str",
    },
    "industry": {
        "table": "brands", "column": "industry", "group": "identity",
        "label": "行业", "type": "str",
    },
    # ---- B 核心业务 ----
    "cities": {
        "table": "brands", "column": "cities", "group": "business",
        "label": "所在城市/服务区域", "type": "str",
    },
    "business": {
        "table": "client_profiles", "column": "business", "group": "business",
        "label": "主营服务", "type": "str",
    },
    "target_users": {
        "table": "client_profiles", "column": "target_users", "group": "business",
        "label": "目标客户", "type": "str",
    },
    "pain_points": {
        "table": "client_profiles", "column": "pain_points", "group": "business",
        "label": "客户最常问的问题", "type": "json_list",
    },
    "competitors": {
        "table": "client_profiles", "column": "competitors", "group": "business",
        "label": "主要竞品", "type": "json_list",
    },
    "products": {
        "table": "client_profiles", "column": "products", "group": "business",
        "label": "主要产品/服务包", "type": "json_list",
    },
    # ---- C 深度营销 ----
    "company_intro": {
        "table": "client_profiles", "column": "company_intro", "group": "marketing",
        "label": "公司简介", "type": "str",
    },
    "core_value": {
        "table": "client_profiles", "column": "core_value", "group": "marketing",
        "label": "为什么选你", "type": "str",
    },
    "selling_points": {
        "table": "client_profiles", "column": "selling_points", "group": "marketing",
        "label": "核心优势", "type": "str",
    },
    "success_cases": {
        "table": "client_profiles", "column": "success_cases", "group": "marketing",
        "label": "典型案例", "type": "str",
    },
    "testimonials": {
        "table": "client_profiles", "column": "testimonials", "group": "marketing",
        "label": "资质/客户证言", "type": "str",
    },
    # ---- E 市场洞察 ----
    "service_scope": {
        "table": "client_profiles", "column": "service_scope", "group": "market_insight",
        "label": "服务范围", "type": "enum_scope",
    },
    "local_competitors": {
        "table": "client_profiles", "column": "local_competitors", "group": "market_insight",
        "label": "本地竞品", "type": "json_list",
    },
}

# 允许进 notes 但不写正式 profile 的辅助字段
# (官网链接/客单价/服务流程/售后承诺等 — 没有干净的 canonical column)
NOTES_KEYS: Set[str] = {
    "main_link",            # 官网/大众点评/抖音/小红书/公众号/小程序任一链接
    "qualifications_text",  # 资质/授权/荣誉/证书纯文本(代理可视情况复制到 testimonials)
    "price_range",          # 客单价/预算范围
    "service_flow",         # 服务流程
    "after_sale",           # 售后承诺
    "constraints",          # 禁止宣传或不能承诺的内容
    "highlight_business",   # 希望重点推广的业务
    "extra_notes",          # 客户自由备注
}

ALL_KNOWN_KEYS: Set[str] = set(FIELD_MAP.keys()) | NOTES_KEYS

ENUM_SCOPE_VALID = {"local", "national", "hybrid"}


# ==================== 工具 ====================

def _coerce_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v)


def _coerce_list(v: Any) -> Optional[List[Any]]:
    if v is None:
        return None
    if isinstance(v, list):
        cleaned = [x for x in v if x not in (None, "", " ")]
        return cleaned or None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                cleaned = [x for x in parsed if x not in (None, "", " ")]
                return cleaned or None
        except Exception:
            pass
        items = [
            x.strip()
            for x in s.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
            if x.strip()
        ]
        return items or None
    return None


def _coerce_scope(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if s in ENUM_SCOPE_VALID else None


def normalize_payload(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """切分 payload:
      - mapped: 进 FIELD_MAP 的字段 (已类型校验)
      - notes:  进 NOTES_KEYS 的字段 + 任何不识别的额外 key (统一进 notes.extra)
      - rejected: 类型/枚举不合法被踢出去的字段名 list (前端可提示)
    """
    mapped: Dict[str, Any] = {}
    notes: Dict[str, Any] = {}
    rejected: List[str] = []

    # P0-7: 社媒补充字段单独走 client_profiles.social_fields JSONB
    # 在这里识别后跳过 · 不让它们进 notes._unknown 污染主流程
    try:
        from db.intake_db import SOCIAL_FIELD_KEYS as _SOCIAL_KEYS
    except Exception:
        _SOCIAL_KEYS = ()

    for key, val in (raw or {}).items():
        if key in _SOCIAL_KEYS:
            continue
        if key in FIELD_MAP:
            spec = FIELD_MAP[key]
            kind = spec["type"]
            if kind == "str":
                norm = _coerce_str(val)
            elif kind == "json_list":
                norm = _coerce_list(val)
            elif kind == "enum_scope":
                norm = _coerce_scope(val)
            else:
                norm = None
            if norm is None or norm == "" or norm == []:
                # 留空表示客户没填 -> 不进 mapped, 不算 reject
                continue
            mapped[key] = norm
        elif key in NOTES_KEYS:
            sval = _coerce_str(val)
            if sval:
                notes[key] = sval
        else:
            # 未知 key 收进 notes.unknown 不进 profile
            sval = _coerce_str(val)
            if sval:
                notes.setdefault("_unknown", {})[key] = sval

    return mapped, notes, rejected


# ==================== diff ====================

def _value_filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, list):
        return len(v) > 0
    return True


def build_diff(
    customer_payload_normalized: Dict[str, Any],
    ai_suggested: Optional[Dict[str, Any]],
    current_brand: Dict[str, Any],
    current_profile: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """计算 3 路对比, 给代理审核 UI 用.

    返:
      {
        fields: [
          {
            form_key, label, table, column, group,
            current_value, customer_value, ai_value, recommended,
            is_change, would_overwrite,
          },
        ],
        unmapped_notes: {...}  # 客户填了但没 canonical 字段的辅助信息
      }
    """
    profile = current_profile or {}
    brand = current_brand or {}

    fields: List[Dict[str, Any]] = []
    for key, spec in FIELD_MAP.items():
        table = spec["table"]
        col = spec["column"]
        current = brand.get(col) if table == "brands" else profile.get(col)

        customer_v = customer_payload_normalized.get(key)
        ai_v = (ai_suggested or {}).get(key) if isinstance(ai_suggested, dict) else None

        # 客户值优先, 没填 fallback ai 建议
        if customer_v is not None:
            recommended = customer_v
            recommended_source = "customer"
        elif ai_v is not None:
            recommended = ai_v
            recommended_source = "ai"
        else:
            recommended = None
            recommended_source = None

        is_change = _value_filled(recommended) and recommended != current
        would_overwrite = _value_filled(current) and is_change

        fields.append({
            "form_key": key,
            "label": spec["label"],
            "table": table,
            "column": col,
            "group": spec["group"],
            "type": spec["type"],
            "current_value": current,
            "customer_value": customer_v,
            "ai_value": ai_v,
            "recommended": recommended,
            "recommended_source": recommended_source,
            "is_change": is_change,
            "would_overwrite": would_overwrite,
        })

    return {"fields": fields}


# ==================== merge ====================

def merge_approved_fields(
    submission: Dict[str, Any],
    brand_id: int,
    approved_form_keys: List[str],
) -> Dict[str, Any]:
    """把 approved_form_keys 落进 brands / client_profiles.

    红线:
      - 不绕过 update_profile 白名单
      - brand 表只允许 name/company_name/industry/cities (FIELD_MAP 限定)
      - 落 brand 的字段调原生 SQL (没有 update_brand helper · 复用 ai-fill 的直 UPDATE 模式)
      - 缺 client_profiles 行时自动 create_profile
      - 合并完重新 compute_brand_completeness 返新 score

    返:
      {
        applied: [form_key, ...],
        skipped: [{form_key, reason}, ...],
        completeness_before: int,
        completeness_after: int,
        profile_id, brand_id,
      }
    """
    from db.connection import get_connection
    from db.profile_db import find_profile_by_name, get_profile, create_profile, update_profile
    from utils.brand_completeness import compute_brand_completeness

    payload = submission.get("payload_jsonb") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    ai_suggested = submission.get("ai_suggested_jsonb") or {}
    if isinstance(ai_suggested, str):
        try:
            ai_suggested = json.loads(ai_suggested)
        except Exception:
            ai_suggested = {}

    customer_normalized, _notes, _rejected = normalize_payload(payload)

    # 当前 brand
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        brand_row = cur.fetchone()
    finally:
        conn.close()

    if not brand_row:
        return {
            "applied": [],
            "skipped": [{"form_key": k, "reason": "brand_not_found"} for k in approved_form_keys],
            "completeness_before": 0,
            "completeness_after": 0,
            "profile_id": None,
            "brand_id": brand_id,
        }

    brand_dict = dict(brand_row)
    brand_name = brand_dict.get("name") or "未命名"

    # 当前 profile：优先按 brand_id 找，而不是按 name 找。
    # 代运营客户常见“品牌名/公司名/档案名”不完全一致，按 name 找会误创建第二个 profile。
    profile = None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM client_profiles
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (brand_id,),
        )
        row = cur.fetchone()
        if row:
            profile = get_profile(row["id"])
    finally:
        conn.close()
    if not profile:
        profile = find_profile_by_name(brand_name, brand_id=brand_id)
    if not profile:
        try:
            new_id = create_profile(
                name=brand_name,
                brand_id=brand_id,
                industry=brand_dict.get("industry"),
            )
            profile = get_profile(new_id) or {"id": new_id}
        except Exception as e:
            return {
                "applied": [],
                "skipped": [{"form_key": k, "reason": f"create_profile_failed:{e}"}
                            for k in approved_form_keys],
                "completeness_before": 0,
                "completeness_after": 0,
                "profile_id": None,
                "brand_id": brand_id,
            }
    profile_id = profile.get("id")

    completeness_before = compute_brand_completeness(brand_dict, profile).get("score", 0)

    # 分两路: brand 字段直 UPDATE, profile 字段进 update_profile kwargs
    profile_updates: Dict[str, Any] = {}
    brand_updates: Dict[str, Any] = {}
    applied: List[str] = []
    skipped: List[Dict[str, str]] = []

    for key in approved_form_keys:
        if key not in FIELD_MAP:
            skipped.append({"form_key": key, "reason": "unknown_form_key"})
            continue
        spec = FIELD_MAP[key]

        # 取 customer 优先, 没有就 ai_suggested 已规范化的版本
        cv = customer_normalized.get(key)
        if cv is None:
            ai_v = ai_suggested.get(key) if isinstance(ai_suggested, dict) else None
            kind = spec["type"]
            if kind == "str":
                cv = _coerce_str(ai_v)
            elif kind == "json_list":
                cv = _coerce_list(ai_v)
            elif kind == "enum_scope":
                cv = _coerce_scope(ai_v)

        if cv in (None, "", []):
            skipped.append({"form_key": key, "reason": "empty_value"})
            continue

        if spec["table"] == "brands":
            brand_updates[spec["column"]] = cv
            applied.append(key)
        else:
            profile_updates[spec["column"]] = cv
            applied.append(key)

    # ---- 落 brand 表 ----
    if brand_updates:
        sets = ", ".join([f"{c} = %s" for c in brand_updates])
        values = list(brand_updates.values()) + [brand_id]
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE brands SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 落 client_profiles ----
    if profile_updates:
        try:
            update_profile(profile_id, **profile_updates)
        except Exception as e:
            logger.warning(f"[Intake] update_profile failed: {e}")
            for k in list(profile_updates.keys()):
                form_key = next(
                    (fk for fk, spec in FIELD_MAP.items()
                     if spec["table"] == "client_profiles" and spec["column"] == k),
                    k,
                )
                if form_key in applied:
                    applied.remove(form_key)
                skipped.append({"form_key": form_key, "reason": f"update_profile_failed:{e}"})

    # ---- P0-7: 社媒补充字段 (8 个) 回流 client_profiles.social_fields ----
    # 不在 FIELD_MAP · 不需要 approve 勾选 · 客户填了就保留 (代理后期可手动清理)
    social_fields_payload: Dict[str, Any] = {}
    try:
        from db.intake_db import SOCIAL_FIELD_KEYS, update_profile_social_fields
        for sk in SOCIAL_FIELD_KEYS:
            v = payload.get(sk) if isinstance(payload, dict) else None
            if v not in (None, "", []):
                social_fields_payload[sk] = v
        if social_fields_payload and profile_id:
            update_profile_social_fields(profile_id, social_fields_payload)
    except Exception as e:
        logger.warning(f"[Intake] social_fields merge failed: {e}")

    # ---- 重新算 completeness ----
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        new_brand = dict(cur.fetchone() or {})
    finally:
        conn.close()
    new_profile = get_profile(profile_id) or profile
    completeness_after = compute_brand_completeness(new_brand, new_profile).get("score", 0)

    return {
        "applied": applied,
        "skipped": skipped,
        "completeness_before": completeness_before,
        "completeness_after": completeness_after,
        "profile_id": profile_id,
        "brand_id": brand_id,
        "social_fields_saved": list(social_fields_payload.keys()),
    }
