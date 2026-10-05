"""P2-2 · 运营助手:读真实计划,解释下一步(2026-08-14)。

研究定稿 §16 P2-2:代理打开写作项目要能一眼知道「现在卡在哪一步、下一步点哪里」。
既有的 closed-loop summary 依赖全黑的 geo_article_* 闭环机器(4 表 0 行、flag 关),
M3 推荐是纯前端规则 —— 都不读写作链真实状态。本模块**只读真实表**
(topics / articles.quality_warning / mhz 发布订单 / monitoring_tasks),
按确定性规则推导下一步;规则产出解释,LLM 一概不参与(「LLM 只解释」的
下限是可以完全不用 LLM —— 规则可解释时不花一分钱)。

分类:O1 —— 任何查询失败返回 available=False,前端不渲染,主流程零影响。
文案纪律:人话,不露 stage 数字/内部枚举(M3 渐进披露原则同源)。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("GEO-WritingNextStep")

NEXT_STEP_VERSION = "writing-next-step-v1.0"


def derive_next_step(facts: dict[str, int]) -> dict[str, str]:
    """真实计数 → 下一步(纯函数,判别测试打这里)。

    阶梯自上而下取第一个命中;每档给 人话标题 + 为什么 + 点哪里(cta)。
    """
    f = {k: int(facts.get(k) or 0) for k in (
        "keywords", "topics", "topics_untitled", "topics_ready", "topics_writing",
        "topics_failed", "articles", "advisory_open", "eligible_unpublished",
        "published", "monitoring_tasks",
    )}
    if f["keywords"] == 0:
        return {"stage": "keywords", "title": "先确认关键词",
                "why": "项目还没有已确认的关键词,写作从选词开始。",
                "cta": "去关键词确认"}
    if f["topics"] == 0 or f["topics_untitled"] > 0:
        n = f["topics_untitled"] or f["keywords"]
        return {"stage": "titles", "title": "生成标题",
                "why": f"有 {n} 个待出题位还没有标题。",
                "cta": "生成标题"}
    if f["topics_failed"] > 0:
        return {"stage": "retry", "title": "重试失败的文章",
                "why": f"有 {f['topics_failed']} 篇生成失败,可安全重试。",
                "cta": "查看失败并重试"}
    if f["topics_ready"] > 0:
        return {"stage": "write", "title": "开始写作",
                "why": f"有 {f['topics_ready']} 个标题就绪、还没开始写。",
                "cta": "开始写作"}
    if f["topics_writing"] > 0:
        return {"stage": "writing", "title": "文章生成中",
                "why": f"{f['topics_writing']} 篇正在生成,稍候刷新即可。",
                "cta": "刷新进度"}
    # [R2-5 2026-08-15] 发布优先于普通提示:普通提示不影响发布(D8 审核右移的
    # 产品口径),有可发布文章时「去发布」必须是主动作 —— 旧序把 advisory 排前,
    # 等于让不阻断发布的提示阻断了运营节奏。提示作为次要入口在 why 里点名。
    if f["eligible_unpublished"] > 0:
        why = f"有 {f['eligible_unpublished']} 篇可发布、还没发布。"
        if f["advisory_open"] > 0:
            why += f"(另有 {f['advisory_open']} 篇带可优化提示,不影响发布,可稍后逐条处理或忽略)"
        return {"stage": "publish", "title": "去发布", "why": why, "cta": "去发布中心"}
    if f["advisory_open"] > 0:
        return {"stage": "review", "title": "处理文章提示",
                "why": f"有 {f['advisory_open']} 篇文章带可优化提示(可逐条修复或忽略继续)。",
                "cta": "查看提示"}
    if f["published"] > 0 and f["monitoring_tasks"] == 0:
        return {"stage": "monitor", "title": "开启监测",
                "why": f"已发布 {f['published']} 单,还没有监测任务看效果。",
                "cta": "开启监测"}
    if f["articles"] == 0:
        return {"stage": "write", "title": "开始写作",
                "why": "标题已就绪,还没有文章。",
                "cta": "开始写作"}
    return {"stage": "observe", "title": "看监测效果",
            "why": "本项目写作—发布链已跑通,关注监测与被引结果即可。",
            "cta": "查看监测"}


def build_next_step(quote_id: int) -> dict[str, Any]:
    """读真实表取计数并推导下一步。O1:失败 → available=False。"""
    try:
        from db.connection import get_db
        from services.article_review_gate import _advisory_state  # 与徽章同一 SSOT

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM confirmed_keywords ck
                    WHERE ck.quote_id = %(qid)s AND (ck.is_core IS NOT FALSE)) AS keywords,
                  (SELECT COUNT(*) FROM topics t WHERE t.quote_id = %(qid)s) AS topics,
                  (SELECT COUNT(*) FROM topics t WHERE t.quote_id = %(qid)s
                    AND NULLIF(BTRIM(t.optimized_title),'') IS NULL AND t.article_id IS NULL) AS topics_untitled,
                  (SELECT COUNT(*) FROM topics t WHERE t.quote_id = %(qid)s
                    AND t.article_id IS NULL AND NULLIF(BTRIM(t.optimized_title),'') IS NOT NULL
                    AND COALESCE(t.status,'draft') IN ('draft','pending','titles_ready')) AS topics_ready,
                  (SELECT COUNT(*) FROM topics t WHERE t.quote_id = %(qid)s
                    AND t.status = 'writing' AND t.article_id IS NULL
                    AND t.writing_started_at > NOW() - INTERVAL '90 minutes') AS topics_writing,
                  (SELECT COUNT(*) FROM topics t WHERE t.quote_id = %(qid)s
                    AND t.status = 'failed') AS topics_failed,
                  (SELECT COUNT(*) FROM articles a WHERE a.quote_id = %(qid)s) AS articles,
                  -- [WP7 cutover 2026-08-17] 这一条只数**代发链**,而且数的是订单
                  -- 而不是交付单元:人工登记与插件两条链的发布一篇都不算,同一
                  -- 交付单元的两次重试会被数成两单。改由六阶段投影统一供数
                  -- (见下方 `_stage_published`),这里只留 0 占位保持列形状。
                  0 AS published,
                  (SELECT COUNT(*) FROM monitoring_tasks mt WHERE mt.quote_id = %(qid)s) AS monitoring_tasks
                """,
                {"qid": int(quote_id)},
            )
            facts = {k: int(v or 0) for k, v in dict(cur.fetchone() or {}).items()}

            # advisory open 篇数(读 quality_warning,与徽章口径同一个函数)。
            cur.execute(
                "SELECT quality_warning FROM articles WHERE quote_id = %s AND quality_warning IS NOT NULL",
                (int(quote_id),),
            )
            advisory_open = 0
            for row in cur.fetchall() or []:
                state, count = _advisory_state(row.get("quality_warning"))
                if state == "open" and count > 0:
                    advisory_open += 1
            facts["advisory_open"] = advisory_open

            # [WP7] 统一发布数走 quote-scoped 投影。**不可用时保持 None**,
            # 下游的"下一步"提示宁可少给一条,也不能凭一个假 0 让代理以为没发过。
            try:
                from services.publication_stage_adapters import quote_published_active

                _active = quote_published_active(int(quote_id), cursor=cur)
                facts["published"] = int(_active) if _active is not None else 0
                facts["published_available"] = _active is not None
            except Exception as _pe:
                logger.warning("[writing-next] 六阶段投影不可用 quote=%s: %s", quote_id, _pe)
                facts["published_available"] = False
            # 可发未发:完成文章数 - 已发布订单覆盖的(粗口径,advisory 用途;
            # 精确 eligible 逐篇算太贵,列表页已有徽章逐篇兜底)。
            # [R2-5] 🔴 不再扣减 advisory_open:普通提示**不影响发布资格**
            # (eligible 的 SSOT 里 advisory 本来就不参与),扣了 = 把提示做成隐性闸。
            facts["eligible_unpublished"] = max(
                0, facts.get("articles", 0) - facts.get("published", 0)
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[P2-2] quote %s 下一步推导失败: %s", quote_id, str(exc)[:200])
        return {"available": False, "version": NEXT_STEP_VERSION}

    step = derive_next_step(facts)
    return {"available": True, "version": NEXT_STEP_VERSION,
            "facts": facts, "next_step": step}
