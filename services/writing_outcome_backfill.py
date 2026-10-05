"""W6 · 真实回环回写(命门:防越进化越差)。

对上线 ≥N 天的写作版本,用指纹(prompt_sha)把文章归因到「新版 / 旧版」,查被引数据算
`ai_citations_delta_30d`,写入 `writing_strategy_outcome_events` + `geo_recommendation_predictions/outcomes`
(供 W6.3 校准读库),并给出「建议回滚」黄灯判断(不自动回滚,只提示,走既有 rollback 双闸)。

🔴 不造假:被引数据源 `_citation_count_for_articles` 是可注入 seam;无匹配 → 诚实返回 0 并标
`insufficient_data`,绝不编造 delta。

[2026-08-06 · WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 订正] 模块头原先写「生产当前 0 发布」——
这句**在 2026-08-06 已经不成立**:生产有 246 个已发布 URL。恒 0 的真因不是没发布,是
`_citation_count_for_articles` 只查 geo_research 语料池,而那个池只命中 1 个;客户级证据
在监测池里(13 个)。现已改为**两池相加**,详见该函数 docstring。
本服务函数带 dry_run;scheduler 层注册见 `api/scheduler.py` id="writing_outcome_backfill"。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from psycopg2.extras import Json

from db.connection import get_db
from db.writing_fingerprint_db import list_articles_by_style_version_id

logger = logging.getLogger("GEO-WritingFlywheel.OutcomeBackfill")

ROLLBACK_MIN_SAMPLE = 30
ROLLBACK_DROP_THRESHOLD = 0.20  # 降幅 > 20% 且样本 ≥30 → 建议回滚

# [出口审核修] sync 回写从 scheduler(直调)+ API(to_thread)两处触发,加 threading.Lock 防并发双写 outcome 行。
_BACKFILL_TLOCK = threading.Lock()


def _jsonb(value: Any) -> Json:
    return Json(value or {}, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def evaluate_rollback_suggestion(
    *,
    new_rate: float,
    old_rate: float,
    new_sample: int,
    old_sample: int,
) -> dict[str, Any]:
    """黄灯逻辑:新版被引率显著低于旧版(样本 ≥30 且降幅 >20%)→ 建议回滚。不自动回滚。"""
    if new_sample < ROLLBACK_MIN_SAMPLE or old_sample < ROLLBACK_MIN_SAMPLE:
        return {"suggest_rollback": False, "confident": False,
                "reason": f"样本不足(新{new_sample}/旧{old_sample},需各≥{ROLLBACK_MIN_SAMPLE}),不下结论"}
    if old_rate <= 0:
        return {"suggest_rollback": False, "confident": False, "reason": "旧版无被引基线,无法比较"}
    drop = (old_rate - new_rate) / old_rate
    drop_pct = round(drop * 100, 1)
    if drop > ROLLBACK_DROP_THRESHOLD:
        return {"suggest_rollback": True, "confident": True, "drop_pct": drop_pct,
                "reason": f"新版被引率较旧版下降 {drop_pct}%(>20%),建议回滚"}
    return {"suggest_rollback": False, "confident": True, "drop_pct": drop_pct,
            "reason": f"新版被引率未显著低于旧版(变化 {drop_pct}%)"}


# [V3] 进化历史评语进程缓存(按 measure 整数指纹;数据真变才换新指纹重生成)。
# 生产 dormant:citation 源诚实 no-op → insufficient 恒真 → 门槛恒 False → 永不调 LLM。
_V3_COMMENTARY_CACHE: dict[str, str] = {}


def _measure_fingerprint(measure: dict[str, Any], since_days: int) -> str:
    """按稳定整数字段做指纹(不用已 round 的 new_rate/old_rate 浮点,防抖动/掩盖真实变化)。"""
    from writing.flywheel_llm import fingerprint

    return fingerprint(
        measure.get("style_code"), measure.get("new_version_id"), measure.get("old_version_id"),
        measure.get("new_articles"), measure.get("new_citations"),
        measure.get("old_articles"), measure.get("old_citations"), since_days,
    )


def generate_outcome_commentary(
    measure: dict[str, Any], *, since_days: int = 30, generate: bool = True
) -> str | None:
    """[V3] 对「可比较且非 insufficient」的替换事件生成一句人话评语(LLM 只解释真实数字)。

    - 空数据 / 不可比较 / insufficient → None(不调 LLM,不编故事)——生产恒走此分支。
    - generate=False(dry_run/页面加载路径):只取缓存,绝不内联调 LLM(与 V4/V5 同款效果层纪律)。
    - fail-soft:LLM 失败 / 守卫拦截 / 输出空 → None,前端回退到黄灯数字规则展示。
    - 黄灯判定仍以 evaluate_rollback_suggestion 数字规则为准,LLM 评语只是转述层。
    """
    if not measure.get("comparable") or measure.get("insufficient_data"):
        return None
    fp = _measure_fingerprint(measure, since_days)
    cached = _V3_COMMENTARY_CACHE.get(fp)
    if cached is not None:
        return cached
    if not generate:
        return None
    try:
        from writing.flywheel_llm import call_flywheel_llm_sync, guard_output

        rb = measure.get("rollback_suggestion") or {}
        system = (
            "你是 GEO 写作策略分析助手。只根据给定的真实数字,用一句话转述某写作版本上线后的被引表现,"
            "并给方向性建议(保持 / 继续观察 / 建议回滚)。禁止编造或推算任何新数字;禁止提及任何 AI 模型或"
            "服务商名称;禁止承诺、保证类话术。只返回一句中文,不超过 60 字。"
        )
        user = (
            f"文体:{measure.get('style_name')}\n"
            f"新版被引率:{round((measure.get('new_rate') or 0) * 100, 1)}%(样本 {measure.get('new_articles')} 篇)\n"
            f"旧版被引率:{round((measure.get('old_rate') or 0) * 100, 1)}%(样本 {measure.get('old_articles')} 篇)\n"
            f"黄灯判定:{'建议回滚' if rb.get('suggest_rollback') else '未触发回滚'}({rb.get('reason', '')})\n"
            f"请用一句话转述并给方向性建议。"
        )
        text = (call_flywheel_llm_sync("outcome_commentary", system, user, max_tokens=120) or "").strip()
        cleaned, blocked = guard_output(text)
        if not text or blocked:
            return None
        _V3_COMMENTARY_CACHE[fp] = cleaned
        return cleaned
    except Exception as exc:  # 评语失败绝不阻断回写/看板
        logger.warning("[outcome-commentary] 生成失败(忽略): %s", str(exc)[:200])
        return None


def _citation_count_for_articles(article_ids: list[int], since_days: int = 30) -> dict[str, Any]:
    """某组写作文章的真实被引计数(只读跨域 join · 可注入 seam)· **两个池相加**。

    池 A —— research 语料池([WP9-P0-3 · Owner D-P0-3 授权只读跨 research-monitor 边界]):
      写作 article_id → mhz_publish_order_items(status='published' 的 publish_url)
      → 匹配 geo_research_articles.url(**排除 domain_tier='blacklist' / clean_status≠'cleaned'
        的 tombstone/禁用行** · tombstone 可见性不变式:刻意禁用的 research 行不得复活进计功)
      → geo_research_article_citations 计数(时间窗内)。

    池 B —— 归因账本([WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 · 2026-08-06]):
      `geo_article_citation_attributions`(源头是监测池 `monitoring_results.search_citations`
      经 `strict_article_outcomes` 归一化归因)。
      🔴 加这个池的理由是实测数据:全站 246 个已发布 URL 里,进池 A 的**只有 1 个**,
      进池 B 的有 13 个(三家口径)。只查池 A = 查一个几乎空的池子,这正是
      `writing_strategy_outcome_events` 长期恒 0 行的直接原因。

    **两池皆只读**;写入面仍仅 writing_strategy_outcome_events + 写作指纹域。
    任一池查询异常 / 账本表未建 → 该池诚实计 0,不抛;两池皆无匹配 →
    citations=0 + insufficient_data=True,绝不造假。
    返回值额外带 research_pool_* / attribution_ledger_* 分池明细,便于判断水是从哪来的。
    """
    if not article_ids:
        return {"articles": 0, "citations": 0, "insufficient_data": True}
    ids = [int(a) for a in article_ids if a]
    if not ids:
        return {"articles": 0, "citations": 0, "insufficient_data": True}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                -- [WP12 P2-6 修] 原实现从 mhz_publish_order_items.article_id 取文章,
                -- 但该列**只存在于测试 fixture**;生产 DDL(db/meijiehezi_db.py)里
                -- article_id 挂在 mhz_publish_orders 上,items 只有 order_id。
                -- 结果是生产每次都抛 column does not exist → 被外层 except 吞掉 →
                -- 永远返回 citations=0 + insufficient_data,而 fixture 测试恒绿。
                -- 改走真实链路 items.order_id -> orders.article_id。
                SELECT COUNT(DISTINCT gra.id) AS matched_articles,
                       COUNT(c.id)            AS citations
                FROM mhz_publish_order_items i
                JOIN mhz_publish_orders o
                     ON o.id = i.order_id
                JOIN geo_research_articles gra
                     ON gra.url = i.publish_url
                    AND gra.domain_tier <> 'blacklist'
                    AND gra.clean_status = 'cleaned'
                JOIN geo_research_article_citations c
                     ON c.article_id = gra.id
                    AND c.cited_at >= NOW() - make_interval(days => %s)
                WHERE o.article_id = ANY(%s)
                  AND i.status = 'published'
                  AND btrim(COALESCE(i.publish_url, '')) <> ''
                """,
                (int(since_days), ids),
            )
            row = cur.fetchone()
        matched = int((row["matched_articles"] if isinstance(row, dict) else row[0]) or 0) if row else 0
        citations = int((row["citations"] if isinstance(row, dict) else row[1]) or 0) if row else 0
    except Exception as exc:
        logger.warning("[outcome-citation] 被引 join 失败(诚实 insufficient,不造假): %s", str(exc)[:200])
        matched, citations = 0, 0

    # [WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 · 2026-08-06] 第二个池:归因账本(监测池口径)。
    # 上面那段查的是 geo_research 语料池 —— 2026-08-06 生产实测,全站 246 个已发布 URL
    # 里进那个池的**只有 1 个**(URL 归一化后也只到 2),而监测池里有 13 个。飞轮一直
    # 只查前者,所以 writing_strategy_outcome_events 恒 0 行。这里把两个池**相加**而不是
    # 替换:geo_research 侧命中的那 1~2 个是真的,没有理由丢掉。
    # 账本表缺失 / 迁移未跑 → ledger 侧诚实返 0,总数退回原行为,不抛。
    ledger_matched, ledger_citations = 0, 0
    try:
        from services.article_attribution_ledger import citation_counts_for_articles

        ledger = citation_counts_for_articles(ids, since_days=since_days)
        ledger_matched = int(ledger.get("matched_articles") or 0)
        ledger_citations = int(ledger.get("citations") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[outcome-citation] 归因账本读取失败(只影响账本侧,不影响语料池侧): %s",
                       str(exc)[:200])

    total_matched = matched + ledger_matched
    total_citations = citations + ledger_citations
    # 只有真实发布并被匹配(matched>0)才算数据充分;否则诚实 insufficient。
    return {
        "articles": len(ids),
        "matched_articles": total_matched,
        "citations": total_citations,
        "research_pool_matched": matched,
        "research_pool_citations": citations,
        "attribution_ledger_matched": ledger_matched,
        "attribution_ledger_citations": ledger_citations,
        "insufficient_data": total_matched == 0,
    }


def _age_days(iso_str: str | None) -> float | None:
    if not iso_str:
        return None
    try:
        s = str(iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return None


def _iter_active_versions(min_age_days: int) -> list[dict[str, Any]]:
    """从版本面取上线 ≥min_age_days 的 active 版本 + 它替换的旧版(按 rollback_parent / stable 基线)。"""
    from writing.style_control import load_control_state

    state = load_control_state()
    active = state.get("active_by_style") or {}
    stable = state.get("stable_by_style") or {}
    versions = {v.get("version_id"): v for v in state.get("versions") or []}
    out: list[dict[str, Any]] = []
    for style_code, vid in active.items():
        v = versions.get(vid)
        if not v:
            continue
        age = _age_days(v.get("activated_at"))
        if age is None or age < min_age_days:
            continue
        parent_id = v.get("rollback_parent") or stable.get(style_code)
        parent = versions.get(parent_id) if parent_id and parent_id != vid else None
        out.append({"style_code": style_code, "new": v, "old": parent, "age_days": round(age, 1)})
    return out


def _measure(
    entry: dict[str, Any],
    since_days: int,
    citation_fn: Callable[[list[int], int], dict[str, Any]],
) -> dict[str, Any]:
    new_v = entry["new"]
    old_v = entry.get("old")
    # [W6 归因 · 出口审核修] 用 style_version_id(稳定)而非 prompt_sha256(每篇渲染漂移永不匹配)
    new_articles = list_articles_by_style_version_id(new_v.get("version_id") or "", since_days=since_days)
    new_cite = citation_fn(new_articles, since_days)
    try:
        from writing.style_registry import WRITING_STYLES
        style_name = (WRITING_STYLES.get(entry["style_code"]) or {}).get("name") or entry["style_code"]
    except Exception:
        style_name = entry["style_code"]
    result = {
        "style_code": entry["style_code"],
        "style_name": style_name,  # [review fix] 前端 C2/C6 渲染人话名,不裸吐 style_code
        "new_version_id": new_v.get("version_id"),
        "age_days": entry["age_days"],
        "new_articles": new_cite["articles"],
        "new_citations": new_cite["citations"],
    }
    if not old_v:
        result.update({"comparable": False, "reason": "无旧版基线(首个 active),不比较"})
        return result
    # [review fix · 黄灯窗口] 旧版 cohort 锚定到「新版上线时刻」往前 since_days 取样:
    # 旧版在新版上线(≥30 天前)后已停产,用「最近 30 天」窗口取旧版恒为空 → 黄灯按构造永不可亮。
    old_articles = list_articles_by_style_version_id(
        old_v.get("version_id") or "", since_days=since_days, until_iso=new_v.get("activated_at")
    )
    old_cite = citation_fn(old_articles, since_days)
    new_rate = (new_cite["citations"] / new_cite["articles"]) if new_cite["articles"] else 0.0
    old_rate = (old_cite["citations"] / old_cite["articles"]) if old_cite["articles"] else 0.0
    insufficient = new_cite.get("insufficient_data") or old_cite.get("insufficient_data")
    rollback = evaluate_rollback_suggestion(
        new_rate=new_rate, old_rate=old_rate,
        new_sample=new_cite["articles"], old_sample=old_cite["articles"],
    )
    result.update({
        "comparable": True,
        "old_version_id": old_v.get("version_id"),
        "old_articles": old_cite["articles"],
        "old_citations": old_cite["citations"],
        "new_rate": round(new_rate, 4),
        "old_rate": round(old_rate, 4),
        "citation_delta_per_article": round(new_rate - old_rate, 4),
        "insufficient_data": bool(insufficient),
        "rollback_suggestion": rollback,
    })
    return result


def _write_outcome(measure: dict[str, Any], industry_key: str = "general") -> None:
    """写 outcome event + prediction/outcome 对(供 W6.3 校准读库)。strategy_id NULL,细节进 metadata。"""
    from db.writing_style_flywheel_db import init_writing_style_flywheel_tables

    init_writing_style_flywheel_tables()
    delta = int(round((measure.get("citation_delta_per_article") or 0) * max(1, measure.get("new_articles") or 0)))
    with get_db() as conn:
        cur = conn.cursor()
        # [FIX-4] 幂等写入:new_version_id 提列 + measurement_window=当周(date_trunc('week'))。
        #   同 (version, 周, 行业) 重跑(手动+定时同周)→ ON CONFLICT DO UPDATE 刷新不新增行;
        #   跨周是新测量新行(消费端按版本取最新窗口一行,不跨周求和 → 不线性膨胀)。
        cur.execute(
            """
            INSERT INTO writing_strategy_outcome_events
                (strategy_id, article_id, industry_key, publish_status,
                 ai_citations_delta_30d, metadata, new_version_id, measurement_window, observed_at)
            VALUES (NULL, NULL, %s, %s, %s, %s, %s, date_trunc('week', NOW())::date, NOW())
            ON CONFLICT (new_version_id, measurement_window, industry_key)
                WHERE new_version_id IS NOT NULL
            DO UPDATE SET
                ai_citations_delta_30d = EXCLUDED.ai_citations_delta_30d,
                publish_status = EXCLUDED.publish_status,
                metadata = EXCLUDED.metadata,
                observed_at = NOW()
            """,
            (
                industry_key, "measured",
                delta,
                _jsonb({
                    "style_code": measure.get("style_code"),
                    "new_version_id": measure.get("new_version_id"),
                    "old_version_id": measure.get("old_version_id"),
                    "new_rate": measure.get("new_rate"),
                    "old_rate": measure.get("old_rate"),
                    "insufficient_data": measure.get("insufficient_data"),
                    "rollback_suggestion": measure.get("rollback_suggestion"),
                    "commentary": measure.get("commentary"),  # [V3] LLM 一句话评语(生产 dormant → None)
                }),
                measure.get("new_version_id"),
            ),
        )
        scope_key = f"writing_style:{measure.get('style_code')}:{industry_key}"
        cur.execute(
            """
            INSERT INTO geo_recommendation_predictions
                (scope_key, prediction_type, predicted_payload, version, measurement_window)
            VALUES (%s, %s, %s, %s, date_trunc('week', NOW())::date)
            ON CONFLICT (scope_key, version, measurement_window)
                WHERE measurement_window IS NOT NULL
            DO UPDATE SET predicted_payload = EXCLUDED.predicted_payload,
                          prediction_type = EXCLUDED.prediction_type
            RETURNING id
            """,
            (scope_key, "writing_style_citation_lift",
             _jsonb({"predicted_citation_lift_30d": measure.get("citation_delta_per_article") or 0}),
             str(measure.get("new_version_id") or "")[:80]),
        )
        pred_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO geo_recommendation_outcomes
                (prediction_id, scope_key, outcome_payload, measurement_window, observed_at)
            VALUES (%s, %s, %s, date_trunc('week', NOW())::date, NOW())
            ON CONFLICT (prediction_id)
                WHERE prediction_id IS NOT NULL
            DO UPDATE SET outcome_payload = EXCLUDED.outcome_payload, observed_at = NOW()
            """,
            (pred_id, scope_key,
             _jsonb({"ai_citations_delta_30d": measure.get("citation_delta_per_article") or 0,
                     "insufficient_data": measure.get("insufficient_data")})),
        )


def backfill_writing_outcomes(
    *,
    dry_run: bool = True,
    since_days: int = 30,
    min_age_days: int = 30,
    citation_fn: Callable[[list[int], int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """主入口。dry_run:列出将测量的版本 + 计数(不写库)。真跑:逐版本测量→写 outcome/prediction。

    citation_fn 可注入(测试/未来接真实源);缺省 = 生产 seam(当前诚实返回 0)。
    """
    fn = citation_fn or _citation_count_for_articles
    entries = _iter_active_versions(min_age_days)
    measures = [_measure(e, since_days, fn) for e in entries]

    # [V3] 附一句人话评语 —— 在锁外生成(LLM 慢,不拉长回写持锁时间;reader 警告点)。
    # dry_run/页面加载只取缓存(generate=False);真跑允许生成。生产 insufficient 恒真 → 恒 dormant。
    for m in measures:
        commentary = generate_outcome_commentary(m, since_days=since_days, generate=not dry_run)
        if commentary:
            m["commentary"] = commentary

    if dry_run:
        return {
            "mode": "dry_run",
            "eligible_versions": len(entries),
            "measures": measures,
            "note": "真跑将把上述测量写入 outcome_events + prediction/outcome(strategy_id NULL,细节进 metadata)。",
        }

    if not _BACKFILL_TLOCK.acquire(blocking=False):
        return {"status": "in_progress", "message": "回写正在进行中,请稍后再试。", "measures": measures}
    try:
        written = 0
        rollback_flags = 0
        for m in measures:
            if not m.get("comparable"):
                continue
            try:
                _write_outcome(m)
                written += 1
                if (m.get("rollback_suggestion") or {}).get("suggest_rollback"):
                    rollback_flags += 1
            except Exception as exc:
                logger.warning("[outcome-backfill] 写入失败 style=%s: %s", m.get("style_code"), str(exc)[:200])
        logger.info("[outcome-backfill] 完成 written=%s rollback_flags=%s", written, rollback_flags)
        return {"status": "completed", "written": written, "rollback_flags": rollback_flags, "measures": measures}
    finally:
        _BACKFILL_TLOCK.release()
        # [V7] 真回写改动 outcome 数据源 → 失效 outcome 读聚合 + 总汇总缓存(fail-soft)
        try:
            from writing.flywheel_cache import SCOPE_INSIGHT, SCOPE_OUTCOME, invalidate

            invalidate([SCOPE_OUTCOME, SCOPE_INSIGHT])
        except Exception:
            pass
