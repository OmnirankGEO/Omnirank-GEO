"""W2 · 标准答案蒸馏层(老板的「不同问题的不同标准答案」)。

第一性原理:我们坐拥一个天然对照实验集 —— 同样被搜索引擎收录的文章里,一部分被 AI 真实
采纳进答案,另一部分只被搜到没被采纳。"什么让 AI 选中一篇文章"的答案藏在两组的系统性差异里。
本模块把这份差异蒸馏成**每一类问题(行业 × 文体)的候选改进方案**:

  候选包三件套:
    1. 标准答案模板(answer_template)—— 该类问题的结构骨架,运营可读的人话文档
    2. 候选 prompt(candidate_prompt)—— 在该文体当前 prompt 基础上的增量修订版(可直接替换)
    3. 证据附录(evidence)—— 样本数 / lift 特征 / 引用的采纳样文 id

落点:R6 版本面(style_control)draft 态 —— LLM 调用在本服务层,产物经 `create_distilled_draft`
写入版本面并跑既有守卫,**不违反控制面「不调 LLM / 不发布客户输出」铁律**。draft 须管理员在看板
走既有双闸(activate)才影响后续新文章。

🔴 train/holdout 隔离(防"训练集上赢"):每组语料按 article_id 稳定哈希切分 train(70%,供蒸馏)/
holdout(30%,供 W4 评审)。蒸馏只读 train 侧样文与特征;W4 评审的 lift 特征与证据样例来自 holdout 侧。
切分函数 `holdout_side` 单点实现,蒸馏与评审共用防口径漂移。

成本三闸:dry_run 默认(计数 + 预估成本,零 LLM)/ 后台 + 互斥(WORKERS=1)/ cap(默认每次 ≤10 组)。
flag `writing_distill_auto`(默认关)控制"新数据到位后自动产候选"。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from typing import Any

import httpx

from services.article_structure_analysis import (
    _feature_lift,      # 复用 lift 口径 SSOT(adopted_share / max(control_share,0.05),≥1.5 且 ≥0.2 才 recommended)
    _feature_share,     # 复用 21 特征占比口径
    load_labeled_article_rows,
)
from services.research_monitor.article_intent_classifier import _extract_json_object
from tools.llm_call_tracker import llm_track, usage_from_response_payload
from writing.intent_style_map import resolve_style_code
from writing.llm_utils import get_llm_config
from writing.style_registry import WRITING_STYLES, get_prompt_for_style

logger = logging.getLogger("GEO-WritingFlywheel.Distiller")

# ---- 门槛与配额 ----
MIN_ADOPTED_FOR_DISTILL = 30        # 该组 train 侧采纳样本门槛(< 30 只观察不蒸馏)
MIN_CONTROL_FOR_DISTILL = 10        # train 侧对照样本门槛(缺对照不能算 lift 差异)
TOP_ADOPTED_EXCERPTS = 8
CONTROL_EXCERPTS = 3
EXCERPT_MAX_CHARS = 3000
CURRENT_PROMPT_MAX_CHARS = 6000
DEFAULT_DISTILL_CAP = max(1, int(os.getenv("WRITING_DISTILL_AUTO_CAP", "10")))
TRAIN_RATIO_PCT = 7                 # hash % 10 < 7 → train(70%)

# 蒸馏是长输入 + 长输出的重调用,成本预估保守取整(平台承担,不扣用户)
_DISTILL_INPUT_TOKENS = 6000
_DISTILL_OUTPUT_TOKENS = 3000

# [出口审核修] threading.Lock 而非 asyncio.Lock:本函数从 scheduler 新事件循环 + API 主事件循环
# 两处触发,asyncio.Lock 不跨循环互斥;threading.Lock + acquire(blocking=False) 跨线程/跨循环真互斥。
_DISTILL_LOCK = threading.Lock()


# ============ train / holdout 稳定切分(单点 · 蒸馏与 W4 评审共用)============
def holdout_side(cluster_identity: Any) -> str:
    """Stable cluster split: every duplicate/转载 cluster stays on one side.

    The caller must pass ``content_cluster_id`` for inference rows.  A missing
    identity is never holdout-eligible and remains train-only as a fail-closed
    compatibility fallback.
    """
    key = str(cluster_identity if cluster_identity is not None else "").strip()
    if not key:
        # 无 id 的行归 train(不进 holdout 评审集,避免评审读到无法追溯的样本)
        return "train"
    digest = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    return "train" if (digest % 10) < TRAIN_RATIO_PCT else "holdout"


def _row_style_code(row: dict[str, Any]) -> str:
    """把一行的 article intent_type 映射到 style_code(article intent 与 query intent 同 8 类口径)。"""
    intent = str(row.get("intent_type") or "").strip()
    family = str(row.get("style_family") or "").strip()
    return resolve_style_code(intent, family)


def _excerpt(row: dict[str, Any], max_chars: int = EXCERPT_MAX_CHARS) -> dict[str, Any]:
    body = str(row.get("body") or "")[:max_chars]
    return {
        "id": row.get("id"),
        "title": str(row.get("title") or "")[:200],
        "domain": str(row.get("domain") or row.get("url") or "")[:120],
        "excerpt": body,
    }


# ============ 分组规划(dry_run 与真跑共用)============
def plan_distill_groups(
    industry: str,
    limit: int = 1000,
    min_chars: int = 500,
    oss_backfill_cap: int | None = None,
) -> dict[str, Any]:
    """加载带标签的行,按 (industry_key × style_code) 分组,train/holdout 切分并统计各侧样本。

    返回 groups[],每组含 train/holdout 侧采纳/对照计数、构成 intents、eligible 判定。
    不调 LLM(纯统计),供 dry_run 与真跑前置规划复用。

    oss_backfill_cap:透传给正文 OSS 回读。分组/达标计数只依赖 SQL 字段(group_key/intent/字数),
    不依赖正文 → 纯计数口径(dry_run/看板)传 0 = 零 OSS 下载;真跑蒸馏需要正文喂 LLM → 传 None 全量。
    """
    industry_key, rows = load_labeled_article_rows(
        industry, limit=limit, min_chars=min_chars, oss_backfill_cap=oss_backfill_cap
    )
    # bucket[(style_code)] -> {"train":{"adopted":[],"control":[]}, "holdout":{...}, "intents":set}
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        style_code = _row_style_code(row)
        gk = row.get("group_key")
        side = holdout_side(row.get("content_cluster_id"))
        b = buckets.setdefault(
            style_code,
            {
                "train_adopted": [], "train_control": [],
                "holdout_adopted": [], "holdout_control": [],
                "intents": set(),
            },
        )
        if row.get("intent_type"):
            b["intents"].add(str(row.get("intent_type")))
        if gk == "adopted_group":
            b[f"{side}_adopted"].append(row)
        elif gk == "search_only_control_group":
            b[f"{side}_control"].append(row)
        # cited/reference 组不进蒸馏对照(只用最干净的 采纳 vs 纯搜索对照 两极)

    groups: list[dict[str, Any]] = []
    for style_code, b in sorted(buckets.items()):
        train_adopted = len(b["train_adopted"])
        train_control = len(b["train_control"])
        holdout_adopted = len(b["holdout_adopted"])
        holdout_control = len(b["holdout_control"])
        eligible = train_adopted >= MIN_ADOPTED_FOR_DISTILL and train_control >= MIN_CONTROL_FOR_DISTILL
        groups.append(
            {
                "industry_key": industry_key,
                "style_code": style_code,
                "style_name": (WRITING_STYLES.get(style_code) or {}).get("name") or style_code,
                "intents": sorted(b["intents"]),
                "train_adopted": train_adopted,
                "train_control": train_control,
                "holdout_adopted": holdout_adopted,
                "holdout_control": holdout_control,
                "eligible": eligible,
                "reason": (
                    "达标可蒸馏" if eligible
                    else f"train 采纳 {train_adopted}/{MIN_ADOPTED_FOR_DISTILL} · 对照 {train_control}/{MIN_CONTROL_FOR_DISTILL}(样本积累中)"
                ),
                "_rows": b,  # 内部用,dry_run 输出前剔除
            }
        )
    eligible_groups = [g for g in groups if g["eligible"]]
    return {
        "industry_key": industry_key,
        "loaded": len(rows),
        "group_count": len(groups),
        "eligible_count": len(eligible_groups),
        "groups": groups,
    }


def estimate_distill_cost(n_groups: int) -> dict[str, Any]:
    n = max(0, int(n_groups or 0))
    in_tokens = n * _DISTILL_INPUT_TOKENS
    out_tokens = n * _DISTILL_OUTPUT_TOKENS
    # 蒸馏用写作域模型(qwen/deepseek 级),保守单价口径
    est_cost = round(in_tokens / 1000 * 0.002 + out_tokens / 1000 * 0.008, 4)
    return {
        "group_count": n,
        "est_input_tokens": in_tokens,
        "est_output_tokens": out_tokens,
        "est_cost_cny": est_cost,
    }


# ============ 单组蒸馏(调 LLM)============
def _build_distill_messages(
    *,
    style_code: str,
    style_name: str,
    industry_key: str,
    current_prompt: str,
    lift_features: list[dict[str, Any]],
    adopted_excerpts: list[dict[str, Any]],
    control_excerpts: list[dict[str, Any]],
) -> list[dict[str, str]]:
    top_lift = [
        f"- {row['label']}:采纳组 {round(row['adopted_share'] * 100)}% vs 对照组 {round(row['control_share'] * 100)}%(lift {row['lift']})"
        for row in lift_features[:10]
        if row.get("recommended")
    ]
    lift_block = "\n".join(top_lift) or "(本组暂无显著结构差异,谨慎给出温和改进)"
    adopted_block = "\n\n".join(
        f"【采纳范文 {i + 1}】{e['title']}\n{e['excerpt']}" for i, e in enumerate(adopted_excerpts)
    )
    control_block = "\n\n".join(
        f"【未被采纳对照 {i + 1}】{e['title']}\n{e['excerpt']}" for i, e in enumerate(control_excerpts)
    )
    system = (
        "你是 GEO 内容策略蒸馏师。任务:根据「被 AI 采纳的文章」相对「只被搜到没被采纳的文章」的"
        "结构差异,为某一类文体产出一份可落地的改进方案。要求:\n"
        "1. 只返回 JSON,不要任何解释性前后缀。\n"
        "2. candidate_prompt 必须是在【当前 prompt】基础上的增量修订(保留其骨架与约束,只针对性增强),"
        "不要从零重写,便于人工 diff。\n"
        "3. 绝对禁止在输出中出现:成本/毛利/ratio/markup 等内部字段,不做疗效/收益/排名等绝对化承诺,"
        "不编造具体价格或百分比数据。\n"
        "4. answer_template 用运营看得懂的中文人话描述结构骨架,不是 prompt 术语。"
    )
    user = f"""文体:{style_name}({style_code}) · 行业:{industry_key}

【采纳文章相对对照的结构差异(真实数据 lift,只列显著项)】
{lift_block}

【当前 prompt(在此基础上增量修订)】
{current_prompt[:CURRENT_PROMPT_MAX_CHARS]}

【被 AI 采纳的范文节选(学习对象)】
{adopted_block}

【只被搜到没被采纳的对照节选(反例)】
{control_block}

请返回 JSON,严格如下结构:
{{
  "answer_template": "运营可读的中文文档:该类问题的结构骨架(开头怎么写/几个小标题/要不要 FAQ/数据引用怎么放)+ 要素清单 + 一段示例开头",
  "candidate_prompt": "在当前 prompt 基础上的增量修订版完整 prompt,可直接替换使用",
  "key_changes": ["相对当前 prompt 的关键改动,人话,不超过 5 条"]
}}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _call_distill_llm(messages: list[dict[str, str]], *, temperature: float = 0.3) -> str:
    api_url, api_key, model, provider = get_llm_config("answer_distill", "writing")
    if not api_key:
        raise RuntimeError("蒸馏 LLM 未配置 api_key(get_llm_config answer_distill/writing)")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 6000,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=240.0) as client:
        async with llm_track(
            "writing_flywheel", provider, model=model, metadata={"task": "answer_distill"}
        ) as tracker:
            try:
                resp = await client.post(api_url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                it, ot, ct = usage_from_response_payload(data)
                tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
            except Exception as exc:
                tracker.record(success=False, error_msg=str(exc)[:500])
                raise
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


async def distill_one_group(group: dict[str, Any]) -> dict[str, Any]:
    """对一组(已 eligible)真跑蒸馏 LLM,返回候选包三件套。只读 train 侧样文与特征。"""
    rows = group["_rows"]
    train_adopted = rows["train_adopted"]
    train_control = rows["train_control"]
    # lift 用 train 侧(蒸馏输入);口径复用 article_structure_analysis._feature_lift
    lift = _feature_lift(
        {"feature_share": _feature_share(train_adopted)},
        {"feature_share": _feature_share(train_control)},
    )
    adopted_sorted = sorted(train_adopted, key=lambda r: float(r.get("source_weight") or 0), reverse=True)
    adopted_excerpts = [_excerpt(r) for r in adopted_sorted[:TOP_ADOPTED_EXCERPTS]]
    control_excerpts = [_excerpt(r, max_chars=EXCERPT_MAX_CHARS // 2) for r in train_control[:CONTROL_EXCERPTS]]
    style_code = group["style_code"]
    current_prompt = get_prompt_for_style(style_code)
    messages = _build_distill_messages(
        style_code=style_code,
        style_name=group["style_name"],
        industry_key=group["industry_key"],
        current_prompt=current_prompt,
        lift_features=lift,
        adopted_excerpts=adopted_excerpts,
        control_excerpts=control_excerpts,
    )
    raw = await _call_distill_llm(messages)
    parsed = _extract_json_object(raw)
    candidate_prompt = str(parsed.get("candidate_prompt") or "").strip()
    answer_template = str(parsed.get("answer_template") or "").strip()
    key_changes = [str(x).strip() for x in (parsed.get("key_changes") or []) if str(x or "").strip()][:5]
    if not candidate_prompt:
        raise ValueError("蒸馏返回缺 candidate_prompt")
    evidence = {
        "industry_key": group["industry_key"],  # [review fix] 供重复蒸馏去重按 (style, industry) 精确匹配
        "sample": {
            "train_adopted": len(train_adopted),
            "train_control": len(train_control),
            "holdout_adopted": len(rows["holdout_adopted"]),
            "holdout_control": len(rows["holdout_control"]),
        },
        "intents": group["intents"],
        "train_lift_top": lift[:8],
        "adopted_sample_ids": [e["id"] for e in adopted_excerpts],
        "key_changes": key_changes,
        "note": "lift/样文取自 train 侧;W4 评审 rubric 与证据须取 holdout 侧(holdout_side 同口径可复现)。",
    }
    return {
        "style_code": style_code,
        "industry_key": group["industry_key"],
        "candidate_prompt": candidate_prompt,
        "answer_template": answer_template,
        "key_changes": key_changes,
        "evidence": evidence,
        "train_adopted": len(train_adopted),
        "train_control": len(train_control),
    }


# ============ 主入口:dry_run / 真跑 ============
def is_distill_running() -> bool:
    return _DISTILL_LOCK.locked()


def _pending_distill_keys() -> tuple[set[tuple[str, str]], set[str]]:
    """[review fix] 已有未审结蒸馏候选(draft/admin_review)的组键 —— 防重复蒸馏。

    没有这层守卫,自动蒸馏(flag 开后每日跑)对同一批达标组会天天重蒸:重复烧 LLM 成本、
    版本面每天堆近似重复 draft、审核人被同质候选淹没。审结(activate/驳回/blocked)后组自动解锁。
    返回 (精确键集 {(style_code, industry_key)}, 兜底集 {style_code} —— 老 draft 缺行业信息时按文体保守跳过)。
    """
    try:
        from writing.style_control import load_control_state
        state = load_control_state()
    except Exception:
        return set(), set()
    exact: set[tuple[str, str]] = set()
    style_any: set[str] = set()
    for v in state.get("versions") or []:
        if not isinstance(v, dict) or v.get("source") != "answer_distiller":
            continue
        if v.get("status") not in {"draft", "admin_review"}:
            continue
        style = str(v.get("style_code") or "")
        ind = str((v.get("evidence_chain") or {}).get("industry_key") or "")
        if ind:
            exact.add((style, ind))
        else:
            style_any.add(style)
    return exact, style_any


async def distill_candidates(
    *,
    industry: str = "",
    limit: int = 1000,
    min_chars: int = 500,
    dry_run: bool = True,
    cap: int | None = None,
    actor_id: int = 0,
    persist_draft: bool = True,
) -> dict[str, Any]:
    """主入口。dry_run:列出可蒸馏组 + 成本预估(零 LLM)。真跑:后台 + 互斥 + cap,每组蒸馏→写 draft。"""
    # [review fix] plan 是同步重活(SQL + OSS 正文串行回读):挪线程池,不堵 uvicorn 唯一事件循环
    # (生产 WORKERS=1,堵住 = 全站 API/SSE/WS 冻结)。dry_run 只要计数 → cap=0 零 OSS;真跑需正文 → 全量(线程内)。
    plan = await asyncio.to_thread(
        plan_distill_groups, industry, limit=limit, min_chars=min_chars,
        oss_backfill_cap=(0 if dry_run else None),
    )
    # [review fix] 重复蒸馏去重:同组已有未审结 draft → 跳过(dry_run 成本预估同口径,保持与真跑误差一致)
    pending_exact, pending_style_any = await asyncio.to_thread(_pending_distill_keys)
    for g in plan["groups"]:
        g["already_pending"] = bool(
            g["eligible"]
            and ((g["style_code"], g["industry_key"]) in pending_exact or g["style_code"] in pending_style_any)
        )
    eligible = [g for g in plan["groups"] if g["eligible"] and not g["already_pending"]]
    skipped_pending = sum(1 for g in plan["groups"] if g.get("already_pending"))
    run_cap = max(1, int(cap or DEFAULT_DISTILL_CAP))

    if dry_run:
        preview = [
            {k: v for k, v in g.items() if k != "_rows"}
            for g in plan["groups"]
        ]
        return {
            "mode": "dry_run",
            "industry_key": plan["industry_key"],
            "loaded": plan["loaded"],
            "group_count": plan["group_count"],
            "eligible_count": len(eligible),
            "skipped_pending_review": skipped_pending,
            "will_distill": min(len(eligible), run_cap),
            "cap": run_cap,
            **estimate_distill_cost(min(len(eligible), run_cap)),
            "groups": preview,
        }

    if not eligible:
        return {"status": "completed", "distilled": 0, "failed": 0, "eligible_count": 0,
                "skipped_pending_review": skipped_pending}
    if not _DISTILL_LOCK.acquire(blocking=False):
        return {"status": "in_progress", "message": "蒸馏正在进行中,请稍后再试。"}
    try:
        targets = eligible[:run_cap]
        distilled = 0
        failed = 0
        drafts: list[dict[str, Any]] = []
        for group in targets:
            try:
                package = await distill_one_group(group)
                if persist_draft:
                    # LLM 调用已完成;此处仅持久化(create_distilled_draft 内不调 LLM),跑既有守卫。
                    # [review fix] JSON 状态 read-modify-write 挪线程池,不堵事件循环。
                    from writing.style_control import create_distilled_draft
                    result = await asyncio.to_thread(
                        create_distilled_draft,
                        style_code=package["style_code"],
                        industry_key=package["industry_key"],
                        prompt_text=package["candidate_prompt"],
                        template_doc=package["answer_template"],
                        evidence=package["evidence"],
                        strategy_summary=f"{group['style_name']} · {package['industry_key']} · 蒸馏候选",
                        actor_id=actor_id,
                        expected_config_version=None,  # 后台单写者(互斥内),每次 load 最新 state
                        sample_count=package["train_adopted"],
                        control_sample_count=package["train_control"],
                        source="answer_distiller",
                    )
                    version = result.get("version") or {}
                    drafts.append(
                        {
                            "style_code": package["style_code"],
                            "industry_key": package["industry_key"],
                            "version_id": version.get("version_id"),
                            "status": version.get("status"),
                            "guard": (version.get("guard_summary") or {}).get("decision"),
                        }
                    )
                distilled += 1
            except Exception as exc:  # fail-soft:单组失败不拖垮整批
                failed += 1
                logger.warning(
                    "[distill] 组蒸馏失败 style=%s industry=%s: %s",
                    group.get("style_code"),
                    group.get("industry_key"),
                    str(exc)[:200],
                )
        logger.info("[distill] 完成 distilled=%s failed=%s skipped_pending=%s cap=%s",
                    distilled, failed, skipped_pending, run_cap)
        return {
            "status": "completed",
            "distilled": distilled,
            "failed": failed,
            "eligible_count": len(eligible),
            "skipped_pending_review": skipped_pending,
            "cap": run_cap,
            "drafts": drafts,
        }
    finally:
        _DISTILL_LOCK.release()
