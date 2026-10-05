"""包 C④⑥ · manifest 补齐 + health 不再返回笼统 OK。

🔴 **先说清楚哪些是 35/38 班已经做的,本包没重做**(Review 指令 1):
   · release 三类 chunk 与 manifest **同一事务切换** → 已做,判据在
     `tests/xiaobang_vnext_2026_08_18/test_release_atomicity_and_recovery_authz_pg.py`③
     (中途注毒必须让旧库一行不少);
   · 重建**状态位 + 失败告警** → 已做,判据在 `test_kb_reindex_status_pg.py`;
   · 部署链「先 seed 同步 → 后重建」+ 两种失败形态各一条告警 → 已做,
     判据在 `test_kb_release_step_pg.py`。
   本包只补它们**没覆盖到的两处缺口**:
     ④ manifest 缺 术语规则版本 / 路由清单 / 构建时间;
     ⑥ health 的 `ok` 是**字面量 True**,分不出 stale / mixed / incomplete。
"""

from __future__ import annotations

import pytest

from services.kb_health_verdict import (
    STATUS_INCOMPLETE,
    STATUS_MIXED,
    STATUS_OK,
    STATUS_STALE,
    current_expectations,
    evaluate,
)


def _good_manifest(**over):
    exp = current_expectations()
    m = {
        "content_version": "v1",
        "release_sha": "deadbeef",
        "operation_registry_version": exp["operation_registry_version"],
        "terminology_rule_id": exp["terminology_rule_id"],
        "terminology_rule_version": exp["terminology_rule_version"],
        "route_manifest_hash": exp["route_manifest_hash"],
        "route_count": exp["route_count"],
        "source_hashes": {"doc": "a", "faq": "b", "preset": "c"},
        "chunk_counts": {"doc": 10, "faq": 5, "preset": 3},
        "built_at": "2026-08-21T00:00:00+00:00",
    }
    m.update(over)
    return m


LIVE_OK = {"doc": 10, "faq": 5, "preset": 3}


# ══════════════════════════════════════════════════════════════════════
# ① 分母自证:expectations 必须是真取来的,不是空壳
# ══════════════════════════════════════════════════════════════════════

def test_expectations_come_from_the_live_single_sources():
    """🔴 三项期望值都必须非空 —— 空的话下面所有对账都是「跟空比」,恒绿。"""
    exp = current_expectations()
    assert exp["terminology_rule_version"], exp
    assert exp["operation_registry_version"], exp
    assert exp["route_manifest_hash"], exp
    assert exp["route_count"] > 100, "路由分母只有 %s 条,取数坏了" % exp["route_count"]


# ══════════════════════════════════════════════════════════════════════
# ② 一致 → ok
# ══════════════════════════════════════════════════════════════════════

def test_a_matching_manifest_is_ok():
    v = evaluate(_good_manifest(), LIVE_OK)
    assert v["status"] == STATUS_OK, v["reasons"]
    assert v["ok"] is True
    assert v["reasons"] == []
    # 版本信息必须被带出来给运维看
    assert v["manifest_version"]["release_sha"] == "deadbeef"
    assert v["manifest_version"]["built_at"] == "2026-08-21T00:00:00+00:00"


# ══════════════════════════════════════════════════════════════════════
# ③ stale —— 三条各自独立判红
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("field,bad", [
    ("terminology_rule_version", "2020-01-01-old"),
    ("operation_registry_version", "operation-registry-v1"),
    ("route_manifest_hash", "0" * 64),
])
def test_each_version_drift_is_reported_as_stale_with_both_values(field, bad):
    """🔴 三项**各自**能判红,且 reason 里说得出谁跟谁不一致。

    只断言「status != ok」挡不住「三条里只有一条真在判」——
    另外两条整个删掉,判据照样绿(本仓记过这种「被别的规则顺手判红」)。
    """
    v = evaluate(_good_manifest(**{field: bad}), LIVE_OK)
    assert v["status"] == STATUS_STALE, v
    assert v["ok"] is False
    assert v["reasons"], v
    joined = " ".join(v["reasons"])
    if field == "route_manifest_hash":
        assert "路由清单变了" in joined, joined
    else:
        assert field in joined, joined


def test_a_missing_manifest_field_is_stale_not_ok():
    """老 manifest(本包之前建的)没有新字段 ⇒ stale,**不是** ok。

    这条同时是本包的**升级路径**判据:线上现存 manifest 就是缺这三样的,
    部署后 health 会如实报 stale,直到跑过一次重建 —— 那正是我们要的行为。
    """
    m = _good_manifest()
    del m["terminology_rule_version"]
    v = evaluate(m, LIVE_OK)
    assert v["status"] == STATUS_STALE
    assert any("缺字段" in r for r in v["reasons"]), v["reasons"]


# ══════════════════════════════════════════════════════════════════════
# ④ mixed —— manifest 说的和库里实际对不上
# ══════════════════════════════════════════════════════════════════════

def test_count_mismatch_is_mixed_and_names_both_numbers():
    v = evaluate(_good_manifest(), {"doc": 10, "faq": 4, "preset": 3})
    assert v["status"] == STATUS_MIXED, v
    assert v["ok"] is False
    joined = " ".join(v["reasons"])
    assert "faq" in joined and "5" in joined and "4" in joined, joined


def test_mixed_outranks_stale():
    """两种问题同时存在时报更重的那一个(mixed 说明有人绕过了 release)。"""
    v = evaluate(_good_manifest(terminology_rule_version="old"),
                 {"doc": 1, "faq": 5, "preset": 3})
    assert v["status"] == STATUS_MIXED
    assert len(v["reasons"]) >= 2, v["reasons"]


# ══════════════════════════════════════════════════════════════════════
# ⑤ incomplete
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("empty", [None, {}])
def test_no_manifest_is_incomplete_not_ok(empty):
    v = evaluate(empty, LIVE_OK)
    assert v["status"] == STATUS_INCOMPLETE
    assert v["ok"] is False
    assert v["manifest_version"] is None


# ══════════════════════════════════════════════════════════════════════
# ⑥ 判据里不许有「今天」
# ══════════════════════════════════════════════════════════════════════

def test_built_at_never_participates_in_the_verdict():
    """🔴 `built_at` 只作记录。

    「多久算 stale」是个写死的 cutoff,而写死 cutoff + 时间流逝 = 定时炸弹
    (同天上午全绿晚上全红,红因与被测代码零因果)。
    这里用**一个远古时间戳**证明它不参与裁定:陈旧与否只由版本对账决定。
    """
    ancient = evaluate(_good_manifest(built_at="1999-01-01T00:00:00+00:00"), LIVE_OK)
    assert ancient["status"] == STATUS_OK, ancient["reasons"]
    future = evaluate(_good_manifest(built_at="2099-01-01T00:00:00+00:00"), LIVE_OK)
    assert future["status"] == STATUS_OK, future["reasons"]
    missing = evaluate(_good_manifest(built_at=None), LIVE_OK)
    assert missing["status"] == STATUS_OK, missing["reasons"]


def test_the_verdict_module_does_not_read_the_clock():
    """结构锚:裁定模块里不许出现取当前时间的调用。"""
    import io
    import pathlib
    from services import kb_health_verdict as mod
    src = io.open(pathlib.Path(mod.__file__), encoding="utf-8").read()
    for bad in ("datetime.now", "utcnow", "time.time", "date.today"):
        assert bad not in src, "裁定模块读了钟表:%s" % bad


# ══════════════════════════════════════════════════════════════════════
# ⑦ manifest 生产端真的写了那三样(接线,不是只验裁定函数认不认)
# ══════════════════════════════════════════════════════════════════════

def test_the_builder_really_writes_the_three_new_fields(monkeypatch):
    """🔴 接线锁:`rebuild_release` 组装出来的 manifest 必须含新三项。

    只验 `evaluate()` 认不认这些字段,证明不了**有人写过它们** ——
    把 `rebuild_release` 里那段删掉,上面所有裁定判据一条都不会红。
    """
    import tools.xiaobang_kb_indexer as idx

    monkeypatch.setattr(idx, "build_doc_chunks", lambda: [{"a": 1}])
    monkeypatch.setattr(idx, "build_faq_chunks", lambda: [])
    monkeypatch.setattr(idx, "build_preset_chunks", lambda: [])
    captured = {}

    def _fake_replace(*, source_types, chunks, manifest):
        captured["manifest"] = manifest
        return {"inserted": len(chunks)}

    monkeypatch.setattr(idx, "replace_chunks_transactionally", _fake_replace)

    out = idx.rebuild_release("sha-under-test")
    m = out["manifest"]
    assert captured["manifest"] is m, "写进事务的 manifest 必须就是返回的那一份"

    exp = current_expectations()
    # 必须命中:三项新字段都在,且值等于当前真值
    assert m["terminology_rule_version"] == exp["terminology_rule_version"]
    assert m["terminology_rule_id"] == exp["terminology_rule_id"]
    assert m["route_manifest_hash"] == exp["route_manifest_hash"]
    assert m["route_count"] == exp["route_count"]
    assert m["built_at"], "构建时间必须写"
    # 反向对照:35/38 班原有的四项一个都不许丢
    for legacy in ("content_version", "release_sha",
                   "operation_registry_version", "source_hashes", "chunk_counts"):
        assert legacy in m, legacy

    # 端到端:拿这份刚建出来的 manifest 去裁定,必须是 ok
    v = evaluate(m, m["chunk_counts"])
    assert v["status"] == STATUS_OK, v["reasons"]


# ══════════════════════════════════════════════════════════════════════
# ⑧ 端点接线锁(真 HTTP)
# ══════════════════════════════════════════════════════════════════════

def _health_client(monkeypatch, manifest, live_counts):
    """真 HTTP 打 /api/xiaobang/health。只替库读,裁定与端点编排走真代码。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.xiaobang_api as xb
    import db.kb_db as kb

    monkeypatch.setattr(kb, "load_release_manifest", lambda: manifest)
    monkeypatch.setattr(kb, "count_chunks", lambda **kw: dict(live_counts))
    monkeypatch.setattr(xb, "_ensure_cache_loaded", lambda: None)

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"user_id": 7703, "is_admin": True}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(xb.router)
    return TestClient(app, raise_server_exceptions=False)


def test_health_endpoint_reports_stale_instead_of_a_blanket_ok(monkeypatch):
    """🔴 主锁:manifest 与当前代码版本不一致时,端点必须报 stale + 说清原因。

    把端点里的裁定换回 `"ok": True` 字面量,本条必红。
    """
    client = _health_client(monkeypatch,
                            _good_manifest(terminology_rule_version="1999-old"),
                            LIVE_OK)
    resp = client.get("/api/xiaobang/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    assert body["status"] == STATUS_STALE, body
    assert body["reasons"], body
    assert any("terminology_rule_version" in r for r in body["reasons"]), body["reasons"]
    # 向后兼容:旧消费方读的键一个都不许少
    for legacy in ("model", "kb_chunks", "bm25_threshold",
                   "deepseek_key_configured", "cache_loaded_at"):
        assert legacy in body, legacy


def test_health_endpoint_is_ok_when_everything_matches(monkeypatch):
    """反向对照:一致时必须是 ok —— 否则上面那条可能只是「永远 false」。"""
    client = _health_client(monkeypatch, _good_manifest(), LIVE_OK)
    body = client.get("/api/xiaobang/health").json()
    assert body["ok"] is True, body
    assert body["status"] == STATUS_OK
    assert body["reasons"] == []


def test_health_endpoint_never_500s_when_the_verdict_blows_up(monkeypatch):
    """裁定算不出来时:报 incomplete,**不 500、也不退回 ok=True**。

    「读不到」和「一切正常」在返回值上同形,是本仓反复付费的假绿。
    """
    import services.kb_health_verdict as hv

    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(hv, "evaluate", _boom)

    client = _health_client(monkeypatch, _good_manifest(), LIVE_OK)
    resp = client.get("/api/xiaobang/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    assert body["status"] == STATUS_INCOMPLETE, body
