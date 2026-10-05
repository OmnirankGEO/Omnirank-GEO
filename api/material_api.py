"""
社媒操盘手 v3.0 - 素材库 API
管理金句、模板、成功案例等素材
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import json
import os

from auth.brand_access import require_profile_access

router = APIRouter(prefix="/api/materials", tags=["素材库"])

# [FIND-MHZ-ORDERITEMS-2026-08-04 · S2] 这里原本有一份本地 `_get_profile_safe`,
# docstring 自己写着「宽松版,不做 brand 权限校验」—— 它只判档案存不存在,
# 任何登录用户拿任意 profile_id 都能过。它**长得像守卫但不是守卫**,
# 因此按函数名扫 IDOR 的判据会把本文件整个判成"有守卫"而漏掉。
# 同一份假货曾在 geo_assets_api.py 被删过一次(GEO-R8-CAN-003,
# tests/regression/test_fix_geo_assets_api.py 还留着"不许它回来"的断言),
# 但那次没有横扫,本文件与另两个社媒 router(运营 / 人设,已随 E3 删)三处原样留着。
# 现统一改调 canonical 的 auth.brand_access.require_profile_access
# (同签名同返回,只是多了真正的 require_brand_access)。



# 素材存储路径
MATERIALS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "materials.json")


# ========== Pydantic Models ==========

class MaterialCreate(BaseModel):
    """创建素材请求"""
    type: str  # golden_quote / template / case / hook / cta
    content: str
    title: Optional[str] = None
    tags: Optional[List[str]] = []
    profile_id: Optional[str] = None  # 关联档案
    source: Optional[str] = None  # 来源
    metrics: Optional[dict] = None  # 效果数据


class MaterialUpdate(BaseModel):
    """更新素材请求"""
    content: Optional[str] = None
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    metrics: Optional[dict] = None


# ========== 本地存储 ==========

def _ensure_storage():
    """确保存储文件存在"""
    os.makedirs(os.path.dirname(MATERIALS_PATH), exist_ok=True)
    if not os.path.exists(MATERIALS_PATH):
        with open(MATERIALS_PATH, 'w', encoding='utf-8') as f:
            json.dump({"materials": []}, f)


def _load_materials() -> List[dict]:
    """加载所有素材"""
    _ensure_storage()
    with open(MATERIALS_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get("materials", [])


def _save_materials(materials: List[dict]):
    """保存素材"""
    _ensure_storage()
    with open(MATERIALS_PATH, 'w', encoding='utf-8') as f:
        json.dump({"materials": materials}, f, ensure_ascii=False, indent=2)


# ========== API 路由 ==========

@router.get("", summary="获取素材列表")
async def api_list_materials(
    request: Request,
    type: Optional[str] = None,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None
):
    """获取素材列表，支持按类型/档案/标签筛选"""
    materials = _load_materials()

    # 过滤
    if type:
        materials = [m for m in materials if m.get("type") == type]
    if profile_id:
        # 指定了profile_id，校验权限
        require_profile_access(request, profile_id)
        materials = [m for m in materials if m.get("profile_id") == profile_id]
    else:
        # 没有指定profile_id，按用户品牌过滤
        from auth.brand_access import get_user_brand_filter
        from db.profile_db import list_profiles
        allowed_brands = get_user_brand_filter(request)
        if allowed_brands is not None:
            visible_profiles = list_profiles(brand_ids=allowed_brands)
            visible_profile_ids = {p["id"] for p in visible_profiles}
            materials = [m for m in materials if m.get("profile_id") in visible_profile_ids or not m.get("profile_id")]
    if tag:
        materials = [m for m in materials if tag in m.get("tags", [])]

    return {
        "success": True,
        "count": len(materials),
        "materials": materials
    }


@router.get("/types", summary="获取素材类型")
async def api_get_types():
    """获取所有素材类型"""
    return {
        "success": True,
        "types": [
            {"id": "golden_quote", "name": "金句", "icon": "💎"},
            {"id": "template", "name": "模板", "icon": "📄"},
            {"id": "case", "name": "成功案例", "icon": "🏆"},
            {"id": "hook", "name": "开场钩子", "icon": "🎣"},
            {"id": "cta", "name": "行动号召", "icon": "📢"},
        ]
    }


@router.get("/{material_id}", summary="获取单个素材")
async def api_get_material(material_id: str, request: Request):
    """获取素材详情。

    [FIND-MHZ-ORDERITEMS-2026-08-04 · S2] 改前本端点**连 `request` 参数都没有**,
    即挂了 `profile_id` 的素材也能被任一登录用户读走 —— 而同文件的
    PUT `:175` / DELETE `:201` 是有条件守卫的。这里先补齐到与它们同级。

    🔴 **只补齐了一半**:`profile_id` 为空的素材仍然不校验。
    根因是这个模块没有租户模型 —— 素材存在**单个全局 JSON 文件**
    (`data/materials.json`),不是按用户分库分行的。要做到"无条件校验",
    得先定「无 profile_id 的素材归谁」这个语义(可能还要补数据),
    那是产品决策,不在本包范围。见交付说明 §S2。
    """
    materials = _load_materials()
    for m in materials:
        if m.get("id") == material_id:
            if m.get("profile_id"):
                require_profile_access(request, m["profile_id"])
            return {"success": True, "material": m}
    raise HTTPException(status_code=404, detail="素材不存在")


@router.post("", summary="创建素材")
async def api_create_material(data: MaterialCreate, request: Request):
    """创建新素材"""
    if data.profile_id:
        require_profile_access(request, data.profile_id)

    materials = _load_materials()
    
    # 生成ID
    import uuid
    material_id = f"mat_{uuid.uuid4().hex[:8]}"
    
    material = {
        "id": material_id,
        "type": data.type,
        "content": data.content,
        "title": data.title or data.content[:30],
        "tags": data.tags or [],
        "profile_id": data.profile_id,
        "source": data.source,
        "metrics": data.metrics or {},
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    
    materials.append(material)
    _save_materials(materials)
    
    return {
        "success": True,
        "material_id": material_id,
        "message": "素材创建成功"
    }


@router.put("/{material_id}", summary="更新素材")
async def api_update_material(material_id: str, data: MaterialUpdate, request: Request):
    """更新素材"""
    materials = _load_materials()

    for i, m in enumerate(materials):
        if m.get("id") == material_id:
            # 校验权限：素材关联的profile_id
            if m.get("profile_id"):
                require_profile_access(request, m["profile_id"])
            if data.content is not None:
                materials[i]["content"] = data.content
            if data.title is not None:
                materials[i]["title"] = data.title
            if data.tags is not None:
                materials[i]["tags"] = data.tags
            if data.metrics is not None:
                materials[i]["metrics"] = data.metrics
            materials[i]["updated_at"] = datetime.now().isoformat()
            
            _save_materials(materials)
            return {"success": True, "message": "素材更新成功"}
    
    raise HTTPException(status_code=404, detail="素材不存在")


@router.delete("/{material_id}", summary="删除素材")
async def api_delete_material(material_id: str, request: Request):
    """删除素材"""
    materials = _load_materials()

    for i, m in enumerate(materials):
        if m.get("id") == material_id:
            # 校验权限：素材关联的profile_id
            if m.get("profile_id"):
                require_profile_access(request, m["profile_id"])
            materials.pop(i)
            _save_materials(materials)
            return {"success": True, "message": "素材删除成功"}
    
    raise HTTPException(status_code=404, detail="素材不存在")


# ========== 批量操作 ==========

@router.post("/batch", summary="批量创建素材")
async def api_batch_create(materials_data: List[MaterialCreate], request: Request):
    """批量创建素材"""
    # 校验所有带profile_id的素材权限
    checked_profiles = set()
    for item in materials_data:
        if item.profile_id and item.profile_id not in checked_profiles:
            require_profile_access(request, item.profile_id)
            checked_profiles.add(item.profile_id)

    materials = _load_materials()
    created_ids = []

    import uuid
    for data in materials_data:
        material_id = f"mat_{uuid.uuid4().hex[:8]}"
        material = {
            "id": material_id,
            "type": data.type,
            "content": data.content,
            "title": data.title or data.content[:30],
            "tags": data.tags or [],
            "profile_id": data.profile_id,
            "source": data.source,
            "metrics": data.metrics or {},
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        materials.append(material)
        created_ids.append(material_id)
    
    _save_materials(materials)
    
    return {
        "success": True,
        "created_count": len(created_ids),
        "material_ids": created_ids
    }


@router.get("/stats/summary", summary="素材统计")
async def api_get_stats():
    """获取素材库统计"""
    materials = _load_materials()
    
    type_counts = {}
    for m in materials:
        t = m.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    
    return {
        "success": True,
        "total": len(materials),
        "by_type": type_counts
    }
