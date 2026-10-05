"""
锚词=售价最高 · keyword_cluster._dedup_within_group 同义组保留标准回归测试(2026-06-07)

决策1A(老板拍):同义组内保留 selling_price 最高(并列 required_articles 最高)= 锚词,
其余同义词 → 覆盖(免费/不监测)。LLM 只做"哪些词同义"的语义分组,保留谁由代码层确定性决定。

mock LLM 返回固定 groups,断言保留逻辑确定性——不依赖真 LLM / 不连 DB。
"""
import json
import pytest

import tools.keyword_cluster as kc


def _make(keyword, selling_price, required_articles=1, search_volume=0):
    return {
        "keyword": keyword,
        "selling_price": selling_price,
        "required_articles": required_articles,
        "search_volume": search_volume,
    }


def _patch_llm_groups(monkeypatch, groups):
    async def fake_llm(prompt, verbose=False):
        return json.dumps({"groups": groups}, ensure_ascii=False)
    monkeypatch.setattr("tools.multi_llm_caller.call_llm_with_fallback", fake_llm)


@pytest.mark.asyncio
async def test_keeps_highest_selling_price(monkeypatch):
    # quote349 簇1 复现:哪家好=2975(搜索量最高·旧逻辑会选它) / 推荐=3500(售价最高) / 排名=3325
    # 新逻辑应保留售价最高(3500),便宜同义词归覆盖
    kws = [
        _make("龙岗别墅电梯哪家好", 2975, search_volume=200),
        _make("龙岗别墅电梯推荐", 3500, required_articles=8),
        _make("龙岗别墅电梯排名", 3325, required_articles=7),
    ]
    _patch_llm_groups(monkeypatch, [["龙岗别墅电梯哪家好", "龙岗别墅电梯推荐", "龙岗别墅电梯排名"]])
    kept, deduped = await kc._dedup_within_group(kws, "龙岗别墅电梯")
    assert [k["keyword"] for k in kept] == ["龙岗别墅电梯推荐"], "应保留售价最高(3500)而非搜索量最高"
    assert {k["keyword"] for k in deduped} == {"龙岗别墅电梯哪家好", "龙岗别墅电梯排名"}


@pytest.mark.asyncio
async def test_tie_break_by_required_articles(monkeypatch):
    # 售价并列 1000 → 按 required_articles 降序选;第三词 500 垫底确保走 LLM 路径(组内 >2)
    kws = [
        _make("词A", 1000, required_articles=5),
        _make("词B", 1000, required_articles=8),   # 并列售价 · 文章数更多 = 锚
        _make("词C", 500, required_articles=3),
    ]
    _patch_llm_groups(monkeypatch, [["词A", "词B", "词C"]])
    kept, deduped = await kc._dedup_within_group(kws, "组")
    assert [k["keyword"] for k in kept] == ["词B"], "并列售价按 required_articles 最高决定锚"
    assert {k["keyword"] for k in deduped} == {"词A", "词C"}


@pytest.mark.asyncio
async def test_no_groups_fallback_keeps_all(monkeypatch):
    # LLM 未给同义分组 → 安全兜底:全部保留(不误删/不误并)
    kws = [_make("词A", 1000), _make("词B", 2000), _make("词C", 3000)]

    async def fake_llm(prompt, verbose=False):
        return "{}"
    monkeypatch.setattr("tools.multi_llm_caller.call_llm_with_fallback", fake_llm)
    kept, deduped = await kc._dedup_within_group(kws, "组")
    assert len(kept) == 3 and deduped == [], "无 groups 安全兜底全部保留"


@pytest.mark.asyncio
async def test_ungrouped_words_all_kept(monkeypatch):
    # A/B 同义(留贵 B) · C/D 各自独立未被分组 → 全部保留
    kws = [_make("A", 1000), _make("B", 2000), _make("C", 1500), _make("D", 800)]
    _patch_llm_groups(monkeypatch, [["A", "B"]])
    kept, deduped = await kc._dedup_within_group(kws, "组")
    assert {k["keyword"] for k in kept} == {"B", "C", "D"}, "同义留贵 + 独立词全留"
    assert [k["keyword"] for k in deduped] == ["A"]


@pytest.mark.asyncio
async def test_multi_groups_each_keeps_priciest(monkeypatch):
    # 两个同义组各自留最贵
    kws = [
        _make("g1低", 100), _make("g1高", 900),
        _make("g2低", 200), _make("g2高", 700),
    ]
    _patch_llm_groups(monkeypatch, [["g1低", "g1高"], ["g2低", "g2高"]])
    kept, deduped = await kc._dedup_within_group(kws, "组")
    assert {k["keyword"] for k in kept} == {"g1高", "g2高"}
    assert {k["keyword"] for k in deduped} == {"g1低", "g2低"}


@pytest.mark.asyncio
async def test_small_group_skips_dedup(monkeypatch):
    # <=2 词早返回:不调 LLM · 全部保留(既有行为 · 明确边界:2 词同义组本批不收敛)
    called = {"n": 0}

    async def fake_llm(prompt, verbose=False):
        called["n"] += 1
        return "{}"
    monkeypatch.setattr("tools.multi_llm_caller.call_llm_with_fallback", fake_llm)
    kws = [_make("A", 1000), _make("B", 3000)]
    kept, deduped = await kc._dedup_within_group(kws, "组")
    assert len(kept) == 2 and deduped == [] and called["n"] == 0, "<=2 词跳过 LLM 全保留"
