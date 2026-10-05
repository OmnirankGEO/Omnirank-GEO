"""GEO 抖音图文 · 客户素材上下文(知识库 / 物料 / 图片资产)

回答的是一个真缺口:图文链原本只吃 `keyword + brand_name`,**完全没用客户知识库** ——
而文章链早就用了(`writing/article_writer.py::_load_client_knowledge` → unified RAG)。
同一个客户,写文章能引用他的资料,做图文却不能,说不过去。

本模块把三个**生产上真有数据**的来源汇成一份写作上下文:

| 来源 | 生产行数(2026-08-02 实测) | 用途 |
|---|---|---|
| `client_materials` | 61 | 公司介绍/核心卖点/案例/资质/客户证言 → 文案素材 |
| `brand_image_assets` | 232 | LOGO/实拍图,带 `vision_summary`/`ocr_text` → 生图风格与元素参考 |
| unified RAG 客户库(按 brand_id 隔离) | — | 与关键词相关的长尾知识 |

🔴 **竞对资料本轮不接,因为没有**:生产 `competitors` 表 **0 行**、`competitor_snapshots` 也空。
   接一个空表只会产出"看起来有、其实永远为空"的假功能。要做竞对参考,得先有数据源
   (可选路径:飞轮 `geo_research_source_signals` 里有竞对投放样本)—— 那是独立一单。

🔴 **图片资产只用授权过的**:`publish_allowed` 且 `rights_confirmed` 才进上下文。
   没确权的图不能拿去生成对外发布物(版权面,fail-closed)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("GEO-Douyin-KB")

# 单块素材进 prompt 的字数上限(防把 prompt 撑爆)
_MATERIAL_FIELD_MAX = 300
_KB_CHUNK_MAX = 500
_KB_TOP_K = 5


@dataclass
class BrandContext:
    """喂给文案/生图的客户上下文。空字段一律留空,不编。"""
    brand_name: str = ""
    company_intro: str = ""
    core_selling_points: str = ""
    unique_value: str = ""
    case_studies: str = ""
    credentials: str = ""
    # 🔴 2026-08-03 补:这四项客户填了但**从来没进过 prompt**(Review 硬要求
    #    「生成上下文不丢全量物料」)。方法论/价格档位尤其重要 ——
    #    §3 实证里"带价格""适用人群"正是被采纳内容比对照组高的少数特征之一。
    testimonials: str = ""
    methodology: str = ""
    pricing_tiers: str = ""
    service_area: str = ""
    team_size: str = ""
    kb_snippets: List[str] = field(default_factory=list)
    logo_hints: List[str] = field(default_factory=list)      # LOGO 的视觉描述
    image_hints: List[str] = field(default_factory=list)     # 其他已授权图片的视觉描述
    sources_used: List[str] = field(default_factory=list)    # 留痕:实际用到了哪些来源
    # 🔴 哪几路**取失败了**(不是"没有数据",是根本没读成)。
    #    这两件事必须分开:前者是我们的 bug,后者是客户没填。
    #    只把失败降级成"没有素材"的话,一个 SQL 类型错能安静躺一整个版本
    #    —— publish_allowed 的 smallint/boolean 事故就是这么来的。
    load_errors: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any([self.company_intro, self.core_selling_points, self.unique_value,
                        self.case_studies, self.credentials, self.kb_snippets])

    def to_prompt_block(self) -> str:
        """拼成给 LLM 的素材块。没有素材就返回空串 —— **绝不输出"未提供"之类占位**,
        那会诱导模型去编。"""
        parts: List[str] = []
        if self.company_intro:
            parts.append(f"【公司简介】{self.company_intro}")
        if self.core_selling_points:
            parts.append(f"【核心卖点】{self.core_selling_points}")
        if self.unique_value:
            parts.append(f"【差异化价值】{self.unique_value}")
        if self.credentials:
            parts.append(f"【资质/背书】{self.credentials}")
        if self.case_studies:
            parts.append(f"【案例】{self.case_studies}")
        if self.testimonials:
            parts.append(f"【客户证言】{self.testimonials}")
        if self.methodology:
            parts.append(f"【方法论】{self.methodology}")
        if self.pricing_tiers:
            parts.append(f"【价格档位】{self.pricing_tiers}")
        if self.service_area:
            parts.append(f"【服务区域】{self.service_area}")
        if self.team_size:
            parts.append(f"【团队规模】{self.team_size}")
        for i, s in enumerate(self.kb_snippets, 1):
            parts.append(f"【客户知识库 {i}】{s}")

        # 🔴 2026-08-05 补。在此之前 `logo_hints` / `image_hints` **从来没进过 prompt** ——
        #    它们被读出来(brand_image_assets)、挂在 context 上,却只用于算
        #    `has_real_photo` 和 `sources_used`,一个字都没给到生成侧。
        #    后果:视觉身份(配色/场景)**无从按客户定制**,只能靠 seed 随机 ——
        #    Owner 2026-08-05:「颜色要根据客户的素材或者行业来定」。
        #    这是本仓第三次同型缺口:数据取到了,没接到消费端。
        if self.image_hints:
            parts.append("【客户实拍图(可作为这一组的视觉场景依据)】"
                         + "；".join(self.image_hints[:6]))
        if self.logo_hints:
            parts.append("【客户 LOGO 视觉描述(可作为配色依据)】"
                         + "；".join(self.logo_hints[:3]))
        return "\n".join(parts)

    @property
    def has_real_photo(self) -> bool:
        """这个客户有没有**已授权的实拍图**(LOGO 不算)。

        🔴 2026-08-04 Review 攻破 §3.1 后新增。在此之前,「实拍叠字」那一款
           要不要降级是拿 `"brand_image_assets" in sources_used` 判的,而
           `sources_used` 是 `logo_hints or image_hints` 就会置位的 ——
           于是**只有 LOGO、零实拍图**的客户信号照样为 True,实拍叠字不降级,
           模型就会凭空画一张"看起来像实拍"的假实景图挂到客户名下。
           生产量化:9 个有双闸资产的品牌里 5 个是 logo-only(56%)——
           这是多数场景,不是边角。

        🔴 为什么不去改 `sources_used`:那个列表的语义是「这条内容**实际用到了**
           哪些来源」,是给用户看的留痕(详情页会渲染成 chips)。logo-only 的客户,
           LOGO 提示确实进了 prompt、确实被用了 —— 把它从留痕里摘掉是**另造一个错**。
           错的是消费侧拿一个"展示用的列表"去判一个"能不能画实拍"的资格。
           所以修在消费侧:给一个**只表达这件事**的专用信号。
        """
        return bool(self.image_hints)

    def to_dict(self) -> dict:
        return {
            "brand_name": self.brand_name,
            "has_materials": bool(self.company_intro or self.core_selling_points),
            "kb_snippets": len(self.kb_snippets),
            "logo_hints": len(self.logo_hints),
            "image_hints": len(self.image_hints),
            "has_real_photo": self.has_real_photo,
            "sources_used": list(self.sources_used),
            "load_errors": list(self.load_errors),
        }


def _clip(v: object, n: int = _MATERIAL_FIELD_MAX) -> str:
    s = str(v or "").strip()
    return s[:n]


def load_client_materials(brand_id: int) -> dict:
    """读 `client_materials`(软删除过滤)。列名已对生产核过,不是猜的。

    🔴 2026-08-03:列表**改成从 `MATERIAL_FIELDS` 取**,不再手写。
       原来这里只 SELECT 了 6 列,而 `load_material_summary` 按 10 项算填充度 ——
       于是「资料 9/10」的客户,喂给 LLM 的其实只有 6 项:
       方法论 / 价格档位 / 服务区域 / 团队规模 **四项白填了**。
       用户看得到分数、模型看不到内容,这种不一致最难发现。
       两个函数现在共用同一份列名,想漏也漏不掉。
    """
    from db.connection import get_connection
    cols = ", ".join(k for k, _ in MATERIAL_FIELDS)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {cols}
                 FROM client_materials
                WHERE brand_id = %s AND COALESCE(is_deleted, FALSE) = FALSE
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1""",
            (brand_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def load_authorized_images(brand_id: int, limit: int = 8) -> List[dict]:
    """读 `brand_image_assets`,**只取已授权且已确权**的。

    🔴 `publish_allowed` + `rights_confirmed` 两个位都要为真 —— 版权面 fail-closed,
       没确权的图不进对外发布物的生成上下文。
       生产实测这两位的与**不是冗余**:232 行里 publish_allowed=1 的有 129 行,
       但两位都为 1 的只有 33 行(96 行是"允许发但没确权")。少判一位多放行 96 行。

    🔴🔴 这两列在生产是 **smallint(0/1)**,不是 boolean。
       原先写的 `COALESCE(publish_allowed, FALSE) = TRUE` 在 PG 里直接报
       `COALESCE types smallint and boolean cannot be matched` —— 整条查询抛异常,
       被上层 `except` 吞掉后降级成"这个客户没有图片素材"。
       表现是**功能安静地不存在**,不是报错,所以能一直没人发现。
       (成对对照已在生产实跑:原写法 ERROR / 本写法返回 33。)
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT image_type, title, caption, alt_text,
                      vision_summary, ocr_text, tags
                 FROM brand_image_assets
                WHERE brand_id = %s
                  AND COALESCE(publish_allowed, 0) = 1
                  AND COALESCE(rights_confirmed, 0) = 1
                ORDER BY id DESC
                LIMIT %s""",
            (brand_id, max(1, min(int(limit), 30))),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── 右栏「知识库 / 写作资料」卡的明细 ─────────────────────────
# (key, 用户可见名)。10 项是 client_materials 里真正的内容字段 ——
# 生产实测单个品牌通常填到 8-10 项,所以"N/10"是个有意义的进度,不是装饰。
MATERIAL_FIELDS = (
    ("company_intro", "公司简介"),
    ("core_selling_points", "核心卖点"),
    ("unique_value", "差异化价值"),
    ("case_studies", "案例"),
    ("credentials", "资质背书"),
    ("testimonials", "客户证言"),
    ("methodology", "方法论"),
    ("pricing_tiers", "价格档位"),
    ("service_area", "服务区域"),
    ("team_size", "团队规模"),
)

# 这条内容实际用到了哪些来源 → 人话。key 与 BrandContext.sources_used 对齐。
SOURCE_LABELS = {
    "client_materials": "客户资料",
    "client_knowledge_base": "知识库检索",
    "brand_image_assets": "已授权图片",
}


def load_material_summary(brand_id: int) -> dict:
    """资料填充度:哪几项填了、共几项。列名已对生产核过。"""
    from db.connection import get_connection
    cols = ", ".join(k for k, _ in MATERIAL_FIELDS)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT {cols} FROM client_materials
                 WHERE brand_id = %s AND COALESCE(is_deleted, FALSE) = FALSE
                 ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
            (brand_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    items = []
    for key, label in MATERIAL_FIELDS:
        present = bool(str((row or {}).get(key) or "").strip())
        items.append({"key": key, "label": label, "present": present})
    return {"filled": sum(1 for i in items if i["present"]),
            "total": len(MATERIAL_FIELDS), "items": items}


def resolve_thumb(row: dict) -> str:
    """一行图片资产 → 缩略图地址。

    🔴 取法**照抄既有约定**(api/image_asset_api.py 里那两行):
       thumbnail_key → safe_size_key → public_url,前两者是存储 key 要补前导 `/`。
       另起一套会在换存储时和既有页面各错各的。
    抽成纯函数是为了能**直接测行为** —— 之前只断言"源码里有 thumbnail_key 这个词",
    把整行换成 `tk = None` 那个词还在 SQL 里,锁照样绿(实测该变异存活)。
    """
    tk = (row or {}).get("thumbnail_key") or (row or {}).get("safe_size_key")
    if tk:
        return "/" + str(tk).lstrip("/")
    return str((row or {}).get("public_url") or "")


# 🔴 `build_client_knowledge` 2026-08-03 **搬走了**,现在在
#    `services/client_knowledge.py`,写文章与做图文共用同一个。
#
#    搬家的原因不是整理代码,是同一个客户在两个页面显示的**分数不一样**:
#    写文章那边 7/8(m3 的 8 字段),图文这边 x/10(本模块的 MATERIAL_FIELDS)。
#    Review 裁定分数以 **m3 的 8 字段**为准;本模块的 10 项降级为
#    **生成上下文**专用(信息量更大,喂 LLM 用),不再参与任何分数。
#
#    本模块保留的仍然是"图文这条链自己的"东西:
#      - `load_material_summary` / `count_authorized_images` /
#        `load_authorized_image_thumbs` —— 底层读取,被上面那个共用函数调用;
#      - `describe_material_stock`(库存)与 `describe_sources`(出处)——
#        这两个**依旧严格分开**,合并会拿库存冒充出处。


def describe_material_stock(summary: Optional[dict], image_count: int = 0) -> str:
    """这个客户**手上有什么**资料 → 一句人话(入口页选客户那一步用)。

    🔴 与 `describe_sources` **严格分开,别合并**:
       - 本函数 = 库存("这个客户有 7 项资料、13 张图")
       - describe_sources = 出处("这条内容实际引用了哪几样")
       有资料 ≠ 这条用上了。合成一个函数就会拿库存冒充出处,
       用户会以为内容引用了实际没引用的东西 —— 那比不显示更糟。

    纯函数(不查库),便于直接喂输入断输出。
    """
    items = list((summary or {}).get("items") or [])
    if not items:
        return ""
    have = [str(i.get("label") or "") for i in items if i.get("present")]
    miss = [str(i.get("label") or "") for i in items if not i.get("present")]
    parts: List[str] = []
    if have:
        head = "、".join(have[:4])
        more = f" 等 {len(have)} 项" if len(have) > 4 else ""
        parts.append(f"已有:{head}{more}")
    if image_count > 0:
        parts.append(f"图片 {int(image_count)} 张")
    if miss:
        parts.append(f"还缺:{'、'.join(miss[:3])}{'…' if len(miss) > 3 else ''}")
    return " · ".join(parts)


def describe_sources(generation_meta: Optional[dict]) -> List[str]:
    """这条内容【实际用到】了哪些来源 → 人话。

    🔴 读的是生产时落进 generation_meta 的 `kb_sources`(真实留痕),
       **不是**"这个客户有什么资料"。有资料 ≠ 这条用上了 ——
       拿库存冒充出处,用户会以为内容引用了实际没引用的东西。
    """
    raw = (generation_meta or {}).get("kb_sources") or []
    return [SOURCE_LABELS.get(s, s) for s in raw]


def load_authorized_image_thumbs(brand_id: int, limit: int = 12) -> List[dict]:
    """已授权图片的缩略图。

    🔴 thumb 取法**照抄既有约定**(api/image_asset_api.py 里那两行):
       thumbnail_key → safe_size_key → public_url,前两者要补前导 `/`。
       另起一套取法会在换存储时和既有页面各错各的。
    🔴 授权判据与 load_authorized_images 完全一致(smallint 0/1),
       两处若走岔,右栏显示"有 13 张"而生成时一张都用不上。
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT title, caption, alt_text, public_url,
                      thumbnail_key, safe_size_key
                 FROM brand_image_assets
                WHERE brand_id = %s
                  AND COALESCE(publish_allowed, 0) = 1
                  AND COALESCE(rights_confirmed, 0) = 1
                ORDER BY id DESC LIMIT %s""",
            (brand_id, max(1, min(int(limit), 30))),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    out: List[dict] = []
    for r in rows:
        thumb = resolve_thumb(r)
        if not thumb:
            continue
        out.append({"thumb": thumb,
                    "alt": _clip(r.get("title") or r.get("caption")
                                 or r.get("alt_text"), 40)})
    return out


def count_authorized_images(brand_id: int) -> int:
    """已授权图片总数(缩略图只取前 N 张,但计数要给全量)。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) AS c FROM brand_image_assets
                WHERE brand_id = %s
                  AND COALESCE(publish_allowed, 0) = 1
                  AND COALESCE(rights_confirmed, 0) = 1""",
            (brand_id,),
        )
        return int(cur.fetchone()["c"])
    finally:
        conn.close()


async def _load_kb_snippets(brand_id: int, query: str,
                            errors: Optional[List[str]] = None) -> List[str]:
    """走既有 unified RAG 的客户库(按 brand_id 隔离),不新造检索层。"""
    try:
        from tools.unified_knowledge import get_unified_rag
        rag = get_unified_rag()
        results = await rag.retrieve(query=query, brand_id=brand_id,
                                     top_k=_KB_TOP_K, use_client=True, use_role=False)
        return [_clip(r.get("content"), _KB_CHUNK_MAX)
                for r in (results or []) if r.get("content")]
    except Exception as e:  # noqa: BLE001 - 检索失败不该挡住生产
        logger.error("[douyin-kb] 客户知识库检索失败 brand=%s: %s", brand_id, str(e)[:200])
        if errors is not None:
            errors.append("client_knowledge_base")
        return []


async def build_brand_context(brand_id: Optional[int], keyword: str,
                              brand_name: str = "") -> BrandContext:
    """汇总客户上下文。任何一路失败都降级为"没有那一路",**绝不整体失败** ——
    没素材照样能生成(只是通用一些),但不能因为取素材失败就不让用户做内容。
    """
    import asyncio

    ctx = BrandContext(brand_name=brand_name or "")
    if not brand_id:
        return ctx

    try:
        mat = await asyncio.to_thread(load_client_materials, int(brand_id))
    except Exception as e:  # noqa: BLE001
        # ERROR 级 + 记进 load_errors:取失败必须看得见,不能表现成"客户没资料"
        logger.error("[douyin-kb] client_materials 读取失败 brand=%s: %s",
                     brand_id, str(e)[:200])
        ctx.load_errors.append("client_materials")
        mat = {}
    if mat:
        ctx.company_intro = _clip(mat.get("company_intro"))
        ctx.core_selling_points = _clip(mat.get("core_selling_points"))
        ctx.unique_value = _clip(mat.get("unique_value"))
        ctx.case_studies = _clip(mat.get("case_studies"))
        ctx.credentials = _clip(mat.get("credentials"))
        # 这四项 2026-08-03 才接进来 —— 在此之前客户填了也进不了 prompt
        ctx.testimonials = _clip(mat.get("testimonials"))
        ctx.methodology = _clip(mat.get("methodology"))
        ctx.pricing_tiers = _clip(mat.get("pricing_tiers"))
        ctx.service_area = _clip(mat.get("service_area"))
        ctx.team_size = _clip(mat.get("team_size"))
        if any([ctx.company_intro, ctx.core_selling_points, ctx.unique_value]):
            ctx.sources_used.append("client_materials")

    try:
        imgs = await asyncio.to_thread(load_authorized_images, int(brand_id))
    except Exception as e:  # noqa: BLE001
        logger.error("[douyin-kb] brand_image_assets 读取失败 brand=%s: %s",
                     brand_id, str(e)[:200])
        ctx.load_errors.append("brand_image_assets")
        imgs = []
    for im in imgs:
        desc = _clip(im.get("vision_summary") or im.get("caption")
                     or im.get("alt_text") or im.get("title"), 120)
        if not desc:
            continue
        if str(im.get("image_type") or "").lower() in ("logo", "brand_logo"):
            ctx.logo_hints.append(desc)
        else:
            ctx.image_hints.append(desc)
    if ctx.logo_hints or ctx.image_hints:
        ctx.sources_used.append("brand_image_assets")

    snippets = await _load_kb_snippets(int(brand_id),
                                       f"{keyword} {brand_name}".strip(),
                                       ctx.load_errors)
    if snippets:
        ctx.kb_snippets = snippets
        ctx.sources_used.append("client_knowledge_base")

    return ctx
