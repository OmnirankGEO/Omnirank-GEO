"""发布 exact freeze / commit / release —— **全包唯一**碰计费原语的模块。

规格 §3.4 / §12.1 / §15.7。判据:FIN-01..16、MED-07/08/16/17、§19 变异 19/21/22/54/79。

═══════════════════════════════════════════════════════════════════════
🔴 结构锚就在这一句:本包里**只有本文件** import ``middleware.billing``
═══════════════════════════════════════════════════════════════════════
判据 ``test_only_publish_funding_touches_billing_primitives`` 用 AST 扫全包 ——
别处出现那个 import 就是把钱腿开了第二个出口。
「同一谓词写两处 ⇒ 必有一处没人验」在资金上的代价是最贵的。

受保护文件零 diff:本文件只**调**它们的公共接口,一行都不改(§12.4)。
口径见 :mod:`services.defensive_geo.publish` 的模块 docstring 第 3 条
(**六**个零 diff + ``db/migration_manifest.py`` 单独按"只追加一行"核验)。

═══════════════════════════════════════════════════════════════════════
🔴 四条钱腿,四种真实落地。**没有一条是「返回一个字符串 handle」**
═══════════════════════════════════════════════════════════════════════
窗A 在诊断链上把 ``admin_platform_ledger`` / ``sponsor_platform_ledger``
实现成 ``{'kind': ..., 'ref': 'platform_cost:'+run_token}`` 且注释写明
「该腿尚未接通」。FIN-06 逐字要求「admin exempt 用户钱包不扣,**但平台账真实记账**」,
所以窗C 必须把这条腿接到一个**真的会留下账**的地方。

census 2026-08-21 找到了现役那条腿:``scheduler.py:221 _billing_uid_for_sub``
在 ``billing_mode=='platform'`` 时调
``services/commercial_service_routing.get_platform_direct_service_user_id()``
换出一个**真实的、被显式标记为平台直营服务账号**的 user_id,再走正常
``user_wallets``/``point_transactions`` 流程。这就是本仓「平台账」的现役形态
(不是一张独立 ledger 表)。窗C 复用它,不另造第三种。

  personal_wallet        → freeze_points(actor 本人, _cursor=cur)
  organization_budget    → lock_approval_for_execution + reserve_points_with_cursor
                            (payer = 组织 owner,**不是**发起的成员)
  admin/sponsor_platform → freeze_points(平台直营服务账号, _cursor=cur)
                            对外 fundingState = exempt_recorded(用户没被扣),
                            但平台钱包真的动了 —— 这才叫「真实记账」。

🔴 admin 旁路必须显式挡掉:``freeze_points`` 对 ``is_admin`` 用户直接返回
   ``{'freeze_id': None, 'free': True, 'admin_exempt': True}`` **不写任何表**。
   本模块拿到这个返回一律 raise —— 把它当成功就是变异 22「admin exempt
   完全不写平台账」的实现形态。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping, NamedTuple

from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoPublishFunding")

FUNDING_VERSION = "defgeo-publish-funding-v1"

#: 发布用的现役 feature code。
#:
#: 🔴 [窗G 段二③ · Owner 2026-08-24 批] 由 ``media_publish`` 收敛到
#:    ``media_proxy_publish`` —— 后者才是发布产线**真正冻结**用的那个码
#:    (``api/geo_image_note_api.PUBLISH_FEATURE_CODE``,以及
#:    ``api/meijiehezi_api`` 三处 deduct/refund 用的都是它)。
#:    census 实测 ``db/wallet_db.py:376-377`` **两个都有**且属性相同
#:    (cost_points=0 / cost_compute=0.0 / requires_paid_points=True),
#:    所以 exact points 仍然全部走 ``extra_cost``,收敛不改变冻结金额。
#:
#: 🔴 **不动**的是组织预算路由:``services/organization_route_contract`` 里
#:    legacy ``/api/meijiehezi/publish`` 与 ``/publish/batch`` 仍声明
#:    ``billing="media_publish"``。那是另一条链的路由声明,动它会改变
#:    组织预算的归属 —— 不在本次授权范围内(配套成对判据锁住"没变")。
#:
#: 资金 SSOT:docs/SYSTEM_TRUTH/08_billing.md §3.2 / §3.3 / §6.3。
PUBLISH_FEATURE_CODE = "media_proxy_publish"

FundingPolicy = Literal[
    "personal_wallet", "organization_budget",
    "admin_platform_ledger", "sponsor_platform_ledger",
]

#: 四格 → 对外 fundingState(与 ``funding_projection`` 的诊断矩阵同口径,
#: **刻意不复用那份**:那是诊断域的矩阵,这是发布域的;共用会让改一边顺手改另一边)。
_CONFIRM_FUNDING_STATE: dict[str, str] = {
    "personal_wallet": "frozen",
    "organization_budget": "frozen",          # 组织走 exempt adapter,对外仍是 frozen
    "admin_platform_ledger": "exempt_recorded",
    "sponsor_platform_ledger": "exempt_recorded",
}

_PLATFORM_POLICIES: frozenset[str] = frozenset({
    "admin_platform_ledger", "sponsor_platform_ledger",
})


class FundingError(RuntimeError):
    """资金腿失败。**抛,让整个业务事务回滚** —— 零半状态(§15.7「失败零半状态」)。"""


class BudgetExceeded(FundingError):
    """预算 cap 不够。整项零 freeze / 零 outbox / 零 provider(§3.4 逐字)。"""


class FundingHandle(NamedTuple):
    """一条真实、可恢复的资金句柄。**四元组齐全**才能结算。"""

    kind: str
    freeze_id: int | None
    freeze_backend: str | None
    payer_user_id: int | None
    approval_ref: str | None
    sponsor_policy_ref: str | None
    platform_cost_ref: str | None
    funding_state: str

    def as_command_columns(self) -> dict[str, Any]:
        return {
            "freeze_id": self.freeze_id,
            "freeze_backend": self.freeze_backend,
            "payer_user_id": self.payer_user_id,
            "approval_ref": self.approval_ref,
            "sponsor_policy_ref": self.sponsor_policy_ref,
            "platform_cost_ref": self.platform_cost_ref,
            "funding_state": self.funding_state,
        }


# ══════════════════════════════════════════════════════════════════════════
# 预算 CAS(§3.4:scope 与 global 双闸,同一 budget snapshot 内原子校验)
# ══════════════════════════════════════════════════════════════════════════
class BudgetCheck(NamedTuple):
    ok: bool
    scope: str | None            # 'global' | 'media_publication';ok=True 时 None
    cap: int
    reserved: int
    committed: int
    remaining: int
    required: int

    @property
    def delta(self) -> int:
        return max(0, self.required - self.remaining)


def check_and_lock_budget(
    cur,
    *,
    tenant_owner_id: int,
    accepted_snapshot_id: int,
    service_projection_id: str,
    required_points: int,
    scope_key: str = "media_publication",
) -> tuple[dict[str, Any], list[BudgetCheck]]:
    """锁 budget 行 → 现算 reserved/committed → 双 scope 校验。

    返回 ``(budget_row, blockers)``。``blockers`` 非空 = 不许冻结。

    🔴 ``for_update=True`` 是「多 item 并发锁定同一 budget snapshot」的实现
       (§3.4)。没有它,两个并发 item 各自读到 cap 还够,双双冻结后超限 ——
       **逐项价格正确不代表整包守恒**。
    """
    budget = _store.get_budget(
        cur, tenant_owner_id=tenant_owner_id, accepted_snapshot_id=accepted_snapshot_id,
        service_projection_id=service_projection_id, scope_key=scope_key, for_update=True,
    )
    if budget is None:
        raise FundingError(
            "该 accepted snapshot 上没有 provider-private 执行预算快照 —— "
            "§3.4 要求执行预算与客户报价分开冻结,缺它不得冻结任何执行算力"
        )
    usage = _store.budget_usage(
        cur, execution_budget_snapshot_id=budget["execution_budget_snapshot_id"],
    )
    reserved = int(usage["reservedPoints"])
    committed = int(usage["committedPoints"])
    required = int(required_points)

    blockers: list[BudgetCheck] = []
    for scope_name, cap in (
        ("global", int(budget["global_cap_points"])),
        (scope_key, int(budget["scope_cap_points"])),
    ):
        remaining = cap - reserved - committed
        if reserved + committed + required > cap:
            blockers.append(BudgetCheck(
                ok=False, scope=scope_name, cap=cap, reserved=reserved,
                committed=committed, remaining=remaining, required=required,
            ))
    return budget, blockers


# ══════════════════════════════════════════════════════════════════════════
# freeze
# ══════════════════════════════════════════════════════════════════════════
async def freeze_exact(
    cur,
    *,
    funding_policy: str,
    exact_points: int,
    task_ref: str,
    brand_id: int,
    actor_user_id: int,
    tenant_owner_id: int,
    sponsor_policy_ref: str | None = None,
    organization_context: Any | None = None,
    approval_ref: str | None = None,
) -> FundingHandle:
    """按 funding policy 冻结 **exact** points。在**调用方事务内**。

    🔴 ``exact_points`` 是服务端冻结面的 ``totalExactPoints``,不是现查目录价
       (MED-07「confirm 后 catalog 改价不改变 frozen exact points」)。
    """
    if isinstance(exact_points, bool) or not isinstance(exact_points, int) or exact_points < 0:
        raise FundingError(f"exactPoints 必须是非负整数,实得 {exact_points!r}")
    if funding_policy not in _CONFIRM_FUNDING_STATE:
        raise FundingError(
            f"未知 fundingPolicy {funding_policy!r};合法 = {sorted(_CONFIRM_FUNDING_STATE)}"
        )

    from middleware.billing import freeze_points          # noqa: PLC0415 —— 唯一出口

    if funding_policy == "personal_wallet":
        result = await freeze_points(
            int(actor_user_id), PUBLISH_FEATURE_CODE,
            task_ref=task_ref, brand_id=brand_id,
            extra_cost=exact_points, reason="防御型 GEO 媒体发布",
            _cursor=cur,
        )
        if result.get("admin_exempt"):
            # 🔴 admin 用个人钱包腿走到这里 = 免单旁路。免单必须走平台账那两格,
            #    在这里静默成功等于「不扣任何人、也不记任何账」。
            raise FundingError(
                "personal_wallet 腿命中 admin 免单旁路 —— 免单必须显式走 "
                "admin_platform_ledger 并真实记平台成本账(FIN-06)"
            )
        if exact_points > 0 and not result.get("freeze_id"):
            raise FundingError("personal_wallet 冻结没有拿到 freeze_id —— 句柄不全无法结算")
        return FundingHandle(
            kind="wallet_freeze",
            freeze_id=result.get("freeze_id"),
            freeze_backend=result.get("freeze_table") or "legacy",
            payer_user_id=int(actor_user_id),
            approval_ref=None, sponsor_policy_ref=None, platform_cost_ref=None,
            funding_state=_CONFIRM_FUNDING_STATE[funding_policy],
        )

    if funding_policy == "organization_budget":
        if organization_context is None:
            raise FundingError(
                "organization_budget 必须带服务端解析出的 BillingActorContext —— "
                "缺它就无法保证付款方是组织 owner 而不是发起的成员(FIN-07)"
            )
        from middleware.billing import reserve_points_with_cursor   # noqa: PLC0415

        result = await reserve_points_with_cursor(
            cur, organization_context, reason="防御型 GEO 媒体发布(组织预算)",
        )
        if not result.get("freeze_id"):
            raise FundingError("组织预留没有拿到 freeze_id")
        payer = int(organization_context.identity.payer_user_id)
        if payer == int(actor_user_id) and getattr(
            organization_context.identity, "actor_kind", "",
        ) == "member":
            # 成员发起却由成员本人付款 = 扣了成员个人(变异 04 / FIN-07)。
            raise FundingError("组织腿的 payer 不得是发起成员本人")
        return FundingHandle(
            kind="organization_reservation",
            freeze_id=result.get("freeze_id"),
            freeze_backend=result.get("freeze_table") or "legacy",
            payer_user_id=payer,
            approval_ref=approval_ref,
            sponsor_policy_ref=None, platform_cost_ref=None,
            funding_state=_CONFIRM_FUNDING_STATE[funding_policy],
        )

    # ---- 平台成本两格 -----------------------------------------------------
    if funding_policy == "sponsor_platform_ledger" and not (
        sponsor_policy_ref and str(sponsor_policy_ref).strip()
    ):
        raise FundingError(
            "sponsor_platform_ledger 必须带已签 sponsorPolicyRef —— "
            "缺 ref 时它与 admin_platform_ledger 无法区分(§3.5)"
        )
    if funding_policy == "admin_platform_ledger" and sponsor_policy_ref:
        raise FundingError("admin_platform_ledger 不得带 sponsorPolicyRef")

    platform_uid = _platform_service_user_id()
    result = await freeze_points(
        platform_uid, PUBLISH_FEATURE_CODE,
        task_ref=task_ref, brand_id=brand_id,
        extra_cost=exact_points, reason="防御型 GEO 媒体发布(平台承担)",
        _cursor=cur,
    )
    if result.get("admin_exempt"):
        # 平台直营服务账号被配成了 admin —— 那样它同样不写账。
        raise FundingError(
            "平台直营服务账号命中 admin 免单旁路,平台成本账会是空的 —— "
            "该账号必须是非 admin 的服务商账号(commercial_service_routing 已有同款校验)"
        )
    if exact_points > 0 and not result.get("freeze_id"):
        raise FundingError("平台成本账冻结没有拿到 freeze_id —— 「真实记账」没有发生(FIN-06)")
    return FundingHandle(
        kind="platform_cost_ledger",
        freeze_id=result.get("freeze_id"),
        freeze_backend=result.get("freeze_table") or "legacy",
        payer_user_id=platform_uid,
        approval_ref=None,
        sponsor_policy_ref=sponsor_policy_ref,
        platform_cost_ref=f"platform_freeze:{result.get('freeze_id')}",
        funding_state=_CONFIRM_FUNDING_STATE[funding_policy],
    )


def _platform_service_user_id() -> int:
    """现役平台账腿。**fail-closed**:没配置就抛,不回落到发起人自己的钱包。"""
    from services.commercial_service_routing import get_platform_direct_service_user_id

    return int(get_platform_direct_service_user_id())


# ══════════════════════════════════════════════════════════════════════════
# commit / release —— **物理结算先成功,业务终态才落**(P0-4)
# ══════════════════════════════════════════════════════════════════════════
#
# ═══════════════════════════════════════════════════════════════════════
# 🔴 [P0-4] 顺序反转的理由:原来是「先记账,再动钱,不看动没动成」
# ═══════════════════════════════════════════════════════════════════════
# 改之前这两个原语长这样::
#
#     _assert_direction(command, expected="commit")
#     _mark_settled(cur, command)            # ← 业务终态标记**在前**
#     return await commit_freeze(...)        # ← 物理结算在后,返回值原样上抛
#
# 而 7 个调用点**一个都没有看返回值**,拿到就无条件
# ``bump_status(funding_state="committed"/"released", command_state=...)``。
# 于是 billing 返 ``{"success": False, "reason": "未找到冻结记录"}``、
# 返 ``ambiguous``、或者幂等返回**相反终态**(想 commit 却已 released)时,
# command 照样被写成 committed/completed。Codex 终审 Mock 已复现方向分裂:
# 物理腿在 released、账本 ledger 在 committed。
#
# 这不是"少了一个 if"。它是**两套账各自往前走**:业务账把"已收尾"当既成事实
# (``settled_at`` 一落,④⑦ 收敛器与 Z-1 队列就都不再看这一条),
# 物理账却还停在 frozen —— 没有任何一轮自动收敛会回来看它。
#
# ═══════════════════════════════════════════════════════════════════════
# 🔴 语义照抄诊断链,**不发明第二套**
# ═══════════════════════════════════════════════════════════════════════
# ``services/diagnosis_runs.py::_do_settlement`` 已经有一套跑了很久的真值表,
# 本模块逐条对齐(工单 B-1 逐字要求「照抄那套语义」):
#
#   抛异常                        → R3 retry(不改终态、不动钱)
#   ``ambiguous``                 → R2 manual(不重试不改道 —— 跨表撞号只能人裁)
#   ``success is True`` + 幂等冲突 → R2 manual(想 commit 已 released / 反之)
#   ``success is True``           → R1 **落业务终态**(同一事务 + statusVersion CAS)
#   其余(success False/超时)     → R3 retry;commit 达上限转 manual
#
# 🔴 幂等返回 ≠ 钱按我要的方向动了。``{"success": True, "idempotent": True,
#    "status": "released"}`` 的意思是「这笔已经退了,不用我动」——
#    对一个想 commit 的意图来说那是**方向相反**,写成 committed 就是编账。
#    (本仓记过:资金调用返 success ≠ 钱按你要的方向动了。)
#
# ═══════════════════════════════════════════════════════════════════════
# 🔴 为什么终态写在**原语里**,不在 7 个调用点上
# ═══════════════════════════════════════════════════════════════════════
# 与 ``_mark_settled`` 当初进原语是同一个理由,只是这次的代价更贵:
# 「同一谓词写两处 ⇒ 必有一处没人验」,7 处就是 7 次忘记看 verdict 的机会。
# 现在调用点只剩两件事可做:告诉原语**终态的 commandState 是哪一格**,
# 以及拿到非 settled 的 verdict 之后**不要往下走**。
#
# 判据钉住的那句话是:**billing 失败 ⇒ 零终态写入**
# (``settled_at`` 仍是 NULL、``funding_state`` 一格没动)。

#: 结算裁决的**闭集**。判据拿它当分母,不手抄。
SettlementVerdict = Literal["settled", "retry", "manual"]
SETTLEMENT_VERDICTS: tuple[str, ...] = ("settled", "retry", "manual")

#: 钱向 → 结算成功后的 ``funding_state``。**闭表**,与 §15.7 真值表同源
#: (``publish_settlement._TRUTH_TABLE`` 的 commit/release 两行)。
_TERMINAL_FUNDING_STATE: dict[str, str] = {"commit": "committed", "release": "released"}

#: commit 重试多少次之后承认自动跑不通(诊断链 ``_SETTLE_MAX_ATTEMPTS`` 同值)。
#: release 不设上限 —— 退款一直退不成时,拦住它只会让钱更回不来。
SETTLE_MAX_ATTEMPTS = 5


class SettlementOutcome(NamedTuple):
    """一次结算尝试的裁决。``verdict`` 是**唯一**该被调用点消费的那一位。"""

    verdict: SettlementVerdict
    intent: str                 # 'commit' | 'release'
    reason: str
    billing: dict[str, Any]

    @property
    def settled(self) -> bool:
        return self.verdict == "settled"


async def commit_exact(
    cur, command: Mapping[str, Any], *, reason: str,
    terminal_command_state: str | None = None,
) -> SettlementOutcome:
    """canonical success 才 commit。**幂等**:已 committed/released 再调返回 no-op。

    ``terminal_command_state`` = 结算成功后该落的 ``commandState``;
    ``None`` 表示"调用方已经把 commandState 摆好了,只差钱那一格"
    (广告法门与收敛器⑦ 就是这种形态)。
    """
    return await _settle_exact(
        cur, command, intent="commit", reason=reason,
        terminal_command_state=terminal_command_state,
    )


async def release_exact(
    cur, command: Mapping[str, Any], *, reason: str,
    terminal_command_state: str | None = None,
) -> SettlementOutcome:
    """**明确零外部副作用**才 release。方向不对时抛 —— 不给「顺手退一下」留位置。"""
    return await _settle_exact(
        cur, command, intent="release", reason=reason,
        terminal_command_state=terminal_command_state,
    )


async def _settle_exact(
    cur, command: Mapping[str, Any], *, intent: str, reason: str,
    terminal_command_state: str | None,
) -> SettlementOutcome:
    """§12.1 真值表的**唯一**执行点(诊断链 ``_do_settlement`` 的发布域同构体)。"""
    _assert_direction(command, expected=intent)

    # ── 幂等 ①:已经结算过的,一行钱都不再动、一格状态都不再改 ─────────────
    #    ``settled_at`` 是 R3 引入的「与 fundingState 无关」的终态标记 ——
    #    平台成本腿的 fundingState 是常量,只有它能表达"这一条收过尾了"。
    if command.get("settled_at") is not None:
        return SettlementOutcome(
            "settled", intent, "already_settled_noop",
            {"success": True, "idempotent": True, "no_op": True},
        )

    if command.get("freeze_id") is None:
        # 0 元或平台腿未产生冻结行 —— 没有可结算的物理腿,但状态仍要收敛。
        # 这一格**没有 billing 可失败**,所以直接落终态是安全的。
        if not _write_terminal(cur, command, intent=intent,
                               terminal_command_state=terminal_command_state):
            return SettlementOutcome("retry", intent, "terminal_cas_missed", {})
        return SettlementOutcome(
            "settled", intent, "no_physical_leg",
            {"success": True, "no_physical_leg": True},
        )

    from middleware.billing import commit_freeze, release_freeze   # noqa: PLC0415

    primitive = commit_freeze if intent == "commit" else release_freeze
    try:
        r = await primitive(
            freeze_id=command["freeze_id"],
            task_ref=command["freeze_task_ref"],
            user_id=command.get("payer_user_id"),
            freeze_table=command.get("freeze_backend"),
            reason=reason,
            _cursor=cur,
        )
    except Exception as exc:                              # noqa: BLE001
        # R3:抛出来的都还没定论(可能已到达 billing、也可能没有)——
        # 重试是安全的(两个原语都幂等),写终态不是。
        return SettlementOutcome(
            "retry", intent, f"settlement_exception:{type(exc).__name__}:{str(exc)[:160]}", {},
        )

    r = r if isinstance(r, dict) else {}

    # R2 ambiguous → 转人工(不重试不改道)。跨表撞号自动结算 = 坐实错的那一笔。
    if r.get("ambiguous"):
        return SettlementOutcome("manual", intent, "ambiguous_cross_table", r)

    if r.get("success") is True:
        # 幂等冲突守卫:billing 幂等返回实际 status,与本意图相反 → 转人工。
        if r.get("idempotent"):
            idem_status = r.get("status")
            if (intent == "commit" and idem_status == "released") or \
               (intent == "release" and idem_status == "committed"):
                return SettlementOutcome(
                    "manual", intent, f"idempotent_conflict:{idem_status}", r,
                )
        # R1:物理成功且方向一致 —— **现在**才落业务终态,同一事务 + CAS。
        if not _write_terminal(cur, command, intent=intent,
                               terminal_command_state=terminal_command_state):
            # CAS 0 行 = 期间有人改过这一条。钱已经按这个方向动过了(幂等),
            # 所以下一轮收敛重来一次仍然安全 —— 但**这一次**不许写终态。
            return SettlementOutcome("retry", intent, "terminal_cas_missed", r)
        return SettlementOutcome("settled", intent, "settled", r)

    # R3:success False / 超时 → 退避重试(不改终态、不动钱)。
    return SettlementOutcome(
        "retry", intent, str(r.get("reason") or "settlement_not_success"), r,
    )


def _write_terminal(
    cur, command: Mapping[str, Any], *, intent: str, terminal_command_state: str | None,
) -> bool:
    """物理结算已成功且方向一致 —— 落业务终态。**同一事务 + statusVersion CAS**。

    🔴 CAS 是承重的:Z-1 人工处置面上两个**相反**动作(确认已执行 / 确认未执行)
       各自基于自己读到的旧状态推进时,后到的那一个必须 0 行放弃,
       而不是把前一个的终态盖掉(Codex 已复现交错分裂)。
    🔴 ``mark_settled`` 放在 bump 之后:它自带 ``WHERE settled_at IS NULL``,
       是"至多一次"的那一半;bump 的 CAS 是"基于最新状态"的那一半。两半都要。
    """
    target_id = command.get("publish_command_id")
    if cur is None or not target_id:
        # 纯函数判据路径(没有 cursor)—— 与 ``_mark_settled`` 同款安静跳过。
        return True
    expect = command.get("status_version")
    fresh = _store.write_settlement_terminal(
        cur, publish_command_id=str(target_id),
        funding_state=_TERMINAL_FUNDING_STATE[intent],
        command_state=terminal_command_state,
        expect_status_version=int(expect) if expect is not None else None,
    )
    if fresh is None:
        logger.warning(
            "[defgeo-publish-funding] %s 终态 CAS 落空(expect statusVersion=%s)—— "
            "期间有人改过这一条,本次不写终态", target_id, expect,
        )
        return False
    _mark_settled(cur, command)
    return True


def apply_unsettled(cur, command: Mapping[str, Any], outcome: SettlementOutcome) -> None:
    """非 settled 裁决的**唯一**处置点。诊断链 ``_bump_attempts_retry`` 的同构体。

    · ``manual`` → 隔离进 Z-1 人工队列(资金态 quarantined / 命令态 quarantined);
    · ``retry``  → 只累计尝试次数与原因,**一格终态都不写**;commit 达上限转 manual。

    🔴 这里**永远不写** ``settled_at``、也永远不写 committed/released ——
       工单 B-1 逐字:「success=false / ambiguous / 相反幂等 ⇒ 进 retry/manual,
       绝不写 settled」。
    """
    command_id = command.get("publish_command_id")
    if cur is None or not command_id:
        return
    if outcome.verdict == "settled":                      # pragma: no cover - 调用方守卫
        raise FundingError("apply_unsettled 只处置非 settled 裁决")

    if outcome.verdict == "manual":
        _quarantine(cur, str(command_id), outcome.reason)
        logger.error("[defgeo-publish-funding] %s 结算转人工:%s", command_id, outcome.reason)
        return

    attempts = _store.bump_settlement_attempt(
        cur, publish_command_id=str(command_id), error=outcome.reason,
    )
    if outcome.intent == "commit" and attempts >= SETTLE_MAX_ATTEMPTS:
        _quarantine(cur, str(command_id),
                    f"commit_attempts>={SETTLE_MAX_ATTEMPTS}:{outcome.reason}")
        logger.error("[defgeo-publish-funding] %s commit 重试 %d 次仍未成功 → 转人工",
                     command_id, attempts)
        return
    logger.warning("[defgeo-publish-funding] %s 结算未成功(第 %d 次),保持冻结待下一轮:%s",
                   command_id, attempts, outcome.reason)


def _quarantine(cur, publish_command_id: str, reason: str) -> None:
    """隔离进 Z-1。**资金态不翻转** —— quarantined 仍然是"钱冻着"。"""
    _store.bump_status(
        cur, publish_command_id=publish_command_id,
        funding_state="quarantined", command_state="quarantined",
        status_reason="结果待平台核实，费用已冻结、不会多扣（无需操作）",
    )
    # 只记原因,**不算一次重试** —— 转人工是收口动作,不是又试了一次。
    _store.bump_settlement_attempt(
        cur, publish_command_id=publish_command_id, error=reason, increment=False)


def _mark_settled(cur, command: Mapping[str, Any]) -> None:
    """🔴 [R3] 结算终态标记落在**原语内部**,不在 7 个调用点上。

    理由见 ``store.mark_settled``:平台成本腿的 ``fundingState`` 是常量,
    掉不出收敛器的候选集 —— 没有这个标记,R3 的"同构"会让它每一轮重结算一次。
    放在原语里,任何一个调用点都不可能忘。``cur`` 缺席时安静跳过(纯函数判据)。
    """
    cid = command.get("publish_command_id")
    if cur is None or not cid:
        return
    _store.mark_settled(cur, publish_command_id=str(cid))


def _assert_direction(command: Mapping[str, Any], *, expected: str) -> None:
    """🔴 结算前**再读一次 canonical 事实**,不信调用方说的方向。

    这条断言存在的理由很具体:``release_exact`` 如果只按调用方参数动作,
    那么「POST 后超时按失败退款」(变异 21)只需要调用方传错一个字符串就成立。
    把方向重新从 canonical state 算出来,调用方就没法用参数绕过真值表。
    """
    from services.defensive_geo.publish import publish_settlement as _s

    state = command.get("canonical_publication_state")
    direction = _s.settlement_direction(state)
    if direction != expected:
        raise FundingError(
            f"canonical state={state!r} 的钱向是 {direction!r},不是 {expected!r} —— "
            "未知/未核实态既不 commit 也不 release(§12.1)"
        )


def confirm_funding_state(funding_policy: str) -> str:
    if funding_policy not in _CONFIRM_FUNDING_STATE:
        raise FundingError(f"未知 fundingPolicy {funding_policy!r}")
    return _CONFIRM_FUNDING_STATE[funding_policy]


def is_platform_cost(funding_policy: str) -> bool:
    return funding_policy in _PLATFORM_POLICIES


def census() -> dict[str, Any]:
    return {
        "fundingVersion": FUNDING_VERSION,
        "featureCode": PUBLISH_FEATURE_CODE,
        "confirmFundingState": dict(_CONFIRM_FUNDING_STATE),
        "platformPolicies": sorted(_PLATFORM_POLICIES),
        # [P0-4] 结算裁决闭集与终态映射 —— 判据拿它当分母,不手抄。
        "settlementVerdicts": list(SETTLEMENT_VERDICTS),
        "terminalFundingState": dict(_TERMINAL_FUNDING_STATE),
        "settleMaxAttempts": SETTLE_MAX_ATTEMPTS,
    }
