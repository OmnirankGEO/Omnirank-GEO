"""WP1 · 图文渠道交付槽位的**唯一**写入者(窄 RFC R3 · Review 2026-08-17 批准)。

## 唯一写入者是什么意思

图文渠道 slot 的 claim / release / ready **只能**从这里发生。写入一律是
「同一事务内 append event + `projection_version` CAS 更新投影」——
**禁止直接 UPDATE 投影绕过事件真值**(规格 02 §3.1)。
零行 CAS = 冲突,整项零创建、零冻结,返回 `SLOT_CLAIM_CONFLICT`。

## 四个激活前置(RFC R1,缺一不可)

1. `GEO_IMAGE_NOTE_CONTRACT_ENABLED = true`;
2. sidecar schema readiness 通过;
3. 图文侧 schema readiness 通过;
4. 该 quote **显式 enrolled**(`article_plan_writing_mode='image_note_contract'`
   且 `article_plan_enrolled_at IS NOT NULL`)。

**存量 quote 零自动 enroll**:reconciler 不扫全量历史(实测 enrolled = 0/411)。

## 渠道隔离的非对称口径(035 迁移里写明的那条)

`delivery_channel` 无 DEFAULT,历史行为 NULL。因此:

  · 文章侧读 `(delivery_channel IS NULL OR delivery_channel = 'article')`
    —— 035 之前只有文章一种,NULL 即文章,这是向后兼容方向;
  · 图文侧 claim `delivery_channel = 'douyin_image_note'`
    —— 显式相等,NULL **天然不命中**。

两边方向相反是刻意的:图文这条新链宁可少看见,也不能把文章槽位误当成自己的。
"""
from __future__ import annotations

import json
from typing import Any, Final, Mapping, Optional

from services.article_closed_loop_contract import snapshot_hash

CHANNEL_ARTICLE: Final = "article"
CHANNEL_IMAGE_NOTE: Final = "douyin_image_note"

FULFILLMENT_OPEN: Final = "open"
FULFILLMENT_CLAIMED: Final = "claimed"
FULFILLMENT_GENERATING: Final = "generating"
FULFILLMENT_READY: Final = "ready"

EVENT_SLOT_CLAIMED: Final = "slot_claimed"
EVENT_SLOT_RELEASED: Final = "slot_released"
EVENT_GENERATION_STARTED: Final = "generation_started"
EVENT_POST_LINKED: Final = "post_linked"
EVENT_READY: Final = "ready"

#: 035 新增的五种事件。既有 9 种不在此列 —— 那是文章侧的,本模块不发。
IMAGE_NOTE_EVENT_KINDS: Final[frozenset[str]] = frozenset({
    EVENT_SLOT_CLAIMED, EVENT_SLOT_RELEASED, EVENT_GENERATION_STARTED,
    EVENT_POST_LINKED, EVENT_READY,
})

#: 文章侧读取谓词。给 census 里那四个既有读取方 cutover 用,
#: 写在这里是为了**只有一份** —— 四处各写一遍必然漂移。
ARTICLE_CHANNEL_PREDICATE: Final = (
    "(delivery_channel IS NULL OR delivery_channel = 'article')"
)
IMAGE_NOTE_CHANNEL_PREDICATE: Final = "(delivery_channel = 'douyin_image_note')"


class SlotActivationBlocked(RuntimeError):
    """四前置未全部满足。调用方返回 activation_pending,零 claim 零资金。"""

    def __init__(self, blockers: list[str]):
        super().__init__("SLOT_ACTIVATION_BLOCKED")
        self.blockers = list(blockers)


class SlotClaimConflict(RuntimeError):
    """projection CAS 零行。整项零创建零冻结,前端刷新待制作列表。"""


class SlotWriteUnsafe(RuntimeError):
    """在 autocommit 连接上写槽位。**事件日志会开始说谎**,必须响亮失败。"""


class SlotAuthorityMissing(RuntimeError):
    """槽位存在,但它所属的合同修订还没有编译过 plan run。

    🔴 [返工 2026-08-18 · P0-08] 这不是"参数错",而是**激活前置没到位**:
       `geo_article_delivery_slot_events.plan_run_id` 是 NOT NULL 且 FK 指向
       `geo_article_plan_runs(id)`。没有 run 就写不进事件 —— 必须在
       claim 之前当成 activation blocker 返回,而不是让 FK 在 INSERT 那一刻
       炸成 500(那时候幂等 claim 已经落库了)。
    """


class SlotIdentityMismatch(RuntimeError):
    """调用方报的 quote/brand/owner 与**槽位行自己**的值不符。

    🔴 [返工 2026-08-18 · P0-08] 原来 CAS 只比 `slot_key + projection_version
       + delivery_channel`,而事件行里的 quote_id/brand_id/owner_user_id/
       contract_revision_id **全部来自请求**。于是猜到一个 slot_key 就能
       写出一条署着**别人**品牌的事件,投影也跟着动。
       现在这四格一律取自槽位行本身;调用方传进来的值只用于**核对**。
    """


def _assert_in_transaction(cur) -> None:
    """event append 与 projection CAS 必须**同一事务**。

    🔴 本包自己的迁移矩阵判据把这个顶出来的:autocommit 下两条语句各自提交,
       于是「CAS 被拒」时**事件已经落库了** —— 留下一个没有对应投影变更的孤儿事件,
       而且它占掉了 `(delivery_slot_key, slot_version)` 唯一键,
       下一次合法迁移到同一版本会 UniqueViolation。
       后果比"多一行"严重得多:事件溯源的前提是「事件是权威真值」,
       一个被拒绝的操作留下事件,等于事件日志开始说谎。
       这与 publish_command.CapacityLockUnsafe 是同一族缺陷(静默失效),
       处置也一样:响亮失败,不靠调用方自觉。
    """
    conn = getattr(cur, "connection", None)
    if conn is not None and getattr(conn, "autocommit", False):
        raise SlotWriteUnsafe(
            "槽位写入必须在显式事务内:event append 与 projection CAS 要么一起成功、"
            "要么一起回滚。autocommit 下 CAS 被拒时事件已落库,会留下孤儿事件并"
            "占掉 slot_version 唯一键(规格 02 §3.1:禁止绕过事件真值)。"
        )


def activation_blockers(cur, *, quote_id: int,
                        contract_lane_enabled: bool,
                        sidecar_schema_blockers: list[str],
                        image_note_schema_blockers: list[str]) -> list[str]:
    """RFC R1 四前置的机械求值。返回空列表 = 可激活。

    🔴 顺序是**从便宜到昂贵**:flag 是内存读,两个 readiness 是查表,
       enrollment 要查 quotes。前面挡住就不用查后面 —— 但四条**全部**求值
       而不是短路返回,因为调用方需要知道**全部**缺什么才能给出可执行提示。
    """
    blockers: list[str] = []
    if not contract_lane_enabled:
        blockers.append("flag_disabled:GEO_IMAGE_NOTE_CONTRACT_ENABLED")
    blockers.extend(f"sidecar_schema:{b}" for b in (sidecar_schema_blockers or [])[:5])
    blockers.extend(f"image_note_schema:{b}" for b in (image_note_schema_blockers or [])[:5])

    cur.execute(
        "SELECT article_plan_writing_mode AS mode, article_plan_enrolled_at AS enrolled_at "
        "  FROM quotes WHERE id = %(quote_id)s",
        {"quote_id": int(quote_id)},
    )
    row = cur.fetchone()
    if row is None:
        blockers.append("quote_not_found")
    else:
        data = dict(row)
        if str(data.get("mode") or "") != "image_note_contract" or not data.get("enrolled_at"):
            # 🔴 **不自动 enroll**。这里只报缺失,绝不顺手写 quotes ——
            #    自动 enroll 就是 RFC 明确禁止的"reconciler 批量补造"。
            blockers.append("quote_not_enrolled")
    return blockers


def _event_key(*, slot_key: str, slot_version: int, event_kind: str,
               source_version: str) -> str:
    return snapshot_hash({
        "delivery_slot_key": str(slot_key),
        "slot_version": int(slot_version),
        "event_kind": str(event_kind),
        "source_version": str(source_version),
    })


_APPEND_EVENT = """
INSERT INTO geo_article_delivery_slot_events (
    event_key, delivery_slot_key, slot_version, event_kind, target_state,
    contract_revision_id, plan_run_id, owner_user_id, brand_id, quote_id,
    actor_user_id, source_version, payload, occurred_at, created_at
) VALUES (
    %(event_key)s, %(slot_key)s, %(slot_version)s, %(event_kind)s, %(target_state)s,
    %(contract_revision_id)s, %(plan_run_id)s, %(owner_user_id)s, %(brand_id)s, %(quote_id)s,
    %(actor_user_id)s, %(source_version)s, %(payload)s::jsonb, now(), now()
)
ON CONFLICT (event_key) DO NOTHING
RETURNING id
"""

#: 槽位行 = 商业权威的**唯一**信源(P0-08)。事件行里的四格身份从这里取,
#: 请求里的同名字段只用来核对 —— 不一致就整项拒绝,而不是"以请求为准"。
_LOAD_SLOT_AUTHORITY = """
SELECT s.delivery_slot_key, s.contract_revision_id, s.quote_id, s.brand_id,
       s.owner_user_id, s.projection_version, s.fulfillment_state, s.delivery_channel,
       s.geo_post_id, s.topic_ref,
       (SELECT r.id FROM geo_article_plan_runs r
         WHERE r.contract_revision_id = s.contract_revision_id
         ORDER BY (r.status = 'completed') DESC, r.id DESC
         LIMIT 1) AS plan_run_id
  FROM geo_article_delivery_slots s
 WHERE s.delivery_slot_key = %(slot_key)s
   AND s.delivery_channel = 'douyin_image_note'
 FOR UPDATE OF s
"""


def load_slot_authority(cur, *, slot_key: str) -> dict[str, Any]:
    """读**槽位行自己**的商业权威并锁行。

    🔴 `FOR UPDATE OF s`(而不是裸 `FOR UPDATE`):子查询里的
       `geo_article_plan_runs` 是只读的,把它一起锁住会在多 worker 下
       无谓地互相阻塞 —— 而且 PG 对聚合/子查询行加锁的语义也不是我们要的。
    🔴 渠道谓词写在这里,让「文章槽位被图文链摸到」在**读**这一层就不可能,
       不依赖后面 CAS 那道(CAS 那道也留着,两道都要)。
    """
    cur.execute(_LOAD_SLOT_AUTHORITY, {"slot_key": str(slot_key)})
    row = cur.fetchone()
    if row is None:
        raise SlotClaimConflict(
            f"槽位 {slot_key} 不在图文渠道内或已不存在,请刷新本报价的待制作列表")
    data = dict(row)
    if not data.get("plan_run_id"):
        raise SlotAuthorityMissing(
            f"合同修订 {data.get('contract_revision_id')} 还没有编译过交付计划"
            "(geo_article_plan_runs 无记录),槽位事件写不出来")
    return data


def assert_slot_matches(slot: Mapping[str, Any], *, quote_id: Optional[int] = None,
                        brand_id: Optional[int] = None,
                        owner_user_id: Optional[int] = None,
                        contract_revision_id: Optional[int] = None) -> None:
    """核对调用方报的作用域与槽位行一致。任一格不符即整项拒绝。

    🔴 只核对**传进来的**那几格:`None` 表示"这次不声明",不是"随便"。
       把 None 当通配写成 `or slot[...]` 会让漏传变成静默放行 ——
       本仓刚在别处栽过同一形态(默认参数悄悄换掉了原路径)。
    """
    checks = (
        ("quote_id", quote_id), ("brand_id", brand_id),
        ("owner_user_id", owner_user_id),
        ("contract_revision_id", contract_revision_id),
    )
    bad = [
        f"{name}: 槽位={slot.get(name)} 请求={value}"
        for name, value in checks
        if value is not None and int(value) != int(slot.get(name) or 0)
    ]
    if bad:
        raise SlotIdentityMismatch(
            "槽位归属与本次请求不一致(" + " / ".join(bad) + ")")


def assert_topic_ref_unchanged(slot: Mapping[str, Any],
                               topic_ref: Optional[str]) -> None:
    """槽位一旦带了选题,后续操作**不许换成别的**(规格 §5.6 身份口径)。

    🔴 [第 3 棒 · P0-09 的可做那一半] `topic_ref` 目前是**客户端自述**的
       不透明串:`_CAS_PROJECTION` 里写的是 `COALESCE(新值, 旧值)` ——
       只要传了新值就直接覆盖。于是同一个槽位可以在两次提交之间被换题,
       而"客户买的是这个词的一篇"这条商业事实**没有任何一步在守**。

       这一道守的是**变更**:空 → 有(第一次落题)照旧允许;
       有 → 另一个值 = 换题,拒。相同值(重放)放行。

    🔴 完整的五字段 snapshot 链(`distill_task_id + slot + ordinal + snapshot_hash`,
       快照来自 `geo_douyin_distill_tasks.result`)**仍未做** —— 它需要一个
       会签发 topic_ref 的生产者(选题→槽位的绑定入口),那是独立 WP。
       在签发者出现之前,本函数只能守住"别换",守不住"这个 ref 是真的"。
       交付单已按未做申报,不在这里假装它存在。
    """
    current = str(slot.get("topic_ref") or "").strip()
    incoming = str(topic_ref or "").strip()
    if current and incoming and current != incoming:
        raise SlotIdentityMismatch(
            "这个交付槽位已经绑定了选题,不能在本次提交里换成另一个;"
            "要换题请先释放这个槽位"
        )


_CAS_PROJECTION = """
UPDATE geo_article_delivery_slots
   SET fulfillment_state = %(next_state)s,
       geo_post_id       = COALESCE(%(geo_post_id)s, geo_post_id),
       topic_ref         = COALESCE(%(topic_ref)s, topic_ref),
       projection_version = projection_version + 1,
       current_event_id   = %(event_id)s,
       last_event_key     = %(event_key)s,
       updated_at         = now()
 WHERE delivery_slot_key = %(slot_key)s
   AND projection_version = %(expected_version)s
   AND delivery_channel = 'douyin_image_note'
   AND COALESCE(fulfillment_state, 'open') = ANY(%(allowed_from)s::text[])
RETURNING delivery_slot_key, projection_version, fulfillment_state, geo_post_id
"""

#: 履约态迁移矩阵。**列出全部允许的前驱**,不用"不等于终态"这种反向写法 ——
#: 反向写法在加新状态时会静默放行。
_ALLOWED_FROM: Final[dict[str, tuple[str, ...]]] = {
    FULFILLMENT_CLAIMED: (FULFILLMENT_OPEN,),
    FULFILLMENT_GENERATING: (FULFILLMENT_CLAIMED,),
    FULFILLMENT_READY: (FULFILLMENT_GENERATING, FULFILLMENT_CLAIMED),
    FULFILLMENT_OPEN: (FULFILLMENT_CLAIMED, FULFILLMENT_GENERATING),   # release 回开放
}

_EVENT_FOR_STATE: Final[dict[str, str]] = {
    FULFILLMENT_CLAIMED: EVENT_SLOT_CLAIMED,
    FULFILLMENT_GENERATING: EVENT_GENERATION_STARTED,
    FULFILLMENT_READY: EVENT_READY,
    FULFILLMENT_OPEN: EVENT_SLOT_RELEASED,
}


def transition(cur, *, slot_key: str, expected_version: int, next_state: str,
               identity: Mapping[str, Any],
               contract_revision_id: Optional[int] = None,
               plan_run_id: Optional[int] = None,
               quote_id: Optional[int] = None, brand_id: Optional[int] = None,
               geo_post_id: Optional[int] = None, topic_ref: Optional[str] = None,
               payload: Optional[Mapping[str, Any]] = None,
               source_version: str = "geo-image-note-slot-v1") -> dict[str, Any]:
    """同一事务内:append event → CAS 投影。零行即冲突。

    🔴 顺序是「先写事件、再 CAS 投影」:事件是权威真值,投影只是它的物化。
       反过来(先改投影再补事件)会在两者之间崩溃时留下一个**没有事件支撑的投影** ——
       那时候"事件溯源"这句话就不成立了。
    🔴 CAS 谓词里的 `delivery_channel = 'douyin_image_note'` 是渠道隔离的物理保证:
       文章槽位即使 slot_key 被猜到也改不动(RFC R3)。

    🔴 [返工 2026-08-18 · P0-08] `contract_revision_id / plan_run_id / quote_id /
       brand_id` 四格**不再取自请求**,而是取自槽位行本身;传进来的值降级为
       **核对项**(不传就不核对)。原来的写法有两个硬故障:
         ① 调用点传 `plan_run_id=0`,而事件列是 NOT NULL + FK 指向
            `geo_article_plan_runs(id)` —— 每一次真实 claim 必然
            ForeignKeyViolation 500(`contract_revision_id or 0` 同病);
         ② 身份全听请求 ⇒ 猜到 slot_key 就能写出署着别人品牌的事件。
    """
    _assert_in_transaction(cur)
    if next_state not in _ALLOWED_FROM:
        raise ValueError(f"unknown fulfillment state: {next_state!r}")
    event_kind = _EVENT_FOR_STATE[next_state]

    slot = load_slot_authority(cur, slot_key=slot_key)
    assert_slot_matches(slot, quote_id=quote_id, brand_id=brand_id,
                        contract_revision_id=contract_revision_id,
                        owner_user_id=identity.get("tenant_owner_user_id"))
    assert_topic_ref_unchanged(slot, topic_ref)
    contract_revision_id = int(slot["contract_revision_id"])
    plan_run_id = int(slot["plan_run_id"])
    quote_id = int(slot["quote_id"])
    brand_id = int(slot["brand_id"])

    event_key = _event_key(slot_key=slot_key, slot_version=int(expected_version) + 1,
                           event_kind=event_kind, source_version=source_version)

    # 🔴 SAVEPOINT:CAS 被拒时必须把**本次的 event append 一起回滚**。
    #    没有它,一次被拒的迁移会在调用方事务里留下一条 pending 事件,
    #    占掉 `(delivery_slot_key, slot_version)` 唯一键 —— 紧接着的**合法**迁移
    #    (比如先试非法的 open→ready 被拒、再正常 claim)会撞 UniqueViolation。
    #    批量协调器要在一个事务里连续 claim N 个槽位,这个形态必然踩到。
    #    本仓已有同族教训:「try/except 包 SQL 无 SAVEPOINT = 打废调用方事务」。
    _sp = f"sp_slot_{abs(hash((slot_key, expected_version, next_state))) % 10**9}"
    cur.execute(f"SAVEPOINT {_sp}")
    try:
        return _transition_inner(
            cur, slot_key=slot_key, expected_version=expected_version,
            next_state=next_state, event_kind=event_kind, event_key=event_key,
            identity=identity, contract_revision_id=contract_revision_id,
            plan_run_id=plan_run_id, quote_id=quote_id, brand_id=brand_id,
            geo_post_id=geo_post_id, topic_ref=topic_ref, payload=payload,
            source_version=source_version)
    except Exception:
        cur.execute(f"ROLLBACK TO SAVEPOINT {_sp}")
        raise
    finally:
        # RELEASE 只在成功路径有意义;回滚路径上 savepoint 已随 ROLLBACK TO 保留,
        # 显式 RELEASE 避免长事务里 savepoint 堆积。
        try:
            cur.execute(f"RELEASE SAVEPOINT {_sp}")
        except Exception:  # noqa: BLE001 —— 事务已中止时 RELEASE 会失败,忽略
            pass


def _transition_inner(cur, *, slot_key, expected_version, next_state, event_kind,
                      event_key, identity, contract_revision_id, plan_run_id,
                      quote_id, brand_id, geo_post_id, topic_ref, payload,
                      source_version):
    cur.execute(_APPEND_EVENT, {
        "event_key": event_key,
        "slot_key": str(slot_key),
        "slot_version": int(expected_version) + 1,
        "event_kind": event_kind,
        # target_state 是**记录生命周期**轴(active/blocked/cancelled/superseded),
        # 与 fulfillment_state 正交 —— 履约推进不改记录生命周期,所以恒 'active'。
        "target_state": "active",
        "contract_revision_id": int(contract_revision_id),
        "plan_run_id": int(plan_run_id),
        # owner 也取槽位行(上游 assert_slot_matches 已核对过它等于本次租户),
        # 保证事件行的四格身份**整组**同源,不会一半来自行、一半来自请求。
        "owner_user_id": int(identity["tenant_owner_user_id"]),
        "brand_id": int(brand_id),
        "quote_id": int(quote_id),
        "actor_user_id": int(identity.get("actor_user_id") or identity["tenant_owner_user_id"]),
        "source_version": source_version,
        "payload": json.dumps(dict(payload or {}), ensure_ascii=False),
    })
    event_row = cur.fetchone()
    if event_row is None:
        # 同 event key 已存在 ⇒ 幂等重放。规格 §3.1:相同 event key 用
        # `ON CONFLICT DO NOTHING` **后读取**,不能 `DO UPDATE` 改事件。
        cur.execute(
            "SELECT id FROM geo_article_delivery_slot_events WHERE event_key = %(k)s",
            {"k": event_key})
        existing = cur.fetchone()
        if existing is None:
            raise SlotClaimConflict("事件写入冲突且回读不到,请刷新后重试")
        event_id = int(dict(existing)["id"])
    else:
        event_id = int(dict(event_row)["id"])

    cur.execute(_CAS_PROJECTION, {
        "slot_key": str(slot_key),
        "expected_version": int(expected_version),
        "next_state": next_state,
        "allowed_from": list(_ALLOWED_FROM[next_state]),
        "geo_post_id": int(geo_post_id) if geo_post_id else None,
        "topic_ref": topic_ref,
        "event_id": event_id,
        "event_key": event_key,
    })
    row = cur.fetchone()
    if row is None:
        raise SlotClaimConflict(
            f"槽位 {slot_key} 的状态已被其他操作改变(期望版本 {expected_version}),"
            "请刷新本报价的待制作列表"
        )
    return dict(row)


def claim(cur, **kwargs) -> dict[str, Any]:
    return transition(cur, next_state=FULFILLMENT_CLAIMED, **kwargs)


def release(cur, **kwargs) -> dict[str, Any]:
    """释放回 open。

    🔴 调用方**必须**先落 release 证据(资金已 release)才允许调用它 ——
       规格 §3.1:「只有尚未 external-start 且资金已 release,幂等 slot_released
       事件携带可核验 settlement authority/handle/evidence 才可回到 open」。
       本函数不校验那个前提(它看不到资金),所以调用点必须自己守住;
       判据 `test_release_requires_settlement_evidence` 打的就是调用点。
    """
    return transition(cur, next_state=FULFILLMENT_OPEN, **kwargs)
