"""晋升决策引擎:pending event → promoted / private_only / rejected / pending_review。

铁律:
- LLM(实体/outcome)在写事务之外做(不在写锁内烧 LLM);写阶段短事务 + lease CAS(旧 worker 复活零写)。
- 晋升必须:policy.promotion_enabled + 已批准 promotion_legal_basis(R11:法务未批→private_only)。
- 严格资格:gate.promotable(R1/R2/R3)+ entity=confirmed_*(非 ambiguous/unknown/provider_error)。
- 匿名 signal + assert_signal_clean(隐私硬门)+ 一事件一 signal(UNIQUE event_id)。
- 防刷/k匿名:HMAC contributor bucket 5 元组(R6,不含 source_type);缺 HMAC→公共晋升 fail-closed(R7)→private_only。
- 赢票→effective_weight=base;败票(同 5 元组已投)→仍 promoted 作稳定度样本但 effective_weight=0(不加客户权重)。
- 撤回不在此(见 reconciler);signals 不可变。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from db.connection import get_db
from . import repository, source_hooks, privacy, hmac_buckets, policy as policy_mod
from .audit import write_audit
from .contracts import (
    EntityState, ProcessingState, PromptIntent, ResponseStatus, SourceType, TargetOutcome,
)
from .entity_review import (
    ENTITY_PROMPT_VERSION, OUTCOME_PROMPT_VERSION, VERIFIER_SCHEMA_VERSION, LeaseLost, ProviderResultUnknown,
    classify_outcome, classify_research_outcome, decision_to_entity_state,
    make_official_outcome_classifier, make_official_verifier, resolve_entity,
)

# 兼容别名:历史内部名 _LeaseLost 现指向 entity_review.LeaseLost(BaseException,穿透 resolver 宽 except)
_LeaseLost = LeaseLost

logger = logging.getLogger("GEO-ObservationPromotion")

#: [CUR-08 · 防御型 GEO WP1] 名次「未测量」的唯一表示。
#:
#: 用具名常量而不是裸 ``None``,是为了让「有意不测」与「忘了填」在代码里长得不一样 ——
#: 裸 None 谁都能顺手改成一个数字,而且改了没人看得出来那是猜的。
#: 判据 ``tests/defensive_geo_2026_08_21/test_rank_not_fabricated.py`` 锁死:
#: 只要 ``services/geo_observation/repository.py`` 的 ``position`` 列还没有真 producer,
#: 这一格就必须恒等于本常量;任何从 is_recommended / 行号 / 提及顺序反推名次的写法必红
#: (§19 变异 30 → MET-03)。
#:
#: 值本身是 None:数据库 ``target_position`` 列可空,``NULL`` 就是「未测量」的正确表示。
#: §16.2 兼容默认明确要求「``null`` 旧行不回填成 0 或 false」—— 0 会被当成「第 0 名」。
RANK_NOT_MEASURED = None

_SOURCE_WEIGHT_KEY = {
    "research_round": "research",
    "paid_diagnosis": "paid_diagnosis",
    "recurring_monitoring": "monitoring",
}
_PROMOTABLE_ENTITY = (EntityState.confirmed_mention, EntityState.confirmed_non_mention)
_RECOMMENDATION_OUTCOMES = (TargetOutcome.recommended, TargetOutcome.conditionally_recommended,
                            TargetOutcome.candidate_only, TargetOutcome.mentioned_only)
_CONFIDENCE_BY_METHOD = {
    "trusted_exact": 9500, "deterministic": 8000, "deepseek_v4_flash_structured": 7000,
}
MAX_PROMOTION_ATTEMPTS = 5   # 毒性事件重试上限(超限 → error 终态,不再 poison-loop / 反复烧 LLM)


@dataclass
class DecisionContext:
    """晋升治理上下文(每次决策从 policy SSOT 读新鲜值)。legal_basis=None → 一律 private_only(R11)。"""
    promotion_enabled: bool
    promotion_legal_basis: Optional[str]
    consent_policy_version: Optional[str]
    retention_until: Optional[datetime]
    outcome_gold_gate_passed: bool = False


def _reload_observation(cur, event: dict):
    """按 event 的 source_record_id/source_subkey 重新只读核验源终态并取 answer/question(晋升期新鲜度)。"""
    st = event["source_type"]
    rec_id = event["source_record_id"]
    subkey = event["source_subkey"]
    if st == "recurring_monitoring":
        gate, obs = source_hooks.assess_monitoring_result(cur, int(rec_id))
        return gate, obs
    if st == "research_round":
        gate, obs = source_hooks.assess_research_raw(cur, int(rec_id))
        return gate, obs
    if st == "paid_diagnosis":
        gate, observations = source_hooks.build_paid_diagnosis_observations(cur, rec_id)
        obs = next((o for o in observations if o.event_fields.get("source_subkey") == subkey), None)
        return gate, obs
    return None, None


def _build_signal(event: dict, obs, gate, entity_state: EntityState, decision, outcome: TargetOutcome,
                  policy_doc: dict, won_vote: bool, canon_industry: str) -> dict:
    src_weight = int(policy_doc["source_base_weights_bps"][_SOURCE_WEIGHT_KEY[event["source_type"]]])
    method = getattr(decision, "method", "") or ""
    confidence = _CONFIDENCE_BY_METHOD.get(method, 6000)
    # 品牌名(private · 仅用于分类/匿名化剥离,绝不入 signal)。signal 与 bucket 用同一 family_key + 受控行业 ID。
    brand_names = list(getattr(obs, "brand_names", []) or [])
    branded = privacy.is_branded_prompt(obs.question_text, brand_names)
    family_key = privacy.anonymize_prompt_family_key(obs.question_text, brand_names, canon_industry)
    intent = privacy.derive_prompt_intent(obs.question_text, branded)
    domains = privacy.clean_source_domains(obs.citations)
    quality = 5000 + (2500 if domains else 0) + (1500 if entity_state is EntityState.confirmed_mention and decision.verdict.name == "YES" else 0)
    quality = max(0, min(10000, quality))
    return {
        "industry_key": canon_industry,   # 受控 ID(非自由文本)
        "prompt_family_key": family_key,
        "prompt_intent": intent.value,
        "is_branded_prompt": branded,
        "platform_key": event["platform_key"],
        "provider_key": event["provider_key"],
        "model_key": event["model_key"],
        "model_revision": event.get("model_revision"),
        "surface_key": event["surface_key"],
        "search_provider": event.get("search_provider"),
        "response_status": ResponseStatus.answered.value,
        "target_outcome": outcome.value,
        # 🔴 [CUR-08 · 防御型 GEO WP1] 恒 None 是**有意的**,不是忘了填。
        #   规格 §2.2 CUR-08:现役没有 rank producer,也没有 rank-eligible 分母
        #   (§6.3 `position_eligible_samples` = "回答明确给出有序候选列表且可稳定解析 rank"),
        #   两者都不存在时任何非 None 值都只能是猜的。
        #   §19 变异 30「所有回答强算 Top3/Top5」必红 MET-03 —— 换句话说,
        #   **禁止**从 is_recommended / 出现行号 / 提及顺序反推名次。
        #   这一格要变成真值,必须先有:① 有序候选列表解析器 ② rank-eligible 分母登记
        #   ③ 对应 MET 判据。在那之前 UI 侧不显示 TopN(WP1 退出条件)。
        "target_position": RANK_NOT_MEASURED,
        "sentiment": privacy.derive_sentiment(obs.answer_text).value,
        "competitor_count": int(getattr(obs, "competitor_count", 0) or 0),
        "source_domains": domains,
        "citation_count": len(obs.citations or []),
        "source_count": len(domains),
        "search_query_count": None,
        "search_query_theme_keys": [],
        "quality_score_bps": quality,
        "base_weight_bps": src_weight,
        "effective_weight_bps": src_weight if won_vote else 0,   # 败票=稳定度样本,不加客户权重(§6.3)
        "confidence_bps": confidence,
        "observed_at": event["observed_at"],
    }


def commit_decision(
    cur,
    event: dict,
    entity_state: EntityState,
    decision,
    outcome: TargetOutcome,
    gate,
    obs,
    ctx: DecisionContext,
    policy_doc: dict,
    *,
    request_id: str,
    governance_in_txn: bool = False,
) -> dict:
    """在给定短事务(cur)内提交决策 + lease CAS + 审计。返回 {state, reasons}。

    调用前 event 必须仍是本 worker 持 lease 的 'processing' 行;finish CAS 失败=零写(旧 worker 复活)。

    P1-2:governance_in_txn=True(生产路径)时,在本事务内 `SELECT ... FOR SHARE` 重读晋升治理 →
      与管理员关闸/撤依据的 `FOR UPDATE`(update_policy/update_promotion_governance/record_gold_evaluation)串行化。
      关闸请求一旦返回(提交),任何后续晋升读到的都是关后的值 → 不会再用旧治理快照晋升在途信号。
      gold_gate 仍取 ctx(Phase-2 值,与是否已调 LLM outcome 一致);测试可传显式 ctx 走确定性决策。
    """
    lease = event["lease_token"]
    event_id = event["id"]

    # ── Phase-3 起点:同一事务内 FOR UPDATE 锁定并校验本 worker 仍持 lease(P1)──
    #   模型返回后到此的窗口若阻塞过租约 → 另一 worker 可能已重领;此处 CAS 失败即零写返回,整个提交
    #   (治理读 / signal / finish / audit)全部在持本行锁的同一事务内,期间其他 worker 的 claim
    #   (FOR UPDATE SKIP LOCKED)会跳过本行 → 提交期间不可能被重领并重复处理/重复付费。
    if not repository.lock_lease_for_commit(cur, event_id, lease):
        raise _LeaseLost(event_id)   # 租约已丢(被重领/过期)→ 抛出使 get_db 事务回滚,零写

    # ── 治理值来源:生产在本事务内 FOR SHARE 重读(消除 TOCTOU 穿闸);测试注入 ctx 走确定性 ──
    if governance_in_txn:
        cur.execute("""SELECT policy_json, promotion_legal_basis, consent_policy_version, outcome_gold_gate_passed
                         FROM public.geo_observation_policy WHERE singleton_id=1 FOR SHARE""")
        grow = cur.fetchone()
        _pj = grow["policy_json"] or {}
        _flags = dict(_pj.get("feature_flags", {}))
        _env = policy_mod.env_overridden_flags()
        promo_enabled = bool(_env.get("promotion_enabled", _flags.get("promotion_enabled", False)))
        legal_basis = grow["promotion_legal_basis"]
        consent = grow["consent_policy_version"]
        retention_until = datetime.now(timezone.utc) + timedelta(days=int(_pj.get("retention_days", 365)))
        policy_doc = _pj   # 权重也用同一 FOR SHARE 快照,与治理判定同源一致
        # 金标准门 AND 语义:Phase-2 值(是否已调 certified LLM)AND 提交前 in-txn 值(管理员是否已关门)。
        #   FALSE→TRUE(门刚开):Phase-2=False→未调 LLM→outcome=ambiguous→本就 pending_review,AND 亦 False,一致;
        #   TRUE→FALSE(门刚关):in-txn=False→AND=False→confirmed_mention 推荐落 pending_review,闭合 gold TOCTOU。
        gold_gate_passed = bool(ctx.outcome_gold_gate_passed) and bool(grow["outcome_gold_gate_passed"])
    else:
        promo_enabled = ctx.promotion_enabled
        legal_basis = ctx.promotion_legal_basis
        consent = ctx.consent_policy_version
        retention_until = ctx.retention_until
        gold_gate_passed = ctx.outcome_gold_gate_passed

    # 源已不可登记(退款/阻断/变更)→ reject
    if gate is None or not gate.registerable or obs is None:
        reasons = ["source_no_longer_registerable"] + (gate.reasons if gate else [])
        ok = repository.finish_event(cur, event_id, lease, "rejected", rejection_codes=reasons,
                                     source_terminal_state=getattr(gate, "terminal_state", None),
                                     retention_until=retention_until)
        if ok:
            write_audit(cur, "reject", "system", event_id=event_id, reason_codes=reasons,
                        request_id=request_id, idempotency_token="reject")
        return {"state": "rejected" if ok else "lease_lost", "reasons": reasons}

    reasons: list[str] = list(gate.reasons)
    is_research = event["source_type"] == "research_round"
    legal_ok = bool(promo_enabled and legal_basis)

    # ── 决策树(晋升治理:法务门 → 源资格 → 实体 → 金标准门 → 人工队列)──
    if not legal_ok:
        target = "private_only"                       # R11:未批准依据/晋升关 → 私域
        reasons.append("promotion_disabled" if not promo_enabled else "legal_basis_not_approved")
    elif not gate.promotable:
        target = "private_only"                       # 源严格资格不足(R1/R2/R3 证明不全)
    elif is_research:
        target = "promoted"                           # 调研行业级(无目标品牌 outcome,金标准门不适用)
    elif entity_state is EntityState.confirmed_non_mention:
        target = "promoted"                           # not_mentioned 硬轴(确定性,不需金标准门)
    elif entity_state is EntityState.confirmed_mention:
        # 推荐级别必须来自已验证金标准的官方 LLM outcome;门未过 / llm_outcome 无效 → 人工 pending_review(绝不用关键词启发式自动晋升)
        if gold_gate_passed and outcome in _RECOMMENDATION_OUTCOMES:
            target = "promoted"
        else:
            target = "pending_review"
            reasons.append("outcome_gold_gate_not_passed" if not gold_gate_passed else "llm_outcome_unavailable")
    else:
        # ambiguous / unknown / provider_error → 人工样本治理队列(不 private_only,绝不当 not_mentioned)
        target = "pending_review"
        reasons.append(f"entity_{entity_state.value if entity_state else 'none'}")

    if target != "promoted":
        ok = repository.finish_event(cur, event_id, lease, target, rejection_codes=reasons,
                                     source_terminal_state=gate.terminal_state,
                                     consent_policy_version=consent,
                                     retention_until=retention_until)
        if ok:
            write_audit(cur, target, "system", event_id=event_id, reason_codes=reasons,
                        after={"entity_state": entity_state.value if entity_state else None, "outcome": outcome.value},
                        request_id=request_id, idempotency_token=target)
        return {"state": target if ok else "lease_lost", "reasons": reasons}

    # ── promoted 前置:行业受控字典门(P1-3)——自由文本/未知行业(可能含客户名/项目名)不得进公共层
    canon_industry = privacy.canonical_industry(event.get("industry_key"))
    if canon_industry is None:
        reasons.append("industry_not_in_allowlist")
        ok = repository.finish_event(cur, event_id, lease, "private_only", rejection_codes=reasons,
                                     source_terminal_state=gate.terminal_state,
                                     consent_policy_version=consent,
                                     retention_until=retention_until)
        if ok:
            write_audit(cur, "private_only", "system", event_id=event_id, reason_codes=reasons,
                        request_id=request_id, idempotency_token="private_only")
        return {"state": "private_only" if ok else "lease_lost", "reasons": reasons}

    # ── promoted:HMAC 桶(R7 fail-closed)→ signal → cap → finish CAS → audit(全同事务)──
    owner_id = event.get("owner_user_id")
    brand_id = event.get("brand_id")
    contribution_day = (event["observed_at"].date() if hasattr(event["observed_at"], "date")
                        else date.today())
    try:
        ub = hmac_buckets.user_bucket(owner_id) if owner_id is not None else None
        bb = hmac_buckets.brand_bucket(brand_id) if brand_id is not None else None
    except hmac_buckets.HmacKeyUnavailable:
        # R7:缺 HMAC → 公共晋升 fail-closed → private_only(私域仍保留)
        reasons.append("hmac_key_unavailable")
        ok = repository.finish_event(cur, event_id, lease, "private_only", rejection_codes=reasons,
                                     source_terminal_state=gate.terminal_state,
                                     consent_policy_version=consent,
                                     retention_until=retention_until)
        if ok:
            write_audit(cur, "private_only", "system", event_id=event_id, reason_codes=reasons,
                        request_id=request_id, idempotency_token="private_only")
        return {"state": "private_only" if ok else "lease_lost", "reasons": reasons}

    # 先试赢票(5 元组唯一门,R6);赢=独立票,败=稳定度样本(effective_weight=0)。family_key 用受控行业 ID。
    family_key = privacy.anonymize_prompt_family_key(
        obs.question_text, list(getattr(obs, "brand_names", []) or []), canon_industry)
    if is_research:
        # 公共调研:无客户归属,不占客户票/不建桶,满权重贡献(不受客户 cap 约束)
        won_vote = True
    elif ub is not None and bb is not None:
        # 客户来源(诊断/监测):5 元组唯一门(R6)。赢=独立票+满权重;败=稳定度样本 effective=0
        won_vote = repository.insert_contributor_bucket(
            cur, event_id, ub, bb, family_key, event["platform_key"], contribution_day,
            bucket_key_version=hmac_buckets.BUCKET_KEY_VERSION)
    else:
        # 客户来源缺 owner/brand 归属 → 无法执行 R6 防刷门 → 公共晋升 fail-closed → private_only
        reasons.append("missing_contributor_attribution")
        ok = repository.finish_event(cur, event_id, lease, "private_only", rejection_codes=reasons,
                                     source_terminal_state=gate.terminal_state,
                                     consent_policy_version=consent,
                                     retention_until=retention_until)
        if ok:
            write_audit(cur, "private_only", "system", event_id=event_id, reason_codes=reasons,
                        request_id=request_id, idempotency_token="private_only")
        return {"state": "private_only" if ok else "lease_lost", "reasons": reasons}

    signal = _build_signal(event, obs, gate, entity_state, decision, outcome, policy_doc, won_vote, canon_industry)
    privacy.assert_signal_clean(signal)   # 隐私硬门(带参 URL/PII/禁字段 → 抛)
    signal_inserted = repository.insert_signal(cur, event_id, signal)

    ok = repository.finish_event(cur, event_id, lease, "promoted", rejection_codes=reasons,
                                 source_terminal_state=gate.terminal_state,
                                 consent_policy_version=consent,
                                 promotion_legal_basis=legal_basis,
                                 retention_until=retention_until)
    if not ok:
        # lease 丢失 → 整事务应回滚(调用方 get_db 见异常回滚);此处主动抛以确保零写
        raise _LeaseLost(event_id)
    write_audit(cur, "promote", "system", event_id=event_id,
                after={"outcome": outcome.value, "won_vote": won_vote, "signal_inserted": signal_inserted},
                evidence={
                    "model": "deepseek-v4-flash",
                    "entity_method": getattr(decision, "method", None),
                    "entity_prompt_version": ENTITY_PROMPT_VERSION,
                    "outcome_prompt_version": OUTCOME_PROMPT_VERSION if entity_state is EntityState.confirmed_mention else None,
                    "schema_version": VERIFIER_SCHEMA_VERSION,
                    "evidence_hash": event.get("answer_hash"),
                },
                reason_codes=reasons, request_id=request_id, idempotency_token="promote")
    return {"state": "promoted", "reasons": reasons, "won_vote": won_vote, "signal_inserted": signal_inserted}


async def _lease_heartbeat_loop(event_id: int, lease_token: str, lease_seconds: int) -> None:
    """Phase-2 伴飞续租(best-effort 保活):领取后**立即**续一次(首轮不 sleep),再每 ~lease_seconds/3 续一次,
    使非阻塞长 await(LLM)期间租约不过期。

    正确性**不依赖本 loop**——由每次付费模型调用前的主动 CAS(`_renew_lease_or_lost`)+ Phase-3 finish CAS 双重保证:
    伴飞尚未跑首轮 / 事件循环被同步源读阻塞时,本 loop 无法续租,但调用前的主动 CAS 会检出租约已丢并停止。
    因此本 loop 失败静默退出(绝不误停主流程、绝不 raise)。`to_thread` 卸载同步 DB commit,不阻塞 event loop。
    """
    interval = max(2, lease_seconds // 3)

    def _hb() -> bool:
        with get_db() as conn:
            return repository.heartbeat_event(conn.cursor(), event_id, lease_token, lease_seconds)

    while True:
        try:
            ok = await asyncio.to_thread(_hb)   # 首轮立即续租(P1:不能先 sleep 到 interval 才第一次续)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 瞬时 DB 异常:保活忽略继续(正确性靠调用前主动 CAS 兜底)
            logger.warning("event %s 伴飞续租异常(忽略,靠调用前主动 CAS 保正确): %s", event_id, exc)
            ok = True
        if not ok:
            return   # token 不匹配=租约确已丢 → 停保活(主流程调用前主动 CAS + finish CAS 会检出并零写)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


async def _renew_lease_or_lost(event_id: int, lease_token: str, lease_seconds: int) -> bool:
    """付费模型调用前**主动** CAS 续租+验证租约(P1:不能只被动读伴飞 set 的 Event —— 伴飞首轮未跑 /
    事件循环被同步源读阻塞期间,被动标志仍为 false,旧 worker 会照常进入付费调用造成双花)。

    以本 worker 的 lease_token 做 CAS:命中(仍持租约)→ 同步续租并返回 True;未命中(被重领/过期)/ DB 异常
    → 返回 False,主流程必须立即停止,**不调付费模型、不提交**(fail-closed 防重复扣费)。
    """
    def _cas() -> bool:
        with get_db() as conn:
            return repository.heartbeat_event(conn.cursor(), event_id, lease_token, lease_seconds)

    try:
        return await asyncio.to_thread(_cas)
    except Exception as exc:  # noqa: BLE001 CAS 异常保守当租约丢(fail-closed)
        logger.warning("event %s 付费调用前主动续租异常,保守停止: %s", event_id, exc)
        return False


def _make_lease_guard(event_id: int, lease_token: str, lease_seconds: int):
    """产出注入 verifier/outcome-classifier 的租约守卫,在**每一次真实 provider POST 前**调用(权威边界)。

    - 主动 CAS 续租(命中=仍持租约);未命中(被重领/过期)/DB 异常 → raise LeaseLost 阻断该次真实付费调用。
    - **首次**付费 POST 前于同一 CAS 事务持久化 `paid_call_started_at`(付费幂等锚):一旦发起付费调用即留
      耐久标记 → kill-9/租约丢失后被重领的 worker 据此路由人工,绝不自动再次付费(见 process_next_pending 重领路由)。
    - 标记在 POST **之前**落库:mark 与 POST 之间死亡 → 未花钱但保守转人工(安全侧);POST 期间死亡 → 已花钱且
      标记已在 → 重领转人工不重付。二者都不重复付费。跨事件单例(verifier POST 与 outcome POST 共享,只标一次)。
    """
    state = {"paid_marked": False}

    def _cas(mark_paid: bool) -> bool:
        with get_db() as conn:
            return repository.heartbeat_event(conn.cursor(), event_id, lease_token, lease_seconds,
                                              mark_paid_call=mark_paid)

    async def _guard() -> None:
        first = not state["paid_marked"]
        try:
            ok = await asyncio.to_thread(_cas, first)
        except Exception as exc:  # noqa: BLE001 CAS 异常保守当租约丢(fail-closed 防重复扣费)
            logger.warning("event %s 付费 POST 前守卫续租异常,保守阻断: %s", event_id, exc)
            ok = False
        if not ok:
            raise LeaseLost(event_id)   # BaseException:穿透 resolver/verifier 宽 except → promotion 显式捕获零写
        if first:
            state["paid_marked"] = True

    return _guard


async def process_next_pending(
    *,
    verifier: Optional[Callable] = None,
    outcome_classifier: Optional[Callable] = None,
    ctx: Optional[DecisionContext] = None,
    lease_seconds: int = repository.DEFAULT_LEASE_SECONDS,
) -> Optional[dict]:
    """领取并处理一条 pending event。返回决策结果或 None(无待处理)。绝不 raise(单条毒性事件不中断批)。

    Phase1 claim(短事务)→ Phase2 只读源+LLM(事务外)→ Phase3 决策提交(短事务+lease CAS)。
    ctx=None 时每次从 policy SSOT 读新鲜治理(P1-1);测试可显式覆盖 ctx。
    """
    # Phase 1: claim
    with get_db() as conn:
        claimed = repository.claim_next_event(conn.cursor(), lease_seconds=lease_seconds)
    if not claimed:
        return None
    event_id = claimed["id"]
    lease = claimed["lease_token"]
    request_id = f"promote:{event_id}:{lease}"

    # P1(付费幂等状态机):上个 worker 已发起过真实付费 provider 调用(paid_call_started_at 非空)但事件被重领
    #   —— kill-9 / 租约丢失,**外部调用结果未知**。绝不自动再次付费猜测重试 → 路由人工 pending_review
    #   (paid_call_result_unknown)。lease CAS 保证只有当前持租约者写终态;又被抢则留 processing,下一个重领者
    #   同样命中本分支(仍不会自动付费)。此判定必须先于毒性熔断/任何 LLM,是防重复扣费的耐久锚。
    if claimed.get("paid_call_started_at") is not None:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                if repository.finish_event(cur, event_id, lease, "pending_review",
                                           rejection_codes=["paid_call_result_unknown"]):
                    write_audit(cur, "pending_review", "system", event_id=event_id,
                                reason_codes=["paid_call_result_unknown"],
                                request_id=request_id, idempotency_token="paid_call_result_unknown")
            return {"event_id": event_id, "state": "pending_review", "reasons": ["paid_call_result_unknown"]}
        except Exception as exc:  # noqa: BLE001 终态写失败(瞬时 DB)不中断批;留 processing 待重领再判(仍不自动付费)
            logger.error("event %s 付费未知态终态写失败(留待重领): %s", event_id, exc, exc_info=True)
            return {"event_id": event_id, "state": "exception", "error": str(exc)[:200]}

    # 毒性事件熔断(P1-7):超重试上限 → error 终态,不再 poison-loop / 反复烧 LLM。
    # 已领取(processing+本 worker lease)后绝不 raise:终态写若遇瞬时 DB 故障也隔离 →
    #   事务回滚留 processing,lease 过期后重领 → 熔断器再判 error(LLM 在 Phase 2 之前,零重烧),批不中断。
    if int(claimed.get("attempts") or 0) > MAX_PROMOTION_ATTEMPTS:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                if repository.finish_event(cur, event_id, lease, "error", rejection_codes=["max_attempts_exceeded"]):
                    write_audit(cur, "error", "system", event_id=event_id, reason_codes=["max_attempts_exceeded"],
                                request_id=request_id, idempotency_token="error")
            return {"event_id": event_id, "state": "error", "reasons": ["max_attempts_exceeded"]}
        except Exception as exc:  # noqa: BLE001 熔断终态写失败(瞬时 DB)不中断批;留 processing 待重领再判 error
            logger.error("event %s 熔断终态写失败(留待重领): %s", event_id, exc, exc_info=True)
            return {"event_id": event_id, "state": "exception", "error": str(exc)[:200]}

    # P1:领取成功后**立即**起伴飞续租,覆盖 Phase 2 + Phase 3 提交全程(源读 / 付费调用 / 最终事务锁等待)。
    #   毒性熔断/付费未知态在此之前(纯 DB 写、无付费、无源读),不需伴飞。伴飞仅 best-effort 保活;
    #   正确性由**每次真实 provider POST 前的守卫 CAS**(_make_lease_guard)+ Phase-3 起点 FOR UPDATE 锁+校验保证。
    _lease_guard = _make_lease_guard(event_id, lease, lease_seconds)   # 注入 verifier/classifier:每次真实付费 POST 前 CAS+首次标记
    # P2 核账关联:每笔真实付费 POST 的 llm_track metadata 携带 event_id/request_id/call_purpose/attempt_no
    #   (attempt 为本次处理内全局递增,跨 verifier/400 fallback/outcome)→ 并发 paid_call_result_unknown 可逐笔对账。
    _track_ctx = {"event_id": event_id, "request_id": request_id, "attempt": 0}
    _hb_task = asyncio.create_task(_lease_heartbeat_loop(event_id, lease, lease_seconds))
    try:
        try:
            # 金标准门(决定是否调 LLM outcome)—— ctx 显式则用之,否则读新鲜。policy 读放 try 内:读失败被隔离,不 raise。
            if ctx is not None:
                gold_gate = ctx.outcome_gold_gate_passed
            else:
                gold_gate = bool(policy_mod.get_policy()["outcome_gold_gate_passed"])

            # Phase 2: 只读源 + 实体/outcome(事务外,不持写锁)。源读若阻塞事件循环 → 伴飞无法续 →
            #   租约可能过期被重领;下方每次付费调用前的主动 CAS 会检出并停止,防重复扣费。
            with get_db() as conn2:
                gate, obs = _reload_observation(conn2.cursor(), claimed)

            entity_state = EntityState.unknown
            decision = None
            outcome = TargetOutcome.entity_ambiguous
            is_research = claimed["source_type"] == "research_round"

            if obs is not None and is_research:
                # 公共调研:行业级,无目标品牌 → 跳过品牌实体,用调研 outcome(entity 不作为晋升门)。纯函数无付费调用。
                entity_state = None
                obs.brand_names = []
                if gate is not None:
                    gate.brand_names = []
                outcome = classify_research_outcome(ResponseStatus.answered, obs.answer_text)
            elif obs is not None:
                # 付费前粗粒度早退(避免同步身份加载再徒劳):租约已丢即停。**权威守卫在真实 POST 边界**——
                #   _lease_guard 已注入 verifier(经 resolve_entity)与 outcome-classifier,在每次真实 provider POST
                #   前(即身份同步加载之后、400 fallback 前)做 CAS,租约丢失 raise LeaseLost 阻断该次付费调用。
                if not await _renew_lease_or_lost(event_id, lease, lease_seconds):
                    raise _LeaseLost(event_id)
                entity_state, decision, identity = await resolve_entity(
                    claimed.get("brand_id"), obs.answer_text, fallback_name="",
                    verifier=verifier, lease_guard=_lease_guard, track_ctx=_track_ctx)
                try:
                    obs.brand_names = list(identity.all_trusted_names)
                except Exception:  # noqa: BLE001
                    obs.brand_names = []
                if gate is not None:
                    gate.brand_names = obs.brand_names
                outcome = classify_outcome(ResponseStatus.answered, entity_state, obs.is_detected, obs.answer_text)
                # 金标准门通过时:confirmed_mention 的推荐级别由官方 LLM 判定;失败/门未过 → 决策树判 pending_review
                if entity_state is EntityState.confirmed_mention and gold_gate:
                    if not await _renew_lease_or_lost(event_id, lease, lease_seconds):
                        raise _LeaseLost(event_id)   # 粗粒度早退;权威守卫在 classifier 内的真实 POST 前
                    clf = outcome_classifier or make_official_outcome_classifier(lease_guard=_lease_guard,
                                                                                 track_ctx=_track_ctx)
                    llm_out = await clf(obs.question_text, obs.answer_text, identity)
                    outcome = (classify_outcome(ResponseStatus.answered, entity_state, obs.is_detected,
                                                obs.answer_text, llm_outcome=llm_out)
                               if llm_out is not None else TargetOutcome.entity_ambiguous)

            # 伴飞继续覆盖 Phase 3(不在此停)—— 模型返回后到 Phase-3 取锁的窗口若阻塞过租约,伴飞保活;
            #   最终由 commit_decision 起点的 FOR UPDATE 锁+校验兜底。伴飞在外层 finally 于提交后统一收尾。
            # Phase 3:提交决策(短事务)。生产(ctx=None)在 commit_decision 内 FOR SHARE 重读治理 →
            #   与管理员关闸 FOR UPDATE 串行化(P1-2:消除"读治理→提交信号"之间的 TOCTOU 穿闸)。
            #   gold_gate 保持 Phase-2 值(与"是否已调 LLM outcome"一致,防门刚开→关键词启发式误晋升)。
            pol = policy_mod.get_policy()["policy"]      # 仅测试 ctx 路径用作 policy_doc;生产路径 commit_decision 会用同事务快照覆盖
            governance_in_txn = ctx is None
            if ctx is not None:
                decision_ctx = ctx
            else:
                # 生产:governance 字段由 commit_decision 在同事务 FOR SHARE 重读覆盖;此处只承载 gold_gate(Phase-2 值)
                decision_ctx = DecisionContext(
                    promotion_enabled=False, promotion_legal_basis=None, consent_policy_version=None,
                    retention_until=None, outcome_gold_gate_passed=gold_gate,
                )
            with get_db() as conn3:
                cur = conn3.cursor()
                result = commit_decision(cur, claimed, entity_state, decision, outcome, gate, obs, decision_ctx, pol,
                                         request_id=request_id, governance_in_txn=governance_in_txn)
            return {"event_id": event_id, **result}
        except _LeaseLost:
            logger.warning("event %s lease lost before paid model/commit → zero write", event_id)
            return {"event_id": event_id, "state": "lease_lost"}
        except ProviderResultUnknown as exc:
            # 付费 provider 请求已发出但响应未知(ReadTimeout 等,可能已计费)→ 绝不自动重发/晋升 → 转人工。
            #   耐久锚 paid_call_started_at 已在该次 POST 前落库(伴飞守卫);此处以本 worker lease 直接写终态。
            logger.warning("event %s 付费响应未知(%s)→ pending_review 转人工零晋升", event_id, exc)
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    if repository.finish_event(cur, event_id, lease, "pending_review",
                                               rejection_codes=["paid_call_result_unknown"]):
                        write_audit(cur, "pending_review", "system", event_id=event_id,
                                    reason_codes=["paid_call_result_unknown"], request_id=request_id,
                                    idempotency_token="paid_call_result_unknown")
                return {"event_id": event_id, "state": "pending_review", "reasons": ["paid_call_result_unknown"]}
            except Exception as e2:  # noqa: BLE001 终态写失败(瞬时 DB)不中断批;留 processing,锚非空 → 重领仍转人工
                logger.error("event %s 付费未知态终态写失败(留待重领,锚已在): %s", event_id, e2, exc_info=True)
                return {"event_id": event_id, "state": "exception", "error": str(e2)[:200]}
        except Exception as exc:  # noqa: BLE001 单条毒性事件不中断批;event 留 processing,lease 过期后重领,attempts++ → 终 error
            logger.error("event %s 处理异常(留待重试/熔断): %s", event_id, exc, exc_info=True)
            return {"event_id": event_id, "state": "exception", "error": str(exc)[:200]}
    finally:
        _hb_task.cancel()   # 安全网:异常路径也停伴飞(已停则为幂等 no-op)
        try:
            await _hb_task
        except asyncio.CancelledError:
            pass
