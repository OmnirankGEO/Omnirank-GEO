"""角度池(Owner 2026-07-22:同主题天天发不重样)。

分配不重复 / 轮换确定性 / 角度进 LLM prompt 与 visual brief / 角度冻结进
geo_snapshot / retry 按 job 血缘轮换 / fallback 正文结构随角度变。
"""
import asyncio
import json

from api import marketing_material_api
from services.marketing import content_angles, content_center, geo_factory


def _strategy() -> dict:
    return {
        "audience": "服务商老板", "audience_status": "客户在问 AI", "action_resistance": "担心没证据",
        "human_problem": "客户问 AI 时，答案里有没有他的品牌", "core_angle": "先看诊断",
        "single_value": "用真实诊断把下一步讲清楚", "evidence_statement": "只用冻结证据",
        "single_action": "领取一次 GEO 诊断",
    }


def _teacher() -> dict:
    return {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师", "system_method": "先把术语翻成人话"}


def test_angle_pool_has_five_named_angles_with_guidance():
    assert set(content_angles.ANGLE_POOL) == {
        "deal_fact", "business_logic", "philosophy", "trust_persona", "industry_observation",
    }
    names = {spec["name"] for spec in content_angles.ANGLE_POOL.values()}
    assert names == {"成交事实", "商业逻辑", "经营理念", "靠谱人设", "行业观察"}
    for spec in content_angles.ANGLE_POOL.values():
        assert spec["guidance"] and "第一人称" in spec["guidance"]  # 指导语含人话文风口径
        assert spec["visual"]  # 每个角度都有视觉指导语


def test_assign_angles_base_mapping_matches_owner_example():
    assigned = content_angles.assign_angles(
        ["professional_poster", "moments", "xiaohongshu", "douyin", "infographic"], rotation=0,
    )
    # Owner 示例口径:海报=成交事实、朋友圈=经营理念、小红书=商业逻辑、抖音=靠谱人设、信息图=行业观察
    assert assigned == {
        "professional_poster": "deal_fact",
        "moments": "philosophy",
        "xiaohongshu": "business_logic",
        "douyin": "trust_persona",
        "infographic": "industry_observation",
    }
    assert len(set(assigned.values())) == 5  # 同一 job 内不重复


def test_assign_angles_deal_package_no_repeat_within_job():
    channels = ["deal_poster", "deal_chat", "deal_data_card", "deal_story", "deal_feedback_card"]
    assigned = content_angles.assign_angles(channels, rotation=0)
    assert len(set(assigned.values())) == 5  # 五个晒成交渠道各占一个角度
    rotated = content_angles.assign_angles(channels, rotation=2)
    assert len(set(rotated.values())) == 5


def test_assign_angles_rotation_deterministic_and_changes_every_channel():
    channels = ["professional_poster", "moments", "xiaohongshu", "douyin", "infographic"]
    base = content_angles.assign_angles(channels, rotation=0)
    rotated = content_angles.assign_angles(channels, rotation=1)
    assert rotated == content_angles.assign_angles(channels, rotation=1)  # 确定性
    assert rotated != base
    assert all(rotated[channel] != base[channel] for channel in channels)  # 每个渠道都换角度
    assert len(set(rotated.values())) == 5  # 轮换后仍不重复
    # rotation 取模循环:rotation=5 与 rotation=0 一致
    assert content_angles.assign_angles(channels, rotation=5) == base


def test_rotation_from_request_hash_deterministic_and_bounded():
    first = content_angles.rotation_from_request_hash("ab12cd34ef56" * 5)
    assert first == content_angles.rotation_from_request_hash("ab12cd34ef56" * 5)
    assert 0 <= first < len(content_angles.ANGLE_POOL)
    assert content_angles.rotation_from_request_hash("") == 0  # 非法输入回落 0
    assert content_angles.rotation_from_request_hash(None) == 0


def test_next_rotation_follows_job_lineage():
    assert content_angles.next_rotation(0) == 1
    assert content_angles.next_rotation(len(content_angles.ANGLE_POOL) - 1) == 0  # 回绕
    assert content_angles.next_rotation(None) == 1  # 存量无角度 job 从 1 起步
    assert content_angles.next_rotation("bad") == 1


def test_fallback_copy_body_structure_varies_by_angle():
    strategy = _strategy()
    contact = {"mode": "none", "text": ""}
    bodies, outlines = {}, {}
    for key in content_angles.ANGLE_POOL:
        content = content_center.fallback_channel_content(
            "xiaohongshu", strategy, {"facts": []}, contact, angle=key,
        )
        bodies[key] = content["body"]
        outlines[key] = tuple(content["card_outline"])
    assert len(set(bodies.values())) == 5  # 五个角度正文全不同(不只换标题)
    assert len(set(outlines.values())) == 5  # 卡片提纲结构也随角度变
    assert bodies["philosophy"].startswith("我做这门生意")
    assert bodies["business_logic"].startswith("算一笔明白账")
    # angle=None 保持 v1 原结构(存量 job 无角度,行为不变)
    legacy = content_center.fallback_channel_content("xiaohongshu", strategy, {"facts": []}, contact)
    assert legacy["body"].startswith("很多老板已经开始直接问 AI 选谁")
    assert legacy["card_outline"] == ["客户正在怎么问 AI", "诊断里能看到什么", "下一步先做什么"]
    assert legacy["body"] not in set(bodies.values())


def test_fallback_moments_and_deal_copy_vary_by_angle_but_keep_facts():
    strategy = _strategy()
    contact = {"mode": "none", "text": ""}
    tones = {
        key: content_center.fallback_channel_content(
            "moments", strategy, {"facts": []}, contact, angle=key,
        )["tones"]["restrained"]
        for key in content_angles.ANGLE_POOL
    }
    assert len(set(tones.values())) == 5
    # 晒成交:事实锚点(确认单金额/原话)不随角度漂移,角度只换叙事骨架
    evidence = {"facts": [
        {"key": "deal_amount", "label": "成交金额", "value": "5万元"},
        {"key": "customer_praise", "label": "客户原话", "value": "响应速度特别快"},
    ]}
    deal_bodies = {
        key: content_center.fallback_channel_content(
            "deal_poster", strategy, evidence, contact, angle=key,
        )["body"]
        for key in content_angles.ANGLE_POOL
    }
    assert len(set(deal_bodies.values())) == 5
    for body in deal_bodies.values():
        assert "5万元" in body  # 事实锚点以确认单为准,任何角度都在


def test_angle_reaches_llm_prompt_and_fallback(monkeypatch):
    captured = {}

    async def fake_llm(prompt, verbose=False, before_provider_call=None):
        captured["prompt"] = prompt
        return None  # 触发确定性回落

    import tools.multi_llm_caller as llm_module

    monkeypatch.setattr(llm_module, "call_llm_with_fallback", fake_llm)
    content, qa = asyncio.run(content_center.generate_channel_content(
        "moments", strategy=_strategy(), evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": ""}, teacher=_teacher(), angle="philosophy",
    ))
    assert qa["passed"] is True
    assert "经营理念" in captured["prompt"] and "philosophy" in captured["prompt"]
    assert "角度指导" in captured["prompt"]  # 该角度怎么讲的指导语进 prompt
    assert "一直认一个理" in content["tones"]["restrained"]  # 回落文案同样按角度


def test_angle_enters_visual_brief_subject():
    prompt = content_center.visual_prompt(
        slot={"slot": "infographic", "size": "3:4"}, channel="infographic",
        strategy=_strategy(), content={"title": "先看诊断", "body": "只讲可核验事实"},
        evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
        angle="industry_observation",
    )
    document = json.loads(prompt.split("\n", 1)[1])
    assert document["subject"]["angle"]["key"] == "industry_observation"
    assert document["subject"]["angle"]["name"] == "行业观察"
    assert document["subject"]["angle"]["guidance"]
    # 无角度(存量 job):subject.angle 为空,不注入角度指导
    legacy = content_center.visual_prompt(
        slot={"slot": "infographic", "size": "3:4"}, channel="infographic",
        strategy=_strategy(), content={"title": "先看诊断"},
        evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
    )
    assert json.loads(legacy.split("\n", 1)[1])["subject"]["angle"] is None
    # 非法角度 fail-closed 为 None(冻结快照之外的值不注入)
    invalid = content_center.visual_prompt(
        slot={"slot": "infographic", "size": "3:4"}, channel="infographic",
        strategy=_strategy(), content={"title": "先看诊断"},
        evidence={"facts": []}, trend={"used": False},
        contact={"mode": "none", "text": "", "qr_reference": None}, brand={},
        angle="clickbait",
    )
    assert json.loads(invalid.split("\n", 1)[1])["subject"]["angle"] is None


def _stub_package_api(monkeypatch):
    from auth import brand_access
    from services.marketing import strategy_teachers

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 1})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 1)
    monkeypatch.setattr(marketing_material_api, "_principal_user_id", lambda _request, _user: 1)
    monkeypatch.setattr(brand_access, "require_brand_access", lambda _request, _brand_id: None)
    monkeypatch.setattr(
        strategy_teachers, "resolve_teacher",
        lambda **_kwargs: {"teacher_id": "shu", "version": "1.0.0", "name": "舒老师"},
    )
    monkeypatch.setattr(marketing_material_api, "_freeze_service_brand", lambda _p: {"name": ""})
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 1}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    return captured


def _package_payload(**overrides) -> dict:
    payload = {
        "request_id": "angle-freeze-001",
        "brief": "向服务商老板推广真实 GEO 诊断",
        "quick_task": "promote_geo",
        "strategy": _strategy(),
        "channels": ["professional_poster", "moments", "xiaohongshu", "douyin", "infographic"],
        "evidence": {"source_type": "none"},
        "contact": {"mode": "none"},
    }
    payload.update(overrides)
    return payload


def test_angles_frozen_into_geo_snapshot(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    captured = _stub_package_api(monkeypatch)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    response = TestClient(app).post("/api/marketing/content-packages", json=_package_payload())
    assert response.status_code == 200, response.text
    snapshot = captured["geo_snapshot"]
    angles = snapshot["angles"]
    assert set(angles) == set(snapshot["channels"])  # 每渠道一个角度
    assert set(angles.values()) <= set(content_angles.ANGLE_POOL)
    assert len(set(angles.values())) == len(angles)  # 同 job 内不重复
    # 轮换位 = request hash 决定,且与冻结的 request_hash 自洽(可溯)
    assert snapshot["angle_rotation"] == content_angles.rotation_from_request_hash(snapshot["request_hash"])
    assert angles == content_angles.assign_angles(
        snapshot["channels"], rotation=snapshot["angle_rotation"],
    )


def test_retry_rotates_angles_from_job_lineage(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(marketing_material_api, "_require_user", lambda _request: {"id": 9})
    monkeypatch.setattr(marketing_material_api, "_uid", lambda _user: 9)
    channels = ["professional_poster", "moments", "xiaohongshu", "douyin", "infographic"]
    geo = {
        "request_id": "angle-freeze-001", "request_hash": "a" * 64,
        "channels": channels, "moments_layout": "single",
        "strategy": _strategy(), "evidence": {"source_type": "none", "facts": []},
        "contact": {"mode": "none", "text": ""},
        "teacher": _teacher(),
        "angles": content_angles.assign_angles(channels, rotation=0),
        "angle_rotation": 0,
    }
    job = {
        "id": 5, "user_id": 9, "status": "succeeded", "error_summary": "",
        "brand_id": None, "resolution": "1k", "input_fields_jsonb": {"_geo": geo},
    }
    monkeypatch.setattr(marketing_material_api.marketing_db, "get_job", lambda _id: dict(job))
    captured = {}

    async def prepare(**kwargs):
        captured["geo_snapshot"] = kwargs["geo_snapshot"]
        return {"status": "generating", "job_id": 6}

    monkeypatch.setattr(geo_factory, "prepare_geo_package_job", prepare)
    app = FastAPI()
    app.include_router(marketing_material_api.router)
    response = TestClient(app).post("/api/marketing/jobs/5/retry", json={
        "request_id": "angle-retry-001", "component_ids": ["moments:copy"],
    })
    assert response.status_code == 200, response.text
    retry_geo = captured["geo_snapshot"]
    assert retry_geo["angle_rotation"] == 1  # job 血缘父 rotation+1
    assert retry_geo["angles"] == content_angles.assign_angles(channels, rotation=1)
    assert retry_geo["angles"] != geo["angles"]  # 与上一版角度不同
    assert retry_geo["angles"]["moments"] != geo["angles"]["moments"]
    assert len(set(retry_geo["angles"].values())) == 5  # 轮换后仍不重复
