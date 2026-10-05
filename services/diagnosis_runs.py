"""
诊断资金状态机(WORKERS=4 · SPEC §3 · billing.py 只调不改)
==========================================================

诊断从"启动 freeze → 完成 commit / 失败 release"的补偿状态机,承载 WORKERS=4 下的:
- **恰一次终态**(I2):每笔 freeze 最终 committed / released 恰一次,单点崩溃(含 CAS 后结算前)
  由 sweeper 自愈;终态**只在 billing 确认 success 后**写(R1)。
- **结果可见性**(§3.6 P0-3):终态与产物可见性同一本地事务原子写(commit→published / release→withheld)——
  杜绝"已退款仍白拿正式结果"。
- **请求级幂等**(§3.3a HC1):无目标 INSERT ON CONFLICT DO NOTHING + 0 行三路定性(幂等旧单 / 品牌活跃单 / 内部错)。
- **终态 DB 回补**(§3.6a HC3):final_snapshot_jsonb 随结算事务落库,reconciler 在 Redis 故障后回补终态快照。

🔴 红线:本模块**只 import 调用** billing 的 `commit_freeze/release_freeze`(四参全传 · 关键字),
   绝不改其内部;billing 三函数各自 `with get_db()` 自管事务 → freeze 与 run 行不可同事务,
   故用补偿状态机 + sweeper 自愈(SPEC §3.5)。

R0 五分支(§3.0 · 异常必分类 · 未知禁 cancelled):
  1 付费成功 freeze_id 非空+backend∈legacy/v35 → running
  2 免单 free=True → running(billing_mode=exempt · 无 billing 调用)
  3 确定性失败(402/400 已知业务拒绝)→ cancelled
  4 结构异常 freeze_id 非空但 backend 缺 → 解析 backend → release_pending;解析不出 → settlement_manual
  5 结果未知(超时/断连/白名单外)→ 保持 pending_freeze,立即双表定位:找到→release_pending;
    **首次查空绝不 cancelled**(原冻结事务可能 COMMIT 在途)→ 留 pending_freeze,交 sweeper 延迟确认。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import NamedTuple, Optional

logger = logging.getLogger("GEO-DiagRuns")

# 活跃(未终态)状态集 —— 与 uq_diag_active_per_brand partial predicate 一致
ACTIVE_STATUSES = ("pending_freeze", "running", "commit_pending", "release_pending")
# 终态集(reconciler 扫描 / sweeper 排除)· delivery_repair_pending 为"钱已结算但产物缺"的非成功终态
TERMINAL_STATUSES = ("committed", "released", "cancelled", "cancelled_no_freeze",
                     "completed_exempt", "failed_exempt", "delivery_repair_pending")
# 成功终态(reconciler 判 Redis 陈旧成功态用 · 与失败/退款终态相反)
_SUCCESS_TERMINAL_STATUSES = ("committed", "completed_exempt")

# R0 分支 3 确定性失败:billing 主动 raise 的已知业务拒绝 HTTP 码白名单(402 余额不足族 / 400 参数非法)
_DETERMINISTIC_HTTP_CODES = (400, 402)

_SETTLE_MAX_ATTEMPTS = 5           # R3:commit 重试上限 → settlement_manual
_RUNNING_DEAD_SECONDS = 300        # §3.4.1 running 判死(心跳超 5min)
_PENDING_FREEZE_REAP_SECONDS = 600 # §3.4.2 pending_freeze 收尸(超 10min)
_VERIFY_INTERVAL_SECONDS = 30      # §3.0 两次查空间隔 ≥30s 才计连续
_ALERT_LIST_KEY = "diag:settlement_alerts"   # 跨 worker 资金告警(admin 可读)
_MANUAL_STUCK_ALERT_SECONDS = 3600 # [返工2 item8] settlement_manual/delivery_repair 卡超 1h → 限频告警 ops
_MANUAL_LEASE_SECONDS = 90         # [返工4 P0] 双表处置**租约**时长:claim 拿 resolution_token + lease=NOW()+90s ·
                                   #   有效租约内相同意图也只返"处理中"(不并发)· sweeper 仅租约过期后 CAS 接管(拿新 token)


# ============================================================================
# 基础工具
# ============================================================================
def mint_run_token() -> str:
    """生成全局唯一 run_token(PK)。"""
    return "run_" + uuid.uuid4().hex


def freeze_task_ref(run_token: str) -> str:
    """freeze 的 task_ref = 'diag_'||run_token(SPEC §3.2)。"""
    return "diag_" + run_token


def settlement_payer_user_id(run) -> Optional[int]:
    """这一单的**物理冻结长在谁的钱包上** —— 全部冻结定位/结算的唯一 payer 谓词。

    [WO_CODEX_P0_DIAG_FUNDING A-1 · Codex P0-1 第二层 · 2026-08-25]

    为什么非它不可
    --------------
    防御型 GEO 的**平台承担腿**在 confirm 里走的是
    ``freeze_points(platform_uid, ...)`` —— 冻结行 ``point_freezes.user_id``
    是**平台直营服务账号**,不是发起人。而本模块此前所有冻结定位与结算
    (``commit_freeze`` / ``release_freeze`` / ``_frozen_total_for_run`` /
    ``_reserved_split_for_run`` / ``_double_table_locate`` / 人工处置三元组核验)
    统一拿 ``run["owner_user_id"]`` 当 user。user 对不上 ⇒ 那一行**根本定位不到**:
      · commit/release 永远 success=false → 退避重试 → settlement_manual;
      · 降级交付一律 ``frozen_amount_unreadable`` / ``reserved_split_freeze_missing``;
      · 收尸双表定位查空两次 → ``cancelled_no_freeze``,**冻结原样挂着零退款**。
    Codex 原文点名:「即使取消 exempt 旁路,现有结算仍用 tenant owner_user_id
    查找冻结,而冻结行属于 platform UID,第二层仍无法结算。」

    ``payer_user_id IS NULL`` 的语义就是「payer 就是 owner」(存量 + 个人钱包腿
    常态)。回落**只写在这里一处**:同一谓词写两处必有一处没人验,而这一处写歪
    的代价是拿错账号去动钱。

    读不出来就返 ``None``(与既有 ``if not fid or not owner`` 的 fail-closed
    同口径),绝不猜一个 id 出来。
    """
    if not isinstance(run, dict):
        return None
    value = run.get("payer_user_id")
    if value is None or (isinstance(value, str) and not value.strip()):
        value = run.get("owner_user_id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _alert(kind: str, run_token: str, detail: str) -> None:
    """资金告警 → Redis 列表(跨 worker · admin 可查);Redis 不可达降级 logger.critical。"""
    payload = {"kind": kind, "run_token": run_token, "detail": detail[:400]}
    logger.critical(f"[DiagRuns/ALERT] {kind} run={run_token} · {detail[:300]}")
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is not None:
            r.lpush(_ALERT_LIST_KEY, json.dumps(payload, ensure_ascii=False))
            r.ltrim(_ALERT_LIST_KEY, 0, 999)
    except Exception:
        pass


def _write_settlement_audit(run_token: str, operator: str, action: str, detail: str, cur=None) -> None:
    """[返工2 item8] 持久审计(append-only diagnosis_settlement_audit)· 人工/自动资金处置全程留痕
    (Redis 告警列表易失 · 此表持久供事后审计/对账)。
    [返工5 复审 req#4] **传 cur(处置事务游标)→ 同事务原子写入 · 失败不吞让异常传播 → 处置一并回滚**
    (资金处置与持久审计必须同事务:审计失败绝不能留下"已处置无留痕")。
    不传 cur → 独立连接 best-effort(失败只 logger.warning · 用于自动转人工等非同事务场景)。"""
    _sql = ("INSERT INTO diagnosis_settlement_audit (run_token, operator, action, detail) VALUES (%s, %s, %s, %s)")
    _args = (run_token, (operator or "system")[:200], action[:80], (detail or "")[:2000])
    if cur is not None:
        cur.execute(_sql, _args)   # 同事务 · 失败即 raise → 调用方 with 块回滚(资金处置与审计原子)
        return
    from db.connection import get_db
    try:
        with get_db() as conn:
            conn.cursor().execute(_sql, _args)
    except Exception as e:
        logger.warning(f"[DiagRuns] settlement 审计写入失败 run={run_token} action={action}: {e}")


def _alert_to_manual(kind: str, run_token: str, detail: str) -> None:
    """[返工2 item8] 自动转 settlement_manual:资金告警 + 持久审计(auto_to_manual)一处两写
    (arg 顺序与 _alert 一致 · 便于在转 manual 处直接替换)。"""
    _alert(kind, run_token, detail)
    _write_settlement_audit(run_token, "system", "auto_to_manual", f"{kind}: {detail}")


def list_settlement_audit(run_token: str, limit: int = 200) -> list:
    """[返工2 item8] 读某 run 的持久审计流水(admin 对账)。"""
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, run_token, operator, action, detail, created_at FROM diagnosis_settlement_audit "
                "WHERE run_token=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                (run_token, max(1, int(limit))),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
        for r in rows:
            if r.get("created_at") is not None and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception:
        return []


# ============================================================================
# §3.7 settlement_manual 人工闭环(admin-only · 全程审计 · 禁绕状态机伪造终态)
# ============================================================================
# freeze 双表映射(legacy=老积分冻结表 / v35=客户信用冻结表)· user_id 列名不同
_FREEZE_TABLE = {"legacy": ("point_freezes", "user_id"),
                 "v35": ("customer_credit_freezes", "customer_user_id")}


def _verify_freeze_exists(backend: str, freeze_id: int, payer_user_id: int, task_ref: str) -> bool:
    """独立事务(robust · 一表异常不污染)校验 (freeze_id + user_id + task_ref) 三元组在 backend 表存在。
    查询异常 → False(fail-closed:校验不过不推进 · 绝不误当匹配)。

    [A-1] 第三个参数是 **payer**(冻结行真正长在谁名下),不是 owner ——
    平台承担腿两者不相等,拿 owner 查等于永远查空。调用方一律传
    ``settlement_payer_user_id(run)``,由 ``test_payer_census`` 机械枚举守。"""
    if backend not in _FREEZE_TABLE:
        return False
    from db.connection import get_db
    table, uid_col = _FREEZE_TABLE[backend]
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT id FROM {table} WHERE id=%s AND {uid_col}=%s AND task_ref=%s",
                (int(freeze_id), payer_user_id, task_ref),
            )
            return cur.fetchone() is not None
    except Exception:
        return False




def _locate_all_freezes(payer_user_id: int, task_ref: str) -> list:
    """[返工2 P0-3] 列该 (user, task_ref) 在**双表**所有匹配冻结:[{backend, freeze_id, status}, ...](0/1/2 条)。
    admin 双表处置据此取两笔 freeze_id;各表独立事务(一表未建/异常不影响另一表)。

    [A-1] 第一个参数是 **payer**,不是 owner(理由见 ``settlement_payer_user_id``)。"""
    out = []
    from db.connection import get_db
    for backend, (table, uid_col) in _FREEZE_TABLE.items():
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT id, status FROM {table} WHERE task_ref=%s AND {uid_col}=%s ORDER BY id DESC LIMIT 1",
                    (task_ref, payer_user_id),
                )
                r = cur.fetchone()
                if r is not None:
                    out.append({"backend": backend, "freeze_id": int(r["id"]), "status": r["status"]})
        except Exception:
            continue
    return out


def list_settlement_manual(limit: int = 100) -> list:
    """§3.7 管理队列:列全部 settlement_manual run + 双表只读定位现场(供 admin 核对 FreezeHandle)。
    [返工2 P0-3] locate 之外额外给 **all_freezes(双表全部匹配冻结)+ double_frozen**(两表都冻结时 admin 必须
    双表处置 · 两笔 freeze_id 都在此供选)。"""
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        # [返工5 P2] 队列纳入 **manual_resolving**(处置租约在途/过期待接管)—— 否则卡租约的 run 后台完全不可见。
        #   仅只读展示 + 前端禁直接 resolve(admin resolve 的 CAS 只认 settlement_manual · manual_resolving 由 sweeper/持有者续跑)。
        cur.execute(
            "SELECT run_token, session_id, owner_user_id, payer_user_id, brand_id, billing_mode, freeze_task_ref, "
            "freeze_id, freeze_backend, run_status, settlement_attempts, last_settlement_error, "
            "manual_lease_until, status_changed_at, created_at, "
            "(manual_lease_until IS NOT NULL AND manual_lease_until > NOW()) AS lease_valid "
            "FROM diagnosis_runs WHERE run_status IN ('settlement_manual','manual_resolving') "
            "ORDER BY status_changed_at ASC LIMIT %s",
            (max(1, int(limit)),),
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
    for r in rows:
        b, fid, fstatus = _double_table_locate(settlement_payer_user_id(r), r["freeze_task_ref"])
        r["locate"] = {"backend": b, "freeze_id": fid, "freeze_status": fstatus}
        _all = _locate_all_freezes(settlement_payer_user_id(r), r["freeze_task_ref"])
        r["all_freezes"] = _all
        r["double_frozen"] = len(_all) >= 2   # 两表都冻结 → admin 必须双表处置(单表 resolve 会被拒)
        # [返工5 P2] manual_resolving 只读展示 · 禁直接 resolve(标 is_resolving/lease_state · 前端据此禁用处置按钮)
        r["is_resolving"] = (r["run_status"] == "manual_resolving")
        r["resolvable"] = (r["run_status"] == "settlement_manual")
        if r["is_resolving"]:
            r["lease_state"] = "processing" if r.get("lease_valid") else "expired_await_takeover"
        for _k in ("status_changed_at", "created_at", "manual_lease_until"):
            if r.get(_k) is not None and hasattr(r[_k], "isoformat"):
                r[_k] = r[_k].isoformat()
    return rows


def verify_and_resolve_manual(run_token: str, operator: str, backend: str, freeze_id: int,
                              decision: str, note: str) -> dict:
    """§3.7 人工处置:先建立并验证唯一 FreezeHandle(所选表 user_id+task_ref+freeze_id 三元组匹配 run)→
    回填 → CAS settlement_manual→*_pending(交还 sweeper 正常结算)。**禁止**直接改余额/写终态/绕状态机。
    decision='commit'(交付 · 冻结转消费) | 'release'(退款)。operator 由端点从认证上下文注入。"""
    if backend not in ("legacy", "v35"):
        return {"ok": False, "error": "backend 必须是 legacy/v35"}
    if decision not in ("commit", "release"):
        return {"ok": False, "error": "decision 必须是 commit/release"}
    from db.connection import get_db
    table, uid_col = _FREEZE_TABLE[backend]
    target = "commit_pending" if decision == "commit" else "release_pending"
    audit = f"manual_resolved_by:{operator}|backend={backend}|decision={decision}|note={note[:300]}"
    # [返工 P0-3] **单事务原子**:SELECT run FOR UPDATE(锁 run 行)→ 锁内状态复核 → 三元组验证 →
    #   Handle+状态+审计+退避 一条 UPDATE 原子写。持锁期间状态不可被并发改 → 两 admin A/B 并发时
    #   一个先拿锁完成迁移(settlement_manual→*_pending),另一个拿锁后复核状态已变 → 拒绝(恰一个成功);
    #   验证不过 / 状态不符 → 事务内直接返回(nothing 提交 · Handle 绝不先写)。
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM diagnosis_runs WHERE run_token=%s FOR UPDATE", (run_token,))
            run = cur.fetchone()
            if run is None:
                return {"ok": False, "error": "run_not_found"}
            if run["run_status"] != "settlement_manual":
                return {"ok": False, "error": f"run 非 settlement_manual(当前 {run['run_status']})"}
            # [返工2 P0-3] **双表都冻结守卫**:ambiguous 转人工的根因正是 legacy+v35 双表同时匹配;单表处置只结算
            #   一笔,另一笔继续冻结(客户被扣两笔只交付/退一笔)。此处强制拒绝单表处置,导向双表处置
            #   resolve_double_frozen(两笔都给审计处置)。_double_table_locate 各表独立事务读(不污染本 FOR UPDATE txn)。
            _loc_b, _, _ = _double_table_locate(settlement_payer_user_id(run), run["freeze_task_ref"])
            if _loc_b == "ambiguous":
                return {"ok": False, "double_frozen": True,
                        "error": "double_frozen: legacy 与 v35 双表均有冻结 · 单表处置会漏结算另一笔 · "
                                 "请用双表处置(提供 other_backend/other_freeze_id 对两笔分别处置)"}
            # 锁内验证冻结三元组(user_id+task_ref+freeze_id 匹配)· 同事务
            cur.execute(
                f"SELECT id FROM {table} WHERE id=%s AND {uid_col}=%s AND task_ref=%s",
                (freeze_id, settlement_payer_user_id(run), run["freeze_task_ref"]),
            )
            fz = cur.fetchone()
            if fz is None:
                return {"ok": False, "error": "Handle 校验不过:所选表无匹配 (user_id+task_ref+freeze_id) 行 · 拒绝提交"}
            # 原子:回填 Handle + CAS settlement_manual→*_pending + 审计 + 立即可结算(持锁 · CAS 必命中 1 行)
            cur.execute(
                "UPDATE diagnosis_runs SET freeze_id=%s, freeze_backend=%s, run_status=%s, "
                "status_changed_at=NOW(), last_settlement_error=%s, settlement_attempts=0, "
                "next_settlement_at=NOW() WHERE run_token=%s AND run_status='settlement_manual'",
                (int(fz["id"]), backend, target, audit[:480], run_token),
            )
            if cur.rowcount != 1:
                # 持 FOR UPDATE 锁下理论必命中;未命中=异常 → 抛出回滚(绝不半写 Handle)
                raise RuntimeError(f"manual resolve CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
    except Exception as e:
        return {"ok": False, "error": f"人工处置失败(已回滚 · run 状态未变): {e}"}
    _alert("manual_resolved", run_token, audit)
    _write_settlement_audit(run_token, operator, "manual_resolve_single", audit)  # [返工2 item8] 持久审计
    return {"ok": True, "target": target, "backend": backend, "freeze_id": int(freeze_id)}


def _parse_intent(raw) -> Optional[dict]:
    """[返工3 P0] 解析 manual_resolution 持久意图 JSON(str/None → dict/None)。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _intent_json(operator: str, keeper_backend: str, keeper_freeze_id: int, keeper_decision: str,
                 other_backend: str, other_freeze_id: int) -> str:
    """[返工3 P0] 规范化双表处置意图 JSON(sort_keys 稳定 · 供 claim 持久化 + resume 一致性比对)。"""
    return json.dumps({
        "keeper_backend": keeper_backend, "keeper_freeze_id": int(keeper_freeze_id),
        "keeper_decision": keeper_decision, "other_backend": other_backend,
        "other_freeze_id": int(other_freeze_id), "operator": operator,
    }, ensure_ascii=False, sort_keys=True)


def _same_intent(existing_raw, intent_json: str) -> bool:
    """[返工3 P0] 已持久意图与本次请求意图是否**完全一致**(仅比资金相关键 · operator 变化不算不同意图)。
    一致 = 幂等 resume(崩溃续跑同一处置);不一致 = 另一处置在途 → 拒并发相反处置。"""
    a = _parse_intent(existing_raw)
    b = _parse_intent(intent_json)
    if not a or not b:
        return False
    keys = ("keeper_backend", "keeper_freeze_id", "keeper_decision", "other_backend", "other_freeze_id")
    return all(str(a.get(k)) == str(b.get(k)) for k in keys)


def _mint_resolution_token() -> str:
    """[返工4 P0] 双表处置租约 token(每次 claim/接管生成新值 · 只有持此 token 者能动钱/回退/推进)。"""
    return "res_" + uuid.uuid4().hex


def _claim_double_frozen(run_token: str, operator: str, keeper_backend: str, keeper_freeze_id: int,
                         keeper_decision: str, other_backend: str, other_freeze_id: int) -> dict:
    """[返工4 P0] **退款前原子 claim + 租约(resolution_token + lease_until)**(单事务 FOR UPDATE)。
    只有持有效租约(token)者能动钱/回退/推进(下游 CAS 都带 `manual_resolution_token=%s`)。分支:
      · settlement_manual → **fresh claim**:CAS manual_resolving + 持久意图 + 新 token + lease=NOW()+LEASE。返回 token。
      · manual_resolving + **有效租约未过期** → **返回 in_progress**(即使意图相同也**不放第二执行者并发续跑** ——
        这是返工3 双退根因:相同意图立即放行 + 瞬时失败者无 token 回退 → 打开反向 claim 窗口。现有效租约期一律"处理中")。
      · manual_resolving + **租约过期**(前执行者崩/超时)+ 持久意图与本次**一致** → **接管**:CAS 换新 token + 新 lease(续跑同处置)。
      · 租约过期但意图不一致 → 拒(另一处置)。
    并发相反 keeper 的另一执行者:有效租约期被拒 in_progress;过期后接管需意图一致(相反 keeper 意图不同 → 拒)。恰一个持租约动钱。"""
    from db.connection import get_db
    intent_json = _intent_json(operator, keeper_backend, keeper_freeze_id, keeper_decision,
                               other_backend, other_freeze_id)
    new_token = _mint_resolution_token()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT owner_user_id, payer_user_id, freeze_task_ref, run_status, manual_resolution, "
            "(manual_lease_until IS NOT NULL AND manual_lease_until > NOW()) AS lease_valid "
            "FROM diagnosis_runs WHERE run_token=%s FOR UPDATE",
            (run_token,),
        )
        run = cur.fetchone()
        if run is None:
            return {"ok": False, "error": "run_not_found"}
        st = run["run_status"]
        if st == "manual_resolving":
            if run["lease_valid"]:
                # 有效租约:另一执行者在处理中 → **不放并发**(即使意图相同 · 消除返工3 双退根因)
                return {"ok": False, "in_progress": True,
                        "error": "该 run 正在被处置(有效租约未过期)· 请稍后重试"}
            # 租约过期(前执行者崩/超时)→ 仅"持久意图与本次一致"才接管续跑;不一致 = 另一处置 → 拒
            if not _same_intent(run.get("manual_resolution"), intent_json):
                return {"ok": False, "error": "该 run 持久处置意图与本次不一致 · 拒绝(过期租约但意图不符)"}
            cur.execute(
                "UPDATE diagnosis_runs SET manual_resolution_token=%s, "
                "manual_lease_until=NOW()+make_interval(secs=>%s), status_changed_at=NOW() "
                "WHERE run_token=%s AND run_status='manual_resolving'",
                (new_token, int(_MANUAL_LEASE_SECONDS), run_token),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"接管过期租约 CAS 命中 {cur.rowcount} 行(预期 1)")
            # [A-1] 返回的是 **payer**(冻结行真正长在谁名下),不是 owner ——
            #   下游 `_release_dup_and_advance` 拿它做三元组校验和 release_freeze。
            return {"ok": True, "resumed": True, "token": new_token,
                    "payer_user_id": settlement_payer_user_id(run),
                    "freeze_task_ref": run["freeze_task_ref"]}
        if st != "settlement_manual":
            return {"ok": False, "error": f"run 非 settlement_manual(当前 {st})"}
        # fresh claim:settlement_manual → manual_resolving + 持久意图 + 新 token + lease(持锁 · 必命中 1)
        cur.execute(
            "UPDATE diagnosis_runs SET run_status='manual_resolving', manual_resolution=%s, "
            "manual_resolution_token=%s, manual_lease_until=NOW()+make_interval(secs=>%s), status_changed_at=NOW() "
            "WHERE run_token=%s AND run_status='settlement_manual'",
            (intent_json[:4000], new_token, int(_MANUAL_LEASE_SECONDS), run_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"claim CAS 命中 {cur.rowcount} 行(预期 1)")
        return {"ok": True, "resumed": False, "token": new_token,
                "payer_user_id": settlement_payer_user_id(run),
                "freeze_task_ref": run["freeze_task_ref"]}


def _revert_claim(run_token: str, token: str) -> None:
    """[返工4 P0] 三元组校验不过(**未动钱**)→ 回退 claim:manual_resolving → settlement_manual + 清意图/token/lease。
    **token-gated**:只有持当前租约 token 者能回退 —— 防瞬时失败的**旧/被接管执行者**把 run 误回退成 settlement_manual
    (返工3 双退根因:无 token 回退打开反向 claim 窗口 → A、C 各退一笔)。token 不匹配(已被接管/状态已变)→ rowcount 0 no-op。"""
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE diagnosis_runs SET run_status='settlement_manual', manual_resolution=NULL, "
                "manual_resolution_token=NULL, manual_lease_until=NULL, status_changed_at=NOW() "
                "WHERE run_token=%s AND run_status='manual_resolving' AND manual_resolution_token=%s",
                (run_token, token),
            )
    except Exception as e:
        logger.warning(f"[DiagRuns] claim 回退失败 run={run_token}: {e}")


def _commit_keeper_after_dup_release(run_token: str, operator: str, keeper_backend: str, keeper_freeze_id: int,
                                     keeper_decision: str, note: str,
                                     other_backend: str, other_freeze_id: int, token: str) -> dict:
    """[返工4 P0] 双表处置末步(持租约 token + 重复冻结已 release 成功后):锁 run → 复核仍 manual_resolving **且 token 匹配** →
    回填 keeper Handle + CAS→keeper *_pending + 清意图/token/lease + 审计 · 一条 UPDATE 原子。keeper 交 sweeper 正常结算。
    **token-gated**:非 manual_resolving 或 token 不匹配(已被前次/接管者推进)→ 幂等返回"已处置"(重复笔已退 · 不重复推进)。"""
    from db.connection import get_db
    target = "commit_pending" if keeper_decision == "commit" else "release_pending"
    audit = (f"manual_double_resolved_by:{operator}|keeper={keeper_backend}#{keeper_freeze_id}:{keeper_decision}"
             f"|dup_released={other_backend}#{other_freeze_id}|note={note[:220]}")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT run_status, manual_resolution_token FROM diagnosis_runs WHERE run_token=%s FOR UPDATE", (run_token,))
            run = cur.fetchone()
            if run is None:
                return {"ok": False, "error": "run_not_found"}
            if run["run_status"] != "manual_resolving" or run["manual_resolution_token"] != token:
                # 已被前次/接管者推进,或租约已被接管(token 变) → 幂等:重复笔已退,本次不重复推进 keeper
                return {"ok": True, "idempotent": True,
                        "note": f"run 已被推进或租约被接管(当前 {run['run_status']})· 重复冻结已退款 · 幂等无操作"}
            cur.execute(
                "UPDATE diagnosis_runs SET freeze_id=%s, freeze_backend=%s, run_status=%s, status_changed_at=NOW(), "
                "last_settlement_error=%s, settlement_attempts=0, next_settlement_at=NOW(), "
                "manual_resolution=NULL, manual_resolution_token=NULL, manual_lease_until=NULL "
                "WHERE run_token=%s AND run_status='manual_resolving' AND manual_resolution_token=%s",
                (int(keeper_freeze_id), keeper_backend, target, audit[:480], run_token, token),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"keeper CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
    except Exception as e:
        return {"ok": False, "error": f"keeper 推进失败(已回滚 · 重复冻结已退款 · run 仍 manual_resolving 可续跑): {e}"}
    _alert("manual_double_resolved", run_token, audit)
    _write_settlement_audit(run_token, operator, "manual_resolve_double", audit)  # [返工2 item8] 持久审计
    return {"ok": True, "target": target, "keeper_backend": keeper_backend, "keeper_freeze_id": int(keeper_freeze_id),
            "dup_released": {"backend": other_backend, "freeze_id": int(other_freeze_id)}}


async def _release_dup_and_advance(run_token: str, operator: str, payer: int, task_ref: str,
                                   keeper_backend: str, keeper_freeze_id: int, keeper_decision: str, note: str,
                                   other_backend: str, other_freeze_id: int, token: str) -> dict:
    """[返工3 P0] claim 获胜后(持 manual_resolving)真正动钱两步:先 release 重复笔(幂等)→ 成功后推进 keeper。
    失败保 manual_resolving(持久意图在)· 交 admin 重触发 / sweeper 自动续跑(release_freeze 幂等安全)。
    🔴 billing 只调不改(release_freeze 四参关键字)。

    [返工3 修复净增量] **动钱前 task_ref-bound 三元组校验(keeper+other)· fresh/resume/sweeper 唯一动钱入口都经此**:
      billing 的 release_freeze/commit_freeze 给了 freeze_id+user_id 时**只按 id+user_id 定位 · 不约束 task_ref**;
      若 admin 误填 other/keeper_freeze_id 成同一 user 名下**另一任务**的冻结,不绑 task_ref 会退/结算错任务的钱。
      故此处对两笔都校验 (freeze_id + user_id + **task_ref**) 三元组;校验存在性(非 status · 允许 resume 时 other 已软退
      仍匹配自身 task_ref → 不破幂等)。任一不匹配 → 告警 + **token-gated 回退 claim 到 settlement_manual**(未动钱 · 坏意图回人工队列)。
    [返工4 P0] 全程带租约 token:回退(_revert_claim)/ 推进(_commit_keeper_after_dup_release)都 `manual_resolution_token=%s` gated —
      旧/被接管执行者 token 不匹配 → CAS no-op(不误回退、不重复推进)· release_freeze 幂等 → 恰一次。"""
    if not await asyncio.to_thread(_verify_freeze_exists, keeper_backend, int(keeper_freeze_id), payer, task_ref):
        _alert_ratelimited("manual_dup_keeper_mismatch", run_token,
                           f"keeper 三元组(含 task_ref)不匹配 {keeper_backend}#{keeper_freeze_id} · 回退 settlement_manual")
        await asyncio.to_thread(_revert_claim, run_token, token)
        return {"ok": False, "error": "keeper 冻结三元组(backend+freeze_id+user+task_ref)不匹配 · 拒绝动钱(claim 已回退 settlement_manual)"}
    if not await asyncio.to_thread(_verify_freeze_exists, other_backend, int(other_freeze_id), payer, task_ref):
        _alert_ratelimited("manual_dup_other_mismatch", run_token,
                           f"other 三元组(含 task_ref)不匹配 {other_backend}#{other_freeze_id} · 回退 settlement_manual")
        await asyncio.to_thread(_revert_claim, run_token, token)
        return {"ok": False, "error": "other(重复)冻结三元组(含 task_ref)不匹配 · 拒绝动钱(claim 已回退 settlement_manual)"}
    from middleware.billing import release_freeze
    try:
        r = await release_freeze(
            freeze_id=int(other_freeze_id), task_ref=task_ref,
            user_id=payer, freeze_table=other_backend,
            reason=f"双表重复冻结去重退款 run={run_token} · by {operator}",
        )
    except Exception as e:
        _alert_ratelimited("manual_dup_release_exc", run_token, f"other freeze release exc: {str(e)[:200]}")
        return {"ok": False, "error": f"重复冻结退款异常(run 仍 manual_resolving · 可续跑): {str(e)[:200]}"}
    if r.get("ambiguous"):
        _alert_ratelimited("manual_dup_release_ambiguous", run_token, f"other release ambiguous: {r}")
        return {"ok": False, "error": "重复冻结退款遇 ambiguous · 需人工核对底层冻结(run 仍 manual_resolving)"}
    if not r.get("success"):
        _alert_ratelimited("manual_dup_release_fail", run_token, f"other release not success: {r}")
        return {"ok": False, "error": f"重复冻结退款未成功(run 仍 manual_resolving · 可续跑): {r.get('reason')}"}
    # [返工5 P0] release_freeze 对**已 committed / 非 frozen** 冻结也返 {success:True, idempotent:True, status:<非 released>}
    #   —— 但钱**并未退回**(commit 已消费 · release 不可撤销 commit)。若仅凭 success=True 就推进 keeper,
    #   等于把"已扣费的重复笔"记成"已退款"并继续结算 keeper → **双扣**(客户两笔都被扣却记录为已退)。
    #   规则(reviewer P0):仅接受"本次真实 release(非 idempotent · 有 balance 无 status)"或"idempotent 且 status=='released'
    #   (前次已退 · resume 幂等)";其余(尤其 committed)→ **禁推进 keeper** · token-gated 回退 settlement_manual · 告警转人工
    #   (走 admin 退款工单真实退此已消费笔后,再重触发处置)。release 是 no-op(status!=frozen)故未动钱 · 回退安全。
    if r.get("idempotent") and r.get("status") != "released":
        _alert_ratelimited(
            "manual_dup_other_not_released", run_token,
            f"other 重复冻结未真实退款(idempotent status={r.get('status')} · 疑已 committed/消费)· 拒推进 keeper · 回退 settlement_manual 转人工")
        await asyncio.to_thread(_revert_claim, run_token, token)
        return {"ok": False,
                "error": f"重复冻结未真实退款(idempotent status={r.get('status')})· 已消费不可 release 撤销 · "
                         f"已回退 settlement_manual · 请先走 admin 退款工单人工退此笔,再重触发处置(禁自动推进 keeper 防双扣)"}
    _write_settlement_audit(run_token, operator, "dup_release",  # [返工2 item8] 重复笔退款持久留痕
                            f"{other_backend}#{other_freeze_id} released(idempotent={r.get('idempotent')} status={r.get('status')})")
    return await asyncio.to_thread(
        _commit_keeper_after_dup_release, run_token, operator, keeper_backend, int(keeper_freeze_id),
        keeper_decision, note, other_backend, int(other_freeze_id), token)


async def resolve_double_frozen(run_token: str, operator: str, keeper_backend: str, keeper_freeze_id: int,
                                keeper_decision: str, other_backend: str, other_freeze_id: int,
                                note: str) -> dict:
    """[返工3 P0] 双表都冻结的人工处置(**退款前原子 claim** · 杜绝并发相反 keeper 双退)。
    模型:两笔是同一诊断的**重复冻结**(应只保留一笔)——
      · keeper(admin 选定保留笔):按 keeper_decision(commit=交付 / release=退款)进状态机由 sweeper 结算;
      · other(重复笔):**始终 release 退款**(重复扣不该消费)。
    顺序(claim-before-money · 恰一次):
      1) **原子 claim**:CAS settlement_manual→manual_resolving + 持久处置意图(只有获胜者动钱 · 并发相反 keeper 的
         另一 admin 拿锁后见状态已变即拒 → 不会各退一笔);幂等 resume:已 manual_resolving 且意图完全一致 → 续跑;
      2) 动钱(_release_dup_and_advance):**先 task_ref-bound 校验两笔三元组**(任一不匹配即回退 claim→settlement_manual ·
         未动钱)→ release 重复笔(billing 幂等)→ CAS manual_resolving→keeper *_pending + 清持久意图。
    🔴 billing 只调不改(release_freeze 四参关键字)。校验并入 _release_dup_and_advance 单一动钱入口(fresh/resume 同守)。"""
    if keeper_backend not in _FREEZE_TABLE or other_backend not in _FREEZE_TABLE:
        return {"ok": False, "error": "backend 必须是 legacy/v35"}
    if keeper_backend == other_backend:
        return {"ok": False, "error": "双表处置的两笔必须分属 legacy 与 v35(同表非双表歧义)"}
    if keeper_decision not in ("commit", "release"):
        return {"ok": False, "error": "keeper_decision 必须是 commit/release"}
    # 1) 原子 claim + 租约(退款前持久化处置权 · 单事务 FOR UPDATE · 返回 token)
    claim = await asyncio.to_thread(_claim_double_frozen, run_token, operator, keeper_backend, keeper_freeze_id,
                                    keeper_decision, other_backend, other_freeze_id)
    if not claim.get("ok"):
        return claim
    payer, task_ref = claim["payer_user_id"], claim["freeze_task_ref"]
    # 2) 动钱(带 token):先 task_ref-bound 三元组校验(不过即 token-gated 回退 · 未动钱)→ release 重复笔 → 推进 keeper
    return await _release_dup_and_advance(run_token, operator, payer, task_ref, keeper_backend, int(keeper_freeze_id),
                                          keeper_decision, note, other_backend, int(other_freeze_id), claim["token"])


async def resume_double_frozen(run_token: str) -> dict:
    """[返工4 P0] 崩溃续跑:run 卡 manual_resolving 且**租约已过期**(前执行者崩/超时)→ 按持久意图**接管**续跑。
    走 _claim_double_frozen 的接管路径(有效租约期返 in_progress 不抢在途处理 · 过期才 CAS 拿新 token)· 幂等:release_freeze
    幂等 + keeper CAS token-gated 只命中当前租约持有者。sweeper 仅对租约过期 run 调 + admin 可重触发。"""
    run = get_run(run_token)
    if run is None or run.get("run_status") != "manual_resolving":
        return {"ok": False, "reason": "not_manual_resolving"}
    intent = _parse_intent(run.get("manual_resolution"))
    if not intent or intent.get("other_backend") not in _FREEZE_TABLE \
            or intent.get("keeper_backend") not in _FREEZE_TABLE:
        _alert_ratelimited("manual_resolving_bad_intent", run_token, "manual_resolving 无有效持久意图 · 需人工核对")
        return {"ok": False, "reason": "bad_intent"}
    # 接管(仅租约过期 · 拿新 token);有效租约 → in_progress(不抢在途处理)· 意图取自持久列(与本次一致 → 接管)
    claim = await asyncio.to_thread(
        _claim_double_frozen, run_token, intent.get("operator", "system(resume)"),
        intent["keeper_backend"], int(intent["keeper_freeze_id"]), intent.get("keeper_decision", "release"),
        intent["other_backend"], int(intent["other_freeze_id"]))
    if not claim.get("ok"):
        return claim  # in_progress(租约有效)或状态已变
    payer, task_ref = claim["payer_user_id"], claim["freeze_task_ref"]
    return await _release_dup_and_advance(
        run_token, intent.get("operator", "system(resume)"), payer, task_ref,
        intent["keeper_backend"], int(intent["keeper_freeze_id"]), intent.get("keeper_decision", "release"),
        "resume", intent["other_backend"], int(intent["other_freeze_id"]), claim["token"])


# ============================================================================
# [返工3 P1] delivery_repair_pending 受控处置闭环(admin-only · CAS+持久意图+审计+幂等 · 不碰 billing)
# ============================================================================
def list_delivery_repair(limit: int = 100, resolved_only: bool = False, offset: int = 0) -> list:
    """[返工3 P1 / 返工5 P1] 列 delivery_repair_pending run(钱已 committed/exempt 但产物 0 行 · 供 admin 补发/核销)。
    [返工5 P1] **已核销(ops_resolved:*)默认排除出活跃队列** —— 否则历史 resolved 按最老排序占满 LIMIT,新退款异常从后台队列消失。
      · resolved_only=False(默认·活跃队列):仅 awaiting + refund_pending(未 resolved)· 最老优先(先清积压)。
      · resolved_only=True(历史分页):仅 ops_resolved:*(已核销)· 最新优先 + offset 翻页。
    附 has_product(完整产物是否已恢复 → 可 republish)+ resolved/refund_pending/repair_stage 三阶段。
    [P1-2] 附 freeze_backend(前端据此区分受控处置入口);
      v35 + refund_pending 行附**只读退款预览** v35_refund_preview(freeze_id/owner/总额/逐池)；仅 paid + v35 +
      delivery_repair_pending + refund_pending + committed + amount_bonus=0 才允许自动执行。含赠送额度只标人工核对。"""
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        if resolved_only:
            cur.execute(
                "SELECT run_token, session_id, owner_user_id, payer_user_id, brand_id, billing_mode, run_status, "
                "freeze_id, freeze_backend, freeze_task_ref, "
                "last_settlement_error, status_changed_at, created_at FROM diagnosis_runs "
                "WHERE run_status='delivery_repair_pending' AND last_settlement_error LIKE 'ops_resolved:%%' "
                "ORDER BY status_changed_at DESC LIMIT %s OFFSET %s",
                (max(1, int(limit)), max(0, int(offset))),
            )
        else:
            cur.execute(
                "SELECT run_token, session_id, owner_user_id, payer_user_id, brand_id, billing_mode, run_status, "
                "freeze_id, freeze_backend, freeze_task_ref, "
                "last_settlement_error, status_changed_at, created_at FROM diagnosis_runs "
                "WHERE run_status='delivery_repair_pending' "
                "AND (last_settlement_error IS NULL OR last_settlement_error NOT LIKE 'ops_resolved:%%') "
                "ORDER BY status_changed_at ASC LIMIT %s",
                (max(1, int(limit)),),
            )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        for r in rows:
            # [返工4 P1] has_product 须判**完整**产物(total_score 非空)· 不是"有任意一行"(空壳占位不算可 republish)
            cur.execute("SELECT 1 FROM diagnosis_records WHERE session_id=%s AND total_score IS NOT NULL LIMIT 1", (r["session_id"],))
            r["has_product"] = cur.fetchone() is not None
            lse = r.get("last_settlement_error") or ""
            # [返工4 P1] 三阶段:awaiting(无标记·待处置)/ refund_pending(已 writeoff·退款待确认·仍告警)/ resolved(已确认退款完成·抑告警)
            r["resolved"] = lse.startswith("ops_resolved:")
            r["refund_pending"] = lse.startswith("refund_pending:")
            r["repair_stage"] = ("resolved" if r["resolved"] else "refund_pending" if r["refund_pending"] else "awaiting")
            # [P1-2] v35 + refund_pending → 只读退款预览(前端弹窗展示 owner/冻结/总额/逐池 · 实际以执行事务核验为准)
            r["v35_refund_preview"] = None
            r["can_execute_v35_refund"] = False
            r["bonus_refund_requires_manual"] = False
            if r.get("freeze_backend") == "v35" and r["repair_stage"] == "refund_pending" \
                    and r.get("freeze_id") and settlement_payer_user_id(r) is not None:
                try:
                    cur.execute(
                        "SELECT amount_total, amount_tool, amount_publish, amount_bonus, status "
                        "FROM customer_credit_freezes WHERE id=%s AND task_ref=%s AND customer_user_id=%s",
                        (int(r["freeze_id"]), r.get("freeze_task_ref"), settlement_payer_user_id(r)))
                    _fz = cur.fetchone()
                    if _fz is not None:
                        r["v35_refund_preview"] = {
                            "owner_user_id": int(r["owner_user_id"]),
                            "payer_user_id": settlement_payer_user_id(r), "freeze_id": int(r["freeze_id"]),
                            "total": int(_fz["amount_total"] or 0), "tool": int(_fz["amount_tool"] or 0),
                            "publish": int(_fz["amount_publish"] or 0), "bonus": int(_fz["amount_bonus"] or 0),
                            "freeze_status": _fz["status"],
                        }
                        _bonus = int(_fz["amount_bonus"] or 0)
                        r["bonus_refund_requires_manual"] = _bonus > 0
                        r["can_execute_v35_refund"] = (
                            r.get("billing_mode") == "paid"
                            and r.get("run_status") == "delivery_repair_pending"
                            and r.get("freeze_backend") == "v35"
                            and r.get("repair_stage") == "refund_pending"
                            and r.get("freeze_task_ref") == freeze_task_ref(r["run_token"])
                            and _fz["status"] == "committed"
                            and _bonus == 0
                        )
                except Exception:
                    pass   # 预览失败不阻断列表(执行事务会 fail-closed 核验)
            for _k in ("status_changed_at", "created_at"):
                if r.get(_k) is not None and hasattr(r[_k], "isoformat"):
                    r[_k] = r[_k].isoformat()
    return rows


# [返工5 复审二 P1] 已删除 _verify_refund_work_order_completed 与"手填 external_ref + 原扣费金额即标已退"的自证闭环。
#   老板复审二:confirm 只读原冻结金额 + 接受手填任意凭证 = 系统自造凭证再自证退款(假闭环),且只核总额不核资金池。
#   正解:confirm 只能**引用并只读核验真实钱包退款流水**(finance 退款路径产生的 type='refund'),见 _verify_ledger_refund。


# [单账本接线 2026-08-17 · R4 孤儿清理] 以下 v35 专用符号已删除 —— 它们的**唯一**使用方
# 是本批退役的两个 v35 分支(_verify_ledger_refund 的 else 支 / refund_and_confirm_v35_delivery),
# 删除时逐个 grep 实证「本文件仅剩定义行、外部 0 引用」:
#   _V35_AGENT_OWNERSHIP_ERROR / _MESSAGE · _V35_CUSTOMER_OWNERSHIP_ERROR / _MESSAGE ·
#   _V35_REFUND_FREEZE_NOT_COMMITTED_ERROR · _V35PostWriteVerificationConflict ·
#   _normalise_agent_user_id · _v35_agent_ownership_mismatch · _v35_customer_ownership_mismatch
# ⚠️ _REFUND_CONFIRM_REQUIRES_PAID_ERROR **不是** v35 专用(legacy confirm_refund 仍在用),保留。
_REFUND_CONFIRM_REQUIRES_PAID_ERROR = "REFUND_CONFIRM_REQUIRES_PAID"


def _verify_ledger_refund(cur, run_token: str, run, refund_tx_id) -> dict:
    """[返工5 复审二 P1] **只读**核验:引用的 refund_tx_id 是绑定本 run 冻结的**真实钱包退款流水**(finance 退款路径产生的
    `type='refund'`),且 paid + committed freeze + owner + freeze/wallet/consume/refund **服务商归属一致**
    + **逐资金池拆分** + 金额全对。
    **拒 CMT(consume 只证扣费)/RLS(release 只适用未提交冻结)**。
    无真实退款流水 → 返 ok:False(调用方保持 refund_pending · 禁手填凭证标记已退)。**只 SELECT · 不改余额 · 不写任何 billing 表**。
    返 {ok:True, total, pool_split(dict), refund_tx_ref} 或 {ok:False, error}。链:run.freeze → consume 流水 → refund 流水(order_id/related_order_id 绑定)。"""
    backend = run.get("freeze_backend"); fid = run.get("freeze_id")
    # [A-1] 冻结/流水一律按 **payer** 定位:平台承担腿的冻结与退款流水都在平台钱包上,
    #   拿 owner 查会 fail-closed 拒掉一笔真实退款(或更糟:核不出来只能人工)。
    tref = run.get("freeze_task_ref"); payer = settlement_payer_user_id(run)
    expected_tref = freeze_task_ref(run_token)
    if tref != expected_tref:
        return {
            "ok": False,
            "error_code": "RUN_FREEZE_TASK_REF_MISMATCH",
            "error": (
                f"run_token 与资金 task_ref 绑定不符({tref!r} != {expected_tref!r})"
                "· fail-closed 拒"
            ),
        }
    if backend not in ("legacy", "v35") or not fid:
        return {"ok": False, "error": "本 run 无有效冻结绑定(freeze_id/backend)· 无法核验退款(fail-closed)"}
    try:
        tx_id = int(str(refund_tx_id).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "退款流水 id 必须是整数 · 请填 finance 退款路径产生的真实退款流水(type=refund)id"}

    if backend == "legacy":
        cur.execute("SELECT amount_bonus, amount_commission, amount_paid, amount_total, user_id "
                    "FROM point_freezes WHERE id=%s AND user_id=%s AND task_ref=%s", (int(fid), payer, tref))
        fz = cur.fetchone()
        if fz is None:
            return {"ok": False, "error": "定位不到本 run 的 legacy 冻结(id+user+task_ref)· fail-closed 拒"}
        expect = {"bonus": int(fz["amount_bonus"] or 0), "commission": int(fz["amount_commission"] or 0),
                  "paid": int(fz["amount_paid"] or 0)}
        total = int(fz["amount_total"] or 0)
        # 本冻结的扣费(consume)流水:order_id='CMT{fid}-...'(commit_freeze 写入)· 取 id + order_id 串
        cur.execute("SELECT id, order_id, point_type, amount FROM point_transactions "
                    "WHERE type='consume' AND user_id=%s AND order_id LIKE %s",
                    (payer, f"CMT{int(fid)}-%"))
        _crows = cur.fetchall() or []
        if not _crows:
            return {"ok": False, "error": "定位不到本冻结的扣费流水(CMT)· 无从核验退款绑定 · fail-closed 拒"}
        consume_got = {k: 0 for k in ("bonus", "commission", "paid")}
        for r in _crows:
            point_type = r["point_type"]
            if point_type not in consume_got:
                return {"ok": False, "error": f"本冻结 consume 流水含未知资金池 {point_type} · fail-closed 拒"}
            amount = int(r["amount"] or 0)
            if amount >= 0:
                return {
                    "ok": False,
                    "error_code": "INVALID_CONSUME_LEDGER_SIGN",
                    "error": f"本冻结 consume 流水 #{r['id']} 符号非法(amount={amount} · 必须 < 0)· fail-closed 拒",
                }
            consume_got[point_type] += -amount
        if consume_got != expect:
            return {
                "ok": False,
                "error": f"本冻结 consume 资金池拆分不符(实扣 {consume_got} ≠ 冻结拆分 {expect})· fail-closed 拒",
            }
        # [返工5 复审二 修复净增量] 退款流水 order_id 两种真实生产格式都接受:
        #   · admin_refund_order(唯一在线 admin 退款工具 · api/admin_api.py)写 order_id = 被退 consume 的 **order_id 串**('CMT{fid}-...')
        #   · refund_points(任务失败自动退)写 order_id = 被退 consume 的 **数字 id**
        #   否则真退了款的 run 因格式不匹配恒 fail-closed → 永卡 refund_pending + 告警风暴(本轮曾漏 · 测试同假设假绿)。
        _match = [str(r["id"]) for r in _crows] + list({r["order_id"] for r in _crows if r["order_id"]})
        # 真实退款流水:**仅 type='refund'**(拒 consume/release)· order_id ∈ 本冻结 consume 的 id 或 order_id 串
        cur.execute("SELECT id, point_type, amount FROM point_transactions "
                    "WHERE type='refund' AND user_id=%s AND order_id = ANY(%s)", (payer, _match))
        refunds = cur.fetchall() or []
        pool_keys = ("bonus", "commission", "paid")
    else:  # v35 —— [单账本接线 2026-08-17 · R4] 历史分支已删除
        # 原分支读 customer_credit_freezes / customer_agent_credit_wallets /
        # customer_credit_transactions 三张停写表核验 v35 退款。删除依据(三重不可达):
        #   ① 产出源封死:freeze_backend 唯一来自 classify_freeze_result,其输入
        #      freeze_result['freeze_table'] 唯一来自 middleware/billing.py 的
        #      freeze_points —— 该函数 :1514 **硬编码** "freeze_table": "legacy";
        #   ② 旁路封死:唯一能造 v35 冻结的 customer_credit.freeze_customer_credit
        #      全仓 0 调用方(已随本批删除);
        #   ③ 存量为 0:生产 diagnosis_runs 87 行中 0 行 freeze_backend='v35',
        #      customer_credit_freezes 0 行 status='frozen'。
        # 保留 fail-closed 出口而不是静默放行:万一将来真出现 v35 标记,必须转人工,
        # 绝不能当成"核验通过"去 CAS→resolved。
        return {
            "ok": False,
            "error_code": "V35_LEDGER_RETIRED",
            "error": (
                "本 run 标记为 v35 账本,但 v35 账本已于 2026-07-29 单账本收敛后停写 · "
                "fail-closed 拒自动核验,请人工核对资金记录"
            ),
        }

    if not refunds:
        return {"ok": False,
                "error": "未找到本 run 的**真实退款流水**(type=refund)· 保持 refund_pending(禁仅凭手填凭证标记已退款 · 请先由 finance 退款路径退款)"}
    # [单账本接线 2026-08-17 · R4] 原 `if backend == "v35":` 的 refund 流水归属校验已删除:
    # 上方 v35 分支现在无条件 return,走到这里的 backend 恒为 'legacy'(变量 freeze_agent
    # 也只在已删除的 v35 分支里赋值,留着会变成 NameError 隐患)。
    if tx_id not in {int(r["id"]) for r in refunds}:
        return {"ok": False, "error": f"退款流水 #{tx_id} 不属于本 run 的退款(未绑定本冻结的扣费)· 拒(防挪用他 run/他笔退款流水)"}
    got = {k: 0 for k in pool_keys}
    for r in refunds:
        pt = r["point_type"]
        if pt not in got:
            return {"ok": False, "error": f"退款流水含未知资金池 {pt} · fail-closed 拒"}
        amount = int(r["amount"] or 0)
        if amount <= 0:
            return {
                "ok": False,
                "error_code": "INVALID_REFUND_LEDGER_SIGN",
                "error": f"退款流水 #{r['id']} 符号非法(amount={amount} · 必须 > 0)· fail-closed 拒",
            }
        got[pt] += amount
    if got != expect:
        return {"ok": False, "error": f"退款资金池拆分不符(实退 {got} ≠ 冻结拆分 {expect})· 拒(退错池/漏池/破坏赠送或发布额度边界)"}
    if sum(got.values()) != total:
        return {"ok": False, "error": f"退款总额 {sum(got.values())} ≠ 冻结总额 {total} · 拒"}
    return {"ok": True, "total": total, "pool_split": got, "refund_tx_ref": str(tx_id)}


def _record_refund_and_cas_resolved(cur, run_token: str, run, operator: str, note: str, verify: dict) -> dict:
    """[P1-2 共用] 已核验退款(verify = _verify_ledger_refund 结果)→ **同事务**:建 run-bound 退款记录
    (SAVEPOINT 防唯一冲突毒化)+ CAS run refund_pending→ops_resolved:refund_confirmed。**不含持久审计**
    (审计由调用方统一写 · 避免两条路径重复/口径打架)。返回 {ok, idempotent?, error?, ...}。CAS 命中≠1 → **抛异常**
    (调用方 with 块回滚 · 含 v35 已产生的退款流水一并回滚)。legacy 人工 confirm 与 v35 系统退款两条路径共用
    (单一真相 · 绑定/CAS 一致 · UNIQUE(run_token)一 run 一退款 · UNIQUE(backend,refund_tx_ref)一证一 run)。"""
    cur.execute("SAVEPOINT sp_refund_rec")
    try:
        cur.execute(
            "INSERT INTO diagnosis_refund_records "
            "(run_token, freeze_task_ref, freeze_id, freeze_backend, owner_user_id, points, pool_split, refund_tx_ref, operator, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (run_token, run["freeze_task_ref"], int(run["freeze_id"]), run["freeze_backend"],
             # [A-1] 这一列记的是「钱退回谁的钱包」= **payer**(平台承担腿 ≠ owner)。
             int(settlement_payer_user_id(run)), int(verify["total"]),
             json.dumps(verify["pool_split"], ensure_ascii=False),
             verify["refund_tx_ref"], operator[:200], (note or "")[:2000]))
        cur.execute("RELEASE SAVEPOINT sp_refund_rec")
    except Exception as ie:
        cur.execute("ROLLBACK TO SAVEPOINT sp_refund_rec")
        _m = str(ie)
        if "uq_diag_refund_run" in _m:
            return {"ok": True, "idempotent": True, "note": "本 run 已有退款记录(重复 confirm)"}
        if "uq_diag_refund_txref" in _m:
            return {"ok": False, "error": f"退款流水 {verify['refund_tx_ref']} 已核销其它 run · 一退款流水只核销一个 run · 拒(防一证多用)"}
        return {"ok": False, "error": f"建退款记录失败(fail-closed 拒 · 已回滚): {_m[:160]}"}
    _marker = f"ops_resolved:refund_confirmed:tx={verify['refund_tx_ref']}:pts={verify['total']}:by={operator}"
    cur.execute(
        "UPDATE diagnosis_runs SET last_settlement_error=%s, status_changed_at=NOW() "
        "WHERE run_token=%s AND run_status='delivery_repair_pending' AND last_settlement_error LIKE 'refund_pending:%%'",
        (_marker[:480], run_token))
    if cur.rowcount != 1:
        raise RuntimeError(f"confirm_refund CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
    return {"ok": True, "resolved": True, "total": verify["total"],
            "pool_split": verify["pool_split"], "refund_tx_ref": verify["refund_tx_ref"]}


def repair_delivery(
    run_token: str,
    operator: str,
    decision: str,
    note: str,
    refund_tx_id: str = None,
    diagnosis_id: int = None,
) -> dict:
    """[返工4 P1 / 返工5 复审 P1] delivery_repair_pending 受控处置(钱已 committed 无产物 · admin-only · CAS+审计+幂等):
      · republish:产物已恢复 → 锁本 run 完整产物记录(total_score 非空)→ 只发布该条 rowcount==1 + CAS→committed。无完整产物则拒。
      · writeoff:产物不可恢复 · 走退款 → 标记 **refund_pending**(**不抑告警** · 退款未确认)+ 审计。
      · confirm_refund:**确认真实退款完成** → 必须**引用并只读核验真实钱包退款流水** `refund_tx_id`(_verify_ledger_refund):
        finance 退款路径产生的 `type='refund'` 流水,绑定本 run 冻结、owner、**逐资金池拆分**、金额全对、单次使用 →
        建 run-bound diagnosis_refund_records(引用该退款流水)+ CAS→resolved(抑告警)。**仅从 refund_pending 进入**。
        [返工5 复审二 P1] 取代"手填 external_ref + 原扣费金额即标已退"的自证闭环(系统自造凭证再自证):
          - **无真实退款流水 → 保持 refund_pending**(禁仅凭手填凭证标记已退)· 只核总额不够 · 必逐资金池拆分核对。
          - 退款记录 INSERT + 状态 CAS + 持久审计 **全在同一事务**(req#4 · 失败一并回滚)· UNIQUE(backend,refund_tx_ref)一证一 run。
          - **只读钱包流水 · 不碰 recharge_orders/refund_work_orders · 不改余额/不写任何 billing 表**(实际积分冲账由 finance 退款路径先行完成)。
    幂等:republish 已 committed / writeoff 已 refund_pending / confirm 已 resolved(或 UNIQUE run_token)→ no-op success。CAS+FOR UPDATE 保恰一次。
    🔴 若需**平台自动积分冲账**(改余额)→ 属红线 · 停下报老板(本函数绝不碰 billing/余额 · 只只读核验+绑定+留痕+状态)。"""
    if decision not in ("republish", "writeoff", "confirm_refund"):
        return {"ok": False, "error": "decision 必须是 republish/writeoff/confirm_refund"}
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT session_id, run_status, last_settlement_error, owner_user_id, payer_user_id, "
                        "billing_mode, freeze_id, freeze_backend, freeze_task_ref, final_snapshot_jsonb "
                        "FROM diagnosis_runs "
                        "WHERE run_token=%s FOR UPDATE", (run_token,))
            run = cur.fetchone()
            if run is None:
                return {"ok": False, "error": "run_not_found"}
            st = run["run_status"]
            sid = run["session_id"]
            lse = run["last_settlement_error"] or ""
            run_payer = settlement_payer_user_id(run)
            if decision == "republish":
                if st == "committed":
                    return {"ok": True, "idempotent": True, "note": "已 committed(前次 republish 已生效)"}
                if st != "delivery_repair_pending":
                    return {"ok": False, "error": f"run 非 delivery_repair_pending(当前 {st})"}
                # [返工4 修复净增量] republish 与退款路径**互斥**(资金不变量 · 不依赖前端隐藏按钮):已 writeoff(refund_pending)/
                #   已确认退款(refund_confirmed)之后**禁再补发**——否则客户既退款又拿报告=双重受益。如确需补发须先撤销退款工单。
                if lse.startswith("refund_pending:") or lse.startswith("ops_resolved:refund_confirmed:"):
                    return {"ok": False, "error": "该 run 已发起/确认退款(refund_pending/refund_confirmed)· 退款与补发互斥 · 不可再补发(如确需补发请先撤销退款工单)"}
                frozen_snapshot = run.get("final_snapshot_jsonb")
                if isinstance(frozen_snapshot, str):
                    try:
                        frozen_snapshot = json.loads(frozen_snapshot)
                    except Exception:
                        frozen_snapshot = None
                try:
                    frozen_diagnosis_id = int((frozen_snapshot or {}).get("diagnosis_id"))
                except (TypeError, ValueError):
                    frozen_diagnosis_id = None
                explicit_diagnosis_id = None
                if diagnosis_id is not None:
                    try:
                        explicit_diagnosis_id = int(diagnosis_id)
                    except (TypeError, ValueError):
                        return {"ok": False, "error": "diagnosis_id 必须是正整数"}
                    if explicit_diagnosis_id <= 0:
                        return {"ok": False, "error": "diagnosis_id 必须是正整数"}
                if frozen_diagnosis_id and explicit_diagnosis_id and frozen_diagnosis_id != explicit_diagnosis_id:
                    return {"ok": False, "error": "显式 diagnosis_id 与既有不可变产物锚冲突"}
                bound_legacy_anchor = False
                if not frozen_diagnosis_id:
                    if not explicit_diagnosis_id:
                        return {"ok": False, "error": "该 run 缺少不可变诊断产物锚；请显式提交已核验的 diagnosis_id"}
                    frozen_diagnosis_id = explicit_diagnosis_id
                    bound_legacy_anchor = True
                # 锁本 run 预结算时冻结的精确客户产物；禁止按 session 重新选择另一条记录。
                cur.execute(
                    "SELECT id, report_v2_modules_jsonb, run_token FROM diagnosis_records "
                    "WHERE id=%s AND session_id=%s AND total_score IS NOT NULL "
                    "AND (run_token IS NULL OR run_token=%s) FOR UPDATE",
                    (frozen_diagnosis_id, sid, run_token),
                )
                prow = cur.fetchone()
                if prow is None:
                    return {"ok": False, "error": "该 session 无**完整**产物(total_score 为空/仅空壳占位)· 无可发布 · 请先恢复完整报告或走 writeoff"}
                from services.report_html_renderer import is_client_report_ready
                if not is_client_report_ready(prow.get("report_v2_modules_jsonb")):
                    return {"ok": False, "error": "该诊断的客户报告尚未 ready，禁止补发"}
                did = prow["id"]
                if bound_legacy_anchor:
                    bound_snapshot = dict(frozen_snapshot or {})
                    bound_snapshot["diagnosis_id"] = did
                    cur.execute(
                        "UPDATE diagnosis_runs SET final_snapshot_jsonb=%s "
                        "WHERE run_token=%s AND run_status='delivery_repair_pending'",
                        (json.dumps(bound_snapshot, ensure_ascii=False), run_token),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("legacy diagnosis anchor bind CAS missed")
                    cur.execute(
                        "UPDATE diagnosis_records SET run_token=%s "
                        "WHERE id=%s AND session_id=%s AND (run_token IS NULL OR run_token=%s)",
                        (run_token, did, sid, run_token),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("legacy diagnosis product bind conflict")
                # 只发布这条完整记录(rowcount==1 强校验 · 防把空壳占位一并 published)
                cur.execute("UPDATE diagnosis_records SET result_visibility='published' WHERE id=%s", (did,))
                if cur.rowcount != 1:
                    raise RuntimeError(f"republish 产物发布 rowcount={cur.rowcount}(预期 1)· 回滚")
                _snap = {"type": "complete", "stage": "done", "progress": 100, "done": True, "terminal": True,
                         "diagnosis_id": did, "message": "报告已补发"}
                cur.execute(
                    "UPDATE diagnosis_runs SET run_status='committed', status_changed_at=NOW(), "
                    "final_snapshot_jsonb=%s, last_settlement_error=%s "
                    "WHERE run_token=%s AND run_status='delivery_repair_pending'",
                    (json.dumps(_snap, ensure_ascii=False),
                     f"ops_resolved:republish:{(note or '')[:200]}", run_token),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"republish CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
                _target = "committed"; _action = "delivery_republish"; _alert_kind = "delivery_repair_resolved"
                _audit = (
                    f"delivery_republish_by:{operator}|diagnosis_id={did}"
                    f"|legacy_anchor_bound={str(bound_legacy_anchor).lower()}|note={note[:220]}"
                )
            elif decision == "writeoff":
                if st != "delivery_repair_pending":
                    return {"ok": False, "error": f"run 非 delivery_repair_pending(当前 {st})"}
                if lse.startswith("ops_resolved:"):
                    return {"ok": False, "error": "已确认退款完成(resolved)· 无需再 writeoff"}
                if run["billing_mode"] == "exempt":
                    # [返工5-复审 修复净增量 P2] **exempt(免单·无扣费)run 无退款可欠** → writeoff **直接 ops_resolved(抑告警)**,不进 refund_pending。
                    #   置于 refund_pending 幂等短路**之前**,以便把已卡 refund_pending 的 exempt run 一并救回收口(否则:exempt 无冻结 →
                    #   confirm_refund 的金额核验恒 fail-closed → 永久卡 refund_pending + 每小时 stuck 告警 · 本轮 confirm 重写引入的运维死角)。
                    #   🔴 仅放宽 exempt · **paid 异常(freeze_id NULL)仍走 refund_pending → confirm fail-closed 强制人工核对冻结**(不在此顺带放宽)。
                    cur.execute(
                        "UPDATE diagnosis_runs SET last_settlement_error=%s, status_changed_at=NOW() "
                        "WHERE run_token=%s AND run_status='delivery_repair_pending' "
                        "AND (last_settlement_error IS NULL OR last_settlement_error NOT LIKE 'ops_resolved:%%')",
                        (f"ops_resolved:writeoff_exempt:by={operator}:note={(note or '')[:150]}", run_token),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"writeoff(exempt) CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
                    _target = "delivery_repair_pending(exempt_closed)"; _action = "delivery_writeoff_exempt"
                    _alert_kind = "delivery_repair_resolved"
                    _audit = f"delivery_writeoff_exempt_by:{operator}|免单无扣费无需退款·直接核销抑告警|note={(note or '')[:200]}"
                elif lse.startswith("refund_pending:"):
                    return {"ok": True, "idempotent": True, "note": "已标记 refund_pending(退款待确认完成)"}
                else:
                    # [返工4 P1] paid:writeoff **只进 refund_pending · 不抑告警**(退款未确认 · stuck-alert 仍提醒 ops 去确认)· 退款走 admin 工单
                    cur.execute(
                        "UPDATE diagnosis_runs SET last_settlement_error=%s, status_changed_at=NOW() "
                        "WHERE run_token=%s AND run_status='delivery_repair_pending' "
                        "AND (last_settlement_error IS NULL OR "
                        "     (last_settlement_error NOT LIKE 'ops_resolved:%%' AND last_settlement_error NOT LIKE 'refund_pending:%%'))",
                        (f"refund_pending:writeoff:by={operator}:note={(note or '')[:170]}", run_token),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"writeoff CAS 命中 {cur.rowcount} 行(预期 1)· 回滚")
                    _target = "delivery_repair_pending(refund_pending)"; _action = "delivery_writeoff_intent"
                    _alert_kind = "delivery_refund_pending"
                    _audit = f"delivery_writeoff_intent_by:{operator}|退款走admin工单·待确认完成|note={note[:220]}"
            else:  # confirm_refund
                if st != "delivery_repair_pending":
                    return {"ok": False, "error": f"run 非 delivery_repair_pending(当前 {st})"}
                if lse.startswith("ops_resolved:"):
                    return {"ok": True, "idempotent": True, "note": "已确认退款完成(resolved)"}
                if not lse.startswith("refund_pending:"):
                    return {"ok": False, "error": "只能从 refund_pending(先 writeoff)确认退款完成 · 请先 writeoff"}
                if run.get("billing_mode") != "paid":
                    return {
                        "ok": False,
                        "error_code": _REFUND_CONFIRM_REQUIRES_PAID_ERROR,
                        "error": "本次诊断没有已提交的付费扣款，无法确认退款完成，请人工核对",
                    }
                if not note or len(note.strip()) < 4:
                    return {"ok": False, "error": "确认退款完成必须提供证据说明(note)"}
                if run_payer is None:
                    return {"ok": False, "error": "本 run 无 payer/owner_user_id · fail-closed 拒确认"}
                # [返工5 复审二 P1] confirm **不创建凭证** · 只引用并**只读核验真实钱包退款流水** refund_tx_id
                #   (finance 退款路径产生的 type='refund' · 绑定本 run 冻结 + owner + 逐资金池拆分 + 金额 + 单次使用)。
                #   无真实退款流水 → 保持 refund_pending(禁仅凭手填凭证标记已退)。只读钱包流水 · 不改余额。
                if refund_tx_id is None or not str(refund_tx_id).strip():
                    return {"ok": False, "error": "确认退款完成必须提供**真实退款流水 id** refund_tx_id(finance 退款路径产生的 type=refund 流水 · 非手填任意凭证)"}
                _v = _verify_ledger_refund(cur, run_token, run, refund_tx_id)
                if not _v.get("ok"):
                    return {"ok": False, "error_code": _v.get("error_code"),
                            "error": _v.get("error", "退款流水核验失败 · 保持 refund_pending")}
                # [P1-2] 建记录 + CAS→resolved 走共用 helper(与 v35 系统退款单一真相)· idempotent/拒 直接返回(未/已处置)
                _res = _record_refund_and_cas_resolved(cur, run_token, run, operator, note, _v)
                if not _res.get("ok") or _res.get("idempotent"):
                    return _res
                _target = "delivery_repair_pending(refund_confirmed)"; _action = "delivery_refund_confirmed"
                _alert_kind = "delivery_repair_resolved"
                _audit = (f"delivery_refund_confirmed_by:{operator}|refund_tx={_v['refund_tx_ref']}"
                          f"|pool={_v['pool_split']}|total={_v['total']}|证据={(note or '')[:150]}")
            # [返工5 复审 req#4] 持久审计与资金处置**同事务原子写**(cur 传入 → 审计失败即随处置一并回滚 · 杜绝"已处置无留痕")
            _write_settlement_audit(run_token, operator, _action, _audit, cur=cur)
    except Exception as e:
        return {"ok": False, "error": f"delivery_repair 处置失败(已回滚 · run 状态未变): {e}"}
    _alert(_alert_kind, run_token, _audit)
    return {"ok": True, "decision": decision, "target": _target}


def refund_and_confirm_v35_delivery(run_token: str, operator: str, note: str) -> dict:
    """[P1-2] v35 交付缺失的精确退款执行 —— **[单账本接线 2026-08-17 · R4] 实现已退役**。

    函数与端点(`server.py` 的 admin 处置路由)**保留不拆**,契约不变(仍返 {ok, error});
    删掉的是函数体里对三张停写表(`customer_credit_freezes` /
    `customer_agent_credit_wallets` / `customer_credit_transactions`)的读写实现。

    退役依据(三重不可达,与 `_verify_ledger_refund` 同源):
      ① 本函数第一道门就是 `backend != "v35" → 拒`,而 freeze_backend 的唯一产出源
         `middleware/billing.py:1514` 的 freeze_points **硬编码** freeze_table="legacy";
      ② 唯一能造 v35 冻结的 `customer_credit.freeze_customer_credit` 全仓 0 调用方(已删);
      ③ 生产实证:diagnosis_runs 87 行中 0 行 freeze_backend='v35',
         customer_credit_freezes 0 行 status='frozen'。
    即本函数在**任何**输入下都只会走到 ① 的拒绝分支 —— 原实现是一段永不执行的代码。

    🔴 为什么不直接删函数:`server.py` 有 admin 端点 import 它;删了会让整个
       router 注册链断(历史教训:缺符号导致 server.py 静默跳过整个 router)。
       故保留符号 + fail-closed,绝不静默返回成功。
    """
    _ = (operator, note)  # 保留签名 · 退役后不再使用
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_status, freeze_backend FROM diagnosis_runs WHERE run_token=%s",
                (run_token,))
            run = cur.fetchone()
    except Exception as e:
        return {"ok": False, "error": f"读取 run 失败: {e}"}
    if run is None:
        return {"ok": False, "error": "run_not_found"}

    backend = run["freeze_backend"]
    if backend != "v35":
        # 唯一可达分支 —— 与退役前逐字同义(legacy 走 finance 退款 + confirm_refund)
        return {"ok": False,
                "error": f"本端点只执行 v35 退款(当前 backend={backend})· "
                         f"legacy 走 finance 退款 + confirm_refund 手填流水 id"}
    return {
        "ok": False,
        "error_code": "V35_LEDGER_RETIRED",
        "error": ("本 run 标记为 v35 账本,但 v35 账本已于 2026-07-29 单账本收敛后停写 · "
                  "自动退款通道已退役 · 请人工核对资金记录后处置"),
    }


def _alert_ratelimited(kind: str, run_token: str, detail: str, ttl: int = 3600) -> None:
    """[对抗审核 P2 #11/12] 限频资金告警(同 run_token+kind 每 ttl 秒最多一条)· 用于 release 长期重试卡住。
    Redis SET NX EX 去重;Redis 不可达则降级每次都 alert(宁多勿漏)。"""
    fired = True
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is not None:
            fired = bool(r.set(f"diag:alert_rl:{kind}:{run_token}", "1", nx=True, ex=ttl))
    except Exception:
        fired = True
    if fired:
        _alert(kind, run_token, detail)


def _release_slot_best_effort(run_token: str) -> None:
    """[对抗审核 P2 #10] sweeper 收尸/终态后主动 ZREM 并发槽(SPEC §3.4)· 不靠 TTL 慢释放。"""
    try:
        from cache import diagnosis_slots
        diagnosis_slots.release_slot(run_token)
    except Exception:
        pass


def get_settlement_alerts(limit: int = 200) -> list:
    """admin 读资金告警(最近 N 条)。"""
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is None:
            return []
        raw = r.lrange(_ALERT_LIST_KEY, 0, max(0, limit - 1))
        out = []
        for item in raw or []:
            try:
                out.append(json.loads(item))
            except Exception:
                continue
        return out
    except Exception:
        return []


# ============================================================================
# R0 分类
# ============================================================================
class FreezeClassification(NamedTuple):
    branch: str                 # 'paid' | 'exempt' | 'cancelled' | 'anomaly' | 'unknown'
    freeze_id: Optional[int]
    freeze_backend: Optional[str]
    billing_mode: str           # 'paid' | 'exempt'
    # [P0-3b 挂账 A] freeze_points 返回的 physical_split_snapshot(含权威 order)原样带下来,
    #   由 _persist_freeze_handle 在**冻结当时**落列。带默认值 → 既有四参构造
    #   (server.py 的 exempt 分支 `FreezeClassification("exempt", None, None, "exempt")`)逐字节不变。
    split_snapshot: Optional[dict] = None


def classify_freeze_result(freeze_result: Optional[dict],
                           exc: Optional[BaseException]) -> FreezeClassification:
    """R0 五分支判定(SPEC §3.0)。调用方成功传 (result, None),异常传 (None, exc)。

    分类**不耦合 fastapi**:用 getattr(exc,'status_code') 判 HTTPException(400/402=确定性失败)。
    """
    if exc is not None:
        code = getattr(exc, "status_code", None)
        if code in _DETERMINISTIC_HTTP_CODES:
            # 分支 3:明确业务拒绝(余额不足/参数非法)→ cancelled
            return FreezeClassification("cancelled", None, None, "paid")
        # 白名单外异常(超时/断连/500)→ 分支 5 未知(禁 cancelled)
        return FreezeClassification("unknown", None, None, "paid")

    r = freeze_result or {}
    if r.get("free") is True:
        # 分支 2:免单/零价 → exempt
        return FreezeClassification("exempt", None, None, "exempt")
    fid = r.get("freeze_id")
    table = r.get("freeze_table")
    if fid is not None and table in ("legacy", "v35"):
        # 分支 1:付费成功
        # [P0-3b 挂账 A] 顺手把 billing 返回的物理拆分快照带上(middleware/billing.py:1503)。
        #   只取不算:order 是冻结当时钱包偏好算出来的**权威值**,结算时再推就是猜。
        _split = r.get("physical_split_snapshot")
        return FreezeClassification("paid", int(fid), table, "paid",
                                    _split if isinstance(_split, dict) else None)
    if fid is not None:
        # 分支 4:结构异常(有 freeze_id 但 backend 缺/非法)→ 需解析 backend
        return FreezeClassification("anomaly", int(fid), None, "paid")
    # freeze_id 空且非 free(不该出现)→ 未知(禁 cancelled · 保守留 pending_freeze)
    return FreezeClassification("unknown", None, None, "paid")


# ============================================================================
# HC1 请求级幂等 admission(§3.3a)
# ============================================================================
class AdmitResult(NamedTuple):
    admitted: bool                    # True=本请求新建了 run(应继续 freeze)
    run: Optional[dict]               # admitted=False 时返回既有 run(幂等旧单/品牌活跃单)· 或 None(内部错)
    reason: str                       # 'admitted' | 'idempotent_retry' | 'brand_active' | 'internal_error'


def admit_run(owner_user_id: int, client_request_id: str, brand_id: Optional[int],
              session_id: str, run_token: str, billing_mode: str = "paid",
              *, _cursor=None) -> AdmitResult:
    """HC1:无目标 INSERT ON CONFLICT DO NOTHING → 0 行三路定性(SPEC §3.3a)。

    ①插入成功 → admitted;②冲突(0 行)→ 先查 (owner,client_request_id) 幂等旧单;再查 (owner,brand_id,活跃)
    品牌活跃单;都无 → 内部一致性错(告警)。**所有冲突路径都不冻结**(调用方据 admitted=False 精确 ZREM 释槽)。
    单事务(INSERT + SELECT 同一 get_db 连接)。

    [防御型 GEO WP2 · 2026-08-21] 新增**可选** ``_cursor`` 借用模式
    ------------------------------------------------------------------
    传 ``_cursor`` 时不自开连接,直接在**调用方事务**里做 INSERT+SELECT ——
    形态与 ``middleware/billing.freeze_points(_cursor=...)`` 完全一致
    (那是本仓既有、有三个生产调用方的模式,见
    ``services/geo_douyin/contract_freeze.py`` 顶部说明)。

    为什么需要它:§3.5 要求 confirm 在**同一事务**里完成
    「消费 preview + 创建 formal command + exact points freeze + outbox」。
    freeze 侧本来就能借游标,command 侧不能借就凑不成一个原子单元 ——
    要么放弃原子性(崩在中间会留下"钱冻了但没有 command"),
    要么把这段 INSERT 在别处再写一遍(同一谓词写两处 ⇒ 必有一处没人验)。

    🔴 **默认行为一字未动**:``_cursor=None`` 时走原来的 ``with get_db()``,
       连重试语义都不变。keyword-only 保证既有 60+ 个位置参数调用点全部不受影响
       (调用方 census:生产仅 ``server.py`` 一处 + 判据脚本)。
    🔴 借用模式下**不重试**:重试的前提是"新事务里冲突已清",而借来的事务
       是调用方的,在里面重试只会在同一个快照里再撞一次 —— 那是假重试。
       借用模式把 ``internal_error`` 如实返回,由调用方决定回滚还是重来。
    """
    from db.connection import get_db

    class _BorrowedTxn:
        """把借来的游标包成 ``with`` 可用的形状。刻意无 commit/rollback ——
        事务归调用方所有(与 billing 的 ``_BorrowedCursorTransaction`` 同形)。"""

        def __init__(self, cursor):
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self._cursor

    def _conn_ctx():
        return get_db() if _cursor is None else _BorrowedTxn(_cursor)

    def _attempt() -> AdmitResult:
        with _conn_ctx() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO diagnosis_runs
                    (run_token, session_id, owner_user_id, brand_id, client_request_id,
                     billing_mode, freeze_task_ref, run_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending_freeze')
                ON CONFLICT DO NOTHING
                RETURNING run_token
                """,
                (run_token, session_id, owner_user_id, brand_id, client_request_id,
                 billing_mode, freeze_task_ref(run_token)),
            )
            if cur.fetchone():
                return AdmitResult(True, None, "admitted")
            # 冲突:0 行 —— 依次定性(两种唯一约束都要接住)
            cur.execute(
                "SELECT * FROM diagnosis_runs WHERE owner_user_id=%s AND client_request_id=%s LIMIT 1",
                (owner_user_id, client_request_id),
            )
            idem = cur.fetchone()
            if idem:
                return AdmitResult(False, dict(idem), "idempotent_retry")
            if brand_id is not None:
                cur.execute(
                    "SELECT * FROM diagnosis_runs WHERE owner_user_id=%s AND brand_id=%s "
                    "AND run_status = ANY(%s) ORDER BY created_at DESC LIMIT 1",
                    (owner_user_id, brand_id, list(ACTIVE_STATUSES)),
                )
                active = cur.fetchone()
                if active:
                    return AdmitResult(False, dict(active), "brand_active")
            # 都查不到 → 内部一致性错误(竞态:冲突 run 在 INSERT 冲突与 SELECT 间转终态退出 partial 索引)
            return AdmitResult(False, None, "internal_error")

    # [对抗审核 P2 #8] internal_error = 瞬时竞态(冲突行刚转终态退出 uq_diag_active_per_brand partial)→
    #   在新事务重试一次 INSERT(此时冲突已清 · 应能插入);仍 internal_error 才如实返回(调用方 409 可重试)。
    res = _attempt()
    if _cursor is None and not res.admitted and res.reason == "internal_error":
        # 见上:借用模式下重试是假重试(同一事务同一快照),故只在自管事务时重试。
        res = _attempt()
    return res


# ============================================================================
# CAS / 读取
# ============================================================================
def get_run(run_token: str) -> Optional[dict]:
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM diagnosis_runs WHERE run_token=%s", (run_token,))
        row = cur.fetchone()
        return dict(row) if row else None


def _enqueue_diagnosis_run_terminal(cur, run: dict, terminal_state: str) -> None:
    from services.notification_events import NotificationEventType, RecipientKind
    from services.notification_outbox import (
        enqueue_admin_notification_events,
        enqueue_notification_event,
    )

    event_by_terminal = {
        "committed": NotificationEventType.DIAGNOSIS_COMPLETED,
        "completed_exempt": NotificationEventType.DIAGNOSIS_COMPLETED,
        "released": NotificationEventType.DIAGNOSIS_REFUNDED,
        "failed_exempt": NotificationEventType.DIAGNOSIS_FAILED,
        "cancelled": NotificationEventType.DIAGNOSIS_CANCELLED,
        "cancelled_no_freeze": NotificationEventType.DIAGNOSIS_CANCELLED,
        "settlement_manual": NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED,
        "delivery_repair_pending": NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED,
    }
    event_type = event_by_terminal.get(str(terminal_state))
    if event_type is None:
        return
    session_id = str(run.get("session_id") or run.get("run_token") or "")
    facts = {
        "business_no": f"诊断-{session_id}",
        "status": {
            "committed": "报告已生成",
            "completed_exempt": "报告已生成",
            "released": "诊断未完成，费用已退回",
            "failed_exempt": "诊断未完成",
            "cancelled": "诊断已取消",
            "cancelled_no_freeze": "诊断已取消，未扣费",
            "settlement_manual": "需要平台人工核验",
            "delivery_repair_pending": "报告需要平台人工补发",
        }[str(terminal_state)],
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": "请在品牌体检历史中查看结果和下一步。",
    }
    enqueue_notification_event(
        cur,
        event_type=event_type,
        business_id=str(run.get("run_token") or session_id),
        terminal_state=str(terminal_state),
        recipient_user_id=int(run["owner_user_id"]),
        recipient_kind=RecipientKind.USER,
        facts=facts,
    )
    if terminal_state in {"settlement_manual", "delivery_repair_pending"}:
        enqueue_admin_notification_events(
            cur,
            event_type=event_type,
            business_id=str(run.get("run_token") or session_id),
            terminal_state=str(terminal_state),
            facts=facts,
        )


def _cas(run_token: str, from_statuses, to_status: str, extra: Optional[dict] = None,
         finish: bool = False, *, _cursor=None) -> bool:
    """原子状态迁移:UPDATE ... WHERE run_status IN (from) RETURNING。返回是否命中 1 行。
    finish=True 附带 finished_at=NOW()(终态 cancel 用)。

    [防御型 GEO WP2 · 2026-08-21] 新增**可选** keyword-only ``_cursor`` 借用模式,
    与 ``admit_run(_cursor=…)`` / ``middleware.billing.freeze_points(_cursor=…)`` 同形。
    ``_cursor=None`` 时行为一字未动(仍是自开 ``with get_db()``)。
    SQL 只写一份 —— 借用与自开共用同一条语句,不另起第二份谓词。
    """
    from db.connection import get_db
    sets = ["run_status=%s", "status_changed_at=NOW()"]
    if finish:
        sets.append("finished_at=NOW()")
    params = [to_status]
    for k, v in (extra or {}).items():
        sets.append(f"{k}=%s")
        params.append(v)
    params.append(run_token)
    params.append(list(from_statuses))
    sql = (f"UPDATE diagnosis_runs SET {', '.join(sets)} "
           f"WHERE run_token=%s AND run_status = ANY(%s) RETURNING *")

    def _run(cur) -> bool:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is not None:
            _enqueue_diagnosis_run_terminal(cur, dict(row), to_status)
        return row is not None

    if _cursor is not None:
        return _run(_cursor)
    with get_db() as conn:
        return _run(conn.cursor())


def _persist_freeze_handle(run_token: str, freeze_id: int, freeze_backend: str,
                           split_snapshot: Optional[dict] = None, *,
                           payer_user_id: Optional[int] = None, _cursor=None) -> None:
    """回填 FreezeHandle(freeze_id/backend)· 必须在 CAS 进 running 前(满足 chk_freeze_handle)。

    [P0-3b 挂账 A] 同一条 UPDATE 顺带落 `reserved_split_snapshot_jsonb` —— **冻结当时**
    的物理三池拆分 + 权威 order。落在这里而不是结算时回读,是因为 order 是冻结当时
    钱包扣费偏好的产物,过后就再也复原不出来了(这正是 P0-3 只能对单池自动按比例、
    多池一律转人工的原因)。与 organization 链把 split 存进 charge link 同一条
    「不可变预留快照」纪律。

    `split_snapshot=None`(exempt / 老调用方 / billing 没给)→ 该列写 NULL,
    结算侧回落 P0-3 行为,不猜。

    [合流 2026-08-25 Review-CTO] 两侧扩展并集:生产侧(P0-3b,已上线)的
    ``split_snapshot`` 落列语义为准;防御线侧的 ``_cursor`` 借用模式保留
    (同 ``_cas``,默认路径行为不变)。SQL 只有一份 —— 借用与否走同一条语句。

    [A-1 · 2026-08-25] 追加 ``payer_user_id``:冻结行长在谁名下,在**冻结当时**
    就落列,与 ``split_snapshot`` 同一条「不可变预留快照」纪律。

    🔴 它与 ``split_snapshot`` 的写法**刻意不同** —— 用 ``COALESCE(%s::integer, payer_user_id)``:
       ``None`` 的语义是「本次调用不知道 payer,别动这一列」,而不是「把它清成 NULL」。
       理由是这个函数有**四个不传 payer 的既有调用方**(sweeper 收尸定位 /
       activate_after_freeze 三处),它们只想回填 freeze 句柄;
       若无条件写 NULL,会把 confirm 当时落好的真实 payer 抹掉,
       平台腿的钱又变成拿 owner 去结算 —— 也就是本包要修的那个 bug 借由
       「顺手回填一次句柄」原地复活。
       (``split_snapshot`` 的无条件写是 P0-3b 已上线的语义,本包**一字不动**。)
    """
    from db.connection import get_db
    sql = ("UPDATE diagnosis_runs SET freeze_id=%s, freeze_backend=%s, "
           "reserved_split_snapshot_jsonb=%s, "
           "payer_user_id=COALESCE(%s::integer, payer_user_id) WHERE run_token=%s")
    params = (freeze_id, freeze_backend,
              json.dumps(split_snapshot, ensure_ascii=False) if isinstance(split_snapshot, dict) else None,
              int(payer_user_id) if payer_user_id is not None else None,
              run_token)
    if _cursor is not None:
        _cursor.execute(sql, params)
        return
    with get_db() as conn:
        conn.cursor().execute(sql, params)

def start_run_in_caller_txn(cur, run_token: str, *, freeze_id=None,
                            freeze_backend: str = "legacy",
                            split_snapshot: Optional[dict] = None,
                            payer_user_id: Optional[int] = None) -> bool:
    """[防御型 GEO WP2] 在**调用方事务**里把 run 从 ``pending_freeze`` 推进 ``running``。

    为什么必须有这一腿 —— 一个被判据抓出来的真缺陷
    ------------------------------------------------
    confirm 第一版做完 admit(``pending_freeze``)+ freeze + outbox 就 commit 了,
    **从来没有人把 run 推进 running**。后果不是"状态不好看",是资金事故:

      · ``uq_diag_active_per_brand`` 把该品牌锁死,后续诊断一律 409;
      · 10 分钟后 sweeper 的 ``pending_freeze`` 收尸双表定位到那笔冻结,
        CAS 进 ``release_pending`` → **算力被退回、诊断静默死掉**,
        而用户那边显示的是"已开始"。

    现役 ``activate_after_freeze`` 做的就是这件事,但它自开三条连接,
    塞不进 confirm 的单一事务。这里复用它的**同两条**底层写(
    ``_persist_freeze_handle`` / ``_cas``),只是都走借来的游标 ——
    SQL 仍然只有一份,不制造第二套谓词。

    ``chk_freeze_handle`` 约束:``billing_mode='paid'`` 的行进 running 前
    必须已有 ``freeze_id + freeze_backend``。所以顺序是先回填后 CAS,
    且 0 价(无冻结行)必须按现役口径纠正成 ``billing_mode='exempt'``
    —— 与 ``activate_after_freeze`` 的 exempt 分支逐字同义。
    """
    if freeze_id is not None:
        # [A-1 / A-4] 冻结的**三件事实**(句柄 / 三池拆分 / payer)必须在冻结当时
        #   一起落列。少落 split → 部分履约结算读不到拆分 → reserved_split_order_unknown
        #   → settlement_manual,资金长挂(Codex P1-4);少落 payer → 结算拿错账号
        #   定位冻结 → 永远 commit 不掉(Codex P0-1 第二层)。
        _persist_freeze_handle(run_token, int(freeze_id), str(freeze_backend),
                               split_snapshot, payer_user_id=payer_user_id, _cursor=cur)
        return _cas(run_token, ["pending_freeze"], "running", _cursor=cur)
    # 无冻结句柄:0 价。paid 行不许无句柄进 running(chk_freeze_handle),
    # 按现役 activate_after_freeze 的 exempt 分支纠正 billing_mode。
    # 🔴 这条分支**不再包含平台腿**:平台承担腿现在真的产生冻结行、真的走物理结算,
    #    它会带着 freeze_id 走上面那一支。落到这里的只剩「这次真的不要钱」。
    return _cas(run_token, ["pending_freeze"], "running",
                {"billing_mode": "exempt"}, _cursor=cur)





def mark_product_pending(
    run_token: str,
    session_id: str,
    diagnosis_id: int,
    snapshot: dict,
) -> None:
    """Freeze the exact customer-ready product identity before settlement.

    The product stamp and run snapshot share one transaction.  Settlement
    retries must therefore use this immutable diagnosis id rather than select
    another row that happens to share the session id.
    """
    from db.connection import get_db
    expected_id = int(diagnosis_id)
    frozen_snapshot = dict(snapshot)
    if int(frozen_snapshot.get("diagnosis_id") or 0) != expected_id:
        raise RuntimeError("diagnosis product snapshot identity mismatch")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT report_v2_modules_jsonb FROM diagnosis_records "
            "WHERE id=%s AND session_id=%s AND total_score IS NOT NULL FOR UPDATE",
            (expected_id, session_id),
        )
        product = cur.fetchone()
        if product is None:
            raise RuntimeError("diagnosis product incomplete or identity mismatch")
        from services.report_html_renderer import is_client_report_ready
        if not is_client_report_ready(product.get("report_v2_modules_jsonb")):
            raise RuntimeError("diagnosis customer report not ready")
        cur.execute(
            "UPDATE diagnosis_records SET result_visibility='pending', run_token=%s "
            "WHERE id=%s AND session_id=%s "
            "AND (run_token IS NULL OR run_token=%s) "
            "AND (result_visibility IS NULL OR result_visibility='pending')",
            (run_token, expected_id, session_id, run_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("diagnosis product pending stamp conflict")
        cur.execute(
            "UPDATE diagnosis_runs SET final_snapshot_jsonb=%s "
            "WHERE run_token=%s AND session_id=%s AND run_status='running'",
            (json.dumps(frozen_snapshot, ensure_ascii=False), run_token, session_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("diagnosis run product anchor conflict")


def require_complete_product(
    session_id: str,
    diagnosis_id: int,
    run_token: str = None,
) -> dict:
    """Return the exact durable diagnosis product or fail before funds settle."""
    from db.connection import get_db

    try:
        expected_id = int(diagnosis_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("diagnosis artifact identity missing") from exc
    if expected_id <= 0:
        raise RuntimeError("diagnosis artifact identity missing")

    with get_db() as conn:
        cur = conn.cursor()
        if run_token is None:
            cur.execute(
                "SELECT id, brand_id, report_v2_modules_jsonb, ai_total_tests, ai_engines_tested "
                "FROM diagnosis_records "
                "WHERE id=%s AND session_id=%s AND total_score IS NOT NULL",
                (expected_id, session_id),
            )
        else:
            cur.execute(
                "SELECT id, brand_id, report_v2_modules_jsonb, ai_total_tests, ai_engines_tested "
                "FROM diagnosis_records "
                "WHERE id=%s AND session_id=%s AND run_token=%s AND total_score IS NOT NULL",
                (expected_id, session_id, run_token),
            )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("diagnosis product incomplete or identity mismatch")
    from services.report_html_renderer import is_client_report_ready
    if not is_client_report_ready(row.get("report_v2_modules_jsonb")):
        raise RuntimeError("diagnosis customer report not ready")
    # [P0-3 · 2026-08-24] 「有报告」不等于「测出来了」—— 这一道才是资金判定。
    #
    #   上面两道都拦不住"全引擎失败"这一单:total_score 有值(评分链失败会 fallback 0 分),
    #   is_client_report_ready 也一定 True(模块 1 的 insight 在
    #   services/report_writer_v2.py:348 有无条件默认串 "你的 GEO 现状如下。",
    #   结构上不可能为空)。于是四引擎全挂 → 报告满篇「暂无结论」→ 0 分「隐形级」→
    #   照样全额 commit,还把"没测成"呈现成"AI 完全不认识你"。
    #
    #   放在这里而不是放在 workflow 闸门,是因为**这里是唯一的资金收口**:
    #     · server.py:2781 结算前的产物证明走这里;
    #     · services/diagnosis_runs.py:1831 `_do_settlement(intent="commit")` 走这里,
    #       而 sweeper(:2138 退避重试)和收尸后补结算又都汇进 _do_settlement;
    #     · server.py:2803 organization 计费口在调 settle_charge 前也重复调这里。
    #   workflow 那道闸只覆盖 `diagnosis_scope == "geo"`,legacy / full(技术版)/ 老数据
    #   全从旁边流过去 —— 判定写在收口才对所有诊断成立(工单要求"影响所有诊断含 legacy")。
    from services.diagnosis_sample_contract import (
        DiagnosisNotMeasuredError, observed_engine_delivery,
    )
    not_measured = observed_engine_delivery(row)
    if not_measured is not None:
        raise DiagnosisNotMeasuredError(not_measured.message)
    return dict(row)


def heartbeat_run(run_token: str) -> Optional[bool]:
    """任务心跳:CAS 刷 heartbeat_at(仅 running)。返回 True=仍 running / False=已不 running(被收尸/终态)/
    None=DB 抖动。任务据 False 自我中止(§3.4)。"""
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE diagnosis_runs SET heartbeat_at=NOW() WHERE run_token=%s AND run_status='running'",
                (run_token,),
            )
            return cur.rowcount > 0
    except Exception:
        return None


def run_lease_lost(run_token: str) -> bool:
    """[返工2 P1-4] DB fencing 判定:**确定性**读到 run 已非 'running'(被 sweeper 收尸 release_pending / 换态 /
    终态)→ 返 True = lease 丢失,调用方中止落库/外部副作用。asyncio.cancel() 无法中断已进入 to_thread/同步落库的
    旧任务 —— 协程取消 ≠ 旧任务停止;故 workflow 写正式产物前显式 fence,杜绝"钱已 released(withheld)却续写正式诊断行"。
    **DB 抖动/查询异常 → 返 False(fail-open:不中止落库)** —— 资金正确性由下游结算 + result_visibility 闸兜底
    (即便误落库,run 非 running 时 commit CAS 失败 → sweeper release → 可见性 withheld · 不泄露);fail-open 只为避免
    因 fencing 查询瞬时抖动误退一笔**已完成**诊断。查不到 run(None)也返 False(不确定 · 交下游兜底)。"""
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT run_status FROM diagnosis_runs WHERE run_token=%s", (run_token,))
            row = cur.fetchone()
            if row is None:
                return False
            return row["run_status"] != "running"
    except Exception:
        return False


# ============================================================================
# freeze 后落地(R0 分支 → 状态)
# ============================================================================
def _double_table_locate(payer_user_id: int, task_ref: str):
    """§3.0/§3.4.2 双表只读定位(不筛 status · 各带 user 约束)。返回:
    ('legacy', freeze_id, status) / ('v35', ...) / ('ambiguous', None, None) / (None, None, None) 查空 /
    ('error', None, None) 查询异常(不可假定已退款)。

    [对抗审核 P2 #5] 两表各独立事务查(一表失败不污染另一表结果);**区分**表未建(UndefinedTable
    = 确定无该 backend 冻结 · 安全)与真查询异常(statement_timeout/锁等待/schema 漂 → 'error' 不假定无冻)。

    [A-1] 第一个参数是 **payer**,不是 owner。平台承担腿两者不等,拿 owner 查会
    查空两次 → 收尸 ``cancelled_no_freeze``,而冻结**原样挂着零退款**。"""
    from db.connection import get_db

    def _q(table: str, uid_col: str):
        """返回 (row_or_None, ok):ok=False=真查询异常(非"表未建")→ 调用方不可假定无冻结。"""
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT id, status FROM {table} WHERE task_ref=%s AND {uid_col}=%s ORDER BY id DESC LIMIT 1",
                    (task_ref, payer_user_id),
                )
                return cur.fetchone(), True
        except Exception as e:
            try:
                import psycopg2
                if isinstance(e, psycopg2.errors.UndefinedTable):
                    return None, True   # 表未建 = 确定无该 backend 冻结(安全)
            except Exception:
                pass
            # [修复净增量审核 P2] 字符串兜底**仅认"关系/表不存在"**(UndefinedTable 消息 = `relation "X" does not exist`);
            #   绝不用裸 "does not exist" —— 它会误吞 UndefinedColumn(`column "status" does not exist`)等**列级 schema 漂**,
            #   把"真异常(不可假定无冻)"错判成"查空" → 收尸 cancelled_no_freeze 客户已冻结零退款(恰 fix #5 要防的)。
            _msg = str(e).lower()
            if "relation" in _msg and "does not exist" in _msg:
                return None, True       # 表(relation)不存在 = 确定无该 backend 冻结(安全)
            logger.warning(f"[DiagRuns] 定位查询真异常 {table} task_ref={task_ref}: {e}")
            return None, False          # 真异常(含列级 schema 漂)→ 不可假定无冻结 → 'error'
    leg, leg_ok = _q("point_freezes", "user_id")
    v35, v35_ok = _q("customer_credit_freezes", "customer_user_id")
    if not leg_ok or not v35_ok:
        return ("error", None, None)
    if leg and v35:
        return ("ambiguous", None, None)
    if leg:
        return ("legacy", int(leg["id"]), leg["status"])
    if v35:
        return ("v35", int(v35["id"]), v35["status"])
    return (None, None, None)


def activate_after_freeze(run_token: str, owner_user_id: int,
                          classification: FreezeClassification) -> str:
    """freeze 后按 R0 分支落地状态。返回最终 run_status(供调用方决定是否 create_task)。

    [A-1] 公共签名一字未动(60+ 调用点全部位置传参)。这条是**legacy 诊断链**:
    冻结由 ``server.py`` 用发起人自己的 uid 冻的,所以这里 payer **就是** 传进来的
    ``owner_user_id`` —— 下面显式落成 ``payer_user_id`` 局部,让「谁是 payer」
    在每一条冻结定位上都是同一个名字(payer census 机械枚举时不留例外)。
    """
    payer_user_id = owner_user_id
    branch = classification.branch
    task_ref = freeze_task_ref(run_token)

    if branch == "paid":
        # [P0-3b 挂账 A] 只有 paid 分支落拆分快照:另外三处调用点(anomaly 补 backend / sweeper
        #   收尸定位)对应的 run 都是奔着 release_pending 去的**全额退款**,没有"按比例扣"这回事,
        #   给它们塞快照只会让人以为那条路也会部分扣费。
        _persist_freeze_handle(run_token, classification.freeze_id, classification.freeze_backend,
                               classification.split_snapshot)
        if _cas(run_token, ["pending_freeze"], "running"):
            return "running"
        return get_run(run_token)["run_status"] if get_run(run_token) else "running"

    if branch == "exempt":
        # billing_mode 置 exempt(非 admin 零价路径:admit 时是 paid,此处纠正)+ 进 running
        _cas(run_token, ["pending_freeze"], "running", {"billing_mode": "exempt"})
        return "running"

    if branch == "cancelled":
        # [返工 P1-3] 走终态事务:同步 withheld + final_snapshot(不留 NULL 可见性 / 旧成功态回退窗口)
        if not _terminal_local_txn(run_token, ["pending_freeze"], "cancelled", "withheld", _cancel_snapshot())[0]:
            _cas(run_token, ["pending_freeze"], "cancelled",
                 {"reaped_reason": "freeze_deterministic_failure"}, finish=True)
        return "cancelled"

    if branch == "anomaly":
        # 结构异常:有 freeze_id 但 backend 缺 → 双表定位补 backend → release_pending(退款);解析不出 → settlement_manual
        b, fid, _status = _double_table_locate(payer_user_id, task_ref)
        if b in ("legacy", "v35"):
            _persist_freeze_handle(run_token, fid, b)
            _cas(run_token, ["pending_freeze"], "release_pending",
                 {"reaped_reason": "freeze_structural_anomaly"})
            _schedule_retry(run_token)
            return "release_pending"
        _cas(run_token, ["pending_freeze"], "settlement_manual",
             {"last_settlement_error": f"anomaly_backend_unresolved:{b}"})
        _alert_to_manual("anomaly_backend_unresolved", run_token, f"freeze_id in result but backend unresolved(locate={b})")
        return "settlement_manual"

    # branch == 'unknown':立即双表定位(找到即退)· 首次查空绝不 cancelled → 留 pending_freeze
    b, fid, _status = _double_table_locate(payer_user_id, task_ref)
    if b in ("legacy", "v35"):
        _persist_freeze_handle(run_token, fid, b)
        _cas(run_token, ["pending_freeze"], "release_pending", {"reaped_reason": "freeze_unknown_located"})
        _schedule_retry(run_token)
        return "release_pending"
    if b == "ambiguous":
        _cas(run_token, ["pending_freeze"], "settlement_manual", {"last_settlement_error": "unknown_ambiguous_double_frozen"})
        _alert_to_manual("unknown_ambiguous", run_token, "double-table both frozen · manual")
        return "settlement_manual"
    # 查空或查询异常 → 保持 pending_freeze(sweeper 延迟确认 · 禁首次 cancelled)
    return "pending_freeze"


def _schedule_retry(run_token: str, attempts: Optional[int] = None) -> None:
    """设 next_settlement_at 退避(2→5→15min)。attempts 未给时读当前。"""
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        if attempts is None:
            cur.execute("SELECT settlement_attempts FROM diagnosis_runs WHERE run_token=%s", (run_token,))
            row = cur.fetchone()
            attempts = (row["settlement_attempts"] if row else 0) or 0
        delay_min = 2 if attempts <= 0 else (5 if attempts == 1 else 15)
        cur.execute(
            f"UPDATE diagnosis_runs SET next_settlement_at = NOW() + INTERVAL '{int(delay_min)} minutes' "
            "WHERE run_token=%s",
            (run_token,),
        )


# ============================================================================
# 终态本地事务(状态 + 可见性 + final_snapshot 同一事务原子 · §3.6/§3.6a)
# ============================================================================
def _terminal_local_txn(run_token: str, from_statuses, to_status: str,
                        visibility: str, snapshot: Optional[dict]):
    """原子写终态:CAS run_status + final_snapshot_jsonb + UPDATE 产物 result_visibility,单事务一起 commit。
    返回 **(cas_ok, final_status)**:
      · cas_ok=False → CAS 0 行(已被收尸/竞态)· final_status=None · **不碰可见性**(不误发布);
      · cas_ok=True  → final_status = 实际落地终态(通常 = to_status)。
    可见性按 **session_id** 键(run 行的 session_id):覆盖占位与 save_diagnosis 直建两路。

    [返工 P1-2] final_snapshot 富化 diagnosis_id(同事务 SELECT diagnosis_records.id)· 前端跳转/reconciler 回补需要。
    [返工2 P0-2] **published(成功终态)但产物可见性 UPDATE 命中 0 行**(产物被并发删/未落库)→ 同事务**降级
      run_status='delivery_repair_pending'**(钱已 committed/exempt 完成但无产物 = 非成功终态)+ 写 repair 快照 →
      调用方据 final_status != 成功态 **绝不发 complete**。final_snapshot 落 repair(reconciler 回补的是异常而非成功)。"""
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        # ① CAS 状态(暂不写 snapshot · 先拿 session_id)
        cur.execute(
            "UPDATE diagnosis_runs SET run_status=%s, status_changed_at=NOW(), settled_at=NOW(), "
            "finished_at=NOW() WHERE run_token=%s AND run_status = ANY(%s) RETURNING *",
            (to_status, run_token, list(from_statuses)),
        )
        row = cur.fetchone()
        if row is None:
            return (False, None)
        sid = row["session_id"]
        # ② 只认结算前耐久冻结的 diagnosis_id；禁止按 session 重新挑最新行。
        try:
            snap = _merge_terminal_snapshot(row.get("final_snapshot_jsonb"), snapshot)
        except (TypeError, ValueError, json.JSONDecodeError):
            if visibility == "published":
                raise
            # A refund/release must still reach its durable terminal state when
            # a legacy snapshot is malformed.  Never invent an artifact id.
            snap = dict(snapshot or {})
            snap["snapshot_integrity_error"] = True
        try:
            expected_prod_id = int((snap or {}).get("diagnosis_id"))
        except (TypeError, ValueError):
            expected_prod_id = None
        prod_id = None
        prod_brand_id = None
        if expected_prod_id is not None and expected_prod_id > 0:
            cur.execute(
                "SELECT id, brand_id, report_v2_modules_jsonb FROM diagnosis_records "
                "WHERE id=%s AND session_id=%s FOR UPDATE",
                (expected_prod_id, sid),
            )
            prow = cur.fetchone()
            if prow is not None:
                if visibility != "published":
                    prod_id = prow["id"]
                    prod_brand_id = prow["brand_id"]
                else:
                    from services.report_html_renderer import is_client_report_ready
                    if is_client_report_ready(prow.get("report_v2_modules_jsonb")):
                        prod_id = prow["id"]
                        prod_brand_id = prow["brand_id"]
        # ③ 产物可见性(同事务原子 · §3.6 P0-3)
        if visibility == "published" and prod_id is not None:
            cur.execute(
                "UPDATE diagnosis_records SET result_visibility=%s WHERE id=%s",
                (visibility, prod_id),
            )
            vis_rows = cur.rowcount
        elif visibility == "published":
            vis_rows = 0
        elif prod_id is not None:
            cur.execute(
                "UPDATE diagnosis_records SET result_visibility=%s WHERE id=%s",
                (visibility, prod_id),
            )
            vis_rows = cur.rowcount
        elif expected_prod_id is not None:
            # The anchored row was removed.  Never let a later row that merely
            # reused the session id inherit this run's refund/withhold state.
            vis_rows = 0
        else:
            cur.execute(
                "UPDATE diagnosis_records SET result_visibility=%s WHERE session_id=%s",
                (visibility, sid),
            )
            vis_rows = cur.rowcount
        # [返工2 P1-2] withheld(退款/失败/中断)且该诊断正是 brand 冗余列 latest_diagnosis_id 指向者 →
        #   同事务把 brands.latest_score/latest_diagnosis_id 回退到最近 **published** 诊断(无则清空)。
        #   否则 m3 _BRAND_FIELDS / client-context-list 读的 b.latest_score 会展示已退款诊断的分数(冗余列泄露)。
        #   仅当 latest_diagnosis_id 正指向本诊断才动(不误改指向别的诊断的 brand)· 幂等 · 同事务原子。
        if visibility == "withheld" and prod_id is not None and prod_brand_id is not None:
            # [返工2 修复净增量 P2] **SAVEPOINT 隔离**:brands 回退是装饰性冗余列维护(所有客户读取点已改 published-only
            #   子查询·不依赖此列)· 但它与终态 CAS/可见性/final_snapshot 同一事务,若 UPDATE 抛错(死锁/锁等待)会毒化
            #   事务 → 紧随的 final_snapshot 写抛 InFailedSqlTransaction → get_db 回滚**整个终态事务**(已退款却卡 pending)。
            #   SAVEPOINT + ROLLBACK TO SAVEPOINT 让它真正非阻塞(同 agent_rebate/customer_credit 惯例)。
            try:
                cur.execute("SAVEPOINT sp_brands_revert")
                cur.execute(
                    "UPDATE brands b SET "
                    "  latest_diagnosis_id = sub.id, latest_score = sub.total_score, updated_at = NOW() "
                    "FROM (SELECT %s::int AS brand_id, "
                    "        (SELECT d.id FROM diagnosis_records d WHERE d.brand_id=%s "
                    "          AND (d.result_visibility IS NULL OR d.result_visibility='published') "
                    "          ORDER BY d.created_at DESC LIMIT 1) AS id, "
                    "        (SELECT d.total_score FROM diagnosis_records d WHERE d.brand_id=%s "
                    "          AND (d.result_visibility IS NULL OR d.result_visibility='published') "
                    "          ORDER BY d.created_at DESC LIMIT 1) AS total_score) sub "
                    "WHERE b.id = sub.brand_id AND b.latest_diagnosis_id = %s",
                    (prod_brand_id, prod_brand_id, prod_brand_id, prod_id),
                )
                cur.execute("RELEASE SAVEPOINT sp_brands_revert")
            except Exception as _bre:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_brands_revert")  # 只回滚 brands 回退 · 终态写不受累
                except Exception:
                    pass
                logger.warning(f"[DiagRuns] withheld 后 brands.latest_score 冗余列回退失败(已 SAVEPOINT 隔离 · 非阻塞): {_bre}")
        final_status = to_status
        # [返工2 P0-2] published 但 0 产物行 → 钱已结算却无产物可发布 → **降级非成功终态**(同事务原子)。
        #   条件用 vis_rows==0(UPDATE 真实命中数),不叠 prod_id(SELECT 早于 UPDATE · 叠加反漏"SELECT 后删"竞态)。
        if visibility == "published" and vis_rows == 0:
            repair_snap = _delivery_repair_snapshot()
            if expected_prod_id is not None:
                repair_snap["diagnosis_id"] = expected_prod_id
            snap = repair_snap
            cur.execute(
                "UPDATE diagnosis_runs SET run_status='delivery_repair_pending', status_changed_at=NOW() "
                "WHERE run_token=%s",
                (run_token,),
            )
            final_status = "delivery_repair_pending"
        cur.execute(
            "UPDATE diagnosis_runs SET final_snapshot_jsonb=%s WHERE run_token=%s",
            (json.dumps(snap, ensure_ascii=False) if snap is not None else None, run_token),
        )
        terminal_run = dict(row)
        terminal_run["run_status"] = final_status
        _enqueue_diagnosis_run_terminal(cur, terminal_run, final_status)
    if final_status == "delivery_repair_pending":
        _detail = (f"终态 {to_status} 但可见性命中 0 产物行(session={sid} 可能被并发删)· 钱已结算无产物 · "
                   f"已降级 delivery_repair_pending 待人工补发/退款")
        _alert("committed_no_product", run_token, _detail)
        _write_settlement_audit(run_token, "system", "delivery_repair", _detail)  # [返工2 item8] 持久留痕
    return (True, final_status)


def _snapshot_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("diagnosis snapshot must be a JSON object")
        return dict(parsed)
    if value is None:
        return {}
    raise ValueError("diagnosis snapshot has unsupported type")


def _merge_terminal_snapshot(persisted: object, incoming: Optional[dict]) -> dict:
    """Preserve the durable product identity across commit/release snapshots."""
    persisted_snap = _snapshot_dict(persisted)
    incoming_snap = dict(incoming or {})
    persisted_id = persisted_snap.get("diagnosis_id")
    incoming_id = incoming_snap.get("diagnosis_id")
    if persisted_id is not None and incoming_id is not None and int(persisted_id) != int(incoming_id):
        raise RuntimeError("diagnosis terminal snapshot identity conflict")
    merged = dict(persisted_snap)
    merged.update(incoming_snap)
    if persisted_id is not None:
        merged["diagnosis_id"] = persisted_id
    return merged


# ============================================================================
# 结算(R1/R2/R3 · 四参强制 · commit/release 共用)· 供 task 成功/失败路径 + sweeper 重试
# ============================================================================
def _frozen_total_for_run(run) -> Optional[int]:
    """读本 run 冻结总额(定位不到返回 None → 调用方转人工,绝不猜金额)。

    [P0-3 · 2026-08-24] 补 `from db.connection import get_db`。本模块**没有**模块级
    导入 get_db(其余每个函数都在函数体里各导各的),而本函数原来直接用了裸 `get_db` ——
    于是每次调用都是 NameError,又被下面那个 `except Exception: return None` 原样吞掉,
    **恒返 None**。后果:`_partial_commit_points` 永远拿不到冻结额 → 永远返
    `frozen_amount_unreadable` → 每一单降级交付都被推去 settlement_manual,
    "按已履约比例部分扣费"从来没有真的执行过一次。
    (这类 bug 不会有任何判据变红 —— 兜底 except 把它变成了一条静默的正常分支。)
    """
    from db.connection import get_db
    fid, tref = run.get("freeze_id"), run.get("freeze_task_ref")
    payer = settlement_payer_user_id(run)          # [A-1] 平台承担腿的冻结不在 owner 名下
    table = "point_freezes" if str(run.get("freeze_backend")) == "legacy" else None
    if not fid or not payer or not table:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT amount_total FROM {table} WHERE id=%s AND user_id=%s AND task_ref=%s",
                (int(fid), int(payer), tref),
            )
            row = cur.fetchone()
        return int(row["amount_total"] or 0) if row else None
    except Exception:
        return None


def _split_snapshot_dict(value):
    """把 reserved_split_snapshot_jsonb 还原成 dict;认不出来一律 None(= 没有快照,回落既有逻辑)。

    绝不抛:这条路径在结算里,解析失败必须退化成"没快照",而不是把一次结算炸成异常重试。
    """
    if isinstance(value, dict):
        return value or None
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) and parsed else None
    return None


def _reserved_split_for_run(run):
    """[P0-3 · 2026-08-24] 取本 run 冻结的**物理三池拆分**,交 commit_freeze 做部分扣费。

    为什么非它不可:`middleware/billing.py:1611` 里 `_actual_points` 分支第一行就是
    `if not _reserved_split: raise RuntimeError("organization settle requires immutable
    reserved split")` —— 也就是说**不带 split 的部分扣费根本调不通**。本仓此前唯一的
    `_actual_points` 调用方是 organization 那条链(:1962),它在预留时就把
    `physical_split_snapshot` 存进了 charge link;诊断链没有存过,所以只能在结算时回读
    冻结行(`commit` 只动 frozen_points、三池金额不动,故冻结行的拆分与预留时同值)。

    返回 (split, error):
      · (split, None) —— 拆分**可证**(只有一个池出钱 → 无论 order 怎么排,
        `_actual_and_release_split` 走出来的结果都一样,不存在猜的成分);
      · (None, error) —— **两个及以上池出钱**:此时 order(先扣哪个池、余额退回哪个池)
        决定客户的钱从哪个池扣、退到哪个池,而 order 是冻结当时的钱包偏好,事后无从得知。
        猜 = 拿客户的赠送/佣金/现金池互换,属于资金语义,**宁可转人工也不猜**
        (与本函数上游 `_partial_commit_points` 一贯的"既不多收也不少收"同一口径)。
        → 要让多池冻结也能自动按比例扣,得在 freeze 时把 `physical_split_snapshot`
          落进 diagnosis_runs(加一列),那是 Owner 的口径决定,已在交付说明里单列。
    """
    from db.connection import get_db      # 本模块无模块级 get_db(见 _frozen_total_for_run 的说明)
    fid, tref = run.get("freeze_id"), run.get("freeze_task_ref")
    payer = settlement_payer_user_id(run)          # [A-1] 同上:按 payer 定位冻结行
    if not fid or not payer or str(run.get("freeze_backend")) != "legacy":
        return None, "reserved_split_backend_unsupported"
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT amount_total, amount_bonus, amount_commission, amount_paid "
                "FROM point_freezes WHERE id=%s AND user_id=%s AND task_ref=%s",
                (int(fid), int(payer), tref),
            )
            row = cur.fetchone()
    except Exception as e:
        return None, f"reserved_split_unreadable:{str(e)[:80]}"
    if row is None:
        return None, "reserved_split_freeze_missing"
    pools = {name: int(row.get(f"amount_{name}") or 0) for name in ("bonus", "commission", "paid")}
    total = int(row.get("amount_total") or 0)
    if sum(pools.values()) != total or total <= 0:
        # 三池之和对不上冻结总额 → 拆分不可信,禁据此动钱。
        return None, "reserved_split_sum_mismatch"

    # [P0-3b 挂账 A] 冻结当时留了权威快照 → 用它,多池也能自动按比例(order 是真值,零猜)。
    snap = _split_snapshot_dict(run.get("reserved_split_snapshot_jsonb"))
    if snap is not None:
        snap_pools = {name: int(snap.get(name) or 0) for name in ("bonus", "commission", "paid")}
        order = [str(x) for x in (snap.get("order") or [])]
        # order 必须是 billing 认的两种形态之一(middleware/billing.py:581 `_actual_and_release_split`
        # 会拒别的);这里先自检,免得把一个 billing 必 raise 的 split 送进资金原语。
        if sorted(order) not in (["bonus", "commission", "paid"], ["commission", "paid"]):
            return None, "reserved_split_order_invalid"
        if snap_pools != pools:
            # 快照与冻结行对不上(列被改过 / 冻结行被动过)→ 不可信,与和不符同一口径:转人工,不猜。
            return None, "reserved_split_sum_mismatch"
        return {**snap_pools, "order": order}, None

    # 存量 run(冻结时还没这一列)→ 保持 P0-3 行为:单池可证则自动,多池转人工。
    funded = [name for name, value in pools.items() if value > 0]
    if len(funded) != 1:
        return None, "reserved_split_order_unknown"
    return {**pools, "order": ["bonus", "commission", "paid"]}, None


def _identity_suspected(snapshot) -> bool:
    """快照里是否标了「疑似品牌识别失败」（P0-1 ②）。

    只认显式 ``suspected is True``；读不到/结构不对一律 False —— 不靠猜测改动资金。
    """
    if not isinstance(snapshot, dict):
        return False
    for container in (
        snapshot,
        snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {},
    ):
        ai = container.get("ai_visibility") if isinstance(container, dict) else None
        if isinstance(ai, dict):
            suspicion = ai.get("identity_suspicion")
            if isinstance(suspicion, dict) and suspicion.get("suspected") is True:
                return True
        suspicion = container.get("identity_suspicion") if isinstance(container, dict) else None
        if isinstance(suspicion, dict) and suspicion.get("suspected") is True:
            return True
    return False


def _verdict_integer_counts(verdict):
    """[E2-1] 从交付判定里取**整数**计数 ``(planned, succeeded)``。取不到返回 ``None``。

    取不到 = 旧快照(那时只落了 ratio)或数据被改过。调用方对 ``None`` 的处置是
    **转人工**,不是回落 float —— 四位小数已经把信息丢掉了,反推不回真分数。

    校验是 fail-closed 的:两个键都得在、都得是整数、``planned > 0``、
    且 ``0 < succeeded < planned``。最后一条是 degraded 这一档的定义
    (等于 planned 是 sufficient、等于 0 是 insufficient,两者都不该走到分账);
    数据不满足它 ⇒ 快照与 outcome 自相矛盾 ⇒ 宁可转人工也不猜。
    """
    if not isinstance(verdict, dict):
        return None
    raw_planned, raw_succeeded = verdict.get("planned"), verdict.get("succeeded")
    if raw_planned is None or raw_succeeded is None:
        return None
    # 🔴 只收**真 int**。bool 是 int 的子类(True/False 当计数是脏数据);
    #    "10" / 10.0 这种能被 int() 转成功、但**生产不会发**的形状也拒掉 ——
    #    JSONB 回来的整数恒是 int，出现别的类型只能是被改过，
    #    收下它就等于允许一个"看起来像数"的脏值去算钱。
    if not isinstance(raw_planned, int) or not isinstance(raw_succeeded, int):
        return None
    if isinstance(raw_planned, bool) or isinstance(raw_succeeded, bool):
        return None
    planned, succeeded = int(raw_planned), int(raw_succeeded)
    if planned <= 0 or not (0 < succeeded < planned):
        return None
    return planned, succeeded


def _partial_commit_points(run, snapshot, *, reserved_total_provider=None):
    """按交付判定算应结算点数。**个人腿与组织腿共用的唯一一份**(A-3)。

    返回 (actual_points 或 None, error 或 None):
      - 无判定 / 判定为 sufficient → (None, None):走原全额 commit,行为不变;
      - degraded 且能读到预留总额 → (部分点数, None);
      - 判定版本不符 / 比例越界 / 读不到预留总额 → (None, error):调用方转人工(不多收不少收)。

    [A-3 · Codex P0-3 · 2026-08-25] ``reserved_total_provider``
    ------------------------------------------------------------
    组织腿的「预留总额」不是 legacy 冻结行的 ``amount_total``,而是 charge link 的
    ``reserved_ceiling_points``。除此之外**两条腿的判定逻辑逐条相同**
    (疑似身份 → 转人工;无判定/sufficient → 全额;degraded → 版本+比例校验 → 按比例)。

    所以这里只把「总额从哪来」抽成一个 provider,**不复制第二份实现** ——
    Codex 原文要求「组织腿与个人腿共用同一个 SSOT,禁复制一份第二实现」,
    而本仓的教训是「同一谓词写两处必有一处没人验」。

    🔴 provider 是**惰性**的(callable 而不是直接传值):个人腿在 sufficient /
       无判定时**从来不读冻结行**,直接传值会凭空多一次 SELECT ——
       那是可观察的行为变化,而本次改动对个人腿的要求是行为零变化。
    """
    from services.diagnosis_sample_contract import (
        OUTCOME_DEGRADED, OUTCOME_SUFFICIENT, SAMPLE_CONTRACT_VERSION,
    )
    snap = snapshot if isinstance(snapshot, dict) else {}
    durable = _snapshot_dict(run.get("final_snapshot_jsonb")) or {}

    # [P0-1 ② · 2026-07-26] 疑似品牌识别失败的这一次，**不自动全额扣费**。
    #
    #   生产实证 brand 278（名字带换行 + "城市:" 标签）：诊断 456/468 全 0 分，
    #   客户付费两次拿到废报告 —— 那是我们的识别没对上，不是客户的业务结论。
    #
    #   这里不自造资金路径：复用既有「转人工结算」原语
    #   （settlement_manual + _alert_to_manual），由有权限的人决定退/不退
    #   （退款走 admin 工单制）。既不自动多收，也不自动退款。
    if _identity_suspected(snap) or _identity_suspected(durable):
        return None, IDENTITY_REVIEW_REASON

    verdict = snap.get("delivery_verdict")
    if not isinstance(verdict, dict):
        verdict = durable.get("delivery_verdict")
    if not isinstance(verdict, dict):
        return None, None                      # 无判定 → 全额(向后兼容,行为不变)
    outcome = str(verdict.get("outcome") or "")
    if outcome == OUTCOME_SUFFICIENT:
        return None, None                      # 全履约 → 全额
    if outcome != OUTCOME_DEGRADED:
        return None, f"unexpected_delivery_outcome:{outcome[:40]}"
    if str(verdict.get("version") or "") != SAMPLE_CONTRACT_VERSION:
        return None, "delivery_verdict_version_mismatch"
    try:
        ratio = float(verdict.get("billable_ratio"))
    except (TypeError, ValueError):
        return None, "delivery_verdict_ratio_invalid"
    if not (0.0 < ratio < 1.0):
        return None, "delivery_verdict_ratio_out_of_range"
    # [E2-1 · Codex 二审 P1-F1 · 2026-08-26] 分账一律走**整数**,ratio 不参与算钱。
    #
    # 修的是什么:`billable_ratio` 落库时被 `round(_v.billable_ratio, 4)` 截到四位小数
    # (workflows/diagnosis_workflow.py 两处持久化点),结算再 `int(total * ratio)`。
    # 亲手验算过 Codex 那一例:planned=32 succeeded=11 total=20800
    #   → 原始 11/32 = 0.34375 → 持久化 0.3438 → int(20800*0.3438) = 7151
    #   → 整数精确 (20800*11)//32 = 7150   ⇒ **多收 1**。
    # 我自己枚举 p≤64 × s<p × 三个总额(6048 组)复核:1545 组不精确(25.5%),
    # 且**两个方向都有** —— 多收 678 组、少收 867 组。所以这不是"偏保守"或"偏激进",
    # 是"这个数就是错的",与本函数一贯的「既不多收也不少收」口径直接冲突。
    #
    # 为什么能改成整数:`planned` / `succeeded` **本来就在快照里**
    # (两处持久化点都写了这两个键),此前只是没人用。
    #
    # 🔴 旧快照只有 ratio、没有整数计数 ⇒ **转人工**,不许拿 float 回猜。
    #    回猜等于把这个 P1 换个地方原样保留:四位小数已经把信息丢了,
    #    从 0.3438 反推不出 11/32(0.3437.. ~ 0.3438.. 之间有无穷多个真分数)。
    counts = _verdict_integer_counts(verdict)
    if counts is None:
        return None, "delivery_verdict_counts_missing"
    planned, succeeded = counts

    total = (reserved_total_provider or (lambda: _frozen_total_for_run(run)))()
    if total is None:
        return None, "frozen_amount_unreadable"
    if total <= 0:
        return None, None                      # 没预留额可分 → 交原路径

    # ratio 只留作**交叉校验**(展示口径与整数口径不许差太远;差太远说明快照被改过)。
    # 容差按「四位小数截断本身能造成的最大偏移」给,不是拍脑袋:
    # 截断误差 < 1e-4,乘以 total 后 < total * 1e-4,再加上两边各自的取整各 1。
    exact = (total * succeeded) // planned
    drift = abs(exact - int(total * ratio))
    if drift > int(total * 1e-4) + 2:
        return None, "delivery_verdict_ratio_counts_disagree"

    actual = max(1, min(total, exact))
    return actual, None


async def _do_settlement(run_token: str, intent: str, snapshot: Optional[dict]) -> dict:
    """执行一次结算(intent='commit'|'release')。要求 run 已在对应 *_pending 态。
    R1 success→终态本地事务;R2 ambiguous→settlement_manual;R3 失败→退避重试(commit attempts≥5→manual)。"""
    run = get_run(run_token)
    if run is None:
        return {"ok": False, "reason": "run_not_found"}
    pending_state = "commit_pending" if intent == "commit" else "release_pending"
    if run["run_status"] != pending_state:
        return {"ok": False, "reason": f"not_in_{pending_state}", "actual": run["run_status"]}
    if run.get("freeze_id") is None or run.get("freeze_backend") is None:
        # FreezeHandle 不全 → 禁调 billing(四参构造不出)→ settlement_manual
        _cas(run_token, [pending_state], "settlement_manual", {"last_settlement_error": "incomplete_freeze_handle"})
        _alert_to_manual("incomplete_handle", run_token, f"missing freeze_id/backend at settlement intent={intent}")
        return {"ok": False, "terminal": "settlement_manual"}

    if intent == "commit":
        try:
            durable_snapshot = _snapshot_dict(run.get("final_snapshot_jsonb"))
            require_complete_product(
                str(run.get("session_id") or ""),
                durable_snapshot.get("diagnosis_id"),
                run_token,
            )
        except (RuntimeError, TypeError, ValueError, json.JSONDecodeError) as integrity_error:
            from services.diagnosis_sample_contract import DiagnosisNotMeasuredError
            not_measured = isinstance(integrity_error, DiagnosisNotMeasuredError)
            reason = (
                f"commit_not_measured:{str(integrity_error)[:220]}" if not_measured
                else f"commit_product_invalid:{str(integrity_error)[:220]}"
            )
            if not _cas(
                run_token,
                ["commit_pending"],
                "release_pending",
                {"last_settlement_error": reason},
            ):
                return {"ok": False, "reason": "commit_product_invalid_cas_missed"}
            failed_snapshot = dict(snapshot or {})
            # [P0-3b · R-b] 两个分支都必须盖掉成功快照里的四个键。
            #   `commit_product_invalid`(产物证明不过关的**其它**原因:身份错配 / 报告没 ready)
            #   同样是"钱退了",可它原来只 update 了 `_failed_snapshot()` 的那几个键 ——
            #   `result{total_score,level}` / `share_token` 从 persisted 成功快照里**原样活到终态**
            #   (`_merge_terminal_snapshot` 只有覆盖没有删除),再随 server.py 的
            #   `return {"found": True, **_snap}` 漏给前端。和 not_measured 一个病,只是原因不同。
            failed_snapshot.update(
                _not_measured_snapshot() if not_measured
                else _refunded_snapshot("诊断未完成,费用已退,请重试")
            )
            return await _do_settlement(run_token, "release", failed_snapshot)

    from middleware.billing import commit_freeze, release_freeze
    target = "committed" if intent == "commit" else "released"
    visibility = "published" if intent == "commit" else "withheld"
    # [C组①·V10/§12.1] 降级交付只收已履约部分:从**耐久 run 快照**取交付判定,
    # 算出 _actual_points 交 commit_freeze 做"部分扣费 + 余额释放"。
    # 判定不合法(版本不符/比例越界/读不到冻结额)→ 既不多收也不少收 → 转人工,禁猜。
    _actual = None
    _actual_split = None
    if intent == "commit":
        try:
            _actual, _partial_err = _partial_commit_points(run, snapshot)
        except Exception as _pe:
            _actual, _partial_err = None, f"partial_calc_exception:{str(_pe)[:80]}"
        if not _partial_err and _actual is not None:
            # [P0-3] 部分扣费必须带物理拆分,否则 commit_freeze 直接 raise(见 _reserved_split_for_run)。
            # 拆分不可证 → 与上面同一处置:转人工,不猜。
            try:
                _actual_split, _split_err = _reserved_split_for_run(run)
            except Exception as _se:
                _actual_split, _split_err = None, f"reserved_split_exception:{str(_se)[:80]}"
            if _split_err:
                _partial_err = _split_err
        if _partial_err:
            _cas(run_token, [pending_state], "settlement_manual",
                 {"last_settlement_error": _partial_err})
            _alert_to_manual("partial_settlement_undecidable", run_token, _partial_err)
            return {"ok": False, "terminal": "settlement_manual", "reason": _partial_err}
    try:
        if intent == "commit":
            r = await commit_freeze(
                freeze_id=run["freeze_id"], task_ref=run["freeze_task_ref"],
                user_id=settlement_payer_user_id(run), freeze_table=run["freeze_backend"],
                reason=(f"诊断完成 run={run_token}" if _actual is None
                        else f"诊断降级交付(只收已履约)run={run_token}"),
                **({} if _actual is None else {
                    "_actual_points": int(_actual),
                    "_reserved_split": _actual_split,
                }),
            )
        else:
            r = await release_freeze(
                freeze_id=run["freeze_id"], task_ref=run["freeze_task_ref"],
                user_id=settlement_payer_user_id(run), freeze_table=run["freeze_backend"],
                reason=f"诊断失败/中断退款 run={run_token}",
            )
    except Exception as e:
        _bump_attempts_retry(run_token, intent, str(e))
        return {"ok": False, "retry": True, "error": str(e)[:200]}

    # R2 ambiguous → 转人工(不重试不改道)
    if r.get("ambiguous"):
        _cas(run_token, [pending_state], "settlement_manual", {"last_settlement_error": "ambiguous_cross_table"})
        _alert_to_manual("ambiguous", run_token, f"billing ambiguous intent={intent}")
        return {"ok": False, "terminal": "settlement_manual"}

    if r.get("success") is True:
        # 幂等冲突守卫:billing 幂等返回实际 status,与本意图冲突(想 commit 但已 released,或反之)→ 转人工
        if r.get("idempotent"):
            idem_status = r.get("status")
            if (intent == "commit" and idem_status == "released") or \
               (intent == "release" and idem_status == "committed"):
                _cas(run_token, [pending_state], "settlement_manual",
                     {"last_settlement_error": f"idempotent_conflict:{idem_status}"})
                _alert_to_manual("idempotent_conflict", run_token, f"intent={intent} but freeze already {idem_status}")
                return {"ok": False, "terminal": "settlement_manual"}
        # R1 成功:终态 + 可见性 + snapshot 同一本地事务
        ok, final = _terminal_local_txn(run_token, [pending_state], target, visibility, snapshot)
        if not ok:
            # CAS 0 行:被并发收尸/竞态 → 交对方/下轮
            return {"ok": False, "reason": "terminal_cas_missed"}
        if final == "delivery_repair_pending":
            # [返工2 P0-2] commit 结算成功但 0 产物 → 非成功终态 · 调用方禁发 complete(发异常/待人工补)
            return {"ok": False, "terminal": "delivery_repair_pending"}
        return {"ok": True, "terminal": target}

    # R3:success False/超时 → 退避重试;commit attempts≥5 → settlement_manual
    return _bump_attempts_retry(run_token, intent, r.get("reason") or "settlement_not_success")


def _bump_attempts_retry(run_token: str, intent: str, err: str) -> dict:
    """settlement_attempts+1 + 退避;commit 达上限转 manual;release 不设上限(告警限频)。"""
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE diagnosis_runs SET settlement_attempts = settlement_attempts + 1, "
            "last_settlement_error=%s WHERE run_token=%s RETURNING settlement_attempts",
            (str(err)[:480], run_token),
        )
        row = cur.fetchone()
        attempts = (row["settlement_attempts"] if row else 0) or 0
    if intent == "commit" and attempts >= _SETTLE_MAX_ATTEMPTS:
        _cas(run_token, ["commit_pending"], "settlement_manual",
             {"last_settlement_error": f"commit_attempts>={_SETTLE_MAX_ATTEMPTS}:{str(err)[:200]}"})
        _alert_to_manual("commit_max_attempts", run_token, f"commit failed {attempts}x → manual")
        return {"ok": False, "terminal": "settlement_manual"}
    # [对抗审核 P2 #11/12] release 不设上限但必须限频告警(SPEC R3 "release 告警限频每小时一条"):
    #   否则 release_freeze 长期 success=False(如"未找到冻结记录")→ frozen_points 静默悬挂,运营零信号。
    if intent == "release" and attempts >= 2:
        _alert_ratelimited("release_retry_stuck", run_token,
                           f"release 已重试 {attempts}x 仍未成功: {str(err)[:200]}", ttl=3600)
    _schedule_retry(run_token, attempts)
    return {"ok": False, "retry": True, "attempts": attempts}


async def commit_run(run_token: str, snapshot: Optional[dict] = None) -> dict:
    """成功路径结算(task 调):exempt→completed_exempt(无 billing);paid→CAS running→commit_pending→结算。"""
    run = get_run(run_token)
    if run is None:
        return {"ok": False, "reason": "run_not_found"}
    if run["billing_mode"] == "exempt":
        ok, final = _terminal_local_txn(run_token, ["running"], "completed_exempt", "published", snapshot)
        if not ok:
            return {"ok": False, "reason": "terminal_cas_missed"}
        if final == "delivery_repair_pending":
            # [返工2 P0-2] exempt 完成但 0 产物 → 非成功终态 · 禁发 complete
            return {"ok": False, "terminal": "delivery_repair_pending"}
        return {"ok": True, "terminal": "completed_exempt"}
    # paid:CAS running→commit_pending(0 行 = 已被收尸/换态 → 放弃 commit,交 sweeper)
    if not _cas(run_token, ["running"], "commit_pending"):
        return {"ok": False, "reason": "reaped_or_not_running", "actual": (get_run(run_token) or {}).get("run_status")}
    return await _do_settlement(run_token, "commit", snapshot)


def mark_session_visibility_pending(session_id: str) -> None:
    """[返工3 P1] 失败/结算中的诊断:把该 session 产物可见性置 'pending'(结算中)· 让状态端点轮询回退显示
    done:false"结算处理中"而非 NULL-score 假完成(付费失败路径**保留占位行做刷新追踪** · 必须同步压 pending;
    否则占位行 result_visibility NULL + total_score NULL → 状态端点回退成"complete"假完成)。幂等 · best-effort。
    只压未终态行(NULL/pending)· 不覆盖已 published/withheld · 终态 release 时 _terminal_local_txn 覆盖为 withheld。"""
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE diagnosis_records SET result_visibility='pending' "
                "WHERE session_id=%s AND (result_visibility IS NULL OR result_visibility='pending')",
                (session_id,),
            )
    except Exception as e:
        logger.warning(f"[DiagRuns] mark session pending 失败 session={session_id}: {e}")


async def release_run(run_token: str, reason: str, snapshot: Optional[dict] = None) -> dict:
    """失败路径结算(task 调):exempt→failed_exempt(无 billing · withheld);paid→CAS running→release_pending→结算。"""
    run = get_run(run_token)
    if run is None:
        return {"ok": False, "reason": "run_not_found"}
    if run["billing_mode"] == "exempt":
        ok, _ = _terminal_local_txn(run_token, ["running"], "failed_exempt", "withheld", snapshot)
        return {"ok": ok, "terminal": "failed_exempt"}
    # paid:CAS running→release_pending
    if _cas(run_token, ["running"], "release_pending", {"reaped_reason": (reason or "task_failed")[:200]}):
        return await _do_settlement(run_token, "release", snapshot)
    # 未在 running(可能已被 sweeper 收尸成 release_pending)→ 若已 release_pending 继续结算
    cur_run = get_run(run_token)
    if cur_run and cur_run["run_status"] == "release_pending":
        return await _do_settlement(run_token, "release", snapshot)
    return {"ok": False, "reason": "not_running", "actual": cur_run["run_status"] if cur_run else None}


async def dispatch_success_settlement(
    *,
    run_token: str,
    session_id: str,
    diagnosis_id: int,
    snapshot: Optional[dict],
    organization_charge_id: Optional[int],
    organization_charge_points: Optional[int],
    organization_claim_token: Optional[str],
    brand_id: Optional[int],
) -> dict:
    """[P0-3c 件2] 成功路径的**结算分派**:org 走 charge link,非 org 走 legacy 冻结。

    ## 为什么把它从 server.py 搬出来

    这一段原来长在 `run_diagnosis_task._run_body` 里,是个**只有静态锁看得见**的分支:
    判据目录没有任何一条在运行时执行过 server.py,所以"方向反了会怎样"这类问题
    一条判据都答不了 —— 同族守卫反转后 92 条判据全绿(P0-3b 交回项 A6b,
    以及 Review 复审又挖出的 2740/2970)。

    搬到这里,`org / 非 org` 两条臂就能被**真跑**:真 `settle_charge`、真 `commit_run`、
    真终态落库。剩下那层 `if` 不再靠"有没有人记得给它写把 AST 锁"活着。

    ⚠️ 搬家不是免费的:P0-3b 的教训正是"抽函数让逻辑可判了,**调用点反而没人管**"
    (`_dr` 未绑定,上线每单必挂)。所以本包同时给调用点配了**执行毒**判据 ——
    把符号改成未定义名,运行时判据必须红。

    行为与搬家前逐字一致(`tests/p03c_org_guards_2026_08_25/_equivalence_baseline.json`
    是重构**之前**跑出来的可观察终态,重构后逐字比对)。

    ## [A-3 · 2026-08-25] org 臂不再恒按预留上限结算

    上面那句「行为逐字一致」到本次为止仍然成立于**全履约**的单
    (无 `delivery_verdict` / verdict=sufficient → 仍按 ceiling,等价基线照样绿)。
    **变的是降级与疑点两档**:org 臂开始消费 `delivery_verdict` /
    `identity_suspicion`,与个人腿共用同一个 `_partial_commit_points`。
    这正是 Codex P0-3 点名的缺口 —— 详见下面 org 分支里的整段说明。
    """
    if organization_charge_id is not None:
        # Organization billing has its own settlement primitive, so repeat the
        # same exact product proof at that money boundary instead of relying
        # only on the earlier stamp.
        #
        # [P0-3c 件3] 与**上游那次** `require_complete_product` 的关系
        #   (上游 = `server.py::run_diagnosis_task` 里 `mark_product_pending` **之前**
        #    的那次 `_dr.require_complete_product`;这里刻意不写行号 —— 行号会漂,
        #    本包的 census 就是为了不再靠行号定位):
        #
        #   零成功观测的单在**上游那次**就抛 `DiagnosisNotMeasuredError` 走掉了,
        #   **根本到不了这里** —— 那种单由外层 `except` 的 org release 分支退款。
        #   所以这里这一次不是重复劳动:它把"产物证明"钉在**动钱那一刻**,
        #   防的是上游那次到此处之间产物被并发删/改(钱和产物之间的那个窗口)。
        #
        #   顺序保证由 `tests/test_diagnosis_delivery_integrity_2026_07_22.py::
        #   test_server_checks_durable_product_before_any_settlement` 守
        #   (P0-3c 配了一发重排变异 M1:把这一行挪到 settle 之后 → 那条判据必红)。
        await asyncio.to_thread(require_complete_product, session_id, diagnosis_id, run_token)

        # ── [A-3 · Codex P0-3] 组织腿也按**真实履约**算 actual_points ──────────
        #
        # 修之前:这里恒传 `reserved_ceiling_points`。诊断结果里明明带着
        # `delivery_verdict`(降级交付比例)和 `identity_suspicion`(疑似认错品牌),
        # 组织分支**一个都不消费** —— 只有 25% 平台测成、或者根本没认出这个品牌,
        # 预留 650 照样实扣 650。个人钱包腿早就有部分履约扣费,组织腿没有。
        #
        # 修法是**共用同一个谓词**(`_partial_commit_points`),只把「预留总额从哪来」
        # 换成 charge link 的 ceiling —— 不复制第二份实现(复制必有一份没人验)。
        _org_run = get_run(run_token) or {}
        _org_ceiling = int(organization_charge_points or 0)
        try:
            _org_actual, _org_err = _partial_commit_points(
                _org_run, snapshot,
                reserved_total_provider=lambda: _org_ceiling)
        except Exception as _ope:                       # noqa: BLE001
            _org_actual, _org_err = None, f"partial_calc_exception:{str(_ope)[:80]}"
        if _org_err:
            # 与个人腿**同一处置**:既不多收也不少收 → 隔离转人工,charge link
            # 原样停在 reserved(钱既没扣也没退,由有权限的人裁)。
            # 🔴 特别是 `identity_suspicion`:那一档在个人腿就是「不自动扣也不自动退」,
            #    组织腿照抄 —— 自动全额结算等于拿我们自己的识别失误去扣客户组织的预算。
            _cas(run_token, ["running", "commit_pending"], "settlement_manual",
                 {"last_settlement_error": _org_err})
            _alert_to_manual("partial_settlement_undecidable", run_token,
                             f"org charge={organization_charge_id} {_org_err}")
            return {"ok": False, "terminal": "settlement_manual", "reason": _org_err}
        # `_org_actual is None` = 全履约(或无判定)→ 仍按 ceiling 结算,行为不变。
        _org_settle_points = _org_ceiling if _org_actual is None else int(_org_actual)

        from services.organization_billing import settle_charge as _settle_org_charge
        charge = await _settle_org_charge(
            charge_link_id=organization_charge_id,
            actual_points=_org_settle_points,
            result_payload={
                "diagnosis_id": int(diagnosis_id),
                "session_id": session_id,
                "run_token": run_token,
            },
            artifacts=(
                {
                    "artifact_type": "diagnosis",
                    "artifact_id": int(diagnosis_id),
                    "brand_id": brand_id,
                    "visibility": "private",
                    "publish": True,
                },
            ),
            diagnosis_run_token=run_token,
            claim_token=organization_claim_token,
        )
        return {"ok": True, "terminal": "committed", "charge": charge}
    return await commit_run(run_token, snapshot)


# ============================================================================
# sweeper(§3.4)· cron 内(§2 fencing 下)· 三类扫描
# ============================================================================
def _failed_snapshot(msg: str = "服务中断,费用已退,请重试") -> dict:
    return {"type": "error", "stage": "failed", "progress": 0, "done": True,
            "terminal": True, "message": msg, "error": msg}


#: 结算侧会从快照里读的判定信号 —— 单点清单,发送方与消费方共用同一个真相。
SETTLEMENT_SIGNAL_KEYS = ("delivery_verdict", "identity_suspicion")


def settlement_signals_from_result(result) -> dict:
    """[R2-② · 2026-08-24] 从 workflow 结果里取出结算要用的判定信号,原样带进完成 payload。

    为什么要有这个函数(而不是在 payload 里内联两条表达式):

    这两条信号栽过同一个跟头 —— **判定算了却没进快照**,结算侧永远读不到,
    于是「降级按比例扣」和「疑似识别失败转人工」两道守卫从上线起一次没开过火。
    P0-3/P0-3b 接线时配的是**结构锁**(断言键名出现在 payload 字面量里),
    而 Review 亲手注毒证明:**留着键、把值钉成 None**,52 条判据照样全绿 ——
    结构对了不等于值对了,那是两道缝。

    抽成函数就是为了把第二道缝焊上:取值逻辑成了可直接驱动的纯函数,
    判据能拿真实形状的 workflow result 断言「值真的穿透到了 payload」,
    而不是只断言键名出现过。同一取值逻辑只留这一处,发送方不会和消费方漂移。

    读不到就给 None(而不是省略键):省略键会让下游分不清"没算"和"算了是空",
    而 None 与两个消费方(`_partial_commit_points` / `_identity_suspected`)
    "拿不到就按没有处理"的既有语义一致。
    """
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    ai_visibility = data.get("ai_visibility")
    ai_visibility = ai_visibility if isinstance(ai_visibility, dict) else {}
    return {
        "delivery_verdict": data.get("delivery_verdict"),
        "identity_suspicion": ai_visibility.get("identity_suspicion"),
    }


def commit_outcome_is_success(out) -> bool:
    """[P0-3 · 2026-08-24] 结算返回的 `ok=True` 是否**真的**代表这一单成交了。

    `commit_run` 的 `ok=True` 只说明"这次结算动作执行成功了",不代表钱收了:
    commit 在 `_do_settlement` 内部可能因为产物证明不过关(典型是零成功观测的"没测成")
    **改道走 release**,此时返回的是 `{"ok": True, "terminal": "released"}`。
    调用方只看 `ok` 就发成功终态,用户会拿到"诊断完成 + 分数 + 等级(隐形级)",
    而钱其实已经退了、报告已经 withheld(点进去 404)—— 自相矛盾且误导。

    抽成函数是为了**可判**:这条判断原来是 server.py 里一句内联 if,
    判据够不到,变异改掉它没有任何锁会红(M8 实测存活)。
    """
    if not isinstance(out, dict) or not out.get("ok"):
        return False
    return out.get("terminal") != "released"


#: [P0-3 · 2026-08-24] 「没测成」对用户的唯一口径。不带任何等级词 —— 0 分「隐形级」的意思是
#: "测了,AI 不认识你",没测成的意思是"我们这次没测出来",把后者说成前者就是拿我们自己的
#: 故障去打客户的品牌,还顺手把钱收了。
NOT_MEASURED_MESSAGE = "这次没有测成(AI 搜索引擎全部访问失败),算力已退回,请稍后重试"

#: [P0-3b 挂账 B] 「疑似品牌识别失败」的用户口径。
#: 这一档**不自动退也不自动扣**(`_partial_commit_points` 返
#: `suspected_identity_failure_manual_review` → settlement_manual,冻结原样挂着由人裁),
#: 所以文案既不能说"已退回"(还没退),也不能给等级词(0 分很可能是我们没认出这个品牌,
#: 不是 AI 不认识它)。只说事实:结果要人工看一眼。
IDENTITY_REVIEW_MESSAGE = "这次的结果需要人工复核(可能是品牌名没被正确识别),算力暂未结算,我们会尽快处理"

#: 触发上面那条文案的结算错误码(`_partial_commit_points` 的返回值,单点常量防两处写歪)。
IDENTITY_REVIEW_REASON = "suspected_identity_failure_manual_review"


# ============================================================================
# [settlement-manual-ux 2026-08-25] 结算转人工的**用户可见终态**
#
# 起因(P0-3c 交回件②):结算转人工的几档里,有的发 done:false"正在完成结算",
# 有的**什么都不发** —— 前端停在 99% 无限转圈。
# 「绝不假装已退」是对的(Owner 已拍:外部副作用已发生 ⇒ 不自动 release,转人工;
# **本单一行不碰这个资金语义**),但「什么都不说」是另一回事。
#
# 🔴 只有**确定落到 settlement_manual** 的单才发这条终态。
#    "还在重试、交 sweeper"(commit_pending / release_pending)**必须**继续
#    done:false —— 那种单 sweeper 还会推真终态,提前宣布终态等于骗用户。
# ============================================================================

#: 文案 code:前端按它认档,不硬编码中文串。
SETTLEMENT_MANUAL_CODE = "settlement_manual"

#: 用户口径(Owner 2026-08-25 **定稿**,逐字)。术语铁律:说「费用/算力」,不出现
#: charge / unknown / release / token / quarantine 这类工程词。
#:
#: 🔴 后半句是定稿时**改过的**:初稿说「核实完成会自动更新」,但前端收到终态
#: 就停轮询 —— 页面不会原地更新,那是句假承诺。改成「回到这里就能看到结果」
#: 与真实行为一致。判据侧有一条负锁钉死("自动更新"不许再出现),别改回去。
SETTLEMENT_MANUAL_MESSAGE = (
    "结算转人工核实中,费用已冻结、不会多扣;核实完成后,回到这里就能看到结果,无需操作。"
)


def settlement_manual_terminal(message: Optional[str] = None) -> dict:
    """转人工终态 SSE 事件的**单点来源**。

    三档(commit 转人工 / 外层释放未即时终态 / 内层 cex 释放亦失败)共用这一个
    构造器 —— 同一个 payload 各写三遍的话必有一处漂(R-b 就是这么来的)。

    `type` 刻意既不是 `complete` 也不是 `error`:这不是成交,也不是失败,
    是"要人看一眼"。同时带 `needs_manual_review`,与既有的品牌识别复核档同键,
    前端一处判断即可覆盖两档(那一档的 payload 本单**一个字没动**)。
    不带 `error` 键 —— 带了会被前端渲染成红色故障横幅并给出"重新诊断"按钮,
    而转人工档重试无意义且可能双花。
    """
    return {
        "type": SETTLEMENT_MANUAL_CODE,
        "stage": "settlement_manual",
        "progress": 100,
        "done": True,
        "terminal": True,
        "needs_manual_review": True,
        "settlement_manual": True,
        "code": SETTLEMENT_MANUAL_CODE,
        "message": message or SETTLEMENT_MANUAL_MESSAGE,
    }


def run_is_settlement_manual(run_token: str) -> bool:
    """这一单**当前是不是**真的停在 settlement_manual。

    存在的理由:外层/内层两处兜底分支同时覆盖"转人工(确定终态)"与
    "没即时成功、交 sweeper 重试"两种情况。只看"走进了哪个 except"分不出来,
    必须问 run 行本身。读一行状态,不动任何钱。
    """
    try:
        run = get_run(run_token)
    except Exception:  # noqa: BLE001 —— 读状态失败时保持旧行为(不发终态),绝不假装
        return False
    return bool(run) and run.get("run_status") == "settlement_manual"


#: 成功快照里**会把等级词/分数/分享链接带到终态**的四个键。退款类终态一律显式覆盖。
#: 单点常量:两个退款分支各写一遍的话,迟早有一处漏(R-b 就是这么来的)。
_SUCCESS_LEAK_KEYS = ("result", "share_token", "score", "level")


def _refunded_snapshot(msg: str, **extra) -> dict:
    """退款/未成交类终态快照 —— 在 `_failed_snapshot` 之上**显式覆盖**会漏成功信息的四个键。

    为什么非覆盖不可:终态快照是 `_merge_terminal_snapshot(persisted, incoming)` 算出来的,
    而 persisted 是 `mark_product_pending` 落的那份**成功** payload(带
    `result={"total_score":0,"level":"隐形级"}` 和 share_token)。merge 只做
    `dict(persisted).update(incoming)` —— **没有删除语义**,incoming 不写这几个键
    它们就原样活到终态;而 server.py 的轮询兜底是 `return {"found": True, **_snap}`,
    整份快照直接回前端,等级词就这么漏出去。写 None 是覆盖,不是删除。
    """
    snap = _failed_snapshot(msg)
    snap.update({key: None for key in _SUCCESS_LEAK_KEYS})
    snap.update(extra)
    return snap


def _not_measured_snapshot(msg: str = NOT_MEASURED_MESSAGE) -> dict:
    """零成功观测 → 退款终态快照(带 not_measured 标,前端/运营可区分"没测成"与"测了但失败")。"""
    return _refunded_snapshot(msg, not_measured=True)


def _complete_snapshot(msg: str = "诊断完成") -> dict:
    """[对抗审核 P2 #6] 成功终态 canonical 快照(sweeper commit 分支也须写 · 否则 final_snapshot NULL →
    reconciler `IS NOT NULL` 跳过 → Redis 恢复后页面永久"处理中")。"""
    return {"type": "complete", "stage": "done", "progress": 100, "done": True,
            "terminal": True, "message": msg}


def _exempt_failed_snapshot(msg: str = "服务中断,请重试") -> dict:
    """[对抗审核 P2 #13] exempt 判死终态快照(exempt 无退款 · 但须落 final_snapshot 供 reconciler 回补)。"""
    return {"type": "error", "stage": "failed", "progress": 0, "done": True,
            "terminal": True, "message": msg, "error": msg}


def _cancel_snapshot(msg: str = "诊断未完成,费用未扣或已退,请重试") -> dict:
    """[返工 P1-3] cancelled / cancelled_no_freeze 终态快照(同步 withheld + final_snapshot ·
    否则 Redis 过期后前端可能回退显示旧成功态)。"""
    return {"type": "error", "stage": "failed", "progress": 0, "done": True,
            "terminal": True, "message": msg, "error": msg}


def _delivery_repair_snapshot(msg: str = "报告生成异常,已记录并将尽快为你补发,如有疑问请联系客服") -> dict:
    """[返工2 P0-2] delivery_repair_pending 终态快照:钱已结算(committed/completed_exempt)但产物 0 行
    (被并发删/未落库)→ **非成功终态**,前端显异常而非"完成"(杜绝"扣了钱却拿到 done+分数但无报告")。"""
    return {"type": "error", "stage": "failed", "progress": 100, "done": True,
            "terminal": True, "message": msg, "error": msg}


async def run_diagnosis_sweep() -> dict:
    """诊断 run 兜底扫描(判死 / pending_freeze 收尸 / settlement 退避重试)。全异常兜底 · 返回统计 dict。"""
    from db.connection import get_db
    stats = {"reaped_running": 0, "reaped_exempt": 0, "corpse_released": 0,
             "corpse_cancelled_no_freeze": 0, "corpse_pending": 0, "settled": 0, "settle_manual": 0,
             "stuck_alerted": 0, "manual_resumed": 0}

    # 1) 判死:running 心跳超 5min → paid: release_pending(reaped·交 settlement 退款) / exempt: failed_exempt
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE diagnosis_runs SET run_status='release_pending', status_changed_at=NOW(), "
                "reaped_reason='heartbeat_dead_reaped' "
                "WHERE billing_mode='paid' AND run_status='running' "
                f"AND heartbeat_at < NOW() - INTERVAL '{int(_RUNNING_DEAD_SECONDS)} seconds' RETURNING run_token",
            )
            reaped = [r["run_token"] for r in (cur.fetchall() or [])]
            # [P2 #13] exempt 判死改**逐 run 走 _terminal_local_txn**(写 final_snapshot + withheld · reconciler 可回补)
            cur.execute(
                "SELECT run_token FROM diagnosis_runs WHERE billing_mode='exempt' AND run_status='running' "
                f"AND heartbeat_at < NOW() - INTERVAL '{int(_RUNNING_DEAD_SECONDS)} seconds'",
            )
            exempt_dead = [r["run_token"] for r in (cur.fetchall() or [])]
        stats["reaped_running"] = len(reaped)
        for rt in reaped:
            _schedule_retry(rt, 0)                     # 立即可结算
            _release_slot_best_effort(rt)              # [P2 #10] 判死即释并发槽(不靠 TTL 慢释放)
        for rt in exempt_dead:
            if _terminal_local_txn(rt, ["running"], "failed_exempt", "withheld", _exempt_failed_snapshot())[0]:
                stats["reaped_exempt"] += 1
                _release_slot_best_effort(rt)          # [P2 #10]
    except Exception as e:
        logger.warning(f"[DiagSweep] 判死阶段异常: {e}")

    # 1b) [P2 #7] exempt pending_freeze 收尸:activate 崩在 admit 后/CAS running 前 → exempt run 卡 pending_freeze
    #     占用 uq_diag_active_per_brand 锁死品牌。exempt 无 freeze → 超 10min 直接 failed_exempt(无 billing · 写 snapshot)。
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token FROM diagnosis_runs WHERE billing_mode='exempt' AND run_status='pending_freeze' "
                f"AND status_changed_at < NOW() - INTERVAL '{int(_PENDING_FREEZE_REAP_SECONDS)} seconds'",
            )
            exempt_stuck = [r["run_token"] for r in (cur.fetchall() or [])]
        for rt in exempt_stuck:
            if _terminal_local_txn(rt, ["pending_freeze"], "failed_exempt", "withheld", _exempt_failed_snapshot())[0]:
                stats["reaped_exempt"] += 1
                _release_slot_best_effort(rt)
    except Exception as e:
        logger.warning(f"[DiagSweep] exempt pending_freeze 收尸异常: {e}")

    # 2) pending_freeze 收尸(超 10min · 双表只读定位 · 首次查空≠没冻 · verify≥2 且间隔≥30s 才 cancelled)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token, owner_user_id, payer_user_id, freeze_task_ref, verify_empty_count, last_verify_at "
                "FROM diagnosis_runs WHERE billing_mode='paid' AND run_status='pending_freeze' "
                f"AND status_changed_at < NOW() - INTERVAL '{int(_PENDING_FREEZE_REAP_SECONDS)} seconds'",
            )
            corpses = [dict(r) for r in (cur.fetchall() or [])]
        for c in corpses:
            rt = c["run_token"]
            b, fid, fstatus = _double_table_locate(settlement_payer_user_id(c), c["freeze_task_ref"])
            if b == "error":
                continue  # 查询异常 · 不计数 · 下轮再试(绝不假定已退款)
            if b in ("legacy", "v35"):
                _persist_freeze_handle(rt, fid, b)
                if _cas(rt, ["pending_freeze"], "release_pending",
                        {"reaped_reason": "corpse_located", "verify_empty_count": 0}):
                    _schedule_retry(rt, 0)
                    stats["corpse_released"] += 1
                    _release_slot_best_effort(rt)   # [P2 #10] 收尸即释槽
                continue
            if b == "ambiguous":
                _cas(rt, ["pending_freeze"], "settlement_manual",
                     {"last_settlement_error": "corpse_ambiguous_double_frozen"})
                _alert_to_manual("corpse_ambiguous", rt, "double-table both frozen")
                stats["settle_manual"] += 1
                _release_slot_best_effort(rt)       # [P2 #10]
                continue
            # b is None:查空 → verify 计数(间隔≥30s 才计连续);≥2 → cancelled_no_freeze
            _handle_verify_empty(c, stats)
    except Exception as e:
        logger.warning(f"[DiagSweep] 收尸阶段异常: {e}")

    # 3) settlement 退避重试:commit_pending/release_pending 且 next_settlement_at 到期
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token, run_status, final_snapshot_jsonb FROM diagnosis_runs "
                "WHERE run_status IN ('commit_pending','release_pending') "
                "AND (next_settlement_at IS NULL OR next_settlement_at <= NOW()) "
                "ORDER BY status_changed_at ASC LIMIT 100",
            )
            due = [dict(r) for r in (cur.fetchall() or [])]
        for d in due:
            intent = "commit" if d["run_status"] == "commit_pending" else "release"
            # [P2 #6] commit 分支也传成功终态 snapshot(否则 final_snapshot NULL → reconciler 跳过 → 永久"处理中")
            snap = d.get("final_snapshot_jsonb") if intent == "commit" else _failed_snapshot()
            if intent == "commit" and not isinstance(snap, dict):
                try:
                    snap = json.loads(snap) if isinstance(snap, str) else None
                except Exception:
                    snap = None
            out = await _do_settlement(d["run_token"], intent, snap)
            if out.get("ok"):
                stats["settled"] += 1
                _release_slot_best_effort(d["run_token"])       # [P2 #10] 终态释并发槽
                await _publish_run_terminal_best_effort(d["run_token"])  # [返工2 P3] 终态即刻回推·不等 reconciler
            elif out.get("terminal") == "settlement_manual":
                stats["settle_manual"] += 1
                _release_slot_best_effort(d["run_token"])       # 转人工也释槽(任务已不再跑)
    except Exception as e:
        logger.warning(f"[DiagSweep] settlement 重试阶段异常: {e}")

    # 3b) [返工4 P0] manual_resolving 崩溃续跑:仅**租约过期**(前执行者崩/超时 · manual_lease_until < NOW())的 run 才
    #     接管续跑 —— 有效租约(在途处理中)绝不抢。resume_double_frozen 走 claim 接管路径(CAS 拿新 token)· 幂等
    #     (release_freeze 幂等 + keeper CAS token-gated)。续跑成功 → run 进 keeper *_pending 交 settlement 重试正常结算。
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token FROM diagnosis_runs WHERE run_status='manual_resolving' "
                "AND (manual_lease_until IS NULL OR manual_lease_until < NOW()) "
                "ORDER BY status_changed_at ASC LIMIT 100",
            )
            resuming = [r["run_token"] for r in (cur.fetchall() or [])]
        for rt in resuming:
            try:
                out = await resume_double_frozen(rt)
                if out.get("ok"):
                    stats["manual_resumed"] += 1
            except Exception as _rex:
                _alert_ratelimited("manual_resolving_resume_exc", rt, f"resume exc: {str(_rex)[:200]}")
    except Exception as e:
        logger.warning(f"[DiagSweep] manual_resolving 续跑阶段异常: {e}")

    # 4) [返工2 item8 / 返工3 P0] settlement_manual / delivery_repair_pending / manual_resolving 超时告警:卡人工/续跑
    #    超 1h 未闭 → 限频提醒 ops(每 run+kind 每小时最多一条 · 防告警风暴)· 资金卡在中间态太久运营必须知道。
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token, run_status FROM diagnosis_runs "
                "WHERE run_status IN ('settlement_manual','delivery_repair_pending','manual_resolving') "
                f"AND status_changed_at < NOW() - INTERVAL '{int(_MANUAL_STUCK_ALERT_SECONDS)} seconds' "
                "AND (last_settlement_error IS NULL OR last_settlement_error NOT LIKE 'ops_resolved:%') "  # [返工3 P1] writeoff 核销后抑制告警(无参 execute · 单 %)
                "ORDER BY status_changed_at ASC LIMIT 200",
            )
            stuck = [dict(r) for r in (cur.fetchall() or [])]
        for s in stuck:
            _alert_ratelimited(
                "settlement_stuck", s["run_token"],
                f"{s['run_status']} 卡超 {_MANUAL_STUCK_ALERT_SECONDS // 60}min 未处置 · 请 admin 核对处置",
                ttl=3600)
            stats["stuck_alerted"] += 1
    except Exception as e:
        logger.warning(f"[DiagSweep] settlement 超时告警阶段异常: {e}")

    # 5) [WO_SETTLEMENT_AI_ADJUDICATOR_2026-08-25] 结算 AI 审查员:对 settlement_manual 单
    #    产证据包 → 机械规则裁定 → 走 admin 同一把 CAS(verify_and_resolve_manual)交还本 sweeper 结算。
    # 🔴 挂在这里而不是新注册一个 cron job:本 job 已是**无条件注册 + sched_claim 防双跑**,
    #    复用它 ⇒ `api/scheduler.py` 零 diff(scheduler 是慎改区)。
    # 🔴 审查员**不动钱**:它只出 commit/release 二值 decision,金额与已履约比例仍由
    #    本文件既有的 `_partial_commit_points` 在后续 tick 算。
    # 🔴 未配置系数 ⇒ 审查员整轮空转(fail-closed),本段等于零行为变化。
    # 🔴 全异常兜底:审查员炸掉绝不能带走 sweeper 前四段(判死/收尸/结算重试/告警)。
    try:
        from services.settlement_adjudicator import run_adjudication_tick
        _adj = await asyncio.to_thread(run_adjudication_tick)
        # 🔴 只把**动作计数**并进 stats(标量),不要把整个 dict 塞进去:
        #    `any(stats.values())` 对非空 dict 恒真 ⇒ 会让 sweeper 每 2min 无脑打一行日志。
        stats["adjudicated"] = int(
            _adj.get("auto_commit", 0) + _adj.get("auto_release", 0) + _adj.get("escalated", 0))
        if any(v for k, v in _adj.items() if k != "skipped_no_config"):
            logger.info(f"[Adjudicator] {_adj}")
    except Exception as e:
        logger.warning(f"[DiagSweep] 结算审查员阶段异常: {e}")

    if any(stats.values()):
        logger.info(f"[DiagSweep] {stats}")
    return stats


def _handle_verify_empty(corpse: dict, stats: dict) -> None:
    """pending_freeze 双表查空:间隔≥30s 才 +1;连续 ≥2 → cancelled_no_freeze(零退款零告警)。"""
    from db.connection import get_db
    rt = corpse["run_token"]
    with get_db() as conn:
        cur = conn.cursor()
        # 只在距上次查空≥30s 时计连续;否则不动计数(防同轮/过密重复计)
        cur.execute(
            "SELECT verify_empty_count, "
            f"(last_verify_at IS NULL OR last_verify_at < NOW() - INTERVAL '{int(_VERIFY_INTERVAL_SECONDS)} seconds') AS gap_ok "
            "FROM diagnosis_runs WHERE run_token=%s",
            (rt,),
        )
        row = cur.fetchone()
        if row is None:
            return
        if not row["gap_ok"]:
            return  # 间隔不足 · 不计
        new_count = (row["verify_empty_count"] or 0) + 1
        cur.execute(
            "UPDATE diagnosis_runs SET verify_empty_count=%s, last_verify_at=NOW() "
            "WHERE run_token=%s AND run_status='pending_freeze'",
            (new_count, rt),
        )
    if new_count >= 2:
        # 连续两次查空且间隔≥30s → 确认无冻结 → cancelled_no_freeze(零退款零告警)
        # [返工 P1-3] 走终态事务:同步 withheld + final_snapshot(否则占位产物/可见性未闭 · Redis 过期后回退成功态)
        if _terminal_local_txn(rt, ["pending_freeze"], "cancelled_no_freeze", "withheld", _cancel_snapshot())[0]:
            stats["corpse_cancelled_no_freeze"] += 1
            _release_slot_best_effort(rt)   # [P2 #10] 终态释槽
    else:
        stats["corpse_pending"] += 1


# ============================================================================
# reconciler(§3.6a)· Redis 恢复后回补终态快照
# ============================================================================
async def _publish_run_terminal_best_effort(run_token: str) -> None:
    """[返工2 修复净增量 P3] sweeper 终态写出后 best-effort 跨 worker publish · 砍掉"终态只靠 reconciler 每 5min
    回补"这条腿(崩溃/丢租恢复窗口 7-12min → ~5-7min)。只读 DB+Redis · 不碰 billing · 失败吞掉 · 只在终态触发。"""
    try:
        from db.connection import get_db
        from cache import progress_bus
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT session_id, final_snapshot_jsonb FROM diagnosis_runs WHERE run_token=%s", (run_token,))
            row = cur.fetchone()
        if not row or row.get("final_snapshot_jsonb") is None:
            return
        payload = row["final_snapshot_jsonb"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            await progress_bus.publish(row["session_id"], payload, terminal=True)
    except Exception:
        pass


async def run_diagnosis_reconciler() -> dict:
    """扫近 24h 已终态 run,其 final_snapshot 存在但 Redis 快照缺失/陈旧(非终态)→ 重发终态。"""
    from db.connection import get_db
    from cache import progress_bus
    stats = {"checked": 0, "republished": 0}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_token, session_id, run_status, final_snapshot_jsonb FROM diagnosis_runs "
                "WHERE run_status = ANY(%s) AND finished_at > NOW() - INTERVAL '24 hours' "
                "AND final_snapshot_jsonb IS NOT NULL ORDER BY finished_at DESC LIMIT 500",
                (list(TERMINAL_STATUSES),),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.warning(f"[DiagReconciler] 扫描异常: {e}")
        return stats
    for row in rows:
        stats["checked"] += 1
        sid = row["session_id"]
        snap = progress_bus.get_snapshot_sync(sid)
        snap_terminal = bool(snap and (snap.get("terminal") or snap.get("done")))
        # [返工2 P0-1c] Redis 有终态 ≠ 可信:可能是"陈旧成功态"(impl 早发/结算后翻案退款)。
        #   以 DB 终态为准:Redis 终态"成功性"与 DB 终态不一致(Redis 显 complete 但 DB 是
        #   released/cancelled/failed_exempt/delivery_repair_pending 退款/失败终态)→ 必须用 DB final_snapshot 纠正,
        #   否则前端永久看到"已完成+分数+诊断 ID"而钱已退。仅当 Redis 终态且成功性与 DB 一致才跳过。
        snap_is_success = bool(snap and snap.get("type") == "complete" and not snap.get("error"))
        db_is_success = row["run_status"] in _SUCCESS_TERMINAL_STATUSES
        if snap_terminal and (snap_is_success == db_is_success):
            continue  # Redis 与 DB 终态一致(同为成功 or 同为失败/退款)· 无需回补
        # Redis 缺失/非终态 / 或终态成功性与 DB 相反 → 以 DB 终态回补(重发终态 · publish 带 terminal 取新 seq · HC3)
        payload = row["final_snapshot_jsonb"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            continue
        try:
            await progress_bus.publish(sid, payload, terminal=True)
            stats["republished"] += 1
        except Exception:
            continue
    if stats["republished"]:
        logger.info(f"[DiagReconciler] 回补终态 {stats['republished']}/{stats['checked']}")
    return stats
