"""客户知识库「这个客户有什么」的**唯一**计算处(跨板块 SSOT)

2026-08-03 · Owner:「图2图3这里,知识库应该是同一个地方,外框应该是差不多的」。

## 为什么必须收成一份(调研实锤,不是洁癖)

改之前同一个客户在两个页面显示的**分数不一样**:

| 页面 | 计算处 | 分母 |
|---|---|---|
| 写文章(写作大厅右栏) | `api/m3_material_confirm_api._summarize_materials` | **8** |
| 制作 GEO 图文(入口页/详情页) | `services/geo_douyin/knowledge_context.MATERIAL_FIELDS` | **10** |

不是"外框长得不一样",是**数字对不上** —— 用户在一个页面看到 7/8、
换个页面看到 7/10,同一份资料两个进度。

## 谁是 SSOT

**分数以 m3 的 8 字段为准**(Review 裁定 §18)。理由:它是既有实现,而且和
「客户确认链接 `/m`」那条链**共用同一份快照** —— 发给客户看的和内部看的必须同源。
后加的 10 项口径退让。

## 但全量物料**不能丢**

10 项 `client_materials`(方法论/价格档位/服务区域/团队规模…)是喂给 LLM 的
生成上下文,信息量比 8 项展示口径大。所以本模块**两个都返回**:

  - `materials`      → 8 字段,**显示/分数**用(唯一口径)
  - `materials_full` → 10 项,**生成上下文**用(不参与任何分数)

🔴 两者绝不可互相顶替:拿 full 去显示 = 又回到两个分母;
   拿 8 字段去喂 LLM = 白丢一半客户资料。

## 一处过渡说明

本模块在函数内 `from api.m3_material_confirm_api import ...` —— service 反向依赖 api
层是**层次倒置**,明知故犯的理由是:那几个 `_` 私有函数事实上已经是跨模块共享的
(`server.py:19685` 早就在这么 import),把它们搬进 services 会动到写文章链的
既有 import 面,超出本包范围。**先统一口径,搬家另立一单。**
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("GEO-ClientKnowledge")

# m3 的 8 个字段 → 用户可见名。
# 🔴 只是**展示层**的翻译:key 与 filled/total 全部来自 m3 的 `_summarize_materials`,
#    这里一个字段都不许增删 —— 增一个就是又造了第三份口径。
M3_FIELD_LABELS = {
    "company_intro": "公司简介",
    "core_value": "核心价值",
    "usp": "差异化卖点",
    "selling_points": "卖点清单",
    "products": "产品与场景",
    "customers": "客户与痛点",
    "cases": "案例",
    "testimonials": "客户证言",
}


def _empty() -> dict:
    return {
        "has_brand": False,
        "load_failed": False,
        "materials": {"filled": 0, "total": 0, "items": []},
        "materials_full": {"filled": 0, "total": 0, "items": []},
        "images": {"count": 0, "thumbs": []},
        "contact_ready": False,
        "summary": "",
    }


def load_display_materials(brand_id: int) -> dict:
    """8 字段口径的资料填充度(**分数 SSOT**)。

    直接调 m3 的 `_summarize_materials`,不在这里重算 —— 重算就是第三份实现。
    """
    from api.m3_material_confirm_api import (
        _build_materials_snapshot,
        _get_brand_info,
        _get_materials_for_brand,
        _get_profile_for_brand,
    )

    brand_info = _get_brand_info(int(brand_id)) or {}
    profile = _get_profile_for_brand(int(brand_id))
    materials = _get_materials_for_brand(int(brand_id))
    snapshot = _build_materials_snapshot(brand_info, profile, materials)

    from api.m3_material_confirm_api import _summarize_materials

    summary = _summarize_materials(snapshot)
    filled = set(summary.get("fields_filled") or [])
    missing = list(summary.get("fields_missing") or [])
    items = [
        {"key": key, "label": M3_FIELD_LABELS.get(key, key), "present": key in filled}
        for key in list(filled) + missing
    ]
    # 顺序按 M3_FIELD_LABELS 的声明顺序,免得每次请求顺序都在跳
    order = {k: i for i, k in enumerate(M3_FIELD_LABELS)}
    items.sort(key=lambda x: order.get(x["key"], 99))
    return {
        "filled": int(summary.get("filled_count") or 0),
        "total": int(summary.get("total_fields") or 0),
        "items": items,
        "intro_excerpt": summary.get("intro_excerpt") or "",
        "usp_excerpt": summary.get("usp_excerpt") or "",
    }


def build_client_knowledge(brand_id: Optional[int], *,
                           with_thumbs: bool = False) -> dict:
    """「这个客户有什么」——写文章与做图文**共用这一个**。

    🔴 只回"库存",**不回"这条内容用了什么"** —— 后者是
       `services/geo_douyin/knowledge_context.describe_sources` 的事,
       依赖具体某条内容的 generation_meta。两者严格分开:
       有资料 ≠ 这条用上了,拿库存冒充出处比不显示更糟。

    🔴 `load_failed` 与"没有资料"必须分开:前者是我们的 bug(SQL 类型错这类),
       后者是客户没填。混成一个的话,一个查询异常能安静躺一整个版本。
    """
    from services.geo_douyin.knowledge_context import (
        count_authorized_images,
        describe_material_stock,
        load_authorized_image_thumbs,
        load_material_summary,
    )

    empty = _empty()
    if not brand_id:
        return empty

    try:
        display = load_display_materials(int(brand_id))
        full = load_material_summary(int(brand_id))
        image_count = count_authorized_images(int(brand_id))
        thumbs = load_authorized_image_thumbs(int(brand_id)) if with_thumbs else []
    except Exception as e:  # noqa: BLE001
        logger.error("[client-kb] 客户资料读取失败 brand=%s: %s", brand_id, str(e)[:200])
        return {**empty, "has_brand": True, "load_failed": True}

    contact_ready = False
    try:
        from db.profile_db import get_contact_info_by_brand
        c = get_contact_info_by_brand(int(brand_id)) or {}
        contact_ready = bool(c.get("phone") or c.get("wechat"))
    except Exception:  # noqa: BLE001 - 联系方式读不到不该让整张卡打不开
        contact_ready = False

    return {
        "has_brand": True,
        "load_failed": False,
        # 显示/分数:8 字段(m3 口径,唯一)
        "materials": {"filled": display["filled"], "total": display["total"],
                      "items": display["items"]},
        "intro_excerpt": display.get("intro_excerpt") or "",
        "usp_excerpt": display.get("usp_excerpt") or "",
        # 生成上下文:10 项 client_materials(不参与分数)
        "materials_full": full,
        "images": {"count": int(image_count or 0), "thumbs": thumbs},
        "contact_ready": contact_ready,
        # 摘要按**全量**说("这个客户有几项资料几张图"),因为它描述的是库存不是评分
        "summary": describe_material_stock(full, int(image_count or 0)),
    }


# ===========================================================================
# 人工确认竞品 · 跨板块共用(2026-08-07 P1-1)
# ===========================================================================
#
# ## 为什么必须收成一份
#
# 改之前同一个客户的"竞品是谁"有**三套答案**:
#
# | 入口 | 来源 | 后果 |
# |---|---|---|
# | 文章中心 | `quotes.competitor_list` + `competitor_mode` | 代理在报价阶段确认过 |
# | 图文选题 | `keyword_insights.brands_found` | 监测蒸馏出来的 |
# | 图文榜单 | `geo_research_answer_entities` | 行业研究攒的 |
#
# 结果是:代理在报价里**亲手确认过**的竞品,榜单可能一家都看不到;
# 两个入口做出来的竞品名单可以完全不同。
#
# ## 🔴 一条不加就会出事的闸:`competitor_mode`
#
# 生产实测(2026-08-07 只读通道现取):
#
#     competitor_mode:  fictional 340 · real 47 · semi 4 · evidence_only 1
#     有名单的报价 53 单 → real 46 / semi 4 / evidence_only 1 / **fictional 2**
#
# `fictional` 是**演示用的编造竞品**。不按 mode 过滤就直接当"人工确认名单"用,
# 等于把编出来的公司名放进一个对外发布的排行榜 —— 那正是这个功能存在的理由
# 要防的事(治理 SSOT:虚构企业属 H0)。
#
# ## 🔴 血缘要精确,不许"取最近一份报价"
#
# 关联走 `confirmed_keywords`(quote_id ↔ keyword),不是按 brand_id 取最新那单。
# 猜出来的关联会把 A 词的竞品用到 B 词的内容上,而且看不出来。

#: 只有这几种模式下的竞品名单是**真实**的。`fictional` 是演示数据,绝不进榜单。
REAL_COMPETITOR_MODES: tuple = ("real", "semi", "evidence_only")

#: 只有这些状态的报价算数(草稿里的还没定)。
CONFIRMED_QUOTE_STATUSES: tuple = ("confirmed", "paid")


def _normalized_engines(raw) -> list:
    """`confirmed_by` 里的引擎写法归一。

    🔴 这是本仓**第三套**引擎写法(`Doubao` / `DashScope` / `Kimi` /
       `秘塔直搜` / `手动添加`),前两套是 answer_entities 的中文与
       source_signals 的小写拉丁。归一走既有 SSOT,本处不建第四套。
       归一不掉的(秘塔直搜/手动添加)**原样保留** —— 它们是核验来源,
       不是 AI 引擎,吞掉会让"这条是人工加的"这个事实消失。
    """
    from services.research_monitor.answer_entity_extractor import _normalize_engine

    out: list = []
    for x in (raw or []):
        s = _normalize_engine(str(x or "").strip())
        if s and s not in out:
            out.append(s)
    return out


def _profile_citable(item) -> bool:
    """画像可引用判定 —— 转调合同,**本模块不另立口径**。取不到一律 False(保守)。"""
    try:
        from writing.competitor_name_contract import is_profile_citable
        return bool(is_profile_citable(item))
    except Exception:   # noqa: BLE001 — 合同取不到不该挡住整条读取
        return False


def load_confirmed_competitors(brand_id, keywords=None) -> list:
    """这个客户**在报价里亲手确认过**的竞品(带完整来源)。

    返回每条:`entity_id` / `display_name` / `source_type` / `source_id` /
    `confirm_status` / `observed_at` / `evidence`。

    🔴 `entity_id` 用 `build_answer_entity_key` —— 与
       `geo_research_answer_entities.entity_key` **同一个键空间**。
       这才是"共用"的实质:人工名单能直接和证据表 join,
       而不是两张各说各话的名字列表。

    `keywords` 非空时按**血缘**收窄(confirmed_keywords.quote_id ↔ keyword);
    留空 = 这个客户全部已确认报价。取不到一律返回 `[]`,**不抛异常**(永不中断)。
    """
    if not brand_id:
        return []
    kws = [str(k).strip() for k in (keywords or []) if str(k or "").strip()]
    params: list = [int(brand_id), list(REAL_COMPETITOR_MODES),
                    list(CONFIRMED_QUOTE_STATUSES)]
    kw_clause = ""
    if kws:
        # 🔴 精确血缘:这个词是从哪张报价单上买的。**不按 brand_id 取最近一单猜。**
        kw_clause = (" AND q.id IN (SELECT ck.quote_id FROM confirmed_keywords ck"
                     "              WHERE ck.quote_id = q.id AND ck.keyword = ANY(%s))")
        params.append(kws)
    sql = f"""
        SELECT q.id, q.status, q.updated_at, q.competitor_mode, q.competitor_list
          FROM quotes q
         WHERE q.brand_id = %s
           AND q.competitor_mode = ANY(%s)
           AND q.status = ANY(%s)
           AND q.competitor_list IS NOT NULL
           AND q.competitor_list <> ''{kw_clause}
         ORDER BY q.updated_at DESC NULLS LAST
         LIMIT 20
    """
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 取不到竞品不该挡住整条内容
        logger.warning("[client-kb] 人工确认竞品读取失败 brand=%s: %s",
                       brand_id, str(e)[:200])
        return []

    from services.research_monitor.answer_entity_extractor import build_answer_entity_key

    out: list = []
    seen: set = set()
    for r in rows:
        raw = r.get("competitor_list")
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            # 🔴 2026-08-08:`excluded` **必须过滤** —— 此前本函数不看它,
            #    而文章链(`article_writer._load_db_competitors`)和前端
            #    (写作大厅显示「N家已排除」)都在过滤。结果是:代理在写作大厅里
            #    手工排除掉的竞品,榜单链照样把它捞回来放进对外发布的榜单。
            #    生产实测 428 条里 28 条已被人工排除 —— 全是榜单链会误用的。
            if isinstance(it, dict) and it.get("excluded"):
                continue
            name = (str(it.get("name") or "").strip()
                    if isinstance(it, dict) else str(it or "").strip())
            if not name:
                continue
            key = build_answer_entity_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            ev = it if isinstance(it, dict) else {}
            out.append({
                "entity_id": key,
                "display_name": name,
                "source_type": "quote_confirmed",
                "source_id": int(r.get("id") or 0),
                "confirm_status": str(r.get("status") or ""),
                "observed_at": (r["updated_at"].isoformat()
                                if r.get("updated_at") is not None else None),
                # 🔴 2026-08-08:画像**带出来**(此前在这一层就被丢掉了)。
                #    它是本系统里对同行最丰富的一份结构化资料(联网检索后综述,
                #    带 verify_source 链接与引擎确认),而三条链此前都只拿名字:
                #      · 本函数不返回它(榜单链根本看不见)
                #      · `article_writer._load_db_competitors` 有意只传名称
                #      · `placement_service` 收集了 `comp_profiles` 却零读取方
                #
                # 🔴 **只作生成上下文,不作可引用事实**。理由不是合规表演:
                #    「某某拥有五大生产基地/98.6% 交付率」这类句子是我们自己
                #    LLM 综述出来的,写错了是对**第三方公司**的不实陈述。
                #    名字有 `verify_source` 链接核验,能力描述没有 —— 这条线
                #    `competitor_name_contract` v2 早就划了(名字已核验 ≠ 能力已核验),
                #    本次一格没动它,只是让画像能进 prompt 当选材背景。
                "profile": str(ev.get("profile") or ev.get("desc") or "").strip(),
                # 🔴 B 层(2026-08-08):这段画像能不能**引用进成品**。
                #    判定收在 `writing/competitor_name_contract` 一处
                #    (那里已经是"名字能不能写"的全仓唯一判定点),本处不另立口径。
                #    门槛不是 source_count —— 实测它是**检索命中量**不是可信度:
                #    源数最高那条(7 源)恰恰是「"环保防霉胶"并非单一品牌」这种品类说明。
                "profile_citable": _profile_citable(ev),
                "evidence": {
                    "competitor_mode": str(r.get("competitor_mode") or ""),
                    "verify_source": str(ev.get("verify_source") or ""),
                    "metaso_verified": bool(ev.get("metaso_verified")),
                    "confirmed_by": _normalized_engines(ev.get("confirmed_by")),
                    "source_count": int(ev.get("source_count") or 0),
                    # 画像可信度分级信号(UI 上就显示成「高 2源」/「中 1源」)。
                    # B 层"可引用素材"要用它分级;A 层只喂上下文,不分级。
                    "confidence": str(ev.get("confidence") or ""),
                },
            })
    return out
