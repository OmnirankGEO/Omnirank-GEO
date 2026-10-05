"""
v3.8 · 反哺编排器 (CTO-13.3 · 2026-04-19 PLAN Q15)

职责：把单次 rerun / correct 产出的候选 items 按"差异阈值 → 投票 → 合并写库 → 审计"四步流程
沉淀到 `industry_knowledge` 共享池。

入口：
  - `feed_from_rerun(new_brief, profile, user_id, source_ref)` — /industry-brief/rerun 调
  - `feed_from_correct(...)` — （留给 C4 对接 correct 端点用）

设计原则（CTO-15.2 避坑 #1-4）：
  - merge 直接复用 tools.knowledge_layers.merge_with_layers，不重写
  - 投票用 multi_ai_voter.review_field_value（按字段值，非网页）
  - 环境变量开关：RERUN_REVERSE_FEED_ENABLED=true 默认开
  - 失败静默日志，不影响主流程（rerun 的 response 已经返给用户）
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

from tools.knowledge_layers import LAYER_CONFIG, merge_with_layers, META_PREFIX
from services.diff_gate import filter_duplicates
from services.multi_ai_voter import review_field_value
from db.reverse_feed_db import record_reverse_feed

logger = logging.getLogger("KnowledgeReverseFeed")


# ============================================================
# 可反哺字段配置
# ============================================================
# 按老板拍板（PLAN Q1）: L1 道层不反哺（120 天不过期 · 只 admin rebuild 触发）
# L1 法/术 + L2 器 都反哺

_L1_FEEDBACK_FIELDS = [
    # 法层
    "industry_jargon",
    "counter_consensus",
    "top_brands",
    # 术层
    "authority_sources",
]

_L2_FEEDBACK_FIELDS = [
    # 法层
    "typical_products",
    "target_audience",
    "differentiation_angles",
    # 术层
    "conversion_paths",
    "hot_formats",
    "content_types",
    "cta_templates",
    # 器层（永远叠加）
    "user_voices_pool",
    "case_evidence_pool",
]

# overwrite 类字段不反哺（单值覆盖无从"合并"，容易打坏共享）
_OVERWRITE_FIELDS_SKIP = {
    f for f, cfg in LAYER_CONFIG.items()
    if cfg.get("merge") == "overwrite"
}


def _is_enabled() -> bool:
    # 默认开（未设置视为开），只有显式 false 才关
    return os.environ.get("RERUN_REVERSE_FEED_ENABLED", "true").lower() != "false"


def _get_ik_row(level: str, industry: str, category: Optional[str]):
    """读共享池 industry_knowledge 条目 (id + knowledge dict)"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        if category:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge "
                "WHERE level=%s AND industry=%s AND category=%s",
                (level, industry, category)
            )
        else:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge "
                "WHERE level=%s AND industry=%s AND category IS NULL",
                (level, industry)
            )
        row = cur.fetchone()
        if not row:
            return None
        knowledge = row["knowledge"]
        if isinstance(knowledge, str):
            try:
                knowledge = json.loads(knowledge)
            except Exception:
                knowledge = {}
        return {"id": row["id"], "knowledge": knowledge or {}}
    finally:
        conn.close()


def _write_ik_row(ik_id: int, new_knowledge: dict):
    """把合并后的 knowledge 写回 industry_knowledge 表"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE industry_knowledge
            SET knowledge = %s,
                version = version + 1,
                generated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(new_knowledge, ensure_ascii=False), ik_id)
        )
        conn.commit()
    finally:
        conn.close()


async def _feed_one_field(
    *,
    level: str,
    industry: str,
    category: Optional[str],
    field_name: str,
    new_value: Any,
    source_type: str,
    source_ref: Optional[str],
    profile_id: Optional[str],
    user_id: Optional[int],
) -> dict:
    """处理单个字段的反哺：差异门控 → 投票 → 合并 → 审计。返回摘要 dict 用于整体汇总。"""
    cfg = LAYER_CONFIG.get(field_name)
    if not cfg:
        return {"field": field_name, "status": "skip_not_configured"}
    if cfg.get("layer") == "dao":
        return {"field": field_name, "status": "skip_dao_layer"}
    if field_name in _OVERWRITE_FIELDS_SKIP:
        return {"field": field_name, "status": "skip_overwrite_type"}

    # new_value 必须是非空 list（反哺只处理 list 类型）
    if not isinstance(new_value, list) or not new_value:
        return {"field": field_name, "status": "skip_not_list_or_empty"}

    # 读共享池现状
    ik = _get_ik_row(level, industry, category)
    if not ik:
        return {"field": field_name, "status": "skip_no_shared_row"}

    existing_items = ik["knowledge"].get(field_name) or []
    if not isinstance(existing_items, list):
        existing_items = []

    # Step 1: 差异门控（去掉已在共享池的重复）
    diff = filter_duplicates(field_name, new_value, existing_items)
    unique_new = diff["unique_new"]
    duplicates = diff["duplicates"]
    if not unique_new:
        return {
            "field": field_name,
            "status": "all_duplicates",
            "duplicate_count": len(duplicates),
        }

    # Step 2: LLM 投票（2 AI, threshold=1）
    vote = await review_field_value(
        industry=industry,
        field_name=field_name,
        new_items=unique_new,
        existing_items=existing_items[:20],   # 只给 20 条参考避免 token 爆
        category=category,
        source=source_type,
        threshold=1,
    )
    accepted = vote["accepted_items"]
    rejected = vote["rejected_items"]

    if not accepted:
        # 全被拒 → 只写审计不改共享池
        try:
            record_reverse_feed(
                source_type=source_type,
                source_ref=source_ref,
                profile_id=profile_id,
                user_id=user_id,
                level=level,
                industry=industry,
                category=category,
                field_name=field_name,
                new_items=[],
                rejected_items=rejected,
                duplicate_items=duplicates,
                vote_result={
                    "passed": False,
                    "final_verdict": vote["final_verdict"],
                    "vote_pass_count": vote["vote_pass_count"],
                    "vote_total": vote["vote_total"],
                    "verdicts": vote["verdicts"],
                },
                diff_summary={
                    "merged_count": 0,
                    "rejected_count": len(rejected),
                    "duplicate_count": len(duplicates),
                },
            )
        except Exception as e:
            logger.warning(f"[reverse-feed] audit write failed: {e}")
        return {
            "field": field_name,
            "status": "all_rejected_by_vote",
            "rejected_count": len(rejected),
            "duplicate_count": len(duplicates),
        }

    # Step 3: 合并写共享池
    try:
        merged_knowledge = merge_with_layers(
            ik["knowledge"],
            {field_name: accepted},
            archive_old=True,
        )
        _write_ik_row(ik["id"], merged_knowledge)
    except Exception as e:
        logger.error(f"[reverse-feed] merge write failed field={field_name}: {e}")
        return {"field": field_name, "status": "write_failed", "error": str(e)[:80]}

    # Step 4: 审计
    feed_id = None
    try:
        feed_id = record_reverse_feed(
            source_type=source_type,
            source_ref=source_ref,
            profile_id=profile_id,
            user_id=user_id,
            level=level,
            industry=industry,
            category=category,
            field_name=field_name,
            new_items=accepted,
            rejected_items=rejected,
            duplicate_items=duplicates,
            vote_result={
                "passed": True,
                "final_verdict": vote["final_verdict"],
                "vote_pass_count": vote["vote_pass_count"],
                "vote_total": vote["vote_total"],
                "verdicts": vote["verdicts"],
            },
            diff_summary={
                "merged_count": len(accepted),
                "rejected_count": len(rejected),
                "duplicate_count": len(duplicates),
            },
        )
    except Exception as e:
        logger.warning(f"[reverse-feed] audit write failed: {e}")

    return {
        "field": field_name,
        "status": "merged",
        "feed_id": feed_id,
        "merged_count": len(accepted),
        "rejected_count": len(rejected),
        "duplicate_count": len(duplicates),
    }


async def feed_from_rerun(
    *,
    new_brief: dict,
    profile: dict,
    user_id: int,
    updated_fields: list[str],
    source_ref: Optional[str] = None,
) -> dict:
    """
    rerun 成功后反哺入共享池。

    调用方式（推荐）:
        asyncio.create_task(feed_from_rerun(...))   # fire-and-forget, 不阻塞 rerun 响应

    Args:
        new_brief: deep_analyze_user 返回的完整 brief dict
        profile: client_profiles 记录（至少含 id / industry / category / brand_id）
        user_id: 触发 rerun 的用户
        updated_fields: 本次 rerun 实际写 client_profiles.industry_brief 的字段（其他字段不反哺）
        source_ref: rerun 请求标识（profile_id+timestamp）用于审计追溯
    """
    if not _is_enabled():
        logger.info("[reverse-feed] RERUN_REVERSE_FEED_ENABLED=false, 跳过反哺")
        return {"skipped": True, "reason": "disabled"}

    industry = (profile.get("industry") or "").strip()
    category = (profile.get("category") or "").strip() or None
    profile_id = profile.get("id")
    if not industry:
        return {"skipped": True, "reason": "no_industry"}

    tasks = []
    # L1 层字段（按 industry）
    for field in _L1_FEEDBACK_FIELDS:
        if field not in updated_fields:
            continue
        val = new_brief.get(field)
        if val is None:
            continue
        tasks.append(_feed_one_field(
            level="industry",
            industry=industry,
            category=None,
            field_name=field,
            new_value=val,
            source_type="ai_rerun",
            source_ref=source_ref,
            profile_id=profile_id,
            user_id=user_id,
        ))
    # L2 层字段（按 category · 无 category 跳过）
    if category:
        for field in _L2_FEEDBACK_FIELDS:
            if field not in updated_fields:
                continue
            val = new_brief.get(field)
            if val is None:
                continue
            tasks.append(_feed_one_field(
                level="category",
                industry=industry,
                category=category,
                field_name=field,
                new_value=val,
                source_type="ai_rerun",
                source_ref=source_ref,
                profile_id=profile_id,
                user_id=user_id,
            ))

    if not tasks:
        return {"skipped": True, "reason": "no_feedback_fields_in_updated"}

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.error(f"[reverse-feed] feed_from_rerun gather failed: {e}")
        return {"error": str(e)[:120]}

    # 汇总
    merged = [r for r in results if isinstance(r, dict) and r.get("status") == "merged"]
    skipped = [r for r in results if isinstance(r, dict) and r.get("status", "").startswith("skip_")]
    rejected = [r for r in results if isinstance(r, dict) and r.get("status") == "all_rejected_by_vote"]
    dup_only = [r for r in results if isinstance(r, dict) and r.get("status") == "all_duplicates"]
    errors = [r for r in results if isinstance(r, Exception) or (isinstance(r, dict) and r.get("status") in ("write_failed", "skip_no_shared_row"))]

    logger.info(
        f"[reverse-feed · rerun] user={user_id} profile={profile_id} "
        f"industry={industry} category={category or '-'} "
        f"merged={len(merged)} rejected={len(rejected)} dup_only={len(dup_only)} "
        f"skipped={len(skipped)} errors={len(errors)}"
    )
    return {
        "merged_fields": [r["field"] for r in merged],
        "rejected_fields": [r["field"] for r in rejected],
        "duplicate_only_fields": [r["field"] for r in dup_only],
        "skipped_fields": [r.get("field") for r in skipped if isinstance(r, dict)],
        "total_tasks": len(tasks),
    }
