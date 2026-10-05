"""小榜 ``publish_image_note`` 的 **execute adapter**(WO-B ② · 规格 §10 / §16.1)。

## 在这之前 execute 停在哪

``api/xiaobang_operations_api.operation_execute`` 一路跑完身份 → 归属 → revision →
状态 → 漂移 → 确认档,然后因为 ``contract.executable=False`` 返 409
``DOMAIN_ADAPTER_NOT_AVAILABLE``。合同 ``notes`` 里写着解锁前置:
图文包的原子 claim / durable outbox 上线。那批已经在 34 班随并车上线,所以本模块把
最后一跳接上。

## 🔴 资金:不新开路径,复用**同一条**产线

执行段调的是 ``services.geo_douyin.publish_batch_core.materialize_publish_batch`` ——
就是 ``POST /api/meijiehezi/image-notes/publish-batch`` 用的那一个函数
(本包把它从 handler 里搬了出来,搬家是纯移动)。于是:

* ``claim_request`` 的原子 ``_CLAIM`` —— 同一份;
* ``resolve_settlement_authority`` 的结算权威 / payer 解析 —— 同一份;
* ``freeze_one_item`` 的 ``freeze_per_item`` 逐项冻结 + 完整句柄 —— 同一份;
* 组织审批门、资格、频控预占、广告法闸、锁定 revision、总价确认锁 —— 同一份。

**本模块不 import ``middleware.billing``,不写任何 wallet 表,不自己拼价公式。**

## 🔴 幂等键 = intent 回执

工单原话「幂等键沿用 intent 回执」。落地形态:

    publish_idempotency_keys.request_id = <receipt_id>          (``xcr_…``)
    command_id                          = "pubcmd_" + receipt_id

回执是**单次消费品**且与 ``(intent_id, intent_revision)`` 一一对应(唯一索引),
所以它天然就是这次执行的幂等身份 —— 不需要再造一个 id,也不能用
``execution_request_id``(那是客户端给的,客户端换一个就绕过了幂等)。

崩溃重放 / 用户双击的结果因此是:第二次 ``claim_request`` 拿到零行 →
``{"replayed": True}`` → 返回**同一个** command_id。判据断言的就是这一位。

## 🔴 冻结额 == 意图,不是「返回 success」

`expected_total_price_points` 传的是 **intent 冻结时那个报价**
(``xiaobang_operation_intents.compute_quote_amount``),不是执行时现算的数。
于是价目在 prepare 与 execute 之间变了 ⇒ 产线的总价确认锁抛 ``PriceChanged``
⇒ 零冻结零建单,让用户重新核对。

用户按下确认时看到的那个数字,和真正被冻住的那笔钱,必须是同一个。
判据打的是**冻结终态**(``mhz_publish_order_items.reserved_amount`` /
``freeze_id`` / ``billing_mode``)== 意图,不是打这个函数的返回值。
"""
from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from services.xiaobang_intent import IntentError

#: 派生 ``item_request_id`` 的命名空间。``mhz_publish_order_items.item_request_id``
#: 是 **uuid** 列(chain3 判据实测),所以这里必须给一个真 uuid,不能给 ``xcr_…``。
#: 用 uuid5 而不是 uuid4:同一个回执重放两次必须派生出**同一个** item id,
#: 否则「一篇一账号」的一对一断言会在重放时看到两个 item。
_ITEM_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def _refuse(code: str, message: str, *, next_action: str,
            detail: Optional[dict] = None) -> IntentError:
    """执行段拒绝。一律 409 + **人话** —— 不回显枚举、不回显账号名。"""
    return IntentError(code, message, http_status=409,
                       next_action=next_action, detail=detail or {})


def frozen_execution_binding(intent_row: Mapping[str, Any]) -> dict:
    """取 prepare 时冻结的执行绑定。

    🔴 只从**服务端冻结的那一份**取(``preview``),绝不从请求体取:
       从请求取等于让调用方在最后一刻改掉"发哪一篇、发到哪个号、多少钱"。
    """
    preview = dict(intent_row.get("preview") or {})
    return dict(preview.get("execution_binding") or {})


def _resolve_publish_target(cur, *, geo_post_id: int, owner_user_id: int) -> dict:
    """从 geo post 找到**当前生效版本**与它**已准备好的**发布素材。

    🔴 两跳都带租户/归属条件,并且 artifact 必须与 active revision **同一版**:
       素材是按 revision 准备的,拿一份旧素材去发新文案就是「审 A 发 B」。
    """
    cur.execute(
        "SELECT post_revision_id, manifest_hash"
        "  FROM geo_douyin_post_revisions"
        " WHERE geo_post_id = %s AND status = 'active'"
        " ORDER BY revision_no DESC LIMIT 1",
        (int(geo_post_id),))
    revision = cur.fetchone()
    if revision is None:
        raise _refuse(
            "PUBLISH_CONTENT_NOT_READY",
            "这篇图文还没有可以发布的定稿版本,先回制作页确认一版。",
            next_action="打开现役页面继续")
    revision = dict(revision)

    cur.execute(
        "SELECT prepared_artifact_id, manifest_hash, state"
        "  FROM geo_douyin_publish_artifacts"
        " WHERE geo_post_id = %s AND post_revision_id = %s"
        "   AND tenant_owner_user_id = %s AND state = 'ready'"
        " ORDER BY prepared_artifact_id DESC LIMIT 1",
        (int(geo_post_id), int(revision["post_revision_id"]), int(owner_user_id)))
    artifact = cur.fetchone()
    if artifact is None:
        raise _refuse(
            "PUBLISH_ARTIFACT_NOT_READY",
            "这一版的发布素材还没准备好,回制作页点一次「准备发布素材」再来。",
            next_action="打开现役页面继续")
    artifact = dict(artifact)

    return {
        "post_revision_id": int(revision["post_revision_id"]),
        "prepared_artifact_id": int(artifact["prepared_artifact_id"]),
        # manifest 以 **artifact 那一份**为准:`assert_artifact_matches` 比的就是它。
        "manifest_hash": str(artifact.get("manifest_hash") or ""),
    }


def build_execution_items(cur, *, intent_row: Mapping[str, Any],
                          owner_user_id: int, receipt_id: str) -> list[dict]:
    """把一个 intent 摊成产线要的 item 列表(图文规格 §7.3:一篇一账号 ⇒ 恰好 1 项)。"""
    binding = frozen_execution_binding(intent_row)
    preview = dict(intent_row.get("preview") or {})
    form = dict(preview.get("form_prefill") or {})

    geo_post_id = form.get("geo_post_id")
    if not geo_post_id:
        raise _refuse(
            "PUBLISH_TARGET_MISSING",
            "这次安排里没有指定要发哪一篇图文,回小榜重新准备一下。",
            next_action="重新准备")

    media_id = binding.get("media_id")
    if not media_id:
        # 走到这里说明 prepare 那一刻渠道就没核出来。执行段**必须**拦:
        # 猜一个账号的后果是发到别人的号上。
        raise _refuse(
            "PUBLISH_CHANNEL_NOT_SELECTED",
            "还没选定要发到哪个投放账号,回小榜选完再执行。",
            next_action="重新准备")

    target = _resolve_publish_target(cur, geo_post_id=int(geo_post_id),
                                     owner_user_id=int(owner_user_id))
    return [{
        # 同一回执重放派生同一个 id(见 _ITEM_NAMESPACE 注释)
        "item_request_id": str(uuid.uuid5(_ITEM_NAMESPACE, "xiaobang:" + str(receipt_id))),
        "geo_post_id": int(geo_post_id),
        "post_revision_id": target["post_revision_id"],
        "prepared_artifact_id": target["prepared_artifact_id"],
        "manifest_hash": target["manifest_hash"],
        "media_id": int(media_id),
        # 🔴 **冻结时**的价格指纹,不是执行时现算的那个。现算的话这条锁自比自己,
        #    永远不会红 —— 而它要抓的正是「prepare 与 execute 之间价目变了」。
        "expected_price_fingerprint": str(binding.get("price_fingerprint") or ""),
    }]


def execute_publish_image_note(
    cur,
    *,
    intent_row: Mapping[str, Any],
    receipt: Mapping[str, Any],
    identity: Mapping[str, Any],
    settlement: Mapping[str, Any],
    daily_limit: Optional[int] = None,
    endpoint: str = "/api/xiaobang/operations/publish_center/execute",
) -> dict:
    """在**调用方的事务**里把一个已确认的 intent 变成一条真实投放 command。

    返回 ``{"command_id", "replayed", "items", "summary", "media_id"}``。
    不 commit —— 由调用方在同一事务里连同 intent 的状态迁移一起提交。
    """
    from api.geo_image_note_api import PUBLISH_FEATURE_CODE, image_note_daily_limit
    from services.geo_douyin.artifact_prepare import ArtifactRequestConflict
    from services.geo_douyin.contract_approval import ApprovalPending
    from services.geo_douyin.contract_freeze import FreezeProducedNoHandle
    from services.geo_douyin.contract_funding import FundingHandleInvalid
    from services.geo_douyin.contract_idempotency import IdempotencyConflict
    from services.geo_douyin.contract_pricing import (
        PriceChanged, PriceFingerprintMismatch,
    )
    from services.geo_douyin.legal_gate import LegalGateBlocked
    from services.geo_douyin.publish_batch_core import materialize_publish_batch
    from services.geo_douyin.publish_command import CapacityExceeded, PublishAttemptConflict
    from services.xiaobang_channel_eligibility import resolve_channel_option

    receipt_id = str(receipt["receipt_id"])
    owner_user_id = int(identity["tenant_owner_user_id"])
    limit = image_note_daily_limit() if daily_limit is None else int(daily_limit)

    # ── ① 执行前**现场**再核一次渠道资格(prepare 那次是几分钟前的事)──────
    #    资格会在 prepare 与 execute 之间变:号被停用、今天额度被别的单用完。
    #    这一跳是 WO-B ③ 那张翻转矩阵在 execute 侧的落点。
    #
    # 🔴 顺序**刻意排在报价检查之前**(实拆演练订正的):
    #    不可用的账号在 prepare 就不会被报价,于是"先查报价"会让用户看到
    #    「还没算出要用多少算力」——那是**答非所问**。他选的号发不了图文,
    #    就该听见"这个账号发不了图文,换一个能发图文的"。
    #    顺带,这一格也因此成为**承重**的:摘掉它,下游 `CapacityExceeded`
    #    给出的通用措辞会让逐档文案判据当场红。
    binding = frozen_execution_binding(intent_row)
    channel = resolve_channel_option(cur, binding.get("channel_option_id"),
                                     daily_limit=limit)
    if not channel.resolved or not channel.eligible:
        raise _refuse(
            "PUBLISH_CHANNEL_NOT_ELIGIBLE", channel.reason_text,
            next_action="重新准备",
            detail={"channel_option_id": channel.channel_option_id})

    # ── ② 冻结时那个报价 = 这次要冻的钱 ────────────────────────────────
    # 🔴 [窗G] 取数走 ``compute_estimate.frozen_estimate_amount`` —— 与 confirm 那道
    #    「没有预估不许确认」的门**同一个函数**。原来这里自己读一次
    #    ``intent_row["compute_quote_amount"]``,confirm 那侧什么都不读;
    #    现在两侧共用一处实现,改坏它两侧判据一起红(同一谓词只许有一处)。
    #
    #    这一格保留成**纵深防御**:正常路径上 confirm 已经拦掉了,所以它平时不触发。
    #    不删是因为 execute 也可以被别的路径进来(崩溃恢复、以后新增的入口),
    #    而"没数字就花钱"这件事不能只靠上游守。它不承重 ⇒ 不为它单挂判据。
    from services.defensive_geo.xiaobang.compute_estimate import frozen_estimate_amount

    expected_points = frozen_estimate_amount(intent_row)
    if expected_points is None:
        # 报不出价就执行 = 让用户在不知道花多少的情况下花钱。
        raise _refuse(
            "COMPUTE_QUOTE_MISSING",
            "这次安排还没算出要用多少算力,回小榜重新准备一下。",
            next_action="重新准备")

    items = build_execution_items(cur, intent_row=intent_row,
                                  owner_user_id=owner_user_id, receipt_id=receipt_id)

    # ── ③ 同一条产线 ────────────────────────────────────────────────────
    try:
        out = materialize_publish_batch(
            cur,
            request_id=receipt_id,                    # 🔴 幂等键 = 回执
            endpoint=endpoint,
            identity=identity,
            settlement=settlement,
            items=items,
            expected_total_price_points=int(expected_points),
            daily_limit=limit,
            feature_code=PUBLISH_FEATURE_CODE,
            command_id="pubcmd_" + receipt_id)
    except PublishAttemptConflict as exc:
        raise _refuse("PUBLISH_ATTEMPT_EXISTS", "这条作品已有发布记录，先查看提交结果。",
                      next_action="查看进度", detail={"command_id": exc.command_id, "reason": str(exc)[:120]})
    except (PriceChanged, PriceFingerprintMismatch) as exc:
        raise _refuse("PRICE_CHANGED", "投放算力有变化,重新核对一下再执行。",
                      next_action="重新准备", detail={"reason": str(exc)[:120]})
    except CapacityExceeded as exc:
        raise _refuse("PUBLISH_CHANNEL_NOT_ELIGIBLE",
                      "这个投放账号今天发不了了,换一个或者明天再发。",
                      next_action="重新准备", detail={"reason": str(exc)[:120]})
    except ArtifactRequestConflict as exc:
        raise _refuse("PUBLISH_ARTIFACT_NOT_READY",
                      "发布素材需要重新准备,回制作页确认当前版本。",
                      next_action="打开现役页面继续", detail={"reason": str(exc)[:120]})
    except ApprovalPending as exc:
        raise IntentError(
            "APPROVAL_PENDING", "这次投放需要团队负责人确认,已经提交给他了。",
            http_status=409, next_action="查看我的审批",
            detail={"approval_request_id": exc.approval_request_id})
    except LegalGateBlocked as exc:
        raise _refuse("LEGAL_TERM_BLOCKED",
                      "文案里有广告法明令禁止的说法,改掉再发。",
                      next_action="打开现役页面继续",
                      detail={"hit_count": len(exc.hits)})
    except IdempotencyConflict as exc:
        raise _refuse("EXECUTION_CONFLICT",
                      "这次执行已经提交过了,去看进度就行。",
                      next_action="查看进度", detail={"reason": str(exc)[:120]})
    except (FundingHandleInvalid, FreezeProducedNoHandle) as exc:
        # 🔴 资金句柄不成立 = **响亮失败**,不接受「零句柄的成功」。
        raise IntentError(
            "SETTLEMENT_HANDLE_INVALID",
            "提交失败,算力没有被扣除,请稍后重试。",
            http_status=500, next_action="重试", detail={"reason": str(exc)[:120]})

    command_id = "pubcmd_" + receipt_id
    return {
        "command_id": command_id,
        "replayed": bool(out.get("replayed")),
        "items": list(out.get("items") or []),
        "summary": dict(out.get("summary") or {}),
        "media_id": int(items[0]["media_id"]),
    }


__all__ = [
    "build_execution_items",
    "execute_publish_image_note",
    "frozen_execution_binding",
]
