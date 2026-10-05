"""
营销资料确认 API
- 销售端：生成确认链接、查看确认状态
- 客户端：查看营销资料、提交确认（公开访问，通过 token）
"""

import json
import secrets
import logging
import copy
from datetime import datetime, timedelta
from typing import Optional, List, Any

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db.diagnosis_db import get_connection
from services.notification_events import NotificationEventType
from services.notification_outbox import enqueue_brand_owner_notification_event

logger = logging.getLogger("GEO-MarketingConfirm")

router = APIRouter(prefix="/api", tags=["营销资料确认"])


# ========== 数据模型 ==========

class GenerateLinkRequest(BaseModel):
    brand_id: int


class ConfirmMaterialsRequest(BaseModel):
    customer_notes: str = Field(default="", max_length=2000)


class FeedbackRequest(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=5000)


class MaterialPatchEdit(BaseModel):
    path: str = Field(..., min_length=1, max_length=160)
    value: Any = ""


class UpdateMaterialsRequest(BaseModel):
    edits: List[MaterialPatchEdit] = Field(default_factory=list, max_length=80)
    note: str = Field(default="", max_length=5000)
    action: str = Field(default="feedback")


# ========== 内部工具函数 ==========

def _get_brand_info(brand_id: int) -> dict:
    """获取品牌基础信息"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, industry, company_name, cities FROM brands WHERE id = %s", (brand_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "品牌不存在")
        return dict(row)
    finally:
        try:
            conn.close()
        except Exception: pass


def _get_profile_for_brand(brand_id: int) -> Optional[dict]:
    """获取品牌关联的 client_profiles 营销资料"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM client_profiles
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY updated_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        profile = dict(row)
        # Parse JSON fields
        for field in ['products', 'pain_points', 'competitors', 'persona_catchphrases',
                      'persona_golden_quotes', 'brand_display_names', 'target_platforms',
                      'content_pillars', 'success_cases', 'testimonials']:
            if profile.get(field) and isinstance(profile[field], str):
                try:
                    profile[field] = json.loads(profile[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        if profile.get('structured_knowledge') and isinstance(profile['structured_knowledge'], str):
            try:
                profile['structured_knowledge'] = json.loads(profile['structured_knowledge'])
            except (json.JSONDecodeError, TypeError):
                pass
        return profile
    finally:
        try:
            conn.close()
        except Exception: pass


def _get_materials_for_brand(brand_id: int) -> Optional[dict]:
    """获取品牌关联的 client_materials 补充资料"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM client_materials
            WHERE brand_id = %s
            ORDER BY updated_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        materials = dict(row)
        for field in ['core_selling_points', 'case_studies', 'pricing_tiers', 'testimonials', 'credentials']:
            if materials.get(field) and isinstance(materials[field], str):
                try:
                    materials[field] = json.loads(materials[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return materials
    finally:
        try:
            conn.close()
        except Exception: pass


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _build_contact_snapshot(profile: dict) -> dict:
    """Build customer-visible contact facts, when they exist."""
    prof = profile or {}
    contact = {
        "phone": _clean_text(prof.get("contact_phone") or prof.get("phone") or prof.get("mobile")),
        "wechat": _clean_text(prof.get("contact_wechat") or prof.get("wechat")),
        "website": _clean_text(prof.get("contact_website") or prof.get("website")),
        "address": _clean_text(prof.get("contact_address") or prof.get("address")),
    }
    return {k: v for k, v in contact.items() if v}


def _normalize_public_asset_url(value: Any) -> str:
    """Root-absolutize bare 'uploads/...' paths so customer-visible images resolve
    from site root, not page-relative under /m/<token> (fixes broken thumbnails)."""
    text = _clean_text(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:", "blob:")):
        return text
    if text.startswith("//"):
        return "https:" + text
    text = text.replace("\\", "/").lstrip("/")
    if text.startswith("uploads/"):
        return "/" + text
    if text.startswith("article-images/"):
        return "/uploads/" + text
    if text.startswith("static/") or text.startswith("assets/"):
        return "/" + text
    return "/" + text


def _json_or_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    if text[:1] not in ("[", "{"):
        return value
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return value


def _items_from_any(value: Any) -> list:
    value = _json_or_value(value)
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        lines = [p.strip() for p in text.replace("\r", "\n").split("\n") if p.strip()]
        if not lines:
            lines = [text]
        return lines
    return []


def _first_text(item: dict, keys: list[str]) -> str:
    for key in keys:
        value = _clean_text(item.get(key))
        if value:
            return value
    return ""


def _normalize_testimonial_items(*sources: Any) -> list[dict]:
    """Normalize testimonials from profile/material JSON or text.

    Profile testimonials are passed first by callers, so real customer quotes win over
    older generic material summaries.
    """
    result = []
    seen = set()
    for source in sources:
        for raw_item in _items_from_any(source):
            if isinstance(raw_item, str):
                item = {"quote": raw_item}
            elif isinstance(raw_item, dict):
                item = raw_item
            else:
                continue
            quote = _first_text(item, ["quote", "评价原话", "content", "text", "comment", "评价"])
            if not quote:
                continue
            dedupe_key = quote.strip()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized = {
                "name": _first_text(item, ["name", "评价人称呼", "customer", "client", "称呼"]),
                "title": _first_text(item, ["title", "评价人职位", "position", "role", "职位"]),
                "company": _first_text(item, ["company", "评价人所在行业", "industry", "所在行业"]),
                "quote": quote,
            }
            result.append({k: v for k, v in normalized.items() if v})
    return result[:12]


def _normalize_case_items(*sources: Any) -> list[dict]:
    result = []
    seen = set()
    for source in sources:
        for raw_item in _items_from_any(source):
            if isinstance(raw_item, str):
                item = {"results": raw_item}
            elif isinstance(raw_item, dict):
                item = raw_item
            else:
                continue
            normalized = {
                "client": _first_text(item, ["client", "客户", "客户行业", "customer", "industry"]),
                "background": _first_text(item, ["background", "合作前的问题", "合作前的问", "problem", "痛点"]),
                "solution": _first_text(item, ["solution", "我们做了什么", "方案", "approach"]),
                "results": _first_text(item, ["results", "合作后的效果", "效果", "result", "outcome"]),
                "quote": _first_text(item, ["quote", "评价原话", "客户评价", "comment"]),
            }
            if not any(normalized.values()):
                continue
            dedupe_key = "|".join(normalized.values())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append({k: v for k, v in normalized.items() if v})
    return result[:12]


def _public_image_snapshot(asset: dict) -> Optional[dict]:
    """Convert an image asset row to a customer-visible confirmation item."""
    if not isinstance(asset, dict):
        return None
    url = _normalize_public_asset_url(asset.get("url") or asset.get("public_url") or asset.get("safe_size_key"))
    thumb = _normalize_public_asset_url(asset.get("thumbnail_url") or asset.get("thumbnail_key") or asset.get("thumb_key") or url)
    if not url and not thumb:
        return None

    image_id = asset.get("id")
    try:
        image_id = int(image_id) if image_id is not None else None
    except (TypeError, ValueError):
        image_id = None

    item = {
        "id": image_id,
        "url": url or thumb,
        "thumbnail_url": thumb or url,
        "title": _clean_text(asset.get("title") or asset.get("caption") or asset.get("alt_text")),
        "caption": _clean_text(asset.get("caption") or asset.get("vision_summary")),
        "image_type": _clean_text(asset.get("image_type") or asset.get("suggested_placement")),
        "rights_confirmed": bool(asset.get("rights_confirmed")),
    }
    return {k: v for k, v in item.items() if v not in ("", None)}


def _get_confirmable_images(brand_id: int) -> list:
    """List active brand images that should be visible in customer confirmation."""
    if not brand_id:
        return []
    try:
        from db.brand_image_assets_db import list_image_assets
        assets = list_image_assets(int(brand_id), include_archived=False)
    except Exception as err:
        logger.debug(f"[marketing-confirm] 图片素材读取失败: brand_id={brand_id}, err={err}")
        return []

    result = []
    for asset in assets or []:
        if str(asset.get("status") or "active").lower() != "active":
            continue
        # publish_allowed is the content safety gate. rights_confirmed may still be false before customer confirmation.
        if asset.get("publish_allowed") in (False, 0, "0", "false", "False"):
            continue
        item = _public_image_snapshot(asset)
        if item:
            result.append(item)
    return result[:24]


def _build_materials_snapshot(brand_info: dict, profile: dict, materials: dict) -> dict:
    """合并品牌信息、profile、materials 为客户端展示用的快照

    [CTO-15.23 2026-05-08 P0-B] 5 维 dimension 加 client_materials fallback:
    · 老板报客户已粘贴公司简介长文 + 点 AI 整理 · 但 4 项仍标"待补"(usp/products/customers/cases)
    · 真因:LLM 旧 prompt 不输出 structured_knowledge · sk 永远空 · snapshot 5 维全空
    · A 主修(m3_material_confirm_api.py)让 LLM 新输出 sk · 但老 client_materials 数据没 sk
    · B 兜底:snapshot 构造时 · 5 维空时从 client_materials/profile 衍生(让历史数据立刻显示)
    · 优先级:sk 非空 > materials 衍生 > profile 文本衍生(不丢用户已确认数据)
    """
    sk = profile.get('structured_knowledge') or {} if profile else {}
    # structured_knowledge 是 TEXT 列 · 可能是 JSON 字符串或已解析的 dict
    if isinstance(sk, str):
        try:
            import json as _sk_json
            sk = _sk_json.loads(sk) if sk.strip() else {}
        except Exception:
            sk = {}
    mat = materials or {}
    prof = profile or {}

    def _list_from_text(value, key='point') -> list:
        if not value:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        text = str(value).strip()
        if not text:
            return []
        parts = [p.strip() for p in text.replace('；', ';').replace('，', ',').replace('、', ',').split(';') if p.strip()]
        if len(parts) <= 1:
            parts = [p.strip() for p in text.replace('\r', '\n').replace(',', '\n').split('\n') if p.strip()]
        return [{key: p} for p in parts] if parts else []

    # 核心卖点：优先 structured_knowledge.differentiation，fallback profile.selling_points, fallback materials
    selling_points_text = prof.get('selling_points', '')
    selling_points_list = mat.get('core_selling_points') or _list_from_text(selling_points_text, 'point')
    diff = sk.get('differentiation') or {}
    products = sk.get('products') or {}
    pain_points = sk.get('painPoints') or {}
    customers = sk.get('customers') or {}
    cases = sk.get('cases') or prof.get('success_cases') or []

    # B 兜底 · sk.differentiation.usp 空 → 用 mat.unique_value 衍生(USP ≈ unique_value 简版)
    diff_usp = diff.get('usp') or ''
    if not diff_usp and mat.get('unique_value'):
        diff_usp = str(mat.get('unique_value') or '')[:160]

    # B 兜底 · sk.products 全空 → 从 mat.core_selling_points 衍生 features(让 has_products 不假报)
    products_features = products.get('features') or []
    products_scenarios = products.get('scenarios') or []
    products_name = products.get('name', '')
    if not (products_features or products_scenarios or products_name):
        sp_list = mat.get('core_selling_points') or []
        if isinstance(sp_list, list):
            features_fallback = []
            for sp_item in sp_list:
                if isinstance(sp_item, dict):
                    p = sp_item.get('point') or sp_item.get('name') or ''
                    if p:
                        features_fallback.append(str(p))
                elif isinstance(sp_item, str):
                    features_fallback.append(sp_item)
            if features_fallback:
                products_features = features_fallback[:5]

    # B 兜底 · sk.customers 全空 → 从 prof.target_users 衍生 segments
    customer_segments = customers.get('segments') or []
    customer_needs = customers.get('needs') or []
    customer_pain = pain_points.get('scenario', '')
    if not (customer_segments or customer_pain) and prof.get('target_users'):
        tu_text = str(prof.get('target_users') or '').strip()
        if tu_text:
            customer_segments = [tu_text[:160]]

    # B 兜底 · cases 已 fallback prof.success_cases · 再补 mat.case_studies(LLM 整理后的标准结构)
    if not cases and mat.get('case_studies'):
        cs = mat.get('case_studies') or []
        if isinstance(cs, list):
            cases = cs
        elif isinstance(cs, str):
            try:
                import json as _cs_json
                cases = _cs_json.loads(cs) if cs.strip() else []
            except Exception:
                cases = []

    normalized_cases = _normalize_case_items(cases, mat.get('case_studies'), prof.get('success_cases'))
    normalized_testimonials = _normalize_testimonial_items(prof.get('testimonials'), mat.get('testimonials'))

    return {
        "company": {
            "name": brand_info.get('company_name') or brand_info.get('name', ''),
            "brand_name": brand_info.get('name', ''),
            "industry": prof.get('industry') or brand_info.get('industry', ''),
            "intro": prof.get('company_intro') or mat.get('company_intro', ''),
            "core_value": prof.get('core_value') or mat.get('unique_value', ''),
            "target_users": prof.get('target_users', ''),
            "service_area": mat.get('service_area', '') or (brand_info.get('cities') or ''),
        },
        "selling_points": {
            "summary": selling_points_text,
            "items": selling_points_list,
            "usp": diff_usp,
            "advantages": diff.get('advantages') or [],
            "killer_data": diff.get('killerData') or [],
        },
        "products": {
            "name": products_name,
            "features": products_features,
            "scenarios": products_scenarios,
            "metrics": products.get('metrics', ''),
        },
        "customers": {
            "segments": customer_segments,
            "needs": customer_needs,
            "barriers": customers.get('barriers') or [],
            "concerns": customers.get('concerns') or [],
            "pain_scenario": customer_pain,
            "pain_triggers": pain_points.get('triggers', ''),
        },
        "competitors": {
            "names": prof.get('competitors') or diff.get('competitors') or [],
        },
        "cases": normalized_cases or (cases if isinstance(cases, list) else _list_from_text(cases, 'result')),
        "testimonials": normalized_testimonials,
        "credentials": mat.get('credentials') or [],
        "methodology": mat.get('methodology', ''),
        "contact": _build_contact_snapshot(prof),
        "images": [
            item for item in (_public_image_snapshot(asset) for asset in _get_confirmable_images(brand_info.get('id')))
            if item
        ],
    }


def _load_materials_snapshot(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


_PATCH_SCALAR_PATHS = {
    "company.name",
    "company.brand_name",
    "company.industry",
    "company.intro",
    "company.core_value",
    "company.target_users",
    "company.service_area",
    "selling_points.summary",
    "selling_points.usp",
    "products.name",
    "products.metrics",
    "customers.pain_scenario",
    "customers.pain_triggers",
    "methodology",
    "contact.phone",
    "contact.wechat",
    "contact.website",
    "contact.address",
}

_PATCH_LIST_FIELDS = {
    "selling_points.items": {"point", "evidence", "name"},
    "cases": {"client", "background", "solution", "results", "quote"},
    "testimonials": {"name", "title", "company", "quote"},
    "credentials": {"type", "name", "year"},
    "images": {"title", "caption", "alt_text", "image_type"},
}

_PATCH_STRING_LISTS = {
    "selling_points.advantages",
    "selling_points.killer_data",
    "products.features",
    "products.scenarios",
    "customers.segments",
    "customers.needs",
    "customers.barriers",
    "customers.concerns",
    "competitors.names",
}


def _patch_value_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        raise HTTPException(400, "修改内容格式不正确")
    text = _clean_text(value)
    if len(text) > 3000:
        raise HTTPException(400, "单项修改内容过长")
    return text


def _reject_unsafe_patch_path(path: str) -> list[str]:
    text = _clean_text(path)
    if not text or "__" in text or "[" in text or "]" in text:
        raise HTTPException(400, "不允许修改该字段")
    parts = text.split(".")
    if any(not part or part.startswith("_") for part in parts):
        raise HTTPException(400, "不允许修改该字段")
    return parts


def _set_nested_scalar(target: dict, path: str, value: str) -> tuple[Any, Any]:
    parts = _reject_unsafe_patch_path(path)
    if path not in _PATCH_SCALAR_PATHS:
        raise HTTPException(400, "不允许修改该字段")
    node: Any = target
    for part in parts[:-1]:
        if not isinstance(node, dict):
            raise HTTPException(400, "资料结构异常")
        node = node.setdefault(part, {})
    old = node.get(parts[-1], "") if isinstance(node, dict) else ""
    node[parts[-1]] = value
    return old, value


def _set_list_patch_value(target: dict, parts: list[str], value: str) -> tuple[Any, Any]:
    if len(parts) < 2:
        raise HTTPException(400, "不允许修改该字段")

    string_list_path = ".".join(parts[:-1])
    if string_list_path in _PATCH_STRING_LISTS:
        try:
            string_index = int(parts[-1])
        except (TypeError, ValueError):
            raise HTTPException(400, "不允许修改该字段")
        if string_index < 0:
            raise HTTPException(400, "不允许修改该字段")
        node: Any = target
        for part in parts[:-1]:
            if not isinstance(node, dict):
                raise HTTPException(400, "资料结构异常")
            node = node.get(part)
        if not isinstance(node, list) or string_index >= len(node):
            raise HTTPException(400, "不允许修改该字段")
        old = node[string_index]
        node[string_index] = value
        return old, value

    try:
        index = int(parts[-2])
    except (TypeError, ValueError):
        raise HTTPException(400, "不允许修改该字段")
    if index < 0:
        raise HTTPException(400, "不允许修改该字段")

    list_path = ".".join(parts[:-2])
    field = parts[-1]
    if list_path in _PATCH_LIST_FIELDS:
        if field not in _PATCH_LIST_FIELDS[list_path]:
            raise HTTPException(400, "不允许修改该字段")
        node: Any = target
        for part in parts[:-2]:
            if not isinstance(node, dict):
                raise HTTPException(400, "资料结构异常")
            node = node.get(part)
        if not isinstance(node, list) or index >= len(node):
            raise HTTPException(400, "不允许修改该字段")
        if not isinstance(node[index], dict):
            node[index] = {"value": _clean_text(node[index])}
        old = node[index].get(field, "")
        node[index][field] = value
        return old, value

    raise HTTPException(400, "不允许修改该字段")


def _material_patch_dict(edit: Any) -> dict:
    if isinstance(edit, MaterialPatchEdit):
        return edit.model_dump()
    if hasattr(edit, "dict"):
        return edit.dict()
    return dict(edit or {})


def _apply_material_patch(snapshot: dict, edits: list[Any]) -> tuple[dict, list[dict]]:
    """Apply customer-approved inline edits to a public materials snapshot.

    Only a small allowlist of customer-visible text fields is writable; asset URLs,
    hidden paths, token/session fields and out-of-range list items are rejected.
    """
    patched = copy.deepcopy(snapshot or {})
    audit = []
    for raw_edit in edits or []:
        edit = _material_patch_dict(raw_edit)
        path = _clean_text(edit.get("path"))
        value = _patch_value_text(edit.get("value", ""))
        parts = _reject_unsafe_patch_path(path)
        if path in _PATCH_SCALAR_PATHS:
            old, new = _set_nested_scalar(patched, path, value)
        else:
            old, new = _set_list_patch_value(patched, parts, value)
        if _clean_text(old) == _clean_text(new):
            continue
        audit.append({"path": path, "old": _clean_text(old), "new": new})
    return patched, audit


def _patch_history_entries(existing_payload: Any) -> list[dict]:
    if not existing_payload:
        return []
    try:
        parsed = json.loads(existing_payload) if isinstance(existing_payload, str) else existing_payload
    except (TypeError, ValueError):
        return []

    if isinstance(parsed, list):
        return [copy.deepcopy(item) for item in parsed if isinstance(item, dict)]
    if not isinstance(parsed, dict):
        return []

    history = parsed.get("history")
    if isinstance(history, list):
        return [copy.deepcopy(item) for item in history if isinstance(item, dict)]

    legacy_entry = {
        key: copy.deepcopy(parsed.get(key))
        for key in ("edits", "note", "action", "updated_at")
        if key in parsed
    }
    if legacy_entry.get("edits") or _clean_text(legacy_entry.get("note")):
        return [legacy_entry]
    return []


def _customer_patch_payload_with_history(existing_payload: Any, current_payload: dict) -> dict:
    current_entry = {
        key: copy.deepcopy(current_payload.get(key))
        for key in ("edits", "note", "action", "updated_at")
        if key in current_payload
    }
    history = _patch_history_entries(existing_payload)
    history.append(current_entry)
    payload = copy.deepcopy(current_payload)
    payload["history"] = history
    return payload


def _join_texts(items: Any) -> str:
    if isinstance(items, str):
        return items.strip()
    if not isinstance(items, list):
        return ""
    result = []
    for item in items:
        if isinstance(item, dict):
            text = _clean_text(item.get("point") or item.get("name") or item.get("quote") or item.get("results"))
            evidence = _clean_text(item.get("evidence"))
            if text and evidence:
                result.append(f"{text}：{evidence}")
            elif text:
                result.append(text)
        else:
            text = _clean_text(item)
            if text:
                result.append(text)
    return "\n".join(result)


def _non_empty_dict(values: dict) -> dict:
    result = {}
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        result[key] = value
    return result


def _snapshot_to_profile_and_material_updates(snapshot: dict) -> tuple[dict, dict]:
    company = (snapshot or {}).get("company") or {}
    selling = (snapshot or {}).get("selling_points") or {}
    products = (snapshot or {}).get("products") or {}
    customers = (snapshot or {}).get("customers") or {}
    competitors = (snapshot or {}).get("competitors") or {}
    contact = (snapshot or {}).get("contact") or {}
    cases = (snapshot or {}).get("cases") or []
    testimonials = (snapshot or {}).get("testimonials") or []
    credentials = (snapshot or {}).get("credentials") or []
    methodology = _clean_text((snapshot or {}).get("methodology"))

    selling_points = selling.get("items") or []
    product_features = products.get("features") or []
    product_scenarios = products.get("scenarios") or []
    customer_needs = customers.get("needs") or []
    customer_barriers = customers.get("barriers") or []
    customer_concerns = customers.get("concerns") or []

    profile_updates = _non_empty_dict({
        "name": company.get("name"),
        "industry": company.get("industry"),
        "company_intro": company.get("intro"),
        "core_value": company.get("core_value") or selling.get("usp"),
        "selling_points": _join_texts(selling_points) or selling.get("summary"),
        "products": product_features or ([products.get("name")] if products.get("name") else []),
        "target_users": company.get("target_users") or _join_texts(customers.get("segments") or []),
        "pain_points": customer_needs + customer_barriers + customer_concerns,
        "competitors": competitors.get("names") or [],
        "success_cases": cases,
        "testimonials": testimonials,
        "structured_knowledge": _non_empty_dict({
            "differentiation": _non_empty_dict({
                "usp": selling.get("usp") or company.get("core_value"),
                "advantages": selling.get("advantages") or [],
                "killerData": selling.get("killer_data") or [],
            }),
            "products": _non_empty_dict({
                "name": products.get("name"),
                "features": product_features,
                "scenarios": product_scenarios,
                "metrics": products.get("metrics"),
            }),
            "customers": _non_empty_dict({
                "segments": customers.get("segments") or [],
                "needs": customer_needs,
                "barriers": customer_barriers,
                "concerns": customer_concerns,
            }),
            "cases": cases,
            "credentials": credentials,
            "methodology": methodology,
        }),
        "contact_phone": contact.get("phone"),
        "contact_wechat": contact.get("wechat"),
        "contact_website": contact.get("website"),
        "contact_address": contact.get("address"),
    })

    material_updates = _non_empty_dict({
        "company_intro": company.get("intro"),
        "service_area": company.get("service_area"),
        "unique_value": company.get("core_value") or selling.get("usp"),
        "core_selling_points": selling_points,
        "methodology": methodology,
        "case_studies": cases,
        "testimonials": testimonials,
        "credentials": credentials,
    })
    return profile_updates, material_updates


def _write_materials_update_for_brand(brand_id: int, material_updates: dict) -> None:
    if not material_updates:
        return
    json_fields = {"core_selling_points", "case_studies", "testimonials", "credentials"}
    payload = {
        key: json.dumps(value, ensure_ascii=False) if key in json_fields else value
        for key, value in material_updates.items()
    }
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM client_materials WHERE brand_id = %s ORDER BY updated_at DESC LIMIT 1",
            (brand_id,),
        )
        row = cursor.fetchone()
        if row:
            material_id = row["id"] if isinstance(row, dict) else row[0]
            set_clause = ", ".join([f"{key} = %s" for key in payload.keys()])
            cursor.execute(
                f"UPDATE client_materials SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                tuple(payload.values()) + (material_id,),
            )
        else:
            columns = ["brand_id"] + list(payload.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            cursor.execute(
                f"INSERT INTO client_materials ({', '.join(columns)}) VALUES ({placeholders})",
                (brand_id,) + tuple(payload.values()),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write_image_metadata_for_brand(brand_id: int, snapshot: dict) -> None:
    """Sync customer-approved image labels back to brand_image_assets.

    Only customer-visible descriptive metadata is writable here. URLs, storage keys,
    ownership, publish flags and rights flags stay controlled by the admin image API.
    """
    images = (snapshot or {}).get("images") or []
    if not images:
        return

    allowed_fields = ("title", "caption", "alt_text", "image_type")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for image in images:
            if not isinstance(image, dict) or image.get("id") is None:
                continue
            try:
                image_id = int(image.get("id"))
            except (TypeError, ValueError):
                continue

            updates = {
                field: _clean_text(image.get(field))
                for field in allowed_fields
                if field in image
            }
            if not updates:
                continue

            set_clause = ", ".join([f"{field} = %s" for field in updates.keys()])
            cursor.execute(
                f"""
                UPDATE brand_image_assets
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND brand_id = %s AND status = 'active'
                """,
                tuple(updates.values()) + (image_id, int(brand_id)),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write_snapshot_back_to_knowledge(brand_id: int, snapshot: dict) -> bool:
    profile_updates, material_updates = _snapshot_to_profile_and_material_updates(snapshot)
    try:
        brand_info = _get_brand_info(int(brand_id))
        from db.profile_db import create_profile, list_profiles, update_profile

        profiles = list_profiles(brand_ids=[int(brand_id)])
        if profiles:
            update_profile(profiles[0]["id"], **profile_updates)
        else:
            create_profile(
                name=brand_info.get("company_name") or brand_info.get("name") or "未命名客户",
                industry=brand_info.get("industry") or profile_updates.get("industry") or "",
                brand_id=int(brand_id),
                **profile_updates,
            )
        _write_materials_update_for_brand(int(brand_id), material_updates)
        _write_image_metadata_for_brand(int(brand_id), snapshot)
        return True
    except Exception as err:
        logger.warning(f"[marketing-confirm] 客户确认资料回写失败: brand_id={brand_id}, err={err}")
        return False


def _snapshot_from_current_brand(brand_id: int) -> dict:
    brand_info = _get_brand_info(int(brand_id))
    profile = _get_profile_for_brand(int(brand_id))
    materials = _get_materials_for_brand(int(brand_id))
    return _build_materials_snapshot(brand_info, profile or {}, materials or {})


def _snapshot_for_public_session(session: dict) -> dict:
    """Use live brand material for editable links; keep confirmed snapshots immutable."""
    stored = _load_materials_snapshot(session.get("materials_snapshot"))
    if session.get("status") == "feedback" and session.get("customer_patch_json"):
        return stored
    if session.get("status") in ("pending", "feedback") and session.get("brand_id"):
        try:
            fresh = _snapshot_from_current_brand(int(session["brand_id"]))
            if fresh:
                return fresh
        except Exception as err:
            logger.warning(f"[marketing-confirm] 刷新确认快照失败: brand_id={session.get('brand_id')}, err={err}")
    return stored


def _confirmed_image_ids_from_snapshot(snapshot: dict) -> list:
    ids = []
    for item in (snapshot or {}).get("images") or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            image_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        if image_id not in ids:
            ids.append(image_id)
    return ids


def _mark_confirmed_image_rights(brand_id: int, snapshot: dict) -> None:
    image_ids = _confirmed_image_ids_from_snapshot(snapshot)
    if not image_ids:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for image_id in image_ids:
            cursor.execute(
                """
                UPDATE brand_image_assets
                SET rights_confirmed = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND brand_id = %s AND status = 'active'
                """,
                (image_id, brand_id),
            )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_table():
    """确保确认会话表存在"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marketing_confirm_sessions (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                brand_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                materials_snapshot TEXT,
                customer_notes TEXT DEFAULT '',
                confirmed_at TIMESTAMP,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                customer_patch_json TEXT DEFAULT '',
                FOREIGN KEY (brand_id) REFERENCES brands(id)
            )
        """)
        for column, column_type in [
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("customer_patch_json", "TEXT DEFAULT ''"),
        ]:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'marketing_confirm_sessions' AND column_name = %s
                """,
                (column,),
            )
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE marketing_confirm_sessions ADD COLUMN {column} {column_type}")
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 销售端 API（需登录 + 品牌归属） ==========
#
# 🔴🔴🔴 [P0 IDOR 热修 2026-08-10 · WO_P0_MARKETING_CONFIRM_IDOR]
# 本节三个端点原本**函数体零鉴权**:不校验登录、不校验 brand 归属,直接拿调用方传进来的
# brand_id 查库。生产实测:一个零模块权限、只拥有 brand 684 的账号
# `GET /api/marketing-confirm/status/662`(662=真实客户)→ 200 返回该品牌的 confirm token;
# 该 token 再喂公开门户 `/api/m/{token}` → 拿到完整客户物料快照。
# 暴露面 257 个真实活跃品牌 × 131 个活跃账号。
#
# 为什么全局安全网没兜住(两道防线同时失效,所以修法是三件不是一件):
#   ① `auth/module_mapping.py` 只登记了 `/api/marketing` 前缀 → `/api/marketing-confirm/*`
#      被它**吞掉**,降级为"仅需登录";
#   ② `auth/middleware.py::_extract_brand_id` 只认 query 参数与 `/api/client-context/`,
#      **不认 path 参数形态**,而本节 brand_id 走的正是 path(status/resend)与
#      **POST body**(generate-link,中间件明确不读 body)。
# 端点级 `require_brand_access` 是第一道闸,也是这里唯一真正拦得住的一道。
#
# `require_brand_access` 对非归属品牌抛 **404**(不是 403)—— 这是既有语义,正好满足
# "防枚举、不泄露存在性"的要求,不需要在这里另写。

def _mask_token(token: Any) -> str:
    """日志里的 confirm token 一律打码。

    [P0 IDOR 热修 2026-08-10 · 修法 2b 的**真**落点]
    这些 token 是 `/api/m/{token}` 的通行凭据(无需登录即可读完整客户物料快照)。
    原代码在 5 处 `logger.info` 里写**明文** —— 日志会落文件、进聚合器、被运维/排障
    多方看到,等于一条不需要越权就能拿到有效 token 的旁路。打码是零行为变更、零风险。

    ⚠️ 与之相对,工单 2b 提的"从 status 响应里删掉 token 字段"我**没有做**,
       理由写在交付单:同一响应里的 `url` 就是 `/m/{token}`,token 逐字在里面 ——
       只删 token 保留 url 是表演,不是纵深;两个都删会打断"复制已有确认链接"这个真实功能,
       超出 P0 热修最小面。真正降爆炸半径的是本单的三道闸 + 这里的日志打码。
    """
    text = str(token or "")
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def _require_brand_owner(request: Request, brand_id: int) -> None:
    """销售端营销确认端点的统一归属闸(登录 + brand 归属)。

    单点存在的理由:三个端点要用同一把闸,分开写三份迟早漂移;
    并且元判据锁可以断言"这三个端点都调了它"(接线锁打在接线上,不是函数上)。
    """
    from auth.brand_access import require_brand_access
    require_brand_access(request, brand_id, allow_null=False)


@router.post("/marketing-confirm/generate-link")
async def generate_confirm_link(req: GenerateLinkRequest, request: Request):
    """销售生成营销资料确认链接"""
    _require_brand_owner(request, req.brand_id)
    _ensure_table()

    brand_info = _get_brand_info(req.brand_id)
    profile = _get_profile_for_brand(req.brand_id)
    materials = _get_materials_for_brand(req.brand_id)

    if not profile and not materials:
        raise HTTPException(400, "请先填写营销资料后再生成确认链接")

    snapshot = _build_materials_snapshot(brand_info, profile, materials)

    # 失效旧链接
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE marketing_confirm_sessions
            SET status = 'expired'
            WHERE brand_id = %s AND status = 'pending'
        """, (req.brand_id,))

        # 生成新 token
        token = secrets.token_urlsafe(12)
        expires_at = datetime.now() + timedelta(days=7)

        cursor.execute("""
            INSERT INTO marketing_confirm_sessions (token, brand_id, status, materials_snapshot, expires_at)
            VALUES (%s, %s, 'pending', %s, %s)
            RETURNING id, token
        """, (token, req.brand_id, json.dumps(snapshot, ensure_ascii=False), expires_at))

        row = cursor.fetchone()
        conn.commit()
        conn.close()

        logger.info(f"生成营销资料确认链接: brand_id={req.brand_id}, token={_mask_token(token)}")

        return {
            "success": True,
            "token": token,
            "url": f"/m/{token}",
            "expires_at": expires_at.isoformat(),
        }
    finally:
        try:
            conn.close()
        except Exception: pass


@router.post("/marketing-confirm/resend/{brand_id}")
async def resend_confirm(brand_id: int, request: Request):
    """交付修改资料后，刷新快照并重新发送给客户确认"""
    # [P0 IDOR 热修] 同病同批修:本端点原本同样零鉴权,且它是**写**操作
    #   (刷新快照 + 重置 status/customer_notes/customer_patch_json),
    #   越权后果比 status 更重 —— 能把别人家客户已提交的确认反馈直接抹掉。
    _require_brand_owner(request, brand_id)
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 找到最新的 session（feedback 或 pending 状态）
        cursor.execute("""
            SELECT id, token FROM marketing_confirm_sessions
            WHERE brand_id = %s AND status IN ('feedback', 'pending')
            ORDER BY created_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            raise HTTPException(400, "没有待处理的确认会话")

        # 重新构建快照（拿最新资料）
        brand_info = _get_brand_info(brand_id)
        profile = _get_profile_for_brand(brand_id)
        materials = _get_materials_for_brand(brand_id)
        snapshot = _build_materials_snapshot(brand_info, profile, materials)

        expires_at = datetime.now() + timedelta(days=7)

        cursor.execute("""
            UPDATE marketing_confirm_sessions
            SET status = 'pending', materials_snapshot = %s, customer_notes = '',
                customer_patch_json = '', expires_at = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (json.dumps(snapshot, ensure_ascii=False), expires_at, row['id']))
        conn.commit()
        conn.close()

        logger.info(f"重新发送确认链接: brand_id={brand_id}, token={_mask_token(row['token'])}")

        return {
            "success": True,
            "token": row['token'],
            "url": f"/m/{row['token']}",
        }
    finally:
        try:
            conn.close()
        except Exception: pass


@router.get("/marketing-confirm/status/{brand_id}")
async def get_confirm_status(brand_id: int, request: Request):
    """查询品牌的营销资料确认状态"""
    # [P0 IDOR 热修] 这就是被生产实测打穿的那个端点(u114 → status/662 → 200 + 真 token)。
    _require_brand_owner(request, brand_id)
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT token, status, confirmed_at, customer_notes, expires_at, created_at
            FROM marketing_confirm_sessions
            WHERE brand_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (brand_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return {"has_session": False}

        session = dict(row)
        # 检查过期
        if session['status'] == 'pending' and session.get('expires_at'):
            try:
                exp = datetime.fromisoformat(str(session['expires_at']))
                if datetime.now() > exp:
                    session['status'] = 'expired'
            except (ValueError, TypeError):
                pass

        return {
            "has_session": True,
            "token": session['token'],
            "status": session['status'],
            "confirmed_at": session.get('confirmed_at'),
            "customer_notes": session.get('customer_notes', ''),
            "expires_at": session.get('expires_at'),
            "url": f"/m/{session['token']}",
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 客户端 API（公开，/api/m/） ==========

@router.get("/m/{token}")
async def get_material_page(token: str):
    """客户端获取营销资料页面数据"""
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM marketing_confirm_sessions WHERE token = %s
        """, (token,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            raise HTTPException(404, "链接不存在或已失效")

        session = dict(row)

        # 检查过期
        if session['status'] == 'pending' and session.get('expires_at'):
            try:
                exp = datetime.fromisoformat(str(session['expires_at']))
                if datetime.now() > exp:
                    return {"status": "expired"}
            except (ValueError, TypeError):
                pass

        snapshot = _snapshot_for_public_session(session)

        # v3.6 白标：客户公开物料确认页下发归属代理品牌（surface 写死 customer · 脱敏）
        # 契约见 EXEC_WHITELABEL_V36_FULL_2026-05-29.md §0「客户页后端下发契约」
        _wl_payload = {"whitelabel": None, "display_scope": "platform"}
        try:
            from services.public_whitelabel import get_public_whitelabel_data
            _wl_payload = get_public_whitelabel_data(
                brand_id=session.get('brand_id'),
            )
        except Exception as _wl_err:
            logger.debug(f"[m/token] whitelabel 下发跳过: {_wl_err}")

        return {
            "status": session['status'],
            "brand_name": snapshot.get('company', {}).get('brand_name', ''),
            "materials": snapshot,
            "confirmed_at": session.get('confirmed_at'),
            "customer_notes": session.get('customer_notes', ''),
            "whitelabel": _wl_payload.get("whitelabel"),
            "branding_status": _wl_payload.get("display_scope", "platform"),
            # [audit #10 返修] 去 owner_user_id:白标已内联下发 · 不再回代理 user_id(防 brand→agent 串联枚举)。
        }
    finally:
        try:
            conn.close()
        except Exception: pass


@router.post("/m/{token}/confirm")
async def confirm_materials(token: str, req: ConfirmMaterialsRequest):
    """客户确认营销资料"""
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM marketing_confirm_sessions WHERE token = %s", (token,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            raise HTTPException(404, "链接不存在")

        session = dict(row)

        if session['status'] == 'confirmed':
            conn.close()
            return {"success": True, "message": "已确认", "confirmed_at": session.get('confirmed_at')}

        if session['status'] not in ('pending', 'feedback'):
            conn.close()
            raise HTTPException(400, "链接已过期或状态异常")

        # 检查过期
        if session.get('expires_at'):
            try:
                exp = datetime.fromisoformat(str(session['expires_at']))
                if datetime.now() > exp:
                    conn.close()
                    raise HTTPException(400, "链接已过期")
            except (ValueError, TypeError):
                pass

        now = datetime.now().isoformat()
        snapshot = _snapshot_for_public_session(session)
        cursor.execute("""
            UPDATE marketing_confirm_sessions
            SET status = 'confirmed', confirmed_at = %s, customer_notes = %s,
                materials_snapshot = %s, updated_at = CURRENT_TIMESTAMP
            WHERE token = %s
        """, (now, req.customer_notes, json.dumps(snapshot, ensure_ascii=False), token))
        enqueue_brand_owner_notification_event(
            cursor,
            brand_id=int(session["brand_id"]),
            event_type=NotificationEventType.ASSET_REVIEW_CONFIRMED,
            business_id=f"marketing_materials:{int(session['id'])}",
            terminal_state="confirmed",
            facts={
                "business_no": f"MATERIAL-{int(session['id'])}",
                "status": "客户已确认营销资料",
                "occurred_at": now,
                "summary": "可以继续内容创作。",
            },
        )
        conn.commit()
        conn.close()
        knowledge_synced = _write_snapshot_back_to_knowledge(session['brand_id'], snapshot)
        try:
            _mark_confirmed_image_rights(session['brand_id'], snapshot)
        except Exception as e:
            logger.warning(f"图片素材确认标记失败: {e}")

        brand_name = snapshot.get('company', {}).get('brand_name', '')

        logger.info(f"客户确认营销资料: brand={brand_name}, token={_mask_token(token)}")

        return {"success": True, "confirmed_at": now, "knowledge_synced": knowledge_synced}
    finally:
        try:
            conn.close()
        except Exception: pass


@router.patch("/m/{token}/materials")
async def update_materials_from_customer(token: str, req: UpdateMaterialsRequest):
    """客户在确认页直接修改资料；保存修改或确认时同步回客户档案。"""
    _ensure_table()
    action = req.action if req.action in ("feedback", "confirm") else "feedback"

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM marketing_confirm_sessions WHERE token = %s", (token,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, "链接不存在")

        session = dict(row)
        if session["status"] == "confirmed":
            conn.close()
            return {"success": False, "message": "资料已确认，无法再修改"}
        if session["status"] not in ("pending", "feedback"):
            conn.close()
            raise HTTPException(400, "链接已过期或状态异常")
        if session.get("expires_at"):
            try:
                exp = datetime.fromisoformat(str(session["expires_at"]))
                if datetime.now() > exp:
                    conn.close()
                    raise HTTPException(400, "链接已过期")
            except (ValueError, TypeError):
                pass

        snapshot = _snapshot_for_public_session(session)
        patched_snapshot, audit = _apply_material_patch(snapshot, req.edits)
        if not audit and not req.note.strip():
            conn.close()
            raise HTTPException(400, "请先填写修改内容")

        now = datetime.now().isoformat()
        patch_payload = {
            "edits": audit,
            "note": req.note.strip(),
            "action": action,
            "updated_at": now,
        }
        patch_payload = _customer_patch_payload_with_history(session.get("customer_patch_json"), patch_payload)
        if action == "confirm":
            cursor.execute("""
                UPDATE marketing_confirm_sessions
                SET status = 'confirmed', confirmed_at = %s, customer_notes = %s,
                    materials_snapshot = %s, customer_patch_json = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE token = %s
            """, (
                now,
                req.note,
                json.dumps(patched_snapshot, ensure_ascii=False),
                json.dumps(patch_payload, ensure_ascii=False),
                token,
            ))
        else:
            cursor.execute("""
                UPDATE marketing_confirm_sessions
                SET status = 'feedback', customer_notes = %s,
                    materials_snapshot = %s, customer_patch_json = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE token = %s
            """, (
                req.note,
                json.dumps(patched_snapshot, ensure_ascii=False),
                json.dumps(patch_payload, ensure_ascii=False),
                token,
            ))
        event_type = (
            NotificationEventType.ASSET_REVIEW_CONFIRMED
            if action == "confirm"
            else NotificationEventType.ASSET_FEEDBACK_RECEIVED
        )
        enqueue_brand_owner_notification_event(
            cursor,
            brand_id=int(session["brand_id"]),
            event_type=event_type,
            business_id=f"marketing_materials:{int(session['id'])}",
            terminal_state="confirmed" if action == "confirm" else "feedback_received",
            facts={
                "business_no": f"MATERIAL-{int(session['id'])}",
                "status": "客户已确认并修改资料" if action == "confirm" else "收到客户修改意见",
                "occurred_at": now,
                "summary": f"客户在确认页修改了 {len(audit)} 处资料。",
            },
        )
        conn.commit()
        conn.close()

        knowledge_synced = False
        if audit:
            knowledge_synced = _write_snapshot_back_to_knowledge(session["brand_id"], patched_snapshot)
        if action == "confirm":
            try:
                _mark_confirmed_image_rights(session["brand_id"], patched_snapshot)
            except Exception as e:
                logger.warning(f"图片素材确认标记失败: {e}")

        return {
            "success": True,
            "status": "confirmed" if action == "confirm" else "feedback",
            "changed_count": len(audit),
            "materials": patched_snapshot,
            "confirmed_at": now if action == "confirm" else None,
            "knowledge_synced": knowledge_synced,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.post("/m/{token}/feedback")
async def submit_feedback(token: str, req: FeedbackRequest):
    """客户提交修改意见，资料返回给交付修改"""
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM marketing_confirm_sessions WHERE token = %s", (token,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            raise HTTPException(404, "链接不存在")

        session = dict(row)

        if session['status'] == 'confirmed':
            conn.close()
            return {"success": False, "message": "资料已确认，无法再提交修改意见"}

        if session['status'] not in ('pending', 'feedback'):
            conn.close()
            raise HTTPException(400, "链接已过期或状态异常")

        cursor.execute("""
            UPDATE marketing_confirm_sessions
            SET status = 'feedback', customer_notes = %s, updated_at = CURRENT_TIMESTAMP
            WHERE token = %s
        """, (req.feedback, token))
        now = datetime.now().isoformat()
        enqueue_brand_owner_notification_event(
            cursor,
            brand_id=int(session["brand_id"]),
            event_type=NotificationEventType.ASSET_FEEDBACK_RECEIVED,
            business_id=f"marketing_materials:{int(session['id'])}",
            terminal_state="feedback_received",
            facts={
                "business_no": f"MATERIAL-{int(session['id'])}",
                "status": "收到客户修改意见",
                "occurred_at": now,
                "summary": "请在营销资料页面复核客户反馈。",
            },
        )
        conn.commit()
        conn.close()

        snapshot = json.loads(session['materials_snapshot']) if session.get('materials_snapshot') else {}
        brand_name = snapshot.get('company', {}).get('brand_name', '')

        logger.info(f"客户提交修改意见: brand={brand_name}, token={_mask_token(token)}")

        return {"success": True}
    finally:
        try:
            conn.close()
        except Exception: pass
