"""[答案实体] 抽取器:entity_key 归一 + LLM 失败 degraded 不造假 + answer 组映射 + fail-soft。"""
import pytest

from services.research_monitor.answer_entity_extractor import (
    build_answer_entity_key,
    extract_entities_from_group,
)


def test_entity_key_folds_cross_language_alias():
    """阿里云 / Alibaba Cloud / alibaba cloud → 同一 key(种子命中)。"""
    k1 = build_answer_entity_key("阿里云")
    k2 = build_answer_entity_key("Alibaba Cloud")
    k3 = build_answer_entity_key("alibaba cloud")
    assert k1 and k1 == k2 == k3, f"跨语言别名应折叠成同一 key: {k1}/{k2}/{k3}"


def test_entity_key_folds_case_and_whitespace_for_unseeded():
    a = build_answer_entity_key("Foo Bar Tech")
    b = build_answer_entity_key("foobartech")
    c = build_answer_entity_key("FOO BAR TECH")
    assert a and a == b == c


def test_entity_key_empty_name_returns_empty():
    assert build_answer_entity_key("   ") == ""


def _group():
    """一个 answer 组(SQL 侧已归一 engine + 折叠 citation 行)。"""
    return {"anchor_raw_id": 1, "raw_ids": [1, 2, 3], "industry": "科技数码",
            "query": "云服务哪家好", "engine": "DeepSeek", "batch_id": "b1",
            "answer_md5": "d41d8cd98f00b204e9800998ecf8427e",
            "answer_text": "综合来看推荐阿里云,稳定可靠。" * 10,
            "citation_urls": ["http://a.com/1", "http://b.com/2"]}


@pytest.mark.asyncio
async def test_extract_maps_brands_to_entities(monkeypatch):
    import tools.distillation.distiller as dz

    async def fake_ok(**kw):
        res = dz.DistillationResult(keyword="k", brands=[
            dz.BrandInfo(name="阿里云", ranks={"DeepSeek": 1},
                         recommendation_reasons=["稳定可靠"], description_keywords=["云计算"]),
        ])
        return res, 10, "auto"

    monkeypatch.setattr(dz, "distill_keyword", fake_ok)
    out = await extract_entities_from_group(_group())
    assert out["quality_flag"] == "auto"
    assert len(out["entities"]) == 1
    ent = out["entities"][0]
    assert ent["entity_name"] == "阿里云"
    assert ent["entity_key"] == build_answer_entity_key("阿里云")
    assert ent["recommendation_reasons"] == ["稳定可靠"]
    assert ent["recommendation_rank"] == 1
    # fact 是 answer 级:answer_hash 来自 SQL 组、raw_ids 聚合全部 citation 行
    assert out["fact"]["engine"] == "DeepSeek"
    assert out["fact"]["answer_hash"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert out["fact"]["raw_ids"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_llm_failure_returns_degraded_no_fabrication(monkeypatch):
    import tools.distillation.distiller as dz

    async def fake_degraded(**kw):
        return dz.DistillationResult(keyword="k", brands=[]), 0, "degraded"

    monkeypatch.setattr(dz, "distill_keyword", fake_degraded)
    out = await extract_entities_from_group(_group())
    assert out["quality_flag"] == "degraded"
    assert out["entities"] == [], "LLM 失败不得造假写实体"


@pytest.mark.asyncio
async def test_rebuild_fail_soft_returns_error_dict_not_raise(monkeypatch):
    """[review fix low] 后台路径循环外异常(如 fetch DB 错)→ 顶层 fail-soft 返 error dict,不冒泡。"""
    from services.research_monitor import answer_entity_extractor as ext

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ext, "fetch_answer_groups_for_extraction", boom)
    out = await ext.rebuild_answer_entities(industry="教育培训", limit=10, dry_run=False)
    assert out["mode"] == "error" and "error" in out
