"""[P0 代发内容漂移]发布窗口内的稿件一致性 —— 根因阻断 + 软失败 + 出口。

## 事故形态(死锁)

1. 用户下单代发,系统冻结一份稿件快照(`mhz_publish_orders.article_content_snapshot`);
2. **发布窗口还开着**,`placement_service` 的 LLM 自动修复(或插图)把
   `articles.content` 改了 —— 它压根不看这篇文章有没有在途发布单;
3. 提交/重试时稿件与快照对不上 = 内容漂移,提交失败;
4. 重试**沿用旧快照**,必然再失败,直到"重试耗尽 → 退款";
5. 用户看到一句没有下一步的失败,再下单又撞同一堵墙。

所以出口按钮只是止痛,**根因是第 2 步**:发布窗口内不该有人偷偷改稿。

## 本模块的三件事

1. `article_has_active_publication(...)` —— 给写入方问"这篇现在能不能改";
2. `detect_order_content_drift(...)` —— 提交/重试前判漂移,并**判断是谁改的**;
3. `build_drift_contract(...)` —— 出 §13 合同(七字段 + 至少一个可执行 action)。

## 归因原则:默认不甩锅

漂移不等于"用户乱改"。绝大多数情况是系统自己在准备期优化了稿件。
判据(按可信度排序):
- 有自动修复审计痕迹(`article_review` 刷新记录 / kb issue 被标记修复)→ 系统侧;
- 无审计痕迹但 `articles.updated_at` 落在订单创建之后 → 无法归因,按 `unknown` 说话;
- 只有在能确证是人工编辑时才用"这篇稿件在提交后被修改过"的措辞。

**任何情况下都不得预设是用户改的** —— 说错一次,用户就觉得平台在推卸责任。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger("GEO-PublishDrift")

# 漂移的机器码。软失败 —— **不进**"重试耗尽 → 退款"通道。
DRIFT_CODE = "PUBLISH_CONTENT_DRIFT"
DRIFT_RULE_VERSION = "publish-content-drift-v1"

# item 落脚状态:挂起等人工动作。刻意不用 'failed' —— failed 会触发退款往返,
# 而漂移是可修复的,退了钱用户还得重下单,体验更差、对账也更脏。
ITEM_STATUS_AWAITING_ACTION = "awaiting_action"

# 这些状态视为"发布仍在途",此时禁止改稿
ACTIVE_ITEM_STATUSES = ("pending", "submitting", "submitted", "awaiting_sync",
                        "awaiting_confirmation", ITEM_STATUS_AWAITING_ACTION)

ORIGIN_SYSTEM = "system_rewrite"
ORIGIN_USER = "user_edit"
ORIGIN_UNKNOWN = "unknown"


def content_hash(text: Optional[str]) -> str:
    """与快照落库时同一口径的哈希。None/空串统一成空串,避免 NULL 与 '' 判成漂移。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DriftVerdict:
    drifted: bool
    origin: str = ORIGIN_UNKNOWN
    #: 'content' | 'title' | 'both' —— 用于文案与排查,不改变处置方式
    field: str = "content"
    snapshot_hash: Optional[str] = None
    current_hash: Optional[str] = None
    article_id: Optional[int] = None
    order_id: Optional[int] = None


def article_has_active_publication(cursor, article_id: int) -> bool:
    """这篇文章现在有没有在途的发布单。

    写入方(自动改稿/插图)必须先问这一句 —— 有在途发布就让路,别在窗口里改稿。
    任何异常返回 **True**(按"有在途"处理 = 不改):宁可少改一次稿,
    也不能把在途订单改崩。
    """
    try:
        cursor.execute(
            """
            SELECT 1
              FROM mhz_publish_order_items i
              JOIN mhz_publish_orders o ON o.id = i.order_id
             WHERE o.article_id = %s
               AND i.status = ANY(%s)
             LIMIT 1
            """,
            (int(article_id), list(ACTIVE_ITEM_STATUSES)),
        )
        return cursor.fetchone() is not None
    except Exception as exc:  # pragma: no cover - 防御
        logger.warning("[drift] 在途发布查询失败,按'有在途'处理 article=%s: %s", article_id, exc)
        return True


def classify_drift_origin(cursor, *, article_id: int, order_created_at) -> str:
    """判断稿件是被**系统**改的还是被**人**改的。判不出来就说 unknown,不猜。"""
    # 1) 系统自动修复会刷新审核记录并把人工复核清空;这是最强的系统侧痕迹
    try:
        cursor.execute(
            """
            SELECT article_review_status, article_human_review_status, updated_at
              FROM articles
             WHERE id = %s
            """,
            (int(article_id),),
        )
        row = cursor.fetchone()
    except Exception as exc:  # pragma: no cover
        logger.warning("[drift] 归因查询失败 article=%s: %s", article_id, exc)
        return ORIGIN_UNKNOWN

    if not row:
        return ORIGIN_UNKNOWN

    def _get(key):
        return row.get(key) if isinstance(row, Mapping) else None

    # 自动修复路径会调用 refresh_article_review → 人工复核状态被清空(NULL)
    # 且机器审核状态被重写。人工编辑走的是别的路径,不会清空人工复核。
    if _get("article_human_review_status") is None and _get("article_review_status"):
        return ORIGIN_SYSTEM

    updated_at = _get("updated_at")
    if updated_at is not None and order_created_at is not None:
        try:
            if updated_at > order_created_at:
                # 确实是下单之后改的,但没有系统侧痕迹 —— **不能**就此断定是用户改的
                return ORIGIN_UNKNOWN
        except TypeError:
            return ORIGIN_UNKNOWN
    return ORIGIN_UNKNOWN


def detect_order_content_drift(cursor, order: Mapping[str, Any]) -> DriftVerdict:
    """比对订单快照与文章当前内容。没有快照 = 无从判断,按"没漂移"放行。"""
    snapshot_hash = order.get("article_content_snapshot_hash")
    article_id = order.get("article_id")
    if not snapshot_hash or not article_id:
        return DriftVerdict(drifted=False, article_id=article_id, order_id=order.get("id"))

    try:
        cursor.execute("SELECT content, title FROM articles WHERE id = %s", (int(article_id),))
        row = cursor.fetchone()
    except Exception as exc:  # pragma: no cover
        logger.warning("[drift] 读取文章失败 article=%s: %s", article_id, exc)
        return DriftVerdict(drifted=False, article_id=article_id, order_id=order.get("id"))

    current = (row.get("content") if isinstance(row, Mapping) else None) if row else None
    current_hash = content_hash(current)
    content_drifted = current_hash != str(snapshot_hash).strip()

    # 标题漂移同理:标题也是客户确认过的一部分,改了同样不能照发。
    # 订单快照里存的是下单时的标题(article_title)。
    snapshot_title = order.get("article_title")
    current_title = (row.get("title") if isinstance(row, Mapping) else None) if row else None
    title_drifted = bool(
        snapshot_title is not None
        and current_title is not None
        and str(snapshot_title).strip() != str(current_title).strip()
    )

    if not content_drifted and not title_drifted:
        return DriftVerdict(drifted=False, snapshot_hash=str(snapshot_hash),
                            current_hash=current_hash, article_id=article_id,
                            order_id=order.get("id"))

    field = "both" if (content_drifted and title_drifted) else ("title" if title_drifted else "content")

    origin = classify_drift_origin(
        cursor, article_id=int(article_id), order_created_at=order.get("created_at")
    )
    return DriftVerdict(
        drifted=True, origin=origin, field=field, snapshot_hash=str(snapshot_hash),
        current_hash=current_hash, article_id=article_id, order_id=order.get("id"),
    )


def build_drift_contract(verdict: DriftVerdict) -> dict[str, Any]:
    """§13 七字段告警合同。**至少一个可执行 action** —— 没有下一步的红码是事故。

    文案按归因分情形,且默认不指称用户(见模块 docstring)。
    """
    what = {"title": "标题", "both": "标题和正文", "content": "正文"}.get(verdict.field, "正文")

    if verdict.origin == ORIGIN_SYSTEM:
        message = "系统在准备期间优化了这篇稿件,需要重新确认一次即可发布。"
        reason = f"稿件的{what}在下单后被系统自动优化过,与下单时冻结的版本不一致。"
    elif verdict.origin == ORIGIN_USER:
        message = "这篇稿件在提交后被修改过,重新确认后即可发布。"
        reason = f"稿件的{what}与下单时冻结的版本不一致。"
    else:
        # 判不出来是谁改的 —— 只陈述事实,不指向任何一方
        message = "这篇稿件与下单时的版本不一致,重新确认一次即可发布。"
        reason = f"稿件的{what}与下单时冻结的版本不一致,未能确定变更来源。"

    return {
        "code": DRIFT_CODE,
        "message": message,
        "reason": reason,
        "impact": "本次没有发布,也没有扣新的费用;原订单已挂起等你确认。",
        "repair_hint": "点「重新准备并审核」,系统会用最新稿件重新生成一份待发版本并送审,通过后即可再次提交。",
        "actions": [
            {
                "id": "reprepare_and_review",
                "label": "重新准备并审核",
                "type": "api",
                "method": "POST",
                "target": f"/api/meijiehezi/orders/{verdict.order_id}/re-prepare",
            },
            {
                "id": "view_article",
                "label": "先看看稿件",
                "type": "nav",
                "target": f"/writing?article_id={verdict.article_id}",
            },
        ],
        "rule_version": DRIFT_RULE_VERSION,
        "drift_origin": verdict.origin,
    }


def contract_to_columns(contract: Mapping[str, Any]) -> tuple[str, str, str]:
    """[P1] 拆成 `reject_code / reject_user_message / reject_contract(JSON)` 三列。"""
    return (
        str(contract.get("code") or ""),
        str(contract.get("message") or ""),
        json.dumps(dict(contract), ensure_ascii=False),
    )
