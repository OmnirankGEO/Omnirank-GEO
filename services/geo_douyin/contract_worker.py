"""图文合同链的**生产启动者**(返工 2026-08-18 · 链 3 · Codex P0-03/04/06)。

## 这个模块存在的理由,一句话

`durable_worker.py` 里的租约 / CAS / 回收原语**全是真的**,但仓里
**没有任何一个生产调用者**会去调它们 —— 于是:

  · 制作任务插进去就是 `queued`,永远没人领,永远不会变;
  · 素材准备 claim 完就返回 `preparing`,永远没人上传;
  · 发布 item 建成 `queued`,而现役提交器只认 `pending`,永远不会被提交;
  · 三条链的钱**只 freeze、没有 commit / release** —— §8.2 的资金闭环
    从来没有闭上过。

「模块层真实 + 执行链没接通」的标本。本模块就是那三个缺失的调用者。

## 三条链,一个形状

```
claim(租约 + CAS)  ──►  external_start 留痕  ──►  真外调  ──►  终态 + 结算
      │                                                        │
      └──────────────── 崩溃 ───► reconciler 按 external_started_at 分流 ──┘
```

🔴 **顺序不能换**:`mark_external_start` 必须在真外调**之前**落库并提交。
   反过来(先调再记)时,"调用发出后、记录落库前"崩溃的那一瞬,
   DB 里看起来像"从没开始过" ⇒ reconciler 会重投 ⇒ 重复外调 + 重复扣费。
   这是整套租约机制唯一真正要防的事,别的都是附带。

## 资金闭环(§8.2)在这里怎么闭

入口(API)只做到 `freeze`,句柄整组落在 `geo_douyin_post_tasks` /
`mhz_publish_order_items` 上。worker 消费**那一组**句柄:

  · 成功 → `settlement_pending` → `commit_freeze` → `committed`;
  · 失败 → `release_freeze` → `released`;
  · **结果未知** → 不 commit 也不 release,置 `manual` 转人工。

🔴 第三条是刻意的。release 意味着"确定没发生",而结果未知时我们恰恰不确定。
   把未知当失败退款,渠道那边真发了的话就是白送。

## 单进程语义

本仓生产是 `WORKERS=1`(见 06_DEPLOYMENT)。即便如此,claim 仍走
`FOR UPDATE SKIP LOCKED` + 原子 UPDATE —— 因为蓝绿切换期间**会有两个实例
同时在线**,那时候"只有一个 worker"这个假设不成立。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import os
from typing import Any, Optional

from services.geo_douyin.contract_seams import (
    ARTICLE_TYPE_IMAGE_NOTE,
    LEGACY_SUBMITTER_CLAIM_STATUS,
    LEGACY_SUBMITTER_TERMINAL_STATUSES,
    SUBMIT_DELIVERED,
    SUBMIT_NOT_CLAIMED,
    classify_production_tail_identity,
    classify_submit_result,
    classify_unsettled_publish_item,
    resolve_production_terminal,
    settlement_actually_happened,
)
from services.geo_douyin.pipeline_gate import pipeline_gate_open
from services.geo_douyin.contract_states import (
    SETTLEMENT_COMMITTED,
    SETTLEMENT_MANUAL,
    SETTLEMENT_RELEASED,
    TASK_STATUS_READY,
)
from services.geo_douyin.durable_worker import (
    DEFAULT_LEASE_SECONDS,
    SCOPE_CONTRACT,
    LeaseLost,
    claim_next_task,
    finish_task,
    mark_external_start,
)

logger = logging.getLogger("GEO-ImageNote-Worker")

#: 一轮最多处理几件。不是限流,是**让出**:一轮跑完就回到调度器,
#: 让 reconciler / 其它任务也有机会跑,而不是一个 worker 霸住整个循环。
DEFAULT_BATCH_PER_TICK = 5


def worker_id() -> str:
    """worker 身份 = 主机 + 进程。蓝绿双实例时两边天然不同名,
    租约比对(`lease_owner = %(worker)s`)因此真的能区分出"是不是我"。"""
    return f"{socket.gethostname()}:{os.getpid()}"


# 🔴 [WO-A ② · 2026-08-20] ``raise_wiring_alert`` 已**搬**到
#    ``services/ai_ops_alerts``:小榜索引重建要用同一条告警产线,不该为了一条告警
#    把整条图文链 import 进去。这里 re-export 保持既有调用方(``api/geo_douyin_api.py``)
#    逐字不变 —— 抄第二份实现的话,两份必然各自漂移。
from services.ai_ops_alerts import raise_wiring_alert  # noqa: E402,F401


# ─────────────────────────────────────────────────────────────
# 链 A · 制作生成
# ─────────────────────────────────────────────────────────────

def _load_production_context(cur, *, task_id: int) -> Optional[dict[str, Any]]:
    """把跑一篇所需要的东西一次读齐(post + batch + 槽位主题)。

    🔴 一次读齐而不是分三次:分开读会在两次读之间被别的事务改掉,
       于是同一次生产的"用哪个品牌"和"用哪个关键词"可能来自两个瞬间。
    """
    cur.execute(
        """
        SELECT t.id AS task_id, t.task_ref, t.freeze_id, t.freeze_table,
               t.reserved_amount, t.settlement_authority, t.payer_user_id,
               t.production_batch_id, t.batch_item_request_id, t.progress_total,
               p.id AS post_id, p.brand_id, p.keyword, p.topic_ref,
               p.delivery_slot_key, p.quote_id, p.actor_user_id,
               -- 🔴 [第 3 棒 · P0-3] 成品版本的 CAS 谓词比的是 **post 上**的
               --    代际(`activate_revision` 的 WHERE),不是 task 行上那一列
               --    (task.generation_epoch 由 034 建列时默认 0,从没人写过它)。
               --    取错这一列的后果不是报错,是 CAS **恒零行** ⇒ 每一次
               --    activate 都抛 GenerationSuperseded ⇒ 成品版本永远建不出来。
               p.generation_epoch AS post_epoch, p.active_generation_task_id,
               p.topic_snapshot, p.style_key, p.aspect_ratio, p.contact_enabled,
               b.name AS brand_name,
               s.projection_version AS slot_version,
               s.contract_revision_id
          FROM geo_douyin_post_tasks t
          JOIN geo_douyin_posts p ON p.id = t.post_id
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN geo_article_delivery_slots s
                 ON s.delivery_slot_key = p.delivery_slot_key
         WHERE t.id = %(task_id)s
        """,
        {"task_id": int(task_id)},
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


async def run_production_once(*, worker: Optional[str] = None,
                              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Optional[dict]:
    """领**一个**合同链制作任务并跑完。没有可领的返回 `None`。

    🔴 领取与真外调分属两个事务:claim 提交之后才开始生成。
       同一个事务里干完会把整个生成时长(实测 p95 3-5 分钟)变成一个长事务,
       它会挡住 prestart 迁移要的 ACCESS EXCLUSIVE —— 本仓 2026-08-10
       正是这样把生产打成 503 十六分钟。
    """
    from db.connection import get_connection
    from services.geo_douyin.production_task import run_image_post_production

    me = worker or worker_id()
    conn = get_connection()
    try:
        cur = conn.cursor()
        task = claim_next_task(cur, worker=me, lease_seconds=lease_seconds,
                               scope=SCOPE_CONTRACT)
        if task is None:
            conn.rollback()
            return None
        ctx = _load_production_context(cur, task_id=int(task["id"]))
        if ctx is None:
            # 任务行在 claim 与读取之间没了(理论上不可能,post 有 FK)。
            # 不静默跳过 —— 让它响亮,否则一个永远读不到的任务会被无限重领。
            conn.rollback()
            raise RuntimeError(f"task={task['id']} 领到了但读不出上下文")
        # 🔴 external-start 与 claim 同一个事务提交:生成会真的调 LLM 与出图,
        #    那是外部副作用,必须在"发出去之前"就已经落库。
        mark_external_start(cur, task_id=int(task["id"]), worker=me)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # 🔴 [第 4 棒 · Codex R3 P0-B] 传**整组持久句柄**,不是只传 freeze_id + task_ref。
    #
    #    034 早就把整组落库了(`freeze_table` / `payer_user_id` / `reserved_amount` /
    #    `physical_split_snapshot`),`_load_production_context` 也一直读得出来 ——
    #    丢是丢在这一格:这个 dict 只挑了四个键,于是下游 `commit_freeze` /
    #    `release_freeze` 只能去**猜**冻结在哪张表。而两张冻结表的 `freeze_id`
    #    是各自独立自增的,**撞号必然发生**:猜错就是结算到别人那一笔
    #    (`middleware/billing.py:_route_freeze_table` 的消歧注释写着这件事)。
    #
    #    发布链在第 3 棒已经修对了(见 `_settle_publish_item`),制作链原样留着 ——
    #    「一个病两条链只修一条」。这里补上另一条。
    contract_task = {
        "task_id": int(ctx["task_id"]),
        "task_ref": str(ctx["task_ref"]),
        "freeze_id": ctx.get("freeze_id"),
        "freeze_table": ctx.get("freeze_table"),
        "payer_user_id": ctx.get("payer_user_id"),
        "reserved_amount": ctx.get("reserved_amount"),
        "settlement_authority": ctx.get("settlement_authority"),
        "status_ready": TASK_STATUS_READY,
    }
    outcome = await run_image_post_production(
        post_id=int(ctx["post_id"]),
        user_id=int(ctx.get("payer_user_id") or ctx.get("actor_user_id") or 0),
        keyword=str(ctx.get("keyword") or ctx.get("topic_ref") or ""),
        brand_id=ctx.get("brand_id"),
        brand_name=str(ctx.get("brand_name") or ""),
        card_count=int(ctx.get("progress_total") or 0) or 4,
        contract_task=contract_task,
    )

    # 🔴 [第 3 棒 · Codex R2 P0-2] 原来这里是
    #        `status = TASK_STATUS_READY if outcome.ok else TASK_STATUS_FAILED`
    #    —— 一条**布尔中继**。而 `ProductionOutcome` 有两个维度:`ok` 与
    #    `completing`(部分交付:有成品、**缺图**、冻结**未**收敛)。
    #    部分交付那一格 `ok=True`,于是被提升成 `ready`:
    #    批次显示完成、资金视图显示已结算、槽位推进到 ready 计入合同 ——
    #    三个读侧同时说谎。终态提升必须**逐值**落在封闭状态集上。
    terminal = resolve_production_terminal(
        ok=bool(outcome.ok), completing=bool(getattr(outcome, "completing", False)),
        # 🔴 [第 4 棒 · P0-B] 第三维:资金**真的**结清了没有。
        #    默认 True ⇒ 没有这一维的老调用路径行为不变。
        settlement_ok=bool(getattr(outcome, "settlement_ok", True)))
    status = terminal.status
    conn = get_connection()
    revision_id = None
    try:
        cur = conn.cursor()
        try:
            finish_task(cur, task_id=int(ctx["task_id"]), worker=me, status=status)
        except LeaseLost:
            # 租约被接管 ⇒ 别人已经在处理这一条。**立刻停手**,不覆盖终态。
            conn.rollback()
            logger.warning("[imgnote-worker] task=%s 租约已易主,放弃写终态",
                           ctx["task_id"])
            return {"task_id": int(ctx["task_id"]), "status": "lease_lost"}
        if terminal.reason:
            # 非 ready 的终态必须带原因:界面上"待处理"而没有原因,
            # 与"卡住了"在用户眼里是同一件事。
            cur.execute(
                "UPDATE geo_douyin_post_tasks"
                "   SET error_msg = COALESCE(NULLIF(error_msg, ''), %s)"
                " WHERE id = %s", (terminal.reason[:400], int(ctx["task_id"])))
        if status == TASK_STATUS_READY:
            # 🔴 [第 3 棒 · Codex R2 P0-3] 成品版本在这里**才**产生。
            #    上一轮 `stage_revision` / `activate_revision` 零生产调用者 ⇒
            #    `posts.active_revision_id` 恒 NULL ⇒ 素材准备入口
            #    (它硬要求 active_revision_id)对**每一篇新生产的作品**都 409。
            #    整条「生产 → 素材 → 发布」在这一格断开。
            revision_id = _freeze_active_revision(cur, ctx)
        if terminal.advance_slot_to_ready and ctx.get("delivery_slot_key") \
                and ctx.get("slot_version"):
            _advance_slot_to_ready(cur, ctx)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"task_id": int(ctx["task_id"]), "status": status,
            "post_id": int(ctx["post_id"]), "ok": bool(outcome.ok),
            "completing": bool(getattr(outcome, "completing", False)),
            "post_revision_id": revision_id}


#: [#184 d1] 冻结逻辑已搬进 `post_revisions`(普通链与合同链共用同一份)。
#: 这里保留原名别名 —— :248 / :1222 两个调用点与其判据逐字不变。
from services.geo_douyin.post_revisions import (  # noqa: E402
    freeze_active_revision as _freeze_active_revision,
)




def _advance_slot_to_ready(cur, ctx: dict) -> None:
    """生成成功 ⇒ 槽位推进到 `ready`(事件 + CAS,与入口 claim 同一个写入者)。

    🔴 CAS 失败**不抛**:槽位可能已被 release / 被新一版合同 supersede。
       那不是本次生产的错,生产结果照样有效。硬抛会把"作品做好了"回滚掉。
    """
    from services.geo_douyin.delivery_slots import (
        FULFILLMENT_READY, SlotClaimConflict, transition,
    )
    try:
        transition(cur, slot_key=str(ctx["delivery_slot_key"]),
                   expected_version=int(ctx["slot_version"]),
                   next_state=FULFILLMENT_READY,
                   identity={"tenant_owner_user_id": int(ctx["payer_user_id"]),
                             "actor_user_id": int(ctx.get("actor_user_id") or 0)},
                   geo_post_id=int(ctx["post_id"]))
    except SlotClaimConflict as exc:
        logger.info("[imgnote-worker] 槽位 %s 已被别的操作推进,跳过 ready:%s",
                    ctx["delivery_slot_key"], exc)


# ─────────────────────────────────────────────────────────────
# 链 B · 发布素材准备
# ─────────────────────────────────────────────────────────────

_CLAIM_ARTIFACT = """
UPDATE geo_douyin_publish_artifacts
   SET lease_owner = %(worker)s,
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval,
       heartbeat_at = now(),
       updated_at = now()
 WHERE prepared_artifact_id = (
     SELECT a.prepared_artifact_id FROM geo_douyin_publish_artifacts a
      WHERE a.state = 'preparing'
        AND (a.lease_owner IS NULL
             OR (a.lease_expires_at IS NOT NULL AND a.lease_expires_at < now()
                 AND a.external_started_at IS NULL))
      ORDER BY a.created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
 )
RETURNING prepared_artifact_id, geo_post_id, post_revision_id, tenant_owner_user_id
"""


async def run_artifact_prepare_once(*, worker: Optional[str] = None,
                                    lease_seconds: int = DEFAULT_LEASE_SECONDS
                                    ) -> Optional[dict]:
    """领**一个** `preparing` 的素材并真的把图准备好。

    🔴 三种收尾必须分开(规格 §7.1):
       `ready` / `failed`(零外部残留,可重试)/ `unknown`(远端可能已收,不自动重传)。
       合并 failed 与 unknown 会让恢复逻辑对"远端可能已经收了"的情形也重传。
    """
    from db.connection import get_connection
    from services.geo_douyin.artifact_prepare import mark_failed, mark_ready, mark_unknown
    from services.geo_douyin.post_revisions import compute_manifest_hash
    from services.geo_douyin.publish_adapter import prepare_publish_images

    me = worker or worker_id()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_CLAIM_ARTIFACT, {"worker": me, "lease_seconds": int(lease_seconds)})
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        art = dict(row)
        # 🔴 [第 3 棒 · Codex R2 P0-3] 图从**冻结的那一版**取,不从 post 的
        #    可变列取。artifact 是绑定在 `post_revision_id` 上的(§11.3),
        #    而 `posts.oss_keys` 会被下一次重做/重抽整列覆盖 —— 从它取图
        #    等于"确认的是 A 版、上传的是 B 版",而且**没有任何一步会报错**。
        cur.execute(
            "SELECT asset_manifest FROM geo_douyin_post_revisions"
            " WHERE post_revision_id = %s AND geo_post_id = %s",
            (int(art["post_revision_id"]), int(art["geo_post_id"])))
        rev = dict(cur.fetchone() or {})
        cur.execute(
            "UPDATE geo_douyin_publish_artifacts"
            "   SET external_started_at = COALESCE(external_started_at, now())"
            " WHERE prepared_artifact_id = %s AND lease_owner = %s",
            (int(art["prepared_artifact_id"]), me))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    manifest = dict(rev.get("asset_manifest") or {})
    oss_keys = [str(k) for k in (manifest.get("oss_keys") or []) if k]

    state, manifest_hash, card_statuses = "failed", None, []
    if not oss_keys:
        # 作品还没有出图 —— 明确失败、零外部残留,可以原 key 重试。
        card_statuses = [{"index": 0, "state": "failed", "reason": "NO_CARDS"}]
    else:
        try:
            prepared = await prepare_publish_images(oss_keys)
            urls = list(getattr(prepared, "image_urls", None) or [])
            if getattr(prepared, "ok", False) and len(urls) == len(oss_keys):
                state = "ready"
                manifest_hash = compute_manifest_hash(
                    {"oss_keys": oss_keys, "image_urls": urls})
                card_statuses = [{"index": i, "state": "ready", "url": u}
                                 for i, u in enumerate(urls)]
            else:
                # 🔴 `prepare_publish_images` 是 fail-closed:任何一张失败就整体
                #    返回 ok=False。但**已经上传成功的那几张在渠道侧是真实存在的**
                #    (它逐张上传,失败时不回收前面的)。所以这里是 `unknown`
                #    而不是 `failed` —— 自动重传会造出重复素材。
                #    只有一张都没传成功时才是干净的失败。
                state = "failed" if not urls else "unknown"
                card_statuses = [{"index": i, "state": "ready", "url": u}
                                 for i, u in enumerate(urls)]
                card_statuses.append({"index": len(urls), "state": state,
                                      "reason": str(getattr(prepared, "error", ""))[:160]})
        except Exception as exc:  # noqa: BLE001
            # 网络类异常:远端**可能**已经收下了。按 unknown 处置,转人工。
            state = "unknown"
            card_statuses = [{"index": 0, "state": "unknown",
                              "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}]
            logger.warning("[imgnote-worker] 素材准备外调异常 artifact=%s: %s",
                           art["prepared_artifact_id"], exc)

    conn = get_connection()
    try:
        cur = conn.cursor()
        aid = int(art["prepared_artifact_id"])
        if state == "ready":
            mark_ready(cur, artifact_id=aid, manifest_hash=manifest_hash,
                       card_statuses=card_statuses)
        elif state == "unknown":
            mark_unknown(cur, artifact_id=aid, card_statuses=card_statuses)
        else:
            mark_failed(cur, artifact_id=aid, card_statuses=card_statuses)
        cur.execute("UPDATE geo_douyin_publish_artifacts SET lease_owner = NULL,"
                    " lease_expires_at = NULL WHERE prepared_artifact_id = %s", (aid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"prepared_artifact_id": int(art["prepared_artifact_id"]), "state": state}


# ─────────────────────────────────────────────────────────────
# 链 C · 发布提交(进真实供应商链)
# ─────────────────────────────────────────────────────────────

#: 🔴 [第 3 棒 · Codex R2 P0-1] claim 写的是 `LEGACY_SUBMITTER_CLAIM_STATUS`
#:    (= `'pending'`),**不是** `'submitting'`。
#:
#:    上一版写 `'submitting'` —— 那恰好是被调方 `try_lock_for_submit` 自己
#:    CAS 上去的**下一态**,所以它看起来非常像"在途中"这个语义。但
#:    `_submit_short_video_order` 的领取谓词是 `oi["status"] == "pending"`
#:    (`api/meijiehezi_api.py:2199`),于是它 `locked` 恒空、恒返
#:    `{'submitted': 0}` ⇒ 新链的每一次发布都是**保证空转**,
#:    而且空转被 `ok = submitted > 0` 读成"失败"⇒ 逐项 release + 标 failed。
#:
#:    判据因此必须打**被调方真的领到活**(submitted > 0),
#:    而不是"调用发生了"——后者在整条链断开的情况下也是绿的。
_CLAIM_PUBLISH_ITEM = """
UPDATE mhz_publish_order_items
   SET status = %(claim_status)s,
       lease_owner = %(worker)s,
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval,
       -- 🔴 `mhz_publish_order_items` **没有 `updated_at` 列**(只有 created_at /
       --    last_submit_at)。基线版三条 SQL 全带了它 ⇒ 每一次 claim 都
       --    UndefinedColumn ⇒ 发布链在**第一条语句**就死,连"空转"都到不了。
       --    `run_tick` 逐件 try 把异常吃成一行日志,于是从外面看只是"没动静"。
       last_submit_at = now()
 WHERE id = (
     SELECT i.id FROM mhz_publish_order_items i
      WHERE i.status = 'queued'
        AND i.billing_mode = 'freeze_per_item'
        -- 租约:崩溃后可回收,但**已经外调过**的永远不重领(重复外调 + 重复扣费)
        AND (i.lease_owner IS NULL
             OR (i.lease_expires_at IS NOT NULL AND i.lease_expires_at < now()
                 AND i.external_started_at IS NULL))
      ORDER BY i.created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
 )
RETURNING id, order_id, user_id, media_id, cost_points, freeze_id, freeze_table,
          payer_user_id, reserved_amount, settlement_authority, task_ref,
          source_geo_post_id, source_post_revision_id, prepared_artifact_id,
          manifest_hash, capacity_date, media_type
"""

#: 外发内容取自**冻结的成品版本 + 已准备的素材**,不取 post 可变列、
#: 更不取客户端自述(§7.3:「不接受客户端权威 brand/title/body/images/price」)。
_LOAD_OUTBOUND = """
SELECT r.post_revision_id, r.title, r.body, r.hashtags, r.manifest_hash AS rev_manifest_hash,
       r.status AS revision_status,
       a.prepared_artifact_id, a.geo_post_id AS artifact_post_id,
       a.post_revision_id AS artifact_revision_id, a.state AS artifact_state,
       a.manifest_hash AS artifact_manifest_hash, a.card_statuses,
       p.id AS post_id, p.keyword, p.brand_id, p.contact_enabled,
       b.name AS brand_name
  FROM geo_douyin_publish_artifacts a
  JOIN geo_douyin_posts p ON p.id = a.geo_post_id
  LEFT JOIN geo_douyin_post_revisions r
         ON r.post_revision_id = a.post_revision_id AND r.geo_post_id = a.geo_post_id
  LEFT JOIN brands b ON b.id = p.brand_id
 WHERE a.prepared_artifact_id = %(artifact_id)s
"""


class OutboundIdentityMismatch(RuntimeError):
    """要发的那一份与订单项冻结的身份对不上。**零外调**。

    这不是"稍后重试"类错误:身份对不上意味着「审的是 A、要发的是 B」,
    重试一次只会再对不上一次。所以它走确定失败(release + 释放容量),
    而不是 unknown。
    """


def _assert_outbound_identity(item: dict, row: dict) -> list[str]:
    """逐格核对 artifact / revision / item 三方身份,返回可外发的图片地址。

    🔴 [Codex R2 P0-3] 上一版**只**比 revision 与 manifest,不比 `geo_post_id` ——
       于是同租户内可以拼出「B 的 post + A 的 artifact」:两条都通过校验,
       因为校验从没问过"这份素材是哪一篇的"。
    """
    if int(row.get("artifact_post_id") or 0) != int(item.get("source_geo_post_id") or 0):
        raise OutboundIdentityMismatch(
            "发布素材属于作品 %s,订单项记的是作品 %s"
            % (row.get("artifact_post_id"), item.get("source_geo_post_id")))
    if int(row.get("artifact_revision_id") or 0) != int(
            item.get("source_post_revision_id") or 0):
        raise OutboundIdentityMismatch(
            "发布素材对应的是作品版本 %s,订单项记的是 %s"
            % (row.get("artifact_revision_id"), item.get("source_post_revision_id")))
    if str(row.get("artifact_state") or "") != "ready":
        raise OutboundIdentityMismatch(
            "发布素材当前状态 %r,不能外发" % (row.get("artifact_state"),))
    # bpchar(64) 会补空格,两边都 strip(与 artifact_prepare 同一条理由)
    if str(row.get("artifact_manifest_hash") or "").strip() != str(
            item.get("manifest_hash") or "").strip():
        raise OutboundIdentityMismatch("图片清单已变化,与下单时确认的不一致")
    if str(row.get("revision_status") or "") != "active":
        raise OutboundIdentityMismatch(
            "作品版本 %s 已不是当前有效版本(%r)"
            % (row.get("artifact_revision_id"), row.get("revision_status")))
    urls = [str(c.get("url") or "") for c in (row.get("card_statuses") or [])
            if str(c.get("state") or "") == "ready" and c.get("url")]
    if not urls:
        raise OutboundIdentityMismatch("这份发布素材里没有可外发的图片")
    return urls


async def run_publish_submit_once(*, worker: Optional[str] = None,
                                  lease_seconds: int = DEFAULT_LEASE_SECONDS
                                  ) -> Optional[dict]:
    """把**一个** queued 的合同链发布项真正提交给供应商,并结算它的冻结。

    🔴 [P0-05] 走的是现役 `_submit_short_video_order` —— 不是第二条投放链。
       新链只负责"什么时候提交"和"钱怎么结",**下单动作复用既有那一套**。

    🔴 [第 3 棒 · P0-1/P0-3/P0-4/P0-6] 这一趟里四件事同时收口:
       ① claim 写被调方真正的领取谓词(否则保证空转);
       ② 外发内容取自**冻结的 revision + 已准备的 artifact**,并逐格核对身份;
       ③ 外发前(external-start 之前)冻结提交快照 + 再过一次广告法闸 ——
          **审的就是要发的那一份**;
       ④ 结算消费**持久化的整组句柄**,并与 item 终态**同一事务**提交。
    """
    from db.connection import get_connection
    from db.meijiehezi_db import set_item_submission_snapshot
    from services.geo_douyin.legal_gate import LegalGateBlocked, assert_outbound_clean

    me = worker or worker_id()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_CLAIM_PUBLISH_ITEM, {
            "claim_status": LEGACY_SUBMITTER_CLAIM_STATUS,
            "worker": me, "lease_seconds": int(lease_seconds)})
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        item = dict(row)
        cur.execute(_LOAD_OUTBOUND,
                    {"artifact_id": int(item["prepared_artifact_id"] or 0)})
        outbound = dict(cur.fetchone() or {})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ── 身份 + 外发闸(全部在 external-start 之前;命中即零外调)────────
    blocked_reason = ""
    urls: list[str] = []
    legal_version = ""
    try:
        urls = _assert_outbound_identity(item, outbound)
        legal = assert_outbound_clean({
            "title": str(outbound.get("title") or ""),
            "body_text": str(outbound.get("body") or ""),
        })
        legal_version = str(legal.get("pack_version") or "")
    except OutboundIdentityMismatch as exc:
        blocked_reason = str(exc)
    except LegalGateBlocked as exc:
        blocked_reason = ("文案里有广告法明令禁止的说法(命中 %d 处,目录版本 %s)"
                          % (len(exc.hits), exc.pack_version))
    if blocked_reason:
        # 确定失败、**零外部效果**:release + 释放容量,item 标 failed。
        logger.error("[imgnote-worker] item=%s 外发前被拦下:%s", item["id"], blocked_reason)
        return await _settle_publish_item(
            item, classify_submit_result({"submitted": 0, "failed": 1,
                                          "reason": blocked_reason}),
            item_status_override="failed", error=blocked_reason)

    title = str(outbound.get("title") or "")
    body = str(outbound.get("body") or "")

    # ── external-start 之前:冻结提交快照(§8.3)+ 落外调标记 ────────────
    await asyncio.to_thread(
        set_item_submission_snapshot, [int(item["id"])], title=title, content=body,
        source="geo_image_note_contract", legal_catalog_version=legal_version)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE mhz_publish_order_items"
            "   SET external_started_at = COALESCE(external_started_at, now())"
            " WHERE id = %s AND lease_owner = %s RETURNING id",
            (int(item["id"]), me))
        if cur.fetchone() is None:
            # 租约已易主 ⇒ 别人在处理这一条。**立刻停手**,一个字节都不外发。
            conn.rollback()
            logger.warning("[imgnote-worker] item=%s 租约已易主,放弃提交", item["id"])
            return {"order_item_id": int(item["id"]), "kind": "lease_lost"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    from api.meijiehezi_api import _submit_short_video_order

    result, exc_seen = None, None
    try:
        result = await _submit_short_video_order(
            int(item["user_id"]), int(item["order_id"]), title, body,
            str(outbound.get("keyword") or ""), "", "",
            str(outbound.get("brand_name") or ""), "",
            # 🔴 图文笔记的权威类型是 **3**,且 `video_url` 必须为空、
            #    `image_urls` 必填(见 ShortVideoPublishRequest 的字段注释)。
            #    上一版写死 `article_type=1 + image_urls=""` —— 即便领取谓词
            #    修好了,发出去的也是一条**没有视频地址的视频直发单**。
            article_type=ARTICLE_TYPE_IMAGE_NOTE, image_urls=",".join(urls),
            # 🔴 [第 5 棒 · Codex R5 P0-1] 兜底异常必须**原样抛回来**。
            #    旧提交器默认把它吞成 `{submitted:0, failed:n}` ⇒ 本链读成
            #    `rejected` ⇒ release + 释放容量。而 POST 之后的超时是
            #    **结果未知**:供应商可能已经接单,退款就是白送一次投放。
            #    拿到异常本体之后 `classify_submit_result(exception=...)`
            #    会落 `unknown`(保持 frozen + 转人工),这才是 §7.5 的口径。
            raise_on_ambiguity=True)
    except Exception as exc:  # noqa: BLE001
        exc_seen = exc
        logger.warning("[imgnote-worker] 发布提交异常 item=%s: %s", item["id"], exc)

    outcome = classify_submit_result(result, exception=exc_seen)
    if outcome.kind == SUBMIT_NOT_CLAIMED:
        # 接线断裂:被调方一条都没领到。**钱按"确定零外调"退回**(见
        # `classify_submit_result` 的 P1-C 说明),而"链断了"这件事由
        # 我们自己的告警面承载 —— 日志 + `ai_ops_alerts` 各一份。
        logger.error("[imgnote-worker] item=%s %s", item["id"], outcome.reason)
        raise_wiring_alert(
            rule_key="geo_imgnote_publish_seam_broken",
            fingerprint="publish_submit_claim_predicate",
            title="图文发布提交:被调方未领到任务(接线断裂)",
            detail=("发布提交器没有领到 item=%s —— 领取谓词不匹配。"
                    "已按『确定零外调』退回算力并置 needs_action;"
                    "在修好接线之前,这条链发不出去任何东西。" % item["id"]),
            payload={"order_item_id": int(item["id"]),
                     "seam": "publish_submit",
                     "expected_claim_status": LEGACY_SUBMITTER_CLAIM_STATUS,
                     "order_id": item.get("order_id"),
                     "reason": outcome.reason})
    return await _settle_publish_item(item, outcome,
                                      item_status_override=outcome.item_status_override,
                                      error=outcome.reason)


async def _settle_publish_item(item: dict, outcome, *,
                               item_status_override: Optional[str] = None,
                               error: str = "") -> dict:
    """消费**持久化的整组句柄**结算,并与 item 终态**同一事务**落库。

    🔴 [Codex R2 P0-4] 上一版四处失真,每一处都能让"库说结了、钱没动":

      · `task_ref` 临时重造成 `imgnote:pub:{item_id}` —— 与冻结时用的
        `imgnote:{command_request_id}:{item_key}` **不是同一个值**;
      · 不传 `payer_user_id` / `freeze_table` —— `_route_freeze_table` 只好去猜表,
        两张冻结表的相同数字 id 撞号时会结算到**别人那一笔**;
      · **不看返回值** —— `commit_freeze` 找不到冻结或跨表歧义时返回
        `{"success": False}` **不抛**,于是异常处理一次都不会触发;
      · 资金与 item 终态分属两个事务 —— 中间崩溃就写成 committed 而钱没动。

    四处一起修:整组句柄原样传、返回值逐格判、`_cursor` 借调用方事务。
    """
    from db.connection import get_connection
    from middleware.billing import commit_freeze, release_freeze

    settlement = outcome.settlement
    settlement_intent = outcome.settlement      # 原意图:下面 settlement 可能被改写成 manual
    capacity_state = outcome.capacity_state
    reason = error or outcome.reason
    conn = get_connection()
    try:
        cur = conn.cursor()
        funds = None
        if item.get("freeze_id") and settlement in ("committed", "released"):
            kwargs = {
                "freeze_id": int(item["freeze_id"]),
                # 🔴 冻结时落库的那一个,不是现拼的
                "task_ref": str(item.get("task_ref") or "") or None,
                "user_id": int(item["payer_user_id"]) if item.get("payer_user_id") else None,
                "freeze_table": str(item.get("freeze_table") or "") or None,
                "_cursor": cur,
            }
            if settlement == "committed":
                funds = await commit_freeze(reason="GEO 图文发布已提交", **kwargs)
            else:
                funds = await release_freeze(reason="GEO 图文发布未接单退回", **kwargs)
        # 🔴 [第 8 棒 · R8 P0] 判的是**钱有没有按这个方向真的动**,不是"调用返回 success"。
        #    `commit_freeze` 对一笔**已经被退掉**的冻结会返 `success=True, idempotent=True`
        #    —— 只看 success 就会把 item 写成 committed,而用户已经拿了退款。
        funds_ok, funds_reason = settlement_actually_happened(funds, intent=settlement)
        if not funds_ok:
            settlement = SETTLEMENT_MANUAL
            capacity_state = "reserved"
            reason = ("算力结算未完成(%s),需人工核对" % funds_reason)[:400]
            item_status_override = "needs_action"
            logger.error("[imgnote-worker] item=%s 结算未落实:%s | 返回=%s",
                         item["id"], funds_reason, funds)
            raise_wiring_alert(
                rule_key="geo_imgnote_settlement_not_actually_applied",
                fingerprint="publish_item:%s" % int(item["id"]),
                severity="critical",
                title="图文发布:结算调用成功但钱没按这个方向动(待人工核对)",
                detail=("item=%s 要写 `%s`,而资金侧的回答是:%s。"
                        "已按 manual + needs_action 落库,**没有**写成已结算。"
                        % (item["id"], settlement_intent, funds_reason)),
                payload={"order_item_id": int(item["id"]),
                         "intent": settlement_intent,
                         "funds": {k: funds.get(k) for k in
                                   ("success", "idempotent", "status", "reason")}
                                  if isinstance(funds, dict) else None})

        # 🔴 [第 8 棒 · R8 P0] 结算写入加 **CAS**:只有还没被判定过的项才允许写终态。
        #    没有 CAS 时,任何抢先写过终态的人(老 sweeper / 收敛器 / 人工)
        #    都会被我们无声覆盖掉 —— 而覆盖的方向恰好是"更乐观的那个"。
        cur.execute(
            "UPDATE mhz_publish_order_items"
            "   SET settlement_status = %(settlement)s,"
            "       capacity_state = %(capacity)s,"
            "       status = COALESCE(%(status_override)s, status),"
            "       reject_reason = COALESCE(NULLIF(%(reason)s, ''), reject_reason),"
            "       lease_owner = NULL, lease_expires_at = NULL"
            " WHERE id = %(item_id)s"
            "   AND settlement_status = ANY(%(from_states)s)"
            " RETURNING id",
            {"settlement": settlement, "capacity": capacity_state,
             "status_override": item_status_override, "reason": reason[:200],
             "item_id": int(item["id"]),
             "from_states": list(SETTLEMENT_NOT_YET_DECIDED)})
        if cur.fetchone() is None:
            # 结算已被别人判定过 ⇒ **不覆盖**,交人工。这一格是真会发生的:
            # 老 sweeper 抢在 worker 前面标 failed 就是其中一条路径。
            conn.rollback()
            logger.error("[imgnote-worker] item=%s 的结算已被别人判定过,放弃覆盖",
                         item["id"])
            raise_wiring_alert(
                rule_key="geo_imgnote_settlement_already_decided",
                fingerprint="publish_item:%s" % int(item["id"]),
                severity="critical",
                title="图文发布:结算已被别的写入方判定,worker 未覆盖",
                detail=("item=%s 本次要写 `%s`,但它的 settlement_status 已经不在"
                        "「尚未判定」集合里。**没有覆盖**,请人工核这一条到底是谁写的。"
                        % (item["id"], settlement)),
                payload={"order_item_id": int(item["id"]), "intended": settlement})
            return {"order_item_id": int(item["id"]), "kind": outcome.kind,
                    "ok": False, "settlement_status": "already_decided",
                    "capacity_state": capacity_state,
                    "error": "结算已被别的写入方判定,未覆盖"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"order_item_id": int(item["id"]), "kind": outcome.kind,
            "ok": outcome.kind == SUBMIT_DELIVERED,
            "settlement_status": settlement, "capacity_state": capacity_state,
            "error": reason}


# ─────────────────────────────────────────────────────────────
# 调度入口
# ─────────────────────────────────────────────────────────────

async def run_tick(*, per_tick: int = DEFAULT_BATCH_PER_TICK) -> dict[str, int]:
    """一轮:三条链各处理至多 `per_tick` 件,再回收一次卡住的外调。

    🔴 每一件都单独 try —— 一件炸掉不许把整轮打断,否则一个坏任务
       会让**其它所有**任务永远排在它后面(本仓 FIFO 排死那一类)。
    """
    counts = {"production": 0, "artifact": 0, "publish": 0, "reconciled": 0}

    # 🔴 [WO_213] 总闸只停**领新活**这三条 lane,**不停**下面的收敛器与回填。
    #
    #    一开始我写的是整个 run_tick 早退 —— 那是**错的**,而且是危险的错:
    #    三条 lane 之后还有五个收敛器 + 回填,它们负责把**已经在途**的任务走完,
    #    包括把冻结的算力结清。整体早退 = 关闸的那一刻,在途任务连同冻结一起卡死。
    #    本文件上游的调度注册处早就写过这句警告(「已经在途的任务仍必须被收敛」),
    #    我差点照字面把它违反掉。
    #
    #    所以闸的语义是:**不再开始新的、不再花新的钱**;已经花出去的,
    #    照样把它走到终态。
    counts["pipeline_open"] = 1 if pipeline_gate_open() else 0

    for key, runner in (("production", run_production_once),
                        ("artifact", run_artifact_prepare_once),
                        ("publish", run_publish_submit_once)):
        if not counts["pipeline_open"]:
            break          # 闸关:三条 lane 一件都不领
        for _ in range(int(per_tick)):
            try:
                out = await runner()
            except Exception as exc:  # noqa: BLE001
                logger.error("[imgnote-worker] %s 轮次异常: %s", key, exc)
                break
            if out is None:
                break
            counts[key] += 1
    # 🔴 [第 8 棒 · R8 P1] 五个收敛器 + 回填**逐个 try**,与上面三条主链同待遇。
    #    上一版它们是裸调的:排在前面的任何一个抛异常,后面的全部不执行 ——
    #    而**回填排在最后**,于是「某个收敛器一直炸」= 回填被永久饿死,
    #    且从外面看只是 `run_tick` 抛了一次,没人知道少跑了什么。
    async def _guarded(key: str, thunk):
        try:
            return await thunk()
        except Exception as exc:  # noqa: BLE001
            logger.error("[imgnote-worker] 收敛器 %s 本轮异常(不影响其余): %s", key, exc)
            counts["converger_errors"] = int(counts.get("converger_errors") or 0) + 1
            raise_wiring_alert(
                rule_key="geo_imgnote_converger_failed",
                fingerprint="converger:%s" % key,
                severity="warn",
                title="图文调度:收敛器 %s 本轮抛异常" % key,
                detail=("收敛器 %s 抛了 %s。本轮其余收敛器与回填**照常执行**;"
                        "但它连续失败意味着它负责的那一格在积压。"
                        % (key, type(exc).__name__)),
                payload={"converger": key, "error": type(exc).__name__})
            return None

    counts["converger_errors"] = 0
    counts["reconciled"] = await _guarded(
        "stuck_tasks", lambda: asyncio.to_thread(_reconcile_stuck)) or 0
    # 🔴 [第 3 棒 · Codex R2 P0-5] 三条链**各有**自己的崩溃面,回收器不能只有一个。
    #    上一轮只回收制作任务:素材准备崩在上传中途 ⇒ 永久 `preparing`
    #    (claim 只回收 `external_started_at IS NULL` 的),发布项崩在提交中途
    #    ⇒ 永久 `pending` 且**钱一直冻着**。两条都没有任何人会再碰它们。
    counts["reconciled_artifacts"] = await _guarded(
        "stuck_artifacts", lambda: asyncio.to_thread(_reconcile_stuck_artifacts)) or 0
    counts["reconciled_publish"] = await _guarded(
        "stuck_publish_items", lambda: asyncio.to_thread(_reconcile_stuck_publish_items)) or 0
    # 🔴 [第 4 棒 · Codex R3 P0-A] 上面三个回收器收的都是「**我们**崩在外调中途」
    #    (item/artifact/task 还停在我们自己写的在途值上)。它们**够不着**
    #    另一类窗口:被调方已经自己 commit 了终态,而我方的结算/收尾事务
    #    还没提交就崩了。那一类只能由下面两个「由资金列驱动」的收敛器收 ——
    #    少了它们,钱就永久冻在库里,而且没有任何判据会红。
    _unsettled = await _guarded("unsettled_publish", reconcile_unsettled_publish_items) or {}
    counts["reconciled_unsettled_publish"] = int(_unsettled.get("scanned") or 0)
    counts["reconciled_production_tails"] = await _guarded(
        "production_tails",
        lambda: asyncio.to_thread(reconcile_unfinished_production_tails)) or 0
    # 🔴 [第 5 棒 · P1] 归因锚回填也挂在这一轮:它是 `published_url` 的唯一写入方,
    #    不接进调度就又是一个"函数写好了没人调"(本包被撤过一次 PASS 的根因)。
    # [#184 d3c] 失败投影与回填同一轮、同待遇(逐个 _guarded):
    #   回填只管成功,这条只管"确定失败"。少了它,新链失败的作品永远停在 publishing。
    _fp = await _guarded(
        "failed_publish_projection",
        lambda: asyncio.to_thread(project_failed_publish_posts)) or {}
    counts["publish_failed_projected"] = int(_fp.get("failed_projected") or 0)
    _bf = await _guarded(
        "backfill_urls",
        lambda: asyncio.to_thread(backfill_published_urls_from_provider)) or {}
    counts["backfilled_urls"] = int(_bf.get("post_projected") or 0)
    counts["receipt_contradictions"] = int(_bf.get("contradictions") or 0)
    counts["backfill_row_errors"] = int(_bf.get("row_errors") or 0)
    return counts


def _reconcile_stuck() -> int:
    from db.connection import get_connection
    from services.geo_douyin.durable_worker import reconcile_stuck_external_tasks

    conn = get_connection()
    try:
        cur = conn.cursor()
        ids = reconcile_stuck_external_tasks(cur)
        conn.commit()
        return len(ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


#: 租约之外再留一段。理由同 `reconcile_stuck_external_tasks`:租约刚过期
#: 那一刻可能只是心跳抖动,立刻判死会把正常任务打成需人工。
RECONCILE_GRACE_SECONDS = 900

_RECLAIM_STUCK_ARTIFACT = """
UPDATE geo_douyin_publish_artifacts
   SET state = 'unknown',
       card_statuses = card_statuses || %(note)s::jsonb,
       status_version = status_version + 1,
       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
 WHERE state = 'preparing'
   AND external_started_at IS NOT NULL
   AND lease_expires_at IS NOT NULL
   AND lease_expires_at < now() - (%(grace)s || ' seconds')::interval
RETURNING prepared_artifact_id
"""


def _reconcile_stuck_artifacts(grace_seconds: int = RECONCILE_GRACE_SECONDS) -> int:
    """把「已开始逐图上传、租约早已过期」的素材收敛成 `unknown`。

    🔴 是 `unknown` 不是 `failed`:逐图上传**失败时不回收已传的那几张**,
       远端很可能已经有一部分素材。`failed` 是可自动重试的(`RETRYABLE_STATES`),
       重试就会造出重复素材。
    """
    import json as _json

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_RECLAIM_STUCK_ARTIFACT, {
            "grace": int(grace_seconds),
            "note": _json.dumps([{"index": -1, "state": "unknown",
                                  "reason": "上传中途中断,结果未知,待人工核对"}],
                                ensure_ascii=False)})
        return len(cur.fetchall() or [])
    finally:
        conn.commit()
        conn.close()


_RECLAIM_STUCK_PUBLISH_ITEM = """
UPDATE mhz_publish_order_items
   SET status = 'needs_action',
       settlement_status = 'manual',
       reject_reason = COALESCE(NULLIF(reject_reason, ''), %(reason)s),
       lease_owner = NULL, lease_expires_at = NULL
 WHERE billing_mode = 'freeze_per_item'
   AND status IN ('pending', 'submitting')
   AND external_started_at IS NOT NULL
   AND lease_expires_at IS NOT NULL
   AND lease_expires_at < now() - (%(grace)s || ' seconds')::interval
RETURNING id
"""


# ══════════════════════════════════════════════════
# 崩溃窗口收敛器(第 4 棒 · Codex R3 P0-A)· 由 `settlement_status` 驱动
# ══════════════════════════════════════════════════
#
# 上面那个 `_reconcile_stuck_publish_items` 收的是「**我们**崩在外调中途」——
# item 还停在我们写的 `pending`,租约到期即可回收。
#
# 这里收的是**另一条缝**:旧提交器**已经自己 commit 了终态**
# (`submitted` / `awaiting_sync` / `failed`),而我方的结算事务还没提交就崩了。
# 那几个态**不在**上面那个回收器的扫描集 `('pending','submitting')` 里,
# 也不在 worker 的 claim 谓词(`queued`)里 —— 没有任何人会再碰它们,
# 钱永久冻着。缝的成因是"旧提交器自带独立连接与 commit",同事务做不到,
# 所以只能**逐态枚举 + 逐态收敛**(枚举表见 `contract_seams` 缝合面 C)。
#
# 🔴 扫描谓词的**主语是 `settlement_status`**,不是 item `status`:
#    "这笔钱到底结没结"只有我们自己的资金列说了算;item status 是被调方写的,
#    值域不归我们管。反过来写(先按 status 找、再看钱)会在被调方新增一个
#    终态时静默漏掉一整类窗口。

_CLAIM_UNSETTLED_PUBLISH_ITEM = """
UPDATE mhz_publish_order_items
   SET lease_owner = %(worker)s,
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval
 WHERE id = (
     SELECT i.id FROM mhz_publish_order_items i
      WHERE i.billing_mode = 'freeze_per_item'
        -- ① 钱还冻着(主语)
        AND i.settlement_status = 'frozen'
        -- ② 被调方已经写过终态(窗口的另一侧)
        AND i.status = ANY(%(terminal_statuses)s)
        -- ③ 确实外调过 —— 没外调过的由 claim 谓词负责,不归这里
        AND i.external_started_at IS NOT NULL
        -- ④ 租约过期 + 宽限:活着的 worker 正在结算时不许插手
        AND (i.lease_expires_at IS NULL
             OR i.lease_expires_at < now() - (%(grace)s || ' seconds')::interval)
      ORDER BY i.created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
 )
   -- 🔴 CAS:再判一次 `frozen`。它防的是 READ COMMITTED 下的 EvalPlanQual 那一格
   --    (子查询选中之后、行锁拿到之前,别人把 settlement 改掉并提交)。
   --
   -- ⚠️ [第 5 棒 · Codex R5 P2 · **如实标注**] 这一行**没有判据覆盖**:
   --    子查询里的 `FOR UPDATE SKIP LOCKED` 已经把可构造的并发路径全挡住了,
   --    两连接竞态跑不出能让 CAS 与子查询谓词不一致的时序(第 4 棒变异 M2 存活,
   --    正是这个原因)。定性 = **纵深防御,非承重**;
   --    它**不计入**"已验证闭环",删掉它不会有任何判据转红。
   AND settlement_status = 'frozen'
RETURNING id, order_id, user_id, media_id, cost_points, freeze_id, freeze_table,
          payer_user_id, reserved_amount, settlement_authority, task_ref,
          source_geo_post_id, source_post_revision_id, prepared_artifact_id,
          manifest_hash, capacity_date, media_type, status, mhz_order_id
"""


async def reconcile_unsettled_publish_items(
        *, worker: Optional[str] = None,
        grace_seconds: int = RECONCILE_GRACE_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_items: int = 20) -> dict:
    """收「被调方已落终态、我方未结算」的崩溃窗口。**幂等**。

    幂等靠两件事,不是靠调用方只调一次:
      · 领取 SQL 的 CAS 谓词 `settlement_status = 'frozen'`;
      · 处置完之后 `settlement_status` 必然离开 `frozen`(committed /
        released / manual 三者之一)⇒ 第二趟扫不到同一行。

    返回各处置的计数,给判据与运维看"这一轮到底收了什么"。
    """
    from db.connection import get_connection

    me = worker or worker_id()
    counts = {"scanned": 0, "committed": 0, "released": 0, "manual": 0}
    for _ in range(int(max_items)):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(_CLAIM_UNSETTLED_PUBLISH_ITEM, {
                "worker": me, "lease_seconds": int(lease_seconds),
                "grace": int(grace_seconds),
                "terminal_statuses": sorted(LEGACY_SUBMITTER_TERMINAL_STATUSES)})
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                break
            item = dict(row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        outcome = classify_unsettled_publish_item(
            item_status=item.get("status"), mhz_order_id=item.get("mhz_order_id"))
        settled = await _settle_publish_item(
            item, outcome, item_status_override=outcome.item_status_override,
            error=outcome.reason)
        counts["scanned"] += 1
        _final = str(settled.get("settlement_status") or "")
        if _final == SETTLEMENT_COMMITTED:
            counts["committed"] += 1
        elif _final == SETTLEMENT_RELEASED:
            counts["released"] += 1
        else:
            counts["manual"] += 1
        logger.error(
            "[imgnote-worker] 崩溃窗口收敛 item=%s(渠道态=%s)-> %s:%s",
            item["id"], item.get("status"), _final, outcome.reason)
        raise_wiring_alert(
            rule_key="geo_imgnote_unsettled_after_crash",
            fingerprint="publish_item:%s" % item["id"],
            severity="warn",
            title="图文发布:崩溃后遗留的未结算项已收敛",
            detail=("item=%s 在『渠道已返回、结算未提交』之间崩过,"
                    "渠道态=%s,收敛结果=%s。%s"
                    % (item["id"], item.get("status"), _final, outcome.reason)),
            payload={"order_item_id": int(item["id"]),
                     "channel_status": item.get("status"),
                     "settlement_status": _final,
                     "kind": outcome.kind})
    return counts


_CLAIM_UNFINISHED_PRODUCTION_TAIL = """
UPDATE geo_douyin_post_tasks
   SET lease_owner = %(worker)s,
       lease_expires_at = now() + (%(lease_seconds)s || ' seconds')::interval,
       updated_at = now()
 WHERE id = (
     SELECT t.id FROM geo_douyin_post_tasks t
       JOIN geo_douyin_posts p ON p.id = t.post_id
      WHERE t.production_batch_id IS NOT NULL      -- 合同链作用域(同 claim 闸)
        AND t.superseded_at IS NULL
        -- ① 钱已经结清(主语同上:资金列说了算)
        AND t.settlement_status = 'committed'
        -- ② 任务处在「资金已结之后」的两个态之一。
        --    🔴 [第 5 棒 · Codex R5 P0-2] 上一版只收 `ready`,盖不住更早那一格:
        --       资金那一段事务已提交,而 `ddb.update_task(status=ready)` 是**再下
        --       一个**事务 —— 崩在中间留下 `running + committed`,一样没人管。
        --       `settlement='committed'` 只在成功路径产生(失败走 release/manual),
        --       所以它就是"生产成功了"的耐久证据,据此补写终态不是猜。
        AND t.status IN ('running', 'ready')
        -- ③ 可收尾**没做完**:成品版本指针还是空的
        AND p.active_revision_id IS NULL
        AND (t.lease_expires_at IS NULL
             OR t.lease_expires_at < now() - (%(grace)s || ' seconds')::interval)
        -- 🔴 [第 7 棒] 本趟已判定"身份不符、零变更退出"的行不再重领:
        --    零变更意味着连 claim 写的租约都被 rollback 掉了,不排除就会
        --    在同一趟里被无限重领(死循环),而不是被跳过。
        AND t.id <> ALL(%(skip)s::bigint[])
      ORDER BY t.id
      FOR UPDATE SKIP LOCKED
      LIMIT 1
 )
   AND settlement_status = 'committed' AND status IN ('running', 'ready')
RETURNING id, status
"""


_LOCK_TAIL_IDENTITY = """
SELECT p.status              AS post_status,
       p.deleted_at          AS post_deleted_at,
       p.active_revision_id,
       p.active_generation_task_id,
       p.generation_epoch
  FROM geo_douyin_post_tasks t
  JOIN geo_douyin_posts p ON p.id = t.post_id
 WHERE t.id = %(task_id)s
   FOR UPDATE
"""


def reconcile_unfinished_production_tails(
        *, worker: Optional[str] = None,
        grace_seconds: int = RECONCILE_GRACE_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_items: int = 20) -> int:
    """收制作链的第三个同构窗口:**钱已结、成品版本没冻**。

    `run_image_post_production` 内部自己提交了「task=ready + settlement=committed
    + post=ready」,而收尾(`finish_task` + 冻成品版本 + 推进槽位)是返回之后
    **另一个**事务。崩在中间 ⇒ `posts.active_revision_id` 恒 NULL ⇒
    素材准备入口对这一篇**永久 409**,而钱已经扣了。
    既有的 `reconcile_stuck_external_tasks` 只扫 `status='running'`,够不着它。

    🔴 这里**不碰钱**(钱已经 commit 完了,那一步是对的),只把没做完的
       收尾补上:冻成品版本 + 推进槽位 + 放掉死 worker 留下的租约。
    🔴 幂等:补完之后 `active_revision_id` 非空 ⇒ 第二趟扫不到;
       CAS 输了(已被新一代接管)⇒ `mark_task_superseded` ⇒ 也扫不到。
    """
    from db.connection import get_connection

    me = worker or worker_id()
    healed = 0
    skip_ids: list[int] = []
    rejected: list[tuple[int, tuple[str, ...]]] = []
    superseded: list[int] = []
    for _ in range(int(max_items)):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(_CLAIM_UNFINISHED_PRODUCTION_TAIL, {
                "worker": me, "lease_seconds": int(lease_seconds),
                "grace": int(grace_seconds), "skip": list(skip_ids)})
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                break
            claimed = dict(row)
            task_id = int(claimed["id"])
            # 🔴 [第 7 棒 · Codex R7 P0-A] **先把 post 行也锁上,再核身份**。
            #    claim 的 `FOR UPDATE SKIP LOCKED` 只锁了 task 行,而收敛器接下来
            #    要写的四个对象里有三个挂在 post 上。不锁不核的后果(Codex 反例):
            #    `post.status='failed'` 的作品会被收敛器**复活**成已完成。
            cur.execute(_LOCK_TAIL_IDENTITY, {"task_id": task_id})
            ident = classify_production_tail_identity(
                task_id=task_id, row=dict(cur.fetchone() or {}))
            if not ident.ok:
                # 🔴 **四对象零变更退出**:连 claim 刚写的租约也一并 rollback ——
                #    "不符就不动",而不是"不符就只动一点点"。
                conn.rollback()
                skip_ids.append(task_id)
                rejected.append((task_id, ident.reasons))
                continue
            ctx = _load_production_context(cur, task_id=task_id)
            if ctx is None:
                conn.rollback()
                raise RuntimeError("task=%s 领到了但读不出上下文" % task_id)
            revision_id = _freeze_active_revision(cur, ctx)
            if revision_id is None:
                # 🔴 成品版本没冻成(CAS 输了 ⇒ `_freeze_active_revision` 已经把
                #    这条 task 标成 superseded)。这时候**一律不许**继续往下写
                #    task ready / 作品 ready / 推槽位 —— 上一版就是无条件写的,
                #    于是那句 `SET status='ready'` 把刚标好的 superseded **又改回了
                #    ready**,一条已易主的任务在库里显示成功。
                #    只提交 superseded 那一笔。
                conn.commit()
                skip_ids.append(task_id)
                superseded.append(task_id)
                continue
            if ctx.get("delivery_slot_key") and ctx.get("slot_version"):
                _advance_slot_to_ready(cur, ctx)
            # 收尾做完,放掉死 worker 留下的租约(否则下一趟还得等宽限期)。
            # 🔴 崩在「资金已结、终态未写」那一格时,终态也要补上 ——
            #    否则这一条会永远停在 `running`,而它其实早就做完并结完账了。
            cur.execute(
                "UPDATE geo_douyin_post_tasks"
                "   SET status = %s, lease_owner = NULL, lease_expires_at = NULL,"
                "       finished_at = COALESCE(finished_at, now()), updated_at = now()"
                " WHERE id = %s", (TASK_STATUS_READY, task_id))
            # 🔴 [第 6 棒 · Codex R6 P0-A] **作品自己的状态也在这条缝里**。
            #
            #    真实崩溃态是 `posts.status = 'generating'`:生产成功路径的次序是
            #    ①资金(同事务落 settlement)→ ②`update_task(ready)` → ③`set_post_status(ready)`。
            #    崩在 ① 之后,②③ 都没跑 —— 上一版收敛器补了任务终态、成品版本、槽位,
            #    **唯独没碰作品状态**,于是这一篇在用户眼里永远"还在做",而钱早扣了。
            #
            #    上一版的判据没抓到,是因为**夹具把 post 预置成了 `ready`** ——
            #    夹具替被测代码把活干了,这一格就永远绿。夹具现在只置 `generating`。
            #
            #    只从 `generating` 抬到 `ready`,是刻意的窄谓词:`settlement='committed'`
            #    只在**全量成功**路径产生(部分交付 `completing` 不 commit、失败走 release),
            #    所以这一格的作品确实是做完的;而 `failed` / `completing` / 已经是 `ready`
            #    的行一律不碰 —— 收敛器补的是"没写完的那一笔",不是重写事实。
            cur.execute(
                "UPDATE geo_douyin_posts"
                "   SET status = 'ready', updated_at = now()"
                " WHERE id = %s AND status = 'generating' AND deleted_at IS NULL",
                (int(ctx["post_id"]),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        healed += 1
        logger.error("[imgnote-worker] 制作链收尾补做 task=%s(成品版本此前为空)", task_id)
        raise_wiring_alert(
            rule_key="geo_imgnote_production_tail_unfinished",
            fingerprint="task:%s" % task_id,
            severity="warn",
            title="图文制作:算力已结算但成品版本没冻,已补做收尾",
            detail=("task=%s 在『资金已 commit、成品版本未冻』之间崩过 —— "
                    "在补做之前这一篇进不了素材准备(永久 409),而钱已经扣了。"
                    % task_id),
            payload={"task_id": task_id})
    for task_id, reasons in rejected:
        # 🔴 零变更退出**必须留痕**:不留痕的"跳过"与"处理过了"在读侧一模一样。
        logger.warning("[imgnote-worker] 收尾收敛跳过 task=%s(身份不符):%s",
                       task_id, " / ".join(reasons))
        raise_wiring_alert(
            rule_key="geo_imgnote_tail_identity_mismatch",
            fingerprint="task:%s" % task_id,
            severity="warn",
            title="图文制作:收尾收敛遇到身份不符,已零变更退出",
            detail=("task=%s 被收尾收敛领到,但加锁后核验身份不符,"
                    "**没有改动任何对象**:%s" % (task_id, " / ".join(reasons))),
            payload={"task_id": task_id, "reasons": list(reasons)})
    for task_id in superseded:
        logger.info("[imgnote-worker] 收尾收敛遇到已易主的生成 task=%s,"
                    "只标 superseded,不写终态", task_id)
    return healed


# ═══════════════════════════════════════════════════════════════════
# 归因锚:**canonical item 终态先行,post 只是投影**(第 6 棒 · Codex R6 P0-B)
# ═══════════════════════════════════════════════════════════════════
#
# ## 通则(本仓从此照此办理)
#
# 🔴 **任何新 writer 要写 canonical 邻接表,必须声明它与 canonical writer 的
#    写入次序**,并且判据要**打次序**,不只打幂等。
#
#    这里的 canonical writer 是 `db.meijiehezi_db.update_order_item_status(
#    item_id, "published", publish_url=...)` —— 它是"这一项发出去了"这件事的
#    唯一权威落点(写 `status='published'` + `publish_url` + `published_at`,
#    并重算订单状态)。`geo_douyin_posts` 的 `published_url` 是**它的投影**,
#    不是第二个事实源。
#
#    次序:  ① 供应商镜像(`mhz_synced_orders`)= **信号**
#          → ② canonical item 终态 CAS = **权威**
#          → ③ post 回填 = **投影**
#
# ## 第 5 棒错在哪(Codex R6 P0-B)
#
# 上一版直接 `mirror JOIN item → UPDATE post`,**跳过了 ②**。后果是一条
# **矛盾的迟到回执**就能把作品写成已发布:item 早就 `failed + released`
# (钱退了、用户被告知没发出去),而镜像后来同步到 `status=2` ——
# post 于是被写上"已发布 + 真链接"。资金说退了、作品说发了,两边都言之凿凿。
#
# 现在:矛盾态**一个字都不写**,只落一条 observation 告警等人工核实。

#: 镜像里"已完成"的那个取值(供应商枚举:-2 已撤回 / -1 已拒稿 / 0 待接单 /
#: 1 发布中 / 2 已完成)。见 `services/ai_ops/report_metrics/publishing.py`。
PROVIDER_STATUS_DONE = 2

#: item 允许**转入** published 的前置状态。写成封闭集合而不是"不等于某几个" ——
#: 反向写法在被调方新增状态时会静默放行。
ITEM_PUBLISHABLE_FROM = ("submitted", "awaiting_sync")

#: 资金**尚未收敛**的两个态。回执比结算先到是正常时序,不是矛盾 ——
#: 这一格要"等",不要"报"。分不开这两者的话,真矛盾会被日常噪声淹掉。
SETTLEMENT_NOT_YET_RESOLVED = ("frozen", "settlement_pending")

#: 结算**还没被任何人判定**的取值。`_settle_publish_item` 的 CAS 前置态就是它 ——
#: 已经被判定过的项一律不覆盖(老 sweeper 抢先标 failed 是真会发生的一条路径)。
SETTLEMENT_NOT_YET_DECIDED = ("frozen", "settlement_pending")

_FIND_RECEIPTS = """
SELECT i.id AS item_id, i.order_id, i.status AS item_status,
       i.settlement_status, i.source_geo_post_id, s.url, s.published_at
  FROM mhz_publish_order_items i
  JOIN mhz_synced_orders s ON s.order_sn = i.mhz_order_id
 WHERE i.billing_mode = 'freeze_per_item'
   AND s.status = %(done)s
   AND COALESCE(s.url, '') <> ''
   AND i.source_geo_post_id IS NOT NULL
"""

#: ② canonical item 终态转移 = **调 canonical writer 本人**
#:    `db.meijiehezi_db.update_order_item_status(item_id, "published", ...)`。
#:
#:    🔴 [第 7 棒 · Codex R7 P0-B] 上一版这里是一句自己写的 CAS UPDATE。它写对了
#:       终态三列,却漏掉了 canonical writer 在同一笔里做的**其余四件事**:
#:         ① `recompute_order_status` —— 不做的话订单头永远停在 `pending`;
#:         ② 发布快照 `capture_mhz_publication_snapshot_with_cursor`;
#:         ③ 交付 lineage outbox `enqueue_publication_locked_in_transaction_if_enabled`;
#:         ④ 通知 outbox `_enqueue_publish_notification`(用户根本收不到"已发布")。
#:       "同一件事有两个写入者"这件事本身就是病:第二个写入者永远会漏掉第一个
#:       后来新增的动作,而且**漏得无声**。现在只有一个写入者,回填把
#:       `cursor` 并进自己的事务、把 CAS 谓词作为参数传给它。

#: ③ post 回填 —— 读的是**item 的权威列**,不是镜像。
_PROJECT_POST = """
UPDATE geo_douyin_posts p
   SET published_url = i.publish_url,
       published_at  = COALESCE(p.published_at, i.published_at, now()),
       publish_status = 'published',
       updated_at = now()
  FROM mhz_publish_order_items i
 WHERE i.id = %(item_id)s
   AND i.status = 'published'
   AND COALESCE(i.publish_url, '') <> ''
   AND p.id = i.source_geo_post_id
   AND p.deleted_at IS NULL
   AND COALESCE(p.published_url, '') = ''
RETURNING p.id
"""


def _backfill_one_row(cur, row, counts, contradictions) -> None:
    """一行的**权威转移 + 投影**。抛出即由调用方回滚到该行自己的 SAVEPOINT。

    抽成函数不是为了好看,是为了让"一行失败"有一个**能包起来的边界** ——
    内联在循环里时,包住它的 try 会把 `continue` 一并吞掉。

    🔴 [第 8 棒 · R8 P1] `counts` 传进来的是**本行的临时账**,不是全局账。
       上一版直接加在全局 counts 上:一行走到一半抛出去,数据库那半笔被
       `ROLLBACK TO SAVEPOINT` 撤了,而 Python 里已经加上的
       `item_published` / `post_projected` **撤不掉** ⇒ 回执收口日志与
       `run_tick` 的 `backfilled_urls` 会报出**比实际多**的数。
       现在只有 `RELEASE` 成功之后才把这一行的临时账并进全局。
    """
    from db.meijiehezi_db import update_order_item_status

    item_status = str(row.get("item_status") or "")
    settlement = str(row.get("settlement_status") or "")
    if item_status == "published":
        # 权威已经是 published,只补投影(幂等重放走这条)
        pass
    elif item_status in ITEM_PUBLISHABLE_FROM:
        # 🔴 [第 6 棒 · 变异 Q7 逼出来的] "能不能转 published"这个谓词**只有一处**:
        #    作为参数传给 canonical writer,由它拼进那一句 UPDATE 的 WHERE。
        #    上一版在 Python 侧又判了一遍 `settlement == "committed"`,于是 SQL 里
        #    那半句成了摆设:把它改坏(变异 Q7)照样全绿 —— 两处判同一件事,
        #    永远有一处是没人验的。
        moved = update_order_item_status(
            int(row["item_id"]), "published",
            publish_url=str(row["url"]),
            cursor=cur,
            expect_status_in=ITEM_PUBLISHABLE_FROM,
            expect_settlement=SETTLEMENT_COMMITTED,
            published_at=row.get("published_at"))
        if not moved.get("changed"):
            # CAS 没命中:要么钱还没结(等下一轮),要么状态已被别人动过。
            if settlement in SETTLEMENT_NOT_YET_RESOLVED:
                counts["pending_settlement"] += 1
            else:
                counts["contradictions"] += 1
                contradictions.append(row)
            return
        counts["item_published"] += 1
    else:
        # 🔴 矛盾回执:镜像说完成,而权威侧说这一项失败/已退款。
        #    **不猜哪边对**,两边都不动,交人工。
        counts["contradictions"] += 1
        contradictions.append(row)
        return

    cur.execute(_PROJECT_POST, {"item_id": int(row["item_id"])})
    if cur.fetchone() is not None:
        counts["post_projected"] += 1


#: [#184 d3c] 新链作品的**失败**投影。
#:
#: 🔴 为什么要单独一条:`backfill_published_urls_from_provider` 只投影**成功**
#:    (镜像 `status=done` 且 url 非空)。逐项冻结的项若终态是 failed/rejected/
#:    cancelled/withdrawn,回执里根本不会出现 done —— 于是作品永远停在
#:    `publishing`,而老链那条收敛器已经被 d3 按 `freeze_per_item` 排除了。
#:    结果就是 #151 那个症状在新链原样重现:服务商界面上「正在发布」挂到天荒地老。
#:
#: 🔴 判定复用 `publish_convergence.classify_order_items`(全部终态且无一成功才算失败)
#:    ——不另写一套。两套"什么叫失败"的定义迟早会分叉,而分叉时没人会红。
#: 🔴 **不写 URL**:失败没有链接。这条只动 `publish_status`。
_FIND_NEW_CHAIN_PUBLISHING = """
SELECT p.id AS post_id
  FROM geo_douyin_posts p
 WHERE p.publish_status = 'publishing'
   AND p.deleted_at IS NULL
   AND COALESCE(p.published_url, '') = ''
   AND EXISTS (SELECT 1 FROM mhz_publish_order_items i
                WHERE i.source_geo_post_id = p.id
                  AND i.billing_mode = 'freeze_per_item')
 LIMIT %(limit)s
"""

_NEW_CHAIN_ITEMS = """
SELECT status, publish_url
  FROM mhz_publish_order_items
 WHERE source_geo_post_id = %(post_id)s
   AND billing_mode = 'freeze_per_item'
"""

_MARK_POST_PUBLISH_FAILED = """
UPDATE geo_douyin_posts
   SET publish_status = 'failed', updated_at = now()
 WHERE id = %(post_id)s
   AND publish_status = 'publishing'
   AND COALESCE(published_url, '') = ''
RETURNING id
"""


def project_failed_publish_posts(limit: int = 200) -> dict:
    """把新链上**确定失败**的作品从 publishing 里放出来。返回读数。"""
    from db.connection import get_connection
    from services.geo_douyin.publish_convergence import classify_order_items

    counts = {"scanned": 0, "failed_projected": 0, "still_in_flight": 0}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_FIND_NEW_CHAIN_PUBLISHING, {"limit": int(limit)})
        posts = [int(dict(r)["post_id"]) for r in (cur.fetchall() or [])]
        counts["scanned"] = len(posts)
        for post_id in posts:
            cur.execute(_NEW_CHAIN_ITEMS, {"post_id": post_id})
            verdict, _url = classify_order_items(
                [dict(r) for r in (cur.fetchall() or [])])
            if verdict != "failed":
                # 成功由既有回填器投影(它带矛盾回执与结算守卫);在途就等下一轮。
                counts["still_in_flight"] += 1
                continue
            cur.execute(_MARK_POST_PUBLISH_FAILED, {"post_id": post_id})
            if cur.fetchone() is not None:
                counts["failed_projected"] += 1
                logger.info("[imgnote-backfill] 作品 %s 的发布项全部终态且无一成功 ⇒ 标 failed",
                            post_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def backfill_published_urls_from_provider() -> dict:
    """供应商回执 → **canonical item 终态** → post 投影。返回各段计数。

    🔴 三条边界一条都不能少:
      ① 只认镜像 `status = 2` 且 `url` 非空(待接单/发布中/已拒稿都不是已发布);
      ② item 必须**还在可发布前置态且钱已结清**才允许转 published(CAS);
      ③ post 只填**空的** `published_url`(投影是补锚,不是覆盖事实)。

    🔴 **矛盾回执**(镜像说完成、而 item 已 `failed`/`cancelled` 或钱已
       `released`/`manual`)⇒ **一个字都不写**,落一条 observation 告警。
       这类矛盾是真的会发生的(迟到的同步 + 已经退过款的项),
       上一版会把它写成"已发布 + 真链接",与资金账直接打架。

    🔴 **"还没结算"不算矛盾**:回执比结算先到是正常时序,记 `pending_settlement`
       并等下一轮。两者不分开的话,每天都会拉一堆假告警,真矛盾就被淹掉了。

    🔴 **逐行 SAVEPOINT**(第 7 棒):改走 canonical writer 之后,单行的失败面比
       原来那句自写 UPDATE 大得多 —— 订单头重算 / 发布快照 / 交付 lineage /
       通知 outbox 都在同一笔里。没有 SAVEPOINT 的话,**一条毒行会把整批一起
       回滚**,下一轮还撞同一条 ⇒ 回填就此永久停摆,而且是无声的。
    """
    from db.connection import get_connection

    counts = {"scanned": 0, "item_published": 0, "post_projected": 0,
              "contradictions": 0, "pending_settlement": 0, "row_errors": 0}
    conn = get_connection()
    contradictions: list = []
    row_errors: list = []
    try:
        cur = conn.cursor()
        cur.execute(_FIND_RECEIPTS, {"done": PROVIDER_STATUS_DONE})
        rows = [dict(r) for r in (cur.fetchall() or [])]
        counts["scanned"] = len(rows)
        for row in rows:
            # 🔴 [第 8 棒 · R8 P1] 本行先记在**临时账**上;只有 SAVEPOINT 真的
            #    RELEASE 了才并进全局。回滚掉的行只留 `row_errors` 一个数,
            #    不许把它那半笔已经撤销的写入算进 item_published / post_projected。
            row_counts = {"item_published": 0, "post_projected": 0,
                          "contradictions": 0, "pending_settlement": 0}
            row_contradictions: list = []
            cur.execute("SAVEPOINT geoimg_backfill_row")
            try:
                _backfill_one_row(cur, row, row_counts, row_contradictions)
                cur.execute("RELEASE SAVEPOINT geoimg_backfill_row")
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT geoimg_backfill_row")
                cur.execute("RELEASE SAVEPOINT geoimg_backfill_row")
                counts["row_errors"] += 1
                row_errors.append((dict(row), exc))
                continue
            for _k, _v in row_counts.items():
                counts[_k] += _v
            contradictions.extend(row_contradictions)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for row, exc in row_errors:
        logger.error("[imgnote-worker] 回执回填单行失败 item=%s:%r",
                     row.get("item_id"), exc)
        raise_wiring_alert(
            rule_key="geo_imgnote_backfill_row_failed",
            fingerprint="publish_item:%s" % row.get("item_id"),
            severity="critical",
            title="图文发布:回执回填单行失败(同批其余行不受影响)",
            detail=("item=%s 的回填在权威转移或投影时抛了 %s。该行已回滚到它自己的"
                    "SAVEPOINT,**同批其余行照常写入**。请人工核这一条 —— "
                    "它在修好之前每一轮都会失败。"
                    % (row.get("item_id"), type(exc).__name__)),
            payload={"order_item_id": row.get("item_id"),
                     "error": type(exc).__name__})

    for row in contradictions:
        logger.error(
            "[imgnote-worker] 回执与权威态矛盾 item=%s(渠道镜像=已完成,"
            "而 item=%s / 结算=%s)—— 未改任何状态,待人工核实",
            row["item_id"], row.get("item_status"), row.get("settlement_status"))
        raise_wiring_alert(
            rule_key="geo_imgnote_receipt_contradiction",
            fingerprint="publish_item:%s" % row["item_id"],
            severity="critical",
            title="图文发布:渠道回执与权威状态矛盾(待人工核实)",
            detail=("item=%s 的渠道镜像显示已完成且有地址,而权威侧是 %s / 结算 %s。"
                    "两边不可能同时为真 —— 已**不做任何写入**,请人工判定是"
                    "迟到回执还是错误退款。"
                    % (row["item_id"], row.get("item_status"),
                       row.get("settlement_status"))),
            payload={"order_item_id": int(row["item_id"]),
                     "item_status": row.get("item_status"),
                     "settlement_status": row.get("settlement_status"),
                     "mirror_url": str(row.get("url") or ""),
                     "geo_post_id": row.get("source_geo_post_id")})
    if counts["item_published"] or counts["post_projected"]:
        logger.info("[imgnote-worker] 回执收口 %s", counts)
    return counts


def _reconcile_stuck_publish_items(grace_seconds: int = RECONCILE_GRACE_SECONDS) -> int:
    """已外调、租约过期的发布项 → `needs_action + manual`(§8.2 末)。

    🔴 **不 release**:release 的语义是"确定没发生",而这里恰恰不确定 ——
       渠道可能已经收下了。也**不重投**:重投是重复外调。
    🔴 谓词显式限定 `billing_mode = 'freeze_per_item'`:老链的 pending/submitting
       由老 sweeper 负责,两套收敛逻辑不能互相伸手。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_RECLAIM_STUCK_PUBLISH_ITEM, {
            "grace": int(grace_seconds),
            "reason": "已联系发布渠道但结果未知,算力保持冻结待人工核对"})
        return len(cur.fetchall() or [])
    finally:
        conn.commit()
        conn.close()
