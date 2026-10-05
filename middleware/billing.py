"""
积分扣费中间件
在 API handler 调用业务逻辑之前检查余额并扣费
扣费优先级: bonus_points 先扣 → paid_points 兜底
requires_paid_points=true 的功能只扣 paid_points

[单账本收敛 2026-07-27 · Owner 授权改本文件,仅限拆除 V3.5 分流]
本文件曾按客户身份分两个账本扣费(user_wallets vs customer_agent_credit_wallets)。
Owner 2026-07-27 定:「不能有两本账,用户只有充值算力和赠送算力」,且资金唯一权威源
(OmniRank_定价成本_生产真值_SSOT_2026-06-27.md)里根本没有"信用钱包"这个概念。
阶段①已把 5 个客户的额度并回 user_wallets,本批拆掉分流:

- **所有用户统一走 user_wallets**(paid / bonus / commission / frozen_points)
- 已删除:_is_v35_customer / _v35_check_credit_only / _v35_credit_error_to_http /
  _refund_v35_customer_credit / _v35_confirmed_refund_points,以及
  check_balance_only、deduct_points、refund_points、freeze_points 里的按身份分流
- 🔴 **刻意保留**:commit_freeze / release_freeze 的 v35 路由 + _route_freeze_table。
  它们按冻结记录【实际所在表】判定(BUG-P2 机制),不看客户当前身份 ——
  切换瞬间若有在途 v35 冻结,必须仍能正确结算(工单 §4.2 点名的坑)。
- 扣费顺序 / deduction_preference / 幂等键 / freeze-commit-release 主干 / debt offset
  等与本单无关的逻辑**一行未动**。

- 文案护城河:客户面 message 不漏 pool/tool/publish/SKU/agent_user_id

文档:docs/AI-CONTEXT/V35_FOLLOWUP_PLAN_2026-05-26.md §1.3-1.5
"""

import hashlib
import json
import logging
import re
import time
import shortuuid
from typing import TYPE_CHECKING, Optional
from fastapi import HTTPException
from db.connection import get_db
from db.wallet_db import get_or_create_wallet, get_feature_pricing, insert_transaction
# [单账本收敛 2026-07-27] 原 import consume_credit / is_publish_feature /
# InsufficientCreditError / PublishPaidOnlyError —— 全部只服务于已删除的 V3.5 分流,
# 现无调用方。services.customer_credit 模块本身在阶段③处置,本文件不再依赖它。

if TYPE_CHECKING:
    from services.notification_events import RefundNotificationContext
    from services.organization_contract import BillingActorContext

logger = logging.getLogger("GEO-Billing")


class _BorrowedCursorTransaction:
    """Adapter that lets legacy billing code reuse a caller-owned transaction.

    It deliberately has no commit/rollback behavior.  The organization service
    owns the surrounding transaction and both wallet and authorization legs.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor

    def rollback(self):
        raise RuntimeError("borrowed billing transaction cannot rollback")


def _billing_transaction(cursor=None):
    return get_db() if cursor is None else _BorrowedCursorTransaction(cursor)


# ==========================================================================
# V3.5 工厂模式分流 helper(2026-05-26 批 1A/1B)
# ==========================================================================

# [单账本收敛 2026-07-27] _is_v35_customer 已删除。
# 依据:Owner「不能有两本账,用户只有充值算力和赠送算力」+ 资金 SSOT 无此概念。
# 阶段①已把 5 个客户的信用额度并入 user_wallets,此后【所有用户统一走 user_wallets】,
# 扣费链不再按身份分流 —— 判定函数没有调用方,留着只会诱导下一个人重建双轨。
# ⚠️ 冻结句柄的路由【不受影响】:_route_freeze_table 按冻结记录【实际所在表】判,
#    从来不用 _is_v35_customer(见该函数 BUG-P2 注释),历史冻结仍能正确 commit/release。

def _route_freeze_table(cursor, freeze_id=None, task_ref=None, user_id=None, freeze_table=None):
    """[BUG-P2] commit/release 按冻结记录【实际所在表】路由 · 而非按客户【当前】是否 V3.5。

    原 bug:freeze 在 legacy point_freezes 创建后,客户运行期开通 V3.5(customer_agent_credit_wallets
    出现 row)→ _is_v35_customer 翻 True → commit/release 错路由到 customer_credit_freezes → 找不到
    → success=False → frozen_points 永不结算 → sweeper 12h 误当 zombie release 白退(任务成功仍退钱)。

    [A0 根治] 调用方若回传 freeze_points 句柄里的 freeze_table('legacy'|'v35')→ 直接用,不猜,
    从根上消除 freeze_id 跨表撞号歧义。仅无标记的老调用方回落到下方 ①② 消歧(A1+A2+A3 保证安全)。

    消歧(无显式 freeze_table 时):freeze_id 跨两表独立自增必撞号 → 有 user_id 时按 user_id/customer_user_id 约束。
    ① status='frozen' 优先:先匹配【可操作的 frozen 行】· 避免一表 committed/released 的撞号行
       shadow 掉另一表的 frozen 行(否则该 frozen 行永不结算 → 又白退)。
       [A3] frozen 探测带 FOR UPDATE:在调用方同一事务内锁住候选行 · 路由判定到结算落账之间
       不留 TOCTOU 窗口(并发把 frozen→committed 后 frozen-first 失效漏退)。
    ② 无 frozen(幂等 re-call:目标已 committed/released · 终态不变)→ 按存在性 legacy-first 定位返回 no-op。
    [A2] 两表同 user 双 frozen 撞号(无法纯按 id 区分)→ 返回 'ambiguous' 哨兵 · 调用方禁止动钱转人工。
    返回 'legacy' / 'v35' / 'ambiguous' / None(两表都无)。
    """
    # [A0] 显式 table 标记 → 直接用 · 不猜(根除撞号歧义)
    if freeze_table in ("legacy", "v35"):
        return freeze_table

    # [复核加固] 走到这里=无显式 table 的消歧路径。user_id 缺失时探测/加锁不能 user-scope,
    # 撞号下可能锁/匹配到别客户同 id 行。所有真实 caller 都传 user_id(A1 已堵 sweeper 的 None 源);
    # 仍 None 属异常,记 warning + 下方 FOR UPDATE 仅在 user_id 非空时加(不跨客户锁)。
    if user_id is None:
        logger.warning(f"[Billing] _route_freeze_table 消歧路径 user_id=None(freeze_id={freeze_id} "
                       f"task_ref={task_ref})· 退化为非 user-scope 探测 · 不加跨客户锁")

    def _exists(table, uid_col, only_frozen):
        # table/uid_col 为硬编码字面量(非外部输入)· 无注入风险
        status_cond = " AND status = 'frozen'" if only_frozen else ""
        # [A3] 锁住可操作的 frozen 候选行;[复核加固] 仅 user_id 非空时加锁,避免 None 时跨客户锁串扰
        lock_cond = " FOR UPDATE" if (only_frozen and user_id is not None) else ""
        if freeze_id is not None:
            if user_id is not None:
                cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s AND {uid_col} = %s{status_cond} LIMIT 1{lock_cond}",
                               (freeze_id, user_id))
            else:
                cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s{status_cond} LIMIT 1{lock_cond}", (freeze_id,))
        elif task_ref is not None:
            if user_id is not None:
                cursor.execute(f"SELECT 1 FROM {table} WHERE task_ref = %s AND {uid_col} = %s{status_cond} LIMIT 1{lock_cond}",
                               (task_ref, user_id))
            else:
                cursor.execute(f"SELECT 1 FROM {table} WHERE task_ref = %s{status_cond} LIMIT 1{lock_cond}", (task_ref,))
        else:
            return False
        return cursor.fetchone() is not None

    def _v35_exists(only_frozen):
        # customer_credit_freezes 表可能未建(migration 未跑)· 兜底不崩
        try:
            return _exists("customer_credit_freezes", "customer_user_id", only_frozen)
        except Exception as exc:
            logger.warning(f"[Billing] _route_freeze_table customer_credit_freezes 查询跳过(表可能未建): {exc}")
            return False

    # ① frozen 优先(实际可操作状态 · FOR UPDATE 锁定 · 锁顺序恒 legacy→v35 防死锁)
    leg_frozen = _exists("point_freezes", "user_id", True)
    v35_frozen = _v35_exists(True)
    if leg_frozen and not v35_frozen:
        return "legacy"
    if v35_frozen and not leg_frozen:
        return "v35"
    if leg_frozen and v35_frozen:
        # [A2] 两表同 user 双 frozen 撞号 → 不猜 · 返回 ambiguous · 调用方 fail-closed 不动钱转人工
        logger.error(f"[Billing] freeze 跨表双 frozen 撞号歧义 id={freeze_id} task_ref={task_ref} "
                     f"user={user_id} · 拒绝自动结算 · 转人工核")
        return "ambiguous"

    # ② 无 frozen(幂等 re-call · 目标已 committed/released)→ 按存在性 legacy-first
    if _exists("point_freezes", "user_id", False):
        return "legacy"
    if _v35_exists(False):
        return "v35"
    return None


# [单账本收敛 2026-07-27] _v35_check_credit_only / _v35_credit_error_to_http 已删除。
# 前者是信用钱包的余额预检(只被 check_balance_only 的 v35 分支调用),
# 后者把 InsufficientCreditError/PublishPaidOnlyError 翻成 402(只被 v35 分支调用)。
# 统一走 user_wallets 后两者都没有调用方。

def _generate_order_id() -> str:
    """生成订单号: OR + 时间戳 + 短UUID，如 OR20260406-a3b5c7"""
    ts = time.strftime("%Y%m%d")
    uid = shortuuid.uuid()[:6].lower()
    return f"OR{ts}-{uid}"


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _raise_non_replayable_deduction_status(status: str) -> None:
    if status == "refund_pending":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CHARGE_REFUND_PENDING",
                "message": "该次扣费正在退款处理中，请刷新后重新发起导出",
            },
        )
    if status == "refunded":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CHARGE_REFUNDED",
                "message": "该次扣费已退款，请重新发起导出",
            },
        )


def _normalize_deduction_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
    """Validate the opt-in key without changing legacy callers that omit it."""
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "幂等键格式无效"},
        )
    key = idempotency_key.strip()
    if key != idempotency_key or not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "幂等键格式无效"},
        )
    return key


def _deduction_request_fingerprint(
    *,
    user_id: int,
    feature_code: str,
    total_cost: int,
    extra_cost: int,
    brand_id: Optional[int],
    requires_paid: bool,
) -> str:
    canonical = json.dumps(
        {
            "user_id": int(user_id),
            "feature_code": str(feature_code),
            "total_cost": int(total_cost),
            "extra_cost": int(extra_cost),
            "brand_id": int(brand_id) if brand_id is not None else None,
            "requires_paid": bool(requires_paid),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay_completed_deduction(
    *,
    idempotency_key: Optional[str],
    user_id: int,
    feature_code: str,
    extra_cost: int,
    brand_id: Optional[int],
) -> Optional[dict]:
    """Replay a committed result before mutable auth/pricing dependencies.

    The caller-supplied request parameters are immutable. The resolved price is
    stored with the first transaction, so a later price/configuration change
    must not turn the same request into a second charge or hide its charge ID.
    """
    if idempotency_key is None:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, feature_code, extra_cost, brand_id, status,
                   response_jsonb, charge_tx_id, ledger_type
              FROM billing_deduction_idempotency
             WHERE idempotency_key=%s
            """,
            (idempotency_key,),
        )
        existing = cursor.fetchone()
        if not existing:
            return None
        same_request = (
            int(existing["user_id"]) == int(user_id)
            and str(existing["feature_code"]) == str(feature_code)
            and int(existing["extra_cost"]) == int(extra_cost)
            and (
                int(existing["brand_id"])
                if existing.get("brand_id") is not None
                else None
            )
            == (int(brand_id) if brand_id is not None else None)
        )
        if not same_request:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_KEY_CONFLICT",
                    "message": "该请求标识已用于其他扣费参数，请重新操作",
                },
            )
        status = str(existing.get("status") or "")
        _raise_non_replayable_deduction_status(status)
        response = existing.get("response_jsonb")
        if status != "charged" or not isinstance(response, dict):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "IDEMPOTENCY_RESULT_UNAVAILABLE",
                    "message": "扣费结果暂时无法确认，请稍后重试",
                },
            )
        replayed = dict(response)
        charge_tx_id = existing.get("charge_tx_id")
        ledger_type = existing.get("ledger_type")

    if charge_tx_id is not None and ledger_type in ("legacy", "v35"):
        try:
            from services.billing_debt_offset_outbox import (
                process_debt_offset_for_charge,
            )

            process_debt_offset_for_charge(
                ledger_type=str(ledger_type),
                charge_tx_id=int(charge_tx_id),
                db_factory=get_db,
            )
        except Exception as exc:
            logger.error(
                "[BillingDebtOffset] replay recovery deferred charge=%s ledger=%s (%s)",
                charge_tx_id,
                ledger_type,
                type(exc).__name__,
            )
    return replayed


def _begin_deduction_idempotency(
    cursor,
    *,
    idempotency_key: Optional[str],
    user_id: int,
    feature_code: str,
    total_cost: int,
    extra_cost: int,
    brand_id: Optional[int],
    request_fingerprint: Optional[str],
) -> Optional[dict]:
    """Create the transaction-local owner row or return a committed response.

    INSERT ... ON CONFLICT serializes concurrent requests on the primary key.
    The new row remains invisible until the same transaction commits both the
    balance mutation and charged response. A hard process exit therefore
    leaves either no attempt/charge or one replayable charged attempt/charge.
    """
    if idempotency_key is None:
        return None
    cursor.execute(
        """
        INSERT INTO billing_deduction_idempotency
            (idempotency_key, user_id, feature_code, total_cost, extra_cost,
             brand_id, request_fingerprint, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'in_progress')
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING idempotency_key
        """,
        (
            idempotency_key,
            int(user_id),
            str(feature_code),
            int(total_cost),
            int(extra_cost),
            int(brand_id) if brand_id is not None else None,
            request_fingerprint,
        ),
    )
    if cursor.fetchone() is not None:
        return None

    cursor.execute(
        """
        SELECT user_id, feature_code, total_cost, extra_cost, brand_id,
               request_fingerprint, status, response_jsonb
          FROM billing_deduction_idempotency
         WHERE idempotency_key=%s
         FOR UPDATE
        """,
        (idempotency_key,),
    )
    existing = cursor.fetchone()
    if not existing:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "IDEMPOTENCY_STATE_UNAVAILABLE",
                "message": "扣费状态暂时无法确认，请稍后重试",
            },
        )
    same_request = (
        int(existing["user_id"]) == int(user_id)
        and str(existing["feature_code"]) == str(feature_code)
        and int(existing["total_cost"]) == int(total_cost)
        and int(existing["extra_cost"]) == int(extra_cost)
        and (
            int(existing["brand_id"]) if existing.get("brand_id") is not None else None
        )
        == (int(brand_id) if brand_id is not None else None)
        and str(existing["request_fingerprint"]) == str(request_fingerprint)
    )
    if not same_request:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_KEY_CONFLICT",
                "message": "该请求标识已用于其他扣费参数，请重新操作",
            },
        )
    status = str(existing.get("status") or "")
    _raise_non_replayable_deduction_status(status)
    response = existing.get("response_jsonb")
    if status != "charged" or not isinstance(response, dict):
        # A committed in_progress row is impossible on the normal path because
        # the owner insert and completion update share one transaction. Treat
        # manually corrupted state as unknown, never as permission to re-deduct.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "IDEMPOTENCY_RESULT_UNAVAILABLE",
                "message": "扣费结果暂时无法确认，请稍后重试",
            },
        )
    return dict(response)


def _complete_deduction_idempotency(
    cursor,
    *,
    idempotency_key: Optional[str],
    request_fingerprint: Optional[str],
    response: dict,
) -> None:
    if idempotency_key is None:
        return
    deducted = int(response.get("deducted") or 0)
    charge_tx_id = response.get("charge_tx_id")
    if deducted > 0:
        try:
            charge_tx_id = int(charge_tx_id)
        except (TypeError, ValueError) as exc:
            # This exception occurs before get_db commits, so balance and
            # transaction writes roll back together instead of creating an
            # unidentifiable charge.
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "BILLING_IDENTITY_UNAVAILABLE",
                    "message": "扣费未完成，请稍后重试",
                },
            ) from exc
    else:
        charge_tx_id = None
    ledger_type = response.get("channel") or ("legacy" if deducted > 0 else None)
    if ledger_type not in (None, "legacy", "v35"):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "BILLING_IDENTITY_UNAVAILABLE",
                "message": "扣费未完成，请稍后重试",
            },
        )
    cursor.execute(
        """
        UPDATE billing_deduction_idempotency
           SET status='charged', response_jsonb=%s::jsonb,
               charge_tx_id=%s, ledger_type=%s, deducted=%s,
               completed_at=NOW(), updated_at=NOW()
         WHERE idempotency_key=%s AND request_fingerprint=%s
           AND status='in_progress'
        RETURNING idempotency_key
        """,
        (
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            charge_tx_id,
            ledger_type,
            deducted,
            idempotency_key,
            request_fingerprint,
        ),
    )
    if cursor.fetchone() is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "IDEMPOTENCY_COMMIT_CONFLICT",
                "message": "扣费状态暂时无法确认，请稍后重试",
            },
        )


def _enqueue_deduction_debt_offset(
    cursor,
    *,
    user_id: int,
    feature_code: str,
    response: dict,
    idempotency_key: Optional[str],
) -> None:
    """Persist and try the per-charge debt advancement in this transaction."""
    deducted = int(response.get("deducted") or 0)
    if deducted <= 0:
        return
    try:
        charge_tx_id = int(response.get("charge_tx_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "BILLING_IDENTITY_UNAVAILABLE",
                "message": "扣费未完成，请稍后重试",
            },
        ) from exc
    ledger_type = response.get("channel") or "legacy"
    from services.billing_debt_offset_outbox import (
        enqueue_and_process_debt_offset_cursor,
    )

    enqueue_and_process_debt_offset_cursor(
        cursor,
        user_id=int(user_id),
        feature_code=str(feature_code),
        charge_tx_id=charge_tx_id,
        ledger_type=str(ledger_type),
        consumed_points=deducted,
        idempotency_key=idempotency_key,
    )


# ===========================================================================
# 2026-04-18 三轨积分拆分（v3.4）
#
# 三池：bonus_points / commission_points / paid_points
#   - bonus: 赠送/返利/膨胀，仅消费，不退款不提现
#   - commission: 代理佣金本金，消费 + 可提现，不原路退款
#   - paid: 用户自充，消费 + 可原路退款，不提现
#
# 扣费顺序（preference 由 user_wallets.deduction_preference 决定）:
#   'default'（默认 / 平台友好）: bonus → commission → paid（保留充值款退款权）
#   'agent_friendly'（代理友好）: bonus → paid → commission（保留佣金提现权）
#
# 媒体发布（requires_paid_points=True）: 不扣 bonus，只扣 commission + paid（按偏好）
# ==========================================================================

def _deduction_order(wallet: dict, *, requires_paid: bool, preference: str):
    """SSOT for the legacy physical-pool order used by deduct/freeze/settle."""
    bonus = (wallet.get("bonus_points", 0) or 0) if hasattr(wallet, "get") else (wallet["bonus_points"] or 0)
    commission = (wallet.get("commission_points", 0) or 0) if hasattr(wallet, "get") else 0
    paid = (wallet.get("paid_points", 0) or 0) if hasattr(wallet, "get") else (wallet["paid_points"] or 0)
    if requires_paid:
        return [('paid', paid), ('commission', commission)] if preference == 'agent_friendly' else [('commission', commission), ('paid', paid)]
    return [('bonus', bonus), ('paid', paid), ('commission', commission)] if preference == 'agent_friendly' else [('bonus', bonus), ('commission', commission), ('paid', paid)]


def _compute_deduction_split(wallet: dict, total_cost: int,
                              requires_paid: bool = False,
                              preference: str = 'default') -> dict:
    """把 total_cost 按扣费顺序拆到 bonus / commission / paid 三桶

    返回: {"bonus": X, "commission": Y, "paid": Z, "total_taken": X+Y+Z, "remaining": R}
    约束: 若 R > 0 表示余额不足（调用方自行抛 402）

    注意: 输入的 wallet 可以是 dict 或 psycopg2 RealDictRow，两者都支持 .get
    """
    remaining = int(total_cost)
    use = {"bonus": 0, "commission": 0, "paid": 0}
    for tag, avail in _deduction_order(wallet, requires_paid=requires_paid, preference=preference):
        if remaining <= 0:
            break
        take = min(int(avail), remaining)
        use[tag] += take
        remaining -= take

    use["total_taken"] = int(total_cost) - remaining
    use["remaining"] = remaining
    return use


def _actual_and_release_split(reserved_split: dict, actual_points: int) -> tuple[dict, dict]:
    """Allocate actual cost along the immutable order captured at reservation."""
    reserved = {name: int(reserved_split.get(name, 0) or 0) for name in ("bonus", "commission", "paid")}
    order = list(reserved_split.get("order") or [])
    if sorted(order) not in (["bonus", "commission", "paid"], ["commission", "paid"]):
        raise RuntimeError("invalid immutable reserved split order")
    total = sum(reserved.values())
    actual_points = int(actual_points)
    if actual_points < 0 or actual_points > total:
        raise ValueError("actual points outside reserved ceiling")
    remaining = actual_points
    actual = {"bonus": 0, "commission": 0, "paid": 0}
    for name in order:
        take = min(reserved[name], remaining)
        actual[name] = take
        remaining -= take
    if remaining:
        raise RuntimeError("reserved split cannot satisfy actual points")
    released = {name: reserved[name] - actual[name] for name in reserved}
    return actual, released


def _get_deduction_preference(wallet) -> str:
    """从 wallet 读取扣费偏好（迁移前老数据默认 'default'）"""
    if hasattr(wallet, "get"):
        return wallet.get("deduction_preference") or 'default'
    return wallet["deduction_preference"] if "deduction_preference" in wallet else 'default'


async def check_balance_only(user_id: int, feature_code: str, extra_cost: int = 0) -> dict:
    """
    只检查余额是否足够扣费，**不真扣**。用于"后扣费"模式。

    2026-04-18 v3.4: 三轨（bonus + commission + paid）合计检查。
    媒体发布 (requires_paid_points=True) 跳过 bonus，仅 commission + paid。

    2026-05-26 批 1A:V3.5 客户(customer_agent_credit_wallets 有 row)→ 走 V3.5 预检
    不 fallback 老 user_wallets · 余额不足直接 402(混合余额铁律)
    """
    from db.auth_db import get_user
    user_info = get_user(user_id)
    if user_info and user_info.get("is_admin"):
        return {"ok": True, "free": True, "admin_exempt": True, "would_deduct": 0}

    pricing = get_feature_pricing(feature_code)
    total_cost = pricing["cost_points"] + extra_cost
    if total_cost == 0:
        return {"ok": True, "free": True, "would_deduct": 0}

    # [单账本收敛 2026-07-27] 原来这里有一段 V3.5 预检分流:命中信用钱包的客户走
    # _v35_check_credit_only 查三池余额并直接 return channel='v35'。现已删除 ——
    # 阶段①把额度并回 user_wallets 之后,再按身份分流就会读到一个恒为 0 的空钱包。
    # 所有用户统一走下面这段原 legacy 预检(它本来就是 user_wallets 的正确实现)。
    # 注:原分支为查信用钱包额外开了一个 get_db() 连接,一并省掉。
    wallet = get_or_create_wallet(user_id)
    bonus = wallet.get("bonus_points", 0) or 0
    commission = wallet.get("commission_points", 0) or 0
    paid = wallet.get("paid_points", 0) or 0

    if pricing["requires_paid_points"]:
        # 媒体发布：只算 commission + paid
        available_pay = commission + paid
        if available_pay < total_cost:
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_PAID_POINTS",
                "message": "充值积分 + 佣金积分不足（媒体发布不可用赠送积分）",
                "required": total_cost,
                "available_pay": available_pay,
                "available_paid": paid,
                "available_commission": commission,
            })
    else:
        # AI 功能：三轨合计
        available = bonus + commission + paid
        if available < total_cost:
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_POINTS",
                "message": f"积分不足，需要 {total_cost}",
                "required": total_cost,
                "available": available,
                "available_paid": paid,
                "available_commission": commission,
                "available_bonus": bonus,
            })
    return {"ok": True, "would_deduct": total_cost}


async def deduct_points(
    user_id: int,
    feature_code: str,
    extra_cost: int = 0,
    brand_id: int = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    统一扣费入口（2026-04-18 v3.4 三轨化）。

    扣费顺序由 user_wallets.deduction_preference 决定：
      'default': bonus → commission → paid（平台友好，保留充值款原路退款权）
      'agent_friendly': bonus → paid → commission（代理友好，保留佣金提现权）

    媒体发布 (requires_paid_points=True) 跳过 bonus，只从 commission + paid 扣。

    返回:
        {"success": True, "deducted": 650, "deducted_bonus": ..., "deducted_commission": ...,
         "deducted_paid": ..., "balance": {...}}
    异常:
        HTTPException 402 — 余额不足
    """
    # Optional and additive: callers that omit the key follow the exact legacy
    # path. Opt-in callers get a transaction-bound replayable deduction result.
    idempotency_key = _normalize_deduction_idempotency_key(idempotency_key)
    replayed = _replay_completed_deduction(
        idempotency_key=idempotency_key,
        user_id=user_id,
        feature_code=feature_code,
        extra_cost=extra_cost,
        brand_id=brand_id,
    )
    if replayed is not None:
        return replayed

    # 管理员免扣费
    from db.auth_db import get_user
    user_info = get_user(user_id)
    if user_info and user_info.get("is_admin"):
        logger.info(f"[Billing] 管理员 user_id={user_id} 免扣费: {feature_code}")
        return {"success": True, "deducted": 0, "free": True, "admin_exempt": True}

    pricing = get_feature_pricing(feature_code)
    total_cost = pricing["cost_points"] + extra_cost

    if total_cost == 0:
        return {"success": True, "deducted": 0, "free": True}

    requires_paid = pricing.get("requires_paid_points", False)
    request_fingerprint = (
        _deduction_request_fingerprint(
            user_id=user_id,
            feature_code=feature_code,
            total_cost=total_cost,
            extra_cost=extra_cost,
            brand_id=brand_id,
            requires_paid=requires_paid,
        )
        if idempotency_key is not None
        else None
    )

    with get_db() as conn:
        cursor = conn.cursor()
        replayed = _begin_deduction_idempotency(
            cursor,
            idempotency_key=idempotency_key,
            user_id=user_id,
            feature_code=feature_code,
            total_cost=total_cost,
            extra_cost=extra_cost,
            brand_id=brand_id,
            request_fingerprint=request_fingerprint,
        )
        if replayed is not None:
            return replayed

        # [单账本收敛 2026-07-27] 原来这里有一段 V3.5 扣费分流:命中信用钱包的客户走
        # consume_credit 扣三池(tool/publish/bonus),并返回 channel='v35' + charge_tx_ids。
        # 现已删除,理由同上 —— 额度已并回 user_wallets,再分流只会扣到空钱包(402)。
        # 🔴 删除范围严格限定在"按身份分流"这件事:下面 legacy 段的扣费顺序、
        #    deduction_preference、幂等键、debt offset 等逻辑【一行未动】。
        # legacy 路径(原 user_wallets 三轨化保留)
        # 悲观锁：SELECT FOR UPDATE 锁行后再扣减（防并发竞态）
        cursor.execute(
            "SELECT paid_points, bonus_points, commission_points, deduction_preference "
            "FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        locked_wallet = cursor.fetchone()
        if not locked_wallet:
            raise HTTPException(status_code=402, detail={"code": "NO_WALLET", "message": "钱包不存在"})

        preference = _get_deduction_preference(locked_wallet)
        split = _compute_deduction_split(locked_wallet, total_cost,
                                          requires_paid=requires_paid,
                                          preference=preference)

        if split["remaining"] > 0:
            # 余额不足通知（重要级别）
            try:
                from utils.notify import notify_user, LEVEL_IMPORTANT
                notify_user(
                    user_id,
                    f"积分余额不足，{pricing['feature_name']}需要 {total_cost} 积分",
                    level=LEVEL_IMPORTANT,
                    type="wallet",
                    link="/wallet",
                )
            except Exception:
                pass
            if requires_paid:
                raise HTTPException(status_code=402, detail={
                    "code": "INSUFFICIENT_PAID_POINTS",
                    "message": "充值积分 + 佣金积分不足（媒体发布不可用赠送积分）",
                    "required": total_cost,
                    "available_pay": (locked_wallet.get("commission_points") or 0) + (locked_wallet.get("paid_points") or 0),
                })
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_POINTS",
                "message": f"积分不足，需要 {total_cost} 积分",
                "required": total_cost,
                "available": (locked_wallet.get("bonus_points") or 0) + (locked_wallet.get("commission_points") or 0) + (locked_wallet.get("paid_points") or 0),
                "available_paid": locked_wallet.get("paid_points") or 0,
                "available_commission": locked_wallet.get("commission_points") or 0,
                "available_bonus": locked_wallet.get("bonus_points") or 0,
            })

        deduct_bonus = split["bonus"]
        deduct_commission = split["commission"]
        deduct_paid = split["paid"]

        cursor.execute("""
            UPDATE user_wallets
            SET bonus_points = bonus_points - %s,
                commission_points = commission_points - %s,
                paid_points = paid_points - %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING paid_points, bonus_points, commission_points
        """, (deduct_bonus, deduct_commission, deduct_paid, user_id))

        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=402, detail={
                "code": "DEDUCT_CONFLICT",
                "message": "扣费失败，请重试",
            })

        order_id = _generate_order_id()

        # V3.3.1:消费流水 source='balance_deduction'(§3.6 防套利铁律)
        # [GEO-R2-CAN-039 返工] 捕获主 consume 笔 id → charge_tx_id;同一次拆分扣费共享 order_id,
        #   退款方按 charge_tx_id 反查 order_id 组精确退全部拆分笔(消除并发退错笔 + 少退拆分组)。
        _charge_tx_id = None
        if deduct_bonus > 0:
            _tid = insert_transaction(cursor, user_id, "consume", "bonus", -deduct_bonus,
                             result["bonus_points"], feature_code,
                             description=pricing["feature_name"], order_id=order_id, brand_id=brand_id,
                             source="balance_deduction")
            if _charge_tx_id is None:
                _charge_tx_id = _tid
        if deduct_commission > 0:
            _tid = insert_transaction(cursor, user_id, "consume", "commission", -deduct_commission,
                             result["commission_points"], feature_code,
                             description=pricing["feature_name"], order_id=order_id, brand_id=brand_id,
                             source="balance_deduction")
            if _charge_tx_id is None:
                _charge_tx_id = _tid
        if deduct_paid > 0:
            _tid = insert_transaction(cursor, user_id, "consume", "paid", -deduct_paid,
                             result["paid_points"], feature_code,
                             description=pricing["feature_name"], order_id=order_id, brand_id=brand_id,
                             source="balance_deduction")
            if _charge_tx_id is None:
                _charge_tx_id = _tid

        deduction_result = {
            "success": True,
            "deducted": total_cost,
            "deducted_bonus": deduct_bonus,
            "deducted_commission": deduct_commission,
            "deducted_paid": deduct_paid,
            "order_id": order_id,
            "charge_tx_id": _charge_tx_id,
            "balance": {
                "paid_points": result["paid_points"],
                "commission_points": result["commission_points"],
                "bonus_points": result["bonus_points"],
                "total": (
                    result["paid_points"]
                    + result["commission_points"]
                    + result["bonus_points"]
                ),
            },
        }
        _complete_deduction_idempotency(
            cursor,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response=deduction_result,
        )
        _enqueue_deduction_debt_offset(
            cursor,
            user_id=user_id,
            feature_code=feature_code,
            response=deduction_result,
            idempotency_key=idempotency_key,
        )

    logger.info(f"[Billing] 用户{user_id} 扣费 {feature_code}: "
                f"bonus={deduct_bonus} commission={deduct_commission} paid={deduct_paid} "
                f"total={total_cost} order={order_id}")

    return deduction_result


def _cumulative_refund_target(original_points: int, amount: int = None) -> int:
    """Return the cumulative refund target for an existing charge group.

    The historical API has no independent refund request id.  Treating
    ``amount`` as an increment makes a network retry indistinguishable from a
    second refund and can over-credit the wallet.  It therefore represents the
    desired cumulative refunded amount.  ``None`` means the full original
    charge.  Values above the original charge are safely capped.
    """
    original_points = max(0, int(original_points or 0))
    if amount is None:
        return original_points
    requested = int(amount)
    if requested < 0:
        raise ValueError("refund amount must be non-negative")
    return min(requested, original_points)


def _validated_refund_group_totals(
    rows: list,
    *,
    original_field: str,
    refunded_field: str,
    ledger_label: str,
) -> tuple[int, int]:
    """Validate every consume leg before calculating a group refund target.

    A group-level sum is not sufficient: an over-refunded bonus leg can be
    hidden by an under-refunded paid/tool leg while the group total still looks
    valid.  Refuse that dirty state before any wallet or lifecycle mutation.
    """
    original_total = 0
    confirmed_total = 0
    for row in rows:
        original = abs(int(row.get(original_field) or 0))
        refunded = int(row.get(refunded_field) or 0)
        if refunded < 0 or refunded > original:
            raise RuntimeError(f"{ledger_label} refund leg exceeds original charge")
        original_total += original
        confirmed_total += refunded
    return original_total, confirmed_total


# [单账本收敛 2026-07-27] _refund_v35_customer_credit 已删除。
# 它是「V3.5 客户退费对称退回信用钱包」的实现,只被 refund_points 的 v35 分支调用。
# 单账本后退款一律回 user_wallets,该函数无调用方。
# ⚠️ 历史退款流水(customer_credit_transactions)不受影响,表保留只读供审计。

def _select_split_refund_txs(consume_txs: list) -> list:
    """[BUG-P2] 从候选 consume 中选出"同一次拆分扣费"的笔(并组退,上游已 LIMIT 3)。

    - 有 order_id → 严格【同 order_id】才并组(同一次拆分扣费共享 order_id,如 FRZ/CMT/OR 前缀);
    - base_order 为 NULL(历史无 order_id 行)→ 才用 5 秒窗兜底,且对方也须 order_id 为 NULL。

    原 BUG:对"有 order_id 但不同"的行也用 5 秒窗并组 → 把 5 秒内多笔【不同 order_id】的
    独立扣费(批量诊断逐笔/双击/并发)误并退,一次失败多退 2-3 笔。
    """
    if not consume_txs:
        return []
    base_order = consume_txs[0].get("order_id")
    out = [consume_txs[0]]
    for tx in consume_txs[1:]:
        if base_order is not None:
            if tx.get("order_id") == base_order:
                out.append(tx)
        else:
            if tx.get("order_id") is None:
                if abs((consume_txs[0]["created_at"] - tx["created_at"]).total_seconds()) <= 5:
                    out.append(tx)
    return out


def _validate_refund_notification(notification, charge_tx_id: int = None) -> None:
    """Validate the typed notification before any wallet mutation occurs."""
    if notification is None:
        return
    from datetime import datetime, timezone
    from services.notification_events import RefundNotificationContext, render_notification

    if not isinstance(notification, RefundNotificationContext):
        raise TypeError("notification must be RefundNotificationContext")
    render_notification(
        notification.event_type,
        notification.recipient_kind,
        {
            "business_no": notification.business_no,
            "points": "0",
            "status": notification.status,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": notification.summary,
        },
    )


def _enqueue_confirmed_refund_notification(cursor, *, user_id: int, points: int, notification) -> None:
    if notification is None:
        return
    from datetime import datetime, timezone
    from services.notification_outbox import enqueue_notification_event

    enqueue_notification_event(
        cursor,
        event_type=notification.event_type,
        business_id=notification.business_id,
        terminal_state=notification.terminal_state,
        recipient_user_id=int(user_id),
        recipient_kind=notification.recipient_kind,
        facts={
            "business_no": notification.business_no,
            "points": str(int(points)),
            "status": notification.status,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": notification.summary,
        },
    )


def _legacy_confirmed_refund_points(cursor, *, user_id: int, feature_code: str, charge_tx_id: int) -> int:
    """Read already-committed refunds for one exact legacy charge group."""
    cursor.execute(
        "SELECT order_id FROM point_transactions "
        "WHERE id=%s AND user_id=%s AND feature_code=%s AND type='consume'",
        (int(charge_tx_id), int(user_id), feature_code),
    )
    charge = cursor.fetchone()
    if not charge:
        return 0
    if charge.get("order_id") is not None:
        cursor.execute(
            "SELECT id::text AS id FROM point_transactions "
            "WHERE user_id=%s AND feature_code=%s AND type='consume' AND order_id=%s",
            (int(user_id), feature_code, charge["order_id"]),
        )
    else:
        cursor.execute(
            "SELECT id::text AS id FROM point_transactions WHERE id=%s AND user_id=%s AND type='consume'",
            (int(charge_tx_id), int(user_id)),
        )
    consume_ids = [row["id"] for row in cursor.fetchall()]
    if not consume_ids:
        return 0
    cursor.execute(
        "SELECT COALESCE(SUM(amount),0)::bigint AS refunded FROM point_transactions "
        "WHERE user_id=%s AND type='refund' AND order_id=ANY(%s)",
        (int(user_id), consume_ids),
    )
    return int((cursor.fetchone() or {}).get("refunded") or 0)


# [单账本收敛 2026-07-27] _v35_confirmed_refund_points 已删除(仅服务于 v35 退款路径)。
# legacy 侧的 _legacy_confirmed_refund_points 保留不动,它现在是唯一路径。

def _lock_exact_charge_refund_context_cursor(
    cursor,
    *,
    user_id: int,
    feature_code: str,
    charge_tx_id: int | None,
    ledger_type: str,
) -> dict | None:
    """Acquire the global refund lock order before any wallet row is locked."""
    if charge_tx_id is None:
        return None
    from services.billing_debt_offset_outbox import (
        lock_charge_refund_context_cursor,
    )

    return lock_charge_refund_context_cursor(
        cursor,
        user_id=int(user_id),
        feature_code=str(feature_code),
        charge_tx_id=int(charge_tx_id),
        ledger_type=ledger_type,
    )


def _reconcile_exact_charge_refund_cursor(
    cursor,
    *,
    context: dict | None,
    confirmed_refund_points: int,
) -> dict:
    from services.billing_debt_offset_outbox import reconcile_charge_refund_cursor

    return reconcile_charge_refund_cursor(
        cursor,
        context=context,
        confirmed_refund_points=int(confirmed_refund_points),
    )


async def refund_points(user_id: int, feature_code: str, reason: str,
                        amount: int = None, charge_tx_id: int = None,
                        ledger_type: str = None,
                        notification: Optional["RefundNotificationContext"] = None,
                        _cursor=None) -> dict:
    """
    退费：默认查最近一笔该功能的扣费记录，原路退回。
    支持拆分扣费（bonus+paid 两笔）的完整退费，带幂等性保护和行锁。

    [GEO-R6-CAN-009 / R2-CAN-039 老板批 2026-07-12 · additive · 默认行为不变向后兼容]
      - charge_tx_id: 指定要退的那笔 consume 的不可变 id(deduct_points 返回的 charge_tx_id)→
        精确退该笔(及其同 order_id 拆分组),不再靠 newest-by-feature(修 R2-CAN-039:两并发同
        user+feature 退错笔)。
      - amount: 没有 refund_request_id 的兼容接口采用【累计退款目标】语义。默认 None =
        退款到原扣费全额；传 4 后重试 4 不重复退，随后传 6 只补退 2。该契约既允许
        部分成功退款，也保证网络重试、恢复 worker 和多 worker 不会少退或超退。

    [v9 · Deploy-CTO NO-GO P1-3 老板批红线 additive · 默认行为不变向后兼容]
      - ledger_type: [单账本收敛 2026-07-27 后] 只剩一个账本,该参数不再影响退款路由,
        但**形参保留**:它仍作为锁上下文的一部分传给
        _lock_exact_charge_refund_context_cursor,且外部调用方(server.py 文章恢复链、
        fund_recovery_orders)仍在传值。删它会改签名并断掉那条链。
    """
    _validate_refund_notification(notification, charge_tx_id)
    with _billing_transaction(_cursor) as conn:
        cursor = conn.cursor()

        # [单账本收敛 2026-07-27] 原来这里按 ledger_type / _is_v35_customer 选退款账本。
        # 单账本后只剩一个账本,恒为 'legacy'(= user_wallets)。
        # 🔴 ledger_type 形参【保留不删】:它还要往下传给
        #    _lock_exact_charge_refund_context_cursor 作为锁上下文的一部分,
        #    且外部调用方(server.py 的文章恢复链、fund_recovery_orders)仍在传值。
        #    删形参会改签名、断掉那条链 —— 本单只拆"按身份分流",不动调用契约。
        _resolved_ledger_type = "legacy"
        # Fixed global lock order: deduction lifecycle -> debt receipt -> wallet
        # -> debt rows.  Both the direct and recovery refund paths use this
        # exact context, so a scheduler can never advance debt after a refund.
        refund_debt_context = _lock_exact_charge_refund_context_cursor(
            cursor,
            user_id=user_id,
            feature_code=feature_code,
            charge_tx_id=charge_tx_id,
            ledger_type=_resolved_ledger_type,
        )
        # FIX-3: 悲观锁，防止并发重复退费
        cursor.execute(
            "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,))
        locked_wallet = cursor.fetchone()
        if not locked_wallet:
            return {"success": False, "reason": "钱包不存在"}

        # 每条 consume 保留在候选集中，并带上已关联 refund 的累计金额。
        # 旧实现用 NOT IN 排除任何出现过退款的 consume，导致部分退款后无法补足。
        _legacy_consume_columns = """
            c.*,
            COALESCE((
                SELECT SUM(r.amount)
                  FROM point_transactions r
                 WHERE r.user_id = c.user_id
                   AND r.type = 'refund'
                   AND r.order_id = c.id::text
            ), 0)::bigint AS refunded_amount
        """
        if charge_tx_id is not None:
            # [R2-CAN-039 返工修 2026-07-12] 精确退指定 charge:先按不可变 id 反查其 order_id,
            # 退【整个 order_id 拆分组】(bonus+commission+paid 同一次扣费共享 order_id),不再只查单行。
            cursor.execute(
                "SELECT order_id FROM point_transactions "
                "WHERE id=%s AND user_id=%s AND feature_code=%s AND type='consume'",
                (charge_tx_id, user_id, feature_code),
            )
            _origin = cursor.fetchone()
            if not _origin:
                return {"success": False, "reason": "指定扣费记录不存在"}
            if _origin.get("order_id") is not None:
                cursor.execute(f"""
                    SELECT {_legacy_consume_columns}
                      FROM point_transactions c
                     WHERE c.user_id=%s AND c.feature_code=%s AND c.type='consume'
                       AND c.order_id=%s
                     ORDER BY c.created_at, c.id
                """, (user_id, feature_code, _origin["order_id"]))
            else:
                cursor.execute(f"""
                    SELECT {_legacy_consume_columns}
                      FROM point_transactions c
                     WHERE c.id=%s AND c.user_id=%s AND c.feature_code=%s
                       AND c.type='consume'
                """, (charge_tx_id, user_id, feature_code))
            consume_txs = cursor.fetchall()
        else:
            cursor.execute(f"""
                SELECT {_legacy_consume_columns}
                  FROM point_transactions c
                 WHERE c.user_id=%s AND c.feature_code=%s AND c.type='consume'
                 ORDER BY c.created_at DESC, c.id DESC LIMIT 3
            """, (user_id, feature_code))
            _candidates = cursor.fetchall()
            if _candidates and _candidates[0].get("order_id") is not None:
                cursor.execute(f"""
                    SELECT {_legacy_consume_columns}
                      FROM point_transactions c
                     WHERE c.user_id=%s AND c.feature_code=%s AND c.type='consume'
                       AND c.order_id=%s
                     ORDER BY c.created_at, c.id
                """, (user_id, feature_code, _candidates[0]["order_id"]))
                consume_txs = cursor.fetchall()
            else:
                consume_txs = _select_split_refund_txs(_candidates)

        if not consume_txs:
            return {"success": False, "reason": "未找到扣费记录"}

        txs_to_refund = consume_txs
        original_total, confirmed_before = _validated_refund_group_totals(
            txs_to_refund,
            original_field="amount",
            refunded_field="refunded_amount",
            ledger_label="legacy",
        )
        target = _cumulative_refund_target(original_total, amount)
        remaining_target = max(0, target - confirmed_before)

        total_refunded = 0
        refund_details = []
        _remaining = remaining_target

        for tx in txs_to_refund:
            if _remaining <= 0:
                break
            refundable = abs(int(tx["amount"] or 0)) - max(
                0, int(tx.get("refunded_amount") or 0)
            )
            refund_amount = min(max(0, refundable), _remaining)
            if refund_amount <= 0:
                continue
            point_type = tx["point_type"]

            # v3.4 三轨归位（bonus / commission / paid 各自对应 wallet 列）
            if point_type == "bonus":
                col = "bonus_points"
            elif point_type == "commission":
                col = "commission_points"
            else:
                col = "paid_points"
            cursor.execute(f"""
                UPDATE user_wallets
                SET {col} = {col} + %s, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
                RETURNING {col}
            """, (refund_amount, user_id))
            result = cursor.fetchone()
            balance_after = result[col] if result else 0

            # order_id 存原始扣费 tx.id,用于幂等性判断
            # V3.3.1(Codex 二审 P1-9):refund 流水 source='balance_deduction'
            insert_transaction(cursor, user_id, "refund", point_type,
                              refund_amount, balance_after, feature_code,
                              description=f"退费: {reason}",
                              order_id=str(tx["id"]),
                              source="balance_deduction")

            total_refunded += refund_amount
            _remaining -= refund_amount
            refund_details.append({"amount": refund_amount, "point_type": point_type})

        if total_refunded != remaining_target:
            raise RuntimeError("legacy refund target cannot be satisfied by charge ledger")

        confirmed_points = (
            _legacy_confirmed_refund_points(
                cursor,
                user_id=user_id,
                feature_code=feature_code,
                charge_tx_id=int(charge_tx_id),
            )
            if charge_tx_id is not None
            else confirmed_before + total_refunded
        )
        _reconcile_exact_charge_refund_cursor(
            cursor,
            context=refund_debt_context,
            confirmed_refund_points=confirmed_points,
        )
        _enqueue_confirmed_refund_notification(
            cursor,
            user_id=user_id,
            points=confirmed_points,
            notification=notification,
        )

    logger.info(f"[Billing] 用户{user_id} 退费 {feature_code}: "
                f"共{total_refunded}积分 {len(refund_details)}笔 ({reason})"
                + (f" [累计退款目标={amount}]" if amount is not None else "")
                + (f" [按charge={charge_tx_id}]" if charge_tx_id is not None else ""))

    return {
        "success": True,
        "refunded": total_refunded,
        "confirmed_refunded": confirmed_points,
        "refund_target": target,
        "remaining_refund": max(0, target - confirmed_points),
        "already_refunded": remaining_target == 0,
        "details": refund_details,
    }


from contextlib import asynccontextmanager


# ==========================================================================
# 2026-04-18 后扣费 + 冻结机制（严格"完成才扣"心智）
#
# 三种扣费形态：
#   A 类 · 同步短任务（autofill/article_gen/topic_gen/...）
#     → 用 charge_on_success(user_id, feature_code)
#     → 预检余额 + 跑业务 + 只有 yield 不抛异常才扣
#
#   B 类 · 异步长任务（geo_diagnosis/monitor_*/meeting_*/...）
#     → 启动时 freeze_points → 返回 freeze_id
#     → 任务完成 callback → commit_freeze(freeze_id)
#     → 任务失败 → release_freeze(freeze_id, reason)
#     → 用户钱包看到 frozen_points 栏（"使用中"）
#
#   C 类 · 外部下单（media_publish/media_proxy_publish）
#     → 走订单状态机（api/meijiehezi_api.py），不用本模块
# ==========================================================================


# ══════════════════════════════════════════════════════════════════════════
# 🔴 [#79 · 2026-09-05 · 用户点名批准动本文件] 对客的「钱动没动」信封
#
# 本节**只做派生**:纯函数、零 I/O、零状态改动,不碰冻结/扣费/退费任何语义。
# 之所以放在 billing.py 而不是各路由各写一份:「这次到底扣没扣」这件事的**真相**
# 只有这里知道;七个会扣费的路由各判一次,迟早有几处判错,而判错不会报错。
#
# 实测背景:全仓 99 处 freeze/charge 调用,其中**只有 7 处在路由处理器里**
# (另 81 处在 worker/调度,根本没有 HTTP 响应可带信封);而返回 `charged` 的 24 处
# 与那 7 个路由**零重叠** —— 「我们有 charged 信封」这句话成立,却对真正扣钱的
# 七条路**一次都没生效**。
# ══════════════════════════════════════════════════════════════════════════

#: 信封的三态。**纯字符串枚举,不用 true/false/"unknown" 混类型** ——
#: 混类型会掉进真值陷阱:前端 `if (charged.happened)` 会把 `"unknown"` 当成「扣了」,
#: 而 unknown 的语义恰恰是「我们也不知道」。三态压回布尔是本仓记过账的病。
CHARGED_YES = "yes"
CHARGED_NO = "no"
CHARGED_UNKNOWN = "unknown"


def charged_envelope(result=None, *, unknown: bool = False, reason: str = "") -> dict:
    """把一次扣费/冻结的结果翻译成对客信封。**纯函数,不读库不写库。**

    - `unknown=True` ⇒ 我们**不知道**钱动没动(例如扣费调用抛在半路)。
      这一态必须有:把它压成 `no` 是在承诺一件我们没验证的事。
    - `result` 为 None / 空 ⇒ 没扣(`no`)。
    - `result` 里 `free`/`admin_exempt` 为真,或金额为 0 ⇒ 没扣(`no`)。
    - 否则 ⇒ 扣了(`yes`),`points` 取真实金额。
    """
    if unknown:
        return {"state": CHARGED_UNKNOWN, "points": 0,
                "reason": reason or "系统出错,若已扣会自动退回"}
    if not result:
        return {"state": CHARGED_NO, "points": 0, "reason": reason}
    if not isinstance(result, dict):
        # 不认识的形状**不猜**:说不知道,比说「没扣」诚实。
        return {"state": CHARGED_UNKNOWN, "points": 0,
                "reason": reason or "扣费结果形状未知"}
    if result.get("free") or result.get("admin_exempt"):
        return {"state": CHARGED_NO, "points": 0, "reason": reason or "本次免费"}
    points = int(result.get("amount") or result.get("points") or 0)
    if points <= 0:
        return {"state": CHARGED_NO, "points": 0, "reason": reason}
    return {"state": CHARGED_YES, "points": points, "reason": reason}


@asynccontextmanager
async def charge_on_success(user_id: int, feature_code: str,
                             brand_id: int = None, extra_cost: int = 0):
    """"完成才扣"上下文管理器（A 类 · 同步短任务）

    用法:
        async with charge_on_success(user_id, "article_gen") as charge:
            result = await generate_article(...)
            # 如果想主动判定失败 → 抛异常，yield 里没扣过，直接退出不扣
            if not result.get("ok"):
                raise HTTPException(500, "生成失败")
            # 正常走完 with 块 → 离开时自动扣费（charge 会塞进扣费结果）

    语义：
      1. 进入 with 时仅余额预检（不足直接 402 抛）
      2. with 体抛异常 → 根本没扣过，不需要退费
      3. with 体正常返回 → 最后一刻才 deduct_points
      4. admin / user_id=0 / cost=0 → 直接 yield 不扣
    """
    charge_holder = {"committed": False, "result": None}
    if not user_id:
        yield charge_holder
        return
    # 预检余额（不足 → 402 抛出，不会进入 with 体）
    await check_balance_only(user_id, feature_code, extra_cost=extra_cost)
    try:
        yield charge_holder
        # with 体正常结束才到这里 → 真扣费
        charge_holder["result"] = await deduct_points(
            user_id, feature_code, extra_cost=extra_cost, brand_id=brand_id,
        )
        charge_holder["committed"] = True
    except Exception:
        # with 体抛异常 → 不扣费（根本没扣过，也不需要退）
        logger.info(f"[charge_on_success] {feature_code} 业务失败，未扣费")
        raise


async def freeze_points(user_id: int, feature_code: str,
                         task_ref: str = None, brand_id: int = None,
                         extra_cost: int = 0, reason: str = None,
                         _cursor=None,
                         _organization_context: Optional["BillingActorContext"] = None) -> dict:
    """冻结积分（B 类异步长任务启动时用）

    把 bonus_points/paid_points 转入 frozen_points 池，记录到 point_freezes。
    任务完成 → commit_freeze(freeze_id) / 失败 → release_freeze(freeze_id)。

    返回:
        {"freeze_id": 123, "amount": 1040, "amount_bonus": 1040, "amount_paid": 0,
         "balance": {"paid": ..., "bonus": ..., "frozen": ...}}
    """
    # admin 免冻结（组织上下文必须形成真实、可恢复的物理腿，不能走旧免单旁路）
    if _organization_context is None:
        from db.auth_db import get_user
        user_info = get_user(user_id)
        if user_info and user_info.get("is_admin"):
            return {"freeze_id": None, "amount": 0, "free": True, "admin_exempt": True}
    elif int(user_id) != int(_organization_context.identity.payer_user_id):
        raise RuntimeError("organization payer must be server-resolved from membership")

    pricing = get_feature_pricing(feature_code, cursor=_cursor)
    total_cost = pricing["cost_points"] + extra_cost
    if total_cost == 0:
        return {"freeze_id": None, "amount": 0, "free": True}

    requires_paid = pricing.get("requires_paid_points", False)

    with _billing_transaction(_cursor) as conn:
        cursor = conn.cursor()

        if _organization_context is not None:
            cursor.execute(
                """
                SELECT u.id,EXISTS(
                  SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                  WHERE ur.user_id=u.id AND r.name='admin'
                ) AS is_admin
                FROM users u WHERE u.id=%s
                """,
                (user_id,),
            )
            _payer = cursor.fetchone()
            if not _payer or bool(_payer.get("is_admin")):
                raise HTTPException(status_code=409, detail={
                    "code": "ORG_PAYER_NOT_BILLABLE",
                    "message": "组织付款账号不能使用管理员免单路径",
                })

        # [单账本收敛 2026-07-27] 原来这里有一段 V3.5 冻结分流:命中信用钱包的客户走
        # freeze_customer_credit(真扣三池 + 落 customer_credit_freezes),返回 freeze_table='v35'。
        # 现已删除 —— 单账本后【不再新建】v35 冻结记录。
        #
        # 🔴 但 commit_freeze / release_freeze 的 v35 路由【刻意保留】(见那两个函数):
        #    _route_freeze_table 按冻结记录【实际所在表】判定,不看客户当前身份。
        #    切换瞬间若还有在途的 v35 冻结,必须仍能正确结算 —— 这正是工单 §4.2 点名的坑,
        #    也是 billing.py:93 那个 [BUG-P2] 机制存在的理由,沿用它、不另造。
        #    (生产实证 2026-07-27:customer_credit_freezes 历史仅 1 行且早已 committed,
        #     在途 0;但这是时点数据,切换当日仍须复查,见 switchday_check 脚本第 ① 项。)
        # legacy 路径(原 user_wallets 冻结链保留)
        # 行锁拿钱包（v3.4 加 commission_points + deduction_preference）
        cursor.execute(
            "SELECT paid_points, bonus_points, commission_points, frozen_points, "
            "deduction_preference FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        w = cursor.fetchone()
        if not w:
            raise HTTPException(status_code=402, detail={"code": "NO_WALLET", "message": "钱包不存在"})

        preference = _get_deduction_preference(w)
        split = _compute_deduction_split(w, total_cost,
                                          requires_paid=requires_paid,
                                          preference=preference)

        if split["remaining"] > 0:
            if requires_paid:
                raise HTTPException(status_code=402, detail={
                    "code": "INSUFFICIENT_PAID_POINTS",
                    "message": "充值积分 + 佣金积分不足（媒体发布不可用赠送积分）",
                    "required": total_cost,
                    "available_pay": (w.get("commission_points") or 0) + (w.get("paid_points") or 0),
                })
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_POINTS",
                "message": f"积分不足，需要 {total_cost}",
                "required": total_cost,
                "available": (w.get("bonus_points") or 0) + (w.get("commission_points") or 0) + (w.get("paid_points") or 0),
                "available_paid": w.get("paid_points") or 0,
                "available_commission": w.get("commission_points") or 0,
                "available_bonus": w.get("bonus_points") or 0,
            })

        take_bonus = split["bonus"]
        take_commission = split["commission"]
        take_paid = split["paid"]

        # 三池 → 冻结池
        cursor.execute(
            """
            UPDATE user_wallets
               SET bonus_points = bonus_points - %s,
                   commission_points = commission_points - %s,
                   paid_points = paid_points - %s,
                   frozen_points = frozen_points + %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE user_id = %s
            RETURNING paid_points, bonus_points, commission_points, frozen_points
            """,
            (take_bonus, take_commission, take_paid, total_cost, user_id),
        )
        bal = cursor.fetchone()

        # 落 point_freezes（含 amount_commission）
        cursor.execute(
            """
            INSERT INTO point_freezes
                (user_id, feature_code, amount_total, amount_bonus, amount_commission, amount_paid,
                 status, task_ref, brand_id, reason)
            VALUES (%s, %s, %s, %s, %s, %s, 'frozen', %s, %s, %s)
            RETURNING id
            """,
            (user_id, feature_code, total_cost, take_bonus, take_commission, take_paid,
             task_ref, brand_id, reason),
        )
        freeze_id = cursor.fetchone()["id"]

        # 账单流水(status=frozen 冻结中)
        # V3.3.1(Codex 二审 P1-9):freeze 流水 source='balance_deduction'
        order_id = _generate_order_id()
        if take_bonus > 0:
            insert_transaction(cursor, user_id, "freeze", "bonus", -take_bonus,
                               bal["bonus_points"], feature_code,
                               description=f"{pricing['feature_name']}(冻结)",
                               order_id=f"FRZ{freeze_id}-{order_id}", brand_id=brand_id,
                               source="balance_deduction")
        if take_commission > 0:
            insert_transaction(cursor, user_id, "freeze", "commission", -take_commission,
                               bal["commission_points"], feature_code,
                               description=f"{pricing['feature_name']}(冻结)",
                               order_id=f"FRZ{freeze_id}-{order_id}", brand_id=brand_id,
                               source="balance_deduction")
        if take_paid > 0:
            insert_transaction(cursor, user_id, "freeze", "paid", -take_paid,
                               bal["paid_points"], feature_code,
                               description=f"{pricing['feature_name']}(冻结)",
                               order_id=f"FRZ{freeze_id}-{order_id}", brand_id=brand_id,
                               source="balance_deduction")

    logger.info(
        f"[Billing] freeze user={user_id} feature={feature_code} "
        f"freeze_id={freeze_id} amount={total_cost} "
        f"(bonus={take_bonus} commission={take_commission} paid={take_paid}) task_ref={task_ref}"
    )
    return {
        "freeze_id": freeze_id,
        "amount": total_cost,
        "amount_bonus": take_bonus,
        "amount_commission": take_commission,
        "amount_paid": take_paid,
        "physical_split_snapshot": {
            "bonus": take_bonus,
            "commission": take_commission,
            "paid": take_paid,
            "order": [name for name, _ in _deduction_order(
                w, requires_paid=requires_paid, preference=preference
            )],
            "requires_paid_points": bool(requires_paid),
            "deduction_preference": preference,
        },
        # [A0] 句柄带冻结实际所在表标记 · 调用方回传 commit/release 显式指定表免猜(根除撞号歧义)
        "freeze_table": "legacy",
        "balance": {
            "paid_points": bal["paid_points"],
            "commission_points": bal["commission_points"],
            "bonus_points": bal["bonus_points"],
            "frozen_points": bal["frozen_points"],
            "total": bal["paid_points"] + bal["commission_points"] + bal["bonus_points"],
        },
    }


async def commit_freeze(freeze_id: int = None, task_ref: str = None, reason: str = "任务完成",
                        user_id: int = None, freeze_table: str = None,
                        _cursor=None, _actual_points: int = None,
                        _reserved_split: Optional[dict] = None) -> dict:
    """结算冻结（任务成功完成时调用）—— frozen_points 出池 → 正式消费

    可用 freeze_id 或 task_ref 任一定位。幂等：已 committed/released 的 freeze 再调返回 no-op。

    [批2B] user_id 传且命中 V3.5 客户 → 路由到 customer_credit_freezes(commit_customer_freeze)·
    否则走 legacy point_freezes。⚠️ 必须与对应 freeze_points 用【同一 user_id】(否则跨表路由不一致)。
    """
    if not freeze_id and not task_ref:
        return {"success": False, "reason": "需提供 freeze_id 或 task_ref"}

    with _billing_transaction(_cursor) as conn:
        cursor = conn.cursor()
        # ★ [BUG-P2] 按冻结【实际所在表】路由(防客户运行期 legacy→V3.5 漂移致跨表错路由 → sweeper 白退)·
        # 原靠客户【当前】是否 V3.5 客户判表,legacy 冻结遇客户后开通 V3.5 会被错路由到新表找不到。
        _route = _route_freeze_table(cursor, freeze_id=freeze_id, task_ref=task_ref,
                                     user_id=user_id, freeze_table=freeze_table)
        if _route == "ambiguous":
            # [A2] 跨表双 frozen 撞号 → 拒绝自动结算(不静默坐实错的那笔)· 转人工
            # [复核加固] 立即 rollback 释放上面 frozen 探测的 FOR UPDATE 锁(否则持锁到 with 退出)
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[Billing] commit_freeze 跨表撞号歧义 freeze_id={freeze_id} task_ref={task_ref} "
                         f"user={user_id} · 拒绝动钱 · 转人工核")
            return {"success": False, "reason": "freeze 跨表撞号歧义,需人工核", "ambiguous": True}
        if _route == "v35":
            from services.customer_credit import commit_customer_freeze
            r = commit_customer_freeze(cursor, freeze_id=freeze_id, task_ref=task_ref,
                                       reason=reason, customer_user_id=user_id)
            logger.info(
                f"[Billing/V3.5] commit_freeze customer={user_id} freeze_id={freeze_id} "
                f"task_ref={task_ref} -> {r}"
            )
            return r
        # 行锁找 freeze（legacy point_freezes）· [A1] 有 user_id 时带 user 约束防跨表/跨客户撞号错锁
        if freeze_id:
            if user_id is not None:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE id = %s AND user_id = %s FOR UPDATE",
                    (freeze_id, user_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE id = %s FOR UPDATE",
                    (freeze_id,),
                )
        else:
            if user_id is not None:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE task_ref = %s AND user_id = %s AND status = 'frozen' "
                    "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                    (task_ref, user_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE task_ref = %s AND status = 'frozen' "
                    "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                    (task_ref,),
                )
        fz = cursor.fetchone()
        if not fz:
            return {"success": False, "reason": "未找到冻结记录"}
        if fz["status"] != "frozen":
            # 幂等：已经 committed/released 过了
            return {"success": True, "idempotent": True, "status": fz["status"]}

        user_id = fz["user_id"]
        amount = fz["amount_total"]

        reserved_amount_bonus = fz.get("amount_bonus", 0) or 0
        reserved_amount_commission = fz.get("amount_commission", 0) or 0
        reserved_amount_paid = fz.get("amount_paid", 0) or 0
        if _actual_points is None:
            actual_amount = int(amount)
            actual_split = {
                "bonus": int(reserved_amount_bonus),
                "commission": int(reserved_amount_commission),
                "paid": int(reserved_amount_paid),
            }
            released_split = {"bonus": 0, "commission": 0, "paid": 0}
        else:
            if not _reserved_split:
                raise RuntimeError("organization settle requires immutable reserved split")
            if sum(int(_reserved_split.get(name, 0) or 0) for name in ("bonus", "commission", "paid")) != int(amount):
                raise RuntimeError("organization reserved split does not match physical freeze")
            actual_amount = int(_actual_points)
            actual_split, released_split = _actual_and_release_split(_reserved_split, actual_amount)

        # ⚠️ 关键：commit 只动 frozen_points，三池（bonus/commission/paid）不动！
        # 那三池的钱已经在 freeze 时搬走进了 frozen，再 UPDATE 会让用户被扣两次。
        if _actual_points is None:
            cursor.execute(
                """
                UPDATE user_wallets
                   SET frozen_points = frozen_points - %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE user_id = %s
                RETURNING paid_points, bonus_points, commission_points, frozen_points
                """,
                (amount, user_id),
            )
        else:
            # Dynamic organization costs commit only actual; unused ceiling is
            # restored to the exact physical pools captured at reservation.
            cursor.execute(
                """
                UPDATE user_wallets
                   SET bonus_points = bonus_points + %s,
                       commission_points = commission_points + %s,
                       paid_points = paid_points + %s,
                       frozen_points = frozen_points - %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE user_id = %s
                RETURNING paid_points, bonus_points, commission_points, frozen_points
                """,
                (
                    released_split["bonus"], released_split["commission"],
                    released_split["paid"], amount, user_id,
                ),
            )
        bal = cursor.fetchone()

        cursor.execute(
            """
            UPDATE point_freezes
               SET status = 'committed',
                   committed_at = CURRENT_TIMESTAMP,
                   reason = COALESCE(%s, reason)
             WHERE id = %s
            """,
            (reason, fz["id"]),
        )

        # 账单流水：3 笔 consume 记录（bonus/commission/paid 各自被用了多少）
        pricing = get_feature_pricing(fz["feature_code"], cursor=cursor)
        feature_name = pricing.get("feature_name", fz["feature_code"]) if pricing else fz["feature_code"]
        order_id = _generate_order_id()
        amount_bonus = actual_split["bonus"]
        amount_commission = actual_split["commission"]
        amount_paid = actual_split["paid"]
        charge_tx_ids = []

        # V3.3.1(Codex 二审 P1-9):commit_freeze 流水 source='balance_deduction'
        if amount_bonus > 0:
            _tx_id = insert_transaction(cursor, user_id, "consume", "bonus", -amount_bonus,
                               bal["bonus_points"], fz["feature_code"],
                               description=feature_name,
                               order_id=f"CMT{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")
            charge_tx_ids.append(_tx_id)
        if amount_commission > 0:
            _tx_id = insert_transaction(cursor, user_id, "consume", "commission", -amount_commission,
                               bal["commission_points"], fz["feature_code"],
                               description=feature_name,
                               order_id=f"CMT{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")
            charge_tx_ids.append(_tx_id)
        if amount_paid > 0:
            _tx_id = insert_transaction(cursor, user_id, "consume", "paid", -amount_paid,
                               bal["paid_points"], fz["feature_code"],
                               description=feature_name,
                               order_id=f"CMT{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")
            charge_tx_ids.append(_tx_id)

        # Keep a separate release trail for the unused reservation ceiling.
        if _actual_points is not None and sum(released_split.values()) > 0:
            release_order_id = _generate_order_id()
            for point_type in ("bonus", "commission", "paid"):
                released = released_split[point_type]
                if released <= 0:
                    continue
                insert_transaction(
                    cursor, user_id, "release", point_type, released,
                    bal[f"{point_type}_points"], fz["feature_code"],
                    description=f"{feature_name}(释放预留差额)",
                    order_id=f"DIF{fz['id']}-{release_order_id}", brand_id=fz["brand_id"],
                    source="balance_deduction",
                )

    logger.info(f"[Billing] commit_freeze id={fz['id']} user={user_id} amount={actual_amount} "
                f"(bonus={amount_bonus} commission={amount_commission} paid={amount_paid}) {reason}")
    return {
        "success": True,
        "freeze_id": fz["id"],
        "amount": actual_amount,
        "reserved_amount": int(amount),
        "released_difference": int(amount) - actual_amount,
        "charge_tx_id": charge_tx_ids[0] if charge_tx_ids else None,
        "charge_tx_ids": charge_tx_ids,
        "balance": {
            "paid_points": bal["paid_points"],
            "commission_points": bal["commission_points"],
            "bonus_points": bal["bonus_points"],
            "frozen_points": bal["frozen_points"],
        },
    }


async def release_freeze(freeze_id: int = None, task_ref: str = None, reason: str = "任务失败",
                         user_id: int = None, freeze_table: str = None,
                         _cursor=None) -> dict:
    """释放冻结（任务失败时调用）—— frozen_points 退回原 bonus/paid 池

    幂等：已 committed/released 的 freeze 再调返回 no-op。

    [批2B] user_id 传且命中 V3.5 客户 → 路由到 customer_credit_freezes(release_customer_freeze ·
    refund_credit 退回三池)· 否则走 legacy point_freezes。⚠️ 必须与对应 freeze_points 用【同一 user_id】。
    freeze_sweeper 兜底新表 zombie 时也按表行 customer_user_id 回传(防漏传致钱卡冻结)。
    """
    if not freeze_id and not task_ref:
        return {"success": False, "reason": "需提供 freeze_id 或 task_ref"}

    with _billing_transaction(_cursor) as conn:
        cursor = conn.cursor()
        # ★ [BUG-P2] 按冻结【实际所在表】路由(防客户运行期 legacy→V3.5 漂移致跨表错路由 → 白退/漏退)·
        # 原靠客户【当前】是否 V3.5 客户判表,legacy 冻结遇客户后开通 V3.5 会被错路由到新表找不到。
        _route = _route_freeze_table(cursor, freeze_id=freeze_id, task_ref=task_ref,
                                     user_id=user_id, freeze_table=freeze_table)
        if _route == "ambiguous":
            # [A2] 跨表双 frozen 撞号 → 拒绝自动退(不静默退错那笔)· 转人工(sweeper 收到 success=False 记 failed)
            # [复核加固] 立即 rollback 释放上面 frozen 探测的 FOR UPDATE 锁
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[Billing] release_freeze 跨表撞号歧义 freeze_id={freeze_id} task_ref={task_ref} "
                         f"user={user_id} · 拒绝动钱 · 转人工核")
            return {"success": False, "reason": "freeze 跨表撞号歧义,需人工核", "ambiguous": True}
        if _route == "v35":
            from services.customer_credit import release_customer_freeze
            r = release_customer_freeze(cursor, freeze_id=freeze_id, task_ref=task_ref,
                                        reason=reason, customer_user_id=user_id)
            logger.info(
                f"[Billing/V3.5] release_freeze customer={user_id} freeze_id={freeze_id} "
                f"task_ref={task_ref} -> {r}"
            )
            return r
        # legacy point_freezes · [A1] 有 user_id 时带 user 约束防跨表/跨客户撞号错锁
        if freeze_id:
            if user_id is not None:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE id = %s AND user_id = %s FOR UPDATE",
                    (freeze_id, user_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE id = %s FOR UPDATE",
                    (freeze_id,),
                )
        else:
            if user_id is not None:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE task_ref = %s AND user_id = %s AND status = 'frozen' "
                    "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                    (task_ref, user_id),
                )
            else:
                cursor.execute(
                    "SELECT * FROM point_freezes WHERE task_ref = %s AND status = 'frozen' "
                    "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                    (task_ref,),
                )
        fz = cursor.fetchone()
        if not fz:
            return {"success": False, "reason": "未找到冻结记录"}
        if fz["status"] != "frozen":
            return {"success": True, "idempotent": True, "status": fz["status"]}

        user_id = fz["user_id"]
        amount_bonus = fz.get("amount_bonus", 0) or 0
        amount_commission = fz.get("amount_commission", 0) or 0
        amount_paid = fz.get("amount_paid", 0) or 0

        # frozen_points 减 total，bonus/commission/paid 按 freeze 时的拆分比例归位
        cursor.execute(
            """
            UPDATE user_wallets
               SET bonus_points = bonus_points + %s,
                   commission_points = commission_points + %s,
                   paid_points = paid_points + %s,
                   frozen_points = frozen_points - %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE user_id = %s
            RETURNING paid_points, bonus_points, commission_points, frozen_points
            """,
            (amount_bonus, amount_commission, amount_paid, fz["amount_total"], user_id),
        )
        bal = cursor.fetchone()

        cursor.execute(
            """
            UPDATE point_freezes
               SET status = 'released',
                   released_at = CURRENT_TIMESTAMP,
                   reason = COALESCE(%s, reason)
             WHERE id = %s
            """,
            (reason, fz["id"]),
        )

        # 账单流水：3 笔 release 记录
        pricing = get_feature_pricing(fz["feature_code"], cursor=cursor)
        feature_name = pricing.get("feature_name", fz["feature_code"]) if pricing else fz["feature_code"]
        order_id = _generate_order_id()
        # V3.3.1(Codex 二审 P1-9):release_freeze 流水 source='balance_deduction'
        if amount_bonus > 0:
            insert_transaction(cursor, user_id, "release", "bonus", amount_bonus,
                               bal["bonus_points"], fz["feature_code"],
                               description=f"{feature_name}(释放)",
                               order_id=f"RLS{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")
        if amount_commission > 0:
            insert_transaction(cursor, user_id, "release", "commission", amount_commission,
                               bal["commission_points"], fz["feature_code"],
                               description=f"{feature_name}(释放)",
                               order_id=f"RLS{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")
        if amount_paid > 0:
            insert_transaction(cursor, user_id, "release", "paid", amount_paid,
                               bal["paid_points"], fz["feature_code"],
                               description=f"{feature_name}(释放)",
                               order_id=f"RLS{fz['id']}-{order_id}", brand_id=fz["brand_id"],
                               source="balance_deduction")

    logger.info(f"[Billing] release_freeze id={fz['id']} user={user_id} amount={fz['amount_total']} "
                f"(bonus={amount_bonus} commission={amount_commission} paid={amount_paid}) {reason}")
    return {
        "success": True,
        "freeze_id": fz["id"],
        "amount": fz["amount_total"],
        "balance": {
            "paid_points": bal["paid_points"],
            "commission_points": bal["commission_points"],
            "bonus_points": bal["bonus_points"],
            "frozen_points": bal["frozen_points"],
        },
    }


# ===========================================================================
# Organization caller-owned transaction primitives (2026-07-20 contract)
#
# These are additive. Legacy callers continue through the wrappers above with
# their original signatures and transaction ownership. Organization callers
# must pass a server-resolved BillingActorContext and an existing cursor.
# ===========================================================================

def lock_legacy_wallet_with_cursor(cursor, payer_user_id: int) -> dict:
    """Lock the only permitted organization physical wallet in fixed order."""
    cursor.execute(
        """
        SELECT u.id,EXISTS(
          SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
          WHERE ur.user_id=u.id AND r.name='admin'
        ) AS is_admin
        FROM users u WHERE u.id=%s
        """,
        (payer_user_id,),
    )
    user = cursor.fetchone()
    if not user or bool(user.get("is_admin")):
        raise HTTPException(status_code=409, detail={
            "code": "ORG_PAYER_NOT_BILLABLE",
            "message": "组织付款账号不能使用管理员免单路径",
        })
    # [单账本收敛 2026-07-27] 原来这里禁止"有信用钱包的客户"作为组织共享付款账号。
    # 单账本后该限制不再成立:那 5 个客户的额度已并入自己的 user_wallets,
    # 与其他用户没有任何区别,继续按 wallet row 是否存在把他们挡在外面反而是错的
    # (表停写只读、历史 row 永远都在 → 这几个人会被永久误禁)。
    cursor.execute(
        """
        SELECT paid_points,bonus_points,commission_points,frozen_points,deduction_preference
        FROM user_wallets WHERE user_id=%s FOR UPDATE
        """,
        (payer_user_id,),
    )
    wallet = cursor.fetchone()
    if not wallet:
        raise HTTPException(status_code=402, detail={"code": "NO_WALLET", "message": "钱包不存在"})
    return dict(wallet)

async def reserve_points_with_cursor(
    cursor,
    actor_context: "BillingActorContext",
    *,
    reason: str,
) -> dict:
    """Reserve the finite ceiling in the owner's legacy physical wallet."""
    if actor_context.identity.actor_kind not in {"owner", "member", "system"}:
        raise ValueError("invalid organization billing actor kind")
    pricing = get_feature_pricing(actor_context.feature_code, cursor=cursor)
    base_cost = int(pricing["cost_points"])
    ceiling = int(actor_context.reserved_ceiling_points)
    if ceiling < base_cost:
        raise ValueError("reserved ceiling is below feature price")
    result = await freeze_points(
        actor_context.identity.payer_user_id,
        actor_context.feature_code,
        task_ref=actor_context.task_ref or actor_context.execution_id,
        brand_id=actor_context.brand_id,
        extra_cost=ceiling - base_cost,
        reason=reason,
        _cursor=cursor,
        _organization_context=actor_context,
    )
    if result.get("freeze_table") != "legacy" or not result.get("freeze_id"):
        raise RuntimeError("organization reservation did not create a legacy physical freeze")
    if int(result.get("amount") or -1) != ceiling:
        raise RuntimeError("organization physical reservation ceiling mismatch")
    return result


async def commit_reserved_points_with_cursor(
    cursor,
    actor_context: "BillingActorContext",
    *,
    freeze_id: int,
    actual_points: int,
    reserved_split: dict,
    reason: str,
) -> dict:
    """Commit actual cost and return the unused ceiling in the same transaction."""
    actual_points = int(actual_points)
    if actual_points < 0 or actual_points > int(actor_context.reserved_ceiling_points):
        raise ValueError("actual points exceed reserved ceiling")
    return await commit_freeze(
        freeze_id=freeze_id,
        reason=reason,
        user_id=actor_context.identity.payer_user_id,
        freeze_table="legacy",
        _cursor=cursor,
        _actual_points=actual_points,
        _reserved_split=reserved_split,
    )


async def release_reserved_points_with_cursor(
    cursor,
    actor_context: "BillingActorContext",
    *,
    freeze_id: int,
    reason: str,
) -> dict:
    """Release an uncommitted organization reservation without owning TX."""
    return await release_freeze(
        freeze_id=freeze_id,
        reason=reason,
        user_id=actor_context.identity.payer_user_id,
        freeze_table="legacy",
        _cursor=cursor,
    )


async def refund_committed_points_with_cursor(
    cursor,
    actor_context: "BillingActorContext",
    *,
    charge_tx_id: int,
    cumulative_refund_target: int,
    reason: str,
) -> dict:
    """Refund the exact immutable legacy charge group to its original pools."""
    return await refund_points(
        actor_context.identity.payer_user_id,
        actor_context.feature_code,
        reason,
        amount=int(cumulative_refund_target),
        charge_tx_id=int(charge_tx_id),
        ledger_type="legacy",
        notification=None,
        _cursor=cursor,
    )


@asynccontextmanager
async def charge_with_refund(user_id: int, feature_code: str,
                              brand_id: int = None, extra_cost: int = 0,
                              reason_on_fail: str = None):
    """
    扣费上下文管理器 —— 2026-04-18 升级为"后扣费"语义（兼容保留旧名）。

    新语义（与 charge_on_success 一致）：
      1. 进入 with 时余额预检（不足直接 402 抛，不扣）
      2. with 体抛异常 → 根本没扣过，不退费（业务失败不扣）
      3. with 体正常返回 → 最后才 deduct_points

    老语义（扣 → 失败退）已废弃，因为"白扣再退"有数据库崩溃时漏退的风险。
    新语义"根本不扣就不需要退"更鲁棒。

    ⚠️ 长任务（诊断/监测/会议）请用 freeze_points + commit_freeze/release_freeze，
    这个上下文只适合"一次 API 调用跑完就出结果"的同步短任务。

    reason_on_fail 参数保留但不再使用（向后兼容签名）。
    """
    _ = reason_on_fail  # 兼容老签名，实际已无用
    if not user_id:
        yield {}
        return

    # 预检：余额不足 → 402 抛，不进 with 体
    await check_balance_only(user_id, feature_code, extra_cost=extra_cost)

    holder = {"committed": False}
    try:
        yield holder
    except Exception:
        # 业务失败：没扣过 → 什么都不做
        logger.info(f"[charge_with_refund] {feature_code} 业务失败，未扣费（后扣费模式）")
        raise

    # with 体正常结束 → 真扣
    try:
        charge_result = await deduct_points(
            user_id, feature_code, extra_cost=extra_cost, brand_id=brand_id,
        )
        holder["committed"] = True
        holder["result"] = charge_result
        logger.info(f"[charge_with_refund] {feature_code} 业务成功扣费 {charge_result.get('deducted')} 积分")
    except Exception as e:
        # [BUG-P3] 扣费失败(预检通过后的并发竞态/余额被抽干)· 数据已给用户 · 不挂用户。
        # 原仅 logger.error 静默 → 漏扣不可见、无法对账追回(并发抽干余额时业务白送)。
        # 升级 critical + 结构化 [UNCHARGED] 告警(监控可抓)+ 标记 holder["uncharged"](调用方可感知),
        # 让运营可发现并人工追扣。
        holder["uncharged"] = True
        logger.critical(
            "[charge_with_refund][UNCHARGED] 后扣费失败(业务已完成·用户白用) "
            "user_id=%s feature_code=%s extra_cost=%s err=%s",
            user_id, feature_code, extra_cost, e,
        )
