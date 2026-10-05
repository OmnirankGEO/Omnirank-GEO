"""客户线上购买门控 · 判别锁(2026-07-29)

工单:docs/AI-CONTEXT/WORKORDER_CLIENT_PURCHASE_GATE_2026-07-27.md §2

这些锁全部是**行为级**的:喂一个按参数键值应答的假 cursor,调真 handler,断结论。
不是源码串断言 —— 把判定逻辑换个写法但读错账号,假 cursor 会照实返回错账号的值,
锁照样转红。唯一的例外是"文案扫描锁"和"前端单一判定源锁",它们本来就是文本约束。

变异验证(每条锁都做过,详见交付说明):
  M1 去掉后端拦截只留前端        → 锁 4 / 锁 5 转红
  M2 判定只读主账号层忽略客户级  → 锁 6 / 锁 7 转红
  M3 归属链读销售层而非主账号    → 锁 8 转红
  M4 误伤服务商本人             → 锁 2 转红
  M5 自由充值区改为置灰仍渲染    → 锁 10 转红
  M6 拦截文案写进服务商名/角色词 → 锁 9 转红
"""

import re
from pathlib import Path

import pytest

from services import client_purchase_gate as gate

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 假 cursor:按**实际绑定参数**应答,而不是按 SQL 文本猜。
# 判定层读错账号 → 这里返回错账号的行 → 锁转红。
# ============================================================

class FakeCursor:
    def __init__(self, *, users, wallets, admins, memberships, orgs, bindings):
        self.users = users              # {user_id: allow_client_online_purchase(bool)}
        self.wallets = wallets          # {user_id: agent_level(int)}
        self.admins = set(admins)       # {user_id}
        self.memberships = memberships  # [{user_id, organization_id, is_owner, status}]
        self.orgs = orgs                # {org_id: {owner_user_id, status}}
        self.bindings = bindings        # {customer_user_id: {agent_user_id, override}}
        self._result = None
        self.seen_user_ids = []         # 判定层到底问了哪些账号(锁 8 用)

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "is_org_member" in text:
            uid = int(params["uid"])
            self._result = [{
                "agent_level": int(self.wallets.get(uid, 0) or 0),
                "is_admin": uid in self.admins,
                "is_org_member": any(
                    m["user_id"] == uid and m["status"] == "active" for m in self.memberships
                ),
            }]
            return
        if "FROM customer_agent_bindings" in text:
            uid = int(params[0])
            row = self.bindings.get(uid)
            self._result = [] if row is None else [{
                "agent_user_id": row["agent_user_id"],
                "online_purchase_override": row["override"],
            }]
            return
        if "organization_memberships m" in text and "owner_user_id" in text:
            uid = int(params[0])
            for m in sorted(self.memberships, key=lambda r: r["organization_id"]):
                org = self.orgs.get(m["organization_id"], {})
                if (m["user_id"] == uid and m["status"] == "active"
                        and not m["is_owner"] and org.get("status") == "active"):
                    self._result = [{"owner_user_id": org["owner_user_id"]}]
                    return
            self._result = []
            return
        if "allow_client_online_purchase FROM users" in text:
            uid = int(params[0])
            self.seen_user_ids.append(uid)
            self._result = (
                [] if uid not in self.users
                else [{"allow_client_online_purchase": self.users[uid]}]
            )
            return
        raise AssertionError(f"假 cursor 收到未预期的 SQL: {text[:160]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


def build(**overrides):
    """默认世界:主账号 100(服务商) · 销售 101(组织 7 的非 owner 席位) · 客户 200/201。"""
    base = dict(
        # 400 = 另一个主账号,开关**关**。300 绑在 400 名下,且 300 自己不带任何
        # 组织席位 —— 这样锁 2 才能单独打到 agent_level 那条身份放行分支上,
        # 不会被"组织席位放行"顺手兜住(那样删掉服务商分支也全绿 = 假绿)。
        users={100: True, 101: True, 300: True, 400: False},
        wallets={100: 1, 101: 0, 200: 0, 201: 0, 300: 2, 400: 1},
        admins=set(),
        memberships=[
            {"user_id": 100, "organization_id": 7, "is_owner": True, "status": "active"},
            {"user_id": 101, "organization_id": 7, "is_owner": False, "status": "active"},
        ],
        orgs={7: {"owner_user_id": 100, "status": "active"}},
        bindings={
            200: {"agent_user_id": 100, "override": None},
            201: {"agent_user_id": 101, "override": None},  # 绑在销售身上
            # 主账号 100 / 销售 101 自己名下也挂着到 300 的残留绑定。
            # 真实场景:老客户后来升级成服务商 / 加入组织席位,旧 binding 还在
            # (services/admin_user_governance._convert_customer_binding_to_channel_on_upgrade)。
            # 有这两行,锁 2 才对"误伤服务商本人/销售本人"这个变异有区分度 ——
            # 否则他们走 no_unique_provider 分支恒放行,删掉身份放行分支测试照样全绿(假绿)。
            100: {"agent_user_id": 300, "override": None},
            101: {"agent_user_id": 300, "override": None},
            300: {"agent_user_id": 400, "override": None},
        },
    )
    base.update(overrides)
    return FakeCursor(**base)


def decide(cur, uid):
    return gate.resolve_online_purchase_permission(cur, uid)["can_purchase_online"]


# ============================================================
# 锁 1 / 2:开关开、无推荐人、服务商本人、销售本人 → 与今日行为一致(放行)
# ============================================================

def test_lock1_switch_on_customer_can_purchase():
    """主账号开关默认开 → 名下客户照常线上购买(存量零变化)。"""
    assert decide(build(), 200) is True


def test_lock2_service_provider_self_never_blocked():
    """变异:误伤服务商本人 → 本锁转红。

    300 是纯服务商(agent_level=2 · 无组织席位),名下残留一条到主账号 400 的绑定,
    而 400 的开关是关的。只有"服务商本人恒放行"这条分支在,300 才买得了。
    """
    assert decide(build(), 300) is True
    # 同一个人若不是服务商(agent_level=0),同样的绑定就该被拦 —— 证明区分度来自身份
    cur = build(wallets={100: 1, 101: 0, 200: 0, 201: 0, 300: 0, 400: 1})
    assert decide(cur, 300) is False


def test_lock2b_sales_seat_self_never_blocked():
    """销售(组织非 owner 席位)自己的购买不受名下客户门控影响。"""
    cur = build(users={100: False, 101: True, 300: False})
    assert decide(cur, 101) is True


def test_lock2c_admin_never_blocked():
    cur = build(users={100: False, 101: True, 300: False}, admins={999},
                wallets={100: 1, 101: 0, 200: 0, 201: 0, 300: 2, 999: 0},
                bindings={999: {"agent_user_id": 300, "override": None}})
    assert decide(cur, 999) is True


def test_lock3_customer_without_referrer_not_blocked():
    """自注册无推荐人 → 找不到唯一主账号 → 视为开(工单 §1-2 明示)。"""
    cur = build(users={100: False, 101: True, 300: True}, bindings={})
    assert decide(cur, 500) is True


def test_lock3b_multiple_bindings_fail_open():
    """归属不唯一时不拦 —— 门控自己拿不准绝不堵付款路。"""
    cur = build(users={100: False, 101: True, 300: True})
    original = cur.execute

    def two_rows(sql, params=None):
        if "FROM customer_agent_bindings" in " ".join(sql.split()):
            cur._result = [
                {"agent_user_id": 100, "online_purchase_override": None},
                {"agent_user_id": 101, "online_purchase_override": None},
            ]
            return
        original(sql, params)

    cur.execute = two_rows
    assert decide(cur, 200) is True


def test_lock3c_gate_failure_fails_open():
    """判定层自己炸了也必须放行(fail-open 铁律)。"""
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    assert decide(Boom(), 200) is True


# ============================================================
# 锁 4 / 5:开关关 → 后端真的拦(403 + 同口径 code/文案)
# ============================================================

def test_lock4_switch_off_blocks_customer():
    """变异:去掉后端拦只留前端 → 本锁转红。"""
    cur = build(users={100: False, 101: True, 300: True})
    assert decide(cur, 200) is False


def test_lock5_guard_raises_403_with_canonical_payload(monkeypatch):
    """支付发起守卫抛 403,detail 与前端消费的 code/文案逐字一致。"""
    from fastapi import HTTPException

    monkeypatch.setattr(gate, "can_purchase_online", lambda uid, cursor=None: False)
    with pytest.raises(HTTPException) as exc:
        gate.require_online_purchase_allowed(200)
    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "code": gate.BLOCKED_CODE,
        "message": gate.BLOCKED_MESSAGE,
    }

    monkeypatch.setattr(gate, "can_purchase_online", lambda uid, cursor=None: True)
    gate.require_online_purchase_allowed(200)  # 放行态不抛


# ============================================================
# 锁 6 / 7:两级优先级(客户级 > 主账号默认)· 三组合各一条
# ============================================================

def test_lock6_customer_allow_overrides_provider_off():
    """主账号关 + 客户级"允许" → 放行。变异(只读主账号层)→ 转红。"""
    cur = build(
        users={100: False, 101: True, 300: True},
        bindings={200: {"agent_user_id": 100, "override": True}},
    )
    assert decide(cur, 200) is True


def test_lock7_customer_offline_only_overrides_provider_on():
    """主账号开 + 客户级"仅线下" → 拦。变异(只读主账号层)→ 转红。"""
    cur = build(
        users={100: True, 101: True, 300: True},
        bindings={200: {"agent_user_id": 100, "override": False}},
    )
    assert decide(cur, 200) is False


@pytest.mark.parametrize("provider_default,expected", [(True, True), (False, False)])
def test_lock7b_customer_null_follows_provider_default(provider_default, expected):
    """客户级 NULL → 严格跟随主账号默认(两个方向都测)。"""
    cur = build(
        users={100: provider_default, 101: True, 300: True},
        bindings={200: {"agent_user_id": 100, "override": None}},
    )
    assert decide(cur, 200) is expected


# ============================================================
# 锁 8:归属链上溯 —— 销售名下客户读到的是**主账号**开关
# ============================================================

def test_lock8_sales_bound_customer_reads_primary_account_switch():
    """客户 201 绑在销售 101 身上;主账号 100 关、销售 101 开。

    正确实现读主账号 100 → 拦。
    变异(读销售层 101)→ 会放行 → 本锁转红。
    """
    cur = build(users={100: False, 101: True, 300: True})
    assert decide(cur, 201) is False
    # 判定层必须真的去问了主账号,而不是销售
    assert cur.seen_user_ids == [100]


def test_lock8b_sales_bound_customer_follows_primary_when_on():
    cur = build(users={100: True, 101: False, 300: True})
    assert decide(cur, 201) is True
    assert cur.seen_user_ids == [100]


# ============================================================
# 锁 9:文案扫描 —— 拦截提示零内部术语零服务商信息
# ============================================================

# 客户面前禁止出现的内部角色词/身份词(工单 §0 白标铁律)
FORBIDDEN_CUSTOMER_FACING = (
    "服务商", "代理", "主账号", "销售", "上级", "运营商", "经销商", "分销",
    "agent", "provider", "reseller",
)


def test_lock9_backend_blocked_copy_has_no_internal_terms():
    """后端拦截文案 + code 全文扫描:零命中内部角色词、零服务商标识字段。"""
    payload = gate.blocked_http_detail()
    blob = f"{payload['code']} {payload['message']}"
    for term in FORBIDDEN_CUSTOMER_FACING:
        assert term.lower() not in blob.lower(), f"客户可见文案出现内部术语: {term}"
    # 必须用「推荐人」称呼(工单 §0 定死的口径)
    assert "推荐人" in payload["message"]
    # 绝不下发任何归属主体标识
    assert "provider_user_id" not in blob and "agent_user_id" not in blob


def test_lock9b_frontend_customer_facing_copy_has_no_internal_terms():
    """前端客户侧拦截文案(唯一来源文件)同样零内部术语。"""
    source = (ROOT / "frontend/src/components/wallet/onlinePurchaseGate.ts").read_text(
        encoding="utf-8"
    )
    # 只扫字符串字面量,注释里解释铁律时会出现这些词属正常
    literals = re.findall(r"=\s*'([^']*)'", source)
    assert literals, "未取到任何文案字面量 · 扫描锁失效"
    for text in literals:
        for term in FORBIDDEN_CUSTOMER_FACING:
            assert term.lower() not in text.lower(), f"前端客户可见文案出现内部术语: {term} in {text}"
    assert any("推荐人" in t for t in literals)


def test_lock9c_frontend_and_backend_copy_match_byte_for_byte():
    """前后端拦截文案必须逐字一致 —— 否则同一次拦截会出现两种说法。"""
    source = (ROOT / "frontend/src/components/wallet/onlinePurchaseGate.ts").read_text(
        encoding="utf-8"
    )
    assert f"'{gate.BLOCKED_MESSAGE}'" in source
    assert f"'{gate.BLOCKED_CODE}'" in source


# ============================================================
# 锁 10:自由充值窗跟随可见性 —— 被禁时**不渲染**,不是置灰
# ============================================================

def test_lock10_free_amount_area_is_conditionally_unrendered():
    """BuyCredit 里自由充值区必须挂在 canPurchase 条件渲染下。

    变异(改成 disabled/置灰仍渲染)→ 条件消失 → 本锁转红。
    真渲染断言另见 frontend/tests/online-purchase-gate.spec.ts(Playwright)。
    """
    source = (ROOT / "frontend/src/pages/Customer/BuyCredit.tsx").read_text(encoding="utf-8")
    assert "{!canPurchase ? (" in source, "自由充值区没有按 canPurchase 二选一渲染"
    # 自由金额输入框必须落在 canPurchase 为真的那一支里
    blocked_branch = source.split("{!canPurchase ? (")[1].split(") : (")[0]
    assert "自由充值金额" not in blocked_branch, "被禁分支里仍然渲染了自由金额输入区"
    assert "online-purchase-blocked-notice" in blocked_branch, "被禁分支缺提示卡"
    # 不允许用置灰替代不渲染
    assert "disabled={!canPurchase}" not in source, "自由充值区用了置灰而不是不渲染"


# ============================================================
# 锁 11:单一判定源 —— 前端只消费后端布尔,不自行拼两级逻辑
# ============================================================

def test_lock11_frontend_consumes_backend_boolean_only():
    hook = (ROOT / "frontend/src/hooks/useOnlinePurchaseGate.tsx").read_text(encoding="utf-8")
    ctx = (ROOT / "frontend/src/context/WalletContext.tsx").read_text(encoding="utf-8")
    buy = (ROOT / "frontend/src/pages/Customer/BuyCredit.tsx").read_text(encoding="utf-8")

    # 判定只来自 wallet.canPurchaseOnline
    assert "wallet.canPurchaseOnline" in hook
    assert "can_purchase_online" in ctx

    # 前端**不得**出现两级判定的原料:客户级覆盖三态 / 主账号默认开关
    for forbidden in ("online_purchase_override", "allow_client_online_purchase"):
        assert forbidden not in hook, f"hook 里出现了两级判定原料 {forbidden}"
        assert forbidden not in buy, f"客户购买页出现了两级判定原料 {forbidden}"
        assert forbidden not in ctx, f"钱包上下文出现了两级判定原料 {forbidden}"


def test_lock11b_wallet_serializer_exposes_only_the_boolean():
    """钱包序列化白名单放行布尔,但绝不放行归属主体标识。"""
    from api.wallet_api import _PUBLIC_WALLET_FIELDS, _serialize_public_wallet

    assert "can_purchase_online" in _PUBLIC_WALLET_FIELDS
    result = _serialize_public_wallet({
        "paid_points": 10,
        "can_purchase_online": False,
        "provider_user_id": 100,
        "agent_user_id": 100,
    })
    assert result["can_purchase_online"] is False
    assert "provider_user_id" not in result
    assert "agent_user_id" not in result


# ============================================================
# 锁 12:守卫 → 判定 的真接线(不 monkeypatch,走真链路)
# ============================================================

def test_lock12_guard_consults_the_real_resolver():
    """变异:把 can_purchase_online 改成恒 True(等于摘掉后端拦)→ 本锁转红。

    锁 5 为了单测 HTTP 形状 monkeypatch 了判定函数,那条锁挡不住"判定被架空"。
    这条锁不 patch 任何东西,喂真 cursor 走 resolve → can_purchase_online →
    require_online_purchase_allowed 全链。
    """
    from fastapi import HTTPException

    blocked = build(users={100: False, 101: True, 300: True, 400: False})
    with pytest.raises(HTTPException) as exc:
        gate.require_online_purchase_allowed(200, cursor=blocked)
    assert exc.value.status_code == 403

    allowed = build()
    gate.require_online_purchase_allowed(200, cursor=allowed)  # 开关开 → 不抛


# ============================================================
# 锁 13:支付发起端点 → 守卫 的真接线
#
# 变异(删掉任一端点里的 require_online_purchase_allowed)→ 对应用例转红。
# 这里直接调 handler 协程,不是扫源码串 —— 把守卫换个写法但没真拦,照样转红。
# ============================================================

def _stub_request(user_id: int = 200):
    from types import SimpleNamespace

    return SimpleNamespace(
        state=SimpleNamespace(user={"user_id": user_id, "is_admin": False}),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _assert_blocked(coro_factory, monkeypatch):
    """判定被拦时,handler 必须在做任何别的事之前抛 403 + 同口径 detail。"""
    import asyncio

    from fastapi import HTTPException

    monkeypatch.setattr(gate, "can_purchase_online", lambda uid, cursor=None: False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro_factory())
    assert exc.value.status_code == 403, f"期望 403 实际 {exc.value.status_code}"
    assert exc.value.detail == {"code": gate.BLOCKED_CODE, "message": gate.BLOCKED_MESSAGE}


def test_lock13a_wallet_recharge_endpoint_is_gated(monkeypatch):
    from api.wallet_api import RechargeRequest, create_recharge

    _assert_blocked(
        lambda: create_recharge(RechargeRequest(custom_amount_yuan=100), _stub_request()),
        monkeypatch,
    )


def test_lock13b_wechat_jsapi_create_endpoint_is_gated(monkeypatch):
    import api.wallet_api as wallet_api

    body = wallet_api._WxJsapiCreateBody(order_id="ORDER-1", openid="oX")
    _assert_blocked(
        lambda: wallet_api.wechat_jsapi_create(body, _stub_request()),
        monkeypatch,
    )

