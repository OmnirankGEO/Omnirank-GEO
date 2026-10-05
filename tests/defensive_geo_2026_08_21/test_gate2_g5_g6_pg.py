"""门二返修:G5 错误信封全 code 补齐 · G6 admin/平台直营 payer 走平台成本账。

G5 —— 为什么是**全 code census** 而不是「把 VALIDATION_FAILED 补上」
--------------------------------------------------------------------
返修单点名 `VALIDATION_FAILED` 裸信封,但只补那一个,下一个新加的 `raise` 又会忘。
所以判据的分母 = `_ERROR_TABLE` 的**全部键**(机械取,不手抄),
并且实现侧在**构造期**就 fail-closed —— 裸信封在代码层构造不出来。

G6 —— 判据形态就是返修单那句话
------------------------------
「admin confirm → 要么 200+平台账记账、要么 typed 4xx;裸 500=红」。
所以每条 G6 判据都断言**三件事**:HTTP 码不是 5xx、库里的账对不对、
她看到的那句话是不是人话。只验状态码会漏掉「返回 200 但平台账是空的」——
那正是 FIN-06 要防的「admin exempt 完全不写账」。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.defensive_geo.copy_registry import assert_public_copy_clean

pytestmark = pytest.mark.integration

TENANT = 8401
PLATFORM_UID = 8402  # conftest 播过种、有钱包的非 admin 账号,当平台直营服务账号用


@pytest.fixture(scope="module")
def client():
    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        tid = request.headers.get("X-Test-Tenant")
        if tid:
            request.state.user = {
                "user_id": int(tid),
                "is_admin": request.headers.get("X-Test-Admin") == "1",
            }
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _allow_brand(monkeypatch):
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clean_denominator(db):
    with db.cursor() as cur:
        cur.execute("DELETE FROM diagnosis_runs WHERE owner_user_id = ANY(%s)",
                    ([TENANT, PLATFORM_UID],))
    db.commit()
    yield


def _h(tenant=TENANT, idem=None, admin=False):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    if admin:
        h["X-Test-Admin"] = "1"
    return h


def _make_preview(client, *, admin=False):
    plan = client.post(
        "/api/defensive-geo/question-plans/preview",
        json={"clientRequestId": "creq-" + uuid.uuid4().hex[:10], "brandId": 901,
              "profileRevisionId": "prof-1", "mode": "defensive",
              "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                             "familyKey": "identity_check", "brandExposure": "named"}]},
        headers=_h(admin=admin)).json()
    return client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": ["deepseek"]},
        headers=_h(idem="idem-" + uuid.uuid4().hex[:10], admin=admin)).json()


def _counts(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM diagnosis_runs")
        a = cur.fetchone()["c"]
        cur.execute("SELECT count(*) AS c FROM point_freezes")
        b = cur.fetchone()["c"]
        cur.execute("SELECT count(*) AS c FROM notification_outbox")
        c = cur.fetchone()["c"]
    # 🔴 读完立刻收事务(与 test_wp2_confirm_pg.Counts 的 #62 同一病):不收,这条连接就握着
    #    diagnosis_runs / notification_outbox 的读锁「idle in transaction」,随后测试同步等 HTTP,
    #    应用那头重放 diagnosis_runs 迁移要 AccessExclusiveLock —— 等的是测试自己,PG 查不出死锁,
    #    只能挂到超时(新快照库 + prestart 上两臂都复现,Review 09-28)。三个数仍来自同一事务,快照一致不受影响。
    db.rollback()
    return (a, b, c)


# ══════════════════════════════════════════════════════════════════════════
# G5:每个 code 必带 publicExplanation + typed nextAction
# ══════════════════════════════════════════════════════════════════════════
def test_every_error_code_carries_human_copy_and_typed_action():
    """全 code census。分母 = `_ERROR_TABLE` 全部键,机械取。"""
    import api.defensive_geo_api as mod

    codes = sorted(mod._ERROR_TABLE)
    assert len(codes) >= 12, f"错误表只有 {len(codes)} 个 code —— 分母可疑"

    missing_copy, missing_action = [], []
    for code in codes:
        exc = mod._safe_error(code)
        payload = exc.detail
        if code not in mod._NEVER_SURFACED_CODES and not payload.get("publicExplanation"):
            missing_copy.append(code)
        action = payload.get("nextAction") or {}
        if not action.get("kind") or not action.get("label"):
            missing_action.append(code)

    assert not missing_copy, f"这些 code 没有人话解释,她会看到一片空白:{missing_copy}"
    assert not missing_action, f"这些 code 没有 typed 下一步,等于死路:{missing_action}"


def test_every_public_explanation_is_clean_copy():
    """人话必须真的是人话 —— 非空 + 零内部词 + 无 snake_case 枚举形态。"""
    import api.defensive_geo_api as mod

    for code in sorted(mod._ERROR_TABLE):
        payload = mod._safe_error(code).detail
        if payload.get("publicExplanation"):
            assert_public_copy_clean(payload["publicExplanation"], field=f"{code}.publicExplanation")
        assert_public_copy_clean(payload["nextAction"]["label"], field=f"{code}.nextAction.label")


def test_error_defaults_cover_the_table_exactly():
    """默认表与错误表必须**逐键对齐**。

    多一个:留了一条永远走不到的默认(下次改名时没人发现它成了死配置)。
    少一个:那个 code 会回落到 INTERNAL_ERROR 的默认文案,对她说错话。
    """
    import api.defensive_geo_api as mod

    assert set(mod._ERROR_DEFAULTS) == set(mod._ERROR_TABLE), (
        f"只在默认表:{sorted(set(mod._ERROR_DEFAULTS) - set(mod._ERROR_TABLE))};"
        f"只在错误表:{sorted(set(mod._ERROR_TABLE) - set(mod._ERROR_DEFAULTS))}"
    )


def test_never_surfaced_set_stays_minimal():
    """🔴 「永不上屏」是 §0.5 L124 给**一个** code 的豁免,不是一扇后门。

    这个集合每多一个成员,就多一个"她看到空白"的 code。要扩必须有 §0.5 依据。
    而且即便永不上屏,它**仍然**必须带 typed nextAction —— 前端要据此静默动作。
    """
    import api.defensive_geo_api as mod

    assert mod._NEVER_SURFACED_CODES == frozenset({"IDEMPOTENCY_CONFLICT"}), (
        f"永不上屏集合变了:{sorted(mod._NEVER_SURFACED_CODES)}。"
        "扩它需要 §0.5 的明文依据,不能因为「懒得写文案」就加进来。"
    )
    payload = mod._safe_error("IDEMPOTENCY_CONFLICT").detail
    assert "publicExplanation" not in payload, "永不上屏的 code 不该下发用户句"
    assert payload["nextAction"]["label"], "永不上屏 ≠ 没有下一步:前端仍要据此动作"


def test_bare_envelope_cannot_be_constructed(monkeypatch):
    """反向对照:把默认表里某个 code 的 reason 抽掉,构造必须**抛**而不是给裸信封。

    没有这条,上面几条全绿也可能只是"默认表恰好填满了",
    而不是"实现真的拦得住"。
    """
    import api.defensive_geo_api as mod

    patched = dict(mod._ERROR_DEFAULTS)
    kind, target = patched["VALIDATION_FAILED"][1], patched["VALIDATION_FAILED"][2]
    patched["VALIDATION_FAILED"] = (None, kind, target)
    monkeypatch.setattr(mod, "_ERROR_DEFAULTS", patched)

    with pytest.raises(RuntimeError, match="publicExplanation"):
        mod._safe_error("VALIDATION_FAILED")


def test_validation_failed_over_real_http_is_not_a_bare_envelope(client):
    """返修单点名的那一条 —— 真 HTTP,不是纯函数。

    confirm 缺 Idempotency-Key 走的就是 VALIDATION_FAILED。
    """
    prev = _make_preview(client)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h())  # 刻意不给 Idempotency-Key
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["code"] == "VALIDATION_FAILED"
    assert d.get("publicExplanation"), f"裸信封:{d}"
    assert d.get("nextAction", {}).get("label"), f"没有下一步:{d}"
    assert_public_copy_clean(d["publicExplanation"], field="publicExplanation")
    assert_public_copy_clean(d["nextAction"]["label"], field="nextAction.label")


def test_action_ref_is_deterministic_not_random():
    """actionRef 必须由 (kind, target) 确定性导出。

    随机值不泄露对象存在性,但它让**任何**「两个响应必须同形」的判据永远无法成立
    —— 等于把那道防泄露的门拆了(门二当场撞到:NOT_FOUND 一带上 nextAction,
    跨租户/不存在同形判据就红了)。
    """
    import api.defensive_geo_api as mod

    a = mod._action("back_to_list", target={"kind": "page", "page": "history"})
    b = mod._action("back_to_list", target={"kind": "page", "page": "history"})
    c = mod._action("back_to_list", target={"kind": "page", "page": "wallet"})
    assert a["actionRef"] == b["actionRef"], "同一动作两次调用 ref 不同 —— 又是随机值"
    assert a["actionRef"] != c["actionRef"], "不同 target 撞成同一个 ref"
    assert "uuid" not in a["actionRef"].lower()


# ══════════════════════════════════════════════════════════════════════════
# G6:admin / 平台直营 → admin_platform_ledger,平台成本账真记账
# ══════════════════════════════════════════════════════════════════════════
def test_payer_classification_matrix():
    """分类只看服务端身份,不看客户端任何自报字段。"""
    import api.defensive_geo_api as mod

    class _Req:
        def __init__(self, user):
            self.state = type("S", (), {"user": user})()

    assert mod._classify_payer(_Req({"user_id": 1, "is_admin": True})) == (
        "admin_platform_ledger", "platform_cost_center", None)
    assert mod._classify_payer(_Req({"user_id": TENANT, "is_admin": False})) == (
        "personal_wallet", "personal", None)


def test_admin_preview_is_signed_as_platform_ledger(client):
    prev = _make_preview(client, admin=True)
    assert prev["fundingPolicy"] == "admin_platform_ledger", prev
    assert_public_copy_clean(prev["fundingPolicyUserLabel"], field="fundingPolicyUserLabel")


def test_admin_confirm_either_books_the_platform_ledger_or_typed_4xx(client, db, monkeypatch):
    """🔴 返修单的验收句:要么 200+平台账真记账,要么 typed 4xx。**裸 500 = 红**。

    这里给的是「平台账可用」那一臂:换号到一个真实的非 admin 服务账号,
    平台钱包必须真的动 —— 用户钱包一分不扣。
    """
    import services.commercial_service_routing as routing

    monkeypatch.setattr(routing, "get_platform_direct_service_user_id", lambda: PLATFORM_UID)

    prev = _make_preview(client, admin=True)
    before = _counts(db)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="adm-" + uuid.uuid4().hex[:8], admin=True))

    assert r.status_code != 500, f"裸 500 = 红:{r.text}"
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fundingState"] == "exempt_recorded", body

    after = _counts(db)
    dc, df, do = (after[i] - before[i] for i in range(3))
    assert (dc, df, do) == (1, 1, 1), f"(command,freeze,outbox) 增量 = {(dc, df, do)}"

    # 账记在**平台账号**头上,不是发起的 admin 头上
    with db.cursor() as cur:
        cur.execute(
            "SELECT user_id, task_ref FROM point_freezes ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    assert int(row["user_id"]) == PLATFORM_UID, (
        f"平台成本账记到了 {row['user_id']} 头上,应为平台直营账号 {PLATFORM_UID}"
    )


def test_admin_confirm_without_platform_account_is_typed_4xx_not_500(client, db, monkeypatch):
    """平台账在门栈不可用 → typed 4xx + 人话 + 零副作用。"""
    import services.commercial_service_routing as routing

    def _unavailable():
        raise routing.PlatformDirectUnavailable("not configured")

    monkeypatch.setattr(routing, "get_platform_direct_service_user_id", _unavailable)

    prev = _make_preview(client, admin=True)
    before = _counts(db)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="adm2-" + uuid.uuid4().hex[:8], admin=True))
    after = _counts(db)

    assert r.status_code != 500, f"裸 500 = 红:{r.text}"
    assert 400 <= r.status_code < 500, f"应为 typed 4xx,实得 {r.status_code}:{r.text}"
    d = r.json()["detail"]
    assert d["code"] == "PLATFORM_DIRECT_UNSUPPORTED", d
    assert "平台直营账号暂不支持发起" in d["publicExplanation"], d
    assert d["nextAction"]["label"], d
    assert_public_copy_clean(d["publicExplanation"], field="publicExplanation")

    assert tuple(after[i] - before[i] for i in range(3)) == (0, 0, 0), (
        "typed 拒绝却留下了副作用 —— 拒绝必须零 command 零 freeze 零 outbox"
    )


def test_zero_ledger_guard_still_refuses_when_platform_account_is_admin(
    client, db, monkeypatch
):
    """🔴 那把零记账守卫**原样保留**(返修单点名不许削)。

    平台直营账号被配成 admin 时,billing 走免单旁路、**一张表都不写**,
    而对外却会显示"平台已承担" —— 账面凭空消失。守卫必须拦住它。

    门二只改了它的**失败形态**(裸 500 → typed 4xx),没改它拦不拦:
    这条判据同时钉住「仍然拒绝」和「零副作用」。
    """
    import middleware.billing as billing
    import services.commercial_service_routing as routing

    monkeypatch.setattr(routing, "get_platform_direct_service_user_id", lambda: PLATFORM_UID)

    async def _admin_bypass(*a, **k):
        # 复刻 billing 对 is_admin 用户的真实返回(freeze_id=None 且不写任何表)
        return {"freeze_id": None, "amount": 0, "free": True, "admin_exempt": True}

    monkeypatch.setattr(billing, "freeze_points", _admin_bypass)

    prev = _make_preview(client, admin=True)
    before = _counts(db)
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="adm3-" + uuid.uuid4().hex[:8], admin=True))
    after = _counts(db)

    assert r.status_code != 500, f"裸 500 = 红:{r.text}"
    assert 400 <= r.status_code < 500, f"应为 typed 4xx,实得 {r.status_code}:{r.text}"
    assert r.json()["detail"]["code"] == "PLATFORM_DIRECT_UNSUPPORTED"
    assert tuple(after[i] - before[i] for i in range(3)) == (0, 0, 0), (
        "零记账旁路被当成了成功 —— 那正是 FIN-06 要防的「admin exempt 完全不写账」"
    )


def test_non_admin_path_is_unchanged(client, db, monkeypatch):
    """回归:普通服务商仍走个人钱包,账仍记在她自己头上。"""
    import services.commercial_service_routing as routing

    monkeypatch.setattr(routing, "get_platform_direct_service_user_id", lambda: PLATFORM_UID)

    prev = _make_preview(client)
    assert prev["fundingPolicy"] == "personal_wallet", prev
    r = client.post(
        f"/api/defensive-geo/run-previews/{prev['previewId']}/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=_h(idem="usr-" + uuid.uuid4().hex[:8]))
    assert r.status_code == 200, r.text
    assert r.json()["fundingState"] == "frozen"

    with db.cursor() as cur:
        cur.execute("SELECT user_id FROM point_freezes ORDER BY id DESC LIMIT 1")
        assert int(cur.fetchone()["user_id"]) == TENANT, "普通用户的账被记到了别人头上"
