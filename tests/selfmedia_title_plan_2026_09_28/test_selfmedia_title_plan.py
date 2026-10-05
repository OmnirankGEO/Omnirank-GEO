# -*- coding: utf-8 -*-
"""自媒体口径单生成标题全入口 500(Review 09-28 · WO_225-c1 起)。

病:出题侧按**条**算推荐配比总数(`_required_article_count`:7 槽 ⇒ 35 条),
    `direction_distribution.build_user_choice_style_plan` 却按**槽**校验(`_capacity_of`:7)
    ⇒ 条 ≠ 槽(自媒体 1 槽≈5 条)时必抛 `user_choice_distribution sum=35 != configurable_count=7`,
    整批 500 + 全额退款,且 500 的 detail 把这句内部串原样给了服务商。老单没快照 ⇒ 1:1 ⇒ 不中。

修法(Review 定):
  · 对内按条:builder 加可选 `posts_per_keyword`,给了就按条校验、按条排 slot;不给 = 老行为(按槽)。
  · 对服务商仍按槽:自定义配比照旧按 configurable_count(槽)校验,生成前用 `scale_distribution_to_posts`
    按比例放大到条数(最大余数法);防御型上限按标题条数计,放大后超出的分给服务商选的其他方向。
  · 500 对外一句中文 + 请求编号,细节只进日志。

  S1 🔴 自媒体单(7 槽 ⇒ 35 条)真走 KeywordTopicGenerator.generate()(假 LLM 只拦 chat/completions):成功、35 条、每条都有文体
  S2 🔴 k=1 老单:推荐路径与自定义配比路径的计划,与改前逐条相同(改前算法原样抄进本文件当参照)
  S3 🔴 牙证:同样的自媒体数据,按槽校验(= 改前)必抛 —— 生产上的那句
  S4 🔴 放大:k=1 原样;合计恰为条数;防御型封顶 14 且超出按比例分走;只选防御型时报「防御型最多」
  S5 🔴 generate-titles 的兜底 500 不再 detail=str(e),对外带请求编号
  S6 🔴 接线:出题侧推荐路径与 server 自定义配比路径都把每词条数交给 builder,自定义路径先放大再排
"""
from __future__ import annotations

import ast
import asyncio
import functools
import json
import sys
from collections import Counter
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from writing.direction_distribution import (  # noqa: E402
    DistributionValidationError,
    build_user_choice_style_plan,
    scale_distribution_to_posts,
)
from writing.defensive_questions import defensive_capacity  # noqa: E402

SELF_MEDIA = [{"id": 1, "keyword": "新词七槽", "required_articles": 7, "planned_posts_default": 35},
              {"id": 2, "keyword": "零槽词", "required_articles": 0, "planned_posts_default": 0}]
K1 = [{"id": 1, "keyword": "甲", "required_articles": 7, "planned_posts_default": 7},
      {"id": 2, "keyword": "乙", "required_articles": 3, "planned_posts_default": 3}]


# ─────────────────────────── 假 LLM(只拦 chat/completions,其余外呼拒绝) ───────────────────────────

@pytest.fixture
def fake_llm(monkeypatch):
    from writing import keyword_topic_generator as ktg
    state = {"gen": None, "calls": 0}
    orig_init = ktg.KeywordTopicGenerator.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        state["gen"] = self

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    async def _post(_self, url, *a, json=None, **k):
        if "chat/completions" not in str(url):
            raise RuntimeError("判据拒绝外呼 %s" % url)
        state["calls"] += 1
        gen = state["gen"]
        prompt = "".join(str(m.get("content") or "") for m in ((json or {}).get("messages") or []))
        names = {kw.get("id"): kw.get("keyword") for kw in gen.keywords}
        topics = [{"original_keyword": names[p["keyword_id"]], "keyword_id": p["keyword_id"],
                   "slot_index": p["slot_index"],
                   "optimized_title": "%s怎么选?第%d个要看的地方" % (names[p["keyword_id"]], p["slot_index"] + 1),
                   "article_style": p.get("user_choice") or ""}
                  for p in (gen.style_plan or []) if names[p["keyword_id"]] in prompt]
        content = __import__("json").dumps({"topics": topics}, ensure_ascii=False)
        return _Resp({"choices": [{"message": {"content": content}}], "model": "fake",
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def _blocked(*a, **k):
        raise RuntimeError("判据拒绝同步外呼")

    monkeypatch.setattr(ktg.KeywordTopicGenerator, "__init__", _init)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(httpx, "post", _blocked)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-not-a-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-not-a-key")
    return state


def test_s1_self_media_quote_generates_every_post_with_a_style(fake_llm):
    from writing.keyword_topic_generator import KeywordTopicGenerator
    gen = KeywordTopicGenerator(keywords=[dict(k) for k in SELF_MEDIA], brand_name="测试品牌", industry="测试行业")
    assert len(gen.style_plan) == 35 and Counter(p["keyword_id"] for p in gen.style_plan) == {1: 35}
    topics = asyncio.run(gen.generate())
    assert fake_llm["calls"] >= 1, "没走到 LLM —— 判据走了别的路"
    assert len(topics) == 35
    assert all(t.get("user_choice") for t in topics), "有标题没分到文体"
    assert sorted(t["slot_index"] for t in topics) == list(range(35))
    assert gen.title_failure_report["produced"] == 35 and gen.title_failure_report["failed"] == 0


# ─────────────────────────── 改前算法(从 6225813b8 原样抄来,只当参照) ───────────────────────────

def _old_builder(keywords, distribution, *, source, industry=None):
    """AM 6225813b8 的 build_user_choice_style_plan:按槽(_capacity_of)校验与排 slot。"""
    return build_user_choice_style_plan(keywords, distribution, source=source, industry=industry)


def _old_server_inline_plan(detail_keywords, distribution):
    """AM 6225813b8 server.py generate-titles 自定义配比那段就地排法(原样)。"""
    from collections import defaultdict as _defaultdict
    _kw_slots = _defaultdict(list)
    for _kw in detail_keywords:
        _kw_id = _kw.get("id")
        _required = int(_kw.get("required_articles") or 1)
        for _s in range(_required):
            _kw_slots[_kw_id].append(_s)
    _dist_remaining = dict(distribution)
    plan = []
    _max_slots = max(len(s) for s in _kw_slots.values()) if _kw_slots else 0
    for _slot_idx in range(_max_slots):
        for _kw_id, _slots in _kw_slots.items():
            if _slot_idx >= len(_slots):
                continue
            for _uc, _cnt in list(_dist_remaining.items()):
                if _cnt > 0:
                    plan.append({"keyword_id": _kw_id, "slot_index": _slot_idx, "user_choice": _uc,
                                 "user_choice_source": "batch_distribution"})
                    _dist_remaining[_uc] = _cnt - 1
                    break
    return plan


def test_s2_k1_recommended_plan_is_unchanged():
    from writing.keyword_topic_generator import KeywordTopicGenerator, _required_article_count
    from writing.direction_distribution import compute_recommended_user_choice_distribution
    gen = KeywordTopicGenerator(keywords=[dict(k) for k in K1], brand_name="测试品牌", industry="测试行业")
    recommended = compute_recommended_user_choice_distribution("测试行业", 10)
    before = _old_builder([dict(k) for k in K1], recommended, source=None, industry="测试行业")
    assert sum(_required_article_count(k) for k in K1) == 10
    # 生成器上的计划还过了一道「意图适配闸」,那道闸对新旧两份输入是同一个函数;比它之前的原始计划
    after = build_user_choice_style_plan([dict(k) for k in K1], recommended, source=None, industry="测试行业",
                                         posts_per_keyword={1: 7, 2: 3})
    assert after == before
    assert len(gen.style_plan) == 10


def test_s2_k1_custom_distribution_plan_is_unchanged():
    dist = {"multi_brand_comparison": 4, "evidence_qa": 3, "implementation_guide": 3}
    before = _old_server_inline_plan([dict(k) for k in K1], dist)
    posts = {1: 7, 2: 3}
    after = build_user_choice_style_plan([dict(k) for k in K1], scale_distribution_to_posts(dist, 10),
                                         source="batch_distribution", posts_per_keyword=posts)
    assert after == before


# ─────────────────────────── 牙证与放大 ───────────────────────────

def test_s3_counting_in_slots_is_what_broke_production():
    rec = {"multi_brand_comparison": 20, "evidence_qa": 15}
    with pytest.raises(DistributionValidationError, match="sum=35 != configurable_count=7"):
        build_user_choice_style_plan([dict(SELF_MEDIA[0])], rec, source=None)          # 改前:按槽
    plan = build_user_choice_style_plan([dict(SELF_MEDIA[0])], rec, source=None, posts_per_keyword={1: 35})
    assert len(plan) == 35


def test_s4_scale_to_posts():
    k1 = {"multi_brand_comparison": 4, "evidence_qa": 3}
    assert scale_distribution_to_posts(k1, 7) == k1                                       # k=1 原样
    s = scale_distribution_to_posts({"multi_brand_comparison": 4, "evidence_qa": 3}, 35)
    assert s == {"multi_brand_comparison": 20, "evidence_qa": 15} and sum(s.values()) == 35
    s = scale_distribution_to_posts({"multi_brand_comparison": 2, "evidence_qa": 2, "implementation_guide": 3}, 36)
    assert sum(s.values()) == 36
    cap = defensive_capacity()
    s = scale_distribution_to_posts({"defensive_company": 4, "evidence_qa": 3}, 35)       # 4 槽 ⇒ 20 条 > 上限
    assert s["defensive_company"] == cap and sum(s.values()) == 35 and s["evidence_qa"] == 35 - cap
    # [WO_317 第三笔] 只选防御型:钳到上限,合计 < 条数,由调用方收小本批并写明(不再报错不出)
    only_def = scale_distribution_to_posts({"defensive_company": 4}, 20)
    assert only_def == {"defensive_company": cap}
    # 可用上限更小(子集批次减掉同一单已有的防御题)
    assert scale_distribution_to_posts({"defensive_company": 4, "evidence_qa": 3}, 35, defensive_cap=5) == \
        {"defensive_company": 5, "evidence_qa": 30}


# ─────────────────────────── 静态:500 对外文案与接线 ───────────────────────────

@functools.lru_cache(maxsize=1)
def _generate_titles_src() -> str:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    return ast.get_source_segment(src, fn)


def leaks_exception_text(fn_src: str) -> list[int]:
    """`HTTPException(status_code=500, detail=str(<异常名>))` 的行号。"""
    out = []
    for n in ast.walk(ast.parse(fn_src)):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "HTTPException":
            kw = {k.arg: k.value for k in n.keywords}
            code, detail = kw.get("status_code"), kw.get("detail")
            if (isinstance(code, ast.Constant) and code.value == 500 and isinstance(detail, ast.Call)
                    and getattr(detail.func, "id", None) == "str"):
                out.append(n.lineno)
    return out


def test_s5_the_500_does_not_leak_internal_text():
    fn = _generate_titles_src()
    assert leaks_exception_text(fn) == []
    assert "请提供请求编号 {_title_request_id}" in fn
    assert leaks_exception_text(fn + "\n\ndef _x(e):\n    raise HTTPException(status_code=500, detail=str(e))\n")  # 牙证


def test_s6_both_paths_hand_post_counts_to_the_builder():
    fn = _generate_titles_src()
    assert "_scaled_for_gen = scale_distribution_to_posts(\n                    _distribution_for_gen, _total_for_gen, defensive_cap=_def_cap_now)" in fn
    assert "                    _plannable_for_gen,\n                    _scaled_for_gen,\n" in fn
    assert "posts_per_keyword=_posts_for_gen" in fn
    assert "_style_plan_for_gen.append(" not in fn, "自定义配比又就地按槽排了"
    ktg = (ROOT / "writing" / "keyword_topic_generator.py").read_text(encoding="utf-8")
    assert "posts_per_keyword={item.get(\"id\"): _required_article_count(item)" in ktg
