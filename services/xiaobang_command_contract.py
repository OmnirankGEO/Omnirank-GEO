"""Registry v3 的 ``command_contract`` 值对象与注册期校验(规格 §8.1/§8.2)。

## 这个模块只做三件事

1. **把 ``side_effect`` 与 ``confirmation_policy.mode`` 的映射写死**,注册期不一致
   就拒绝注册。规格 §8.1 原话:「映射错误 = 测试红」—— 所以判据不是文档里的一句
   叮嘱,是 :class:`CommandContract` 构造函数里的一个 ``raise``。

   * ``side_effect=external``(对外不可逆:真实发布/对外发送/真实投放)
     → ``required_user_click``;
   * ``side_effect=compute_only``(纯算力消耗、可撤销)
     → ``silent_with_notice``(静默扣 + 事后通知,依据已签发 DP-A6.1)。

   这两档不是本模块发明的口径:``external`` 档的硬门依据是 DP-A5 + 资金 SSOT,
   ``compute_only`` 档的依据是 DP-A6.1。两者都经 :mod:`services.xiaobang_decision_ledger`
   的 :func:`assert_gate_is_signed` 复核 —— **没有任何一档挂在 XO-06/07/08 这些
   candidate 上**。

2. **社媒域负向枚举锁**(Owner 2026-08-17「员工席位永久剔除社媒」)。
   锁的作用域是「员工席位能看得见/走得通的东西」,而那是**两处**:能力注册表
   与员工路由白名单。只锁一处 = 锁的范围小于轴的作用域,这个错本仓犯过三次。
   :func:`assert_no_social_domain` 因此对两类输入都能用,配套测试对两处分别枚举。

3. **11 项确认绑定的完整性**。§10.3 的复核清单与 §8.1 的 ``bind`` 必须逐项对齐;
   少一项就等于少一条漂移判据,而漂移是 confirm→execute 之间 TOCTOU 的唯一入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from services.xiaobang_decision_ledger import assert_gate_is_signed

COMMAND_CONTRACT_VERSION = "xiaobang-command-contract-v3"

# ── side_effect / confirmation_policy 两档 ────────────────────────────────
SIDE_EFFECT_EXTERNAL = "external"
SIDE_EFFECT_COMPUTE_ONLY = "compute_only"
SIDE_EFFECTS = (SIDE_EFFECT_EXTERNAL, SIDE_EFFECT_COMPUTE_ONLY)

CONFIRM_REQUIRED_USER_CLICK = "required_user_click"
CONFIRM_SILENT_WITH_NOTICE = "silent_with_notice"
CONFIRMATION_MODES = (CONFIRM_REQUIRED_USER_CLICK, CONFIRM_SILENT_WITH_NOTICE)

#: 写死的映射。operation 不许自定义混搭。
SIDE_EFFECT_TO_CONFIRMATION: dict[str, str] = {
    SIDE_EFFECT_EXTERNAL: CONFIRM_REQUIRED_USER_CLICK,
    SIDE_EFFECT_COMPUTE_ONLY: CONFIRM_SILENT_WITH_NOTICE,
}

#: 每一档背后的已签发 H0 闸。构造时复核,candidate 挂不上来。
SIDE_EFFECT_TO_H0_GATE: dict[str, str] = {
    SIDE_EFFECT_EXTERNAL: "XB-H0-EXTERNAL-CONFIRM",
    SIDE_EFFECT_COMPUTE_ONLY: "XB-H0-SILENT-NOTICE",
}

# ── 阶段 ──────────────────────────────────────────────────────────────────
PHASE_QUERY = "query"
PHASE_PREPARE = "prepare"
PHASE_CONFIRM = "confirm"
PHASE_CANCEL = "cancel"
PHASE_EXECUTE = "execute"
PHASE_STATUS = "status"
ALL_PHASES = (PHASE_QUERY, PHASE_PREPARE, PHASE_CONFIRM,
              PHASE_CANCEL, PHASE_EXECUTE, PHASE_STATUS)

#: §8.1 幂等合同要求的四个阶段,一个都不能少。
REQUIRED_IDEMPOTENT_PHASES = (PHASE_PREPARE, PHASE_CONFIRM, PHASE_CANCEL, PHASE_EXECUTE)

# ── 确认绑定字段 ──────────────────────────────────────────────────────────
#: §8.1 external 档的 11 项 bind,与 §10.3 confirm 复核清单逐项对齐。
EXTERNAL_BIND_FIELDS: tuple[str, ...] = (
    "actor_id",
    "tenant_owner_id",
    "payer_id",
    "organization_id",
    "membership_version",
    "assignment_authority_version",
    "approval_policy_version",
    "payload_hash",
    "object_manifest_hash",
    "compute_quote_hash",
    "expires_at",
)

#: compute_only 档不存在「等用户点击」这个动作,因此不要求携带确认时刻的绑定;
#: 但身份/对象/报价三类绑定仍然必须在,否则扣错人的钱这条路就开着。
COMPUTE_ONLY_MIN_BIND_FIELDS: tuple[str, ...] = (
    "actor_id",
    "tenant_owner_id",
    "payer_id",
    "payload_hash",
    "object_manifest_hash",
    "compute_quote_hash",
)


class ContractRegistrationError(ValueError):
    """注册期拒绝。注册不进去 = 模型看不见 = 不可能被调用。"""


class SocialDomainLockError(ContractRegistrationError):
    """社媒域负向枚举锁命中(Owner 2026-08-17)。"""


# ── 社媒域负向枚举锁 ──────────────────────────────────────────────────────
#: 锁按**四个轴**判,不只按名字猜:operation_id 词干 / 前端路由前缀 /
#: capability 命名空间 / 后端模块归属。任何一个轴命中即拒。
_SOCIAL_OPERATION_TOKENS: tuple[str, ...] = (
    "social", "socialstudio", "social_studio", "ip_studio", "myip", "my_ip",
    "persona", "script", "hook", "digital_human", "shipinhao", "xiaohongshu",
    "douyin_script", "poster",
)
_SOCIAL_ROUTE_PREFIXES: tuple[str, ...] = (
    "/s/", "/social", "/social-studio", "/s-end", "/send",
    # 见下方 _SOCIAL_ROUTE_EXACT 的说明:社媒订阅签约/管理页。
    "/subscription/",
)
#: 🔴 2026-08-21(包 B):这三条**在 App.tsx 里明确注明是社媒板块的订阅入口**
#:    (`frontend/src/App.tsx:583-590`「订阅相关 3 路由从 GEO <Layout /> 子路由内移出」,
#:     用户从 `/s` 顶栏点套餐胶囊跳过来)。
#:    它们既不以 `/s/` 开头、名字里也没有 social ⇒ **原锁一条都拦不住**。
#:    实测:`social_domain_hits(route="/subscription/manage")` → 空,不判红。
#:    工单 §6 包 B④ 字面要求「扩展…订阅…能力」,照做就会把社媒路由registered 进
#:    GEO 席位能力面,而负向锁**全程绿着** —— 这正是「加宽 pattern 没加正样本
#:    等于没判据在守」的镜像:漏了 pattern,锁形同虚设。
#:    ⇒ 按 Owner 2026-08-17(ORG-SEAT-NO-SOCIAL)/ 2026-08-18(小榜只管 GEO)补进锁面。
#:    ⚠️ 用**精确值 + 带斜杠前缀**,不用裸子串:`/pricing` `/agent/pricing`
#:      `/feature-pricing` 是 GEO 自己的,绝不能被误伤(正/反样本见
#:      tests/xiaobang_solution_first_2026_08_21/test_social_lock_widened.py)。
_SOCIAL_ROUTE_EXACT: frozenset[str] = frozenset({"/s", "/pricing-plans"})
_SOCIAL_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "social.", "ip.", "persona.", "script.",
)
#: 🔴 [WO_261 E2 · 2026-09-22] 这张表是**四轴锁里唯一按路径字面量判**的一轴。
#:
#: 另外三轴(identifier 词段 / route 精确值 / capability 前缀)判的是**名字与语义**,
#: 删域之后照常有牙 —— 有人重新注册一个叫 `social_studio_script` 的能力,它们仍会拦。
#: 只有本轴判的是「模块路径里有没有这些串」,而 E3 把这些路径**整个删掉**之后,
#: 它就变成**恒不命中的死锁:看起来全绿,守的东西已经不存在**(OSS_12 §2.8)。
#:
#: ⇒ 处置不是「整段退役」(那会拆掉还在守的三轴),而是给每个 token 一个**状态**:
#:    在役 = 仓里仍有这个路径,本轴对它有牙;
#:    已退役 = 路径已删,本轴对它必然不命中,由「该路径必须不存在」的判据接替。
#:    判据 `test_social_module_tokens_are_live_or_retired` 钉住这件事:
#:    **路径没了却还标在役 ⇒ 当场红**,不会安静地绿下去。
#:
#: 🔴 为什么要「路径必须不存在」这条接替判据:本轴原来守的是
#:    「社媒模块被登进 GEO 席位能力面」。删域后这件事的前提消失,但**它会回来** ——
#:    有人把已删的社媒工具包目录重新加回仓里时,接替判据当场红,
#:    而本轴(已标退役)不会再响。两者合起来才覆盖删域前后两段时间。
_SOCIAL_MODULE_TOKEN_SPECS: tuple[tuple[str, str], ...] = (
    # (token, 仓内路径 —— 用来判它还在不在;空串 = 这个 token 不对应单一路径)
    ("tools.social_operator", "tools/social_operator"),
    ("tools/social_operator", "tools/social_operator"),
    ("agents.social_agent", "agents/social_agent.py"),
    ("agents/social_agent", "agents/social_agent.py"),
    ("api.social_mainpath_api", "api/social_mainpath_api.py"),
    ("api/social_mainpath_api", "api/social_mainpath_api.py"),
    ("SocialStudio", "frontend/src/pages/SocialStudio"),
    ("SocialOperator", "frontend/src/pages/SocialOperator"),
    ("s_end", "frontend/src/components/s_end"),
)
_SOCIAL_MODULE_TOKENS: tuple[str, ...] = tuple(t for t, _ in _SOCIAL_MODULE_TOKEN_SPECS)


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def _segments(identifier: str) -> tuple[str, ...]:
    """把标识符切成词段。

    🔴 **禁止裸子串匹配**。第一版这里用的是 ``token in identifier``,当场把
    ``personal_settings``(个人设置)判成社媒域 —— 因为 ``persona`` 是
    ``personal`` 的子串。同一种病本仓记过:
    ``feedback_secret_string_cannot_be_substring_of_allowed_label``。
    锁必须按**整词段**判,否则它拦的是无辜的人。
    """
    return tuple(part for part in re.split(r"[^a-z0-9]+", _normalize(identifier)) if part)


def social_domain_hits(
    *,
    identifier: str = "",
    route: str = "",
    capability: str = "",
    modules: Iterable[str] = (),
) -> list[str]:
    """返回命中的社媒轴清单(空 = 不是社媒域)。

    刻意返回清单而不是布尔:命中时要能说清**是哪个轴**命中的,
    否则「为什么被拒」这句话没法给出可执行的下一步。
    """
    hits: list[str] = []
    ident_segments = frozenset(_segments(identifier))
    for token in _SOCIAL_OPERATION_TOKENS:
        # 整词段命中,不做子串命中(见 _segments docstring)。
        if token in ident_segments:
            hits.append("operation_id=={0}".format(token))
    path = _normalize(route).split("?", 1)[0]
    if path in _SOCIAL_ROUTE_EXACT:
        hits.append("route=={0}".format(path))
    for prefix in _SOCIAL_ROUTE_PREFIXES:
        if path.startswith(prefix):
            hits.append("route^{0}".format(prefix))
    cap = _normalize(capability)
    for prefix in _SOCIAL_CAPABILITY_PREFIXES:
        # capability 命名空间是带点的真前缀(social. / ip. …),前缀判在这里
        # 不会误伤 —— "publish.execute" 不以任何一条开头。
        if cap.startswith(prefix):
            hits.append("capability^{0}".format(prefix))
    for module in modules:
        raw = str(module or "")
        for token in _SOCIAL_MODULE_TOKENS:
            if token.lower() in raw.lower():
                hits.append("module~{0}".format(token))
    return sorted(dict.fromkeys(hits))


def assert_no_social_domain(
    *,
    identifier: str = "",
    route: str = "",
    capability: str = "",
    modules: Iterable[str] = (),
    where: str = "registry",
) -> None:
    """员工席位可见面(注册表 / 员工路由白名单)禁止出现社媒域条目。"""
    hits = social_domain_hits(
        identifier=identifier, route=route, capability=capability, modules=modules
    )
    if hits:
        raise SocialDomainLockError(
            "{0} 拒绝社媒域条目 {1!r}:命中 {2}。"
            "员工席位永久剔除社媒(Owner 2026-08-17 · 决策 ORG-SEAT-NO-SOCIAL);"
            "社媒能力不在本产品内,不进 GEO 席位能力发现。"
            .format(where, identifier or route or capability, hits)
        )


# ── 值对象 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConfirmationPolicy:
    mode: str
    bind: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"mode": self.mode, "bind": list(self.bind)}


@dataclass(frozen=True)
class IdempotencyContract:
    scope: str
    required_phases: tuple[str, ...]
    phase_contracts: Mapping[str, str]

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "required_phases": list(self.required_phases),
            "phase_contracts": dict(self.phase_contracts),
        }


@dataclass(frozen=True)
class CommandContract:
    """一个 operation 的可执行合同。``None`` 表示该 operation 仍只是导航项。"""

    required_capability: str
    resource_kind: str
    side_effect: str
    supported_phases: tuple[str, ...]
    adapters: Mapping[str, str]
    confirmation_policy: ConfirmationPolicy
    idempotency: IdempotencyContract
    billing_feature: Optional[str] = None
    request_schema_version: str = "1"
    response_schema_version: str = "1"
    #: WP1 只放行只读/协调阶段。领域执行要等图文包的原子 claim 上线后才置 True
    #: (规格 §18 开工前置 / §16.1 NO-GO 前置)。
    executable: bool = False
    notes: str = ""
    h0_gate_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if self.side_effect not in SIDE_EFFECTS:
            raise ContractRegistrationError(
                "未知 side_effect:{0!r};只有 {1}".format(self.side_effect, SIDE_EFFECTS)
            )
        expected_mode = SIDE_EFFECT_TO_CONFIRMATION[self.side_effect]
        if self.confirmation_policy.mode != expected_mode:
            raise ContractRegistrationError(
                "side_effect={0} 必须映射 confirmation_policy.mode={1},"
                "实际 {2!r} —— 两档映射由 Registry 写死,operation 不许自定义混搭"
                "(规格 §8.1)".format(
                    self.side_effect, expected_mode, self.confirmation_policy.mode
                )
            )

        bind = tuple(self.confirmation_policy.bind)
        if self.side_effect == SIDE_EFFECT_EXTERNAL:
            if bind != EXTERNAL_BIND_FIELDS:
                missing = [f for f in EXTERNAL_BIND_FIELDS if f not in bind]
                extra = [f for f in bind if f not in EXTERNAL_BIND_FIELDS]
                raise ContractRegistrationError(
                    "external 档的 bind 必须与 §10.3 复核清单逐项对齐;"
                    "缺 {0} / 多 {1}".format(missing, extra)
                )
        else:
            missing = [f for f in COMPUTE_ONLY_MIN_BIND_FIELDS if f not in bind]
            if missing:
                raise ContractRegistrationError(
                    "compute_only 档仍必须绑定身份/对象/报价三类,缺 {0}".format(missing)
                )
            # 规格 §8.1 对 compute_only 是「不**要求**携带等待点击的确认字段」,
            # 不是「禁止」—— 所以这里只拦未定义字段,不拦多带的合法字段。
            unknown = [f for f in bind if f not in EXTERNAL_BIND_FIELDS]
            if unknown:
                raise ContractRegistrationError(
                    "bind 出现未定义字段 {0}".format(unknown)
                )

        phases = tuple(self.supported_phases)
        unknown_phase = [p for p in phases if p not in ALL_PHASES]
        if unknown_phase:
            raise ContractRegistrationError("未知阶段 {0}".format(unknown_phase))
        if PHASE_QUERY not in phases or PHASE_PREPARE not in phases:
            raise ContractRegistrationError("supported_phases 至少要含 query 与 prepare")

        stray_adapter = [p for p in self.adapters if p not in phases]
        if stray_adapter:
            raise ContractRegistrationError(
                "adapters 声明了未支持的阶段 {0}".format(stray_adapter)
            )
        if self.executable and PHASE_EXECUTE not in self.adapters:
            raise ContractRegistrationError(
                "executable=True 但没有 execute adapter"
            )

        required = tuple(self.idempotency.required_phases)
        missing_idem = [p for p in REQUIRED_IDEMPOTENT_PHASES if p not in required]
        if missing_idem:
            raise ContractRegistrationError(
                "idempotency.required_phases 缺 {0}(规格 §8.1)".format(missing_idem)
            )
        missing_contract = [
            p for p in required if not str(self.idempotency.phase_contracts.get(p) or "")
        ]
        if missing_contract:
            raise ContractRegistrationError(
                "idempotency.phase_contracts 未覆盖 {0}".format(missing_contract)
            )

        if not str(self.required_capability or "").strip():
            raise ContractRegistrationError("required_capability 不能为空")
        if not str(self.resource_kind or "").strip():
            raise ContractRegistrationError("resource_kind 不能为空")

        assert_no_social_domain(
            identifier=self.required_capability,
            capability=self.required_capability,
            modules=tuple(self.adapters.values()),
            where="command_contract",
        )

        gate_id = SIDE_EFFECT_TO_H0_GATE[self.side_effect]
        # 挂不上已签发闸的档一律进不来 —— 这条就是 invrel P0 的反向对照物。
        assert_gate_is_signed(gate_id)
        object.__setattr__(self, "h0_gate_id", gate_id)

    # 公共 DTO 永不含 adapter 名、内部 URL、HTTP method 与供应商信息(§8.2)。
    def as_public_dict(self) -> dict:
        return {
            "required_capability": self.required_capability,
            "resource_kind": self.resource_kind,
            "side_effect": self.side_effect,
            "supported_phases": list(self.supported_phases),
            "confirmation_policy": self.confirmation_policy.as_dict(),
            "billing_feature": self.billing_feature,
            "request_schema_version": self.request_schema_version,
            "response_schema_version": self.response_schema_version,
            "executable": self.executable,
        }

    def as_internal_dict(self) -> dict:
        out = self.as_public_dict()
        out["adapters"] = dict(self.adapters)
        out["idempotency"] = self.idempotency.as_dict()
        out["h0_gate_id"] = self.h0_gate_id
        out["notes"] = self.notes
        return out


def external_confirmation_policy() -> ConfirmationPolicy:
    return ConfirmationPolicy(CONFIRM_REQUIRED_USER_CLICK, EXTERNAL_BIND_FIELDS)


def compute_only_confirmation_policy(
    extra_bind: Iterable[str] = (),
) -> ConfirmationPolicy:
    bind = tuple(dict.fromkeys((*COMPUTE_ONLY_MIN_BIND_FIELDS, *extra_bind)))
    return ConfirmationPolicy(CONFIRM_SILENT_WITH_NOTICE, bind)


def standard_idempotency(scope: str = "tenant_operation_request") -> IdempotencyContract:
    return IdempotencyContract(
        scope=scope,
        required_phases=REQUIRED_IDEMPOTENT_PHASES,
        phase_contracts={
            PHASE_PREPARE: "canonical_input_hash_and_prepare_request_id",
            PHASE_CONFIRM: "intent_revision_and_single_receipt",
            PHASE_CANCEL: "intent_revision_and_cancel_request_id",
            PHASE_EXECUTE: "execution_request_id_and_intent_revision",
        },
    )
