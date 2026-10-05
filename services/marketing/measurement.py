"""
services/marketing/measurement.py — 度量学习(相关转化回测 + 框架标签归因 + 营销周报)

总设计 §9 归因保守 / 工程 §F:
  · 相关转化三指标(触达后 N 天内 ①充值 ②回访 ③再用功能)· 无对照组禁"因果/带来收入"表述。
  · 每案件观察窗口(默认 7/14 天)自动跑三指标写 marketing_measurements。
  · 框架标签归因:复盘按技能包标签聚合相关转化(marketing_db.measurement_summary_by_skillpack)。
  · 营销周报(每周一 Agent 产,含"AI 分析仅供参考"底注,术语守卫双层)。
  · 对照组:schema(measurements.is_control)+ 分组逻辑 gate 到 control_group flag(样本量达标点亮)。

术语守卫:report 文本禁"因果/带来 X 收入/增长归因",统一"触达后 N 日相关转化"。
"""
import logging
from datetime import date

from db import marketing_db

logger = logging.getLogger("GEO-Marketing-Measure")

# 术语守卫禁用词(无对照组期间)
CAUSAL_BANNED = ["因果", "带来收入", "带来了", "增长归因", "贡献了", "提升了收入", "驱动增长"]
REPORT_FOOTER = "AI 分析仅供参考,请结合业务实际判断。"


def _measure_case(case: dict) -> int:
    """对一个 executed 案件跑三指标聚合回测(观察窗口到期才测)。返回写入指标数。"""
    from db.connection import get_connection
    window = int(case.get("observation_window_days") or 7)
    case_id = case["id"]
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 观察窗口是否到期(executed_at + window <= now)
        cur.execute("SELECT (executed_at IS NOT NULL AND executed_at + (%s||' days')::interval <= NOW()) AS due, "
                    "executed_at FROM marketing_cases WHERE id=%s", (str(window), case_id))
        row = cur.fetchone()
        if not row or not row["due"]:
            return 0
        # 触达人群(sent)+ 三指标聚合(全部 DB 侧日期数学)
        cur.execute(
            """
            WITH touched AS (
              SELECT user_id, MIN(created_at) AS touch_at
              FROM marketing_touch_events
              WHERE case_id=%s AND status='sent'
              GROUP BY user_id
            )
            SELECT
              COUNT(*)::int AS targeted,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM recharge_orders ro WHERE ro.user_id=t.user_id
                  AND ro.payment_status='paid' AND ro.paid_at > t.touch_at
                  AND ro.paid_at < t.touch_at + (%s||' days')::interval))::int AS recharge_conv,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM users u WHERE u.id=t.user_id
                  AND u.last_active_at > t.touch_at
                  AND u.last_active_at < t.touch_at + (%s||' days')::interval))::int AS revisit_conv,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM point_transactions pt WHERE pt.user_id=t.user_id AND pt.type='consume'
                  AND pt.created_at > t.touch_at
                  AND pt.created_at < t.touch_at + (%s||' days')::interval))::int AS reuse_conv
            FROM touched t
            """,
            (case_id, str(window), str(window), str(window)))
        agg = cur.fetchone() or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[measure] 案件 %s 回测失败: %s", case_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()

    targeted = int(agg.get("targeted", 0) or 0)
    if targeted == 0:
        return 0
    metrics = {
        "recharge": int(agg.get("recharge_conv", 0) or 0),
        "revisit": int(agg.get("revisit_conv", 0) or 0),
        "feature_reuse": int(agg.get("reuse_conv", 0) or 0),
    }
    n = 0
    for metric, conv in metrics.items():
        marketing_db.upsert_measurement(
            case_id=case_id, metric_type=metric, window_days=window,
            converted=(conv > 0),
            result={"targeted": targeted, "converted": conv,
                    "rate_pct": round(100.0 * conv / targeted, 2)})
        n += 1
    marketing_db.add_event(event_type="measured", case_id=case_id,
                           message=f"相关转化回测(窗口 {window} 天):触达 {targeted} · "
                                   f"充值 {metrics['recharge']} / 回访 {metrics['revisit']} / 再用 {metrics['feature_reuse']}",
                           payload={"targeted": targeted, "metrics": metrics, "window": window})
    return n


def run_measurement(force: bool = False) -> dict:
    """扫描 executed 案件,观察窗口到期者跑三指标回测。"""
    cases = marketing_db.list_cases(status="executed", limit=500)
    measured = 0
    for c in cases:
        measured += _measure_case(c)
    return {"cases_scanned": len(cases), "metrics_written": measured}


def run_measurement_job() -> None:
    """Scheduler 入口(每日)。永不抛异常。"""
    try:
        r = run_measurement(force=False)
        if r["metrics_written"]:
            logger.info("[measure] %s", r)
    except Exception as e:  # noqa: BLE001
        logger.warning("[measure] 回测轮异常: %s", e)


# ============================================================================
# 营销周报(Agent 每周一产 · 术语守卫双层)
# ============================================================================
def _scrub_causal(text: str) -> str:
    out = text or ""
    for w in CAUSAL_BANNED:
        out = out.replace(w, "相关")
    return out


def build_weekly_report() -> dict:
    """组装周报(纯 SQL 聚合 + 术语守卫;LLM 可选叠加,默认规则版)。"""
    ledger = marketing_db.read_view_one("marketing_v_self_ledger") or {}
    funnel = marketing_db.read_view_one("marketing_v_funnel") or {}
    attribution = marketing_db.measurement_summary_by_skillpack()
    status_counts = marketing_db.count_cases_by_status()

    lines = [
        f"# 营销周报 · {date.today().isoformat()}",
        "",
        "## 上周动作回测(相关转化口径)",
        f"- 建议案件:待审批 {ledger.get('cases_pending',0)} · 已批准 {ledger.get('cases_approved',0)} · "
        f"已执行 {ledger.get('cases_executed',0)} · 已驳回 {ledger.get('cases_rejected',0)}",
        f"- 触达成功:{ledger.get('touches_sent',0)} 次 · 相关转化命中:{ledger.get('measured_conversions',0)}",
        f"- 赠送算力(已入账):{ledger.get('grants_applied_points',0)}",
        "",
        "## 漏斗快照",
        f"- 注册 {funnel.get('registered',0)} → 激活 {funnel.get('activated',0)} "
        f"→ 首次消费 {funnel.get('first_spend',0)} → 首充 {funnel.get('first_charge',0)} "
        f"→ 复购 {funnel.get('repurchase',0)}",
        "",
        "## 框架标签归因(哪个角度的文案相关转化更高)",
    ]
    if attribution:
        for a in attribution[:8]:
            lines.append(f"- {a['skill_pack']}:测量 {a['measured']} · 相关转化 {a['converted']} "
                         f"({a.get('conv_rate_pct')}%)")
    else:
        lines.append("- 数据积累中(样本不足,暂不展示归因)")
    lines += [
        "",
        "## 本周建议",
        "- 优先清空待审批队列(今日军情),批准的自动执行;",
        "- 关注相关转化较高的框架标签,可上调其技能包 weight(人工调,不自动);",
        "",
        f"> {REPORT_FOOTER}",
    ]
    markdown = _scrub_causal("\n".join(lines))
    summary = _scrub_causal(f"上周触达 {ledger.get('touches_sent',0)} 次 · 相关转化 "
                            f"{ledger.get('measured_conversions',0)} · 待审批 {status_counts.get('pending',0)}")
    return {"markdown": markdown, "summary": summary,
            "metrics": {"ledger": ledger, "funnel": funnel, "attribution": attribution}}


def generate_weekly_report(force: bool = False) -> dict:
    """产周报并落 marketing_events(type=weekly_report · 幂等到当周)。
    [返工 R7] 真判重:当周已有 weekly_report 事件则跳过(force=True 强制重生成)。"""
    iso_week = date.today().isocalendar()
    week_key = f"{iso_week[0]}-W{iso_week[1]:02d}"
    if not force:
        try:
            for ev in marketing_db.list_events(event_type="weekly_report", limit=10):
                payload = ev.get("payload_jsonb") or {}
                if payload.get("iso_week") == week_key:
                    return {"ok": True, "week": week_key, "summary": payload.get("summary", ""),
                            "skipped": "already_generated"}
        except Exception as e:  # noqa: BLE001 · 判重失败宁可多产一份也不断周报
            logger.warning("[measure] 周报判重失败(继续生成): %s", e)
    report = build_weekly_report()
    marketing_db.add_event(event_type="weekly_report", severity="info",
                           message=f"营销周报 {week_key}",
                           payload={"iso_week": week_key, **report})
    return {"ok": True, "week": week_key, "summary": report["summary"]}


def run_weekly_report_job() -> None:
    """Scheduler 入口(每周一)。永不抛异常。"""
    try:
        if not marketing_db.is_flag_enabled("marketing_agent.enabled", default=False):
            return
        r = generate_weekly_report(force=False)
        logger.info("[measure] 周报已产: %s", r.get("week"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[measure] 周报生成异常: %s", e)


def latest_weekly_report() -> dict:
    """取最近一份周报(effect 页 AI 复盘卡)。"""
    for ev in marketing_db.list_events(limit=100):
        if ev.get("event_type") == "weekly_report":
            return ev.get("payload_jsonb") or {}
    return {}


# ============================================================================
# 效果复盘数据(4 级可测漏斗 + KPI · 参考图 04)
# ============================================================================
def effect_overview() -> dict:
    """效果复盘:4 级漏斗(目标→触达→N日回访→N日充值)+ KPI + 相关转化三指标。"""
    from db.connection import get_connection
    conn = get_connection()
    funnel = {"targeted": 0, "reached": 0, "revisit": 0, "recharge": 0}
    try:
        cur = conn.cursor()
        cur.execute("""SELECT
            COUNT(DISTINCT user_id)::int AS targeted,
            COUNT(DISTINCT user_id) FILTER (WHERE status='sent')::int AS reached
          FROM marketing_touch_events""")
        r = cur.fetchone() or {}
        funnel["targeted"] = int(r.get("targeted", 0) or 0)
        funnel["reached"] = int(r.get("reached", 0) or 0)
        cur.execute("""SELECT metric_type, SUM((result_jsonb->>'converted')::int)::int AS conv
                       FROM marketing_measurements GROUP BY metric_type""")
        for row in cur.fetchall():
            if row["metric_type"] == "revisit":
                funnel["revisit"] = int(row["conv"] or 0)
            elif row["metric_type"] == "recharge":
                funnel["recharge"] = int(row["conv"] or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("[measure] effect_overview 失败: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()

    control_on = marketing_db.is_flag_enabled("marketing_agent.control_group.enabled", default=False)
    return {
        "funnel_4level": funnel,
        "kpi": {
            "reached": funnel["reached"],
            "correlated_conversion": funnel["recharge"] + funnel["revisit"],
            "recharge_conversion": funnel["recharge"],
        },
        "attribution": marketing_db.measurement_summary_by_skillpack(),
        "ai_review": latest_weekly_report(),
        "control_group": {"enabled": control_on, "note": "样本量达标后由老板点亮"},
        "term_note": "无对照组期间统一'触达后 N 日相关转化',不作因果宣称。",
    }
