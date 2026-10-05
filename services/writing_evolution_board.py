"""W5 · 写作进化看板聚合(每文体一张卡的服务端数据源)。

前端看板数据横跨:版本面草稿(style_control)+ 模拟对比(writing_style_simulations)+ 评审
(review_summary)。这里服务端聚合成「每文体一张卡 + 8 态」,前端直接消费,不用客户端拼多端点。

纯读聚合,不调 LLM、不写库、不改客户可见输出。
"""
from __future__ import annotations

from typing import Any

MIN_ADOPTED_FOR_DISTILL = 30
MIN_CONTROL_FOR_DISTILL = 10


def _board_state(card: dict[str, Any]) -> tuple[str, str]:
    """8 态矩阵 → (state, human_label)。见 SPEC W5 状态矩阵。"""
    data_health = card.get("data_health") or {}
    if data_health.get("state") == "data_blocked":
        # [2026-08-01 刷屏修] data_health 是**页面级单例**(整块看板一次 get_article_data_health),
        # 原来每张卡都把同一串 blockers 拼进自己的标签 → 十几张卡重复刷同一句
        # 「数据不可裁决：no_strict_article_question_events、monitoring_lineage_not_100_percent」。
        # 现在卡上只留短标签,完整原因由看板级 data_blocked_reason 出一次(见 get_evolution_board)。
        return "data_blocked", "数据不可裁决"
    review = card.get("review_summary") or {}
    verdict = review.get("verdict")
    has_candidate = bool(card.get("candidate_version_id"))
    sim_status = card.get("latest_sim_status")

    if sim_status == "failed":
        return "failed", "生成失败,点击重试"
    if card.get("candidate_generating"):
        return "generating", "AI 正在生成对比样文(约 2 分钟)"
    if has_candidate and verdict == "replace":
        return "recommend_replace", "发布前质量更优，待真实效果实验"
    if has_candidate and verdict in ("keep",):
        return "keep", "当前版更优,候选未超越"
    if has_candidate and verdict == "observe":
        # 双模型不一致 or 单模型
        if review.get("dual_model") and not review.get("reviewer_agreement"):
            return "disagree", "AI 意见不一致,建议继续观察"
        return "observe", "建议继续观察"
    if has_candidate:
        return "candidate_unreviewed", "有候选,待评审"
    # 无候选:看样本积累
    train_adopted = int(card.get("train_adopted") or 0)
    train_control = int(card.get("train_control") or 0)
    if 0 < train_adopted < MIN_ADOPTED_FOR_DISTILL:
        return "observing", f"结构样本 {train_adopted}/{MIN_ADOPTED_FOR_DISTILL} · 够 30 篇生成候选草稿"
    if train_adopted >= MIN_ADOPTED_FOR_DISTILL:
        if train_control < MIN_CONTROL_FOR_DISTILL:
            return (
                "ready_pending_distill",
                f"结构样本已够 {train_adopted} 篇 · 对照样本 {train_control}/{MIN_CONTROL_FOR_DISTILL} · 补足后蒸馏候选草稿",
            )
        return "ready_pending_distill", f"结构样本已够 {train_adopted} 篇 · 等待蒸馏候选草稿"
    return "no_candidate", "暂无新候选(语料更新后自动蒸馏)"


def get_evolution_board(industry_key: str | None = None) -> dict[str, Any]:
    from db.writing_style_simulation_db import list_simulations
    from writing.style_control import load_control_state
    from writing.style_registry import WRITING_STYLES

    state = load_control_state()
    versions = {v.get("version_id"): v for v in state.get("versions") or []}
    active_by_style = state.get("active_by_style") or {}
    try:
        # [504 治理 2026-08-18] 生产实测 get_article_data_health() 单次 4.2s,是本看板最大单项,
        #   且同一次页面加载里 flywheel-insight 的 _safe_health 会**再算一遍**。
        #   两处改走同一个共享 TTL 缓存(同 key) → 一次页面加载只算一次。
        #   数值零变化(缓存只是复用同一份真实聚合),故意不改口径。
        from services.article_data_health import get_article_data_health
        from writing.flywheel_cache import SCOPE_ARTICLE_HEALTH, get_or_compute, make_key
        data_health = get_or_compute(make_key(SCOPE_ARTICLE_HEALTH), 60.0, get_article_data_health)
    except Exception as exc:
        data_health = {
            "state": "data_blocked",
            "decision_status": "INSUFFICIENT_SAMPLES",
            "blockers": [f"data_health_error:{type(exc).__name__}"],
        }

    # [出口审核修 · 死态可达] 活取每文体 train 采纳累积(即便还没蒸馏出草稿),使 observing 态可达。
    #   一次 plan_distill_groups(与蒸馏 dry_run 同口径)拿全文体计数;失败 fail-soft 空 map。
    live_adopted: dict[str, int] = {}
    live_control: dict[str, int] = {}
    try:
        from services.writing_answer_distiller import plan_distill_groups
        # [review fix] 看板只要计数(分组/达标不依赖正文)→ oss_backfill_cap=0 零 OSS 回读:
        # T1 backfill 后正文大量卸载 OSS,否则每次看板加载最多触发上千次串行 OSS 下载。
        plan = plan_distill_groups(industry_key or "general", limit=1000, oss_backfill_cap=0)
        for g in plan.get("groups", []):
            style = g["style_code"]
            live_adopted[style] = int(g.get("train_adopted") or 0)
            live_control[style] = int(g.get("train_control") or 0)
    except Exception:
        live_adopted = {}
        live_control = {}
    running_style = None
    try:
        from services.writing_style_simulation import get_running_simulation_style
        running_style = get_running_simulation_style()
    except Exception:
        running_style = None

    cards: list[dict[str, Any]] = []
    for style_code, meta in WRITING_STYLES.items():
        active_v = versions.get(active_by_style.get(style_code))
        drafts = [
            v for v in state.get("versions") or []
            if v.get("style_code") == style_code and v.get("status") == "draft"
        ]
        latest_draft = drafts[-1] if drafts else None
        candidate_vid = (latest_draft or {}).get("version_id")
        # [出口审核修 · 防张冠李戴] 评审按**候选版本精确匹配**取,不取"最新一次模拟"
        #   (否则新候选 D2 会套用旧候选 D1 的 verdict,绕过双模型闸门)。
        latest_sim = None
        if candidate_vid:
            sims = list_simulations(
                style_code=style_code, industry_key=industry_key or None,
                version_id=candidate_vid, limit=1,
            )
            latest_sim = sims[0] if sims else None
        review = (latest_sim or {}).get("review_summary") or {}
        evidence = (latest_draft or {}).get("evidence_chain") or {}
        sample = evidence.get("sample") or {}

        card = {
            "style_code": style_code,
            "style_name": meta.get("name") or style_code,
            "current_version_id": (active_v or {}).get("version_id"),
            "current_version_label": (active_v or {}).get("strategy_summary")
            or ("代码默认" if not active_v else style_code),
            "current_activated_at": (active_v or {}).get("activated_at"),
            "candidate_version_id": (latest_draft or {}).get("version_id"),
            "candidate_created_at": (latest_draft or {}).get("created_at"),
            "candidate_evidence_samples": int(sample.get("train_adopted") or 0),
            # 活取累积(即便无草稿也有值)→ observing 态可达;回退 draft 证据计数
            "train_adopted": int(live_adopted.get(style_code, int(sample.get("train_adopted") or 0))),
            "train_control": int(live_control.get(style_code, int(sample.get("train_control") or 0))),
            "latest_sim_id": (latest_sim or {}).get("id"),
            "latest_sim_status": (latest_sim or {}).get("status"),
            "review_summary": review,
            "candidate_generating": running_style == style_code,
            "data_health": data_health,
        }
        state_key, state_label = _board_state(card)
        card["board_state"] = state_key
        card["board_state_label"] = state_label
        cards.append(card)

    # 发布前质量更优的候选排最前；它仍不能绕过真实效果实验。
    order = {"data_blocked": 0, "recommend_replace": 1, "disagree": 2, "candidate_unreviewed": 3,
             "keep": 3, "observe": 3, "ready_pending_distill": 4, "observing": 5, "no_candidate": 6,
             "generating": 0, "failed": 0}
    cards.sort(key=lambda c: order.get(c["board_state"], 9))
    # [2026-08-01 刷屏修] 阻断原因看板级出一次(前端渲染成页面级提示,卡内不再重复)。
    blocked_reasons = list(data_health.get("blockers") or []) if data_health.get("state") == "data_blocked" else []
    return {
        "industry_key": industry_key or "all",
        "cards": cards,
        "has_candidate_count": sum(1 for c in cards if c.get("candidate_version_id")),
        "recommend_replace_count": sum(1 for c in cards if c["board_state"] == "recommend_replace"),
        "data_health": data_health,
        "data_blocked": bool(blocked_reasons),
        "data_blocked_reasons": blocked_reasons,
        "data_blocked_reason": (
            "数据不可裁决：" + "、".join(blocked_reasons) if blocked_reasons else ""
        ),
        "data_blocked_card_count": sum(1 for c in cards if c["board_state"] == "data_blocked"),
    }
