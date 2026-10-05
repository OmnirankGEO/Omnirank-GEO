"""
AI Ops 日报生成 · v2(运维 + 运营)· 2026-07-02

收集运维/运营数据 → 生成结构化 metrics + Markdown → 写 ai_ops_reports((date,type) 幂等)。

口径纪律(CTO 审核 + 手册 + FULL_OPERATION_EXECUTION_BRIEF §5):
  - 金额内部 cents(withdrawal_requests 例外:表本身存元);展示统一转元。
  - 已支付充值 payment_status='paid' + COALESCE(paid_at, created_at)。
  - agent_revenue_ledger 有效收益 reversed_at IS NULL;status 只认 frozen/settled/cancelled。
  - 运营段由 services/ai_ops/report_metrics 包采集:每段独立失败,子指标独立失败,
    失败标 unavailable 带原因,绝不编数字;拿不准口径的段如实标待接入。
  - SQL 只读;日报不写任何业务表。

日报本身是纯 DB 读 + 写 ai_ops_reports(无 Codex/无 worktree),可在 web 进程内(scheduler)
直接跑,也可被手动 API/命令台同步调用。老板待办 ≤ 3 项,
优先级:资金 > 生产失败 > 待审批 > bug 反馈 > 交付失败。
"""

import logging
from datetime import date as _date
from typing import Optional

from db import ai_ops_db as aiops_db
from services.ai_ops.report_metrics import collect_all, sub_ok

logger = logging.getLogger("AiOps-Report")


def _collect_new_bug_count(report_date) -> Optional[int]:
    """当日新增 bug 反馈数(faq_feedback.kind='bug' 按 created_at 归日)。失败 None 不编数。"""
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM faq_feedback WHERE kind = 'bug' AND created_at::date = %s",
            (report_date,))
        row = cur.fetchone() or {}
        return int(row.get("cnt") or 0)
    except Exception as e:  # noqa: BLE001
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        logger.warning("[report] 当日新增反馈读取失败: %s", e)
        return None
    finally:
        if conn is not None:
            conn.close()


def _collect_metrics(report_date=None) -> dict:
    """收集运维(ops/审批/反馈)+ 运营(report_metrics 包 6 段)指标。"""
    task_counts = aiops_db.count_tasks_by_status()
    approval_counts = aiops_db.count_approvals_by_status()
    feedback_counts = {}
    try:
        from db.faq_db import get_feedback_counts
        feedback_counts = get_feedback_counts(kind="bug")
    except Exception as e:  # noqa: BLE001
        logger.warning("[report] 读反馈计数失败: %s", e)
    rd = report_date or _date.today()
    metrics = {
        "tasks": task_counts,
        "approvals": approval_counts,
        "bug_feedback": feedback_counts,
        "new_bug_feedback_today": _collect_new_bug_count(rd),
    }
    metrics.update(collect_all(rd))   # finance/geo_delivery/monitoring/research_monitor/llm_cost/publishing
    return metrics


def _fmt_yuan(cents: int) -> str:
    return f"¥{cents / 100:.2f}"


def _derive_action_items(metrics: dict) -> list[str]:
    """老板今日待办(≤3)· 优先级:资金 > 生产失败 > 待审批 > bug 反馈 > 交付失败。"""
    items: list[str] = []

    # 1. 资金待办(只基于真实 SQL 证据)
    fin = metrics.get("finance") or {}
    fund_bits = []
    wd = fin.get("withdrawal")
    if sub_ok(wd) and wd.get("pending_count"):
        fund_bits.append(f"提现待审 {wd['pending_count']} 笔 ¥{wd['pending_amount_yuan']:.2f}")
    st = fin.get("settlement")
    if sub_ok(st) and st.get("pending_count"):
        fund_bits.append(f"服务商结算待付 {st['pending_count']} 笔 {_fmt_yuan(st['pending_amount_cents'])}")
    rf = fin.get("refund")
    if sub_ok(rf) and rf.get("open_count"):
        fund_bits.append(f"退款工单在途 {rf['open_count']} 单")
    if fund_bits:
        items.append("资金待办:" + " · ".join(fund_bits))

    # 2. 生产失败
    failed = metrics["tasks"].get("failed", 0)
    if failed:
        items.append(f"有 {failed} 个 AI 运维任务失败,需要看事件流定位")

    # 3. 待审批
    pending_ap = metrics["approvals"].get("pending", 0)
    if pending_ap:
        items.append(f"审批队列有 {pending_ap} 条待处理(L3/L4 高危动作)")

    # 4. bug 反馈
    open_bug = metrics["bug_feedback"].get("pending", 0) + metrics["bug_feedback"].get("read", 0)
    if open_bug:
        items.append(f"未结案 bug 反馈 {open_bug} 条")

    # 5. 交付失败(写作)
    geo = metrics.get("geo_delivery") or {}
    arts = geo.get("articles")
    if sub_ok(arts) and arts.get("today_failed"):
        items.append(f"今日文章生成失败 {arts['today_failed']} 篇")

    return items[:3]


def _md_unavailable(name: str, sub: dict) -> str:
    err = (sub or {}).get("error", "unknown")
    return f"- {name}:不可用({err})"


def _collect_gaps(metrics: dict) -> list[str]:
    """数据缺口清单:列出所有 unavailable 的段/子指标及原因。"""
    gaps: list[str] = []
    seg_names = {
        "finance": "财务资金链", "geo_delivery": "GEO 交付", "monitoring": "监测",
        "research_monitor": "调研监测", "llm_cost": "LLM 成本", "publishing": "发布",
    }
    for seg_key, seg_label in seg_names.items():
        seg = metrics.get(seg_key)
        if not isinstance(seg, dict):
            gaps.append(f"{seg_label}:整段缺失")
            continue
        if seg.get("status") == "unavailable":
            gaps.append(f"{seg_label}:整段不可用({seg.get('error', '')})")
            continue
        for sub_key, sub in seg.items():
            if isinstance(sub, dict) and sub.get("status") == "unavailable":
                gaps.append(f"{seg_label}/{sub_key}:{sub.get('error', '')}")
    if metrics.get("new_bug_feedback_today") is None:
        gaps.append("问题反馈/当日新增:读取失败")
    return gaps


def build_markdown(report_date, metrics: dict, action_items: list[str]) -> str:
    t = metrics["tasks"]
    a = metrics["approvals"]
    b = metrics["bug_feedback"]
    new_bugs = metrics.get("new_bug_feedback_today")
    fin = metrics.get("finance") or {}
    geo = metrics.get("geo_delivery") or {}
    mon = metrics.get("monitoring") or {}
    rsm = metrics.get("research_monitor") or {}
    llm = metrics.get("llm_cost") or {}
    pub = metrics.get("publishing") or {}

    running = t.get("running", 0) + t.get("waiting_approval", 0)
    open_bug = b.get("pending", 0) + b.get("read", 0)

    # ---- 一句话总览(只拼真实可用的数) ----
    ov = [f"运维任务在跑 {running} · 失败 {t.get('failed', 0)} · 待审批 {a.get('pending', 0)} · 未结案 bug {open_bug}"]
    rc = fin.get("recharge")
    if sub_ok(rc):
        ov.append(f"今日充值 {rc['day_paid_count']} 笔 {_fmt_yuan(rc['day_paid_amount_cents'])}")
    arts = geo.get("articles")
    if sub_ok(arts) and arts.get("today_failed"):
        ov.append(f"写作失败 {arts['today_failed']}")
    mr = mon.get("results")
    if sub_ok(mr):
        ov.append(f"今日监测 {mr['today_results']} 条(检出 {mr['today_detected']})")
    overview = " · ".join(ov)

    todo_md = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(action_items)) or "1. 今天没有需要老板处理的紧急项"

    # ---- 财务资金链 ----
    fin_lines: list[str] = []
    if sub_ok(rc):
        fin_lines.append(f"- 今日已支付充值:{rc['day_paid_count']} 笔 · {_fmt_yuan(rc['day_paid_amount_cents'])}")
        fin_lines.append(f"- 本月累计已支付充值:{rc['month_paid_count']} 笔 · {_fmt_yuan(rc['month_paid_amount_cents'])}")
    else:
        fin_lines.append(_md_unavailable("充值", rc))
    rf = fin.get("refund")
    if sub_ok(rf):
        fin_lines.append(f"- 退款工单:在途 {rf['open_count']} 单(申请合计 {_fmt_yuan(rf['open_requested_cents'])})· 今日新增 {rf['today_new']}")
    else:
        fin_lines.append(_md_unavailable("退款工单", rf))
    wd = fin.get("withdrawal")
    if sub_ok(wd):
        fin_lines.append(f"- 待提现审核:{wd['pending_count']} 笔 · ¥{wd['pending_amount_yuan']:.2f}")
    else:
        fin_lines.append(_md_unavailable("提现", wd))
    fin_lines.append("- 口径:paid+COALESCE(paid_at,created_at) · 退款在途=draft/submitted/approved/payout_pending(同财务中心)")

    # ---- GEO 业务交付 ----
    geo_lines: list[str] = []
    dg = geo.get("diagnosis")
    geo_lines.append(f"- 今日新建诊断:{dg['today_created']} 次" if sub_ok(dg) else _md_unavailable("诊断", dg))
    if sub_ok(arts):
        geo_lines.append(f"- 今日文章生成:{arts['today_generated']} 篇(失败 {arts['today_failed']})")
    else:
        geo_lines.append(_md_unavailable("写作", arts))
    br = geo.get("brands")
    geo_lines.append(f"- 今日新增品牌:{br['today_new']}" if sub_ok(br) else _md_unavailable("品牌", br))
    geo_lines.append("- 报价/人工核:待接入(selection session 口径未核实,不拼 SQL)")

    # ---- 发布与监测 ----
    pm_lines: list[str] = []
    # 主口径 = 现役媒介盒子代发 mhz_synced_orders(真实同步状态);老 publish_orders 仅 legacy 参考
    ms = pub.get("mhz_synced")
    if sub_ok(ms):
        dist = " · ".join(f"{k} {v}" for k, v in (ms.get("by_status") or {}).items()) or "无"
        pm_lines.append(f"- 今日代发订单(媒介盒子):{ms['today_orders']}(已完成 {ms['today_completed']} · 进行中 {ms['today_in_progress']} · 拒稿 {ms['today_rejected']})")
        pm_lines.append(f"  · 状态分布:{dist}")
    else:
        pm_lines.append(_md_unavailable("代发订单(媒介盒子)", ms))
    inf = pub.get("mhz_local_inflight")
    if sub_ok(inf) and inf.get("today_items"):
        pm_lines.append(f"- 本地在途 item:{inf['today_items']}(" + " · ".join(f"{k} {v}" for k, v in inf["by_status"].items()) + ")")
    leg = pub.get("legacy_publish_orders")
    if sub_ok(leg) and leg.get("today_orders"):
        pm_lines.append(f"- 老链路发布订单(已废弃 · 仅参考):{leg['today_orders']}")
    if sub_ok(mr):
        pm_lines.append(f"- 今日监测结果:{mr['today_results']} 条 · 检出 {mr['today_detected']} · 覆盖平台 {mr['platforms']}")
    else:
        pm_lines.append(_md_unavailable("监测结果", mr))
    rr = rsm.get("rounds")
    if sub_ok(rr):
        pm_lines.append(
            f"- 调研监测:运行中 {rr['running']} · failed_resumable {rr['failed_resumable']} · "
            f"今日轮次 {rr['today_rounds']}(补跑 {rr['today_missed_cron_recovery']})")
    else:
        pm_lines.append(_md_unavailable("调研监测", rr))

    # ---- 服务商经营 ----
    sv_lines: list[str] = []
    st = fin.get("settlement")
    if sub_ok(st):
        sv_lines.append(f"- 待结算申请:{st['pending_count']} 笔 · {_fmt_yuan(st['pending_amount_cents'])}")
    else:
        sv_lines.append(_md_unavailable("结算申请", st))
    lg = fin.get("ledger")
    sv_lines.append(f"- 今日冲销 ledger:{lg['reversed_today']} 条" if sub_ok(lg) else _md_unavailable("收益冲销", lg))

    # ---- AI 成本 ----
    llm_lines: list[str] = []
    lt = llm.get("today")
    if sub_ok(lt):
        llm_lines.append(f"- 今日 LLM 调用:{lt['calls']} 次 · 估算成本 ¥{lt['cost_yuan']:.2f} · 失败 {lt['fails']}")
    else:
        llm_lines.append(_md_unavailable("今日调用", lt))
    lm = llm.get("month")
    llm_lines.append(f"- 本月估算成本:¥{lm['cost_yuan']:.2f}" if sub_ok(lm) else _md_unavailable("本月成本", lm))
    tp = llm.get("top_platform")
    if sub_ok(tp) and tp.get("top"):
        llm_lines.append("- 今日 Top 平台:" + " · ".join(f"{x['platform']} {x['calls']}次" for x in tp["top"]))
    llm_lines.append("- 口径:llm_call_log.estimated_cost(元 · 与 /admin/llm-cost 同源)")

    # ---- 数据缺口 ----
    gaps = _collect_gaps(metrics)
    gaps_md = "\n".join(f"- {g}" for g in gaps) if gaps else "- 本日所有已接入段读取正常"

    new_bug_line = f"- 今日新增 bug 反馈:{new_bugs} 条\n" if new_bugs is not None else ""

    return f"""# AI 运维与运营日报 {report_date}

## 一句话总览
{overview}

## 今日需要老板处理的 3 件事
{todo_md}

## 生产运维
- AI 运维任务:queued {t.get('queued', 0)} · running {t.get('running', 0)} · 待审批 {t.get('waiting_approval', 0)} · 成功 {t.get('succeeded', 0)} · 失败 {t.get('failed', 0)} · 取消 {t.get('cancelled', 0)}
- 审批:pending {a.get('pending', 0)} · approved {a.get('approved', 0)} · rejected {a.get('rejected', 0)} · expired {a.get('expired', 0)} · executed {a.get('executed', 0)}

## 问题反馈与 AI 修复
{new_bug_line}- bug 反馈:待处理 {b.get('pending', 0)} · 已读 {b.get('read', 0)} · 已处理 {b.get('done', 0)} · 关闭 {b.get('closed', 0)}

## 财务资金链
{chr(10).join(fin_lines)}

## GEO 业务交付
{chr(10).join(geo_lines)}

## 发布与监测
{chr(10).join(pm_lines)}

## 服务商经营
{chr(10).join(sv_lines)}

## AI 成本与模型调用
{chr(10).join(llm_lines)}

## 安全与审计
- AI 运维动作全部写 ai_ops_task_events;高危动作(L3/L4)进审批队列,无自动执行
- 资金动作(退款/提现/结算/调账)永远 L4 人工;Kill Switch 开启即冻结执行

## 数据缺口
{gaps_md}
"""


def build_daily_report(
    report_date=None,
    *,
    report_type: str = "daily",
    generated_by_task_id: Optional[int] = None,
) -> int:
    """生成并 upsert 当日日报((date,type) 唯一 · 同日重复生成幂等覆盖)。返回 report id。"""
    if report_date is None:
        # 注意:调用方(scheduler/worker)在真实运行时提供当天日期;这里兜底用 today。
        report_date = _date.today()
    metrics = _collect_metrics(report_date)
    action_items = _derive_action_items(metrics)
    markdown = build_markdown(report_date, metrics, action_items)
    summary = markdown.split("## 一句话总览\n", 1)[-1].split("\n", 1)[0].strip()
    report_id = aiops_db.save_report(
        report_date,
        report_type=report_type,
        status="ready",
        markdown=markdown,
        summary=summary,
        metrics=metrics,
        action_items=action_items,
        generated_by_task_id=generated_by_task_id,
    )
    return report_id


def build_report_for_task(task: dict) -> int:
    """Worker 处理 kind='report' 任务时调用。"""
    ctx = task.get("source_context_jsonb") or {}
    rd = ctx.get("report_date") if isinstance(ctx, dict) else None
    if rd:
        try:
            from datetime import datetime
            rd = datetime.strptime(rd, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            rd = None
    return build_daily_report(rd, generated_by_task_id=task.get("id"))


def generate_daily_report_with_task(
    report_date=None,
    *,
    source_type: str = "schedule",
    created_by: Optional[int] = None,
) -> tuple[dict, int]:
    """
    走任务总线生成日报(P1-2):创建幂等 kind='report' 任务 → 建报告 → 写事件 → 标 succeeded。
    scheduler 与手动 API 共用,保证有 task/events + generated_by_task_id,不绕过审计。
    返回 (task, report_id)。
    """
    if report_date is None:
        report_date = _date.today()
    key = f"report:{report_date.isoformat()}:daily"
    task, _created = aiops_db.create_task(
        kind="report", source_type=source_type, source_id=report_date.isoformat(),
        source_context={"report_date": report_date.isoformat(), "report_type": "daily"},
        title=f"生成日报 {report_date}", instruction="收集当日运维/运营数据生成日报",
        risk_level="L0", priority="P2", created_by=created_by, task_key=key,
    )
    tid = task["id"]
    aiops_db.update_task_status(tid, "running")
    aiops_db.append_event(tid, "report_started", "开始生成日报")
    try:
        rid = build_daily_report(report_date, generated_by_task_id=tid)
        # 任务摘要放日报真实摘要(老板点开任务看到的是结论,不是一句 report_id=N);
        # result 带 report_id/report_date,前端据此提供「打开日报」直达按钮。
        report = aiops_db.get_report(report_date, report_type="daily") or {}
        r_summary = (report.get("summary") or "").strip()
        aiops_db.append_event(tid, "report_generated", f"日报已生成 report_id={rid}")
        aiops_db.update_task_status(
            tid, "succeeded",
            summary=(r_summary[:400] if r_summary else f"日报已生成(#{rid}),正文在报告中心"),
            result={"report_id": rid, "report_date": report_date.isoformat(), "report_type": "daily"},
        )
        return task, rid
    except Exception as e:  # noqa: BLE001
        aiops_db.append_event(tid, "error", f"日报生成失败: {e}", severity="error")
        aiops_db.update_task_status(tid, "failed", summary=f"日报失败: {e}")
        raise


def run_daily_report_job() -> None:
    """
    Scheduler 入口 · 受总开关 + Kill Switch gate(P0-2)· 走任务总线(P1-2)。
    (date,type) 幂等,蓝绿/重复注册不双写。
    """
    if aiops_db.is_kill_switch_enabled():
        logger.info("[report] Kill Switch 开启,跳过定时日报")
        return
    if not aiops_db.is_flag_enabled("ai_ops.enabled", default=False):
        logger.info("[report] ai_ops.enabled=false,跳过定时日报")
        return
    try:
        task, rid = generate_daily_report_with_task(_date.today(), source_type="schedule")
        logger.info("[report] 定时日报已生成 report_id=%s task=%s", rid, task["id"])
    except Exception as e:  # noqa: BLE001
        logger.exception("[report] 定时日报生成失败: %s", e)
