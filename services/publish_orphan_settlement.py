"""代发「僵尸孤儿单」收口 —— 拿不到外部单号的条目怎么判、怎么退。

背景(WO-PUB-ZOMBIE-2026-08-04 · 生产实证):
批量下单时部分 item 没拿到 ``mhz_order_id``,既没标失败也没退款,永远停在
``pending``;而去重判据把 ``pending`` 当"进行中",于是这些文章对该媒体**永久锁死**。
生产实测 12 条真僵尸、真实用户资金滞留 17,355 算力、最久 98 天。

本模块只负责一件事:**一条拿不到外部单号的 item,该退款还是该转人工。**

🔴 三分支(不可简化成"查不到就退"):

===================  =========================  ==============================
外部镜像有无同名单    该单号是否已被本地条目认领   处理
===================  =========================  ==============================
查不到                —                          退款(对方根本没收到)
查到                  是(本地重复下单)            退款(重复收费)
查到                  **否**                     🔴 **禁止自动退款** · 转人工
===================  =========================  ==============================

第三种就是"对方收了、我们丢了回执"(请求到达对方并建单,响应在回程丢失)。
那种单对方真在发,自动退款 = 既退了钱又发了稿。生产 2026-08-04 实测当下就有
一条命中(item 490),**不是理论分支**。

🔴 前置:镜像完整性。``mhz_synced_orders`` 由 ``_mhz_status_sync`` 全量刷新,
某次同步失败/不完整时,"外部查不到"就是**假结论**。所以每轮先过两道闸:
  1. `mirror_is_fresh` —— 同步时间戳新鲜度(超时则整轮跳过,一分不退);
  2. `mirror_lookup_is_discriminating` —— 拿**已知有单号**的条目回查镜像,
     必须 100% 命中。命中不了说明查法/镜像本身失效,这时"查不到"毫无判别力。

退款一律走既有 ``db.meijiehezi_db.refund_for_publish_order`` + 幂等键
``item:{id}``,不另造第二条资金路径。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("GEO-PublishOrphan")

#: 判定结果:可以自动退款。
ACTION_REFUND = "refund"
#: 判定结果:禁止自动退款,转人工。
ACTION_MANUAL_REVIEW = "manual_review"

#: 镜像同步时间戳超过这个岁数就认为镜像不可信,整轮跳过(一分不退)。
MIRROR_MAX_AGE_MINUTES_DEFAULT = 60

#: 下单后多久还没拿到外部单号就判定为僵尸。
#: 取 3 小时是因为既有重试扫描 `_retry_stuck_publish_orders` 的窗口是
#: (创建后 10 分钟, 1 小时),3 小时让正常重试跑完整整三轮才轮到清扫器。
ORPHAN_MIN_AGE_HOURS_DEFAULT = 3

#: 允许被清扫的源状态。
#: 🔴 只收"卡住且本不该卡"的态:
#:   · pending / submitted —— 正常应在几分钟内拿到单号
#:   · paused_admin_dedupe —— 人工暂停态,生产实测 3 条卡了 98 天没有任何出口
#: 🔴 刻意**不含** awaiting_sync / awaiting_confirmation / awaiting_action:
#:   那三个态各有自己的 resolver 和人工出口(见 `_mhz_awaiting_sync_resolver`),
#:   在这里重复处置等于两个主人抢一条 item。
SWEEPABLE_STATUSES = ("pending", "submitted", "paused_admin_dedupe")

#: 同步时间戳按渠道各查各的 —— 软文通道挂了不该冻住自媒体通道的清扫,反之亦然。
_SYNC_CONFIG_KEY_BY_MEDIA_TYPE = {
    "mhz": "last_status_sync",
    "": "last_status_sync",
    "wemedia": "last_toutiao_status_sync",
}


@dataclass
class OrphanSweepResult:
    scanned: int = 0
    refunded_items: int = 0
    refunded_points: int = 0
    manual_review_items: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"本轮跳过({self.skipped_reason})"
        return (f"扫描{self.scanned} 退款{self.refunded_items}条/{self.refunded_points}算力 "
                f"转人工{self.manual_review_items}条 错误{len(self.errors)}")


# ---------------------------------------------------------------------------
# 纯判定 —— 不碰 DB,便于逐格锁死
# ---------------------------------------------------------------------------

def decide_orphan_action(mirror_rows: Optional[list[dict[str, Any]]]) -> str:
    """三分支判定。``mirror_rows`` 是外部镜像里同名同媒体的记录。

    每行形如 ``{"order_sn": "...", "claimed_by": <本地 item_id> | None}``,
    ``claimed_by is None`` 表示这个外部单号**没有任何本地条目认领**。

    只要有**一行**没被认领,就一律转人工 —— 宁可慢,不可退错。
    """
    if not mirror_rows:
        return ACTION_REFUND
    for row in mirror_rows:
        if row.get("claimed_by") is None:
            return ACTION_MANUAL_REVIEW
    return ACTION_REFUND


def parse_sync_timestamp(raw: Any) -> Optional[datetime]:
    """把 mhz_config 里存的 ISO 字符串解析成 datetime。解析不了返回 None。"""
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def sync_age_minutes(raw: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    """同步时间戳距今多少分钟。解析不了返回 None(调用方按不新鲜处理)。"""
    stamp = parse_sync_timestamp(raw)
    if stamp is None:
        return None
    ref = now or datetime.now()
    try:
        return (ref - stamp).total_seconds() / 60.0
    except TypeError:
        # 一边带时区一边不带 —— 宁可判成不新鲜,也不猜。
        return None


def mirror_is_fresh(raw: Any, *,
                    max_age_minutes: int = MIRROR_MAX_AGE_MINUTES_DEFAULT,
                    now: Optional[datetime] = None) -> bool:
    """镜像新鲜度闸。🔴 fail-closed:时间戳缺失/解析不了/未来时间 一律判不新鲜。"""
    age = sync_age_minutes(raw, now=now)
    if age is None:
        return False
    if age < 0:
        # 时间戳在未来 = 时钟或写入有问题,不能信。
        return False
    return age <= float(max_age_minutes)


def sync_config_key_for(media_type: Any) -> str:
    """这条 item 该看哪个同步时间戳。未知渠道回落到软文那条(最保守的已知值)。"""
    key = (str(media_type or "")).strip()
    return _SYNC_CONFIG_KEY_BY_MEDIA_TYPE.get(key, "last_status_sync")


# ---------------------------------------------------------------------------
# 带 DB 的部分
# ---------------------------------------------------------------------------

def mirror_lookup_is_discriminating(*, sample_size: int = 6) -> bool:
    """判别力自检:拿**已知有外部单号**的条目按 order_sn 回查镜像,必须全部命中。

    命中不了 = 镜像不全或查法失效,这时"僵尸在外部查不到"是假结论,
    整轮比对作废。没有可用样本时同样返回 False(无从证明查法有效)。
    """
    from db.meijiehezi_db import count_mirror_hits_for_known_orders

    try:
        checked, hits = count_mirror_hits_for_known_orders(sample_size=sample_size)
    except Exception as exc:
        logger.error("[orphan] 判别力自检异常,本轮跳过: %s", exc)
        return False
    if checked <= 0:
        logger.warning("[orphan] 判别力自检无可用样本(没有任何带外部单号的条目),本轮跳过")
        return False
    if hits != checked:
        logger.error("[orphan] 判别力自检未通过 %s/%s 命中 —— 镜像疑似不全,本轮不做任何退款",
                     hits, checked)
        return False
    return True


def classify_orphan_item(item: dict[str, Any]) -> str:
    """查镜像 + 三分支判定。查询异常时 fail-closed 转人工(绝不因查不动就退款)。"""
    from db.meijiehezi_db import find_mirror_rows_for_orphan

    try:
        rows = find_mirror_rows_for_orphan(
            article_title=item.get("article_title") or "",
            media_id=item.get("media_id"),
            media_name=item.get("media_name") or "",
        )
    except Exception as exc:
        logger.error("[orphan] item=%s 镜像比对异常 → 转人工(不退款): %s", item.get("id"), exc)
        return ACTION_MANUAL_REVIEW
    return decide_orphan_action(rows)


def sweep_orphan_publish_items(
    *,
    min_age_hours: int = ORPHAN_MIN_AGE_HOURS_DEFAULT,
    mirror_max_age_minutes: int = MIRROR_MAX_AGE_MINUTES_DEFAULT,
    max_batch: int = 50,
    now: Optional[datetime] = None,
) -> OrphanSweepResult:
    """僵尸孤儿单兜底清扫。

    🔴 只碰 ``mhz_order_id`` 为空的条目 —— 有单号的那是真在外部通道跑,
    清了等于凭空退款。这条约束落在 SQL 的 WHERE 里,不靠调用方自觉。
    """
    from db.meijiehezi_db import (
        find_orphan_publish_items, get_config, mark_orphan_needs_manual_review,
        refund_for_publish_order, update_order_item_status,
    )

    result = OrphanSweepResult()

    if not mirror_lookup_is_discriminating():
        result.skipped_reason = "镜像判别力自检未通过"
        return result

    try:
        candidates = find_orphan_publish_items(
            min_age_hours=int(min_age_hours),
            statuses=list(SWEEPABLE_STATUSES),
            limit=int(max_batch),
        )
    except Exception as exc:
        result.errors.append(f"候选查询失败: {exc}")
        logger.error("[orphan] 候选查询失败: %s", exc)
        return result

    if not candidates:
        return result

    # 新鲜度按渠道各查一次,同一渠道多条 item 不重复打 DB。
    _fresh_cache: dict[str, bool] = {}

    for item in candidates:
        result.scanned += 1
        cfg_key = sync_config_key_for(item.get("media_type"))
        if cfg_key not in _fresh_cache:
            try:
                _fresh_cache[cfg_key] = mirror_is_fresh(
                    get_config(cfg_key), max_age_minutes=mirror_max_age_minutes, now=now)
            except Exception as exc:
                logger.error("[orphan] 读同步时间戳 %s 失败 → 判不新鲜: %s", cfg_key, exc)
                _fresh_cache[cfg_key] = False
        if not _fresh_cache[cfg_key]:
            # 🔴 镜像不新鲜时"外部查不到"是假结论 —— 这条跳过,一分不退。
            result.skipped_reason = f"{cfg_key} 同步不新鲜"
            continue

        action = classify_orphan_item(item)
        item_id = int(item["id"])

        if action == ACTION_MANUAL_REVIEW:
            reason = "外部通道查到同名订单但未被任何本地条目认领,需人工核对后处置"
            try:
                if mark_orphan_needs_manual_review(item_id, reason=reason):
                    result.manual_review_items += 1
                    logger.warning(
                        "[orphan] item=%s 转人工(外部有单未被认领 · 禁止自动退款) "
                        "media=%s cost=%s", item_id, item.get("media_name"),
                        item.get("cost_points"))
            except Exception as exc:
                result.errors.append(f"item={item_id} 转人工失败: {exc}")
                logger.error("[orphan] item=%s 转人工失败: %s", item_id, exc)
            continue

        # ---- 退款分支 ----
        # 顺序刻意是「先置终态、再退款」:与既有 `_apply_failed` 同序,
        # 反过来会在"退款成功但状态没落库"的窗口里被下一轮重复捡起。
        try:
            update_order_item_status(
                item_id, "failed",
                reject_reason="未提交到外部通道(超时未取得外部单号),费用已自动退回")
        except Exception as exc:
            result.errors.append(f"item={item_id} 置终态失败: {exc}")
            logger.error("[orphan] item=%s 置终态失败,跳过退款: %s", item_id, exc)
            continue

        cost = int(item.get("cost_points") or 0)
        if cost <= 0:
            continue
        try:
            refund = refund_for_publish_order(
                user_id=int(item["user_id"]),
                amount=cost,
                refund_key=f"item:{item_id}",
                reason="超时未取得外部单号,自动退回",
            )
        except Exception as exc:
            result.errors.append(f"item={item_id} 退款异常: {exc}")
            logger.error("[orphan] item=%s 退款异常: %s", item_id, exc)
            continue
        if refund.get("success") and not refund.get("skipped"):
            result.refunded_items += 1
            result.refunded_points += int(refund.get("refunded") or 0)
        elif refund.get("skipped"):
            logger.info("[orphan] item=%s 未产生退款流水: %s", item_id, refund.get("reason"))
        else:
            result.errors.append(f"item={item_id} 退款失败: {refund.get('reason')}")
            logger.error("[orphan] item=%s 退款失败: %s", item_id, refund.get("reason"))

    if result.scanned:
        logger.info("[orphan] 僵尸清扫: %s", result.summary())
    return result
