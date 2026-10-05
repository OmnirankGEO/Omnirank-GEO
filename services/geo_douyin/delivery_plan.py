"""图文交付计划读取(规格 02 §4.2 · 裁定 P0-4 改名后的唯一读侧)。

## 本模块的边界:它**只读**,而且只从「已签发的 active contract revision」读

规格 4.1/4.2 的硬约束,逐条落在这里:

  · 没有已签发 ordered allocation 时,plan 处于「准备中」——
    **不得** builder 自创 first-N / round-robin,**不得** fallback 到 brand latest topic,
    **不得**回退 `content_plan` 的 brand+keyword 缺口算法。
  · `0` 容量必须**可与「尚未编译/编译失败」区分**:前者是 active revision + delivery_count=0
    + 空 slots + `channel_allocated_capacity=0`;后者是 `activation_pending`。
  · `global_ordinal` 是不可变身份;`channel_display_index` 只用于显示,**绝不作 key**。

## 为什么现在只有降级分支

dormant slot sidecar 的激活要先过窄 RFC(工单 §1.4「批了才许动,直接升格 = NO-GO」),
RFC 见 `docs/AI-CONTEXT/RFC_GEO_IMAGE_NOTE_SLOT_SIDECAR_ACTIVATION_2026-08-17.md`。
在 Review 批复之前:

  · `GEO_IMAGE_NOTE_CONTRACT_ENABLED` 默认 False;
  · 本模块对 `geo_article_*` 六表**零读零写**;
  · 一律返回 `activation_pending` + 可执行下一步。

这不是"占位没写完",是 RFC §3 里已经写明并承诺的降级形态 —— 用户看到
「制作计划准备中 · 重试」,零 claim、零资金、零制作。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("GEO-Douyin-DeliveryPlan")

CHANNEL_IMAGE_NOTE = "douyin_image_note"
CHANNEL_ARTICLE = "article"

# 图文渠道专属 flag。刻意**不复用** ARTICLE_PLAN_CANARY_UPSERT_ENABLED ——
# 后者的 feature_flag_blockers() 硬依赖两个未签发常量
# (CANARY_THRESHOLD_POLICY_VERSION='UNSIGNED' / CANARY_NEW_QUOTE_NOT_BEFORE=None),
# 复用它要么被 blocker 挡死、要么得去动未签发策略。详见 RFC R1。
FLAG_CONTRACT_ENABLED = "GEO_IMAGE_NOTE_CONTRACT_ENABLED"

# quote 的显式 enrollment 标记(写在既有 quotes.article_plan_writing_mode 上,不新增列)
ENROLLMENT_MODE = "image_note_contract"


class SidecarNotApproved(RuntimeError):
    """RFC 未批复时试图走 sidecar 读写路径 —— 这是编程错误,不是用户错误。"""


def contract_lane_enabled() -> bool:
    raw = os.getenv(FLAG_CONTRACT_ENABLED)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _activation_pending_payload(quote_id: int, brand: Optional[dict], reason_code: str) -> dict:
    """降级响应。

    🔴 三条纪律都在这个 payload 里:
      ① `channel_allocated_capacity` 用 **None** 而不是 0 —— 0 是「已签发且确实是零容量」
         的合法读数,拿它表示「还不知道」会把两件事混成一件,正是规格要求区分的那对。
      ② 必带可执行下一步(统一错误合同 §12:actions 至少一项,type 用现役 builder 认的
         `retry|nav|contact|dismiss|api`,不写会被 builder 丢弃的 `kind`)。
      ③ 文案是人话,不露 `slot` / `revision` / `sidecar` 这些工程术语(01 §1 画像口径)。
    """
    return {
        "quote_id": int(quote_id),
        # 降级态**必须**是 None:此时一个槽位都没读到,报一个修订号等于编造。
        "contract_revision_id": None,
        "brand": {"id": brand.get("id"), "name": brand.get("name")} if brand else None,
        "state": "activation_pending",
        "reason_code": reason_code,
        "summary": {
            "channel_allocated_capacity": None,
            "quote_total_capacity": None,
            "ready": 0,
            "in_progress": 0,
            "open": 0,
        },
        "slots": [],
        "user_message": "制作计划准备中",
        "repair_hint": "稍后刷新;若长时间未就绪请联系管理员核对这张报价的开通状态",
        "actions": [
            {"id": "refresh_plan", "label": "刷新", "type": "retry"},
            {"id": "contact_admin", "label": "联系管理员核对", "type": "contact"},
        ],
        "next_action": "refresh_plan",
        "retryable": True,
    }


def _sidecar_schema_ready(cur: Any = None) -> bool:
    """035 的五列是否都在。只读 information_schema,不碰业务表。"""
    required = {"delivery_channel", "media_mix_bucket", "fulfillment_state",
                "geo_post_id", "topic_ref"}
    own = None
    if cur is None:
        try:
            from db.connection import get_connection
        except Exception:
            return False
        own = get_connection()
        cur = own.cursor()
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_name = 'geo_article_delivery_slots'")
        have = {str(dict(r)["column_name"]) for r in (cur.fetchall() or [])}
    except Exception:
        return False
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
    return required <= have


def build_delivery_plan(*, quote_id: int, brand: Optional[dict], channel: str = CHANNEL_IMAGE_NOTE,
                        cur: Any = None) -> dict:
    """返回该报价在指定渠道的交付计划。

    当前实现只有降级分支(见模块 docstring)。RFC 批复后,active 分支在这里接上:
    从 active contract revision 的 membership + slot fulfillment 聚合,按 channel 分拆,
    每个 global ordinal 恰计一次,保持 `0..capacity` 上限语义。
    """
    if channel not in (CHANNEL_IMAGE_NOTE, CHANNEL_ARTICLE):
        raise ValueError(f"unsupported delivery channel: {channel!r}")

    if not contract_lane_enabled():
        return _activation_pending_payload(quote_id, brand, "CONTRACT_LANE_DISABLED")

    # ── active 分支(RFC R1/R2/R3 已于 2026-08-17 获 Review-CTO 批复)──────────
    #
    # 🔴 这一段是**真 LLM E2E 准备阶段才发现缺的**:WP1 交了写入侧(slot writer)
    #    与四前置,却把读侧留在"RFC 批复后再接上"的注释里。于是把 flag 打开之后
    #    形态是**半边通**:批量创建走得通(它不经过本函数),而页面列不出任何可制作项
    #    —— 用户看到的是 503,不是"没有可做的"。
    #    教训:**开关两侧要一起验**。只验写入侧的"能创建",证明不了用户走得完。
    #
    # 🔴 仍然只读、零写:本函数一个字都不往 geo_article_* 写(RFC R3 唯一写入者
    #    是 services/geo_douyin/delivery_slots.py)。这里连 enroll 都不做 ——
    #    没 enroll 的报价由 activation_blockers 报缺,不在读路径上顺手补。
    from services.geo_douyin.delivery_slots import (
        ARTICLE_CHANNEL_PREDICATE, FULFILLMENT_READY, IMAGE_NOTE_CHANNEL_PREDICATE,
    )

    # 🔴 flag 开了但 schema 没跑到位 → **响亮**失败,不返回空计划。
    #    这条不变式从"RFC 未批"搬家到了"schema 未就绪":两者的危害是同一个 ——
    #    一个空的 slots 列表在界面上等于"这客户没有容量",而真相是"我们还没准备好"。
    #    0 与"不知道"长得一样但含义相反,这是本包一路在守的那条线。
    if not _sidecar_schema_ready():
        raise SidecarNotApproved(
            "GEO_IMAGE_NOTE_CONTRACT_ENABLED 已开启,但交付槽位 sidecar 的 schema "
            "尚未就绪(035 迁移未跑到位)。宁可报错也不返回空计划 —— "
            "空计划会被读成『这张报价没有图文容量』。"
        )

    predicate = (IMAGE_NOTE_CHANNEL_PREDICATE if channel == CHANNEL_IMAGE_NOTE
                 else ARTICLE_CHANNEL_PREDICATE)

    own_conn = None
    if cur is None:
        from db.connection import get_connection

        own_conn = get_connection()
        cur = own_conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT s.delivery_slot_key, s.contract_ordinal, s.quote_id,
                   s.fulfillment_state, s.current_state, s.topic_ref,
                   s.geo_post_id, s.keyword_id, s.blocked_user_message,
                   s.next_action, s.updated_at,
                   -- 🔴 [返工 2026-08-18 · P0-08] CAS 的期望版本必须从这里下发。
                   --    原来页面拿不到它,提交时只能填 0,而新建槽位的
                   --    projection_version 是 1 ⇒ **每一次 claim 的 CAS 都零行**。
                   --    "乐观锁"没有版本号可比时,不是宽松,是恒失败。
                   s.projection_version, s.contract_revision_id
              FROM geo_article_delivery_slots s
             WHERE s.quote_id = %(quote_id)s
               AND {predicate}
             ORDER BY s.contract_ordinal
            """,
            {"quote_id": int(quote_id)},
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.execute(
            "SELECT COUNT(*) AS n FROM geo_article_delivery_slots WHERE quote_id = %(q)s",
            {"q": int(quote_id)},
        )
        total_row = dict((cur.fetchone() or {"n": 0}))
    finally:
        if own_conn is not None:
            own_conn.close()

    # 渠道内显示序号:**派生值**,只用于显示,绝不作 key。
    # 身份永远是 contract_ordinal(全局序号)—— 交错分配时按渠道重编会出现
    # "第 18/12 篇"那种自相矛盾的说法。
    slots = []
    for idx, row in enumerate(rows, start=1):
        state = str(row.get("fulfillment_state") or row.get("current_state") or "")
        slots.append({
            "delivery_slot_key": str(row["delivery_slot_key"]),
            "global_ordinal": int(row["contract_ordinal"]),
            "channel_display_index": idx,
            "fulfillment_state": state,
            "topic_ref": row.get("topic_ref"),
            "geo_post_id": row.get("geo_post_id"),
            "keyword_id": row.get("keyword_id"),
            "occupied_by": row.get("blocked_user_message"),
            "next_action": row.get("next_action"),
            # 提交时原样回传成 `expected_slot_version`。它是"我看到的是第几版",
            # 不是"我想写第几版" —— 服务端据此判断这段时间里有没有别人动过。
            "slot_version": int(row.get("projection_version") or 0),
        })

    ready = sum(1 for s_ in slots if s_["fulfillment_state"] == FULFILLMENT_READY)
    in_progress = sum(1 for s_ in slots
                      if s_["fulfillment_state"] in ("claimed", "generating"))
    # 合同修订号:取槽位行自己的值(同一 quote 的图文槽位同属一个 active revision)。
    # 🔴 原来这里恒 `None` —— 页面因此永远发不出 contract_revision_id,
    #    批次行与 post 行的 `contract_revision_id` 全部落 NULL,
    #    「这批是哪一版合同下的」这个事实在库里根本不存在。
    revision_ids = {int(r["contract_revision_id"]) for r in rows
                    if r.get("contract_revision_id")}
    return {
        "quote_id": int(quote_id),
        "contract_revision_id": (max(revision_ids) if revision_ids else None),
        "brand": {"id": brand.get("id"), "name": brand.get("name")} if brand else None,
        "state": "active",
        "reason_code": None,
        "summary": {
            # 容量 = 该渠道**已签发**的槽位数。0 与 None 的区别照旧:
            # 这里已经签发过(active 分支才走得到),所以 0 是合法读数。
            "channel_allocated_capacity": len(slots),
            "quote_total_capacity": int(total_row.get("n") or 0),
            "ready": ready,
            "in_progress": in_progress,
            "open": max(0, len(slots) - ready - in_progress),
        },
        "slots": slots,
        "user_message": None,
        "repair_hint": None,
        "actions": [{"id": "refresh_plan", "label": "刷新", "type": "retry"}],
        "next_action": "refresh_plan",
        "retryable": True,
    }
