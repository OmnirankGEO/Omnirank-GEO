"""发布 outbox 的**消费者** + 回执轮询(规格 §12.3 四个 kill window)。

═══════════════════════════════════════════════════════════════════════
🔴 这个文件是终审 P0-1 的另一半
═══════════════════════════════════════════════════════════════════════
P0-1 的处置是「关掉客户入口」,理由逐字:

    dispatch_once / claim_outbox / reconcile_once 全仓零调用点,
    调度器里跟发布有关的只有只读告警。而 confirm 是真冻钱的。

关入口是止血,不是修。本模块 + ``api/scheduler.py`` 里那三条 ``add_job``
才是修:三个函数各自有了**生产调用点**,调度器里有了**推进**而不只是告警。

═══════════════════════════════════════════════════════════════════════
🔴 事务边界不是风格问题,它就是窗口③本身
═══════════════════════════════════════════════════════════════════════
一次派发跨**两个**事务,中间夹着外调:

    事务 A: claim outbox → 读 command → 法律门 → mark_external_start → COMMIT
                                                                        ↑
                                        (``dispatch_once`` 的提交钩子在这里)
    ——— 外调(不持任何事务)———
    事务 B: 落 canonical outcome → 收 outbox → COMMIT

marker 与外调**同一个未提交事务**时,进程在外调途中被杀 ⇒ 事务回滚 ⇒
marker 消失 ⇒ 下一次派发盲重传。§12.3 逐字要的是「恢复时任一 marker 存在
均不得盲目二次外调」,而"存在"的前提是它熬得过崩溃 —— 所以必须先提交。

外调期间**不持事务**还有第二个理由:本仓记过「跑完不关事务」的伤害,
一次外调可能几十秒,把连接占住整场是自找的阻塞。

═══════════════════════════════════════════════════════════════════════
🔴 「通道没配好」不是「外调失败」
═══════════════════════════════════════════════════════════════════════
两者的钱向完全不同:
  · 外调失败(已 marker)→ 可能已到达对方 → **hold frozen**,转核验;
  · 通道没配好          → 一次外调都没发生 → **安全重试**,不动状态、不动钱。

所以就绪自检发生在 ``claim`` 之后、``dispatch_once`` 之前:不 ready 就
:func:`~store.defer_outbox` 把租约放回去、记原因、延后再来。
把后者当成前者会白白把客户的钱冻在核验队列里。

═══════════════════════════════════════════════════════════════════════
🔴 生产路径零 ``is_test`` / ``dry_run`` / ``sandbox``
═══════════════════════════════════════════════════════════════════════
判据要注入假 transport,就把它当**参数**传给 :func:`dispatch_pending`。
生产的 cron 一个参数都不传,走 ``provider_transport.resolve()``。
"分支"会在生产里被求值,"参数"不会 —— 后者靠物理上不存在,不靠环境变量没设。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Mapping

from services.defensive_geo.publish import body_hash as _bh
from services.defensive_geo.publish import provider_transport as _transport
from services.defensive_geo.publish import publish_outbox as _dispatch
from services.defensive_geo.publish import publish_settlement as _settle
from services.defensive_geo.publish import reconciler as _recon
from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoPublishConsumer")

CONSUMER_VERSION = "defgeo-publish-consumer-v1"

#: 一次 tick 最多派发几条。
BATCH = 5
#: 通道没就绪时,这一条延后多久再来。
NOT_READY_BACKOFF_SECONDS = 120
#: outbox 尝试多少次之后转人工(不是"重试上限",是"什么时候承认自动跑不通")。
MAX_ATTEMPTS = 6


class BodyResolutionError(RuntimeError):
    """取不到**冻结的那一版**正文。**不拿最新版顶上** —— 那会发出一篇没人签过的稿。"""


# ══════════════════════════════════════════════════════════════════════════
# 冻结正文
# ══════════════════════════════════════════════════════════════════════════
def _article_id_of(article_revision_id: str) -> int:
    """``article_revision_id`` → 现役 ``articles.id``。

    façade 的 revision id 是字符串(VARCHAR(120)),现役文章库的主键是整数。
    只认两种形态:``article:<id>`` 与裸数字。认不出来就抛 ——
    **不猜**:猜错会去发另一篇文章。
    """
    text = str(article_revision_id or "").strip()
    if text.startswith("article:"):
        text = text.split(":", 1)[1].strip()
    if not text.isdigit():
        raise BodyResolutionError(
            f"认不出这条 articleRevisionId 指向哪一篇现役文章:{article_revision_id!r}"
        )
    return int(text)


def load_frozen_body(cur, *, article_revision_id: str, article_hash: str) -> tuple[str, str]:
    """取正文 + 标题,并**逐字节核对**它就是冻结时那一版(DEL-08)。

    对不上时抛,而不是"发最新版":confirm 冻的是那一版的指纹,
    她按下确认时看到的也是那一版。发别的等于替客户改了交付物。
    """
    article_id = _article_id_of(article_revision_id)
    cur.execute("SELECT id, title, content FROM articles WHERE id = %s", (article_id,))
    row = cur.fetchone()
    if row is None:
        raise BodyResolutionError(f"文章 {article_id} 不存在")
    title = str((row["title"] if isinstance(row, Mapping) else row[1]) or "")
    body = str((row["content"] if isinstance(row, Mapping) else row[2]) or "")
    actual = _bh.body_hash(body)
    if actual != str(article_hash):
        raise BodyResolutionError(
            f"文章 {article_id} 的正文与冻结指纹对不上(冻结 {article_hash} / 现在 {actual})"
            " —— 确认之后有人改过稿,不发这一版"
        )
    return title, body


def _enrich(cur, command: Mapping[str, Any]) -> dict[str, Any]:
    """把 command 补成 transport 能直接吃的形状。

    三个下划线开头的键是**合成键**,不是表里的列 —— ``insert_command``
    对表外字段是显式报错的,所以它们不可能被误写回库。
    """
    title, body = load_frozen_body(
        cur,
        article_revision_id=str(command["article_revision_id"]),
        article_hash=str(command["article_hash"]),
    )
    media = _transport.resolve_provider_media(cur, str(command["public_media_key"]))
    enriched = dict(command)
    enriched["_title"] = title
    enriched["_frozen_body"] = body
    enriched["_provider_media_ids"] = [media["provider_media_id"]]
    return enriched


# ══════════════════════════════════════════════════════════════════════════
# 派发
# ══════════════════════════════════════════════════════════════════════════
async def dispatch_pending(
    *, limit: int = BATCH, provider_call: "_dispatch.ProviderCall | None" = None,
) -> dict[str, Any]:
    """领取并派发。``provider_call=None`` ⇒ 走生产登记表(见模块 docstring)。"""
    from db.connection import get_db

    claim_token = "defgeo-pub-" + secrets.token_hex(8)
    results: list[dict[str, Any]] = []
    deferred = 0

    with get_db() as conn:
        claimed = _store.claim_outbox(conn.cursor(), claim_token=claim_token, limit=int(limit))
    if not claimed:
        return {"dispatched": 0, "deferred": 0, "quarantined": 0, "items": []}

    quarantined = 0
    for row in claimed:
        token = str(row.get("claim_token") or claim_token)
        try:
            outcome = await _dispatch_one_row(row, provider_call=provider_call)
        except _transport.TransportNotConfigured as exc:
            # 🔴 一次外调都没发生 —— 放回队列,不动状态、不动钱。
            #
            # ═══════════════════════════════════════════════════════════
            # 🔴 [B-3 = Codex P1-2] 但**不能无限放**
            # ═══════════════════════════════════════════════════════════
            # 改之前这一条是无条件 ``_defer``:通道压根没配置时,
            # 每 20 秒延后 120 秒、永远延后下去 —— 客户的算力**无限期**冻着,
            # 而运营侧没有任何一个可执行的下一步(outbox 永远 pending,
            # 于是收敛器⑦ 的 ``o.status = 'needs_review'`` 谓词永不成立,
            # 那条「零外调 ⇒ 退款」的活路也永远走不到)。
            #
            # 达上限 ⇒ 收成 ``needs_review``:**资金态一格不动**
            # (队列失败与钱向是两件事),但队列进运维面,
            # 同时让收敛器⑦ 够得着它 —— 那条路会去做真正的退款裁决。
            # 🔴 [E1-2 = Codex 二审 §9 P2] **先写、成了才计数**。
            #    原来是先 ``+= 1`` 再写:租约过期被别人重领时写入命中 0 行,
            #    统计里却多了一笔"延后/转人工",运维看到的是一件没发生的事。
            #    计数与写入必须同命运。
            if int(row.get("attempt_count") or 0) >= MAX_ATTEMPTS:
                quarantined += int(_quarantine_row(int(row["id"]), token, str(exc)))
            else:
                deferred += int(_defer(int(row["id"]), token, str(exc)))
            continue
        except Exception as exc:                          # noqa: BLE001
            logger.error("[defgeo-publish] outbox=%s 派发异常:%s", row["id"], exc,
                         exc_info=True)
            _settle_row(int(row["id"]), token, int(row.get("attempt_count") or 0),
                        f"{type(exc).__name__}: {exc}")
            continue
        results.append(outcome)
    return {"dispatched": len(results), "deferred": deferred,
            "quarantined": quarantined, "items": results}


async def _dispatch_one_row(
    row: Mapping[str, Any], *, provider_call: "_dispatch.ProviderCall | None",
) -> dict[str, Any]:
    from db.connection import get_db

    command_id = str(row["publish_command_id"])

    with get_db() as conn:
        cur = conn.cursor()
        command = _store.get_command_any_tenant(cur, publish_command_id=command_id)
        if command is None:
            raise RuntimeError(f"outbox 指向的 command {command_id} 不存在")

        # 已经是终态的,只收队列不再派发(重放安全)。
        if str(command["command_state"]) in _store.TERMINAL_COMMAND_STATES:
            _store.settle_outbox(cur, outbox_id=int(row["id"]),
                                 claim_token=str(row["claim_token"]),
                                 status="dispatched", last_error=None)
            return {"commandId": command_id, "skipped": "already_terminal"}

        # 🔴 就绪自检在 claim 之后、marker 之前。不 ready 就一步都不往下走。
        call = provider_call
        if call is None:
            call = _transport.resolve().call        # 不 ready ⇒ TransportNotConfigured

        enriched = _enrich(cur, command)

        outcome = await _dispatch.dispatch_once(
            cur,
            command=enriched,
            frozen_body=str(enriched["_frozen_body"]),
            provider_call=call,
            # 🔴 [E1-1] 把**我这一次租约**的凭据交给 marker 那条 CAS。
            #    租约过期被别人重领之后,这条 UPDATE 0 行 —— 一次都不外发。
            claim_token=str(row["claim_token"]),
            # 🔴 窗口③:marker 落盘**在外调之前**。
            on_external_start_committed=conn.commit,
        )
        # 🔴 [B-2] 收尾必须带**自己那一次租约的** token:租约过期后这条已经被
        #    别人重新领走时,迟到的收尾 0 行放弃,不许盖掉新持有者的进度。
        _store.settle_outbox(cur, outbox_id=int(row["id"]),
                             claim_token=str(row["claim_token"]),
                             status="dispatched", last_error=None)

    logger.info("[defgeo-publish] %s 派发完成:%s / %s(%s)",
                command_id, outcome.canonical_state, outcome.funding_state, outcome.note)
    return {
        "commandId": command_id,
        "canonicalState": outcome.canonical_state,
        "fundingState": outcome.funding_state,
        "legalBlocked": outcome.legal_blocked,
        "providerCalls": outcome.provider_calls,
    }


def _defer(outbox_id: int, claim_token: str, reason: str) -> bool:
    """返回 **这一行是不是真的被我延后了**。

    🔴 [E1-2 = Codex 二审 §9 P2] 返回 bool 而不是 None:租约过期被别人重领时,
       ``defer_outbox`` 的 fencing 谓词命中 0 行 —— 什么都没发生。
       调用方拿不到这个事实就会照样 ``deferred += 1``,把一件**没做成的事**
       记进运维统计。计数与写入必须同命运。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            return bool(_store.defer_outbox(
                conn.cursor(), outbox_id=outbox_id, claim_token=claim_token,
                seconds=NOT_READY_BACKOFF_SECONDS, last_error=reason))
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-publish] outbox=%s 延后失败(交租约超时兜底):%s",
                       outbox_id, exc)
        return False


def _quarantine_row(outbox_id: int, claim_token: str, reason: str) -> bool:
    """[B-3] 通道达 attempt 上限:收进运维面。**资金态一格不动**。

    钱向的裁决不在这里 —— 它在收敛器⑦(``o.status = 'needs_review'`` 是它的
    候选谓词之一)。这里只负责让"自动跑不通"这件事**停止无限延期**并变得可见。

    🔴 [E1-2] 同 :func:`_defer`:返回真实写入结果,0 行不许计数。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            ok = bool(_store.settle_outbox(
                conn.cursor(), outbox_id=outbox_id, claim_token=claim_token,
                status="needs_review",
                last_error=f"外发通道持续不可用,已达 {MAX_ATTEMPTS} 次:{reason}"[:2000],
            ))
        if ok:
            logger.error("[defgeo-publish] outbox=%s 外发通道 %d 次仍不可用 → 转人工"
                         "(资金保持冻结,交收敛器裁定钱向):%s",
                         outbox_id, MAX_ATTEMPTS, reason)
        else:
            logger.warning("[defgeo-publish] outbox=%s 转人工 0 行 —— 租约已易主,"
                           "本次不计入 quarantined", outbox_id)
        return ok
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-publish] outbox=%s 转人工失败:%s", outbox_id, exc)
        return False


def _settle_row(outbox_id: int, claim_token: str, attempt_count: int, reason: str) -> None:
    """派发异常时收队列。次数没耗尽 ⇒ 放回重试;耗尽 ⇒ ``needs_review`` 进运维面。

    🔴 **不动 command 的资金状态**。队列失败与钱向是两件事:
       command 是否已 external-start 只有 ``dispatch_once`` 知道,
       在这里顺手改钱会把「没调过」和「调过但没记上」混成一件事。
    """
    from db.connection import get_db

    terminal = attempt_count >= MAX_ATTEMPTS
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if terminal:
                _store.settle_outbox(cur, outbox_id=outbox_id, claim_token=claim_token,
                                     status="needs_review", last_error=reason[:2000])
            else:
                _store.defer_outbox(cur, outbox_id=outbox_id, claim_token=claim_token,
                                    seconds=NOT_READY_BACKOFF_SECONDS,
                                    last_error=reason[:2000])
    except Exception as exc:                              # noqa: BLE001
        logger.warning("[defgeo-publish] outbox=%s 收尾失败:%s", outbox_id, exc)


# ══════════════════════════════════════════════════════════════════════════
# 回执轮询(窗口③ → ④ 之间那一段)
# ══════════════════════════════════════════════════════════════════════════
def poll_pending_outcomes(*, limit: int = 20, poll=None) -> dict[str, Any]:
    """向上游核对**已外调、还没终态**的命令。

    §12.3 逐字:「provider 不支持幂等查询时进入人工核验,**不能用自动重传赌结果**」。
    所以本函数永远只**读**上游结果,一次都不重传;查不到就什么都不做,
    交给 ``reconciler._external_started_without_outcome`` 在超时后转核验。

    ``poll`` 与 ``dispatch_pending`` 的 ``provider_call`` 同款:判据传参,生产不传。
    """
    from db.connection import get_db

    reader = poll or _synced_order_outcome
    touched: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    with get_db() as conn:
        cur = conn.cursor()
        rows = _store.commands_awaiting_outcome(cur, limit=int(limit))
        for command in rows:
            ref = command.get("provider_order_ref")
            if not ref:
                continue
            fact = reader(cur, str(ref))
            if fact is None:
                _store.bump_status(cur, publish_command_id=str(command["publish_command_id"]),
                                   provider_last_polled_at=_now(cur))
                continue
            canonical, _direction = _settle.project(fact)
            if canonical == str(command["canonical_publication_state"]):
                _store.bump_status(cur,
                                   publish_command_id=str(command["publish_command_id"]),
                                   provider_last_polled_at=_now(cur))
                continue
            _store.bump_status(
                cur,
                publish_command_id=str(command["publish_command_id"]),
                canonical_publication_state=canonical,
                raw_state_source_table=fact.source_table,
                raw_state_source_column=fact.source_column,
                raw_state_value=str(fact.raw_value),
                url_verification_state=fact.url_verification_state,
                url_availability_state=fact.url_availability_state,
                provider_last_polled_at=_now(cur),
            )
            touched.append({
                "commandId": str(command["publish_command_id"]),
                "canonicalState": canonical,
            })
    return {"polled": len(rows), "advanced": len(touched), "items": touched}


def _now(cur):
    cur.execute("SELECT NOW() AS n")
    row = cur.fetchone()
    return row["n"] if isinstance(row, Mapping) else row[0]


def _synced_order_outcome(cur, order_ref: str):
    """从**现役同步表**读上游终态。零外调 —— 同步本身由现役 mhz 回流链负责。

    ``mhz_synced_orders.status`` 是 integer(census 实测),
    ``publish_settlement.RAW_STATE_CENSUS`` 里那张 int 表就是它的分母。
    """
    cur.execute(
        "SELECT status FROM mhz_synced_orders WHERE order_sn = %s "
        "ORDER BY id DESC LIMIT 1",
        (order_ref,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    raw = row["status"] if isinstance(row, Mapping) else row[0]
    if raw is None:
        return None
    return _settle.PublicationFacts(
        source_table="mhz_synced_orders", source_column="status", raw_value=int(raw),
        external_start_recorded=True,
        url_verification_state=None, url_availability_state=None, legal_rule_hit=False,
    )


# ══════════════════════════════════════════════════════════════════════════
# 收敛器
# ══════════════════════════════════════════════════════════════════════════
async def reconcile_tick(*, limit: int = 50) -> dict[str, Any]:
    """跑一轮 ``reconcile_once``。**一个事务**,里面每条 action 都是 CAS。"""
    from db.connection import get_db

    with get_db() as conn:
        actions = await _recon.reconcile_once(conn.cursor(), limit=int(limit))
    if actions:
        logger.info("[defgeo-publish] 收敛 %d 条:%s", len(actions),
                    [(a.command_id, a.kind) for a in actions])
    return {
        "actions": len(actions),
        "kinds": sorted({a.kind for a in actions}),
        "items": [{"commandId": a.command_id, "kind": a.kind,
                   "before": a.before, "after": a.after} for a in actions],
    }


# ══════════════════════════════════════════════════════════════════════════
# APScheduler 入口 —— 与 run_executor 的 sync 包装同形
# ══════════════════════════════════════════════════════════════════════════
def _run(coro) -> Any:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:                                 # noqa: BLE001
            pass


def dispatch_pending_sync() -> dict[str, Any]:
    try:
        result = _run(dispatch_pending())
    except Exception as exc:                              # noqa: BLE001 —— job 不许把调度线程带走
        logger.error("[defgeo-publish] dispatch tick 异常:%s", exc, exc_info=True)
        return {"dispatched": 0, "deferred": 0, "quarantined": 0, "items": []}
    try:
        poll_pending_outcomes()
    except Exception as exc:                              # noqa: BLE001
        logger.error("[defgeo-publish] 回执轮询异常:%s", exc, exc_info=True)
    return result


def reconcile_tick_sync() -> dict[str, Any]:
    try:
        return _run(reconcile_tick())
    except Exception as exc:                              # noqa: BLE001
        logger.error("[defgeo-publish] reconcile tick 异常:%s", exc, exc_info=True)
        return {"actions": 0, "kinds": [], "items": []}


def census() -> dict[str, Any]:
    return {
        "consumerVersion": CONSUMER_VERSION,
        "consumes": _store.OUTBOX_TABLE,
        "schedulerEntrypoints": ["dispatch_pending_sync", "reconcile_tick_sync"],
        "transactionBoundaries": [
            "A: claim + 法律门 + external-start marker + COMMIT",
            "(外调 · 不持事务)",
            "B: canonical outcome + 收 outbox + COMMIT",
        ],
        "maxAttempts": MAX_ATTEMPTS,
        "notReadyBackoffSeconds": NOT_READY_BACKOFF_SECONDS,
    }
