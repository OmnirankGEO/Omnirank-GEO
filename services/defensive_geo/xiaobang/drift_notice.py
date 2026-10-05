"""POR-13 漂移的**用户面**:内容变了 / 价格变了 / 权限变了,各说各的下一步。

Owner 2026-08-24 批:漂移命中 **不静默拒单**,走「内容/价格有更新,请重新确认」
明示文案,并**区分「内容变了 / 价格变了」两种下一步**。

🔴 为什么要区分,而不是一句「有变化,请重新核对」
------------------------------------------------
两者的用户动作完全不同:

* **内容变了** —— 她要回去看看变成了什么(可能是客户换了稿子、换了投放对象),
  看完才知道还确不确认;
* **价格变了** —— 内容没动,只是这次要花的算力不一样了;她要看的是**新数字**,
  一眼就能决定。

把两者压成一句,她只能把两种情况都当成"又出问题了"重新走一遍全流程。

🔴 分母纪律
-----------
:data:`DRIFT_REASONS` 必须**覆盖** ``services.xiaobang_intent.drift_reason``
可能返回的全部取值。少一格 ⇒ 那一档漂移会拿不到文案 ⇒ 上屏一个裸枚举
(§0.5.5 U-1 验收红)。配套判据从 ``drift_reason`` 的**源码**机械枚举它的
返回字面量来对账,不手抄。
"""

from __future__ import annotations

from typing import Any, Mapping

DRIFT_NOTICE_VERSION = "defgeo-xiaobang-drift-notice-v1"

#: 「内容变了」—— 这次要动的东西本身变过了。
CONTENT_DRIFT: tuple[str, ...] = ("payload_hash", "object_manifest_hash")

#: 「价格变了」—— 内容没动,只是这次要花的算力不一样了。
PRICE_DRIFT: tuple[str, ...] = ("compute_quote_hash",)

#: 「权限/归属变了」—— 人或团队这一侧变过了(付款方、组织、成员代际、
#: 指派授权、审批策略、权限代际)。这一档她自己改不动,下一步是回去看看还归不归她。
AUTHORITY_DRIFT: tuple[str, ...] = (
    "payer_user_id", "organization_id", "membership_version",
    "assignment_authority_version", "approval_policy_version", "permission_version",
)

#: 全集 = 三档并集。判据拿它当分母,不手抄。
DRIFT_REASONS: tuple[str, ...] = CONTENT_DRIFT + PRICE_DRIFT + AUTHORITY_DRIFT

#: 漂移原因 → 文案桶。桶名同时是 copy registry 的 code。
_REASON_TO_BUCKET: dict[str, str] = {
    **{r: "content_changed" for r in CONTENT_DRIFT},
    **{r: "price_changed" for r in PRICE_DRIFT},
    **{r: "authority_changed" for r in AUTHORITY_DRIFT},
}

#: 文案桶全集。三档,不多不少。
DRIFT_BUCKETS: tuple[str, ...] = ("content_changed", "price_changed", "authority_changed")


class UnknownDriftReason(KeyError):
    """漂移原因没有对应文案。

    **抛,不回落成裸枚举** —— 回落会让「忘了补译」表现成
    「界面上出现一个英文单词」,而那正是 §0.5.5 U-1 说的验收红,
    却没有任何东西会因此变红。
    """


def drift_bucket(reason: str) -> str:
    """漂移原因归到哪个文案桶。"""
    bucket = _REASON_TO_BUCKET.get(str(reason or ""))
    if bucket is None:
        raise UnknownDriftReason(
            f"漂移原因 {reason!r} 没有登记文案桶。合法 = {list(DRIFT_REASONS)}。"
            "新增一维绑定时必须同步登记,否则那一档漂移会把裸枚举送上屏。"
        )
    return bucket


def drift_notice(reason: str) -> dict[str, str]:
    """一次漂移要下发给用户的全部东西:说什么 + 下一步点哪。

    🔴 两句都从 copy registry 取(U-1 单一 SSOT),本函数只负责选桶。
    """
    from services.defensive_geo.copy_registry import assert_public_copy_clean, user_label

    bucket = drift_bucket(reason)
    message = assert_public_copy_clean(
        user_label("intent_drift", bucket), field=f"intent_drift.{bucket}")
    next_action = assert_public_copy_clean(
        user_label("intent_drift_next_action", bucket),
        field=f"intent_drift_next_action.{bucket}")
    return {"bucket": bucket, "message": message, "next_action": next_action}


def census() -> dict[str, Any]:
    return {
        "driftNoticeVersion": DRIFT_NOTICE_VERSION,
        "driftReasons": list(DRIFT_REASONS),
        "buckets": list(DRIFT_BUCKETS),
        "reasonToBucket": dict(_REASON_TO_BUCKET),
        "contentDrift": list(CONTENT_DRIFT),
        "priceDrift": list(PRICE_DRIFT),
        "authorityDrift": list(AUTHORITY_DRIFT),
    }


__all__ = [
    "AUTHORITY_DRIFT",
    "CONTENT_DRIFT",
    "DRIFT_BUCKETS",
    "DRIFT_NOTICE_VERSION",
    "DRIFT_REASONS",
    "PRICE_DRIFT",
    "UnknownDriftReason",
    "census",
    "drift_bucket",
    "drift_notice",
]
