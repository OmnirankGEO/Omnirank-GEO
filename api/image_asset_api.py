"""
客户图片素材 API(brand_image_assets)

三件套之①②的对外端点:
- 上传客户图片 → Pillow 存原图+缩略+安全尺寸 → admin **geo_vision_model** 结构化读图 → 落图库表
- 列表 / 改「可用于文章」开关(publish_allowed)+「确认可外发」(rights_confirmed)/ 软删
- 全程 require_brand_access 校验 brand 归属(asset 级操作用 asset 真实 brand_id 校验·防 IDOR)

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import logging
from typing import Literal, Optional, List

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from pydantic import BaseModel, Field, field_validator

from auth.brand_access import require_brand_access
from db.brand_image_assets_db import (
    insert_image_asset, get_image_asset, list_image_assets,
    update_image_asset, soft_delete_image_asset, init_brand_image_assets_table,
    get_image_assets_by_ids, batch_set_article_usage,
)
from services.image_storage import save_brand_image, delete_brand_image_files
from tools.vision.image_describe import describe_image_structured

logger = logging.getLogger("GEO-BrandImage-API")
router = APIRouter(prefix="/api/brand-images", tags=["客户图片素材"])

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
_MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15MB

# PATCH 白名单(只允许改这些字段·防前端塞 brand_id 等越权字段)
_UPDATABLE = {
    "publish_allowed", "rights_confirmed", "title", "caption", "alt_text",
    "image_type", "suggested_placement", "usage_scenarios", "status",
}


class AssetUpdate(BaseModel):
    publish_allowed: Optional[int] = None
    rights_confirmed: Optional[int] = None
    title: Optional[str] = None
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    image_type: Optional[str] = None
    suggested_placement: Optional[str] = None
    usage_scenarios: Optional[List[str]] = None
    status: Optional[str] = None


class BulkArticleUsageRequest(BaseModel):
    asset_ids: List[int] = Field(min_length=1, max_length=200)
    enabled: bool
    rights_confirmed: bool = False

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, values: List[int]) -> List[int]:
        unique = list(dict.fromkeys(int(value) for value in values))
        if any(value <= 0 for value in unique):
            raise ValueError("asset_ids 必须是正整数")
        return unique


class BulkArticleUsageItem(BaseModel):
    asset_id: int
    status: Literal["updated", "skipped", "failed"]
    reason: str


class BulkArticleUsageResponse(BaseModel):
    success: bool
    enabled: bool
    updated: int
    skipped: int
    failed: int
    items: List[BulkArticleUsageItem]


def _uploader_id(request: Request) -> Optional[int]:
    user = getattr(request.state, "user", None) or {}
    return user.get("user_id") or user.get("id")


@router.post("/upload", summary="上传客户图片到图库(读图+缩略图+落表)")
async def upload_brand_image(
    request: Request,
    brand_id: int = Form(...),
    file: UploadFile = File(...),
):
    """上传一张客户图片:存储 → 视觉识别(含风险) → 入图库表。返回完整资产。"""
    require_brand_access(request, brand_id)

    filename = (file.filename or "image.png").strip() or "image.png"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"仅支持图片格式:{', '.join(sorted(_IMAGE_EXTENSIONS))}")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="图片过大(上限 15MB),请压缩后再传")

    try:
        init_brand_image_assets_table()  # 幂等·防表未建
    except Exception as e:
        logger.warning(f"[brand-images] 建表检查异常(继续): {e}")

    # 1) 存储:原图 + 缩略图 + 安全尺寸图
    stored = save_brand_image(brand_id, image_bytes, filename)
    # 2) 视觉识别:结构化标注(失败保守降级 publish_allowed=0,不中断)
    desc = await describe_image_structured(image_bytes, filename)

    asset_id = insert_image_asset(
        brand_id=brand_id, file_name=filename,
        storage_key=stored["storage_key"], public_url=stored["public_url"],
        thumbnail_key=stored.get("thumbnail_key"), safe_size_key=stored.get("safe_size_key"),
        image_type=desc.get("image_type"), title=desc.get("title"), caption=desc.get("caption"),
        alt_text=desc.get("alt_text"), suggested_placement=desc.get("suggested_placement"),
        usage_scenarios=desc.get("usage_scenarios"), vision_summary=desc.get("vision_summary"),
        ocr_text=desc.get("ocr_text"), tags=desc.get("tags"),
        publish_allowed=desc.get("publish_allowed", 0), rights_confirmed=0,
        risk_flags=desc.get("risk_flags"), uploaded_by=_uploader_id(request),
    )
    logger.info(f"[brand-images] 上传 brand_id={brand_id} asset_id={asset_id} type={desc.get('image_type')} "
                f"risk={desc.get('risk_flags')} vision_ok={desc.get('vision_ok')}")
    return {"success": True, "asset": get_image_asset(asset_id), "vision_ok": desc.get("vision_ok", True)}


@router.get("/list/{brand_id}", summary="列某客户图库")
async def list_brand_images(brand_id: int, request: Request, include_archived: bool = False):
    require_brand_access(request, brand_id)
    return {"success": True, "assets": list_image_assets(brand_id, include_archived=include_archived)}


@router.patch("/asset/{asset_id}", summary="改图片(可用于文章/确认可外发/标题等)")
async def update_brand_image(asset_id: int, data: AssetUpdate, request: Request):
    asset = get_image_asset(asset_id)
    if not asset or asset.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="图片不存在")
    # 🔴 用 asset 真实 brand_id 校验(不信前端)· 鉴权归属 == 写入归属
    require_brand_access(request, asset.get("brand_id"))

    fields = {k: v for k, v in data.model_dump(exclude_unset=True).items()
              if v is not None and k in _UPDATABLE}
    if not fields:
        return {"success": True, "asset": asset}
    # 🔴 未确认可外发前不得 publish_allowed=1(老板规则·首次开启「可用于文章」必须显式确认 rights_confirmed)
    final_rights = fields.get("rights_confirmed", asset.get("rights_confirmed"))
    if fields.get("publish_allowed") == 1 and final_rights != 1:
        raise HTTPException(status_code=400, detail="请先确认这张图片可用于对外发布")
    update_image_asset(asset_id, **fields)
    return {"success": True, "asset": get_image_asset(asset_id)}


@router.post(
    "/assets/article-usage",
    summary="批量用于文章/取消用于文章",
    response_model=BulkArticleUsageResponse,
)
async def batch_update_article_usage(data: BulkArticleUsageRequest, request: Request):
    """Authorize by each stored asset.brand_id, then update once transactionally."""
    assets = get_image_assets_by_ids(data.asset_ids)
    by_id = {int(asset["id"]): asset for asset in assets}

    checked_brand_ids: set[int] = set()
    for asset_id in data.asset_ids:
        asset = by_id.get(asset_id)
        if not asset:
            continue
        real_brand_id = int(asset["brand_id"])
        if real_brand_id not in checked_brand_ids:
            require_brand_access(request, real_brand_id)
            checked_brand_ids.add(real_brand_id)

    if data.enabled and not data.rights_confirmed:
        raise HTTPException(status_code=400, detail="请先确认所选图片均已获授权，可用于对外发布")

    result = batch_set_article_usage(data.asset_ids, enabled=data.enabled)
    return {
        "success": result["failed"] == 0,
        "enabled": data.enabled,
        **result,
    }


@router.delete("/asset/{asset_id}", summary="删除图片(软删 + 清物理文件)")
async def delete_brand_image(asset_id: int, request: Request):
    asset = get_image_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="图片不存在")
    require_brand_access(request, asset.get("brand_id"))
    soft_delete_image_asset(asset_id)
    delete_brand_image_files(asset.get("storage_key"), asset.get("thumbnail_key"), asset.get("safe_size_key"))
    return {"success": True}


@router.get("/article-preview/{article_id}", summary="渲染文章[CLIENT_IMAGE]占位符为图片(写作预览所见即所发)")
async def render_article_preview(article_id: int, request: Request):
    """写作/发布预览用:把文章 content 里的 [CLIENT_IMAGE asset_id=N] 占位符渲染成真实图片(相对 URL · 同域)。
    content 原文(占位符)不动,只返回渲染后的副本供展示。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT a.content, a.generation_request_snapshot, q.brand_id FROM articles a "
            "LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s",
            (article_id,),
        )
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="文章不存在")
    brand_id = row.get("brand_id")
    content = row.get("content") or ""
    # 校验归属(无 brand 文章放行 · 也无图可渲染)
    require_brand_access(request, brand_id, allow_null=True)
    from services.image_placeholder import (
        count_rendered_images,
        has_client_images,
        render_for_preview_fail_closed,
    )
    # [工单 C-2 T1] 旧「brand_id 为 NULL 则返回原文」的三元短路是 fail-open:
    # quotes.brand_id 可空/换绑/解绑时把 [CLIENT_IMAGE] 原文裸返给前端 ——
    # 生产 8/10 深档裸露的漏点之一。render_for_preview(content, None) 本身
    # fail-closed(逐图校验归属,不匹配静默删),直接调,不再短路。
    rendered = render_for_preview_fail_closed(content, brand_id)
    # [2026-06-02] 联系方式预览(卡片 + 自发布完整/媒体软化提示)· 服务端可信 brand_id
    _has_contact = False
    from services.contact_placeholder import (
        contact_consent_from_snapshot,
        enforce_contact_opt_out,
        hard_strip_contact_without_lookup,
        has_contact_placeholders,
        render_contact_for_preview,
    )
    _contact_consent = contact_consent_from_snapshot(row.get("generation_request_snapshot"))
    from writing.article_length_contract import build_length_guidance_projection

    _length_guidance = build_length_guidance_projection(
        row.get("generation_request_snapshot"),
        content,
    )
    _has_contact = has_contact_placeholders(content) and _contact_consent is not False
    if _contact_consent is False:
        try:
            rendered = enforce_contact_opt_out(rendered, brand_id)
        except Exception as exc:
            logger.error("contact opt-out preview scrub failed article=%s: %s", article_id, exc)
            try:
                rendered = hard_strip_contact_without_lookup(rendered)
            except Exception as fallback_exc:
                logger.error("contact opt-out preview hard scrub failed article=%s: %s", article_id, fallback_exc)
                raise HTTPException(status_code=500, detail="contact_opt_out_preview_failed_closed")
    elif brand_id and _has_contact:
        try:
            rendered = render_contact_for_preview(rendered, brand_id)
        except Exception:
            rendered = hard_strip_contact_without_lookup(rendered)
            _has_contact = False
    return {"success": True, "content": rendered, "has_images": has_client_images(content),
            "has_contact": _has_contact,
            "contact_consent": (
                "enabled" if _contact_consent is True
                else "disabled" if _contact_consent is False
                else "legacy_unknown"
            ),
            "effective_add_contact": _contact_consent,
            "length_guidance": _length_guidance,
            # [工单 T4 2026-07-29] 服务端给出**真实渲染数**与掉图原因。
            # 前端此前用 `content.match(/\[CLIENT_IMAGE/g).length` 数占位符原文当
            # "已自动配图 N 张" —— 那是"想配几张",不是"实际出了几张"。生产 200 个
            # 标记里 62 个因素材被软删而静默剥离,用户看到的正是"说配了图、却一张
            # 都不显示",也就是 Owner 说的"图片加载不出来"。
            "image_render": count_rendered_images(content, brand_id),
            "images": _parse_article_images(content, brand_id), "brand_id": brand_id}


def _parse_article_images(content: str, brand_id: int = None) -> list:
    """解析 content 里的 [CLIENT_IMAGE] 占位符清单(供前端配图「更换/不使用」管理)。
    🔴 brand_id 过滤:不属当前文章 brand 的图(正文手写别客户 asset_id)不返回,防配图清单暴露别客户图缩略图/标题。"""
    import re as _re
    out = []
    for i, mm in enumerate(_re.finditer(r'\[CLIENT_IMAGE\s+asset_id=(\d+)[^\]]*\]', content or "")):
        aid = int(mm.group(1))
        a = get_image_asset(aid)
        # 跨 brand / 无效图 / brand_id 不可知 → 跳过(无法确认归属一律不露·index 保留 content 原序号)
        if not brand_id or not a or a.get("brand_id") != brand_id:
            continue
        role_m = _re.search(r'role=([a-zA-Z_]+)', mm.group(0))
        cap_m = _re.search(r'caption="([^"]*)"', mm.group(0))
        thumb = ""
        if a:
            tk = a.get("thumbnail_key") or a.get("safe_size_key")
            thumb = ("/" + tk) if tk else (a.get("public_url") or "")
        out.append({
            "index": i, "asset_id": aid,
            "role": role_m.group(1) if role_m else "",
            "caption": cap_m.group(1) if cap_m else "",
            "thumbnail_url": thumb,
            "title": (a.get("title") if a else "") or "",
            "valid": bool(a and a.get("status") == "active"),
        })
    return out


class ImageActionRequest(BaseModel):
    action: str             # "remove"(不使用) | "replace"(更换)
    placeholder_index: int  # 第几个 [CLIENT_IMAGE](0-based)
    new_asset_id: Optional[int] = None


@router.post("/article/{article_id}/image-action", summary="改文章配图(不使用 remove / 更换 replace)")
async def article_image_action(article_id: int, data: ImageActionRequest, request: Request):
    """用户在写作预览手动覆盖自动配图。content 改写在后端 + RBAC 校验新图属该 brand 且可外发(防前端绕过)。"""
    import re as _re
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT a.content, a.generation_request_snapshot, q.brand_id FROM articles a LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s", (article_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文章不存在")
        brand_id = row.get("brand_id")
        content = row.get("content") or ""
        # [GEO-R3-CAN-003] 写操作 fail-closed:article 归属只能经 quote → brand → owner 反查,
        # 当 brand_id 为 NULL(无 quote / null-brand quote)时无归属线索,allow_null=True 会对所有
        # 已登录非管理员放行 → 任意用户可按可枚举 article_id 抹除他人 null-brand 文章配图(IDOR)。
        # 改 allow_null=False:brand 无法解析即拒绝(403),不再静默放行。
        require_brand_access(request, brand_id, allow_null=False)

        matches = list(_re.finditer(r'\[CLIENT_IMAGE\s+asset_id=(\d+)[^\]]*\]', content))
        idx = data.placeholder_index
        if idx < 0 or idx >= len(matches):
            raise HTTPException(status_code=400, detail="配图序号无效")
        m = matches[idx]

        if data.action == "remove":
            new_content = content[:m.start()] + content[m.end():]
        elif data.action == "replace":
            if not data.new_asset_id:
                raise HTTPException(status_code=400, detail="缺少替换图片")
            asset = get_image_asset(data.new_asset_id)
            # 🔴 校验:新图属该 brand + active + 已确认可外发
            if not asset or asset.get("brand_id") != brand_id or asset.get("status") != "active":
                raise HTTPException(status_code=403, detail="无效图片或不属该客户")
            if asset.get("publish_allowed") != 1 or asset.get("rights_confirmed") != 1:
                raise HTTPException(status_code=400, detail="该图未确认可对外发布")
            role_m = _re.search(r'role=([a-zA-Z_]+)', m.group(0))
            role = role_m.group(1) if role_m else ""
            cap = (asset.get("caption") or asset.get("title") or "").replace('"', "'")
            new_ph = "[CLIENT_IMAGE asset_id=%d%s caption=\"%s\"]" % (
                data.new_asset_id, (" role=" + role) if role else "", cap)
            new_content = content[:m.start()] + new_ph + content[m.end():]
        else:
            raise HTTPException(status_code=400, detail="未知操作")

        new_content = _re.sub(r'\n{3,}', '\n\n', new_content)
        from services.contact_placeholder import (
            contact_consent_from_snapshot,
            enforce_contact_opt_out,
            hard_strip_contact_without_lookup,
            has_contact_placeholders,
            render_contact_for_preview,
        )
        _contact_consent = contact_consent_from_snapshot(row.get("generation_request_snapshot"))
        if _contact_consent is False:
            try:
                new_content = enforce_contact_opt_out(new_content, brand_id)
            except Exception as exc:
                logger.error("contact opt-out image save scrub failed article=%s: %s", article_id, exc)
                try:
                    new_content = hard_strip_contact_without_lookup(new_content)
                except Exception as fallback_exc:
                    logger.error("contact opt-out image save hard scrub failed article=%s: %s", article_id, fallback_exc)
                    raise HTTPException(status_code=500, detail="contact_opt_out_image_save_failed_closed")
        # [P0 代发内容漂移 · 根因阻断] 同 placement_service:发布窗口内不得改稿,
        # 否则订单快照与实际内容对不上,提交必失败并空转到退款。
        from services.publication_content_drift import article_has_active_publication

        if article_has_active_publication(c, article_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTICLE_LOCKED_BY_PUBLICATION",
                    "message": "这篇稿件正在发布中,暂时不能改动。",
                    "reason": "下单时已冻结稿件版本;此时改动会让发布内容与订单不一致。",
                    "impact": "本次修改没有保存。",
                    "repair_hint": "等发布完成后再编辑;若要现在改,请先在发布中心取消该订单。",
                    "actions": [
                        {"id": "view_orders", "label": "查看发布任务", "type": "nav", "target": "/publish"},
                    ],
                    "rule_version": "publish-content-drift-v1",
                },
            )

        c.execute("UPDATE articles SET content = %s, word_count = %s WHERE id = %s",
                  (new_content, len(new_content), article_id))
        from services.article_review_gate import refresh_article_review

        refresh_article_review(article_id, cursor=c)
        conn.commit()
    finally:
        conn.close()

    from services.image_placeholder import render_for_preview_fail_closed
    # [工单 C-2 T1] 同 article-preview:去掉 fail-open 短路,渲染失败也剥离不裸返。
    rendered = render_for_preview_fail_closed(new_content, brand_id)
    # [2026-06-02] 配图操作后预览也渲染联系方式(自发布完整/媒体软化提示)· 服务端可信 brand_id
    if _contact_consent is False:
        try:
            rendered = enforce_contact_opt_out(rendered, brand_id)
        except Exception as exc:
            logger.error("contact opt-out image preview scrub failed article=%s: %s", article_id, exc)
            try:
                rendered = hard_strip_contact_without_lookup(rendered)
            except Exception as fallback_exc:
                logger.error("contact opt-out image hard scrub failed article=%s: %s", article_id, fallback_exc)
                raise HTTPException(status_code=500, detail="contact_opt_out_preview_failed_closed")
    elif brand_id and has_contact_placeholders(new_content):
        try:
            rendered = render_contact_for_preview(rendered, brand_id)
        except Exception:
            rendered = hard_strip_contact_without_lookup(rendered)
    from writing.article_length_contract import build_length_guidance_projection

    _length_guidance = build_length_guidance_projection(
        row.get("generation_request_snapshot"),
        new_content,
    )
    return {"success": True, "content": new_content, "content_rendered": rendered,
            "length_guidance": _length_guidance,
            "images": _parse_article_images(new_content, brand_id)}
