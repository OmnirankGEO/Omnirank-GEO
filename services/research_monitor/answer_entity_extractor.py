"""[答案实体] 从 geo_research_raw 答案抽「真实推荐图谱」(品牌实体级)。

净增量(B1-B6 未做):AI 答案里真实推荐了哪些品牌/公司/产品 + 排第几 + 为什么 + 哪些引擎推。

🔴 粒度(出口严审 P1-1 修正):`geo_research_raw` 一行一 citation·共享 answer_text(见 api/research_monitor_citations_api.py:6)。
抽取按 **answer 组**(db.fetch_answer_groups_for_extraction:GROUP BY engine_norm/batch/MD5(answer_text),与 citations_api 同款)——
一条答案抽一次、算一次,不按 raw 行(否则 N 倍 LLM + N 倍计数)。

🔴 强制复用(SPEC §4,防平行造轮):
- **分组/join 口径**:复用 citations_api 的 answer_md5 分组范式(db 层);raw_id/batch_id/engine + 现算 industry_key。
- **品牌抽取原语**:复用 tools/distillation/distiller.distill_keyword(不写第 3 个抽取器)· BrandInfo 为 schema 蓝本。
- **引擎归一**:db 层 _ENGINE_NORMALIZE_SQL(SQL 侧,分组时归一);Python 侧 _ENGINE_CANONICAL 同口径(读侧过滤/rank 对齐用)。
- **实体键归一**:build_entity_key(canonicalize_media_name.lower()) + brand-alias 种子折叠。
- LLM 失败:走 distiller 的 fallback(quality_flag='degraded')→ 不写实体、不造假、落 degraded fact 标记(幂等收敛)。

纪律:shadow-only · dry_run 优先(不调 LLM)· 真跑走后台 + async 互斥 + 分页(WORKERS=1 不冻站)· 不改报价/发布/扣费。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.media_entity_flywheel import (
    build_entity_key,
    canonicalize_media_name,
    normalize_industry_key,
)
from db.research_answer_entity_db import (
    EXTRACTOR_VERSION,
    count_extractable_answers,
    fetch_answer_groups_for_extraction,
    upsert_answer_fact_with_entities,
)

logger = logging.getLogger("GEO-AnswerEntityExtractor")

_MAX_ANSWER_CHARS = 8000

# [引擎归一] 与 db/geo_source_signals_db._ENGINE_NORMALIZE_SQL 同口径的 Python 镜像(读侧过滤/rank 对齐用)。
_ENGINE_CANONICAL = {
    "doubao": "豆包", "豆包": "豆包",
    "kimi": "Kimi",
    "deepseek": "DeepSeek",
    "qwen": "千问", "千问": "千问",
    "dashscope": "千问", "doubao_app": "豆包",
}

# [实体键跨语言别名种子] 🔴 canonicalize_media_name+lower 只折叠大小写/空格,不折叠中英别名。
# 按 _INDUSTRY_ALIASES 同 idiom 加一层 brand-alias 种子折叠。**seed-extensible**:
# 只覆盖常见品牌;未收录的跨语言名仅按大小写/空格折叠(诚实局限,见交付说明)。
_BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "阿里云": ("阿里云", "阿里巴巴云", "alibabacloud", "alicloud", "aliyun"),
    "腾讯云": ("腾讯云", "tencentcloud"),
    "华为云": ("华为云", "huaweicloud"),
    "百度智能云": ("百度智能云", "百度云", "baiducloud", "baiduaicloud"),
    "亚马逊云": ("亚马逊云", "aws", "amazonwebservices", "amazoncloud"),
    "微软云": ("微软云", "azure", "microsoftazure"),
    "字节跳动": ("字节跳动", "bytedance"),
    "美团": ("美团", "meituan"),
}
_BRAND_ALIAS_LOOKUP: dict[str, str] = {}
for _canon, _aliases in _BRAND_ALIASES.items():
    _canon_norm = canonicalize_media_name(_canon).lower()
    for _a in _aliases:
        _BRAND_ALIAS_LOOKUP[canonicalize_media_name(_a).lower()] = _canon_norm


def _normalize_engine(engine: str) -> str:
    return _ENGINE_CANONICAL.get((engine or "").strip().lower(), (engine or "").strip())


def build_answer_entity_key(name: str) -> str:
    """归一实体键:canonicalize+lower → brand-alias 种子折叠 → build_entity_key(me_ 前缀 sha1)。

    阿里云 / Alibaba Cloud / alibaba cloud → 同一 key(种子命中);未收录名仅按大小写/空格折叠。
    """
    base = canonicalize_media_name(name).lower()
    if not base:
        return ""
    folded = _BRAND_ALIAS_LOOKUP.get(base, base)
    return build_entity_key(folded)


def _derive_confidence(brand: Any, engine: str) -> float:
    """透明启发式(非伪造):成功解析基线 0.7;该引擎有明确 rank +0.2;有推荐理由 +0.1(封顶 1.0)。"""
    conf = 0.7
    ranks = getattr(brand, "ranks", None) or {}
    if any(_normalize_engine(str(k)) == engine or str(k).strip().lower() == engine.lower() for k in ranks):
        conf += 0.2
    if getattr(brand, "recommendation_reasons", None):
        conf += 0.1
    return round(min(conf, 1.0), 3)


def _brand_rank_for_engine(brand: Any, engine: str) -> int | None:
    ranks = getattr(brand, "ranks", None) or {}
    for k, v in ranks.items():
        if _normalize_engine(str(k)) == engine or str(k).strip().lower() == engine.lower():
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    if len(ranks) == 1:  # 单引擎场景:ranks 只有一个 → 取它
        try:
            return int(next(iter(ranks.values())))
        except (TypeError, ValueError):
            return None
    return None


def _map_brand_to_entity(brand: Any, mention_rank: int, engine: str, source_urls: list[str],
                         llm_model: str) -> dict[str, Any] | None:
    name = (getattr(brand, "name", "") or "").strip()
    if not name:
        return None
    ekey = build_answer_entity_key(name)
    if not ekey:
        return None
    reasons = [str(r).strip() for r in (getattr(brand, "recommendation_reasons", None) or []) if str(r).strip()][:8]
    phrases = [str(p).strip() for p in (getattr(brand, "description_keywords", None) or []) if str(p).strip()][:8]
    return {
        "entity_name": name,
        "entity_key": ekey,
        "entity_type": "brand",
        "recommendation_rank": _brand_rank_for_engine(brand, engine),
        "mention_rank": mention_rank,
        "recommendation_reasons": reasons,
        "evidence_phrases": phrases,
        "source_urls": source_urls,
        "confidence": _derive_confidence(brand, engine),
        "llm_model": llm_model,
        "extractor_version": EXTRACTOR_VERSION,
    }


def _build_fact(group: dict[str, Any], engine: str, industry: str, industry_key: str,
                query: str, answer_text: str, source_urls: list[str], quality_flag: str) -> dict[str, Any]:
    raw_ids = [int(x) for x in (group.get("raw_ids") or []) if x is not None]
    if not raw_ids and group.get("anchor_raw_id") is not None:
        raw_ids = [int(group["anchor_raw_id"])]
    return {
        "raw_id": int(group.get("anchor_raw_id") or (raw_ids[0] if raw_ids else 0)),
        "industry": industry,
        "industry_key": industry_key,
        "query": query,
        "engine": engine,
        "batch_id": group.get("batch_id") or "",
        "answer_hash": group.get("answer_md5") or "",  # SQL 侧 MD5(answer_text)· answer 级去重键
        "raw_ids": raw_ids,
        "citation_urls": source_urls[:20],
        "answer_excerpt": (answer_text or "")[:400],
        "quality_flag": quality_flag,
        "extractor_version": EXTRACTOR_VERSION,
    }


async def extract_entities_from_group(group: dict[str, Any]) -> dict[str, Any]:
    """对单个 answer 组(折叠 citation 行后)抽实体(纯:不写 DB,便于测试)。

    返回 {fact, entities, quality_flag}。LLM 失败 → quality_flag='degraded',entities=[]。
    复用 distill_keyword(min_platforms=1 允许单答案);known_brands=[] 不做外部归一。
    """
    from tools.distillation.distiller import distill_keyword, DEFAULT_MODEL

    raw_answer = group.get("answer_text") or ""
    # [GEO-R1-CAN-011] LLM 输入按固定预算截断;超预算时 char 8000 之后的实体/理由会被静默丢弃。
    # 记录截断事实,使遗漏「loss-aware」(fact.quality_flag 标 'truncated'),可被 only_pending=False 重抽召回。
    answer_truncated = len(raw_answer) > _MAX_ANSWER_CHARS
    answer_text = raw_answer[:_MAX_ANSWER_CHARS]
    engine = (group.get("engine") or "").strip()  # SQL 已归一
    query = group.get("query") or ""
    industry = group.get("industry") or ""
    industry_key = normalize_industry_key(industry)

    result, _tokens, quality_flag = await distill_keyword(
        keyword=query,
        client_brand="",
        known_brands=[],
        platform_responses={engine or "engine": answer_text},
        min_platforms=1,
    )

    source_urls = list(group.get("citation_urls") or [])
    for src in (getattr(result, "sources_cited", None) or []):
        ref = (getattr(src, "url_or_reference", "") or "").strip()
        if ref and ("://" in ref or "." in ref) and ref not in source_urls:
            source_urls.append(ref)
    source_urls = source_urls[:20]

    entities: list[dict[str, Any]] = []
    if quality_flag != "degraded":
        seen: set[str] = set()
        for idx, brand in enumerate(getattr(result, "brands", None) or [], start=1):
            ent = _map_brand_to_entity(brand, idx, engine, source_urls, DEFAULT_MODEL)
            if not ent or ent["entity_key"] in seen:
                continue
            seen.add(ent["entity_key"])
            entities.append(ent)

    # [GEO-R1-CAN-011] 截断且抽取未 degraded 时,fact 标 'truncated'(不覆盖 degraded 语义;
    # 不改 entities 门控——仍用 distiller 原始 quality_flag 决定是否写实体)。
    fact_flag = quality_flag
    if answer_truncated and quality_flag != "degraded":
        fact_flag = "truncated"
    fact = _build_fact(group, engine, industry, industry_key, query, answer_text, source_urls, fact_flag)
    return {"fact": fact, "entities": entities, "quality_flag": quality_flag}


# ---- rebuild 编排(dry_run 优先 · async 互斥 · 分页 · answer 级 · 不 inline 全库)----

_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = asyncio.Lock()


async def _acquire(key: str) -> bool:
    async with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return False
        _INFLIGHT.add(key)
        return True


async def _release(key: str) -> None:
    async with _INFLIGHT_LOCK:
        _INFLIGHT.discard(key)


def _industry_values(industry: str) -> list[str] | None:
    from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope
    if not industry or is_all_industry_scope(industry):
        return None
    return industry_filter_values(industry) or [industry]


def _persist_skip_marker(group: dict[str, Any], flag: str) -> None:
    """[review fix] 把抽取异常/降级的 answer 组落一条空 fact(entities=[]),
    使 only_pending(NOT EXISTS fact)下轮排除它 → 防无限重抽 + DESC 窗口饿死老组。
    (答案被回填/改写需重抽走 only_pending=False;answer_hash=MD5 已存供变更检测。)
    """
    answer_text = (group.get("answer_text") or "")[:_MAX_ANSWER_CHARS]
    industry = group.get("industry") or ""
    fact = _build_fact(
        group, (group.get("engine") or "").strip(), industry, normalize_industry_key(industry),
        group.get("query") or "", answer_text, list(group.get("citation_urls") or []), flag,
    )
    upsert_answer_fact_with_entities(fact, [])


async def rebuild_answer_entities(industry: str = "", limit: int = 200,
                                  dry_run: bool = True, only_pending: bool = True) -> dict[str, Any]:
    """rebuild 主入口(**answer 级**)。

    - dry_run=True(默认):只计数/预览,**不调 LLM**(零成本 · 可安全 inline)。
    - dry_run=False:真跑 LLM 抽取。调用方必须走后台任务(见 api 层),这里做 async 互斥 + 分页,
      单次最多处理 `limit` 个答案组,不 inline 全库(WORKERS=1 不冻站)。

    [review fix] 顶层 try/except 全包:后台 create_task 的协程异常(如 fetch DB 错)fail-soft 返 error dict,
    不冒泡。degraded/异常组落 fact 标记(见 _persist_skip_marker)→ 幂等可收敛,不再无限重抽。
    """
    try:
        values = _industry_values(industry)

        if dry_run:
            counts = count_extractable_answers(values, limit=limit)
            preview_groups = fetch_answer_groups_for_extraction(values, limit=min(limit, 10), only_pending=only_pending)
            preview = [{
                "anchor_raw_id": g.get("anchor_raw_id"),
                "engine": g.get("engine") or "",
                "industry_key": normalize_industry_key(g.get("industry") or ""),
                "query": (g.get("query") or "")[:60],
                "citation_rows": len(g.get("raw_ids") or []),
            } for g in preview_groups]
            return {"mode": "dry_run", "llm_called": False, **counts, "preview": preview}

        key = f"answer_entity_rebuild::{industry or 'all'}"
        if not await _acquire(key):
            return {"mode": "in_progress", "message": "该抽取正在进行中,请稍后再试(避免并发双成本)。", "key": key}
        scanned = 0
        facts_written = 0
        entities_written = 0
        degraded = 0
        try:
            groups = fetch_answer_groups_for_extraction(values, limit=limit, only_pending=only_pending)
            for group in groups:
                scanned += 1
                try:
                    out = await extract_entities_from_group(group)
                except Exception as e:  # fail-soft:单组失败不炸整批 + 落 skipped 标记防重抽
                    logger.warning(f"[answer-entity] anchor_raw_id={group.get('anchor_raw_id')} 抽取异常(标记 skipped): {e}")
                    try:
                        _persist_skip_marker(group, "skipped")
                    except Exception:
                        pass
                    degraded += 1
                    continue
                if out["quality_flag"] == "degraded":
                    # LLM 失败 → 落 degraded fact(entities=[]),下轮 only_pending 排除,防无限重抽
                    upsert_answer_fact_with_entities(out["fact"], [])
                    degraded += 1
                    continue
                res = upsert_answer_fact_with_entities(out["fact"], out["entities"])
                facts_written += 1
                entities_written += int(res.get("entity_written") or 0)
            logger.info(f"[answer-entity] rebuild done industry={industry or 'all'} answers={scanned} "
                        f"facts={facts_written} entities={entities_written} degraded={degraded}")
            return {
                "mode": "rebuild", "llm_called": True, "industry": industry or "all",
                "scanned": scanned, "facts_written": facts_written,
                "entities_written": entities_written, "degraded": degraded, "batch_limit": limit,
            }
        finally:
            await _release(key)
    except Exception as e:
        logger.error(f"[answer-entity] rebuild_answer_entities 失败 industry={industry or 'all'}: {e}")
        # [GEO-R1-CAN-012] 显式携带 status='failed':上游 flywheel_bridge 用 result.setdefault('status','success')
        # 归并阶段状态,若无此键会把抽取错误误判为 success。带 status 后 setdefault 不覆盖 → 阶段/整体正确降为 failed/partial。
        return {"mode": "error", "status": "failed", "llm_called": not dry_run, "error": str(e)[:200]}
