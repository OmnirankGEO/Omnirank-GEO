"""Z-1 · 资金隔离态的运营处置面(规格 §0.5.6 Z-1)。

Z-1 原文认定这是**全规格唯一系统性死路**:§12.3 只说了「未知态有人工核验与
owner/admin 处置入口」,但那只有名词没有操作面。裁定给了六条,逐条落成本模块:

  ① 角色 = **平台 admin**;
  ② 界面 = 现役 admin 面板新增「资金核验队列」;
  ③ 逐条动作集 = {确认已执行→commit / 确认未执行→release / 维持隔离(**必填理由**)};
  ④ 全部处置**留痕入账本**;
  ⑤ pending 超 **7 天**自动告警;
  ⑥ 服务商侧 ``verify_outcome`` 保持**只读**,另加「提交线下核实凭证」动作,
     凭证进同一队列作为 admin 裁定材料 —— **服务商有事可做,收口权在平台**。

🔴 为什么动作集只有三个,且第三个必填理由
----------------------------------------
「先放着」是这条链上最容易出现的死路形态:它看起来是个动作,实际是不做决定。
必填理由把「不做决定」变成一条**可追责、可复盘**的记录。
DB 层的 ``chk_defgeo_sre_hold_reason`` 是承重的那一半 —— 应用层的校验会被绕过,
CHECK 不会。

🔴 服务商为什么不能自己 commit/release
--------------------------------------
钱在平台账本上,服务商是收款方之一。让他自己判「这单到底发出去没有」
等于让利益相关方裁定自己的账。Z-1 逐字:「收口权在平台」。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping, NamedTuple, Sequence

from services.defensive_geo.publish import publish_funding as _funding
from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoSettlementReview")

REVIEW_VERSION = "defgeo-settlement-review-v1"

#: 🔴 队列的分母:哪些资金态需要人工核验。**闭集**。
QUEUE_FUNDING_STATES: tuple[str, ...] = ("pending_reconciliation", "quarantined")

AdminAction = Literal["admin_commit", "admin_release", "admin_hold"]
#: Z-1 逐字的三条动作。多一条少一条都要改这里,判据拿它当分母。
ADMIN_ACTIONS: tuple[AdminAction, ...] = ("admin_commit", "admin_release", "admin_hold")

#: 服务商唯一能做的写动作。``verify_outcome`` 本身仍是只读。
PROVIDER_ACTION = "provider_evidence"

#: 处置后的目标资金态。``admin_hold`` **不改资金态** —— 它的作用是留痕 + 计时。
_ACTION_TARGET_STATE: dict[str, str | None] = {
    "admin_commit": "committed",
    "admin_release": "released",
    "admin_hold": None,
}

#: 对客/对服务商的人话(U-2 口径:凡涉钱必须说清"扣没扣/退没退")。
ACTION_LABELS: dict[str, str] = {
    "admin_commit": "确认已执行（扣除这笔算力）",
    "admin_release": "确认未执行（退回这笔算力）",
    "admin_hold": "维持隔离（需填写理由）",
    "provider_evidence": "提交线下核实凭证",
}


class ReviewError(RuntimeError):
    """处置不合法。**不动钱**。"""


class ReviewOutcome(NamedTuple):
    command_id: str
    action: str
    funding_state_before: str
    funding_state_after: str
    entry_id: int


def queue_states() -> tuple[str, ...]:
    return QUEUE_FUNDING_STATES


#: 🔴 [R3] 平台成本腿的"待人工核验"**不在 fundingState 上**(它恒为
#:    ``exempt_recorded``),而在 ``commandState`` 上。闭集,判据拿它当分母。
QUEUE_PLATFORM_COMMAND_STATES: tuple[str, ...] = (
    "settlement_pending", "quarantined", "needs_action",
)


def is_queue_member(command: Mapping[str, Any]) -> bool:
    """这条命令**当前**是否该出现在 Z-1 人工核验队列里。

    ═══════════════════════════════════════════════════════════════════
    🔴 [R3] 平台成本腿曾经**根本进不了这个队列**
    ═══════════════════════════════════════════════════════════════════
    队列的分母原来只按 ``fundingState`` 圈 —— 而迁移 044 的
    ``chk_defgeo_pcmd_platform_state`` 保证平台腿恒为 ``exempt_recorded``,
    于是它永远不满足 ``IN ('pending_reconciliation','quarantined')``:
    需要人工裁的平台单**既不出现在队列里,也会被处置接口当场拒绝**。

    与 ④⑦ 候选集那两个洞是同一类(「按一列圈范围,而那一列对某条腿是常量」),
    只是长在 ``settlement_review`` 而不是 ``reconciler`` ——
    我第一版的类锁只扫 ``reconciler.py``,所以没扫到它。现在类锁扫全包。

    平台腿的判别位换成 ``commandState`` + **还没结算**(``settled_at IS NULL``):
    已经结算完的账不许被人工再动一次(重复处置 = 重复扣/重复退)。
    """
    if _funding.is_platform_cost(str(command.get("funding_policy") or "")):
        if command.get("settled_at") is not None:
            return False
        return str(command.get("command_state") or "") in QUEUE_PLATFORM_COMMAND_STATES
    return str(command.get("funding_state") or "") in QUEUE_FUNDING_STATES


def assert_queue_member(command: Mapping[str, Any]) -> None:
    if not is_queue_member(command):
        raise ReviewError(
            f"该 command 的资金态是 {str(command.get('funding_state') or '')!r}、"
            f"命令态是 {str(command.get('command_state') or '')!r},不在核验队列里 —— "
            "已收敛的账不许被人工再动一次(重复处置 = 重复扣/重复退)"
        )


def _cas_bump(
    cur, command: Mapping[str, Any], *, admin_user_id: int, **updates: Any,
) -> dict[str, Any]:
    """🔴 [P0-4 要求③] 人工处置的第一手就带 **statusVersion CAS**。

    Codex 复现的分裂是这样来的:两个 admin 各自读到同一版状态,
    A 推 ``verified_published`` 去 commit、B 推 ``failed_no_effect`` 去 release ——
    两条都基于**自己读到的那一版旧状态**往前走,谁都没被拦。

    CAS 让后到的那一个 0 行:它读到的版本已经不是最新的了,
    于是**在动钱之前**就被挡回去,而不是先把钱动了再发现方向对不上。
    """
    fresh = _store.bump_status(
        cur, publish_command_id=str(command["publish_command_id"]),
        expect_status_version=int(command["status_version"]),
        **updates,
    )
    if fresh is None:
        raise ReviewError(
            "这条命令刚刚被另一个处置动作改过 —— 请刷新核验队列后重新裁定"
            "(两个相反的处置不得各自基于旧状态推进)"
        )
    return fresh


def _assert_settled(outcome: Any, publish_command_id: str) -> None:
    """物理结算没成功 ⇒ **整笔处置回滚**(端点对 ReviewError 是 rollback + 400)。

    🔴 为什么是抛而不是"记一笔再返回成功":admin 面是同步的,
       她按下「确认已执行」得到 200 的含义就是"这笔已经扣了"。
       物理腿没动却回 200,正是 P0-4 那条「业务账早于物理结算落终态」
       在人工面上的形态。抛出去 → billing 与 canonical bump 一起回滚 →
       这条命令原样留在核验队列里,可以再裁一次。
    """
    if outcome.settled:
        return
    logger.error("[defgeo-settlement-review] %s 物理结算未完成(%s/%s),整笔处置回滚",
                 publish_command_id, outcome.verdict, outcome.reason)
    raise ReviewError(
        "这笔的算力结算没有完成，处置未生效（费用保持冻结、不会多扣）——"
        "请稍后重试；若反复失败请转技术核实"
    )


async def apply_admin_action(
    cur,
    *,
    publish_command_id: str,
    action: str,
    admin_user_id: int,
    reason: str | None = None,
) -> ReviewOutcome:
    """平台 admin 逐条处置。**留痕与动钱在同一事务**。

    🔴 ``admin_hold`` 必填理由:应用层这里挡一次,DB 的
       ``chk_defgeo_sre_hold_reason`` 再挡一次。两道不是冗余 ——
       应用层这道给的是**人话报错**,DB 那道保证**绕不过**。
    """
    if action not in ADMIN_ACTIONS:
        raise ReviewError(f"未知处置动作 {action!r};合法 = {list(ADMIN_ACTIONS)}")
    if action == "admin_hold" and not (reason and reason.strip()):
        raise ReviewError("「维持隔离」必须填写理由 —— 没有理由的「先放着」等于把死路写进账本")

    command = _store.get_command_any_tenant(cur, publish_command_id=publish_command_id)
    if command is None:
        raise ReviewError("该发布命令不存在")
    assert_queue_member(command)

    before = str(command["funding_state"])
    target = _ACTION_TARGET_STATE[action]

    if target == "committed":
        # 🔴 admin 说「确认已执行」= 提供了 canonical outcome。
        #    先把 canonical state 推到 verified_published,再走真值表结算 ——
        #    绕过 canonical state 直接 commit 会让 §15.7 真值表失去约束力。
        fresh = _cas_bump(
            cur, command, admin_user_id=admin_user_id,
            canonical_publication_state="verified_published",
            status_reason=f"平台核验确认已发布（处置人 {admin_user_id}）",
        )
        # 🔴 [R3 · Owner 批口径①] 平台腿与钱包腿同构:人工核验确认已执行,
        #    平台账那笔冻结同样要 commit(否则它永远悬着 —— 与收敛器同一个理由)。
        #    ``fundingState`` 的不变性由 ``bump_status`` 的 CASE 保证,
        #    这里不再写第二遍平台三元式(同一谓词写两处必有一处没人验)。
        # 🔴 [P0-4] 终态(committed/completed)现在写在 ``commit_exact`` 内部,
        #    与 billing **同一事务 + statusVersion CAS**;物理没成功就不写。
        outcome = await _funding.commit_exact(
            cur, fresh, reason="平台核验确认已执行", terminal_command_state="completed")
        _assert_settled(outcome, publish_command_id)
    elif target == "released":
        fresh = _cas_bump(
            cur, command, admin_user_id=admin_user_id,
            canonical_publication_state="failed_no_effect",
            status_reason=f"平台核验确认未执行，费用已退回（处置人 {admin_user_id}）",
        )
        # 🔴 [R3] 同上:平台腿的 release 也不再被挡。
        outcome = await _funding.release_exact(
            cur, fresh, reason="平台核验确认未执行", terminal_command_state="cancelled")
        _assert_settled(outcome, publish_command_id)
    else:
        # 维持隔离:资金态**不变**,只把 command 推进 quarantined 以便计时与告警。
        _store.bump_status(
            cur, publish_command_id=publish_command_id,
            funding_state="quarantined", command_state="quarantined",
            status_reason="结果待平台核实，费用已冻结、不会多扣（无需操作）",
        )

    after_row = _store.get_command_any_tenant(cur, publish_command_id=publish_command_id)
    assert after_row is not None
    after = str(after_row["funding_state"])
    entry = _store.insert_review_entry(
        cur, publish_command_id=publish_command_id, entry_kind=action,
        actor_user_id=admin_user_id, actor_role="platform_admin",
        reason=reason, funding_state_before=before, funding_state_after=after,
    )
    logger.info(
        "[defgeo-settlement-review] %s %s by admin=%s %s→%s",
        publish_command_id, action, admin_user_id, before, after,
    )
    return ReviewOutcome(publish_command_id, action, before, after, int(entry["id"]))


def submit_provider_evidence(
    cur,
    *,
    publish_command_id: str,
    tenant_owner_id: int,
    provider_user_id: int,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """服务商提交线下核实凭证。**零资金副作用**。

    🔴 归属用 ``get_command`` 带 tenant 过滤(不是查到之后再比)——
       跨租户与不存在同形 404(POR-14)。
    """
    command = _store.get_command(
        cur, publish_command_id=publish_command_id, tenant_owner_id=tenant_owner_id,
    )
    if command is None:
        raise ReviewError("该发布命令不存在")
    assert_queue_member(command)
    if not evidence:
        raise ReviewError("凭证内容不能为空")
    return _store.insert_review_entry(
        cur, publish_command_id=publish_command_id, entry_kind=PROVIDER_ACTION,
        actor_user_id=provider_user_id, actor_role="service_provider",
        reason=None, evidence_payload=dict(evidence),
        funding_state_before=str(command["funding_state"]),
        funding_state_after=str(command["funding_state"]),   # 服务商动不了钱
    )


def census() -> dict[str, Any]:
    return {
        "reviewVersion": REVIEW_VERSION,
        "queueFundingStates": list(QUEUE_FUNDING_STATES),
        "adminActions": list(ADMIN_ACTIONS),
        "providerAction": PROVIDER_ACTION,
        "actionTargetState": dict(_ACTION_TARGET_STATE),
        "actionLabels": dict(ACTION_LABELS),
        "reasonRequiredActions": ["admin_hold"],
    }
