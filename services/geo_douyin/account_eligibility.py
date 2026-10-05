"""WP5 · 图文账号资格的**唯一** canonical 服务(规格 02 §6.1)。

## 统一资源键 `(media_type='svideo', media_id)`

目录、推荐、预检、正式提交**全部**调用同一服务。规格原文:
「至少复用并校正:抖音平台、`can_tuwen=1`、active、blacklist=0、真实频控。
`load_today_counts` 必须显式限定 `media_type='svideo'`,避免跨 lane 同 ID 串数」。

## 现役 `load_today_counts` 的真缺陷(现尖复核实证 2026-08-17)

`services/geo_douyin/publish_adapter.py:148-155` 的查询是:

    WHERE media_id = ANY(...) AND created_at >= CURRENT_DATE
      AND status NOT IN ('cancelled','withdrawn','failed','rejected')

**没有 `media_type` 条件**。而 `mhz_publish_order_items.media_type` 是存在的列
(生产 schema 实测),文章 lane 与短视频 lane 共用这张表。于是:

  · 同一个数字 `media_id` 在两个 lane 各指一个**不同**的媒体;
  · 图文的当日额度会把文章 lane 的单子也数进去 → 明明没发满却被判"今天已满";
  · 反方向同理,某些情况下会**少数**,导致超发。

这不是理论风险:`idx_mhz_item_capacity` 这类索引以 `(capacity_date, media_type, media_id)`
三元组为键,正说明 `media_id` 单独不构成账号身份。

## 为什么不直接改 publish_adapter 那个函数

改它会同时改变现役 `/frequency-check` 端点的行为(那是活的线上路径)。
本模块提供**收口后**的版本给新链用,并把老函数标注为 legacy;
cutover 由 WP5 的 caller census 统一做,不在这里顺手改别人正在跑的路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Optional, Sequence

#: 统一资源键的 media_type。图文与短视频共用抖音账号池,lane 标识是 'svideo'。
MEDIA_TYPE_SVIDEO: Final = "svideo"

#: 不占用当日额度的终态 —— 这些稿子根本没发出去。
#: 与现役 `load_today_counts` 逐字一致(不改口径,只补 media_type 维度)。
NON_CONSUMING_STATUSES: Final[tuple[str, ...]] = (
    "cancelled", "withdrawn", "failed", "rejected",
)

REASON_NOT_FOUND: Final = "ACCOUNT_NOT_FOUND"
REASON_INACTIVE: Final = "ACCOUNT_INACTIVE"
REASON_BLACKLISTED: Final = "ACCOUNT_BLACKLISTED"
REASON_NOT_DOUYIN: Final = "ACCOUNT_NOT_DOUYIN"
REASON_NO_IMAGE_NOTE: Final = "ACCOUNT_CANNOT_PUBLISH_IMAGE_NOTE"
REASON_FREQUENCY_FULL: Final = "FREQUENCY_LIMIT_REACHED"


@dataclass
class AccountEligibility:
    media_id: int
    capabilities: dict[str, bool] = field(default_factory=dict)
    available: bool = False
    today_remaining: int = 0
    reason_code: Optional[str] = None
    next_available_at: Optional[str] = None
    final_price_points: Optional[int] = None
    price_version: Optional[str] = None

    def to_dto(self) -> dict[str, Any]:
        """规格 §6.1 的响应形状。

        🔴 只出算力,不出供应商原始名称/回包/成本价/倍率(规格 §12 末 + 开发原则第 6 条)。
        """
        return {
            "media_id": int(self.media_id),
            "capabilities": dict(self.capabilities),
            "availability": {
                "available": bool(self.available),
                "today_remaining": int(self.today_remaining),
                "reason_code": self.reason_code,
                "next_available_at": self.next_available_at,
            },
            "final_price_points": self.final_price_points,
            "price_version": self.price_version,
        }


_TODAY_COUNTS_SQL = """
SELECT media_id, count(*) AS today_used
  FROM mhz_publish_order_items
 WHERE media_id = ANY(%(media_ids)s::int[])
   AND media_type = %(media_type)s
   AND created_at >= CURRENT_DATE
   AND (status IS NULL OR status <> ALL(%(non_consuming)s::text[]))
 GROUP BY media_id
"""


def load_today_counts_scoped(cur, media_ids: Sequence[int], *,
                             media_type: str = MEDIA_TYPE_SVIDEO) -> dict[int, int]:
    """当日已用量,**按 lane 限定**。

    🔴 `AND media_type = %(media_type)s` 是本函数与现役版本的**唯一**区别,
       也是它存在的全部理由:没有这一句,`media_id` 在两个 lane 之间串数。
       删掉它,`test_today_counts_do_not_cross_lanes` 当场红。

    🔴 `status IS NULL OR status <> ALL(...)`:现役写的是 `NOT IN (...)`,
       对 NULL 恒 UNKNOWN ⇒ status 为 NULL 的行会被**整行漏掉**(少数 → 超发)。
       生产 `mhz_publish_order_items.status` 有 DEFAULT 'pending' 但**可为 NULL**
       (schema 实测无 NOT NULL),所以这不是假想。
    """
    ids = [int(m) for m in (media_ids or []) if m]
    if not ids:
        return {}
    cur.execute(_TODAY_COUNTS_SQL, {
        "media_ids": ids,
        "media_type": media_type,
        "non_consuming": list(NON_CONSUMING_STATUSES),
    })
    return {int(dict(r)["media_id"]): int(dict(r)["today_used"]) for r in (cur.fetchall() or [])}


_ACCOUNT_SQL = """
SELECT id, media_name, platform, is_active,
       COALESCE(blacklist, 0) AS blacklist,
       COALESCE(can_tuwen, 0) AS can_tuwen,
       our_price_points, price
  FROM mhz_short_video
 WHERE id = ANY(%(media_ids)s::int[])
"""


def evaluate_accounts(cur, media_ids: Sequence[int], *, daily_limit: int,
                      price_resolver=None,
                      media_type: str = MEDIA_TYPE_SVIDEO) -> dict[int, AccountEligibility]:
    """一次评估一批账号。目录 / 推荐 / preflight / 正式提交**共用这一个出口**。

    🔴 判定顺序刻意是「先身份、后频控」:一个不能发图文的号,不该因为"今天还没发过"
       就显示成可用 —— 用户会一直等它,而它永远不会变成可用。
       reason_code 也因此是**第一个**失败原因,不是最后一个。
    """
    ids = [int(m) for m in (media_ids or []) if m]
    if not ids:
        return {}
    cur.execute(_ACCOUNT_SQL, {"media_ids": ids})
    rows = {int(dict(r)["id"]): dict(r) for r in (cur.fetchall() or [])}
    used = load_today_counts_scoped(cur, ids, media_type=media_type)

    out: dict[int, AccountEligibility] = {}
    for media_id in ids:
        row = rows.get(media_id)
        if row is None:
            out[media_id] = AccountEligibility(media_id=media_id, reason_code=REASON_NOT_FOUND)
            continue
        can_image_note = int(row.get("can_tuwen") or 0) == 1
        capabilities = {"image_note": can_image_note, "video": True}

        reason: Optional[str] = None
        if not bool(row.get("is_active")):
            reason = REASON_INACTIVE
        elif int(row.get("blacklist") or 0) != 0:
            reason = REASON_BLACKLISTED
        elif str(row.get("platform") or "") != "抖音":
            reason = REASON_NOT_DOUYIN
        elif not can_image_note:
            reason = REASON_NO_IMAGE_NOTE

        remaining = max(0, int(daily_limit) - int(used.get(media_id, 0)))
        if reason is None and remaining <= 0:
            reason = REASON_FREQUENCY_FULL

        price_points = price_version = None
        if price_resolver is not None:
            resolved = price_resolver(row) or {}
            price_points = resolved.get("final_price_points")
            price_version = resolved.get("price_version")

        out[media_id] = AccountEligibility(
            media_id=media_id,
            capabilities=capabilities,
            available=reason is None,
            today_remaining=remaining if reason in (None, REASON_FREQUENCY_FULL) else 0,
            reason_code=reason,
            final_price_points=price_points,
            price_version=price_version,
        )
    return out
