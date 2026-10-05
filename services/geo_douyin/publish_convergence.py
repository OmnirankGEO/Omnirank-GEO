"""GEO 图文成品的**发布终态收敛**(#151)。

## 缺陷

`geo_douyin_posts.publish_status` 停在 `publishing` 出不去。
post 24 自 2026-08-10 卡了 29 天(`publish_order_id=491`),
既不算成功也不算失败 —— 客户看不到结果,我们也数不出交付。

### 🔴 机理订正(2026-09-08 · Deploy 只读取证)

我第一版在这里写的是「`publishing` **没有出口**」,依据是
`bind_publish_result` 全仓只有两个调用点、都不写 `published`。
**那个机理是错的**,虽然结论(需要收敛)是对的:

  · Deploy 实证(`DEPLOY_READONLY_POST24_ORDER491_2026-09-08.md`):
    同路 **502 条**已 `published`,中位延迟 133 分钟、最长 6.2 天 ——
    **出口存在**,而且平时在用;
  · 卡住的 22 条(order 483/484/486/491)**全落在 08-10~08-12 那个故障窗**,
    08-13 之后同路 `submitted` 残留为 0;
  · order 491 七条 item 的 `mhz_raw_response` 都是 `success:7`(供应商已接单),
    但 `publish_url` / `published_at` 全 NULL、27 天没人重捞。

⇒ 真相是「**那三天的回写没发生,而且没有重试**」,不是「没有回写这条路」。
  ⚠️ 记这一笔:**结论对而机理错**是最难发现的一种错 ——
     它不会让判据变红,只会让下一个读注释的人去修一个不存在的东西。

### 本模块的定位(据此收窄)

本模块**只**做「item 已到终态 ⇒ 把作品状态收敛过去」。
它对 `submitted` 判在途 ⇒ 上面那 22 条**它收敛不了**,这是**对的**:
`submitted` 意味着供应商接了单但结果未知,不是终态。
把它们捞回来要重探供应商,那是 **#151-B**,不在本模块职责内。

## 收敛依据:mhz 侧的 item 终态

`mhz_publish_order_items` 有 `status` 与 `publish_url`。本模块**只读**它,
按整单 item 的终态推出这条作品的终态:

  · 有任何一条成(`published` / `success`)⇒ 作品 `published`,并取第一条 `publish_url`;
  · 全部是败态(`failed` / `rejected` / `cancelled` / `withdrawn`)⇒ 作品 `failed`;
  · 还有非终态 item(`pending` / `submitting` / `awaiting_confirmation` /
    `awaiting_sync`)⇒ **什么都不做**,它还在途。

## 🔴 超时那一档:**不编造结论**

工单写的是「成功/失败/超时」三档。前两档有数据依据,第三档没有 ——
「卡了 29 天」只说明**我们不知道**它有没有发出去,不等于它失败了。
钱已经花掉,把它宣布成 `failed` 是一个业务判断(要不要退款、对客户怎么说),
不该由一个 sweeper 替 Owner 做。

所以本模块对超时的处置是**让它可见**:记一条 WARNING + 计数,
状态**保持 `publishing` 不动**,并把这一档交回 Review/Owner 定夺。
—— 造一个「看起来收敛了」的假终态,比留着一个诚实的未知更坏。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-Douyin-PublishConverge")

#: item 的**成功**终态。
ITEM_SUCCESS = ("published", "success")
#: item 的**失败**终态。
ITEM_FAILURE = ("failed", "rejected", "cancelled", "withdrawn")
#: 卡多久算「该有人看一眼」。只影响**告警**,不影响状态。
STALE_WARN_HOURS = 72
#: 列表页展示「发布结果待确认」的门槛。
#: 🔴 它是**展示态**,由现有字段派生 —— 不落库、不进 `publish_status` 的取值域。
#:    前端对 publish_status 没有白名单,新加一个裸串会直接上屏(本仓红线)。
PENDING_CONFIRM_DISPLAY_HOURS = 24


def classify_order_items(rows) -> tuple:
    """`(终态判定, publish_url)`。判定 ∈ {published, failed, None}。

    `None` = 还有 item 在途,**不收敛**。

    🔴 判定顺序是「有一条成就算成」而不是「全成才算成」:
       一条作品发到 N 个账号,成一个就是发出去了。
       反过来写会把「三个账号成了一个」判成失败,而那条内容明明在线上。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return None, ""
    states = [str(r.get("status") or "").strip() for r in rows]
    if any(s not in ITEM_SUCCESS + ITEM_FAILURE for s in states):
        return None, ""          # 还有在途的
    for r in rows:
        if str(r.get("status") or "").strip() in ITEM_SUCCESS:
            return "published", str(r.get("publish_url") or "")
    return "failed", ""


def _stuck_posts(cur, limit: int) -> list:
    cur.execute(
        """SELECT p.id, p.publish_order_id, p.updated_at
             FROM geo_douyin_posts p
            WHERE p.publish_status = 'publishing'
              AND p.publish_order_id IS NOT NULL
              AND p.deleted_at IS NULL
              -- 🔴 [#184 d3 · 2026-09-13] **一链一列一写入方**。
              --    新链(逐项冻结 freeze_per_item)的 published_url 由
              --    `contract_worker.backfill_published_urls_from_provider`
              --    按 `items.source_geo_post_id` 从供应商回执投影 ——
              --    它带着本函数**没有**的三道守卫:矛盾回执(镜像说完成而 item
              --    已 failed/已退款)一个字都不写、结算未清等下一轮、只填**空的**
              --    published_url,并且走 canonical writer(订单头重算 / 发布快照 /
              --    交付 lineage / 通知 outbox)。
              --    本函数只 UPDATE 三列。两个写入方落在同一列上,第二个永远会漏掉
              --    第一个后来新增的动作,而且漏得无声(contract_worker :1362 原话)。
              --    d2 开始给新链的作品写 publish_order_id,所以这条排除**必须**有:
              --    没有它,下一小时这里就会绕过那三道守卫去覆盖。
              AND NOT EXISTS (
                    SELECT 1 FROM mhz_publish_order_items i
                     WHERE i.order_id = p.publish_order_id
                       AND i.billing_mode = 'freeze_per_item')
            ORDER BY p.updated_at
            LIMIT %s""",
        (int(limit),),
    )
    return [dict(r) for r in cur.fetchall()]


def _order_items(cur, order_id: int) -> list:
    cur.execute(
        """SELECT status, publish_url
             FROM mhz_publish_order_items
            WHERE order_id = %s""",
        (int(order_id),),
    )
    return [dict(r) for r in cur.fetchall()]


def converge_publishing_posts(limit: int = 200) -> dict:
    """把能判的收敛掉,把判不了的**喊出来**。返回读数供任务日志与判据用。"""
    from db.connection import get_connection
    from db.geo_douyin_db import bind_publish_result

    out = {"scanned": 0, "published": 0, "failed": 0, "in_flight": 0, "stale": 0}
    conn = get_connection()
    try:
        cur = conn.cursor()
        posts = _stuck_posts(cur, limit)
        out["scanned"] = len(posts)
        for post in posts:
            verdict, url = classify_order_items(
                _order_items(cur, post["publish_order_id"]))
            if verdict is None:
                out["in_flight"] += 1
                # 🔴 超时**只告警不改状态**(见模块 docstring):
                #    「卡了很久」说明我们不知道结果,不说明它失败了。
                age = post.get("updated_at")
                if age is not None:
                    import datetime as _dt

                    now = _dt.datetime.now(getattr(age, "tzinfo", None))
                    hours = int((now - age).total_seconds() // 3600)
                    if hours >= STALE_WARN_HOURS:
                        out["stale"] += 1
                        # 🔴 告警要**可操作**:给得出「去查哪一单」的三件套。
                        #    只说「有 N 条卡住了」的告警,收到的人不知道下一步做什么。
                        logger.warning(
                            "[publish-converge] 作品 post_id=%s 停在 publishing 已 %s 小时"
                            "(publish_order_id=%s)—— item 仍非终态,状态**不动**。"
                            "下一步:只读 mhz_publish_order_items 看这一单的真相。",
                            post["id"], hours, post["publish_order_id"])
                continue
            bind_publish_result(int(post["id"]), order_id=None, item_ids=None,
                                publish_status=verdict, published_url=url)
            out[verdict] += 1
            logger.info("[publish-converge] 作品 %s 收敛为 %s", post["id"], verdict)
    finally:
        conn.close()
    if out["stale"]:
        logger.warning("[publish-converge] 本轮有 %s 条长期停在 publishing", out["stale"])
    return out


def pending_confirm_display(post) -> bool:
    """列表页要不要显示「发布结果待确认」。

    🔴 **展示态,不是状态**:由 `publish_status` + `updated_at` 现场派生,
       既不落库也不进 `publish_status` 的取值域 ——
       前端对 publish_status 没有白名单,新加一个裸串会直接上屏。

    条件:仍在 `publishing`,且已经超过 `PENDING_CONFIRM_DISPLAY_HOURS`。
    """
    import datetime as _dt

    if not isinstance(post, dict):
        return False
    if str(post.get("publish_status") or "").strip() != "publishing":
        return False
    ts = post.get("updated_at")
    if ts is None:
        return False
    now = _dt.datetime.now(getattr(ts, "tzinfo", None))
    return (now - ts).total_seconds() >= PENDING_CONFIRM_DISPLAY_HOURS * 3600
