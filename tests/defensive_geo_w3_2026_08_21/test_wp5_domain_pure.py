"""窗C · WP5 纯函数层判据:媒体身份 / 正文指纹 / 决策冻结快照 / 广告法门。

不打库。每条判据的 docstring **首行**写清对应验收 ID。

🔴 分母纪律:凡是「集合/矩阵/闭表」类的判据,分母一律从被测模块的 ``census()``
   机械取,判据自己只写**期望值**。手抄分母漏掉的那一项不会让任何判据变红。

🔴 判别力纪律:每一条「必须抛」的判据旁边都配一条「必须不抛」——
   恒抛的守卫和写对了的守卫长得一模一样。
"""

from __future__ import annotations

import copy
import unicodedata

import pytest

from services.defensive_geo.publish import decision_snapshot as ds
from services.defensive_geo.publish import legal_gate as lg
from services.defensive_geo.publish import media_identity as mi
from services.defensive_geo.publish.body_hash import (
    BODY_HASH_VERSION,
    BodyHashError,
    assert_body_hash,
    body_hash,
    is_body_hash,
)

# ══════════════════════════════════════════════════════════════════════════
# 夹具:三个不同媒体 + 一个同根子域。**不打库** —— project_media_row 只吃 Mapping。
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 目录行**必须带私有列真值**:私有列全 NULL 的夹具会让泄漏判据零判别力
#:    (什么都不写自然什么都不漏)。这里逐列给非空值。
_PRIVATE_PAYLOAD = {
    "provider": "mhz",
    "price": 12000,
    "price1": 11000,
    "price2": 10000,
    "wholesale_cents": 8800,
    "platform_cost_cents": 900,
    "remark": "内部备注:这家可以压价",
    "entrance_link": "https://internal.example/entrance/8821",
}


def _row(media_id: int, domain: str, name: str = "判据媒体") -> dict:
    return {
        "id": media_id,
        "media_name": f"{name}{media_id}",
        "source_domain": domain,
        "provider_media_id": media_id + 500000,
        **_PRIVATE_PAYLOAD,
    }


def _identity(
    *,
    media_id: int = 990001,
    domain: str = "example-daily.com.cn",
    ordinal: int = 0,
    snapshot_id: str = "dsnap-w3c-1",
    role: str = "authority_anchor",
) -> mi.PublicMediaIdentity:
    return mi.project_media_row(
        _row(media_id, domain),
        media_role=role,
        decision_snapshot_id=snapshot_id,
        ordinal=ordinal,
    )


# ══════════════════════════════════════════════════════════════════════════
# A. media_identity —— MED-06 / MED-12 / POR-06 / POR-07
# ══════════════════════════════════════════════════════════════════════════
def test_med06_same_media_two_options_counts_as_one_media():
    """MED-06:同一媒体的两个 option 只算一家媒体、一个根域。

    去重键是冻结的 publicMediaKey,**不是** option id —— 按 option 去重会让
    「同一家媒体挂两条库存」冒充两家媒体,MED-06 要防的正是这一形态。
    """
    a = _identity(ordinal=0)
    b = _identity(ordinal=1)

    assert a.public_media_option_id != b.public_media_option_id, (
        "两个 option 必须是两个不同的选择句柄,否则 option 无法区分候选"
    )
    assert a.public_media_key == b.public_media_key
    assert mi.distinct_public_media([a, b]) == 1
    assert mi.distinct_root_domains([a, b]) == 1


def test_med06_two_subdomains_of_one_root_count_as_one_root():
    """MED-06:同 root 的两个子域算一个根域,但仍是两家媒体。"""
    parent = _identity(media_id=990002, domain="trade-example.com", ordinal=0)
    child = _identity(media_id=990003, domain="news.trade-example.com", ordinal=1)

    assert parent.public_media_key != child.public_media_key
    assert mi.distinct_public_media([parent, child]) == 2
    assert mi.distinct_root_domains([parent, child]) == 1, (
        "子域冒充第二个根域 = MED-06 点名的「同 root 多 URL 冒充多个根域」"
    )


def test_med12_por06_private_columns_absent_from_public_dtos():
    """MED-12/POR-06:两个公开 DTO 一列私有字段都不含(分母取自 census)。

    分母 = ``census()['privateColumns']`` + ``privateColumnPrefixes``,
    不手抄 —— 手抄漏掉的那一列恰恰是判据要证明「没漏」的那一列。
    """
    census = mi.census()
    private_columns = census["privateColumns"]
    prefixes = census["privateColumnPrefixes"]
    assert private_columns, "census 的私有列分母为空 —— 判据会打在空气上"
    assert prefixes, "census 的私有前缀分母为空"

    identity = _identity()
    provider_dto = identity.provider_dto()
    customer_dto = identity.customer_dto()

    for column in private_columns:
        assert column not in provider_dto, f"provider_dto 泄漏私有列 {column}"
        assert column not in customer_dto, f"customer_dto 泄漏私有列 {column}"

    # 前缀规则同样是分母的一部分:新加一列 cost_xxx 必须被自动纳管。
    for prefix in prefixes:
        synthetic = f"{prefix}w3c_probe"
        assert mi.is_private_column(synthetic), f"前缀 {prefix} 没有被纳管"
        assert synthetic not in provider_dto
        assert synthetic not in customer_dto

    # 反向:公开 DTO 的每个键都必须**不是**私有列(挡住「换个名字塞进去」)。
    assert mi.private_columns_in(provider_dto) == []
    assert mi.private_columns_in(customer_dto) == []

    # 私有原料确实在输入里(证明这一条不是「输入本来就干净」的空转)。
    assert mi.private_columns_in(_row(990001, "example-daily.com.cn"))


def test_por07_customer_dto_carries_no_points_and_no_internal_handles():
    """POR-07:客户投影不含 points,也不含 option/media/root 三个内部句柄。"""
    census = mi.census()
    customer_dto = _identity().customer_dto()

    assert set(customer_dto) == set(census["customerMediaFields"])

    for key in customer_dto:
        assert "point" not in key.lower(), f"客户投影出现算力字段 {key}"

    for handle in ("publicMediaOptionId", "publicMediaKey", "canonicalRootDomainKey"):
        assert handle not in customer_dto, (
            f"客户投影出现内部句柄 {handle} —— 它会让客户面与服务商面产生可关联 opaque ref"
        )

    # 判别力:那三个句柄**确实**在服务商面上(否则这条断言在两边都空的情况下也绿)。
    provider_dto = _identity().provider_dto()
    for handle in ("publicMediaOptionId", "publicMediaKey", "canonicalRootDomainKey"):
        assert handle in provider_dto


def test_por06_assert_no_private_leak_has_discriminating_power():
    """POR-06:私有键名必抛 / 纯公开 payload 必不抛(证明它不是恒抛)。"""
    with pytest.raises(mi.PrivateColumnLeak):
        mi.assert_no_private_leak({"provider_media_id": 1})

    # 嵌套一层也要抓到 —— 泄漏往往藏在 list[dict] 里。
    with pytest.raises(mi.PrivateColumnLeak):
        mi.assert_no_private_leak({"options": [{"publicMediaName": "x"}, {"wholesale_cents": 8800}]})

    # 必须不命中的那一半。
    clean = {
        "options": [_identity(ordinal=0).provider_dto(), _identity(ordinal=1).provider_dto()],
        "customer": _identity().customer_dto(),
        "exactPoints": 120,
    }
    mi.assert_no_private_leak(clean)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # com.cn 一族取三段
        ("example-daily.com.cn", "example-daily.com.cn"),
        ("a.b.example-daily.com.cn", "example-daily.com.cn"),
        ("shop.local-example.net.cn", "local-example.net.cn"),
        # 普通后缀取两段
        ("trade-example.com", "trade-example.com"),
        ("news.trade-example.com", "trade-example.com"),
        ("a.b.c.trade-example.com", "trade-example.com"),
        # scheme / path / query / fragment / 端口 / 大小写 / 尾点 / www 归一
        ("https://www.News.Trade-Example.com/p/1?a=1#f", "trade-example.com"),
        ("http://trade-example.com:8080/x", "trade-example.com"),
        ("WWW.TRADE-EXAMPLE.COM.", "trade-example.com"),
        ("  news.trade-example.com  ", "trade-example.com"),
    ],
)
def test_med06_canonical_root_domain_normalizes(raw, expected):
    """MED-06:root domain 归一化 —— com.cn 一族三段、普通两段、脏值归一。"""
    assert mi.canonical_root_domain(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "localhost", "example", "not a domain.com", "例子.com", "-bad.com", 12345, None],
)
def test_med06_canonical_root_domain_rejects_illegal(bad):
    """MED-06:非法域名一律抛 —— 猜一个 root 出来会让去重口径静默漂移。"""
    with pytest.raises(mi.MediaIdentityError):
        mi.canonical_root_domain(bad)


def test_med12_public_identity_is_stable_and_snapshot_bound():
    """MED-12:同输入跨调用逐值不变;option id 绑 snapshot,跨 snapshot 必不同。"""
    first = _identity(snapshot_id="dsnap-A", ordinal=0)
    again = _identity(snapshot_id="dsnap-A", ordinal=0)
    other_snapshot = _identity(snapshot_id="dsnap-B", ordinal=0)

    assert first == again, "身份必须稳定 —— 漂移会让跨 preview→confirm→status 的引用断裂"
    assert first.public_media_key == other_snapshot.public_media_key
    assert first.public_media_option_id != other_snapshot.public_media_option_id, (
        "option id 必须绑 snapshot,否则跨 snapshot 复用能解析成功(§15.7)"
    )


def test_med12_public_media_key_is_not_the_upstream_id():
    """MED-12/POR-06:公开身份不是上游 media id 的任何可见形态。"""
    row = _row(990001, "example-daily.com.cn")
    identity = mi.project_media_row(
        row, media_role="authority_anchor", decision_snapshot_id="dsnap-A", ordinal=0,
    )
    for probe in (str(row["id"]), str(row["provider_media_id"]), row["provider"]):
        assert probe not in identity.public_media_key
        assert probe not in identity.canonical_root_domain_key
        assert probe not in identity.public_media_option_id


# ══════════════════════════════════════════════════════════════════════════
# B. body_hash —— DEL-06
# ══════════════════════════════════════════════════════════════════════════
_BODY = "第一行正文\n第二行正文\n结尾"


def test_del06_newline_variants_hash_identically():
    """DEL-06:CRLF / CR / LF 三种换行必须同 hash。"""
    lf = _BODY
    crlf = _BODY.replace("\n", "\r\n")
    cr = _BODY.replace("\n", "\r")
    assert lf != crlf != cr
    assert body_hash(lf) == body_hash(crlf) == body_hash(cr)


def test_del06_nfc_equivalents_hash_identically():
    """DEL-06:NFC 等价形必须同 hash。"""
    composed = "café 咖啡 骨"          # é = U+00E9
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed, "夹具没构造出真正的等价异形 —— 这条判据会零判别力"
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert body_hash(composed) == body_hash(decomposed)


@pytest.mark.parametrize(
    "left, right, why",
    [
        ("Ａ级服务方案", "A级服务方案", "全角/半角(NFKC 会把它们折成同一个 —— 那是过度归一)"),
        ("①号方案", "1号方案", "圈码数字(NFKC 会折,NFC 不折)"),
        ("方案说明，共三条", "方案说明,共三条", "中英文标点差异"),
        ("正文一行\n", "正文一行", "行尾换行(rstrip 会抹掉)"),
        ("正文一行   \n第二行", "正文一行\n第二行", "行尾空白(逐行 rstrip 会抹掉)"),
        ("正文\n\n\n\n\n第二段", "正文\n\n第二段", "多空行(折叠 4+ 空行会抹掉)"),
        ("方案 A", "方案  A", "行内空白数量"),
        ("HELLO", "hello", "大小写(casefold 会折)"),
    ],
)
def test_del06_no_over_normalization(left, right, why):
    """DEL-06:标点/空白/措辞不同**必须**得到不同 hash(反向锁:防 NFKC / rstrip)。

    这一发是「把 NFC 换成 NFKC」或「加一行 strip/折叠空行」的直接杀手 ——
    过度归一会让两篇真正不同的稿子撞成同一指纹,进而被 H0 判 duplicate 并阻断合法交付。
    """
    assert body_hash(left) != body_hash(right), f"过度归一:{why}"


def test_del06_version_prefix_is_inside_the_fingerprint():
    """DEL-06/DEL-08:指纹带版本前缀,裸 hex 一律不认。"""
    value = body_hash(_BODY)
    assert value.startswith(BODY_HASH_VERSION + ":")
    assert is_body_hash(value) is True

    bare_hex = value.split(":", 1)[1]
    assert len(bare_hex) == 64
    assert is_body_hash(bare_hex) is False, (
        "裸 hex 会让「换了算法」和「正文改了」长得一模一样"
    )
    assert is_body_hash("body-sha256-v2:" + bare_hex) is False
    assert is_body_hash(value[:-1] + "z") is False        # 非 hex 字符
    assert is_body_hash(value + "0") is False             # 长度不对

    assert assert_body_hash(value) == value               # 合法值必不抛
    for bad in (bare_hex, None, 123, "", "body-sha256-v1:"):
        with pytest.raises(BodyHashError):
            assert_body_hash(bad)


def test_del06_non_string_body_is_refused():
    """DEL-06:非 str 正文拒绝指纹,不指纹一个猜出来的值。"""
    for bad in (None, 123, b"bytes body", ["line"]):
        with pytest.raises(BodyHashError):
            body_hash(bad)


# ══════════════════════════════════════════════════════════════════════════
# E. decision_snapshot —— MED-14 / MED-17 / MED-21
# ══════════════════════════════════════════════════════════════════════════
_BUDGET = {
    "capPoints": 100000,
    "reservedPoints": 1200,
    "committedPoints": 800,
    "remainingPoints": 98000,
}


def _candidate(*, media_id: int, domain: str, ordinal: int, points: int) -> ds.Candidate:
    return ds.Candidate(
        identity=_identity(media_id=media_id, domain=domain, ordinal=ordinal),
        reason_facts=(("industry_fit", "行业相关"), ("authority_fit", "权威度匹配")),
        exact_points=points,
    )


def _freeze(*, decision=None, alternatives=None, **overrides):
    decision = decision or _candidate(media_id=990001, domain="example-daily.com.cn",
                                      ordinal=0, points=120)
    if alternatives is None:
        alternatives = [
            decision,
            _candidate(media_id=990002, domain="trade-example.com", ordinal=1, points=80),
        ]
    kwargs = dict(
        decision_snapshot_id="dsnap-w3c-1",
        publish_slot_id="pslot_w3c",
        snapshot_version=1,
        expires_at="2026-08-22T00:00:00Z",
        accepted_snapshot_id="acc-1",
        accepted_snapshot_hash="acchash-1",
        service_projection_id="svc-1",
        service_projection_version=3,
        plan_item_key="plan-item-1",
        question_key="q-1",
        question_revision=2,
        article_revision_id="rev-1",
        article_hash=body_hash("冻结正文"),
        pricing_catalog_version="pricing-v1",
        inventory_snapshot_version="inv-v1",
        execution_budget_snapshot_id="ebs-1",
        execution_budget_version=1,
        global_budget=dict(_BUDGET),
        scope_budget=dict(_BUDGET),
        decision=decision,
        alternatives=list(alternatives),
        publish_item_request_id="pireq_w3c",
        replacement_policy_version="repl-v1",
        funding_policy="personal_wallet",
        principal_kind="personal",
        approval_requirement="not_required",
        sponsor_policy_ref=None,
    )
    kwargs.update(overrides)
    return ds.freeze_snapshot(**kwargs)


def test_med17_frozen_funding_matrix_five_rows_all_legal():
    """MED-17:五行冻结资金矩阵逐行合法(分母取自 census,不手抄)。"""
    rows = ds.census()["frozenFundingRows"]
    assert len(rows) == 5, f"冻结资金矩阵必须是五行(organization 占两行),实得 {len(rows)}"

    for row in rows:
        ref = "sponsor-policy-ref-1" if row["sponsorRefRequired"] else None
        out = ds.validate_frozen_funding(
            funding_policy=row["fundingPolicy"],
            principal_kind=row["principalKind"],
            approval_requirement=row["approvalRequirement"],
            sponsor_policy_ref=ref,
        )
        assert out.principal_kind == row["principalKind"]
        assert ds.MANDATORY_INSUFFICIENT_OPTION in row["allowedOptionKinds"], (
            "cancel_no_charge 必须始终在每一行的出口里(§15.7 逐字)"
        )


@pytest.mark.parametrize(
    "kwargs, why",
    [
        (dict(funding_policy="personal_wallet", principal_kind="organization",
              approval_requirement="not_required", sponsor_policy_ref=None),
         "personal 格串 organization principalKind"),
        (dict(funding_policy="organization_budget", principal_kind="personal",
              approval_requirement="required", sponsor_policy_ref=None),
         "organization 格串 personal principalKind"),
        (dict(funding_policy="admin_platform_ledger", principal_kind="personal",
              approval_requirement="not_required", sponsor_policy_ref=None),
         "平台成本中心格串 personal"),
        (dict(funding_policy="sponsor_platform_ledger", principal_kind="platform_cost_center",
              approval_requirement="not_required", sponsor_policy_ref=None),
         "sponsor 缺 policy ref —— 缺 ref 的 sponsor 与 admin 无法区分"),
        (dict(funding_policy="admin_platform_ledger", principal_kind="platform_cost_center",
              approval_requirement="not_required", sponsor_policy_ref="ref-1"),
         "admin 多了 sponsor ref"),
        (dict(funding_policy="personal_wallet", principal_kind="personal",
              approval_requirement="required", sponsor_policy_ref=None),
         "personal + required 是矩阵里不存在的格"),
        (dict(funding_policy="wechat_pay", principal_kind="personal",
              approval_requirement="not_required", sponsor_policy_ref=None),
         "第五种 fundingPolicy"),
    ],
)
def test_med17_frozen_funding_matrix_rejects_cross_cell(kwargs, why):
    """MED-17:冻结资金矩阵错格必抛(四类 payer 串格是变异 157 的形态)。"""
    with pytest.raises(ds.SnapshotError):
        ds.validate_frozen_funding(**kwargs)


def test_med17_live_approval_state_conserves_frozen_requirement():
    """MED-17:live approvalState 必须与 frozen approvalRequirement 守恒。"""
    # 必须抛的那一半:organization/required 下 live 说 not_required。
    with pytest.raises(ds.SnapshotError):
        ds.validate_live_approval(
            funding_policy="organization_budget", approval_requirement="required",
            approval_state="not_required",
        )
    # personal 那一行只允许 not_required —— 借 approved 提权同样要红。
    with pytest.raises(ds.SnapshotError):
        ds.validate_live_approval(
            funding_policy="personal_wallet", approval_requirement="not_required",
            approval_state="approved",
        )
    # 必须不抛的那一半(证明不是恒抛)。
    for state in ("required", "approved", "rejected"):
        ds.validate_live_approval(
            funding_policy="organization_budget", approval_requirement="required",
            approval_state=state,
        )
    ds.validate_live_approval(
        funding_policy="personal_wallet", approval_requirement="not_required",
        approval_state="not_required",
    )


def test_med17_confirmability_requires_approved_for_required_row():
    """MED-17:organization/required 只有 approved 能 canConfirm。"""
    confirm = ds.confirmability(
        lifecycle="open", funding_policy="organization_budget",
        approval_requirement="required", approval_state="approved",
        blocked_reason=None, target_ids={}, label_for=lambda k: f"文案:{k}",
    )
    assert confirm["canConfirm"] is True

    with pytest.raises(ds.SnapshotError):
        ds.confirmability(
            lifecycle="open", funding_policy="organization_budget",
            approval_requirement="required", approval_state="required",
            blocked_reason=None, target_ids={}, label_for=lambda k: f"文案:{k}",
        )


def test_med17_sidecar_can_confirm_true_must_carry_no_blockers():
    """MED-17:canConfirm=true 带 blockers/options 必抛。"""
    confirm = {"canConfirm": True, "reasonCode": None}
    ds.assert_sidecar_consistent(confirm=confirm, blockers=[], options=[])   # 必不抛

    blocker = {"scope": "media_publication", "requiredExactPoints": 120,
               "remainingPoints": 20, "deltaPoints": 100}
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=[blocker], options=[])
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(
            confirm=confirm, blockers=[], options=[{"kind": "cancel_no_charge"}],
        )


def _insufficient(**blocker_overrides):
    blocker = {"scope": "media_publication", "requiredExactPoints": 120,
               "remainingPoints": 20, "deltaPoints": 100}
    blocker.update(blocker_overrides)
    return (
        {"canConfirm": False, "reasonCode": "insufficient_points"},
        [blocker],
        [{"kind": "top_up"}, {"kind": "cancel_no_charge"}],
    )


def test_med17_insufficient_points_sidecar_contract():
    """MED-17:资金不足必须给出可复算差额 + 始终包含 cancel_no_charge。"""
    confirm, blockers, options = _insufficient()
    ds.assert_sidecar_consistent(confirm=confirm, blockers=blockers, options=options)

    # ① 空 options 必抛。
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=blockers, options=[])
    # ② 空 blockers 必抛。
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=[], options=options)
    # ③ options 里没有 cancel_no_charge 必抛。
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(
            confirm=confirm, blockers=blockers, options=[{"kind": "top_up"}],
        )
    # ④ 差额算错必抛(120 - 20 != 99)。
    confirm, bad_blockers, options = _insufficient(deltaPoints=99)
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=bad_blockers, options=options)
    # ⑤ 金额不是整数必抛。
    confirm, float_blockers, options = _insufficient(deltaPoints=100.0)
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=float_blockers, options=options)
    # ⑥ blocker 缺 scope 必抛。
    confirm, no_scope, options = _insufficient(scope=None)
    with pytest.raises(ds.SnapshotError):
        ds.assert_sidecar_consistent(confirm=confirm, blockers=no_scope, options=options)


def test_med17_terminal_lifecycle_sidecar_is_inactive():
    """MED-17:lifecycle 终结态必须是 InactiveSidecar(blockers/options 全空)。"""
    for lifecycle, expected_reason in ds.census()["lifecycleReason"].items():
        confirm = ds.confirmability(
            lifecycle=lifecycle, funding_policy="personal_wallet",
            approval_requirement="not_required", approval_state="not_required",
            # 🔴 调用方给的 blocked_reason 一律不采信 —— 终结态的 reason 由 lifecycle 决定。
            blocked_reason="insufficient_points",
            target_ids={"plan_item": "pi-1", "publish_snapshot": "dsnap-1",
                        "publish_command": "cmd-1"},
            label_for=lambda k: f"文案:{k}",
        )
        assert confirm["canConfirm"] is False
        assert confirm["reasonCode"] == expected_reason
        ds.assert_sidecar_consistent(confirm=confirm, blockers=[], options=[])
        with pytest.raises(ds.SnapshotError):
            ds.assert_sidecar_consistent(
                confirm=confirm, blockers=[], options=[{"kind": "cancel_no_charge"}],
            )


@pytest.mark.parametrize(
    "funding_policy, approval_requirement, option_kind, why",
    [
        ("personal_wallet", "not_required", "request_approval", "个人收到组织审批出口"),
        ("personal_wallet", "not_required", "contact_platform_budget_owner", "个人收到平台出口"),
        ("admin_platform_ledger", "not_required", "top_up", "平台收到个人充值出口"),
        ("sponsor_platform_ledger", "not_required", "top_up", "sponsor 收到个人充值出口"),
        ("admin_platform_ledger", "not_required", "request_approval", "平台收到组织审批出口"),
        ("organization_budget", "required", "top_up", "组织收到个人充值出口"),
    ],
)
def test_med17_assert_option_allowed_rejects_cross_leg(
    funding_policy, approval_requirement, option_kind, why,
):
    """MED-17:三条钱腿的调整出口不许互串。"""
    with pytest.raises(ds.SnapshotError):
        ds.assert_option_allowed(
            funding_policy=funding_policy, approval_requirement=approval_requirement,
            option_kind=option_kind,
        )


def test_med17_assert_option_allowed_accepts_every_own_kind():
    """MED-17:必须不命中的那一半 —— 每一行自己的出口全部放行。"""
    for row in ds.census()["frozenFundingRows"]:
        for kind in row["allowedOptionKinds"]:
            ds.assert_option_allowed(
                funding_policy=row["fundingPolicy"],
                approval_requirement=row["approvalRequirement"],
                option_kind=kind,
            )
            assert kind in ds.ADJUSTMENT_OPTION_KINDS, (
                f"{kind} 不在七类调整出口闭集里 —— 出口清单与矩阵已经打架"
            )


def test_med21_canonical_hash_covers_ordered_complete_alternatives():
    """MED-21:改字段 / 换序 / 增删候选都必须改 hash;同输入两次必同 hash。"""
    decision = _candidate(media_id=990001, domain="example-daily.com.cn",
                          ordinal=0, points=120)
    alt = _candidate(media_id=990002, domain="trade-example.com", ordinal=1, points=80)
    extra = _candidate(media_id=990004, domain="local-example.cn", ordinal=2, points=40)

    _, baseline = _freeze(decision=decision, alternatives=[decision, alt])
    _, again = _freeze(decision=decision, alternatives=[decision, alt])
    assert baseline == again, "同输入两次 hash 必须相同 —— 漂移的 hash 无法承载「冻结」"

    # ① 改任一候选字段(价格)。
    cheaper = _candidate(media_id=990002, domain="trade-example.com", ordinal=1, points=79)
    _, changed_points = _freeze(decision=decision, alternatives=[decision, cheaper])
    assert changed_points != baseline

    # ② 改候选身份(换一家媒体)。
    other_media = _candidate(media_id=990005, domain="tail-example.net", ordinal=1, points=80)
    _, changed_media = _freeze(decision=decision, alternatives=[decision, other_media])
    assert changed_media != baseline

    # ③ 🔴 换序 —— 排序会让这一发静默存活。
    _, reordered = _freeze(decision=decision, alternatives=[alt, decision])
    assert reordered != baseline, "换序没有改变 hash —— alternatives 被排序了(MED-21 逐字禁止)"

    # ④ 追加候选。
    _, appended = _freeze(decision=decision, alternatives=[decision, alt, extra])
    assert appended != baseline

    # ⑤ 删减候选。
    _, removed = _freeze(decision=decision, alternatives=[decision])
    assert removed != baseline

    # ⑥ 换 decision 本身。
    _, other_decision = _freeze(decision=alt, alternatives=[decision, alt])
    assert other_decision != baseline

    assert len({baseline, changed_points, changed_media, reordered,
                appended, removed, other_decision}) == 7


def test_med21_option_ids_must_resolve_to_exactly_one_candidate():
    """MED-21:同一 option id 指向两个不同候选必抛;逐字同对象必不抛。"""
    identity = _identity(media_id=990001, ordinal=0)
    cheap = ds.Candidate(identity, (("industry_fit", "行业相关"),), 120)
    expensive = ds.Candidate(identity, (("industry_fit", "行业相关"),), 999)
    assert cheap.wire()["publicMediaOptionId"] == expensive.wire()["publicMediaOptionId"]

    with pytest.raises(ds.SnapshotError):
        ds.assert_option_ids_resolve(cheap, [expensive])

    # 同 id 但 reasonFacts 不同 —— 同样是两个候选。
    relabelled = ds.Candidate(identity, (("audience_fit", "受众匹配"),), 120)
    with pytest.raises(ds.SnapshotError):
        ds.assert_option_ids_resolve(cheap, [relabelled])

    # 必须不抛:decision 逐字出现在 alternatives 里是合法的。
    alt = _candidate(media_id=990002, domain="trade-example.com", ordinal=1, points=80)
    ds.assert_option_ids_resolve(cheap, [cheap, alt])


def test_med14_frozen_face_is_byte_stable_and_not_written_back():
    """MED-14:冻结面字节不随 live 面重算变化。"""
    frozen, digest = _freeze()
    snapshot_before = copy.deepcopy(frozen)

    # 跑一整轮 live 面重算。
    confirm = ds.confirmability(
        lifecycle="open", funding_policy=frozen["fundingPolicy"],
        approval_requirement=frozen["approvalRequirement"],
        approval_state="not_required", blocked_reason="insufficient_points",
        target_ids={"publish_snapshot": frozen["decisionSnapshotId"]},
        label_for=lambda k: f"文案:{k}",
    )
    _, blockers, options = _insufficient()
    ds.assert_sidecar_consistent(confirm=confirm, blockers=blockers, options=options)

    assert frozen == snapshot_before, "live sidecar 重算改写了冻结面 —— MED-14 的红"
    assert frozen["canonicalHash"] == digest

    # 冻结面里没有任何 live 字段(canConfirm/blockers/options 属于 sidecar)。
    for live_key in ("canConfirm", "budgetBlockers", "adjustmentOptions", "lifecycle",
                     "confirmability", "approvalState"):
        assert live_key not in frozen


def test_med14_frozen_snapshot_refuses_illegal_inputs():
    """MED-14:冻结面签发前逐项核 —— 不自洽就**不签发**。"""
    # ① 裸 hex 的 articleHash。
    with pytest.raises(BodyHashError):
        _freeze(article_hash="a" * 64)
    # ② 预算算术不守恒。
    with pytest.raises(ds.SnapshotError):
        _freeze(global_budget={"capPoints": 100, "reservedPoints": 10,
                               "committedPoints": 10, "remainingPoints": 90})
    # ③ 预算缺字段。
    with pytest.raises(ds.SnapshotError):
        _freeze(scope_budget={"capPoints": 100, "reservedPoints": 0, "committedPoints": 0})
    # ④ 负数 exactPoints。
    negative = ds.Candidate(_identity(), (("industry_fit", "行业相关"),), -1)
    with pytest.raises(ds.SnapshotError):
        _freeze(decision=negative, alternatives=[negative])
    # ⑤ 未登记的 reasonFact kind。
    bad_reason = ds.Candidate(_identity(), (("vibes", "感觉不错"),), 120)
    with pytest.raises(ds.SnapshotError):
        _freeze(decision=bad_reason, alternatives=[bad_reason])
    # ⑥ 资金格串格。
    with pytest.raises(ds.SnapshotError):
        _freeze(funding_policy="organization_budget", principal_kind="personal")


def test_med09_assert_exact_points_equal_value_by_value():
    """MED-09:三处 exact points 任一处不等必抛。"""
    frozen, _ = _freeze()
    points = frozen["totalExactPoints"]
    ds.assert_exact_points_equal(
        frozen=frozen, exact_settlement_points=points, item_points=points,
    )

    # ① response 层不等。
    with pytest.raises(ds.SnapshotError):
        ds.assert_exact_points_equal(
            frozen=frozen, exact_settlement_points=points + 1, item_points=points,
        )
    # ② item 层不等。
    with pytest.raises(ds.SnapshotError):
        ds.assert_exact_points_equal(
            frozen=frozen, exact_settlement_points=points, item_points=points - 1,
        )
    # ③ snapshot 层被改。
    tampered = copy.deepcopy(frozen)
    tampered["totalExactPoints"] = points + 5
    with pytest.raises(ds.SnapshotError):
        ds.assert_exact_points_equal(
            frozen=tampered, exact_settlement_points=points, item_points=points,
        )
    # ④ decision 层被改。
    tampered2 = copy.deepcopy(frozen)
    tampered2["decision"]["exactPoints"] = points + 7
    with pytest.raises(ds.SnapshotError):
        ds.assert_exact_points_equal(
            frozen=tampered2, exact_settlement_points=points, item_points=points,
        )
    # ⑤ 非整数 / 负数。
    for bad in (points + 0.0, -1, True):
        with pytest.raises(ds.SnapshotError):
            ds.assert_exact_points_equal(
                frozen=frozen, exact_settlement_points=bad, item_points=points,
            )


def test_med12_frozen_snapshot_carries_no_private_column():
    """MED-12/POR-06:冻结面整体过私有列扫描 —— 一列都不许在。"""
    frozen, _ = _freeze()
    mi.assert_no_private_leak(frozen, field="frozenSnapshot")

    # 判别力:同一把扫描器对一个被污染的冻结面必须报红。
    polluted = copy.deepcopy(frozen)
    polluted["decision"]["provider_media_id"] = 1490001
    with pytest.raises(mi.PrivateColumnLeak):
        mi.assert_no_private_leak(polluted, field="frozenSnapshot")


# ══════════════════════════════════════════════════════════════════════════
# F. legal_gate —— MED-11 / MED-18
# ══════════════════════════════════════════════════════════════════════════
def _signed_terms() -> list[str]:
    from services.marketing.legal_context import absolute_terms

    return sorted(absolute_terms())


def test_med11_gate_mode_is_blocking():
    """MED-11:当前法律包已由 Owner 签发 ⇒ 门是 blocking,不是 advisory。"""
    assert lg.gate_mode() == "blocking"
    assert lg.census()["mode"] == "blocking"
    assert lg.RULE_ID in lg.census()["signedRuleIds"], (
        "规则没在已签发清单里,门却报 blocking —— 两处口径已经打架"
    )


def test_med18_evaluate_hits_absolute_term_and_misses_clean_body():
    """MED-18:含绝对化用语的正文必命中、干净正文必不命中。"""
    terms = _signed_terms()
    assert terms, "签发包词表为空 —— 这条判据会零判别力"
    term = terms[0]

    verdict = lg.evaluate(
        article_revision_id="rev-1", frozen_body=f"本次服务在试点中被评为{term}方案。",
    )
    assert verdict.mode == "blocking"
    assert verdict.blocked is True
    assert verdict.hits and verdict.primary is not None
    assert verdict.primary.article_revision_id == "rev-1"
    assert verdict.primary.rule_id == lg.RULE_ID
    assert verdict.primary.repair_action_kind == lg.REPAIR_ACTION_KIND
    assert verdict.primary.passage_ref.startswith("rev:rev-1#chars=")

    clean = "本次服务在三家门店试点后回访满意度较高，具体数据见附表。"
    # 先证明这条「干净」正文真的一个签发词都不含(否则「不命中」是假的)。
    assert not [t for t in terms if t in clean]
    clean_verdict = lg.evaluate(article_revision_id="rev-1", frozen_body=clean)
    assert clean_verdict.blocked is False
    assert clean_verdict.hits == ()
    assert clean_verdict.primary is None


def test_med18_repair_action_matches_hit_value_by_value():
    """MED-18:repair_action 的 target.id/passageRef 与 hit 逐值相等。"""
    term = _signed_terms()[0]
    verdict = lg.evaluate(
        article_revision_id="rev-77", frozen_body=f"我们提供{term}的服务体验。",
    )
    hit = verdict.primary
    assert hit is not None

    action = lg.repair_action(hit, label="修好这一句")
    assert action["kind"] == "repair_legal_passage"
    assert action["capability"] == "repair_content"
    assert action["target"]["id"] == hit.article_revision_id
    assert action["target"]["passageRef"] == hit.passage_ref
    assert hit.passage_ref in action["actionRef"]
    lg.assert_repair_matches(hit, action)              # 必不抛

    # 必须抛的那一半:id 错配 / passageRef 错配(§19 变异 162)。
    wrong_id = copy.deepcopy(action)
    wrong_id["target"]["id"] = "rev-78"
    with pytest.raises(AssertionError):
        lg.assert_repair_matches(hit, wrong_id)

    wrong_ref = copy.deepcopy(action)
    wrong_ref["target"]["passageRef"] = "rev:rev-77#chars=0-1"
    with pytest.raises(AssertionError):
        lg.assert_repair_matches(hit, wrong_ref)

    with pytest.raises(AssertionError):
        lg.assert_repair_matches(hit, {"kind": "repair_legal_passage"})   # target 缺失


def test_med11_cannot_widen_the_gate_beyond_the_signed_pack():
    """MED-11:扩门反向锁 —— 包外的词必抛,包内的词必不抛。"""
    with pytest.raises(AssertionError):
        lg.assert_terms_come_only_from_signed_pack(["某个包外的词"])

    terms = _signed_terms()
    # 混一个包外词进合法列表也必须抛(不是「只看第一个」)。
    with pytest.raises(AssertionError):
        lg.assert_terms_come_only_from_signed_pack([terms[0], "某个包外的词"])

    # 必须不命中的那一半:整份签发词表全部放行。
    lg.assert_terms_come_only_from_signed_pack(terms)
