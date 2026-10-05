"""
定时监测调度器
- 按北京时间每天指定时刻触发
- 多品牌串行（避免 API 限流）、品牌内并行
- 独立线程执行，不阻塞主 Web 服务
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from services.sched_claim import (  # [WORKERS=4 · SPEC §2.2] per-job tick 级 claim(第二层防双跑)
    sched_claim,
    scheduler_sync_callable,
)
from services.flywheel_heartbeat import flywheel_job  # [A2] 飞轮 job 统一心跳 + 失败告警

logger = logging.getLogger("GEO-Scheduler")

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# 全局调度器实例（单例）
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()

# 调度器内部暴露的钩子函数（在 setup_schedule 内部定义后填入，外部用 import 访问）
# 用 dict 而不是给 setup_schedule 加属性，避免重新调 setup_schedule 时残留旧引用
_scheduler_hooks: dict = {}

# 执行状态
_running_status: Dict = {
    "is_running": False,
    "abort_requested": False,
    "current_brand": None,
    "progress": {},       # {brand_name: {total, completed, detected}}
    "last_run": None,
    "last_result": None,
}


class MonitoringBatchUnavailable(RuntimeError):
    """No monitoring row was persisted, so a delivery claim may be retried."""


def _run_freeze_sweep_hourly_job() -> Dict:
    """Run the async freeze sweeper from BackgroundScheduler's worker thread."""
    try:
        from services.freeze_sweeper import run_freeze_sweep_hourly

        return asyncio.run(run_freeze_sweep_hourly())
    except Exception as exc:
        logger.exception("[FreezeSweeper] scheduler wrapper failed: %s", exc)
        return {"scanned": 0, "released": 0, "failed": 0, "error": str(exc)}


def get_scheduler() -> BackgroundScheduler:
    """获取或创建调度器。

    [WORKERS=4 · SPEC §1/§2] .start() 现受 cron 触发闸 (services.cron_gate) 控制:
    仅 ROLE=cron 且选主成功的进程真正 start 并触发 job;web/backup 进程拿到的是
    未 start 的 scheduler(add_job 到未 start 的 scheduler 不会触发 → 天然不 4× 双跑)。
    选主在 server.py 启动路径完成后置闸,故导入期(闸未开)只创建+注册、不 start;
    leader 激活后再次调用本函数即 start(见 _maybe_start_scheduler)。
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(
                timezone=BEIJING_TZ,
                job_defaults={"coalesce": True, "max_instances": 1}
            )
            # flag 关的 V3.3.1 / 渠道等级 cron 在"创建期"注册(add_job 幂等 · 未 start 不触发)
            # —— 从原 start 分支移出,使 leader 激活时这些 job 已在册随之触发。
            try:
                from services.service_fee_cron import register_v3_3_1_jobs
                register_v3_3_1_jobs(_scheduler)
                logger.info("[Scheduler] V3.3.1 cron 已注册(flag 关时 job 内部直接 skip)")
            except Exception as v3_exc:
                logger.warning("[Scheduler] V3.3.1 cron 注册失败: %s", v3_exc)
            try:
                from services.channel_tier_cron import register_channel_tier_jobs
                register_channel_tier_jobs(_scheduler)
                logger.info("[Scheduler] 渠道等级激励 cron 已注册(flag 关时 job 内部 skip)")
            except Exception as tier_exc:
                logger.warning("[Scheduler] 渠道等级激励 cron 注册失败: %s", tier_exc)
        _maybe_start_scheduler(_scheduler)
        return _scheduler


def _maybe_start_scheduler(sch: "BackgroundScheduler"):
    """仅在 cron 触发闸开(ROLE=cron 且已选主)且 scheduler 未 running 时 start。
    SCHEDULER_ENABLED=false(staging)强制不 start;cron_gate 不可用 → 保守不 start(fail-closed)。"""
    import os as _os
    if _os.environ.get("SCHEDULER_ENABLED", "1").lower() in ("0", "false", "no"):
        return
    try:
        from services.cron_gate import cron_should_fire
    except Exception:
        return
    if cron_should_fire() and not sch.running:
        sch.start()
        logger.info("[Scheduler] 调度器已启动(cron leader)")


def get_status() -> Dict:
    """获取调度器状态"""
    scheduler = get_scheduler()
    jobs = []
    monitoring_enabled = False
    for job in scheduler.get_jobs():
        trigger = job.trigger
        # Tentatively-added APScheduler jobs may not expose next_run_time until
        # the scheduler has started. Status reads must stay available meanwhile.
        next_run = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else None,
            "trigger": str(trigger),
        })
        if job.id in (
            "keyword_subscription_daily_monitoring",
            "hourly_monitoring_check",
            "daily_monitoring",  # pre-unification compatibility during rolling restart
        ):
            monitoring_enabled = True

    return {
        "scheduler_running": scheduler.running,
        "monitoring_enabled": monitoring_enabled,
        "jobs": jobs,
        "is_executing": _running_status["is_running"],
        "current_brand": _running_status["current_brand"],
        "progress": _running_status["progress"],
        "last_run": _running_status["last_run"],
        "last_result": _running_status["last_result"],
    }


def _run_answer_entity_daily_extract():
    """[T5] 答案实体日增量抽取 job。运行时查 flag answer_entity_auto_extract:
    默认关 → 立即返回(仅一次 flag 读取,零 LLM/零写库/零行为变化,不需重启即可翻);
    开启 → 对待抽答案组做 only_pending 增量抽取,单日 cap(ANSWER_ENTITY_DAILY_EXTRACT_CAP,默认 500 组)防成本失控。
    平台承担 LLM 成本,不向用户扣费;失败 fail-soft 不影响其它 job。"""
    try:
        import os
        from writing.feature_switches import is_feature_enabled
        if not is_feature_enabled("answer_entity_auto_extract"):
            return
        cap = max(0, int(os.getenv("ANSWER_ENTITY_DAILY_EXTRACT_CAP", "500")))
        if cap <= 0:
            return
        from services.research_monitor.answer_entity_extractor import rebuild_answer_entities
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                rebuild_answer_entities(industry="", limit=cap, dry_run=False, only_pending=True)
            )
            logger.info(f"[Scheduler] 答案实体日增量抽取完成(cap={cap}): {result.get('mode') if isinstance(result, dict) else result}")
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[Scheduler] 答案实体日增量抽取失败(不影响其它 job): {e}")


def _run_query_intent_daily_classify():
    """[W1] 搜索问题类型日增量分类 job。运行时查 flag query_intent_auto_classify:
    默认关 → 立即返回(仅一次 flag 读取,零 LLM/零写库/零行为变化,不需重启即可翻);
    开启 → 对尚未打标签的搜索问题做增量分类,单日 cap(QUERY_INTENT_DAILY_CLASSIFY_CAP,默认 500 条)防成本失控。
    平台承担 LLM 成本,不向用户扣费;失败 fail-soft 不影响其它 job。"""
    try:
        import os
        from writing.feature_switches import is_feature_enabled
        if not is_feature_enabled("query_intent_auto_classify"):
            return
        cap = max(0, int(os.getenv("QUERY_INTENT_DAILY_CLASSIFY_CAP", "500")))
        if cap <= 0:
            return
        from services.research_monitor.query_intent_classifier import run_query_intent_backfill
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                run_query_intent_backfill(industry=None, limit=cap, dry_run=False)
            )
            logger.info(
                f"[Scheduler] 问题类型日增量分类完成(cap={cap}): "
                f"{result.get('classified') if isinstance(result, dict) else result}"
            )
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[Scheduler] 问题类型日增量分类失败(不影响其它 job): {e}")


def _run_writing_distill_auto():
    """[W2] 每月 1/16 日写作候选自动蒸馏 job。运行时查 flag writing_distill_auto:
    默认关 → 立即返回(零 LLM/零写库/零行为变化);
    开启 → 对达标 (行业×文体) 组做 LLM 蒸馏,单次 cap(WRITING_DISTILL_AUTO_CAP,默认 10 组)防成本失控。
    产物只落版本面 draft,绝不直接影响客户可见文章(须管理员看板 activate)。失败 fail-soft。"""
    try:
        import os
        from writing.feature_switches import is_feature_enabled
        if not is_feature_enabled("writing_distill_auto"):
            return
        cap = max(0, int(os.getenv("WRITING_DISTILL_AUTO_CAP", "10")))
        if cap <= 0:
            return
        from services.writing_answer_distiller import distill_candidates
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                distill_candidates(industry="", dry_run=False, cap=cap, actor_id=0, persist_draft=True)
            )
            logger.info(
                f"[Scheduler] 写作候选自动蒸馏完成(cap={cap}): "
                f"{result.get('distilled') if isinstance(result, dict) else result}"
            )
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"[Scheduler] 写作候选自动蒸馏失败(不影响其它 job): {e}")


@flywheel_job("article_attribution_sync")
def _run_article_attribution_sync():
    """[WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 · 2026-08-06]归因账本同步。

    把 `strict_article_outcomes` 已经算得出来、却从来没人落库的归因事实,幂等写进
    `geo_article_citation_attributions`:我方哪个已发布 URL、被哪个引擎、在哪次监测里
    引用、对应哪篇文章 / 哪个媒体位 / 什么问题族 / 什么 target_outcome。

    🔴 **monitoring 与 research 两域一行不写**,纯下游消费者。
    🔴 心跳 detail 会原样带上引擎的 `quality_counts` —— 没水的时候要能一眼看出是卡在
       哪道闸(生产实测主闸是 `monitoring_lineage_incomplete` 与 `publication_snapshot_missing`),
       而不是只显示一个"跑成功了 / written=0"。
    """
    from services.article_attribution_ledger import sync_article_attributions

    result = sync_article_attributions(dry_run=False)
    logger.info(
        f"[Scheduler] 归因账本同步完成(new={result.get('written')} "
        f"exists={result.get('already_present')} engine={result.get('engine_event_count')})"
    )
    return result


@flywheel_job("monitoring_outcome_backfill")
def _run_monitoring_outcome_backfill():
    """[P0-3/R2-2 · 2026-08-15] monitoring_results 存量 target_outcome 解析回填。

    🔴 [R2-2 签发闸] 自动 apply **默认关闭**:91,592 行历史改写属受控一次性
    回填,须 Owner/Review 单独签发后置 GEO_OUTCOME_BACKFILL_AUTO_APPLY=1 才真跑
    (生产回滚窗口已关,只能前向修复 —— 未签发前 cron 只报 gated,零写入)。
    真跑时:旧值账本先行(monitoring_outcome_backfill_journal,同一事务)+
    批次 ID 可按 scripts/restore_monitoring_outcome_backfill_2026_08_15.py 反做;
    空答案行落 no_answer_unjudgeable,不塞业务分类。
    O1:失败只影响本批,监测/发布/计费主链零依赖。
    """
    import os as _os
    from scripts.backfill_monitoring_outcome_2026_08_14 import run_backfill

    if str(_os.getenv("GEO_OUTCOME_BACKFILL_AUTO_APPLY", "")).strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        logger.info("[Scheduler] 监测 outcome 回填:签发闸未开(GEO_OUTCOME_BACKFILL_AUTO_APPLY),本轮 0 写入")
        return {"gated": True, "written": 0}
    batch = 5000
    try:
        batch = max(100, int(_os.getenv("GEO_OUTCOME_BACKFILL_BATCH", "5000")))
    except (TypeError, ValueError):
        pass
    result = run_backfill(apply=True, batch=batch, max_batches=4)
    logger.info(
        f"[Scheduler] 监测 outcome 回填完成(batch_id={result.get('batch_id')} "
        f"written={result.get('written')} journaled={result.get('journaled')} "
        f"scanned={result.get('scanned')} skipped_no_brand={result.get('skipped_no_brand')})"
    )
    return result


@flywheel_job("publication_outcome_facts")
def _run_publication_outcome_facts():
    """[WO-PRICING-FP §返工 · 2026-08-09]投放结果事实表重建。

    把「发布动作 × 客户词 × 引擎 × 7/14/30 天窗口」的**计数**落成两张派生事实表,
    供报价/诊断在查询侧现算比率(表里一个比率都不存)。

    🔴 事务内**全量重建**,不是纯 upsert:发布订单会从 published 改成 rejected/cancelled,
       URL/品牌/发布时间也会被订正。只增不删的话,过期战果会一直被下游当真。
    🔴 缺表(迁移 032 未跑)走 fail-soft 记 0 行不抛 —— 事实表是观测面,
       绝不能挡住发布/监测/计费主链。**代价是漏跑静默**,靠部署单核验收 SQL。
    """
    from services.publication_outcome_facts import build_publication_facts

    result = build_publication_facts(dry_run=False)
    if result.get("table_missing"):
        logger.warning("[Scheduler] 投放结果事实表缺失(迁移 032 未跑),本轮记 0 行")
        return result
    logger.info(
        f"[Scheduler] 投放结果事实表重建完成(attempt={result.get('attempt_rows')} "
        f"outcome={result.get('outcome_rows')} 清理过期={result.get('stale_rows_deleted')} "
        f"可观察={result.get('tests_observable')}/{result.get('tests_total')})"
    )
    return result


@flywheel_job("writing_outcome_backfill")
def _run_writing_outcome_backfill():
    """[WP9-P0-3 · Owner D-P0-3 授权点火]效果闭环回写。

    对上线 ≥30 天的 active 写作版本,读真实文章级被引(写作文章→mhz publish_url→
    geo_research_articles.url→citations · **只读跨界·排除 tombstone/blacklist 行**),
    回写 writing_strategy_outcome_events + 写作飞轮预测账本(**research 域一行不写**)。
    无发布/无匹配 → 诚实 insufficient,不写伪造功效。

    [A2 · 2026-07-29] 原来的 `except → logger.warning` 已删除:异常由 @flywheel_job 统一
    落心跳表 + 拉 AIOps 告警,不再静默吞。被包裹后仍不会向 APScheduler 抛,不影响其它 job。
    """
    from services.writing_outcome_backfill import backfill_writing_outcomes
    result = backfill_writing_outcomes(dry_run=False, since_days=30, min_age_days=30)
    _written = (result or {}).get("written") if isinstance(result, dict) else None
    logger.info(f"[Scheduler] 写作效果闭环回写完成(written={_written})")
    return result


@flywheel_job("corpus_value_labeling")
def _run_corpus_value_labeling():
    """[B2 · 2026-07-29] 语料价值标注:给被引语料打"能不能复用/什么场景/几档"标签。

    生产实证 23474 条引用 / 16103 篇被引文章当前无人消费。判断点默认关时
    整轮走规则兜底(按被引次数与意图判),仍然出水,只是判断粗一些。
    research 域一行不写,标签落 flywheel_corpus_value_labels。
    """
    from services.flywheel_corpus_value import run_corpus_value_labeling
    result = run_corpus_value_labeling()
    logger.info(f"[Scheduler] 语料价值标注完成: {result}")
    return result


@flywheel_job("writing_assignment_outcome_backfill")
def _run_writing_assignment_outcome_backfill():
    """[A4 · 2026-07-29] 指派级效果回流:writing_strategy_assignments → outcome_events。

    与上面的 `writing_outcome_backfill` 分工:那条按**文体版本**聚合(style_control 血缘),
    这条按**quote 级策略指派**聚合(strategy_versions 血缘),两条各写各的行不互相覆盖
    (幂等键分别是 new_version_id 和 quote_id,partial unique 互不重叠)。
    """
    from services.writing_strategy_assignment import backfill_assignment_outcomes
    result = backfill_assignment_outcomes(dry_run=False, min_age_days=30, since_days=30)
    logger.info(f"[Scheduler] 写作指派效果回流完成: {result}")
    return result


@flywheel_job("source_signal_lineage_backfill")
def _run_source_signal_lineage_backfill():
    """[工单 B §4 · 2026-07-27] 引用信号血缘回填:source_signals.article_id 补上。

    生产 71403 行该列全空,结构研究每次都得现场 JOIN url_hash 凑,outcome 归因没有落脚点。
    幂等:只写 article_id IS NULL 的行,重复跑不会改动已写的血缘。
    行业对不上且同 hash 多篇的少数行**诚实留空**,不猜(见 service 模块头的口径说明)。
    """
    from services.geo_source_signal_lineage_backfill import backfill_source_signal_article_ids

    result = backfill_source_signal_article_ids()
    logger.info(f"[Scheduler] 引用信号血缘回填完成: {result}")
    return result


@flywheel_job("writing_effectiveness_review")
def _run_writing_effectiveness_review():
    """[WP12 P2-6] 两周一次写作有效性复核报告。

    读「我方已发布 URL → geo_research_articles → citations」血缘,按文体 / 长度档 /
    域 / 引擎分列,并把北极星「我方已发布 URL 被引数」放在最前。
    **只写 writing_effectiveness_reports 一张存档表**,绝不改文体配比或价格。
    数据不可用时诚实落 unavailable,不编造数字。fail-soft:异常不影响其它 job。
    """
    from datetime import date

    from services.writing_effectiveness_report import (
        build_writing_effectiveness_report,
        persist_writing_effectiveness_report,
    )

    report = build_writing_effectiveness_report()
    if not report.get("available"):
        # 数据不足是**诚实的正常结局**(不是失败):记成功 + processed=0,不拉误报警。
        logger.warning(
            f"[Scheduler] 写作有效性复核:数据不可用({report.get('reason')}),本期不出结论"
        )
        return {"processed": 0, "available": False, "reason": report.get("reason")}
    stored = persist_writing_effectiveness_report(report, report_key=date.today().isoformat())
    north = report.get("north_star") or {}
    logger.info(
        "[Scheduler] 写作有效性复核完成(北极星 已发布 %s / 被引 %s · stored=%s)",
        north.get("published_urls"), north.get("cited_urls"), stored.get("stored"),
    )
    return {
        "processed": 1,
        "available": True,
        "published_urls": north.get("published_urls"),
        "cited_urls": north.get("cited_urls"),
    }


def _run_geo_article_evolution_cycle():
    """Persist the 1st/16th GEO article truth snapshot; no automatic activation."""
    try:
        from writing.feature_switches import is_feature_enabled
        if not is_feature_enabled("geo_article_evolution_auto"):
            return
        from services.research_monitor.corpus_labeler import promote_jc5_from_direct_signals
        from services.article_evolution_cycle import persist_evolution_cycle

        label_result = promote_jc5_from_direct_signals(dry_run=False, limit=1000)
        result = persist_evolution_cycle(actor_user_id=0, trigger="scheduler")
        logger.info(
            "[Scheduler] GEO 文章半月进化审计已生成: %s · JC5 promoted=%s",
            (result.get("snapshot") or {}).get("cycle_key"), label_result.get("promoted", 0),
        )
    except Exception as e:
        logger.warning(f"[Scheduler] GEO 文章半月进化审计失败(不影响其它 job): {e}")


def _run_selfserve_queue_consume():
    """[R 批 · U4] 代理自助调研队列周期兜底消费 job(每 3 分钟)。

    端点提交后已用 BackgroundTask 即时触发消费,本 job 兜住「任务排队时无端点触发 / 上一轮跑完
    还有排队 / BackgroundTask 触发失败」的场景。内部:无活跃 round 才取最旧一条 queued 派发(单条)。
    无排队 → 一次 SELECT 即返回,近零成本。fail-soft。"""
    try:
        from services.research_monitor.selfserve_worker import consume_selfserve_queue_sync
        consume_selfserve_queue_sync()
    except Exception as e:
        logger.warning(f"[Scheduler] 自助调研队列消费失败(不影响其它 job): {e}")


def _run_selfserve_pending_reap():
    """[#4] 代理自助调研 stale 'pending' 孤儿回收 job(每 5 分钟)。

    P0-1 扩大 freeze→promote 窗口;进程被杀在窗口内 → task 永卡 pending(钱已冻):worker 只领 queued、
    freeze_sweeper 排除 pending、restart_recovery 只管 round → 钱冻死无自愈。本 job 兜底:超阈值
    (默认 15min)的 stale pending → release 退款 + cancel(pending_orphan_reaped)。无孤儿 → 一次
    SELECT 即返回,近零成本。fail-soft。"""
    try:
        from services.research_monitor.selfserve_worker import reap_stale_pending_selfserve_sync
        reap_stale_pending_selfserve_sync()
    except Exception as e:
        logger.warning(f"[Scheduler] 自助调研 pending 孤儿回收失败(不影响其它 job): {e}")


def setup_schedule(hour: int = 0, minute: int = 0, enabled: bool = True):
    """
    设置定时监测总开关
    每小时检查一次，按客户各自的 monitoring_interval_hours 和 monitoring_start_hour 决定是否执行
    """
    scheduler = get_scheduler()
    job_id = "hourly_monitoring_check"

    # 移除旧任务（兼容旧的 daily_monitoring）
    for old_id in ["daily_monitoring", "hourly_monitoring_check"]:
        if scheduler.get_job(old_id):
            scheduler.remove_job(old_id)

    if not enabled:
        for aux_id in ["daily_service_check", "daily_compliance_check"]:
            if scheduler.get_job(aux_id):
                scheduler.remove_job(aux_id)
        logger.info(f"[Scheduler] 定时监测已关闭（含辅助任务）")
        _save_schedule_config(hour, minute, enabled=False)
        return {"status": "disabled"}

    # 每小时整点检查
    trigger = CronTrigger(minute=0, timezone=BEIJING_TZ)
    scheduler.add_job(
        _hourly_check,
        trigger=trigger,
        id=job_id,
        name="每小时监测检查",
        replace_existing=True,
    )

    # 辅助任务保持不变
    check_id = "daily_service_check"
    if not scheduler.get_job(check_id):
        scheduler.add_job(
            _check_service_periods,
            trigger=CronTrigger(hour=1, minute=0, timezone=BEIJING_TZ),
            id=check_id,
            name="每日服务期检查 01:00 CST",
            replace_existing=True,
        )

    compliance_id = "daily_compliance_check"
    if not scheduler.get_job(compliance_id):
        scheduler.add_job(
            _run_compliance_check,
            trigger=CronTrigger(hour=23, minute=30, timezone=BEIJING_TZ),
            id=compliance_id,
            name="每日达标判定 23:30 CST",
            replace_existing=True,
        )

    # v3.2: T+3 佣金结算（每天凌晨 2 点跑）
    commission_settle_id = "daily_commission_settle"
    if not scheduler.get_job(commission_settle_id):
        scheduler.add_job(
            _run_commission_settlement,
            trigger=CronTrigger(hour=2, minute=0, timezone=BEIJING_TZ),
            id=commission_settle_id,
            name="T+3 佣金结算 02:00 CST",
            replace_existing=True,
        )

    # 2026-05-22 P0 fix(Deploy-CTO D0 实证):
    # point_freezes 表 8 笔历史 zombie · 通用 sweep job 在 prod 0 跑
    # 项目地图 v2 §3.9 F7 老板预期"sweep job 1h 自动 release"未落地
    # 新增 freeze_sweeper hourly cron · 扫 12h+ frozen 自动 release(调 release_freeze 公开接口)
    # 不动 middleware/billing.py(🔴 A 级红线)· 见 services/freeze_sweeper.py
    freeze_sweep_id = "freeze_sweeper_hourly"
    if not scheduler.get_job(freeze_sweep_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            scheduler.add_job(
                _run_freeze_sweep_hourly_job,
                trigger=IntervalTrigger(hours=1),
                id=freeze_sweep_id,
                name="point_freezes zombie 兜底扫描(每 1h · 12h+ frozen 自动 release)",
                replace_existing=True,
            )
            logger.info("[Scheduler] freeze_sweeper hourly 已注册(每 1h)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 freeze_sweeper 失败: {e}")

    # [WORKERS=4 · SPEC §3.4/§3.6a] 诊断资金 sweeper/reconciler 的**无条件注册**已移入
    #   register_v32_core_tasks(避免 BUG-004 同坑:setup_schedule 受 auto_monitor_enabled 门控 · prod=0 永不注册)。
    #   此处不再注册 —— 见 register_v32_core_tasks 内 "diagnosis_run_sweeper" 块。

    # [B1-4] 调研监测 zombie round 周期兜底 sweep(每 15min)
    # 修前:sweep_zombie_rounds 只挂 server.py @app.on_event("startup")。若服务器崩后 10min 内重启,
    #       死 round 心跳仍"新鲜"不被标记 → 永久卡 running,阻断后续所有 cron/手动跑批。
    # 复用既有周期调度器(job_defaults coalesce+max_instances=1 防重叠 + server.py Redis 锁保证单持有者),
    # startup sweep 保留。sweep_zombie_rounds 内部已全异常兜底、返回 dict(scheduler 忽略返回值)。
    research_zombie_sweep_id = "research_monitor_zombie_sweep"
    if not scheduler.get_job(research_zombie_sweep_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            from services.research_monitor.restart_recovery import sweep_zombie_rounds
            scheduler.add_job(
                sweep_zombie_rounds,
                trigger=IntervalTrigger(minutes=15),
                id=research_zombie_sweep_id,
                name="调研监测 zombie round 周期兜底 sweep(每 15min · startup sweep 之外)",
                replace_existing=True,
            )
            logger.info("[Scheduler] research_monitor zombie sweep 已注册(每 15min)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 research_monitor zombie sweep 失败: {e}")

    # [包E 2026-08-24] Z-1 只读告警的注册**已搬走** —— 见 register_v32_core_tasks。
    #   搬的理由不是整洁:本函数受 auto_monitor_enabled 门控(生产实测 = 0),
    #   而且 enabled=False 时它在上面就 return 了。挂在这里 = 注册了但永远不跑,
    #   与 BUG-004 / P0-G / B1-4 / T5 / 飞轮那五次是同一个坑的第六次复发。
    #   症状会长成:「发布结算冻了 7 天,没有任何人收到告警」。

    # [B5-1] 媒体实体效果回流(每周一 05:00)· 幂等 · 内部 advisory 锁防双跑 · 只写飞轮快照表
    #   补 outcome_rollup(published_count / citation_lift_30d)回写数据源,让 shadow_score 的 0.10 outcome 权重真正闭环。
    media_outcome_id = "media_entity_outcome_sync"
    if not scheduler.get_job(media_outcome_id):
        try:
            from services.media_outcome_sync import sync_media_entity_outcome_rollups
            scheduler.add_job(
                sync_media_entity_outcome_rollups,
                trigger=CronTrigger(day_of_week="mon", hour=5, minute=0, timezone=BEIJING_TZ),
                id=media_outcome_id,
                name="媒体实体效果回流(每周一 05:00 · outcome_rollup 回写)",
                replace_existing=True,
            )
            logger.info("[Scheduler] media outcome sync 已注册(每周一 05:00)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 media outcome sync 失败: {e}")

    # P0-5 fix · social agent · mark_stale_plans + consolidate_profile_memory cron
    # 修前:函数定义存在但 0 处自动触发 · 24h stale 永远不转 · Memory v3 consolidation 整套白做
    social_agent_stale_id = "social_agent_mark_stale_plans"
    if not scheduler.get_job(social_agent_stale_id):
        scheduler.add_job(
            _social_agent_mark_stale_plans,
            trigger=CronTrigger(hour=3, minute=0, timezone=BEIJING_TZ),
            id=social_agent_stale_id,
            name="社媒 Agent · 标记 stale plans 03:00 CST",
            replace_existing=True,
        )

    social_agent_consolidate_id = "social_agent_consolidate_memory"
    if not scheduler.get_job(social_agent_consolidate_id):
        scheduler.add_job(
            _social_agent_consolidate_memory,
            trigger=CronTrigger(hour=3, minute=30, timezone=BEIJING_TZ),
            id=social_agent_consolidate_id,
            name="社媒 Agent · Memory v3 consolidation 03:30 CST",
            replace_existing=True,
        )

    # v3.2 Phase 3: 代理试用过期清理（每 5 分钟）
    trial_expire_id = "trial_pass_expire"
    if not scheduler.get_job(trial_expire_id):
        from apscheduler.triggers.interval import IntervalTrigger
        scheduler.add_job(
            _run_trial_pass_expire,
            trigger=IntervalTrigger(minutes=5),
            id=trial_expire_id,
            name="代理试用过期清理（每 5 分钟）",
            replace_existing=True,
        )

    # v3.3: GEO 托管套餐 tick（每 6 小时）
    managed_tick_id = "managed_campaign_tick"
    if not scheduler.get_job(managed_tick_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            from tools.geo_managed.campaign_tick import managed_campaign_tick
            scheduler.add_job(
                managed_campaign_tick,
                trigger=IntervalTrigger(hours=6),
                id=managed_tick_id,
                name="GEO 托管套餐 tick（每 6h）",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO 托管 tick 已注册（每 6h）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 GEO 托管 tick 失败: {e}")

    # v3.3: pending_review 24h 自动发布（每 30 分钟扫一次）
    pending_review_id = "managed_pending_review_auto_publish"
    if not scheduler.get_job(pending_review_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            from tools.geo_managed.campaign_tick import process_due_pending_reviews
            scheduler.add_job(
                process_due_pending_reviews,
                trigger=IntervalTrigger(minutes=30),
                id=pending_review_id,
                name="GEO 托管半自动模式 24h 自动发（每 30min）",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO 托管 pending_review 自动发已注册（每 30min）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 pending_review 失败: {e}")

    # v3.3: 12 个月休眠扫描（每天 03:00 跑）
    dormancy_id = "managed_dormancy_scan"
    if not scheduler.get_job(dormancy_id):
        try:
            from tools.geo_managed.campaign_tick import dormancy_scan
            scheduler.add_job(
                dormancy_scan,
                trigger=CronTrigger(hour=3, minute=0, timezone=BEIJING_TZ),
                id=dormancy_id,
                name="GEO 托管 12 个月休眠扫描 03:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO 托管 dormancy_scan 已注册（每天 03:00）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 dormancy_scan 失败: {e}")

    # v3.4: 周一凌晨复盘引擎
    weekly_review_id = "managed_weekly_review"
    if not scheduler.get_job(weekly_review_id):
        try:
            from tools.geo_managed.campaign_tick import weekly_review_brand_packages
            scheduler.add_job(
                weekly_review_brand_packages,
                trigger=CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=BEIJING_TZ),
                id=weekly_review_id,
                name="GEO 托管 v3.4 周复盘引擎 周一 04:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO 托管 weekly_review 已注册（周一 04:00）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 weekly_review 失败: {e}")

    publish_pool_id = "publish_media_effective_pool_distill"
    if not scheduler.get_job(publish_pool_id):
        try:
            scheduler.add_job(
                _run_media_effective_pool_distill,
                trigger=CronTrigger(day_of_week="mon", hour=3, minute=30, timezone=BEIJING_TZ),
                id=publish_pool_id,
                name="V2.3 媒体推荐池蒸馏 周一 03:30 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] V2.3 媒体推荐池蒸馏已注册（周一 03:30）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册媒体推荐池蒸馏失败: {e}")

    publish_outcome_id = "publish_outcome_sync"
    if not scheduler.get_job(publish_outcome_id):
        try:
            scheduler.add_job(
                _run_publish_outcome_sync,
                trigger=CronTrigger(hour=4, minute=15, timezone=BEIJING_TZ),
                id=publish_outcome_id,
                name="V2.3 投放结果回流 每天 04:15 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] V2.3 投放结果回流已注册（每天 04:15）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册投放结果回流失败: {e}")

    # M1a A8(CTO-15.9 2026-04-25)· 24h/72h 流失提醒(每 30 分钟扫一次)
    # 基于 pipeline_stage_log · 24h 无下一步的诊断 / 报价 触发 notifications
    flow_drop_reminder_id = "m1a_flow_drop_reminder"
    if not scheduler.get_job(flow_drop_reminder_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            scheduler.add_job(
                _m1a_flow_drop_reminder,
                trigger=IntervalTrigger(minutes=30),
                id=flow_drop_reminder_id,
                name="M1a 24/72h 流失提醒扫描(每 30min)",
                replace_existing=True,
            )
            logger.info("[Scheduler] M1a 流失提醒已注册(每 30min)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 M1a 流失提醒失败: {e}")

    # A.9-B (CTO-15.9 session 3 · 2026-04-25 · 老板拍板"默认关 · 代理同意才扣")
    # 自动 monitor 24h after publish · 每 30 分钟扫 1 次
    # 仅对 user_wallets.auto_monitor_after_publish=TRUE 的代理触发
    # 元指令 11 遵守:charge_on_success(完成才扣)· 失败不扣 · 代理明示同意
    auto_monitor_id = "m1a_auto_monitor_after_publish"
    if not scheduler.get_job(auto_monitor_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            scheduler.add_job(
                _m1a_auto_monitor_after_publish,
                trigger=IntervalTrigger(minutes=30),
                id=auto_monitor_id,
                name="A.9-B 自动 monitor 24h after publish(每 30min)",
                replace_existing=True,
            )
            logger.info("[Scheduler] A.9-B 自动 monitor 已注册(每 30min)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 A.9-B 自动 monitor 失败: {e}")

    # CTO-11.0 修：深度行业解析僵尸任务清理（每 5 分钟）
    # 场景：worker 重启 / event loop 崩溃 / 内层 except 未捕获 → 状态卡 collecting_l*
    # 兜底：扫 15min 外仍在运行态的，强制 failed + 退款
    industry_brief_watchdog_id = "industry_brief_watchdog"
    if not scheduler.get_job(industry_brief_watchdog_id):
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            scheduler.add_job(
                _industry_brief_watchdog,
                trigger=IntervalTrigger(minutes=5),
                id=industry_brief_watchdog_id,
                name="深度解析僵尸任务清理（每 5 分钟）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 深度解析看门狗已注册（每 5min）")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册深度解析看门狗失败: {e}")

    _save_schedule_config(hour, minute, enabled=True)
    logger.info(f"[Scheduler] 每小时监测检查已启动")

    scheduled_job = scheduler.get_job(job_id)
    next_run = getattr(scheduled_job, "next_run_time", None) if scheduled_job else None
    return {
        "status": "enabled",
        "time": "每小时整点",
        "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else None,
    }


@flywheel_job("media_effective_pool_distill")
def _run_media_effective_pool_distill():
    """[A3 · 2026-07-29] 原来的 `except → return {'status':'error'}` 已删除:失败由
    @flywheel_job 落心跳 + 告警。返回值里的处理量会记进心跳表的 processed 列。"""
    from scripts.distill_media_effective_pool import distill
    result = distill()
    logger.info(f"[Scheduler] V2.3 媒体推荐池蒸馏完成: {result}")
    return result


@flywheel_job("publish_outcome_sync")
def _run_publish_outcome_sync():
    from db.publish_db import sync_publish_outcome_records
    result = sync_publish_outcome_records(window_days=30)
    logger.info(f"[Scheduler] V2.3 投放结果回流完成: {result}")
    return result


@flywheel_job("media_entity_outcome_sync")
def _run_media_entity_outcome_sync():
    """[A2] 媒体实体效果回流的心跳包裹层。

    修前这个 job 直接把 `sync_media_entity_outcome_rollups` 挂进调度器,没有任何执行留痕
    (`sched_job_runs` 里一行也没有,因为它不走 claim 路径)—— 跑没跑成没人看得见。"""
    from services.media_outcome_sync import sync_media_entity_outcome_rollups
    result = sync_media_entity_outcome_rollups()
    logger.info(f"[Scheduler] 媒体实体效果回流完成: {result}")
    return result


def _pool_staleness_days() -> Optional[float]:
    """媒体推荐池最近刷新距今天数。查询失败返 None(调用方按"无法判定"处理,不擅自补跑)。"""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(updated_at))) / 86400.0 AS stale_days "
                "FROM media_effective_pool"
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or row["stale_days"] is None:
            return None
        return float(row["stale_days"])
    except Exception as e:
        logger.warning(f"[Scheduler] 媒体推荐池新鲜度查询失败: {e}")
        return None


def run_now():
    """立即执行一次全品牌监测（后台线程）"""
    if _running_status["is_running"]:
        return {"status": "error", "error": "已有监测任务在执行中"}

    _running_status["abort_requested"] = False
    thread = threading.Thread(target=_run_all_brands, daemon=True)
    thread.start()
    return {"status": "started", "message": "全品牌监测已启动（后台执行）"}


def stop_monitoring():
    """请求停止当前正在执行的监测任务"""
    if not _running_status["is_running"]:
        return {"status": "error", "error": "当前没有监测任务在执行"}

    _running_status["abort_requested"] = True
    logger.info("[Scheduler] 收到停止请求，将在当前品牌完成后停止")
    return {"status": "stopping", "message": "正在停止，当前品牌完成后会中断"}


def _save_schedule_config(hour: int, minute: int, enabled: bool):
    """保存调度配置到数据库"""
    try:
        from db.monitoring_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cron_str = f"{minute} {hour} * * *"
            cur.execute("""
                UPDATE monitoring_config
                SET auto_monitor_enabled = %s, auto_monitor_cron = %s, updated_at = CURRENT_TIMESTAMP
                WHERE client_id = '_global_'
            """, (1 if enabled else 0, cron_str))
            conn.commit()
            conn.close()
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"[Scheduler] 保存配置失败: {e}")


def _run_all_brands():
    """执行全品牌监测（被 run_now 和旧 cron 调用，统一走扣费逻辑）"""
    from db.monitoring_db import get_monitoring_enabled_clients
    clients = get_monitoring_enabled_clients()
    if not clients:
        logger.info("[Scheduler] 无需要监测的客户")
        return
    _run_clients_with_billing(clients)


def _hourly_check():
    """每小时检查：哪些客户该在这个小时执行监测"""
    from db.monitoring_db import get_monitoring_enabled_clients
    now = datetime.now(BEIJING_TZ)
    current_hour = now.hour

    clients = get_monitoring_enabled_clients()
    to_run = []

    for client in clients:
        interval_hours = client.get("monitoring_interval_hours") or 24
        start_hour = client.get("monitoring_start_hour", 8)
        last_run = _normalize_beijing_dt(client.get("monitoring_last_run_at"))

        # [CTO-15.23 2026-05-09 Kimi 月烧治理] 03:00 整点雪崩 hash 分散
        # 蒸馏报告:13510 请求/8 天 · 03:00 整点首笔精度到秒 · 41% 凌晨 · 持续 4-12 小时
        # 根因:大量客户 monitoring_start_hour=3 集中触发 · WORKERS=1 队列长 + Kimi RPM=60 限流 · 重试浪费
        # 修法:按 brand_id hash 分散到 [start_hour, start_hour+5] 窗口内 · 平滑并发
        brand_id = client.get("brand_id") or 0
        hash_offset = (brand_id * 7 + (client.get("id") or 0)) % 6  # 0-5 小时偏移
        effective_start_hour = (start_hour + hash_offset) % 24

        if last_run is None:
            if current_hour == effective_start_hour:
                to_run.append(client)
            continue

        elapsed_seconds = (now - last_run).total_seconds()
        if elapsed_seconds >= interval_hours * 3600:
            to_run.append(client)

    if not to_run:
        logger.info(f"[Scheduler] {current_hour:02d}:00 无客户需要监测")
        return

    logger.info(f"[Scheduler] {current_hour:02d}:00 有 {len(to_run)} 个客户需要监测")
    thread = threading.Thread(target=_run_clients_with_billing, args=(to_run,), daemon=True)
    thread.start()


def _normalize_beijing_dt(value):
    """把 DB 里的 naive/aware/string timestamp 统一成北京时间 aware datetime。"""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return BEIJING_TZ.localize(value)
    return value.astimezone(BEIJING_TZ)


def _run_clients_with_billing(clients: list):
    """带扣费的客户监测执行"""
    if _running_status["is_running"]:
        logger.warning("[Scheduler] 跳过：上一轮尚未完成")
        return

    _running_status["is_running"] = True
    _running_status["abort_requested"] = False
    _running_status["progress"] = {}
    _running_status["current_brand"] = None
    start_time = datetime.now()

    logger.info(f"[Scheduler] ========== 开始监测 {len(clients)} 个客户 ==========")

    # 去重（同一个 brand_id 只跑一次）
    brand_map = {}
    for c in clients:
        bid = c["brand_id"]
        if bid not in brand_map or c["kw_count"] > brand_map[bid]["kw_count"]:
            brand_map[bid] = c
    clients = list(brand_map.values())

    total_results = []

    try:
        for i, client in enumerate(clients):
            if _running_status["abort_requested"]:
                logger.info(f"[Scheduler] 收到停止请求，已完成 {i}/{len(clients)} 个品牌后中断")
                break

            if client["kw_count"] == 0:
                logger.info(f"[Scheduler] 跳过 {client['brand_name']}（无关键词）")
                continue

            brand_name = client["brand_name"]
            kw_count = client["kw_count"]
            quote_id = client["quote_id"]
            _running_status["current_brand"] = f"({i+1}/{len(clients)}) {brand_name}"

            # Resolve every keyword against its persisted purchase snapshot before
            # claim, billing, task creation, or provider execution.
            from db.monitoring_db import (
                get_keywords_for_monitoring,
                get_monitoring_config,
                DEFAULT_MONITORING_PLATFORMS,
            )
            from tools.monitoring.batch_monitor import PlatformAdapter
            _platform_config = get_monitoring_config(
                brand_id=client.get("brand_id"),
                client_id=str(quote_id),
            )
            _configured_platforms = _platform_config.get(
                "default_platforms", DEFAULT_MONITORING_PLATFORMS
            )
            _monitoring_keywords = get_keywords_for_monitoring(
                quote_id=quote_id,
                exclude_keyword_subscription_owned=True,
            )
            _platform_plan = PlatformAdapter.resolve_keyword_monitoring_plan(
                _monitoring_keywords,
                configured_platforms=_configured_platforms,
            )
            if not _platform_plan["keywords"]:
                logger.warning(
                    "[Scheduler] skip %s: no purchased and collectable monitoring platforms",
                    brand_name,
                )
                continue
            kw_count = len(_platform_plan["keywords"])
            client = {
                **client,
                "_monitoring_keywords": _platform_plan["keywords"],
                # Union is summary metadata only. Execution must always consume the
                # per-keyword matrix stored on _monitoring_keywords.
                "_monitoring_platform_summary": _platform_plan["platforms"],
                "_configured_monitoring_platforms": _configured_platforms,
            }

            # [CTO-15.23 2026-05-19 P0 补漏] atomic claim · 防蓝绿+多 scheduler race 重复扣
            # 现象:scheduler.py monitoring_keyword_daily path 已加 claim_subscription_for_today
            #       这条 path(scheduled_monitoring) 同样有蓝绿双 instance race 风险
            # 修法:用 quotes.monitoring_last_run_at 做 quote 级 atomic claim
            #       grace 窗口 = max(1, interval-1) 小时(留 1h 防 race 卡死)
            interval_hours = client.get("monitoring_interval_hours") or 24
            quote_claim_state = None
            try:
                from db.monitoring_db import claim_quote_for_monitoring_with_token
                quote_claim_state = claim_quote_for_monitoring_with_token(
                    quote_id,
                    interval_hours=interval_hours,
                )
                if not quote_claim_state:
                    logger.info(
                        f"[Scheduler] 跳过 {brand_name} (quote#{quote_id}) "
                        f"· {max(1, interval_hours-1)}h 内已被其他 instance race 跑过"
                    )
                    continue
            except Exception as claim_err:
                # [GEO-R2-CAN-033] fail-closed:atomic claim 是蓝绿双 instance 重复扣费的唯一去重闸,
                #   freeze_points 层对同小时 task_ref 无幂等去重;claim 异常若继续走原流程,
                #   两 instance 同周期都会 freeze+commit → 客户被重复扣费。
                #   守卫:claim 获取异常 → 跳过该品牌(本周期不监测),宁可少跑一次也绝不双扣。
                logger.warning(
                    f"[Scheduler] {brand_name} claim 异常 · fail-closed 跳过该品牌(防重复扣费): {claim_err}"
                )
                continue

            # ===== 扣费逻辑 =====
            # [BUG-P2] 单价从 feature_pricing 实时取(不硬编码 38):freeze_points 内 total = cost_points + extra_cost,
            # 原 total_cost=kw*38 与实扣(130 + 38*(kw-1))不一致(10 词实冻 472 vs 文案 380 多扣 24%);
            # 且管理员调 feature_pricing.cost_points 后实扣随之变而硬编码 38 不变 → 漂移。统一以 DB 单价为基。
            from db.wallet_db import get_feature_pricing
            try:
                _mon_price = int(get_feature_pricing("scheduled_monitoring")["cost_points"])
            except Exception as _fb_exc:
                # [WO_240] 🔴 这一处最没人看着:scheduler 里**自动扣费**,没有用户在等回包。
                #   而上面那段注释正是在骂「硬编码 38 会漂移」—— 修它的那一版
                #   自己在 except 里留了个硬编码 130。**同一个理由适用于这一行。**
                from services.fallback_observability import raised as _fb_raised
                _fb_raised(feature="scheduled_monitoring",
                           where="api/scheduler.py:_run_clients_with_billing",
                           key="cost_points", used=130, exc=_fb_exc)
                _mon_price = 130
            total_cost = kw_count * _mon_price
            user_id = None

            try:
                from db.monitoring_db import get_connection
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("""
                    SELECT b.owner_user_id
                    FROM quotes q JOIN brands b ON q.brand_id = b.id
                    WHERE q.id = %s
                """, (quote_id,))
                row = cur.fetchone()
                conn.close()

                if not row or not row["owner_user_id"]:
                    logger.warning(f"[Scheduler] {brand_name} 无法找到品牌所有者，跳过")
                    from db.monitoring_db import release_quote_monitoring_claim
                    release_quote_monitoring_claim(
                        quote_id,
                        quote_claim_state["claim_token"],
                        quote_claim_state["previous_value"],
                    )
                    continue

                user_id = row["owner_user_id"]

                # 2026-04-18 冻结模式：启动前冻结，成功 commit，失败 release
                # task_ref 按小时唯一：支持 6/12 小时等一天多次监测，同时避免同小时重复冻结。
                task_ref = f"sched_mon_{quote_id}_{datetime.now(BEIJING_TZ).strftime('%Y%m%d%H')}"
                from middleware.billing import freeze_points
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        freeze_points(
                            user_id, "scheduled_monitoring",
                            task_ref=task_ref,
                            brand_id=client.get("brand_id") if isinstance(client, dict) else None,
                            extra_cost=total_cost - _mon_price,  # [BUG-P2] 以 DB 单价为基(freeze 内再加 1 份 cost_points)
                            reason=f"定时监测 {brand_name}",
                        )
                    )
                    logger.info(f"[Scheduler] {brand_name} 冻结成功: {total_cost} 积分, user={user_id}, task_ref={task_ref}")
                except Exception as billing_err:
                    from db.monitoring_db import pause_monitoring
                    pause_monitoring(quote_id, f"积分不足: 需要{total_cost}积分")
                    from db.monitoring_db import release_quote_monitoring_claim
                    release_quote_monitoring_claim(
                        quote_id,
                        quote_claim_state["claim_token"],
                        quote_claim_state["previous_value"],
                    )
                    logger.warning(f"[Scheduler] {brand_name} 积分不足({total_cost}积分)，已自动暂停监测")

                    continue
                finally:
                    loop.close()

            except Exception as e:
                logger.error(f"[Scheduler] {brand_name} 冻结流程异常: {e}")
                try:
                    from db.monitoring_db import release_quote_monitoring_claim
                    release_quote_monitoring_claim(
                        quote_id,
                        quote_claim_state["claim_token"],
                        quote_claim_state["previous_value"],
                    )
                except Exception as release_err:
                    logger.warning("[Scheduler] quote#%s claim 归还失败: %s", quote_id, release_err)
                continue

            # ===== 执行监测 =====
            freeze_released = False
            result_task_id = None
            def _capture_scheduled_task_id(created_task_id: int) -> None:
                nonlocal result_task_id
                result_task_id = int(created_task_id)
            try:
                client = {
                    **client,
                    "owner_user_id": user_id,
                    "_task_created_callback": _capture_scheduled_task_id,
                    "_settlement_reference": task_ref,
                }
                result = _run_single_brand(client)
                result_task_id = result.get("task_id")
                if not result.get("delivery_started"):
                    if user_id:
                        from middleware.billing import release_freeze
                        loop_r = asyncio.new_event_loop()
                        try:
                            loop_r.run_until_complete(
                                release_freeze(
                                    task_ref=task_ref,
                                    reason=f"定时监测未发送平台请求 {brand_name}",
                                    user_id=user_id,
                                )
                            )
                            freeze_released = True
                        finally:
                            loop_r.close()
                    if result_task_id:
                        from db.monitoring_db import set_monitoring_task_fulfillment_state
                        set_monitoring_task_fulfillment_state(result_task_id, "released")
                    raise MonitoringBatchUnavailable("平台请求发送前执行失败，冻结已释放")
                _running_status["progress"][brand_name] = result
                total_results.append(result)
                logger.info(
                    f"[Scheduler] [{i+1}/{len(clients)}] {brand_name} 完成: "
                    f"{result.get('detected', 0)}/{result.get('total', 0)} 检出"
                )
                # 监测成功 → 结算冻结为正式消费
                if user_id:
                    try:
                        from middleware.billing import commit_freeze
                        loop_c = asyncio.new_event_loop()
                        loop_c.run_until_complete(
                            commit_freeze(task_ref=task_ref, reason=f"定时监测完成 {brand_name}", user_id=user_id)
                        )
                        loop_c.close()
                        if result_task_id:
                            from db.monitoring_db import set_monitoring_task_fulfillment_state
                            set_monitoring_task_fulfillment_state(result_task_id, "covered")
                    except Exception as commit_err:
                        logger.error(f"[Scheduler] {brand_name} commit 冻结失败: {commit_err}")
                        # commit 的远端结果不确定时禁止 release，也禁止自动免费重试。
                        if result_task_id:
                            try:
                                from db.monitoring_db import set_monitoring_task_fulfillment_state
                                set_monitoring_task_fulfillment_state(
                                    result_task_id, "coverage_unknown"
                                )
                            except Exception as state_err:
                                logger.error(
                                    "[Scheduler] task#%s coverage_unknown 写入失败: %s",
                                    result_task_id, state_err,
                                )
                try:
                    from db.monitoring_db import mark_quote_monitoring_last_run
                    mark_quote_monitoring_last_run(quote_id)
                except Exception as mark_err:
                    logger.warning(f"[Scheduler] {brand_name} 更新最近监测时间失败: {mark_err}")
            except BaseException as e:
                logger.error(f"[Scheduler] {brand_name} 监测失败: {e}")
                _running_status["progress"][brand_name] = {"error": str(e)}
                if isinstance(e, MonitoringBatchUnavailable) or freeze_released:
                    try:
                        if freeze_released and result_task_id:
                            from db.monitoring_db import set_monitoring_task_fulfillment_state
                            set_monitoring_task_fulfillment_state(result_task_id, "released")
                        from db.monitoring_db import release_quote_monitoring_claim
                        release_quote_monitoring_claim(
                            quote_id,
                            quote_claim_state["claim_token"],
                            quote_claim_state["previous_value"],
                        )
                    except Exception as release_err:
                        logger.warning("[Scheduler] quote#%s identity claim 归还失败: %s", quote_id, release_err)
                # A task callback binds the durable plan before any provider can
                # run. Once its dispatch fence is crossed, failure/cancellation
                # commits the original fulfillment instead of refunding delivered work.
                if user_id and not freeze_released:
                    delivery_started = False
                    if result_task_id:
                        try:
                            from db.monitoring_db import get_connection as _get_delivery_connection
                            delivery_conn = _get_delivery_connection()
                            try:
                                delivery_cur = delivery_conn.cursor()
                                delivery_cur.execute(
                                    """
                                    SELECT EXISTS (
                                        SELECT 1 FROM public.monitoring_run_cells
                                         WHERE task_id=%s AND provider_dispatched_at IS NOT NULL
                                    ) AS started
                                    """,
                                    (int(result_task_id),),
                                )
                                delivery_row = delivery_cur.fetchone()
                                delivery_started = bool(delivery_row and delivery_row["started"])
                            finally:
                                delivery_conn.close()
                        except Exception as delivery_error:
                            delivery_started = True
                            logger.error(
                                "[Scheduler] task#%s dispatch fence read failed: %s",
                                result_task_id, delivery_error,
                            )
                    try:
                        loop2 = asyncio.new_event_loop()
                        try:
                            if delivery_started:
                                from middleware.billing import commit_freeze
                                loop2.run_until_complete(
                                    commit_freeze(
                                        task_ref=task_ref,
                                        reason=f"定时监测已发送平台请求 {brand_name}",
                                        user_id=user_id,
                                    )
                                )
                            else:
                                from middleware.billing import release_freeze
                                loop2.run_until_complete(
                                    release_freeze(
                                        task_ref=task_ref,
                                        reason=f"监测发送前失败 {brand_name}",
                                        user_id=user_id,
                                    )
                                )
                        finally:
                            loop2.close()
                        if result_task_id:
                            from db.monitoring_db import set_monitoring_task_fulfillment_state
                            set_monitoring_task_fulfillment_state(
                                result_task_id, "covered" if delivery_started else "released"
                            )
                    except Exception as settlement_error:
                        logger.error(f"[Scheduler] {brand_name} 结算状态未知: {settlement_error}")
                        if result_task_id:
                            try:
                                from db.monitoring_db import set_monitoring_task_fulfillment_state
                                set_monitoring_task_fulfillment_state(
                                    result_task_id, "coverage_unknown"
                                )
                            except Exception:
                                pass
                if isinstance(e, asyncio.CancelledError):
                    raise

        # 汇总
        elapsed = round((datetime.now() - start_time).total_seconds(), 1)
        summary = {
            "brands_count": len(clients),
            "total_tests": sum(r.get("total", 0) for r in total_results),
            "total_detected": sum(r.get("detected", 0) for r in total_results),
            "elapsed_seconds": elapsed,
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _running_status["last_run"] = summary["completed_at"]
        _running_status["last_result"] = summary

        logger.info(
            f"[Scheduler] ========== 监测完成 ==========\n"
            f"  品牌: {summary['brands_count']}, 测试: {summary['total_tests']}, "
            f"检出: {summary['total_detected']}, 耗时: {elapsed}s"
        )

    except Exception as e:
        logger.error(f"[Scheduler] 监测异常: {e}")
        _running_status["last_result"] = {"error": str(e)}
    finally:
        _running_status["is_running"] = False
        _running_status["abort_requested"] = False
        _running_status["current_brand"] = None


def _run_single_brand(client: Dict) -> Dict:
    """
    执行单个品牌的监测（在独立事件循环中跑 async 代码）
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_async_run_brand(client))
    finally:
        loop.close()


async def _async_run_brand(client: Dict) -> Dict:
    """异步执行单品牌监测"""
    import time
    from db.monitoring_db import (
        get_connection,
        get_keywords_for_monitoring,
        get_monitoring_config,
        DEFAULT_MONITORING_PLATFORMS,
        claim_monitoring_run_cell,
        create_monitoring_run_cells,
        create_monitoring_task,
        finish_monitoring_cell_error,
        mark_monitoring_cell_dispatched,
        save_monitoring_result,
        set_monitoring_task_fulfillment_state,
        update_task_status,
        save_trend_stat,
        calculate_rate_change,
    )
    from tools.monitoring.batch_monitor import PlatformAdapter, build_question, resolve_monitoring_query

    quote_id = client["quote_id"]
    brand_id = client["brand_id"]

    # Prefer the pre-claim purchase-aware plan. Direct test callers are resolved
    # here with the same persisted per-keyword authority before task creation.
    keywords = client.get("_monitoring_keywords")
    if keywords is None:
        keywords = get_keywords_for_monitoring(
            quote_id=quote_id,
            exclude_keyword_subscription_owned=True,
        )
    if not keywords:
        return {"total": 0, "detected": 0, "brand": client["brand_name"]}

    if not all(kw.get("_eligible_monitoring_platforms") for kw in keywords):
        config = get_monitoring_config(brand_id=brand_id, client_id=str(quote_id))
        platform_plan = PlatformAdapter.resolve_keyword_monitoring_plan(
            keywords,
            configured_platforms=config.get(
                "default_platforms", DEFAULT_MONITORING_PLATFORMS
            ),
        )
        keywords = platform_plan["keywords"]
    if not keywords or not any(
        kw.get("_eligible_monitoring_platforms") for kw in keywords
    ):
        raise MonitoringBatchUnavailable("当前配置没有可用的监测引擎")
    platform_summary = []
    for kw in keywords:
        for platform in kw["_eligible_monitoring_platforms"]:
            if platform not in platform_summary:
                platform_summary.append(platform)

    # 创建任务（自动监测标记为 scheduled，自动同步趋势）
    task_id = create_monitoring_task(
        client_id=str(brand_id),
        brand_id=brand_id,
        keyword_ids=[k["id"] for k in keywords],
        concurrency=20,
        trigger_type="scheduled",
        planned_test_count=sum(
            len(keyword["_eligible_monitoring_platforms"])
            for keyword in keywords
        ),
        planned_platform_count=len(platform_summary),
    )
    task_created_callback = client.get("_task_created_callback")
    if task_created_callback is not None:
        task_created_callback(int(task_id))
    import uuid as _uuid
    durable_cells = create_monitoring_run_cells(
        task_id=task_id,
        brand_id=brand_id,
        keywords=keywords,
        search_mode="enhanced",
        fulfillment_credential=str(_uuid.uuid4()),
        fulfillment_state="reserved",
        retry_coverage={
            "policy_version": "monitoring-retry-v1",
            "coverage": "included",
            "max_attempts": 1,
        },
        settlement_reference=client.get("_settlement_reference"),
    )
    cell_lookup = {
        (cell["keyword_source"], int(cell["keyword_id"]), cell["platform"]): cell
        for cell in durable_cells if cell["is_planned"]
    }

    update_task_status(task_id, "running")

    # 构建任务列表（定时监测默认使用增强模式：直接走 doubao_app，避免前后端口径不一致）
    from tools.monitoring.batch_monitor import MonitoringTask
    # P0.8 (CTO-15.9 Codex bug 4): 定时监测也使用 monitoring_query · fallback build_question
    monitoring_tasks = []
    for kw in keywords:
        question = resolve_monitoring_query(kw)
        for platform in kw["_eligible_monitoring_platforms"]:
            cell = cell_lookup[(kw.get("source") or "unknown", int(kw["id"]), platform)]
            monitoring_tasks.append(MonitoringTask(
                keyword_id=kw["id"],
                keyword=kw["keyword"],
                target_brand=kw["target_brand"],
                platform=platform,
                question=question,
                search_mode="enhanced",
                keyword_source=kw.get("source") or "unknown",
                cell_id=int(cell["id"]),
            ))

    # 限速并行：控制并发数，避免 API 限流和数据库连接池耗尽
    # 每个并发任务占 1 个 DB 连接（save_monitoring_result），池大小 20，留 10 给 Web 请求
    from db.monitoring_db import PLATFORM_WEIGHTS
    semaphore = asyncio.Semaphore(5)
    total_count = len(monitoring_tasks)

    async def run_one(task: MonitoringTask):
        claim = None
        dispatched = False
        result = None
        try:
            async with semaphore:
                claim = claim_monitoring_run_cell(
                    cell_id=int(task.cell_id),
                    task_id=task_id,
                    brand_id=brand_id,
                    allowed_state="queued",
                    lease_seconds=300,
                )
                task.cell_claim_token = str(claim["claim_token"])
                # Dispatch fence is immediately adjacent to the provider call. Once
                # crossed, this brand-level freeze must never be released merely
                # because the provider reply or the DB response is lost.
                mark_monitoring_cell_dispatched(
                    cell_id=int(task.cell_id),
                    claim_token=task.cell_claim_token,
                )
                task.provider_dispatched = True
                dispatched = True
                result = await PlatformAdapter.query(
                    platform=task.platform,
                    question=task.question,
                    target_brand=task.target_brand,
                    search_mode=task.search_mode,
                    caller="monitoring",
                    brand_id=brand_id,
                    quote_id=quote_id,
                    user_id=client.get("owner_user_id"),
                    monitoring_task_id=task_id,
                    keyword_id=task.keyword_id,
                    keyword=task.keyword,
                )
        except BaseException as exc:
            if claim:
                finish_monitoring_cell_error(
                    cell_id=int(task.cell_id),
                    claim_token=str(claim["claim_token"]),
                    state="pending_provider_confirmation" if dispatched else "failed",
                    error_code=(
                        "provider_outcome_unknown" if dispatched
                        else "worker_lost_before_dispatch"
                    ),
                    error_message=str(exc)[:2000] or "监测执行中断",
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {
                "status": "error",
                "error": "平台执行结果未确认" if dispatched else "平台请求发送前执行失败",
                "error_code": (
                    "provider_outcome_unknown" if dispatched
                    else "worker_lost_before_dispatch"
                ),
                "keyword_id": task.keyword_id,
                "keyword": task.keyword,
                "platform": task.platform,
                "keyword_source": task.keyword_source,
                "cell_id": task.cell_id,
                "provider_dispatched": dispatched,
            }

        result.update({
            "keyword_id": task.keyword_id,
            "keyword": task.keyword,
            "platform": task.platform,
            "keyword_source": task.keyword_source,
            "keyword_type": task.keyword_type,
            "keyword_resolver_status": task.keyword_resolver_status,
            "sent_question_snapshot": task.question,
            "target_brand": task.target_brand,
            "cell_id": task.cell_id,
            "provider_dispatched": True,
        })
        if result.get("status") == "error":
            error_code = str(result.get("error_code") or "platform_unavailable")
            cell_state = (
                "pending_provider_confirmation"
                if error_code in {"platform_unavailable", "provider_outcome_unknown"}
                else ("unavailable" if error_code == "platform_not_supported" else "failed")
            )
            finish_monitoring_cell_error(
                cell_id=int(task.cell_id),
                claim_token=task.cell_claim_token,
                state=cell_state,
                error_code=error_code,
                error_message=str(result.get("error") or "该平台本次未返回可用结果"),
            )
            result["cell_state"] = cell_state
            return result

        try:
            result["result_id"] = save_monitoring_result(
                task_id=task_id,
                keyword_id=int(task.keyword_id),
                keyword=task.keyword,
                platform=task.platform,
                is_detected=bool(result.get("is_detected")),
                mention_type=result.get("mention_type") or "none",
                response_snippet=result.get("response_snippet") or "",
                full_response=result.get("full_response") or "",
                search_citations=result.get("search_citations") or "",
                competitors_mentioned=result.get("competitors_mentioned") or [],
                lineage=result,
                identity_brand_id=brand_id,
                identity_candidates=result.get("identity_candidates") or [],
                identity_evidence_snippet=result.get("identity_evidence_snippet") or "",
                identity_review_state=(
                    "pending" if result.get("status") == "pending_identity" else "not_required"
                ),
                cell_id=int(task.cell_id),
                cell_claim_token=task.cell_claim_token,
            )
        except Exception as exc:
            finish_monitoring_cell_error(
                cell_id=int(task.cell_id),
                claim_token=task.cell_claim_token,
                state="pending_provider_confirmation",
                error_code="provider_outcome_unknown",
                error_message=str(exc)[:2000],
            )
            result.update({
                "status": "error",
                "cell_state": "pending_provider_confirmation",
                "error_code": "provider_outcome_unknown",
                "error": "平台已调用但结果持久化未确认",
            })
        return result

    # 执行所有
    raw_results = await asyncio.gather(*[run_one(t) for t in monitoring_tasks], return_exceptions=True)

    exc_results = [r for r in raw_results if isinstance(r, BaseException)]
    if exc_results:
        update_task_status(task_id, "failed")
        raise exc_results[0]

    # Every provider outcome was settled against its own durable cell above.
    results = list(raw_results)
    detected_count = 0
    valid_count = 0
    error_count = sum(1 for result in results if result.get("status") == "error")
    weighted_detected = 0.0
    weighted_total = 0.0
    for result in results:
        if result.get("status") in {"error", "pending_identity"}:
            continue
        is_detected = result.get("is_detected", False)
        w = PLATFORM_WEIGHTS.get(result["platform"], 0.25)
        valid_count += 1
        weighted_total += w
        if is_detected:
            detected_count += 1
            weighted_detected += w

    # 更新任务状态（加权出现率）
    detection_rate = round(weighted_detected / weighted_total * 100, 1) if weighted_total > 0 else 0
    summary = {
        "attempted_tests": total_count,
        "total_tests": valid_count,
        "error_count": error_count,
        "pending_identity_count": sum(
            1 for result in results if result.get("status") == "pending_identity"
        ),
        "detected_count": detected_count,
        "detection_rate": detection_rate,
        "completed_at": datetime.now().isoformat(),
    }
    update_task_status(task_id, "completed", valid_count, summary)

    # [§7 监测蒸馏补桥] flag monitoring_scheduled_distillation 默认关 → 定时监测行为与改前逐字节一致。
    #   修前:manual 路径(monitoring_api)完成后 fire-and-forget 触发蒸馏,但 scheduled 主路径全程无蒸馏。
    #   开启:定时监测完成后 fire-and-forget 触发蒸馏(与手动一致),失败 fail-soft log,
    #   绝不影响监测任务已 completed 状态,不改扣费(平台吸收蒸馏成本)。
    try:
        from writing.feature_switches import is_feature_enabled
        if brand_id and is_feature_enabled("monitoring_scheduled_distillation"):
            from tools.distillation.trigger import trigger_distillation
            asyncio.create_task(trigger_distillation(task_id, brand_id))
    except Exception as _distill_err:
        logger.warning(f"[Scheduler] scheduled distillation 触发失败(不影响监测): {_distill_err}")

    # 写入趋势统计
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        conn2 = get_connection()
        cur2 = conn2.cursor()
        from services.monitoring_identity_review import aggregate_eligible_sql
        cur2.execute(f"""
            SELECT COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id, COUNT(*) as tests,
                   SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) as detected
            FROM monitoring_results
            WHERE task_id = %s AND {aggregate_eligible_sql()}
            GROUP BY COALESCE(confirmed_keyword_id, keyword_id)
        """, (task_id,))
        for row in cur2.fetchall():
            kid = row["keyword_id"]
            tests = row["tests"]
            det = row["detected"]
            if tests > 0:
                rate = round(det / tests * 100, 1)
                source = "confirmed"
                for kw in keywords:
                    if kw["id"] == kid:
                        source = kw.get("source", "confirmed")
                        break
                # [audit #654 2026-06-10] 修参数顺序错位:原 (kid, source, "daily", today_str) 把 current_rate
                #   位塞了 "daily"、period_type 位塞了日期串 → 查不到行恒返 0(rate_change 永远 0)+ 未来若
                #   period_type 含日期值会 str-float TypeError 砸掉趋势写入段。对齐根 scheduler:473 与函数签名。
                prev_rate = calculate_rate_change(kid, source, rate, "daily")
                save_trend_stat(kid, source, "daily", today_str, tests, det, prev_rate)
        conn2.close()
        # 标记趋势已同步
        conn3 = get_connection()
        cur3 = conn3.cursor()
        cur3.execute("UPDATE monitoring_tasks SET trend_synced = 1 WHERE id = %s", (task_id,))
        conn3.commit()
        conn3.close()
    except Exception as e:
        logger.error(f"[Scheduler] 趋势统计写入失败: {e}")

    # CTO-15.23 2026-05-25 · 老板报漏斗 monitor 0 · 补 scheduler 定时监测主路径埋点
    # · prod 88411 行 monitoring_results 主要源自 _async_run_brand(trigger_type='scheduled')
    # · 此前 monitoring_api.py SSE / batch_monitor.run_client_monitoring / _m1a_auto_monitor_after_publish 都有 stage_log
    # · 唯独 _async_run_brand 这个 daily scheduled cron 漏埋点 → funnel monitor=0
    # · funnel 按 (brand_id, stage_name) 去重 · 多次跑同 brand 仍算 1 次(90 天窗口内)
    try:
        from db.pipeline_stage_log_db import log_stage_event
        log_stage_event(
            brand_id=brand_id,
            stage_name="monitor",
            event="complete",
            meta={
                "source": "scheduler_daily",
                "task_id": task_id,
                "trigger_type": "scheduled",
                "keyword_count": len(keywords),
                "platform_count": len(platform_summary),
                "total_tests": valid_count,
                "detected_count": detected_count,
                "detection_rate": detection_rate,
            },
            actor_user_id=client.get("owner_user_id"),
        )
    except Exception as _le:
        logger.warning(f"[Scheduler monitor stage_log] 失败(非阻塞) brand={brand_id}: {_le}")

    return {
        "brand": client["brand_name"],
        "attempted": total_count,
        "total": valid_count,
        "errors": error_count,
        "detected": detected_count,
        "rate": detection_rate,
        "task_id": task_id,
        "delivery_started": any(
            bool(result.get("provider_dispatched")) for result in results
            if isinstance(result, dict)
        ),
    }


def _notify_rotation_blocked_by_service_period(cur) -> int:
    """[服务期 SSOT 2026-08-06 §1.5] 因服务期到点而被摘出自动监测轮换 → 内部必须看得见。

    只发给**代理**(RecipientKind.AGENT),不通知客户端 —— 静默纪律只约束客户面,
    内部该看见的必须看见。去重键里带上到期日:
      terminal_state = `rotation_blocked:<service_end_date>`
    → 同一张单同一个到期日**只提醒一次**(不刷屏);续费后到期日变了会再提醒一次。

    返回本次真正新入队的条数(ON CONFLICT DO NOTHING,重复的不计)。
    """
    try:
        from db.monitoring_db import get_service_period_blocked_clients
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import enqueue_notification_event
    except Exception as _imp_err:  # pragma: no cover - 依赖缺失不该拖垮整个 job
        logger.warning(f"[ServiceCheck] 到期不静默模块导入失败(不影响其余检查): {_imp_err}")
        return 0

    try:
        blocked = get_service_period_blocked_clients()
    except Exception as _q_err:
        logger.error(f"[ServiceCheck] 查被摘出轮换的客户失败: {_q_err}")
        return 0

    enqueued = 0
    for row in blocked:
        end = row.get("service_end_date")
        overdue = row.get("overdue_days")
        # 先落一条 warning —— 就算通知链路挂了,运维日志里也留得下痕迹
        logger.warning(
            "[ServiceCheck] 自动监测轮换已停(服务期): quote=%s brand=%s 到期日=%s 已过 %s 天 · 原因=%s",
            row.get("quote_id"), row.get("brand_name"), end, overdue, row.get("blocked_reason"),
        )
        try:
            cur.execute("SELECT owner_user_id FROM brands WHERE id=%s", (row.get("brand_id"),))
            owner = cur.fetchone()
            if not owner or owner.get("owner_user_id") is None:
                continue
            if end is None:
                _detail = "这个客户还没设服务期,自动监测排不上班"
            else:
                _detail = f"服务期已到({end})· 自动监测已暂停,续费后自动恢复"
            inserted = enqueue_notification_event(
                cur,
                event_type=NotificationEventType.SERVICE_EXPIRING,
                business_id=f"quote:{int(row['quote_id'])}",
                terminal_state=f"rotation_blocked:{end or 'not_set'}",
                recipient_user_id=int(owner["owner_user_id"]),
                recipient_kind=RecipientKind.AGENT,
                facts={
                    "business_no": f"QUOTE-{int(row['quote_id'])}",
                    "status": "服务期已到 · 待续费",
                    "occurred_at": datetime.now().isoformat(timespec="seconds"),
                    "summary": f"{_detail}。去客户报价页面续费即可继续监测。",
                },
            )
            enqueued += int(inserted is not None)
        except Exception as _n_err:
            logger.warning(
                f"[ServiceCheck] 续费提醒入队失败 quote={row.get('quote_id')}: {_n_err}"
            )
    return enqueued


def _check_service_periods():
    """
    每日凌晨检查所有客户的日历服务期：
    1. 日历到期只作为续费提醒，不自动标记 expired，不停止门户 token。
    2. 7 天内到期 → 标记 expiring，发通知提醒续费。

    2026-06-23 续费口径:
    服务完成 = 累计达标天数 compliant_days >= service_days。
    日历到期但达标天数未满的客户必须继续可监测。

    [服务期 SSOT 2026-08-06 §1.5] 到期不静默:
      日历到期本身不停服(上面那条口径不变),但**自动监测轮换资格闸确实卡的是日历**
      (`get_monitoring_enabled_clients` 要求 service_end_date >= CURRENT_DATE)。
      于是出现了 Owner 报的那一幕:界面显示服务充足、"还需达标 N 天",
      而轮换早在到期那天就静默停了 —— 谁都没收到过一个字。
      这里补上:凡"只差服务期这一条"就能进轮换的客户,给**代理**发一条内部通知
      (客户端不通知 · 静默纪律),并落 warning 日志。
    🔴 这里刻意**不改**轮换闸本身的口径 —— 那一条挂在 OWNER_DECISION_BRIEF 第 3 项,
      Owner 拍了再说(工单「边界」明写)。本条只保证它不再是静默的。
    """
    logger.info("[ServiceCheck] 开始每日服务期检查")
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()

            # 1. 日历到期只记日志/通知，不再自动改 expired，不停门户 token。
            cur.execute("""
                SELECT id, brand_name, service_end_date
                FROM quotes
                WHERE service_end_date IS NOT NULL
                  AND service_end_date < CURRENT_DATE
                  AND COALESCE(service_status, 'active') NOT IN ('cancelled', 'inactive')
            """)
            expired = cur.fetchall()
            for row in expired:
                logger.info(
                    f"[ServiceCheck] 日历已到期但不自动停服: {row['brand_name']} "
                    f"(到期日: {row['service_end_date']} · 继续按达标天数履约)"
                )

            # 1.5 [§1.5 到期不静默] 因服务期被摘出轮换池的客户 → 内部告警 + 给代理发续费提醒
            rotation_blocked = _notify_rotation_blocked_by_service_period(cur)

            # 2. 标记即将到期（7天内）
            cur.execute("""
                UPDATE quotes
                SET service_status = 'expiring'
                WHERE service_end_date IS NOT NULL
                  AND service_end_date >= CURRENT_DATE
                  AND service_end_date <= CURRENT_DATE + INTERVAL '7 days'
                  AND COALESCE(service_status, 'active') = 'active'
                RETURNING id, brand_id, brand_name, service_end_date
            """)
            expiring = cur.fetchall()

            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event
            for row in expiring:
                cur.execute("SELECT owner_user_id FROM brands WHERE id=%s", (row["brand_id"],))
                owner = cur.fetchone()
                if not owner or owner.get("owner_user_id") is None:
                    continue
                days_left = (row["service_end_date"] - datetime.now().date()).days
                enqueue_notification_event(
                    cur,
                    event_type=NotificationEventType.SERVICE_EXPIRING,
                    business_id=f"quote:{int(row['id'])}",
                    terminal_state="expiring",
                    recipient_user_id=int(owner["owner_user_id"]),
                    recipient_kind=RecipientKind.AGENT,
                    facts={
                        "business_no": f"QUOTE-{int(row['id'])}",
                        "status": "服务即将到期",
                        "occurred_at": datetime.now().isoformat(timespec="seconds"),
                        "summary": f"预计 {days_left} 天后到期，请在客户报价页面准备续费方案。",
                    },
                )

            conn.commit()

            conn.close()
            logger.info(
                f"[ServiceCheck] 检查完成: {len(expired)} 个日历到期提醒, {len(expiring)} 个即将到期, "
                f"{rotation_blocked} 个因服务期被摘出自动监测轮换(已发内部续费提醒)"
            )
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"[ServiceCheck] 检查失败: {e}")


def _run_compliance_check():
    """每日达标判定：检查所有付费客户关键词是否达到套餐目标检出率

    [CTO-15.23 2026-05-08 监测归档机制] 判定后自动归档:
      - 达标完成(compliance_progress >= 100% AND service_end < NOW()) → 'compliance_complete'
      - 服务期到(service_end < NOW() · 不论是否达标) → 'service_expired'
    """
    logger.info("[Compliance] 开始每日达标判定")
    try:
        from db.monitoring_db import run_daily_compliance_check
        result = run_daily_compliance_check()
        logger.info(f"[Compliance] 判定完成: {result}")
    except Exception as e:
        logger.error(f"[Compliance] 判定失败: {e}")

    # [CTO-15.23 2026-05-29 D6 哨兵] 真客户被"服务锚缺失"误隐藏 → admin alert(防 paid_at/service_start_date 双空 P0 再发)
    try:
        from db.monitoring_db import check_hidden_real_customers
        _hidden = check_hidden_real_customers()
        if _hidden:
            _ids = "、".join(f"quote {h['quote_id']}({h.get('brand_name', '')})" for h in _hidden[:10])
            try:
                import hashlib
                from datetime import datetime, timezone
                from services.notification_events import NotificationEventType
                from services.notification_outbox import enqueue_admin_notification_events_durable

                anomaly_identity = ",".join(str(h["quote_id"]) for h in _hidden)
                anomaly_digest = hashlib.sha256(anomaly_identity.encode("utf-8")).hexdigest()[:12]
                enqueue_admin_notification_events_durable(
                    event_type=NotificationEventType.SYSTEM_JOB_FAILED,
                    business_id=f"monitoring-anchor:{anomaly_digest}",
                    terminal_state="detected",
                    facts={
                        "business_no": f"MONITOR-ANCHOR-{anomaly_digest.upper()}",
                        "status": "监测客户服务锚异常",
                        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "summary": f"发现 {len(_hidden)} 个客户需要在运维页面核验。",
                    },
                )
            except Exception as _ne:
                logger.exception("[Sentinel] admin outbox 写入失败: %s", _ne)
            logger.warning(f"[Sentinel] 真客户服务锚缺失 {len(_hidden)} 个: {_ids}")
    except Exception as e:
        logger.warning(f"[Sentinel] 真客户锚哨兵失败(非阻塞): {e}")

    # 自动归档(防 monitoring 跑已完成/已到期的词)
    try:
        from db.connection import get_connection as _gc
        conn = _gc()
        cur = conn.cursor()
        # 1. [履约口径 2026-06-04] A 方案纯履约 · 取消"自然日历到期就全归档"
        #   旧:service_start_date + service_days < NOW() → 归档(无论是否达标)= 自然日历封顶
        #   新:A 方案不设自然日历封顶 · 服务完成只看 compliant_days >= service_days(下方第 2 段)
        #      过了自然日历但还没达标满 → 继续监测,不在这里归档。
        #   因此本段(service_expired)整体停用 · expired 恒 0 · 保留变量供日志兼容。
        expired = 0

        # 2. 达标完成 + 服务期内 也归档(防止跑无意义监测)
        # [CTO-15.23 2026-05-10 双 bug 修 · 此路径之前从未生效]
        #   老:FROM monitoring_compliance_log WHERE compliance_progress >= 100
        #     bug 1: 表名错位 · 实际表是 keyword_compliance_log (跟续费 server.py:5850 同源 typo)
        #     bug 2: compliance_progress 不是 keyword_compliance_log 的字段
        #            那是 Python 层 (compliant_days / service_days) 计算 · SQL 表里只有 is_compliant
        #     双 bug 触发 try/except 吞掉 · "达标完成提前归档"事实上从未跑过 ·
        #            老板原话"真完成自动取消监测"中"达标完成"路径失效
        #   修:用对表 + GROUP BY 累积 is_compliant=TRUE 天数 · HAVING >= q.service_days = 100%
        #
        # 🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-5] `keyword_source = 'confirmed'` 这一条
        #    **刻意保留**,它不是"手动词被漏掉了",而是 K3 的必然结果 —— 执行方已复核确认:
        #
        #      归档判据是 `compliant_days >= q.service_days`,即"服务期跑满了就停"。
        #      而 Owner 2026-08-16 定的 K3 是「手动词的运行不受报价单状态约束」:
        #      它花的是代理钱包里的算力、由代理自己开关决定,**没有服务期承诺**,
        #      `q.service_days` 对它根本没有意义(取数的 extra 臂同样不套这个条件)。
        #      给手动词套上"达标满就自动归档",等于用一个它从不承诺的期限去停掉代理正在付费的监测。
        #    ⇒ 手动词**不参与达标满自动归档**,跑到代理手动关、或余额不足暂停为止。
        #
        #    另两点已核实,免得下一个人以为这里还有别的漏:
        #      (1) keyword_compliance_log.keyword_source 生产实测只有 'confirmed'(28,144 行)
        #          与 'extra'(75 行)两个值,**没有** 'contract' —— 所以这里不像
        #          api/m3_api.py 那三处那样存在"漏掉 contract 拼写"的问题(那三处本单已修)。
        #      (2) 手动词不会因为本条 SQL 被漏掉**别的**处理:外层 UPDATE 的对象是
        #          confirmed_keywords,手动词的归档走 server.py 的归档端点(src=="extra" 分支),
        #          本单 P0-3 已让那条路径联动取消 extra 订阅。
        try:
            cur.execute("""
                UPDATE confirmed_keywords ck
                   SET monitoring_status = 'archived',
                       archived_at = NOW(),
                       archive_reason = 'compliance_complete'
                 WHERE ck.id IN (
                    SELECT kcl.keyword_id
                      FROM keyword_compliance_log kcl
                      JOIN quotes q ON kcl.quote_id = q.id
                     WHERE kcl.keyword_source = 'confirmed'
                       AND q.service_days IS NOT NULL
                       -- 达标天数下界用服务锚 · 防污染/旧 keyword_compliance_log 的假 is_compliant 行虚增
                       -- 锚口径对齐全链:COALESCE(service_start_date, paid_at)
                       AND kcl.check_date >= COALESCE(q.service_start_date, q.paid_at::date)
                       -- [履约口径 2026-06-04] A 方案纯履约:去掉自然日历上界(不设日历封顶)
                       --   过了自然日历窗口的达标天数仍计入完成判定,直到 compliant_days >= service_days。
                       --   只保留下界(服务锚)防脏行;无上界。
                     GROUP BY kcl.keyword_id, kcl.keyword_source, q.id, q.service_days
                    HAVING COUNT(*) FILTER (WHERE kcl.is_compliant = TRUE) >= q.service_days
                 )
                   AND ck.monitoring_status = 'active'
                   AND ck.is_monitored = TRUE
            """)
            completed = cur.rowcount
        except Exception as _comp_e:
            logger.warning(f"[Compliance] 达标完成提前归档 SQL 失败 (跳过): {_comp_e}")
            completed = 0  # 表/字段不存在 · 兼容

        conn.commit()
        conn.close()
        if expired + completed > 0:
            logger.info(f"[Compliance] 自动归档 service_expired={expired} compliance_complete={completed}")
    except Exception as e:
        logger.warning(f"[Compliance] 自动归档失败(不影响主流程): {e}")


@sched_claim("daily_commission_settle", 86400)  # [WORKERS=4] tick 级 claim(叠 §3 结算 CAS · 双层防双结算)
def _run_commission_settlement():
    """v3.2: T+3 佣金结算（每天凌晨 2 点执行）

    把到期的 pending_commissions 转为 settled，发 paid_points 到代理钱包
    检测退款订单，自动取消对应佣金
    """
    logger.info("[Commission Settle] 开始 T+3 佣金结算")
    try:
        from api.referral_api import settle_due_commissions
        result = settle_due_commissions(dry_run=False)
        logger.info(f"[Commission Settle] 完成: {result}")
    except Exception as e:
        logger.error(f"[Commission Settle] 失败: {e}")


def _social_agent_mark_stale_plans():
    """P0-5 fix · 每天 03:00 CST 把超 24h 未推进的 active plan 转 stale.

    修前 mark_stale_plans 函数定义但 0 scheduler 触发 · prod active plan
    永远不会自动 stale · 用户回来时 LLM 永远 "继续上次" 即使是 1 个月前的。
    """
    logger.info("[SocialAgent Stale] 开始标记 stale plans(>24h 未推进)")
    try:
        from db.social_agent_plans import mark_stale_plans
        count = mark_stale_plans(stale_after_hours=24)
        logger.info(f"[SocialAgent Stale] 完成 · 标记 {count} 个 plan stale")
    except Exception as e:
        logger.error(f"[SocialAgent Stale] 失败: {e}")


def _social_agent_consolidate_memory():
    """P0-5 fix · 每天 03:30 CST 跑 profile_memory_events consolidation(Memory v3).

    合并相似 events(text 相似度 > 0.85)· 保留高 importance · 老旧低分 archive 不删。
    遍历所有 active profile · 逐个调 consolidate_profile_memory(profile_id, dry_run=False)。
    修前 consolidate_profile_memory.py 存在但 0 scheduler 触发 · Memory v3 整套白做。
    """
    logger.info("[SocialAgent Consolidate] 开始 Memory v3 consolidation(遍历 active profile)")
    try:
        from scripts.cron.consolidate_profile_memory import consolidate_profile_memory
        from db.connection import get_db

        # 拿所有近 90 天活跃过的 profile_id(有 memory event 写入)
        profile_ids: list[str] = []
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT profile_id
                FROM profile_memory_events
                WHERE created_at > NOW() - INTERVAL '90 days'
                  AND profile_id IS NOT NULL
                LIMIT 500
            """)
            for row in (cur.fetchall() or []):
                pid = row[0] if not isinstance(row, dict) else row.get("profile_id")
                if pid:
                    profile_ids.append(str(pid))

        total_merged = 0
        total_failed = 0
        for pid in profile_ids:
            try:
                result = consolidate_profile_memory(pid, dry_run=False, threshold=0.85)
                merged = int(result.get("merged", 0)) if isinstance(result, dict) else 0
                total_merged += merged
            except Exception as exc:
                total_failed += 1
                logger.warning(f"[SocialAgent Consolidate] profile_id={pid} failed: {exc}")
        logger.info(
            f"[SocialAgent Consolidate] 完成 · profiles={len(profile_ids)} · "
            f"merged={total_merged} · failed={total_failed}"
        )
    except Exception as e:
        logger.error(f"[SocialAgent Consolidate] 失败: {e}", exc_info=True)


def _m1a_flow_drop_reminder():
    """M1a A8(CTO-15.9 2026-04-25)· 24/72h 流失提醒扫描

    基于 pipeline_stage_log 判断"某 brand 进 stage X 后 24h / 72h 仍无下一步" ·
    触发 notifications 提醒代理主动跟进

    流失规则(PRD M1a):
      · diagnosis complete 24h 后仍无 quote complete → "方案书还没生成 · 点生成 GEO 方案书"
      · diagnosis complete 72h 后仍无 quote complete → 二次提醒 · 更紧迫
      · quote complete 24h 后仍无 pay complete → "报价已发送 · 去客户那确认付款"
      · quote complete 72h 后仍无 pay complete → 升级催收
      · pay complete 24h 后仍无 write complete → "可以开始写文章了 · 去 /writing"

    防重复:用 meta.reminder_sent=true 标记已发提醒 · 避免每 30 分钟重发
    写 notifications 表 · 代理下次登录看到红点
    """
    from db.connection import get_connection
    import json as _json

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. diagnosis 完成 24h/72h 仍无 quote
        for hours, template_key in ((24, "24h"), (72, "72h")):
            cur.execute(
                f"""
                SELECT psl.brand_id, psl.actor_user_id, psl.created_at AS diagnosis_at,
                       psl.meta, b.name AS brand_name
                FROM pipeline_stage_log psl
                LEFT JOIN brands b ON psl.brand_id = b.id
                WHERE psl.stage_name = 'diagnosis'
                  AND psl.event = 'complete'
                  AND psl.created_at <= NOW() - INTERVAL '{hours} hours'
                  AND psl.created_at > NOW() - INTERVAL '{hours * 2} hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM pipeline_stage_log p2
                      WHERE p2.brand_id = psl.brand_id
                        AND p2.stage_name = 'quote'
                        AND p2.event = 'complete'
                        AND p2.created_at >= psl.created_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM pipeline_stage_log p3
                      WHERE p3.brand_id = psl.brand_id
                        AND p3.stage_name = 'diagnosis'
                        AND p3.event = 'complete'
                        AND p3.meta->>'flow_drop_reminder_{hours}h' = 'sent'
                  )
                """
            )
            rows = cur.fetchall() or []
            for row in rows:
                try:
                    _send_flow_drop_notification(
                        row["brand_id"],
                        row.get("actor_user_id"),
                        row["brand_name"],
                        from_stage="diagnosis",
                        hours=hours,
                    )
                    # 标记已发 · 避免重发(写到最近一条 diagnosis log 的 meta)
                    existing_meta = row.get("meta") or {}
                    if isinstance(existing_meta, str):
                        try:
                            existing_meta = _json.loads(existing_meta)
                        except Exception:
                            existing_meta = {}
                    existing_meta[f"flow_drop_reminder_{hours}h"] = "sent"
                    cur.execute(
                        """
                        UPDATE pipeline_stage_log
                        SET meta = %s::jsonb
                        WHERE brand_id = %s AND stage_name = 'diagnosis' AND event = 'complete'
                          AND created_at = %s
                        """,
                        (_json.dumps(existing_meta, ensure_ascii=False), row["brand_id"], row["diagnosis_at"]),
                    )
                except Exception as _e:
                    logger.warning(f"[flow-drop] diagnosis→quote {hours}h 提醒失败 brand={row.get('brand_id')}: {_e}")

        # 2. quote 完成 24h/72h 仍无 pay
        for hours in (24, 72):
            cur.execute(
                f"""
                SELECT psl.brand_id, psl.actor_user_id, psl.created_at AS quote_at,
                       psl.meta, b.name AS brand_name
                FROM pipeline_stage_log psl
                LEFT JOIN brands b ON psl.brand_id = b.id
                WHERE psl.stage_name = 'quote'
                  AND psl.event = 'complete'
                  AND psl.created_at <= NOW() - INTERVAL '{hours} hours'
                  AND psl.created_at > NOW() - INTERVAL '{hours * 2} hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM pipeline_stage_log p2
                      WHERE p2.brand_id = psl.brand_id
                        AND p2.stage_name = 'pay'
                        AND p2.event = 'complete'
                        AND p2.created_at >= psl.created_at
                  )
                  AND (psl.meta->>'flow_drop_reminder_{hours}h') IS DISTINCT FROM 'sent'
                """
            )
            rows = cur.fetchall() or []
            for row in rows:
                try:
                    _send_flow_drop_notification(
                        row["brand_id"],
                        row.get("actor_user_id"),
                        row["brand_name"],
                        from_stage="quote",
                        hours=hours,
                    )
                    existing_meta = row.get("meta") or {}
                    if isinstance(existing_meta, str):
                        try:
                            existing_meta = _json.loads(existing_meta)
                        except Exception:
                            existing_meta = {}
                    existing_meta[f"flow_drop_reminder_{hours}h"] = "sent"
                    cur.execute(
                        """
                        UPDATE pipeline_stage_log
                        SET meta = %s::jsonb
                        WHERE brand_id = %s AND stage_name = 'quote' AND event = 'complete'
                          AND created_at = %s
                        """,
                        (_json.dumps(existing_meta, ensure_ascii=False), row["brand_id"], row["quote_at"]),
                    )
                except Exception as _e:
                    logger.warning(f"[flow-drop] quote→pay {hours}h 提醒失败 brand={row.get('brand_id')}: {_e}")

        # 3. pay 完成 24h 仍无 write(只 24h · 付款后不急着催 72h)
        cur.execute(
            """
            SELECT psl.brand_id, psl.actor_user_id, b.name AS brand_name, psl.meta, psl.created_at AS pay_at
            FROM pipeline_stage_log psl
            LEFT JOIN brands b ON psl.brand_id = b.id
            WHERE psl.stage_name = 'pay'
              AND psl.event = 'complete'
              AND psl.created_at <= NOW() - INTERVAL '24 hours'
              AND psl.created_at > NOW() - INTERVAL '48 hours'
              AND NOT EXISTS (
                  SELECT 1 FROM pipeline_stage_log p2
                  WHERE p2.brand_id = psl.brand_id
                    AND p2.stage_name = 'write'
                    AND p2.event = 'complete'
                    AND p2.created_at >= psl.created_at
              )
              AND (psl.meta->>'flow_drop_reminder_24h') IS DISTINCT FROM 'sent'
            """
        )
        rows = cur.fetchall() or []
        for row in rows:
            try:
                _send_flow_drop_notification(
                    row["brand_id"],
                    row.get("actor_user_id"),
                    row["brand_name"],
                    from_stage="pay",
                    hours=24,
                )
            except Exception as _e:
                logger.warning(f"[flow-drop] pay→write 24h 提醒失败 brand={row.get('brand_id')}: {_e}")

        conn.commit()
    except Exception as e:
        logger.error(f"[flow-drop] 扫描失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _send_flow_drop_notification(
    brand_id: int,
    _actor_user_id: Optional[int],  # 预留给未来 user-level 通知扩展
    brand_name: str,
    from_stage: str,
    hours: int,
) -> None:
    """写一条流失提醒通知 + pipeline_stage_log 标记 reminder_sent"""
    import json as _json
    templates = {
        ("diagnosis", 24): {
            "title": f"⏰ {brand_name} 诊断已完成 24h · 还没生成 GEO 报价方案",
            "content": "点击进诊断报告 → 生成 GEO 报价方案(¥400 积分)",
            "type": "flow_drop_diagnosis_24h",
        },
        ("diagnosis", 72): {
            "title": f"🔴 {brand_name} 诊断完成 72h 未跟进 · 客户可能流失",
            "content": "最后机会 · 点进诊断报告生成报价方案",
            "type": "flow_drop_diagnosis_72h",
        },
        ("quote", 24): {
            "title": f"💬 {brand_name} 报价方案已生成 24h · 联系客户了吗?",
            "content": "点击进报价中心 → 发送 / 跟进付款",
            "type": "flow_drop_quote_24h",
        },
        ("quote", 72): {
            "title": f"🔴 {brand_name} 报价方案 72h 未付款 · 考虑降档/改方案",
            "content": "升级催收 · 代理主动沟通",
            "type": "flow_drop_quote_72h",
        },
        ("pay", 24): {
            "title": f"✍️ {brand_name} 已付款 24h · 可以开始写文章了",
            "content": "进写作大厅 → 批量生成标题 + 文章",
            "type": "flow_drop_pay_24h",
        },
    }
    tpl = templates.get((from_stage, hours))
    if not tpl:
        return

    try:
        from services.notification_events import NotificationEventType
        from services.notification_outbox import enqueue_brand_owner_notification_event_durable
        enqueue_brand_owner_notification_event_durable(
            brand_id=int(brand_id),
            event_type=NotificationEventType.BUSINESS_ACTION_REQUIRED,
            business_id=f"brand_flow:{int(brand_id)}:{from_stage}:{int(hours)}",
            terminal_state=f"waiting_{int(hours)}h",
            facts={
                "business_no": f"BRAND-{int(brand_id)}",
                "status": tpl["title"],
                "occurred_at": datetime.now().isoformat(timespec="seconds"),
                "summary": "请在今日工作台查看下一步。",
            },
        )
        logger.info(f"[flow-drop] 已发提醒 brand={brand_id} ({from_stage} → next) {hours}h")
    except Exception as e:
        logger.exception("[flow-drop] 通知写入 outbox 失败 brand=%s: %s", brand_id, e)


def _m1a_auto_monitor_after_publish():
    """A.9-B (CTO-15.9 session 3 · 2026-04-25 · 老板拍板"默认关 · 代理同意才扣")

    每 30 分钟扫一次:
      1. 找过去 24-48h 之间 publish complete 事件(避免重复扫旧数据)
      2. 取该 brand 的 owner 代理 user_id
      3. 检查 user_wallets.auto_monitor_after_publish · 关闭则跳过(默认全部跳过)
      4. 检查该 brand 24h 内是否已有 monitor 触发 · 已有则跳过(去重)
      5. 调 batch_monitor.run_client_monitoring(brand_id) 跑当前可采集引擎(charge_on_success)
      6. 写 pipeline_stage_log stage='monitor' event='auto_after_publish'(防重)

    元指令 11:charge_on_success · 失败不扣
    元指令 16:仅代理操作(开关只代理可切 · 客户不知情不参与)
    """
    from db.connection import get_connection
    import json as _json

    try:
        conn = get_connection()
    except Exception as e:
        logger.warning(f"[auto-monitor] 拿连接失败: {e}")
        return

    try:
        cur = conn.cursor()
        # 1. 找 24-48h 前的 publish complete 事件 + 该 brand owner 开关 ON + 24h 内无 monitor 已触发
        cur.execute("""
            SELECT psl.brand_id, b.name AS brand_name, b.owner_user_id, psl.created_at AS publish_at
            FROM pipeline_stage_log psl
            JOIN brands b ON psl.brand_id = b.id
            JOIN user_wallets w ON w.user_id = b.owner_user_id
            WHERE psl.stage_name = 'publish'
              AND psl.event = 'complete'
              AND psl.created_at <= NOW() - INTERVAL '24 hours'
              AND psl.created_at > NOW() - INTERVAL '48 hours'
              AND w.auto_monitor_after_publish = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM pipeline_stage_log p2
                  WHERE p2.brand_id = psl.brand_id
                    AND p2.stage_name = 'monitor'
                    AND p2.created_at >= psl.created_at
              )
            ORDER BY psl.created_at ASC
            LIMIT 50
        """)
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        return

    # [auto_after_publish 2026-06-07 老板拍 B:只停假完成·不激活扣费]
    #   原代码 bug:同步调用 async run_client_monitoring 未 await → 协程被丢弃从不真跑(0 监测/0 扣费),
    #   却照写 monitor/complete stage_log(防重)+ "已自动监测完成"通知 = 误判已完成(funnel 假数据 + 下次扫不再触发)。
    #   按老板:① 不 await/不真跑(暂不激活发布后自动监测扣费)② 不写假 stage_log complete + 不写假通知
    #          ③ 不写防重 stage_log(避免下次被误判已完成)④ 仅留 info 痕。
    #   ⚠️ 未来激活真实自动监测 = 另开资金批(开关/灰度/扣费确认/失败退费/真机回归一起做)。
    brand_ids = [r.get("brand_id") for r in rows if r.get("brand_id")]
    logger.info(
        f"[auto-monitor] 发布后自动监测当前未启用真实监测 · {len(brand_ids)} 个品牌符合条件但跳过 · "
        f"未跑、未扣费、未写完成、未写防重 stage_log · 激活需另开资金批 · brand_ids={brand_ids[:20]}"
    )
    return


@sched_claim("industry_brief_watchdog", 300)  # [FF5] tick claim(audit NEEDS_CLAIM · 覆盖 :545/:2320 两处注册)
def _industry_brief_watchdog():
    """深度行业解析僵尸任务清理（每 5 分钟）

    场景：worker 重启 / event loop 异常 / 内层 except 未触发
    → DB status 卡在 collecting_l*，前端永远显示"超时待重试"，260 积分永远不退

    动作：扫 industry_brief_started_at < NOW()-15min 仍在运行态的任务
    → 强制改 failed + 退款（async refund_points 在线程内开新 loop 跑）
    """
    from db.connection import get_connection
    from db.profile_db import update_profile
    from middleware.billing import refund_points

    cutoff = datetime.now() - timedelta(minutes=15)
    zombies = []
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cp.id AS profile_id, cp.industry_brief_status, b.owner_user_id
            FROM client_profiles cp
            LEFT JOIN brands b ON cp.brand_id = b.id
            WHERE cp.industry_brief_status IN ('running','collecting_l1','collecting_l2','collecting_l3')
              AND cp.industry_brief_started_at IS NOT NULL
              AND cp.industry_brief_started_at < %s
            """,
            (cutoff,),
        )
        zombies = list(cur.fetchall())
    except Exception as e:
        logger.error(f"[Watchdog] 扫描深度解析僵尸任务失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not zombies:
        return

    for row in zombies:
        profile_id = row.get("profile_id")
        owner = row.get("owner_user_id")
        status = row.get("industry_brief_status")
        logger.warning(f"[Watchdog] 发现僵尸深度解析: profile={profile_id}, status={status}, owner={owner}")
        try:
            update_profile(profile_id, industry_brief_status="failed")
        except Exception as e:
            logger.error(f"[Watchdog] 改 failed 失败 profile={profile_id}: {e}")
        if owner:
            try:
                # BackgroundScheduler 工作线程默认无 loop，asyncio.run 安全
                refund_result = asyncio.run(
                    refund_points(owner, "deep_analyze", reason="深度解析任务僵死自动退款")
                )
                if isinstance(refund_result, dict) and refund_result.get("success"):
                    logger.info(f"[Watchdog] 退款成功 user={owner}, profile={profile_id}")
                else:
                    # 常见场景：deduct_points 从未真正执行（历史 bug），找不到扣费记录
                    reason = refund_result.get("reason") if isinstance(refund_result, dict) else "未知"
                    logger.info(f"[Watchdog] 无需退款 user={owner}, profile={profile_id}: {reason}")
            except Exception as e:
                logger.error(f"[Watchdog] 退款异常 user={owner}: {e}")


def _run_trial_pass_expire():
    """v3.2 Phase 3: 代理试用过期清理（每 5 分钟执行）

    status='active' 且 expires_at <= NOW() 的记录转为 expired
    """
    try:
        from api.trial_pass_api import expire_active_trials
        count = expire_active_trials()
        if count > 0:
            logger.info(f"[TrialPass Expire] 清理 {count} 条过期试用")
    except Exception as e:
        logger.error(f"[TrialPass Expire] 失败: {e}")


def _zombie_killer_job():
    """Phase 4 PLAN 03 Task 1 · 每分钟扫 geo_plan_tasks 僵尸任务

    把 heartbeat_at < now - 2min 的 running 任务标 failed:zombie (对应 D8 维度防御).
    异常全吞,只 log,避免污染 APScheduler 内置错误处理.
    """
    try:
        from db.geo_plan_tasks_db import mark_zombie
        n = mark_zombie(threshold_seconds=120)
        if n > 0:
            logger.warning(f"[scheduler/zombie] 标记 {n} 个 geo_plan_tasks 僵尸任务")
    except Exception as e:
        logger.error(f"[scheduler/zombie] 失败: {e}")


def _geo_plan_reconcile_job():
    """[v5 req1] geo_plan_tasks【结算/退款补偿队列】reconcile(每 1 分钟)。

    扫 settlement_pending / refund_pending → 重试 commit_freeze / release_freeze(幂等)→ 落终态。
    统一收口 worker 结算失败、cancel、zombie、server_restart 遗留的待补偿 freeze。
    异常全吞,只 log,避免污染 APScheduler 内置错误处理。
    """
    try:
        import asyncio
        from services.geo_plan_settlement import reconcile_pending
        # BackgroundScheduler 工作线程默认无 loop · asyncio.run 安全
        stats = asyncio.run(reconcile_pending(limit=100))
        if stats.get("settled_done") or stats.get("refunded_terminal") or stats.get("still_pending") or stats.get("conflict"):
            logger.warning(f"[scheduler/geo_plan_reconcile] {stats}")
    except Exception as e:
        logger.error(f"[scheduler/geo_plan_reconcile] 失败: {e}")


def _fund_recovery_processor_job():
    """[v6 req3] fund_recovery_orders 处理器(每 1 分钟)· 认领到期 pending 工单补偿退款,退避/转 manual。异常全吞只 log。"""
    try:
        import asyncio
        from services.fund_recovery_processor import process_pending
        stats = asyncio.run(process_pending(limit=100))
        if stats.get("claimed"):
            logger.warning(f"[scheduler/fund_recovery] {stats}")
    except Exception as e:
        logger.error(f"[scheduler/fund_recovery] 失败: {e}")


def _billing_debt_offset_processor_job():
    """Retry durable per-charge debt-offset receipts (DB-only, Redis independent)."""
    try:
        from services.billing_debt_offset_outbox import process_pending_debt_offsets

        stats = process_pending_debt_offsets(limit=100)
        if stats.get("claimed"):
            logger.warning(f"[scheduler/billing_debt_offset] {stats}")
    except Exception as e:
        logger.error(f"[scheduler/billing_debt_offset] 失败: {type(e).__name__}")


def _archive_job():
    """Phase 4 PLAN 03 Task 1 · 每日凌晨归档 geo_plan_tasks 老任务

    把 done_at < now - 90 天 且 status 终态的任务 archived_at = NOW,
    list_tasks_by_user 默认过滤掉 (D15 维度).
    异常全吞,只 log.
    """
    try:
        from db.geo_plan_tasks_db import archive_done_older_than
        n = archive_done_older_than(days=90)
        logger.info(f"[scheduler/archive] 归档 {n} 个 90 天前的终态任务")
    except Exception as e:
        logger.error(f"[scheduler/archive] 失败: {e}")


def _run_partner_kyc_cleanup():
    """v1.1 代理申请审核制: 90 天清理拒绝/撤回申请的身份证材料

    每天 03:30 CST 执行:
      1. 扫 status IN ('rejected', 'withdrawn') AND reviewed_at < NOW() - 90 天
      2. 删 OSS 3 个对象 (front / back / selfie)
      3. UPDATE agent_applications 把 *_key 置 NULL (保留审计记录本身)

    注意: 本任务**不删除**申请记录, 只清敏感材料. 审计/反滥用线索保留.
    """
    from db.connection import get_connection
    try:
        from services.oss_service import delete_oss_object, OSSConfigError
    except Exception as e:
        logger.warning(f"[KYC-Cleanup] 服务未就绪, 跳过: {e}")
        return

    deleted = 0
    failed = 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, id_card_front_key, id_card_back_key, id_card_selfie_key
            FROM agent_applications
            WHERE status IN ('rejected', 'withdrawn')
              AND reviewed_at IS NOT NULL
              AND reviewed_at < NOW() - INTERVAL '90 days'
              AND (id_card_front_key IS NOT NULL
                   OR id_card_back_key IS NOT NULL
                   OR id_card_selfie_key IS NOT NULL)
            LIMIT 500
            """
        )
        rows = cursor.fetchall() or []

        for row in rows:
            app_id = row["id"]
            keys = [
                row.get("id_card_front_key"),
                row.get("id_card_back_key"),
                row.get("id_card_selfie_key"),
            ]
            keys = [k for k in keys if k]

            all_deleted = True
            for k in keys:
                try:
                    ok = delete_oss_object(k)
                except OSSConfigError as e:
                    logger.warning(f"[KYC-Cleanup] OSS 未配置, 任务终止: {e}")
                    return
                except Exception as e:
                    logger.warning(f"[KYC-Cleanup] 删 key={k} 异常: {e}")
                    ok = False
                if not ok:
                    all_deleted = False

            if all_deleted:
                try:
                    cursor.execute(
                        """
                        UPDATE agent_applications
                        SET id_card_front_key = NULL,
                            id_card_back_key = NULL,
                            id_card_selfie_key = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (app_id,),
                    )
                    deleted += 1
                except Exception as e:
                    logger.error(f"[KYC-Cleanup] 清 DB 字段失败 app={app_id}: {e}")
                    failed += 1
            else:
                failed += 1

        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error(f"[KYC-Cleanup] 执行失败: {e}")
    finally:
        try: conn.close()
        except Exception: pass

    if deleted or failed:
        logger.info(f"[KYC-Cleanup] 完成: 清理 {deleted} 条, 失败 {failed} 条")


def _topic_writing_watchdog():
    """[写作卡死根治 2026-06-08 · 方案 B] 兜底:扫卡死 90min+ 的 writing 选题标 write_timeout(不回 draft)。

    方案 B(老板 2026-06-08 拍):卡死选题多数已扣费,若回 draft 用户重选会二次扣费(双扣)。
    故标 write_timeout — start-articles 不接受该状态 → 不可重选 → 防双扣。钱待 V3.5 精确退款资金批退。
    判据 COALESCE(writing_started_at, created_at) < NOW()-90min:
      - writing_started_at 在入口 + generate_one(单篇拿到并发槽)各刷新 → 正在写的不误杀
      - 阈值 90min 覆盖大批次排队(97 篇约 10 波末波排队 ~30min · 留足余量)
      - 旧数据 writing_started_at NULL → 退回 created_at(本就卡死)
    护栏 article_id IS NULL:已出稿的不动。幂等 · 无 freeze · 不退费 · billing.py 0 碰。
    """
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("""
                UPDATE topics
                SET status='write_timeout'
                WHERE status='writing' AND article_id IS NULL
                  AND COALESCE(writing_started_at, created_at) < NOW() - INTERVAL '90 minutes'
                RETURNING quote_id
            """)
            _rows = c.fetchall()
            n = len(_rows)

            # ── [WO R2 2026-08-09] regenerating 也纳入看门狗(孤儿根因) ──
            # `db/diagnosis_db.regenerate_topic()` 只写 status='regenerating'
            # + regenerate_count+1,**不碰 is_optimize**。而:
            #   · `optimize_title_jobs.recover_optimize_title_jobs` 只捡
            #     is_optimize=TRUE 且 optimized_title='标题生成中...' 的行;
            #   · 本看门狗(改之前)只管 status='writing';
            #   · `article_generation_reset` 又对 regenerating 抛
            #     ARTICLE_RESET_IN_PROGRESS —— 客户连自救都点不动。
            # 三方都不管 = 死胡同。生产实证:quote 386 的 topic 6122-6133 从
            # 2026-07-15 卡到被人工发现,12 行、regenerate_count 6-15。
            #
            # 🔴 恢复语义必须**保持 charge-on-success**:与上面 writing 那支同一条
            #   铁律 —— 推回 draft/pending 会让用户重选、**二次扣费**。所以推
            #   'failed':它是**可恢复**态(optimize_title_jobs 的派发输入集含
            #   failed;article_generation_reset 也不拒绝它),客户能自己走正常路重来,
            #   而重来那一次才按 charge-on-success 正常计费 —— 卡死这一次不产生任何扣费。
            #
            # 阈值 24h(不是 90min):选题重生成是人点一次、LLM 跑几十秒的动作,
            #   没有写作那种大批次排队;24h 足够宽,不会误杀正在跑的。
            # 护栏 article_id IS NULL:已出稿的不动(同上面那支)。
            #
            # 🔴 [返工 2026-08-09] 判据锚 regenerate_started_at,**不是 created_at**。
            #   created_at 是选题**生成**时间 —— 30 天前的老选题今天刚点"重新生成",
            #   用 created_at 判会当场被这支误杀。与上面 writing 支同构
            #   (它 2026-06-08 就因同一个理由改锚 writing_started_at)。
            #   三个入态点都写 NOW():db.diagnosis_db.regenerate_topic /
            #   server.py 智能补足建行(INSERT)/ server.py 智能补足重试(UPDATE)。
            #   COALESCE 回落 created_at 只服务**本次上线前就已在途**的存量行
            #   —— 6122-6133 那批(卡了 20+ 天)正在这一集里,要的就是捡起它们。
            # 幂等 · 无 freeze · 不退费 · billing.py 0 碰。
            c.execute("""
                UPDATE topics
                SET status='failed',
                    fail_reason=COALESCE(fail_reason, '选题重生成超时未回执(看门狗回收,可重新生成)')
                WHERE status='regenerating' AND article_id IS NULL
                  AND COALESCE(regenerate_started_at, created_at) < NOW() - INTERVAL '24 hours'
                RETURNING quote_id
            """)
            _regen_rows = c.fetchall()
            n_regen = len(_regen_rows)
            _rows = list(_rows) + list(_regen_rows)
            # [P1 修复] 同步回退受影响项目的 writing_status:释放后若该 quote 已无 writing topics,
            # 回退 quotes.writing_status='titles_ready'(口径对齐请求失败释放 helper),防项目列表仍显示"写作中"
            _affected_quotes = {r["quote_id"] for r in _rows if r.get("quote_id") is not None}
            for _qid in _affected_quotes:
                c.execute(
                    "UPDATE quotes SET writing_status='titles_ready' "
                    "WHERE id=%s AND writing_status='writing' "
                    "AND NOT EXISTS (SELECT 1 FROM topics WHERE quote_id=%s AND status='writing')",
                    (_qid, _qid),
                )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if n:
            logger.info(f"[topic_writing_watchdog] 标记 {n} 个卡死 writing 选题 → write_timeout(防双扣·待退款)")
        if n_regen:
            logger.info(
                f"[topic_writing_watchdog] 回收 {n_regen} 个卡死 regenerating 选题 → failed"
                f"(可重新生成 · charge-on-success 不产生扣费)"
            )
    except Exception as e:
        logger.warning(f"[topic_writing_watchdog] 执行失败: {e}")


def register_monitoring_delivery_jobs(scheduler: BackgroundScheduler) -> bool:
    """Register paid phrase monitoring on the single scheduler control plane.

    The callable stays in ``scheduler.py`` for explicit/manual compatibility,
    but automatic registration belongs here. The DB entitlement readers are
    disjoint per phrase: once a keyword subscription exists, that state machine
    owns the phrase; quote-level scheduling handles only never-subscribed phrases.
    """
    job_id = "keyword_subscription_daily_monitoring"
    if scheduler.get_job(job_id):
        return False

    from scheduler import job_daily_monitoring

    claimed_job = sched_claim(job_id, 24 * 60 * 60)(job_daily_monitoring)
    scheduler.add_job(
        scheduler_sync_callable(claimed_job),
        trigger=CronTrigger(hour=9, minute=0, timezone=BEIJING_TZ),
        id=job_id,
        name="逐词订阅每日监测 09:00 CST（单控制面）",
        replace_existing=True,
    )
    logger.info("[Scheduler] 逐词订阅每日监测已注册到 api.scheduler 单控制面")
    return True


def run_async_in_scheduler(coro_func, loop=None):
    """把 async 函数包成 BackgroundScheduler 能跑的同步 callable。

    🔴 [#197 P0 · 2026-09-13] 为什么必须包:`BackgroundScheduler` 在线程里**同步**
       调 job。直接把 `async def` 传进 `add_job`,线程里只会**造出一个 coroutine
       对象然后返回** —— 调度器记 executed successfully,函数体一行没跑。
       生产日志里唯一的痕迹是 `RuntimeWarning: coroutine ... was never awaited`,
       而那条警告在正常日志量里没人会看见。
       实测后果:图文合同链 worker 自 08-17 注册以来**从未执行过**
       (三条 lane + 五个收敛器 + 回填 + 失败投影全在里面),
       而「注册了」看起来和「在跑」一模一样。

    🔴 只有这**一个**实现:`_setup_mhz_jobs` 里那只同名闭包现在委托给它。
       同一件事两个实现,第二个永远会漏掉第一个后来新增的动作。

    loop: 调度器所属的主事件循环;None 时在包装**当下**取。
          取不到就退化成 `asyncio.run()`(自己起一个),与既有行为一致。
    """
    import asyncio
    import inspect

    # 🔴 非协程函数**原样放行**。这样动态派发的注册点(下面那张
    #    "模块:函数" 表,运行时才知道拿到的是什么)可以**无条件**包一层:
    #    拿到同步函数照旧、拿到 async 自动包好。
    #    否则那张表就是下一个同型缺陷的温床 —— 往里加一个 async,
    #    它会安静地每轮造一个 coroutine 然后什么都不做。
    if not inspect.iscoroutinefunction(coro_func):
        return coro_func

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

    job_name = getattr(coro_func, "__name__", "async_job")

    def _report_exception(fut):
        """[#197 c5] 把 Future 里的异常**捞出来记日志**。

        🔴 `run_coroutine_threadsafe` 是 fire-and-forget:返回的 Future 没人读,
           协程抛的异常就留在里面,既不打印也不上报 —— 又是一次静默,
           而这正是本单(#197)那个病往下一层的同一张脸。
           `asyncio.run` 那条分支会把异常抛回 APScheduler,它自己会记,不用动。
        """
        try:
            exc = fut.exception()
        except Exception:                      # 被取消 / Future 状态异常
            return
        if exc is not None:
            logger.error("[scheduler] async job %s 异常: %r", job_name, exc,
                         exc_info=exc)

    def wrapper():
        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro_func(), loop)
            fut.add_done_callback(_report_exception)
        else:
            asyncio.run(coro_func())

    wrapper.__name__ = job_name
    return wrapper


def register_v32_core_tasks():
    """注册 v3.2/v3.3 核心定时任务（与监测开关解耦）

    ⚠️ 安全修复 2026-04-17 (P0-G):
       之前这些任务写在 setup_schedule 里，只在 auto_monitor_enabled=True 才注册。
       结果：全新环境 / 关闭监测的用户 → 佣金永远 pending，试用永不过期，
       托管套餐永不 tick，brand_package 永不休眠。
       修复：抽出核心任务在 startup 无条件注册，与监测开关无关。
    """
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler = get_scheduler()

    # ── GEO 图文合同链 durable worker(返工 2026-08-18 · 链 3)────────────────
    # 🔴 注册在**无条件**这一段,而不是 setup_schedule:后者受 auto_monitor_enabled
    #    门控(prod 实测 = 0),挂在那里等于"注册了但永远不跑" —— 与"零生产调用者"
    #    是同一种死法,只是更难发现(job 列表里看得见,却从不触发)。
    # 🔴 `coalesce + max_instances=1`:蓝绿重叠期两个实例都会跑这个 cron,
    #    真正的互斥靠 claim 的 `FOR UPDATE SKIP LOCKED` + 租约 CAS,
    #    这两个参数只防同一进程内自己叠自己。
    # 🔴 flag 关时**注册但空转**:worker 内部按 `queued` 领,flag 关时不会有新任务,
    #    但**已经在途**的任务仍必须被收敛(否则关 flag 等于让在途任务连同冻结一起卡死)。
    if not scheduler.get_job("geo_image_note_contract_worker"):
        try:
            from services.geo_douyin.contract_worker import run_tick

            scheduler.add_job(
                # 🔴 [#197 P0] `run_tick` 是 async def。裸传给 BackgroundScheduler
                #    等于每 20 秒造一个 coroutine 就扔掉 —— 08-17 注册至今一行没跑。
                run_async_in_scheduler(run_tick),
                trigger=IntervalTrigger(seconds=20),
                id="geo_image_note_contract_worker",
                name="GEO 图文合同链 worker(每 20s · 制作/素材/发布三条链 + 回收)",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("[Scheduler] GEO 图文合同链 worker 已注册(每 20s)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Scheduler] 注册 GEO 图文合同链 worker 失败: {e}")

    # 用户通知只由 cron leader 派发。数据库 outbox 自身再用 SKIP LOCKED、
    # claim token 与唯一 event_key 防御蓝绿重叠、kill-9 和重复 tick。
    if not scheduler.get_job("notification_outbox_dispatch"):
        try:
            from services.notification_outbox import notification_outbox_cron

            scheduler.add_job(
                notification_outbox_cron,
                trigger=IntervalTrigger(seconds=10),
                id="notification_outbox_dispatch",
                name="关键业务通知 outbox 派发（每10秒·leader）",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("[Scheduler] 关键业务通知 outbox 派发任务已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 通知 outbox 派发任务注册失败: {e}")

    # ── 防御型 GEO 正式诊断执行器(门三 G9)────────────────────────────────
    # 🔴 这条接线本身就是修复。缺了它,confirm 会把 run 推进 running、冻住算力,
    #    然后没有任何东西接手 —— 门库实证:run_b6ad4e77… 在 running 挂了 23 分钟,
    #    7800 算力一直冻着,客户付了钱而报告永远不会出现。
    #    判据 `test_executor_is_registered_in_the_scheduler` 钉住这一行的存在,
    #    变异「从调度里摘掉执行器」必须让它变红。
    if not scheduler.get_job("defgeo_run_execute"):
        try:
            from services.defensive_geo.run_executor import consume_confirmed_runs_sync

            scheduler.add_job(
                consume_confirmed_runs_sync,
                trigger=IntervalTrigger(seconds=20),
                id="defgeo_run_execute",
                name="防御型 GEO 正式诊断执行器（每20秒·领取已确认未执行的 run）",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("[Scheduler] 防御型 GEO 诊断执行器已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 防御型 GEO 诊断执行器注册失败: {e}")

    # ── 防御型 GEO 发布链执行侧(包E · 终审 P0-1 的另一半)────────────────
    # 🔴 P0-1 的处置是「关掉客户入口」,理由逐字:dispatch_once / claim_outbox /
    #    reconcile_once 全仓零调用点,调度器里跟发布有关的只有只读告警,
    #    而 confirm 是真冻钱的。关入口是止血;**这四条 add_job 才是修**。
    # 🔴 全部注册在**无条件**这一段:挂进 gated 的 setup_schedule 等于
    #    「注册了但永远不跑」—— 本仓已记过五次同坑,发布链再犯一次的症状是
    #    客户的算力冻着没人派发。
    # 🔴 `coalesce + max_instances=1` 只防同进程自己叠自己;蓝绿重叠期的真互斥
    #    靠 claim 的 FOR UPDATE SKIP LOCKED + 租约 CAS。
    for _job_id, _entry, _seconds, _name, _fail_closed in (
        (
            "defgeo_activation_materialize",
            "services.defensive_geo.activation_materializer:materialize_pending_sync",
            30,
            "防御型 GEO 激活物化（每30秒·签发 provider 执行预算·零冻结）",
            True,
        ),
        (
            "defgeo_publish_dispatch",
            "services.defensive_geo.publish.publish_worker:dispatch_pending_sync",
            20,
            "防御型 GEO 发布派发（每20秒·领取 outbox → 外调 → 落 canonical outcome）",
            True,
        ),
        (
            "defgeo_publish_reconcile",
            "services.defensive_geo.publish.publish_worker:reconcile_tick_sync",
            300,
            "防御型 GEO 发布结算收敛（每5分钟·冻结未派发/外调无回执/结果未结算）",
            True,
        ),
        (
            "defgeo_publish_settlement_pending_alert",
            "services.defensive_geo.publish.settlement_alerts:scan_and_alert",
            3600,
            "防御型 GEO 发布结算待核验超 7 天告警（每小时·只读 + upsert_alert）",
            False,
        ),
    ):
        if scheduler.get_job(_job_id):
            continue
        try:
            _mod_name, _fn_name = _entry.split(":")
            # [#197] 动态派发:运行时才知道拿到的是同步还是 async。
            #   无条件包一层 —— 包装器对非协程函数原样放行,所以对现有条目
            #   逐字节同行为;而将来往这张表里加 async 时不会静默失效。
            _fn = run_async_in_scheduler(
                getattr(__import__(_mod_name, fromlist=[_fn_name]), _fn_name))
            scheduler.add_job(
                _fn,
                trigger=IntervalTrigger(seconds=_seconds),
                id=_job_id,
                name=_name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("[Scheduler] %s 已注册(每 %ss)", _job_id, _seconds)
        except Exception as e:  # noqa: BLE001
            if _fail_closed:
                # 🔴 fail-closed:少一个**推进器**的 cron 不是健康的 cron。
                #    让它带病当上 leader,症状就是 P0-1 原样复发 ——
                #    客户确认发布、算力冻住、没有任何东西接手。
                #    与 register_monitoring_delivery_jobs 同口径:拒绝启动。
                logger.error("[Scheduler] %s 注册失败(fail-closed,拒绝启动 cron): %s",
                             _job_id, e)
                raise RuntimeError(f"{_job_id} 注册失败，拒绝带病启动 cron") from e
            logger.warning("[Scheduler] 注册 %s 失败: %s", _job_id, e)

    # GEO article delivery planning is a default-off sidecar.  Registration is
    # unconditional so a later flag change does not require another code path;
    # the worker itself is a no-op unless reconciler + shadow flags are ready.
    if not scheduler.get_job("article_delivery_plan_reconciler"):
        try:
            from services.article_delivery_plan import article_plan_cron

            scheduler.add_job(
                article_plan_cron,
                trigger=IntervalTrigger(seconds=30),
                id="article_delivery_plan_reconciler",
                name="GEO文章交付影子计划对账（每30秒·默认关闭）",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("[Scheduler] GEO文章交付影子计划对账任务已注册（运行开关默认关闭）")
        except Exception as e:
            logger.warning(f"[Scheduler] GEO文章交付影子计划对账注册失败: {e}")

    try:
        register_monitoring_delivery_jobs(scheduler)
    except Exception as exc:
        # A missing paid-delivery job is not a healthy cron process. Propagate the
        # failure so ROLE=cron cannot become leader while silently under-delivering.
        logger.error("[Scheduler] 逐词订阅监测注册失败（fail-closed，不回退根调度）: %s", exc)
        raise RuntimeError("逐词订阅监测注册失败，拒绝启动 cron") from exc

    # Dealer-resale profit is withdrawable only after the DB-clock 72h window.
    # The SQL uses SKIP LOCKED + status predicates, so duplicate cron processes or
    # a kill/retry can only move each ledger row pending -> available once.
    if not scheduler.get_job("dealer_resale_profit_maturity"):
        try:
            from services.dealer_inventory_resale import mature_profits

            scheduler.add_job(
                mature_profits,
                trigger=IntervalTrigger(minutes=5),
                id="dealer_resale_profit_maturity",
                name="逐级库存转售利润到期释放(每5分钟)",
                replace_existing=True,
                kwargs={"limit": 500},
            )
            logger.info("[Scheduler] 逐级库存转售利润到期任务已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 逐级库存转售利润到期任务注册失败: {e}")

    # Local timeout is not payment-channel evidence. This job is deliberately
    # read-only and only surfaces stale reservations for reconciliation.
    if not scheduler.get_job("dealer_resale_stale_reservation_release"):
        try:
            from services.dealer_inventory_resale import release_stale_reservations

            scheduler.add_job(
                release_stale_reservations,
                trigger=IntervalTrigger(minutes=5),
                id="dealer_resale_stale_reservation_release",
                name="逐级库存转售超时预占对账扫描(每5分钟·不自动释放)",
                replace_existing=True,
                kwargs={"limit": 500, "stale_after_minutes": 30},
            )
            logger.info("[Scheduler] 逐级库存转售超时预占只读对账任务已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 逐级库存转售超时预占释放任务注册失败: {e}")

    # T+3 佣金结算
    if not scheduler.get_job("daily_commission_settle"):
        try:
            scheduler.add_job(
                _run_commission_settlement,
                trigger=CronTrigger(hour=2, minute=0, timezone=BEIJING_TZ),
                id="daily_commission_settle",
                name="T+3 佣金结算 02:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] T+3 佣金结算已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] T+3 佣金结算注册失败: {e}")

    # [BUG-004 集成修复 2026-06-10 Deploy-CTO] freeze_sweeper / daily_compliance_check / daily_service_check
    # 此前只在 setup_schedule 注册(受 auto_monitor_enabled 控制)·prod 该值=0 → 三 cron 全停:
    #   冻结积分永久悬挂 / 达标日志缺天 / 过期服务不停门户 token。移入无条件 startup 注册根治。
    #   (hourly_monitoring_check 仍留 setup_schedule 受 auto_monitor_enabled 控制,它本就该跟监测开关走。)
    #   idempotent:setup_schedule 也注册同 id 时 if-not-get_job + replace_existing 保证单实例,无双注册。
    if not scheduler.get_job("daily_service_check"):
        try:
            scheduler.add_job(
                _check_service_periods,
                trigger=CronTrigger(hour=1, minute=0, timezone=BEIJING_TZ),
                id="daily_service_check",
                name="每日服务期检查 01:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] 每日服务期检查已注册(无条件·BUG-004)")
        except Exception as e:
            logger.warning(f"[Scheduler] 每日服务期检查注册失败: {e}")

    if not scheduler.get_job("daily_compliance_check"):
        try:
            scheduler.add_job(
                _run_compliance_check,
                trigger=CronTrigger(hour=23, minute=30, timezone=BEIJING_TZ),
                id="daily_compliance_check",
                name="每日达标判定 23:30 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] 每日达标判定已注册(无条件·BUG-004)")
        except Exception as e:
            logger.warning(f"[Scheduler] 每日达标判定注册失败: {e}")

    if not scheduler.get_job("freeze_sweeper_hourly"):
        try:
            scheduler.add_job(
                _run_freeze_sweep_hourly_job,
                trigger=IntervalTrigger(hours=1),
                id="freeze_sweeper_hourly",
                name="point_freezes zombie 兜底扫描(每 1h · 12h+ frozen 自动 release)",
                replace_existing=True,
            )
            logger.info("[Scheduler] freeze_sweeper hourly 已注册(无条件·BUG-004)")
        except Exception as e:
            logger.warning(f"[Scheduler] freeze_sweeper 注册失败: {e}")

    # [B1-4 · Deploy-CTO 集成修复 2026-07-03] 调研监测 zombie round 周期兜底 sweep(每 15min)
    #   飞轮包原把此 job 加在 setup_schedule(受 auto_monitor_enabled 控制·prod=0 → 永不注册),
    #   与 BUG-004/P0-G 同坑。移入无条件 startup 注册(register_v32_core_tasks)根治。
    #   setup_schedule 内同 id 副本保留:if-not-get_job + replace_existing 幂等,无双注册。
    if not scheduler.get_job("research_monitor_zombie_sweep"):
        try:
            from services.research_monitor.restart_recovery import sweep_zombie_rounds
            scheduler.add_job(
                sweep_zombie_rounds,
                trigger=IntervalTrigger(minutes=15),
                id="research_monitor_zombie_sweep",
                name="调研监测 zombie round 周期兜底 sweep(每 15min · startup sweep 之外)",
                replace_existing=True,
            )
            logger.info("[Scheduler] research_monitor zombie sweep 已注册(每 15min · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 research_monitor zombie sweep 失败: {e}")

    # [WORKERS=4 · SPEC §3.4/§3.6a · 对抗审核 P0] 诊断资金 sweeper(2min)+ reconciler(5min)**无条件注册**。
    #   🔴 初版误放 gated setup_schedule(auto_monitor_enabled · prod=0 → 永不注册),与 BUG-004/P0-G/B1-4/T5
    #   同坑 —— prod 下资金自愈(判死/收尸/结算退避)+ 终态回补全死。移入无条件 register_v32_core_tasks 根治。
    #   sweeper money 但天然幂等(CAS + billing idempotent)→ sched_claim reclaimable=True;reconciler 纯读+publish。
    #   不动 middleware/billing.py(🔴 A 级红线 · 结算四参在 services.diagnosis_runs 内调公开接口)。
    if not scheduler.get_job("diagnosis_run_sweeper"):
        try:
            from apscheduler.triggers.interval import IntervalTrigger  # 块内导入(此处早于 :2233 的 _IT · 防 NameError 静默失败)
            from services.diagnosis_runs import run_diagnosis_sweep
            _wrapped_diag_sweep = scheduler_sync_callable(
                sched_claim("diagnosis_run_sweeper", 120, reclaimable=True)(run_diagnosis_sweep)
            )
            scheduler.add_job(
                _wrapped_diag_sweep,
                trigger=IntervalTrigger(minutes=2),
                id="diagnosis_run_sweeper",
                name="诊断资金 sweeper(每 2min · 判死/收尸/结算退避重试 · 无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] diagnosis_run_sweeper 已注册(每 2min · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 diagnosis_run_sweeper 失败: {e}")

    if not scheduler.get_job("diagnosis_reconciler"):
        try:
            from apscheduler.triggers.interval import IntervalTrigger  # 块内导入(防 NameError 静默失败)
            from services.diagnosis_runs import run_diagnosis_reconciler
            _wrapped_diag_reconciler = scheduler_sync_callable(
                sched_claim("diagnosis_reconciler", 300, fail_open=True, reclaimable=True)(run_diagnosis_reconciler)
            )
            scheduler.add_job(
                _wrapped_diag_reconciler,
                trigger=IntervalTrigger(minutes=5),
                id="diagnosis_reconciler",
                name="诊断终态回补 reconciler(每 5min · Redis 恢复后重发终态快照 · 无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] diagnosis_reconciler 已注册(每 5min · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 diagnosis_reconciler 失败: {e}")

    # [T5 · 2026-07-03 复审自修] 答案实体日增量抽取 04:00 CST。
    #   🔴 初版误放 gated setup_schedule(auto_monitor_enabled 控制·prod=0 → 永不注册),
    #   与 BUG-004/P0-G/B1-4 同坑,复审抓出移入无条件 startup 注册根治。
    #   job 内部运行时查 flag answer_entity_auto_extract(默认关):关 → 每日仅一次 flag 读取即返回,
    #   零 LLM/零写库/零行为变化;开 → only_pending 增量抽取,单日 cap(ANSWER_ENTITY_DAILY_EXTRACT_CAP=500)。
    if not scheduler.get_job("answer_entity_daily_extract"):
        try:
            scheduler.add_job(
                _run_answer_entity_daily_extract,
                trigger=CronTrigger(hour=4, minute=0, timezone=BEIJING_TZ),
                id="answer_entity_daily_extract",
                name="答案实体日增量抽取 04:00 CST(flag 默认关 · 无条件注册)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 答案实体日增量抽取已注册(04:00 CST · flag 门控 · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册答案实体日增量抽取失败: {e}")

    # [W1 · 2026-07-03] 搜索问题类型日增量分类 04:20 CST。
    #   同 BUG-004 铁律:进无条件 register_v32_core_tasks,禁进 gated setup_schedule。
    #   job 内运行时查 flag query_intent_auto_classify(默认关):关 → 每日仅一次 flag 读取即返回,
    #   零 LLM/零写库/零行为变化;开 → 增量分类未打标签的问题,单日 cap(QUERY_INTENT_DAILY_CLASSIFY_CAP=500)。
    if not scheduler.get_job("query_intent_daily_classify"):
        try:
            scheduler.add_job(
                _run_query_intent_daily_classify,
                trigger=CronTrigger(hour=4, minute=20, timezone=BEIJING_TZ),
                id="query_intent_daily_classify",
                name="搜索问题类型日增量分类 04:20 CST(flag 默认关 · 无条件注册)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 问题类型日增量分类已注册(04:20 CST · flag 门控 · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册问题类型日增量分类失败: {e}")

    # [GEO article v1.4] 写作候选自动蒸馏每月 1/16 日 04:40 CST。
    #   job 内运行时查 flag writing_distill_auto(默认关):关 → 每半月一次 flag 读取即返回;
    #   开 → 对达标组 LLM 蒸馏,cap(WRITING_DISTILL_AUTO_CAP=10),产物只落 draft 不影响客户。
    if not scheduler.get_job("writing_distill_auto"):
        try:
            scheduler.add_job(
                _run_writing_distill_auto,
                trigger=CronTrigger(day="1,16", hour=4, minute=40, timezone=BEIJING_TZ),
                id="writing_distill_auto",
                name="写作候选自动蒸馏 每月1/16日04:40 CST(flag 默认关)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 写作候选自动蒸馏已注册(每月1/16日04:40 CST · flag 门控)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册写作候选自动蒸馏失败: {e}")

    # [P0-3 · 2026-08-14]监测 outcome 存量回填:每日 03:50 CST(分批,幂等只补 NULL)。
    # 🔴 **必须早于 04:05 的归因账本同步**:同步的引擎读 target_outcome,回填排在
    #    同步之后,当天新解析的行要多等一天才进账本(与 04:05/04:20 同一条时序纪律)。
    if not scheduler.get_job("monitoring_outcome_backfill"):
        try:
            scheduler.add_job(
                _run_monitoring_outcome_backfill,
                trigger=CronTrigger(hour=3, minute=50, timezone=BEIJING_TZ),
                id="monitoring_outcome_backfill",
                name="监测outcome存量回填 每日03:50 CST(分批·只补NULL·确定性零LLM)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 监测outcome存量回填已注册(每日03:50 CST)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册监测outcome存量回填失败: {e}")

    # [WO_DELIVERY_FLYWHEEL_CLOSURE §2.1 · 2026-08-06]归因账本同步:每日 04:05 CST。
    # 🔴 **必须早于 04:20 的 writing_outcome_backfill**:回写读的就是这个账本,同步排在
    #    回写之后的话,每天的回写永远只能看到前一天的账本(差一天的静默偏差)。
    #    这 15 分钟的间隔是设计,不是随手挑的 —— 改动这两个时间必须一起改。
    if not scheduler.get_job("article_attribution_sync"):
        try:
            scheduler.add_job(
                _run_article_attribution_sync,
                trigger=CronTrigger(hour=4, minute=5, timezone=BEIJING_TZ),
                id="article_attribution_sync",
                name="归因账本同步 每日04:05 CST(监测池→账本·只写不改)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 归因账本同步已注册(每日04:05 CST)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册归因账本同步失败: {e}")

    # [WO-PRICING-FP §返工 · 2026-08-09]投放结果事实表重建:每日 04:35 CST。
    # 🔴 **必须晚于 04:05 归因同步**:两者读同一批监测数据,排在前面只会重复算一遍旧账本。
    #    也晚于 04:20 的回写,避免三个 job 同时压库。
    if not scheduler.get_job("publication_outcome_facts"):
        try:
            scheduler.add_job(
                _run_publication_outcome_facts,
                trigger=CronTrigger(hour=4, minute=35, timezone=BEIJING_TZ),
                id="publication_outcome_facts",
                name="投放结果事实表重建 每日04:35 CST(全量重建·只存计数不存比率)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 投放结果事实表重建已注册(每日04:35 CST)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册投放结果事实表重建失败: {e}")

    # [WP9-P0-3 · Owner D-P0-3 授权点火]写作效果闭环回写:每日 04:20 CST 跑一次
    # (幂等 upsert · 只读跨界取被引 · research 域零写 · 无数据诚实 insufficient)。
    if not scheduler.get_job("writing_outcome_backfill"):
        try:
            scheduler.add_job(
                _run_writing_outcome_backfill,
                trigger=CronTrigger(hour=4, minute=20, timezone=BEIJING_TZ),
                id="writing_outcome_backfill",
                name="写作效果闭环回写 每日04:20 CST(诚实被引·research只读)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 写作效果闭环回写已注册(每日04:20 CST)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册写作效果闭环回写失败: {e}")

    # [WP12 P2-6 · Master SSOT v2.4 ⑥]两周一次写作有效性复核报告:每月 1/16 日
    # 05:40 CST。**只出报告不自动改配比**(配比涉收入结构,留人审)。承接既有
    # P0-3 闭环的发布→被引血缘,不重造第二条链路。
    if not scheduler.get_job("writing_effectiveness_review"):
        try:
            scheduler.add_job(
                _run_writing_effectiveness_review,
                trigger=CronTrigger(day="1,16", hour=5, minute=40, timezone=BEIJING_TZ),
                id="writing_effectiveness_review",
                name="写作有效性两周复核报告 每月1/16日05:40 CST(只出报告)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 写作有效性两周复核报告已注册(每月1/16日05:40 CST)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册写作有效性复核报告失败: {e}")

    if not scheduler.get_job("geo_article_evolution_cycle"):
        try:
            scheduler.add_job(
                _run_geo_article_evolution_cycle,
                trigger=CronTrigger(day="1,16", hour=5, minute=10, timezone=BEIJING_TZ),
                id="geo_article_evolution_cycle",
                name="GEO文章进化审计 每月1/16日05:10 CST(flag 默认关)",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO文章进化审计已注册(每月1/16日05:10 CST · flag 门控)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册GEO文章进化审计失败: {e}")

    # [R 批 · U4 · 2026-07-05] 代理自助调研队列兜底消费(每 3 分钟)。
    #   同 BUG-004 铁律:进无条件 register_v32_core_tasks,禁进 gated setup_schedule。
    #   端点提交时已 BackgroundTask 即时触发,本 job 兜住无端点触发/上轮跑完仍有排队的场景;
    #   内部无活跃 round 才派发单条 queued,无排队则一次 SELECT 即返回,近零成本。
    if not scheduler.get_job("selfserve_research_queue_consume"):
        try:
            scheduler.add_job(
                _run_selfserve_queue_consume,
                trigger=IntervalTrigger(minutes=3),
                id="selfserve_research_queue_consume",
                name="代理自助调研队列兜底消费(每 3 分钟 · 无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 自助调研队列消费已注册(每 3 分钟 · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册自助调研队列消费失败: {e}")

    # [#4 · R 批覆审修复 2026-07-06] 代理自助调研 stale 'pending' 孤儿回收(每 5 分钟)。
    #   同 BUG-004 铁律:进无条件 register_v32_core_tasks,禁进 gated setup_schedule。
    #   兜住 P0-1 freeze→promote 窗口进程被杀致 task 卡 pending(钱冻死)· 超阈值 pending → release+cancel。
    if not scheduler.get_job("selfserve_research_pending_reap"):
        try:
            scheduler.add_job(
                _run_selfserve_pending_reap,
                trigger=IntervalTrigger(minutes=5),
                id="selfserve_research_pending_reap",
                name="代理自助调研 pending 孤儿回收(每 5 分钟 · release+cancel · 无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 自助调研 pending 孤儿回收已注册(每 5 分钟 · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册自助调研 pending 孤儿回收失败: {e}")

    # [B5-1 · Deploy-CTO 集成修复 2026-07-03] 媒体实体效果回流(每周一 05:00)· 幂等 · advisory 锁防双跑
    #   同上:飞轮包原加在 gated 的 setup_schedule → 永不注册。移入无条件 startup 注册。
    if not scheduler.get_job("media_entity_outcome_sync"):
        try:
            scheduler.add_job(
                _run_media_entity_outcome_sync,
                trigger=CronTrigger(day_of_week="mon", hour=5, minute=0, timezone=BEIJING_TZ),
                id="media_entity_outcome_sync",
                name="媒体实体效果回流(每周一 05:00 · outcome_rollup 回写)",
                replace_existing=True,
            )
            logger.info("[Scheduler] media outcome sync 已注册(每周一 05:00 · 无条件)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册 media outcome sync 失败: {e}")

    # ===== [A 段 · 飞轮闭环 2026-07-29] 以下五块全部**无条件注册**,禁再放回 gated setup_schedule =====

    # [A3 根因修复] 媒体推荐池蒸馏 + 投放结果回流。
    #   🔴 真因(生产实证 2026-07-28):这两个 job 此前**只**写在 setup_schedule 里,而
    #   setup_schedule 只有 monitoring_config._global_.auto_monitor_enabled=1 时才被调用 ——
    #   生产该值实测 =0(见 load_saved_schedule),于是两个 job 从来没有被注册过。
    #   media_effective_pool 因此停在 2026-05-11,78 天没刷新;
    #   `sched_job_runs` 里也查不到它们任何记录,所以"静默死"没有任何痕迹。
    #   这是 BUG-004 / P0-G / B1-4 / T5 同一个坑的第五次复发,按同一铁律移入无条件注册根治。
    if not scheduler.get_job("publish_media_effective_pool_distill"):
        try:
            scheduler.add_job(
                _run_media_effective_pool_distill,
                trigger=CronTrigger(day_of_week="mon", hour=3, minute=30, timezone=BEIJING_TZ),
                id="publish_media_effective_pool_distill",
                name="V2.3 媒体推荐池蒸馏 周一 03:30 CST(无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 媒体推荐池蒸馏已注册(周一 03:30 · 无条件 · A3)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册媒体推荐池蒸馏失败: {e}")

    if not scheduler.get_job("publish_outcome_sync"):
        try:
            scheduler.add_job(
                _run_publish_outcome_sync,
                trigger=CronTrigger(hour=4, minute=15, timezone=BEIJING_TZ),
                id="publish_outcome_sync",
                name="V2.3 投放结果回流 每天 04:15 CST(无条件)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 投放结果回流已注册(每天 04:15 · 无条件 · A3)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册投放结果回流失败: {e}")

    # [A3 补首次全量刷新] 池子已经停更 78 天,等下一个周一才刷新等于再瞎 6 天。
    #   注册期查一次新鲜度:超过阈值(默认 14 天)→ 排一次性补跑(启动后 10 分钟,错开启动峰值)。
    #   查询失败(返 None)→ **不补跑**,不拿"查不到"当"很旧"用。
    if not scheduler.get_job("publish_media_effective_pool_catchup"):
        try:
            _stale_days = _pool_staleness_days()
            _stale_threshold = float(os.getenv("FLYWHEEL_POOL_CATCHUP_STALE_DAYS", "14"))
            if _stale_days is not None and _stale_days > _stale_threshold:
                from apscheduler.triggers.date import DateTrigger
                scheduler.add_job(
                    _run_media_effective_pool_distill,
                    trigger=DateTrigger(
                        run_date=datetime.now(BEIJING_TZ) + timedelta(minutes=10)
                    ),
                    id="publish_media_effective_pool_catchup",
                    name="媒体推荐池首次补跑(池已停更 %.0f 天)" % _stale_days,
                    replace_existing=True,
                    # 注册发生在导入期,scheduler 要等 leader 选主成功才 start。
                    # 默认 misfire_grace_time=1s 会把"选主慢于 10 分钟"的这次补跑判成 misfire 丢掉,
                    # 于是又要等到下周一 —— 给 1 小时宽限,选主再慢也能补上。
                    misfire_grace_time=3600,
                )
                logger.warning(
                    "[Scheduler] 媒体推荐池已停更 %.1f 天 · 已排 10 分钟后一次性补跑", _stale_days
                )
            else:
                logger.info("[Scheduler] 媒体推荐池新鲜度 %s · 无需补跑", _stale_days)
        except Exception as e:
            logger.warning(f"[Scheduler] 媒体推荐池补跑排程失败: {e}")

    # [A4] 写作策略指派效果回流(每日 04:50 · 错开 04:20 的版本级回写)。
    if not scheduler.get_job("writing_assignment_outcome_backfill"):
        try:
            scheduler.add_job(
                _run_writing_assignment_outcome_backfill,
                trigger=CronTrigger(hour=4, minute=50, timezone=BEIJING_TZ),
                id="writing_assignment_outcome_backfill",
                name="写作策略指派效果回流 每日 04:50 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] 写作指派效果回流已注册(每日 04:50 · 无条件 · A4)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册写作指派效果回流失败: {e}")

    # [工单 B §4] 引用信号血缘回填(每日 03:40 · 排在 04:20/04:50 两条效果回流之前,
    #   让它们当天读到的 article_id 已经是补齐的)。
    #   BUG-004 铁律:进无条件 register_v32_core_tasks,禁进 gated setup_schedule。
    if not scheduler.get_job("source_signal_lineage_backfill"):
        try:
            scheduler.add_job(
                _run_source_signal_lineage_backfill,
                trigger=CronTrigger(hour=3, minute=40, timezone=BEIJING_TZ),
                id="source_signal_lineage_backfill",
                name="引用信号血缘回填 每日 03:40 CST(幂等 · 只写空值行)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 引用信号血缘回填已注册(每日 03:40 · 无条件 · 工单B §4)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册引用信号血缘回填失败: {e}")

    # [B2] 语料价值标注(每日 05:10 · 错开其它回流 job)。
    if not scheduler.get_job("flywheel_corpus_value_labeling"):
        try:
            scheduler.add_job(
                _run_corpus_value_labeling,
                trigger=CronTrigger(hour=5, minute=10, timezone=BEIJING_TZ),
                id="flywheel_corpus_value_labeling",
                name="语料价值标注 每日 05:10 CST(判断点关时走规则兜底)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 语料价值标注已注册(每日 05:10 · 无条件 · B2)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册语料价值标注失败: {e}")

    # [A2] 飞轮看门狗(每 30 分钟):按各 job 既定周期判"连续 N 个周期未成功"→ 拉 AIOps 告警;
    #   恢复后自动消警。同时检查两道闸门(自动调研开关 / 写作策略 active 版本数)。
    if not scheduler.get_job("flywheel_job_watchdog"):
        try:
            from services.flywheel_heartbeat import run_flywheel_watchdog_job
            scheduler.add_job(
                run_flywheel_watchdog_job,
                trigger=IntervalTrigger(minutes=30),
                id="flywheel_job_watchdog",
                name="飞轮 job 心跳看门狗(每 30 分钟 · 失败/停跑告警)",
                replace_existing=True,
            )
            logger.info("[Scheduler] 飞轮看门狗已注册(每 30min · 无条件 · A2)")
        except Exception as e:
            logger.warning(f"[Scheduler] 注册飞轮看门狗失败: {e}")

    # [A2 判别锁] 必失败探针:FLYWHEEL_HEARTBEAT_SELFTEST=1 时才注册。
    #   用途是在**真实调度路径**上验证"job 失败 → 心跳表 failed 行 + AIOps 告警",
    #   验完把环境变量去掉即可。生产默认不注册,零成本。
    try:
        from services.flywheel_heartbeat import selftest_enabled, selftest_failing_job_wrapped
        if selftest_enabled() and not scheduler.get_job("flywheel_selftest_failing"):
            scheduler.add_job(
                selftest_failing_job_wrapped,
                trigger=IntervalTrigger(minutes=5),
                id="flywheel_selftest_failing",
                name="飞轮心跳判别锁(必失败探针 · 仅 SELFTEST 开启时注册)",
                replace_existing=True,
            )
            logger.warning("[Scheduler] 飞轮心跳判别锁已注册(每 5min 必失败 · 记得验完关掉)")
    except Exception as e:
        logger.warning(f"[Scheduler] 飞轮判别锁注册失败: {e}")

    # AI Ops 每日运维日报 08:10 CST(唯一注册入口 · load_saved_schedule 内)
    # 双重防双跑:get_job guard + ai_ops_reports (report_date, report_type) UNIQUE 幂等。
    if not scheduler.get_job("ai_ops_daily_report"):
        try:
            from services.ai_ops.report_builder import run_daily_report_job
            scheduler.add_job(
                run_daily_report_job,
                trigger=CronTrigger(hour=8, minute=10, timezone=BEIJING_TZ),
                id="ai_ops_daily_report",
                name="AI Ops 每日运维日报 08:10 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] AI Ops 日报已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] AI Ops 日报注册失败: {e}")

    # AI Ops 主动巡逻(包B · 每 5 分钟)· flag ai_ops.patrol.enabled 默认关,job 内静默跳过
    if not scheduler.get_job("ai_ops_patrol"):
        try:
            from services.ai_ops.patrol import run_patrol_job
            scheduler.add_job(
                run_patrol_job,
                trigger=IntervalTrigger(minutes=5),
                id="ai_ops_patrol",
                name="AI Ops 主动巡逻(每 5 分钟)",
                replace_existing=True,
            )
            logger.info("[Scheduler] AI Ops 巡逻已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] AI Ops 巡逻注册失败: {e}")

    # ===== 营销中心(营销军师)定时任务(2026-07-04)· 全部 flag 关时 job 内静默跳过 =====
    # 军师每日巡逻(每天 08:10 · flag marketing_agent.enabled)
    if not scheduler.get_job("marketing_patrol"):
        try:
            from services.marketing.patrol import run_patrol_job as _mkt_patrol_job
            scheduler.add_job(
                _mkt_patrol_job,
                trigger=CronTrigger(hour=8, minute=10, timezone=BEIJING_TZ),
                id="marketing_patrol", name="营销军师每日巡逻", replace_existing=True,
                max_instances=1)
            logger.info("[Scheduler] 营销军师巡逻已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 营销军师巡逻注册失败: {e}")

    # 发放对账补发(每小时 · flag marketing_agent.grant.enabled;dry_run 时也跑对账)
    if not scheduler.get_job("marketing_grant_reconcile"):
        try:
            from apscheduler.triggers.interval import IntervalTrigger as _IT
            from services.marketing.reconcile import reconcile_grants_job as _mkt_reconcile_job
            scheduler.add_job(
                _mkt_reconcile_job, trigger=_IT(hours=1),
                id="marketing_grant_reconcile", name="营销发放对账补发(每小时)",
                replace_existing=True, max_instances=1)
            logger.info("[Scheduler] 营销发放对账已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 营销发放对账注册失败: {e}")

    # 相关转化回测(每天 04:20 · 各案件观察窗口自动跑三指标)
    if not scheduler.get_job("marketing_measurement"):
        try:
            from services.marketing.measurement import run_measurement_job as _mkt_measure_job
            scheduler.add_job(
                _mkt_measure_job,
                trigger=CronTrigger(hour=4, minute=20, timezone=BEIJING_TZ),
                id="marketing_measurement", name="营销相关转化回测(每日)",
                replace_existing=True, max_instances=1)
            logger.info("[Scheduler] 营销回测已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 营销回测注册失败: {e}")

    # 营销周报(每周一 09:00 · Agent 自动写)
    if not scheduler.get_job("marketing_weekly_report"):
        try:
            from services.marketing.measurement import run_weekly_report_job as _mkt_weekly_job
            scheduler.add_job(
                _mkt_weekly_job,
                trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=BEIJING_TZ),
                id="marketing_weekly_report", name="营销周报(每周一)",
                replace_existing=True, max_instances=1)
            logger.info("[Scheduler] 营销周报已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 营销周报注册失败: {e}")

    # 代理试用过期清理
    if not scheduler.get_job("trial_pass_expire"):
        try:
            scheduler.add_job(
                _run_trial_pass_expire,
                trigger=IntervalTrigger(minutes=5),
                id="trial_pass_expire",
                name="代理试用过期清理（每 5 分钟）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 试用过期清理已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 试用过期清理注册失败: {e}")

    # GEO 托管套餐 tick
    if not scheduler.get_job("managed_campaign_tick"):
        try:
            from tools.geo_managed.campaign_tick import managed_campaign_tick
            scheduler.add_job(
                sched_claim("managed_campaign_tick", 21600)(managed_campaign_tick),  # [WORKERS=4] tick claim(6h)
                trigger=IntervalTrigger(hours=6),
                id="managed_campaign_tick",
                name="GEO 托管套餐 tick（每 6h）",
                replace_existing=True,
            )
            logger.info("[Scheduler] GEO 托管 tick 已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] GEO 托管 tick 注册失败: {e}")

    # [写作卡死根治 2026-06-08] 写作卡死自愈(治 user98 写作永久卡死 · 兜请求级释放漏网)
    if not scheduler.get_job("topic_writing_watchdog"):
        try:
            scheduler.add_job(
                _topic_writing_watchdog,
                trigger=IntervalTrigger(minutes=5),
                id="topic_writing_watchdog",
                name="写作卡死防双扣（每 5 分钟标记 90min+ 卡死选题 write_timeout）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 写作卡死自愈已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 写作卡死自愈注册失败: {e}")

    # 智能补足标题的耐久恢复。topic.status='regenerating' 是任务真相源；
    # 进程重启或蓝绿切换后由本任务重新派发，session advisory lock 防双跑。
    if not scheduler.get_job("optimize_title_recovery"):
        try:
            from services.optimize_title_jobs import recover_optimize_title_jobs
            scheduler.add_job(
                recover_optimize_title_jobs,
                trigger=IntervalTrigger(minutes=1),
                id="optimize_title_recovery",
                name="智能补足标题任务恢复（每 1 分钟）",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] 智能补足标题任务恢复已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 智能补足标题任务恢复注册失败: {e}")

    # pending_review 24h 自动发布
    if not scheduler.get_job("managed_pending_review_auto_publish"):
        try:
            from tools.geo_managed.campaign_tick import process_due_pending_reviews
            scheduler.add_job(
                sched_claim("managed_pending_review_auto_publish", 1800)(process_due_pending_reviews),  # [WORKERS=4] tick claim(30min)
                trigger=IntervalTrigger(minutes=30),
                id="managed_pending_review_auto_publish",
                name="GEO 托管半自动 24h 自动发（每 30min）",
                replace_existing=True,
            )
            logger.info("[Scheduler] pending_review 自动发已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] pending_review 自动发注册失败: {e}")

    # 12 个月休眠扫描
    if not scheduler.get_job("managed_dormancy_scan"):
        try:
            from tools.geo_managed.campaign_tick import dormancy_scan
            scheduler.add_job(
                sched_claim("managed_dormancy_scan", 86400)(dormancy_scan),  # [FF5] tick claim(audit NEEDS_CLAIM)
                trigger=CronTrigger(hour=3, minute=0, timezone=BEIJING_TZ),
                id="managed_dormancy_scan",
                name="GEO 托管 12 个月休眠扫描 03:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] dormancy_scan 已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] dormancy_scan 注册失败: {e}")

    # v3.4 周复盘引擎
    if not scheduler.get_job("managed_weekly_review"):
        try:
            from tools.geo_managed.campaign_tick import weekly_review_brand_packages
            scheduler.add_job(
                weekly_review_brand_packages,
                trigger=CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=BEIJING_TZ),
                id="managed_weekly_review",
                name="GEO 托管 v3.4 周复盘 周一 04:00 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] weekly_review 已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] weekly_review 注册失败: {e}")

    # 深度行业解析僵尸任务清理
    if not scheduler.get_job("industry_brief_watchdog"):
        try:
            scheduler.add_job(
                _industry_brief_watchdog,
                trigger=IntervalTrigger(minutes=5),
                id="industry_brief_watchdog",
                name="深度解析僵尸任务清理（每 5 分钟）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 深度解析看门狗已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 深度解析看门狗注册失败: {e}")

    # Phase 4 PLAN 03 · geo_plan_tasks 僵尸清理 (每 1 分钟) · D8 维度
    if not scheduler.get_job("geo_plan_task_zombie_killer"):
        try:
            scheduler.add_job(
                _zombie_killer_job,
                trigger=IntervalTrigger(minutes=1),
                id="geo_plan_task_zombie_killer",
                name="GEO 方案任务僵尸清理（每 1 分钟）",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] geo_plan_task_zombie_killer 已注册（每 1min）")
        except Exception as e:
            logger.warning(f"[Scheduler] geo_plan_task_zombie_killer 注册失败: {e}")

    # [v5 req1] geo_plan_tasks 结算/退款补偿队列 reconcile (每 1 分钟)
    if not scheduler.get_job("geo_plan_task_settle_reconcile"):
        try:
            scheduler.add_job(
                _geo_plan_reconcile_job,
                trigger=IntervalTrigger(minutes=1),
                id="geo_plan_task_settle_reconcile",
                name="GEO 方案任务结算/退款补偿队列 reconcile（每 1 分钟）",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] geo_plan_task_settle_reconcile 已注册（每 1min）")
        except Exception as e:
            logger.warning(f"[Scheduler] geo_plan_task_settle_reconcile 注册失败: {e}")

    # [v6 req3] fund_recovery_orders 处理器 (每 1 分钟)
    if not scheduler.get_job("fund_recovery_processor"):
        try:
            scheduler.add_job(
                _fund_recovery_processor_job,
                trigger=IntervalTrigger(minutes=1),
                id="fund_recovery_processor",
                name="资金补偿工单处理器（每 1 分钟）",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] fund_recovery_processor 已注册（每 1min）")
        except Exception as e:
            logger.warning(f"[Scheduler] fund_recovery_processor 注册失败: {e}")

    if not scheduler.get_job("billing_debt_offset_processor"):
        try:
            scheduler.add_job(
                _billing_debt_offset_processor_job,
                trigger=IntervalTrigger(minutes=1),
                id="billing_debt_offset_processor",
                name="扣费债务推进耐久 outbox（每 1 分钟）",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] billing_debt_offset_processor 已注册（每 1min）")
        except Exception as e:
            logger.warning(f"[Scheduler] billing_debt_offset_processor 注册失败: {e}")

    # Phase 4 PLAN 03 · geo_plan_tasks 90 天归档 (每日凌晨 3 点) · D15 维度
    if not scheduler.get_job("geo_plan_task_cleanup_archiver"):
        try:
            scheduler.add_job(
                _archive_job,
                trigger=CronTrigger(hour=3, minute=0, timezone=BEIJING_TZ),
                id="geo_plan_task_cleanup_archiver",
                name="GEO 方案任务 90 天归档 03:00 CST",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] geo_plan_task_cleanup_archiver 已注册（每日 03:00）")
        except Exception as e:
            logger.warning(f"[Scheduler] geo_plan_task_cleanup_archiver 注册失败: {e}")

    # CTO-C 2026-04-26 · m3_customer_events 365 天清理(每天 03:15 CST · 错开 03:00/03:30)
    if not scheduler.get_job("m3_customer_events_cleanup"):
        try:
            def _run_m3_events_cleanup():
                try:
                    from db.m3_events_db import cleanup_expired
                    n = cleanup_expired()
                    logger.info(f"[Scheduler] m3_customer_events cleanup 删除 {n} 行")
                except Exception as ex:
                    logger.warning(f"[Scheduler] m3_customer_events cleanup 失败: {ex}")

            scheduler.add_job(
                _run_m3_events_cleanup,
                trigger=CronTrigger(hour=3, minute=15, timezone=BEIJING_TZ),
                id="m3_customer_events_cleanup",
                name="M3 客户行为事件 365 天清理 03:15 CST",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("[Scheduler] m3_customer_events_cleanup 已注册（每日 03:15）")
        except Exception as e:
            logger.warning(f"[Scheduler] m3_customer_events_cleanup 注册失败: {e}")

    # v1.1 代理申请材料 90 天清理（每天 03:30 CST，错开 dormancy_scan 03:00）
    if not scheduler.get_job("partner_kyc_cleanup"):
        try:
            scheduler.add_job(
                _run_partner_kyc_cleanup,
                trigger=CronTrigger(hour=3, minute=30, timezone=BEIJING_TZ),
                id="partner_kyc_cleanup",
                name="代理申请材料 90 天清理 03:30 CST",
                replace_existing=True,
            )
            logger.info("[Scheduler] KYC 90 天清理已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] KYC 清理注册失败: {e}")

    # [WORKERS=4 · SPEC §7] 控制面:命令队列消费(10s)+ 状态上报(30s)· 随 leader start 才触发
    if not scheduler.get_job("sched_control_consume"):
        try:
            from services.sched_control import consume_commands_once
            scheduler.add_job(
                consume_commands_once,
                trigger=IntervalTrigger(seconds=10),
                id="sched_control_consume",
                name="调度控制面:消费手动触发命令队列（每 10s · leader）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 控制面命令消费已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 控制面命令消费注册失败: {e}")

    if not scheduler.get_job("sched_control_status"):
        try:
            from services.sched_control import report_status_once
            scheduler.add_job(
                report_status_once,
                trigger=IntervalTrigger(seconds=30),
                id="sched_control_status",
                name="调度控制面:状态上报 sched:status（每 30s · leader）",
                replace_existing=True,
            )
            logger.info("[Scheduler] 控制面状态上报已注册")
        except Exception as e:
            logger.warning(f"[Scheduler] 控制面状态上报注册失败: {e}")

    # GEO 统一观测:治理晋升/来源补登记/聚合只接 ROLE=cron 的同一控制面。
    # 默认 bridge 模式不注册 AI-1 采集 job；仅显式 native_sampling_driver
    # 模式才注册 native jobs，避免与现役付费诊断/监测/调研双调用、双扣费。
    from services.geo_observation.integration import register_geo_observation_integration_jobs

    register_geo_observation_integration_jobs(scheduler)


def load_saved_schedule():
    """
    启动时加载数据库中已保存的定时配置
    在 server.py startup 事件中调用
    """
    # ⚠️ 先注册 v3.2/v3.3 核心任务（与监测开关解耦，必跑）
    try:
        register_v32_core_tasks()
    except Exception as e:
        logger.error(f"[Scheduler] 核心任务注册异常: {e}", exc_info=True)

    # 监测定时（受 auto_monitor_enabled 控制）
    try:
        from db.monitoring_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT auto_monitor_enabled, auto_monitor_cron FROM monitoring_config WHERE client_id = '_global_'"
            )
            row = cur.fetchone()
            conn.close()

            if row and row["auto_monitor_enabled"] == 1 and row["auto_monitor_cron"]:
                cron = row["auto_monitor_cron"]  # "30 16 * * *"
                parts = cron.split()
                if len(parts) >= 2:
                    minute, hour = int(parts[0]), int(parts[1])
                    setup_schedule(hour, minute, enabled=True)
                    logger.info(f"[Scheduler] 从数据库恢复定时监测: {hour:02d}:{minute:02d}")
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"[Scheduler] 加载定时监测配置失败: {e}")

    # 外部发布通道定时任务
    _setup_mhz_jobs()


def _setup_mhz_jobs():
    """外部发布通道相关定时任务"""
    import asyncio

    async def _mhz_status_sync():
        """开源版:订单状态从已接入的发布渠道回流(services/publish_channels/status.py);没接入时不做事。"""
        try:
            from services.publish_channels.status import sync_once
            await sync_once()
        except Exception as e:
            logger.error(f"[Scheduler] 发布渠道状态回流失败: {e}")

    async def _mhz_toutiao_order_sync():
        """开源版:发布渠道未接入,本任务不做事。"""
        return

    async def _mhz_media_sync():
        try:
            from api.meijiehezi_api import _get_client, _map_media_to_db
            from db.meijiehezi_db import set_config, save_media_batch, get_all_media_ids, deactivate_media
            from services.catalog_snapshot_guard import screen_stale_for_deactivation
            from datetime import datetime
            client = _get_client()
            all_items = await client.get_all_media_raw()
            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] local_ids 现在默认按 provider 归口。
            #   修前它是全供应商在售集合 —— 另一家供应商的 34,747 条被算进 stale,
            #   把下面的阈值永久撑破,本 job 的下架分支自 2026-07-27 起一次都没执行过
            #   (实测 last_media_sync_result 恒「下架0」)。守卫看着在工作,其实是死的。
            local_ids = get_all_media_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_media_to_db(item) for item in all_items]
            save_media_batch(records)
            # [GEO-R6-CAN-016] 快照完整性守卫:get_all_media_raw 分页遇空页/短页会提前终止,
            #   返回部分甚至空快照;若据此 deactivate,极端情况(空首页/session 过期)会误下架整个媒体库。
            #   判据本体已收进 services/catalog_snapshot_guard(原来三个 job 各抄一份 +
            #   admin 端点一份都没有;阈值口径逐字沿用,不重新发明)。
            _verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="媒体同步", logger=logger)
            stale = _verdict.stale
            if stale:
                deactivate_media(list(stale))
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            set_config("last_media_sync", datetime.now().isoformat())
            set_config("last_media_sync_result",
                       f"共{len(all_items)}家,新增{added},更新{len(all_items)-added},{_verdict.summary()}")
        except Exception as e:
            logger.error(f"[Scheduler] 外部发布通道媒体同步失败: {e}")

    async def _mhz_wemedia_sync():
        try:
            from api.meijiehezi_api import _get_client, _map_wemedia_to_db
            from db.meijiehezi_db import set_config, save_wemedia_batch, get_all_wemedia_ids, deactivate_wemedia
            from services.catalog_snapshot_guard import screen_stale_for_deactivation
            from datetime import datetime
            client = _get_client()
            all_items = await client.get_all_wemedia_raw()
            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 归口同 _mhz_media_sync
            #   (本条污染面更大:97,982 条 · 实测 last_wemedia_sync_result 同样恒「下架0」)。
            local_ids = get_all_wemedia_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_wemedia_to_db(item) for item in all_items]
            save_wemedia_batch(records)
            # [GEO-R6-CAN-016] 快照完整性守卫(同 _mhz_media_sync):空/部分快照禁止 mass-deactivate。
            _verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="自媒体同步", logger=logger)
            stale = _verdict.stale
            if stale:
                deactivate_wemedia(list(stale))
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            set_config("last_wemedia_sync", datetime.now().isoformat())
            set_config("last_wemedia_sync_result",
                       f"共{len(all_items)}家,新增{added},更新{len(all_items)-added},{_verdict.summary()}")
        except Exception as e:
            logger.error(f"[Scheduler] 外部发布通道自媒体同步失败: {e}")

    async def _mhz_short_video_sync():
        """[svideo lane · 2026-07-04] 短视频资源池同步（只读拉取，安全）。mirror _mhz_wemedia_sync。"""
        try:
            from api.meijiehezi_api import _get_client, _map_short_video_to_db
            from db.meijiehezi_db import set_config, save_short_video_batch, get_all_short_video_ids, deactivate_short_video
            from services.catalog_snapshot_guard import screen_stale_for_deactivation
            from datetime import datetime
            client = _get_client()
            all_items = await client.get_all_short_video_raw()
            # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10] 归口同 _mhz_media_sync。短视频板块
            #   目前只有一家供货 → 归口前后结果相同;正因如此它是本次修复的**反向对照**:
            #   同一段守卫代码在这里从未被撑破(实测 last_short_video_sync_result「下架23」)。
            local_ids = get_all_short_video_ids()
            remote_ids = {item["id"] for item in all_items}
            records = [_map_short_video_to_db(item) for item in all_items]
            save_short_video_batch(records)
            # [GEO-R6-CAN-016] 快照完整性守卫(同 _mhz_media_sync):空/部分快照禁止 mass-deactivate。
            _verdict = screen_stale_for_deactivation(
                local_ids=local_ids, remote_ids=remote_ids,
                label="短视频同步", logger=logger)
            stale = _verdict.stale
            if stale:
                deactivate_short_video(list(stale))
            added = sum(1 for item in all_items if item["id"] not in local_ids)
            set_config("last_short_video_sync", datetime.now().isoformat())
            set_config("last_short_video_sync_result",
                       f"共{len(all_items)}个,新增{added},更新{len(all_items)-added},{_verdict.summary()}")
        except Exception as e:
            logger.error(f"[Scheduler] 短视频资源同步失败: {e}")

    async def _kyb_media_catalog_sync():
        """[WO_KYB_CATALOG_GOVERNANCE 2026-08-10 · A.2] 新渠道**目录**同步(每天 6:00/18:00)。

        修前:`sync_from_api` / `sync_catalog` 在 /app/api 下**零调用方** —— 目录是一次性
        手工导入的,所以出现过「停摆 13 天没人发现」(2026-07-28 → 08-10)。目录价
        `our_price_yuan = price × markup` 只在同步时重算,停摆 = 拿两周前的成本价在卖。

        与媒介盒子三个目录 job 同频(Owner 2026-08-10 拍板日更,6/18 两跑是日更的超集)。
        留痕键与 `last_media_sync` / `last_kyb_status_sync` 同一套,便于一眼看出谁停摆。

        下架由 `sync_catalog` 内的部分快照守卫兜(本 job 不传 deactivate_missing=False:
        守卫已上线,正常下架是这条链要恢复的能力,不是要关掉的)。
        """
        try:
            from services.kuaiyibo.config import is_configured as _kyb_configured

            if not _kyb_configured():
                return
            from db.meijiehezi_db import set_config
            from services.kuaiyibo.media_sync import sync_from_api
            from datetime import datetime

            _res = await asyncio.to_thread(sync_from_api)
            set_config("last_kyb_media_sync", datetime.now().isoformat())
            set_config("last_kyb_media_sync_result", _res.summary())
            logger.info("[Scheduler] 新渠道目录同步完成: %s", _res.summary())
        except Exception as e:
            # 失败也要落留痕:只有 last_kyb_media_sync(时间)不动、result 记失败,
            # 才能把「同步失败」与「根本没跑」区分开 —— 停摆 13 天正是因为两者同形。
            logger.error(f"[Scheduler] 新渠道目录同步失败: {e}")
            try:
                from db.meijiehezi_db import set_config as _sc
                _sc("last_kyb_media_sync_result", f"失败: {e}"[:480])
            except Exception:
                pass

    async def _mhz_short_video_order_sync():
        """开源版:发布渠道未接入,本任务不做事。"""
        return

    async def _mhz_svideo_stuck_refund_sweep():
        """[svideo lane · Codex 复审 2026-07-04] 短视频卡单兜底退款——无 1 小时上限。

        主 retry 扫描(_retry_stuck_publish_orders)只看 [10min, 1h) 窗口，且短视频跳过
        软文重试、只靠其中 35 分钟超时分支退款；若 scheduler 停摆/阻塞超 1 小时，
        pending 短视频会漏出窗口【永不退款】。本 job 专扫短视频 pending 无 sn 超 35 分钟
        (不设上限) → 标 failed + 按 item 退款。
        安全性：退款幂等键 item:{id} 与主分支同源，双跑不双退；同标题 10 分钟内已同步到
        订单表的跳过(可能已实际到达发布平台，交给订单同步/人工，不盲退)。
        """
        from db.connection import get_connection
        from db.meijiehezi_db import refund_for_publish_order, update_order_item_status
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT i.id AS item_id, i.cost_points, o.id AS order_id, o.user_id
                FROM mhz_publish_order_items i
                JOIN mhz_publish_orders o ON o.id = i.order_id
                WHERE o.article_id < 0
                  AND i.media_type = 'svideo'
                  AND i.status = 'pending'
                  -- 🔴 [第 8 棒 · R8 P0] 只扫**老口径**的项,和 4185/4941 两个兄弟一致。
                  --    图文合同链的项 **也会停在 `pending`**:`_CLAIM_PUBLISH_ITEM` 故意把
                  --    status 写成 `LEGACY_SUBMITTER_CLAIM_STATUS`(='pending'),
                  --    好让旧提交器领得到。于是本 sweeper 的四个条件它全中
                  --    (article_id<0 / media_type='svideo' / pending / 无单号),
                  --    35 分钟后就会把一条**钱还冻着**的合同项强标 failed,
                  --    并按老口径 `refund_for_publish_order` 退一笔**从来没扣过**的钱
                  --    —— 幽灵退款 + 抢在 worker 前面改终态,两笔账同时错。
                  --    显式列出老值(NULL 与 deduct_upfront):`<> 新值` 对 NULL 恒 UNKNOWN,
                  --    会把所有老行一起漏掉。
                  AND (i.billing_mode IS NULL OR i.billing_mode = 'deduct_upfront')
                  AND i.mhz_order_id IS NULL
                  AND o.created_at < NOW() - INTERVAL '35 minutes'
                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s
                      WHERE s.user_id = o.user_id
                        AND s.title = o.article_title
                        AND s.synced_at > NOW() - INTERVAL '10 minutes'
                  )
                LIMIT 50
            """)
            rows = c.fetchall()
            conn.close()
            if not rows:
                return
            refunded_total = 0
            for r in rows:
                update_order_item_status(r["item_id"], "failed", reject_reason="超时未发出，费用已自动退回")
                _cost = int(r.get("cost_points") or 0)
                if _cost > 0:
                    res = refund_for_publish_order(
                        user_id=r["user_id"], amount=_cost,
                        refund_key=f"item:{r['item_id']}",
                        reason=f"短视频卡单超时兜底退款 (order_id={r['order_id']})",
                    )
                    if res.get("success") and not res.get("skipped"):
                        refunded_total += _cost
            logger.warning(f"[Scheduler] 短视频卡单兜底退款: items={len(rows)}, refunded={refunded_total}pts")
        except Exception as e:
            logger.error(f"[Scheduler] 短视频卡单兜底退款失败: {e}")

    async def _mhz_orphan_zombie_sweep():
        """[WO-PUB-ZOMBIE-2026-08-04] 软文/自媒体「僵尸孤儿单」兜底清扫。

        既有出口全都够不着这批单:
          · `_mhz_status_sync` 靠 ``mhz_order_id`` 回写状态 —— 单号为空的它永远够不着;
          · `_retry_stuck_publish_orders` 的窗口是「创建后 10 分钟 ~ 1 小时」——
            出了窗口就再也不会被捡起(生产实测 12 条卡了 4~98 天);
          · `_mhz_svideo_stuck_refund_sweep` 只管短视频;
          · 快易播那条链 `refund_stale_orders` 只管快易播。
        于是这批单既没标失败也没退款,永久停在 pending,还被去重判据当成"进行中"
        把那篇文章对那家媒体锁死。本 job 就是那个缺失的出口。

        🔴 退款前必过外部镜像三分支(见 `services.publish_orphan_settlement`):
        对方查不到才退;对方查到且没被任何本地条目认领的**禁止自动退款**,转人工。
        """
        try:
            from services.publish_orphan_settlement import sweep_orphan_publish_items

            res = await asyncio.to_thread(sweep_orphan_publish_items)
            if res.scanned or res.skipped_reason:
                from db.meijiehezi_db import set_config as _set_cfg
                from datetime import datetime as _dt
                _set_cfg("last_orphan_zombie_sweep", _dt.now().isoformat())
                _set_cfg("last_orphan_zombie_sweep_result", res.summary())
                logger.warning(f"[Scheduler] 僵尸孤儿单清扫: {res.summary()}")
        except Exception as e:
            logger.error(f"[Scheduler] 僵尸孤儿单清扫失败: {e}")

    async def _mhz_session_keepalive():
        """[2026-04-30 增强] mhz session 失效时发最高级别站内通知给所有管理员
        + 记录失效时长（连续失效 N 次后升级提醒），防止失效期间所有用户代发都失败但管理员不知道。
        """
        from db.meijiehezi_db import get_config, set_config
        try:
            from api.meijiehezi_api import _get_client
            client = _get_client()
            ok = await client.check_session()
        except Exception as e:
            logger.warning(f"[Scheduler] MHZ Session 保活异常: {e}")
            ok = False

        if ok:
            # 健康 → 清除失效标记
            try:
                failed_since = get_config("mhz_session_failed_since") or ""
                if failed_since:
                    from datetime import datetime, timezone
                    from services.notification_events import NotificationEventType
                    from services.notification_outbox import enqueue_admin_notification_events_durable

                    enqueue_admin_notification_events_durable(
                        event_type=NotificationEventType.EXTERNAL_CHANNEL_RECOVERED,
                        business_id=f"mhz-session:{failed_since}",
                        terminal_state="recovered",
                        facts={
                            "business_no": "CHANNEL-MHZ",
                            "status": "外部发布通道已恢复",
                            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "summary": "排队任务将按既有调度规则继续处理。",
                        },
                    )
                set_config("mhz_session_failed_since", "")
                set_config("mhz_session_failed_count", "0")
            except Exception as exc:
                logger.exception("[Scheduler] 外部发布通道恢复 outbox 写入失败: %s", exc)
            return

        # 失效 → 记失效时间 + 计数 + 通知管理员
        logger.warning("[Scheduler] MHZ Session 保活失败，所有代发请求会失败，需要管理员立即重新登录外部发布通道后台")
        try:
            from datetime import datetime, timezone
            from services.notification_events import NotificationEventType
            from services.notification_outbox import enqueue_admin_notification_events_durable

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            failed_since = get_config("mhz_session_failed_since") or ""
            failed_count = int(get_config("mhz_session_failed_count") or "0")

            if not failed_since:
                # 首次失效
                failed_since = now_iso
                set_config("mhz_session_failed_since", now_iso)
                set_config("mhz_session_failed_count", "1")
            else:
                new_count = failed_count + 1
                set_config("mhz_session_failed_count", str(new_count))
            enqueue_admin_notification_events_durable(
                event_type=NotificationEventType.EXTERNAL_CHANNEL_FAILED,
                business_id=f"mhz-session:{failed_since}",
                terminal_state="failed",
                facts={
                    "business_no": "CHANNEL-MHZ",
                    "status": "外部发布通道不可用",
                    "occurred_at": now_iso,
                    "summary": "发布任务已暂停或等待恢复，请在运维页面处理通道状态。",
                },
            )
        except Exception as ne:
            logger.exception("[Scheduler] 外部发布通道失效 outbox 写入失败: %s", ne)

    async def _retry_stuck_publish_orders():
        """扫描卡在 pending 且无 mhz_order_id 的订单，重试或超时放弃。

        [防重复提交守卫]：
        1. 单 item 重试间隔 10 分钟（last_submit_at 守卫，防止 5 分钟扫一次过密）
        2. 单 item 最多重试 3 次（submit_attempts 守卫）
        3. 同标题最近 10 分钟内已有提交记录的不再重试（防 mhz 端重复）
        4. 重试也走 try_lock_for_submit 拿锁（和首次提交共享一把锁）
        """
        from db.connection import get_connection
        from db.meijiehezi_db import (
            get_order_items_by_order, update_order_item_submitted,
            update_order_item_status, try_lock_for_submit, release_submit_lock,
            set_item_submission_snapshot,
        )
        try:
            conn = get_connection()
            c = conn.cursor()
            # 守卫加固：
            # - last_submit_at 为空（从未提交过）OR 距上次 >= 10 分钟（避开提交中窗口）
            # - submit_attempts < 3 防无限重试
            # - 同 user_id + article_title 最近 10 分钟无提交到 mhz 的记录（防同标题重复）
            # - 创建时间在 [10分钟, 1小时] 区间（早于 10 分钟才考虑重试，超 1 小时放弃）
            c.execute("""
                SELECT DISTINCT o.id AS order_id, o.user_id, o.article_id,
                       o.article_title, o.article_content_snapshot,
                       o.total_cost_points, o.created_at
                FROM mhz_publish_orders o
                JOIN mhz_publish_order_items i ON i.order_id = o.id
                WHERE i.status = 'pending'
                  -- [返工 2026-08-18] 合同链的项是 `queued`,由 contract_worker 提交;
                  -- 这条卡单重试是**老链**的兜底。两条链都来重投同一个 order
                  -- = 重复外调。显式把新链排除掉。
                  AND (i.billing_mode IS NULL OR i.billing_mode = 'deduct_upfront')
                  AND i.mhz_order_id IS NULL
                  AND COALESCE(i.submit_attempts, 0) < 3
                  AND (i.last_submit_at IS NULL OR i.last_submit_at < NOW() - INTERVAL '10 minutes')
                  AND o.created_at < NOW() - INTERVAL '10 minutes'
                  AND o.created_at > NOW() - INTERVAL '1 hour'
                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s
                      WHERE s.user_id = o.user_id
                        AND s.title = o.article_title
                        AND s.synced_at > NOW() - INTERVAL '10 minutes'
                  )
                ORDER BY o.created_at
                LIMIT 5
            """)
            stuck = c.fetchall()
            conn.close()

            if not stuck:
                return

            logger.info(f"[Scheduler] 发现 {len(stuck)} 个卡住的 pending 订单")
            from api.meijiehezi_api import _get_client, _mhz_api_lock
            from db.meijiehezi_db import refund_for_publish_order
            from datetime import datetime, timedelta
            # [P1-G] 超时从 1h → 35min
            # 我们已经有 attempts<3 + 10min 间隔守卫，最多 30 分钟内 3 次重试完
            # 加 5 分钟缓冲让最后一次重试有时间完成 = 35 分钟
            max_retry_age = timedelta(minutes=35)

            for order in stuck:
                order_id = order["order_id"]
                user_id = order["user_id"]
                order_age = datetime.now() - order["created_at"] if order["created_at"] else timedelta(hours=99)

                # 超过 35 分钟：放弃重试，标记 failed + 按 item 退款
                if order_age > max_retry_age:
                    logger.warning(f"[Scheduler] 订单 {order_id} 超过 {max_retry_age.total_seconds()//60:.0f} 分钟未提交，放弃重试并退款")
                    items = get_order_items_by_order(order_id)
                    # [统一退款键 item:{id}] 和 release_submit_lock 用同一种幂等键
                    # 防止 scheduler 全单退 + release_submit_lock 按项退导致双重退款
                    total_refunded = 0
                    refund_count = 0
                    for it in items:
                        if it["status"] == "pending" and not it.get("mhz_order_id"):
                            update_order_item_status(it["id"], "failed", reject_reason="超时未提交到外部发布通道，已自动退款")
                            item_cost = int(it.get("cost_points") or 0)
                            if item_cost > 0:
                                result = refund_for_publish_order(
                                    user_id=user_id,
                                    amount=item_cost,
                                    refund_key=f"item:{it['id']}",
                                    reason=f"卡单超时退款 (order_id={order_id})",
                                )
                                if result.get("success") and not result.get("skipped"):
                                    total_refunded += item_cost
                                    refund_count += 1
                    if refund_count > 0:
                        logger.info(f"[Scheduler] 卡单超时退款: user={user_id}, order={order_id}, items={refund_count}, total={total_refunded}pts")
                    continue

                # [svideo lane · 2026-07-04 资金守卫] 短视频订单不走软文重试路径。
                #   短视频 article_id 是负数(视频稿占位命名空间)，正文不在 articles 表——
                #   若放行到下面"文章内容为空"分支，item 会被标 failed 且【不退款】，
                #   且 failed 后上面 35 分钟超时退款分支再也看不到 → 用户钱永久丢失。
                #   跳过重试，留给上面的 35 分钟超时分支统一 failed+按 item 退款（media_type 无关，安全）。
                if (order.get("article_id") or 0) < 0:
                    logger.info(f"[Scheduler] 订单 {order_id} 为短视频(article_id<0)，跳过软文重试，等 35 分钟超时分支处理")
                    continue

                # 2 分钟 ~ 1 小时：尝试重试
                content_md = None
                _retry_brand_id = None
                try:
                    conn2 = get_connection()
                    c2 = conn2.cursor()
                    c2.execute(
                        """SELECT a.content, q.brand_id
                             FROM articles a
                             LEFT JOIN quotes q ON q.id=a.quote_id
                            WHERE a.id = %s""",
                        (order["article_id"],),
                    )
                    row = c2.fetchone()
                    conn2.close()
                    if order.get("article_content_snapshot") is not None:
                        content_md = order.get("article_content_snapshot")
                    elif row and row["content"]:
                        content_md = row["content"]
                    _retry_brand_id = row.get("brand_id") if row else None
                except Exception:
                    pass

                # [P0 代发内容漂移] 重试前先判漂移。
                #
                # 旧行为:漂移了照样拿**旧快照**重投 —— 保证失败的空转,转满 3 次后
                # 进"重试耗尽 → 退款",用户白等一场还要重新下单。
                # 现行为:确诊漂移即**停止自动重试**,把 item 挂起(软失败,不退款),
                # 并写下带 actions 的 §13 合同,让用户点「重新准备并审核」自己走出去。
                try:
                    from services.publication_content_drift import (
                        build_drift_contract, detect_order_content_drift,
                    )
                    from db.meijiehezi_db import suspend_item_for_user_action

                    _dconn = get_connection()
                    try:
                        _dc = _dconn.cursor()
                        _verdict = detect_order_content_drift(_dc, order)
                        _dconn.commit()
                    finally:
                        _dconn.close()

                    if _verdict.drifted:
                        _contract = build_drift_contract(_verdict)
                        _suspended = 0
                        for _it in get_order_items_by_order(order_id):
                            if _it.get("status") in ("pending", "submitting"):
                                if suspend_item_for_user_action(_it["id"], _contract):
                                    _suspended += 1
                        logger.warning(
                            "[Scheduler] 订单 %s 内容漂移(origin=%s),已挂起 %s 个 item 等用户确认,"
                            "不重试、不退款",
                            order_id, _verdict.origin, _suspended,
                        )
                        continue
                except Exception as _drift_err:
                    # 判不出来就按原路走 —— 漂移检测本身不该成为新的阻断源
                    logger.warning("[Scheduler] 订单 %s 漂移检测异常,按原路继续: %s", order_id, _drift_err)

                if not content_md:
                    logger.warning(f"[Scheduler] 重试订单 {order_id} 失败：文章内容为空，标记 failed")
                    items = get_order_items_by_order(order_id)
                    for it in items:
                        if it["status"] == "pending" and not it.get("mhz_order_id"):
                            update_order_item_status(it["id"], "failed", reject_reason="重试失败：文章内容为空")
                    continue

                items = get_order_items_by_order(order_id)

                # [防重复] 重试也走乐观锁：拿到锁的 item 才会真正提交
                # 这是和首次提交共享的一把锁，杜绝 API 调用 + scheduler 并发重复
                # [svideo lane · 2026-07-04] 软文桶显式排除 svideo（不再用 != wemedia 兜底），
                #   防短视频 item 万一进重试被当软文发到 /index/article。短视频重投由其独立链处理，
                #   这里保持 pending 不误发（正常 svideo 下单总闸关时不会有此类 item）。
                mhz_items = [it for it in items
                             if it.get("status") == "pending"
                             and not it.get("mhz_order_id")
                             and (it.get("media_type") or "mhz") not in ("wemedia", "svideo")
                             and try_lock_for_submit(it["id"])]
                wm_items = [it for it in items
                            if it.get("status") == "pending"
                            and not it.get("mhz_order_id")
                            and it.get("media_type") == "wemedia"
                            and try_lock_for_submit(it["id"])]

                if not mhz_items and not wm_items:
                    logger.info(f"[Scheduler] 订单 {order_id} 所有 item 都拿不到锁（已在提交中或超过重试上限），跳过")
                    continue

                # [Dedup] 调度器去重：同文章同媒体已有活跃订单时取消 + 退款，不再向 mhz 发
                # 解决：scheduler 重试多条本地 pending 订单时每次都给 mhz 创建新订单的问题
                from db.meijiehezi_db import (
                    check_duplicate_submission, mark_item_failed_duplicate,
                    check_recent_active_for_user_media,
                    find_synced_order_by_signature,
                    save_mhz_raw_response,
                )

                def _scheduler_dedup_filter(items_list, label):
                    """[防线 1+2] 调度器重试前的双层去重"""
                    cleaned = []
                    for _it in items_list:
                        # 防线 1a: 排除自身的 dedup（找到其他 item）
                        _conflict = check_duplicate_submission(order["article_id"], _it["media_id"], _it["id"])
                        # 防线 1b: 30 分钟时间窗（兜底，万一 1a 漏）
                        if _conflict is None:
                            _conflict = check_recent_active_for_user_media(
                                user_id=order["user_id"],
                                article_id=order["article_id"],
                                media_id=_it["media_id"],
                                window_minutes=30,
                                exclude_item_id=_it["id"],
                            )
                        if _conflict:
                            logger.warning(f"[Scheduler Dedup-{label}] item={_it['id']} media={_it['media_id']} 已有活跃订单 item={_conflict}，取消并退款")
                            mark_item_failed_duplicate(_it["id"])
                            _cost = int(_it.get("cost_points") or 0)
                            if _cost > 0:
                                refund_for_publish_order(order["user_id"], _cost, f"item:{_it['id']}", f"重复投稿：调度器去重自动退款（{label}）")
                            continue

                        # 防线 2: 跨表反查 mhz_synced_orders
                        # 重试前先确认 mhz 那边是不是已经有这单了（订单 created_at 之后的时间窗）
                        # 找到 → 直接回填 mhz_order_id 跳过本次重试，避免给 mhz 又发一单
                        try:
                            _existing_sn = find_synced_order_by_signature(
                                user_id=order["user_id"],
                                article_title=order["article_title"],
                                resource_id=_it["media_id"],
                                submit_after=order.get("created_at"),
                                window_minutes=60,  # 比首次提交宽一点，覆盖多次同步延迟
                            )
                        except Exception as _qerr:
                            logger.warning(f"[Scheduler 防线2] item={_it['id']} 反查 mhz_synced_orders 异常: {_qerr}")
                            _existing_sn = None
                        if _existing_sn:
                            logger.warning(
                                f"[Scheduler 防线2] item={_it['id']} 在 mhz_synced_orders 反查到已有订单 sn={_existing_sn}，"
                                f"回填并跳过重试（避免给 mhz 重复创建订单）"
                            )
                            update_order_item_submitted(_it["id"], _existing_sn)  # WO-PUB-ZOMBIE-sn-nonempty: 上面 if _existing_sn 已保证非空
                            continue

                        cleaned.append(_it)
                    return cleaned

                mhz_items = _scheduler_dedup_filter(mhz_items, "mhz")
                wm_items = _scheduler_dedup_filter(wm_items, "wemedia")
                if not mhz_items and not wm_items:
                    logger.warning(f"[Scheduler Dedup] order_id={order_id} 所有 item 均重复或已被 mhz 接单，跳过 mhz 提交")
                    continue

                mhz_ids = [it["media_id"] for it in mhz_items]
                wm_ids = [it["media_id"] for it in wm_items]

                async with _mhz_api_lock:
                    # 复用 api/meijiehezi_api.py 的 confirms helper
                    try:
                        from api.meijiehezi_api import _build_confirms_from_items as _build_confirms
                    except Exception:
                        _build_confirms = lambda items: {}  # noqa: E731  # 兜底
                    # [WO-KYB-ROUTING D1] 分渠道投递三件套 + 领单号共用件。
                    # 🔴 这几个是**必需**的,不给兜底 lambda:拿不到就宁可整轮重试失败,
                    #    也不能悄悄退回"不分流"——那正是快易播 15 单全灭的原因。
                    from services.kuaiyibo.routing import (
                        apply_provider_preference, build_provider_kwargs_extras,
                        clear_routing, client_for_provider, persist_routing,
                        split_items_by_provider,
                    )
                    from api.meijiehezi_api import settle_group_order_sns as _settle_group_sns
                    try:
                        from db.meijiehezi_db import (
                            mark_item_awaiting_confirmation as _mark_awaiting,
                            set_item_awaiting_sync as _set_awaiting_sync,
                        )
                    except Exception:
                        _mark_awaiting = None
                        _set_awaiting_sync = None
                    try:
                        from services.meijiehezi.client import (
                            ConfirmationRequiredError as _ConfirmReq,
                            AmbiguousResponseError as _AmbiguousErr,
                            PartialSuccessError as _PartialErr,
                        )
                    except Exception:
                        _ConfirmReq = None
                        _AmbiguousErr = None
                        _PartialErr = None

                    try:
                        client = _get_client()
                        await client.refresh_token()
                        if mhz_ids:
                            # [WO-KYB-ROUTING D1] 按渠道分组重试。全同渠道时 split
                            # 只返一组,循环体只跑一次 —— 与接入前逐位等价。
                            # 🔴 try/except 在循环体内:一组抛异常不许波及另一组。
                            # [D3b] 择优改道:同一媒体两家都有时改到更便宜的那家。放在投递前、扣费后 ——
                            # 扣费已按用户选中那条媒体的价算完,售价对用户零变化,省下的差价进毛利。
                            # 开关关 / 无可改道条目时,_smhzg_dispatch 与 mhz_items 逐位等价。
                            _smhzg_dispatch, _smhzg_routed = apply_provider_preference(mhz_items, is_wemedia=False)
                            if _smhzg_routed:
                                # 🔴 先落库再投递:落库失败就退回原渠道。绝不允许「发去了快易播但订单表
                                #    不知道」—— 那种单回流永远扫不到,24h 后被误判未发布而退款。
                                try:
                                    persist_routing(_smhzg_routed)
                                except Exception as _e_route:
                                    logger.error('[routing] 调度器软文 改道落库失败,退回原渠道: %s', _e_route)
                                    _smhzg_dispatch, _smhzg_routed = mhz_items, {}
                            # 🔴 必须定义在 try **之外**:异常若发生在 try 开头,finally 仍会执行,
                            #    定义在 try 内就是 NameError 把真异常盖掉。
                            _smhzg_submitted = set()
                            try:
                                for _smhzg_provider, _smhzg in split_items_by_provider(_smhzg_dispatch):
                                    _smhzg_ids = [it["media_id"] for it in _smhzg]
                                    try:
                                        from services.contact_placeholder import (
                                            safe_render_contact_for_publish, strictest_policy,
                                        )
                                        from db.meijiehezi_db import get_media_contact_policies

                                        _retry_mhz_policy = "none"
                                        try:
                                            _retry_mhz_policy = strictest_policy(list(
                                                # [D3b] 原媒体 ∪ 改道后媒体,取最严(fail-closed)
                                                get_media_contact_policies(
                                                    sorted({it["media_id"] for it in mhz_items}
                                                           | set(_smhzg_ids)), is_wemedia=False
                                                ).values()
                                            ))
                                        except Exception:
                                            _retry_mhz_policy = "none"
                                        _retry_mhz_body = safe_render_contact_for_publish(
                                            content_md, _retry_brand_id, channel="media",
                                            contact_policy=_retry_mhz_policy,
                                        )
                                        from services.article_publish_dispatch import dispatch_article_to_provider
                                        _retry_mhz_source = f"scheduler_retry:softarticle:{_retry_mhz_policy}"
                                        _smhzg_kwargs = {
                                            "media_ids": _smhzg_ids,
                                            "confirms": _build_confirms(_smhzg),
                                            "brand_id": _retry_brand_id,
                                        }
                                        _smhzg_kwargs.update(
                                            build_provider_kwargs_extras(_smhzg, _smhzg_provider))
                                        result = await dispatch_article_to_provider(
                                            client=client_for_provider(_smhzg_provider, client),
                                            dispatch_kind="publish",
                                            article_id=order["article_id"],
                                            source_title=order["article_title"], source_content=content_md,
                                            outgoing_title=order["article_title"], outgoing_content=_retry_mhz_body,
                                            source=_retry_mhz_source,
                                            provider_kwargs=_smhzg_kwargs,
                                            snapshot_writer=lambda snap: set_item_submission_snapshot(
                                                [it["id"] for it in _smhzg],
                                                title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
                                            ),
                                        )
                                        # [审计] 保存 mhz 原始响应
                                        for it in _smhzg:
                                            save_mhz_raw_response(it["id"], result.raw_data)
                                        # [2026-04-30] 批量按 media_id 各自取 sn
                                        # [WO-KYB-ROUTING D1] 领单号走与批量/单发同一个共用件:
                                        #   媒介盒子用主 sn 兜底;快易播必须按 item 拿到自己的单号,
                                        #   拿不到即"没提交成功"→ 退款,不许留着把算力冻住。
                                        sn_map = result.order_sn_map or {}
                                        _smhzg_submitted |= _settle_group_sns(
                                            _smhzg, result, _smhzg_provider,
                                            awaiting_reason="重试提交后未取得外部单号,等定时反查回填",
                                        )
                                        logger.info(f"[Scheduler] 重试订单 {order_id} 软文提交成功: provider={_smhzg_provider}, primary_sn={result.order_sn}, sn_count={len(sn_map)}")
                                    except Exception as ce:
                                        if _ConfirmReq and isinstance(ce, _ConfirmReq) and _mark_awaiting:
                                            logger.info(
                                                f"[Scheduler] 订单 {order_id} 软文进入待确认: code={ce.code}, field={ce.field}, msg={ce.msg}"
                                            )
                                            for it in _smhzg:
                                                ok = _mark_awaiting(it["id"], ce.code, ce.msg, ce.field)
                                                if not ok:
                                                    release_submit_lock(
                                                        it["id"], success=False,
                                                        reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                                                    )
                                        elif _AmbiguousErr and isinstance(ce, _AmbiguousErr) and _set_awaiting_sync:
                                            # [防线 4] mhz 响应模糊：标 awaiting_sync 等定时反查，绝不再重试
                                            # 这是 04-29 故障的关键修复：禁止 release_submit_lock 回 pending 触发再发
                                            logger.warning(
                                                f"[Scheduler 防线4] 订单 {order_id} 软文 mhz 响应模糊 (code=200, sn 空): raw={ce.raw_data}"
                                            )
                                            for it in _smhzg:
                                                save_mhz_raw_response(it["id"], ce.raw_data)
                                                _set_awaiting_sync(
                                                    it["id"],
                                                    reason=f"调度器重试时 mhz 响应 code=200 但 order_sn 为空：{ce.msg or ''}",
                                                )
                                        elif _PartialErr and isinstance(ce, _PartialErr) and _set_awaiting_sync:
                                            # [2026-04-30] 重试时 mhz 部分接单 → 标 awaiting_sync 待人工
                                            logger.warning(
                                                f"[Scheduler 部分接单] 订单 {order_id} 软文 勾 {ce.selected_num} 接 {ce.success_count}"
                                            )
                                            for it in _smhzg:
                                                save_mhz_raw_response(it["id"], ce.raw_data)
                                                _set_awaiting_sync(
                                                    it["id"],
                                                    reason=f"调度器重试时 mhz 部分接单（勾 {ce.selected_num} 接 {ce.success_count}）",
                                                )
                                        else:
                                            raise
                            finally:
                                # 🔴 放 finally 不放循环后面:调度器那两段的 except 有 `else: raise`,
                                #    异常会穿出循环,写在循环后面就跑不到,改道标记会留在没投出的单上。
                                if _smhzg_routed:
                                    try:
                                        clear_routing(set(_smhzg_routed) - _smhzg_submitted)
                                    except Exception as _e_clr:
                                        logger.error('[routing] 调度器软文 撤销未投出标记失败: %s', _e_clr)
                        if wm_ids:
                            # [WO-KYB-ROUTING D1] 按渠道分组重试。全同渠道时 split
                            # 只返一组,循环体只跑一次 —— 与接入前逐位等价。
                            # 🔴 try/except 在循环体内:一组抛异常不许波及另一组。
                            # [D3b] 择优改道:同一媒体两家都有时改到更便宜的那家。放在投递前、扣费后 ——
                            # 扣费已按用户选中那条媒体的价算完,售价对用户零变化,省下的差价进毛利。
                            # 开关关 / 无可改道条目时,_swmg_dispatch 与 wm_items 逐位等价。
                            _swmg_dispatch, _swmg_routed = apply_provider_preference(wm_items, is_wemedia=True)
                            if _swmg_routed:
                                # 🔴 先落库再投递:落库失败就退回原渠道。绝不允许「发去了快易播但订单表
                                #    不知道」—— 那种单回流永远扫不到,24h 后被误判未发布而退款。
                                try:
                                    persist_routing(_swmg_routed)
                                except Exception as _e_route:
                                    logger.error('[routing] 调度器自媒体 改道落库失败,退回原渠道: %s', _e_route)
                                    _swmg_dispatch, _swmg_routed = wm_items, {}
                            # 🔴 必须定义在 try **之外**:异常若发生在 try 开头,finally 仍会执行,
                            #    定义在 try 内就是 NameError 把真异常盖掉。
                            _swmg_submitted = set()
                            try:
                                for _swmg_provider, _swmg in split_items_by_provider(_swmg_dispatch):
                                    _swmg_ids = [it["media_id"] for it in _swmg]
                                    try:
                                        from services.contact_placeholder import (
                                            safe_render_contact_for_publish, strictest_policy,
                                        )
                                        from db.meijiehezi_db import get_media_contact_policies

                                        _retry_wm_policy = "none"
                                        try:
                                            _retry_wm_policy = strictest_policy(list(
                                                # [D3b] 原媒体 ∪ 改道后媒体,取最严(fail-closed)
                                                get_media_contact_policies(
                                                    sorted({it["media_id"] for it in wm_items}
                                                           | set(_swmg_ids)), is_wemedia=True
                                                ).values()
                                            ))
                                        except Exception:
                                            _retry_wm_policy = "none"
                                        _retry_wm_body = safe_render_contact_for_publish(
                                            content_md, _retry_brand_id, channel="media",
                                            contact_policy=_retry_wm_policy,
                                        )
                                        from services.article_publish_dispatch import dispatch_article_to_provider
                                        _retry_wm_source = f"scheduler_retry:wemedia:{_retry_wm_policy}"
                                        _swmg_kwargs = {
                                            "toutiao_ids": _swmg_ids,
                                            "confirms": _build_confirms(_swmg),
                                            "brand_id": _retry_brand_id,
                                        }
                                        _swmg_kwargs.update(
                                            build_provider_kwargs_extras(_swmg, _swmg_provider))
                                        wm_result = await dispatch_article_to_provider(
                                            client=client_for_provider(_swmg_provider, client),
                                            dispatch_kind="publish_wemedia",
                                            article_id=order["article_id"],
                                            source_title=order["article_title"], source_content=content_md,
                                            outgoing_title=order["article_title"], outgoing_content=_retry_wm_body,
                                            source=_retry_wm_source,
                                            provider_kwargs=_swmg_kwargs,
                                            snapshot_writer=lambda snap: set_item_submission_snapshot(
                                                [it["id"] for it in _swmg],
                                                title=snap.title, content=snap.content, source=snap.source, legal_catalog_version=snap.legal_prohibition_catalog_version,
                                            ),
                                        )
                                        # [2026-04-30] 批量按 media_id 各自取 sn（每个 media 独立 sn）
                                        wm_sn_map = wm_result.order_sn_map or {}
                                        for it in _swmg:
                                            save_mhz_raw_response(it["id"], wm_result.raw_data)
                                        # [WO-KYB-ROUTING D1] 同软文段,领单号走共用件。
                                        _swmg_submitted |= _settle_group_sns(
                                            _swmg, wm_result, _swmg_provider,
                                            awaiting_reason="重试自媒体提交后未取得外部单号,等定时反查回填",
                                        )
                                        logger.info(f"[Scheduler] 重试订单 {order_id} 自媒体提交成功: provider={_swmg_provider}, primary_sn={wm_result.order_sn}, sn_count={len(wm_sn_map)}")
                                    except Exception as ce:
                                        if _ConfirmReq and isinstance(ce, _ConfirmReq) and _mark_awaiting:
                                            logger.info(
                                                f"[Scheduler] 订单 {order_id} 自媒体进入待确认: code={ce.code}, field={ce.field}, msg={ce.msg}"
                                            )
                                            for it in _swmg:
                                                ok = _mark_awaiting(it["id"], ce.code, ce.msg, ce.field)
                                                if not ok:
                                                    release_submit_lock(
                                                        it["id"], success=False,
                                                        reject_reason=f"待确认状态标记失败: code {ce.code} {ce.msg}",
                                                    )
                                        elif _AmbiguousErr and isinstance(ce, _AmbiguousErr) and _set_awaiting_sync:
                                            # [防线 4] 自媒体反查列表 3 次没命中 → 标 awaiting_sync 兜底
                                            logger.warning(
                                                f"[Scheduler 防线4] 订单 {order_id} 自媒体响应模糊（反查未命中）: raw={ce.raw_data}"
                                            )
                                            for it in _swmg:
                                                save_mhz_raw_response(it["id"], ce.raw_data)
                                                _set_awaiting_sync(
                                                    it["id"],
                                                    reason=f"调度器重试自媒体时反查 toutiao_order 列表未命中：{ce.msg or ''}",
                                                )
                                        else:
                                            raise
                            finally:
                                # 🔴 放 finally 不放循环后面:调度器那两段的 except 有 `else: raise`,
                                #    异常会穿出循环,写在循环后面就跑不到,改道标记会留在没投出的单上。
                                if _swmg_routed:
                                    try:
                                        clear_routing(set(_swmg_routed) - _swmg_submitted)
                                    except Exception as _e_clr:
                                        logger.error('[routing] 调度器自媒体 撤销未投出标记失败: %s', _e_clr)
                    except Exception as e:
                        # 重试失败：释放所有拿到的锁回 pending（attempts 已 ++）
                        # 注意：到这里的不会是 AmbiguousResponseError（已被内层捕获）
                        _err_payload = {"_error": type(e).__name__, "_message": str(e)[:500]}
                        for it in (mhz_items + wm_items):
                            save_mhz_raw_response(it["id"], _err_payload)
                            release_submit_lock(it["id"], success=False, reject_reason=f"重试失败: {str(e)[:200]}")
                        logger.warning(f"[Scheduler] 重试订单 {order_id} 本轮失败（{order_age.total_seconds()//60:.0f}min），等待下次重试: {e}")
        except Exception as e:
            logger.error(f"[Scheduler] 卡单重试任务异常: {e}")

    # ----------------------------------------------------------------------
    # [防线 4 配套定时任务 · 2026-04-30]
    # 处理 awaiting_sync 状态的 item：
    #   首选 → 实时调 mhz 列表接口反查（最快几秒就能拿到 sn）
    #   降级 → mhz_synced_orders 同步表反查（防 mhz 列表接口暂时不通时仍能恢复）
    #   12h 仍反查不到 → 转人工审核（不退款不重发）
    # ----------------------------------------------------------------------
    async def _mhz_awaiting_sync_resolver():
        """每 5 分钟扫 awaiting_sync 的 item，**实时**调 mhz 列表接口反查回填或转人工。

        [2026-04-30 优化] 之前只查同步表 mhz_synced_orders（每 10 分钟全量一次），
        极端情况下 awaiting_sync 要等 ~10 分钟才能自愈。现在改成优先**实时调 mhz
        列表接口**，从最差等 10 分钟变最差等几秒。
        """
        try:
            from db.meijiehezi_db import (
                find_awaiting_sync_items_to_check,
                find_awaiting_sync_items_overdue,
                find_synced_order_by_signature,
                link_awaiting_sync_to_order_sn,
                mark_item_manual_review,
            )
            from services.meijiehezi.config import ENDPOINTS
            from api.meijiehezi_api import _get_client

            # 准备 mhz client（实时反查需要直接调接口）
            try:
                _client = _get_client()
                await _client.refresh_token()
                _client_ok = True
            except Exception as ce:
                logger.warning(f"[awaiting_sync] mhz client 初始化失败，本轮降级走同步表反查: {ce}")
                _client = None
                _client_ok = False

            async def _lookup_realtime(item_dict):
                """开源版:发布渠道未接入,不做实时反查(调用方按「查不到」走后续兜底)。"""
                return None

            # Step 1: 处理 2 分钟～12 小时内进入 awaiting_sync 的 item
            pending = find_awaiting_sync_items_to_check(min_age_minutes=2, max_age_minutes=720)
            # 先去 DB 拿 media_type 字段（find_awaiting_sync_items_to_check 默认没返回）
            if pending:
                from db.connection import get_connection
                _conn = get_connection()
                try:
                    _ids = [int(it["id"]) for it in pending]
                    _cur = _conn.cursor()
                    _cur.execute(
                        "SELECT id, media_type FROM mhz_publish_order_items WHERE id = ANY(%s)",
                        (_ids,),
                    )
                    _mt_map = {int(r["id"]): (r.get("media_type") or "") for r in _cur.fetchall()}
                    for it in pending:
                        it["media_type"] = _mt_map.get(int(it["id"]), "")
                finally:
                    _conn.close()

            linked = 0
            for it in pending:
                sn = await _lookup_realtime(it)
                if sn and link_awaiting_sync_to_order_sn(it["id"], sn):
                    linked += 1
                    logger.info(f"[awaiting_sync] item={it['id']} 反查到 sn={sn}，已回填")

            # Step 2: 超过 12 小时仍反查不到 → 转人工（最后再实时反查一次）
            overdue = find_awaiting_sync_items_overdue(max_age_minutes=720)
            if overdue:
                from db.connection import get_connection
                _conn = get_connection()
                try:
                    _ids = [int(it["id"]) for it in overdue]
                    _cur = _conn.cursor()
                    _cur.execute(
                        "SELECT id, media_type FROM mhz_publish_order_items WHERE id = ANY(%s)",
                        (_ids,),
                    )
                    _mt_map = {int(r["id"]): (r.get("media_type") or "") for r in _cur.fetchall()}
                    for it in overdue:
                        it["media_type"] = _mt_map.get(int(it["id"]), "")
                finally:
                    _conn.close()

            manual = 0
            # 本轮已实时反查过且没命中的 item —— Step 3 直接复用结论，不重复打 mhz 接口
            _probed_miss: set = set()
            for it in overdue:
                sn = await _lookup_realtime(it)
                if sn and link_awaiting_sync_to_order_sn(it["id"], sn):
                    linked += 1
                    logger.info(f"[awaiting_sync] item={it['id']} 超时前最后反查命中 sn={sn}，已回填")
                    continue
                _probed_miss.add(int(it["id"]))
                if mark_item_manual_review(it["id"]):
                    manual += 1
                    logger.warning(
                        f"[awaiting_sync] item={it['id']} 超 12 小时仍反查不到 sn，转人工审核 "
                        f"(article_id={it.get('article_id')}, media={it.get('media_name')}, "
                        f"cost={it.get('cost_points')}积分)"
                    )

            # ------------------------------------------------------------------
            # Step 3: [P0 出口 2026-07-26] 超 72 小时 + 反查失败 ≥3 次 → 开人工出口
            #
            # Step 2 的 mark_item_manual_review 只打一个布尔标记、item 仍是
            # awaiting_sync，而 Step 1/2 两个扫描器的 WHERE 都带
            # manual_review_required=FALSE —— 打完标记这条 item 从所有自动扫描里
            # **永久消失**，唯一出口是管理员主动翻牌。线上最老一条因此滞留 58 天。
            # Step 3 刻意不过滤 manual_review_required，把这些历史卡单重新纳入管理，
            # 推到 awaiting_action + §13 合同，让用户侧看得见、点得动。
            #
            # 绝不在这里退款：mhz 已回执"成功提交，正在执行发布"，稿件可能真发出去了，
            # 自动退款 = 既退钱又发稿。动钱只在管理员核实后走 admin_manual_review_resolve
            # ('mark_failed')，幂等键唯一为 item:{id}。
            # ------------------------------------------------------------------
            from db.meijiehezi_db import (
                bump_awaiting_sync_probe_attempt,
                find_awaiting_sync_items_for_exit,
                open_awaiting_sync_user_exit,
            )
            from services.publication_awaiting_sync_exit import (
                EXIT_MIN_AGE_HOURS,
                build_awaiting_sync_exit_contract,
                should_open_user_exit,
            )

            stuck = find_awaiting_sync_items_for_exit(min_age_hours=EXIT_MIN_AGE_HOURS)
            exits = 0
            for it in stuck:
                _iid = int(it["id"])
                if _iid not in _probed_miss:
                    sn = await _lookup_realtime(it)
                    if sn and link_awaiting_sync_to_order_sn(_iid, sn):
                        linked += 1
                        logger.info(f"[awaiting_sync] item={_iid} 出口前最后反查命中 sn={sn}，已回填")
                        continue
                attempts = bump_awaiting_sync_probe_attempt(_iid)
                if not should_open_user_exit(
                    probe_attempts=attempts, awaiting_hours=it.get("awaiting_hours")
                ):
                    continue
                contract = build_awaiting_sync_exit_contract(
                    item_id=_iid,
                    media_name=it.get("media_name"),
                    awaiting_hours=it.get("awaiting_hours"),
                    probe_attempts=attempts,
                )
                if open_awaiting_sync_user_exit(_iid, contract):
                    exits += 1
                    logger.warning(
                        f"[awaiting_sync] item={_iid} 反查 {attempts} 次仍无解且滞留超 "
                        f"{EXIT_MIN_AGE_HOURS} 小时，已挂起等人工动作 "
                        f"(article_id={it.get('article_id')}, media={it.get('media_name')}, "
                        f"cost={it.get('cost_points')}积分，**未退款**)"
                    )

            if pending or overdue or stuck:
                logger.info(
                    f"[awaiting_sync] 本轮处理: pending={len(pending)} overdue={len(overdue)} "
                    f"stuck={len(stuck)} linked={linked} manual_review={manual} user_exit={exits}"
                )

            # mark_item_manual_review 已在状态事务内为用户和管理员写强类型 outbox。

            # 关闭 client
            if _client is not None:
                try:
                    await _client.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[awaiting_sync] 反查任务异常: {e}")

    # [2026-04-30] mhz 调用日志清理任务，每天 03:00 删 30 天前的日志（防表无限增长）
    async def _mhz_call_log_cleanup():
        try:
            from db.meijiehezi_db import cleanup_mhz_call_log
            deleted = cleanup_mhz_call_log(retention_days=30)
            if deleted > 0:
                logger.info(f"[Scheduler] mhz_call_log 清理: 删除 {deleted} 条 30 天前的记录")
        except Exception as e:
            logger.warning(f"[Scheduler] mhz_call_log 清理失败: {e}")

    # 兜底补退（每小时跑一次）— 扫描终态 item 但没有 refund 记录的，自动补退
    # 防御场景：sync 拒稿/撤稿循环中途崩溃 → 部分 item 状态变 rejected/withdrawn 但没退款
    async def _backfill_missing_refunds_job():
        try:
            from db.connection import get_connection
            from db.meijiehezi_db import refund_for_publish_order
            conn = get_connection()
            try:
                c = conn.cursor()
                # 找所有终态 + 有费用 + 但没有对应 item:{id} 退款记录的 item
                c.execute("""
                    SELECT i.id, i.user_id, i.cost_points, i.status
                    FROM mhz_publish_order_items i
                    WHERE i.status IN ('rejected', 'withdrawn')
                      AND i.cost_points > 0
                      -- [返工 2026-08-18 · Codex P0-06] 只补退**老口径**的项。
                      -- freeze_per_item 的钱还冻着,由 contract_worker 的
                      -- commit/release 收敛;在这里补退 = 双补偿。
                      -- 显式列出老值(NULL 与 deduct_upfront),不用 `<> 新值` ——
                      -- 那个写法对 NULL 恒 UNKNOWN,会把所有老行一起漏掉。
                      AND (i.billing_mode IS NULL OR i.billing_mode = 'deduct_upfront')
                      AND NOT EXISTS (
                          SELECT 1 FROM point_transactions t
                          WHERE t.user_id = i.user_id
                            AND t.type = 'refund'
                            AND t.feature_code = 'media_proxy_publish'
                            AND t.order_id = 'item:' || i.id::text
                      )
                    LIMIT 100
                """)
                missing = c.fetchall()
                conn.close()

                if not missing:
                    return

                logger.warning(f"[Scheduler] 发现 {len(missing)} 个终态 item 未退款，开始补退")
                refunded_count = 0
                for it in missing:
                    r = refund_for_publish_order(
                        user_id=int(it["user_id"]),
                        amount=int(it["cost_points"]),
                        refund_key=f"item:{it['id']}",
                        reason=f"补退: 终态({it['status']})未退款",
                    )
                    if r.get("success") and not r.get("skipped"):
                        refunded_count += 1
                if refunded_count > 0:
                    logger.warning(f"[Scheduler] 补退完成: {refunded_count}/{len(missing)} 个 item")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Scheduler] 补退任务异常: {e}")

    # 幂等键表清理（每天 03:00 清 24h 前的）— 防止表无限增长
    async def _cleanup_idempotency_keys_job():
        try:
            from db.meijiehezi_db import cleanup_old_idempotency_keys
            cleaned = cleanup_old_idempotency_keys(retain_hours=24)
            if cleaned > 0:
                logger.info(f"[Scheduler] 清理了 {cleaned} 个过期幂等键")
        except Exception as e:
            logger.error(f"[Scheduler] 幂等键清理任务异常: {e}")

    # [P0-B] 清理 submitting 孤儿状态（每分钟跑一次）
    async def _auto_cancel_stale_awaiting_job():
        """超过 24 小时未确认的 awaiting_confirmation item 自动取消并退款。

        防止 item 永远卡在待确认状态占用钱不退给用户。
        """
        try:
            from db.meijiehezi_db import auto_cancel_stale_awaiting
            cancelled = auto_cancel_stale_awaiting(stale_hours=24)
            if cancelled > 0:
                logger.warning(f"[Scheduler] 自动取消 {cancelled} 个超 24 小时未确认的 item（已退款）")
        except Exception as e:
            logger.error(f"[Scheduler] 自动取消待确认 item 任务异常: {e}")

    async def _cleanup_orphan_submitting_items_job():
        """清理 submitting 孤儿订单 — 提交期间崩溃留下的 zombie。

        判定：status='submitting' 且 last_submit_at 超过 5 分钟（mhz API
        正常只需几秒到几十秒，5 分钟还没回的肯定是孤儿）。

        动作：
          - submit_attempts < 3 → 回 pending，下次正常调度器重试
          - submit_attempts >= 3 → 标 failed（之后由 _retry_stuck_publish_orders 走超时退款分支）
        """
        try:
            from db.meijiehezi_db import cleanup_orphan_submitting_items
            cleaned = cleanup_orphan_submitting_items(timeout_minutes=5)
            if cleaned > 0:
                logger.warning(f"[Scheduler] 清理了 {cleaned} 个 submitting 孤儿订单（自动恢复）")
        except Exception as e:
            logger.error(f"[Scheduler] 清理 submitting 孤儿任务异常: {e}")

    # [P0-A] 服务启动时立即扫描卡住的 pending 订单
    # 防止上一次进程在"扣费成功 → asyncio.create_task 还没跑"之间死亡留下的卡单
    async def _startup_sweep_stuck_orders():
        """服务启动时一次性 sweep — 把最近 1 小时内创建但仍 pending 的订单
        交给 _retry_stuck_publish_orders 立即重试一次（比正常每 5 分钟快）。

        正常调度器要等 10 分钟创建时间 + 5 分钟扫描间隔，最差 15 分钟用户体验"消失"。
        启动 sweep 把这个时间窗口压到 ~30 秒。
        """
        try:
            # 先清一次 submitting 孤儿。timeout=1 分钟而不是 0：
            #   - 单 worker 场景：1 分钟前的 submitting 必然是上次进程留下的孤儿，清掉
            #   - 多 worker 场景：当前 worker 启动时另一 worker 可能正在提交（status='submitting'），
            #     1 分钟内的 submitting 不动（mhz API 通常几秒到几十秒就返回），避免误清
            from db.meijiehezi_db import cleanup_orphan_submitting_items, get_recent_pending_orders
            cleaned = cleanup_orphan_submitting_items(timeout_minutes=1)
            if cleaned > 0:
                logger.warning(f"[Startup-Sweep] 启动时清理了 {cleaned} 个 submitting 孤儿（上次进程死在提交中）")

            # 然后立即跑一次重试任务（重试任务自带守卫，不会重复提交）
            stuck = get_recent_pending_orders(within_minutes=60)
            if stuck:
                logger.warning(f"[Startup-Sweep] 启动时发现 {len(stuck)} 个卡住的 pending 订单，立即触发重试")
                # 直接调用同一个重试函数（守卫内部会按 attempts/last_submit_at 等条件过滤）
                await _retry_stuck_publish_orders()
            else:
                logger.info("[Startup-Sweep] 启动检查完成，无卡住订单")
        except Exception as e:
            logger.error(f"[Startup-Sweep] 启动 sweep 异常: {e}")

    # 暴露给外部调用（server.py startup hook 用）
    # 用模块级 dict 而不是给 setup_schedule 加属性，重启 setup_schedule 时不会留旧引用
    _scheduler_hooks["startup_sweep_stuck_orders"] = _startup_sweep_stuck_orders
    _scheduler_hooks["cleanup_orphan_submitting_items"] = _cleanup_orphan_submitting_items_job

    # 缓存主事件循环引用，避免在线程中调用已废弃的 get_event_loop()
    _main_loop: asyncio.AbstractEventLoop | None = None
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    def _run_async(coro_func):
        """在 BackgroundScheduler 线程中安全执行 async 协程。

        [#197] 实现已提到模块级 `run_async_in_scheduler`;这里只是把本函数
        捕获的 `_main_loop` 传过去,既有 9 处调用的行为逐字节不变。
        """
        return run_async_in_scheduler(coro_func, loop=_main_loop)

    try:
        scheduler = get_scheduler()
        scheduler.add_job(_run_async(_mhz_status_sync),
                          'interval', minutes=10, id='mhz_status_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_status_sync (每10分钟)")
        # [2026-04-30] 自媒体订单状态同步（之前完全没有，导致拒稿/撤单/已发布状态全丢失）
        scheduler.add_job(sched_claim("mhz_toutiao_order_sync", 600)(_run_async(_mhz_toutiao_order_sync)),  # [FF5] tick claim
                          'interval', minutes=10, id='mhz_toutiao_order_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_toutiao_order_sync (每10分钟)")
        scheduler.add_job(_run_async(_mhz_media_sync),
                          'cron', hour='6,18', id='mhz_media_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_media_sync (每天6:00/18:00)")
        scheduler.add_job(_run_async(_mhz_wemedia_sync),
                          'cron', hour='6,18', id='mhz_wemedia_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_wemedia_sync (每天6:00/18:00)")
        # [svideo lane · 2026-07-04] 短视频资源池同步（每天6:00/18:00）+ 订单状态同步（每10分钟）
        scheduler.add_job(_run_async(_mhz_short_video_sync),
                          'cron', hour='6,18', id='mhz_short_video_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_short_video_sync (每天6:00/18:00)")
        # [WO_KYB_CATALOG_GOVERNANCE 2026-08-10 · A.2] 新渠道目录同步(每天6:00/18:00)。
        #   修前零调用方 → 目录停摆 13 天无人发现。与上面三个目录 job 同频。
        #   tick claim:蓝绿双槽都跑 scheduler,双跑 = 对上游 1,300+ 页拉两遍,
        #   且两个事务并发 UPDATE 同一批 13 万行。天然幂等(纯 upsert)→ reclaimable=True。
        scheduler.add_job(sched_claim("kyb_media_catalog_sync", 43200, reclaimable=True)(
                              _run_async(_kyb_media_catalog_sync)),
                          'cron', hour='6,18', id='kyb_media_catalog_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: kyb_media_catalog_sync (每天6:00/18:00·新渠道目录)")
        scheduler.add_job(_run_async(_mhz_short_video_order_sync),
                          'interval', minutes=10, id='mhz_short_video_order_sync', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_short_video_order_sync (每10分钟)")
        scheduler.add_job(_run_async(_mhz_svideo_stuck_refund_sweep),
                          'interval', minutes=10, id='mhz_svideo_stuck_refund_sweep', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_svideo_stuck_refund_sweep (每10分钟·卡单兜底退款)")
        # [WO-PUB-ZOMBIE-2026-08-04] 软文/自媒体僵尸孤儿单兜底清扫。
        #   动状态 + 动钱,与其他动钱的 mhz job 对齐加 tick claim,防蓝绿双槽双跑。
        scheduler.add_job(sched_claim("mhz_orphan_zombie_sweep", 900)(_run_async(_mhz_orphan_zombie_sweep)),
                          'interval', minutes=15, id='mhz_orphan_zombie_sweep', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_orphan_zombie_sweep (每15分钟·僵尸孤儿单兜底退款)")
        scheduler.add_job(_run_async(_mhz_session_keepalive),
                          'interval', minutes=10, id='mhz_session_keepalive', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_session_keepalive (每10分钟)")
        scheduler.add_job(sched_claim("mhz_retry_stuck_orders", 300)(_run_async(_retry_stuck_publish_orders)),  # [FF5] tick claim
                          'interval', minutes=5, id='mhz_retry_stuck_orders', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_retry_stuck_orders (每5分钟)")
        # [防线 4 · 2026-04-30] awaiting_sync 反查回填 + 超时转人工
        # [P0 出口 2026-07-26] 加 tick claim：本 job 现在会改 item 状态到 awaiting_action
        #   并写 §13 合同（下一步就是管理员按合同退款），双跑会重复计次/重复告警，
        #   与其他动状态的 mhz job 对齐成 fail-closed claim。
        scheduler.add_job(sched_claim("mhz_awaiting_sync_resolver", 300)(_run_async(_mhz_awaiting_sync_resolver)),
                          'interval', minutes=5, id='mhz_awaiting_sync_resolver', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_awaiting_sync_resolver (每5分钟)")
        # [P0-B] submitting 孤儿清理（每 1 分钟）
        scheduler.add_job(_run_async(_cleanup_orphan_submitting_items_job),
                          'interval', minutes=1, id='mhz_cleanup_orphan_submitting', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_cleanup_orphan_submitting (每1分钟)")
        # 幂等键表清理（每天 03:00）— 防止表无限增长
        scheduler.add_job(_run_async(_cleanup_idempotency_keys_job),
                          'cron', hour=3, minute=0, id='mhz_cleanup_idempotency_keys', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_cleanup_idempotency_keys (每天03:00)")
        # [2026-04-30] mhz 调用日志 30 天清理
        scheduler.add_job(_run_async(_mhz_call_log_cleanup),
                          'cron', hour=3, minute=15, id='mhz_call_log_cleanup', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_call_log_cleanup (每天03:15)")
        # 兜底补退（每小时跑一次）— 防 sync 退款循环崩溃丢失的 item
        scheduler.add_job(sched_claim("mhz_backfill_missing_refunds", 3600)(_run_async(_backfill_missing_refunds_job)),  # [FF5] tick claim
                          'interval', hours=1, id='mhz_backfill_missing_refunds', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_backfill_missing_refunds (每1小时)")
        # 待确认 item 24h 自动取消（每小时检查一次）
        scheduler.add_job(_run_async(_auto_cancel_stale_awaiting_job),
                          'interval', hours=1, id='mhz_auto_cancel_stale_awaiting', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: mhz_auto_cancel_stale_awaiting (每1小时)")
        # [V3.5 工厂模式 2026-05-26] 代理收益 T+3 settle daily cron
        scheduler.add_job(_run_async(_v35_settle_agent_revenue_daily),
                          'cron', hour=2, minute=30, id='v35_settle_agent_revenue', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: v35_settle_agent_revenue (每天02:30)")
        # [V3.5 W4 2026-05-26] 库存对账日报 cron · 02:45(在 v35_settle 之后跑 · 看 settled 后真实差异)
        scheduler.add_job(_run_async(_v35_inventory_audit_daily),
                          'cron', hour=2, minute=45, id='v35_inventory_audit_daily', replace_existing=True)
        logger.info("[Scheduler] 📌 添加定时任务: v35_inventory_audit_daily (每天02:45)")
    except Exception as e:
        logger.warning(f"[Scheduler] 外部发布通道定时任务添加失败: {e}")


# ============================================================
# V3.5 W4 · 库存对账日报 cron
# ============================================================

async def _v35_inventory_audit_daily():
    """每日对账 · 写 inventory_audit_runs + diffs(若 drift)· abs(diff) > 1 告警

    🔴 2026-07-29 返修:这里原来是 `except Exception: logger.exception(...)` —— 只 log、
       不写库、不告警。2026-07-29 01:04 上线的 `allocated_out` NameError 让 run_audit
       每次一进来就抛,于是**连续多天 inventory_audit_runs 一行都不落**:
       财务页看到的是"最近一次 07-28 的旧行",完全看不出对账已经死了。
       现在异常必须落一行 status='failed'(且 has_drift=TRUE)+ 触发 ai_ops 告警。
    """
    try:
        from services.inventory_audit import run_audit
        from db.connection import get_db as _gdb
        with _gdb() as conn:
            cur = conn.cursor()
            result = run_audit(cur, triggered_by="cron")
            conn.commit()
        if result.get("has_drift"):
            logging.getLogger(__name__).error(
                f"[V35-AUDIT-CRON] DRIFT run_id={result['run_id']} "
                f"diff_paid={result['diff_paid']} diff_bonus={result['diff_bonus']} diff_publish={result['diff_publish']}"
            )
        else:
            logging.getLogger(__name__).info(
                f"[V35-AUDIT-CRON] OK run_id={result['run_id']}"
            )
    except Exception as e:
        # 落失败态 + 告警(record_audit_failure 自己开连接 · 内部 fail-soft · 不会二次抛)
        try:
            from services.inventory_audit import record_audit_failure
            record_audit_failure(e, triggered_by="cron")
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("[V35-AUDIT-CRON] 失败态记录本身也失败")
        logging.getLogger(__name__).exception(f"[V35-AUDIT-CRON] 失败: {e}")


# ============================================================
# V3.5 工厂模式 · 代理收益 T+3 settle daily cron
# ============================================================

async def _v35_settle_agent_revenue_daily():
    """
    V3.5 daily cron · 把 agent_revenue_ledger 中 settle_at < NOW() 的 frozen → settled
    跑在 02:30(早于 daily_monitoring 03:00 · 错峰避免 DB 高负载)
    """
    try:
        from db.connection import get_connection
        from services.agent_revenue import settle_frozen_to_settled
        conn = get_connection()
        try:
            cursor = conn.cursor()
            count = settle_frozen_to_settled(cursor, batch_size=5000)
            conn.commit()
            logger.info(f"[V35 T+3 settle] 处理 {count} 条 frozen → settled")
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.exception(f"[V35 T+3 settle] 失败: {e}")
