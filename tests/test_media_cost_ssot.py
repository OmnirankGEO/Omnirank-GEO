"""
P0-0 媒体成本 SSOT 单测(2026-06-13)

覆盖六个修正:
  1. price==price2 仅作一致性检查(verify),不作 sivp 语义证明 → T1/T-doc
  2. 三方(Excel/API/DB)映射:anchor drift fail-closed → T1_anchor
  3. mhz_wemedia 覆盖显式(不进 GEO 成本档) → T8
  4. 档位分类不使用 price(无 price→质量→price 循环) → T9
  5. media_cost_snapshot 禁同版本静默覆盖(内容寻址版本) → T7
  6. stale snapshot 未来参与报价必 fail-closed/needs_review → T5

纯函数 / bootstrap JSON · 不连真 DB。
跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=postgresql://x:x@localhost:5432/placeholder_test \
       PYTHONIOENCODING=utf-8 python -m pytest tests/test_media_cost_ssot.py -q
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.media_cost_ssot import (  # noqa: E402
    MediaCostMappingError,
    MediaCostSnapshot,
    build_snapshot_payload,
    classify_media_tier,
    geo_engine_coverage,
    verify_field_mapping,
    verify_field_mapping_db,
    compare_to_bootstrap,
    load_excel_anchors,
    finalize_snapshot,
    _payload_sha256,
    _DEFAULT_ANCHORS,
    COST_SNAPSHOT_SCHEMA_VERSION,
    _BOOTSTRAP_PATH,
)

FIXED = datetime(2026, 6, 13, 0, 0, 0)


def _rows(prices_by_tier):
    """{(authority, platform): [prices]} → geo_rows list."""
    rows = []
    for (auth, platform), prices in prices_by_tier.items():
        for p in prices:
            rows.append({"price": p, "authority_media": auth, "geo_rank_platform": platform})
    return rows


# ---------------------------------------------------------------- T1: verify fail-closed
def test_verify_ok():
    rep = verify_field_mapping(
        price_eq_price2_ratio=1.0, geo_count=412, zero_price_count=0,
        anchors=[{"case_link": "x", "db_price": 200.0, "db_price1": 210.0,
                  "expect_sivp": 200.0, "expect_vip": 210.0}],
    )
    assert rep["verified"] is True and rep["anchors_checked"] == 1


def test_verify_consistency_drift_raises():
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping(price_eq_price2_ratio=0.90, geo_count=412,
                             zero_price_count=0, anchors=[])


def test_verify_anchor_mismatch_raises():
    # price 不再等于 Excel sivp → 列映射漂移 → fail-closed
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping(
            price_eq_price2_ratio=1.0, geo_count=412, zero_price_count=0,
            anchors=[{"case_link": "x", "db_price": 230.0, "db_price1": 210.0,
                      "expect_sivp": 200.0, "expect_vip": 210.0}],
        )


def test_verify_geo_count_out_of_band_raises():
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping(price_eq_price2_ratio=1.0, geo_count=10,
                             zero_price_count=0, anchors=[])


def test_verify_all_anchors_missing_raises():
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping(
            price_eq_price2_ratio=1.0, geo_count=412, zero_price_count=0,
            anchors=[{"case_link": "x", "db_price": None, "db_price1": None,
                      "expect_sivp": 200.0, "expect_vip": 210.0}],
        )


# ---------------------------------------------------------------- T2: snapshot sanity
def test_build_snapshot_monotonic_and_meta():
    rows = _rows({(1, "a,b,c,d,e,f"): [float(x) for x in range(10, 210, 10)]})  # 20 高覆盖权威
    payload = build_snapshot_payload(
        geo_rows=rows, platform_media_markup=2.0,
        mapping_report={"verified": True}, generated_at=FIXED,
        source="test", wemedia_geo_rows=[{"price": 38}], row_count_total=99, active_count=99,
    )
    ov = payload["overall"]
    assert ov["n"] == 20
    assert ov["p50"] <= ov["p75"] <= ov["p90"] <= ov["p99"] <= ov["max"]
    assert payload["schema_version"] == COST_SNAPSHOT_SCHEMA_VERSION
    assert payload["price_column"] == "price"
    assert "外采价" in payload["price_semantics"] and "sivp" in payload["price_semantics"]
    assert payload["payload_sha256"] and payload["snapshot_version"].startswith("mcs_v1_2026-06-13_")


# ---------------------------------------------------------------- T3: unknown/low-sample → needs_review, never low
def test_real_cost_unknown_tier_needs_review_not_low():
    rows = _rows({(1, "a,b,c,d,e,f"): [100.0] * 12})  # 仅 auth1_high 有样本
    snap = MediaCostSnapshot.from_payload(
        build_snapshot_payload(geo_rows=rows, platform_media_markup=2.0,
                               mapping_report={"verified": True}, generated_at=FIXED, source="t"))
    # auth0_high 无样本 → 回退 bucket/overall,needs_review,cost>0(绝不 0/min)
    r = snap.real_cost_for(authority=0, coverage_bucket="high", percentile="p90", now=FIXED)
    assert r["needs_review"] is True
    assert r["cost"] is not None and r["cost"] > 0


def test_real_cost_empty_snapshot_no_data():
    snap = MediaCostSnapshot.from_payload(
        build_snapshot_payload(geo_rows=[], platform_media_markup=2.0,
                               mapping_report={"verified": True}, generated_at=FIXED, source="t"))
    r = snap.real_cost_for(authority=1, coverage_bucket="high", now=FIXED)
    assert r["confidence"] == "no_data" and r["needs_review"] is True and r["cost"] is None


# ---------------------------------------------------------------- T4: zero/NULL guard
def test_zero_price_excluded_never_yields_zero():
    rows = _rows({(0, "a,b"): [0.0, -5.0, 20.0, 30.0]})
    rows.append({"price": None, "authority_media": 0, "geo_rank_platform": "a"})
    payload = build_snapshot_payload(geo_rows=rows, platform_media_markup=2.0,
                                     mapping_report={"verified": True}, generated_at=FIXED, source="t")
    assert payload["excluded_nonpositive"] == 3   # 0, -5, None
    assert payload["geo_count"] == 2
    assert payload["overall"]["min"] == 20.0


# ---------------------------------------------------------------- T5: stale → needs_review
def test_stale_by_schema_version():
    snap = MediaCostSnapshot.from_payload({"schema_version": "old_v0", "generated_at": FIXED.isoformat()})
    assert snap.is_stale(now=FIXED) is True


def test_stale_by_age_forces_needs_review():
    rows = _rows({(1, "a,b,c,d,e,f"): [100.0] * 12})
    payload = build_snapshot_payload(geo_rows=rows, platform_media_markup=2.0,
                                     mapping_report={"verified": True}, generated_at=FIXED, source="t")
    snap = MediaCostSnapshot.from_payload(payload)
    future = FIXED + timedelta(days=60)
    assert snap.is_stale(now=future) is True
    r = snap.real_cost_for(authority=1, coverage_bucket="high", percentile="p90", now=future)
    assert r["needs_review"] is True and r["confidence"] == "stale"


# ---------------------------------------------------------------- T7: 内容寻址版本(禁同版本静默覆盖)
def test_version_is_content_addressed():
    base = {(1, "a,b,c,d,e,f"): [100.0, 120.0, 150.0]}
    p1 = build_snapshot_payload(geo_rows=_rows(base), platform_media_markup=2.0,
                                mapping_report={"verified": True}, generated_at=FIXED, source="t")
    p2 = build_snapshot_payload(geo_rows=_rows(base), platform_media_markup=2.0,
                                mapping_report={"verified": True}, generated_at=FIXED, source="t")
    assert p1["snapshot_version"] == p2["snapshot_version"]   # 同内容 → 同版本(写入幂等)
    changed = dict(base); changed[(1, "a,b,c,d,e,f")] = [100.0, 120.0, 999.0]
    p3 = build_snapshot_payload(geo_rows=_rows(changed), platform_media_markup=2.0,
                                mapping_report={"verified": True}, generated_at=FIXED, source="t")
    assert p3["snapshot_version"] != p1["snapshot_version"]   # 不同内容 → 不同版本(append-only)


# ---------------------------------------------------------------- T8: wemedia 覆盖显式(不进 GEO 档)
def test_wemedia_explicit_not_in_geo_tiers():
    mhz = _rows({(1, "a,b,c,d,e,f"): [100.0, 120.0]})
    payload = build_snapshot_payload(geo_rows=mhz, platform_media_markup=2.0,
                                     mapping_report={"verified": True}, generated_at=FIXED, source="t",
                                     wemedia_geo_rows=[{"price": 38}, {"price": 75}, {"price": 600}])
    assert payload["geo_count"] == 2                            # 只数 mhz · 不含 wemedia
    assert "informational" in payload["wemedia_geo"]["note"].lower()
    assert payload["wemedia_geo"]["dist"]["n"] == 3
    # GEO 成本档 n 总和 == geo_count(wemedia 未混入)
    auth_sum = sum(payload["by_tier"][k].get("n", 0) for k in ("auth0", "auth1"))
    assert auth_sum == payload["geo_count"]


# ---------------------------------------------------------------- T9: 分类不使用 price
def test_classify_uses_no_price():
    import inspect
    params = inspect.signature(classify_media_tier).parameters
    assert "price" not in params                                # 签名无 price → 不可能用 price 推档
    t = classify_media_tier(authority_media=1, geo_rank_platform="a,b,c,d,e,f")
    assert t["coverage"] == 6 and t["coverage_bucket"] == "high" and t["tier_key"] == "auth1_high"
    assert geo_engine_coverage("a,e,z") == 2                    # z 不计


def test_classify_low_coverage():
    t = classify_media_tier(authority_media=0, geo_rank_platform="a")
    assert t["coverage"] == 1 and t["coverage_bucket"] == "low" and t["tier_key"] == "auth0_low"


# ---------------------------------------------------------------- 修正#1: bootstrap 真实内容寻址(非占位)
def test_bootstrap_content_addressed_self_consistent():
    with open(_BOOTSTRAP_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["payload_sha256"] not in ("", "bootstrap", None)
    assert payload["payload_sha256"] == _payload_sha256(payload)       # 真实 hash 自洽
    assert payload["snapshot_version"].endswith(payload["payload_sha256"][:8])
    assert payload["snapshot_version"].startswith(f"{COST_SNAPSHOT_SCHEMA_VERSION}_")


def test_finalize_is_shared_and_deterministic():
    payload = {"generated_at": "2026-06-13T00:00:00+08:00", "overall": {"p50": 30}}
    finalize_snapshot(payload)
    assert payload["payload_sha256"] == _payload_sha256(payload)
    assert payload["snapshot_version"] == f"{COST_SNAPSHOT_SCHEMA_VERSION}_2026-06-13_{payload['payload_sha256'][:8]}"


# ---------------------------------------------------------------- 修正#2: compare_to_bootstrap(--strict 的底座)
def test_compare_bootstrap_match_and_mismatch():
    rows = _rows({(1, "a,b,c,d,e,f"): [float(x) for x in range(10, 210, 10)]})
    cand = build_snapshot_payload(geo_rows=rows, platform_media_markup=2.0,
                                  mapping_report={"verified": True}, generated_at=FIXED, source="t")
    same = compare_to_bootstrap(cand, cand)
    assert same["ok"] is True and same["diffs"] == []
    drift = json.loads(json.dumps(cand))
    drift["overall"]["p90"] = cand["overall"]["p90"] * 2     # 翻倍 → 超容差
    cmp = compare_to_bootstrap(cand, drift, tolerance=0.01)
    assert cmp["ok"] is False
    assert any(d["field"] == "overall.p90" for d in cmp["diffs"])


# ---------------------------------------------------------------- 修正#3: 三方核验自动化(Excel 真值 + DB cross-check)
def _make_excel(path, rows):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["频道类型", "地区", "媒体名称", "案例链接", "普通会员", "vip", "sivp", "可发GEO"])
    for link, putong, vip, sivp, geo in rows:
        ws.append(["新闻", "全国", "媒体", link, putong, vip, sivp, geo])
    wb.save(path)


def test_load_excel_anchors(tmp_path):
    xlsx = str(tmp_path / "m.xlsx")
    _make_excel(xlsx, [
        ("http://ex.com/a/1", 30, 25, 20, 1),   # GEO=1 → 锚点(expect_sivp=20, expect_vip=25)
        ("http://ex.com/a/2?q=1", 40, 35, 30, 1),  # 含 ? → 跳过(不稳定)
        ("http://ex.com/b/9", 99, 95, 90, 0),   # GEO=0 → 跳过
    ])
    anchors = load_excel_anchors(xlsx)
    assert len(anchors) == 1
    a = anchors[0]
    assert a["case_link"] == "ex.com/a/1" and a["expect_sivp"] == 20.0 and a["expect_vip"] == 25.0


class _FakeCur:
    def __init__(self, price_map, counts):
        self.price_map, self.counts = price_map, counts
    def execute(self, sql, params=None):
        self._sql, self._params = sql, params
    def fetchone(self):
        if "FILTER (WHERE price=price2)" in self._sql:
            return self.counts
        if "case_link LIKE" in self._sql:
            sub = self._params[0].strip("%")
            for k, (pr, p1) in self.price_map.items():
                if k in sub or sub in k:
                    return {"price": pr, "price1": p1}
            return None
        return None


class _FakeConn:
    def __init__(self, price_map, counts): self._c = _FakeCur(price_map, counts)
    def cursor(self): return self._c
    def close(self): pass


def test_verify_db_excel_path_match(tmp_path):
    xlsx = str(tmp_path / "m.xlsx")
    _make_excel(xlsx, [("http://ex.com/a/1", 30, 25, 20, 1)])
    conn = _FakeConn({"ex.com/a/1": (20.0, 25.0)}, {"n": 412, "eq": 412, "zero": 0})
    rep = verify_field_mapping_db(conn=conn, excel_path=xlsx)
    assert rep["verified"] is True and rep["anchors_checked"] == 1


def test_verify_db_excel_path_mismatch_raises(tmp_path):
    xlsx = str(tmp_path / "m.xlsx")
    _make_excel(xlsx, [("http://ex.com/a/1", 30, 25, 20, 1)])
    # DB price=70 != Excel sivp=20 → 列映射漂移 → fail-closed
    conn = _FakeConn({"ex.com/a/1": (70.0, 25.0)}, {"n": 412, "eq": 412, "zero": 0})
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping_db(conn=conn, excel_path=xlsx)


def test_default_anchors_cover_price_bands():
    # 9 个 prod 实证锚点 · 跨 ¥15→¥1000(不是只 2 个)
    assert len(_DEFAULT_ANCHORS) >= 8
    sivps = sorted(a["expect_sivp"] for a in _DEFAULT_ANCHORS)
    assert sivps[0] <= 20 and sivps[-1] >= 600


# ---------------------------------------------------------------- 补修#1: excel_path 0 锚点 fail-closed
def test_verify_db_excel_zero_anchors_fail_closed(tmp_path):
    xlsx = str(tmp_path / "empty.xlsx")
    _make_excel(xlsx, [("http://ex.com/b/9", 99, 95, 90, 0)])   # 全 GEO=0 → 派生 0 锚点
    conn = _FakeConn({}, {"n": 412, "eq": 412, "zero": 0})
    with pytest.raises(MediaCostMappingError):
        verify_field_mapping_db(conn=conn, excel_path=xlsx)


# ---------------------------------------------------------------- 补修#2: compare 检测结构漂移
def _sample_payload():
    rows = _rows({(1, "a,b,c,d,e,f"): [float(x) for x in range(10, 210, 10)],
                  (0, "a"): [10.0, 20.0, 30.0]})
    return build_snapshot_payload(geo_rows=rows, platform_media_markup=2.0,
                                  mapping_report={"verified": True}, generated_at=FIXED, source="t")


def test_compare_detects_missing_tier_key():
    cand = _sample_payload()
    boot = json.loads(json.dumps(cand))
    boot["by_tier"]["auth1_extra"] = {"n": 9, "p50": 1, "p75": 1, "p90": 1, "p95": 1, "p99": 1}
    cmp = compare_to_bootstrap(cand, boot)
    assert cmp["ok"] is False
    assert any(d["field"] == "by_tier.auth1_extra" and d["candidate"] == "MISSING" for d in cmp["diffs"])


def test_compare_detects_extra_tier_key():
    cand = _sample_payload()
    boot = json.loads(json.dumps(cand))
    cand["by_tier"]["auth0_surprise"] = {"n": 9, "p50": 1, "p75": 1, "p90": 1, "p95": 1, "p99": 1}
    cmp = compare_to_bootstrap(cand, boot)
    assert cmp["ok"] is False
    assert any(d["field"] == "by_tier.auth0_surprise" and d["bootstrap"] == "MISSING" for d in cmp["diffs"])


def test_compare_detects_schema_pricecolumn_and_n():
    cand = _sample_payload()
    boot = json.loads(json.dumps(cand))
    boot["schema_version"] = "mcs_v0_old"
    boot["price_column"] = "price1"
    boot["overall"]["n"] = cand["overall"]["n"] + 100
    cmp = compare_to_bootstrap(cand, boot)
    fields = {d["field"] for d in cmp["diffs"]}
    assert {"schema_version", "price_column", "overall.n"} <= fields
    assert cmp["ok"] is False


def test_compare_identical_is_ok():
    cand = _sample_payload()
    assert compare_to_bootstrap(cand, json.loads(json.dumps(cand)))["ok"] is True


# ================================================================
# T-tierB: P0-A flip 前阻塞修(2026-06-15)
#   needs_review 判据 = "是否命中 selector 首选档(回退链首项)",不再 "conf != exact"。
#   设计性 bucket selector(authority=None·tier-B)命中其首选 bucket + 样本足 + 新鲜 → needs_review=False。
#   但真降级(请求 exact 退到 bucket/overall)/ stale / no_data 仍 needs_review=True。
# ================================================================

def _snap(prices_by_tier):
    return MediaCostSnapshot.from_payload(
        build_snapshot_payload(geo_rows=_rows(prices_by_tier), platform_media_markup=2.0,
                               mapping_report={"verified": True}, generated_at=FIXED, source="t"))


def test_real_cost_tierb_designed_bucket_no_review():
    # tier-B selector authority=None+bucket=low:命中设计性首选 bucket_low(n=10≥8 · 新鲜)→ 不强制人审。
    snap = _snap({(0, "a"): [50.0] * 10})        # 平台 "a" = 覆盖1 = low bucket · auth0_low 10 行
    r = snap.real_cost_for(authority=None, coverage_bucket="low", percentile="p75", now=FIXED)
    assert r["tier_key"] == "bucket_low" and r["confidence"] == "bucket_pooled"
    assert r["cost"] is not None and r["cost"] > 0
    assert r["needs_review"] is False            # ★ 核心修:设计性 bucket 首选命中不再强制 needs_review
    # 经 media_tier_cost("B") 同口径(P0-A 真实成本 seam)
    rb = snap.media_tier_cost("B", now=FIXED)
    assert rb["tier_key"] == "bucket_low" and rb["needs_review"] is False


def test_real_cost_exact_request_fallback_to_bucket_still_review():
    # ★ 关键对照:请求 exact(authority=0)但 auth0_low/auth1_low 各 <8 样本不足 → 退到 bucket_low(idx>0)。
    #   命中的同样是 bucket_low/confidence=bucket_pooled,但因是【降级】(非首选档)→ 仍 needs_review=True。
    #   证明只放行"设计性首选 bucket",不放行"所有 bucket_pooled"。
    snap = _snap({(0, "a"): [50.0] * 4, (1, "a"): [60.0] * 6})   # auth0_low=4 / auth1_low=6(均<8) · bucket_low=10
    r = snap.real_cost_for(authority=0, coverage_bucket="low", percentile="p75", now=FIXED)
    assert r["tier_key"] == "bucket_low" and r["confidence"] == "bucket_pooled"
    assert r["needs_review"] is True             # 请求 exact 退到 bucket = 真降级 → 仍人审


def test_real_cost_tierb_stale_still_review():
    # tier-B 设计性 bucket 命中,但快照陈旧 → 仍 needs_review=True(stale 恒人审)。
    snap = _snap({(0, "a"): [50.0] * 10})
    future = FIXED + timedelta(days=60)
    r = snap.real_cost_for(authority=None, coverage_bucket="low", percentile="p75", now=future)
    assert r["confidence"] == "stale" and r["needs_review"] is True


def test_real_cost_tierb_empty_bucket_falls_to_overall_review():
    # tier-B 的设计性 bucket_low 本身无数据(只有高覆盖行)→ 退到 overall(idx>0)= 真降级 → needs_review=True。
    snap = _snap({(1, "a,b,c,d,e,f"): [100.0] * 10})   # 仅 high 覆盖 · bucket_low 空
    r = snap.real_cost_for(authority=None, coverage_bucket="low", percentile="p75", now=FIXED)
    assert r["tier_key"] == "__overall__" and r["needs_review"] is True


def test_real_cost_tierb_no_data_review():
    # 空快照 → 全链无数据 → no_data + needs_review=True(不变)。
    snap = _snap({})
    r = snap.real_cost_for(authority=None, coverage_bucket="low", percentile="p75", now=FIXED)
    assert r["confidence"] == "no_data" and r["needs_review"] is True and r["cost"] is None
