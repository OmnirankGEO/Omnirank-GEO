"""W3 模拟对比层 · 单测(DB-free · mock 重依赖)。

核心(SPEC 🔴):生产同源模拟两臂只差 base_prompt →
  - user_message 逐字节一致(结构/品牌/竞品/知识全同)
  - system_prompt 不同(候选 base 覆盖了当前 base)
sim_overrides 默认 None → 生产路径逐字不变(不进任何 sim 分支)。
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------- 重依赖 fake ----------------
class _FakeCursor:
    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = None


class _FakeCompletions:
    async def create(self, **kwargs):
        return _FakeResp("这是一篇用于模拟对比的测试正文。" * 40)


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, **kwargs):
        self.chat = _FakeChat()


class _FakeDistiller:
    def __init__(self, *a, **k):
        pass

    async def run(self):
        return {
            "client_profile": "{}", "selling_points": "{}", "competitor_analysis": "{}",
            "social_media_data": {}, "authoritative_sources": "{}", "case_examples": "{}",
        }


class _FakeRag:
    def retrieve(self, *a, **k):
        return []


def _install_mocks(monkeypatch):
    import openai
    import db.diagnosis_db as ddb
    import writing.distiller as distiller_mod
    import tools.unified_knowledge as uk
    import services.public_whitelabel as pw

    # _generate_single 里是 `from db.diagnosis_db import get_connection`(方法内 import),patch 源模块属性
    monkeypatch.setattr(ddb, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(distiller_mod, "DistillerPipeline", _FakeDistiller)
    monkeypatch.setattr(uk, "get_unified_rag", lambda: _FakeRag())
    monkeypatch.setattr(pw, "resolve_branding_context", lambda **k: {"source": "platform_default"})
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)


def test_sim_seam_user_message_identical_system_prompt_differs(monkeypatch):
    _install_mocks(monkeypatch)
    from writing.article_generator_service import ArticleGeneratorService

    svc = ArticleGeneratorService(101, "演示品牌", "企业法律服务")
    pinned = {"dynamic_scores": {}, "case_industry": svc._get_next_case_industry()}
    topic = {
        "id": None, "title": "企业法律服务十大品牌推荐", "style_code": "ranking_v2",
        "user_choice": "auto", "_trust_legacy_style": True,
        "structure_guidance_instruction": "【结构参考】先给答案再分点。",
    }
    url, key, model = "https://x/v1/chat/completions", "sk-test", "test-model"

    sim_a = dict(pinned)  # 当前版(base_prompt 缺省)
    art_a = asyncio.run(svc._generate_single(topic, url, key, model, sim_overrides=sim_a))
    sim_b = dict(pinned)
    sim_b["base_prompt"] = "你是候选版排行榜写手。开头先直接给出结论,再分 5 点展开。" * 3
    art_b = asyncio.run(svc._generate_single(topic, url, key, model, sim_overrides=sim_b))

    # 🔴 两臂 user_message 逐字节一致(结构参考 + 品牌 + 竞品 + 知识全同)
    assert sim_a["_captured_user_message"] == sim_b["_captured_user_message"]
    # 🔴 system_prompt 不同(候选 base 覆盖了当前 base)
    assert sim_a["_captured_system_prompt"] != sim_b["_captured_system_prompt"]
    # 候选 base 内容进入了候选臂 system_prompt
    assert "候选版排行榜写手" in sim_b["_captured_system_prompt"]
    assert "候选版排行榜写手" not in sim_a["_captured_system_prompt"]
    # 两臂都真的产出了正文
    assert art_a["content"] and art_b["content"]


def test_sim_structure_guidance_state_shared_both_arms(monkeypatch):
    """结构参考两臂同一份 → user_message 里结构块两臂一致(控制面固定)。"""
    _install_mocks(monkeypatch)
    from writing.article_generator_service import ArticleGeneratorService

    svc = ArticleGeneratorService(1, "B", "法律")
    pinned = {"dynamic_scores": {}, "case_industry": "SaaS企业服务（CRM/ERP）"}
    topic = {
        "id": None, "title": "法律服务榜单", "style_code": "ranking_v2",
        "user_choice": "auto", "_trust_legacy_style": True,
        "structure_guidance_instruction": "【本项目结构参考】开头给答案。",
    }
    sim_a = dict(pinned)
    asyncio.run(svc._generate_single(topic, "u", "k", "m", sim_overrides=sim_a))
    assert "【本项目文章结构参考】" in sim_a["_captured_user_message"]
    assert "开头给答案" in sim_a["_captured_user_message"]


def test_estimate_simulation_cost():
    from services.writing_style_simulation import estimate_simulation_cost

    c = estimate_simulation_cost()
    assert c["generations"] == 2
    assert c["est_cost_cny"] > 0


def test_pick_demo_title_style_aware():
    from services.writing_style_simulation import _pick_demo_title

    t = _pick_demo_title({"industry": "企业法律服务"}, "ranking_v2")
    assert "企业法律服务" in t and "榜单" in t


def test_dry_run_resolves_context_no_llm(monkeypatch):
    import services.writing_style_simulation as sim
    import writing.feature_switches as fs

    monkeypatch.setattr(sim, "get_demo_quote", lambda *a, **k: {"quote_id": 7, "brand_name": "演示", "industry": "法律"})
    monkeypatch.setattr(fs, "is_feature_enabled", lambda key: False)  # 结构关 → off_flag
    res = asyncio.run(sim.run_style_simulation(
        style_code="ranking_v2", industry_key="法律", candidate_prompt="候选 prompt 内容" * 10, dry_run=True
    ))
    assert res["mode"] == "dry_run"
    assert res["demo_quote_id"] == 7
    assert res["structure_guidance_state"] == "off_flag"
    assert res["est_cost_cny"] > 0


def test_run_requires_candidate(monkeypatch):
    import services.writing_style_simulation as sim

    with pytest.raises(ValueError):
        asyncio.run(sim.run_style_simulation(style_code="ranking_v2", industry_key="法律", dry_run=True))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
