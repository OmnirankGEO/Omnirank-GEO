"""AI 原生化升级 V1-V7 · 后端单测(DB-free,用 --noconftest 跑)。

覆盖铁律核验点:
- V2 结构化评语 fail-soft 解析 + prompt 扩字段;闸门不碰(见 test_w4_reviewer.py)。
- V1 结构 diff 只遍历 21 BOOLEAN_FEATURES + fail-soft。
- V3 空数据不调 LLM + 按整数指纹缓存 + 守卫回退。
- V4 手动才调 / GET 不内联 / 守卫回退 / 指纹缓存 / 判定不碰。
- V5 空数据不调 LLM + 缓存/刷新 + JSON 解析 fail-soft。
- V7 缓存 TTL/去重/失效钩子 + key 归一。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_all_caches():
    """每个测试前后清空所有进程缓存,避免跨测试污染。"""
    import services.article_structure_analysis as asa
    import services.flywheel_insight as fi
    import services.writing_outcome_backfill as ob
    import writing.flywheel_cache as fc

    fc.clear_all()
    ob._V3_COMMENTARY_CACHE.clear()
    asa._V4_RULES_CACHE.clear()
    fi.clear_insight_cache()
    yield
    fc.clear_all()
    ob._V3_COMMENTARY_CACHE.clear()
    asa._V4_RULES_CACHE.clear()
    fi.clear_insight_cache()


# ==================== V2 评审评语结构化 ====================
def test_v2_deblind_parses_structured_commentary():
    from services.writing_style_reviewer import _de_blind_result

    parsed = {
        "jia": {"ai_trust": 80, "platform_compliance": 70, "brand_value": 60, "user_value": 75, "adoption_fit": 65},
        "yi": {"ai_trust": 70, "platform_compliance": 65, "brand_value": 55, "user_value": 70, "adoption_fit": 60},
        "better": "jia",
        "diff_summary": ["结构更清晰", "证据更足"],
        "strengths": ["开头直接回答", "有价格区间", "有客户案例", "多余的第四条会被截断"],
        "risks": ["结尾略销售感"],
        "one_line_advice": "候选版结构更完整,值得进一步验证。",
    }
    out = _de_blind_result(parsed, jia_is="candidate")
    assert out["better_side"] == "candidate"
    assert out["strengths"] == ["开头直接回答", "有价格区间", "有客户案例"]  # ≤3 截断
    assert out["risks"] == ["结尾略销售感"]
    assert out["one_line_advice"] == "候选版结构更完整,值得进一步验证。"


def test_v2_deblind_missing_fields_failsoft():
    from services.writing_style_reviewer import _de_blind_result

    parsed = {"jia": {}, "yi": {}, "better": "tie", "diff_summary": ["x"]}  # 无 strengths/risks/advice
    out = _de_blind_result(parsed, jia_is="current")
    assert out["strengths"] == [] and out["risks"] == [] and out["one_line_advice"] == ""


def test_v2_prompt_requests_structured_fields():
    from services.writing_style_reviewer import _build_review_messages

    msgs = _build_review_messages("甲文", "乙文", "rubric")
    user = msgs[1]["content"]
    assert "strengths" in user and "risks" in user and "one_line_advice" in user


# ==================== V1 结构 diff 徽章 ====================
def test_v1_structure_diff_detects_added_features():
    from services.article_structure_analysis import BOOLEAN_FEATURES
    from services.writing_style_simulation import _compute_structure_diff

    current = {"content": "本文介绍如何挑选装修公司。市面上装修公司很多,选择时要看资质和口碑。综合比较更稳妥。"}
    candidate = {"content": current["content"] + "\n常见问题:装修多少钱?答:价格大约每平米 1000 元。客户案例:业主张先生的项目已落地。"}
    diff = _compute_structure_diff("装修公司推荐", current, candidate)
    assert set(diff.keys()) == {"added", "missing"}
    assert "正文有问答块" in diff["added"]
    assert "正文有价格或预算" in diff["added"]
    assert "正文有客户案例" in diff["added"]
    # 只用 21 布尔特征标签,不吐 structure_version/section_count 等混合键
    valid = set(BOOLEAN_FEATURES.values())
    assert all(x in valid for x in diff["added"] + diff["missing"])


def test_v1_structure_diff_identical_is_empty():
    from services.writing_style_simulation import _compute_structure_diff

    art = {"content": "同样的正文内容,榜单推荐,先看资质再看口碑。"}
    diff = _compute_structure_diff("标题", art, art)
    assert diff == {"added": [], "missing": []}


def test_v1_structure_diff_failsoft_on_bad_input():
    from services.writing_style_simulation import _compute_structure_diff

    # content 非字符串 → 内部 str() 兜底;绝不抛
    diff = _compute_structure_diff("t", {"content": None}, {"content": None})
    assert diff == {"added": [], "missing": []}


# ==================== V3 进化历史评语 ====================
def _fake_llm(monkeypatch, text, counter):
    import writing.flywheel_llm as fllm

    def fake(*a, **k):
        counter["n"] += 1
        return text
    monkeypatch.setattr(fllm, "call_flywheel_llm_sync", fake)


def test_v3_not_comparable_no_llm(monkeypatch):
    import services.writing_outcome_backfill as ob

    counter = {"n": 0}
    _fake_llm(monkeypatch, "不该被调用", counter)
    assert ob.generate_outcome_commentary({"comparable": False}, generate=True) is None
    assert counter["n"] == 0


def test_v3_insufficient_no_llm(monkeypatch):
    import services.writing_outcome_backfill as ob

    counter = {"n": 0}
    _fake_llm(monkeypatch, "不该被调用", counter)
    m = {"comparable": True, "insufficient_data": True, "style_name": "榜单"}
    assert ob.generate_outcome_commentary(m, generate=True) is None
    assert counter["n"] == 0


def test_v3_generate_false_only_cache(monkeypatch):
    import services.writing_outcome_backfill as ob

    counter = {"n": 0}
    _fake_llm(monkeypatch, "新版被引率明显提升,建议保持。", counter)
    m = {"comparable": True, "insufficient_data": False, "style_code": "ranking_v2",
         "style_name": "榜单", "new_version_id": "vN", "old_version_id": "vO",
         "new_articles": 40, "new_citations": 20, "old_articles": 40, "old_citations": 10,
         "new_rate": 0.5, "old_rate": 0.25, "rollback_suggestion": {"suggest_rollback": False, "reason": "ok"}}
    # generate=False 且缓存空 → None,不调 LLM(页面加载路径)
    assert ob.generate_outcome_commentary(m, generate=False) is None
    assert counter["n"] == 0
    # generate=True → 调一次并缓存
    assert ob.generate_outcome_commentary(m, generate=True) == "新版被引率明显提升,建议保持。"
    assert counter["n"] == 1
    # 再取 generate=False → 命中缓存,不再调 LLM
    assert ob.generate_outcome_commentary(m, generate=False) == "新版被引率明显提升,建议保持。"
    assert counter["n"] == 1


def test_v3_guard_blocks_promise_language(monkeypatch):
    import services.writing_outcome_backfill as ob

    counter = {"n": 0}
    _fake_llm(monkeypatch, "保证上榜,效果翻倍!", counter)  # 承诺话术 → 守卫拦截
    m = {"comparable": True, "insufficient_data": False, "style_code": "s", "style_name": "n",
         "new_version_id": "vN", "old_version_id": "vO", "new_articles": 40, "new_citations": 30,
         "old_articles": 40, "old_citations": 10, "new_rate": 0.75, "old_rate": 0.25,
         "rollback_suggestion": {"suggest_rollback": False, "reason": "ok"}}
    assert ob.generate_outcome_commentary(m, generate=True) is None


def test_v3_fingerprint_stable_and_sensitive():
    import services.writing_outcome_backfill as ob

    base = {"style_code": "s", "new_version_id": "vN", "old_version_id": "vO",
            "new_articles": 40, "new_citations": 20, "old_articles": 40, "old_citations": 10}
    fp1 = ob._measure_fingerprint(base, 30)
    fp2 = ob._measure_fingerprint(dict(base), 30)
    assert fp1 == fp2
    changed = dict(base); changed["new_articles"] = 41
    assert ob._measure_fingerprint(changed, 30) != fp1


def test_v3_production_backfill_dormant_no_commentary():
    """生产默认 citation_fn(诚实 no-op)→ insufficient 恒真 → 无评语(dormant),不造假。"""
    import services.writing_outcome_backfill as ob

    entry = {"style_code": "ranking_v2", "age_days": 40,
             "new": {"version_id": "vN", "activated_at": "2026-06-01T00:00:00+00:00"},
             "old": {"version_id": "vO"}}
    # 用真实 _iter_active_versions 的替身 + 默认 citation(诚实 no-op)
    import unittest.mock as mock
    with mock.patch.object(ob, "_iter_active_versions", return_value=[entry]), \
         mock.patch.object(ob, "list_articles_by_style_version_id", return_value=list(range(40))):
        res = ob.backfill_writing_outcomes(dry_run=True)
    assert res["mode"] == "dry_run"
    assert all("commentary" not in m for m in res["measures"])  # dormant:无评语


# ==================== V4 结构规则 LLM 化 ====================
def _lift_rows(recommended=True):
    return [
        {"feature": "lead_answers_question", "label": "开头先回答问题", "adopted_share": 0.6,
         "control_share": 0.2, "lift": 3.0, "recommended": recommended},
        {"feature": "has_price_or_budget", "label": "正文有价格或预算", "adopted_share": 0.5,
         "control_share": 0.2, "lift": 2.5, "recommended": recommended},
    ]


def test_v4_no_recommended_returns_none(monkeypatch):
    import services.article_structure_analysis as asa

    counter = {"n": 0}
    _fake_llm(monkeypatch, "不该调用", counter)
    assert asa.generate_industry_rules_llm(_lift_rows(recommended=False), "装修", 40, 40, generate=True) is None
    assert counter["n"] == 0


def test_v4_generate_false_no_llm(monkeypatch):
    import services.article_structure_analysis as asa

    counter = {"n": 0}
    _fake_llm(monkeypatch, "- 建议一\n- 建议二", counter)
    assert asa.generate_industry_rules_llm(_lift_rows(), "装修", 40, 40, generate=False) is None
    assert counter["n"] == 0  # GET 自动加载路径不内联调 LLM


def test_v4_generate_parses_and_caches(monkeypatch):
    import services.article_structure_analysis as asa

    counter = {"n": 0}
    _fake_llm(monkeypatch, "- 开头200字先给结论\n- 补充本地价格区间\n- 加真实客户案例", counter)
    rules = asa.generate_industry_rules_llm(_lift_rows(), "装修", 40, 40, generate=True)
    assert rules == ["开头200字先给结论", "补充本地价格区间", "加真实客户案例"]
    assert counter["n"] == 1
    # 同指纹二次取(generate=False)→ 命中缓存,零 LLM
    assert asa.generate_industry_rules_llm(_lift_rows(), "装修", 40, 40, generate=False) == rules
    assert counter["n"] == 1


def test_v4_guard_blocks(monkeypatch):
    import services.article_structure_analysis as asa

    counter = {"n": 0}
    _fake_llm(monkeypatch, "- 保证上榜必被引", counter)  # 承诺话术
    assert asa.generate_industry_rules_llm(_lift_rows(), "装修", 40, 40, generate=True) is None


# ==================== V5 飞轮总汇总 insight ====================
def test_v5_empty_data_no_llm(monkeypatch):
    import services.flywheel_insight as fi

    counter = {"n": 0}
    _fake_llm(monkeypatch, '{"insights":["x"],"summary":"y"}', counter)
    monkeypatch.setattr(fi, "_collect_facts", lambda ind: {
        "industry_key": "all", "collected": 0, "learned": 0, "applied_versions": 0,
        "has_candidate_count": 0, "eligible_outcome_versions": 0, "board_state_counts": {},
    })
    out = fi.get_flywheel_insight(None)
    assert out["data_available"] is False
    assert out["insights"] == [] and out["generated"] is False
    assert counter["n"] == 0  # 空数据不调 LLM


def test_v5_with_data_generates_and_caches(monkeypatch):
    import services.flywheel_insight as fi

    counter = {"n": 0}
    _fake_llm(monkeypatch, '{"insights":["采集量上升","有2个候选待决策"],"summary":"飞轮转起来了"}', counter)
    facts = {"industry_key": "all", "collected": 100, "learned": 50, "pending_review": 2,
             "applied_versions": 1, "citation_rate": 0.3, "citation_rate_trend": 0.05, "trend_weeks": 4,
             "has_candidate_count": 2, "recommend_replace_count": 1, "board_state_counts": {"observing": 3},
             "eligible_outcome_versions": 1, "rollback_flags": 0, "health_can_activate": True,
             "health_blockers": 0, "health_summary": ""}
    monkeypatch.setattr(fi, "_collect_facts", lambda ind: facts)
    out = fi.get_flywheel_insight(None)
    assert out["data_available"] is True and out["generated"] is True
    assert out["insights"] == ["采集量上升", "有2个候选待决策"]
    assert out["summary"] == "飞轮转起来了"
    assert counter["n"] == 1
    # 同数据指纹二次调用 → 命中缓存,零 LLM
    out2 = fi.get_flywheel_insight(None)
    assert out2["cached"] is True and counter["n"] == 1
    # refresh=True 强制重跑
    out3 = fi.get_flywheel_insight(None, refresh=True)
    assert counter["n"] == 2 and out3["cached"] is False


def test_v5_parse_insights_failsoft():
    from services.flywheel_insight import _parse_insights

    assert _parse_insights("") == ([], "")
    assert _parse_insights("不是JSON随便乱写") == ([], "")
    ins, summ = _parse_insights('{"insights":["a","b","c","d","e","f"],"summary":"s"}')
    assert len(ins) == 5 and summ == "s"  # ≤5 截断


# ==================== V7 进程缓存 + 失效 ====================
def test_v7_get_or_compute_dedup():
    from writing.flywheel_cache import get_or_compute, make_key

    counter = {"n": 0}
    def compute():
        counter["n"] += 1
        return {"v": counter["n"]}
    key = make_key("board", "装修")
    a = get_or_compute(key, 60, compute)
    b = get_or_compute(key, 60, compute)
    assert a == b == {"v": 1} and counter["n"] == 1  # TTL 内只算一次


def test_v7_make_key_normalizes_none():
    from writing.flywheel_cache import make_key

    assert make_key("board", None) == "board:all"
    assert make_key("board", "") == "board:all"
    assert make_key("trends", 12) == "trends:12"
    assert make_key("panorama") == "panorama"


def test_v7_invalidate_scopes():
    from writing.flywheel_cache import get_or_compute, invalidate, make_key

    get_or_compute(make_key("board", "all"), 60, lambda: 1)
    get_or_compute(make_key("panorama"), 60, lambda: 2)
    get_or_compute(make_key("trends", 12), 60, lambda: 3)
    removed = invalidate(["board", "panorama"])
    assert removed == 2
    # trends 仍在
    counter = {"n": 0}
    def c():
        counter["n"] += 1
        return 3
    get_or_compute(make_key("trends", 12), 60, c)
    assert counter["n"] == 0  # trends 未被清,命中缓存


def test_v7_invalidate_control_derived_scope_selection():
    from writing.flywheel_cache import (
        CONTROL_DERIVED_SCOPES, SCOPE_BOARD, SCOPE_HEALTH, SCOPE_TRENDS, invalidate_control_derived,
        get_or_compute, make_key,
    )

    # 写动作应清 board,但不清 trends/health(它们靠 TTL,不受 control 写影响)
    assert SCOPE_BOARD in CONTROL_DERIVED_SCOPES
    assert SCOPE_TRENDS not in CONTROL_DERIVED_SCOPES
    assert SCOPE_HEALTH not in CONTROL_DERIVED_SCOPES
    get_or_compute(make_key(SCOPE_BOARD, "all"), 60, lambda: 1)
    get_or_compute(make_key(SCOPE_TRENDS, 12), 60, lambda: 2)
    invalidate_control_derived()
    # board 被清(重算),trends 保留
    bc = {"n": 0}; tc = {"n": 0}
    get_or_compute(make_key(SCOPE_BOARD, "all"), 60, lambda: bc.update(n=bc["n"] + 1) or 1)
    get_or_compute(make_key(SCOPE_TRENDS, 12), 60, lambda: tc.update(n=tc["n"] + 1) or 2)
    assert bc["n"] == 1  # board 重算
    assert tc["n"] == 0  # trends 命中缓存


# ==================== 出口审核修正回归 ====================
def test_guard_real_percent_passes_promise_blocks():
    """[C3] 真实统计 100% 放行;承诺语境 100%/上榜 拦截。"""
    from writing.flywheel_llm import guard_output

    # 真实统计:被采纳文章 100% 具备某特征 —— 放行
    assert guard_output("被采纳文章 100% 具备清单结构,对照组仅 30%")[1] is False
    assert guard_output("双评审 100% 一致")[1] is False
    # 承诺语境 —— 拦截
    assert guard_output("保证100%上榜")[1] is True
    assert guard_output("这样做几乎能保证被引")[1] is True
    assert guard_output("按此方向必定上榜")[1] is True


def test_guard_external_vendor_and_selfref_block():
    """[C2/C3] 外部厂商/模型自指拦截;监测引擎名放行。"""
    from writing.flywheel_llm import guard_output

    assert guard_output("个别段落像 GPT 模板腔")[1] is True   # gpt 裸串
    assert guard_output("结构接近 ChatGPT 高质量输出")[1] is True
    assert guard_output("作为一个AI语言模型我认为")[1] is True
    # 监测引擎名(GEO 目标平台)放行
    assert guard_output("豆包引擎采纳占比上升,deepseek/Kimi 引用增多")[1] is False
    assert guard_output("千问引擎表现稳定")[1] is False


def test_v2_clean_commentary_filters_guard():
    """[C2] 评语逐条过守卫:命中厂商/承诺的条目丢弃,一句话建议命中则置空。"""
    from services.writing_style_reviewer import _clean_commentary_list, _clean_commentary_text

    got = _clean_commentary_list(["结构更完整", "像 GPT 输出", "证据更充分"])
    assert got == ["结构更完整", "证据更充分"]  # 中间条被守卫丢弃
    assert _clean_commentary_text("保证100%被引") == ""   # 承诺 → 置空
    assert _clean_commentary_text("结构更清晰值得验证") == "结构更清晰值得验证"


def test_v7_cache_generation_drops_stale_fill_after_invalidate():
    """[C6] compute 期间发生 invalidate → 该次 stale 回填被丢弃(不写缓存)。"""
    import writing.flywheel_cache as fc

    fc.clear_all()
    calls = {"n": 0}

    def compute_then_invalidate():
        calls["n"] += 1
        # 模拟:慢 compute 期间另一处写动作触发了失效
        fc.invalidate([fc.SCOPE_BOARD])
        return {"stale": True}

    key = fc.make_key(fc.SCOPE_BOARD, "all")
    fc.get_or_compute(key, 60, compute_then_invalidate)
    # 回填应被丢弃 → 缓存里无此 key → 下次再 compute
    fc.get_or_compute(key, 60, lambda: {"fresh": True})
    assert calls["n"] == 1  # 第一次的 stale 未被缓存,第二次用了新 lambda(说明未命中旧值)


def test_v7_cache_generation_normal_fill_works():
    """[C6] 无失效时正常回填 + 命中(代际机制不影响正常路径)。"""
    import writing.flywheel_cache as fc

    fc.clear_all()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"v": calls["n"]}

    key = fc.make_key(fc.SCOPE_PANORAMA)
    a = fc.get_or_compute(key, 60, compute)
    b = fc.get_or_compute(key, 60, compute)
    assert a == b == {"v": 1} and calls["n"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ==================== 测试/审核 CTO 出口复审修正回归(2026-07-04)====================
def test_guard_domain_phrases_pass_promise_variants_block():
    """[review fix] 原承诺正则含单字「稳/包」与通用副词「一定」→ 本域高频合法话术被误拦;
    收紧为双字承诺组合并补 FN(确保/肯定能/引用率/比率式绝对承诺/全角%)。"""
    from writing.flywheel_llm import guard_output

    # 本域合法分析话术 —— 放行(原正则全被误拦,V3/V5 必然产出这类措辞)
    assert guard_output("样本包含被引文章 30 篇")[1] is False
    assert guard_output("数据包括收录与被引两类信号")[1] is False
    assert guard_output("有一定的被引提升,建议继续观察")[1] is False
    assert guard_output("表现稳定,被引率略升")[1] is False
    assert guard_output("近12周有一定收录增长")[1] is False
    # 补漏的承诺变体 —— 拦截(原正则放行)
    assert guard_output("确保上榜")[1] is True
    assert guard_output("肯定能被引")[1] is True
    assert guard_output("100%提升引用率")[1] is True
    assert guard_output("引用率可达100%")[1] is True
    assert guard_output("百分之百有效")[1] is True
    assert guard_output("保证100％上榜")[1] is True  # 全角%


def _insight_facts(collected: int) -> dict:
    return {"industry_key": "all", "collected": collected, "learned": 50, "pending_review": 2,
            "applied_versions": 1, "citation_rate": 0.3, "citation_rate_trend": 0.05, "trend_weeks": 4,
            "has_candidate_count": 2, "recommend_replace_count": 1, "board_state_counts": {"observing": 3},
            "eligible_outcome_versions": 1, "rollback_flags": 0, "health_can_activate": True,
            "health_blockers": 0, "health_summary": ""}


def test_v5_per_item_guard_keeps_clean_items(monkeypatch):
    """[review fix] 逐条守卫:1 条违规只丢该条,其余保留(整段守卫会拖死全部 insight)。"""
    import services.flywheel_insight as fi

    fi.clear_insight_cache()
    counter = {"n": 0}
    _fake_llm(monkeypatch, '{"insights":["确保上榜没问题","采集量上升明显"],"summary":"整体健康"}', counter)
    monkeypatch.setattr(fi, "_collect_facts", lambda ind: _insight_facts(201))
    out = fi.get_flywheel_insight(None)
    assert out["insights"] == ["采集量上升明显"]
    assert out["generated"] is True and counter["n"] == 1


def test_v5_all_blocked_negative_cache_no_reburn(monkeypatch):
    """[review fix] 守卫全拦/生成失败 → 负缓存(短 TTL):GET 冷缓存路径不再每次页面加载重烧 LLM;
    refresh=True 仍可随时强制重试。"""
    import services.flywheel_insight as fi

    fi.clear_insight_cache()
    counter = {"n": 0}
    _fake_llm(monkeypatch, '{"insights":["确保上榜","保证被引"],"summary":"必定上榜"}', counter)
    monkeypatch.setattr(fi, "_collect_facts", lambda ind: _insight_facts(303))
    out = fi.get_flywheel_insight(None)
    assert out["insights"] == [] and out["generated"] is False
    assert counter["n"] == 1
    out2 = fi.get_flywheel_insight(None)
    assert out2["cached"] is True and counter["n"] == 1  # 负缓存命中,零 LLM
    fi.get_flywheel_insight(None, refresh=True)
    assert counter["n"] == 2  # 手动强刷可重试
