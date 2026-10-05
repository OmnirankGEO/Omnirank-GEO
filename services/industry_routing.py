"""行业路由「前段」—— U3 写路径与 industry_canonical 读路径**共用这一份**(WO_267 · 2026-09-23)。

🔴 为什么只能有一份:付费点亮调研(写路径 `industry_resolver.resolve_or_create_industry`)与
   发布中心主榜(读路径 `industry_canonical.resolve_readonly`)要对同一个「行业原文 + 品牌上下文」
   判出**同一个调研行**,否则「付费跑完榜必点亮」断 —— 光伏客户付费数据落「新能源」,
   主榜却按一条旧 LLM 别名去查「装修建材」。两条路径各写一遍顺序 = 迟早不一样,所以顺序只写在这里。

顺序(Review 09-23 统一裁定,作废此前「读路径字典在别名之后」):
  1. **admin 人工别名**(`geo_research_industry_aliases.resolved_by='admin'`)—— 人的判断最权威;
  2. **行业大类字典**(吃品牌上下文:名称 / 备注 / 种子词;用户选定的大类 key 最优先)→ 字典指定的
     调研行(按 slug)。字典判出了大类就**一定**落这一行 —— 行还没建 / 已停用也不往下落到 LLM 别名,
     否则光伏客户付费之前,读路径会顺着旧别名落到「装修建材」的榜;
  3. **LLM 别名**(付费轮沉淀的机器判断)。
  都不中 ⇒ None,调用方接自己的后段(写路径:LLM 选择题 / 新建;读路径:同名 / 透传)。
  指向已停用行业的别名一律跳过(不路由死行业,与两条路径原来的口径一致)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from services.industry_taxonomy import OTHER_KEY, Resolution, research_target, resolve_industry

BY_ALIAS_ADMIN = "alias_admin"
BY_TAXONOMY = "taxonomy"
BY_ALIAS_LLM = "alias_llm"

#: 前段顺序的**声明**(判据按它核对两条路径,改顺序要改这里 —— 也只许改这里)。
FRONT_ORDER = (BY_ALIAS_ADMIN, BY_TAXONOMY, BY_ALIAS_LLM)


@dataclass(frozen=True)
class FrontHit:
    by: str                       # FRONT_ORDER 之一
    industry_id: Optional[int]    # 字典行还没建时为 None(写路径付费后按字典 slug 建)
    industry_name: str            # 调研行**当前**名;行还没建时 = 字典里的说明名
    active: bool
    slug: str = ""                # 字典命中时:调研行 slug
    category: Optional[Resolution] = None


def _default_alias_row(raw: str) -> Optional[dict]:
    from db.research_selfserve_db import resolve_alias_row
    return resolve_alias_row(raw)


def _default_name_by_id(industry_id: int) -> Optional[str]:
    """只取 active 行业的名字(别名指向软删行业 ⇒ None ⇒ 不路由死行业)。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM geo_research_industries WHERE id = %s AND active = TRUE",
                    (int(industry_id),))
        row = cur.fetchone()
        return (row["name"] or "").strip() or None if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _default_row_by_slug(slug: str) -> Optional[dict]:
    from services.research_monitor.industry_registry import research_row_by_slug
    return research_row_by_slug(slug)


def route_front(
    raw: str,
    *,
    brand: Optional[dict] = None,
    category_key: Optional[str] = None,
    alias_row: Callable[[str], Optional[dict]] = _default_alias_row,
    name_by_id: Callable[[int], Optional[str]] = _default_name_by_id,
    row_by_slug: Callable[[str], Optional[dict]] = _default_row_by_slug,
) -> Optional[FrontHit]:
    """行业原文 → 前段命中(或 None)。`category_key` = 本次请求里用户改选的大类(优先于品牌上存的)。"""
    raw = (raw or "").strip()
    alias = alias_row(raw) if raw else None

    # 1. admin 人工别名
    if alias and alias.get("resolved_by") == "admin":
        name = name_by_id(int(alias["industry_id"]))
        if name:
            return FrontHit(BY_ALIAS_ADMIN, int(alias["industry_id"]), name, True)

    # 2. 行业大类字典
    res = resolve_industry(raw, brand=brand, brand_override=category_key)
    if res.category_key != OTHER_KEY:
        slug, doc_name = research_target(res.category_key)
        if slug:
            row = row_by_slug(slug)
            if row:
                return FrontHit(BY_TAXONOMY, int(row["id"]), (row.get("name") or doc_name).strip(),
                                bool(row.get("active")), slug, res)
            return FrontHit(BY_TAXONOMY, None, doc_name, False, slug, res)

    # 3. LLM 别名
    if alias and alias.get("resolved_by") != "admin":
        name = name_by_id(int(alias["industry_id"]))
        if name:
            return FrontHit(BY_ALIAS_LLM, int(alias["industry_id"]), name, True)
    return None


def category_public(res: Optional[Resolution]) -> Optional[dict]:
    """给前端(点亮调研弹窗「判出的大类 + 可改选」)的大类说明。"""
    if res is None:
        return None
    from services.industry_taxonomy import get_category
    c = get_category(res.category_key)
    sec = get_category(res.secondary) if res.secondary else None
    return {
        "key": res.category_key,
        "name": c.name if c else res.category_key,
        "secondary_key": res.secondary,
        "secondary_name": sec.name if sec else None,
        "needs_review": res.needs_review,
        "source": res.source,
    }
