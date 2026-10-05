"""
图片占位符渲染(image_placeholder)

把文章里的 [CLIENT_IMAGE asset_id=N role=X caption="..."] 占位符渲染成图片:
- render_for_preview:预览用 · 相对 URL(同域 /uploads/...)· 所见即所发
- render_for_publish:发布用 · 绝对公网 URL(拼 PUBLIC_BASE_URL = https://omnirank.top/uploads/...)

🔴 发布铁律(老板):绝不用内部路径 / 相对路径 / /api/... / 临时签名 URL · 必须公网可达绝对 URL。
🔴 降级:asset 不存在 / 已删 / publish_allowed≠1 / rights_confirmed≠1 / 非本 brand → 静默删占位(宁少图不错图)。
   (选图时已过滤一次,这里发布前再过滤一次 · 防图片在选图后被改成「不可外发」仍被发出去)

content 存的是占位符原文(不固化 URL)· 预览 + 发布实时渲染。

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import re
import logging

logger = logging.getLogger("GEO-ImagePlaceholder")

_CLIENT_IMAGE_RE = re.compile(
    r'[ \t]*\[CLIENT_IMAGE\s+asset_id=(\d+)(?:\s+role=([a-zA-Z_]+))?(?:\s+caption="([^"]*)")?\][ \t]*'
)

# [Review-CTO 2026-07-26 · D11 ④ 收口] 图片**需求**占位符(选图前的中间标记,含
# `status=awaiting_client_asset` 的保底占位)是内部信号,任何情况下都不得进入对外正文。
# 背景:D11 ④ 让「该品牌一张已授权图都没有」时留下保底占位,好让前端提示补图;但发布
# 渲染只处理 [CLIENT_IMAGE],不认 [NEED_IMAGE] —— 于是两件坏事:
#   ① 占位符原文会被当成正文发到媒体渠道(客户可见的乱码);
#   ② `platform_safety_profiles` 见到 "[NEED_IMAGE" 即判 hard → rewrite_required
#      (渠道级不可覆盖)→ 保底占位反而变成新的发布阻断,正是 D8/D11 要消灭的形态。
# 因此发布期一律剥除;补图提示已落 quality_warning.image_assets,不依赖正文残留。
_NEED_IMAGE_REQUEST_RE = re.compile(
    r'[ \t]*\[NEED_IMAGE\b[^\]]*\][ \t]*'
)


def strip_image_requests(content: str) -> str:
    """删掉所有 [NEED_IMAGE ...] 需求占位(含保底 awaiting_client_asset)。"""
    if not content or "[NEED_IMAGE" not in content:
        return content
    return re.sub(r'\n{3,}', '\n\n', _NEED_IMAGE_REQUEST_RE.sub("", content))


def _public_base_url() -> str:
    """发布用绝对域名前缀。[WO_331 · 2026-10-03] 转调唯一出处 services.owned_image_policy.public_base_origin()。"""
    from services.owned_image_policy import public_base_origin
    return public_base_origin()


def _asset_url(asset: dict, absolute: bool) -> str:
    """取 asset 对外 URL(优先安全尺寸压缩图)。absolute=True 拼公网域名。"""
    rel = asset.get("public_url") or ""
    if not rel and asset.get("safe_size_key"):
        rel = "/" + asset["safe_size_key"]
    if not rel:
        return ""
    if not rel.startswith("/"):
        rel = "/" + rel
    return (_public_base_url() + rel) if absolute else rel


def _render(content: str, absolute: bool, brand_id: int = None) -> str:
    if not content or "[CLIENT_IMAGE" not in content:
        return content

    # 批量取图(去重 asset_id)
    ids = set()
    for m in _CLIENT_IMAGE_RE.finditer(content):
        try:
            ids.add(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    assets = {}
    try:
        from db.brand_image_assets_db import get_image_asset
        for aid in ids:
            a = get_image_asset(aid)
            if a:
                assets[aid] = a
    except Exception as e:
        logger.warning(f"[placeholder] 取图失败: {e}")

    def _sub(m):
        try:
            aid = int(m.group(1))
        except (TypeError, ValueError):
            return ""
        caption = (m.group(3) or "").strip()
        a = assets.get(aid)
        # 🔴 降级过滤:任一不满足 → 静默删(宁少图不错图)
        if not a or a.get("status") != "active":
            return ""
        if a.get("publish_allowed") != 1 or a.get("rights_confirmed") != 1:
            return ""
        if not brand_id or a.get("brand_id") != brand_id:
            return ""
        url = _asset_url(a, absolute)
        if not url:
            return ""
        alt = (a.get("alt_text") or a.get("title") or caption or "配图").replace('"', "'")
        cap = caption or a.get("caption") or ""
        # 输出 markdown 图片(发布走 md_to_html 会转成 <img> · 预览同理)
        img_md = f'![{alt}]({url})'
        return f'\n\n{img_md}\n*{cap}*\n\n' if cap else f'\n\n{img_md}\n\n'

    out = _CLIENT_IMAGE_RE.sub(_sub, content)
    return re.sub(r'\n{3,}', '\n\n', out)


def render_for_preview(content: str, brand_id: int = None) -> str:
    """预览:相对 URL(同域)· 所见即所发。

    [工单 C-2 T1 2026-07-27] 需求占位 [NEED_IMAGE ...](含保底 awaiting_client_asset)
    与发布口径一致先剥除:它是内部信号,预览面同样不给用户看裸文;补图提示走
    quality_warning.image_assets,不依赖正文残留(见 _NEED_IMAGE_REQUEST_RE 注释)。
    brand_id 为空时 _render 对每个 [CLIENT_IMAGE] 静默删(fail-closed),不裸返。"""
    return _render(strip_image_requests(content), absolute=False, brand_id=brand_id)


def render_for_publish(content: str, brand_id: int = None) -> str:
    """发布:绝对公网 URL(PUBLIC_BASE_URL)· 🔴 绝不用内部/相对/签名 URL。
    🔴 brand_id 强制:发布渲染必须带当前文章的 brand_id 校验每张图归属(防正文手写别客户 asset_id)。
       拿不到 brand_id(None/0)→ 删所有 [CLIENT_IMAGE](绝不裸发未校验归属的图)。
       _render 内对每张图再校验 asset.brand_id == brand_id,不匹配/未启用/未确认可外发的静默删。"""
    if not brand_id:
        return strip_client_images(content)
    # 需求占位先剥除(见 _NEED_IMAGE_REQUEST_RE 注释),再渲染已选定的真实图片。
    return _render(strip_image_requests(content), absolute=True, brand_id=brand_id)


def render_for_preview_fail_closed(content: str, brand_id: int = None) -> str:
    """[工单 C-2 T1] 预览渲染 · 失败剥离。

    生产 10 篇深档实证 8/10 裸露占位符,漏点之一是各预览面把渲染异常当"跳过"
    (except: pass / `if brand_id else content` 三元短路)→ 占位符原文透给用户。
    收口为唯一入口:渲染链任何异常都剥离占位符,绝不裸返。"""
    try:
        return render_for_preview(content, brand_id)
    except Exception as e:
        logger.warning(f"[placeholder] 预览渲染失败,fail-closed 剥离占位符: {e}")
        return strip_client_images(content)


def render_for_publish_fail_closed(content: str, brand_id: int = None) -> str:
    """[工单 C-2 T1] 发布/导出渲染 · 失败剥离(同上,绝对 URL 口径)。"""
    try:
        return render_for_publish(content, brand_id)
    except Exception as e:
        logger.warning(f"[placeholder] 发布渲染失败,fail-closed 剥离占位符: {e}")
        return strip_client_images(content)


def strip_client_images(content: str) -> str:
    """删掉所有图片占位 → 纯文字(目标平台不支持外链图时的发布预案)。

    同时剥除 [NEED_IMAGE ...] 需求占位:它是内部信号,发布预案下更不该留在正文。
    """
    content = strip_image_requests(content)
    if not content or "[CLIENT_IMAGE" not in content:
        return content
    return re.sub(r'\n{3,}', '\n\n', _CLIENT_IMAGE_RE.sub("", content))


def has_client_images(content: str) -> bool:
    """content 里是否含图片占位符(发布前判断「该渠道是否需要外链图」用)。"""
    return bool(content and "[CLIENT_IMAGE" in content)


def count_rendered_images(content: str, brand_id: int = None) -> dict:
    """[工单 T4 2026-07-29] 数**真正渲染得出来**的图，并给出掉图原因。

    生产实证（2026-07-29 只读复核，全站 1248 篇 / 200 个 [CLIENT_IMAGE] 标记）：

    ====== ==== ==================================================
    项      数量 说明
    ====== ==== ==================================================
    标记    200  199 篇文章带 [CLIENT_IMAGE]
    可渲染  138  69%，公网 URL 实测 HTTP 200 + 正确 content-type
    掉图     62  31%，**全部因为素材被软删（status≠active）**
    权限失败  0  publish_allowed / rights_confirmed 均正常
    归属失败  0  quotes.brand_id 与素材 brand_id 全部对得上
    ====== ==== ==================================================

    也就是说"图片加载不出来"**不是** URL / nginx / 存储 / 权限 / 归属的问题，
    而是**素材在被写进正文之后才被软删**，渲染层 fail-closed 静默剥离。
    静默是对的（宁少图不错图），**不说**才是问题：前端那句
    "已自动配图 N 张" 数的是正文里的 `[CLIENT_IMAGE` 原文标记数，
    渲染掉了几张它一无所知 —— 于是用户看到"已配图 2 张 + 一张图都没有"。

    这个函数就是给那句话用的真实分母/分子，让 UI 说得出
    "已配图 2 张（1 张素材已下架，未展示）"。
    """
    total = 0
    ids: list[int] = []
    for m in _CLIENT_IMAGE_RE.finditer(content or ""):
        total += 1
        try:
            ids.append(int(m.group(1)))
        except (TypeError, ValueError):
            continue

    result = {
        "marker_count": total,
        "rendered_count": 0,
        "dropped_count": total,
        "dropped_reasons": {},
    }
    if not total:
        result["dropped_count"] = 0
        return result
    if not brand_id:
        result["dropped_reasons"] = {"missing_brand_id": total}
        return result

    try:
        from db.brand_image_assets_db import get_image_asset
        assets = {aid: get_image_asset(aid) for aid in set(ids)}
    except Exception as e:
        logger.warning(f"[placeholder] 统计渲染图失败: {e}")
        result["dropped_reasons"] = {"asset_lookup_failed": total}
        return result

    reasons: dict = {}
    rendered = 0
    for aid in ids:
        a = assets.get(aid)
        if not a:
            reasons["asset_missing"] = reasons.get("asset_missing", 0) + 1
        elif a.get("status") != "active":
            reasons["asset_removed"] = reasons.get("asset_removed", 0) + 1
        elif a.get("publish_allowed") != 1 or a.get("rights_confirmed") != 1:
            reasons["not_publishable"] = reasons.get("not_publishable", 0) + 1
        elif a.get("brand_id") != brand_id:
            reasons["brand_mismatch"] = reasons.get("brand_mismatch", 0) + 1
        elif not _asset_url(a, False):
            reasons["missing_url"] = reasons.get("missing_url", 0) + 1
        else:
            rendered += 1
    result["rendered_count"] = rendered
    result["dropped_count"] = total - rendered
    result["dropped_reasons"] = reasons
    return result
