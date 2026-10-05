"""
文章自动配图选择器(article_image_selector)

两步占位符的第 2 步「系统选图」:
  LLM 写文章只输出图片「需求」占位符 [NEED_IMAGE role=X purpose=Y](不碰具体图 · 不编 URL),
  本模块按 role 从客户图库(list_publishable_assets)选最合适的真实图片,
  替换成 [CLIENT_IMAGE asset_id=N role=X caption="..."](确定的图)。

🔴 铁律(老板):
- 只从「已确认可外发」的图里选(publish_allowed=1 AND rights_confirmed=1,由 list_publishable_assets 保证)
- 没有合适图 → 删占位符(不插)· 宁缺毋滥,绝不为「看起来丰富」硬塞错图
- 每篇最多 2 张 · 同一张图不重复用
- 不调 LLM(纯规则选图 · 同步)

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import hashlib
import re
import logging

logger = logging.getLogger("GEO-ArticleImageSelector")

# role → 候选 image_type 优先级(从高到低)· 对齐图库 image_type 枚举
_ROLE_TO_TYPES = {
    "hero":        ["storefront", "product", "case", "environment", "team", "logo"],
    "brand_intro": ["logo", "storefront", "team", "environment"],
    "product":     ["product"],
    "case":        ["case", "certificate"],
}
# role → usage_scenarios 偏好(同 image_type 内的次要加分项)
_ROLE_TO_SCENARIO = {
    "hero": "brand_intro", "brand_intro": "brand_intro",
    "product": "product_desc", "case": "case_proof",
}

_MAX_IMAGES = 2
_NEED_IMAGE_RE = re.compile(r'[ \t]*\[NEED_IMAGE\s+role=([a-zA-Z_]+)(?:\s+purpose=([^\]]*))?\][ \t]*')
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")

# [写作质量总工单 2026-07-29 · C-3/⑨] 生成端占位规则的**唯一活 SSOT**。
#
# 事故背景：这条规则原先只存在于 `writing/production_style_v09.py:
# compose_r6_v09_default_prompt`，而该函数**全仓零调用方（除测试）**——
# 也就是说线上 prompt 从来没告诉过模型可以输出 [NEED_IMAGE]。后果是
# `_insert_default_image_need_placeholder` 每篇都机械地在 H1 后补一个占位，
# 图片永远出现在同一个位置、与上下文无关（Owner 实证的
# "全文只有一个 [CLIENT_IMAGE asset_id=217]" 正是这个形态）。
# 规则放在解析器旁边，删规则必然会让解析器的测试一起红。
IMAGE_PLACEHOLDER_RULE = """【配图占位规则】
需要配图的地方只输出需求占位符,系统会在发布前换成客户已授权的真实图片:
- 格式固定为 [NEED_IMAGE role=X purpose=Y],role 只能取 hero / brand_intro / product / case。
- purpose 用一句话说明这里为什么需要图(例:purpose=客户案例或资质图片)。
- **每篇最多 2 处**,放在与该段内容真正相关的位置(案例段配 case、产品段配 product),
  不要全部堆在开头。
- 只使用客户已确认可发布的真实图片,因此你**不要写 Markdown 图片语法、不要编 URL、
  不要描述图片内容当作事实**;没有合适素材时系统会自动删除占位符。
"""

IMAGE_OPT_OUT_PROMPT = """【配图授权:未开启】
本篇不插入任何图片:不得输出 [NEED_IMAGE]、[CLIENT_IMAGE]、Markdown 图片语法或自行设计的图片占位符。
"""

_ROLE_ANCHOR_KEYWORDS = {
    "brand_intro": ("企业概况", "公司介绍", "品牌介绍", "公司简介", "关于", "当前推荐", "推荐对象"),
    "hero": ("企业概况", "公司介绍", "品牌介绍", "公司简介", "关于", "当前推荐", "推荐对象"),
    "product": ("产品", "服务", "业务", "方案", "能力", "卖点", "适合谁"),
    "case": ("案例", "客户案例", "成功案例", "交付", "落地", "项目", "资质", "获奖", "评价"),
}


def _asset_stable_id(asset: dict) -> str:
    return str(asset.get("id") or asset.get("url") or asset.get("title") or asset.get("caption") or "")


def _rotation_offset(article_key: str, role: str, image_type: str, count: int) -> int:
    if not article_key or count <= 1:
        return 0
    seed = f"{article_key}|{role}|{image_type}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % count


def _pick_asset(role: str, assets: list, used_ids: set, article_key: str = ""):
    """按 role→image_type 优先级,从未用过的 assets 里挑一张最合适。挑不到返回 None。"""
    types = _ROLE_TO_TYPES.get(role)
    if not types:
        return None
    prefer_scene = _ROLE_TO_SCENARIO.get(role)
    for t in types:
        candidates = [a for a in assets if a.get("id") not in used_ids and a.get("image_type") == t]
        if not candidates:
            continue
        preferred = [a for a in candidates if prefer_scene in (a.get("usage_scenarios") or [])]
        pool = preferred or candidates
        pool.sort(key=lambda a: _asset_stable_id(a))
        return pool[_rotation_offset(article_key, role, t, len(pool))]
    return None


def _normalize_match_text(value: str | None) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _is_h1_line(line: str, index: int) -> bool:
    return index == 0 and line.lstrip().startswith("# ")


def _is_heading_line(line: str) -> bool:
    return bool(_HEADING_RE.match(line or ""))


def _line_has_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in (line or "") for keyword in keywords)


def _anchor_after_table_if_needed(lines: list[str], index: int) -> int:
    """Return insertion index after a semantic line, keeping markdown tables intact."""
    insert_at = max(0, min(len(lines), index + 1))
    if 0 <= index < len(lines) and lines[index].lstrip().startswith("|"):
        while insert_at < len(lines) and lines[insert_at].lstrip().startswith("|"):
            insert_at += 1
    return insert_at


def _find_brand_anchor(lines: list[str], brand_name: str | None) -> int | None:
    brand_token = _normalize_match_text(brand_name)
    if not brand_token:
        return None

    # Prefer a customer/company section title over the article title or generic lead.
    for index, line in enumerate(lines):
        if _is_h1_line(line, index):
            continue
        if _is_heading_line(line) and brand_token in _normalize_match_text(line):
            return _anchor_after_table_if_needed(lines, index)

    for index, line in enumerate(lines):
        if _is_h1_line(line, index):
            continue
        if brand_token in _normalize_match_text(line):
            return _anchor_after_table_if_needed(lines, index)
    return None


def _find_role_anchor(lines: list[str], role: str) -> int | None:
    keywords = _ROLE_ANCHOR_KEYWORDS.get(role) or ()
    if not keywords:
        return None

    for index, line in enumerate(lines):
        if _is_h1_line(line, index):
            continue
        if _is_heading_line(line) and _line_has_keyword(line, keywords):
            return _anchor_after_table_if_needed(lines, index)

    for index, line in enumerate(lines):
        if _is_h1_line(line, index):
            continue
        if _line_has_keyword(line, keywords):
            return _anchor_after_table_if_needed(lines, index)
    return None


def _find_intro_fallback_anchor(lines: list[str]) -> int:
    start = 1 if lines and lines[0].lstrip().startswith("#") else 0
    for index in range(start, len(lines)):
        if lines[index].strip():
            return _anchor_after_table_if_needed(lines, index)
    return len(lines)


def _insert_marker_at(content: str, marker: str, role: str, brand_name: str | None) -> str:
    lines = (content or "").splitlines()
    anchor = None
    if role in {"brand_intro", "hero"}:
        anchor = _find_brand_anchor(lines, brand_name)
    if anchor is None:
        anchor = _find_role_anchor(lines, role)
    if anchor is None:
        anchor = _find_intro_fallback_anchor(lines)

    anchor = max(0, min(len(lines), anchor))
    lines[anchor:anchor] = ["", marker, ""]
    return "\n".join(lines)


def _client_image_marker(asset: dict, role: str, purpose: str) -> str:
    caption = (asset.get("caption") or asset.get("title") or purpose or "").replace('"', "'").strip()
    return f'[CLIENT_IMAGE asset_id={asset["id"]} role={role} caption="{caption}"]'


def select_images_for_article(
    content: str,
    brand_id: int,
    article_key: str | None = None,
    brand_name: str | None = None,
    batch_used_asset_ids: set | None = None,
) -> str:
    """解析 [NEED_IMAGE] → 选真实图片 → 按语义段落插入 [CLIENT_IMAGE]。

    ``batch_used_asset_ids``（工单 T4 · 同批不重复）：调用方传入一个**跨文章共享**
    的集合，本函数选中的每张图都会写回去，同批后续文章不再选中同一张。

    为什么不能只靠 ``_rotation_offset``：那是个按 article_key 做的哈希轮转，它让
    不同文章**倾向于**取不同下标，但既不保证互斥（哈希会撞），也不知道同批里别的
    文章拿了什么 —— 素材少的品牌（生产里 32 张可发布素材分布在 21 个品牌，多数
    品牌只有 1-2 张）几乎必然整批同一张图。轮转保留，作为集合之外的次要打散。

    传 ``None``（默认）= 不做跨文章去重，保持老调用方行为不变。
    """
    if not content or "[NEED_IMAGE" not in content:
        return content

    try:
        from db.brand_image_assets_db import list_publishable_assets
        assets = list_publishable_assets(brand_id) if brand_id else []
    except Exception as e:
        logger.warning(f"[image_selector] 取图库失败 brand_id={brand_id}: {e}")
        assets = []

    # 同批已用过的图先排除;排除到一张不剩时**退回本篇内去重**——
    # 宁可同批重复,也不能让"素材只有 1 张"的品牌从第 2 篇起全部零图。
    batch_used = batch_used_asset_ids if isinstance(batch_used_asset_ids, set) else None
    if batch_used:
        remaining = [a for a in assets if a.get("id") not in batch_used]
        if remaining:
            assets = remaining
        else:
            logger.info(
                f"[image_selector] brand_id={brand_id} 同批可用图已用尽"
                f"(库存 {len(assets)} 张),本篇回退到允许复用"
            )

    used_ids = set()
    selected: list[dict] = []

    def _replace(m):
        role = (m.group(1) or "").strip().lower()
        purpose = (m.group(2) or "").strip()
        # 超额 / 图库无可用图 → 删占位符
        if len(selected) >= _MAX_IMAGES or not assets:
            return ""
        asset = _pick_asset(role, assets, used_ids, article_key or "")
        if not asset:
            return ""  # 没合适图,不插(宁缺毋滥)
        used_ids.add(asset["id"])
        if batch_used is not None:
            batch_used.add(asset["id"])
        selected.append({
            "role": role,
            "marker": _client_image_marker(asset, role, purpose),
        })
        return ""

    new_content = _NEED_IMAGE_RE.sub(_replace, content)
    # 占位符删除后可能留连续空行,收敛成至多一个空行
    new_content = re.sub(r'\n{3,}', '\n\n', new_content)

    for request in selected:
        new_content = _insert_marker_at(
            new_content,
            request["marker"],
            request["role"],
            brand_name=brand_name,
        )
        new_content = re.sub(r'\n{3,}', '\n\n', new_content)

    logger.info(f"[image_selector] brand_id={brand_id} 实配 {len(selected)} 张(图库 {len(assets)} 张可用)")
    return new_content
