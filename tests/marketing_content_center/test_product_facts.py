"""Product-facts SSOT pack contracts: load/validate, version freeze, prompt
injection, fallback fact citation, and snapshot traceability. DB-free."""
import asyncio
import json
import os

import pytest
from starlette.requests import Request

from api import marketing_material_api
from services.marketing import content_center, product_facts


def _teacher() -> dict:
    return {
        "teacher_id": "shu", "name": "舒老师", "version": "1.0.0",
        "system_method": "受众现状→行动阻力→一句人话问题→唯一卖点→真实证据→低门槛唯一行动。",
        "channel_boundaries": ["不虚构数字、客户证言、排名或收益"],
    }


def _strategy() -> dict:
    return {
        "audience": "有客户资源的服务商老板",
        "human_problem": "客户已经开始问 AI，但答案里可能还没有他的品牌",
        "core_angle": "用真实诊断把下一步讲清楚",
        "single_value": "把诊断、内容和监测变成客户能看见的证据链",
        "single_action": "领取一次 GEO 诊断",
        "evidence_statement": "仅使用用户选择并已冻结的真实资料",
    }


def _contact() -> dict:
    return {"mode": "none", "text": "", "qr_reference": None}


def _frozen_pack() -> dict:
    """模拟 geo_snapshot.product_facts 的冻结产物。"""
    pack = product_facts.resolve_facts_pack()
    return product_facts.freeze_facts_for_snapshot(
        pack, quick_task="promote_geo",
        channels=["moments", "professional_poster", "infographic"],
        angles={"moments": "philosophy", "professional_poster": "deal_fact",
                "infographic": "industry_observation"},
    )


# ---------------------------------------------------------------------------
# 包加载与校验
# ---------------------------------------------------------------------------
def test_pack_loads_with_version_and_verified_facts():
    pack = product_facts.resolve_facts_pack()
    assert pack["pack_id"] == product_facts.PACK_ID == "product_facts"
    assert pack["version"] == product_facts.PACK_VERSION == "1.0.0"
    facts = pack["facts"]
    assert len(facts) >= 10
    ids = [fact["id"] for fact in facts]
    assert len(set(ids)) == len(ids)  # id 唯一
    kinds = {fact["kind"] for fact in facts}
    # 五类齐全:GEO 是什么/能力清单/差异化/术语表/禁用表述
    assert {"concept", "capability", "differentiator", "terminology", "forbidden"} <= kinds
    for fact in facts:
        assert fact["name"] and fact["one_liner"]
        assert fact["provenance"] == "system_verified"  # 只收平台核验事实
        assert fact.get("source")  # 每条带来源文档证据


def test_pack_hot_reload_on_file_change(tmp_path):
    path = tmp_path / "pack.json"
    original = product_facts.resolve_facts_pack()
    payload = {key: value for key, value in original.items() if not key.startswith("_")}
    payload["version"] = "9.9.9"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert product_facts._load_pack(str(path))["version"] == "9.9.9"
    payload["version"] = "9.9.10"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    assert product_facts._load_pack(str(path))["version"] == "9.9.10"  # mtime 变化即热生效


def test_pack_validation_rejects_bad_provenance_and_duplicate_ids(tmp_path):
    original = product_facts.resolve_facts_pack()
    bad = json.loads(json.dumps({k: v for k, v in original.items() if not k.startswith("_")}))
    bad["facts"][0]["provenance"] = "customer_asserted"  # 事实包只收 system_verified
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        product_facts._load_pack(str(path))
    dup = json.loads(json.dumps(bad))
    dup["facts"][0]["provenance"] = "system_verified"
    dup["facts"][1]["id"] = dup["facts"][0]["id"]
    path.write_text(json.dumps(dup, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        product_facts._validate_pack(dup, path="mem")


# ---------------------------------------------------------------------------
# 版本冻结与可溯
# ---------------------------------------------------------------------------
def test_version_resolution_is_fail_closed():
    assert product_facts.resolve_facts_pack()["version"] == "1.0.0"
    assert product_facts.resolve_facts_pack(version="1.0.0")["version"] == "1.0.0"
    with pytest.raises(ValueError, match="product_facts_version_not_found"):
        product_facts.resolve_facts_pack(version="0.0.0")
    with pytest.raises(ValueError, match="product_facts_pack_not_found"):
        product_facts.resolve_facts_pack(pack_id="unknown_pack")


def test_snapshot_freeze_deep_copies_and_stays_traceable():
    pack = product_facts.resolve_facts_pack()
    frozen = product_facts.freeze_facts_for_snapshot(
        pack, quick_task="promote_geo", channels=["moments"], angles={"moments": "philosophy"},
    )
    assert frozen["pack_id"] == "product_facts"
    assert frozen["version"] == "1.0.0"  # pack_id+version 冻结可溯
    assert frozen["facts"]
    for fact in frozen["facts"]:
        assert fact["provenance"] == "system_verified"
        assert fact["kind"] in product_facts.PROMPT_KINDS  # forbidden 不进注入子集
    # 深拷贝:冻结后包内容变化不回流进旧快照
    original_first = frozen["facts"][0]["one_liner"]
    pack["facts"][0]["one_liner"] = "热更新后的新口径"
    assert frozen["facts"][0]["one_liner"] == original_first


def test_selection_is_deterministic_and_forbidden_never_injected():
    pack = product_facts.resolve_facts_pack()
    first = product_facts.select_facts(pack, quick_task="promote_geo", limit=6)
    second = product_facts.select_facts(pack, quick_task="promote_geo", limit=6)
    assert [fact["id"] for fact in first] == [fact["id"] for fact in second]
    assert all(fact["kind"] != "forbidden" for fact in first)
    assert len(first) <= 6  # limit 控 token
    by_angle = product_facts.select_facts(pack, angle="philosophy", limit=1)
    assert by_angle and "philosophy" in (by_angle[0].get("relevance", {}).get("angles") or [])


# ---------------------------------------------------------------------------
# prompt 注入(interpret + 渠道文案 + visual brief)
# ---------------------------------------------------------------------------
def test_interpret_brief_prompt_carries_facts(monkeypatch):
    captured = {}

    async def fake_llm(prompt, verbose=False):
        captured["prompt"] = prompt
        return None  # 触发确定性回落

    import tools.multi_llm_caller as llm_module

    monkeypatch.setattr(llm_module, "call_llm_with_fallback", fake_llm)
    strategy = asyncio.run(content_center.interpret_brief(
        "向服务制造业的服务商推广我的 GEO 诊断服务", _teacher(),
        facts_pack=product_facts.resolve_facts_pack(), quick_task="promote_geo",
    ))
    assert strategy["audience"]  # 回落策略照常产出
    assert "平台已核验产品事实" in captured["prompt"]
    assert "system_verified" in captured["prompt"]
    assert "concept_geo" in captured["prompt"]  # 事实条目进 prompt


def test_channel_prompt_carries_pack_version_and_facts(monkeypatch):
    captured = {}

    async def fake_llm(prompt, verbose=False, before_provider_call=None):
        captured["prompt"] = prompt
        return None

    import tools.multi_llm_caller as llm_module

    monkeypatch.setattr(llm_module, "call_llm_with_fallback", fake_llm)
    frozen = _frozen_pack()
    content, qa = asyncio.run(content_center.generate_channel_content(
        "moments", strategy=_strategy(), evidence={"facts": []}, trend={"used": False},
        contact=_contact(), teacher=_teacher(), angle="philosophy", facts_pack=frozen,
    ))
    assert qa["passed"] is True
    assert "产品事实库:product_facts@1.0.0" in captured["prompt"]  # 版本随注入可溯
    assert "system_verified" in captured["prompt"]
    selected = product_facts.select_facts(frozen, channel="moments", angle="philosophy", limit=5)
    assert selected and selected[0]["one_liner"] in captured["prompt"]
    # 无冻结包(存量 job):prompt 不含产品事实块,行为不变
    captured.clear()
    asyncio.run(content_center.generate_channel_content(
        "moments", strategy=_strategy(), evidence={"facts": []}, trend={"used": False},
        contact=_contact(), teacher=_teacher(), angle="philosophy",
    ))
    assert "产品事实库" not in captured["prompt"]


def test_visual_brief_subject_carries_product_facts_summary():
    frozen = _frozen_pack()
    prompt = content_center.visual_prompt(
        slot={"slot": "infographic", "size": "3:4"}, channel="infographic",
        strategy=_strategy(), content={"title": "先看诊断", "body": "只讲可核验事实"},
        evidence={"facts": []}, trend={"used": False},
        contact=_contact(), brand={}, angle="industry_observation", facts_pack=frozen,
    )
    subject = json.loads(prompt.split("\n", 1)[1])["subject"]
    assert subject["product_facts"]["pack"] == "product_facts@1.0.0"
    assert subject["product_facts"]["provenance"] == "system_verified"
    assert subject["product_facts"]["summary"]  # 摘要非空
    assert len(subject["product_facts"]["summary"]) <= 120  # 控 token
    legacy = content_center.visual_prompt(
        slot={"slot": "infographic", "size": "3:4"}, channel="infographic",
        strategy=_strategy(), content={"title": "先看诊断"},
        evidence={"facts": []}, trend={"used": False}, contact=_contact(), brand={},
    )
    assert json.loads(legacy.split("\n", 1)[1])["subject"]["product_facts"] is None


# ---------------------------------------------------------------------------
# fallback 引用事实(商业逻辑/行业观察/经营理念不再是空话)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("angle", ["business_logic", "industry_observation", "philosophy"])
def test_fallback_cites_fact_for_opinion_angles(angle):
    frozen = _frozen_pack()
    expected = product_facts.select_facts(frozen, angle=angle, limit=1)
    assert expected, "测试前提:该角度在冻结子集中有可引用事实"
    content = content_center.fallback_channel_content(
        "moments", _strategy(), {"facts": []}, _contact(), angle=angle, facts_pack=frozen,
    )
    body = json.dumps(content["tones"], ensure_ascii=False)
    assert expected[0]["one_liner"] in body  # 至少引用一条事实包事实


def test_fallback_without_fact_angles_or_pack_stays_v1():
    frozen = _frozen_pack()
    strategy = _strategy()
    for angle in (None, "deal_fact", "trust_persona"):
        with_pack = content_center.fallback_channel_content(
            "moments", strategy, {"facts": []}, _contact(), angle=angle, facts_pack=frozen,
        )
        without_pack = content_center.fallback_channel_content(
            "moments", strategy, {"facts": []}, _contact(), angle=angle,
        )
        assert with_pack == without_pack  # 事实锚点角度之外输出完全一致
    # 有包但冻结子集为空:不引用、不报错
    empty_pack = {"pack_id": "product_facts", "version": "1.0.0", "facts": []}
    content = content_center.fallback_channel_content(
        "moments", strategy, {"facts": []}, _contact(), angle="philosophy", facts_pack=empty_pack,
    )
    assert content["tones"]["restrained"]


def test_fallback_fact_anchor_requires_system_verified():
    tampered = {
        "pack_id": "product_facts", "version": "1.0.0",
        "facts": [{
            "id": "fake", "kind": "capability", "name": "未核验",
            "one_liner": "未经平台核验的说法", "points": [],
            "relevance": {"angles": ["philosophy"]}, "provenance": "customer_asserted",
        }],
    }
    assert content_center._fallback_fact_anchor(tampered, "philosophy") == ""


# ---------------------------------------------------------------------------
# 管理端点(只读)
# ---------------------------------------------------------------------------
def _request(user_id: int = 9) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/api/marketing/product-facts", "headers": []})
    request.state.user = {"id": user_id}
    return request


def test_product_facts_endpoint_returns_version_and_count():
    response = asyncio.run(marketing_material_api.api_product_facts(_request()))
    assert response["ok"] is True
    pack = response["pack"]
    assert pack["pack_id"] == "product_facts"
    assert pack["version"] == "1.0.0"
    assert pack["fact_count"] == len(pack["facts"]) >= 10
    assert pack["counts_by_kind"]["forbidden"] >= 3  # 禁用表述可见但不在注入子集
    assert pack["source_docs"]
