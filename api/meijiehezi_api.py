"""
外部发布通道代发 API
用户端：媒体列表、下单代发、查看订单
管理端：配置、手动同步、订单管理、统计
"""

import logging
import asyncio
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Request, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from typing import Any, List, Optional, Literal

from db.meijiehezi_db import (
    get_config, set_config, list_media, get_media_filters,
    create_order, get_order_items_by_order, list_orders,
    get_pending_items, update_order_item_submitted,
    set_item_submission_snapshot,
    update_order_item_status, get_admin_stats,
    # [WO-KYB-ROUTING D1] settle_group_order_sns 在模块作用域用它。本文件历来是
    # 在各函数体内 `from db.meijiehezi_db import ... set_item_awaiting_sync`,
    # 模块级并没有这个名字 —— 共用件写在模块作用域,就必须在这里补上,
    # 否则"落库失败"那条分支会 NameError(单测当场抓到过)。
    set_item_awaiting_sync,
    save_media, save_media_batch, get_all_media_ids, deactivate_media,
    save_wemedia_batch, get_all_wemedia_ids, deactivate_wemedia,
    list_wemedia, get_wemedia_filters,
    save_short_video_batch, get_all_short_video_ids, deactivate_short_video,
    list_short_video, get_short_video_filters,
    sync_mhz_orders, list_mhz_synced_orders, get_mhz_synced_order_stats,
    update_synced_order_status,
    backfill_orphan_fields, list_synced_orders_grouped_by_article,
    create_refund_request, list_refund_requests, review_refund_request,
    resolve_refund_payer, RefundNeedsManualReview,
    get_order_refund_status,
    mark_item_awaiting_confirmation,
    get_awaiting_items_by_user,
    get_awaiting_item,
    reset_awaiting_to_pending_for_confirm,
    cancel_awaiting_item,
    # [2026-04-30 admin 衔接补丁] 本地未同步项 + 人工审核队列 + 概览补充
    list_local_pending_items, list_manual_review_items,
    count_local_pending_and_review, admin_manual_review_resolve,
    list_user_publish_history,
)
# [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 三个 admin 手动同步端点的下架守卫。
# 模块级 import(不是函数体内 import):守卫是三个端点的共用件,漏了会 NameError,
# 而这三个端点的失败路径只会写进 last_*_sync_result,不会被调用方看见。
from services.catalog_snapshot_guard import screen_stale_for_deactivation

logger = logging.getLogger("GEO-MHZ-API")

router = APIRouter(prefix="/api/meijiehezi", tags=["外部发布通道代发"])

# RBAC helper（fix/security-audit-p0 #17）
from auth.brand_access import require_brand_access, require_quote_access
# [R2 §②] 用户面双轴投影单一权威源(回执 ≠ 已核实发布)
from services.publication_receipt_projection import (
    PUBLICATION_AXIS_SQL,
    PUBLICATION_LABELS,
    PUBLICATION_REPORTED_UNVERIFIED,
    PUBLICATION_VERIFIED,
)


def _require_article_access(request: Request, article_id: int) -> None:
    """通过 article_id 反查 articles.quote_id → quote.brand_id 校验当前用户归属"""
    if not article_id:
        return
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT quote_id FROM articles WHERE id = %s", (article_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"文章不存在 article_id={article_id}")
        quote_id = row.get("quote_id")
        if quote_id:
            require_quote_access(request, quote_id)
    finally:
        conn.close()

# 同步进度追踪（内存，不需要持久化）
_sync_progress = {
    "media": {"phase": "", "current": 0, "total": 0, "detail": ""},
    "wemedia": {"phase": "", "current": 0, "total": 0, "detail": ""},
    "status": {"phase": "", "current": 0, "total": 0, "detail": ""},
    "short_video": {"phase": "", "current": 0, "total": 0, "detail": ""},  # [svideo lane · 2026-07-04]
}


def _get_user(request: Request) -> dict:
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _require_writing(user: dict):
    """检查用户是否有 writing 权限（管理员或有 writing:write）"""
    if user.get("is_admin"):
        return
    perms = user.get("permissions", [])
    if "writing:write" in perms or "writing:read" in perms:
        return
    raise HTTPException(status_code=403, detail="无权限")


def _require_admin(user: dict):
    """[GEO-R8-CAN-006] 平台管理员闸。
    /api/meijiehezi/ 整个前缀在全局中间件里映射到 'writing' 模块(非 /api/admin/),
    所以 /admin/* 路由若只用 _require_writing 会让任何持 writing:read/write 的普通代理
    读到跨租户全局数据(全平台订单/退款/统计)或触发全局外部同步。凡返回/操作跨租户数据的
    /admin/* handler 必须走本闸,只放行 is_admin。用户自查(user_id 已收窄)的端点仍用 _require_writing。"""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限：仅管理员可访问")


def _get_canonical_article_title(article_id) -> Optional[str]:
    """[GEO-R5-CAN-012] 从 articles.title 读取权威标题(与服务端加载的正文同源)。
    绝不用客户端 req.article_title 作发布/落单标题——客户端可传与 DB 正文不符的过期/篡改标题,
    造成 canonical 正文配错标题。查不到(无 article_id / 文章不存在)返回 None,由调用方兜底。"""
    if not article_id:
        return None
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT title FROM articles WHERE id = %s", (int(article_id),))
        row = c.fetchone()
        if row:
            return row.get("title")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _brand_name_for_byline(brand_id) -> str:
    """[P1-3] 取品牌名供署名段用。任何异常 → 空串(署名不加,发布照常 —— O1)。"""
    try:
        if not brand_id:
            return ""
        from db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM brands WHERE id = %s", (int(brand_id),))
            row = c.fetchone() or {}
            return str(row.get("name") or "").strip()
    except Exception:
        return ""


def _require_article_review_for_publish(article_id: int) -> None:
    """Fail before pricing/deduction; DB create_order repeats this defensively."""
    from services.article_review_gate import (
        ArticlePublicationBlocked, assert_publication_eligible,
    )

    try:
        assert_publication_eligible(article_id)
    except ArticlePublicationBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


def _require_strict_media_presubmit(
    article_id: int,
    media_ids: list,
    media_names: list,
    *,
    user_id: int = 0,
    brand_id: int = 0,
) -> None:
    """严审媒体按**实际下单媒体**再判一次 v2 档 —— 也在扣费之前（工单 T6）。

    `_require_article_review_for_publish` 判的是文章自己存的 publication_profile；
    搜狐系 52.2% 的拒稿正是"按 standard 档写的稿下单到严审媒体"造成的。
    非严审族媒体不受影响（`is_strict_review_media` 只认有语料证据的域族）。
    """
    from services.strict_media_presubmit import (
        StrictMediaPresubmitBlocked, assert_strict_media_presubmit,
    )

    if not article_id or not media_ids:
        return
    items = [
        {"media_id": mid, "media_name": (media_names[i] if i < len(media_names or []) else "")}
        for i, mid in enumerate(media_ids or [])
    ]
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT title, content FROM articles WHERE id = %s", (int(article_id),))
        row = c.fetchone() or {}
        client_brand = ""
        if brand_id:
            try:
                # 🔴 [#114] 生产 brands 的列是 `name`,没有 `brand_name`。
                #    原写法 UndefinedColumn 被外层 except 吞掉 ⇒ client_brand 恒 ""(静默失败)。
                #    不用 `SELECT name AS brand_name`:别名会让 #94 那道列名门把本行判进
                #    UNRESOLVED —— FAIL 归零了,覆盖也一起没了。
                c.execute("SELECT name FROM brands WHERE id = %s", (int(brand_id),))
                client_brand = str((c.fetchone() or {}).get("name") or "")
            except Exception:
                client_brand = ""
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row:
        return

    try:
        assert_strict_media_presubmit(
            article_id=int(article_id),
            title=str(row.get("title") or ""),
            content=str(row.get("content") or ""),
            media_items=items,
            client_brand=client_brand,
            actor_user_id=int(user_id or 0),
        )
    except StrictMediaPresubmitBlocked as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc


def _get_client():
    """发布渠道客户端:按配置取(services/publish_channels);没接入 ⇒ ChannelNotConfigured。"""
    from services.publish_channels import get_client
    return get_client()


def _build_confirms_from_items(items: list) -> dict:
    """从 items 的 pending_confirm_fields 构造 confirms 参数（取并集）。

    item 第一次提交时 pending_confirm_fields 为空 → 返回 {}（无 confirm flag）。
    item 经过 awaiting 后用户确认 → 字段被写入 pending_confirm_fields → 这里读出来传给 publish()。
    """
    import json as _json
    confirms: dict = {}
    for it in items:
        fields = it.get("pending_confirm_fields")
        if not fields:
            continue
        if isinstance(fields, str):
            try:
                fields = _json.loads(fields)
            except Exception:
                fields = []
        if not isinstance(fields, list):
            continue
        for f in fields:
            if isinstance(f, str):
                confirms[f] = 1
    return confirms


# ========== [WO-KYB-ROUTING D1] 分渠道投递的共用件 ==========
#
# 全仓有 4 段"把一批 item 投出去、再按 media_id 领单号"的代码
# (批量软文 / 批量自媒体 / 调度器重试软文 / 调度器重试自媒体),
# 此前它们【都没有 provider 分流】—— 快易播的 media_id(带 1 亿偏移)
# 被当成媒介盒子的 id 发出去,对方不认识,回「请选择媒体!」。
# 快易播媒体上线至今 0 次成功,15 单全失败 35,490 算力,根因就是这个。
#
# 🔴 领单号这段是**资金语义**,不是格式化:
#   - 媒介盒子:批量返回一个主 sn + 一张 media_id→sn 映射;映射里没有的用主 sn 兜底。
#   - 快易播:**必须按 item 拿到自己的单号**,拿不到就是这条压根没提交成功
#     (Owner 口径「未发布成功就退款」)。用主 sn 兜底会把没发出去的标成已提交,
#     算力冻在那里,既不退也不发 —— 正是「僵尸 pending」那类事故。
# 所以这段绝不能在 4 处各抄一份,抄了迟早走样。单发链(submit_to_mhz)
# **有意不改**,以保证"单发路径行为逐位不变"这条验收能用源码 diff 直接证明。


def settle_group_order_sns(group, result, provider, *, awaiting_reason):
    """把一组已投出的 item 结算成"拿到单号 / 等反查 / 未提交",返回真拿到单号的 item_id 集合。

    参数 ``result`` 是 ``dispatch_article_to_provider`` 的返回,需要 ``order_sn``
    与 ``order_sn_map``。``provider`` 决定"拿不到单号"怎么解释(见上方注释)。
    """
    from services.kuaiyibo.config import PROVIDER_KEY as _KYB

    sn_map = getattr(result, "order_sn_map", None) or {}
    submitted_ids = set()
    for it in group:
        media_id = int(it["media_id"])
        item_sn = sn_map.get(media_id) or getattr(result, "order_sn", None)
        if str(provider) == _KYB and not sn_map.get(media_id):
            # 快易播按 item 拿单号;拿不到 = 这条没提交成功。
            # 不标已提交、也不进 awaiting_sync —— 交给下面 fail_and_refund_unsubmitted
            # 走「未发布成功就退款」,不能让算力冻在那里。
            continue
        if not update_order_item_submitted(it["id"], item_sn):
            # 🔴 [WO-PUB-ZOMBIE] 投出去了但没拿到单号:此刻分不清"对方没收到"
            #   还是"对方收了、回执丢了"。不许留 pending(永久锁死的僵尸),
            #   也不许当场退款。交给 awaiting_sync 实时反查回填。
            set_item_awaiting_sync(it["id"], reason=awaiting_reason)
            continue
        submitted_ids.add(int(it["id"]))
    if str(provider) == _KYB:
        try:
            from services.kuaiyibo.status_sync import fail_and_refund_unsubmitted

            # 🔴 [下单备注 P0 · 2026-08-10] 把供应商按 media 的**真实失败原因**带下去。
            #   适配层 raw_data 里的 failure_by_media 是 additive 新字段;取不到就传 None,
            #   下游退回老行为("提交未成功"),不会因为字段缺失而炸。
            _raw = getattr(result, "raw_data", None) or {}
            _reasons = _raw.get("failure_by_media") if isinstance(_raw, dict) else None
            fail_and_refund_unsubmitted(
                group, {int(k) for k in sn_map.keys()}, _reasons)
        except Exception as _exc:  # 退款失败不许把已成功的那些也带崩
            logger.error("[D1] 快易播未提交项退款异常 provider=%s err=%s", provider, _exc)
    return submitted_ids


# ========== [D0-b 方案 A] 进货价不外露 ==========
#
# 原状:目录接口返回 price(我方进货价),/markup 是公开端点直接返回 1.5,
#      展示价由前端算 —— 代理打开 F12 就能同时拿到进货价与加价率。
# 方案 A:售价由服务端算成 price_points 返回,进货价与加价率都不出后端。
#
# 🔴 不做设计稿里那条"双发过渡"(一版内同时返回 price 与 price_points):
#    前端由 Dockerfile multi-stage 打进同一镜像,前后端**必然同发**,不存在版本空窗;
#    而双发那一版 price 照样外露 = 那一版仍是半修。所以直接切。
#    换来的部署硬要求:必须重建前端镜像,回滚 = 整镜像回滚。
#
# 口径与推荐链共用 services/media_price_projection —— 两条链各写一套正是这个 bug 的成因。
from services.media_price_projection import (
    points_to_yuan as _points_to_yuan,
    project_rows as _project_rows,
    public_sort_key as _public_sort_key,
)


def _project_catalog(result: dict, markup: float) -> dict:
    """目录响应投影:加 price_points,删掉全部成本侧字段。

    删而不是保留 —— 留着 price 就等于没修:售价 ÷ price 直接反推出加价率。
    """
    _project_rows(result.get("media") or [], markup)
    return result


# ========== 用户端 ==========

@router.get("/media")
async def api_list_media(
    request: Request,
    page: int = 1, limit: int = 20,
    search: str = "", area: str = "",
    resource_type: str = "", news_resource: str = "",
    portal_media: str = "", resource_type_name: str = "",
    geo_rank: int = 0, authority_media: int = -1,
    sort_by: str = "price_points", sort_dir: str = "asc",
    points_min: int = 0, points_max: int = 0,
    geo_platform: str = Query("", description="GEO排名平台代码"),
    special_industry: int = Query(-1, description="特别行业"),
):
    # [D0-b] 筛选参数换成算力口径。🔴 元口径的 price_min/price_max **必须去掉而不是并存**:
    #   留着它,调用方二分 price_min 看哪些媒体消失,就能把进货价试出来 —— 那是个泄漏信道,
    #   跟直接返回 price 没有本质区别。参数不声明 = FastAPI 直接忽略,老链接不会报错。
    _get_user(request)
    _markup = _get_publish_markup()
    result = list_media(
        page, limit, search, area, resource_type, news_resource,
        _public_sort_key(sort_by), sort_dir,
        price_min=_points_to_yuan(points_min, _markup),
        price_max=_points_to_yuan(points_max, _markup),
        portal_media=portal_media, resource_type_name=resource_type_name,
        geo_rank=geo_rank, authority_media=authority_media,
        geo_platform=geo_platform, special_industry=special_industry,
    )
    return {"status": "success", **_project_catalog(result, _markup)}


@router.get("/media/filters")
async def api_media_filters(request: Request):
    _get_user(request)
    return {"status": "success", **get_media_filters()}


@router.get("/wemedia")
async def api_list_wemedia(
    request: Request,
    page: int = 1, limit: int = 20,
    search: str = "", platform: str = "",
    industry: str = "", province: str = "",
    sort_by: str = "price_points", sort_dir: str = "asc",
    points_min: int = 0, points_max: int = 0,
    geo_platform: str = "", fans_min: int = 0, fans_max: int = 0,
    authority_media: int = -1,
):
    _get_user(request)
    _markup = _get_publish_markup()
    result = list_wemedia(
        page, limit, search, platform, industry, province,
        _public_sort_key(sort_by), sort_dir,
        price_min=_points_to_yuan(points_min, _markup),
        price_max=_points_to_yuan(points_max, _markup),
        geo_platform=geo_platform, fans_min=fans_min, fans_max=fans_max,
        authority_media=authority_media,
    )
    return {"status": "success", **_project_catalog(result, _markup)}


@router.get("/wemedia/filters")
async def api_wemedia_filters(
    request: Request,
    search: str = "", platform: str = "", province: str = "",
    points_min: int = 0, points_max: int = 0,
    geo_platform: str = "", fans_min: int = 0, fans_max: int = 0,
    authority_media: int = -1,
):
    """[WO_PUBLISH_DISPATCH Part② 2026-08-17] `industries` 改成 L1 大类 + facet 计数。

    参数与 `/wemedia` 列表**同名同义**(industry 自己除外 —— 它不参与自己的 facet),
    全部可选:一个都不传 = 全目录口径,老客户端照样能调通。
    """
    _get_user(request)
    _markup = _get_publish_markup()
    return {"status": "success", **get_wemedia_filters(
        search=search, platform=platform, province=province,
        price_min=_points_to_yuan(points_min, _markup),
        price_max=_points_to_yuan(points_max, _markup),
        geo_platform=geo_platform, fans_min=fans_min, fans_max=fans_max,
        authority_media=authority_media,
    )}


@router.get("/short-video")
async def api_list_short_video(
    request: Request,
    page: int = 1, limit: int = 20,
    search: str = "", platform: str = "",
    location: str = "", industry: str = "",
    sort_by: str = "price_points", sort_dir: str = "asc",
    points_min: int = 0, points_max: int = 0,
    fans_min: int = 0, fans_max: int = 0,
    can_modify: int = -1, authority_media: int = -1,
    account_auth: str = "",
    # [#192 c1] -1 = not filtered (legacy calls byte-identical) / 1 = image-note capable only.
    can_tuwen: int = -1,
):
    """短视频资源列表（只读本地已同步资源池）。[D0-b] 对外只给 price_points(售价算力)。"""
    _get_user(request)
    _markup = _get_publish_markup()
    result = list_short_video(
        page, limit, search, platform, location, industry,
        _public_sort_key(sort_by), sort_dir,
        price_min=_points_to_yuan(points_min, _markup),
        price_max=_points_to_yuan(points_max, _markup),
        fans_min=fans_min, fans_max=fans_max,
        can_modify=can_modify, authority_media=authority_media,
        account_auth=account_auth, can_tuwen=can_tuwen,
    )
    return {"status": "success", **_project_catalog(result, _markup)}


@router.get("/short-video/filters")
async def api_short_video_filters(
    request: Request,
    search: str = "", platform: str = "", location: str = "",
    points_min: int = 0, points_max: int = 0,
    fans_min: int = 0, fans_max: int = 0,
    can_modify: int = -1, authority_media: int = -1, account_auth: str = "",
    can_tuwen: int = -1,
):
    """[WO_PUBLISH_DISPATCH Part② 2026-08-17] 与自媒体同治:L1 大类 + facet 计数。"""
    _get_user(request)
    _markup = _get_publish_markup()
    return {"status": "success", **get_short_video_filters(
        search=search, platform=platform, location=location,
        price_min=_points_to_yuan(points_min, _markup),
        price_max=_points_to_yuan(points_max, _markup),
        fans_min=fans_min, fans_max=fans_max, can_modify=can_modify,
        authority_media=authority_media, account_auth=account_auth, can_tuwen=can_tuwen,
    )}


@router.get("/markup")
async def api_get_markup_public(request: Request):
    """[D0-b] 加价比例收窄成 admin-only。

    原状:docstring 自写「无需 admin 权限」,任何登录用户都能拿到 1.5 ——
    配上当时同样外露的进货价,加价率与成本一起躺在浏览器里。

    现在展示价由服务端算成 price_points 直接返回,**前端不再需要这个数**;
    admin 后台算毛利仍要看,所以保留端点但只放行 is_admin。
    """
    user = _get_user(request)
    _require_admin(user)
    ratio = get_config("markup_ratio") or "1.5"
    return {"status": "success", "ratio": float(ratio)}


class PublishOrderRequest(BaseModel):
    article_id: int
    article_title: str
    media_ids: List[int]
    media_names: List[str]
    cost_points: List[int] = []
    cost_yuan: List[float] = []
    brand_name: str = ""
    brand_id: Optional[int] = None
    media_type: str = "article"
    republish_from_sn: Optional[str] = None  # 重发：原拒稿订单的 order_sn
    # [P0-D] 幂等键（客户端 UUID）— 防双击/网络重试导致重复扣费 + 重复下单
    # 同一 request_id 在 24 小时内的二次请求会直接返回首次响应，不再扣费/下单
    request_id: Optional[str] = None
    # [客户反馈② 2026-08-09] 下单备注（非必选）。少数媒体（列举网 / 车主之家随机）
    #   只能靠下单备注指定投放地区，上游 publish(order_remark=) 一直支持、
    #   我方**全部调用方从不填** —— 这里把它接上。
    order_remark: str = ""


# [客户反馈② 2026-08-09] 备注收口:去首尾空白 + 截断。
#   上游是自由文本，我方自己封顶，防误粘长文把下单接口撑爆。
#   与前端 publishCenterInputs.ts 的 REGION_REMARK_MAX_LEN 同值 —— 两侧都收，
#   但**后端这一侧才是有效的那道**（前端可绕过）。
ORDER_REMARK_MAX_LEN = 200


def _normalize_order_remark(raw: Any) -> str:
    return str(raw or "").strip()[:ORDER_REMARK_MAX_LEN]


def _persist_order_remark(order_id, remark: str) -> None:
    """把下单备注落到 `mhz_publish_orders.order_remark`。

    只为一件事:mhz 回 203/204/205 时首次提交会转成 awaiting,用户确认后走**重投**
    路径,而那时请求体早就不在了 —— 不落库,重投那一次就会把地区备注丢掉。

    🔴 **静默失败**(与 migration_030 文件头写的方向一致):列没跑上、DB 抖一下,
      都不许把发布主链带崩;代价是首次提交照常带备注、只有重投那次拿不到 ——
      退回本次修复前的样子,不制造新的坏状态。
    """
    if not remark or not order_id:
        return
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE mhz_publish_orders SET order_remark = %s WHERE id = %s",
                (remark, order_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[OrderRemark] 落库失败 order=%s: %s", order_id, exc)


def _read_order_remark(order_id) -> str:
    """重投时把当初填的备注取回来。取不到 → 空串(不带备注,不阻断)。"""
    if not order_id:
        return ""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT order_remark FROM mhz_publish_orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            return _normalize_order_remark((row or {}).get("order_remark"))
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[OrderRemark] 回读失败 order=%s: %s", order_id, exc)
        return ""


def _remark_kwarg(kind: str, remark: str) -> dict:
    """把备注放进对应渠道的 kwarg 名下；空备注 → 空 dict（不改变原有请求体）。

    🔴 两条链的参数名**本来就不同**，不是笔误：
      - ``client.publish(order_remark=...)``(软文下单)
      - ``client.publish_wemedia(article_remark=...)``(自媒体下单)
    快易播适配器与 ``MeiJieHeZiClient.publish`` 同签名，走 ``order_remark`` 这一支。

    🔴 **[下单备注 P0 · 2026-08-10] 总闸关时这里返空 dict —— 字段根本不进请求体。**
      这是**全链唯一的收口点**:软文 / 自媒体 / 首次提交 / 重投,四条路径都经过它。
      闸放在这里而不是四个调用点各判一次,是为了不给"漏改一处"留机会。
      生产实证:填备注 = 100% 拒单(订单 479/480 failed vs 481 空备注 submitted),
      详见 `services/meijiehezi/config.ORDER_REMARK_ENABLED` 的注释。
    """
    from services.meijiehezi.config import ORDER_REMARK_ENABLED

    if not ORDER_REMARK_ENABLED:
        return {}
    if not remark:
        return {}
    return {"article_remark" if kind == "publish_wemedia" else "order_remark": remark}


def _get_publish_markup() -> float:
    """读媒介盒子加价比例(mhz_config.markup_ratio · SSOT · 与前端 /api/meijiehezi/markup 同源)。

    [D2 收口] 媒体系数真实生效唯一在 mhz_config(admin 改 PUT /api/meijiehezi/admin/config/markup·热生效);
      mhz_config 无值时兜底用 pricing_config.get_media_markup_default()(默认见 pricing_config·统一默认来源)。
      不再维护 system_config.publish_markup_ratio(legacy·只写不读真实扣费·避免后台改了不生效)。"""
    try:
        from db.meijiehezi_db import get_config
        v = get_config("markup_ratio")
        if v:
            return float(v)
    except Exception:
        pass
    try:
        from config.pricing_config import get_media_markup_default
        return get_media_markup_default()
    except Exception:
        return 1.5


def _recompute_publish_charge(media_specs, markup: float):
    """[A-8-1 资金安全修 2026-05-29] 服务端按 media_id 查权威基础价重算扣费积分,绝不信客户端 cost_points。

    media_specs: List[(media_id:int, media_type:str)]:
      media_type=='wemedia' 查 mhz_wemedia · =='svideo' 查 mhz_short_video · 其余(软文/article/'') 查 mhz_media。
    口径与前端 yuanToPoints 完全一致:ceil(基础价 price × markup × 130)。
    短视频计费只用 mhz_short_video.price(不用 hepai_price 合拍价,本包只做直发)。
    返回 per-index (points, base_yuan) list(与输入同序):
      - points   = 服务端权威扣费积分(收入)→ deduct + 订单 cost_points
      - base_yuan = DB 权威外采价(成本)→ 订单 cost_yuan(dashboard SUM(cost_yuan) 当外采成本/毛利,绝不信客户端)
    任一 media_id 在权威表查不到(下架/伪造 id)→ raise 400(fail-closed · 绝不按客户端价放行未知媒体)。
    [svideo lane · 2026-07-04] 三分支归一化:'svideo' 必须保留原值,绝不能被旧的 else→'mhz'
      吞掉去查 mhz_media(会用错表取价 + id 撞号取到错误价格 → 错扣费)。
    """
    import math
    from db.meijiehezi_db import MEDIA_TYPE_WEMEDIA, MEDIA_TYPE_SVIDEO

    def _norm(mt):
        if mt == MEDIA_TYPE_WEMEDIA:
            return "wemedia"
        if mt == MEDIA_TYPE_SVIDEO:
            return "svideo"
        return "mhz"  # 软文（含历史 'article' / ''）

    specs = [(int(mid), _norm(mt)) for mid, mt in media_specs]
    mhz_ids = [mid for mid, mt in specs if mt == "mhz"]
    wm_ids = [mid for mid, mt in specs if mt == "wemedia"]
    sv_ids = [mid for mid, mt in specs if mt == "svideo"]
    price_map: dict = {}
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        if mhz_ids:
            ph = ",".join(["%s"] * len(mhz_ids))
            cur.execute(f"SELECT id, price FROM mhz_media WHERE id IN ({ph}) AND is_active = TRUE", tuple(mhz_ids))
            for r in cur.fetchall():
                price_map[("mhz", int(r["id"]))] = float(r["price"] or 0)
        if wm_ids:
            ph = ",".join(["%s"] * len(wm_ids))
            cur.execute(f"SELECT id, price FROM mhz_wemedia WHERE id IN ({ph}) AND is_active = TRUE", tuple(wm_ids))
            for r in cur.fetchall():
                price_map[("wemedia", int(r["id"]))] = float(r["price"] or 0)
        if sv_ids:
            ph = ",".join(["%s"] * len(sv_ids))
            cur.execute(f"SELECT id, price FROM mhz_short_video WHERE id IN ({ph}) AND is_active = TRUE", tuple(sv_ids))
            for r in cur.fetchall():
                price_map[("svideo", int(r["id"]))] = float(r["price"] or 0)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    charges = []  # 每项 (points, base_yuan) · 二者都来自 DB 权威价
    for mid, mt in specs:
        base_yuan = price_map.get((mt, mid))
        if base_yuan is None:
            raise HTTPException(status_code=400, detail=f"媒体 {mid} 不存在或已下架,无法计价(请刷新媒体列表后重试)")
        pts = int(math.ceil(base_yuan * markup * 130))
        if pts <= 0:
            raise HTTPException(status_code=400, detail=f"媒体 {mid} 定价异常,请刷新重试")
        charges.append((pts, round(float(base_yuan), 2)))
    return charges


def _resolve_svideo_media_names(media_ids) -> dict:
    """[发布链 SSOT §9 缺陷② 2026-08-02] 服务端权威取短视频账号名。

    现象:`mhz_publish_order_items.media_name` 存成空串,订单列表显示缺失
    (生产实证 item_id=458 media_name='')。
    根因:名字只来自客户端 `req.media_names`,客户端没传/传短了就落 ''
    —— 展示字段不该只信客户端(与价格已经服务端权威重算是同一个道理)。
    修法:服务端按 media_id 查真名,客户端值仅作兜底。

    fail-soft:查不到就返回空 dict,由调用方回退客户端值 —— 名字是展示字段,
    绝不能因为取名失败而挡住下单(那会把一个"低"缺陷升级成阻断)。
    """
    ids = [int(m) for m in (media_ids or [])]
    if not ids:
        return {}
    from db.connection import get_connection
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT id, media_name FROM mhz_short_video WHERE id IN ({ph})",
            tuple(ids),
        )
        return {int(r["id"]): (r["media_name"] or "").strip()
                for r in cur.fetchall() if (r["media_name"] or "").strip()}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[svideo] 取账号名失败(回退客户端值): {type(e).__name__}: {str(e)[:120]}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass



def _publish_approval_gate(request, *, articles: int, payload: dict):
    """[发布审批开关 · Owner 2026-07-25 拍板]

    组织员工发布时先问一次组织的审批策略:
      - 开关**关**(默认)→ 直接发,不卡人。老板雇人就是为了不当瓶颈,
        而且员工本来就能用老板的余额跑诊断、生成文章,发布同属交付动作。
      - 开关**开** → 不执行发布、不扣费,转成一张审批单等老板/审核人点头。

    非组织用户(普通服务商本人)完全不走这里,行为一个字不变。
    返回 None = 放行;返回 dict = 已转审批,调用方直接把它返给前端。
    """
    identity = getattr(request.state, "organization_identity", None)
    if identity is None or not getattr(identity, "is_member", False):
        return None

    from db.connection import get_db
    from services.organization_approvals import approval_required_with_default, submit_approval

    with get_db() as conn:
        cursor = conn.cursor()
        required, policy = approval_required_with_default(
            cursor,
            identity,
            action_type="publish.execute",
            default_required=False,      # 老板拍板:没配策略 = 默认直接发
            estimated_points=int(articles or 0),
        )
        conn.commit()
    if not required:
        return None

    approval = submit_approval(
        identity,
        action_type="publish.execute",
        payload=payload,
        request_id=str(payload.get("request_id") or "") or f"publish:{identity.membership_id}:{articles}",
        artifact_type="publish_order",
    )
    return {
        "success": False,
        "approval_submitted": True,
        "detail": {
            "code": "PUBLISH_APPROVAL_REQUIRED",
            "message": "已提交给团队长审批,批准后会自动发布。",
            "reason": "你所在团队开启了「发布需审批」。",
            "impact": "本次没有发布,也没有扣费。",
            "repair_hint": "等待团队长在审批列表中批准;如需直接发布,请团队长关闭该开关。",
            "actions": [
                {"id": "view_approvals", "label": "查看我的审批", "type": "nav", "target": "/organization/approvals"},
            ],
            "rule_version": "publish-approval-toggle-v1",
        },
        "approval": approval,
    }


# ---------------------------------------------------------------------------
# [WO-PUB-ZOMBIE-2026-08-04 · R4] 重复提交拦截的用户文案
# ---------------------------------------------------------------------------

#: 标题截断总长。超了留头留尾中间打省略号,别硬切出「福田区全屋定制：香蜜湖等片区老」
#: 这种断在半句上的东西 —— 用户看不出是哪一篇。
_DUP_TITLE_MAX = 24
#: 每种理由最多列几条,超出的用"等共 N 条"说清楚,不做静默截断。
_DUP_LIST_MAX = 5


def shorten_title_for_message(title: str, limit: int = _DUP_TITLE_MAX) -> str:
    """标题过长时保留首尾、中间省略。短标题原样返回。"""
    text = (title or "").strip()
    if len(text) <= limit:
        return text
    # 头多留一点(信息量集中在前面),尾巴留够认出是哪一篇。
    head = max(1, (limit * 2) // 3)
    tail = max(1, limit - head)
    return f"{text[:head]}…{text[-tail:]}"


def _media_name_of(media_id, media_ids: list, media_names: list) -> str:
    """按下标取媒体名;取不到就退回 media_id 字符串(不编名字)。"""
    try:
        idx = list(media_ids).index(media_id)
    except (ValueError, TypeError):
        return str(media_id)
    return media_names[idx] if idx < len(media_names) else str(media_id)


def build_conflict_entry(*, article_id, article_title: str, media_id,
                         media_name: str, reason_code: str) -> dict:
    """[WO-BATCH-CONFLICT-2026-08-04] 一条结构化冲突。

    🔴 必须带 ``article_id`` + ``media_id`` —— 前端要靠这一对**精确**把冲突组合从
    购物车里摘掉。老响应只有一个拼好的展示串(``label``),那种串回不到具体组合上,
    于是前端只能整批放弃 = 生产里"整批卡死,只有一个 toast"的直接原因。

    ``label`` 保留:它是给 `build_duplicate_block_message` 拼人话用的,不是标识符。
    """
    return {
        "article_id": article_id,
        "media_id": media_id,
        "article_title": article_title or "",
        "media_name": media_name or "",
        "reason_code": reason_code,
        "label": f"{shorten_title_for_message(article_title)}×{media_name}",
    }


def build_duplicate_block_message(entries: list) -> str:
    """把拦截明细拼成人话。``entries`` 每项 ``{"reason_code":..., "label":...}``。

    🔴 老文案对 **235 条 published**(最老 95 天)也说"已有进行中的发布订单",
    用户以为在排队,于是一直等 —— 而它其实早就发完了。按理由码分开说。
    """
    from db.meijiehezi_db import BLOCK_REASON_ALREADY_PUBLISHED

    published = [e["label"] for e in entries
                 if e.get("reason_code") == BLOCK_REASON_ALREADY_PUBLISHED]
    in_flight = [e["label"] for e in entries
                 if e.get("reason_code") != BLOCK_REASON_ALREADY_PUBLISHED]

    def _fmt(labels: list, lead: str) -> str:
        shown = "、".join(labels[:_DUP_LIST_MAX])
        if len(labels) > _DUP_LIST_MAX:
            # 明说还有几条,不静默截断(老实现只显示 5 条,用户以为只冲突 5 个)
            return f"{lead}{shown} 等共 {len(labels)} 条"
        return f"{lead}{shown}"

    parts = []
    if published:
        parts.append(_fmt(published, "这几篇已经发过对应媒体了："))
    if in_flight:
        parts.append(_fmt(in_flight, "这几篇正在发布中，请勿重复提交："))
    return "；".join(parts) if parts else "存在重复的发布组合，请勿重复提交"


@router.post("/publish")
async def api_publish(req: PublishOrderRequest, request: Request):
    """用户提交代发订单 → 扣积分 → 创建本地订单 → 调外部发布通道发文"""
    user = _get_user(request)
    user_id = user["user_id"]
    _gate = _publish_approval_gate(request, articles=1, payload=req.model_dump() if hasattr(req, "model_dump") else {})
    if _gate:
        return _gate
    _pub_charge_txid = None  # [GEO-R2-CAN-039 v3] 精确退款透传：deduct 返回的不可变 charge_tx_id，供 create_order 失败退费精确定位（全路径先绑定）

    # [P0-D] 幂等键检查 — 命中直接返缓存响应，不扣费不下单
    # 防双击/网络抖动 axios retry / 弱网用户重复点击导致同一笔扣两次
    if req.request_id:
        from db.meijiehezi_db import check_idempotency
        cached = check_idempotency(req.request_id, user_id, "/api/publish")
        if cached:
            logger.info(f"[Idempotency] /publish 命中: request_id={req.request_id} user={user_id}")
            return cached

    if not req.media_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个媒体")

    # [svideo lane · 2026-07-04] 短视频有独立发布入口（/short-video/publish，载荷含 video_url）。
    #   在扣费/去重之前 fail-fast 拦截，绝不让 svideo 落进本端点的软文分支被错发/错扣。
    from db.meijiehezi_db import MEDIA_TYPE_SVIDEO as _MT_SVIDEO
    if req.media_type == _MT_SVIDEO:
        raise HTTPException(status_code=400, detail="短视频请通过短视频发布入口提交")

    # RBAC: 文章和品牌归属校验（防伪造他人 article_id / brand_id 让自己扣费发别人文章）
    _require_article_access(request, req.article_id)
    _require_article_review_for_publish(req.article_id)
    if req.brand_id:
        require_brand_access(request, req.brand_id)
    _require_strict_media_presubmit(
        req.article_id, req.media_ids, req.media_names,
        user_id=user_id, brand_id=(req.brand_id or 0),
    )

    # 扣费前去重检查：同文章+媒体已有真该拦的订单时直接拒绝，不扣钱
    # [WO-PUB-ZOMBIE R3] 返回值带理由码;无外部单号的 pending 不再拦(那是没提交成功的僵尸)
    if req.article_id:
        from db.meijiehezi_db import find_active_orders_for_media
        dups = find_active_orders_for_media(req.article_id, req.media_ids)
        if dups:
            entries = [
                build_conflict_entry(
                    article_id=req.article_id,
                    article_title=req.article_title,
                    media_id=d["media_id"],
                    media_name=_media_name_of(d["media_id"], req.media_ids, req.media_names),
                    reason_code=d["reason_code"],
                )
                for d in dups
            ]
            raise HTTPException(status_code=409, detail={
                "code": "DUPLICATE_ORDER",
                "message": build_duplicate_block_message(entries),
                # 🔴 旧键保留:单条提交的老前端只认它,不许因为本单改结构化就断掉。
                "duplicate_media_ids": [d["media_id"] for d in dups],
                "conflicts": entries,
            })

    # 计算总费用（积分）
    # [A-8-1 资金安全修 2026-05-29] 服务端按 media_ids 重算权威价 · 不信客户端 cost_points
    #   (原 sum(req.cost_points) 完全信客户端 · 可篡改成 1 积分发高价稿)
    _markup = _get_publish_markup()
    server_charges = _recompute_publish_charge(
        [(mid, req.media_type) for mid in req.media_ids], _markup
    )
    total_cost_points = sum(c[0] for c in server_charges)
    if total_cost_points <= 0:
        raise HTTPException(status_code=400, detail="费用计算异常，请刷新重试")
    # 观测:客户端报价与服务端权威价不一致时告警(篡改 / 前端缓存过期 / markup 变更)
    _client_total = sum(req.cost_points) if req.cost_points else 0
    if _client_total != total_cost_points:
        logger.warning(f"[A-8-1] 代发扣费口径不一致 user={user_id} client={_client_total} server={total_cost_points} media_ids={req.media_ids} type={req.media_type}")

    # 扣积分（media_proxy_publish: requires_paid_points=True，只扣 paid_points）
    from middleware.billing import deduct_points
    billing_result = await deduct_points(user_id, "media_proxy_publish", extra_cost=total_cost_points)
    _pub_charge_txid = (billing_result or {}).get("charge_tx_id")  # [GEO-R2-CAN-039 v3] 精确退款透传
    logger.info(f"代发扣费: user={user_id}, cost={total_cost_points}, billing={billing_result}")

    # 创建本地订单
    # [WO-ACCEPTANCE-3FIX-2026-08-05 项1] items 里必须带 media_type。
    #   漏这一个键 → `create_order` 的 `it.get("media_type")` 取到 None → 落库 NULL。
    #   生产实证:item 509(mhz_wemedia「梦梦生活」)请求体 media_type='wemedia',落库 (NULL)。
    #   NULL 的代价在**重试链**:`api_confirm_resubmit` 那边 `item.get("media_type") or "mhz"`
    #   会把这条自媒体单当软文重发。批量端点(`api_publish_batch`)一直带着这个键,只有单发漏了。
    #   归一走 `canonical_media_type`:请求侧软文写 'article'(本模型默认值),DB 侧口径是 'mhz'。
    from db.meijiehezi_db import canonical_media_type as _canonical_media_type
    _item_media_type = _canonical_media_type(req.media_type)
    items = []
    for i, mid in enumerate(req.media_ids):
        items.append({
            "media_id": mid,
            "media_name": req.media_names[i] if i < len(req.media_names) else "",
            "cost_points": server_charges[i][0],  # [A-8-1] 服务端权威扣费积分 · 保证扣费==订单==退款一致
            "cost_yuan": server_charges[i][1],     # [F2] 服务端权威外采价(DB price)· 不信客户端 cost_yuan
            "brand_id": req.brand_id,
            "media_type": _item_media_type,
        })
    # [GEO-R6-CAN-011 P2] 扣费(419)先于订单持久化 · 订单创建链任一步失败须退费再抛
    # (单发路径同批量路径根因:扣后异常则用户白扣钱)。
    # [对抗审核订正] _get_canonical_article_title 是 DB 调用可能抛错,必须放进 try 内,
    # 否则它抛错会绕过退费补偿 = 新白扣窗口。
    try:
        # [GEO-R5-CAN-012] 标题以 DB articles.title 为准(与服务端加载的正文同源)· 不信客户端 req.article_title
        _canonical_title = _get_canonical_article_title(req.article_id) or req.article_title
        # [客户反馈② 2026-08-09] 备注随订单落库,给"确认后重投"那一次回读用
        order = create_order(user_id, req.article_id, _canonical_title, items,
                             admin_exempt=bool(billing_result.get("admin_exempt")))  # [BUG-P0-1] 落 admin 免扣标志
        _persist_order_remark(order["order_id"], _normalize_order_remark(req.order_remark))
    except Exception as _create_err:
        try:
            from middleware.billing import refund_points
            from services.notification_events import publication_refund_context
            await refund_points(user_id, "media_proxy_publish",
                                reason=f"代发订单创建失败自动退费: {str(_create_err)[:120]}",
                                charge_tx_id=_pub_charge_txid,
                                notification=publication_refund_context(
                                    f"publication_create:{_pub_charge_txid or 'legacy'}:{int(user_id)}:{int(req.article_id)}"
                                ))  # [GEO-R2-CAN-039 v3] 精确退款透传
            logger.error(f"[R6-CAN-011] 单发 create_order 失败已退费: user={user_id} "
                         f"cost={total_cost_points} err={_create_err}")
        except Exception as _refund_err:
            logger.error(f"[R6-CAN-011] 单发退费也失败(需人工核对): user={user_id} err={_refund_err}")
        raise HTTPException(status_code=500, detail="代发下单失败,已自动退回算力,请稍后重试")

    # 获取文章内容（失败则中止，不发标题充当正文）
    # [2026-06-02 GEO CTO] 同时服务端查 brand_id(articles→quotes)· 配图/联系方式渲染都用它(不信前端 req.brand_id)
    content_md = None
    _trusted_bid = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT a.content, q.brand_id FROM articles a LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s", (req.article_id,))
            row = c.fetchone()
            if row and row["content"]:
                content_md = row["content"]
                _trusted_bid = row.get("brand_id")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"获取文章内容失败: {e}")

    if not content_md:
        # [P0-C] 标记订单失败 + 自动退款（不再让客户自己找客服）
        # [Bug #3 修] 改按 item 退款，refund_key 统一用 item:{id} 格式
        # （之前用 order:{id} 和其他路径不一致，可能造成同一笔钱在不同路径分别退款）
        order_items = get_order_items_by_order(order["order_id"])
        logger.error(f"文章内容为空，中止发布: article_id={req.article_id}, order_id={order['order_id']}")

        from db.meijiehezi_db import refund_for_publish_order
        total_refunded = 0
        for it in order_items:
            update_order_item_status(it["id"], "failed", reject_reason="文章内容为空，无法发布")
            item_cost = int(it.get("cost_points") or 0)
            if item_cost <= 0:
                continue
            try:
                r = refund_for_publish_order(
                    user_id=user_id,
                    amount=item_cost,
                    refund_key=f"item:{it['id']}",
                    reason=f"文章内容获取失败 (article_id={req.article_id})",
                )
                if r.get("success") and not r.get("skipped"):
                    total_refunded += item_cost
                elif r.get("skipped"):
                    logger.info(f"[P0-C] item={it['id']} 已退过(幂等)")
                else:
                    logger.error(f"[P0-C] item={it['id']} 退款失败: {r.get('reason')}")
            except Exception as refund_err:
                logger.error(f"[P0-C] item={it['id']} 退款异常: {refund_err}")

        if total_refunded > 0:
            logger.info(f"[P0-C] 内容空自动退款完成: user={user_id}, order={order['order_id']}, total={total_refunded}pts")

        error_response = {
            "status": "error",
            "detail": "文章内容获取失败，已自动退款到您的账户",
            "refunded": total_refunded,
        }

        # [审计补丁] 内容空 error 也要保存幂等响应
        # 否则用户用同一 request_id 重试 → 又走一遍扣费 → 又走一遍退款（净 0 但产生无意义事务）
        if req.request_id:
            try:
                from db.meijiehezi_db import save_idempotency
                save_idempotency(req.request_id, user_id, "/api/publish", error_response)
            except Exception as _e:
                logger.warning(f"[Idempotency] 内容空 error 保存幂等响应失败: {_e}")

        return error_response

    # 异步提交到外部发布通道（按 media_type 分流，加全局锁防 CSRF 冲突）
    # [Bug #5 修] client 初始化前置 — 失败就不锁 item，让 scheduler 后面重试
    # [Bug #2 修] wemedia 用唯一占位 ID，避免 mhz_order_id 冲突导致 sync 串扰
    async def submit_to_mhz():
        from db.meijiehezi_db import (try_lock_for_submit, release_submit_lock,
                                      check_duplicate_submission, mark_item_failed_duplicate,
                                      refund_for_publish_order,
                                      check_recent_active_for_user_media,
                                      save_mhz_raw_response, set_item_awaiting_sync)
        from services.meijiehezi.client import (
            ConfirmationRequiredError, AmbiguousResponseError, PartialSuccessError,
        )

        async with _mhz_api_lock:
            # 第一步：先验证 client 可用，避免锁了 item 才发现 client 不行
            try:
                client = _get_client()
                await client.refresh_token()
            except Exception as e:
                logger.error(f"[Publish] client 初始化失败，订单等 scheduler 重试: order_id={order['order_id']}, err={e}")
                return  # 不锁 item，scheduler 5min 后会接走

            order_items = get_order_items_by_order(order["order_id"])

            # 第二步：批量拿锁，过滤出真正能提交的 item
            #   - awaiting_confirmation 的 item 不会被锁住（try_lock 只接 pending）
            #   - 所以 awaiting 中的 item 不参与本轮 publish，等用户确认后单独走 resubmit 流程
            locked_items = [it for it in order_items if try_lock_for_submit(it["id"])]
            if not locked_items:
                logger.warning(f"[Publish] order_id={order['order_id']} 所有 item 都拿不到锁（可能已在提交中或待确认），跳过")
                return

            # [Dedup] 本地去重：同文章同媒体已有活跃订单时取消提交并退款
            # [防线 1 加固] 在 check_duplicate_submission（排除自身）之外，
            # 再加 check_recent_active_for_user_media（30 分钟时间窗，排除自身），
            # 兜底拦双击/网络重试/调度器自我重试等场景产生的重复请求。
            _clean = []
            for _it in locked_items:
                _conflict = check_duplicate_submission(req.article_id, _it["media_id"], _it["id"])
                if _conflict is None:
                    _conflict = check_recent_active_for_user_media(
                        user_id=user_id,
                        article_id=req.article_id,
                        media_id=_it["media_id"],
                        window_minutes=30,
                        exclude_item_id=_it["id"],
                    )
                if _conflict:
                    logger.warning(f"[Dedup] item={_it['id']} media_id={_it['media_id']} 已有活跃订单 item={_conflict}，取消并退款")
                    mark_item_failed_duplicate(_it["id"])
                    _cost = int(_it.get("cost_points") or 0)
                    if _cost > 0:
                        refund_for_publish_order(user_id, _cost, f"item:{_it['id']}", "重复投稿：已有同文章同媒体的活跃订单，自动退款")
                else:
                    _clean.append(_it)
            locked_items = _clean
            locked_ids = [it["id"] for it in locked_items]
            if not locked_items:
                logger.warning(f"[Dedup] order_id={order['order_id']} 所有 item 均重复，已退款，跳过 mhz 提交")
                return
            logger.info(f"[Publish] order_id={order['order_id']} 拿到锁的 item: {locked_ids}")

            # 取 item 上累积的 confirm 字段（首次提交是空 dict）
            confirms = _build_confirms_from_items(locked_items)

            # [2026-06-02 GEO CTO] 媒体代发联系方式按渠道软化(取本批所有媒体最严 contact_policy)
            # 🔴 brand_id 服务端可信(_trusted_bid · 不信前端 req.brand_id)· 同时校验配图归属
            # 🔴 [Codex 复审] fail-closed:policy 查询失败默认最严 none;渲染走 safe helper(失败 strip 占位符·绝不发原文)
            from services.contact_placeholder import safe_render_contact_for_publish, strictest_policy
            _is_wm = (req.media_type == "wemedia")

            # [双供应商择优 2026-08-02] 同一媒体两家都有时改道到更便宜的那家。
            #   放在这里(投递前、扣费后)是刻意的:扣费已按用户选中那条媒体的价算完,
            #   所以售价对用户零变化(λ=0 的结构性保证),省下的差价全进毛利。
            #   开关关 / 无可改道条目时 _dispatch_items 与 locked_items 逐位等价。
            from services.kuaiyibo.routing import apply_provider_preference, persist_routing
            _dispatch_items, _routed_meta = apply_provider_preference(
                locked_items, is_wemedia=_is_wm)
            # 🔴 先落库再投递:落库失败就不改道(退回原渠道),绝不允许"发去了 kyb 但
            #   订单表不知道"—— 那种单状态回流永远扫不到,24h 后会被误判未发布而退款。
            if _routed_meta:
                try:
                    persist_routing(_routed_meta)
                except Exception as _e_route:
                    logger.error(f"[routing] 改道落库失败,退回原渠道投递: {_e_route}")
                    _dispatch_items, _routed_meta = locked_items, {}
            # 真拿到对方单号的改道 item。收尾对账用(见下方 finally)。
            # 🔴 必须定义在 try **之外**:异常若发生在 try 开头(比如 import 那几行),
            #    finally 仍会执行,定义在 try 内就是 NameError 把真异常盖掉。
            _routed_submitted: set[int] = set()

            _policy = "none"
            try:
                from db.meijiehezi_db import get_media_contact_policies
                # 策略要覆盖【原媒体 + 改道后媒体】两边,取最严(fail-closed)。
                # 同一个媒体在两家的 contact_policy 可能不同,按严的走才不会把
                # 不允许带联系方式的媒体当成允许。
                _pol_ids = {it["media_id"] for it in locked_items} | {
                    it["media_id"] for it in _dispatch_items}
                _pol_map = get_media_contact_policies(sorted(_pol_ids), is_wemedia=_is_wm)
                _policy = strictest_policy(list(_pol_map.values()))
            except Exception as _e_pol:
                logger.warning(f"[Publish] contact_policy 查询失败 → 默认最严 none: {_e_pol}")
                _policy = "none"
            # [P1-3 媒体形态分档 2026-08-14] 按媒体域名取形态档:编辑档(门户主站/
            # 垂直媒体)把联系方式 policy 压到最严;self_site 追加诚实署名段。
            # O1:目录/域名/形态任一取不到 → _media_forms 空 → 一切与改前逐字一致。
            _media_forms: list = []
            try:
                from db.meijiehezi_db import get_media_source_domains
                from services.media_domain_directory import get_directory_entries
                from services.media_form_adaptation import floor_contact_policy
                _dom_map = get_media_source_domains(sorted(_pol_ids), is_wemedia=_is_wm)
                _dir_entries = get_directory_entries(list(_dom_map.values()))
                _media_forms = [
                    e.get("media_form") for e in _dir_entries.values() if e.get("media_form")
                ]
                _policy = floor_contact_policy(_policy, _media_forms)
            except Exception as _e_form:
                logger.warning(f"[Publish] media_form 分档查询失败 → 按未分档: {_e_form}")
                _media_forms = []
            _pub_md = safe_render_contact_for_publish(content_md, _trusted_bid, channel="media", contact_policy=_policy)
            try:
                from services.media_form_adaptation import media_form_byline
                _byline = media_form_byline(_media_forms, brand_name=_brand_name_for_byline(_trusted_bid))
                if _byline:
                    _pub_md = f"{_pub_md}\n\n{_byline}"
            except Exception as _e_byline:
                logger.warning(f"[Publish] 署名段渲染失败 → 不加署名: {_e_byline}")
            try:
                from services.article_publish_dispatch import dispatch_article_to_provider

                _dispatch_source = f"mhz_channel_submit:{req.media_type}:{_policy}"
                _write_snapshot = lambda snap: set_item_submission_snapshot(
                    locked_ids, title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
                )
                # [多渠道 2026-07-27] 按渠道分组投递。全是媒介盒子时只有一组,
                # 循环体只跑一次 —— 与接入前逐位等价。快易播那组走同签名适配器,
                # 仍然经过上面同一个审核闸(dispatch_article_to_provider)。
                from services.kuaiyibo.routing import (
                    build_provider_kwargs_extras, client_for_provider, split_items_by_provider,
                )

                _is_wemedia = _is_wm
                _kind = "publish_wemedia" if _is_wemedia else "publish"
                _id_field = "toutiao_ids" if _is_wemedia else "media_ids"
                result = None
                # 用改道后的副本分渠道。未改道时它与 locked_items 逐位等价。
                for _provider, _group in split_items_by_provider(_dispatch_items):
                    _group_ids = [it["id"] for it in _group]
                    _client = client_for_provider(_provider, client)
                    _kwargs = {
                        _id_field: [it["media_id"] for it in _group],
                        "confirms": confirms, "brand_id": _trusted_bid,
                    }
                    # [客户反馈② 2026-08-09] 下单备注穿线（空备注 → 不加键，请求体逐位不变）
                    _kwargs.update(_remark_kwarg(_kind, _normalize_order_remark(req.order_remark)))
                    _kwargs.update(build_provider_kwargs_extras(_group, _provider))
                    result = await dispatch_article_to_provider(
                        client=_client, dispatch_kind=_kind, article_id=req.article_id,
                        source_title=_canonical_title, source_content=content_md,
                        outgoing_title=_canonical_title, outgoing_content=_pub_md,
                        source=_dispatch_source,
                        provider_kwargs=_kwargs,
                        snapshot_writer=_write_snapshot,
                    )
                    # [审计] 保存渠道原始响应
                    for item in _group:
                        save_mhz_raw_response(item["id"], result.raw_data)
                    # [2026-04-30] 批量提交时每个 media 的 sn 不一样，按 media_id 各自查
                    sn_map = result.order_sn_map or {}
                    from services.kuaiyibo.config import PROVIDER_KEY as _KYB
                    for item in _group:
                        item_sn = sn_map.get(int(item["media_id"])) or result.order_sn
                        if _provider == _KYB and not sn_map.get(int(item["media_id"])):
                            # 快易播按 item 拿单号,拿不到就是这条没提交成功。
                            # Owner 口径「未发布成功就退款」—— 不能让它挂在那把算力冻住。
                            continue
                        if not update_order_item_submitted(item["id"], item_sn):
                            # 🔴 [WO-PUB-ZOMBIE] 投出去了但这条没拿到单号。
                            #   不能留在 pending(那是永久锁死的僵尸),也不能当场退款
                            #   —— 此刻分不清"对方没收到"还是"对方收了、回执丢了",
                            #   镜像也还没同步到这一单。交给 awaiting_sync:它会实时反查
                            #   回填,反查不到 12h 转人工、72h 开用户出口,且明文拒绝自动退款。
                            set_item_awaiting_sync(
                                item["id"],
                                reason="提交后未取得外部单号,等定时反查回填",
                            )
                            continue
                        if int(item["id"]) in _routed_meta:
                            _routed_submitted.add(int(item["id"]))
                    if _provider == _KYB:
                        from services.kuaiyibo.status_sync import fail_and_refund_unsubmitted
                        _refunded = fail_and_refund_unsubmitted(
                            _group, {int(k) for k in sn_map.keys()})
                        if _refunded:
                            logger.warning(
                                f"[Publish/{_provider}] 提交未成功已退款 {_refunded} 算力 "
                                f"order_id={order['order_id']}")
                    logger.info(
                        f"[Publish/{_provider}] 发文提交完成: order_id={order['order_id']}, "
                        f"primary_sn={result.order_sn}, sn_count={len(sn_map)}, items={_group_ids}")
                if _is_wemedia:
                    return

                # 重发：标记原拒稿订单已重发
                if req.republish_from_sn and result.order_sn:
                    try:
                        from db.connection import get_connection
                        conn2 = get_connection()
                        c2 = conn2.cursor()
                        c2.execute(
                            "UPDATE mhz_synced_orders SET republished_order_sn = %s WHERE order_sn = %s AND status = -1",
                            (result.order_sn, req.republish_from_sn))
                        conn2.commit()
                        conn2.close()
                        logger.info(f"重发标记成功: {req.republish_from_sn} → {result.order_sn}")
                    except Exception as mark_err:
                        logger.warning(f"重发标记失败: {mark_err}")
            except ConfirmationRequiredError as ce:
                # mhz 要求人工确认（203/204/205）→ 把所有 locked_items 标 awaiting_confirmation
                # 不退款、不释放锁回 pending（避免 scheduler 重试再投，绕过 mhz 查重）
                logger.info(
                    f"[Publish] order_id={order['order_id']} 进入待确认: "
                    f"code={ce.code}, field={ce.field}, msg={ce.msg}, items={locked_ids}"
                )
                for item in locked_items:
                    ok = mark_item_awaiting_confirmation(item["id"], ce.code, ce.msg, ce.field)
                    if not ok:
                        # 兜底：标记失败（item 不在 submitting 状态了，被并发改了）→ 走原退款流程
                        release_submit_lock(
                            item["id"], success=False,
                            reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                        )
            except AmbiguousResponseError as ae:
                # [防线 4] mhz 返回 code=200 但 order_sn 为空 → 标 awaiting_sync 等定时反查
                # 严禁释放锁回 pending（旧 bug：那样会让调度器重试，导致 mhz 被重复创建订单）
                logger.warning(
                    f"[Publish] order_id={order['order_id']} mhz 响应模糊: code={ae.code}, raw={ae.raw_data}, items={locked_ids}"
                )
                for item in locked_items:
                    save_mhz_raw_response(item["id"], ae.raw_data)
                    set_item_awaiting_sync(
                        item["id"],
                        reason=f"mhz 响应 code=200 但 order_sn 为空：{ae.msg or '响应已保存到 mhz_raw_response'}",
                    )
            except PartialSuccessError as pe:
                # [2026-04-30] mhz 部分接单（提交 N 个、成功 M 个、M<N）
                # 不能简单全成功也不能全失败 —— 哪几个被接单、哪几个被拒，
                # mhz 没明确告诉我们。统一标 awaiting_sync 让管理员人工核对。
                logger.warning(
                    f"[Publish] order_id={order['order_id']} mhz 部分接单 "
                    f"勾选 {pe.selected_num} 接单 {pe.success_count}, items={locked_ids}"
                )
                for item in locked_items:
                    save_mhz_raw_response(item["id"], pe.raw_data)
                    set_item_awaiting_sync(
                        item["id"],
                        reason=f"mhz 部分接单（勾 {pe.selected_num} 接 {pe.success_count}），需人工核对哪些 item 实际接单",
                    )
                # awaiting_sync 仍是可自动恢复的中间态，不发站内信；超时转
                # manual_review 时由 mark_item_manual_review 事务内写强类型 outbox。
            except Exception as e:
                logger.error(f"外部发布通道发文失败: {e}")
                # [审计] 即使异常也保存原始返回（如果有的话，比如 PublishError 携带的信息）
                _err_payload = {"_error": type(e).__name__, "_message": str(e)[:500]}
                for item in locked_items:
                    save_mhz_raw_response(item["id"], _err_payload)
                # 第四步：异常 → 释放锁回 pending（attempts 已 ++，达上限会自动标 failed）
                for item in locked_items:
                    release_submit_lock(item["id"], success=False, reject_reason=str(e))
            finally:
                # 🔴 路由对账 —— 必须在**每一条**出口上跑,所以放 finally。
                #   `persist_routing` 是在投递【之前】落的库(否则"提交成功但落库前崩"
                #   会丢掉映射,那种单两家回流都认不出来,不可修复)。代价是反向窗口:
                #   **落库了却没投出去**。此时若不撤标记:
                #     · 异常分支把 item 退回 pending → 调度器重试走的是 mhz 专用路径
                #       (全仓 8 处 dispatch 只有这 1 处过路由跳)→ 稿件发去了媒介盒子,
                #       routed_media_id 却还写着快易播 → 快易播回流拿 mhz 单号永远查不到
                #       → 24 小时后判「未发布成功」退款,可稿已经发出去了;
                #     · ConfirmationRequiredError 同型:媒介盒子组排在前面,它一抛,
                #       快易播组压根没投,except 却把 locked_items 全标成待确认。
                #   规则很简单:**只有真拿到对方单号的才留标记,其余一律撤。**
                if _routed_meta:
                    try:
                        from services.kuaiyibo.routing import clear_routing

                        clear_routing(set(_routed_meta) - _routed_submitted)
                    except Exception as _e_clear:
                        # 撤不掉也不能把整个提交流程炸掉;留给 24h 兜底 + 告警。
                        logger.error(f"[routing] 路由对账失败(标记可能残留): {_e_clear}")

    asyncio.create_task(submit_to_mhz())

    response = {
        "status": "success",
        "order_id": order["order_id"],
        "total_items": order["total_items"],
        "message": "订单已创建，正在提交到外部发布通道",
    }

    # [P0-D] 保存幂等响应 — 24 小时内同 request_id 二次请求直接返这个响应
    if req.request_id:
        try:
            from db.meijiehezi_db import save_idempotency
            save_idempotency(req.request_id, user_id, "/api/publish", response)
        except Exception as _e:
            logger.warning(f"[Idempotency] 保存幂等响应失败（不影响主流程）: {_e}")

    return response


# 全局外部发布通道 API 锁：同一时刻只有一个请求在调渠道，避免会话冲突
_mhz_api_lock = asyncio.Lock()


class BatchPublishItem(BaseModel):
    article_id: int
    article_title: str
    media_ids: List[int]
    media_names: List[str]
    cost_points: List[int] = []
    cost_yuan: List[float] = []
    media_types: List[str] = []  # 'mhz' | 'wemedia'，区分软文/自媒体
    # [客户反馈② 2026-08-09] 逐篇的下单备注（非必选）。购物车批量是 PublishCenter
    #   **唯一活着的**代发提交路径（handleMhzProxy/handleWmProxy 早已是死代码、
    #   全文件零引用），所以地区备注必须接在这一条上，接单发那条等于没接。
    order_remark: str = ""


class BatchPublishRequest(BaseModel):
    items: List[BatchPublishItem]
    # [P0-D] 幂等键 — 防双击/网络重试导致重复扣费 + 重复下单
    request_id: Optional[str] = None


@router.post("/orders/{order_id}/re-prepare")
async def api_reprepare_order(order_id: int, request: Request):
    """[P0 内容漂移的出口]「重新准备并审核」。

    漂移把订单挂起后,用户在这里一键走出去:
      1. 用**当前**稿件重新捕获订单快照(不再拿旧快照空转);
      2. 重走文章审核(refresh_article_review) —— 换了内容就必须重新过闸;
      3. 把挂起的 item 放回可提交状态并清零重试计数,等下一轮调度提交。

    审核未通过的不放回 —— 那属于内容本身的问题,不该靠这个按钮绕过去。
    """
    user = _get_user(request)
    user_id = user["user_id"]

    from db.connection import get_connection
    from db.meijiehezi_db import recompute_order_status
    from services.publication_content_drift import (
        ITEM_STATUS_AWAITING_ACTION, content_hash,
    )

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, user_id, article_id
              FROM mhz_publish_orders
             WHERE id = %s
            """,
            (int(order_id),),
        )
        order = c.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        if int(order["user_id"]) != int(user_id) and not user.get("is_admin"):
            # 防枚举:不告诉调用方订单存不存在
            raise HTTPException(status_code=404, detail="订单不存在")
        if not order.get("article_id"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORDER_ARTICLE_MISSING",
                    "message": "这个订单没有关联稿件,无法重新准备。",
                    "reason": "订单缺少 article_id。",
                    "impact": "订单保持原状。",
                    "repair_hint": "请重新下单;若反复出现请联系客服。",
                    "actions": [{"id": "view_orders", "label": "返回发布任务", "type": "nav", "target": "/publish"}],
                    "rule_version": "publish-content-drift-v1",
                },
            )

        article_id = int(order["article_id"])
        c.execute("SELECT content, title FROM articles WHERE id = %s", (article_id,))
        art = c.fetchone()
        current = (art or {}).get("content")
        if not current:
            raise HTTPException(status_code=409, detail="稿件内容为空,无法重新准备")

        # 1) 重新捕获快照 —— 这一步就是"不再空转"的根本
        c.execute(
            """
            UPDATE mhz_publish_orders
               SET article_content_snapshot = %s,
                   article_content_snapshot_hash = %s,
                   article_content_snapshot_at = NOW(),
                   article_content_snapshot_source = 'reprepare',
                   updated_at = NOW()
             WHERE id = %s
            """,
            (current, content_hash(current), int(order_id)),
        )

        # 2) 重走审核:内容换了,之前那次审核结论不能继续用
        review_ok = True
        try:
            from services.article_review_gate import refresh_article_review

            refresh_article_review(article_id, cursor=c)
        except Exception as _rev_err:
            review_ok = False
            logger.warning(f"[re-prepare] 订单 {order_id} 重走审核异常: {_rev_err}")

        # 3) 放回可提交 + 清零重试计数
        c.execute(
            """
            UPDATE mhz_publish_order_items
               SET status = 'pending',
                   submit_attempts = 0,
                   reject_code = NULL,
                   reject_user_message = NULL,
                   reject_contract = NULL,
                   reject_reason = NULL
             WHERE order_id = %s
               AND status = %s
         RETURNING id
            """,
            (int(order_id), ITEM_STATUS_AWAITING_ACTION),
        )
        restored = [r["id"] for r in (c.fetchall() or [])]
        recompute_order_status(int(order_id), cursor=c)
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "order_id": int(order_id),
        "restored_items": len(restored),
        "review_refreshed": review_ok,
        "message": (
            f"已用最新稿件重新准备好 {len(restored)} 条发布任务,系统会自动重新提交。"
            if restored else "没有需要重新准备的发布任务。"
        ),
    }


# ============================================================
# [P0 代发卡单出口 2026-07-26] awaiting_sync 死胡同的两个可执行动作
#
# 合同见 services/publication_awaiting_sync_exit.build_awaiting_sync_exit_contract。
# 两个端点都**不产生退款流水** —— mhz 已回执"成功提交，正在执行发布"，稿件可能
# 真发出去了，自动退款 = 既退钱又发稿。用户点「确认未发布」只是**声明**，
# 真正动钱仍只走管理员的 /admin/manual-review-queue/resolve('mark_failed')，
# 幂等键唯一为 item:{id}（老板 2026-06-08 拍板：禁用户端自助退款入口）。
# ============================================================


class AwaitingSyncNotPublishedRequest(BaseModel):
    note: str = ""


class AwaitingSyncPublishedRequest(BaseModel):
    publish_url: str
    note: str = ""


def _load_exit_item_for_user(item_id: int, user: dict) -> dict:
    """取出口 item 并做归属校验。防枚举：不是自己的一律按不存在处理。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT i.id, i.user_id, i.status, i.reject_code, i.cost_points, i.media_name
              FROM mhz_publish_order_items i
             WHERE i.id = %s
            """,
            (int(item_id),),
        )
        row = c.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    if int(row["user_id"]) != int(user["user_id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="发布任务不存在")
    return dict(row)


@router.get("/pending-user-actions")
async def api_list_pending_user_actions(request: Request, limit: int = 50):
    """当前用户"挂起等你动一下"的发布任务 + §13 合同。

    这是出口的**读**面。此前全仓没有任何用户侧接口 SELECT 过 reject_contract ——
    合同只写不读，用户看不到任何可点的下一步，出口等于不存在。
    """
    user = _get_user(request)
    from db.meijiehezi_db import list_user_pending_action_items

    items = list_user_pending_action_items(int(user["user_id"]), limit=max(1, min(int(limit), 200)))
    return {"status": "success", "items": items, "total": len(items)}


@router.post("/items/{item_id}/report-not-published")
async def api_exit_report_not_published(
    item_id: int, req: AwaitingSyncNotPublishedRequest, request: Request
):
    """[出口动作 1]「确认未发布 · 退还算力」—— 只记录声明，**不动钱**。

    动钱在管理员核实 mhz 后台之后走既有 admin_manual_review_resolve('mark_failed')。
    这条 item 在 open_awaiting_sync_user_exit 时已置 manual_review_required=TRUE，
    因此本次声明会直接出现在管理员的人工审核队列里。
    """
    user = _get_user(request)
    _load_exit_item_for_user(item_id, user)

    from db.meijiehezi_db import record_awaiting_sync_user_claim
    from services.publication_awaiting_sync_exit import USER_CLAIM_NOT_PUBLISHED

    result = record_awaiting_sync_user_claim(
        item_id=int(item_id),
        claim=USER_CLAIM_NOT_PUBLISHED,
        note=(req.note or "").strip()[:500],
    )
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "当前状态无法执行该操作")
    return {"status": "success", **result}


@router.post("/items/{item_id}/record-publish-url")
async def api_exit_record_publish_url(
    item_id: int, req: AwaitingSyncPublishedRequest, request: Request
):
    """[出口动作 2]「已发布 · 补录链接」—— 补录链接并结案，同样不动钱。

    用户选这条等于主动放弃退款，对平台无套利面，所以可以自助闭环。
    """
    user = _get_user(request)
    _load_exit_item_for_user(item_id, user)

    url = (req.publish_url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请填写以 http:// 或 https:// 开头的稿件链接")
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="链接过长")

    from db.meijiehezi_db import record_awaiting_sync_user_claim
    from services.publication_awaiting_sync_exit import USER_CLAIM_PUBLISHED

    result = record_awaiting_sync_user_claim(
        item_id=int(item_id),
        claim=USER_CLAIM_PUBLISHED,
        publish_url=url,
        note=(req.note or "").strip()[:500],
    )
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "当前状态无法执行该操作")
    return {"status": "success", **result}


@router.post("/publish/batch")
async def api_publish_batch(req: BatchPublishRequest, request: Request):
    """购物车批量代发：一次性扣费 → 逐篇串行提交到外部发布通道"""
    user = _get_user(request)
    user_id = user["user_id"]
    _gate = _publish_approval_gate(
        request,
        articles=len(getattr(req, "items", None) or []),
        payload=req.model_dump() if hasattr(req, "model_dump") else {},
    )
    if _gate:
        return _gate
    _batch_charge_txid = None  # [GEO-R2-CAN-039 v3] 精确退款透传：deduct 返回的不可变 charge_tx_id，供 create_order 失败退费精确定位（全路径先绑定）

    # [P0-D] 幂等键检查
    if req.request_id:
        from db.meijiehezi_db import check_idempotency
        cached = check_idempotency(req.request_id, user_id, "/api/publish/batch")
        if cached:
            logger.info(f"[Idempotency] /publish/batch 命中: request_id={req.request_id} user={user_id}")
            return cached

    if not req.items:
        raise HTTPException(status_code=400, detail="购物车为空")

    # [svideo lane · 2026-07-04] 短视频不走购物车批量端点（载荷含 per-order video_url，
    #   本端点 body 无处承载）。fail-fast 拦截：任一 item 含 svideo → 400，绝不扣费、
    #   绝不让 svideo 落进下方软文 catch-all 桶。短视频请走 /short-video/publish。
    from db.meijiehezi_db import MEDIA_TYPE_SVIDEO as _MT_SVIDEO
    for item in req.items:
        if any(mt == _MT_SVIDEO for mt in (item.media_types or [])):
            raise HTTPException(status_code=400, detail="短视频请通过短视频发布入口提交，勿混入批量代发")

    # RBAC: 校验所有文章归属（防伪造他人 article_id 把自己积分扣给别人发文）
    for item in req.items:
        _require_article_access(request, item.article_id)
        _require_article_review_for_publish(item.article_id)
        _require_strict_media_presubmit(
            item.article_id, item.media_ids, item.media_names,
            user_id=user_id, brand_id=(getattr(item, "brand_id", 0) or 0),
        )

    # 扣费前去重检查：检查购物车内**所有**文章+媒体组合
    # [WO-PUB-ZOMBIE R3/R4] 带理由码 + 标题不硬截 + 超出条数明说
    # 🔴 [WO-BATCH-CONFLICT-2026-08-04] 这个循环**必须走完全部 items 才抛** ——
    #   撞到第一个就抛的话,用户剔掉它、重提交、又撞第二个,N 个冲突要来回 N 趟。
    #   前端的"一键剔除全部冲突"能成立,前提就是这里一次给全。
    #   本段整体位于扣费(deduct_points)之前 = 预检零扣费。
    from db.meijiehezi_db import find_active_orders_for_media
    all_dups: list = []
    for item in req.items:
        if item.article_id and item.media_ids:
            dups = find_active_orders_for_media(item.article_id, item.media_ids)
            for d in dups:
                mid = d["media_id"]
                if mid in item.media_ids:
                    all_dups.append(build_conflict_entry(
                        article_id=item.article_id,
                        article_title=item.article_title,
                        media_id=mid,
                        media_name=_media_name_of(mid, item.media_ids, item.media_names),
                        reason_code=d["reason_code"],
                    ))
    if all_dups:
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_ORDER",
            "message": build_duplicate_block_message(all_dups),
            "conflicts": all_dups,
        })

    # 计算总费用
    # [A-8-1 资金安全修 2026-05-29] 服务端逐 item 按 media_ids 重算权威价 · 不信客户端 cost_points
    _markup = _get_publish_markup()
    total_cost = 0
    item_server_charges = []  # 与 req.items 同序 · 每 item 一份 per-media (points, base_yuan)
    for item in req.items:
        if not item.media_ids:
            raise HTTPException(status_code=400, detail=f"文章「{item.article_title[:20]}」未选择媒体")
        specs = [
            (mid, (item.media_types[i] if i < len(item.media_types) else "mhz"))
            for i, mid in enumerate(item.media_ids)
        ]
        charges = _recompute_publish_charge(specs, _markup)
        item_cost = sum(c[0] for c in charges)
        if item_cost <= 0:
            raise HTTPException(status_code=400, detail=f"文章「{item.article_title[:20]}」费用异常")
        item_server_charges.append(charges)
        total_cost += item_cost
    _client_total = sum((sum(it.cost_points) if it.cost_points else 0) for it in req.items)
    if _client_total != total_cost:
        logger.warning(f"[A-8-1] 批量代发扣费口径不一致 user={user_id} client={_client_total} server={total_cost}")

    # 一次性扣费
    from middleware.billing import deduct_points
    billing_result = await deduct_points(user_id, "media_proxy_publish", extra_cost=total_cost)
    _batch_charge_txid = (billing_result or {}).get("charge_tx_id")  # [GEO-R2-CAN-039 v3] 精确退款透传
    logger.info(f"购物车批量扣费: user={user_id}, items={len(req.items)}, cost={total_cost}, billing={billing_result}")

    # 创建所有本地订单
    # [GEO-R6-CAN-011 P2] 扣费(776)先于订单持久化 · 若 create_order 中途 DB 异常,
    # 扣费已消费但订单部分/未创建且原无补偿 → 用户白扣钱(资金链断)。
    # 包 try/except:失败退回本批扣费(refund_points 退最近一笔=本批 total_cost)+
    # 作废已建订单防 scheduler 误提交,再抛错(不吞错、不静默降级)。
    order_ids = []
    try:
        for item_idx, item in enumerate(req.items):
            items_data = []
            _charges = item_server_charges[item_idx]
            for i, mid in enumerate(item.media_ids):
                items_data.append({
                    "media_id": mid,
                    "media_name": item.media_names[i] if i < len(item.media_names) else "",
                    "cost_points": _charges[i][0],  # [A-8-1] 服务端权威扣费积分 · 扣费==订单==退款一致
                    "cost_yuan": _charges[i][1],     # [F2] 服务端权威外采价(DB price)· 不信客户端
                    "media_type": item.media_types[i] if i < len(item.media_types) else None,
                })
            # [GEO-R5-CAN-012] 标题以 DB articles.title 为准 · 不信客户端 item.article_title
            _item_title = _get_canonical_article_title(item.article_id) or item.article_title
            order = create_order(user_id, item.article_id, _item_title, items_data,
                                 admin_exempt=bool(billing_result.get("admin_exempt")))  # [BUG-P0-1] 落 admin 免扣标志
            # [客户反馈② 2026-08-09] 备注随订单落库,给"确认后重投"那一次回读用
            _persist_order_remark(order["order_id"], _normalize_order_remark(item.order_remark))
            order_ids.append(order["order_id"])
    except Exception as _create_err:
        # 退回本批扣费(admin 免扣时 refund 空转无害)
        try:
            from middleware.billing import refund_points
            from services.notification_events import publication_refund_context
            await refund_points(user_id, "media_proxy_publish",
                                reason=f"批量代发订单创建失败自动退费: {str(_create_err)[:120]}",
                                charge_tx_id=_batch_charge_txid,
                                notification=publication_refund_context(
                                    f"publication_batch_create:{_batch_charge_txid or 'legacy'}:{int(user_id)}"
                                ))  # [GEO-R2-CAN-039 v3] 精确退款透传
            logger.error(f"[R6-CAN-011] 批量代发 create_order 失败已退费: user={user_id} "
                         f"cost={total_cost} 已建order={order_ids} err={_create_err}")
        except Exception as _refund_err:
            logger.error(f"[R6-CAN-011] create_order 失败后退费也失败(需人工核对): "
                         f"user={user_id} cost={total_cost} err={_refund_err}")
        # 作废已建订单,防被 scheduler 误提交(已退费,不能再交付)
        for _oid in order_ids:
            try:
                for _oi in get_order_items_by_order(_oid):
                    update_order_item_status(_oi["id"], "failed", reject_reason="批量创建中断已退费")
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="批量代发下单失败,已自动退回算力,请稍后重试")

    # 异步串行提交到外部发布通道（按 media_type 分组：软文走 publish，自媒体走 publish_wemedia）
    async def submit_batch():
        from db.connection import get_connection
        for idx, item in enumerate(req.items):
            # 获取文章内容（失败则跳过该篇，不发标题充当正文）
            content_md = None
            _item_brand_id = None
            # [GEO-R5-CAN-012] 与正文同源读取 DB 权威标题 · 不信客户端 item.article_title
            _item_title = item.article_title
            try:
                conn = get_connection()
                c = conn.cursor()
                c.execute("SELECT a.title, a.content, q.brand_id FROM articles a LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s", (item.article_id,))
                row = c.fetchone()
                conn.close()
                if row and row["content"]:
                    content_md = row["content"]
                    _item_brand_id = row.get("brand_id")
                    if row.get("title"):
                        _item_title = row.get("title")
            except Exception as e:
                logger.error(f"获取文章内容失败: {e}")

            if not content_md:
                logger.error(f"购物车第{idx+1}/{len(req.items)}篇内容为空，跳过并退款: article_id={item.article_id}")
                # [Bug #3 修] 改按 item 退款，refund_key 统一 item:{id}（防跨路径键冲突）
                order_items = get_order_items_by_order(order_ids[idx])
                from db.meijiehezi_db import refund_for_publish_order
                refunded_total = 0
                for oi in order_items:
                    update_order_item_status(oi["id"], "failed", reject_reason="文章内容为空，无法发布")
                    oi_cost = int(oi.get("cost_points") or 0)
                    if oi_cost <= 0:
                        continue
                    r = refund_for_publish_order(
                        user_id=user_id,
                        amount=oi_cost,
                        refund_key=f"item:{oi['id']}",
                        reason=f"批量发布第{idx+1}篇内容为空 (order_id={order_ids[idx]})",
                    )
                    if r.get("success") and not r.get("skipped"):
                        refunded_total += oi_cost
                if refunded_total > 0:
                    logger.info(f"购物车第{idx+1}篇内容为空，按项退款共 {refunded_total} 积分: user={user_id}")
                continue

            # 加锁串行调 MHZ API
            # [Bug #5 修] client 初始化前置 — 失败就跳过这篇，等 scheduler 重试
            # [Bug #4 修] mhz/wemedia 分段独立处理 — 一段失败不影响另一段
            async with _mhz_api_lock:
                # 第一步：先验证 client 可用
                try:
                    client = _get_client()
                    await client.refresh_token()
                except Exception as e:
                    logger.error(f"购物车第{idx+1}/{len(req.items)}篇 client 初始化失败，等 scheduler 重试: {e}")
                    continue  # 跳过当前篇，scheduler 会接走

                from db.meijiehezi_db import (try_lock_for_submit, release_submit_lock, refund_for_publish_order,
                                              check_duplicate_submission, mark_item_failed_duplicate,
                                              check_recent_active_for_user_media,
                                              save_mhz_raw_response, set_item_awaiting_sync)
                # [WO-KYB-ROUTING D1] 分渠道投递三件套。函数内 import,与单发链
                # (submit_to_mhz)同一写法,不动模块顶部 import 块。
                # [D3b] 再加择优改道三件:apply/persist/clear。
                from services.kuaiyibo.routing import (
                    apply_provider_preference, build_provider_kwargs_extras,
                    clear_routing, client_for_provider, persist_routing,
                    split_items_by_provider,
                )

                order_items = get_order_items_by_order(order_ids[idx])

                # 按 media_type 分组并各自拿锁
                # [svideo lane · 2026-07-04] 软文桶显式排除 wemedia 和 svideo（不再用 != wemedia 兜底），
                #   防短视频 item 万一漏进来被当软文发到 /index/article（资金+内容全错）。svideo 正常
                #   已在端点入口 fail-fast 拦截，这里是纵深防御：漏进来的 svideo 既不进软文桶也不进自媒体桶，
                #   保持 pending 不误发（scheduler 侧同样排除，最终由人工/短视频独立链处理）。
                mhz_items = [oi for oi in order_items
                             if (oi.get("media_type") or "mhz") not in ("wemedia", "svideo")
                             and oi["status"] == "pending"
                             and try_lock_for_submit(oi["id"])]
                wm_items = [oi for oi in order_items
                            if oi.get("media_type") == "wemedia"
                            and oi["status"] == "pending"
                            and try_lock_for_submit(oi["id"])]

                from services.meijiehezi.client import ConfirmationRequiredError, AmbiguousResponseError, PartialSuccessError

                # [Dedup] 本地去重：同文章同媒体已有活跃订单时取消提交并退款
                # [防线 1 加固] check_duplicate_submission 之外再加 30 分钟时间窗兜底
                def _dedup_filter(items_list):
                    cleaned = []
                    for _oi in items_list:
                        _conflict = check_duplicate_submission(item.article_id, _oi["media_id"], _oi["id"])
                        if _conflict is None:
                            _conflict = check_recent_active_for_user_media(
                                user_id=user_id,
                                article_id=item.article_id,
                                media_id=_oi["media_id"],
                                window_minutes=30,
                                exclude_item_id=_oi["id"],
                            )
                        if _conflict:
                            logger.warning(f"[Dedup] item={_oi['id']} media_id={_oi['media_id']} 已有活跃订单 item={_conflict}，取消并退款")
                            mark_item_failed_duplicate(_oi["id"])
                            _cost = int(_oi.get("cost_points") or 0)
                            if _cost > 0:
                                refund_for_publish_order(user_id, _cost, f"item:{_oi['id']}", "重复投稿：已有同文章同媒体的活跃订单，自动退款")
                        else:
                            cleaned.append(_oi)
                    return cleaned

                mhz_items = _dedup_filter(mhz_items)
                wm_items = _dedup_filter(wm_items)

                # 段 1: 软文（独立 try/except，失败只影响 mhz_items）
                if mhz_items:
                    # [WO-KYB-ROUTING D1] 按渠道分组投递。全是媒介盒子时
                    # split 只返一组,循环体只跑一次 —— 与接入前逐位等价。
                    # 🔴 try/except 在【循环体内】:一组抛异常不许把另一组
                    #    已拿到单号的 item 也标成待确认/失败。
                    # [D3b] 择优改道:同一媒体两家都有时改到更便宜的那家。放在投递前、扣费后 ——
                    # 扣费已按用户选中那条媒体的价算完,售价对用户零变化,省下的差价进毛利。
                    # 开关关 / 无可改道条目时,_mhzg_dispatch 与 mhz_items 逐位等价。
                    _mhzg_dispatch, _mhzg_routed = apply_provider_preference(mhz_items, is_wemedia=False)
                    if _mhzg_routed:
                        # 🔴 先落库再投递:落库失败就退回原渠道。绝不允许「发去了快易播但订单表
                        #    不知道」—— 那种单回流永远扫不到,24h 后被误判未发布而退款。
                        try:
                            persist_routing(_mhzg_routed)
                        except Exception as _e_route:
                            logger.error('[routing] 批量软文 改道落库失败,退回原渠道: %s', _e_route)
                            _mhzg_dispatch, _mhzg_routed = mhz_items, {}
                    # 🔴 必须定义在 try **之外**:异常若发生在 try 开头,finally 仍会执行,
                    #    定义在 try 内就是 NameError 把真异常盖掉。
                    _mhzg_submitted = set()
                    try:
                        for _mhzg_provider, _mhzg in split_items_by_provider(_mhzg_dispatch):
                            try:
                                # [2026-06-02] 联系方式按渠道软化(mhz 媒体最严 policy)· brand_id 服务端可信
                                # 🔴 [Codex 复审] fail-closed:policy 查询失败默认 none;渲染 safe helper strip(绝不发原文)
                                from services.contact_placeholder import safe_render_contact_for_publish, strictest_policy
                                _mhz_policy = "none"
                                try:
                                    from db.meijiehezi_db import get_media_contact_policies
                                    # [D3b] 策略要覆盖【原媒体 ∪ 改道后媒体】取最严(fail-closed)。
                                    # 同一媒体在两家的 contact_policy 可能不同,只按改道后那家算,
                                    # 会把"不允许带联系方式"的媒体当成允许。
                                    _pol_ids = ({oi["media_id"] for oi in mhz_items}
                                                | {oi["media_id"] for oi in _mhzg})
                                    _pm = get_media_contact_policies(sorted(_pol_ids), is_wemedia=False)
                                    _mhz_policy = strictest_policy(list(_pm.values()))
                                except Exception:
                                    _mhz_policy = "none"
                                _mhz_md = safe_render_contact_for_publish(content_md, _item_brand_id, channel="media", contact_policy=_mhz_policy)
                                from services.article_publish_dispatch import dispatch_article_to_provider
                                _mhz_source = f"mhz_channel_submit:softarticle:{_mhz_policy}"
                                _mhzg_kwargs = {
                                    "media_ids": [oi["media_id"] for oi in _mhzg],
                                    "confirms": _build_confirms_from_items(_mhzg),
                                    "brand_id": _item_brand_id,
                                }
                                # [客户反馈② 2026-08-09] 逐篇备注穿线（软文段 → order_remark）
                                _mhzg_kwargs.update(
                                    _remark_kwarg("publish", _normalize_order_remark(item.order_remark)))
                                # 🔴 快易播适配器靠这三张映射做自记账(provider_spend_ledger);
                                #    不传 = spend_rows 永远 0。mhz 侧返回 {},零影响。
                                _mhzg_kwargs.update(build_provider_kwargs_extras(_mhzg, _mhzg_provider))
                                result = await dispatch_article_to_provider(
                                    client=client_for_provider(_mhzg_provider, client),
                                    dispatch_kind="publish", article_id=item.article_id,
                                    source_title=_item_title, source_content=content_md,
                                    outgoing_title=_item_title, outgoing_content=_mhz_md,
                                    source=_mhz_source,
                                    provider_kwargs=_mhzg_kwargs,
                                    snapshot_writer=lambda snap: set_item_submission_snapshot(
                                        [oi["id"] for oi in _mhzg],
                                        title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
                                    ),
                                )
                                # [审计] 保存 mhz 原始响应
                                for oi in _mhzg:
                                    save_mhz_raw_response(oi["id"], result.raw_data)
                                # [2026-04-30] 按 media_id 各自取 sn（批量场景每个 media 一个独立 sn）
                                # [WO-KYB-ROUTING D1] 领单号走共用件:媒介盒子用主 sn 兜底,
                                #   快易播必须按 item 拿到自己的单号,拿不到即"没提交成功"→ 退款。
                                sn_map = result.order_sn_map or {}
                                _mhzg_submitted |= settle_group_order_sns(
                                    _mhzg, result, _mhzg_provider,
                                    awaiting_reason="批量提交后未取得外部单号,等定时反查回填",
                                )
                                logger.info(f"购物车第{idx+1}/{len(req.items)}篇软文提交成功: provider={_mhzg_provider}, primary_sn={result.order_sn}, sn_count={len(sn_map)}")
                            except ConfirmationRequiredError as ce:
                                logger.info(
                                    f"购物车第{idx+1}/{len(req.items)}篇软文进入待确认: "
                                    f"code={ce.code}, field={ce.field}, msg={ce.msg}"
                                )
                                for oi in _mhzg:
                                    ok = mark_item_awaiting_confirmation(oi["id"], ce.code, ce.msg, ce.field)
                                    if not ok:
                                        release_submit_lock(
                                            oi["id"], success=False,
                                            reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                                        )
                            except AmbiguousResponseError as ae:
                                # [防线 4] code=200 + 空 sn → 标 awaiting_sync 不重试
                                logger.warning(f"购物车第{idx+1}/{len(req.items)}篇软文响应模糊: raw={ae.raw_data}")
                                for oi in _mhzg:
                                    save_mhz_raw_response(oi["id"], ae.raw_data)
                                    set_item_awaiting_sync(
                                        oi["id"],
                                        reason=f"批量提交 mhz 响应 code=200 但 order_sn 为空：{ae.msg or ''}",
                                    )
                            except PartialSuccessError as pe:
                                logger.warning(
                                    f"购物车第{idx+1}/{len(req.items)}篇软文部分接单 "
                                    f"勾 {pe.selected_num} 接 {pe.success_count}"
                                )
                                for oi in _mhzg:
                                    save_mhz_raw_response(oi["id"], pe.raw_data)
                                    set_item_awaiting_sync(
                                        oi["id"],
                                        reason=f"软文部分接单（勾 {pe.selected_num} 接 {pe.success_count}）需人工核对",
                                    )
                                # 中间态不发站内信；超时进入人工终态时统一通知。
                            except Exception as e:
                                # 软文段失败：释放锁回 pending（attempts++，3 次后会标 failed 并自动按 item 退款）
                                logger.error(f"购物车第{idx+1}/{len(req.items)}篇软文提交失败: {e}")
                                _err_payload = {"_error": type(e).__name__, "_message": str(e)[:500]}
                                for oi in _mhzg:
                                    save_mhz_raw_response(oi["id"], _err_payload)
                                    release_submit_lock(oi["id"], success=False, reject_reason=f"软文提交失败: {str(e)[:200]}")

                    finally:
                        # 🔴 放 finally 不放循环后面:调度器那两段的 except 有 `else: raise`,
                        #    异常会穿出循环,写在循环后面就跑不到,改道标记会留在没投出的单上。
                        if _mhzg_routed:
                            try:
                                clear_routing(set(_mhzg_routed) - _mhzg_submitted)
                            except Exception as _e_clr:
                                logger.error('[routing] 批量软文 撤销未投出标记失败: %s', _e_clr)
                # 段 2: 自媒体（独立 try/except，不被段 1 影响）
                # [2026-04-30] 自媒体也走反查真 sn 路径（不再用 wemedia:占位符）
                if wm_items:
                    # [WO-KYB-ROUTING D1] 按渠道分组投递。全是媒介盒子时
                    # split 只返一组,循环体只跑一次 —— 与接入前逐位等价。
                    # 🔴 try/except 在【循环体内】:一组抛异常不许把另一组
                    #    已拿到单号的 item 也标成待确认/失败。
                    # [D3b] 择优改道:同一媒体两家都有时改到更便宜的那家。放在投递前、扣费后 ——
                    # 扣费已按用户选中那条媒体的价算完,售价对用户零变化,省下的差价进毛利。
                    # 开关关 / 无可改道条目时,_wmg_dispatch 与 wm_items 逐位等价。
                    _wmg_dispatch, _wmg_routed = apply_provider_preference(wm_items, is_wemedia=True)
                    if _wmg_routed:
                        # 🔴 先落库再投递:落库失败就退回原渠道。绝不允许「发去了快易播但订单表
                        #    不知道」—— 那种单回流永远扫不到,24h 后被误判未发布而退款。
                        try:
                            persist_routing(_wmg_routed)
                        except Exception as _e_route:
                            logger.error('[routing] 批量自媒体 改道落库失败,退回原渠道: %s', _e_route)
                            _wmg_dispatch, _wmg_routed = wm_items, {}
                    # 🔴 必须定义在 try **之外**:异常若发生在 try 开头,finally 仍会执行,
                    #    定义在 try 内就是 NameError 把真异常盖掉。
                    _wmg_submitted = set()
                    try:
                        for _wmg_provider, _wmg in split_items_by_provider(_wmg_dispatch):
                            try:
                                # [2026-06-02] 联系方式按渠道软化(自媒体最严 policy)· brand_id 服务端可信
                                # 🔴 [Codex 复审] fail-closed:policy 查询失败默认 none;渲染 safe helper strip(绝不发原文)
                                from services.contact_placeholder import safe_render_contact_for_publish, strictest_policy
                                _wm_policy = "none"
                                try:
                                    from db.meijiehezi_db import get_media_contact_policies
                                    # [D3b] 同软文段:原媒体 ∪ 改道后媒体,取最严。
                                    _pol_ids_w = ({oi["media_id"] for oi in wm_items}
                                                  | {oi["media_id"] for oi in _wmg})
                                    _pmw = get_media_contact_policies(sorted(_pol_ids_w), is_wemedia=True)
                                    _wm_policy = strictest_policy(list(_pmw.values()))
                                except Exception:
                                    _wm_policy = "none"
                                _wm_md = safe_render_contact_for_publish(content_md, _item_brand_id, channel="media", contact_policy=_wm_policy)
                                from services.article_publish_dispatch import dispatch_article_to_provider
                                _wm_source = f"mhz_channel_submit:wemedia:{_wm_policy}"
                                _wmg_kwargs = {
                                    "toutiao_ids": [oi["media_id"] for oi in _wmg],
                                    "confirms": _build_confirms_from_items(_wmg),
                                    "brand_id": _item_brand_id,
                                }
                                # [客户反馈② 2026-08-09] 逐篇备注穿线（自媒体段 → article_remark）
                                _wmg_kwargs.update(
                                    _remark_kwarg("publish_wemedia", _normalize_order_remark(item.order_remark)))
                                _wmg_kwargs.update(build_provider_kwargs_extras(_wmg, _wmg_provider))
                                wm_result = await dispatch_article_to_provider(
                                    client=client_for_provider(_wmg_provider, client),
                                    dispatch_kind="publish_wemedia", article_id=item.article_id,
                                    source_title=_item_title, source_content=content_md,
                                    outgoing_title=_item_title, outgoing_content=_wm_md,
                                    source=_wm_source,
                                    provider_kwargs=_wmg_kwargs,
                                    snapshot_writer=lambda snap: set_item_submission_snapshot(
                                        [oi["id"] for oi in _wmg],
                                        title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
                                    ),
                                )
                                wm_sn_map = wm_result.order_sn_map or {}
                                for oi in _wmg:
                                    save_mhz_raw_response(oi["id"], wm_result.raw_data)
                                # [WO-KYB-ROUTING D1] 同软文段,领单号走共用件。
                                _wmg_submitted |= settle_group_order_sns(
                                    _wmg, wm_result, _wmg_provider,
                                    awaiting_reason="批量自媒体提交后未取得外部单号,等定时反查回填",
                                )
                                logger.info(f"购物车第{idx+1}/{len(req.items)}篇自媒体提交成功: provider={_wmg_provider}, primary_sn={wm_result.order_sn}, sn_count={len(wm_sn_map)}")
                            except ConfirmationRequiredError as ce:
                                logger.info(
                                    f"购物车第{idx+1}/{len(req.items)}篇自媒体进入待确认: "
                                    f"code={ce.code}, field={ce.field}, msg={ce.msg}"
                                )
                                for oi in _wmg:
                                    ok = mark_item_awaiting_confirmation(oi["id"], ce.code, ce.msg, ce.field)
                                    if not ok:
                                        release_submit_lock(
                                            oi["id"], success=False,
                                            reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                                        )
                            except AmbiguousResponseError as ae:
                                # [防线 4] mhz 接单了但反查列表 3 次都没命中 → 标 awaiting_sync 兜底
                                logger.warning(f"购物车第{idx+1}/{len(req.items)}篇自媒体响应模糊: raw={ae.raw_data}")
                                for oi in _wmg:
                                    save_mhz_raw_response(oi["id"], ae.raw_data)
                                    set_item_awaiting_sync(
                                        oi["id"],
                                        reason=f"批量自媒体提交反查未命中：{ae.msg or ''}",
                                    )
                            except PartialSuccessError as pe:
                                logger.warning(
                                    f"购物车第{idx+1}/{len(req.items)}篇自媒体部分接单 "
                                    f"勾 {pe.selected_num} 接 {pe.success_count}"
                                )
                                for oi in _wmg:
                                    save_mhz_raw_response(oi["id"], pe.raw_data)
                                    set_item_awaiting_sync(
                                        oi["id"],
                                        reason=f"自媒体部分接单（勾 {pe.selected_num} 接 {pe.success_count}）需人工核对",
                                    )
                                # 中间态不发站内信；超时进入人工终态时统一通知。
                            except Exception as e:
                                logger.error(f"购物车第{idx+1}/{len(req.items)}篇自媒体提交失败: {e}")
                                _err_payload = {"_error": type(e).__name__, "_message": str(e)[:500]}
                                for oi in _wmg:
                                    save_mhz_raw_response(oi["id"], _err_payload)
                                    release_submit_lock(oi["id"], success=False, reject_reason=f"自媒体提交失败: {str(e)[:200]}")

                    finally:
                        # 🔴 放 finally 不放循环后面:调度器那两段的 except 有 `else: raise`,
                        #    异常会穿出循环,写在循环后面就跑不到,改道标记会留在没投出的单上。
                        if _wmg_routed:
                            try:
                                clear_routing(set(_wmg_routed) - _wmg_submitted)
                            except Exception as _e_clr:
                                logger.error('[routing] 批量自媒体 撤销未投出标记失败: %s', _e_clr)
    asyncio.create_task(submit_batch())

    total_media = sum(len(item.media_ids) for item in req.items)
    response = {
        "status": "success",
        "total_articles": len(req.items),
        "total_media": total_media,
        "total_cost": total_cost,
        "message": f"已提交 {len(req.items)} 篇文章 · {total_media} 个媒体",
    }

    # [P0-D] 保存幂等响应
    if req.request_id:
        try:
            from db.meijiehezi_db import save_idempotency
            save_idempotency(req.request_id, user_id, "/api/publish/batch", response)
        except Exception as _e:
            logger.warning(f"[Idempotency] 保存幂等响应失败: {_e}")

    return response


# ══════════════════════════════════════════════════════════════════
# 短视频发布（svideo lane · 2026-07-04）· 独立入口，不走软文/自媒体的 /publish
#   面向用户全程大姐话术：不露供应商/技术枚举。总闸 SVIDEO_PUBLISH_ENABLED 关闭时
#   只读能力(列表/算价)照常，真实发布/上传友好提示"即将开放"，不扣费不下单。
# ══════════════════════════════════════════════════════════════════

class ShortVideoPublishRequest(BaseModel):
    title: str
    content: str = ""              # 内容描述
    keyword: str = ""              # 话题关键词
    media_ids: List[int]           # 短视频账号 id
    media_names: List[str] = []
    # [T5 · 2026-07-30] 视频直发(article_type=1)必填 video_url；图文笔记(=3)必填 image_urls
    #   且 video_url 必须为空。默认 1 → 老前端/老调用方零感知。
    video_url: str = ""            # 前端直传后拿到的视频地址
    image_urls: List[str] = []     # 图文笔记的图片地址（落到接口是英文逗号拼接）
    article_type: int = 1          # 1 视频直发 / 3 图文笔记（2 原创寄拍不在开闸范围，显式拒）
    cover_image: str = ""
    customer_name: str = ""        # 所属客户（展示用）
    remark: str = ""
    brand_id: Optional[int] = None
    # 🔴 [2026-08-10 P1] 内容**来源标识**。此前提交体里没有任何来源字段,
    #    于是服务端想校验归属都无从查起 —— 只能全信客户端传的 brand_id,
    #    而它恰恰是被前端兜底逻辑取错的那个值(内容属 A,记到了 B 名下)。
    #    可选:老前端/自己上传视频的场景不带,此时退回客户端值(仍过 RBAC)。
    geo_post_id: Optional[int] = None
    cost_points: List[int] = []    # 观测用，不作扣费依据
    cost_yuan: List[float] = []    # 观测用
    request_id: Optional[str] = None


def _svideo_allowed_hosts() -> tuple:
    """白名单 SSOT 在 services/meijiehezi/config.py(仅依赖 os)。
    正常环境直接 import；无三方依赖的纯逻辑测试环境下，包 __init__ 会连带拉起
    client→markdown 导致 import 失败，此时按文件路径独立加载 config——
    保证白名单永远只有 config 一份，不出现第二份硬编码。"""
    try:
        from services.meijiehezi.config import SVIDEO_ALLOWED_MEDIA_HOSTS
        return tuple(SVIDEO_ALLOWED_MEDIA_HOSTS)
    except Exception:
        import importlib.util
        import os as _os
        _p = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "services", "meijiehezi", "config.py"))
        _spec = importlib.util.spec_from_file_location("_mhz_config_standalone", _p)
        _m = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
        return tuple(_m.SVIDEO_ALLOWED_MEDIA_HOSTS)


def _is_allowed_svideo_media_url(url: str) -> bool:
    """[Codex 复审 2026-07-04] 视频/封面地址只认发布平台存储域白名单。

    防开闸后绕过上传流程直接传任意 URL 扣费下单(方案§6.3 本就要求域名白名单)。
    完整根治=发布绑定服务端签发的上传凭据/draft(上传链补实时做)，本函数是第一道兜底。
    """
    from urllib.parse import urlparse
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    return p.hostname.lower() in _svideo_allowed_hosts()


async def _submit_short_video_order(user_id, order_id, title, content, keyword,
                                    video_url, cover_image, customer_name, remark,
                                    article_type: int = 1, image_urls: str = "",
                                    *, raise_on_ambiguity: bool = False):
    """提交短视频订单到发布平台（总闸 ON 才走到）。单次同步提交 + awaiting_sync 兜底；
    失败按 item 退款。短视频 v1 不做多轮自动重试（等 ordernum/回流样本确认后升级）。

    🔴 [第 5 棒 · Codex R5 P0-1] `raise_on_ambiguity`(**默认 False = 旧链行为
       逐字节不变**):兜底 `except Exception` 会把 **POST 已发出之后**的
       网络/超时异常(`httpx.ReadTimeout` 那一类)吞成
       `{submitted: 0, failed: n}` —— 也就是把「结果未知」讲成「确定没接单」。

       对老链那是既有口径(它自己退款);但对新链(`freeze_per_item`)后果是
       **确定失败 ⇒ release**:钱退了、item 标 failed,而供应商那边**可能真的
       已经接单** —— 白送一次投放,且再也没人去查。

       所以新链调用时传 `True`:兜底异常**原样抛给调用方**,由调用方按
       `unknown` 处置(保持 frozen + manual + 交给 reconciler 查证)。
       明确语义的三类异常(内容需改 / 歧义回执 / 部分接单)**不受影响** ——
       它们本来就各有确定的处置。

    🔴 抛之前**不做** `_fail_refund`:那会既标 failed 又走老退款,
       与调用方的 `unknown` 处置自相矛盾(而且是资金面的矛盾)。
    """
    from db.meijiehezi_db import (try_lock_for_submit,
                                  update_order_item_submitted, update_order_item_status,
                                  set_item_awaiting_sync, refund_for_publish_order,
                                  save_mhz_raw_response)
    from services.meijiehezi.client import (ConfirmationRequiredError, AmbiguousResponseError,
                                            PartialSuccessError)
    order_items = get_order_items_by_order(order_id)
    async with _mhz_api_lock:
        locked = [oi for oi in order_items if oi["status"] == "pending" and try_lock_for_submit(oi["id"])]
        if not locked:
            return {"submitted": 0}
        media_ids = [oi["media_id"] for oi in locked]

        def _fail_refund(exc, reason):
            refunded = 0
            for oi in locked:
                save_mhz_raw_response(oi["id"], {"_error": type(exc).__name__, "_message": str(exc)[:500]})
                update_order_item_status(oi["id"], "failed", reject_reason=reason[:200])
                _cost = int(oi.get("cost_points") or 0)
                if _cost > 0:
                    r = refund_for_publish_order(user_id, _cost, f"item:{oi['id']}", f"短视频提交失败自动退款 (order_id={order_id})")
                    if r.get("success") and not r.get("skipped"):
                        refunded += _cost
            return refunded

        try:
            client = _get_client()
            result = await client.publish_short_video(
                title=title, content=content, media_ids=media_ids,
                keyword=keyword, remark=remark, customer_name=customer_name,
                video_url=video_url, cover_image=cover_image,
                image_urls=image_urls, article_type=article_type,
            )
            for oi in locked:
                save_mhz_raw_response(oi["id"], result.raw_data)

            # ── [T3 · 资金级 · 2026-07-30] 落库前的第二道：接单数必须覆盖本次全部 item ──
            # 🔴 client 里已有一道 success_count<=0 的挡；这里是**独立第二道**，
            #   防的是"成功路径完全不看 success_count"这个老形态：一旦远端返
            #   code=200 但接单数不足，老代码会把所有 item 标 submitted → 钱已扣、单未接、不退款。
            #   两道都在，任一道被拿掉另一道仍须挡住全失败被标记 submitted。
            # 不足时**不标记 submitted**，转 awaiting_sync 挂人工核对(不自动退也不自动重投：
            #   远端可能已接了一部分，盲退会退掉真发出去的单)。
            _sc = int(getattr(result, "success_count", 0) or 0)
            if _sc <= 0:
                # 全失败：与 client 那道**同结局**(全额退款 + 标 failed)。
                #   两道各自独立能挡住 → 单去一道，另一道仍兜住"扣钱不接单不退款"。
                logger.error(
                    "[svideo成功数不足] order=%s user=%s 提交 %d 条、接单 0 条 → 全额退款",
                    order_id, user_id, len(locked),
                )
                _refunded = _fail_refund(
                    RuntimeError(f"svideo success_count=0 (requested={len(locked)})"),
                    "这次没发出去，费用已原路退回",
                )
                return {"submitted": 0, "failed": len(locked), "refunded": _refunded}
            if _sc < len(locked):
                # 部分接单：不标 submitted、也不盲退(远端可能真接了一部分，盲退会退掉已发出去的单)，
                #   转人工核对 —— 与 PartialSuccessError 分支同结局。
                logger.error(
                    "[svideo成功数不足] order=%s user=%s 提交 %d 条、接单 %d 条 → 不标记已提交，转人工核对",
                    order_id, user_id, len(locked), _sc,
                )
                for oi in locked:
                    set_item_awaiting_sync(
                        oi["id"],
                        reason=f"发布平台接单数不足（提交 {len(locked)} 条、接单 {_sc} 条）需人工核对",
                    )
                return {"submitted": len(locked), "pending_sync": len(locked)}

            sn_map = result.order_sn_map or {}
            for oi in locked:
                item_sn = sn_map.get(int(oi["media_id"])) or result.order_sn
                if not update_order_item_submitted(oi["id"], item_sn):
                    # 🔴 [WO-PUB-ZOMBIE] 接单数够但这条没拿到单号 —— 与上面
                    #   「部分接单」同结局:转 awaiting_sync 人工核对,不留 pending、不盲退。
                    set_item_awaiting_sync(
                        oi["id"],
                        reason="短视频提交后未取得订单号,等定时反查回填",
                    )
            return {"submitted": len(locked)}
        except AmbiguousResponseError as ae:
            for oi in locked:
                save_mhz_raw_response(oi["id"], ae.raw_data)
                set_item_awaiting_sync(oi["id"], reason=f"短视频下单成功但暂未拿到订单号：{ae.msg or ''}")
            return {"submitted": len(locked), "pending_sync": len(locked)}
        except PartialSuccessError as pe:
            for oi in locked:
                save_mhz_raw_response(oi["id"], pe.raw_data)
                set_item_awaiting_sync(oi["id"], reason=f"短视频部分接单（勾 {pe.selected_num} 接 {pe.success_count}）需人工核对")
            return {"submitted": len(locked), "pending_sync": len(locked)}
        except ConfirmationRequiredError as ce:
            refunded = _fail_refund(ce, f"内容或标题需调整后再发（{ce.msg or ''}）")
            return {"submitted": 0, "failed": len(locked), "refunded": refunded, "need_edit": True}
        except Exception as e:
            if raise_on_ambiguity:
                # 🔴 [第 5 棒 · P0-1] POST 之后的异常 = **结果未知**,不是确定失败。
                #    原样抛,不标 failed、不退款 —— 调用方按 unknown 处置。
                logger.error(
                    "[svideo] 提交异常且调用方要求保留歧义 order=%s: %s: %s",
                    order_id, type(e).__name__, e)
                raise
            logger.error(f"[svideo] 提交失败 order={order_id}: {e}")
            refunded = _fail_refund(e, f"短视频提交失败: {str(e)[:200]}")
            return {"submitted": 0, "failed": len(locked), "refunded": refunded}


#: [第 4 棒 · P0-7 · Owner 2026-08-19 定案] legacy 短视频入口的**向前闸**作用域。
#:
#: 🔴 Owner 2026-08-18 原裁:「legacy `/short-video/publish` 加**向前闸**:新调用
#:    必须带非空 `geo_post_id`(存量数据与既有在途单不动,不做回溯修改)」。
#: 🔴 Owner 2026-08-19 **定案收窄**:「向前闸**只封图文 lane**:
#:    `LEGACY_SVIDEO_FORWARD_GATE_ARTICLE_TYPES = {3}`。手动上传视频直发
#:    (`geo_post_id: null`)不受闸,**现役路径零变化**。」
#:
#: 🔴 闸的作用:图文笔记没有 `geo_post_id` 时,内容来源是**客户端自述**的 ——
#:    服务端无从反查权威归属,也没有任何 managed revision / 广告法闸 / 身份链
#:    盖过它。带上之后 `resolve_authoritative_brand_id` 才有东西可查。
#:    视频直发不在图文合同链的治理面内,因此不在闸内。
#:
#: 🔴 收窄的代价必须有对照判据顶着,否则「只封图文」会退化成「谁都不封」:
#:    · `article_type=3` 且无 `geo_post_id` ⇒ **必拒**;
#:    · `article_type≠3` 且 `geo_post_id` 为空 ⇒ **必放行**(视频直发回归对照)。
#:    两条都在 `tests/geo_image_note_2026_08_17/test_r3_p07_fences_pg16.py`。
LEGACY_SVIDEO_FORWARD_GATE_ARTICLE_TYPES = {3}


@router.post("/short-video/publish")
async def api_publish_short_video(req: ShortVideoPublishRequest, request: Request):
    """短视频直发：上传视频拿到地址 → 选账号 → 服务端算价扣费 → 下单。独立入口。"""
    user = _get_user(request)
    user_id = user["user_id"]
    _svideo_charge_txid = None  # [GEO-R2-CAN-039 v3] 精确退款透传：deduct 返回的不可变 charge_tx_id，供 create_order 失败退费精确定位（全路径先绑定）

    from services.meijiehezi.config import SVIDEO_PUBLISH_ENABLED
    if not SVIDEO_PUBLISH_ENABLED:
        # 总闸关闭：友好提示，绝不扣费/下单
        return {"status": "coming_soon", "message": "短视频发布即将开放，敬请期待"}

    if req.request_id:
        from db.meijiehezi_db import check_idempotency
        cached = check_idempotency(req.request_id, user_id, "/api/short-video/publish")
        if cached:
            logger.info(f"[Idempotency] /short-video/publish 命中: request_id={req.request_id} user={user_id}")
            return cached

    # ── [第 4 棒 · P0-7] 向前闸:新调用必须带非空 geo_post_id ────────────
    #
    # 🔴 位置刻意放在**幂等命中之后**:命中缓存的那一路是"既有在途单"的重放,
    #    Owner 裁决明确"存量与在途不动",所以它必须先返回、不进这道闸。
    # 🔴 放在**扣费与下单之前**:闸拦下的调用必须是**零副作用**的。
    # 🔴 `_gate_scope is None` 这半句**保留**:它是"全封"这个取值的语义,
    #    Owner 若哪天改回全封,只需改常量。当前定案是 `{3}`,走的是右半句。
    _gate_scope = LEGACY_SVIDEO_FORWARD_GATE_ARTICLE_TYPES
    if _gate_scope is None or req.article_type in _gate_scope:
        if not str(getattr(req, "geo_post_id", "") or "").strip():
            raise HTTPException(status_code=400, detail={
                "code": "GEO_POST_ID_REQUIRED",
                "message": "现在发布必须从做好的内容发起：请先在创作中心选一条内容，再来发布",
            })
        # 🔴 [第 5 棒 · Codex R5 P0-3] **非空 `geo_post_id` 不是身份凭证**。
        #
        #    带上它并不改变这个端点的形状:标题 / 正文 / 图片地址 / 账号清单
        #    仍然全部来自**客户端**,服务端还会照着这些内容**重铸一张草稿**。
        #    也就是说,一个"看起来来自 managed 作品"的提交,发出去的可以是
        #    完全不同的另一份内容 —— managed 链那一整套(冻结版本 / 素材身份
        #    三方核对 / 外发前广告法闸 / 逐项冻结结算)一条都盖不到它身上。
        #    上一棒把它当成"来源可查"就收了闸,是**认错了凭证**。
        #
        #    规格 §7.3(不接受客户端权威 brand/title/body/images/price)
        #    + 兼容裁定 809 第一选项:图文 lane 的 legacy 入口**直接拒绝**,
        #    指向新 command(`/api/meijiehezi/image-notes/publish-batch`,
        #    前端当时由 `ImageNotePanel` 接通;该面板 09-08 随 #150 §4 删除,
        #    现由 `frontend/src/lib/imageNoteApi.ts` 的 submitPublish 调)。
        #
        # 🔴 作用域仍严格是 `_gate_scope`:视频直发(含 `geo_post_id: null`)
        #    **零变化** —— Owner 2026-08-19 定案的那一半由回归对照判据顶着。
        raise HTTPException(status_code=400, detail={
            "code": "IMAGE_NOTE_LEGACY_ENTRY_CLOSED",
            "message": "图文笔记请在「图文发布」里发：那条链会按你确认过的那一版原样发出去，"
                       "这个老入口发的是这次提交带来的内容，两者可能不是同一份",
            "use_endpoint": "/api/meijiehezi/image-notes/publish-batch",
        })

    if not req.media_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个短视频账号")

    # ── [T5 · 2026-07-30] 发布方式：1 视频直发 / 3 图文笔记；2(原创寄拍)不在开闸范围显式拒 ──
    from services.meijiehezi.config import (SVIDEO_ALLOWED_ARTICLE_TYPES,
                                            SVIDEO_ARTICLE_TYPE_IMAGE,
                                            SVIDEO_TITLE_HARD_LIMIT)
    if req.article_type not in SVIDEO_ALLOWED_ARTICLE_TYPES:
        raise HTTPException(status_code=400, detail="这种发布方式暂时还不支持")
    _is_image_mode = req.article_type == SVIDEO_ARTICLE_TYPE_IMAGE
    _image_urls = [u.strip() for u in (req.image_urls or []) if (u or "").strip()]

    # 标题 45 字是发布平台硬上限(超了远端直接报错)，30 字只是成功率建议 → 这里只拦硬上限
    if len(req.title or "") > SVIDEO_TITLE_HARD_LIMIT:
        raise HTTPException(status_code=400,
                            detail=f"标题太长了，最多 {SVIDEO_TITLE_HARD_LIMIT} 个字")

    if _is_image_mode:
        if not _image_urls:
            raise HTTPException(status_code=400, detail="请先上传图片")
        if (req.video_url or "").strip():
            raise HTTPException(status_code=400, detail="图文笔记不用传视频，请去掉视频后再发")
        # 图文的每张图也必须过白名单(与视频/封面同一道)
        for _u in _image_urls:
            if not _is_allowed_svideo_media_url(_u):
                raise HTTPException(status_code=400, detail="图片地址无效，请重新上传图片")
    else:
        if not (req.video_url or "").strip():
            raise HTTPException(status_code=400, detail="请先上传视频")
        if _image_urls:
            raise HTTPException(status_code=400, detail="视频直发不用传图片，请去掉图片后再发")
        # [Codex 复审] 视频/封面地址域名白名单：拒绝非发布平台存储域的裸 URL(防绕过上传扣费下单)
        if not _is_allowed_svideo_media_url(req.video_url):
            raise HTTPException(status_code=400, detail="视频地址无效，请重新上传视频")
    if (req.cover_image or "").strip() and not _is_allowed_svideo_media_url(req.cover_image):
        raise HTTPException(status_code=400, detail="封面地址无效，请重新上传封面")
    from db.meijiehezi_db import (MEDIA_TYPE_SVIDEO, create_short_video_draft,
                                  find_recent_svideo_duplicates,
                                  resolve_authoritative_brand_id)

    # 🔴🔴 [2026-08-10 P1] **归属由服务端定,不由客户端定。**
    #
    #    原来这里只有一句 `require_brand_access(req.brand_id)` —— 那问的是
    #    「你对这个品牌有没有权限」,**从来没问过「这份内容是不是这个品牌的」**。
    #    于是前端兜底取了项目列表第一个品牌,内容就被记到别的真实客户名下,
    #    服务端全程不吭声(生产实证:svideo 3 条里 2 条错记,受害双方分属不同服务商)。
    #
    #    与同一函数里的 `_recompute_publish_charge` 同形状:服务端自己算权威值 →
    #    与客户端不一致 → **以服务端为准** + WARNING。价格早就不信客户端了,归属也一样。
    _auth_bid, _auth_basis = resolve_authoritative_brand_id(geo_post_id=req.geo_post_id)
    if _auth_bid and req.brand_id and int(req.brand_id) != int(_auth_bid):
        logger.warning(
            "[svideo] 归属不一致 user=%s 客户端 brand_id=%s ≠ 服务端权威 %s(依据 %s),"
            "以服务端为准 geo_post_id=%s",
            user_id, req.brand_id, _auth_bid, _auth_basis, req.geo_post_id)
    effective_brand_id = _auth_bid or req.brand_id

    # 🔴 RBAC 必须打在**最终生效的那个品牌**上,不是客户端传的那个 ——
    #    否则伪造一个别人的 geo_post_id 就能把内容记到别人名下(权威值反而成了越权通道)。
    if effective_brand_id:
        require_brand_access(request, effective_brand_id)

    # [Codex 复审] 短视频专用重复提交拦截(扣费前)：同视频+同账号 30 分钟内已有未终态订单 → 409。
    #   短视频每次提交新铸 draft(article_id 全新负数)，老去重对短视频永不命中，必须在这拦。
    #   [T5] 图文模式 video_url 为空 → 必须按 image_urls 去重，否则同一组图连点几次各自扣费
    _image_urls_str = ",".join(_image_urls)
    _dup_ids = find_recent_svideo_duplicates(user_id, req.media_ids, req.video_url,
                                             image_urls=_image_urls_str)
    if _dup_ids:
        _dup_names = []
        for _mid in _dup_ids:
            if _mid in req.media_ids:
                _idx = req.media_ids.index(_mid)
                if _idx < len(req.media_names):
                    _dup_names.append(req.media_names[_idx])
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_ORDER",
            "message": f"这个视频刚投过这些账号，正在处理中，请勿重复提交：{('、'.join(_dup_names[:5])) or str(_dup_ids)}",
            "duplicate_media_ids": _dup_ids,
        })

    # [P0-1] 铸视频稿占位，用 -draft_id 作 article_id（负号命名空间，与真实文章/彼此都不撞号）
    # 🔴 草稿也必须落**权威值** —— 工单那条最硬的证据就是同一行内部自相矛盾:
    #    `customer_name`="深圳市晨光富士电梯"(对) 而 `brand_id`=698(错)。
    #    只修订单不修草稿,矛盾还在,而且草稿是订单归属的上游。
    #    `geo_post_id` 一并落库:服务端事后才有独立通道能反查(migration 033)。
    draft_id = create_short_video_draft(
        user_id, effective_brand_id, req.title, req.content, req.keyword,
        req.video_url, req.cover_image, req.customer_name,
        article_type=req.article_type, image_urls=_image_urls_str,
        geo_post_id=req.geo_post_id,
    )
    svideo_article_id = -draft_id

    # 服务端权威算价（svideo）· fail-closed（未知/下架 id → 400）
    _markup = _get_publish_markup()
    server_charges = _recompute_publish_charge(
        [(mid, MEDIA_TYPE_SVIDEO) for mid in req.media_ids], _markup
    )
    total_cost_points = sum(c[0] for c in server_charges)
    if total_cost_points <= 0:
        raise HTTPException(status_code=400, detail="费用计算异常，请刷新重试")
    _client_total = sum(req.cost_points) if req.cost_points else 0
    if _client_total != total_cost_points:
        logger.warning(f"[svideo] 扣费口径不一致 user={user_id} client={_client_total} server={total_cost_points}")

    from middleware.billing import deduct_points
    billing_result = await deduct_points(user_id, "media_proxy_publish", extra_cost=total_cost_points)
    _svideo_charge_txid = (billing_result or {}).get("charge_tx_id")  # [GEO-R2-CAN-039 v3] 精确退款透传
    logger.info(f"短视频扣费: user={user_id}, cost={total_cost_points}, billing={billing_result}")

    # [发布链 SSOT §9 缺陷② 2026-08-02] 账号名以服务端为准,客户端值只作兜底。
    #   生产实证 item_id=458 的 media_name='' → 订单列表显示缺失。
    _name_map = _resolve_svideo_media_names(req.media_ids)
    items = []
    for i, mid in enumerate(req.media_ids):
        _client_name = req.media_names[i] if i < len(req.media_names) else ""
        items.append({
            "media_id": mid,
            "media_name": _name_map.get(int(mid)) or _client_name,
            "cost_points": server_charges[i][0],
            "cost_yuan": server_charges[i][1],
            # 🔴 用**权威值**,不是 `req.brand_id`。`create_order` 里还有一道
            #    独立的服务端反查(经 draft.geo_post_id),两道是刻意的冗余:
            #    这一道防"本次调用传错",那一道防"将来别的调用方绕过这里"。
            "brand_id": effective_brand_id,
            "media_type": MEDIA_TYPE_SVIDEO,
        })
    # [GEO-R6-CAN-011 P2] 扣费(1302)先于订单持久化 · create_order 失败须退费再抛(短视频路径同根因)
    try:
        order = create_order(user_id, svideo_article_id, req.title, items)
    except Exception as _create_err:
        try:
            from middleware.billing import refund_points
            from services.notification_events import publication_refund_context
            await refund_points(user_id, "media_proxy_publish",
                                reason=f"短视频代发订单创建失败自动退费: {str(_create_err)[:120]}",
                                charge_tx_id=_svideo_charge_txid,
                                notification=publication_refund_context(
                                    f"publication_video_create:{_svideo_charge_txid or 'legacy'}:{int(user_id)}:{int(svideo_article_id)}"
                                ))  # [GEO-R2-CAN-039 v3] 精确退款透传
            logger.error(f"[R6-CAN-011] 短视频 create_order 失败已退费: user={user_id} "
                         f"cost={total_cost_points} err={_create_err}")
        except Exception as _refund_err:
            logger.error(f"[R6-CAN-011] 短视频退费也失败(需人工核对): user={user_id} err={_refund_err}")
        raise HTTPException(status_code=500, detail="短视频代发下单失败,已自动退回算力,请稍后重试")

    submit_summary = await _submit_short_video_order(
        user_id, order["order_id"], req.title, req.content, req.keyword,
        req.video_url, req.cover_image, req.customer_name, req.remark,
        article_type=req.article_type, image_urls=_image_urls_str,
    )

    # [Codex 复审] 按真实提交结果分状态，绝不把"全失败已退款"包装成"已提交"骗用户：
    #   submitted>0 → success；pending_sync → success+等确认话术；
    #   submitted==0 且 failed>0 → status='failed' + 人话(钱已退回)，前端不清空表单让用户改完再发。
    _submitted = int(submit_summary.get("submitted") or 0)
    _failed = int(submit_summary.get("failed") or 0)
    _pending_sync = int(submit_summary.get("pending_sync") or 0)

    # 🔴🔴 [2026-08-10 P1 修法 5] 把订单结果**回写到源头作品**。
    #
    #    `geo_douyin_posts` 的 publish_order_id / publish_item_ids / publish_status
    #    三列生产上**全空** —— 于是内容被以别人的名义发了出去,而它真正的主人
    #    在创作中心看还是「未发布」。这是富士那边的第二重损失。
    #
    #    机制其实**早就建好了**(`db.geo_douyin_db.bind_publish_result` +
    #    `/api/geo-douyin/publish-result`),只是**一根线都没接** —— 全库零调用方。
    #    (本仓「死函数 = 复审漏接线」的又一例。)
    #
    # 🔴 为什么放在服务端、而不是让前端发完再调一次 `/publish-result`:
    #    依赖客户端补一次调用,正是本单要根治的那类病 —— 客户端漏了没人知道。
    #    服务端这里已经握着 order_id 与来源 post_id,顺手就落了。
    #
    # 🔴 只在**权威来源真的解析出来**时回写(`_auth_basis == "geo_post"`),
    #    不拿客户端随便传的一个 id 去改别人的作品;而且此前已对
    #    `effective_brand_id`(= 该作品的品牌)做过 RBAC。
    #
    # 🔴 fail-soft:回写失败**绝不**让一笔已扣费已提交的订单失败 —— 它是观测面。
    if req.geo_post_id and _auth_basis == "geo_post":
        try:
            from db import geo_douyin_db as _gddb
            await asyncio.to_thread(
                _gddb.bind_publish_result, int(req.geo_post_id),
                order_id=int(order["order_id"]),
                item_ids=list(order.get("item_ids") or []),
                # 只到 publishing:此刻只是"提交给发布平台了",拿到 published_url
                # 才是真发布成功,那一步由既有回调链推进,这里不越权替它宣布成功。
                publish_status=("publishing" if _submitted or _pending_sync else "failed"),
            )
        except Exception as _bind_err:  # noqa: BLE001
            logger.warning("[svideo] 回写作品发布态失败(不影响订单) post=%s order=%s: %s",
                           req.geo_post_id, order.get("order_id"), str(_bind_err)[:200])
    if _submitted == 0 and _failed > 0:
        _status = "failed"
        if submit_summary.get("need_edit"):
            _msg = "内容或标题需要调整后再发，这次的费用已原路退回"
        else:
            _msg = "这次没发出去，费用已原路退回，请稍后再试"
    elif _pending_sync > 0:
        _status = "success"
        _msg = f"已提交{'图文笔记' if _is_image_mode else '短视频'}到 {len(req.media_ids)} 个账号，正在等发布平台确认"
    else:
        _status = "success"
        _msg = f"已提交{'图文笔记' if _is_image_mode else '短视频'}到 {len(req.media_ids)} 个账号"
    response = {
        "status": _status,
        "order_id": order["order_id"],
        "total_cost": total_cost_points,
        "message": _msg,
        **submit_summary,
    }
    if req.request_id:
        try:
            from db.meijiehezi_db import save_idempotency
            save_idempotency(req.request_id, user_id, "/api/short-video/publish", response)
        except Exception as _e:
            logger.warning(f"[Idempotency] 保存短视频幂等响应失败: {_e}")
    return response


class ShortVideoUploadPolicyRequest(BaseModel):
    kind: str = "video"      # video | image —— 只影响 key 扩展名与体积上限，两类共用同一端点
    filename: str = ""       # 原始文件名，仅取扩展名(会被白名单正则洗过)


# [T4] upload-policy 响应允许出现的字段全集(白名单)。
#   任何新增字段必须在这里显式登记 —— 锁按这份清单做**全字段断言**，
#   出现 AccessKeySecret 之类的键即红。凭"不会写错"来保证不泄密是靠不住的。
SVIDEO_UPLOAD_POLICY_RESPONSE_FIELDS = frozenset({
    "status", "host", "key", "policy", "signature", "OSSAccessKeyId",
    "x-oss-security-token", "success_action_status", "expire", "max_bytes", "public_url",
})


@router.post("/short-video/upload-policy")
async def api_short_video_upload_policy(request: Request,
                                        req: ShortVideoUploadPolicyRequest | None = None):
    """[T4] 返回前端直传 OSS 的一次性上传凭据（服务端签名的 PostObject policy）。

    🔴 绝不返回 AccessKeySecret / SecurityToken 以外的长期密钥，绝不返回我们或媒介盒子的 session。
      文件本体不过我们服务器(1GB 视频浏览器直怼 OSS)，只有几百字节签名 JSON 过我们。
    挂上传闸 SVIDEO_UPLOAD_ENABLED(与发布闸分离，可先单独放开联调)。
    """
    _get_user(request)
    from services.meijiehezi.config import SVIDEO_UPLOAD_ENABLED
    if not SVIDEO_UPLOAD_ENABLED:
        raise HTTPException(status_code=503, detail="短视频上传即将开放，敬请期待")
    _kind = (req.kind if req else "video") or "video"
    _filename = (req.filename if req else "") or ""
    client = _get_client()
    try:
        policy = await client.get_short_video_upload_policy(kind=_kind, filename=_filename)
    except Exception as e:
        # 🔴 fail-closed + 可 grep 告警：绝不静默让用户以为传上去了
        #   异常信息只落日志(且 client 侧已保证异常文案不含密钥)，不回给前端
        logger.error(f"[svideo上传凭据] 签发失败 kind={_kind}: {type(e).__name__}: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail="上传暂时用不了，请稍后再试")
    response = {"status": "success", **policy}
    # 结构性兜底：真出现意料外字段(例如日后有人手滑把 sts 整个展开)就地拦下，不下发
    _unexpected = set(response.keys()) - SVIDEO_UPLOAD_POLICY_RESPONSE_FIELDS
    if _unexpected:
        logger.error(f"[svideo上传凭据] 响应含未登记字段，已拦截: {sorted(_unexpected)}")
        raise HTTPException(status_code=503, detail="上传暂时用不了，请稍后再试")
    return response


@router.post("/short-video/upload-video")
async def api_short_video_upload_video(request: Request, filename: str = ""):
    """[2026-08-01 方案B] 视频**服务端中转**上传：浏览器传给我们，我们转投 渠道的对象存储。

    为什么不用直传：渠道存储桶的 CORS 白名单没有 omnirank.top（实测预检 403），而 bucket 是媒介盒子的、
      我们改不了 → 浏览器直传**永远不会成功**。服务器之间无 CORS，故改走中转。
      产物地址仍在渠道自己的存储域，下单接口无感。

    🔴 请求体是**裸文件字节**（不是 multipart）：这样能把 `request.stream()` 直接接到 OSS，
      **不落临时文件、不进内存**。用 multipart 的话 Starlette 会把 1GB spool 到磁盘。
      文件名走 query 参数 `filename`（只用于取扩展名，会被洗）。
    🔴 nginx 必须给本路由单独放开 client_max_body_size + proxy_request_buffering off，
      否则 50M 全局上限会先把请求挡掉（见 nginx.conf 同名 location）。
    """
    # [2026-08-02 修 502] 提前应答前必须先把请求体排空（drain），否则用户看到的是 502 而不是我们写的文案。
    #   成因：nginx 对本路由设了 proxy_request_buffering off（1GB 视频不能落盘缓冲，这条必须保留），
    #   于是 nginx 不再替我们兜住"客户端还在发的那部分"。上游若在客户端发完之前就应答，
    #   连接无法正常收尾 → nginx 判定上游异常 → 回 502，我们精心写的
    #   "换个 MP4 / 视频有点大" 全被吞掉。
    # 🔴 修法是 drain，**不是**把"先校验后读体"改回"先收完再校验" —— 后者会让一个 900MB 的
    #   错误文件白占满带宽和超时，那条设计方向是对的。
    async def _reject(status_code: int, detail: str):
        try:
            async for _ in request.stream():
                pass          # 丢弃即可，不占内存；目的只是让客户端把话说完
        except Exception:
            pass              # 客户端已断开等情况，忽略——本来就要返错
        raise HTTPException(status_code=status_code, detail=detail)

    _get_user(request)
    from services.meijiehezi.config import SVIDEO_UPLOAD_ENABLED, SVIDEO_MAX_VIDEO_BYTES
    if not SVIDEO_UPLOAD_ENABLED:
        await _reject(503, "短视频上传即将开放，敬请期待")

    # 扩展名只收 mp4/flv —— 与媒介盒子前端 plupload 的 filter 对齐（实测 extensions: "mp4,flv"）
    _name = (filename or "").strip()
    if not re.search(r"\.(mp4|flv)$", _name, re.IGNORECASE):
        await _reject(400, "这个视频发不了，换个 MP4 或 FLV 格式的")

    # 长度校验必须在读 body **之前**：否则等于先把超大文件收完再拒，白白吃满带宽和超时
    try:
        _size = int(request.headers.get("content-length") or 0)
    except ValueError:
        _size = 0
    if _size <= 0:
        await _reject(400, "视频是空的，换一个试试")
    if _size > SVIDEO_MAX_VIDEO_BYTES:
        await _reject(400, f"视频有点大，单个别超过 {SVIDEO_MAX_VIDEO_BYTES // (1024 * 1024)}MB")

    client = _get_client()
    try:
        url = await client.relay_short_video_upload(
            filename=_name, size=_size, chunks=request.stream(),
        )
    except Exception as e:
        logger.error(f"[svideo中转] 上传失败 size={_size}: {type(e).__name__}: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail="视频没传上去，请稍后再试")

    # 中转产物也必须过同一道白名单：与封面代理同款守卫，远端换域时就地暴露
    if not _is_allowed_svideo_media_url(url):
        logger.error(f"[svideo中转] 返回域不在白名单: {url[:120]}")
        raise HTTPException(status_code=503, detail="视频没传上去，请稍后再试")
    return {"status": "success", "url": url}


@router.post("/short-video/upload-cover")
async def api_short_video_upload_cover(request: Request, file: UploadFile = File(...)):
    """[T4 降级路径] 图片/封面走媒介盒子代理上传（小文件）。

    正常路径是前端直传 OSS(走 /short-video/upload-policy)；本端点是直传签名不可用时的降级，
    产物域是渠道自己的存储域(已在 T2 白名单内)。
    🔴 **视频没有降级**：不做后端中转(1GB 会拖垮容器)。
    """
    _get_user(request)
    from services.meijiehezi.config import SVIDEO_UPLOAD_ENABLED, SVIDEO_MAX_IMAGE_BYTES
    if not SVIDEO_UPLOAD_ENABLED:
        raise HTTPException(status_code=503, detail="短视频封面上传即将开放")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="图片是空的，换一张试试")
    if len(content) > SVIDEO_MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"图片太大了，别超过 {SVIDEO_MAX_IMAGE_BYTES // (1024 * 1024)}MB")
    client = _get_client()
    try:
        url = await client.upload_short_video_image_via_proxy(
            filename=file.filename or "cover.jpg",
            content=content,
            content_type=file.content_type or "image/jpeg",
        )
    except Exception as e:
        logger.error(f"[svideo上传凭据] 封面代理上传失败: {type(e).__name__}: {str(e)[:200]}")
        raise HTTPException(status_code=503, detail="图片没传上去，请稍后再试")
    # 代理产物也必须过同一道白名单：远端换域时就地暴露，不让一个我们发布时会拒的地址流回前端
    if not _is_allowed_svideo_media_url(url):
        logger.error(f"[svideo上传凭据] 代理上传返回域不在白名单: {url[:120]}")
        raise HTTPException(status_code=503, detail="图片没传上去，请稍后再试")
    return {"status": "success", "url": url}


@router.get("/orders")
async def api_list_orders(request: Request, page: int = 1, limit: int = 20):
    user = _get_user(request)
    result = list_orders(user_id=user["user_id"], page=page, limit=limit)
    # 序列化
    for o in result.get("orders", []):
        for k in ("created_at", "updated_at"):
            if o.get(k) and hasattr(o[k], "isoformat"):
                o[k] = o[k].isoformat()
    return {"status": "success", **result}


@router.get("/published-articles")
async def api_published_articles(request: Request):
    """返回当前用户"分发管道中"的文章 ID 列表(任一 order status IN (0,1,2) 即在)

    [CTO-15.23 2026-05-13 全量隔离修 v2] 老板报"成功那篇没出现在已分发内" 真因:
    旧逻辑查 mhz_publish_orders.article_id · 该表 status 永远 'pending' · 拒稿/撤回的
    也算"已分发" · 用户混乱。
    新逻辑查 mhz_synced_orders 真状态(JOIN items 反查 article_id) · 严格语义:
      - status IN (0, 1, 2):待接单/发布中/已完成 · 都视为"已在分发管道中"
        防 BUG:status=1 发布中文章不能被错标"未分发" · 否则用户重复下单扣费
      - 跟 /rejected-articles 配合:UI 优先级 已分发(含发布中) > 已拒稿 > 未分发
      - 一文多投有 1 拒 + 1 成功 → 进"已分发"(老板预期)
      - 一文多投有 1 拒 + 1 还在跑 → 也进"已分发"(防重复下单)
    """
    user = _get_user(request)
    uid = user["user_id"]
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        # 分发管道中:status IN (0, 1, 2) + JOIN items 反查 article_id(防同步漏填)
        # [2026-05-18 自助发布也算"已分发"] UNION publish_records 表 status='success'
        # 的 article_id。修 bug：原代码只查代发订单，自助发布成功的文章永远不进"已分发"
        # 分组，导致用户重复发同一篇。注：publish_records.user_id 是 TEXT，要 cast。
        c.execute("""
            SELECT DISTINCT article_id FROM (
                -- 代发订单：分发管道中（待接单/发布中/已完成）
                SELECT COALESCE(s.article_id, po.article_id) AS article_id
                FROM mhz_synced_orders s
                LEFT JOIN mhz_publish_order_items i ON i.mhz_order_id = s.id
                LEFT JOIN mhz_publish_orders po ON po.id = i.order_id
                WHERE COALESCE(s.user_id, i.user_id) = %s
                  AND COALESCE(s.article_id, po.article_id) IS NOT NULL
                  AND s.status IN (0, 1, 2)
                UNION
                -- 自助发布：回执 status='success' 就占一个"别重复发"的位
                -- [R2 §②③] 🔴 这个 UNION **刻意保留回执口径**:`article_ids` 的用途
                -- 是**防止用户重复发同一篇**,不是对外宣称"已发布"。口径收紧不该
                -- 让用户白发第二遍。"到底发出去了没有"改由下面两个新字段回答。
                SELECT pr.article_id
                FROM publish_records pr
                WHERE pr.user_id = %s::TEXT
                  AND pr.status = 'success'
                  AND pr.article_id IS NOT NULL
            ) t
        """, (uid, uid))
        ids = [r["article_id"] for r in c.fetchall()]
        # [R2 §②] 发布轴:哪些是**已核实**的、哪些只是回执成功还没核实。
        c.execute(f"""
            SELECT DISTINCT pr.article_id,
                   {PUBLICATION_AXIS_SQL.format(r='pr')} AS axis
              FROM publish_records pr
             WHERE pr.user_id = %s::TEXT AND pr.article_id IS NOT NULL
               AND pr.status = 'success'
        """, (uid,))
        axis_rows = [dict(r) for r in c.fetchall()]
        verified_ids = sorted({r["article_id"] for r in axis_rows
                               if r["axis"] == PUBLICATION_VERIFIED})
        unverified_ids = sorted({r["article_id"] for r in axis_rows
                                 if r["axis"] == PUBLICATION_REPORTED_UNVERIFIED}
                                - set(verified_ids))
        return {
            "status": "success",
            # 兼容字段 · 语义 = 「别重复发」的占位面(回执口径)
            "article_ids": ids,
            "dedupe_placeholder_article_ids": ids,
            # 发布轴 —— 要说"已发布"只能用这个
            "verified_published_article_ids": verified_ids,
            "reported_success_unverified_article_ids": unverified_ids,
        }
    finally:
        conn.close()


@router.get("/article-publish-stats")
async def api_article_publish_stats(request: Request):
    """[CTO-15.23 2026-05-18 老板报"提交后回到未分发,应该显示发布中或媒体标签"]

    替代 /published-articles + /rejected-articles 二选一的简单分类 ·
    一次返每个 article 的完整 status breakdown + 已发/进行中的媒体名列表。

    Returns:
      {
        "status": "success",
        "stats": {
          "859": {
            "status": "published",         # 主状态:published / in_progress / rejected / none
            "published": 3,                # status=2 已完成数(代发) + publish_records success(自助)
            "in_progress": 1,              # status=0/1 待接单/发布中
            "rejected": 0,                 # status=-1/-2 拒稿/撤回
            "published_media": ["凤凰网", "搜狐", "齐家网"],     # 已成功媒体名(前 5)
            "in_progress_media": ["新浪家居"]                    # 发布中媒体名
          }
        }
      }

    主状态优先级:published > in_progress > rejected > none(同时存在多状态时取最高)

    [WO_PUBLISH_DISPATCH Part① 2026-08-17] **分母补第三源 `mhz_publish_order_items`**。
    原来只有镜像表 + 自助两源,而快易播(kyb)渠道**从不写镜像表**(状态回流是
    `services/kuaiyibo/status_sync.py` 直接更新 item)。生产实测:11 条已 published 的
    item(单号 `26…` 族 · 媒体「列举网(可指定地区)」)镜像 0 命中 → tab 恒「未分发」。
    🔴 第三源按 `mhz_order_id` 与镜像行**反连去重**:镜像有的以镜像为准,防同一单双计。
    """
    user = _get_user(request)
    uid = user["user_id"]
    from db.connection import get_connection
    from db.meijiehezi_db import item_countable_statuses, item_status_case_sql
    _item_case = item_status_case_sql("i2.status")
    conn = get_connection()
    try:
        c = conn.cursor()
        # 一次查聚合 · 代发镜像(mhz_synced_orders) UNION 本地 item UNION 自助(publish_records)
        # status 含义:-2 撤回 / -1 拒稿 / 0 待接单 / 1 发布中 / 2 已完成
        c.execute(f"""
            WITH user_orders AS (
                -- 代发订单:JOIN items 反查 article_id 防同步漏填
                SELECT
                    COALESCE(s.article_id, po.article_id) AS article_id,
                    s.status,
                    s.media_name
                FROM mhz_synced_orders s
                LEFT JOIN mhz_publish_order_items i ON i.mhz_order_id = s.id
                LEFT JOIN mhz_publish_orders po ON po.id = i.order_id
                WHERE COALESCE(s.user_id, i.user_id) = %s
                  AND COALESCE(s.article_id, po.article_id) IS NOT NULL
                UNION ALL
                -- 第三源:本地 item —— 只收**镜像里没有**的那些(不写镜像的渠道全靠它)
                SELECT
                    po2.article_id,
                    {_item_case} AS status,
                    i2.media_name
                FROM mhz_publish_order_items i2
                JOIN mhz_publish_orders po2 ON po2.id = i2.order_id
                WHERE i2.user_id = %s
                  AND po2.article_id IS NOT NULL
                  AND i2.status = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s2
                      WHERE COALESCE(i2.mhz_order_id, '') <> ''
                        AND i2.mhz_order_id IN (s2.order_sn, s2.id)
                        AND COALESCE(s2.user_id, i2.user_id) = %s
                  )
                UNION ALL
                -- [R2 §②③] 自助发布**不再**一律等价代发 status=2 已发布:
                -- 只有服务端权威核实过(verification_state='verified')的才算 2;
                -- 回执成功但没核实的走 status=3 这一档新桶,下面单独计数、
                -- 单独给文案「浏览器曾回报成功 · 尚未核实」,不并进 published。
                SELECT
                    pr.article_id,
                    CASE WHEN pr.public_url_verification_state = 'verified'
                         THEN 2 ELSE 3 END AS status,
                    pr.platform AS media_name
                FROM publish_records pr
                WHERE pr.user_id = %s::TEXT
                  AND pr.status = 'success'
                  AND pr.article_id IS NOT NULL
            )
            SELECT
                article_id,
                COUNT(*) FILTER (WHERE status = 2) AS published,
                COUNT(*) FILTER (WHERE status IN (0, 1)) AS in_progress,
                COUNT(*) FILTER (WHERE status IN (-1, -2)) AS rejected,
                COUNT(*) FILTER (WHERE status = 3) AS reported_unverified,
                ARRAY_AGG(media_name) FILTER (WHERE status = 2 AND media_name IS NOT NULL AND media_name != '') AS pub_media,
                ARRAY_AGG(media_name) FILTER (WHERE status IN (0, 1) AND media_name IS NOT NULL AND media_name != '') AS ip_media,
                ARRAY_AGG(media_name) FILTER (WHERE status = 3 AND media_name IS NOT NULL AND media_name != '') AS ru_media
            FROM user_orders
            GROUP BY article_id;
        """, (uid, uid, item_countable_statuses(), uid, uid))

        stats = {}
        for r in c.fetchall():
            aid = r["article_id"]
            published = int(r["published"] or 0)
            in_progress = int(r["in_progress"] or 0)
            rejected = int(r["rejected"] or 0)
            reported_unverified = int(r["reported_unverified"] or 0)
            # 主状态优先级:已核实发布 → 进行中 → [R2 §③] 回执成功待核实 → 拒稿
            # 🔴 `reported_success_unverified` 排在 in_progress **之后**:真有代发在跑,
            #    那件事比"曾经有人自报过"更该被看见。
            if published > 0:
                primary = "published"
            elif in_progress > 0:
                primary = "in_progress"
            elif reported_unverified > 0:
                primary = "reported_success_unverified"
            elif rejected > 0:
                primary = "rejected"
            else:
                primary = "none"
            # 媒体名截 5 个防数据爆炸(超过显示 "+N 家")
            pub_media = (r["pub_media"] or [])[:5]
            ip_media = (r["ip_media"] or [])[:5]
            ru_media = (r["ru_media"] or [])[:5]
            stats[str(aid)] = {
                "status": primary,
                "published": published,
                "in_progress": in_progress,
                "rejected": rejected,
                # [R2 §②③] 回执成功但未核实 —— 独立计数,**不并进 published**
                "reported_success_unverified": reported_unverified,
                "published_media": pub_media,
                "in_progress_media": ip_media,
                "reported_success_unverified_media": ru_media,
                "reported_success_unverified_label": PUBLICATION_LABELS[
                    PUBLICATION_REPORTED_UNVERIFIED],
            }
        return {"status": "success", "stats": stats}
    finally:
        conn.close()


@router.get("/articles/by-article")
async def api_articles_by_article(request: Request, brand_id: Optional[int] = None,
                                   all: str = "", limit: int = 50):
    """[CTO-15.23 2026-05-13] 一文多投聚合视图 · 解决老板报"一拒一成功隔离差"

    每篇文章一组 · 含状态汇总 + 所有平台订单明细
    RBAC 行为跟 /mhz-orders 一致:默认普通用户/管理员都只看自己 · admin 传 all=1 看全部
    """
    user = _get_user(request)
    uid = None if (all == "1" and user.get("is_admin")) else user["user_id"]
    groups = list_synced_orders_grouped_by_article(user_id=uid, brand_id=brand_id, limit=limit)
    return {"status": "success", "groups": groups, "total": len(groups)}


@router.post("/admin/backfill-orphan-fields")
async def api_admin_backfill_orphan(request: Request):
    """[CTO-15.23 2026-05-13] 管理员触发:救援历史 NULL 三字段

    幂等 · 安全可多次跑 · 仅给 admin 用
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "需要管理员权限")
    result = backfill_orphan_fields()
    logger.info(f"backfill_orphan_fields by admin={user['user_id']}: {result}")
    return {"status": "success", **result}


@router.get("/rejected-articles")
async def api_rejected_articles(request: Request):
    """返回当前用户"全失败可重发"的文章 ID 列表(用于发布中心独立分组)

    [CTO-15.23 2026-05-13 全量隔离修 v2] 老板"一拒一成功只看到拒"场景:
    旧口径:仅 status=-1 拒稿 · 且不排除已有成功的篇 · 导致一文多投只要有 1 拒
            就标"已拒稿" · 即便另一平台已成功(老板报的核心 BUG)
    新口径:文章在 -1/-2/3(拒稿/撤回/退款)任一态 + **没有任何 status IN (0,1,2)**
            才进"已拒稿/失败可重发"集合
    防 BUG:status=1 在跑的文章不归"已拒稿"(否则用户看着想重发 · 实际还在跑会重复)
    前端优先级:已分发(含发布中) > 已拒稿 > 未分发(本接口排除分发管道中的篇 · 自然不重叠)
    JOIN items 反查 article_id 防同步漏填导致脏数据
    """
    user = _get_user(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        # 真"全失败":有失败态 (-1/-2/3) 且没有 status=2 的文章
        c.execute("""
            WITH user_orders AS (
                SELECT
                    COALESCE(s.article_id, po.article_id) AS article_id,
                    s.status
                FROM mhz_synced_orders s
                LEFT JOIN mhz_publish_order_items i ON i.mhz_order_id = s.id
                LEFT JOIN mhz_publish_orders po ON po.id = i.order_id
                WHERE COALESCE(s.user_id, i.user_id) = %s
                  AND COALESCE(s.article_id, po.article_id) IS NOT NULL
            )
            SELECT article_id FROM user_orders
            GROUP BY article_id
            HAVING bool_or(status IN (-1, -2, 3)) AND NOT bool_or(status IN (0, 1, 2))
        """, (user["user_id"],))
        ids = [r["article_id"] for r in c.fetchall()]
        return {"status": "success", "article_ids": ids}
    finally:
        conn.close()


@router.get("/mhz-orders")
async def api_mhz_orders(request: Request, page: int = 1, limit: int = 20,
                         status: str = "", all: str = "", brand_id: int = None,
                         search: str = "", media_type: str = ""):
    """获取外部发布通道订单列表（从本地 DB 读取已同步的数据）
    普通用户：只看自己的订单
    管理员传 all=1：看所有人的订单
    brand_id：按品牌筛选  search：模糊搜索标题/订单号/用户名
    media_type：mhz=软文 wemedia=自媒体 空=全部
    """
    user = _get_user(request)
    uid = None if (all == "1" and user.get("is_admin")) else user["user_id"]
    result = list_mhz_synced_orders(user_id=uid, page=page, limit=limit, status=status,
                                    brand_id=brand_id, search=search.strip(), media_type=media_type)
    stats = get_mhz_synced_order_stats(user_id=uid)
    return {"status": "success", **result, "stats": stats}


@router.get("/publish-history")
async def api_publish_history(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    source: Literal["all", "proxy", "self"] = "proxy",
    view: Literal["order", "article"] = "order",
    # 🔴 R2 加了 `reported_unverified` 这一档(浏览器回报过、没人核实过),前端
    #    「待核实」筛选发的就是它。Literal 少一个值 = 前端一点就 422。
    status: Literal["", "pending", "in_progress", "completed",
                    "reported_unverified", "rejected", "withdrawn", "refunded"] = "",
    brand_id: Optional[int] = Query(None, ge=1),
    media_type: Literal["", "mhz", "article", "wemedia", "svideo", "self"] = "",
    search: str = Query("", max_length=120),
):
    """Return the authenticated user's durable, unified publish history.

    This endpoint is intentionally user-scoped even for administrators.  Cross-
    tenant administration continues to use the existing admin endpoints.  It is
    a pure database read: refresh never starts provider sync or changes orders.
    """
    user = _get_user(request)
    try:
        markup = Decimal(str(get_config("markup_ratio") or "1.5"))
        if not markup.is_finite() or markup <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        logger.error("invalid mhz markup_ratio while reading publish history")
        raise HTTPException(status_code=503, detail="发布记录暂时无法读取")

    result = list_user_publish_history(
        user_id=int(user["user_id"]),
        page=page,
        limit=limit,
        source=source,
        view=view,
        status_filter=status,
        brand_id=brand_id,
        media_type=media_type,
        search=search,
        markup_ratio=str(markup),
        # [R3 §③] 只影响行上"能点什么"的广告位,不放宽任何可见范围。
        is_admin=bool(user.get("is_admin")),
    )
    return {
        "status": "success",
        "source": source,
        "view": view,
        **result,
    }


@router.get("/mhz-orders/brands")
async def api_order_brands(request: Request, source: str = "proxy"):
    """查询有订单的品牌列表
    source=proxy: 代发订单的品牌（也用于退款审核）
    source=self: 自发记录的品牌
    """
    user = _get_user(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        if source == "self":
            if user.get("is_admin"):
                c.execute("""
                    SELECT DISTINCT r.brand_id, b.name AS brand_name
                    FROM publish_records r
                    JOIN brands b ON b.id = r.brand_id
                    WHERE r.brand_id IS NOT NULL
                    ORDER BY b.name
                """)
            else:
                c.execute("""
                    SELECT DISTINCT r.brand_id, b.name AS brand_name
                    FROM publish_records r
                    JOIN brands b ON b.id = r.brand_id
                    WHERE r.brand_id IS NOT NULL AND r.user_id = %s
                    ORDER BY b.name
                """, (str(user["user_id"]),))
        else:
            if user.get("is_admin"):
                c.execute("""
                    SELECT DISTINCT o.brand_id, b.name AS brand_name
                    FROM mhz_synced_orders o
                    JOIN brands b ON b.id = o.brand_id
                    WHERE o.brand_id IS NOT NULL
                    ORDER BY b.name
                """)
            else:
                c.execute("""
                    SELECT DISTINCT o.brand_id, b.name AS brand_name
                    FROM mhz_synced_orders o
                    JOIN brands b ON b.id = o.brand_id
                    WHERE o.brand_id IS NOT NULL AND o.user_id = %s
                    ORDER BY b.name
                """, (user["user_id"],))
        brands = [{"id": r["brand_id"], "name": r["brand_name"]} for r in c.fetchall()]
        return {"status": "success", "brands": brands}
    finally:
        conn.close()


@router.get("/mhz-orders/{order_sn}/article-info")
async def api_order_article_info(order_sn: str, request: Request):
    """查询订单关联的 article_id / quote_id / brand_id（用于重发跳转）

    [2026-04-30] 返回值新增 media_type
    [2026-05-05] 三层 article_id 反查 + 反查 quote_id/brand_id 用于重发自动定位项目：
      1. mhz_synced_orders.article_id（最直接）
      2. mhz_publish_order_items.mhz_order_id JOIN orders（适用 mhz_order_id 已回写）
      3. mhz_publish_orders 按 user_id + title + 同一天 兜底（适用 mhz_order_id 漏写）
      反查到 article_id 后再用 articles.quote_id 反查 quote_id + 真实 brand_id
    """
    user = _get_user(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        # 管理员可查所有，普通用户只查自己的
        if user.get("is_admin"):
            c.execute("SELECT article_id, brand_id, title, media_type, user_id, created_at FROM mhz_synced_orders WHERE order_sn = %s", (order_sn,))
        else:
            c.execute("SELECT article_id, brand_id, title, media_type, user_id, created_at FROM mhz_synced_orders WHERE order_sn = %s AND user_id = %s",
                      (order_sn, user["user_id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="订单不存在")
        article_id = row.get("article_id")
        # 第 2 层反查：mhz_order_id 已回写到 items 表
        if not article_id:
            c.execute("""
                SELECT o.article_id FROM mhz_publish_order_items i
                JOIN mhz_publish_orders o ON o.id = i.order_id
                WHERE i.mhz_order_id = %s LIMIT 1
            """, (order_sn,))
            item_row = c.fetchone()
            if item_row:
                article_id = item_row["article_id"]
        # 第 3 层反查：mhz_order_id 漏写时按 user_id + title + 同一天 兜底
        # 适用 53 条 failed/cancelled/paused_admin_dedupe items 的历史脏数据
        if not article_id and row.get("title") and row.get("user_id") and row.get("created_at"):
            c.execute("""
                SELECT article_id FROM mhz_publish_orders
                WHERE user_id = %s AND article_title = %s
                  AND created_at::date = %s::date
                ORDER BY created_at DESC LIMIT 1
            """, (row["user_id"], row["title"].strip() if row["title"] else "", row["created_at"]))
            fb_row = c.fetchone()
            if fb_row:
                article_id = fb_row["article_id"]

        # 拿到 article_id 后反查 quote_id 和真实 brand_id（mhz_synced_orders.brand_id 大量为空）
        quote_id = None
        brand_id = row.get("brand_id")
        if article_id:
            c.execute("""
                SELECT a.quote_id, q.brand_id
                FROM articles a
                LEFT JOIN quotes q ON q.id = a.quote_id
                WHERE a.id = %s
            """, (article_id,))
            ab = c.fetchone()
            if ab:
                quote_id = ab.get("quote_id")
                if not brand_id:
                    brand_id = ab.get("brand_id")

        # 推断 media_type：优先用表里的字段；兜底用订单号前缀
        media_type = (row.get("media_type") or "").strip()
        if not media_type:
            sn_str = str(order_sn or "")
            if sn_str.startswith("12"):
                media_type = "wemedia"
            elif sn_str.startswith("11"):
                media_type = "mhz"
        return {
            "status": "success",
            "article_id": article_id,
            "quote_id": quote_id,
            "brand_id": brand_id,
            "title": row.get("title"),
            "media_type": media_type,  # "mhz" 软文 / "wemedia" 自媒体 / "" 未知
        }
    finally:
        conn.close()


@router.get("/mhz-orders/refund-status")
async def api_order_refund_status(request: Request, order_ids: str = Query("")):
    """批量查询订单的退款申请状态"""
    user = _get_user(request)
    if not order_ids:
        return {"status": "success", "refund_map": {}}
    ids = [x.strip() for x in order_ids.split(",") if x.strip()]
    refund_map = get_order_refund_status(ids, user["user_id"])
    return {"status": "success", "refund_map": refund_map}


@router.post("/orders/withdraw")
async def api_withdraw_order(request: Request, order_id: str = Query(...)):
    """撤回待接单的订单（支持 mhz_synced_orders 和卡在 pending 的 mhz_publish_order_items）

    [Bug #9 修] 两条退款路径都改用 refund_for_publish_order 统一 helper：
      - 路径 1（本地 pending 撤稿）：按 item 退还原始 cost_points
      - 路径 2（已同步到 mhz 的撤稿）：用 item 表里的真实 cost_points（不再用 price*markup*130 重算）
      - 幂等键统一 item:{id}，和系统其他退款路径一致
    """
    user = _get_user(request)
    user_id = user["user_id"]
    from db.connection import get_connection
    from db.meijiehezi_db import refund_for_publish_order

    conn = get_connection()
    try:
        c = conn.cursor()

        # 先查 mhz_synced_orders（已同步的订单）
        c.execute("SELECT * FROM mhz_synced_orders WHERE id = %s AND user_id = %s", (order_id, user_id))
        order = c.fetchone()

        # ============================================
        # 路径 1: 本地卡 pending 的订单（还没真实提交到 mhz）
        # ============================================
        if not order:
            c.execute("""
                SELECT o.id AS order_id, o.article_title, o.total_cost_points
                FROM mhz_publish_orders o
                JOIN mhz_publish_order_items i ON i.order_id = o.id
                WHERE o.id = %s AND o.user_id = %s AND i.status = 'pending' AND i.mhz_order_id IS NULL
                LIMIT 1
            """, (int(order_id) if order_id.isdigit() else -1, user_id))
            stuck_order = c.fetchone()
            if not stuck_order:
                raise HTTPException(status_code=404, detail="订单不存在")

            # 找出该订单所有 pending 的 item，逐项标记 + 退款
            c.execute("""
                SELECT id, cost_points FROM mhz_publish_order_items
                WHERE order_id = %s AND status = 'pending' AND mhz_order_id IS NULL
            """, (stuck_order["order_id"],))
            items_to_withdraw = list(c.fetchall())

            c.execute("""
                UPDATE mhz_publish_order_items SET status = 'withdrawn', reject_reason = '用户撤回（订单未提交到外部发布通道）'
                WHERE order_id = %s AND status = 'pending' AND mhz_order_id IS NULL
            """, (stuck_order["order_id"],))
            withdrawn_count = c.rowcount
            conn.commit()  # 先提交状态变更，再走退款（refund helper 用独立 connection）

            # 按 item 维度退款（统一走 helper，幂等键 item:{id}）
            refunded = 0
            for it in items_to_withdraw:
                cost = int(it.get("cost_points") or 0)
                if cost <= 0:
                    continue
                r = refund_for_publish_order(
                    user_id=user_id,
                    amount=cost,
                    refund_key=f"item:{it['id']}",
                    reason=f"用户撤回本地订单 (order_id={order_id})",
                )
                if r.get("success") and not r.get("skipped"):
                    refunded += cost

            logger.info(f"本地 pending 订单撤回: user={user_id}, order_id={order_id}, items={withdrawn_count}, refunded={refunded}")
            msg = f"订单已撤回（{withdrawn_count} 个媒体）"
            if refunded > 0:
                msg += f"，{refunded} 积分已退还"
            return {"status": "success", "message": msg}

        # ============================================
        # 路径 2: 已同步到 mhz 的订单（调 mhz API 撤稿）
        # ============================================
        if order["status"] not in (0, 1):
            raise HTTPException(status_code=400, detail="只有待接单/发布中的订单可以撤回")

        # [2026-04-30] 软文/自媒体撤单接口分流
        # 软文订单号前缀 "11..."，自媒体前缀 "12..."；走错接口会得到 success=0/fail=1
        # 优先看 mhz_synced_orders.media_type，没有的话用订单号前缀兜底判断
        _order_media_type = (order.get("media_type") or "").strip()
        _order_sn_str = str(order["order_sn"] or "")
        if _order_media_type == "svideo":
            raise HTTPException(status_code=400, detail="短视频订单暂不支持撤回，如需处理请联系客服")
        # 开源版:撤单交给已接入的发布渠道(services/publish_channels);撤成功后照原链把算力退回
        client = _get_client()
        try:
            _cancelled = await client.cancel_order(_order_sn_str)
        finally:
            await client.close()
        if not _cancelled:
            return {"status": "error", "message": "撤回失败，可能已被媒体接单"}

        update_synced_order_status(order_id, -2)

        # 找到本地对应的 item（用 mhz_order_id = order_sn 精确匹配），按 item 真实 cost 退款
        # 不再用 price * markup * 130 重算，避免和扣费时的真实金额不一致
        c.execute("""
            SELECT i.id, i.cost_points
            FROM mhz_publish_order_items i
            WHERE i.mhz_order_id = %s
              AND i.status NOT IN ('withdrawn', 'rejected', 'failed')
        """, (order["order_sn"],))
        items_to_refund = list(c.fetchall())

        # 标记本地 item 为 withdrawn
        c.execute("""
            UPDATE mhz_publish_order_items SET status = 'withdrawn', reject_reason = '用户撤回'
            WHERE mhz_order_id = %s
              AND status NOT IN ('withdrawn', 'rejected', 'failed')
        """, (order["order_sn"],))
        conn.commit()

        # 按 item 退款（统一走 helper）
        refunded = 0
        for it in items_to_refund:
            cost = int(it.get("cost_points") or 0)
            if cost <= 0:
                continue
            r = refund_for_publish_order(
                user_id=user_id,
                amount=cost,
                refund_key=f"item:{it['id']}",
                reason=f"用户撤回同步订单 (order_sn={order['order_sn']})",
            )
            if r.get("success") and not r.get("skipped"):
                refunded += cost

        logger.info(f"撤回退费: user={user_id}, order_id={order_id}, items={len(items_to_refund)}, refunded={refunded}")
        msg = "订单已撤回"
        if refunded > 0:
            msg += f"，{refunded} 积分已退还"
        return {"status": "success", "message": msg}
    finally:
        try:
            conn.close()
        except Exception: pass


class RefundRequest(BaseModel):
    order_id: str
    reason: str = ""


@router.post("/refund/request")
async def api_request_refund(req: RefundRequest, request: Request):
    """用户申请退款（仅已完成的订单，管理员可代申请）"""
    user = _get_user(request)
    from db.connection import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        if user.get("is_admin"):
            c.execute("SELECT * FROM mhz_synced_orders WHERE id = %s", (req.order_id,))
        else:
            c.execute("SELECT * FROM mhz_synced_orders WHERE id = %s AND user_id = %s", (req.order_id, user["user_id"]))
        order = c.fetchone()
        conn.close()
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        if order["status"] != 2:
            raise HTTPException(status_code=400, detail="只有已完成的订单可以申请退款")

        # [WO_310 · 2026-09-27] 受益人一律是付款人(经本平台下单条目关联到的 mhz_publish_orders.user_id);
        #   mhz_synced_orders.user_id 不用于钱(定时同步填 1、管理员手动同步填管理员自己的 id)。
        #   原来管理员代申请时记的是管理员自己 ⇒ 批准后算力退进管理员钱包,客户拿不到。
        payer = resolve_refund_payer(req.order_id)
        if payer is None:
            raise HTTPException(status_code=409, detail="这笔订单找不到本平台的下单记录,无法自动退款,请转人工处理")
        if int(payer["payer_user_id"]) <= 0:
            # 付款人 id ≤ 0 是系统账户,不走应用退款通道
            raise HTTPException(status_code=409, detail="这笔订单的付款方是保留账号,不走应用退款通道,请转人工处理")
        if not user.get("is_admin") and int(payer["payer_user_id"]) != int(user["user_id"]):
            raise HTTPException(status_code=409, detail="这笔订单的付款人不是当前账号,请转人工处理")

        markup = float(get_config("markup_ratio") or "1.5")
        refund_points = int(float(order["price"]) * markup * 130)

        rid = create_refund_request(req.order_id, payer["payer_user_id"], req.reason, refund_points,
                                    requested_by=user["user_id"])
        return {"status": "success", "request_id": rid, "refund_points": refund_points}
    finally:
        try:
            conn.close()
        except Exception: pass


@router.get("/refund/list")
async def api_list_refunds(request: Request, page: int = 1, limit: int = 20):
    """用户查看自己的退款申请"""
    user = _get_user(request)
    result = list_refund_requests(user_id=user["user_id"], page=page, limit=limit)
    return {"status": "success", "items": result.get("requests", []), "total": result.get("total", 0)}


# ========== 管理端 ==========

@router.get("/admin/stats")
async def api_admin_stats(request: Request):
    user = _get_user(request)
    _require_admin(user)  # [GEO-R8-CAN-006] 全平台统计 · 仅管理员
    base = get_admin_stats()
    # [2026-04-30] 衔接补丁：把"本地未同步项""人工审核队列"两个数字带出来
    # 让管理员能在概览看到"另有 N 条订单需要关注"
    try:
        extra = count_local_pending_and_review()
    except Exception as e:
        logger.warning(f"[admin/stats] 计算本地待处理统计失败: {e}")
        extra = {"pre_sync_count": 0, "manual_review_count": 0}
    return {"status": "success", **base, **extra}


@router.get("/admin/local-pending-items")
async def api_admin_local_pending(request: Request, page: int = 1, limit: int = 50,
                                   status: str = "", search: str = "", brand_id: int = None):
    """[2026-04-30] 列出本地 mhz_publish_order_items 里还没同步进 mhz_synced_orders
    的 item（管理员订单管理界面看不到的部分）。

    包括：pending / submitting / submitted / awaiting_confirmation / awaiting_sync
    + 任何 manual_review_required=TRUE 的 item。
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    result = list_local_pending_items(
        page=page, limit=limit, status_filter=status,
        search=search.strip(), brand_id=brand_id,
    )
    return {"status": "success", **result}


@router.get("/admin/manual-review-queue")
async def api_admin_manual_review_queue(request: Request, page: int = 1, limit: int = 50,
                                          search: str = "", brand_id: int = None):
    """[2026-04-30] 人工审核队列 —— manual_review_required=TRUE 的所有 item。

    这些 item 的特征：
      - mhz 接单了但 12 小时反查不到凭证号
      - 不会自动退款也不会自动重发
      - 必须管理员核实 mhz 后台后人工处置（标已发布或标失败 + 退款）
    """
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    result = list_manual_review_items(
        page=page, limit=limit,
        search=search.strip(), brand_id=brand_id,
    )
    return {"status": "success", **result}


class ManualReviewResolveRequest(BaseModel):
    item_id: int
    action: str  # "mark_published" 已确认发出去 / "mark_failed" 没发出去要退款
    admin_note: str = ""


@router.post("/admin/manual-review-queue/resolve")
async def api_admin_manual_review_resolve(req: ManualReviewResolveRequest, request: Request):
    """[2026-04-30] 管理员对人工审核队列里的某条 item 做最终处置。"""
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    if req.action not in ("mark_published", "mark_failed"):
        raise HTTPException(status_code=400, detail="action 必须是 mark_published 或 mark_failed")
    result = admin_manual_review_resolve(
        item_id=int(req.item_id),
        action=req.action,
        admin_id=int(user["user_id"]),
        admin_note=(req.admin_note or "").strip(),
    )
    return {"status": "success" if result.get("success") else "error", **result}


class RefundReviewRequest(BaseModel):
    request_id: int
    approved: bool
    admin_note: str = ""


@router.post("/admin/refund/review")
async def api_review_refund(req: RefundReviewRequest, request: Request):
    """管理员审核退款"""
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    try:
        ok = review_refund_request(req.request_id, req.approved, user["user_id"], req.admin_note)
    except RefundNeedsManualReview as e:
        # [WO_310] 拿不到原扣费(无本平台下单条目)⇒ 不批准、不动钱,申请保持待审,转人工
        raise HTTPException(status_code=409, detail=str(e))
    if ok:
        return {"status": "success", "message": "已通过，积分已退还" if req.approved else "已拒绝"}
    return {"status": "error", "message": "审核失败，可能已被处理"}


@router.get("/admin/refund/list")
async def api_admin_list_refunds(request: Request, page: int = 1, limit: int = 20,
                                status: str = "", search: str = "", brand_id: int = None):
    """查看退款申请列表"""
    user = _get_user(request)
    _require_admin(user)  # [GEO-R8-CAN-006] 全租户退款申请(无 user 过滤) · 仅管理员
    result = list_refund_requests(status=status, page=page, limit=limit,
                                  search=search.strip(), brand_id=brand_id)
    return {"status": "success", "items": result.get("requests", []), "total": result.get("total", 0)}


@router.get("/admin/orders")
async def api_admin_orders(request: Request, page: int = 1, limit: int = 20):
    user = _get_user(request)
    _require_admin(user)  # [GEO-R8-CAN-006] list_orders(user_id=None) 全租户订单 · 仅管理员
    result = list_orders(user_id=None, page=page, limit=limit)
    for o in result.get("orders", []):
        for k in ("created_at", "updated_at"):
            if o.get(k) and hasattr(o[k], "isoformat"):
                o[k] = o[k].isoformat()
    return {"status": "success", **result}


class SessionConfigRequest(BaseModel):
    phpsessid: str


@router.post("/admin/config/session")
async def api_set_session(req: SessionConfigRequest, request: Request):
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    set_config("phpsessid", req.phpsessid)
    # 验证 Session
    try:
        from services.meijiehezi.client import MeiJieHeZiClient
        client = MeiJieHeZiClient(req.phpsessid)
        valid = await client.check_session()
        return {"status": "success", "session_valid": valid}
    except Exception as e:
        return {"status": "success", "session_valid": False, "error": str(e)}


class MarkupRatioRequest(BaseModel):
    ratio: float


@router.get("/admin/config/markup")
async def api_get_markup(request: Request):
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    ratio = get_config("markup_ratio") or "1.5"
    return {"status": "success", "ratio": float(ratio)}


@router.put("/admin/config/markup")
async def api_set_markup(req: MarkupRatioRequest, request: Request):
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    if req.ratio < 1.1 or req.ratio > 5.0:
        raise HTTPException(status_code=400, detail="加价比例需在 1.1 - 5.0 之间")
    set_config("markup_ratio", str(req.ratio), updated_by=user.get("user_id"))
    return {"status": "success", "message": f"加价比例已设为 {req.ratio}"}


# async wrapper（syncer 的回调声明为 async，但 DB 函数是同步的）

def _safe_int(v, default=0):
    """安全转 int，空字符串/None/非数字 → default"""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default

def _safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def _map_media_to_db(m: dict) -> dict:
    """原始 API dict → DB 字段映射，类型匹配实际 DB schema"""
    return {
        "id": m["id"],
        "media_name": str(m.get("media_name", "") or ""),
        "price": _safe_float(m.get("price", 0)),
        "price1": _safe_float(m.get("price1", 0)),
        "price2": _safe_float(m.get("price2", 0)),
        "area": str(m.get("area", "") or ""),
        "portal_media": str(m.get("portal_media", "") or ""),
        "resource_type_name": str(m.get("resource_type_name", "") or ""),
        "resource_type": str(m.get("resource_type", "") or ""),
        "inclusion_rate": _safe_int(m.get("inclusion_rate", 0)),
        "publish_rate": str(m.get("publish_rate", "") or ""),
        "avg_time": _safe_int(m.get("avg_time", 0)),
        "pc_weight": _safe_int(m.get("pc_weight", 0)),
        "m_weight": _safe_int(m.get("m_weight", 0)),
        "news_resource": _safe_int(m.get("news_resource", 0)),
        "link_type": _safe_int(m.get("link_type", 0)),
        "remark": str(m.get("remark", "") or ""),
        "case_link": str(m.get("case_link", "") or ""),
        "geo_rank": _safe_int(m.get("geo_rank", 0)),
        "geo_rank_platform": str(m.get("geo_rank_platform", "") or ""),
        "entrance_level": _safe_int(m.get("entrance_level", 0)),
        "entrance_link": str(m.get("entrance_link", "") or ""),
        "weekend_publish": _safe_int(m.get("weekend_publish", 0)),
        "authority_media": _safe_int(m.get("authority_media", 0)),
        "special_industry": _safe_int(m.get("special_industry", 0)),
    }


def _map_wemedia_to_db(m: dict) -> dict:
    """自媒体原始 API dict → DB 字段映射"""
    return {
        "id": m["id"],
        "toutiao_name": str(m.get("toutiao_name", "") or ""),
        "platform": str(m.get("platform", "") or ""),
        "industry": str(m.get("industry", "") or ""),
        "province": str(m.get("province", "") or ""),
        "fans_num": _safe_int(m.get("fans_num", 0)),
        "read_num": _safe_int(m.get("read_num", 0)),
        "price": _safe_float(m.get("price", 0)),
        "price1": _safe_float(m.get("price1", 0)),
        "price2": _safe_float(m.get("price2", 0)),
        "video_price": _safe_float(m.get("video_price", 0)),
        "weitoutiao_price": _safe_float(m.get("weitoutiao_price", 0)),
        "case_link": str(m.get("case_link", "") or ""),
        "entrance_link": str(m.get("entrance_link", "") or ""),
        "remark": str(m.get("remark", "") or ""),
        "avg_time": _safe_int(m.get("avg_time", 0)),
        "p_rate": str(m.get("p_rate", "") or ""),
        "geo_rank": _safe_int(m.get("geo_rank", 0)),
        "geo_rank_platform": str(m.get("geo_rank_platform", "") or ""),
        "quota": _safe_int(m.get("quota", 0)),
        "authority_media": _safe_int(m.get("authority_media", 0)),
    }


def _map_short_video_to_db(m: dict) -> dict:
    """短视频原始 API dict → DB 字段映射（渠道短视频资源列表）。
    从宽转型：计数 _safe_int（落 BIGINT）、价格 _safe_float、含糊串 str。"""
    return {
        "id": m["id"],
        "media_name": str(m.get("media_name", "") or ""),
        "platform": str(m.get("platform", "") or ""),
        "location": str(m.get("location", "") or ""),
        "occupation": str(m.get("occupation", "") or ""),
        "industry": str(m.get("industry", "") or ""),
        "fans_num": _safe_int(m.get("fans_num", 0)),
        "fans_num_text": str(m.get("fans_num_text", "") or ""),
        "avg_likes_num": _safe_int(m.get("avg_likes_num", 0)),
        "total_likes_num": _safe_int(m.get("total_likes_num", 0)),
        "price": _safe_float(m.get("price", 0)),
        "price1": _safe_float(m.get("price1", 0)),
        "price2": _safe_float(m.get("price2", 0)),
        "hepai_price": _safe_float(m.get("hepai_price", 0)),
        "hepai_price1": _safe_float(m.get("hepai_price1", 0)),
        "hepai_price2": _safe_float(m.get("hepai_price2", 0)),
        "avg_publish_time": str(m.get("avg_publish_time", "") or ""),
        "can_modify": _safe_int(m.get("can_modify", 0)),
        "can_hepai": _safe_int(m.get("can_hepai", 0)),
        "can_tuwen": _safe_int(m.get("can_tuwen", 0)),
        "authority_media": _safe_int(m.get("authority_media", 0)),
        "account_auth": str(m.get("account_auth", "") or ""),
        "remark": str(m.get("remark", "") or ""),
        "case_link": str(m.get("case_link", "") or ""),
        "entrance_link": str(m.get("entrance_link", "") or ""),
        "status": _safe_int(m.get("status", 0)),
        "reason": str(m.get("reason", "") or ""),
        "blacklist": _safe_int(m.get("blacklist", 0)),
    }


async def _async_save_media(m):
    return save_media(_map_media_to_db(m))

async def _async_save_media_batch(media_list):
    return save_media_batch([_map_media_to_db(m) for m in media_list])

async def _async_get_all_ids(): return get_all_media_ids()
async def _async_deactivate(ids): return deactivate_media(ids)
async def _async_get_pending(): return get_pending_items()

async def _async_update_status(item_id, status, publish_url="", reject_reason="", publish_time=""):
    """StatusSyncer 用 5 个位置参数调用，映射到 DB 函数的关键字参数"""
    return update_order_item_status(item_id, status, publish_url=publish_url or None, reject_reason=reject_reason or None)

async def _on_rejected(item_id, reason):
    logger.info(f"拒稿: item={item_id}, reason={reason}")

async def _on_session_expired():
    logger.warning("外部发布通道 Session 已过期！")


@router.post("/admin/sync/media")
async def api_sync_media(request: Request):
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")

    async def _do_media_sync():
        p = _sync_progress["media"]
        try:
            client = _get_client()

            # 阶段 1：分页拉取（原始 dict）
            p.update(phase="fetching", current=0, total=0, detail="正在拉取软文媒体列表...")
            all_items = []
            page = 1
            while True:
                total_count, items = await client.get_media_list_raw(page=page, limit=50)
                all_items.extend(items)
                p.update(current=len(all_items), total=total_count, detail=f"已拉取 {len(all_items)}/{total_count}")
                if len(all_items) >= total_count or not items:
                    break
                page += 1

            # 阶段 2：对比本地 + 批量保存
            p.update(phase="saving", current=0, total=len(all_items), detail="正在保存到数据库...")
            local_ids = get_all_media_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_media_to_db(item) for item in all_items]
            save_media_batch(records)
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            updated = len(all_items) - added
            p.update(current=len(all_items), detail=f"已保存 {len(all_items)} 条")

            # 阶段 3：下架
            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 修前这里**没有任何守卫**：
            #   get_media_list_raw 分页遇空页/短页会提前终止，据此下架会一次性
            #   静默清空目录。且 local_ids 修前未按 provider 归口 —— 管理员点一次
            #   本端点就会把另一家供应商的全部在售媒体（实测 34,747 条）下架。
            #   守卫与归口都收在 services/catalog_snapshot_guard，与 scheduler 同一份。
            verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="admin 手动软文媒体同步", logger=logger)
            stale_ids = verdict.stale
            if stale_ids:
                deactivate_media(list(stale_ids))

            now = datetime.now().isoformat()
            set_config("last_media_sync", now)
            set_config("last_media_sync_result",
                       f"共{len(all_items)}家,新增{added},更新{updated},{verdict.summary()}")
            p.update(phase="done", current=len(all_items), total=len(all_items), detail="")
            logger.info(f"软文媒体同步完成: total={len(all_items)}, added={added}")
        except Exception as e:
            set_config("last_media_sync_result", f"失败: {e}")
            p.update(phase="error", detail=str(e))
            logger.error(f"软文媒体同步失败: {e}")

    asyncio.create_task(_do_media_sync())
    return {"status": "success", "message": "软文媒体同步已在后台启动"}


@router.post("/admin/sync/wemedia")
async def api_sync_wemedia(request: Request):
    """后台同步自媒体列表"""
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")

    async def _do_wemedia_sync():
        p = _sync_progress["wemedia"]
        try:
            client = _get_client()

            # 阶段 1：分页拉取
            p.update(phase="fetching", current=0, total=0, detail="正在拉取自媒体列表...")
            all_items = []
            page = 1
            while True:
                total_count, items = await client.get_wemedia_list_raw(page=page, limit=50)
                all_items.extend(items)
                p.update(current=len(all_items), total=total_count, detail=f"已拉取 {len(all_items)}/{total_count}")
                if len(all_items) >= total_count or not items:
                    break
                page += 1

            # 阶段 2：对比本地 + 批量保存
            p.update(phase="saving", current=0, total=len(all_items), detail="正在保存到数据库...")
            local_ids = get_all_wemedia_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_wemedia_to_db(item) for item in all_items]
            save_wemedia_batch(records)
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            updated = len(all_items) - added
            p.update(current=len(all_items), detail=f"已保存 {len(all_items)} 条")

            # 阶段 3：下架
            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 补部分快照守卫（同软文端点，
            #   此处修前的一键下架面是 97,982 条另一家供应商自媒体）。
            verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="admin 手动自媒体同步", logger=logger)
            stale_ids = verdict.stale
            if stale_ids:
                deactivate_wemedia(list(stale_ids))

            now = datetime.now().isoformat()
            set_config("last_wemedia_sync", now)
            set_config("last_wemedia_sync_result",
                       f"共{len(all_items)}家,新增{added},更新{updated},{verdict.summary()}")
            p.update(phase="done", current=len(all_items), total=len(all_items), detail="")
            logger.info(f"自媒体同步完成: total={len(all_items)}, added={added}")
        except Exception as e:
            set_config("last_wemedia_sync_result", f"失败: {e}")
            p.update(phase="error", detail=str(e))
            logger.error(f"自媒体同步失败: {e}")

    asyncio.create_task(_do_wemedia_sync())
    return {"status": "success", "message": "自媒体同步已在后台启动"}


@router.post("/admin/sync/short-video")
async def api_sync_short_video(request: Request):
    """后台同步短视频资源列表（只读 GET 拉取，安全）。与软文/自媒体同步策略一致。"""
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")

    async def _do_short_video_sync():
        p = _sync_progress["short_video"]
        try:
            client = _get_client()
            p.update(phase="fetching", current=0, total=0, detail="正在拉取短视频资源...")
            all_items = []
            page = 1
            while True:
                total_count, items = await client.get_short_video_list_raw(page=page, limit=50)
                all_items.extend(items)
                p.update(current=len(all_items), total=total_count, detail=f"已拉取 {len(all_items)}/{total_count}")
                if len(all_items) >= total_count or not items:
                    break
                page += 1

            p.update(phase="saving", current=0, total=len(all_items), detail="正在保存到数据库...")
            local_ids = get_all_short_video_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_short_video_to_db(item) for item in all_items]
            save_short_video_batch(records)
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            updated = len(all_items) - added
            p.update(current=len(all_items), detail=f"已保存 {len(all_items)} 条")

            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 补部分快照守卫（同上两个端点）。
            verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="admin 手动短视频资源同步", logger=logger)
            stale_ids = verdict.stale
            if stale_ids:
                deactivate_short_video(list(stale_ids))

            now = datetime.now().isoformat()
            set_config("last_short_video_sync", now)
            set_config("last_short_video_sync_result",
                       f"共{len(all_items)}个,新增{added},更新{updated},{verdict.summary()}")
            p.update(phase="done", current=len(all_items), total=len(all_items), detail="")
            logger.info(f"短视频资源同步完成: total={len(all_items)}, added={added}")
        except Exception as e:
            set_config("last_short_video_sync_result", f"失败: {e}")
            p.update(phase="error", detail=str(e))
            logger.error(f"短视频资源同步失败: {e}")

    asyncio.create_task(_do_short_video_sync())
    return {"status": "success", "message": "短视频资源同步已在后台启动"}


@router.post("/admin/sync/orders")
async def api_sync_orders(request: Request):
    user = _get_user(request)
    _require_admin(user)
    # 开源版:订单状态从已接入的发布渠道回流(services/publish_channels/status.py);没接入 ⇒ 闸门那句 503
    from api.publish_channel_gate import unavailable_response
    from services.publish_channels import configured_mode
    from services.publish_channels.status import sync_once
    if not configured_mode():
        return unavailable_response()
    result = await sync_once()
    return {"status": "success", "message": f"已同步 {result.get('rows', 0)} 条", "result": result}


@router.post("/admin/sync/status")
async def api_sync_status(request: Request):
    user = _get_user(request)
    _require_admin(user)
    # 开源版:订单状态从已接入的发布渠道回流(services/publish_channels/status.py);没接入 ⇒ 闸门那句 503
    from api.publish_channel_gate import unavailable_response
    from services.publish_channels import configured_mode
    from services.publish_channels.status import sync_once
    if not configured_mode():
        return unavailable_response()
    result = await sync_once()
    return {"status": "success", "message": f"已同步 {result.get('rows', 0)} 条", "result": result}


@router.get("/admin/sync/result")
async def api_sync_result(request: Request):
    """查询后台同步的最新结果"""
    user = _get_user(request)
    _require_admin(user)  # [GEO-R8-CAN-006] 全局同步配置/时间戳 · 仅管理员
    return {
        "status": "success",
        "media_sync": get_config("last_media_sync") or "",
        "media_sync_result": get_config("last_media_sync_result") or "",
        "wemedia_sync": get_config("last_wemedia_sync") or "",
        "wemedia_sync_result": get_config("last_wemedia_sync_result") or "",
        "status_sync": get_config("last_status_sync") or "",
        "status_sync_result": get_config("last_status_sync_result") or "",
        "media_progress": _sync_progress["media"],
        "wemedia_progress": _sync_progress["wemedia"],
        "status_progress": _sync_progress["status"],
    }


# ========== 待确认 item（mhz 返回 203/204/205 confirm code 时挂起） ==========


def _serialize_awaiting_item(it: dict) -> dict:
    """把 DB 行规整成前端需要的字段（json 字段去 string 化）"""
    import json as _json
    codes = it.get("pending_confirm_codes")
    if isinstance(codes, str):
        try:
            codes = _json.loads(codes)
        except Exception:
            codes = []
    if not isinstance(codes, list):
        codes = []
    return {
        "item_id": it["id"],
        "order_id": it.get("order_id"),
        "article_id": it.get("article_id"),
        "article_title": it.get("article_title") or "",
        "media_id": it.get("media_id"),
        "media_name": it.get("media_name") or "",
        "media_type": it.get("media_type") or "mhz",
        "brand_id": it.get("brand_id"),
        "pending_codes": codes,
        "pending_msg": it.get("pending_confirm_msg") or "",
        "awaiting_since": it.get("awaiting_since").isoformat() if it.get("awaiting_since") else None,
    }


@router.get("/awaiting-confirmations")
async def api_list_awaiting_confirmations(request: Request):
    """拉取当前用户名下所有待确认的 item（前端轮询 / 进入页面拉一次）"""
    user = _get_user(request)
    _require_writing(user)
    items = get_awaiting_items_by_user(int(user["user_id"]))
    return {
        "status": "success",
        "count": len(items),
        "items": [_serialize_awaiting_item(it) for it in items],
    }


def _extract_sensitive_keywords(pending_msg: str) -> list:
    """[2026-04-30] 从 mhz pending_confirm_msg 解析出敏感词列表。

    mhz 的提示典型格式：
      - "电话"
      - "微信,电话,微信,电话"  (逗号分隔，可能重复)
      - "电话,赔偿,赔偿,赔偿"
      - "该标题已发布过:搜狐焦点家居（焦点房产）,是否仍要继续?"  (含中文标点的描述)

    解析策略：
      - 中文逗号、英文逗号、顿号 都当分隔符
      - 去除前后空白、去重、过滤明显不像敏感词的项（含冒号、"是否"等的描述句）
      - 限制长度 ≤ 10 字符（敏感词通常很短）
    """
    if not pending_msg:
        return []
    msg = pending_msg.strip()
    # 描述句（含冒号或"是否"）一般是"该标题已发过"这种，不是敏感词
    if ":" in msg or "：" in msg or "是否" in msg or "已发布" in msg or "重复" in msg:
        return []
    # 拆分
    import re as _re
    parts = _re.split(r"[,，、]+", msg)
    seen = set()
    keywords = []
    for p in parts:
        w = p.strip()
        if not w or len(w) > 10 or w in seen:
            continue
        seen.add(w)
        keywords.append(w)
    return keywords


@router.get("/awaiting-confirmations/{item_id}/detail")
async def api_awaiting_confirmation_detail(item_id: int, request: Request):
    """[2026-04-30] 拿单条待确认 item 的完整详情：item 信息 + 文章原文 + 提取的敏感词。

    用于新版"待确认"列表里点"去处理"打开内联编辑器时一次性拉到所有需要的数据。
    """
    user = _get_user(request)
    _require_writing(user)
    item = get_awaiting_item(int(item_id), int(user["user_id"]))
    if not item:
        raise HTTPException(status_code=404, detail="待确认 item 不存在或越权访问")
    serialized = _serialize_awaiting_item(item)
    article_id = item.get("article_id") or serialized.get("article_id")
    article_content, _preview_bid, _contact_consent = (
        _fetch_article_preview_context(article_id) if article_id else ("", None, None)
    )
    # 2026-06-02 预览所见即所发:[CLIENT_IMAGE] 占位符渲染成真实图片(相对 URL · 同域)· 🔴 传服务端推导的 brand_id 校验归属
    # [工单 C-2 T1] 旧 except: pass 是 fail-open(渲染抛错→占位符原文透给内联编辑器);
    # 改 fail-closed 唯一入口,并把 [NEED_IMAGE] 需求占位一并覆盖(内部信号不外露)。
    if article_content and (
        "[CLIENT_IMAGE" in article_content or "[NEED_IMAGE" in article_content
    ):
        from services.image_placeholder import render_for_preview_fail_closed
        article_content = render_for_preview_fail_closed(article_content, _preview_bid)
    # [2026-06-02] 联系方式预览(自发布完整 / 媒体软化提示)· 服务端推导 brand_id
    if article_content:
        from services.contact_placeholder import (
            enforce_contact_opt_out,
            hard_strip_contact_without_lookup,
            render_contact_for_preview,
        )
        if _contact_consent is False:
            try:
                article_content = enforce_contact_opt_out(article_content, _preview_bid)
            except Exception as exc:
                logger.error("awaiting preview contact scrub failed article=%s: %s", article_id, exc)
                try:
                    article_content = hard_strip_contact_without_lookup(article_content)
                except Exception:
                    raise HTTPException(status_code=500, detail="contact_opt_out_preview_failed_closed")
        elif "[CLIENT_CONTACT" in article_content or "[NEED_CONTACT" in article_content:
            try:
                article_content = render_contact_for_preview(article_content, _preview_bid)
            except Exception:
                article_content = hard_strip_contact_without_lookup(article_content)
    sensitive_keywords = _extract_sensitive_keywords(serialized.get("pending_msg") or "")
    return {
        "status": "success",
        "item": serialized,
        "article": {
            "id": article_id,
            "content": article_content,
            "contact_consent": (
                "enabled" if _contact_consent is True
                else "disabled" if _contact_consent is False
                else "legacy_unknown"
            ),
        },
        "sensitive_keywords": sensitive_keywords,
    }


class ConfirmActionRequest(BaseModel):
    action: str  # "confirm" 仍然发布 / "cancel" 取消并退款


async def _resubmit_item_after_confirm(item_id: int, user_id: int):
    """用户点"仍然发布"后异步重投单个 item 到 mhz。

    走 try_lock_for_submit → publish(confirms=...) 流程，与首次提交完全相同的安全保护。
    """
    from db.meijiehezi_db import (
        try_lock_for_submit, release_submit_lock,
        get_awaiting_item as _get_item_now,
        update_order_item_submitted as _mark_submitted,
        save_mhz_raw_response, set_item_awaiting_sync,
        set_item_submission_snapshot as _set_submission_snapshot,
    )
    from services.meijiehezi.client import ConfirmationRequiredError, AmbiguousResponseError

    async with _mhz_api_lock:
        # 重新拉一次 item（reset_awaiting 后可能 status 已是 pending）
        item = _get_item_now(item_id, user_id)
        if not item:
            logger.warning(f"[Resubmit] item={item_id} 不存在或越权")
            return

        if item["status"] != "pending":
            logger.warning(f"[Resubmit] item={item_id} 状态={item['status']}，跳过")
            return

        try:
            client = _get_client()
            await client.refresh_token()
        except Exception as e:
            logger.error(f"[Resubmit] item={item_id} client 初始化失败，等 scheduler 重试: {e}")
            return

        if not try_lock_for_submit(item_id):
            logger.warning(f"[Resubmit] item={item_id} 拿不到锁（可能 attempts 已耗尽或被并发提交）")
            return

        # 重新读，拿到 pending_confirm_fields
        item = _get_item_now(item_id, user_id)
        if not item:
            release_submit_lock(item_id, success=False, reject_reason="重投时 item 丢失")
            return

        confirms = _build_confirms_from_items([item])
        article_title = item.get("article_title") or ""
        media_type = item.get("media_type") or "mhz"

        # [svideo lane · 2026-07-04] 短视频不走这条软文/自媒体共享重投路径（正文来自 articles、
        #   而短视频需 video_url/封面，且此路径会 else→client.publish 软文错发）。释放锁保持
        #   pending，交由短视频独立链处理，绝不在此错发。守卫（当前 svideo 下单总闸关闭时不会有此类 item）。
        if media_type == "svideo":
            logger.warning(f"[Resubmit] item={item_id} 为短视频，跳过共享重投路径（由短视频独立链处理）")
            release_submit_lock(item_id, success=False, reject_reason="短视频不走共享重投路径")
            return

        # 重新拿文章正文 + 服务端推导 brand_id(🔴 配图归属校验·防确认重投时图被静默删)
        try:
            content_md, _resubmit_bid = _fetch_article_content_with_brand(item.get("article_id"))
        except Exception as e:
            logger.error(f"[Resubmit] item={item_id} 拉文章失败: {e}")
            release_submit_lock(item_id, success=False, reject_reason=f"拉取正文失败: {str(e)[:200]}")
            return
        if not content_md:
            release_submit_lock(item_id, success=False, reject_reason="文章正文为空")
            return

        # [P0 代发内容漂移] 这条重投路径读的是**当前**正文(不是订单快照),
        # 所以它没有"旧快照空转"的问题,却有更糟的一面:会把客户从没确认过的
        # 版本直接发出去。同样必须拦,并挂起等用户重新确认。
        try:
            from db.connection import get_connection as _drift_conn
            from db.meijiehezi_db import suspend_item_for_user_action
            from services.publication_content_drift import (
                build_drift_contract, detect_order_content_drift,
            )

            _dc_conn = _drift_conn()
            try:
                _dc = _dc_conn.cursor()
                _dc.execute(
                    """SELECT id, article_id, article_title, created_at,
                              article_content_snapshot_hash
                         FROM mhz_publish_orders WHERE id = %s""",
                    (item.get("order_id"),),
                )
                _order_row = _dc.fetchone()
                _verdict = detect_order_content_drift(_dc, dict(_order_row)) if _order_row else None
                _dc_conn.commit()
            finally:
                _dc_conn.close()

            if _verdict is not None and _verdict.drifted:
                release_submit_lock(item_id, success=False, reject_reason="内容漂移 · 挂起等用户确认")
                suspend_item_for_user_action(item_id, build_drift_contract(_verdict))
                logger.warning(
                    f"[Resubmit] item={item_id} 内容漂移(field={_verdict.field} "
                    f"origin={_verdict.origin}),已挂起等用户确认,不重投、不退款"
                )
                return
        except Exception as _drift_err:
            logger.warning(f"[Resubmit] item={item_id} 漂移检测异常,按原路继续: {_drift_err}")

        try:
            # [2026-06-02] 联系方式按渠道软化(该媒体 policy)· brand_id 服务端可信(_resubmit_bid)
            # 🔴 [Codex 复审] fail-closed:policy 查询失败默认 none;渲染 safe helper strip(绝不发原文)
            _is_wm_re = (media_type == "wemedia")
            from services.contact_placeholder import safe_render_contact_for_publish, strictest_policy
            _re_policy = "none"
            try:
                from db.meijiehezi_db import get_media_contact_policies
                _pmr = get_media_contact_policies([item["media_id"]], is_wemedia=_is_wm_re)
                _re_policy = strictest_policy(list(_pmr.values()))
            except Exception:
                _re_policy = "none"
            _re_md = safe_render_contact_for_publish(content_md, _resubmit_bid, channel="media", contact_policy=_re_policy)
            from services.article_publish_dispatch import dispatch_article_to_provider
            _resubmit_source = f"confirmed_resubmit:{media_type}:{_re_policy}"
            _resubmit_snapshot = lambda snap: _set_submission_snapshot(
                [item_id], title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
            )
            if media_type == "wemedia":
                wm_result = await dispatch_article_to_provider(
                    client=client, dispatch_kind="publish_wemedia",
                    article_id=item["article_id"],
                    source_title=article_title, source_content=content_md,
                    outgoing_title=article_title, outgoing_content=_re_md,
                    source=_resubmit_source,
                    provider_kwargs={
                        "toutiao_ids": [item["media_id"]], "confirms": confirms,
                        "brand_id": _resubmit_bid,
                        # [客户反馈② 2026-08-09] 首次提交的地区备注,重投时从订单回读
                        **_remark_kwarg("publish_wemedia", _read_order_remark(item.get("order_id"))),
                    },
                    snapshot_writer=_resubmit_snapshot,
                )
                save_mhz_raw_response(item_id, wm_result.raw_data)
                # [2026-04-30] 用反查命中的真 order_sn，不再用 wemedia:占位符
                # 🔴 [WO-PUB-ZOMBIE 返修 2026-08-20] 这条 sink 自交付起就是**裸调用**
                #   (走的是 `update_order_item_submitted as _mark_submitted` 别名,
                #    当时的棘轮检测器只认符号原名,所以它一直在盲区里)。
                #   _mark_submitted 是 fail-closed 的:单号为空时返 False 不落库,
                #   而本函数在成功路径上不再 release_submit_lock —— item 会带着提交锁
                #   永久留在 pending,正是 WO-PUB-ZOMBIE 那个僵尸形状。
                #   与批量/单发/短视频三处同结局:转 awaiting_sync 等定时反查回填。
                if not _mark_submitted(item_id, wm_result.order_sn):
                    set_item_awaiting_sync(
                        item_id,
                        reason="用户确认后重投自媒体未取得外部单号,等定时反查回填",
                    )
                logger.info(f"[Resubmit] wemedia item={item_id} 提交成功（用户确认后）, order_sn={wm_result.order_sn}")
            else:
                result = await dispatch_article_to_provider(
                    client=client, dispatch_kind="publish", article_id=item["article_id"],
                    source_title=article_title, source_content=content_md,
                    outgoing_title=article_title, outgoing_content=_re_md,
                    source=_resubmit_source,
                    provider_kwargs={
                        "media_ids": [item["media_id"]], "confirms": confirms,
                        "brand_id": _resubmit_bid,
                        # [客户反馈② 2026-08-09] 首次提交的地区备注,重投时从订单回读
                        **_remark_kwarg("publish", _read_order_remark(item.get("order_id"))),
                    },
                    snapshot_writer=_resubmit_snapshot,
                )
                save_mhz_raw_response(item_id, result.raw_data)
                # client.publish 成功路径必有 order_sn（空 sn 走 AmbiguousResponseError）
                # 🔴 [WO-PUB-ZOMBIE 返修 2026-08-20] "必有 order_sn" 只对媒介盒子自己的
                #   client 成立;这条路径经 dispatch_article_to_provider 分流,别家 provider
                #   给不给单号不由我们担保。同上一段:fail-closed 返 False 就转 awaiting_sync,
                #   绝不让它带着锁留在 pending。
                if not _mark_submitted(item_id, result.order_sn):
                    set_item_awaiting_sync(
                        item_id,
                        reason="用户确认后重投软文未取得外部单号,等定时反查回填",
                    )
                logger.info(f"[Resubmit] mhz item={item_id} 提交成功: order_sn={result.order_sn}")
        except ConfirmationRequiredError as ce:
            logger.info(
                f"[Resubmit] item={item_id} 再次进入待确认: code={ce.code}, field={ce.field}, msg={ce.msg}"
            )
            ok = mark_item_awaiting_confirmation(item_id, ce.code, ce.msg, ce.field)
            if not ok:
                release_submit_lock(
                    item_id, success=False,
                    reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                )
        except AmbiguousResponseError as ae:
            # [防线 4] mhz 返回 code=200 但 order_sn 为空 → 标 awaiting_sync 不重试
            logger.warning(f"[Resubmit] item={item_id} mhz 响应模糊: raw={ae.raw_data}")
            save_mhz_raw_response(item_id, ae.raw_data)
            set_item_awaiting_sync(
                item_id,
                reason=f"用户重投后 mhz 响应 code=200 但 order_sn 为空：{ae.msg or ''}",
            )
        except Exception as e:
            logger.error(f"[Resubmit] item={item_id} 提交失败: {e}")
            _err_payload = {"_error": type(e).__name__, "_message": str(e)[:500]}
            save_mhz_raw_response(item_id, _err_payload)
            release_submit_lock(item_id, success=False, reject_reason=str(e)[:200])


def _fetch_article_content(article_id) -> str:
    """从 articles 表拉文章正文（resubmit 时复用）

    [2026-04-30 bug 修] articles 表实际只有 content 字段，老代码查 body_md/body
    永远拉到空字符串 → 用户改完文章重投后 mhz 收到的还是空内容 → 走"文章正文为空"
    路径失败。改成查 content（articles 表真实字段名）。
    body_md/body 仅作历史兼容兜底，绝大多数场景下用不到。
    """
    if not article_id:
        return ""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT content FROM articles WHERE id = %s", (int(article_id),))
            row = c.fetchone()
            if not row:
                return ""
            return (row.get("content") or "").strip()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_fetch_article_content article_id={article_id} 失败: {e}")
        return ""


def _fetch_article_content_with_brand(article_id):
    """拉文章正文 + 服务端推导 brand_id(article→quotes.brand_id)· 发布/预览配图归属校验用。返回 (content, brand_id)。"""
    if not article_id:
        return "", None
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT a.content, q.brand_id FROM articles a LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s", (int(article_id),))
            row = c.fetchone()
            if not row:
                return "", None
            return (row.get("content") or "").strip(), row.get("brand_id")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"_fetch_article_content_with_brand article_id={article_id} 失败: {e}")
        return "", None


def _fetch_article_preview_context(article_id):
    """Return body, trusted brand and immutable contact consent for previews."""
    if not article_id:
        return "", None, None
    try:
        from db.connection import get_connection
        from services.contact_placeholder import contact_consent_from_snapshot

        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT a.content, a.generation_request_snapshot, q.brand_id "
                "FROM articles a LEFT JOIN quotes q ON a.quote_id=q.id WHERE a.id=%s",
                (int(article_id),),
            )
            row = c.fetchone()
            if not row:
                return "", None, None
            return (
                (row.get("content") or "").strip(),
                row.get("brand_id"),
                contact_consent_from_snapshot(row.get("generation_request_snapshot")),
            )
        finally:
            conn.close()
    except Exception as exc:
        # A preview without its consent snapshot is unsafe. Return no body,
        # rather than falling back to the old marker-rendering path.
        logger.warning("_fetch_article_preview_context article_id=%s failed: %s", article_id, exc)
        return "", None, None


@router.post("/confirm/{item_id}")
async def api_confirm_awaiting_item(item_id: int, req: ConfirmActionRequest, request: Request):
    """用户对 awaiting_confirmation 的 item 表态：
       - action='confirm' 仍然发布 → 记录确认字段 → 异步重投 mhz
       - action='cancel'  取消发布   → 自动退款，状态变 cancelled
    """
    user = _get_user(request)
    _require_writing(user)
    user_id = int(user["user_id"])

    item = get_awaiting_item(item_id, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="待确认订单不存在")
    if item["status"] != "awaiting_confirmation":
        raise HTTPException(status_code=409, detail=f"订单状态为 {item['status']}，无需确认")

    if req.action == "cancel":
        result = cancel_awaiting_item(item_id, reject_reason="用户取消发布")
        if not result:
            raise HTTPException(status_code=409, detail="状态已变更，请刷新后重试")
        return {
            "status": "success",
            "action": "cancel",
            "item_id": item_id,
            "refunded_points": result.get("refunded", 0),
            "message": "已取消发布并退款",
        }

    if req.action == "confirm":
        # 确认要发布的 confirm field：用 mhz 最新返回的 codes 反推
        # mhz 用 CONFIRM_CODE_MAP 把 code 映射到 field，这里直接用前端不知道的细节做转换
        from services.meijiehezi.config import CONFIRM_CODE_MAP
        import json as _json
        raw_codes = item.get("pending_confirm_codes") or []
        if isinstance(raw_codes, str):
            try:
                raw_codes = _json.loads(raw_codes)
            except Exception:
                raw_codes = []

        confirmed_any = False
        for code in raw_codes:
            try:
                code_int = int(code)
            except Exception:
                continue
            field = CONFIRM_CODE_MAP.get(code_int)
            if not field:
                continue
            if reset_awaiting_to_pending_for_confirm(item_id, field):
                confirmed_any = True
                # reset 函数本身把状态设为 pending，多次调用幂等（field 集合用 ?+ unique）
                # 但状态已变 pending 后第二次调用会返 False，这里一次调用即可

        if not confirmed_any:
            raise HTTPException(status_code=409, detail="确认失败，状态已变更")

        # 异步重投单个 item
        asyncio.create_task(_resubmit_item_after_confirm(item_id, user_id))

        return {
            "status": "success",
            "action": "confirm",
            "item_id": item_id,
            "message": "已确认，正在重新提交",
        }

    raise HTTPException(status_code=400, detail=f"action 必须是 confirm 或 cancel，收到 {req.action}")
