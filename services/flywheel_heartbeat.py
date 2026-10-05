"""飞轮 job 心跳 / 失败告警(A2 · 2026-07-29)。

背景(2026-07-28 生产实证):飞轮的五个 job 全都是 fail-soft `except → logger.warning` 收尾,
`sched_job_runs` 里一行痕迹都没有。结果:媒体推荐池从 2026-05-11 起停更 78 天没人发现,
写作 assignments / outcome_events 长期 0 行也没人发现 —— **不是修不好,是根本看不见**。

本模块提供三件事:
  1. `flywheel_job(job_key)` 装饰器:每次执行写 started/finished/failed + 处理量到
     `flywheel_job_heartbeats`。**失败绝不静默吞** —— 落表 + 拉 AIOps 告警 + logger.exception,
     然后(默认)不向 APScheduler 抛,避免一个飞轮 job 拖垮整个调度器。
  2. `run_flywheel_watchdog()`:按各 job 的既定周期判"连续 N 个周期未成功",拉/消 AIOps 告警。
  3. 两条闸门规则:自动调研轮开关被熔断关掉、写作策略 active 版本数为 0 —— 这两个是
     "管道接上了但阀门是关的"的静默死因,和 job 失败同等级可见。

告警通道复用既有 AIOps(`ai_ops_alerts` + 管理端监控告警板块),不新造第二套通知面。
"""
from __future__ import annotations

import functools
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from db.flywheel_job_heartbeat_db import (
    get_job_summaries,
    heartbeat_failure,
    heartbeat_start,
    heartbeat_success,
)

logger = logging.getLogger("GEO-FlywheelHeartbeat")

DAY = 86400.0

ALERT_RULE_JOB_FAILED = "flywheel_job_failed"
ALERT_RULE_JOB_STALE = "flywheel_job_stale"
ALERT_RULE_GATE_CLOSED = "flywheel_gate_closed"

WATCHDOG_JOB_KEY = "flywheel_watchdog"

# 归因账本静默阈值(WO_DELIVERY_FLYWHEEL_CLOSURE §2.1.4 明写 48h)。
LEDGER_SILENCE_ALERT_SECONDS = 48 * 3600.0


class FlywheelJobSpec(dict):
    """薄封装,便于 `spec.name` 之外仍可当 dict 用(前端/测试直接 json 序列化)。"""

    @property
    def stale_after_seconds(self) -> float:
        return float(self["period_seconds"]) * float(self["alert_after_periods"])


def _spec(
    job_key: str,
    name: str,
    period_seconds: float,
    *,
    alert_after_periods: float = 2.0,
    severity: str = "warn",
    why: str = "",
) -> FlywheelJobSpec:
    return FlywheelJobSpec(
        job_key=job_key,
        name=name,
        period_seconds=float(period_seconds),
        alert_after_periods=float(alert_after_periods),
        severity=severity,
        why=why,
    )


# 飞轮 job 注册表 —— 新增飞轮 job 必须在此登记,否则 watchdog 看不见它。
FLYWHEEL_JOBS: dict[str, FlywheelJobSpec] = {
    s["job_key"]: s
    for s in (
        _spec(
            "research_auto_round", "调研采集轮(每月 1/16 自动发起)", 15 * DAY,
            alert_after_periods=1.5, severity="error",
            why="采集轮不跑 = 飞轮上游断水,全部反哺链路一起停。",
        ),
        _spec(
            "research_missed_round_catchup", "采集轮漏跑自愈巡查(每 6 小时)", 6 * 3600,
            alert_after_periods=4.0, severity="warn",
            why="自愈巡查本身停了 = 漏跑的采集轮不会再被补上。",
        ),
        _spec(
            "media_effective_pool_distill", "媒体推荐池蒸馏(每周一 03:30)", 7 * DAY,
            alert_after_periods=2.0, severity="error",
            why="池子不刷新 = 发布推荐一直用旧证据打分。",
        ),
        _spec(
            "publish_outcome_sync", "投放结果回流(每日 04:15)", 1 * DAY,
            alert_after_periods=3.0, severity="warn",
            why="投放结果不回流 = 池蒸馏拿不到新的 outcome 信号。",
        ),
        _spec(
            "media_entity_outcome_sync", "媒体实体效果回流(每周一 05:00)", 7 * DAY,
            alert_after_periods=2.0, severity="warn",
            why="outcome_rollup 不回写 = shadow_score 的 outcome 权重恒为 0。",
        ),
        _spec(
            "writing_outcome_backfill", "写作效果闭环回写(每日 04:20)", 1 * DAY,
            alert_after_periods=3.0, severity="warn",
            why="效果不回写 = 写作版本好坏永远无据可判。",
        ),
        _spec(
            "writing_effectiveness_review", "写作有效性复核报告(每月 1/16 05:40)", 15 * DAY,
            alert_after_periods=1.5, severity="warn",
            why="报告不出 = 北极星指标无人复核。",
        ),
        _spec(
            "corpus_value_labeling", "语料价值标注(每日 05:10)", 1 * DAY,
            alert_after_periods=3.0, severity="warn",
            why="语料不打标签 = 两万多条引用继续无人消费,B3/B4 与写作取材都没素材可用。",
        ),
        _spec(
            "writing_assignment_outcome_backfill", "写作策略指派效果回流(每日 04:50)", 1 * DAY,
            alert_after_periods=3.0, severity="warn",
            why="指派不回流 = assignments 有水但 outcome_events 仍是 0 行。",
        ),
        _spec(
            "article_attribution_sync", "归因账本同步(每日 04:05)", 1 * DAY,
            alert_after_periods=2.0, severity="error",
            why=(
                "账本不同步 = 闭环没有事实源:写作效果回写、渠道有效性白名单、"
                "「哪个 URL 被哪个引擎引用」三件事同时失明。"
                "2026-08-06 之前这条链路根本不存在,恒 0 行也没人看得见。"
            ),
        ),
        _spec(
            "source_signal_lineage_backfill", "引用信号血缘回填(每日 03:40)", 1 * DAY,
            alert_after_periods=3.0, severity="warn",
            why="血缘不回填 = 结构研究只能靠 url_hash JOIN 技巧凑,outcome 归因没有落脚点。",
        ),
    )
}


# ============================================================
# 告警通道(复用 AIOps)
# ============================================================

def _raise_alert(rule_key: str, fingerprint: str, severity: str, title: str,
                 detail: str, payload: Optional[dict[str, Any]] = None) -> None:
    try:
        from db import ai_ops_db
        ai_ops_db.upsert_alert(
            rule_key, severity=severity, title=title, detail=detail,
            fingerprint=fingerprint, payload=payload or {},
        )
    except Exception as exc:
        # 告警通道自身故障不能再吞掉 —— 至少留 error 日志(cron 日志是最后一道人眼面)。
        logger.error("[FlywheelHeartbeat] 告警落库失败 rule=%s fp=%s: %s", rule_key, fingerprint, exc)


def _resolve_alert(rule_key: str, fingerprint: str) -> None:
    try:
        from db import ai_ops_db
        ai_ops_db.resolve_alerts(rule_key, fingerprint)
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 告警恢复失败 rule=%s fp=%s: %s", rule_key, fingerprint, exc)


# ============================================================
# 执行包裹
# ============================================================

_PROCESSED_KEYS = (
    "processed", "written", "distilled", "classified", "updated",
    "synced", "count", "rows", "inserted", "total",
)


def _extract_processed(result: Any) -> int:
    if isinstance(result, dict):
        for key in _PROCESSED_KEYS:
            value = result.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
    if isinstance(result, int) and not isinstance(result, bool):
        return int(result)
    return 0


def _result_error(result: Any) -> Optional[str]:
    """识别"没抛异常但内部已失败"的返回值(项目里大量 job 用 {'status':'error'} 收尾)。"""
    if not isinstance(result, dict):
        return None
    if str(result.get("status") or "").lower() in {"error", "failed"}:
        return str(result.get("error") or result.get("message") or "job returned status=error")
    err = result.get("error")
    if err:
        return str(err)
    return None


def _summarized_detail(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[str(key)[:60]] = value if not isinstance(value, str) else value[:300]
            if len(out) >= 20:
                break
        return out
    if result is None:
        return {}
    return {"result": str(result)[:300]}


def run_tracked(job_key: str, fn: Callable[..., Any], *args: Any,
                reraise: bool = False, **kwargs: Any) -> Any:
    """跑一个飞轮 job 并留痕。返回被包裹函数的返回值(失败时返回 None)。"""
    spec = FLYWHEEL_JOBS.get(job_key)
    run_key = heartbeat_start(job_key)
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("[FlywheelHeartbeat] job 失败 job_key=%s: %s", job_key, message)
        heartbeat_failure(job_key, run_key, error=message)
        _raise_alert(
            ALERT_RULE_JOB_FAILED, job_key,
            (spec or {}).get("severity", "warn"),
            f"飞轮任务执行失败:{(spec or {}).get('name', job_key)}",
            f"{message}\n影响:{(spec or {}).get('why', '')}".strip(),
            {"job_key": job_key, "error": message},
        )
        if reraise:
            raise
        return None
    except BaseException as exc:  # KeyboardInterrupt / SystemExit:落表后必须继续中止
        heartbeat_failure(job_key, run_key, error=f"{type(exc).__name__}: {exc}")
        raise

    inner_error = _result_error(result)
    if inner_error:
        logger.error("[FlywheelHeartbeat] job 返回失败态 job_key=%s: %s", job_key, inner_error)
        heartbeat_failure(job_key, run_key, error=inner_error, detail=_summarized_detail(result))
        _raise_alert(
            ALERT_RULE_JOB_FAILED, job_key,
            (spec or {}).get("severity", "warn"),
            f"飞轮任务执行失败:{(spec or {}).get('name', job_key)}",
            f"{inner_error}\n影响:{(spec or {}).get('why', '')}".strip(),
            {"job_key": job_key, "error": inner_error},
        )
        return result

    processed = _extract_processed(result)
    heartbeat_success(job_key, run_key, processed=processed, detail=_summarized_detail(result))
    _resolve_alert(ALERT_RULE_JOB_FAILED, job_key)
    _resolve_alert(ALERT_RULE_JOB_STALE, job_key)
    logger.info("[FlywheelHeartbeat] job 成功 job_key=%s processed=%s", job_key, processed)
    return result


def flywheel_job(job_key: str, *, reraise: bool = False) -> Callable:
    """把飞轮 job 包进统一心跳。被包裹函数内部**不要**再 `except → warning` 吞异常。"""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return run_tracked(job_key, fn, *args, reraise=reraise, **kwargs)
        wrapper.__flywheel_job_key__ = job_key  # type: ignore[attr-defined]
        return wrapper
    return decorator


# ============================================================
# 看门狗
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, datetime):
        return None
    ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return (_now() - ts).total_seconds()


def _humanize(seconds: Optional[float]) -> str:
    if seconds is None:
        return "从未成功"
    days = seconds / DAY
    if days >= 1:
        return f"{days:.1f} 天前"
    return f"{seconds / 3600:.1f} 小时前"


def evaluate_job_health() -> list[dict[str, Any]]:
    """只读评估。返回每个注册 job 的健康快照(供 watchdog 与管理端仪表共用)。"""
    summaries = get_job_summaries(FLYWHEEL_JOBS.keys())
    out: list[dict[str, Any]] = []
    for job_key, spec in FLYWHEEL_JOBS.items():
        summary = summaries.get(job_key) or {}
        success_age = _age_seconds(summary.get("last_success_at"))
        stale_after = spec.stale_after_seconds
        never_succeeded = summary.get("last_success_at") is None
        # 从未成功过:本表是新账本,部署当天所有 job 都"从未成功"。用"账本自身年龄"兜一层,
        # 避免上线瞬间七条告警齐响 —— 只有过了该 job 一个完整周期仍没有成功记录才算真异常。
        stale = bool(success_age is not None and success_age > stale_after)
        out.append({
            **spec,
            "last_run_at": summary.get("last_run_at"),
            "last_status": summary.get("last_status"),
            "last_success_at": summary.get("last_success_at"),
            "last_error": summary.get("last_error"),
            "last_processed": summary.get("last_processed"),
            "consecutive_failures": int(summary.get("consecutive_failures") or 0),
            "runs_7d": int(summary.get("runs_7d") or 0),
            "success_age_seconds": success_age,
            "success_age_human": _humanize(success_age),
            "never_succeeded": never_succeeded,
            "stale": stale,
            "healthy": bool(not stale and not never_succeeded),
        })
    return out


def _ledger_age_seconds() -> Optional[float]:
    """账本自身的年龄:最早一条心跳距今多久。用于"从未成功"判定的宽限。"""
    try:
        from db.connection import get_connection
        from db.flywheel_job_heartbeat_db import init_flywheel_heartbeat_tables
        init_flywheel_heartbeat_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT MIN(started_at) AS first_at FROM flywheel_job_heartbeats")
            row = cur.fetchone()
            return _age_seconds(row["first_at"]) if row else None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 账本年龄查询失败: %s", exc)
        return None


def evaluate_gate_health() -> list[dict[str, Any]]:
    """闸门规则:管道接好了但阀门关着,同样是静默死。全部只读。"""
    findings: list[dict[str, Any]] = []

    # ① 自动调研轮开关(熔断后会被自动置 false 并摘掉 job)
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value_json, updated_by, updated_at FROM geo_research_config WHERE key = 'cron_enabled'"
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None or not isinstance(row["value_json"], bool):
            findings.append({
                "key": "research_cron_gate", "severity": "error", "firing": True,
                "title": "自动调研开关不可判定",
                "detail": "geo_research_config.cron_enabled 缺失或类型非法,自动轮按 fail-closed 不会触发。",
            })
        elif not row["value_json"]:
            findings.append({
                "key": "research_cron_gate", "severity": "error", "firing": True,
                "title": "自动调研已暂停(熔断或人工关闭)",
                "detail": (
                    f"cron_enabled=false(最后修改人 {row['updated_by']} · {row['updated_at']})。"
                    "在管理员重新开启前,每月 1/16 的采集轮不会发起。"
                ),
            })
        else:
            findings.append({"key": "research_cron_gate", "firing": False})
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 调研闸门检查失败: %s", exc)

    # ② 写作策略 active 版本数(0 = 指派链路无水)
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS active_count FROM writing_strategy_versions WHERE status = 'active'"
            )
            active_count = int((cur.fetchone() or {}).get("active_count") or 0)
        finally:
            conn.close()
        if active_count <= 0:
            findings.append({
                "key": "writing_strategy_active", "severity": "warn", "firing": True,
                "title": "写作策略无已启用版本",
                "detail": (
                    "writing_strategy_versions 里 active 版本数为 0,生成端只能走行业基线。"
                    "指派账本会记为 baseline,不会产生按版本归因的效果数据 —— 需要管理员审核启用一个版本。"
                ),
            })
        else:
            findings.append({"key": "writing_strategy_active", "firing": False})
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 写作策略闸门检查失败: %s", exc)

    # ③ 归因账本静默(WO_DELIVERY_FLYWHEEL_CLOSURE §2.1.4「静默超 48h 即报警」)
    #
    # 🔴 为什么这条不能只判"job 有没有跑":job 天天跑、天天 succeeded、天天 written=0 —— 这
    #    正是 2026-08-06 之前的实况(writing_outcome_backfill 心跳 10/10 succeeded,
    #    writing_strategy_outcome_events 恒 0 行)。**"跑了"和"出水了"是两件事**,
    #    上面的 job 心跳只能证明前者,这条闸判的是后者。
    #
    # 🔴 判据必须可证伪(工单原话:"恒绿的探针视同没有"):
    #    - 账本表不存在      → firing(不是"没问题",是"根本没建")
    #    - 账本 0 行         → firing
    #    - 最后写入 > 48h    → firing
    #    - 有行且 ≤48h       → 不 firing
    #    反向对照见 tests/flywheel_close_loop/test_attribution_ledger_2026_08_06.py::
    #    test_ledger_silence_gate_fires_and_clears(四种输入四种结论,不是四种输入一个结论)。
    # [P0-3 观察面 2026-08-14] 三桥配对率随心跳下发(周报口径;info 级,不 firing)。
    # 🔴 比率带「已配对子样本」限定语,任何展示端不得剥离(工单红线 6)。
    try:
        from services.article_attribution_ledger import bridge_pairing_rates

        _bridges = bridge_pairing_rates()
        if _bridges.get("available"):
            findings.append({
                "key": "bridge_pairing_rates", "severity": "info", "firing": False,
                "title": "三桥配对率(已配对子样本覆盖率)",
                "detail": (
                    f"J4 发布桥 {_bridges['j4_publication_bridge']['paired']}/"
                    f"{_bridges['j4_publication_bridge']['total']} · "
                    f"J8 identity {_bridges['j8_identity_bridge']['paired']}/"
                    f"{_bridges['j8_identity_bridge']['total']} · "
                    f"J9 outcome {_bridges['j9_outcome_bridge']['paired']}/"
                    f"{_bridges['j9_outcome_bridge']['total']}。"
                    "口径:已配对子样本覆盖率,不得外推全量;由 P0-3 回填推进。"
                ),
                "data": _bridges,
            })
    except Exception:
        pass  # 观测面:桥率取不到不产 finding,更不阻断心跳

    try:
        from services.article_attribution_ledger import ledger_freshness

        freshness = ledger_freshness()
        if not freshness.get("available"):
            findings.append({
                "key": "attribution_ledger_silence", "severity": "error", "firing": True,
                "title": "归因账本不可读",
                "detail": (
                    "geo_article_citation_attributions 读取失败(多半是迁移 027 未跑)。"
                    f"原因:{freshness.get('reason')}。闭环归因、渠道白名单、写作效果回写三者同时失明。"
                ),
            })
        elif int(freshness.get("total_rows") or 0) <= 0:
            findings.append({
                "key": "attribution_ledger_silence", "severity": "error", "firing": True,
                "title": "归因账本 0 行",
                "detail": (
                    "账本一行都没有:要么同步 job 没跑,要么引擎被质量闸全拦。"
                    "看 flywheel_job_heartbeats 里 article_attribution_sync 最近一次的 "
                    "quality_counts —— 它会指出卡在哪道闸。"
                ),
            })
        else:
            age = freshness.get("age_seconds")
            if age is not None and float(age) > LEDGER_SILENCE_ALERT_SECONDS:
                findings.append({
                    "key": "attribution_ledger_silence", "severity": "warn", "firing": True,
                    "title": "归因账本静默超 48 小时",
                    "detail": (
                        f"最后一次写入距今 {float(age) / 3600:.1f} 小时(阈值 48h),"
                        f"现有 {freshness.get('total_rows')} 行。"
                        "静默本身不一定是故障 —— 也可能是这段时间确实没有新的被引;"
                        "但两天没有任何新归因行,值得看一眼监测是否还在跑、发布是否还有快照。"
                    ),
                })
            else:
                findings.append({"key": "attribution_ledger_silence", "firing": False})
    except Exception as exc:
        logger.warning("[FlywheelHeartbeat] 归因账本闸门检查失败: %s", exc)

    return findings


def run_flywheel_watchdog() -> dict[str, Any]:
    """看门狗本体:拉/消告警。返回 {checked, firing, resolved}。自身也进心跳账本。"""
    jobs = evaluate_job_health()
    ledger_age = _ledger_age_seconds()
    firing = 0
    resolved = 0

    for job in jobs:
        job_key = job["job_key"]
        spec = FLYWHEEL_JOBS[job_key]
        should_fire = bool(job["stale"])
        reason = ""
        if job["stale"]:
            reason = (
                f"最近一次成功在 {job['success_age_human']},"
                f"已超过 {spec['alert_after_periods']:.1f} 个执行周期。"
            )
        elif job["never_succeeded"]:
            # 账本刚建立时全员"从未成功"属正常;超过一个完整周期仍无成功才报。
            if ledger_age is not None and ledger_age > spec.stale_after_seconds:
                should_fire = True
                reason = (
                    f"心跳账本已运行 {_humanize(ledger_age)},该任务至今没有一次成功记录。"
                )

        if should_fire:
            firing += 1
            _raise_alert(
                ALERT_RULE_JOB_STALE, job_key, spec["severity"],
                f"飞轮任务长时间未成功:{spec['name']}",
                f"{reason}\n影响:{spec['why']}\n最近状态:{job['last_status'] or '无记录'}"
                + (f"\n最近错误:{job['last_error']}" if job.get("last_error") else ""),
                {
                    "job_key": job_key,
                    "last_success_at": str(job.get("last_success_at") or ""),
                    "consecutive_failures": job.get("consecutive_failures"),
                },
            )
        else:
            _resolve_alert(ALERT_RULE_JOB_STALE, job_key)
            resolved += 1

    for finding in evaluate_gate_health():
        if finding.get("firing"):
            firing += 1
            _raise_alert(
                ALERT_RULE_GATE_CLOSED, finding["key"], finding.get("severity", "warn"),
                finding["title"], finding.get("detail", ""), {"gate": finding["key"]},
            )
        else:
            _resolve_alert(ALERT_RULE_GATE_CLOSED, finding["key"])
            resolved += 1

    return {"checked": len(jobs), "firing": firing, "resolved": resolved, "processed": len(jobs)}


run_flywheel_watchdog_job = flywheel_job(WATCHDOG_JOB_KEY)(run_flywheel_watchdog)


def selftest_failing_job() -> dict[str, Any]:
    """判别锁用的"必失败 job"。仅在 FLYWHEEL_HEARTBEAT_SELFTEST=1 时由调度器注册。

    存在意义:验收要求"注入一个必失败 job → 心跳表出现 failed 且告警产生"。
    生产默认不注册,翻开关即可在真实调度路径上验证心跳与告警链路。
    """
    raise RuntimeError("flywheel heartbeat selftest: 这是刻意失败的探针任务")


selftest_failing_job_wrapped = flywheel_job("flywheel_selftest_failing")(selftest_failing_job)


def selftest_enabled() -> bool:
    return os.getenv("FLYWHEEL_HEARTBEAT_SELFTEST", "").strip().lower() in {"1", "true", "yes", "on"}
