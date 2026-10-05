"""WP2 · H0 基础判据(03 §5)+ RFC 裁定附加约束 A / D。

四类 H0 各配「构造违例 → 被拒」的**成对**判据(工单 §3.3):
  幂等 / 资金守恒 / 授权 / 对象身份。

🔴 本文件不连库的部分打的是**纯行为**(函数真跑,断言真返回值),
   连库的并发/claim 判据在 test_wp2_idempotency_pg16.py。
"""
from __future__ import annotations

import re

import pytest

from services.geo_douyin.contract_funding import (
    AUTHORITY_ADMIN_EXEMPT,
    AUTHORITY_DIRECT,
    AUTHORITY_LEGACY,
    AUTHORITY_ORGANIZATION,
    ALLOWED_FREEZE_TABLES,
    FREEZE_PER_ITEM_PREDICATE,
    LEGACY_SETTLEMENT_PREDICATE,
    FundingHandleInvalid,
    PlatformAccountUnavailable,
    assert_authority_shape,
    assert_reserved_matches_price,
    build_direct_handle,
    resolve_platform_payer_user_id,
    resolve_settlement_authority,
)
from services.geo_douyin.contract_idempotency import (
    IdempotencyConflict,
    LEGACY_CLEANUP_PREDICATE,
    production_request_payload,
    publish_request_payload,
    request_hash,
)
from services.geo_douyin.contract_pricing import (
    OPERATION_PRODUCTION,
    OPERATION_PUBLISH,
    PriceChanged,
    PriceFingerprintMismatch,
    assert_fingerprint_operation,
    assert_price_unchanged,
    canonical_price_snapshot,
    operation_of,
    production_fingerprint,
    publish_fingerprint,
)


def _prod_fp(**over):
    kwargs = dict(feature_code="geo_douyin_image_post", unit_points=390, card_count=4,
                  extra_card_points=0, final_price_points=390,
                  resolver_version="r1", catalog_version="c1",
                  style_catalog_version="styles-v1", policy_version="p1")
    kwargs.update(over)
    return production_fingerprint(**kwargs)


def _pub_fp(**over):
    kwargs = dict(feature_code="media_proxy_publish", media_id=9001,
                  final_price_points=9360, markup_version="m1",
                  resolver_version="r1", catalog_version="c1", policy_version="p1")
    kwargs.update(over)
    return publish_fingerprint(**kwargs)


# ============================================================
# H0-① 对象身份 · 两条定价链的指纹不可互换(03 §12「operation pricing」行)
# ============================================================

def test_two_chains_produce_distinguishable_fingerprints():
    assert operation_of(_prod_fp()) == OPERATION_PRODUCTION
    assert operation_of(_pub_fp()) == OPERATION_PUBLISH
    assert _prod_fp() != _pub_fp()


def test_publish_fingerprint_rejected_by_production_chain():
    """构造违例 → 被拒:把投放指纹送进制作链。"""
    with pytest.raises(PriceFingerprintMismatch, match="publish"):
        assert_fingerprint_operation(_pub_fp(), OPERATION_PRODUCTION)


def test_production_fingerprint_rejected_by_publish_chain():
    """反方向同样必须拒(规格 §6.2:「反之亦然」)。"""
    with pytest.raises(PriceFingerprintMismatch, match="production"):
        assert_fingerprint_operation(_prod_fp(), OPERATION_PUBLISH)


def test_unknown_fingerprint_is_rejected_not_guessed():
    with pytest.raises(PriceFingerprintMismatch, match="无法识别"):
        assert_fingerprint_operation("sha256:deadbeef", OPERATION_PRODUCTION)


def test_matching_chain_passes():
    """反向对照:同链同值必须放行,否则上面三条"能拒"可能只是"什么都拒"。"""
    assert_fingerprint_operation(_prod_fp(), OPERATION_PRODUCTION)
    assert_price_unchanged(expected_fingerprint=_prod_fp(),
                           actual_fingerprint=_prod_fp(),
                           operation=OPERATION_PRODUCTION)


def test_cross_chain_is_reported_as_identity_not_as_price_drift():
    """跨链传入必须报**对象身份**,不能被伪装成一次普通的价格漂移。

    顺序反了(先比相等再验链)就会退化成 PriceChanged —— 看起来还挺像回事,
    但掩盖了"调用方在跨链传身份"这个真问题。
    """
    with pytest.raises(PriceFingerprintMismatch):
        assert_price_unchanged(expected_fingerprint=_pub_fp(),
                               actual_fingerprint=_prod_fp(),
                               operation=OPERATION_PRODUCTION)


# ============================================================
# H0-② 价格漂移 · 逐项指纹必须敏感(总价相同不能掩盖逐项涨跌)
# ============================================================

@pytest.mark.parametrize("field,value", [
    ("feature_code", "geo_douyin_image_post_regen"),   # 换 SKU 必须变指纹(规格 §7.3)
    ("unit_points", 391),
    ("card_count", 5),
    ("final_price_points", 400),
    ("resolver_version", "r2"),
    ("catalog_version", "c2"),
    ("style_catalog_version", "styles-v2"),
    ("policy_version", "p2"),
])
def test_every_production_input_moves_the_fingerprint(field, value):
    """每一个计价输入都必须进摘要 —— 漏一个,那一维的漂移就静默通过确认锁。"""
    assert _prod_fp(**{field: value}) != _prod_fp(), f"{field} 没进指纹"


@pytest.mark.parametrize("field,value", [
    ("feature_code", "media_publish"),
    ("media_id", 9002),          # 只差账号也必须换指纹
    ("final_price_points", 9361),
    ("markup_version", "m2"),
    ("resolver_version", "r2"),
    ("catalog_version", "c2"),
    ("policy_version", "p2"),
])
def test_every_publish_input_moves_the_fingerprint(field, value):
    assert _pub_fp(**{field: value}) != _pub_fp(), f"{field} 没进指纹"


def test_same_total_price_different_item_still_rejected():
    """规格 §7.3:「总价相同不能掩盖逐项涨跌」。

    两项总价都是 9360,但账号不同 ⇒ 指纹必须不同 ⇒ 拿旧确认锁提交必须 409。
    """
    a = _pub_fp(media_id=9001, final_price_points=9360)
    b = _pub_fp(media_id=9002, final_price_points=9360)
    assert a != b
    with pytest.raises(PriceChanged):
        assert_price_unchanged(expected_fingerprint=a, actual_fingerprint=b,
                               operation=OPERATION_PUBLISH)


def test_price_snapshot_is_serializable_and_complete():
    snap = canonical_price_snapshot(
        operation=OPERATION_PUBLISH, final_price_points=9360,
        feature_code="media_proxy_publish", resolver_version="r1",
        catalog_version="c1", markup_version="m1", policy_version="p1",
        preview_expires_at="2026-08-17T23:59:59+08:00", confirmed_at=None)
    for key in ("final_price_points", "feature_code", "resolver_version",
                "catalog_version", "markup_version", "policy_version",
                "preview_expires_at", "confirmed_at"):
        assert key in snap, f"canonical price snapshot 缺 {key}(规格 §6.2 逐项列出)"


# ============================================================
# H0-③ 资金守恒 · P0-6 admin_exempt 必须是真实记账
# ============================================================

def test_admin_routes_to_platform_account_not_zero_accounting(monkeypatch):
    """裁定 P0-6:admin/平台侧 → 平台直营账号**真实** freeze,不是零记账。"""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "136")
    result = resolve_settlement_authority(
        is_admin=True, organization_id=None,
        organization_billing_ready=False, owner_user_id=42)
    assert result["authority"] == AUTHORITY_ADMIN_EXEMPT
    assert result["payer_user_id"] == 136, "admin 没路由到平台直营账号"
    assert result["payer_user_id"] != 42, "admin 操作扣到了服务商头上"
    assert result["exemption_rule"], "免扣规则没留审计依据"


def test_platform_account_unavailable_is_fail_closed(monkeypatch):
    """平台账没配好 → 抛(调用方 503)。

    绝不能回落成"扣服务商" —— 那正是 2026-08-16 生产事故的形态
    (管理员开监测,不知情花掉服务商 4,290 算力)。
    """
    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)
    with pytest.raises(PlatformAccountUnavailable):
        resolve_platform_payer_user_id()
    with pytest.raises(PlatformAccountUnavailable):
        resolve_settlement_authority(is_admin=True, organization_id=None,
                                     organization_billing_ready=False, owner_user_id=42)


def test_admin_exempt_without_handle_is_rejected():
    """构造违例 → 被拒:旧口径的「零 handle admin_exempt」。"""
    with pytest.raises(FundingHandleInvalid, match="真实记账"):
        assert_authority_shape({
            "settlement_authority": AUTHORITY_ADMIN_EXEMPT,
            "organization_charge_link_id": None,
            "freeze_id": None, "freeze_table": None,
            "payer_user_id": None, "reserved_amount": None,
        })


def test_admin_exempt_with_full_handle_passes():
    """反向对照:带完整 136 句柄的 admin_exempt 必须放行。"""
    assert_authority_shape({
        "settlement_authority": AUTHORITY_ADMIN_EXEMPT,
        "organization_charge_link_id": None,
        "freeze_id": 777, "freeze_table": "legacy",
        "payer_user_id": 136, "reserved_amount": 390,
    })


def test_admin_exempt_reserved_must_equal_price():
    """P0-6 反向改写:exempt 的 reserved 必须 == N,**不再允许 0**。

    谁在 assert_reserved_matches_price 里加回 `if authority == admin_exempt: return`,
    这条当场红。
    """
    assert_reserved_matches_price(authority=AUTHORITY_ADMIN_EXEMPT,
                                  reserved_amount=390, final_price_points=390)
    with pytest.raises(FundingHandleInvalid, match="不一致"):
        assert_reserved_matches_price(authority=AUTHORITY_ADMIN_EXEMPT,
                                      reserved_amount=0, final_price_points=390)


def test_two_authority_families_are_mutually_exclusive():
    """组织 charge link 与 direct handle 不许同时持有。"""
    with pytest.raises(FundingHandleInvalid, match="互斥"):
        assert_authority_shape({
            "settlement_authority": AUTHORITY_ORGANIZATION,
            "organization_charge_link_id": 5,
            "freeze_id": 777, "freeze_table": "legacy",
            "payer_user_id": 42, "reserved_amount": 390,
        })
    with pytest.raises(FundingHandleInvalid, match="互斥"):
        assert_authority_shape({
            "settlement_authority": AUTHORITY_DIRECT,
            "organization_charge_link_id": 5,
            "freeze_id": 777, "freeze_table": "legacy",
            "payer_user_id": 42, "reserved_amount": 390,
        })


def test_freeze_table_domain_matches_the_real_producer():
    """🔴 值域必须与 `middleware/billing.freeze_points` 真正返回的值一致。

    实证锚:`middleware/billing.py` 里既有 `"freeze_table": "legacy"` 的返回,
    也有 `_route_freeze_table` 只接受 `("legacy", "v35")` 的判断。
    这条防的是我犯过的那个错:从 `pg_tables` 取物理表名当值域,
    结果每一个真句柄都会被拒(「同名字段在不同层不同语义」)。
    """
    import io as _io
    import pathlib as _pl

    billing = _io.open(
        _pl.Path(__file__).resolve().parents[2] / "middleware" / "billing.py",
        encoding="utf-8").read()
    for value in ALLOWED_FREEZE_TABLES:
        assert f'"{value}"' in billing or f"'{value}'" in billing, (
            f"值域里的 {value!r} 在 middleware/billing.py 里找不到 —— 词表取错了层"
        )
    # 反向对照:物理表名**不该**出现在句柄值域里
    from services.geo_douyin.contract_funding import PHYSICAL_FREEZE_TABLES
    assert not (ALLOWED_FREEZE_TABLES & PHYSICAL_FREEZE_TABLES), (
        "句柄值域混进了物理表名 —— 那一层的词表不通用"
    )


def test_freeze_table_value_domain_is_closed():
    """不同 freeze 表的相同数字 id 会碰撞 ⇒ 表名值域必须封闭,不许猜。"""
    # 🔴 值域取自**真正的生产者**(middleware/billing 句柄),不是 pg_tables 里的物理表名。
    # 第一版按物理名写,会拒掉每一个真句柄 ——「同名字段在不同层不同语义」。
    assert ALLOWED_FREEZE_TABLES == {"legacy", "v35"}
    with pytest.raises(FundingHandleInvalid, match="不许猜表"):
        build_direct_handle(payer_user_id=136, freeze_id=1,
                            freeze_table="some_other_freeze_table",
                            task_ref="t", reserved_amount=390,
                            physical_split_snapshot={})


@pytest.mark.parametrize("missing", ["payer_user_id", "freeze_id", "task_ref", "reserved_amount"])
def test_partial_direct_handle_is_rejected(missing):
    """半个句柄比没有句柄更危险 —— 它看起来像"有记账"。"""
    kwargs = dict(payer_user_id=136, freeze_id=1, freeze_table="legacy",
                  task_ref="t", reserved_amount=390, physical_split_snapshot={})
    kwargs[missing] = None
    with pytest.raises(FundingHandleInvalid):
        build_direct_handle(**kwargs)


def test_member_without_org_billing_hands_off_never_charges_actor():
    """规格 §9:flags/policy 未开 → member 只能 handoff owner,**绝不 fallback 扣 actor**。"""
    result = resolve_settlement_authority(
        is_admin=False, organization_id=88,
        organization_billing_ready=False, owner_user_id=42)
    assert result["authority"] is None, "组织计费没就绪却给出了结算权威"
    assert result["payer_user_id"] is None, "回落出了一个付款方 —— 极可能是员工自己"
    assert result["handoff_reason"] == "ORGANIZATION_BILLING_NOT_READY"
    # 反向对照:就绪时必须给出 owner 作 payer
    ok = resolve_settlement_authority(is_admin=False, organization_id=88,
                                      organization_billing_ready=True, owner_user_id=42)
    assert ok["authority"] == AUTHORITY_ORGANIZATION and ok["payer_user_id"] == 42


def test_legacy_and_new_lane_predicates_do_not_overlap_on_null():
    """🔴 034 零 DML ⇒ 存量行 billing_mode 为 NULL。两条谓词必须:
    老行只落 legacy、新行只落新链,**NULL 不许穿透新链**(规格 §13)。

    这条打的是谓词文本形态 —— 真正的行为对照在 PG16 用例里跑。
    """
    assert "IS NULL" in LEGACY_SETTLEMENT_PREDICATE, (
        "legacy 谓词没处理 NULL —— 写成 `<> 'freeze_per_item'` 对 NULL 恒 UNKNOWN,老行会被漏掉"
    )
    assert "= 'freeze_per_item'" in FREEZE_PER_ITEM_PREDICATE
    assert "IS NULL" not in FREEZE_PER_ITEM_PREDICATE, "新链谓词把 NULL 老行也收进来了"


def test_legacy_authority_is_exempt_from_per_item_reserve_rule():
    """老链走 deduct_upfront,不适用逐项冻结口径 —— 但这**不是** admin_exempt 的借口。"""
    assert_reserved_matches_price(authority=AUTHORITY_LEGACY,
                                  reserved_amount=0, final_price_points=390)


# ============================================================
# H0-④ 幂等 · request hash 的判别力
# ============================================================

def test_hash_covers_owner_and_endpoint():
    payload = {"a": 1}
    base = request_hash(owner_user_id=1, endpoint="/x", payload=payload)
    assert request_hash(owner_user_id=2, endpoint="/x", payload=payload) != base, "owner 没进 hash"
    assert request_hash(owner_user_id=1, endpoint="/y", payload=payload) != base, "endpoint 没进 hash"
    assert request_hash(owner_user_id=1, endpoint="/x", payload={"a": 2}) != base, "payload 没进 hash"
    # 反向对照:同输入必须同 hash,否则幂等永远命中不了
    assert request_hash(owner_user_id=1, endpoint="/x", payload=payload) == base


def test_item_order_does_not_change_the_hash():
    """同一批 item 换个顺序提交必须得到**同一个** hash —— 否则前端重排一下就绕过幂等。"""
    items = [
        {"item_request_id": "b", "delivery_slot_key": "s2", "topic_ref": "t2",
         "expected_price_fingerprint": "f2", "settings": {"card_count": 4}},
        {"item_request_id": "a", "delivery_slot_key": "s1", "topic_ref": "t1",
         "expected_price_fingerprint": "f1", "settings": {"card_count": 4}},
    ]
    forward = production_request_payload(
        draft_id="d", draft_etag="1", quote_id=410, contract_revision_id="rev8",
        items=items, expected_total_price_points=780)
    backward = production_request_payload(
        draft_id="d", draft_etag="1", quote_id=410, contract_revision_id="rev8",
        items=list(reversed(items)), expected_total_price_points=780)
    assert forward == backward


def test_changing_one_item_changes_the_payload():
    """反向对照:真改了内容必须换 payload,否则上面那条"顺序无关"是靠"什么都无关"实现的。"""
    items = [{"item_request_id": "a", "delivery_slot_key": "s1", "topic_ref": "t1",
              "expected_price_fingerprint": "f1", "settings": {"card_count": 4}}]
    base = production_request_payload(draft_id="d", draft_etag="1", quote_id=410,
                                      contract_revision_id="rev8", items=items,
                                      expected_total_price_points=390)
    changed = production_request_payload(
        draft_id="d", draft_etag="1", quote_id=410, contract_revision_id="rev8",
        items=[{**items[0], "settings": {"card_count": 6}}],
        expected_total_price_points=390)
    assert base != changed


def test_publish_payload_covers_revision_artifact_manifest_account():
    """规格 §8.1:publish hash 必须覆盖 post revision/artifact/manifest/account。"""
    base_item = {"item_request_id": "i1", "geo_post_id": 101, "post_revision_id": "r7",
                 "prepared_artifact_id": "a9", "manifest_hash": "h1", "media_id": 9001,
                 "expected_price_fingerprint": "f1"}
    base = publish_request_payload(items=[base_item], expected_total_price_points=9360)
    for field, value in (("post_revision_id", "r8"), ("prepared_artifact_id", "a10"),
                         ("manifest_hash", "h2"), ("media_id", 9002)):
        other = publish_request_payload(items=[{**base_item, field: value}],
                                        expected_total_price_points=9360)
        assert other != base, f"{field} 没进 publish payload"


def test_legacy_cleanup_predicate_never_touches_new_lane():
    """24h 清理只许碰老行(规格 §8.1:新链寿命与审计寿命一致)。"""
    assert "record_kind IS NULL" in LEGACY_CLEANUP_PREDICATE
    assert "'legacy'" in LEGACY_CLEANUP_PREDICATE
    assert "root" not in LEGACY_CLEANUP_PREDICATE and "retry" not in LEGACY_CLEANUP_PREDICATE


def test_idempotency_conflict_carries_reason():
    exc = IdempotencyConflict("同一请求 id 提交了不同内容", {"request_id": "x"})
    assert exc.reason and exc.existing["request_id"] == "x"


# ============================================================
# 裁定附加约束 A · flag 上车必须 False
# ============================================================

def test_contract_flag_ships_disabled(monkeypatch):
    """🔴 Review 附加约束 A:上车时 `GEO_IMAGE_NOTE_CONTRACT_ENABLED` 必须 False,
    开闸是独立生产配置动作(Owner 拍板 + Review 签发)。

    判据打**默认值**:env 不存在时必须 False —— 而不是"我记得没开"。
    """
    from services.geo_douyin.delivery_plan import contract_lane_enabled

    monkeypatch.delenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", raising=False)
    assert contract_lane_enabled() is False, "flag 默认开着 = 随包上车,违反附加约束 A"
    # 反向对照:显式打开时必须为 True,否则这个 flag 是个恒 False 的死开关
    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")
    assert contract_lane_enabled() is True


#: 真正能把 flag 带上车的**配置载体**。
#: 🔴 刻意**不含 .md**:RFC / 交付单**必须**写出 flag 名和「=true 时…」的激活条件,
#:    那是文档在描述闸门,不是配置在打开闸门。把 .md 收进来会让判据对着自己的
#:    需求文档恒红 —— 而长期红的判据等于没有判据。
#:    收窄之后判别力靠 test_flag_scan_fires_on_a_real_config_vector 兜底:
#:    它在真 .env / docker-compose / start.sh 形态上取证,证明这条扫描没被削废。
_CONFIG_SUFFIXES = {".env", ".yml", ".yaml", ".sh", ".ps1", ".py", ".json", ".conf", ".ini", ".toml"}
_CONFIG_NAMES = {".env", ".env.example", "Dockerfile", "docker-compose.yml", "start.sh"}


_FLAG_PATTERN = re.compile(
    r"GEO_IMAGE_NOTE_CONTRACT_ENABLED\s*[=:]\s*['\"]?(1|true|yes|on)\b", re.IGNORECASE)

_PY_DOCSTRING = re.compile(r'"""[\s\S]*?"""' + "|" + r"'''[\s\S]*?'''")
_HASH_COMMENT_SUFFIXES = {".sh", ".ps1", ".conf", ".ini", ".toml", ".yml", ".yaml", ".env"}


def _strip_non_code(text: str, suffix: str) -> str:
    """剥掉注释与文档字符串再扫。

    🔴 这是「判据打在注释上」的**第四次**。前三次:SQL 形态锁 / flag 扫描撞 RFC 文档 /
       前端 `/130` 撞 `//` 注释。第四次是我自己新写的 `delivery_slots.py` docstring ——
       它**必须**写出「`GEO_IMAGE_NOTE_CONTRACT_ENABLED = true` 是第一个激活前置」,
       那是模块契约的一部分,不是配置在打开闸门。
       前三次我一处一处补;这次把剥离做成**共享**的:扫描类判据默认先剥非代码,
       否则每加一个扫描面就要重踩一次。
    """
    if suffix == ".py":
        text = _PY_DOCSTRING.sub("", text)
        return "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    if suffix in _HASH_COMMENT_SUFFIXES:
        return "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return text          # .json 没有注释;其余保持原样


def _scan_flag_enabled(root):
    import pathlib

    offenders = []
    for path in pathlib.Path(root).rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__", "dist", "tests"} for part in path.parts):
            continue
        if path.suffix.lower() not in _CONFIG_SUFFIXES and path.name not in _CONFIG_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _FLAG_PATTERN.search(_strip_non_code(text, path.suffix.lower())):
            offenders.append(str(path.relative_to(root)))
    return offenders


def test_flag_scan_strips_docstrings_but_not_code():
    """剥离这一步**自己**的判别力对照(第四次踩坑后补的)。

    没有这三条,`_strip_non_code` 可能把整个文件都剥空 ——
    那样"仓内没有"就变成了最廉价的恒真。
    """
    doc = '"""激活前置:GEO_IMAGE_NOTE_CONTRACT_ENABLED = true"""\nX = 1\n'
    code = "import os\nGEO_IMAGE_NOTE_CONTRACT_ENABLED = True\n"
    assert not _FLAG_PATTERN.search(_strip_non_code(doc, ".py")), "docstring 没被剥掉"
    assert _FLAG_PATTERN.search(_strip_non_code(code, ".py")), "剥过头了 —— 真代码也被吃掉"
    assert not _FLAG_PATTERN.search(
        _strip_non_code("# GEO_IMAGE_NOTE_CONTRACT_ENABLED=true\n", ".sh"))
    assert _FLAG_PATTERN.search(
        _strip_non_code("GEO_IMAGE_NOTE_CONTRACT_ENABLED=true\n", ".env"))


def test_flag_is_not_enabled_anywhere_in_repo_config():
    """接线锁:仓内**没有任何配置载体**把它设成开。

    只锁函数默认值不够 —— .env.example / docker-compose / start.sh 里写一句
    `GEO_IMAGE_NOTE_CONTRACT_ENABLED=true` 照样随包上车(附加约束 A)。
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[2]
    offenders = _scan_flag_enabled(repo)
    assert not offenders, f"仓内有配置把 contract flag 打开了(违反附加约束 A):{offenders}"


def test_flag_scan_fires_on_a_real_config_vector(tmp_path):
    """🔴 判别力:在**真配置载体**上必须抓到。

    上面那条收窄了扫描范围(排除 .md),所以必须证明收窄后它对真正的上车路径
    仍然有效 —— 否则"仓内没有"只是因为扫描面被削没了。
    这里逐个造出 .env / docker-compose.yml / start.sh 三种真实形态。
    """
    (tmp_path / ".env").write_text("FOO=1\nGEO_IMAGE_NOTE_CONTRACT_ENABLED=true\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  app:\n    environment:\n      GEO_IMAGE_NOTE_CONTRACT_ENABLED: 'on'\n",
        encoding="utf-8")
    (tmp_path / "start.sh").write_text(
        "#!/bin/sh\nexport GEO_IMAGE_NOTE_CONTRACT_ENABLED=1\n", encoding="utf-8")
    offenders = _scan_flag_enabled(tmp_path)
    assert len(offenders) == 3, f"真配置载体上漏检:只抓到 {offenders}"


def test_flag_scan_does_not_overfire_on_disabled_value(tmp_path):
    """反向对照:显式关闭不该被当成违规,否则没人能把这个 flag 写进配置里关掉。"""
    (tmp_path / ".env").write_text("GEO_IMAGE_NOTE_CONTRACT_ENABLED=false\n", encoding="utf-8")
    assert _scan_flag_enabled(tmp_path) == []


# ============================================================
# 裁定附加约束 D · activation_pending 降级形态的成对判据
# ============================================================

def test_activation_pending_is_human_readable_and_actionable(monkeypatch):
    """🔴 附加约束 D:降级形态是上车后的**默认现实**,它自己要有成对判据。

    ① 人话提示:不露 slot / revision / sidecar / ordinal 等工程术语(01 §1 画像口径)。
    """
    from services.geo_douyin.delivery_plan import build_delivery_plan

    monkeypatch.delenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", raising=False)
    plan = build_delivery_plan(quote_id=410, brand={"id": 36, "name": "某客户"})

    assert plan["state"] == "activation_pending"
    assert plan["user_message"] == "制作计划准备中"
    assert plan["actions"], "降级态没给可执行下一步"
    # actions[].type 必须用现役 builder 认的枚举,不许写会被丢弃的 kind(规格 §12)
    for action in plan["actions"]:
        assert action["type"] in {"retry", "nav", "contact", "dismiss", "api"}, action
        assert "kind" not in action, "写了会被 builder 丢弃的 `kind`"

    blob = " ".join(str(plan[k]) for k in ("user_message", "repair_hint")) + str(plan["actions"])
    for jargon in ("slot", "revision", "sidecar", "ordinal", "outbox", "CAS", "projection"):
        assert jargon.lower() not in blob.lower(), f"用户可见文案里露了工程术语:{jargon}"


def test_activation_pending_distinguishes_unknown_from_real_zero(monkeypatch):
    """② `channel_allocated_capacity` 必须是 **None** 而不是 0。

    0 是「已签发且确实零容量」的合法读数(规格 §4.2 要求二者可区分);
    拿 0 表示「还不知道」正好把这对混成一件事。
    """
    from services.geo_douyin.delivery_plan import build_delivery_plan

    monkeypatch.delenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", raising=False)
    plan = build_delivery_plan(quote_id=410, brand=None)
    assert plan["summary"]["channel_allocated_capacity"] is None
    assert plan["summary"]["quote_total_capacity"] is None
    assert plan["slots"] == []
    assert plan["contract_revision_id"] is None


def test_activation_pending_has_zero_side_effects(monkeypatch):
    """③ 三零断言:零 claim / 零资金 / 零 provider。

    形态判据:降级路径**根本不导入**任何资金/провайдер模块。
    这比"断言没被调用"更强 —— 没被调用可能只是这次没走到那个分支。
    """
    import sys

    from services.geo_douyin.delivery_plan import build_delivery_plan

    monkeypatch.delenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", raising=False)

    # 装一个会炸的哨兵:降级路径一旦碰资金/发布/slot 就当场失败
    class _Tripwire:
        def __getattr__(self, name):
            raise AssertionError(f"降级态触碰了副作用模块属性 {name!r} —— 三零断言破裂")

    for module_name in ("middleware.billing", "services.geo_douyin.publish_adapter"):
        monkeypatch.setitem(sys.modules, module_name, _Tripwire())

    plan = build_delivery_plan(quote_id=410, brand={"id": 36, "name": "某客户"})
    assert plan["state"] == "activation_pending"


def test_flag_on_without_ready_schema_fails_loudly_not_silently(monkeypatch):
    """flag 开了但 schema 没跑到位 → **响亮**失败,不返回空计划。

    [搬家 2026-08-18] 原断言的前提是「RFC 未批 / 读侧未接线」。RFC 已于 08-17 批复、
    active 分支已接线,所以那个前提消失了 —— 但**不变式没变**:
    「flag 开着却给不出真实计划时,必须响亮,而不是返回一个空 slots 列表」。
    空计划在界面上等于"这客户没有容量",而真相是"我们还没准备好";
    0 与"不知道"长得一样但含义相反。按「不变式搬家改断言不退役」处理。
    """
    from services.geo_douyin import delivery_plan as dp

    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")
    monkeypatch.setattr(dp, "_sidecar_schema_ready", lambda cur=None: False)
    with pytest.raises(dp.SidecarNotApproved):
        dp.build_delivery_plan(quote_id=410, brand={"id": 36, "name": "某客户"})


def test_flag_on_with_ready_schema_does_not_raise(monkeypatch):
    """反向对照:schema 就绪时**不许**再抛 —— 否则上一条只是"永远抛"。

    没有这一条,把 `_sidecar_schema_ready` 写成恒 False 也能让上面那条绿,
    而那等于图文 lane 永远打不开。
    """
    from services.geo_douyin import delivery_plan as dp

    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")
    monkeypatch.setattr(dp, "_sidecar_schema_ready", lambda cur=None: True)

    class _Cur:
        def __init__(self):
            self._rows = []

        def execute(self, sql, params=None):
            self._rows = [] if "contract_ordinal" in sql else [{"n": 0}]

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return {"n": 0}

    plan = dp.build_delivery_plan(quote_id=410, brand={"id": 36, "name": "某客户"},
                                  cur=_Cur())
    assert plan["state"] == "active"
    # 已签发但确实零容量 → 0(可用),不是 None(不知道)
    assert plan["summary"]["channel_allocated_capacity"] == 0


# ============================================================
# 能力映射(P0-7 的另一半)
# ============================================================

@pytest.mark.parametrize("feature_code", [
    "geo_douyin_image_post", "geo_douyin_image_post_regen",
    "geo_douyin_image_post_extra_card", "geo_douyin_image_post_redraw",
    "geo_douyin_topic_distill",
])
def test_geo_image_note_features_are_capability_mapped(feature_code):
    """没有能力映射 → 组织成员走到这些操作只能 handoff owner = 员工席位用不了图文制作。"""
    from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES as FEATURE_CAPABILITIES

    assert feature_code in FEATURE_CAPABILITIES, f"{feature_code} 没有能力映射(裁定 P0-7)"
    assert FEATURE_CAPABILITIES[feature_code] == "writing.generate"


def test_production_and_publish_are_separately_authorized():
    """规格 §9:写作/制作与发布**分别**授权。合并成一条 = 能做就能发。"""
    from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES as FEATURE_CAPABILITIES

    assert FEATURE_CAPABILITIES["geo_douyin_image_post"] == "writing.generate"
    assert FEATURE_CAPABILITIES["media_proxy_publish"] == "publish.execute"
    assert FEATURE_CAPABILITIES["geo_douyin_image_post"] != FEATURE_CAPABILITIES["media_proxy_publish"]


def test_default_sales_gets_neither_ability():
    """已签发裁决:默认 sales 不获得写作/发布能力。本包不得把它改松。"""
    from services.organization_contract import (
        DELIVERY_ROLE_CAPABILITIES, SALES_ROLE_CAPABILITIES,
    )

    assert "writing.generate" not in SALES_ROLE_CAPABILITIES
    assert "publish.execute" not in SALES_ROLE_CAPABILITIES
    # 反向对照:交付角色必须**有**,否则"销售没有"只是因为谁都没有
    assert "writing.generate" in DELIVERY_ROLE_CAPABILITIES
    assert "publish.execute" in DELIVERY_ROLE_CAPABILITIES
