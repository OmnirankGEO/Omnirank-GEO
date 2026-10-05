"""窗C · WP6 纯函数层判据:发布格八态 / raw-state census / 真值表 / Z-1 / reconciler。

不打库。每条判据的 docstring **首行**写清对应验收 ID。

🔴 分母纪律:八态、raw 值全集、队列态、never-release 态一律从 ``census()`` 机械取,
   判据只写**期望值**并双向核对(少一格 / 多一格都要红)。
   手抄分母漏掉的那一项不会让任何判据变红。
"""

from __future__ import annotations

import asyncio

import pytest

from services.defensive_geo.publish import publish_settlement as ps
from services.defensive_geo.publish import publish_slot as slot
from services.defensive_geo.publish import reconciler as rc
from services.defensive_geo.publish import settlement_review as sr

# ══════════════════════════════════════════════════════════════════════════
# C. publish_slot —— MED-20 / FIN-14
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 八格的**期望值**逐格写死(值是判据的,分母是 census 的)。
#:    交换任意两格、把某格的 allows_* 从 False 改成 True 都必须转红。
_EXPECTED_SLOT_RULES = {
    # state: (admission, allows_ordinary_preview, allows_confirm, allows_retry_child)
    "empty": ("snapshot_ready", True, False, False),
    "open_snapshot": ("snapshot_ready", True, True, False),
    "in_flight": ("existing_command", False, False, False),
    "outcome_unknown": ("existing_command", False, False, False),
    "fulfilled": ("existing_command", False, False, False),
    "ordinary_no_effect_released": ("retry_child_required", False, False, True),
    "legal_no_effect_released": ("legal_repair_required", False, False, False),
    "retracted_committed": ("replacement_policy_required", False, False, False),
}


def test_med20_eight_slot_states_pinned_cell_by_cell():
    """MED-20:八态全序逐格钉死 admission + 三个 allows_*(分母取自 census)。"""
    census = slot.census()
    states = census["slotStates"]

    # 双向核对分母:少一格、多一格、改名都红。
    assert set(states) == set(_EXPECTED_SLOT_RULES), (
        f"八态全序与判据期望的分母不一致:census={sorted(states)} "
        f"expected={sorted(_EXPECTED_SLOT_RULES)}"
    )
    assert len(states) == 8

    for state in states:
        admission, ordinary, confirm, retry = _EXPECTED_SLOT_RULES[state]
        r = slot.rule(state)
        assert r.admission == admission, f"{state} 的 admission 分支错了"
        assert r.allows_ordinary_preview is ordinary, f"{state} 的普通 preview 准入错了"
        assert r.allows_confirm is confirm, f"{state} 的 confirm 准入错了"
        assert r.allows_retry_child is retry, f"{state} 的 retry-child 准入错了"

        # 三个便捷函数必须与 rule() 同答案(同一谓词写两处 ⇒ 必有一处没人验)。
        assert slot.allows_ordinary_preview(state) is ordinary
        assert slot.allows_confirm(state) is confirm
        assert slot.allows_retry_child(state) is retry

        # 端点投影同样逐值。
        projection = slot.admission_for(state)
        assert projection["slotState"] == state
        assert projection["slotAdmission"] == admission
        assert projection["allowsOrdinaryPreview"] is ordinary
        assert projection["allowsConfirm"] is confirm
        assert projection["allowsRetryChild"] is retry
        assert projection["nextActionKind"] and projection["capability"]

    # census 自己导出的三份清单也要与逐格期望一致。
    assert set(census["ordinaryPreviewStates"]) == {
        s for s, v in _EXPECTED_SLOT_RULES.items() if v[1]
    }
    assert set(census["confirmableStates"]) == {
        s for s, v in _EXPECTED_SLOT_RULES.items() if v[2]
    }
    assert set(census["retryChildStates"]) == {
        s for s, v in _EXPECTED_SLOT_RULES.items() if v[3]
    }


def test_med20_fulfilled_and_legal_slots_never_allow_a_second_publish():
    """MED-20:fulfilled / 法律格 / 未知格一律不许再签发新 preview。

    这三格是「二次扣费」与「绕过局部修复重发」的唯一入口 ——
    单独钉一遍,因为它们是真实损失方向,不只是表里的一行。
    """
    for state in ("fulfilled", "legal_no_effect_released", "outcome_unknown",
                  "in_flight", "retracted_committed"):
        assert slot.allows_ordinary_preview(state) is False
        assert slot.allows_confirm(state) is False
    assert slot.allows_retry_child("legal_no_effect_released") is False, (
        "法律格能 retry-child = 绕过广告法局部修复直接重发(MED-18 逐字禁止)"
    )
    assert slot.allows_retry_child("ordinary_no_effect_released") is True, (
        "普通 no-effect 格必须能 retry-child —— 否则这条判据两边都是 False,零判别力"
    )


@pytest.mark.parametrize(
    "unknown",
    ["published", "cancelled", "snapshot_cancelled_before_command", "EMPTY", "", None, 0,
     "fulfilled ", "unknown"],
)
def test_med20_unknown_slot_state_raises_no_else_fallback(unknown):
    """MED-20:未知态 rule() 必抛 —— 没有 else 兜底。"""
    with pytest.raises(slot.SlotError):
        slot.rule(unknown)
    with pytest.raises(slot.SlotError):
        slot.allows_ordinary_preview(unknown)
    with pytest.raises(slot.SlotError):
        slot.admission_for(unknown)


_SLOT_ID_KWARGS = dict(
    tenant_owner_id=9301,
    service_projection_id="svc-A",
    accepted_snapshot_id="acc-A",
    plan_item_key="plan-item-1",
)


def test_fin14_slot_id_does_not_collide_across_tenants_or_contracts():
    """FIN-14:两 tenant 同 plan_item_key(同 article revision)必得不同 slot id。

    🔴 article revision **不进** slot 身份(实测签名只有五项):slot 是「这条计划项的
       交付格」,换一版正文仍是同一格 —— 所以「同 article revision」这一维在这里
       天然成立,真正承重的是 tenant/service/accepted snapshot 三项。逐项各变一次。
    """
    base = slot.derive_publish_slot_id(**_SLOT_ID_KWARGS)
    assert base.startswith("pslot_")

    other_tenant = slot.derive_publish_slot_id(**{**_SLOT_ID_KWARGS, "tenant_owner_id": 9302})
    assert other_tenant != base, (
        "两个 tenant 用同一个 plan_item_key 碰撞了 —— plan_item_key 本身不是全局唯一(FIN-14)"
    )

    variants = {
        "service_projection_id": "svc-B",
        "accepted_snapshot_id": "acc-B",
        "plan_item_key": "plan-item-2",
        "command_kind": "other_command",
    }
    seen = {base, other_tenant}
    for field, value in variants.items():
        got = slot.derive_publish_slot_id(**{**_SLOT_ID_KWARGS, field: value})
        assert got != base, f"改 {field} 没有改变 slot id —— 该项没有进身份"
        assert got not in seen, f"改 {field} 撞上了别的 slot id"
        seen.add(got)


def test_fin14_slot_id_is_deterministic_across_calls():
    """FIN-14:同一组输入重复调完全相同 —— 换 HTTP key 不能换出第二个格。"""
    ids = {slot.derive_publish_slot_id(**_SLOT_ID_KWARGS) for _ in range(5)}
    assert len(ids) == 1

    # 数字 / 字符串同值必须同 id(库里读出来可能是 int,请求里可能是 str)。
    as_text = slot.derive_publish_slot_id(**{**_SLOT_ID_KWARGS, "tenant_owner_id": "9301"})
    assert as_text == ids.pop()


@pytest.mark.parametrize("field", list(_SLOT_ID_KWARGS))
def test_fin14_slot_id_refuses_empty_component(field):
    """FIN-14:身份少任一项必抛 —— 少一项就可能跨租户碰撞。"""
    for empty in (None, "", "   "):
        with pytest.raises(slot.SlotError):
            slot.derive_publish_slot_id(**{**_SLOT_ID_KWARGS, field: empty})


def test_med13_publish_item_request_id_is_stable_and_slot_bound():
    """MED-13:``publish_item_request_id`` 由 slot 确定性派生,不是浏览器随机值。"""
    slot_id = slot.derive_publish_slot_id(**_SLOT_ID_KWARGS)
    ids = {slot.derive_publish_item_request_id(slot_id) for _ in range(5)}
    assert len(ids) == 1
    request_id = ids.pop()
    assert request_id.startswith("pireq_")

    other = slot.derive_publish_item_request_id(
        slot.derive_publish_slot_id(**{**_SLOT_ID_KWARGS, "tenant_owner_id": 9302})
    )
    assert other != request_id

    for bad in ("random-browser-uuid", "", None, 123, "PSLOT_x", request_id):
        with pytest.raises(slot.SlotError):
            slot.derive_publish_item_request_id(bad)


@pytest.mark.parametrize(
    "canonical, funding, legal, expected, why",
    [
        ("verified_published", "committed", False, "fulfilled",
         "已核实发布且钱已收敛 —— 换 HTTP key 也不能再发再扣"),
        ("failed_no_effect", "released", True, "legal_no_effect_released",
         "法律命中且钱已退 —— 只能局部修复,不许 retry-child"),
        ("failed_no_effect", "released", False, "ordinary_no_effect_released",
         "普通零副作用且钱已退 —— 允许 retry-child"),
        ("failed_no_effect", "frozen", False, "outcome_unknown",
         "🔴 钱没退完不许放行新发"),
        ("failed_no_effect", "pending_reconciliation", False, "outcome_unknown",
         "核验中同样不算收敛"),
        ("failed_no_effect", "quarantined", False, "outcome_unknown",
         "隔离态同样不算收敛"),
        ("rejected_no_effect", "released", False, "ordinary_no_effect_released",
         "上游拒稿同属 release 方向"),
        ("not_started", "frozen", False, "in_flight", "建单未派发"),
        ("queued", "frozen", False, "in_flight", "hold_frozen 方向"),
        ("submitting", "frozen", False, "in_flight", "hold_frozen 方向"),
        ("reported_success_unverified", "frozen", False, "in_flight",
         "上游自报成功但未核实 —— 仍在途,不是 fulfilled"),
        ("failed_unknown", "pending_reconciliation", False, "outcome_unknown", "未知态"),
        ("rejected_unknown", "quarantined", False, "outcome_unknown", "未知态"),
        ("unknown", "pending_reconciliation", False, "outcome_unknown", "未知态"),
        ("conflict", "frozen", False, "outcome_unknown", "镜像冲突"),
        ("retracted", "committed", False, "retracted_committed", "下架保留历史 commit"),
    ],
)
def test_med20_classify_command_covers_every_canonical_state(
    canonical, funding, legal, expected, why,
):
    """MED-20:canonical 事实 → slot 态逐态投影。"""
    assert slot.classify_command(
        canonical_publication_state=canonical,
        funding_state=funding,
        legal_rule_hit=legal,
    ) == expected, why


def test_med20_classify_command_denominator_is_every_canonical_state():
    """MED-20:分母 = publish_settlement 的 canonical 全集,一态都不许没人分类。"""
    unclassified = []
    for state in ps.CANONICAL_STATES:
        try:
            got = slot.classify_command(
                canonical_publication_state=state, funding_state="frozen", legal_rule_hit=False,
            )
        except slot.SlotError:
            unclassified.append(state)
            continue
        assert got in slot.SLOT_STATES
    assert unclassified == [], f"这些 canonical 态没有对应 slot 态:{unclassified}"


def test_med18_legal_flag_beats_funding_state_and_reason_text():
    """MED-18:法律格与普通格的唯一判别位是 legalRuleHit,不是文案、不是 funding。"""
    for funding in ("released", "frozen", "committed", "quarantined"):
        assert slot.classify_command(
            canonical_publication_state="failed_no_effect",
            funding_state=funding, legal_rule_hit=True,
        ) == "legal_no_effect_released"


# ══════════════════════════════════════════════════════════════════════════
# D. publish_settlement —— FIN-12 / FIN-16
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 raw-state census 的**期望值**逐行写死。
#:    key = (表, 列, raw 值);value = (canonicalState, settlementDirection, urlEligible)。
#:    ``None`` / ``multi_axis`` 表示该值只定轴不定终态(必须过第二轴)。
#:    删一行、加一行、改任一映射都必须转红 —— FIN-16 逐字。
_EXPECTED_RAW_ROWS = {
    ("mhz_publish_order_items", "status", "queued"): ("queued", "hold_frozen", False),
    ("mhz_publish_order_items", "status", "pending"): ("queued", "hold_frozen", False),
    ("mhz_publish_order_items", "status", "submitting"): ("submitting", "hold_frozen", False),
    ("mhz_publish_order_items", "status", "submitted"): ("submitting", "hold_frozen", False),
    ("mhz_publish_order_items", "status", "awaiting_confirmation"):
        ("unknown", "hold_or_quarantine", False),
    ("mhz_publish_order_items", "status", "awaiting_sync"):
        ("unknown", "hold_or_quarantine", False),
    ("mhz_publish_order_items", "status", "awaiting_action"):
        ("unknown", "hold_or_quarantine", False),
    # 🔴 published 不得直接落 commit —— 必须过核实轴。
    ("mhz_publish_order_items", "status", "published"): (None, "multi_axis", None),
    ("mhz_publish_order_items", "status", "rejected"): ("rejected_no_effect", "release", False),
    ("mhz_publish_order_items", "status", "withdrawn"): ("rejected_no_effect", "release", False),
    ("mhz_publish_order_items", "status", "cancelled"): ("rejected_no_effect", "release", False),
    # 🔴 failed 不得直接落 release —— 必须过 external-start 轴。
    ("mhz_publish_order_items", "status", "failed"): (None, "multi_axis", None),
    ("mhz_synced_orders", "status", 0): ("queued", "hold_frozen", False),
    ("mhz_synced_orders", "status", 2): (None, "multi_axis", None),
    ("mhz_synced_orders", "status", -1): ("rejected_no_effect", "release", False),
    ("mhz_synced_orders", "status", -2): ("rejected_no_effect", "release", False),
    ("publish_records", "status", "pending"): ("queued", "hold_frozen", False),
    ("publish_records", "status", "success"): (None, "multi_axis", None),
    ("publish_records", "status", "failed"): (None, "multi_axis", None),
}


def test_fin16_raw_state_census_pinned_row_by_row():
    """FIN-16:raw-state census 逐行钉 canonicalState / 钱向 / URL 资格。"""
    rows = ps.census()["rawRows"]
    assert rows, "census 的 rawRows 为空 —— 判据会打在空气上"

    seen = {(r["table"], r["column"], r["rawValue"]) for r in rows}
    assert seen == set(_EXPECTED_RAW_ROWS), (
        "raw 值全集变了(删/加/改名任一行都会走到这里)。"
        f"census 多出={sorted(map(str, seen - set(_EXPECTED_RAW_ROWS)))} "
        f"census 缺少={sorted(map(str, set(_EXPECTED_RAW_ROWS) - seen))}"
    )
    assert len(rows) == len(_EXPECTED_RAW_ROWS), "census 里出现了重复行"

    for row in rows:
        key = (row["table"], row["column"], row["rawValue"])
        canonical, direction, url_eligible = _EXPECTED_RAW_ROWS[key]
        assert row["canonicalState"] == canonical, f"{key} 的 canonicalState 变了"
        assert row["settlementDirection"] == direction, f"{key} 的钱向变了"
        assert row["urlEligible"] == url_eligible, f"{key} 的 URL 资格变了"
        assert row["producer"], f"{key} 没有 producer 坐标 —— 无法复核「真有人写这个值」"
        # 独立复算:census 报的钱向必须与 settlement_direction() 同答案。
        if canonical is not None:
            assert ps.settlement_direction(canonical) == direction
            assert ps.url_eligible(canonical) is url_eligible


def test_fin16_no_raw_value_falls_through_to_release_or_commit_by_default():
    """FIN-16:两个高危 raw 值(published / failed)必须**没有**单轴终态。"""
    for key in (("mhz_publish_order_items", "status", "published"),
                ("mhz_publish_order_items", "status", "failed"),
                ("publish_records", "status", "success"),
                ("publish_records", "status", "failed"),
                ("mhz_synced_orders", "status", 2)):
        canonical, _row = ps.canonical_state_from_raw(*key)
        assert canonical is None or canonical == "unknown", (
            f"{key} 有了单轴终态 —— 上游/浏览器自报被当成结算真值(§12.1 逐字禁止)"
        )


def _facts(**overrides) -> ps.PublicationFacts:
    base = dict(
        source_table="mhz_publish_order_items",
        source_column="status",
        raw_value="published",
        external_start_recorded=True,
        url_verification_state=None,
        url_availability_state=None,
        legal_rule_hit=False,
        mirror_conflict=False,
    )
    base.update(overrides)
    return ps.PublicationFacts(**base)


def test_fin16_published_needs_the_verification_axis():
    """FIN-16:published + 未核实 → hold_frozen;+ 已核实 → commit。"""
    state, direction = ps.project(_facts(url_verification_state=None))
    assert (state, direction) == ("reported_success_unverified", "hold_frozen")

    for unverified in ("unverified", "pending", "needs_action"):
        state, direction = ps.project(_facts(url_verification_state=unverified))
        assert (state, direction) == ("reported_success_unverified", "hold_frozen"), (
            f"{unverified} 被当成已核实 —— 钱会在没核实的情况下 commit"
        )

    for verified in ("verified", "content_matched"):
        state, direction = ps.project(_facts(url_verification_state=verified))
        assert (state, direction) == ("verified_published", "commit")

    # 未登记的核实态必抛(不许静默当成未核实,也不许当成已核实)。
    with pytest.raises(ps.SettlementProjectionError):
        ps.project(_facts(url_verification_state="looks_ok"))


def test_fin16_failed_forks_on_external_start_marker():
    """FIN-16:failed + 已 external-start → hold_or_quarantine;未 external-start → release。"""
    state, direction = ps.project(
        _facts(raw_value="failed", external_start_recorded=True),
    )
    assert (state, direction) == ("failed_unknown", "hold_or_quarantine"), (
        "已对外过却自动退款 = 把接线故障伪装成正常退款(§12.1 逐字禁止)"
    )

    state, direction = ps.project(
        _facts(raw_value="failed", external_start_recorded=False),
    )
    assert (state, direction) == ("failed_no_effect", "release")


def test_fin16_unknown_raw_value_is_unknown_not_failed():
    """FIN-16:不在 census 的 raw 值一律 unknown/hold_or_quarantine,**不是** failed。"""
    for raw in ("some_new_status", "PUBLISHED", "", None, 42, "success"):
        canonical, row = ps.canonical_state_from_raw(
            "mhz_publish_order_items", "status", raw,
        )
        if row is not None:                       # "success" 只在 publish_records 里登记
            continue
        assert canonical == "unknown", f"{raw!r} 没落 unknown"
        state, direction = ps.project(_facts(raw_value=raw))
        assert (state, direction) == ("unknown", "hold_or_quarantine"), (
            f"{raw!r} 走进了 else 兜底 —— FIN-16 点名要红的形态"
        )

    # 未登记的来源表/列必抛。
    with pytest.raises(ps.SettlementProjectionError):
        ps.canonical_state_from_raw("some_other_table", "status", "queued")


def test_fin16_mirror_conflict_and_retraction_precedence():
    """FIN-12:镜像冲突优先于任何一轴;未核实过的下架是 unknown 不是 retracted。"""
    state, direction = ps.project(
        _facts(raw_value="published", url_verification_state="verified", mirror_conflict=True),
    )
    assert (state, direction) == ("conflict", "hold_or_quarantine"), (
        "资金随镜像翻转 —— §12.1 末行「canonical item 优先」的红"
    )

    state, _ = ps.project(
        _facts(url_verification_state="verified", url_availability_state="retracted"),
    )
    assert state == "retracted"

    state, direction = ps.project(
        _facts(url_verification_state="pending", url_availability_state="retracted"),
    )
    assert (state, direction) == ("unknown", "hold_or_quarantine"), (
        "从没核实过就下架却落 retracted —— 会保留一笔从未验证过的 committed"
    )


def test_fin12_url_and_delivery_eligibility_pinned():
    """FIN-12/DEL-04:URL 资格与「计当前有效交付」的分母逐态钉死。"""
    census = ps.census()
    assert set(census["urlEligibleStates"]) == {"verified_published", "retracted"}
    assert set(census["currentDeliveryStates"]) == {"verified_published"}

    for state in ps.CANONICAL_STATES:
        assert ps.url_eligible(state) is (state in ("verified_published", "retracted"))
        assert ps.counts_current_delivery(state) is (state == "verified_published")

    for bad in ("published", "success", None, "", "VERIFIED_PUBLISHED"):
        with pytest.raises(ps.SettlementProjectionError):
            ps.settlement_direction(bad)


_LEGAL_TRUTH_ROWS = [
    # (canonical, funding, command, availability, url)
    ("verified_published", "committed", "completed", "active", "https://example.com/a"),
    ("reported_success_unverified", "frozen", "running", "unknown", None),
    ("reported_success_unverified", "pending_reconciliation", "settlement_pending",
     "unknown", None),
    ("queued", "frozen", "queued", "unknown", None),
    ("failed_no_effect", "released", "failed", "not_published", None),
    ("rejected_no_effect", "released", "cancelled", "not_published", None),
    ("failed_unknown", "quarantined", "quarantined", "unknown", None),
    ("unknown", "pending_reconciliation", "settlement_pending", "unknown", None),
    ("conflict", "frozen", "needs_action", "unknown", None),
    ("retracted", "committed", "completed", "retracted", "https://example.com/a"),
    ("not_started", "frozen", "queued", "not_published", None),
]


@pytest.mark.parametrize("canonical, funding, command, availability, url", _LEGAL_TRUTH_ROWS)
def test_fin12_truth_table_accepts_legal_combinations(
    canonical, funding, command, availability, url,
):
    """FIN-12:合法组合必不抛(证明真值表不是恒抛)。"""
    ps.assert_truth_table(
        canonical_publication_state=canonical, funding_state=funding,
        command_state=command, availability=availability,
        public_url=url, is_platform_cost=False,
    )


@pytest.mark.parametrize(
    "kwargs, why",
    [
        (dict(canonical_publication_state="verified_published", funding_state="released",
              command_state="completed", availability="active",
              public_url="https://example.com/a"),
         "verified + released(已核实发布却把钱退了)"),
        (dict(canonical_publication_state="reported_success_unverified",
              funding_state="committed", command_state="running",
              availability="unknown", public_url=None),
         "unverified + committed(没核实就扣钱)"),
        (dict(canonical_publication_state="unknown", funding_state="released",
              command_state="settlement_pending", availability="unknown", public_url=None),
         "unknown + released(用自动退款掩盖接线故障)"),
        (dict(canonical_publication_state="retracted", funding_state="committed",
              command_state="completed", availability="active",
              public_url="https://example.com/a"),
         "retracted + active(已下架仍对外说在线)"),
        (dict(canonical_publication_state="failed_no_effect", funding_state="released",
              command_state="failed", availability="not_published",
              public_url="https://example.com/a"),
         "no-effect 仍带 public URL"),
    ],
)
def test_fin12_truth_table_rejects_the_five_named_illegal_combinations(kwargs, why):
    """FIN-12:§15.7 点名的五种非法组合逐个必抛。"""
    with pytest.raises(ps.SettlementProjectionError):
        ps.assert_truth_table(is_platform_cost=False, **kwargs)


def test_fin12_truth_table_extra_guards():
    """FIN-12:verified 缺 URL、commandState 串格、平台腿伪装 wallet freeze 都必抛。"""
    # verified_published 必须带已验证 URL。
    with pytest.raises(ps.SettlementProjectionError):
        ps.assert_truth_table(
            canonical_publication_state="verified_published", funding_state="committed",
            command_state="completed", availability="active",
            public_url=None, is_platform_cost=False,
        )
    # commandState 串格。
    with pytest.raises(ps.SettlementProjectionError):
        ps.assert_truth_table(
            canonical_publication_state="verified_published", funding_state="committed",
            command_state="queued", availability="active",
            public_url="https://example.com/a", is_platform_cost=False,
        )
    # 平台成本腿:fundingState 恒 exempt_recorded。
    ps.assert_truth_table(
        canonical_publication_state="verified_published", funding_state="exempt_recorded",
        command_state="completed", availability="active",
        public_url="https://example.com/a", is_platform_cost=True,
    )
    with pytest.raises(ps.SettlementProjectionError):
        ps.assert_truth_table(
            canonical_publication_state="verified_published", funding_state="frozen",
            command_state="completed", availability="active",
            public_url="https://example.com/a", is_platform_cost=True,
        )
    # 反向:非平台腿不许用 exempt_recorded 冒充。
    with pytest.raises(ps.SettlementProjectionError):
        ps.assert_truth_table(
            canonical_publication_state="verified_published", funding_state="exempt_recorded",
            command_state="completed", availability="active",
            public_url="https://example.com/a", is_platform_cost=False,
        )


def test_fin12_availability_projection_covers_every_canonical_state():
    """FIN-12:对外 availability 逐态有值,且只有 verified_published 报 active。"""
    availability = ps.census()["availability"]
    assert set(availability) == set(ps.CANONICAL_STATES)
    assert [s for s, v in availability.items() if v == "active"] == ["verified_published"]
    assert [s for s, v in availability.items() if v == "retracted"] == ["retracted"]
    for state in ps.CANONICAL_STATES:
        assert ps.public_availability(state) == availability[state]


# ══════════════════════════════════════════════════════════════════════════
# G. settlement_review —— Z-1
# ══════════════════════════════════════════════════════════════════════════
def test_z1_admin_actions_are_exactly_three():
    """Z-1:平台 admin 的逐条动作集恰三条(commit / release / hold)。"""
    census = sr.census()
    assert sr.ADMIN_ACTIONS == ("admin_commit", "admin_release", "admin_hold")
    assert census["adminActions"] == list(sr.ADMIN_ACTIONS)
    assert len(sr.ADMIN_ACTIONS) == 3

    # 每条动作都要有目标资金态登记与人话文案(「先放着」不许是无名动作)。
    assert set(census["actionTargetState"]) == set(sr.ADMIN_ACTIONS)
    assert census["actionTargetState"]["admin_commit"] == "committed"
    assert census["actionTargetState"]["admin_release"] == "released"
    assert census["actionTargetState"]["admin_hold"] is None, (
        "「维持隔离」不得改资金态 —— 它的作用是留痕 + 计时"
    )
    assert census["reasonRequiredActions"] == ["admin_hold"]
    for action in sr.ADMIN_ACTIONS:
        assert census["actionLabels"][action].strip()

    # 服务商侧只有一个写动作,且不在 admin 动作集里(收口权在平台)。
    assert sr.PROVIDER_ACTION not in sr.ADMIN_ACTIONS


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_z1_admin_hold_requires_a_written_reason(reason):
    """Z-1:「维持隔离」无理由必抛 —— 没有理由的「先放着」等于把死路写进账本。"""
    with pytest.raises(sr.ReviewError):
        asyncio.run(sr.apply_admin_action(
            None, publish_command_id="cmd-1", action="admin_hold",
            admin_user_id=1, reason=reason,
        ))


def test_z1_admin_hold_with_reason_passes_the_guard():
    """Z-1:必须不命中的那一半 —— 填了理由就不该被这道门拦住。

    🔴 判别力证明:cur=None 时它会往下走到 store 层并因空游标炸 AttributeError。
       「不是 ReviewError」就说明理由这道门放行了,而不是恒抛。
    """
    with pytest.raises(Exception) as exc:
        asyncio.run(sr.apply_admin_action(
            None, publish_command_id="cmd-1", action="admin_hold",
            admin_user_id=1, reason="上游回执矛盾,等线下核实凭证",
        ))
    assert not isinstance(exc.value, sr.ReviewError), (
        f"填了理由仍被拦 —— 这道门恒抛,零判别力(实得 {exc.value!r})"
    )


@pytest.mark.parametrize("action", ["commit", "refund", "ADMIN_COMMIT", "", None,
                                    "provider_evidence"])
def test_z1_unknown_admin_action_is_refused(action):
    """Z-1:动作集之外的处置必抛(含服务商动作 —— 服务商不许自己动钱)。"""
    with pytest.raises(sr.ReviewError):
        asyncio.run(sr.apply_admin_action(
            None, publish_command_id="cmd-1", action=action, admin_user_id=1, reason="x",
        ))


def test_z1_queue_membership_excludes_settled_commands():
    """Z-1:已收敛的账不许再被人工动一次(committed/released 一律拒)。"""
    for settled in ("committed", "released", "frozen", "exempt_recorded", "", None):
        with pytest.raises(sr.ReviewError):
            sr.assert_queue_member({"funding_state": settled})

    # 必须不命中的那一半:队列态取自 census,逐个放行。
    queue_states = sr.census()["queueFundingStates"]
    assert queue_states, "核验队列的分母为空 —— 判据会打在空气上"
    assert set(queue_states) == {"pending_reconciliation", "quarantined"}
    for state in queue_states:
        sr.assert_queue_member({"funding_state": state})


# ══════════════════════════════════════════════════════════════════════════
# H. reconciler
# ══════════════════════════════════════════════════════════════════════════
def test_reconciler_coverage_declares_all_seven_items():
    """FIN-13:§12.3 六项覆盖齐全 + 包E 追加的第 7 项,逐项有实现坐标。

    🔴 [R1] 名字里的数字跟着断言一起改。上一轮我把断言从 6 改成了 7 却留着
       ``..._all_six_items`` 这个名字 —— 判据名是**给人读的判据摘要**,
       名实不符会让复核者按名字去信一件断言里根本没写的事。

    [包E 2026-08-24] §12.3 的六项 **+** 包E 接执行器时补的第 7 项
    (「队列判定跑不通且零 external-start ⇒ release 一次」)。
    🔴 改断言不是放宽:①下面逐项验 case/坐标/符号真实存在;②这里额外钉住
       **前六项就是 §12.3 那六项**(前缀相等),第 7 项只能加在末尾,
       不能借"加一项"把原来某一项换掉。
    """
    coverage = rc.coverage()
    items = coverage["items"]
    assert [i["id"] for i in items] == [1, 2, 3, 4, 5, 6, 7], "覆盖项缺号或乱序"
    assert [i["id"] for i in items[:6]] == [1, 2, 3, 4, 5, 6], (
        "§12.3 原六项必须原位保留 —— 新增项只能追加在末尾")

    for item in items:
        assert item["case"].strip(), f"第 {item['id']} 项没有 case 描述"
        assert item["coveredBy"].strip(), f"第 {item['id']} 项没有实现坐标"
        assert isinstance(item["inThisModule"], bool)
        if item["inThisModule"]:
            symbol = item["coveredBy"].split("(", 1)[0].strip()
            assert hasattr(rc, symbol), (
                f"第 {item['id']} 项声称由本模块的 {symbol} 覆盖,但模块里没有这个符号 —— "
                "声明与实现脱节"
            )

    # 第 1 项显式声明由别处覆盖 —— 不许把「别人管」写成「本模块管」。
    assert items[0]["inThisModule"] is False
    assert "activation" in items[0]["coveredBy"]

    assert coverage["pendingAlertSeconds"] == 7 * 24 * 3600, "Z-1 逐字:pending 超 7 天告警"
    assert rc.PENDING_ALERT_SECONDS == coverage["pendingAlertSeconds"]


def test_reconciler_never_releases_any_hold_or_quarantine_state():
    """FIN-08:``neverReleasesStates`` 恰等于所有 hold_or_quarantine 的态。"""
    never = rc.coverage()["neverReleasesStates"]

    # ① 逐字钉死(改 _DIRECTION 把某个未知态挪去 release 会走到这里)。
    assert never == ["conflict", "failed_unknown", "rejected_unknown", "unknown"]

    # ② 与钱向表双向核对:一态不多一态不少。
    directions = ps.census()["directions"]
    assert set(never) == {s for s, d in directions.items() if d == "hold_or_quarantine"}

    # ③ 判别力:这四个态确实**不是** release 方向,而 release 方向确实另有其态。
    for state in never:
        assert ps.settlement_direction(state) != "release"
    assert {s for s, d in directions.items() if d == "release"} == {
        "rejected_no_effect", "failed_no_effect",
    }
