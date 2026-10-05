"""报价 v2.3 DELTA 补强 · 单元 + 集成测试(2026-06-14)。

覆盖 5 DELTA(在已部署阶段2基础上补增量 · flags 全关 0 生产报价变化):
  DELTA 1/5  media_cost_ssot:source_channel + resolve_snapshot_source 探针 + assert_db_active_snapshot gate
             (不改 load 默认 bootstrap 回落 · fail-closed 只在验收 gate)
  DELTA 2    factory_ratio config 漂移修复:guard_national_unclamped_factory_ratio 被 compute_blowup_guards
             真正消费 · p0c_on gate 防误伤 P0-A 成本回真
  DELTA 3    ratio 观测字段:compute_blowup_guards ratios + _assemble_result 落 cost/value/comp/factory_ratio
             + 客户端脱敏黑名单
  (DELTA 4 影子脚本 scripts/bulk_eval_pricing_v23_shadow.py 自带 §12.4 行为断言)

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_pricing_v23_delta_2026_06_14.py -q
"""
import os
import re
import sys
import textwrap
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import tools.pricing_bands as B            # noqa: E402
import tools.pricing_llm_assessor as A     # noqa: E402
import tools.llm_pricing_flag as F         # noqa: E402
import tools.media_cost_ssot as M          # noqa: E402


# ============================================================
# DELTA 1 / 5:snapshot 来源探针 + 验收 gate(FakeConn · 不连真库)
# ============================================================

class _FakeCur:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCur(self._row)

    def close(self):
        pass


_DB_PAYLOAD = {
    "snapshot_version": "mcs_v1_2026-06-14_deadbeef", "payload_sha256": "deadbeef" * 8,
    "generated_at": "2026-06-14T00:00:00+08:00", "geo_count": 420, "schema_version": "mcs_v1",
}


def test_resolve_source_db_when_active_row():
    info = M.resolve_snapshot_source(conn=_FakeConn({"payload": _DB_PAYLOAD}))
    assert info["source"] == "db" and info["has_db_active_row"] is True
    assert info["snapshot_version"] == "mcs_v1_2026-06-14_deadbeef"
    assert info["geo_count"] == 420


def test_resolve_source_bootstrap_when_no_db_row():
    # DB 无 active row → 回落 committed bootstrap JSON(真实存在)· source=bootstrap(P0-A 验收不合格态)
    info = M.resolve_snapshot_source(conn=_FakeConn(None))
    assert info["source"] == "bootstrap" and info["has_db_active_row"] is False
    assert info["snapshot_version"]  # bootstrap 有内容寻址版本


def test_resolve_source_none_when_neither():
    info = M.resolve_snapshot_source(conn=_FakeConn(None), json_path="/no/such/snapshot.json")
    assert info["source"] == "none"


def test_assert_db_active_raises_on_bootstrap():
    # bootstrap fallback 不能当已写库 → fail-closed(阻止签 verified)
    with pytest.raises(M.MediaCostMappingError):
        M.assert_db_active_snapshot(conn=_FakeConn(None))


_NOW = datetime.fromisoformat("2026-06-14T01:00:00+08:00")  # 与 _DB_PAYLOAD.generated_at 同期(确定性)


def test_assert_db_active_ok_on_db():
    info = M.assert_db_active_snapshot(conn=_FakeConn({"payload": _DB_PAYLOAD}), now=_NOW)
    assert info["source"] == "db"


def test_assert_db_rejects_stale():
    # [Codex#2 返修] DB active row 但 generated_at 超 30 天 → fail-closed(防过期快照定价)
    stale = dict(_DB_PAYLOAD, generated_at="2026-04-01T00:00:00+08:00")
    with pytest.raises(M.MediaCostMappingError):
        M.assert_db_active_snapshot(conn=_FakeConn({"payload": stale}), now=_NOW)


def test_assert_db_rejects_old_schema():
    # [Codex#2 返修] schema_version != mcs_v1 → fail-closed(旧 schema active row 不可用于 P0-A)
    oldsc = dict(_DB_PAYLOAD, schema_version="mcs_v0")
    with pytest.raises(M.MediaCostMappingError):
        M.assert_db_active_snapshot(conn=_FakeConn({"payload": oldsc}), now=_NOW)


def test_assert_db_rejects_missing_sha():
    # [Codex#2 返修] payload_sha256 缺失 → fail-closed(内容寻址不完整)
    nosha = dict(_DB_PAYLOAD); nosha["payload_sha256"] = ""
    with pytest.raises(M.MediaCostMappingError):
        M.assert_db_active_snapshot(conn=_FakeConn({"payload": nosha}), now=_NOW)


def test_assert_db_expected_version_mismatch():
    # [Codex#2 返修] Deploy 对账:expected_snapshot_version 不匹配 → fail-closed
    with pytest.raises(M.MediaCostMappingError):
        M.assert_db_active_snapshot(conn=_FakeConn({"payload": _DB_PAYLOAD}), now=_NOW,
                                    expected_snapshot_version="mcs_v1_2026-06-14_OTHER")


def test_assert_db_expected_match_ok():
    info = M.assert_db_active_snapshot(conn=_FakeConn({"payload": _DB_PAYLOAD}), now=_NOW,
                                       expected_snapshot_version="mcs_v1_2026-06-14_deadbeef",
                                       expected_payload_sha256="deadbeef" * 8)
    assert info["source"] == "db"


def test_load_default_bootstrap_fallback_unchanged():
    # 【不改 load 默认行为】DB 不可用 → 仍回落 bootstrap 绝不抛 · 仅多记 source_channel
    snap = M.load_media_cost_snapshot(conn=_FakeConn(None))
    assert snap.source_channel == "bootstrap"
    snap2 = M.load_media_cost_snapshot(conn=_FakeConn({"payload": _DB_PAYLOAD}))
    assert snap2.source_channel == "db"


# ============================================================
# DELTA 2 / 3:compute_blowup_guards baseline + ratio + config 消费
# ============================================================

CFG = {"factory_ceiling_niche": 6500.0, "factory_ceiling_local": 3500.0,
       "entry_selling_ceiling_niche": 15000.0, "entry_selling_ceiling_local": 10000.0,
       "no_cache_selling": 10000.0,
       "lever_two": 1.5, "lever_three": 1.3, "national_unclamped_factory_ratio": 2.0}


def _t(factory, selling=0):
    return {"flagship": {"factory_price": factory, "selling_price": selling}}


def _bl(cost, vm, comp, factory):
    return {"cost": cost, "value_mult": vm, "true_competition": comp, "factory_price": factory}


def test_no_baseline_legacy_no_ratios():
    # 无 baseline → 现状布尔逻辑(向后兼容)· 不产 ratios key
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=True, p0b_relaxed=False,
                                p0c_on=False, cfg=CFG)
    assert "ratios" not in r and r["needs_review"] is False


def test_baseline_cost_alone_no_lever_trigger():
    # P0-A 成本单独纠偏(cost_ratio 高)· value/comp=1 → 单杠杆 → 不触发 lever(§10.2 cost 单独不淹没)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False,
                                cfg=CFG, cost=225.0, true_competition=30, baseline=_bl(55.0, 1.0, 30, 800.0))
    assert r["ratios"]["cost_ratio"] > 1.5 and r["ratios"]["value_ratio"] == 1.0
    assert "lever_two_compound" not in r["guards"] and r["needs_review"] is False


def test_baseline_two_levers_review():
    # cost_ratio 2.0 + value_ratio 1.67 两杠杆 >1.5 → needs_review
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.75,
                                national_unclamped=False, p0a_on=True, p0b_relaxed=True, p0c_on=False,
                                cfg=CFG, cost=110.0, true_competition=30, baseline=_bl(55.0, 1.05, 30, 1000.0))
    assert r["needs_review"] is True and "lever_two_compound" in r["guards"]


def test_baseline_three_levers_review_without_hiding_affordable_entry_quote():
    # 三杠杆都 >1.3(1.45/1.36/1.43)→ needs_review,但入门价未超阈值时不剥保证价
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_niche", value_mult=1.5,
                                national_unclamped=True, p0a_on=True, p0b_relaxed=True, p0c_on=False,
                                cfg=CFG, cost=80.0, true_competition=40, baseline=_bl(55.0, 1.1, 28, 1200.0))
    assert r["needs_review"] is True and r["guarantee_unavailable"] is False
    assert "lever_three_compound" in r["guards"]


def test_delta2_national_factory_ratio_consumed_when_p0c_on():
    # DELTA 2:p0c_on + national_unclamped + factory_ratio>2 → 专项 review(消费此前闲置 config)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_head", value_mult=1.0,
                                national_unclamped=True, p0a_on=True, p0b_relaxed=False, p0c_on=True,
                                cfg=CFG, cost=200.0, true_competition=40, baseline=_bl(90.0, 1.0, 40, 800.0))
    assert "national_unclamped_factory_ratio" in r["guards"]
    assert r["guards"]["national_unclamped_factory_ratio"]["factory_ratio"] == 2.5
    assert r["needs_review"] is True


def test_delta2_national_factory_ratio_not_misfire_p0c_off():
    # 关键:p0c OFF(仅 P0-A)+ national_unclamped + factory_ratio>2 → 不触发(不误伤 P0-A 成本回真·§9.1)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_head", value_mult=1.0,
                                national_unclamped=True, p0a_on=True, p0b_relaxed=False, p0c_on=False,
                                cfg=CFG, cost=200.0, true_competition=40, baseline=_bl(90.0, 1.0, 40, 800.0))
    assert "national_unclamped_factory_ratio" not in r["guards"]


def test_baseline_divzero_guard():
    # flag-off 基线为 0 的极端词 → ratio=None 不崩(防 ZeroDivisionError)
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False,
                                cfg=CFG, cost=225.0, true_competition=30, baseline=_bl(0.0, 1.0, 30, 0.0))
    assert r["ratios"]["cost_ratio"] is None and r["ratios"]["factory_ratio"] is None


def test_p0c_national_review_without_hiding_affordable_entry_quote():
    # [2026-06-16 护栏收紧] P0-C national_unclamped 仍 needs_review,但不因全国词本身剥保证价。
    # 「参考价·待人工核」只在入门价也超阈值时出现。
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_niche", value_mult=1.0,
                                national_unclamped=True, p0a_on=False, p0b_relaxed=False, p0c_on=True, cfg=CFG)
    assert r["guarantee_unavailable"] is False and r["needs_review"] is True
    assert "national_unclamped_review" in r["guards"]
    assert "national_unclamped_no_guarantee" not in r["guards"]


def test_guard_all_flags_off_inert_unchanged():
    # flags 全关 = 现状 inert(即便传 baseline 也早返回 0 变化)
    r = B.compute_blowup_guards(tiers=_t(99999, 99999), keyword_type="national_niche", value_mult=1.75,
                                national_unclamped=True, p0a_on=False, p0b_relaxed=False, p0c_on=False,
                                cfg=CFG, cost=999.0, true_competition=99, baseline=_bl(55.0, 1.0, 30, 800.0))
    assert r == {"needs_review": False, "guarantee_unavailable": False, "no_cache": False, "guards": {}}


# ============================================================
# DELTA 3:_assemble_result 落 ratio 字段(flags-off 恒 None)
# ============================================================

LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}


def _ctx_no5118(**kw):
    base = dict(keyword="深圳哪家好", measured_comp=12, saturated=False,
                metaso={"competition_count": 12, "content_count": 5}, cost_multiplier=1.0,
                dynamic_cost=55.0, five118={}, metaso_fallback=False)
    base.update(kw)
    return base


def _judged_hi(**kw):
    base = dict(true_competition=12, cost_per_article_suggested=55.0, keyword_type="local_city",
                city="深圳", city_tier="tier1", media_tier_required="B", value_signal=3.0,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _flags(mp, cost=False, value=False, nat=False):
    mp.setattr(F, "is_cost_snapshot_enabled", lambda: cost)
    mp.setattr(F, "is_value_evidence_gate_relaxed", lambda: value)
    mp.setattr(F, "is_national_unclamped_enabled", lambda: nat)


def test_assemble_flags_off_ratios_none(monkeypatch):
    # flags 全关 → ratio 字段恒 None(0 变化:不算基线)
    _flags(monkeypatch)
    r = A._assemble_result(_ctx_no5118(), _judged_hi(), LLM_META, {}, 2.0, None)
    assert r["cost_ratio"] is None and r["value_ratio"] is None
    assert r["comp_ratio"] is None and r["factory_ratio"] is None


def test_assemble_p0b_value_ratio(monkeypatch):
    # P0-B 开 · 无 5118 → flag-off 钳 vm=1.25 · flag-on vm=价值上限 → value_ratio>1 · comp_ratio=1(竞争 native)
    _flags(monkeypatch, value=True)
    r = A._assemble_result(_ctx_no5118(), _judged_hi(), LLM_META, {}, 2.0, None)
    assert r["value_multiplier"] == 1.5
    assert r["value_ratio"] is not None and r["value_ratio"] > 1.0
    assert r["comp_ratio"] == 1.0


def test_assemble_override_cost_ratio_one(monkeypatch):
    # P0-A 开但 cost_override → 成本绝对优先 → cost_ratio=1(flag 不动自设成本)
    _flags(monkeypatch, cost=True)
    r = A._assemble_result(_ctx_no5118(), _judged_hi(), LLM_META, {}, 2.0, 120.0)
    assert r["cost_per_article"] == 120.0 and r["cost_ratio"] == 1.0


def test_assemble_p0a_cost_ratio_computed(monkeypatch):
    # P0-A 开 · 无 override → 快照成本(bootstrap 回落)· cost_ratio 被算出(成本回真观测)
    _flags(monkeypatch, cost=True)
    r = A._assemble_result(_ctx_no5118(), _judged_hi(media_tier_required="B"), LLM_META, {}, 2.0, None)
    assert r["cost_ratio"] is not None


# ============================================================
# DELTA 3:客户端脱敏黑名单含 4 个 ratio 字段(regex 提取 · 不 import selection_api 连库)
# ============================================================

def _load_blacklist():
    sel = open(os.path.join(ROOT, "api", "selection_api.py"), encoding="utf-8").read()
    mt = re.search(r"(_CUSTOMER_INTERNAL_KW_FIELDS\s*=\s*\([\s\S]*?\n\))", sel)
    assert mt, "未定位 _CUSTOMER_INTERNAL_KW_FIELDS"
    ns: dict = {}
    exec(textwrap.dedent(mt.group(1)), ns)
    return ns["_CUSTOMER_INTERNAL_KW_FIELDS"]


def test_ratio_fields_in_customer_strip_blacklist():
    bl = _load_blacklist()
    for fld in ("cost_ratio", "value_ratio", "comp_ratio", "factory_ratio"):
        assert fld in bl, f"{fld} 未加入客户端脱敏黑名单(§10.3 内部杠杆数禁泄露)"
    # 回归:既有脱敏不回退
    assert "guarantee_unavailable" in bl and "cost_per_article" in bl
    # super_red_ocean / should_quote 有意保留给客户 · 不应被误加
    assert "super_red_ocean" not in bl and "should_quote" not in bl


def test_scorer_wires_ratios_into_records():
    # [Codex#1 返修] ratio 必须进正常报价记录(v2_assessor_data 复盘 + scored 行透传)· 不止影子脚本
    src = open(os.path.join(ROOT, "tools", "keyword_value_scorer.py"), encoding="utf-8").read()
    assert '"ratios"' in src, "v2_assessor_data 未写 ratios(复盘记录漏 ratio)"
    for fld in ("cost_ratio", "value_ratio", "comp_ratio", "factory_ratio"):
        assert f'a.get("{fld}")' in src, f"scored 行未从 assessor 透传 {fld}"
