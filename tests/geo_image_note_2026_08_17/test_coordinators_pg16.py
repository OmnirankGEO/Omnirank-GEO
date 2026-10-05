"""协调器落库轮 · 真事务判据。

清掉端点轮的 `*_not_wired` 欠条中的四个:
  production_draft_persistence / publish_command_coordinator /
  artifact_preparation / (publish_preview_pricing 由 preview 复用同一定价源)
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from services.geo_douyin.artifact_prepare import (  # noqa: E402
    STATE_FAILED, STATE_READY, STATE_UNKNOWN,
    ArtifactNotRetryable, ArtifactRequestConflict,
    assert_artifact_matches, assert_retryable, claim_artifact,
    mark_failed, mark_ready, mark_unknown,
)
from services.geo_douyin.contract_funding import (  # noqa: E402
    AUTHORITY_DIRECT, FundingHandleInvalid,
)
from services.geo_douyin.contract_pricing import (  # noqa: E402
    PriceChanged, PriceFingerprintMismatch, production_fingerprint, publish_fingerprint,
)
from services.geo_douyin.production_draft import (  # noqa: E402
    DraftConflict, accept_draft, etag_for, parse_etag, save_draft,
)
from services.geo_douyin.publish_command import CapacityExceeded  # noqa: E402
from services.geo_douyin.publish_coordinator import materialize_command  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql"
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

IDENTITY = {"tenant_owner_user_id": 42, "payer_user_id": 42, "actor_user_id": 99,
            "payer_policy_snapshot": {"policy": "owner_pays"}}


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    name = f"geoimg_test_coord_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    c.execute("SET search_path = public")
    c.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def txn(db):
    """显式事务(协调器必须在事务内 —— 容量锁是事务级的)。"""
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    c = conn.cursor()
    for table in ("mhz_publish_order_items", "mhz_publish_orders",
                  "geo_douyin_publish_artifacts", "geo_douyin_production_batches",
                  "geo_douyin_posts"):
        c.execute(f"DELETE FROM {table}")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ============================================================
# A. 制作草稿 ETag CAS
# ============================================================

def test_draft_create_then_cas_update(txn):
    c = txn.cursor()
    batch_id = str(uuid.uuid4())
    first = save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
                       contract_revision_id=8, payload={"slots": ["a"]},
                       expected_total_price_points=390, expected_etag=None)
    assert first["etag_revision"] == 1 and first["etag"] == '"draft-1"'

    second = save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
                        contract_revision_id=8, payload={"slots": ["a", "b"]},
                        expected_total_price_points=780, expected_etag=1)
    assert second["etag_revision"] == 2
    txn.rollback()


def test_stale_etag_is_a_conflict_not_a_silent_overwrite(txn):
    """01 §3.3:两标签页同改同一草稿,旧 ETag 提交返回可理解冲突,不静默覆盖。"""
    c = txn.cursor()
    batch_id = str(uuid.uuid4())
    save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
               contract_revision_id=8, payload={}, expected_total_price_points=0,
               expected_etag=None)
    save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
               contract_revision_id=8, payload={"v": 2}, expected_total_price_points=0,
               expected_etag=1)
    with pytest.raises(DraftConflict) as excinfo:
        save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
                   contract_revision_id=8, payload={"v": "stale"},
                   expected_total_price_points=0, expected_etag=1)
    assert excinfo.value.current_etag == 2, "冲突没带当前版本 → 前端无法给『加载最新版』"
    txn.rollback()


def test_cross_tenant_cannot_touch_someone_elses_draft(txn):
    c = txn.cursor()
    batch_id = str(uuid.uuid4())
    save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
               contract_revision_id=8, payload={}, expected_total_price_points=0,
               expected_etag=None)
    other = {**IDENTITY, "tenant_owner_user_id": 777}
    with pytest.raises(DraftConflict):
        save_draft(c, batch_id=batch_id, identity=other, brand_id=36, quote_id=410,
                   contract_revision_id=8, payload={"v": "hijack"},
                   expected_total_price_points=0, expected_etag=1)
    txn.rollback()


def test_accept_succeeds_exactly_once(txn):
    """规格 §5.2 末:提交时 CAS 把 draft → accepted,**同一草稿只成功一次**。"""
    c = txn.cursor()
    batch_id = str(uuid.uuid4())
    save_draft(c, batch_id=batch_id, identity=IDENTITY, brand_id=36, quote_id=410,
               contract_revision_id=8, payload={}, expected_total_price_points=0,
               expected_etag=None)
    accepted = accept_draft(c, batch_id=batch_id, tenant_owner_user_id=42, expected_etag=1)
    assert accepted["status"] == "accepted"
    with pytest.raises(DraftConflict):
        accept_draft(c, batch_id=batch_id, tenant_owner_user_id=42, expected_etag=2)
    txn.rollback()


def test_etag_roundtrip():
    assert parse_etag(etag_for(7)) == 7
    for bad in ("", '"7"', "draft", "draft-x", "post-1-rev-2"):
        assert parse_etag(bad) is None, f"{bad!r} 被当成合法 ETag —— 不许猜"


# ============================================================
# B. 发布素材:幂等 / 三态 / unknown 不自动重传
# ============================================================

def _post(c) -> int:
    c.execute("INSERT INTO geo_douyin_posts (brand_id, created_by, keyword, content_type, status)"
              " VALUES (36,99,'k','image_post','ready') RETURNING id")
    return int(c.fetchone()["id"])


def test_artifact_claim_is_idempotent_on_same_hash(txn):
    c = txn.cursor()
    post_id = _post(c)
    rid = str(uuid.uuid4())
    is_new, first = claim_artifact(c, geo_post_id=post_id, post_revision_id=7,
                                   tenant_owner_user_id=42, request_id=rid, request_hash="h1")
    assert is_new is True
    is_new2, again = claim_artifact(c, geo_post_id=post_id, post_revision_id=7,
                                    tenant_owner_user_id=42, request_id=rid, request_hash="h1")
    assert is_new2 is False
    assert again["prepared_artifact_id"] == first["prepared_artifact_id"]
    txn.rollback()


def test_artifact_same_key_different_hash_conflicts(txn):
    c = txn.cursor()
    post_id = _post(c)
    rid = str(uuid.uuid4())
    claim_artifact(c, geo_post_id=post_id, post_revision_id=7, tenant_owner_user_id=42,
                   request_id=rid, request_hash="h1")
    with pytest.raises(ArtifactRequestConflict, match="不同的作品版本"):
        claim_artifact(c, geo_post_id=post_id, post_revision_id=8, tenant_owner_user_id=42,
                       request_id=rid, request_hash="h2")
    txn.rollback()


def test_unknown_state_is_never_auto_retried():
    """🔴 §7.1:远端已接受但本地 ack 丢失 → unknown,**不自动重传**。

    与 failed 合并会让恢复逻辑对"远端可能已经收了"的情形也重传 = 重复外调。
    """
    assert_retryable(STATE_FAILED)                       # 正向
    with pytest.raises(ArtifactNotRetryable, match="人工核对"):
        assert_retryable(STATE_UNKNOWN)
    with pytest.raises(ArtifactNotRetryable):
        assert_retryable(STATE_READY)


def test_terminal_state_cannot_be_flipped_back_to_ready(txn):
    """迟到 worker 不能把 failed/unknown 改回 ready。"""
    c = txn.cursor()
    post_id = _post(c)
    _, art = claim_artifact(c, geo_post_id=post_id, post_revision_id=7,
                            tenant_owner_user_id=42, request_id=str(uuid.uuid4()),
                            request_hash="h1")
    aid = int(art["prepared_artifact_id"])
    assert mark_unknown(c, artifact_id=aid, card_statuses=[]) is not None
    assert mark_ready(c, artifact_id=aid, manifest_hash="m1", card_statuses=[]) is None, \
        "unknown 被改回了 ready"
    assert mark_failed(c, artifact_id=aid, card_statuses=[]) is None
    txn.rollback()


def test_stale_artifact_is_rejected_at_order_time():
    """§11.3:post 在准备后产生新 revision → 旧 artifact 明确 409,不混用旧图新文案。"""
    ready = {"prepared_artifact_id": 1, "post_revision_id": 7,
             "manifest_hash": "m1", "state": STATE_READY}
    assert_artifact_matches(ready, post_revision_id=7, manifest_hash="m1")   # 正向
    with pytest.raises(ArtifactRequestConflict, match="新版本"):
        assert_artifact_matches(ready, post_revision_id=8, manifest_hash="m1")
    with pytest.raises(ArtifactRequestConflict, match="图片清单"):
        assert_artifact_matches(ready, post_revision_id=7, manifest_hash="m2")
    with pytest.raises(ArtifactRequestConflict, match="尚未准备完成"):
        assert_artifact_matches({**ready, "state": "preparing"},
                                post_revision_id=7, manifest_hash="m1")


# ============================================================
# C. 发布 command 单事务落库
# ============================================================

def _fake_freeze(counter: list[int]):
    def _freeze(*, payer_user_id: int, amount: int, task_ref: str):
        counter.append(amount)
        return {"payer_user_id": payer_user_id, "freeze_id": 1000 + len(counter),
                "freeze_table": "legacy", "task_ref": task_ref,
                "reserved_amount": amount, "physical_split_snapshot": {"paid": amount}}
    return _freeze


def _mk(post_id, revision, media_id, item_key=None):
    return {"item_request_id": item_key or str(uuid.uuid4()), "geo_post_id": post_id,
            "post_revision_id": revision, "prepared_artifact_id": "x",
            "manifest_hash": "m1", "media_id": media_id,
            "expected_price_fingerprint": publish_fingerprint(
                feature_code="media_proxy_publish", media_id=media_id,
                final_price_points=9360, markup_version="m1",
                resolver_version="r1", catalog_version="c1")}


def _priced(items):
    return {i["item_request_id"]: {
        "final_price_points": 9360,
        "publish_price_fingerprint": i["expected_price_fingerprint"],
        "price_snapshot": {"final_price_points": 9360},
    } for i in items}


def _artifacts(items):
    return {i["item_request_id"]: {
        "prepared_artifact_id": 1, "post_revision_id": int(i["post_revision_id"]),
        "manifest_hash": i["manifest_hash"], "state": STATE_READY} for i in items}


def test_command_materializes_one_order_and_one_freeze_per_item(txn):
    """🔴 每 item 恰好一 order + 一 item + **一次** freeze(规格 §7.3)。"""
    c = txn.cursor()
    p1, p2 = _post(c), _post(c)
    items = [_mk(p1, 7, 9001), _mk(p2, 8, 9002)]
    counter: list[int] = []
    result = materialize_command(
        c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
        resolved_prices=_priced(items), artifacts=_artifacts(items),
        account_names={9001: "号A", 9002: "号B"}, daily_limit=5,
        settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
        freeze_fn=_fake_freeze(counter))

    assert len(counter) == 2, f"freeze 次数 {len(counter)},期望每项一次"
    assert result["summary"]["total_items"] == 2
    c.execute("SELECT count(*) AS n FROM mhz_publish_orders")
    assert int(c.fetchone()["n"]) == 2, "两篇没有各建一个单内容订单"
    c.execute("SELECT count(*) AS n FROM mhz_publish_order_items WHERE capacity_state='reserved'")
    assert int(c.fetchone()["n"]) == 2
    txn.rollback()


def test_one_to_one_violation_leaves_zero_rows(txn):
    """整批拒绝 = 零订单、零预占、零冻结。"""
    c = txn.cursor()
    p1 = _post(c)
    items = [_mk(p1, 7, 9001), _mk(p1, 7, 9002)]      # 同 revision → 两账号
    counter: list[int] = []
    with pytest.raises(Exception):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=_priced(items), artifacts=_artifacts(items),
            account_names={}, daily_limit=5,
            settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_fake_freeze(counter))
    assert counter == [], "整批拒绝却发生了冻结"
    c.execute("SELECT count(*) AS n FROM mhz_publish_orders")
    assert int(c.fetchone()["n"]) == 0
    txn.rollback()


def test_price_drift_rejects_before_any_freeze(txn):
    c = txn.cursor()
    p1 = _post(c)
    items = [_mk(p1, 7, 9001)]
    priced = _priced(items)
    # 服务端算出来的指纹与客户端确认的不同 = 漂移
    priced[items[0]["item_request_id"]]["publish_price_fingerprint"] = publish_fingerprint(
        feature_code="media_proxy_publish", media_id=9001, final_price_points=9999,
        markup_version="m1", resolver_version="r1", catalog_version="c1")
    counter: list[int] = []
    with pytest.raises(PriceChanged):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=priced, artifacts=_artifacts(items), account_names={},
            daily_limit=5, settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_fake_freeze(counter))
    assert counter == [], "价格漂移却已经冻结了"
    txn.rollback()


def test_cross_chain_fingerprint_is_identity_error_not_price_drift(txn):
    """把**制作**链指纹送进发布提交 → 报对象身份,不是价格漂移。"""
    c = txn.cursor()
    p1 = _post(c)
    items = [_mk(p1, 7, 9001)]
    items[0]["expected_price_fingerprint"] = production_fingerprint(
        feature_code="geo_douyin_image_post", unit_points=390, card_count=4,
        extra_card_points=0, final_price_points=390,
        resolver_version="r1", catalog_version="c1")
    counter: list[int] = []
    with pytest.raises(PriceFingerprintMismatch):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=_priced(items), artifacts=_artifacts(items), account_names={},
            daily_limit=5, settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_fake_freeze(counter))
    assert counter == []
    txn.rollback()


def test_capacity_shortage_rejects_before_any_freeze(txn):
    """余量不足 → 零外调并安全 release;这里体现为**根本没冻结过**。"""
    c = txn.cursor()
    p1, p2 = _post(c), _post(c)
    items = [_mk(p1, 7, 9001), _mk(p2, 8, 9001)]   # 同账号两篇
    counter: list[int] = []
    with pytest.raises(CapacityExceeded):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=_priced(items), artifacts=_artifacts(items),
            account_names={9001: "号A"}, daily_limit=1,     # 余量 1,要 2
            settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_fake_freeze(counter))
    assert counter == [], "余量不足却已经冻结了"
    txn.rollback()


def test_two_posts_same_account_allowed_when_capacity_permits(txn):
    """反向对照:余量够时同账号承接两篇必须放行(A4 已裁)。

    没有这条,上面那条"余量不足则拒"可能只是"同账号一律拒"。
    """
    c = txn.cursor()
    p1, p2 = _post(c), _post(c)
    items = [_mk(p1, 7, 9001), _mk(p2, 8, 9001)]
    counter: list[int] = []
    result = materialize_command(
        c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
        resolved_prices=_priced(items), artifacts=_artifacts(items),
        account_names={9001: "号A"}, daily_limit=2,
        settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
        freeze_fn=_fake_freeze(counter))
    assert result["summary"]["total_items"] == 2 and len(counter) == 2
    txn.rollback()


def test_partial_freeze_handle_is_rejected(txn):
    """半个句柄比没有句柄更危险 —— 它看起来像"有记账"。"""
    c = txn.cursor()
    p1 = _post(c)
    items = [_mk(p1, 7, 9001)]

    def _bad_freeze(*, payer_user_id, amount, task_ref):
        return {"freeze_id": 1, "reserved_amount": amount}   # 缺 freeze_table / payer

    with pytest.raises(FundingHandleInvalid):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=_priced(items), artifacts=_artifacts(items),
            account_names={}, daily_limit=5,
            settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_bad_freeze)
    txn.rollback()


def test_reserved_amount_must_equal_authoritative_price(txn):
    """冻结额必须逐项等于权威价(规格 §6.2 末)。"""
    c = txn.cursor()
    p1 = _post(c)
    items = [_mk(p1, 7, 9001)]

    def _short_freeze(*, payer_user_id, amount, task_ref):
        return {"payer_user_id": payer_user_id, "freeze_id": 1,
                "freeze_table": "legacy", "task_ref": task_ref,
                "reserved_amount": amount - 1, "physical_split_snapshot": {}}

    with pytest.raises(FundingHandleInvalid, match="不一致"):
        materialize_command(
            c, command_request_id=str(uuid.uuid4()), identity=IDENTITY, items=items,
            resolved_prices=_priced(items), artifacts=_artifacts(items),
            account_names={}, daily_limit=5,
            settlement={"authority": AUTHORITY_DIRECT, "payer_user_id": 42},
            freeze_fn=_short_freeze)
    txn.rollback()


def test_coordinator_makes_no_provider_call():
    """🔴「provider 只能在 commit 之后」的形态锁。

    协调器模块**不许**导入任何发布渠道客户端 —— 判据打 AST import 列表,
    不 grep 源码(注释里出现模块名是合法的)。
    """
    import ast
    import io

    src = io.open(REPO / "services/geo_douyin/publish_coordinator.py", encoding="utf-8").read()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = [m for m in imported if "publish_adapter" in m or "meijiehezi_client" in m]
    assert not forbidden, f"协调器导入了发布渠道客户端:{forbidden}"
    # 反向对照:探针能抓到 import(否则"没有"只是探针空转)
    assert imported, "AST import 探针零结果 —— 判据恒真"
    assert any("contract_funding" in m for m in imported)
