"""发布 worker:四个 kill window 安全的外调路径(规格 §12.3)。

判据:FIN-13 / MED-11 / MED-18 / §19 变异 81。

═══════════════════════════════════════════════════════════════════════
🔴 四个窗口,以及每个窗口靠**哪一条结构性约束**收敛
═══════════════════════════════════════════════════════════════════════
§12.3 逐字列了四个 kill window。它们不是四段 try/except,而是四条**约束**:

  ① 幂等 claim 成功后、业务事务开始前被杀
     → 恢复返回同一 root,不创建第二 command。
       承重 = ``defgeo_pcmd_idem_root`` UNIQUE(tenant, slot, key, request_hash)。
  ② 业务对象 + exact freeze + outbox 同事务提交后、HTTP 202 返回前被杀
     → 重放返回同一对象,资金**只冻结一次**。
       承重 = 同一个 UNIQUE + ``defgeo_pout_command_unique``;
       三者在**同一事务**里,所以要么全在要么全不在。
  ③ provider 已接受后、本地 ack / external-start 终态落库前被杀
     → 自动恢复**不得盲重传**,external start 至多一次,未知保持 frozen。
       承重 = ``mark_external_start`` 的 ``WHERE external_start_at IS NULL``
       —— marker 在**外调之前**写,所以「写了 marker 但没收到回执」这一格
       必然表现为「marker 在、outcome 未知」,而不是「什么都没有」。
       🔴 顺序反了(先外调再写 marker)会让这一格无法与「根本没调过」区分,
          于是恢复只能靠猜 —— §12.3 特意要求 marker 在外调**前**写。
  ④ canonical outcome 已持久化后、commit/release/平台账/容量收敛前被杀
     → reconciler 从该 outcome **原子**完成剩余状态,旧 attempt 不原地改成新 attempt。
       承重 = reconciler(见 ``reconciler.py``)+ ``bump_status`` 的 statusVersion CAS。

═══════════════════════════════════════════════════════════════════════
🔴 法律门在 marker 之前
═══════════════════════════════════════════════════════════════════════
:func:`dispatch_once` 的顺序是**不可交换**的:

    法律门 → (命中 ⇒ 零外调 + release + needs_action)
           → mark_external_start
           → provider 调用

把法律门放到 marker 之后,``provider call = 0`` 这条判据就永远绿不了
(marker 递增 ``provider_call_count``,它数的是外调意图)。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable, Mapping, NamedTuple

from services.defensive_geo.publish import legal_gate as _legal
from services.defensive_geo.publish import publish_funding as _funding
from services.defensive_geo.publish import publish_settlement as _settle
from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoPublishWorker")

WORKER_VERSION = "defgeo-publish-worker-v1"


class DispatchOutcome(NamedTuple):
    """一次派发的结果。``provider_calls`` 是判据直接数的那个数。"""

    command_id: str
    legal_blocked: bool
    external_started: bool
    provider_calls: int
    canonical_state: str
    funding_state: str
    note: str


class ProviderResult(NamedTuple):
    """provider 适配器的返回。**三态**,不是布尔。

    没有「失败」这一格 —— 失败必须说清是「确认零接单」还是「结果未知」,
    因为这两者的钱向相反(release vs hold)。把它们压成一个 bool
    正是 §12.1「不得把接线故障伪装成正常退款」的病根。
    """

    kind: str          # 'accepted' | 'rejected_no_effect' | 'unknown'
    raw_table: str
    raw_column: str
    raw_value: Any
    detail: str = ""


ProviderCall = Callable[[Mapping[str, Any]], ProviderResult]


async def dispatch_once(
    cur,
    *,
    command: Mapping[str, Any],
    frozen_body: str,
    provider_call: ProviderCall,
    claim_token: str,
    on_external_start_committed: Callable[[], None] | None = None,
) -> DispatchOutcome:
    """把一条 command 推到 provider。**调用方持有事务**。

    ``provider_call`` 由调用方注入(生产 = 真适配器;判据 = fake provider)。
    注入而不是 import 的理由:四个 kill window 的判据必须能在**任意一步**
    抛异常,而那要求那一步是判据能拿到的对象。

    ``on_external_start_committed``(包E 追加 · **默认 None ⇒ 行为逐字不变**)
    ------------------------------------------------------------------------
    🔴 窗口③ 要的不是"marker 写在外调之前",是 **marker 已经落盘** 在外调之前。
       marker 与外调同处一个未提交事务时,进程在外调途中被杀 ⇒ 事务回滚 ⇒
       marker 消失 ⇒ 下一次派发**盲重传**。那正是 §12.3「恢复时任一 marker
       存在均不得盲目二次外调」要挡的事,而"存在"的前提是它熬得过崩溃。

       所以真正的 worker 传一个提交钩子进来(``conn.commit``):
       marker 写完 → 提交 → 才外调。既有调用方(含判据里的同步 fake)
       不传这个参数时,这一行不执行,拼出来的行为与改动前逐字相同。
    """
    command_id = command["publish_command_id"]

    # ── ⓪ [B-2] 已经**结算过**的命令,一次都不再外发 ─────────────────────
    #    ``settled_at`` 非空 = 这笔钱已经按某个方向收过尾了(退了或扣了)。
    #    收敛器⑦ 在"队列放弃 + 零外调"时会 release 一次并把 commandState
    #    摆成 ``needs_action`` —— 那**不是**终局命令态,所以一个还握着过期租约的
    #    旧 worker 仍能走到这里,于是「先退款、后外发」这个窗口是可达的。
    #    fencing token 挡的是"迟到的收尾覆盖新租约",这一句挡的是
    #    "迟到的外发"本身:钱已经回去了,货不能再发出去。
    if command.get("settled_at") is not None:
        logger.warning("[defgeo-publish] %s 已结算(settled_at 非空),拒绝外发", command_id)
        return DispatchOutcome(
            command_id, False, False, int(command.get("provider_call_count") or 0),
            str(command.get("canonical_publication_state") or "unknown"),
            str(command.get("funding_state") or ""),
            "该命令的资金已收尾 —— 结算之后不得再对外发起(先退款后外发窗口)",
        )

    # ── ① 已经外调过?恢复路径**不得盲目二次外调**(窗口 ③)──────────────
    if command.get("external_start_at") is not None:
        logger.warning(
            "[defgeo-publish] %s 已有 external-start marker,不重传;转未知态核验", command_id,
        )
        _store.bump_status(
            cur, publish_command_id=command_id,
            canonical_publication_state="unknown",
            funding_state="pending_reconciliation",
            command_state="settlement_pending",
            status_reason="上一次外调结果未确认，正在向平台核实；费用保持冻结",
        )
        return DispatchOutcome(
            command_id, False, True, int(command.get("provider_call_count") or 0),
            "unknown", "pending_reconciliation",
            "external-start marker 已存在 —— 恢复不重传(§12.3 窗口③)",
        )

    # ── ② 法律门(marker 之前、provider 之前)────────────────────────────
    verdict = _legal.evaluate(
        article_revision_id=str(command["article_revision_id"]),
        frozen_body=frozen_body,
    )
    if verdict.blocked:
        hit = verdict.primary
        assert hit is not None
        _store.bump_status(
            cur, publish_command_id=command_id,
            canonical_publication_state="failed_no_effect",
            command_state="needs_action",
            legal_rule_id=hit.rule_id,
            legal_rule_version=hit.rule_version,
            legal_passage_ref=hit.passage_ref,
            legal_passage_excerpt=hit.passage_excerpt,
            status_reason="这句话可能违反广告法（已标出位置），没有扣费；修好这一句重新确认即可发布",
        )
        fresh = _store.get_command_any_tenant(cur, publish_command_id=command_id)
        assert fresh is not None
        # 🔴 [R3 · Owner 批口径①] 平台腿不再被挡在结算之外:广告法命中 =
        #    ``provider call = 0`` 的**权威零接单**,与钱包腿同一件事。
        #    ``fundingState`` 的不变性由 ``bump_status`` 的 CASE 保证,
        #    所以这里不需要再挡一次(挡了反而让平台账的冻结永远悬着)。
        # 🔴 [P0-4] 退款**真成了**才落终态 —— ``release_exact`` 内部同事务写
        #    fundingState + settled_at(带 statusVersion CAS)。原来这里跟着
        #    一句无条件的 ``bump_status(funding_state="released")``:
        #    billing 返 success=false / ambiguous / 相反幂等时,
        #    客户看到"没扣费"而钱还冻着。
        outcome = await _funding.release_exact(
            cur, fresh, reason="广告法命中，零外调，全额退回")
        if not outcome.settled:
            _funding.apply_unsettled(cur, fresh, outcome)
            after = _store.get_command_any_tenant(cur, publish_command_id=command_id) or fresh
            return DispatchOutcome(
                command_id, True, False, int(fresh.get("provider_call_count") or 0),
                "failed_no_effect", str(after.get("funding_state") or ""),
                f"命中 {hit.rule_id}@{hit.rule_version}，provider call = 0；"
                f"退款未完成({outcome.reason}),资金保持冻结转核验",
            )
        return DispatchOutcome(
            command_id, True, False, int(fresh.get("provider_call_count") or 0),
            "failed_no_effect",
            "exempt_recorded" if _funding.is_platform_cost(str(fresh["funding_policy"])) else "released",
            f"命中 {hit.rule_id}@{hit.rule_version}，provider call = 0",
        )

    # ── ③ 写 external-start marker(**在外调之前**)──────────────────────
    token = secrets.token_hex(16)
    # 🔴 [E1-1 = Codex 二审 P0-F1] 这一句现在是「能不能外发」的**唯一裁决点**:
    #    租约 + settled_at + 资金/命令态,全部塞进同一条 UPDATE 的 WHERE,
    #    由数据库用**服务端当下的事实**一次裁定。上面那道 ⓪ 门读的是事务开始时的
    #    旧 mapping —— 它挡不住「读完之后才发生的退款」,那正是 P0-F1 的窗口。
    #    这里 0 行 ⇒ 一次 provider 都不发。
    if not _store.mark_external_start(
            cur, publish_command_id=command_id, token=token, claim_token=claim_token):
        # 三种都归到"不外调、不重传":已有 marker / 资金已收尾 / 租约已不在我手里。
        #
        # 🔴 措辞上**刻意避开** "已结算" / "结算之后" 这两个串:上面那道 ⓪ 门的
        #    判据(b2_10)正是按它们判红的。新闸在语义上覆盖了 ⓪ 门,若这里也用
        #    同样的措辞,摘掉 ⓪ 门之后 b2_10 会继续绿 —— **新闸顶掉旧闸**,
        #    ⓪ 门从此没人守。两道闸要各自可判。
        return DispatchOutcome(
            command_id, False, True, int(command.get("provider_call_count") or 0),
            "unknown", "pending_reconciliation",
            "此刻不允许外发(marker 已存在 / 资金已收尾 / 租约已易主),本次不重传",
        )
    if on_external_start_committed is not None:
        # 🔴 marker 落盘**在外调之前**。这一行失败就一步都不再往下走 ——
        #    提交不了意味着 marker 没熬过去,此时外调等于把「调过没有」
        #    这件事交给运气。
        on_external_start_committed()

    # ── ④ 真正外调 ────────────────────────────────────────────────────
    try:
        result = provider_call(dict(command))
    except Exception as exc:                    # noqa: BLE001 —— 必须收成未知,不是失败
        # 🔴 异常 ≠ 零接单。已经写了 marker 就意味着请求可能已经到达对方。
        _store.bump_status(
            cur, publish_command_id=command_id,
            canonical_publication_state="unknown",
            funding_state="pending_reconciliation",
            command_state="settlement_pending",
            status_reason="正在向平台核实结果，费用已冻结",
        )
        logger.error("[defgeo-publish] %s 外调抛异常,转未知态:%s", command_id, exc)
        return DispatchOutcome(
            command_id, False, True, 1, "unknown", "pending_reconciliation",
            f"外调异常收成未知态(不 release):{type(exc).__name__}",
        )

    facts = _settle.PublicationFacts(
        source_table=result.raw_table, source_column=result.raw_column,
        raw_value=result.raw_value, external_start_recorded=True,
        url_verification_state=None, url_availability_state=None,
        legal_rule_hit=False,
    )
    canonical, direction = _settle.project(facts)
    updates: dict[str, Any] = {
        "canonical_publication_state": canonical,
        "raw_state_source_table": result.raw_table,
        "raw_state_source_column": result.raw_column,
        "raw_state_value": str(result.raw_value),
        "command_state": _command_state_for(direction),
    }
    # 🔴 ``status_reason`` 是**给她看的那一句**(façade 会把它上屏)。
    #    上游单号不是人话,也不该出现在客户面 —— 它进 047 那一列。
    if result.kind == "accepted" and result.detail:
        order_ref = str(result.detail)[:120]
        # ═══════════════════════════════════════════════════════════════════
        # 🔴 [B-5 = Codex P1-5] 一个上游单号只能绑**一条** command
        # ═══════════════════════════════════════════════════════════════════
        # 上游那条反查链按「标题 + 媒体」找单号(``services/meijiehezi/client.py``
        # 的 ``_lookup_order_sn_by_signature``)。同标题同媒体重发时,
        # 它可能把**旧订单**的 sn 交回来 —— 于是新 command 绑到旧单上,
        # 而 ``poll_pending_outcomes`` 会拿同一行 ``mhz_synced_orders`` 的终态
        # 去推**两笔**冻结的结算(一个上游结果结算多笔钱)。
        #
        # 051 的部分唯一索引是承重的那一半(应用层预检会被并发绕过,索引不会);
        # 这里这一手是**人话的那一半**:撞了就不绑、不猜、转核验,
        # 而不是让 UniqueViolation 把整个派发事务炸掉(那会连 canonical
        # outcome 一起回滚 —— 「已经外调过」这件事就丢了)。
        owner = _store.command_by_provider_order_ref(cur, provider_order_ref=order_ref)
        if owner is not None and str(owner["publish_command_id"]) != str(command_id):
            logger.error(
                "[defgeo-publish] %s 拿到的上游单号 %s 已经绑在 %s 上 —— 拒绝绑定,转核验",
                command_id, order_ref, owner["publish_command_id"])
            _store.bump_status(
                cur, publish_command_id=command_id,
                canonical_publication_state="unknown",
                funding_state="pending_reconciliation",
                command_state="settlement_pending",
                status_reason="正在向平台核实这一篇的发布结果，费用已冻结、不会多扣",
            )
            return DispatchOutcome(
                command_id, False, True, 1, "unknown", "pending_reconciliation",
                "上游单号已被另一条发布命令占用 —— 不绑定、不结算,转人工核验",
            )
        updates["provider_order_ref"] = order_ref
    else:
        updates["status_reason"] = result.detail or None
    _store.bump_status(cur, publish_command_id=command_id, **updates)
    return DispatchOutcome(
        command_id, False, True, 1, canonical,
        str(command.get("funding_state")), f"provider 返回 {result.kind}",
    )


def _command_state_for(direction: str) -> str:
    """钱向 → commandState。**闭表**,与 §15.7 真值表同源。"""
    table = {
        "none": "queued",
        "hold_frozen": "running",
        "commit": "completed",
        "preserve_historical_commit": "completed",
        "release": "failed",
        "hold_or_quarantine": "settlement_pending",
    }
    if direction not in table:
        raise _settle.SettlementProjectionError(f"未知钱向 {direction!r}")
    return table[direction]


def census() -> dict[str, Any]:
    return {
        "workerVersion": WORKER_VERSION,
        "killWindows": [
            {"id": 1, "boundary": "idempotency claim 成功后 / 业务事务开始前",
             "invariant": "同一 root,不创建第二 command",
             "enforcedBy": "defgeo_pcmd_idem_root UNIQUE"},
            {"id": 2, "boundary": "业务对象+freeze+outbox 同事务提交后 / HTTP 202 返回前",
             "invariant": "重放返回同一对象,资金只冻结一次",
             "enforcedBy": "同一 UNIQUE + defgeo_pout_command_unique + 单事务"},
            {"id": 3, "boundary": "provider 已接受后 / 本地 ack + external-start 终态落库前",
             "invariant": "不盲重传,external start ≤1,未知保持 frozen",
             "enforcedBy": "mark_external_start 的 WHERE external_start_at IS NULL"},
            {"id": 4, "boundary": "canonical outcome 已持久化后 / commit·release·平台账收敛前",
             "invariant": "reconciler 从 outcome 原子收敛,旧 attempt 不原地改新",
             "enforcedBy": "reconciler + bump_status 的 statusVersion CAS"},
        ],
    }
