# -*- coding: utf-8 -*-
"""榜单路由锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3

三层:① 有据/无据两半式(D12④) ② 面×行业默认版式(不是开关行业) ③ 十母版选一。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import ranking_router as rr
from services.geo_douyin.ranking_payload import (
    FORM_MATRIX, FORM_RANKING, FORM_SCENARIO,
)


REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "services" / "geo_douyin" / "ranking_router.py"

_CLIENT = "深圳市晨光富士电梯有限公司"


def _cand(name, rank=None, phrases=(), engines=("deepseek", "kimi")):
    return {"entity_name": name, "best_rank": rank, "engine_count": len(engines),
            "mention_count": 10, "evidence_phrases": list(phrases),
            "source": {"engine": engines[0] if engines else ""}}


# ===========================================================================
# 十母版族完整性
# ===========================================================================

def test_all_ten_masters_have_a_template_id():
    """母版 manifest 里的十个 template_id 必须在代码里都有定义。"""
    import json
    manifest = json.loads(
        (REPO / "docs" / "AI-CONTEXT"
         / "ASSET_MANIFEST_GEO_RANKING_MASTERS_V1.json").read_text(encoding="utf-8"))
    ids = {m["template_id"] for m in manifest["masters"]}
    assert ids == set(rr.TEMPLATES), f"母版与代码对不上:{ids ^ set(rr.TEMPLATES)}"


def test_top3_template_is_pinned_to_exactly_three():
    t = rr.TEMPLATES["top3_provider"]
    assert t.min_entities == t.max_entities == 3
    assert "Do not add a fourth provider" in t.composition


def test_every_template_composition_forbids_inventing_facts():
    """反向对照:十段 Composition 里不得有任何"可以补事实"的口径。"""
    for tid, t in rr.TEMPLATES.items():
        low = t.composition.lower()
        for bad in ("you may invent", "make up", "estimate the score"):
            assert bad not in low, f"{tid} 允许编事实"


# ===========================================================================
# ① 有据 / 无据两半式(D12④)
# ===========================================================================

def test_client_with_real_rank_gets_ranking_form():
    cands = [_cand(_CLIENT, rank=2), _cand("快意电梯", rank=1)]
    v = rr.decide_form(cands, client_brand=_CLIENT)
    assert v.has_evidence and v.form == FORM_RANKING and v.client_rank == 2


def test_client_rank_matches_by_merge_key_not_exact_string():
    """`深圳市X有限公司` 与 `晨光富士电梯` 是同一家 —— 精确串比会漏判成无据。"""
    cands = [_cand("晨光富士电梯", rank=3)]
    v = rr.decide_form(cands, client_brand=_CLIENT)
    assert v.has_evidence and v.client_rank == 3


def test_client_position_is_the_second_evidence_source():
    v = rr.decide_form([_cand("别家", rank=1)], client_brand=_CLIENT,
                       client_position={"best_rank": 9})
    assert v.has_evidence and v.client_rank == 9


@pytest.mark.parametrize("cp", [None, {}, {"best_rank": 0}, {"best_rank": None},
                                {"mentioned_in": 0, "best_rank": 0}])
def test_client_without_rank_never_gets_ranking_form(cp):
    """🔴 无据**绝不**给 ranking form —— 挂榜单名把客户放榜外是 D12④ 明禁。"""
    cands = [_cand("别家A", rank=1), _cand("别家B", rank=2)]
    v = rr.decide_form(cands, client_brand=_CLIENT, client_position=cp)
    assert not v.has_evidence
    assert v.form in (FORM_SCENARIO, FORM_MATRIX)
    assert v.client_rank is None


def test_no_evidence_never_fabricates_a_rank():
    v = rr.decide_form([_cand("别家", rank=1)], client_brand=_CLIENT)
    assert v.client_rank is None, "给客户塞了一个没依据的名次"


def test_manual_override_cannot_turn_no_evidence_into_ranking():
    """🔴 最关键的一条:手动覆盖只影响版式偏好,**不能把无据说成有据**。"""
    cands = [_cand("别家A", rank=1), _cand("别家B", rank=2)]
    out = rr.route_template(cands, industry_key="geo_优化服务",
                            client_brand=_CLIENT, force_ranking=True)
    assert out["has_client_evidence"] is False
    assert out["form"] != FORM_RANKING
    assert out["allows_ranking_wording"] is False


# ===========================================================================
# ② 面 × 行业(默认版式,不是关行业)
# ===========================================================================

@pytest.mark.parametrize("ind", ["home_improvement", "finance"])
def test_negative_industries_default_to_non_ranking_layout(ind):
    assert rr.default_prefers_ranking(ind) is False


@pytest.mark.parametrize("ind", ["geo_优化服务", "auto", "retail_ecommerce",
                                 "education", "文娱游戏"])
def test_positive_industries_default_to_ranking_layout(ind):
    assert rr.default_prefers_ranking(ind) is True


def test_negative_industry_is_a_default_not_a_hard_block():
    """🔴 反向对照:家装**不是被关掉** —— 手动覆盖必须能把版式扳回榜单感。

    D12 唯一成功案例恰是家装 TOP10 文章,「关家装」与类目分治裁决正面冲突。
    """
    assert rr.default_prefers_ranking("home_improvement") is False
    out = rr.route_template([_cand(_CLIENT, rank=1), _cand("别家", rank=2)],
                            industry_key="home_improvement",
                            client_brand=_CLIENT, force_ranking=True)
    assert out["form"] == FORM_RANKING and out["overridden"] is True


def test_article_surface_is_not_narrowed_by_this_module():
    """文章端按 D12② 类目分治,本模块不得意外收窄它。"""
    for ind in ("home_improvement", "finance", "unknown_xyz"):
        assert rr.default_prefers_ranking(ind, surface=rr.SURFACE_ARTICLE) is True


def test_evidence_survives_even_when_layout_is_not_ranking():
    """有据 + 默认非榜单行业 → 版式让步,但**实名次不丢**。"""
    out = rr.route_template([_cand(_CLIENT, rank=2), _cand("别家", rank=1)],
                            industry_key="home_improvement", client_brand=_CLIENT)
    assert out["has_client_evidence"] is True
    assert out["client_rank"] == 2
    assert out["form"] == FORM_SCENARIO           # 版式让步
    assert out["allows_ranking_wording"] is False


# ===========================================================================
# ③ template_id 选择
# ===========================================================================

def test_three_strong_candidates_route_to_top3():
    out = rr.route_template([_cand(_CLIENT, rank=1), _cand("A", rank=2), _cand("B", rank=3)],
                            industry_key="geo_优化服务", client_brand=_CLIENT)
    assert out["template_id"] == "top3_provider"


def test_rich_dimensions_route_to_spec_matrix():
    cands = [_cand("A", rank=1, phrases=["24小时响应", "1-16吨载重"]),
             _cand("B", rank=2, phrases=["非标井道", "本地维保"]),
             _cand("C", rank=3, phrases=["A1", "B2"]), _cand("D", rank=4)]
    out = rr.route_template(cands, industry_key="unknown", client_brand="没有的客户")
    assert out["template_id"] == "tech_spec_matrix"


def test_force_template_is_honoured_when_known():
    out = rr.route_template([_cand("A")], force_template="collab_mode")
    assert out["template_id"] == "collab_mode" and out["overridden"] is True


def test_unknown_force_template_falls_back_instead_of_raising():
    """反向对照:不认识的 template_id 落兜底,不抛(永不中断)。"""
    out = rr.route_template([_cand("A")], force_template="no_such_template")
    assert out["template_id"] in rr.TEMPLATES


def test_router_never_raises_on_garbage_input():
    for bad in ([], [{}], [{"entity_name": None}], None):
        out = rr.route_template(bad or [], industry_key="", client_brand="")
        assert out["template_id"] in rr.TEMPLATES


# ===========================================================================
# 形态锁:不许出现硬拦
# ===========================================================================

def test_router_has_no_blocking_semantics():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not raises, "路由模块里出现 raise —— 路由不该阻断"


def test_industry_lists_are_defaults_not_gates():
    """形态判据:行业名单只能喂给 `default_prefers_ranking`,不得出现在返回 None/抛错的路径上。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "default_prefers_ranking")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns, "default_prefers_ranking 没有 return"
    for r in returns:
        assert r.value is not None, "行业门控返回了 None(疑似当成开关用)"
