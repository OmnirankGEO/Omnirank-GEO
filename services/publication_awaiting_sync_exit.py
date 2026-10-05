"""[P0 代发卡单出口] `awaiting_sync` 死胡同 —— 判据 + §13 合同。

## 事故形态(死胡同)

1. mhz 回 `code=200` 但没给订单号(`AmbiguousResponseError`)→ item 标 `awaiting_sync`;
2. `_mhz_awaiting_sync_resolver` 反查 12 小时仍不命中 → `mark_item_manual_review`
   只打一个 `manual_review_required=TRUE` 的**布尔标记**,item 状态**仍是**
   `awaiting_sync`;
3. 而 `find_awaiting_sync_items_to_check` / `find_awaiting_sync_items_overdue`
   两个扫描器的 WHERE 都带 `manual_review_required = FALSE` ——
   **打完标记的那一刻,这条 item 从所有自动扫描里彻底消失**;
4. 此后唯一出口是管理员主动去 `/admin/manual-review-queue` 翻牌。没人翻 = 永远不动。
   线上实证:最老一条滞留 58 天,全库「曾进 awaiting_sync 且已到终态」= 0 笔。

所以根因**不是**"没有兜底任务"(任务是有的),而是**兜底的终点是一个只有管理员
看得见、且不会自己老化升级的标记位** —— 用户侧看到的永远是"等同步回查",
没有任何可点的下一步。这正是元指令「AI 判定必须有人工出口 · 严禁死胡同」禁止的形态。

## 本模块的两件事

1. `should_open_user_exit(...)` —— 什么时候认定"救不回来了",该开人工出口;
2. `build_awaiting_sync_exit_contract(...)` —— 出 §13 合同(七字段 + ≥2 个可执行 action)。

## 为什么出口**不自动退款**(硬约束,不是偏好)

mhz 已经回过"成功提交,正在执行发布"。稿件**有可能真的发出去了**。
自动退款 = 既退了钱又发了稿,双重损失,且不可追回。
所以出口的两个动作都只**记录用户的声明**,真正动钱的那一步由管理员在既有
`/admin/manual-review-queue/resolve` 执行 —— 那条路径已经用
`refund_for_publish_order(refund_key="item:{id}")` 这唯一一套幂等键。

新增任何"用户点了就退钱"的路径都会:
  - 违反 2026-06-08 老板拍板的「禁用户端自助退款入口」;
  - 且若走 `mhz_refund_requests`,`review_refund_request` 在**查不到 order_sn**
    时会回落到 `refund_request:{id}` 键(卡单恰恰没有 order_sn)——
    与 `item:{id}` 互不相认,同一笔钱会被退两次。
"""

from __future__ import annotations

from typing import Any, Optional

# 机器码 / 规则版本
EXIT_CODE = "PUBLISH_AWAITING_SYNC_UNRESOLVED"
EXIT_RULE_VERSION = "publish-awaiting-sync-exit-v1"

# 开人工出口的双条件(必须**同时**满足)：
#   - 反查失败次数 ≥ 3：证明不是偶发抖动,是真的查不到;
#   - 滞留 ≥ 72 小时：mhz 侧偶有小时级同步延迟,给足自愈窗口,防"刚提交就被判死"。
EXIT_MIN_PROBE_ATTEMPTS = 3
EXIT_MIN_AGE_HOURS = 72

# 用户在出口上的声明(只记录,不动钱)
USER_CLAIM_NOT_PUBLISHED = "not_published"
USER_CLAIM_PUBLISHED = "published"
USER_CLAIMS = (USER_CLAIM_NOT_PUBLISHED, USER_CLAIM_PUBLISHED)


def should_open_user_exit(
    *,
    probe_attempts: Optional[int],
    awaiting_hours: Optional[float],
    min_attempts: int = EXIT_MIN_PROBE_ATTEMPTS,
    min_hours: float = EXIT_MIN_AGE_HOURS,
) -> bool:
    """够不够格开人工出口。两个条件是 **AND**,任一不满足都继续留在自动挽回里。

    None 一律按"还不够"处理 —— 判据缺失时宁可多等一轮,也不提前把在途单判死。
    """
    if probe_attempts is None or awaiting_hours is None:
        return False
    try:
        attempts = int(probe_attempts)
        hours = float(awaiting_hours)
    except (TypeError, ValueError):
        return False
    return attempts >= int(min_attempts) and hours >= float(min_hours)


def _describe_media(media_name: Optional[str]) -> str:
    name = (media_name or "").strip()
    return f"「{name}」" if name else "这家媒体"


def build_awaiting_sync_exit_contract(
    *,
    item_id: int,
    media_name: Optional[str] = None,
    awaiting_hours: Optional[float] = None,
    probe_attempts: Optional[int] = None,
) -> dict[str, Any]:
    """§13 七字段合同。**至少两个可执行 action**,其中一个必须是「确认未发布 · 退还算力」。

    文案纪律:
      - 不说"已发布"也不说"没发布" —— 我们确实不知道,谎报任何一边都会造成损失;
      - 不承诺"立即退款" —— 动钱要人工核实,承诺了做不到比不承诺更伤;
      - 不指称用户做错了什么 —— 这是平台侧与 mhz 的对接问题。
    """
    iid = int(item_id)
    waited = ""
    if awaiting_hours is not None:
        try:
            days = int(float(awaiting_hours) // 24)
            waited = f"已等待 {days} 天," if days >= 1 else f"已等待 {int(float(awaiting_hours))} 小时,"
        except (TypeError, ValueError):
            waited = ""

    probed = ""
    if probe_attempts is not None:
        try:
            probed = f"系统已自动反查 {int(probe_attempts)} 次仍未找到。"
        except (TypeError, ValueError):
            probed = ""

    return {
        "code": EXIT_CODE,
        "message": f"这条发到{_describe_media(media_name)}的稿件,平台一直没拿到发布回执,需要你确认一下。",
        "reason": (
            f"媒体方当时回执了「已接收」,但没有返回可追踪的订单号,{waited}"
            f"平台无法自动判断稿件最终有没有发出去。{probed}"
        ).strip(),
        "impact": "这条发布任务停在这里,没有再重复提交、也没有再扣新的费用;原扣的算力仍在占用中。",
        "repair_hint": (
            "如果你在该媒体上没找到这篇稿件,点「确认未发布 · 退还算力」,"
            "我们人工核实后按原路退回算力;如果已经发出来了,点「已发布 · 补录链接」把链接补上即可结案。"
        ),
        "actions": [
            {
                "id": "report_not_published",
                "label": "确认未发布 · 退还算力",
                "type": "api",
                "method": "POST",
                "target": f"/api/meijiehezi/items/{iid}/report-not-published",
            },
            {
                "id": "record_publish_url",
                "label": "已发布 · 补录链接",
                "type": "api",
                "method": "POST",
                "target": f"/api/meijiehezi/items/{iid}/record-publish-url",
                "fields": [
                    {
                        "name": "publish_url",
                        "label": "稿件链接",
                        "type": "url",
                        "required": True,
                        "placeholder": "https://...",
                    },
                ],
            },
            {
                "id": "view_orders",
                "label": "查看发布任务",
                "type": "nav",
                "target": "/publish",
            },
        ],
        "rule_version": EXIT_RULE_VERSION,
    }
