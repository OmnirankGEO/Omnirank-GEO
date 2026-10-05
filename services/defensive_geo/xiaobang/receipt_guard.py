"""POR-13 —— 聊天「好的」零扣费。

规格 POR-13 逐字:

    「小榜只有未过期、未撤销且逐值绑定 tenant/actor/command/object/snapshot/
      hash/exact points 的**现役** confirmation receipt 才能调用 domain confirm;
      同 receipt 仅幂等重放同 command,聊天『好的』/模型自报/换任一维零副作用。」

🔴 先纠正两个会让判据恒绿的误解(census 实测)
--------------------------------------------
**误解一:「POR-13」在仓里能 grep 到。** 不能 —— 全仓零命中。
仓内的对应物是 ``services/xiaobang_command_contract.EXTERNAL_BIND_FIELDS``
(11 项)+ ``xiaobang_intent.drift_reason`` + 回执表的 ``bound_*`` 列。

**误解二:回执自带内容绑定校验。** **不成立**。
``xiaobang_confirmation_receipts`` 上确实有 ``bound_payload_hash`` /
``bound_object_manifest_hash`` / ``bound_compute_quote_hash`` 三列,
但现役 ``_assert_confirmation_present`` 与 ``consume_confirmation_receipt``
**从不读它们** —— 漂移是靠 ``drift_reason`` 打 **intent 行**判的。
所以任何"回执 hash 对得上 ⇒ 绑定成立"的判据都是恒绿的。
本模块因此把校验显式打在 **intent 行 + 回执可用性**两处,
并在 :func:`assert_bound_columns_are_not_load_bearing` 里把这条
"看起来在守其实没人读"的事实钉成一条会红的判据。

🔴 本模块**不重新实现**五阶段
-----------------------------
§0.5.1 第 2 条明令。这里只提供**核验谓词**,由现役
``api/xiaobang_operations_api`` 在它既有的检查顺序里调用 ——
顺序本身是承重的(重放短路 → revision → 取消/过期 → 审批 → 漂移 →
确认档 → executable),往前插新门会把 ``CONFIRMATION_REQUIRED``
那条判据吞成死码。
"""

from __future__ import annotations

from typing import Any, Mapping

from services.xiaobang_command_contract import (
    COMPUTE_ONLY_MIN_BIND_FIELDS,
    EXTERNAL_BIND_FIELDS,
    SIDE_EFFECT_COMPUTE_ONLY,
    SIDE_EFFECT_EXTERNAL,
)

RECEIPT_GUARD_VERSION = "defgeo-xiaobang-receipt-guard-v1"

#: POR-13 逐字点名的七个维度 → 现役 ``EXTERNAL_BIND_FIELDS`` 的对应项。
#: 🔴 这张映射表是**对账用**的:判据遍历它,核对每个 POR-13 维度
#:    都真的落在现役绑定字段里。少一格 = POR-13 有一维没人绑。
POR13_DIMENSION_TO_BIND_FIELD: dict[str, str] = {
    "tenant": "tenant_owner_id",
    "actor": "actor_id",
    "object": "object_manifest_hash",
    "snapshot": "payload_hash",
    "hash": "compute_quote_hash",
    "exact_points": "compute_quote_hash",
}

#: 🔴 POR-13 的第七维「command」**不是**靠 bind field 绑的,
#:    而是靠回执表上的 ``UNIQUE (intent_id, intent_revision)``
#:    (迁移 038 · ``uq_xb_receipt_intent_revision``)。
#:
#:    把它硬塞进上面那张映射表(比如指向 ``assignment_authority_version``)
#:    会得到一条**恒绿**的对账 —— 那个字段绑的是"谁有权指派",
#:    与"这张回执属于哪一条命令"根本不是一回事。
#:    诚实地单列出来,并由 :func:`assert_receipt_usable` 逐值核
#:    ``intent_id`` / ``intent_revision``,才是这一维真正的守卫。
POR13_COMMAND_DIMENSION_BOUND_BY = "unique(intent_id, intent_revision)"

#: 「聊天里的同意」不是授权。这些**都不是**有效凭证 ——
#: 判据逐个喂进来,每一个都必须零副作用。
NON_AUTHORIZATIONS: tuple[str, ...] = (
    "chat_affirmation",        # 用户在聊天里打了「好的」
    "model_self_report",       # 模型自己说「用户已同意」
    "host_button_callback",    # 宿主 app 的按钮回调
    "prior_unrelated_receipt",  # 别的操作的回执
)


class ReceiptGuardError(ValueError):
    """凭证不成立。**零副作用**,不调 domain confirm。"""


def assert_bind_fields_cover_por13(side_effect: str) -> None:
    """对账:POR-13 的七维是否都落在现役绑定字段里。

    🔴 这条**不是**装饰性检查。合同哪天加/删一个绑定字段,
       这里会立刻告诉我们"POR-13 的哪一维现在没人绑了" ——
       而不是等到某个跨维重放真的被放行。
    """
    if side_effect == SIDE_EFFECT_EXTERNAL:
        available = set(EXTERNAL_BIND_FIELDS)
    elif side_effect == SIDE_EFFECT_COMPUTE_ONLY:
        available = set(COMPUTE_ONLY_MIN_BIND_FIELDS)
    else:
        raise ReceiptGuardError(f"未知 side_effect {side_effect!r}")

    unbound = sorted({
        dim for dim, field in POR13_DIMENSION_TO_BIND_FIELD.items()
        if field not in available
    })
    if unbound:
        raise ReceiptGuardError(
            f"{side_effect} 档的绑定字段没有覆盖 POR-13 维度 {unbound}。"
            "换掉这些维度中任何一个仍能消费同一张回执 —— "
            "这正是 §19 变异 109 的形态。"
        )
    # 第七维单独核:它不在 bind field 里,靠回执表的唯一约束。
    if POR13_COMMAND_DIMENSION_BOUND_BY != "unique(intent_id, intent_revision)":
        raise ReceiptGuardError(
            "POR-13 的 command 维改了绑定方式 —— 必须同步复核 "
            "assert_receipt_usable 里逐值核 intent_id/intent_revision 的那两行"
        )


def assert_bound_columns_are_not_load_bearing(read_call_sites: Mapping[str, int]) -> None:
    """把「``bound_*`` 三列没人读」这个事实钉成一条**会红**的判据。

    ``read_call_sites`` 由判据用 AST/census 机械导出:
    ``{"bound_payload_hash": 0, "bound_object_manifest_hash": 0,
       "bound_compute_quote_hash": 0}``。

    🔴 为什么要断言它们**等于 0** 而不是 >0:
       今天的真相就是 0。写成 >0 会当场红,写成"随便"则等于没判据。
       断言 =0 的价值在于:哪天有人开始读它们(把绑定改成回执自校验),
       这条判据会红,提醒他**同时**去改依赖 ``drift_reason`` 的那些结论 ——
       否则会出现"两处各绑一半、合起来谁也没绑全"。
    """
    unexpected = {k: v for k, v in read_call_sites.items() if v != 0}
    if unexpected:
        raise ReceiptGuardError(
            f"回执 bound_* 列出现了运行时读取方 {unexpected}。"
            "现役绑定校验走的是 intent 行上的 drift_reason,不是回执自校验。"
            "若要改成回执自校验,必须同时重锚所有依赖 drift_reason 的结论 —— "
            "两处各绑一半 = 合起来谁也没绑全。"
        )


def assert_not_a_chat_affirmation(authorization_kind: str) -> None:
    """POR-13 末句:聊天「好的」/模型自报/宿主回调**都不是**资金或发布授权。"""
    if authorization_kind in NON_AUTHORIZATIONS:
        raise ReceiptGuardError(
            f"{authorization_kind} 不是有效授权。"
            "只有未过期、未撤销、逐值绑定的**现役 confirmation receipt** "
            "才能调用 domain confirm(POR-13)。"
            "用户在聊天里说「好的」时,请打开核对页让她真的按一次确认按钮。"
        )


def assert_receipt_usable(
    receipt_row: Mapping[str, Any] | None,
    *,
    intent_id: str,
    intent_revision: int,
    execution_request_id: str = "",
    expired: bool = False,
) -> Mapping[str, Any]:
    """回执可用性 —— 现役 ``_assert_confirmation_present`` **调的就是这一个**。

    「可用」有两种,少一种就把幂等重放判成未确认:

    * 还没消费且没过期 —— 第一次执行;
    * **已被同一个** ``execution_request_id`` **消费过** —— 用户双击 / 客户端重试。
      回执是单次消费品,第一次执行已经烧掉它了;这时再要求"未消费"
      等于让第二发拿到 ``CONFIRMATION_REQUIRED``,而正确答案是回放同一结果。

    🔴 ``expired`` 由**调用方用库的钟**算好传进来(``receipt_expires_at <= NOW()``),
       本函数不自己取时间。两个理由都是硬的:
       ① 判据里不许有"今天" —— 函数内部读挂钟会让这条谓词的可判性依赖跑的时刻;
       ② 消费那一侧(``consume_confirmation_receipt``)用的就是**库的** ``NOW()``,
          在这里换成进程时钟等于给同一个问题引入第二只钟,时钟漂移时两侧会打架。

    🔴 「已消费」优先于「已过期」:一张过期但**已被本次执行消费掉**的回执仍然可用,
       否则崩溃重放会在回执 TTL 之后拿到"未确认",而那次执行明明已经成功了。
    """
    if receipt_row is None:
        raise ReceiptGuardError(
            "还没有你的确认,不能执行。")
    if str(receipt_row.get("intent_id")) != str(intent_id):
        # 纵深防御·非承重:现役调用方按 intent_id 查行,这一支查不出反例。
        # 保留是因为它零成本,且换一种取行方式(比如按 receipt_id 查)时它立刻承重。
        raise ReceiptGuardError("这张确认回执绑的是另一个操作,不能用在这一次")
    consumed_at = receipt_row.get("consumed_at")
    if int(receipt_row.get("intent_revision") or -1) != int(intent_revision):
        raise ReceiptGuardError(
            "你确认的是这次操作的上一版 —— 内容后来变过了,请重新核对一次再确认。")
    if consumed_at is not None:
        consumed_by = str(receipt_row.get("consumed_by_execution_request_id") or "")
        if not execution_request_id or consumed_by != str(execution_request_id):
            raise ReceiptGuardError(
                "这次确认已经用过了。如果是重复点击,请沿原来的执行记录查看结果,"
                "不要重新发起 —— 不会重复扣算力。"
            )
    elif expired:
        raise ReceiptGuardError(
            "你刚才那次确认已经过期了,重新核对一次就能继续;没有扣除任何算力。")
    return receipt_row


def census() -> dict[str, Any]:
    return {
        "guardVersion": RECEIPT_GUARD_VERSION,
        "por13Dimensions": sorted(
            list(POR13_DIMENSION_TO_BIND_FIELD) + ["command"]),
        "por13CommandBoundBy": POR13_COMMAND_DIMENSION_BOUND_BY,
        "por13DimensionToBindField": dict(POR13_DIMENSION_TO_BIND_FIELD),
        "externalBindFields": list(EXTERNAL_BIND_FIELDS),
        "computeOnlyMinBindFields": list(COMPUTE_ONLY_MIN_BIND_FIELDS),
        "nonAuthorizations": list(NON_AUTHORIZATIONS),
    }
