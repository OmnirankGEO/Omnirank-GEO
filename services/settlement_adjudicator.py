"""结算 AI 审查员 —— settlement_manual 单的自动裁定 + 升级通知。

工单:`WO_SETTLEMENT_AI_ADJUDICATOR_2026-08-25.md`(Review 2026-08-25 立单 · 迁移号 049 已登记)。

## 这个模块**不动钱**

裁定核心是**机械规则**,钱由既有状态机动:

    审查员 --decision(commit|release)--> verify_and_resolve_manual
        └─ 单事务 FOR UPDATE + CAS settlement_manual → *_pending
        └─ 交还 diagnosis_run_sweeper 正常结算
            └─ commit 档的**已履约比例**由既有 `_partial_commit_points` 算

三条好处不是"我们遵守纪律"换来的,是结构决定的:

1. **零新资金口** —— 本模块不 import 任何 freeze/settle/release 原语,
   一行改余额的代码都没有(判据侧有一条 census 钉死这一点);
2. **与人工 admin 天然互斥** —— 走的是 admin 处置的同一把 CAS(只认 `settlement_manual`),
   `FOR UPDATE` 保证并发时恰一个成功,另一个拿到 ok=False 而不是崩;
3. **比例算法零重写** —— 我们只出二值 decision,比例归 sweeper。

## 🔴 禁止 LLM 决定金额

`adjudicate()` 是纯函数式机械规则,输入证据包、输出 decision。
LLM 至多用于给裁定记录写人话摘要,且摘要**不参与执行**(本版未接 LLM)。

## 🔴 系数不写在代码里

限额 / 同因阈值全部读 `system_settings`(后台可调)。**读不到就 fail-closed**:
不自动处置、不动钱。所以本文件里没有任何一个业务数字 —— 未配置的生产环境
装上这个包等于零行为变化,配了才开始工作。
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-SettlementAdjudicator")

#: 规则版本 —— 退款争议要能答"这单是哪版规则判的"。改规则必须改这里。
RULE_VERSION = "settlement-adjudicator-v1-2026-08-25"

#: 裁定记录表(迁移 049)。
_TABLE = "diagnosis_settlement_adjudications"

#: 后台可调系数的 key。**值不在代码里**,读不到 → 不自动处置。
SETTING_ENABLED = "settlement_adjudicator_enabled"
SETTING_MAX_FROZEN_POINTS = "settlement_adjudicator_max_auto_frozen_points"
SETTING_SAME_CAUSE_LIMIT = "settlement_adjudicator_same_cause_streak_limit"

#: 升级三触发(WO §2)。
ESCALATE_OVER_LIMIT = "over_limit"
ESCALATE_EVIDENCE_INCOMPLETE = "evidence_incomplete"
ESCALATE_SAME_CAUSE_STREAK = "same_cause_streak"

#: 审查员在既有审计轨里的署名。人工是 "用户名(uid=N)",自动是 "system",
#: 审查员单独一档 —— 事后对账要能一眼分出"这单是 AI 判的还是人判的"。
OPERATOR = "ai_adjudicator"

_TICK_LIMIT = 20   # 单 tick 处理上限(不是业务系数,是 tick 时长护栏)


# ---------------------------------------------------------------------------
# 系数(fail-closed)
# ---------------------------------------------------------------------------

def _setting(cur, key: str) -> Optional[str]:
    cur.execute("SELECT value FROM system_settings WHERE key=%s LIMIT 1", (key,))
    row = cur.fetchone()
    if not row:
        return None
    val = row["value"] if isinstance(row, dict) else row[0]
    return None if val is None else str(val)


def load_coefficients(cur) -> Optional[Dict[str, int]]:
    """读齐全部系数;**任何一个缺失或不合法 → 返回 None(= 本轮不自动处置任何单)**。

    fail-closed 是刻意的:没配置的环境装上这个包必须**零行为变化**,
    而不是"用一个我随手写的默认值去动别人的钱"。
    """
    enabled = _setting(cur, SETTING_ENABLED)
    if str(enabled).strip().lower() not in ("1", "true", "on", "yes"):
        return None
    try:
        max_points = int(str(_setting(cur, SETTING_MAX_FROZEN_POINTS)).strip())
        same_cause = int(str(_setting(cur, SETTING_SAME_CAUSE_LIMIT)).strip())
    except (TypeError, ValueError):
        return None
    if max_points <= 0 or same_cause <= 0:
        return None
    return {"max_frozen_points": max_points, "same_cause_limit": same_cause}


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------

@contextmanager
def _savepoint(cur, name: str):
    """在调用方事务里开一个保存点,让"这一条取数失败"真的只影响这一条。

    🔴 本仓老坑,本包第一版又踩了一次:PostgreSQL 里**一条语句失败会把整个事务置成
    aborted**,之后每一条都返回 `current transaction is aborted`。所以
    `try: cur.execute(...) except: 记一笔 missing` 看着是优雅降级,实际是**把调用方的
    事务打废了** —— 表现成"审查员什么都没做",离真因隔两层。

    连接若是 autocommit(SAVEPOINT 无事务可依附),降级成裸 try —— 那种模式下
    单条失败本来就不会污染别的语句。
    """
    nested = True
    try:
        cur.execute("SAVEPOINT " + name)
    except Exception:  # noqa: BLE001
        nested = False
    try:
        yield
    except Exception:
        if nested:
            cur.execute("ROLLBACK TO SAVEPOINT " + name)
        raise
    else:
        if nested:
            cur.execute("RELEASE SAVEPOINT " + name)


def normalize_failure_cause(run: Dict[str, Any]) -> str:
    """把 run 的失败原因归一成**可索引的短码**。

    「同一失败原因连续 ≥N 单」这条升级触发要成立,原因必须是一列可 GROUP BY 的值,
    而不是对 `last_settlement_error` 自由文本做 LIKE —— 后者是会静默失效的那种判据。
    归一规则:取错误串首个 `:` 前的 token,小写化;取不到 → `unknown`。
    """
    raw = (run.get("last_settlement_error") or "").strip()
    if not raw:
        return "unknown"
    head = raw.split(":", 1)[0].strip().lower()
    return (head or "unknown")[:80]


def collect_evidence(cur, run: Dict[str, Any]) -> Dict[str, Any]:
    """逐单产证据包。**只读**,不写任何表。

    读不到的项一律记 None 并在 `missing` 里点名 —— 绝不"猜一个值让规则跑得下去"。
    """
    run_token = run["run_token"]
    ev: Dict[str, Any] = {
        "run_token": run_token,
        "billing_mode": run.get("billing_mode"),
        # 🔴 冻结额**不取** `diagnosis_runs.points`:生产里那一列从来没被写过
        #    (全仓 0 处赋值)。拿它做限额门控 = 每单读到 NULL = 全部升级 =
        #    审查员上线即"什么都不自动"。真钱在冻结行/charge link 上,见下面 _frozen_amount。
        "frozen_points": None,
        "freeze_backend": run.get("freeze_backend"),
        "freeze_id": run.get("freeze_id"),
        "freeze_task_ref": run.get("freeze_task_ref"),
        "owner_user_id": run.get("owner_user_id"),
        "last_settlement_error": (run.get("last_settlement_error") or "")[:500],
        "reserved_split_snapshot": _jsonish(run.get("reserved_split_snapshot_jsonb")),
        "pool_split": _jsonish(run.get("pool_split")),
        "final_snapshot": _jsonish(run.get("final_snapshot_jsonb")),
        "missing": [],
    }

    # ① 产物在库?—— 交付与否的**唯一硬证据**:有分数的诊断记录行。
    prod = None
    try:
        with _savepoint(cur, "adj_prod"):
            cur.execute(
                "SELECT id, total_score, ai_total_tests, ai_engines_tested "
                "FROM diagnosis_records WHERE run_token=%s AND total_score IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (run_token,),
            )
            prod = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        prod = None
        ev["missing"].append("product_lookup_failed:%s" % str(e)[:120])
    ev["product_present"] = bool(prod)
    ev["product"] = None if not prod else {
        "diagnosis_id": prod["id"],
        "total_score": _num(prod["total_score"]),
        "provider_call_count": _num(prod["ai_total_tests"]),
        "engines_tested": _num(prod["ai_engines_tested"]),
    }

    # ② 冻结行还在不在(钱包侧流水)。三元组任一缺失 → 无法处置,记 missing。
    backend, fid = run.get("freeze_backend"), run.get("freeze_id")
    ev["freeze_row"] = None
    if backend in _FREEZE_TABLES and fid:
        # 表名/列名来自**闭集常量**(_FREEZE_TABLES),不来自 run 行 —— 不是 SQL 注入面;
        # 值仍走参数绑定。
        table, uid_col = _FREEZE_TABLES[backend]
        try:
            with _savepoint(cur, "adj_freeze"):
                cur.execute(
                    "SELECT id, status, amount_total, " + uid_col + " AS owner "
                    "FROM " + table + " WHERE id=%s",
                    (fid,),
                )
                fr = cur.fetchone()
            ev["freeze_row"] = None if not fr else {
                "id": fr["id"], "status": fr["status"], "owner": fr["owner"],
                "amount_total": _num(fr["amount_total"])}
        except Exception as e:  # noqa: BLE001
            ev["missing"].append("freeze_lookup_failed:%s" % str(e)[:120])
    else:
        ev["missing"].append("freeze_handle_incomplete")

    # ③ org 档:charge link 状态。
    # 🔴 列名是**肉眼核过 schema** 的:这张表没有 `points`,只有 estimated_points /
    #    reserved_ceiling_points / actual_points。第一版我按直觉写了 `points`,
    #    UndefinedColumn 直接把整个事务打废 —— 后面每一条取数都跟着失败,
    #    表现成"审查员什么都没做",离真因隔两层。
    ev["charge_link"] = None
    try:
        with _savepoint(cur, "adj_cl"):
            cur.execute(
                "SELECT id, status, estimated_points, reserved_ceiling_points, actual_points "
                "FROM organization_charge_links WHERE task_ref=%s ORDER BY id DESC LIMIT 1",
                (run.get("freeze_task_ref"),),
            )
            cl = cur.fetchone()
        if cl:
            ev["charge_link"] = {
                "id": cl["id"], "status": cl["status"],
                "estimated_points": _num(cl["estimated_points"]),
                "reserved_ceiling_points": _num(cl["reserved_ceiling_points"]),
                "actual_points": _num(cl["actual_points"]),
            }
    except Exception as e:  # noqa: BLE001
        ev["missing"].append("charge_link_lookup_failed:%s" % str(e)[:120])

    ev["failure_cause"] = normalize_failure_cause(run)
    ev["frozen_points"] = _frozen_amount(ev, run)
    return ev


def _frozen_amount(ev: Dict[str, Any], run: Dict[str, Any]):
    """这一单**真正挂着多少钱** —— 限额门控的依据。

    取数顺序按"离钱最近"排:
      ① 冻结行 `amount_total`(legacy / v35 两张表列名相同,肉眼核过);
      ② org 档没有 legacy 冻结行 → charge link 的 `reserved_ceiling_points`(上限即风险敞口);
      ③ 都没有 → None ⇒ 规则侧当"读不到冻结额"升级,**绝不当 0 放行**。
    """
    fr = ev.get("freeze_row") or {}
    if fr.get("amount_total"):
        return fr["amount_total"]
    cl = ev.get("charge_link") or {}
    if cl.get("reserved_ceiling_points"):
        return cl["reserved_ceiling_points"]
    return None


_FREEZE_TABLES = {"legacy": ("point_freezes", "user_id"),
                  "v35": ("customer_credit_freezes", "customer_user_id")}


def _jsonish(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return None


def _num(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# 机械裁定(纯函数 · 零 IO · 零 LLM)
# ---------------------------------------------------------------------------

def adjudicate(evidence: Dict[str, Any], coeffs: Dict[str, int],
               same_cause_streak: int) -> Dict[str, Any]:
    """机械规则。**纯函数** —— 同样的证据包永远得到同样的裁定,可复算可审计。

    抽成纯函数是为了可判:判据能直接喂证据包矩阵,不必每次都真跑一单。
    (但接线仍必须另有运行时判据 —— 抽函数化让逻辑可判,调用点反而容易没人守。)

    返回 {"decision", "escalation_code", "reason"}。
    """
    missing = list(evidence.get("missing") or [])

    # 🔴 升级三之三:同因连续 ≥N —— **按 bug 报,不按退款批**。
    #    系统性故障不许被一单一退悄悄放血,所以这一条排在最前:
    #    哪怕这单证据齐、金额也在限额内,只要它是同一个故障的第 N 单,就停手报人。
    if same_cause_streak >= coeffs["same_cause_limit"]:
        return {"decision": "escalate", "escalation_code": ESCALATE_SAME_CAUSE_STREAK,
                "reason": "同一失败原因 %r 连续 %d 单(阈值 %d)—— 疑似系统性故障,停止自动处置"
                          % (evidence.get("failure_cause"), same_cause_streak,
                             coeffs["same_cause_limit"])}

    # 🔴 升级三之二:证据不全 / 互相矛盾 → 不动钱。
    if missing:
        return {"decision": "escalate", "escalation_code": ESCALATE_EVIDENCE_INCOMPLETE,
                "reason": "证据不全:%s" % ",".join(missing[:5])}

    frozen = evidence.get("frozen_points")
    if frozen is None or int(frozen) <= 0:
        return {"decision": "escalate", "escalation_code": ESCALATE_EVIDENCE_INCOMPLETE,
                "reason": "读不到有效冻结额,无法门控"}

    # 矛盾面:冻结行已经不是 frozen 了(别人动过),但 run 还停在转人工 —— 不猜,交人。
    fr = evidence.get("freeze_row")
    if not fr:
        return {"decision": "escalate", "escalation_code": ESCALATE_EVIDENCE_INCOMPLETE,
                "reason": "冻结行不存在(三元组指向空)"}
    if str(fr.get("status")) != "frozen":
        return {"decision": "escalate", "escalation_code": ESCALATE_EVIDENCE_INCOMPLETE,
                "reason": "冻结行状态为 %r 而非 frozen —— 与'停在转人工'互相矛盾" % fr.get("status")}

    # 🔴 升级三之一:超限额。**commit / release 两向都门**。
    #    审查员不决定金额(金额归 sweeper),所以门控落在**冻结额**上:
    #    大额单无论判交付还是判全退,都必须人看一眼。
    if int(frozen) > coeffs["max_frozen_points"]:
        return {"decision": "escalate", "escalation_code": ESCALATE_OVER_LIMIT,
                "reason": "冻结额 %d 超过自动处置限额 %d(commit/release 两向同门)"
                          % (int(frozen), coeffs["max_frozen_points"])}

    # ── 到这里:证据齐、无矛盾、限额内 → 可自动处置 ────────────────────────
    if evidence.get("product_present"):
        # 有交付证据 → 交付。**比例不在这里算** —— CAS 回 commit_pending 之后,
        # sweeper 的 _partial_commit_points 读快照判 degraded,自动只收已履约那部分。
        return {"decision": "commit", "escalation_code": None,
                "reason": "产物在库(diagnosis_id=%s)⇒ 判交付;已履约比例由 sweeper 按快照计算"
                          % ((evidence.get("product") or {}).get("diagnosis_id"))}

    # 零交付证据 → 全额退。
    return {"decision": "release", "escalation_code": None,
            "reason": "无任何产物记录 ⇒ 零交付,全额释放冻结"}


# ---------------------------------------------------------------------------
# 裁定记录(049 + 既有 audit **同事务双写**)
# ---------------------------------------------------------------------------

def write_adjudication(cur, *, run_token: str, phase: str, decision: str,
                       escalation_code: Optional[str], failure_cause: Optional[str],
                       frozen_points: Optional[int], evidence: Dict[str, Any],
                       outcome: Optional[str]) -> int:
    """049 行 + 既有 `diagnosis_settlement_audit` 行 **同一事务双写**(Review 2026-08-25 硬要求)。

    两轨要么都在要么都不在:049 写失败 → 调用方 with 块回滚 → audit 行同回滚。
    所以这里**不吞异常**,也**不各自开连接**。
    """
    cur.execute(
        "INSERT INTO " + _TABLE + " (run_token, phase, decision, escalation_code, "
        "failure_cause, frozen_points, rule_version, evidence_jsonb, outcome) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id",
        (run_token, phase, decision, escalation_code, failure_cause,
         None if frozen_points is None else int(frozen_points), RULE_VERSION,
         json.dumps(evidence, ensure_ascii=False, default=str), outcome),
    )
    row = cur.fetchone()
    adjudication_id = int(row["id"] if isinstance(row, dict) else row[0])

    # 既有审计轨照写(不退役):admin 的 /audit 端点要能看见审查员做过什么。
    from services.diagnosis_runs import _write_settlement_audit
    _write_settlement_audit(
        run_token, OPERATOR, "adjudicator_" + phase,
        "decision=%s|code=%s|cause=%s|points=%s|rule=%s|adjudication_id=%s|outcome=%s" % (
            decision, escalation_code, failure_cause, frozen_points,
            RULE_VERSION, adjudication_id, (outcome or "")[:200]),
        cur=cur,   # 🔴 同事务:传 cur 时写失败会 raise,让整个 with 块回滚
    )
    return adjudication_id


def same_cause_streak(cur, failure_cause: str, limit_lookback: int = 50,
                      *, current_run_token: Optional[str] = None) -> int:
    """最近若干**单**里,连续同一 failure_cause 的**单**数(遇到不同的就断)。

    「连续」不是「总数」:一天里零星几单同因是正常的,连着 N 单同因才说明是系统性故障。

    🔴 [工单 C-8 · Codex P2 2026-08-25] 数的是**单**,不是**行**
    ------------------------------------------------------------
    049 是 append-only 审计表,一单自动处置会落**两行**带 failure_cause 的记录
    (``decided`` 与 ``executed``/``execution_failed``;崩在中间重跑还会更多)。
    原实现逐行数 ⇒ 每单被数两次 ⇒ 阈值 N 在**第 N/2 单**就触发全局升级
    (阈值配 6 时第 3 单就停止自动处置)。

    去重按 ``run_token`` 而不是按 ``phase='decided'``:
      · ``escalated`` 那一档**没有** decided 行,按 phase 筛会把它整单漏掉,
        于是「连续」在真正出事的那几单上反而断链;
      · 崩在 decided 与 execute 之间的重跑会产生**多条** decided,按 phase 筛
        仍然重复计数。
    按单去重两种情形都对。

    🔴 ``current_run_token`` 必须排除:本单可能已经有 049 行了
    (上一 tick 落了 ``decided`` 就崩)。不排除的话它会被循环数一次、
    再被下面那个 ``+1`` 数一次 —— 同一个双计缺陷换个地方复发。
    """
    cur.execute(
        "SELECT run_token, failure_cause FROM " + _TABLE
        + " WHERE failure_cause IS NOT NULL ORDER BY id DESC LIMIT %s",
        (int(limit_lookback),))
    streak = 0
    seen: set = set()
    if current_run_token:
        seen.add(str(current_run_token))
    for row in cur.fetchall() or []:
        if isinstance(row, dict):
            token, cause = row["run_token"], row["failure_cause"]
        else:
            token, cause = row[0], row[1]
        token = str(token)
        if token in seen:
            # 同一单的第二/第三行 —— 已经数过了(或它就是当前这一单)。
            # 🔴 `continue` 而不是 `break`:同一单的两行之间不会夹进别的单
            #    (append-only + 单事务),但当前这一单的行必须跳过而不是截断。
            continue
        if cause != failure_cause:
            break
        seen.add(token)
        streak += 1
    return streak + 1   # +1 = 当前这一单


def already_escalated(cur, run_token: str, escalation_code: str) -> bool:
    """这单是不是**已经**因同一个理由升级过了。

    没有这道判断的话,卡住的单会每 2 分钟被重新升级一次:
    049 表被刷爆、admin 每 2 分钟收一次同样的通知(告警极简铁律的反面)。
    """
    cur.execute(
        "SELECT 1 FROM " + _TABLE + " WHERE run_token=%s AND phase='escalated' "
        "AND escalation_code=%s LIMIT 1", (run_token, escalation_code))
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# worker tick(挂在既有 diagnosis_run_sweeper 上 · api/scheduler.py 零 diff)
# ---------------------------------------------------------------------------

def _escalation_status_text(code: str) -> str:
    return {
        ESCALATE_OVER_LIMIT: "金额超出自动核验范围，需要人工处理",
        ESCALATE_EVIDENCE_INCOMPLETE: "证据不足，需要人工核验",
        ESCALATE_SAME_CAUSE_STREAK: "连续同类失败，疑似系统性故障，需要排查",
    }.get(code, "需要人工处理")


def _adjudicate_one(run: dict, coeffs: Dict[str, int], stats: Dict[str, int]) -> None:
    """处置一单。

    ## 为什么是「先记录后执行」

    `verify_and_resolve_manual` 自带单事务 FOR UPDATE + CAS,我无法把 049 的写入塞进它那个事务。
    两种顺序都有窗口,取**危害小**的那个:

      先执行后记录 → 崩在中间 = run 已迁出 settlement_manual 但**没有裁定记录**(审计轨断);
      先记录后执行 → 崩在中间 = 多一行 `decided` 但**什么都没发生**(run 仍在 settlement_manual,
                     下一 tick 重新裁定,再多一行)。表是 append-only,多行是可解释的;
                     钱不会重复动,因为 CAS 只认 settlement_manual。

    所以选后者:**宁可多一行记录,不可少一行记录**。
    """
    run_token = run["run_token"]
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        ev = collect_evidence(cur, run)
        streak = same_cause_streak(cur, ev["failure_cause"],
                                   current_run_token=run_token)
        verdict = adjudicate(ev, coeffs, streak)

        if verdict["decision"] == "escalate":
            code = verdict["escalation_code"]
            # 幂等:同一单同一理由只升级一次。否则卡住的单每 2 分钟刷一行 + 推一次通知
            # (告警极简铁律的反面)。
            if already_escalated(cur, run_token, code):
                stats["escalate_skipped_dup"] += 1
                return
            write_adjudication(
                cur, run_token=run_token, phase="escalated", decision="escalate",
                escalation_code=code, failure_cause=ev["failure_cause"],
                frozen_points=ev.get("frozen_points"), evidence=ev,
                outcome=verdict["reason"])
            # 🔴 同事务:049 + audit + outbox 三者一起提交或一起回滚。
            #    只扇 admin —— 升级理由里带内部系数(限额/阈值),不能推给用户。
            from services.notification_events import NotificationEventType
            from services.notification_outbox import enqueue_admin_notification_events
            from datetime import datetime, timezone
            enqueue_admin_notification_events(
                cur,
                event_type=NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED,
                business_id=str(run_token),
                # 与"刚进 settlement_manual"那条区分开:event_key 含 terminal_state,
                # 用不同的值才不会被 ON CONFLICT DO NOTHING 吞掉。
                terminal_state="settlement_manual_adjudicator_escalated",
                facts={
                    "business_no": "诊断-%s" % (run.get("session_id") or run_token),
                    "status": _escalation_status_text(code),
                    "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "summary": "请在诊断资金异常队列中处理。",
                },
            )
            stats["escalated"] += 1
            return

        # 自动处置:先落 decided(与 audit 同事务),提交后再执行。
        write_adjudication(
            cur, run_token=run_token, phase="decided", decision=verdict["decision"],
            escalation_code=None, failure_cause=ev["failure_cause"],
            frozen_points=ev.get("frozen_points"), evidence=ev,
            outcome=verdict["reason"])

    # ── 执行:走 admin 处置同一把闸门。本模块**不 import 任何资金原语** ──────
    from services.diagnosis_runs import verify_and_resolve_manual
    out = verify_and_resolve_manual(
        run_token, OPERATOR, str(run.get("freeze_backend")), int(run.get("freeze_id")),
        verdict["decision"], "AI 审查员自动裁定 · %s · rule=%s" % (verdict["reason"], RULE_VERSION))

    ok = bool(out.get("ok"))
    with get_db() as conn:
        cur = conn.cursor()
        write_adjudication(
            cur, run_token=run_token,
            phase="executed" if ok else "execution_failed",
            decision=verdict["decision"], escalation_code=None,
            failure_cause=ev["failure_cause"], frozen_points=ev.get("frozen_points"),
            evidence=ev, outcome=json.dumps(out, ensure_ascii=False, default=str)[:1900])
    if ok:
        stats["auto_" + verdict["decision"]] += 1
    else:
        # 执行未成 = 钱没动(CAS 没命中,通常是人工 admin 抢先处置了同一单)。
        # 不重试、不升级:run 已经不在 settlement_manual,下一 tick 自然不再是候选。
        stats["execution_failed"] += 1


def run_adjudication_tick() -> Dict[str, int]:
    """审查员 tick。**全异常兜底**,返回统计 dict(照 run_diagnosis_sweep 的形态)。

    宿主 = 既有 `diagnosis_run_sweeper`(每 2min · 无条件注册 · sched_claim 防双跑),
    所以 `api/scheduler.py` 零 diff。
    """
    stats = {"scanned": 0, "auto_commit": 0, "auto_release": 0, "escalated": 0,
             "escalate_skipped_dup": 0, "execution_failed": 0,
             "skipped_no_config": 0, "errors": 0}
    from db.connection import get_db

    try:
        with get_db() as conn:
            coeffs = load_coefficients(conn.cursor())
    except Exception as e:  # noqa: BLE001
        logger.warning("[Adjudicator] 读系数异常,本轮不处置: %s", e)
        stats["errors"] += 1
        return stats

    if coeffs is None:
        # 🔴 未配置 = 零行为变化。不是"用默认值先跑着"。
        stats["skipped_no_config"] = 1
        return stats

    try:
        with get_db() as conn:
            cur = conn.cursor()
            # 候选:停在 settlement_manual、**且没有人工在处置**(manual_resolving 不碰)。
            # 走 admin 同一把 CAS,所以这里读到的候选即便被人抢走,执行时也只会 ok=False。
            cur.execute(
                "SELECT * FROM diagnosis_runs WHERE run_status='settlement_manual' "
                "ORDER BY status_changed_at ASC LIMIT %s", (_TICK_LIMIT,))
            candidates = [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:  # noqa: BLE001
        logger.warning("[Adjudicator] 取候选异常: %s", e)
        stats["errors"] += 1
        return stats

    stats["scanned"] = len(candidates)
    for run in candidates:
        try:
            _adjudicate_one(run, coeffs, stats)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            logger.warning("[Adjudicator] 处置 run=%s 异常: %s", run.get("run_token"), e)
    return stats
