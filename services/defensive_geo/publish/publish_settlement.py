"""发布 canonical 状态 × 钱向 —— 逐态封闭的 exhaustive adapter。

规格 §12.1 / §15.7 真值表。判据:FIN-12 / FIN-16 / MED-16 / MED-20 / DEL-04 /
§19 变异 12 / 80 / 113 / 141。

═══════════════════════════════════════════════════════════════════════
🔴 FIN-16 是本模块存在的全部理由
═══════════════════════════════════════════════════════════════════════
「versioned publish raw-state census 覆盖代码尖每个 producer/value;逐值映射
canonical state、钱向与 URL 资格,**删/错映任一 raw value 或 else 兜底
release/commit 必红**,未知值只能 hold/quarantine」。

所以:

  · :data:`RAW_STATE_CENSUS` 是**数据**,不是 if 链。每一行都带 producer 坐标
    (哪个文件哪一行写出这个值),便于复核「这个值真的有人写」;
  · :func:`canonical_state_from_raw` 对**不在表里**的值返回 ``unknown``,
    而不是 ``failed`` —— ``else: failed/released`` 正是 FIN-16 点名要红的那一形态;
  · :func:`settlement_direction` 没有 ``else``:未登记的 canonical state 直接抛。

census 取数命令(复核用,2026-08-21 实跑):
```
grep -rn "status='" db/meijiehezi_db.py api/meijiehezi_api.py \\
     services/geo_douyin/publish_coordinator.py services/geo_douyin/contract_worker.py \\
     services/publication_content_drift.py
grep -rn "STATE_\\|AVAILABILITY_" services/publication_url_verifier.py
```

═══════════════════════════════════════════════════════════════════════
🔴 「published」不等于「verified_published」—— 两轴判定,不是一个字符串
═══════════════════════════════════════════════════════════════════════
§12.1 把 ``reported_success_unverified`` 与 ``verified_published`` **分成两行**,
钱向一个 ``hold_frozen`` 一个 ``commit``。现役 ``mhz_publish_order_items.status='published'``
只表示**上游回执说发了**;本仓另有一条独立核实轴
``publish_records.public_url_verification_state``(unverified/pending/content_matched/
needs_action/verified,见 ``services/publication_url_verifier.py:92``)。

把 ``status='published'`` 直接映成 commit,等于把「浏览器自报 / 上游自报」当结算真值 ——
§12.1 开头那句「不能把浏览器自报、镜像 URL 或裸 status 当结算真值」逐字禁止。
所以本模块的投影**必须**同时吃两轴:

    published + verification∈{verified, content_matched}  → verified_published(commit)
    published + 其它/缺失                                  → reported_success_unverified(hold_frozen)

代价是诚实的:核实链没跑通时钱**不 commit**、也**不 release**,停在 frozen 等 reconciler
或人工核验(Z-1 队列)。这正是 §12.1「不得把接线故障伪装成正常退款」的另一半。

═══════════════════════════════════════════════════════════════════════
🔴 `failed` 的钱向取决于**有没有 external-start marker**,不取决于文案
═══════════════════════════════════════════════════════════════════════
§12.1 两行并列:

  · ``rejected/failed`` 且**有权威证据确认零接单、零外部副作用`` → release 一次;
  · ``rejected/failed`` 但**已 external-start**、供应商结果未知或证据冲突 → 保持 frozen。

同一个 raw ``'failed'`` 落在哪一行,由 canonical external-start marker 决定。
:func:`project` 因此把 ``external_start_recorded`` 作为**必填**入参 ——
默认值会让调用方漏传时静默走进 release,那是最贵的一种默认。
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

#: 改任一行映射**必须**升版(§19 变异 181「改 mapping 不升 version/hash」)。
RAW_STATE_CENSUS_VERSION = "defgeo-publish-raw-state-census-v1"

CanonicalPublicationState = Literal[
    "not_started", "queued", "submitting", "reported_success_unverified",
    "verified_published", "retracted",
    "rejected_no_effect", "failed_no_effect",
    "rejected_unknown", "failed_unknown", "unknown", "conflict",
]
#: §15.7 ``PublicationSettlementProjection`` 的 canonical state 全集。
CANONICAL_STATES: tuple[CanonicalPublicationState, ...] = (
    "not_started", "queued", "submitting", "reported_success_unverified",
    "verified_published", "retracted",
    "rejected_no_effect", "failed_no_effect",
    "rejected_unknown", "failed_unknown", "unknown", "conflict",
)

SettlementDirection = Literal[
    "none", "hold_frozen", "commit", "preserve_historical_commit",
    "release", "hold_or_quarantine",
]

#: 🔴 canonical state → 钱向。**闭表,无 else**(§15.7 逐字)。
_DIRECTION: dict[CanonicalPublicationState, SettlementDirection] = {
    "not_started": "none",
    "queued": "hold_frozen",
    "submitting": "hold_frozen",
    "reported_success_unverified": "hold_frozen",
    "verified_published": "commit",
    "retracted": "preserve_historical_commit",
    "rejected_no_effect": "release",
    "failed_no_effect": "release",
    "rejected_unknown": "hold_or_quarantine",
    "failed_unknown": "hold_or_quarantine",
    "unknown": "hold_or_quarantine",
    "conflict": "hold_or_quarantine",
}

#: 🔴 只有 ``verified_published`` 有资格出 ``publicUrl``。
#:    其余一律 null —— URL 只能进 ``verificationClues``(§15.7 表格第 2 行逐字)。
_URL_ELIGIBLE: frozenset[CanonicalPublicationState] = frozenset({
    "verified_published", "retracted",   # retracted 可保留历史已验证 URL
})

#: 计**当前有效交付**的唯一一格(DEL-04)。retracted 只留历史 occurrence。
_COUNTS_CURRENT_DELIVERY: frozenset[CanonicalPublicationState] = frozenset({
    "verified_published",
})


class SettlementProjectionError(ValueError):
    """投影不自洽 —— 整个 projection 失败并告警(§15.7 末段逐字)。"""


class RawStateRow(NamedTuple):
    """census 的一行。五项全填 —— 少 producer 坐标就没法复核「真有人写这个值」。"""

    #: 写出这个值的生产者坐标(文件:行 或 函数名)。复核用。
    producer: str
    #: 该 raw 值对应的 canonical state。``None`` = 该值只定「轴」不定终态,
    #: 由 :func:`project` 结合其它轴决定(例如 ``published`` 要看核实轴)。
    canonical: CanonicalPublicationState | None
    #: 该值是否表示「已经对外发起过」。用于 ``failed`` 的钱向分叉。
    implies_external_start: bool
    #: 备注:为什么这么映。
    note: str


#: ── mhz_publish_order_items.status(text)────────────────────────────────
#: 字面值全集来自 census 2026-08-21 的跨文件 grep(见模块 docstring 的取数命令)。
_ITEM_STATUS: dict[str, RawStateRow] = {
    "queued": RawStateRow(
        "services/geo_douyin/publish_coordinator.py:93 _INSERT_ITEM", "queued", False,
        "建单即写。未对外,钱保持 frozen。",
    ),
    "pending": RawStateRow(
        "mhz_publish_order_items DEFAULT + contract_seams.LEGACY_SUBMITTER_CLAIM_STATUS",
        "queued", False,
        "既是列默认值,也是 legacy submitter 的领取态。两种含义都在 external-start 之前。",
    ),
    "submitting": RawStateRow(
        "db/meijiehezi_db.py / api/meijiehezi_api.py", "submitting", True,
        "已开始对外提交。此后 failed 不得自动退款。",
    ),
    "submitted": RawStateRow(
        "db/meijiehezi_db.py / api/meijiehezi_api.py", "submitting", True,
        "provider 已接受但未出终态。§12.1「provider_accepted 保持 frozen」。",
    ),
    "awaiting_confirmation": RawStateRow(
        "api/meijiehezi_api.py(pending_confirm_codes 一族)", "unknown", True,
        "上游要补字段/回执矛盾 —— 结果未知,不 release。",
    ),
    "awaiting_sync": RawStateRow(
        "db/meijiehezi_db.py awaiting_sync_since 一族", "unknown", True,
        "等上游同步。未知态,只能 hold/quarantine。",
    ),
    "awaiting_action": RawStateRow(
        "services/publication_content_drift.py:47 ITEM_STATUS_AWAITING_ACTION", "unknown", True,
        "正文漂移待处置。已对外过,未知。",
    ),
    "published": RawStateRow(
        "db/meijiehezi_db.py:3410 sync_mhz_orders(mhz_status==2)", None, True,
        "🔴 只表示上游回执说发了。终态由核实轴决定(见模块 docstring 两轴判定)。",
    ),
    "rejected": RawStateRow(
        "db/meijiehezi_db.py:3410 sync_mhz_orders(mhz_status==-1)", "rejected_no_effect", True,
        "上游权威拒稿 = 零接单证据。needs_refund=True 是现役同一判断。",
    ),
    "withdrawn": RawStateRow(
        "api/meijiehezi_api.py:3354/3430 + sync(mhz_status==-2)", "rejected_no_effect", False,
        "用户撤回(订单未提交到外部发布通道)。发布后的下架走 availability 轴,不走这里。",
    ),
    "cancelled": RawStateRow(
        "db/meijiehezi_db.py 终态白名单", "rejected_no_effect", False,
        "外调前取消,零副作用。",
    ),
    "failed": RawStateRow(
        "db/meijiehezi_db.py 终态白名单", None, False,
        "🔴 钱向由 external-start marker 分叉,不由这个字符串决定(见模块 docstring)。",
    ),
}

#: ── mhz_synced_orders.status(**integer**,直接落上游原始数字)──────────
#: 数据类型与前两张表不同(census 实测),所以 key 是 int 不是 str ——
#: 用字符串 key 会让 `'2'` 与 `2` 长得一样而实际取不到。
_SYNCED_ORDER_STATUS: dict[int, RawStateRow] = {
    0: RawStateRow("db/meijiehezi_db.py:3364 UPDATE mhz_synced_orders(默认)", "queued", False,
                   "上游默认态。"),
    2: RawStateRow("db/meijiehezi_db.py:3410(mhz_status==2)", None, True,
                   "上游说已发。同样要过核实轴。"),
    -1: RawStateRow("api/meijiehezi_api.py:1270 WHERE status=-1", "rejected_no_effect", True,
                    "上游拒稿。现役 republish 条件就用这个字面整数。"),
    -2: RawStateRow("db/meijiehezi_db.py:3410(mhz_status==-2)", "rejected_no_effect", False,
                    "上游撤单。"),
}

#: ── publish_records.status(text)────────────────────────────────────────
#: 🔴 census 实测:插件死写入模块(WO_273 已删)里 ``update_publish_record`` 的 status 是
#:    **调用方传入的自由字符串**,函数体内零枚举校验。因此这里只登记确知的生产值,
#:    其余一律落 unknown —— 这正是 FIN-16「不存在于已签表的任何新/旧 raw value
#:    一律 unknown/conflict + hold_or_quarantine」。
#: 🔴 [WO_273 · 2026-09-23] 浏览器插件后端整体退役:本列**已无活写入方**,上面那两个函数连同所在模块
#:    一并删除。存量行仍在、读者仍读,所以映射一格不动;producer 坐标改写成「已退役 + 基线可查」,
#:    不再指向一个已经不存在的文件。
_PUBLISH_RECORD_STATUS: dict[str, RawStateRow] = {
    "pending": RawStateRow("[WO_273 已退役 · 无活写入方] 存量行由浏览器插件后端建记录时写入(基线 01cf3a9e2 可查)",
                           "queued", False, "建记录即写。"),
    "success": RawStateRow("[WO_273 已退役 · 无活写入方] 存量行由浏览器插件后端回报时写入(基线 01cf3a9e2 可查)",
                           None, True, "浏览器扩展自报成功 —— §12.1 明列「浏览器自报」不 commit。"),
    "failed": RawStateRow("[WO_273 已退役 · 无活写入方] 存量行由浏览器插件后端回报时写入(基线 01cf3a9e2 可查)",
                          None, False, "同 item 的 failed:钱向由 external-start 分叉。"),
}

#: ── publish_records.public_url_verification_state ──────────────────────
#: 逐字取自 ``services/publication_url_verifier.py:92``。
VERIFICATION_STATES: tuple[str, ...] = (
    "unverified", "pending", "content_matched", "needs_action", "verified",
)
#: 🔴 只有这两个值算「已核实」。``content_matched`` 是内容比对通过,
#:    是本仓核实链能给出的最强证据之一;``needs_action`` 明确不算。
_VERIFIED_VALUES: frozenset[str] = frozenset({"verified", "content_matched"})

#: ── publish_records.public_url_availability_state ──────────────────────
AVAILABILITY_STATES: tuple[str, ...] = (
    "available", "unreachable", "content_missing", "domain_mismatch", "retracted",
)

#: 对外 availability(§15.7 ``PublishItemStatusProjection.availability``)。
PublicAvailability = Literal["not_published", "unknown", "active", "retracted"]

_AVAILABILITY_BY_STATE: dict[CanonicalPublicationState, PublicAvailability] = {
    "not_started": "not_published",
    "queued": "unknown",
    "submitting": "unknown",
    "reported_success_unverified": "unknown",
    "verified_published": "active",
    "retracted": "retracted",
    "rejected_no_effect": "not_published",
    "failed_no_effect": "not_published",
    "rejected_unknown": "unknown",
    "failed_unknown": "unknown",
    "unknown": "unknown",
    "conflict": "unknown",
}

#: 全部 census 表,按 (表, 列) 索引。判据从这里取分母,不手抄。
RAW_STATE_CENSUS: dict[tuple[str, str], dict[Any, RawStateRow]] = {
    ("mhz_publish_order_items", "status"): _ITEM_STATUS,
    ("mhz_synced_orders", "status"): _SYNCED_ORDER_STATUS,
    ("publish_records", "status"): _PUBLISH_RECORD_STATUS,
}


def settlement_direction(state: object) -> SettlementDirection:
    """canonical state → 钱向。未登记的 state **抛**,不兜底。"""
    if state not in _DIRECTION:
        raise SettlementProjectionError(
            f"未登记的 canonical publication state {state!r};合法 = {list(CANONICAL_STATES)}。"
            "FIN-16:不能靠 else 兜底 release/commit"
        )
    return _DIRECTION[state]  # type: ignore[index]


def public_availability(state: object) -> PublicAvailability:
    if state not in _AVAILABILITY_BY_STATE:
        raise SettlementProjectionError(f"未登记的 canonical state {state!r}")
    return _AVAILABILITY_BY_STATE[state]  # type: ignore[index]


def url_eligible(state: object) -> bool:
    """该态能不能出 ``publicUrl``。其余只能进 ``verificationClues``。"""
    settlement_direction(state)          # 先验合法性 —— 未知态在这里就抛
    return state in _URL_ELIGIBLE


def counts_current_delivery(state: object) -> bool:
    """DEL-04:只有 canonical ``verified_published`` 计当前有效交付。"""
    settlement_direction(state)
    return state in _COUNTS_CURRENT_DELIVERY


def canonical_state_from_raw(
    table: str, column: str, raw_value: Any,
) -> tuple[CanonicalPublicationState | None, RawStateRow | None]:
    """查 census。**不在表里 → (``unknown``, None)**,永不落 failed/released。"""
    key = (table, column)
    if key not in RAW_STATE_CENSUS:
        raise SettlementProjectionError(
            f"未登记的 raw state 来源 {key};合法 = {sorted(RAW_STATE_CENSUS)}"
        )
    row = RAW_STATE_CENSUS[key].get(raw_value)
    if row is None:
        return "unknown", None
    return row.canonical, row


class PublicationFacts(NamedTuple):
    """一条 item 的 canonical 事实。**全部必填** —— 默认值是最贵的一种默认。"""

    #: raw 状态来源表与列(必须在 census 里)。
    source_table: str
    source_column: str
    raw_value: Any
    #: canonical external-start marker 是否存在(§12.3 worker 外调前写)。
    external_start_recorded: bool
    #: URL 核实轴。``None`` = 从未核实过。
    url_verification_state: str | None
    #: 可达性轴。``None`` = 未观测。
    url_availability_state: str | None
    #: 广告法命中(布尔即判别位;文案不参与判定)。
    legal_rule_hit: bool
    #: 镜像(synced order)与 canonical item 是否冲突。
    mirror_conflict: bool = False


def project(facts: PublicationFacts) -> tuple[CanonicalPublicationState, SettlementDirection]:
    """把多轴 canonical 事实投影成 (canonical state, 钱向)。

    判定顺序是**有意的**,每一步都对应一个真实损失方向:

      ① **镜像冲突优先** —— §12.1 末行「canonical item 优先,资金不随镜像翻转」。
         冲突时直接 conflict/hold,不看后面任何一轴。
      ② **可达性 retracted** —— 已发布后下架是 availability 变化,
         历史 settlement 保留 committed(``preserve_historical_commit``)。
      ③ raw 值查 census。表里有终态的直接用。
      ④ ``published`` 一族过核实轴(两轴判定)。
      ⑤ ``failed`` 一族过 external-start 分叉。
      ⑥ 兜不住的一律 unknown —— **不是 failed**。
    """
    if facts.url_verification_state is not None and facts.url_verification_state not in VERIFICATION_STATES:
        raise SettlementProjectionError(
            f"未知核实态 {facts.url_verification_state!r};合法 = {list(VERIFICATION_STATES)}"
        )
    if facts.url_availability_state is not None and facts.url_availability_state not in AVAILABILITY_STATES:
        raise SettlementProjectionError(
            f"未知可达态 {facts.url_availability_state!r};合法 = {list(AVAILABILITY_STATES)}"
        )

    # ① 镜像冲突 —— canonical item 优先,钱不随镜像翻转。
    if facts.mirror_conflict:
        return "conflict", settlement_direction("conflict")

    canonical, row = canonical_state_from_raw(
        facts.source_table, facts.source_column, facts.raw_value,
    )

    # ② 已核实发布过之后的下架。**只有**已核实过的才可能 retracted ——
    #    从没核实过就下架,那是 unknown,不是 retracted(retracted 会保留 committed)。
    if facts.url_availability_state == "retracted":
        if facts.url_verification_state in _VERIFIED_VALUES:
            return "retracted", settlement_direction("retracted")
        return "unknown", settlement_direction("unknown")

    # ③ census 里已有终态。
    if canonical is not None:
        return canonical, settlement_direction(canonical)

    # ④⑤ 需要多轴决定的两族。row 为 None 表示 raw 值不在表里 —— 上面 ③ 已按
    #     unknown 返回,所以走到这里 row 必非 None。
    assert row is not None                       # census 表自身的不变式
    if facts.raw_value in ("published", 2, "success"):
        if facts.url_verification_state in _VERIFIED_VALUES:
            return "verified_published", settlement_direction("verified_published")
        # 🔴 上游/浏览器自报成功但没核实 —— 保持 frozen,不 commit 不 release。
        return "reported_success_unverified", settlement_direction("reported_success_unverified")

    if facts.raw_value == "failed":
        if facts.external_start_recorded:
            # 已对外过,供应商结果未知 —— §12.1「不得把接线故障伪装成正常退款」。
            return "failed_unknown", settlement_direction("failed_unknown")
        return "failed_no_effect", settlement_direction("failed_no_effect")

    # ⑥ 表里登记了却没给终态、也不在上面两族 —— 这是 census 表自己的漏洞,
    #    响亮地抛出来,而不是安静地落一个方向。
    raise SettlementProjectionError(
        f"census 行 {facts.source_table}.{facts.source_column}={facts.raw_value!r} "
        f"未给出 canonical 且不属于任何多轴族 —— census 表有洞(producer={row.producer})"
    )


# ══════════════════════════════════════════════════════════════════════════
# §15.7 真值表:settlement direction × fundingState × commandState × availability
# ══════════════════════════════════════════════════════════════════════════
ChargedFundingState = Literal[
    "frozen", "committed", "released", "pending_reconciliation", "quarantined",
]
PLATFORM_FUNDING_STATE = "exempt_recorded"

#: 🔴 §15.7 表格逐行。key = 钱向,value = (允许的 charged fundingState 集合,
#: 允许的 commandState 集合)。**交换任意两格必须转红**。
_TRUTH_TABLE: dict[SettlementDirection, tuple[frozenset[str], frozenset[str]]] = {
    "none": (frozenset({"frozen"}), frozenset({"queued"})),
    "hold_frozen": (
        frozenset({"frozen", "pending_reconciliation"}),
        frozenset({"queued", "running", "settlement_pending", "needs_action"}),
    ),
    "commit": (frozenset({"committed"}), frozenset({"completed"})),
    "preserve_historical_commit": (frozenset({"committed"}), frozenset({"completed"})),
    "release": (frozenset({"released"}), frozenset({"failed", "cancelled", "needs_action"})),
    "hold_or_quarantine": (
        frozenset({"frozen", "pending_reconciliation", "quarantined"}),
        frozenset({"settlement_pending", "needs_action", "quarantined"}),
    ),
}


def assert_truth_table(
    *,
    canonical_publication_state: str,
    funding_state: str,
    command_state: str,
    availability: str,
    public_url: object,
    is_platform_cost: bool,
) -> None:
    """§15.7 末段:非法组合让**整个 projection 失败并告警**,不是修一修发出去。

    点名要拒的五种(§15.7 逐字):``verified+released``、``unverified+committed``、
    ``unknown+released``、``retracted+active``、no-effect 仍带 public URL。
    """
    direction = settlement_direction(canonical_publication_state)
    allowed_funding, allowed_command = _TRUTH_TABLE[direction]

    if is_platform_cost:
        if funding_state != PLATFORM_FUNDING_STATE:
            raise SettlementProjectionError(
                f"platform_cost_center 的 fundingState 必须恒为 {PLATFORM_FUNDING_STATE},"
                f"实得 {funding_state!r} —— 平台账成功响应伪装成 wallet freeze 是变异 157 的形态"
            )
    else:
        if funding_state not in allowed_funding:
            raise SettlementProjectionError(
                f"{canonical_publication_state}({direction}) 不允许 fundingState={funding_state!r};"
                f"允许 = {sorted(allowed_funding)}"
            )

    if command_state not in allowed_command:
        raise SettlementProjectionError(
            f"{canonical_publication_state}({direction}) 不允许 commandState={command_state!r};"
            f"允许 = {sorted(allowed_command)}"
        )

    expected_availability = public_availability(canonical_publication_state)
    if availability != expected_availability:
        raise SettlementProjectionError(
            f"{canonical_publication_state} 的 availability 必须是 {expected_availability!r},"
            f"实得 {availability!r}"
        )

    has_url = public_url not in (None, "")
    if has_url and not url_eligible(canonical_publication_state):
        raise SettlementProjectionError(
            f"{canonical_publication_state} 不得携带 publicUrl —— "
            "未核实的 URL 只能进 verificationClues(§15.7 表格第 2 行)"
        )
    if canonical_publication_state == "verified_published" and not has_url:
        raise SettlementProjectionError(
            "verified_published 必须携带已验证的 PublicHttpUrl"
        )


def census() -> dict[str, Any]:
    """FIN-16 的机械分母。**判据从这里取,不手抄**。"""
    rows: list[dict[str, Any]] = []
    for (table, column), values in RAW_STATE_CENSUS.items():
        for raw, row in values.items():
            canonical = row.canonical
            rows.append({
                "table": table,
                "column": column,
                "rawValue": raw,
                "producer": row.producer,
                "canonicalState": canonical,
                "settlementDirection": settlement_direction(canonical) if canonical else "multi_axis",
                "urlEligible": url_eligible(canonical) if canonical else None,
                "impliesExternalStart": row.implies_external_start,
                "note": row.note,
            })
    return {
        "censusVersion": RAW_STATE_CENSUS_VERSION,
        "canonicalStates": list(CANONICAL_STATES),
        "directions": {s: _DIRECTION[s] for s in CANONICAL_STATES},
        "availability": {s: _AVAILABILITY_BY_STATE[s] for s in CANONICAL_STATES},
        "urlEligibleStates": sorted(_URL_ELIGIBLE),
        "currentDeliveryStates": sorted(_COUNTS_CURRENT_DELIVERY),
        "verificationStates": list(VERIFICATION_STATES),
        "verifiedValues": sorted(_VERIFIED_VALUES),
        "availabilityStates": list(AVAILABILITY_STATES),
        "truthTable": {
            d: {"fundingStates": sorted(f), "commandStates": sorted(c)}
            for d, (f, c) in _TRUTH_TABLE.items()
        },
        "rawRows": rows,
    }
