"""报价中心定价权 单测(2026-06-08 · 老板拍板:报价给终端客户永远用【本人】· 绝不继承上级)。

报价中心 = 代运营报价工具,只服务用户给自己终端客户报价;上级只赚算力/充值/进货/返利链路,
不干预他对客户的报价。报价走 get_*_for_quote_viewer:
  - 永不继承上级(resolve_quote_pricing_user_id 永远本人)
  - [返修点2] 平台默认报价系数固定 1.0(DEFAULT_QUOTE_MARKUP_RATIO);不区分"未设 vs 自设1.0"(都=1.0按成本)
  - [返修点3] 签约闸也管生效:未签普通用户报价生成【忽略】本人自设系数/成本,用平台默认/动态
  - 服务商(agent_level>=1)/ 已签普通用户 → 本人自设生效;admin override 永远优先
旧 get_*_for_viewer 保留"有上级就继承"语义(dormant),用 test_split_* 证明分流。
纯 mock · 不连真 DB。
"""
import services.quote_pricing_preferences as qpp
import services.agent_pricing_overrides as apo
import services.agent_agreement as aa


def _patch(monkeypatch, *, levels=None, owners=None, ratios=None, costs=None, overrides=None, signed=None, admins=None):
    levels = levels or {}; owners = owners or {}; ratios = ratios or {}
    costs = costs or {}; overrides = overrides or {}; signed = signed or {}; admins = admins or set()
    monkeypatch.setattr(qpp, "_get_agent_level", lambda uid: levels.get(uid, 0))
    monkeypatch.setattr(qpp, "resolve_owning_agent", lambda uid: owners.get(uid))
    monkeypatch.setattr(qpp, "get_user_quote_markup_ratio", lambda uid: ratios.get(uid))
    monkeypatch.setattr(qpp, "get_user_cost_per_article", lambda uid: costs.get(uid))
    monkeypatch.setattr(qpp, "get_system_quote_markup_ratio", lambda: 1.0)
    monkeypatch.setattr(apo, "get_agent_quote_markup_override", lambda uid: overrides.get(uid))
    monkeypatch.setattr(aa, "is_pricing_disclaimer_signed", lambda uid: signed.get(uid, False))
    import db.auth_db as auth_db
    monkeypatch.setattr(auth_db, "get_user", lambda uid: {"is_admin": uid in admins})


# ===== 报价中心:永不继承上级 + 已签生效(get_*_for_quote_viewer) =====

def test_quote_resolve_always_self():
    assert qpp.resolve_quote_pricing_user_id(10) == 10
    assert qpp.resolve_quote_pricing_user_id(None) is None


def test_case1_l0_signed_owner_self_1_5_uses_self(monkeypatch):
    # L0(10) 已签 + 有上级 99(2.0)+ 自设 1.5 → 用自己 1.5(不继承上级)
    _patch(monkeypatch, owners={10: 99}, ratios={10: 1.5, 99: 2.0}, signed={10: True})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 1.5


def test_case2_l0_signed_self_1_0_not_inherit(monkeypatch):
    # L0(10) 已签 + 有上级 99(2.0)+ 自设 1.0 → 用自己 1.0(不继承上级 2.0)
    _patch(monkeypatch, owners={10: 99}, ratios={10: 1.0, 99: 2.0}, signed={10: True})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 1.0


def test_case3_l0_signed_unset_platform_default_1_0(monkeypatch):
    # L0(10) 已签 + 有上级 99(2.0)+ 未设(None)→ 平台默认固定 1.0(不继承上级 2.0)
    _patch(monkeypatch, owners={10: 99}, ratios={99: 2.0}, signed={10: True})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 1.0


def test_case4_l0_signed_self_cost_uses_self(monkeypatch):
    # L0(10) 已签 + 有上级(成本 350)+ 自设成本 80 → 用自己 80(不继承上级成本)
    _patch(monkeypatch, owners={10: 99}, costs={10: 80.0, 99: 350.0}, signed={10: True})
    assert qpp.get_cost_per_article_for_quote_viewer(10) == 80.0


def test_case5_l0_signed_no_cost_returns_none(monkeypatch):
    # L0(10) 已签 + 有上级(成本 350)+ 未设成本 → None(走系统动态·不继承上级 350)
    _patch(monkeypatch, owners={10: 99}, costs={99: 350.0}, signed={10: True})
    assert qpp.get_cost_per_article_for_quote_viewer(10) is None


def test_case6_admin_override_wins(monkeypatch):
    # L0(10) 自设 1.5 但平台 admin override 3.0 → 报价用 3.0(平台强制优先·签约与否都优先)
    _patch(monkeypatch, owners={10: 99}, ratios={10: 1.5}, overrides={10: 3.0}, signed={10: True})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 3.0
    _patch(monkeypatch, ratios={10: 1.5}, overrides={10: 3.0}, signed={10: False})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 3.0


# ===== 返修点3:签约闸也管生效(未签 → 忽略本人自设) =====

def test_unsigned_l0_markup_ignored_uses_default(monkeypatch):
    # L0(10) 未签 + 自设 1.5(脏/历史值)→ 报价生成忽略·用平台默认 1.0(签约闸也管生效)
    _patch(monkeypatch, ratios={10: 1.5}, signed={10: False})
    assert qpp.get_quote_markup_for_quote_viewer(10) == 1.0


def test_unsigned_l0_cost_ignored_returns_none(monkeypatch):
    # L0(10) 未签 + 自设成本 80 → 报价生成忽略·返 None 走系统动态(签约闸也管生效)
    _patch(monkeypatch, costs={10: 80.0}, signed={10: False})
    assert qpp.get_cost_per_article_for_quote_viewer(10) is None


def test_agent_no_sign_needed(monkeypatch):
    # 服务商(30·agent_level>=1)未签也能用自己 2.5(服务商豁免·走 v2.3)
    _patch(monkeypatch, levels={30: 1}, ratios={30: 2.5}, signed={30: False})
    assert qpp.get_quote_markup_for_quote_viewer(30) == 2.5


def test_p1_1_quote_viewer_fixed_1_0_decoupled_from_system(monkeypatch):
    # [P1-1] 后台 system quote_markup_ratio=2.0,但报价中心默认固定 1.0(解耦)·调用方传实际值 1.0 防引擎回退后台默认
    _patch(monkeypatch, signed={10: True})  # 已签 L0 · 无自设
    monkeypatch.setattr(qpp, "get_system_quote_markup_ratio", lambda: 2.0)  # 后台设 2.0
    v = qpp.get_quote_markup_for_quote_viewer(10)
    assert v == 1.0       # 报价中心固定 1.0 · 不随后台 2.0 漂移
    assert v is not None  # 报价生成传实际值(含1.0)· 不传 None 致引擎回退


def test_p1_2_admin_unsigned_self_pricing_used(monkeypatch):
    # [P1-2] admin(agent_level=0 · 未签)自设系数 1.5 / 成本 200 → 报价生成用 admin 自己的(admin 豁免)
    _patch(monkeypatch, ratios={5: 1.5}, costs={5: 200.0}, signed={5: False}, admins={5})
    assert qpp._can_use_self_pricing(5) is True
    assert qpp.get_quote_markup_for_quote_viewer(5) == 1.5
    assert qpp.get_cost_per_article_for_quote_viewer(5) == 200.0


def test_p1_2_non_admin_unsigned_still_ignored(monkeypatch):
    # 反向:非 admin 未签 L0 自设 → 仍忽略(admin 豁免不波及普通用户)
    _patch(monkeypatch, ratios={6: 1.5}, costs={6: 200.0}, signed={6: False}, admins=set())
    assert qpp._can_use_self_pricing(6) is False
    assert qpp.get_quote_markup_for_quote_viewer(6) == 1.0
    assert qpp.get_cost_per_article_for_quote_viewer(6) is None


def test_override_for_quote_viewer_self_vs_default(monkeypatch):
    # 已签自设 != 平台默认1.0 → 返 override;= 1.0 → None
    _patch(monkeypatch, ratios={10: 1.5}, signed={10: True})
    assert qpp.get_quote_markup_override_for_quote_viewer(10) == 1.5
    _patch(monkeypatch, ratios={11: 1.0}, signed={11: True})
    assert qpp.get_quote_markup_override_for_quote_viewer(11) is None


def test_split_old_viewer_inherits_new_quote_viewer_self(monkeypatch):
    # [分流证明] 旧 get_quote_markup_for_viewer(dormant)仍继承上级 2.0;新 quote viewer(已签)用本人 1.5
    _patch(monkeypatch, levels={10: 0, 99: 1}, owners={10: 99}, ratios={10: 1.5, 99: 2.0}, signed={10: True})
    assert qpp.get_quote_markup_for_viewer(10) == 2.0
    assert qpp.get_quote_markup_for_quote_viewer(10) == 1.5


# ===== flag helper / None 安全(保留) =====

def test_flag_helper_defaults_off_when_unset(monkeypatch):
    monkeypatch.setattr("config.v3_3_1_flags.get_flag", lambda key: None)
    assert qpp.is_l0_quote_markup_self_edit_enabled() is False
    monkeypatch.setattr("config.v3_3_1_flags.get_flag", lambda key: True)
    assert qpp.is_l0_quote_markup_self_edit_enabled() is True


def test_flag_helper_only_accepts_true_not_truthy_string(monkeypatch):
    monkeypatch.setattr("config.v3_3_1_flags.get_flag", lambda key: "false")
    assert qpp.is_l0_quote_markup_self_edit_enabled() is False
    monkeypatch.setattr("config.v3_3_1_flags.get_flag", lambda key: "true")
    assert qpp.is_l0_quote_markup_self_edit_enabled() is False


def test_null_user_id_safe():
    assert qpp.get_user_quote_markup_ratio(None) is None
    assert qpp.resolve_quote_pricing_user_id(None) is None
    assert qpp._can_use_self_pricing(None) is False
