"""品牌实体审核(复用 BrandIdentityResolver + 注入官方 DeepSeek verifier)+ outcome 判定。

R5(Q2)裁定:
- 向 resolver 注入官方 deepseek-v4-flash verifier(base_url=api.deepseek.com,DEEPSEEK_API_KEY,thinking=disabled);
  诊断与 GEO 观测共享该官方 transport，调用目的、超时和并发边界由调用方显式指定。
- json_object 输出后必须强类型 Pydantic 校验(VerifierOutput);格式/字段/位置错 → provider_error/UNKNOWN;
- 禁 400 后降级弱输出并自动晋升(可去 response_format 重试,但不可信输出一律 UNKNOWN)。

最小化(spec §8.2):发给 LLM 的 body 只含 目标品牌名 + 批准别名 + 行业 + 命中位置最小证据窗口(已 PII 清洗);
禁 owner/user/agent ID、手机、联系人、上游关系、成本、完整自定义问题、完整答案、其它租户证据。
测试拦截真实 request body 断言禁字段/非必要全文=0。

resolver 自身已把 YES 的 matched_text/window/start/end 位置不一致 → 降级 UNKNOWN,故本 verifier 只需失败即 UNKNOWN。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import logging
import re
from typing import Awaitable, Callable, Optional, Sequence

from pydantic import BaseModel, ConfigDict, field_validator

from services.brand_identity_resolver import (
    BrandDecision,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
    build_evidence_windows,
)
from .contracts import EntityState, PromptIntent, ResponseStatus, TargetOutcome, VerifierOutput
from . import privacy

logger = logging.getLogger("GEO-ObservationEntity")

DEEPSEEK_OFFICIAL_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = DEEPSEEK_OFFICIAL_FLASH

# 语义调用可追溯版本(P1-7 trace):随 prompt/schema 变更递增
ENTITY_PROMPT_VERSION = "geo-obs-entity-v1"
OUTCOME_PROMPT_VERSION = "geo-obs-outcome-v1"
VERIFIER_SCHEMA_VERSION = "verifier-out-v1"
_RECOMMENDATION_LEVELS = ("recommended", "conditionally_recommended", "candidate_only", "mentioned_only")

# provider/transport 失败 reason → provider_error(否则 UNKNOWN 归 ambiguous)
_PROVIDER_ERR_HINTS = ("timeout", "http_", "parse_or_transport_error", "verifier_unavailable",
                       "verifier_exception", "transport", "unavailable", "provider_error",
                       "provider_not_sent")

_RISK_WORDS = ("投诉", "风险", "被骗", "维权", "纠纷", "违规", "处罚", "医疗", "金融风险", "诈骗")
_CRITERIA_WORDS = ("筛选", "标准", "建议根据", "可以从", "考察", "参考以下", "如何选择", "选择时")
_LIST_WORDS = ("以下几家", "以下品牌", "包括", "例如", "如：", "候选", "名单", "备选")


PostFn = Callable[[dict], Awaitable[object]]
LeaseGuard = Callable[[], Awaitable[None]]   # 每次真实 provider POST 前调用;租约丢失 → raise LeaseLost


class LeaseLost(BaseException):
    """租约已丢(被重领/过期)。付费 provider POST 前的守卫检出即 raise,**在真实付费请求发出之前**中止。

    **刻意继承 BaseException(非 Exception)**——纵深防御,让守卫信号直接、即时上抛:
    `BrandIdentityResolver.resolve()` 对 verifier 调用有宽 `except Exception → UNKNOWN`
    (brand_identity_resolver.py:597,红线不可改)。若 LeaseLost 是 Exception,verifier 路径的守卫信号会被
    "洗白"成 UNKNOWN→provider_error→engine_error(非 confirmed_mention),再靠下游 Phase-3 FOR UPDATE 锁兜底
    才零写——迂回且依赖 backstop。作为 BaseException 它穿透所有 `except Exception`,只被 promotion 显式
    `except LeaseLost` 捕获,**直接**上抛为 lease_lost,不经 provider_error 迂回、不依赖 Phase-3 兜底。
    注:两种情况都**不会**重复付费(守卫在 POST 前 raise;outcome 分支另有 verifier→UNKNOWN 不达 confirmed_mention
    的天然门 + Phase-3 锁 + paid_call_started_at 耐久锚多重兜底),BaseException 的价值是"直接即时 + 不依赖兜底"。
    该不变量由 test_guard_leaselost_not_swallowed_by_resolver 锁定(翻回 Exception 该测试即失败)。"""


def _guard_post(post: PostFn, lease_guard: Optional[LeaseGuard]) -> PostFn:
    """把 lease_guard 织入 post:**每一次**真实 provider POST 前先 await 守卫(租约丢失即 raise LeaseLost,
    不发起该次付费请求)。覆盖主 POST + 400 去 response_format 的第二次 POST(两处都经本包装)。
    lease_guard=None → 原样返回(测试注入无 POST 的 verifier / 生产未传守卫时)。"""
    if lease_guard is None:
        return post

    async def _guarded(body: dict):
        await lease_guard()   # 租约丢失 → raise LeaseLost(在真实 POST 之前)
        return await post(body)

    return _guarded


DEEPSEEK_POOL_ROLE = "normal"


class ProviderResultUnknown(BaseException):
    """付费 provider 请求**已发出但响应未知**(ReadTimeout / 连接读中断等 —— 服务端可能已处理并计费)。
    **绝不自动换 key 重发**(会二次计费)。作为 BaseException 穿透所有宽 `except Exception`,由 promotion 显式
    捕获 → 路由 `pending_review(paid_call_result_unknown)`,转人工:不晋升、不重付。"""


class _ProviderNotSent(Exception):
    """付费 provider 请求**确定未发出**(无 key / client 初始化 / 全 key 连接建立失败 → **未计费**)。
    普通 Exception:被 verifier/classifier 的宽 except 归为 UNKNOWN/None(transport 失败),
    **不转 paid_call_result_unknown**。
    锚说明:无 key / client 初始化(在守卫**之前**)→ 锚未落;若走到 ConnectError(守卫已在该 key 前跑过一次)→
    锚可能已落但事件随后以 UNKNOWN 正常终态(非重领),锚仅在被重领时才驱动人工路由,故对未计费事件无害
    (mark-before-post 的保守代价:ConnectError+紧接 kill-9 极端下会多一次人工复核,绝不双付)。"""


async def _paid_safe_deepseek_post(body: dict, *, lease_guard: Optional[LeaseGuard] = None,
                                   role: str = DEEPSEEK_POOL_ROLE, timeout: float = 60.0,
                                   url: str = DEEPSEEK_OFFICIAL_URL,
                                   track_ctx: Optional[dict] = None,
                                   call_purpose: str = "entity_verify"):
    """付费安全的官方 DeepSeek POST(观测付费调用专用)。替代共享 `adeepseek_post_with_failover` 的**无差别 failover**
    (后者对 ReadTimeout 也换 key 重发 → 二次计费)。付费幂等边界紧贴 `httpx.AsyncClient.post`:

    - **lease CAS + 耐久锚 `lease_guard` 紧贴每次 httpx.post 之前**:租约丢失即 raise LeaseLost 阻断该次付费;
      首次 POST 前于同 CAS 事务落 `paid_call_started_at`(kill-9 后被重领→转人工)。
    - **响应未知**(ReadTimeout/ReadError/RemoteProtocolError/PoolTimeout —— 可能已计费)→ **不换 key 重发**,
      raise `ProviderResultUnknown` → 转人工。
    - **确定未发出**(ConnectError/ConnectTimeout —— 未计费)→ 换下一个 key 重试(仍逐次守卫)。
    - **收到任意 HTTP 响应**(含非 200)→ 确定性,原样返回,由调用方处理(400 fallback / 非 200 → UNKNOWN)。
    - **无 key / client 初始化失败**(在守卫之前)→ raise `_ProviderNotSent`,不落耐久锚。
    - 每笔真实 httpx.post 经 `llm_track` 记账(caller=geo_observation_entity;记账自身失败不阻塞,见 tracker 兜底)。
      **人工核账关联**(P2):metadata 携带非敏感 `event_id / request_id / call_purpose / attempt_no`
      (attempt_no 为同一 event 处理内所有真实 POST 的全局递增序号,跨 verifier/400 fallback/outcome);
      响应未知的那笔在**同一行**标 `provider_result_unknown=true` —— 并发多事件转人工时可与成本日志逐笔精确对账。

    track_ctx:promotion 每次处理创建的可变 dict {"event_id","request_id","attempt":0};None(测试直调)→ 本地计数。
    """
    import httpx
    from tools.llm_call_tracker import llm_track, usage_from_response_payload
    from services.llm.deepseek_key_pool import get_deepseek_api_keys

    keys = get_deepseek_api_keys(role)
    if not keys:
        raise _ProviderNotSent("no_deepseek_key")   # 守卫未跑 → 无耐久锚(req#4/#6)

    _SENT_UNKNOWN = (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError,
                     httpx.RemoteProtocolError, httpx.PoolTimeout)
    _NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)
    ctx = track_ctx if track_ctx is not None else {"attempt": 0}

    async def _one_post(client, headers):
        """单笔真实 httpx.post + llm_track 记账。httpx 异常向上抛(由调用方按已发出/未发出分类),记账写 failure。"""
        ctx["attempt"] = int(ctx.get("attempt") or 0) + 1
        meta = {"call_purpose": call_purpose, "attempt_no": ctx["attempt"]}
        if ctx.get("event_id") is not None:
            meta["event_id"] = ctx["event_id"]
        if ctx.get("request_id"):
            meta["request_id"] = ctx["request_id"]
        async with llm_track("geo_observation_entity", "deepseek", model=body.get("model"), metadata=meta) as tracker:
            try:
                r = await client.post(url, headers=headers, json=body)
            except _SENT_UNKNOWN:
                # 响应未知(可能已计费)→ 在**同一记账行**标记,供人工按 event_id/attempt_no 精确对账
                try:
                    if isinstance(getattr(tracker, "metadata", None), dict):
                        tracker.metadata["provider_result_unknown"] = True   # tracker.metadata 才是落库的合并 dict
                except Exception:  # noqa: BLE001 标记失败不影响主异常传播
                    pass
                raise
            try:
                if r.status_code == 200:
                    it, ot, ct = usage_from_response_payload(r.json())
                    tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
                else:
                    tracker.record(success=False, error_msg=f"HTTP {r.status_code}")
            except Exception:  # noqa: BLE001 记账解析失败不影响主流程/返回值
                tracker.record(success=(r.status_code == 200))
            return r

    last_not_sent: Optional[Exception] = None
    for key in keys:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            client = httpx.AsyncClient(timeout=timeout)   # client 初始化失败 → 守卫前 → 不写锚
        except Exception as e:  # noqa: BLE001
            raise _ProviderNotSent(f"client_init:{type(e).__name__}")
        try:
            if lease_guard is not None:
                await lease_guard()   # req#1:CAS + 耐久锚,紧贴 httpx.post 之前(LeaseLost 阻断该次付费)
            try:
                resp = await _one_post(client, headers)
            except _NOT_SENT as e:
                last_not_sent = e     # req#3:确定未发出 → 换下一个 key(未计费)
                continue
            except _SENT_UNKNOWN as e:
                raise ProviderResultUnknown(f"{type(e).__name__}")   # req#2:响应未知 → 不换 key,转人工
            return resp               # 收到响应(任意状态)→ 确定性,交调用方
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    raise _ProviderNotSent(str(last_not_sent) if last_not_sent else "all_keys_unreachable")


def _build_minimized_body(identity, evidence_windows: Sequence[str], *, response_format: bool = True) -> dict:
    """构造最小化 request body。只含可信名/行业/证据窗口(已 PII 清洗)。"""
    names = "、".join(identity.all_trusted_names)
    cleaned = [privacy.scrub_pii(w)[:1200] for w in evidence_windows]   # 去 PII + 限长
    numbered = "\n\n".join(f"【证据窗口 {i}】\n{w}" for i, w in enumerate(cleaned, 1))
    prompt = f"""你是品牌身份核验器。只判断证据是否提到同一个真实品牌实体。
【可信身份】{names}
【行业】{identity.industry or '未提供'}
规则:官方全称/已确认简称/英文名/仅一字错写可 YES;同行业后缀相同但专有前缀不同必须 NO(如晨光富士电梯 vs 江苏富士电梯);
不能仅凭行业后缀判定;证据不足为 NO;模型自身无法判断才 UNKNOWN。
YES 时 matched_text 逐字复制证据命中文字并给出窗口编号与该窗口内 0-based start/end(end 不含末字符);NO/UNKNOWN 时 matched_text 空串、位置 null。
{numbered}
仅返回 JSON:{{"verdict":"YES|NO|UNKNOWN","reason":"<=40字","matched_text":"","window_index":null,"matched_start":null,"matched_end":null}}"""
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 160,
        "temperature": 0,
        # 官方 Flash 档关思考(get_thinking_disabled_params 语义)。
        # 🔴 这个字段是**官方线专用**:百炼那边叫 enable_thinking,键都不一样。
        "thinking": {"type": "disabled"},
    }
    if response_format:
        body["response_format"] = {"type": "json_object"}
    return body


def _parse_json_content(raw: str) -> dict:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    return json.loads(s)


def _build_post(post_fn: Optional[PostFn], lease_guard: Optional[LeaseGuard], *,
                track_ctx: Optional[dict] = None, call_purpose: str = "entity_verify",
                timeout: float = 60.0) -> PostFn:
    """选择 post 通道:
    - 生产(post_fn=None)→ `_paid_safe_deepseek_post`,守卫**紧贴每次内部 httpx.post**(含多 key failover 的每一笔),
      并携带核账关联 metadata(track_ctx + call_purpose);
    - 测试注入 post_fn(无内部 httpx failover 的 fake)→ `_guard_post` 粗粒度包装(守卫在该 fake 之前)。"""
    if post_fn is not None:
        return _guard_post(post_fn, lease_guard)

    async def _post(body: dict):
        return await _paid_safe_deepseek_post(body, lease_guard=lease_guard, role=DEEPSEEK_POOL_ROLE,
                                              track_ctx=track_ctx, call_purpose=call_purpose,
                                              timeout=timeout, url=DEEPSEEK_OFFICIAL_URL)

    return _post


def make_official_verifier(post_fn: Optional[PostFn] = None, *, lease_guard: Optional[LeaseGuard] = None,
                           track_ctx: Optional[dict] = None, timeout: float = 60.0,
                           call_purpose: str = "entity_verify"):
    """产出注入 resolver 的官方 verifier。post_fn 可注入(测试拦截 request body / 生产走官方池)。
    lease_guard:每次真实 provider POST(含 400 fallback + 多 key failover 的每一笔)前的租约守卫,
    丢失即 raise LeaseLost 阻断付费调用;响应未知则 raise ProviderResultUnknown(不换 key 重发)。
    track_ctx:核账关联(event_id/request_id/attempt 计数),写入每笔 llm_track metadata。"""
    post = _build_post(
        post_fn,
        lease_guard,
        track_ctx=track_ctx,
        call_purpose=call_purpose,
        timeout=timeout,
    )

    async def _verifier(*, identity, evidence_windows: Sequence[str]) -> VerificationResult:
        if not evidence_windows:
            return VerificationResult(BrandVerdict.UNKNOWN, "no_evidence")
        try:
            body = _build_minimized_body(identity, evidence_windows, response_format=True)
            resp = await post(body)
            status = getattr(resp, "status_code", 200)
            if status == 400:
                # 网关拒 json 模式 → 去 response_format 重试(输出仍须强类型校验,不降级晋升)
                resp = await post(_build_minimized_body(identity, evidence_windows, response_format=False))
                status = getattr(resp, "status_code", 200)
            if status != 200:
                return VerificationResult(BrandVerdict.UNKNOWN, f"http_{status}")
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = _parse_json_content(content)
            # R5:强类型 Pydantic 校验;格式/字段错 → UNKNOWN(不自动晋升)
            out = VerifierOutput.model_validate(parsed)
        except (LeaseLost, ProviderResultUnknown):
            raise   # 租约丢失 / 响应未知 必须向上传播(绝不吞成 UNKNOWN 后继续 → 防重复付费/误晋升)
        except _ProviderNotSent as exc:
            # A request that was definitely not sent is safe for one bounded
            # identity-only retry. It must stay distinct from a malformed or
            # otherwise uncertain response, which may already have incurred cost.
            logger.warning("official deepseek verifier 未发出 brand=%s: %s", getattr(identity, "brand_id", None), type(exc).__name__)
            return VerificationResult(BrandVerdict.UNKNOWN, "provider_not_sent")
        except Exception as exc:  # noqa: BLE001 response may already be billable
            logger.warning("official deepseek verifier 失败 brand=%s: %s", getattr(identity, "brand_id", None), type(exc).__name__)
            return VerificationResult(BrandVerdict.UNKNOWN, "parse_or_transport_error")
        return VerificationResult(
            BrandVerdict(out.verdict),
            out.reason,
            matched_text=(out.matched_text or None),
            window_index=out.window_index,
            matched_start=out.matched_start,
            matched_end=out.matched_end,
        )

    return _verifier


def decision_to_entity_state(decision: BrandDecision) -> EntityState:
    if decision.verdict is BrandVerdict.YES:
        return EntityState.confirmed_mention
    if decision.verdict is BrandVerdict.NO:
        return EntityState.confirmed_non_mention
    # UNKNOWN:transport/parse 失败 → provider_error;否则(混淆/无法消歧)→ ambiguous
    reason = (decision.reason or "").lower()
    if any(h in reason for h in _PROVIDER_ERR_HINTS):
        return EntityState.provider_error
    if "identity_load_failed" in reason:
        return EntityState.unknown
    return EntityState.ambiguous


async def resolve_entity(
    brand_id: Optional[int],
    answer: str,
    *,
    fallback_name: str = "",
    fallback_display_names: Optional[Sequence[str]] = None,
    verifier: Optional[Callable] = None,
    lease_guard: Optional[LeaseGuard] = None,
    track_ctx: Optional[dict] = None,
) -> tuple[EntityState, BrandDecision, object]:
    """跑 resolver(注入官方 verifier)。返回 (entity_state, decision, identity)。

    resolver 内部已保证:YES 位置不一致/不可信 → 降级 UNKNOWN;verifier 异常 → UNKNOWN。

    lease_guard:织入官方 verifier,在真实 verifier POST 前(即 `for_brand` 同步加载品牌/别名之后)
    做租约守卫 —— 同步身份读取阻塞过租约时,守卫在真实付费 POST 前检出并 raise LeaseLost(防重复付费)。
    注:LeaseLost 不被 resolver 的 verifier-异常兜底吞没(verifier 已显式 re-raise),会向上传播到 promotion。
    track_ctx:核账关联 metadata(event_id/request_id/attempt),随每笔真实 POST 写入 llm_call_log。
    """
    resolver = BrandIdentityResolver.for_brand(
        brand_id, fallback_name=fallback_name, fallback_display_names=fallback_display_names,
        verifier=verifier or make_official_verifier(lease_guard=lease_guard, track_ctx=track_ctx),
    )
    decision = await resolver.resolve(answer)
    return decision_to_entity_state(decision), decision, resolver.identity


# ────────────────────────────── outcome 判定(确定性基线 + 可选 LLM 精化)──────────────────────────────
def classify_outcome(
    response_status: ResponseStatus,
    entity_state: EntityState,
    is_detected: Optional[bool],
    answer: str,
    *,
    prompt_intent: Optional[PromptIntent] = None,
    llm_outcome: Optional[TargetOutcome] = None,
) -> TargetOutcome:
    """十分类 outcome。硬轴确定性(engine_error/refused/entity_ambiguous/not_mentioned);

    推荐级别(recommended/conditionally/candidate/mentioned/criteria)默认确定性基线,
    llm_outcome(官方 DeepSeek 精化,金标准校准)仅可细化 confirmed_mention 的推荐级别,不得越过硬轴。
    """
    # 硬轴 1:传输/引擎错误
    if response_status in (ResponseStatus.timeout, ResponseStatus.error, ResponseStatus.budget_blocked):
        return TargetOutcome.engine_error
    if entity_state is EntityState.provider_error:
        return TargetOutcome.engine_error
    # 硬轴 2:实体歧义/未知(绝不当未提到)
    if entity_state in (EntityState.ambiguous, EntityState.unknown):
        return TargetOutcome.entity_ambiguous
    # 硬轴 3:拒答
    if response_status is ResponseStatus.refused or entity_state is EntityState.refused:
        if any(w in (answer or "") for w in _RISK_WORDS):
            return TargetOutcome.refused_risk
        return TargetOutcome.refused_no_evidence
    # 硬轴 4:确认未提及(唯一进未提及分母)
    if entity_state is EntityState.confirmed_non_mention:
        if any(w in (answer or "") for w in _CRITERIA_WORDS):
            return TargetOutcome.criteria_only
        return TargetOutcome.not_mentioned
    # 确认提及 → 推荐级别
    if entity_state is EntityState.confirmed_mention:
        if llm_outcome in (
            TargetOutcome.recommended, TargetOutcome.conditionally_recommended,
            TargetOutcome.candidate_only, TargetOutcome.mentioned_only,
        ):
            return llm_outcome
        if privacy.has_positive_recommendation(answer):
            return (TargetOutcome.conditionally_recommended
                    if privacy.has_conditional_language(answer) else TargetOutcome.recommended)
        if any(w in (answer or "") for w in _LIST_WORDS):
            return TargetOutcome.candidate_only
        return TargetOutcome.mentioned_only
    # 兜底(理论不达):未知实体 → 歧义,绝不 not_mentioned
    return TargetOutcome.entity_ambiguous


class OutcomeOutput(BaseModel):
    """官方 DeepSeek 推荐级别判定器的强类型输出(gold-gate 通过后才用于晋升)。"""
    model_config = ConfigDict(extra="forbid")
    outcome: str
    reason: str = ""

    @field_validator("outcome")
    @classmethod
    def _outcome_enum(cls, v: str) -> str:
        if v not in _RECOMMENDATION_LEVELS:
            raise ValueError(f"outcome 必须是 {_RECOMMENDATION_LEVELS}")
        return v


def _build_outcome_body(identity, evidence_windows, question: str, *, response_format: bool = True) -> dict:
    names = "、".join(identity.all_trusted_names)
    cleaned = [privacy.scrub_pii(w)[:1200] for w in evidence_windows]
    numbered = "\n\n".join(f"【证据窗口 {i}】\n{w}" for i, w in enumerate(cleaned, 1))
    prompt = f"""你是推荐级别判定器。目标品牌已确认在证据中被提及,只判断 AI 对该品牌的推荐级别。
【目标品牌】{names}
【行业问题】{privacy.scrub_pii(question)[:200]}
级别:recommended=明确推荐并给正向理由;conditionally_recommended=在预算/地区/资质等条件下推荐;
candidate_only=进入名单/对比但无明确推荐;mentioned_only=仅提及不构成候选或推荐。
{numbered}
仅返回 JSON:{{"outcome":"recommended|conditionally_recommended|candidate_only|mentioned_only","reason":"<=40字"}}"""
    body = {"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 120, "temperature": 0, "thinking": {"type": "disabled"}}
    if response_format:
        body["response_format"] = {"type": "json_object"}
    return body


def make_official_outcome_classifier(post_fn: Optional[PostFn] = None, *, lease_guard: Optional[LeaseGuard] = None,
                                     track_ctx: Optional[dict] = None):
    """官方推荐级别判定器(仅 gold-gate 通过时调用)。失败/结构错 → None(调用方 → pending_review,绝不冒进晋升)。
    lease_guard:每次真实 provider POST(含 400 fallback + 多 key failover 的每一笔)前的租约守卫,
    丢失即 raise LeaseLost 阻断付费调用;响应未知则 raise ProviderResultUnknown(不换 key 重发)。
    track_ctx:核账关联(event_id/request_id/attempt 计数),写入每笔 llm_track metadata。"""
    post = _build_post(post_fn, lease_guard, track_ctx=track_ctx, call_purpose="outcome_classify")

    async def _clf(question: str, answer: str, identity) -> Optional[TargetOutcome]:
        windows = build_evidence_windows(answer, identity)
        if not windows:
            return None
        try:
            resp = await post(_build_outcome_body(identity, windows, question, response_format=True))
            status = getattr(resp, "status_code", 200)
            if status == 400:
                resp = await post(_build_outcome_body(identity, windows, question, response_format=False))
                status = getattr(resp, "status_code", 200)
            if status != 200:
                return None
            content = resp.json()["choices"][0]["message"]["content"]
            out = OutcomeOutput.model_validate(_parse_json_content(content))
            return TargetOutcome(out.outcome)
        except (LeaseLost, ProviderResultUnknown):
            raise   # 租约丢失 / 响应未知 必须向上传播(绝不吞成 None 后继续 → 防重复付费/误晋升)
        except Exception as exc:  # noqa: BLE001 含 _ProviderNotSent(未计费)→ None(调用方 pending_review 非 unknown)
            logger.warning("official outcome classifier 失败: %s", type(exc).__name__)
            return None

    return _clf


def classify_research_outcome(response_status: ResponseStatus, answer: str) -> TargetOutcome:
    """公共调研(行业级,无目标品牌)outcome:描述答案类型,不作单一品牌判断。"""
    if response_status in (ResponseStatus.timeout, ResponseStatus.error, ResponseStatus.budget_blocked):
        return TargetOutcome.engine_error
    if response_status is ResponseStatus.refused:
        return TargetOutcome.refused_risk if any(w in (answer or "") for w in _RISK_WORDS) else TargetOutcome.refused_no_evidence
    if not (answer or "").strip():
        return TargetOutcome.engine_error
    has_criteria = any(w in answer for w in _CRITERIA_WORDS)
    has_list = any(w in answer for w in _LIST_WORDS)
    if has_criteria and not has_list:
        return TargetOutcome.criteria_only
    if has_list:
        return TargetOutcome.candidate_only
    return TargetOutcome.mentioned_only
