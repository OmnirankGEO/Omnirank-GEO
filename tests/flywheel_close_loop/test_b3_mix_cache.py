"""B3 媒体组合判定 · 缓存 + 异步预热判别锁(recommend-v2 性能第二刀 §B)。

改前:代理每打开一次推荐页 / 每切一次行业 → 请求里同步发一次真实 LLM
(cProfile: judge 1.79s → _call_one 1.78s → httpx.post 1.65s),
而且 media / wemedia 两次调用发的是**完全一样**的判定。

本文件锁住四件事,任何一条被改坏都必须红:

1. **请求路径永不调 LLM** —— 未命中也只投后台,不在请求里现算;
2. **未命中不劣化** —— 保留 T2 代码算好的确定性组合(= 判断点关闭时代理一直看的那份),
   不是空态;
3. **连开 N 次页只发 1 次 LLM**(工单验收 3);
4. **缓存不是无脑复用** —— 过期 / 域已不在 mix / 一边倒的旧选择,一律当未命中。

这些用例跑**真实 PostgreSQL 与真实缓存 SQL**(不 mock `lookup_choice`):
缓存靠 ``input_summary->>'industry'`` 这类 JSONB 取值反查,mock 掉就等于没测
到唯一会出错的那一段。
"""
from __future__ import annotations

import time

import pytest

from services.flywheel_judgment import PROMPT_VERSION
from services.flywheel_media_mix_choice import POINT_KEY

INDUSTRY = "geo_test_建筑建材"
KEYWORD = ""
TOTAL_SLOTS = 4

MIX = {
    "version": "geo-question-family-mix-v1.0",
    "keyword": KEYWORD, "industry": INDUSTRY, "intent_bucket": "recommendation",
    "source": "question_family", "window_days": 180, "total_citations": 122,
    "trunk_share": 0.55, "vertical_share": 0.45,
    "explanation": "测试用 mix",
    "entries": [
        {"domain": "to8to.com", "citations": 36, "share": 0.30, "share_pct": 30.0,
         "role": "vertical_home", "role_label": "家居垂直", "family_key": "to8to",
         "family_label": "土巴兔", "is_trunk": False, "self_serve": False},
        {"domain": "sohu.com", "citations": 33, "share": 0.27, "share_pct": 27.0,
         "role": "portal", "role_label": "综合门户", "family_key": "sohu",
         "family_label": "搜狐", "is_trunk": True, "self_serve": False},
        {"domain": "cnblogs.com", "citations": 18, "share": 0.15, "share_pct": 15.0,
         "role": "tech_community", "role_label": "技术社区", "family_key": "cnblogs",
         "family_label": "博客园", "is_trunk": True, "self_serve": True},
        {"domain": "163.com", "citations": 9, "share": 0.07, "share_pct": 7.0,
         "role": "portal", "role_label": "综合门户", "family_key": "netease",
         "family_label": "网易", "is_trunk": True, "self_serve": False},
        {"domain": "jiaju.sina.com.cn", "citations": 9, "share": 0.07, "share_pct": 7.0,
         "role": "vertical_home", "role_label": "家居垂直", "family_key": "sina_home",
         "family_label": "新浪家居", "is_trunk": False, "self_serve": False},
    ],
}

#: T2 代码算出来的确定性组合 —— 未命中时必须原封不动是这一份。
CODE_PLAN_DOMAINS = ["to8to.com", "sohu.com", "cnblogs.com", "163.com"]


def _plan():
    by_domain = {e["domain"]: e for e in MIX["entries"]}
    slots = [
        {"domain": d, "role": by_domain[d]["role"], "role_label": by_domain[d]["role_label"],
         "is_trunk": by_domain[d]["is_trunk"], "share_pct": by_domain[d]["share_pct"],
         "citations": by_domain[d]["citations"], "fulfilled_by": f"媒体-{d}",
         "fulfilled_media_id": 1, "substituted": False, "substitution_note": "",
         "self_serve_action": None}
        for d in CODE_PLAN_DOMAINS
    ]
    return {
        "version": MIX["version"], "advisory": True, "total_slots": TOTAL_SLOTS,
        "trunk_slots": sum(1 for s in slots if s["is_trunk"]),
        "vertical_slots": sum(1 for s in slots if not s["is_trunk"]),
        "reason": MIX["explanation"], "mix": MIX, "slots": slots,
        "substitutions": [], "self_serve_offers": [],
    }


def _annotations():
    return {"question_family_mix": MIX, "combination_plan": _plan()}


@pytest.fixture
def judgment_log_db():
    """干净的判定留痕表 —— 它同时就是 B3 的缓存存储。"""
    from db.connection import get_db
    from db.flywheel_judgment_db import init_flywheel_judgment_tables
    from services.flywheel_media_mix_cache import reset_state

    init_flywheel_judgment_tables(force=True)

    def _clean():
        with get_db() as conn:
            conn.cursor().execute(
                "DELETE FROM flywheel_judgment_log "
                "WHERE input_summary->>'industry' LIKE 'geo_test%%'"
            )

    _clean()
    reset_state()
    yield
    _clean()
    reset_state()


@pytest.fixture
def point_enabled(monkeypatch):
    monkeypatch.setenv("FLYWHEEL_JUDGE_MEDIA_MIX_DECISION", "1")
    monkeypatch.delenv("FLYWHEEL_MEDIA_MIX_CACHE_TTL_SECONDS", raising=False)


def _seed_cache(selected_domains, *, industry=INDUSTRY, keyword=KEYWORD,
                total_slots=TOTAL_SLOTS, prompt_version=PROMPT_VERSION,
                age_seconds=0):
    """往留痕表写一条"上次模型是这么选的",即 B3 的缓存条目。"""
    from db.connection import get_db
    from db.flywheel_judgment_db import SOURCE_LLM, record_judgment

    record_judgment(
        point_key=POINT_KEY, source=SOURCE_LLM, provider="deepseek",
        model="deepseek-v4-flash", prompt_version=prompt_version,
        input_summary={"keyword": keyword, "industry": industry,
                       "mix_source": "question_family", "candidates": 5,
                       "total_slots": total_slots, "total_citations": 122},
        output={"selected_domains": list(selected_domains), "reason": "缓存里的取舍"},
    )
    if age_seconds:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE flywheel_judgment_log "
                "   SET created_at = NOW() - make_interval(secs => %s) "
                " WHERE point_key = %s AND input_summary->>'industry' = %s",
                (int(age_seconds), POINT_KEY, industry),
            )


def _forbid_llm(monkeypatch):
    """请求路径上任何一次真实 LLM 调用都必须让用例炸掉。"""
    def _boom(*a, **kw):
        raise AssertionError("请求路径不允许调用 LLM")

    monkeypatch.setattr("services.flywheel_judgment._call_one", _boom)
    monkeypatch.setattr("services.flywheel_judgment.judge", _boom)


def _drain_warms(timeout=30.0):
    from services.flywheel_media_mix_cache import inflight_count

    deadline = time.time() + timeout
    while inflight_count() and time.time() < deadline:
        time.sleep(0.05)
    return inflight_count() == 0


# ---------------------------------------------------------------------------
# 1 · 请求路径永不调 LLM
# ---------------------------------------------------------------------------
def test_cache_miss_never_calls_llm_in_request_path(judgment_log_db, point_enabled,
                                                    monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _forbid_llm(monkeypatch)
    # 预热也别真发出去 —— 本用例只锁"请求路径不调",不测预热内容。
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]

    assert plan["llm_choice"]["applied"] is False
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"
    assert plan["llm_choice"]["warming"] is True


def test_cache_miss_keeps_deterministic_code_plan_not_empty(judgment_log_db,
                                                            point_enabled, monkeypatch):
    """🔒 未命中**不劣化**:保留 T2 代码算好的组合,不是"暂无组合建议"空态。

    代码方案是纯统计算出来的,不花钱也不慢,质量等同于判断点关闭时代理一直在看的
    那一份。为省一次 LLM 调用把它换成空态,方向是反的。
    """
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    out = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)
    plan = out["combination_plan"]

    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS
    assert plan["reason"], "组合话术不能空"
    assert out["question_family_mix"] is not None


# ---------------------------------------------------------------------------
# 2 · 缓存命中
# ---------------------------------------------------------------------------
def test_cache_hit_applies_previous_selection_without_llm(judgment_log_db,
                                                          point_enabled, monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"])
    _forbid_llm(monkeypatch)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]

    assert [s["domain"] for s in plan["slots"]] == [
        "sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"]
    assert plan["llm_choice"]["applied"] is True
    assert plan["llm_choice"]["source"] == "cache"
    # 🔒 统计口径仍由代码回填,缓存里没有这些数字也不该采信模型的
    sohu = next(s for s in plan["slots"] if s["domain"] == "sohu.com")
    assert sohu["citations"] == 33 and sohu["role_label"] == "综合门户"
    assert plan["trunk_slots"] == 2 and plan["vertical_slots"] == 2


def test_cache_is_scoped_by_industry(judgment_log_db, point_enabled, monkeypatch):
    """🔒 别的行业的判定不能串味 —— 缓存键必须真的带 industry。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"],
                industry="geo_test_别的行业")
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS


def test_cache_is_scoped_by_total_slots(judgment_log_db, point_enabled, monkeypatch):
    """🔒 limit 变了组合位数就变了,不能拿 8 个位的判定去填 4 个位。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"],
                total_slots=TOTAL_SLOTS + 4)
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"


def test_expired_cache_is_a_miss(judgment_log_db, point_enabled, monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    monkeypatch.setenv("FLYWHEEL_MEDIA_MIX_CACHE_TTL_SECONDS", "60")
    _seed_cache(["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"],
                age_seconds=3600)
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS


def test_cache_from_other_prompt_version_is_a_miss(judgment_log_db, point_enabled,
                                                   monkeypatch):
    """🔒 prompt 改了旧判定就不能再用 —— 否则 prompt 回归看到的是上一版的结论。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"],
                prompt_version="some-older-version")
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"


# ---------------------------------------------------------------------------
# 3 · 缓存也要过结构守卫
# ---------------------------------------------------------------------------
def test_stale_cache_with_vanished_domains_is_rejected(judgment_log_db, point_enabled,
                                                       monkeypatch):
    """🔒 缓存写下之后 mix 会变(补货/缺货)。旧选择的域已不在 mix 里 → 必须当没命中,
    不能把不存在的域塞进坑位。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["早就没了.com", "也没了.com"])
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS
    assert plan["llm_choice"]["applied"] is False


def test_all_trunk_cache_is_rejected(judgment_log_db, point_enabled, monkeypatch):
    """🔒 混合结构守卫对缓存同样生效 —— 一边倒的旧选择正是这台引擎要修的错配。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    _seed_cache(["sohu.com", "cnblogs.com", "163.com"])  # 全主干
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS
    assert plan["llm_choice"]["applied"] is False


def test_point_disabled_skips_cache_entirely(judgment_log_db, monkeypatch):
    """判断点没开 = 本来就该用代码方案,连缓存都不该查。"""
    from services.flywheel_media_mix_choice import refine_media_combination_cached

    monkeypatch.delenv("FLYWHEEL_JUDGE_MEDIA_MIX_DECISION", raising=False)
    monkeypatch.delenv("FLYWHEEL_JUDGE_ALL", raising=False)
    _forbid_llm(monkeypatch)

    def _boom_lookup(**kw):
        raise AssertionError("判断点关闭时不该查缓存")

    monkeypatch.setattr("services.flywheel_media_mix_cache.lookup_choice", _boom_lookup)

    plan = refine_media_combination_cached(
        _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
    assert plan["llm_choice"] == {"source": "rule", "applied": False,
                                  "fallback_reason": "point_disabled"}
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS


# ---------------------------------------------------------------------------
# 4 · 工单验收 3:连开 10 次页,LLM 调用 ≤ 1
# ---------------------------------------------------------------------------
def test_ten_page_opens_trigger_at_most_one_llm_call(judgment_log_db, point_enabled,
                                                     monkeypatch):
    """🔒 工单验收 ③ 的判别锁。

    数的是**真实网络调用** ``_call_one`` 的次数,不是"判断点被调用次数"——
    后者被缓存挡掉是理所当然的,前者才是真的花钱那一下。
    """
    import json

    from services.flywheel_media_mix_choice import refine_media_combination_cached

    calls: list[str] = []

    def _fake_call_one(provider, model, prompt, point):
        calls.append(provider)
        return {
            "ok": True,
            "text": json.dumps({
                "selected_domains": ["sohu.com", "163.com", "to8to.com",
                                     "jiaju.sina.com.cn"],
                "reason": "主干两个 + 垂直两个",
            }, ensure_ascii=False),
            "input_tokens": 1266, "output_tokens": 114, "cost_cny": 0.0015,
        }

    monkeypatch.setattr("services.flywheel_judgment._call_one", _fake_call_one)

    applied_flags = []
    for _ in range(10):
        # 一次"打开推荐页" = 端点里那两次 recommend_for_publish_v2 的 T2 块。
        # 改后 wemedia 侧已 with_question_family=False,所以每页只有一次。
        plan = refine_media_combination_cached(
            _annotations(), keyword=KEYWORD, industry=INDUSTRY)["combination_plan"]
        applied_flags.append(plan["llm_choice"]["applied"])
        assert _drain_warms(), "后台预热没在超时内收敛"

    assert len(calls) <= 1, f"10 次推荐页只允许 1 次真实 LLM 调用,实际 {len(calls)} 次"
    assert len(calls) == 1, "预热必须真的发生过一次,否则缓存永远是空的(假绿)"
    assert applied_flags[0] is False, "第一次必然未命中,用代码方案"
    assert applied_flags[-1] is True, "预热之后必须命中缓存并应用"


# ---------------------------------------------------------------------------
# 4b · 接线锁:placement_service 必须走 _cached 变体
# ---------------------------------------------------------------------------
def test_question_family_annotations_goes_through_cache(judgment_log_db, point_enabled,
                                                        monkeypatch):
    """🔒 接线锁 —— 上面那些用例都直接调 ``refine_media_combination_cached``,
    证明不了**热路径确实接到了它**。把 ``_question_family_annotations`` 改回同步的
    ``refine_media_combination``,本用例必须红。

    注意 ``_question_family_annotations`` 整个包在 try/except 里,同步路径撞上
    "禁止调 LLM" 会被吞成 ``{None, None}`` —— 所以这里同时断言 mix 不为 None,
    否则异常被吞掉会看起来像通过。
    """
    import services.placement_service as ps

    class _FakeMix:
        @staticmethod
        def as_dict():
            return MIX

    class _FakePlan:
        @staticmethod
        def as_dict():
            return _plan()

    monkeypatch.setattr("services.question_family_mix.build_question_family_mix",
                        lambda **kw: _FakeMix)
    monkeypatch.setattr("services.question_family_mix.plan_combination",
                        lambda mix, total_slots=8: _FakePlan)
    _forbid_llm(monkeypatch)
    monkeypatch.setattr("services.flywheel_media_mix_cache.submit_warm",
                        lambda key, runner: True)

    out = ps._question_family_annotations(keyword=KEYWORD, industry=INDUSTRY,
                                          limit=TOTAL_SLOTS)

    assert out["question_family_mix"] is not None, \
        "返回 None 说明异常被 try/except 吞了 —— 多半是热路径还在同步调 LLM"
    plan = out["combination_plan"]
    assert plan["llm_choice"]["fallback_reason"] == "cache_miss"
    assert [s["domain"] for s in plan["slots"]] == CODE_PLAN_DOMAINS


# ---------------------------------------------------------------------------
# 5 · with_question_family 开关本身
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_recommend_internals(monkeypatch):
    """把 recommend_for_publish_v2 周边全部替身掉,只留下 T2 那个分支是真的。

    目的是**只锁 with_question_family 这一个分支**,不把整条推荐链拖进来。
    """
    import services.placement_service as ps

    monkeypatch.setattr(ps, "get_placement_service", lambda: type(
        "S", (), {"_match_research_industry": staticmethod(       # WO_267:真实接口多了 brand= 关键字
            lambda ind, **kw: ({"搜狐": {"score": 90, "engines": [], "citation_rate": 0.5}},
                               "建筑建材"))})())
    monkeypatch.setattr("services.media_balance_gate.evaluate_gate",
                        lambda **kw: {"enabled": True, "reason": "global_on"})
    monkeypatch.setattr("services.media_flywheel_recommendation.recommend_from_media_flywheel",
                        lambda **kw: {"used": False})
    monkeypatch.setattr(ps, "_resolve_industry_head_fallback", lambda a, b: [])
    monkeypatch.setattr(ps, "_load_citation_weight_table", lambda ind: None)
    monkeypatch.setattr(ps, "_load_media_success_table", lambda: {})
    monkeypatch.setattr(ps, "_match_media", lambda *a, **kw: {"vertical": [], "generic": []})
    monkeypatch.setattr(ps, "_match_wemedia", lambda *a, **kw: {"vertical": [], "generic": []})
    monkeypatch.setattr(ps, "_record_media_balance_shadow", lambda **kw: None)
    monkeypatch.setattr(ps, "_citation_result_annotations", lambda t, r: {})

    seen = []
    monkeypatch.setattr(ps, "_question_family_annotations",
                        lambda **kw: seen.append(kw) or {
                            "question_family_mix": {"marker": True},
                            "combination_plan": {"marker": True}})
    return seen


def test_with_question_family_default_true_computes_t2(stub_recommend_internals):
    """默认不传 = 老行为,照常算 T2。"""
    from services.placement_service import recommend_for_publish_v2

    out = recommend_for_publish_v2(industry="建筑建材", media_type="media", limit=8)
    assert len(stub_recommend_internals) == 1
    assert out["question_family_mix"] == {"marker": True}


def test_with_question_family_false_skips_t2(stub_recommend_internals):
    """🔒 传 False 必须整块跳过 —— 这正是省掉 wemedia 那次重复判定的开关。

    改成"参数收下但照算"(``if _mb_on:``)本用例立刻红。
    """
    from services.placement_service import recommend_for_publish_v2

    out = recommend_for_publish_v2(industry="建筑建材", media_type="wemedia", limit=8,
                                   with_question_family=False)
    assert stub_recommend_internals == [], "with_question_family=False 时不该算 T2"
    assert out["question_family_mix"] is None
    assert out["combination_plan"] is None


def test_concurrent_misses_only_warm_once(judgment_log_db, point_enabled, monkeypatch):
    """🔒 单飞:media / wemedia 或多个代理同时进来,只允许一个预热在跑。

    没有这一条,gather 并发之后第一次未命中会同时投两个预热 → 两次 LLM。
    """
    import threading

    from services.flywheel_media_mix_cache import submit_warm

    started = []
    release = threading.Event()

    def _slow_runner():
        started.append(1)
        release.wait(timeout=5)

    key = "same-key"
    first = submit_warm(key, _slow_runner)
    second = submit_warm(key, _slow_runner)
    release.set()
    assert _drain_warms()

    assert first is True and second is False
    assert len(started) == 1
