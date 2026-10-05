"""[打包商品下架 2026-08-10] 锁 —— 打在**产物与真出口**上,不是在源码字符串上。

## 三条被这组锁钉死的东西

1. **种子里不许再有那两行**(否则新环境会以 `is_active=true` 复活 ——
   `seed_feature_pricing()` 每次进程启动都重放,且 INSERT 不写 `is_active`,
   吃列默认值 `true`,生产实测);
2. **权限映射不许再指向不存在的商品**;
3. **`/api/wallet/deduct` 对下架商品给明确 400,不是 500**
   —— 验收 §2 点名,且反向对照:在售 code 必须照常受理。

## 反向对照贯穿

每条正向断言都配一条"仍在售的 code 必须不受影响" —— 只证明"下架了"
证明不了"没把别的也弄坏"。
"""
from __future__ import annotations

import pytest

#: 🔴 [#106b · 2026-09-06] `keyword_expand` 入列。🔴 它与前两个**不同**:
#:    前两个当年是「种子里还有、生产还在售」;这一个是 #106(2026-09-05)已在生产
#:    置 is_active=FALSE、#106b 才删种子 —— 也就是说**生产先于代码**。
#:    纳入本组锁是为了让冷建库与生产同口径:否则新环境会长出一条线上已下架的收费项,
#:    而 `ON CONFLICT DO NOTHING` 不会把它改回去,也不会报错。
DELISTED = ("monitor_month_10", "rank_alert", "keyword_expand")
#: 反向对照:监测真实计费走这三条,与下架 SKU 零交叉(生产实测 766 笔仍在跑)
STILL_SELLING = ("monitoring_keyword_daily", "monitor_single", "scheduled_monitoring")


# ══════════════════════════════════════════════════════════════
# 1. 种子(防新环境复活)
# ══════════════════════════════════════════════════════════════
def _seed_codes() -> set[str]:
    """真读 `seed_feature_pricing` 的 PRICING_DATA。

    🔴 用 AST 不用正则:我第一版按 `split("]")` 截断,被**注释里的 `]`**
    在第 13 条就切断了 —— 断言"种子里没有下架商品"当场变成恒真
    (它们排在第 14、15 位)。判据自己坏掉比漏检更坏。
    """
    import ast
    import inspect
    import textwrap

    from db import wallet_db
    tree = ast.parse(textwrap.dedent(inspect.getsource(wallet_db.seed_feature_pricing)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "PRICING_DATA" for t in node.targets):
            rows = ast.literal_eval(node.value)
            return {str(r[0]) for r in rows}
    raise AssertionError("没找到 PRICING_DATA —— 判据失去对象")


def test_delisted_codes_not_in_seed():
    """🔴 只把 DB 改成 is_active=false 不够:种子还在,行被删就会以 true 复活。"""
    codes = _seed_codes()
    leaked = [c for c in DELISTED if c in codes]
    assert not leaked, f"下架商品仍在种子里,新环境会复活: {leaked}"


def test_seed_still_has_the_ones_we_kept():
    """反向对照:种子本身没被我删空(否则上一条恒绿)。"""
    codes = _seed_codes()
    assert "monitor_single" in codes and "geo_diagnosis" in codes
    assert len(codes) > 20, f"种子只剩 {len(codes)} 条,像是被误删了"


# ══════════════════════════════════════════════════════════════
# 2. 权限映射
# ══════════════════════════════════════════════════════════════
def test_permission_map_has_no_dead_sku():
    from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES as M

    leaked = [c for c in DELISTED if c in M]
    assert not leaked, f"权限映射仍指向已下架商品(死代码): {leaked}"


def test_permission_map_keeps_live_monitoring():
    """反向对照:在售监测 code 的映射一条都不许丢。"""
    from services.organization_contract import BILLABLE_FEATURE_CAPABILITIES as M

    for c in STILL_SELLING:
        assert c in M, f"误伤在售 code 的权限映射: {c}"


# ══════════════════════════════════════════════════════════════
# 3. 真出口:/api/wallet/deduct 的拒绝形态
# ══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_deduct_rejects_delisted_with_400_not_500(monkeypatch):
    """🔴 判据打在**端点**上,不是在 `get_feature_pricing` 上。

    改前链路:`get_feature_pricing` 带 `WHERE is_active=TRUE` → 查不到 →
    `raise ValueError` → `deduct_points` 不捕 → FastAPI **500**。
    用户看到"服务器错误",既不知道商品下架了,也没有下一步。
    """
    from fastapi import HTTPException

    from api import wallet_api

    async def _boom(*_a, **_k):
        raise ValueError("未知的功能编码: monitor_month_10")

    monkeypatch.setattr("middleware.billing.deduct_points", _boom)
    monkeypatch.setattr(wallet_api, "get_wallet_balance",
                        lambda *_a, **_k: {"paid_points": 0, "bonus_points": 0})

    req = wallet_api.DeductRequest(feature_code="monitor_month_10")
    request = _fake_request(is_admin=False)

    with pytest.raises(HTTPException) as ei:
        await wallet_api.deduct(req, request)

    assert ei.value.status_code == 400, f"应是 400,实得 {ei.value.status_code}"
    detail = str(ei.value.detail)
    assert "不可用" in detail, detail
    assert "未扣" in detail, "必须说清钱没动(资金类文案铁律)"
    # 出口:告诉用户下一步(总册 §13.5 拦截必须带出口)
    assert "价目表" in detail or "联系平台" in detail, f"没有出口: {detail}"
    # 不许把内部编码抛给用户
    assert "monitor_month_10" not in detail, "回显了内部 feature_code"


@pytest.mark.asyncio
async def test_deduct_still_accepts_live_code(monkeypatch):
    """🔴 反向对照:没把扣费端点整个堵死 —— 在售 code 必须正常受理。"""
    from api import wallet_api

    called = {}

    async def _ok(user_id, feature_code, *_a, **_k):
        called["code"] = feature_code
        return {"success": True, "deducted": 1}

    monkeypatch.setattr("middleware.billing.deduct_points", _ok)
    monkeypatch.setattr(wallet_api, "get_wallet_balance",
                        lambda *_a, **_k: {"paid_points": 9, "bonus_points": 0})

    req = wallet_api.DeductRequest(feature_code="monitoring_keyword_daily")
    resp = await wallet_api.deduct(req, _fake_request(is_admin=False))

    assert resp["success"] is True
    assert resp["deducted"] == 1
    assert called["code"] == "monitoring_keyword_daily"


def _fake_request(*, is_admin: bool):
    class _State:
        user = {"user_id": 12345, "is_admin": is_admin}

    class _Req:
        state = _State()

    return _Req()


# ══════════════════════════════════════════════════════════════
# 4. 托管:隐藏 ≠ 删除(禁做项的锁)
# ══════════════════════════════════════════════════════════════
def test_managed_code_and_endpoints_still_exist():
    """🔴 Owner 明示:能力保留,未来重启。谁把它当死代码删了,这条转红。"""
    import importlib
    import pathlib

    mod = importlib.import_module("api.managed_campaign_api")
    assert mod is not None, "托管 API 模块被删了 —— Owner 明令保留"

    repo = pathlib.Path(__file__).resolve().parents[2]
    for must_exist in (
        "api/managed_campaign_api.py",
        "scripts/migration_v3_3_managed_campaign.sql",
        "frontend/src/pages/Admin/ManagedCampaignsAdmin.tsx",
        "frontend/src/components/managed/api.ts",
        "docs/AI-CONTEXT/MANAGED_CAMPAIGN_RESTORE_2026-08-10.md",
    ):
        assert (repo / must_exist).exists(), f"被删了(禁做项): {must_exist}"


def test_managed_migration_still_registered():
    """迁移不许反注册 —— 反注册 = 新环境建不出托管的定价行,等于事实废弃。"""
    from db import migration_manifest

    text = " ".join(str(x) for x in getattr(migration_manifest, "MIGRATIONS", []))
    if not text.strip():
        text = (
            __import__("pathlib").Path(migration_manifest.__file__).read_text(encoding="utf-8"))
    assert "migration_v3_3_managed_campaign.sql" in text, "托管迁移被反注册了"
