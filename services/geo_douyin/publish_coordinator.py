"""协调器 · 一篇一账号发布 command 的**单事务**落库(规格 02 §7.3 / §8.2)。

## §8.2 的资金顺序,逐步落在这里

```
claim request                                   ← contract_idempotency.claim_request
→ 服务端解析并冻结 payer/principal/actor        ← contract_funding.resolve_settlement_authority
→ 校验身份、资格、频控、价格                     ← publish_command / account_eligibility
→ 同一显式 DB 事务内 capacity reservation / 订单项
→ 逐项 freeze,持久化完整 handle
→ commit DB;任一步异常整体 rollback
→ durable worker external-start                 ← commit 之后才允许
```

🔴 **provider 只能在 commit 之后开始**。本模块**不**发起任何外部调用 ——
它的职责在 commit 那一刻结束。谁在这里加一句 provider 调用,
就把"异常整体 rollback"变成了"已经发出去但库回滚了"。

## 为什么整个 command 只用一个 cursor

规格 §8.2:「入口 claim、slot/频控 reservation、drafts/orders/items 和全部 freeze
可同一 PG 事务提交」。自开连接的 helper 是这条链上最容易出现
「入口 claim 成功但后续 rollback,幂等记录留下孤儿」的地方 —— 本模块所有 SQL
都走调用方传进来的同一个 cursor,不自己 connect、不自己 commit。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional, Sequence

from services.geo_douyin.account_eligibility import MEDIA_TYPE_SVIDEO
from services.geo_douyin.artifact_prepare import assert_artifact_matches
from services.geo_douyin.contract_funding import (
    BILLING_MODE_FREEZE_PER_ITEM, assert_authority_shape, assert_reserved_matches_price,
)
from services.geo_douyin.contract_pricing import OPERATION_PUBLISH, assert_price_unchanged
from services.geo_douyin.contract_seams import ARTICLE_TYPE_IMAGE_NOTE
from services.geo_douyin.publish_command import (
    CapacityExceeded, PublishAttemptConflict, aggregate_account_demand, assert_one_to_one,
    business_capacity_date, is_retryable, reserve_daily_capacity, _assert_in_transaction,
)


class ProviderCallAttempted(AssertionError):
    """协调器内出现了外部调用。这是**编程错误**,不是运行时异常。

    留一个专门的异常类型,是为了让「provider 只能在 commit 之后」这条
    可以被判据打成硬断言,而不是靠 code review 记得。
    """


#: 视频稿占位。**订单的 article_id 必须是 `-draft.id`,不是 `-geo_post_id`。**
#:
#: 🔴 [返工 2026-08-18 · Codex P0-05] 原实现写 `-abs(geo_post_id)`。
#:    现役归属解析器(`db/meijiehezi_db.resolve_authoritative_brand_id`)对
#:    **任何负数**都当成 draft id 去查 `mhz_short_video_drafts`:
#:
#:        SELECT geo_post_id FROM mhz_short_video_drafts WHERE id = -article_id
#:
#:    于是 `-geo_post_id` 会碰到**编号恰好相同的另一张草稿**,解析出
#:    别人的 geo_post → 别人的 brand。这不是"少了个字段",是**跨租户串号**:
#:    两个命名空间(post id / draft id)被塞进同一个负数空间,而解析器只认后者。
#:    正确的老链形态就在 `api/meijiehezi_api.py:2391`:先建 draft、再 `-draft_id`。
_INSERT_DRAFT = """
INSERT INTO mhz_short_video_drafts (
    user_id, brand_id, title, content, keyword, video_url, cover_image,
    customer_name, article_type, image_urls, geo_post_id
) VALUES (
    %(user_id)s, %(brand_id)s, %(title)s, %(content)s, '', '', '',
    -- 🔴 [第 3 棒 · Codex R2 P0-1] `article_type` 是 **3**(图文笔记),
    --    不是 1(视频直发)。上一版写死 1 + `image_urls` 恒空 ⇒ 草稿层就
    --    把一条图文笔记记成了"没有视频地址的视频直发单";而 `article_type`
    --    是发布渠道那边**决定走哪条投放线**的字段,不是一个展示标签。
    %(customer_name)s, %(article_type)s, %(image_urls)s, %(geo_post_id)s
)
RETURNING id
"""

_INSERT_ORDER = """
INSERT INTO mhz_publish_orders (
    user_id, article_id, article_title, status, total_items, total_cost_points,
    command_request_id, payer_user_id, actor_user_id, payer_policy_snapshot,
    created_at, updated_at
) VALUES (
    %(user_id)s, %(article_id)s, %(article_title)s, 'pending', 1, %(cost_points)s,
    %(command_request_id)s, %(payer_user_id)s, %(actor_user_id)s,
    %(payer_policy_snapshot)s::jsonb, now(), now()
)
RETURNING id
"""

_INSERT_ITEM = """
INSERT INTO mhz_publish_order_items (
    order_id, user_id, media_id, media_name, media_type, status, cost_points,
    command_request_id, source_geo_post_id, source_post_revision_id,
    prepared_artifact_id, manifest_hash, item_request_id, task_ref,
    attempt_root_id, attempt_no, retry_of_item_id, status_version,
    price_snapshot, publish_price_fingerprint, feature_code,
    billing_mode, settlement_authority, settlement_status,
    organization_charge_link_id, freeze_id, freeze_table, payer_user_id,
    reserved_amount, physical_split_snapshot,
    capacity_date, capacity_timezone, capacity_state, created_at
) VALUES (
    %(order_id)s, %(user_id)s, %(media_id)s, %(media_name)s, %(media_type)s,
    'queued', %(cost_points)s,
    %(command_request_id)s, %(source_geo_post_id)s, %(source_post_revision_id)s,
    %(prepared_artifact_id)s, %(manifest_hash)s, %(item_request_id)s, %(task_ref)s,
    %(attempt_root_id)s, %(attempt_no)s, %(retry_of_item_id)s, 1,
    %(price_snapshot)s::jsonb, %(publish_price_fingerprint)s, %(feature_code)s,
    %(billing_mode)s, %(settlement_authority)s, 'frozen',
    %(organization_charge_link_id)s, %(freeze_id)s, %(freeze_table)s, %(payer_user_id)s,
    %(reserved_amount)s, %(physical_split_snapshot)s::jsonb,
    %(capacity_date)s, %(capacity_timezone)s, 'reserved', now()
)
RETURNING id
"""


def _lock_previous_attempts(cur, items: Sequence[Mapping[str, Any]]) -> dict[int, dict]:
    """Lock version creation before capacity/funds, including when no row exists yet.

    The existing unique live-version index remains the final authority. A released
    failure is retired only together with its replacement, never on a browser reset.
    Sorted locks keep multi-item submissions in a stable order; row locks also fence
    the worker's settlement updates. H0: version identity and money conservation.
    """
    _assert_in_transaction(cur)
    by_revision = {int(item["post_revision_id"]): item for item in items}
    previous: dict[int, dict] = {}
    for revision_id in sorted(by_revision):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"geo_image_note_publish_revision:{revision_id}",))
        cur.execute("""
            SELECT id, source_geo_post_id, command_request_id, attempt_root_id,
                   attempt_no, status, settlement_status, terminal_at, availability
              FROM mhz_publish_order_items
             WHERE source_post_revision_id=%s
               AND (terminal_at IS NULL OR availability IS NULL OR availability='active')
             FOR UPDATE
        """, (revision_id,))
        row = cur.fetchone()
        if not row:
            continue
        row = dict(row)
        if (int(row.get("source_geo_post_id") or 0) != int(by_revision[revision_id]["geo_post_id"])
                or not row.get("attempt_root_id") or not row.get("attempt_no")):
            raise PublishAttemptConflict(
                "已有发布记录的版本关联需要核对，请查看发布记录并联系管理员处理，勿重复提交。",
                command_request_id=str(row.get("command_request_id") or ""))
        if not is_retryable({**row, "state": row["status"]}):
            raise PublishAttemptConflict(
                "这个版本已有发布记录，请先查看提交结果；渠道回执和算力结算未确认前无需再次提交。",
                command_request_id=str(row.get("command_request_id") or ""))
        previous[revision_id] = row
    return previous


def materialize_command(
    cur,
    *,
    command_request_id: str,
    identity: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    resolved_prices: Mapping[str, Mapping[str, Any]],
    artifacts: Mapping[str, Mapping[str, Any]],
    account_names: Mapping[int, str],
    daily_limit: int,
    settlement: Mapping[str, Any],
    freeze_fn: Callable[..., Mapping[str, Any]],
    feature_code: str = "media_proxy_publish",
    timezone: Optional[str] = None,
) -> dict[str, Any]:
    """在**调用方的事务**里把一个 command 落成 N 个「一篇一 order 一 item」。

    参数里没有任何 provider 客户端 —— 这是刻意的(见模块 docstring)。

    `freeze_fn(payer_user_id, amount, task_ref)` 由调用方注入,返回完整 direct handle
    或 organization charge handle。注入而不是内部 import,是为了让判据能用
    计数器精确断言「每项恰好一次 freeze」。
    """
    assert_one_to_one(items)
    previous_attempts = _lock_previous_attempts(cur, items)

    tz = timezone or "Asia/Shanghai"
    capacity_date = business_capacity_date(cur, timezone=tz)

    # ① 先按账号聚合再预占 —— 逐项独立预占会在批内自己超额(§7.3)
    demand = aggregate_account_demand(items)
    for media_id, needed in sorted(demand.items()):
        reserve_daily_capacity(
            cur, media_id=int(media_id), needed=int(needed), daily_limit=int(daily_limit),
            capacity_date=capacity_date, media_type=MEDIA_TYPE_SVIDEO, timezone=tz)

    out_items: list[dict[str, Any]] = []
    freeze_calls = 0

    for item in items:
        item_key = str(item["item_request_id"])
        priced = resolved_prices.get(item_key)
        if priced is None:
            raise ValueError(f"item {item_key} 缺少服务端定价")

        # ② 逐项价格确认锁:先验链、再验漂移(跨链传入必须报对象身份)
        assert_price_unchanged(
            expected_fingerprint=str(item["expected_price_fingerprint"]),
            actual_fingerprint=str(priced["publish_price_fingerprint"]),
            operation=OPERATION_PUBLISH)

        # ③ artifact 身份:revision / manifest 任一不符即 409,零资金零外调
        artifact = artifacts.get(item_key)
        if artifact is None:
            raise ValueError(f"item {item_key} 缺少已准备的发布素材")
        assert_artifact_matches(artifact,
                                post_revision_id=int(item["post_revision_id"]),
                                manifest_hash=str(item["manifest_hash"]))

        final_points = int(priced["final_price_points"])

        # ④ 先铸视频稿,再建单。顺序不能反 —— `article_id = -draft_id` 里的
        #    draft_id 只有 INSERT 之后才存在。草稿必须带 `geo_post_id`:
        #    它是服务端事后反查权威归属的**唯一**通道(migration 033 那一列)。
        cur.execute(_INSERT_DRAFT, {
            "user_id": int(identity["payer_user_id"]),
            "brand_id": item.get("brand_id"),
            "title": str(item.get("title") or "GEO 图文笔记"),
            "content": str(item.get("body_text") or ""),
            "customer_name": str(item.get("customer_name") or ""),
            "image_urls": str(item.get("image_urls") or ""),
            "article_type": ARTICLE_TYPE_IMAGE_NOTE,
            "geo_post_id": int(item["geo_post_id"]),
        })
        draft_id = int(dict(cur.fetchone())["id"])

        # ⑤ 建单:现役图文订单仍经既有订单引擎;article_id 用负 **draft** id 约定,
        #    **不能**把 geo post id 直接传给会查 articles 的 create_order(§3.7)
        cur.execute(_INSERT_ORDER, {
            "user_id": int(identity["payer_user_id"]),
            "article_id": -draft_id,
            "article_title": str(item.get("title") or "GEO 图文笔记"),
            "cost_points": final_points,
            "command_request_id": str(command_request_id),
            "payer_user_id": int(identity["payer_user_id"]),
            "actor_user_id": int(identity["actor_user_id"]),
            "payer_policy_snapshot": json.dumps(
                dict(identity.get("payer_policy_snapshot") or {}), ensure_ascii=False),
        })
        order_id = int(dict(cur.fetchone())["id"])

        # ⑥ 逐项 freeze —— 每项**恰好一次**。
        #    task_ref 在这里算一次、随 item 落库一次、commit/release 时读回来用,
        #    **不再现拼**(P1-6:现拼出来的与冻结时用的不是同一个值)。
        task_ref = f"imgnote:{command_request_id}:{item_key}"
        handle = dict(freeze_fn(
            payer_user_id=int(settlement["payer_user_id"]),
            amount=final_points,
            task_ref=task_ref,
        ) or {})
        freeze_calls += 1

        assert_reserved_matches_price(
            authority=str(settlement["authority"]),
            reserved_amount=int(handle.get("reserved_amount") or 0),
            final_price_points=final_points)

        row = {
            "settlement_authority": settlement["authority"],
            "organization_charge_link_id": handle.get("organization_charge_link_id"),
            "freeze_id": handle.get("freeze_id"),
            "freeze_table": handle.get("freeze_table"),
            "payer_user_id": handle.get("payer_user_id"),
            "reserved_amount": handle.get("reserved_amount"),
        }
        assert_authority_shape(row)

        previous = previous_attempts.get(int(item["post_revision_id"]))
        if previous:
            # The worker historically left these columns NULL even after release.
            # Preserve its failed/released outcome; replacing (not retracting) the
            # old attempt and inserting the child must commit or roll back together.
            cur.execute("""
                UPDATE mhz_publish_order_items
                   SET terminal_at=COALESCE(terminal_at,last_authoritative_event_at,now()),
                       availability='replaced', status_version=COALESCE(status_version,0)+1
                 WHERE id=%s
            """, (previous["id"],))

        cur.execute(_INSERT_ITEM, {
            "order_id": order_id,
            "user_id": int(identity["payer_user_id"]),
            "media_id": int(item["media_id"]),
            "media_name": str(account_names.get(int(item["media_id"]), "")),
            "media_type": MEDIA_TYPE_SVIDEO,
            "cost_points": final_points,
            "command_request_id": str(command_request_id),
            "source_geo_post_id": int(item["geo_post_id"]),
            "source_post_revision_id": int(item["post_revision_id"]),
            "prepared_artifact_id": int(artifact["prepared_artifact_id"]),
            "manifest_hash": str(item["manifest_hash"]),
            "item_request_id": item_key,
            "task_ref": task_ref,
            "attempt_root_id": previous["attempt_root_id"] if previous else f"{command_request_id}:{item_key}",
            "attempt_no": int(previous["attempt_no"]) + 1 if previous else 1,
            "retry_of_item_id": previous["id"] if previous else None,
            "price_snapshot": json.dumps(dict(priced.get("price_snapshot") or {}),
                                         ensure_ascii=False),
            "publish_price_fingerprint": str(priced["publish_price_fingerprint"]),
            "feature_code": feature_code,
            "billing_mode": BILLING_MODE_FREEZE_PER_ITEM,
            "settlement_authority": settlement["authority"],
            "organization_charge_link_id": handle.get("organization_charge_link_id"),
            "freeze_id": handle.get("freeze_id"),
            "freeze_table": handle.get("freeze_table"),
            "payer_user_id": handle.get("payer_user_id"),
            "reserved_amount": handle.get("reserved_amount"),
            "physical_split_snapshot": json.dumps(
                dict(handle.get("physical_split_snapshot") or {}), ensure_ascii=False),
            "capacity_date": capacity_date,
            "capacity_timezone": tz,
        })
        item_id = int(dict(cur.fetchone())["id"])
        if previous:
            cur.execute("""
                UPDATE mhz_publish_order_items
                   SET replaced_by_source='mhz_publish_order_items', replaced_by_source_id=%s
                 WHERE id=%s
            """, (str(item_id), previous["id"]))
        out_items.append({
            "item_request_id": item_key,
            "geo_post_id": int(item["geo_post_id"]),
            "media_id": int(item["media_id"]),
            "short_video_draft_id": draft_id,
            "order_id": order_id,
            "order_item_id": item_id,
            "status": "queued",
            "settlement_status": "frozen",
            "final_price_points": final_points,
            "publish_price_fingerprint": str(priced["publish_price_fingerprint"]),
        })

    if freeze_calls != len(items):
        # 不可能走到,但写出来让"每项恰好一次 freeze"成为**代码里的**断言
        raise AssertionError(
            f"freeze 次数 {freeze_calls} 与项数 {len(items)} 不符 —— 资金守恒破裂")

    # ── [#184 d2] 把下单结果回写到**源头作品**(同一事务)──────────────
    # 🔴 今天这三列在生产上全空:内容被发出去了,而它真正的主人在创作中心看
    #    还是「未发布」。回写机制早就建好(`bind_publish_result` + /publish-result),
    #    只是新链一根线都没接 —— 本仓「死函数 = 复审漏接线」的又一例。
    #
    # 🔴 这里写的是**观测列**,不是 `published_url`:URL 由既有回填器按
    #    `items.source_geo_post_id` 从供应商回执投影(contract_worker 那条 20s 链),
    #    它带着矛盾回执与结算守卫。**同一列只留一个写入方**。
    #
    # 🔴 守卫在 `bind_publish_submission` 的 WHERE 里:已经 published 的作品
    #    不许被一笔重放的提交倒回 publishing。
    from db.geo_douyin_db import bind_publish_submission

    # 🔴 `order_id` 是**循环内**变量:本链每一项各建一个订单(既有判据
    #    `test_command_materializes_one_order_and_one_freeze_per_item` 断言"两篇各一单")。
    #    在循环外拿它 = 把每一篇都绑到**最后一单**。所以按项取它自己的 order_id。
    # 🔴 单次命令里「一篇作品恰好对应一个账号」是硬约束
    #    (`publish_command.OneToOneViolation`,按 revision 判重),
    #    所以一篇在本次命令里只会有一个订单一项。分组仍按篇写,
    #    是为了让这段代码不依赖那条约束 —— 约束哪天松了,这里也不会悄悄绑错单。
    #    跨多次命令时本列取**最近一次**提交的单:它是「找得到入口」,
    #    不作对账依据(对账走 items.source_geo_post_id 那条线)。
    _by_post: dict[int, dict] = {}
    for it in out_items:
        _p = int(it["geo_post_id"])
        slot = _by_post.setdefault(_p, {"order_id": int(it["order_id"]), "items": []})
        slot["items"].append(int(it["order_item_id"]))
    for _post_id, _slot in _by_post.items():
        bind_publish_submission(cur, post_id=_post_id,
                                order_id=int(_slot["order_id"]),
                                item_ids=_slot["items"])

    return {
        "command_id": f"pubcmd_{command_request_id}",
        "capacity_date": capacity_date,
        "capacity_timezone": tz,
        "items": out_items,
        "summary": {
            "total_items": len(out_items),
            "total_price_points": sum(i["final_price_points"] for i in out_items),
        },
    }
