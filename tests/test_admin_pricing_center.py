"""算力定价中心后端回归(复审返修)· 纯 mock 不连真 DB。

覆盖老板复审要求的 3 个回归 + 软校验:
  1. 负 wholesale create 被拒(后端兜底·不只靠前端挡)
  2. recalc-all 默认不覆盖 non_standard(活动包/人工谈价包)· include_non_standard=true 才覆盖
  3. 编辑清空进货价 = 按当前规则算(non_standard=False·与输入框文案一致)· 非规则值软校验只标记不阻止

资金铁律:全程只 mock sku_templates 读写 · 不碰 recharge_orders。
"""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import api.admin_factory_api as mod


class FakeCursor:
    def __init__(self, fetchone_result=None, fetchall_result=None):
        self._one = fetchone_result
        self._all = fetchall_result if fetchall_result is not None else []
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def _fake_db(cursor):
    @contextmanager
    def _cm():
        yield FakeConn(cursor)
    return _cm


def _patch_common(monkeypatch):
    # admin 鉴权放行(不读 request.state)
    monkeypatch.setattr(mod, "_require_admin", lambda request: {"user_id": 1, "is_admin": True})
    # 出厂规则(示例 9 折 225/325)
    monkeypatch.setattr("config.pricing_config.get_wholesale_ratio", lambda: (225, 325))


def _update_target_ids(cursor):
    """从 fake cursor.executed 里提取被 UPDATE sku_templates 的 id(params[1])。"""
    ids = []
    for sql, params in cursor.executed:
        if "UPDATE sku_templates" in sql and params:
            ids.append(params[1])
    return ids


# ============================================================
# 回归 1:负 wholesale create 被拒(后端兜底)
# ============================================================

async def test_create_rejects_negative_wholesale(monkeypatch):
    _patch_common(monkeypatch)
    req = mod.AdminSKUCenterCreateRequest(
        sku_type="credit_pack", display_name="测试包",
        points_granted=130, retail_cents=10000, wholesale_cents=-5,
    )
    with pytest.raises(HTTPException) as ei:
        await mod.admin_pricing_sku_create(req, request=None)
    assert ei.value.status_code == 400
    assert "wholesale_cents" in str(ei.value.detail)


# ============================================================
# 回归 2:recalc-all 默认不覆盖 non_standard
# ============================================================

async def test_recalc_all_skips_non_standard_by_default(monkeypatch):
    _patch_common(monkeypatch)
    # id=1 标准(130/90·130×225==90×325) · id=2 非标准(100/999·活动包)
    rows = [
        {"id": 1, "points_granted": 130, "wholesale_cents": 90},
        {"id": 2, "points_granted": 100, "wholesale_cents": 999},
    ]
    cur = FakeCursor(fetchall_result=rows)
    monkeypatch.setattr(mod, "get_db", _fake_db(cur))
    req = mod.AdminSKURecalcAllRequest(only_active=False, include_non_standard=False)
    r = await mod.admin_pricing_sku_recalc_all(req, request=None)
    assert 2 not in _update_target_ids(cur), "非标准包(id=2)不应被默认批量重算覆盖"
    assert r["skipped_count"] == 1
    item2 = next(it for it in r["items"] if it["id"] == 2)
    assert item2.get("skipped") is True
    item1 = next(it for it in r["items"] if it["id"] == 1)
    assert not item1.get("skipped")


async def test_recalc_all_can_force_include_non_standard(monkeypatch):
    _patch_common(monkeypatch)
    rows = [{"id": 2, "points_granted": 100, "wholesale_cents": 999}]
    cur = FakeCursor(fetchall_result=rows)
    monkeypatch.setattr(mod, "get_db", _fake_db(cur))
    req = mod.AdminSKURecalcAllRequest(only_active=False, include_non_standard=True)
    r = await mod.admin_pricing_sku_recalc_all(req, request=None)
    # 强制覆盖:非标准包被重算(100×225/325 ceil = 70 ≠ 999)
    assert 2 in _update_target_ids(cur)
    assert r["skipped_count"] == 0


# ============================================================
# 回归 3:编辑清空进货价 = 按规则算(non_standard=False) · 软校验非规则值只标记不阻止
# ============================================================

async def test_edit_empty_wholesale_recalc_matches_rule(monkeypatch):
    _patch_common(monkeypatch)
    from services.agent_pricing import calc_factory_cents
    # 前端"留空自动算"公式 ceil(points×numer/denom) == 后端 calc_factory_cents(同口径)
    rule_wholesale = calc_factory_cents(130, 225, 325)
    assert rule_wholesale == 90
    # save 收到按规则算的 90 → is_standard → non_standard=False(与文案"按规则自动算"一致)
    cur = FakeCursor(fetchone_result={"points_granted": 130, "wholesale_cents": 90})
    monkeypatch.setattr(mod, "get_db", _fake_db(cur))
    req = mod.AdminSKUCenterSaveRequest(points_granted=130, wholesale_cents=rule_wholesale)
    r = await mod.admin_pricing_sku_save(1, req, request=None)
    assert r["non_standard"] is False


async def test_save_non_standard_only_marks_not_block(monkeypatch):
    _patch_common(monkeypatch)
    # 非规则进货价 999 → 软校验:标记 non_standard 但仍保存成功(老板第六点·不阻止)
    cur = FakeCursor(fetchone_result={"points_granted": 130, "wholesale_cents": 90})
    monkeypatch.setattr(mod, "get_db", _fake_db(cur))
    req = mod.AdminSKUCenterSaveRequest(points_granted=130, wholesale_cents=999)
    r = await mod.admin_pricing_sku_save(1, req, request=None)
    assert r["success"] is True
    assert r["non_standard"] is True


async def test_save_rejects_negative_wholesale(monkeypatch):
    _patch_common(monkeypatch)
    req = mod.AdminSKUCenterSaveRequest(points_granted=130, wholesale_cents=-1)
    with pytest.raises(HTTPException) as ei:
        await mod.admin_pricing_sku_save(1, req, request=None)
    assert ei.value.status_code == 400


# ============================================================
# 返修2:display_name 空值兜底(防全空格脏数据)
# ============================================================

async def test_create_rejects_blank_display_name(monkeypatch):
    _patch_common(monkeypatch)
    req = mod.AdminSKUCenterCreateRequest(
        sku_type="credit_pack", display_name="   ",
        points_granted=130, retail_cents=10000,
    )
    with pytest.raises(HTTPException) as ei:
        await mod.admin_pricing_sku_create(req, request=None)
    assert ei.value.status_code == 400
    assert "display_name" in str(ei.value.detail)


async def test_save_rejects_blank_display_name(monkeypatch):
    _patch_common(monkeypatch)
    req = mod.AdminSKUCenterSaveRequest(display_name="   ", points_granted=130)
    with pytest.raises(HTTPException) as ei:
        await mod.admin_pricing_sku_save(1, req, request=None)
    assert ei.value.status_code == 400
    assert "display_name" in str(ei.value.detail)
