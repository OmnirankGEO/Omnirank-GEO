"""
P1(Codex 第3轮)· fallback 降级补发文章必须带白标 brand_id。

/api/articles/replace 主链找不到 quote 或异常时降级到 _fallback_generate_replacement(),
其构造的 client_data 必须含 diagnosis.brand_id,否则 generate_single_article → ArticleWriter(brand_id=None)
→ 白标代理补发文章 prompt 仍带平台名(fail-open)。本测试锁死该归属:diagnosis 含 brand_id →
generate_single_article 收到的 client_data.brand_id == 该值。
"""
import asyncio

import tools.article_generator as ag


def test_fallback_replacement_threads_brand_id(monkeypatch):
    captured = {}

    async def _fake_generate_single_article(*, writer_type, client_data, **kwargs):
        captured["client_data"] = client_data
        # 返回 success=False 走 else 分支,免 output 文件 IO;本测试只验 client_data 归属
        return {"success": False, "error": "stub"}

    monkeypatch.setattr(ag, "generate_single_article", _fake_generate_single_article)

    diagnosis = {
        "brand_id": 42,
        "brand_name": "代理客户A",
        "industry": "教育",
        "total_score": 60,
    }
    asyncio.run(ag._fallback_generate_replacement(diagnosis, "原标题", article_type="ranking"))

    assert captured.get("client_data", {}).get("brand_id") == 42


def test_fallback_replacement_missing_brand_id_is_none(monkeypatch):
    """诊断无 brand_id → client_data.brand_id 为 None(不伪造,保持向后兼容,不回归)。"""
    captured = {}

    async def _fake_generate_single_article(*, writer_type, client_data, **kwargs):
        captured["client_data"] = client_data
        return {"success": False, "error": "stub"}

    monkeypatch.setattr(ag, "generate_single_article", _fake_generate_single_article)
    asyncio.run(ag._fallback_generate_replacement({"brand_name": "x"}, "t", article_type="ranking"))
    assert captured.get("client_data", {}).get("brand_id") is None
