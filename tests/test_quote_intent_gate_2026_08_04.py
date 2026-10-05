"""报价意图闸回归测试(WO-QUOTE-INTENT-GATE-2026-08-04)。

守的是这个事故:生产会话 194(brand_id=679)六个词全是知识科普题,
照样出价 ¥14,677(入门 8,081 / 标准 14,677 / 旗舰 21,715)。
用户问 AI「…就业率真实吗」时,AI 的回答里不会出现"推荐哪家机构"——
做 GEO 拿不到推荐位,等于卖了个不可能兑现的东西。

本文件不依赖 DB:被测逻辑全在 services/quote_intent_gate.py(纯函数),
`tests/conftest.py` 需要 TEST_DATABASE_URL 时也能靠 `--noconftest` 单跑。

每条正向断言都配一条**必须不命中**的反向对照 —— 只有正向断言的测试
无法区分"闸生效"与"判据恒真"。
"""

import pytest

from services.quote_intent_gate import (
    GATE_ACTIVE,
    GATE_UNAVAILABLE,
    attach_policy_exclusions,
    build_nothing_quotable_message,
    describe_policy_exclusion,
    nothing_quotable_error,
    partition_by_commercial_policy,
    policy_excluded_markdown,
    suggest_commercial_candidates,
)

BRAND = "贵州AI数字技能就业实训基地"

# ---- 生产会话 194 的真实数据(原样照抄,别改成"看起来像"的词)----
# 进了定价并出了价的 6 个词 —— 全部必须被剔
SESSION_194_PRICED_KNOWLEDGE = [
    "AI就业实训基地就业率真实吗",
    "AI数字人才培训就业率高吗",
    "AI就业实训基地提供简历面试辅导吗",
    "AI数字技能就业实训基地案例分享",
    "AI就业实训基地有企业合作吗",
    "AI数字人才培训项目实战多吗",
]
# 躺在候选池里却一个都没进定价的商业词 —— 全部必须放行
SESSION_194_POOL_COMMERCIAL = [
    "AI数字技能就业实训基地哪家靠谱",
    "零基础转行AI就业培训班推荐",
    "AI就业实训和自学哪个好",
    "AI技能培训就业指导机构推荐",
]

SESSION_194_SNAPSHOT = [
    {"id": 1, "keyword": "AI数字技能就业实训基地哪家靠谱", "category_label": "考察"},
    {"id": 2, "keyword": "零基础转行AI就业培训班推荐", "category_label": "认知"},
    {"id": 4, "keyword": "AI数字人才培训就业率高吗", "category_label": "通用"},
    {"id": 5, "keyword": "AI就业实训基地怎么选不会踩坑", "category_label": "考察"},
    {"id": 6, "keyword": "AI就业实训和自学哪个好", "category_label": "对比"},
    {"id": 7, "keyword": "AI技能培训就业指导机构推荐", "category_label": "认知"},
    {"id": 8, "keyword": "AI就业实训基地有企业合作吗", "category_label": "通用"},
    {"id": 10, "keyword": "AI就业实训基地就业率真实吗", "category_label": "通用"},
]


# ============================================================
# 1. 核心切分 —— 正反两向
# ============================================================

@pytest.mark.parametrize("keyword", SESSION_194_PRICED_KNOWLEDGE)
def test_session194_knowledge_keywords_are_excluded(keyword):
    """194 里出了价的 6 个知识题,一个都不许再进报价。"""
    quotable, excluded, status = partition_by_commercial_policy([keyword], brand_name=BRAND)
    assert status == GATE_ACTIVE
    assert quotable == [], f"「{keyword}」不该进报价"
    assert [e["keyword"] for e in excluded] == [keyword]


@pytest.mark.parametrize("keyword", SESSION_194_POOL_COMMERCIAL)
def test_session194_commercial_keywords_are_kept(keyword):
    """反向对照:候选池里的商业词必须放行。

    没有这一组,上面那组无法区分"闸判得准"和"闸把什么都剔了"。
    """
    quotable, excluded, status = partition_by_commercial_policy([keyword], brand_name=BRAND)
    assert status == GATE_ACTIVE
    assert quotable == [keyword], f"「{keyword}」是商业词,不该被剔"
    assert excluded == []


def test_session194_full_selection_leaves_only_the_commercial_word():
    """整单复现:194 实际勾选的 7 个词过闸后只剩「哪家靠谱」。"""
    selected = SESSION_194_PRICED_KNOWLEDGE + [
        "AI数字技能就业实训基地哪家靠谱",
        "AI就业实训基地怎么选不会踩坑",  # 引擎判 uncertain(澄清前不得计价)
    ]
    quotable, excluded, _ = partition_by_commercial_policy(selected, brand_name=BRAND)
    assert quotable == ["AI数字技能就业实训基地哪家靠谱"]
    assert len(excluded) == 7


# ============================================================
# 2. 品牌词豁免 —— 含"去掉豁免就会误剔"的反向对照
# ============================================================

def test_brand_keyword_is_protected():
    """品牌名内部带空格时引擎判定会分叉,必须靠 protected 放行。

    这是 CTO-15.23 修过一次的 QZQZ 爆价案同款词形,不能再踩。
    """
    brand = "QZQZ 美学定制"          # 注意内部空格
    kw = "QZQZ美学定制怎么样"          # 无空格写法
    quotable, excluded, _ = partition_by_commercial_policy(
        [kw], brand_name=brand, protected_keywords={kw},
    )
    assert quotable == [kw]
    assert excluded == []


def test_brand_protection_has_discriminating_power():
    """反向对照:不传 protected 时同一个词**必须**被剔。

    若这条不成立,说明 protected 参数是摆设,上面那条测试恒绿。
    """
    brand = "QZQZ 美学定制"
    kw = "QZQZ美学定制怎么样"
    quotable, excluded, _ = partition_by_commercial_policy(
        [kw], brand_name=brand, protected_keywords=set(),
    )
    assert quotable == []
    assert [e["keyword"] for e in excluded] == [kw]


def test_brand_direct_question_not_excluded_when_names_align():
    """品牌名无内部空格时,引擎自己就判 brand_direct,不靠 protected 也放行。"""
    quotable, excluded, _ = partition_by_commercial_policy(
        [f"{BRAND}靠谱吗"], brand_name=BRAND,
    )
    assert quotable == [f"{BRAND}靠谱吗"]
    assert excluded == []


# ============================================================
# 3. 边界与降级
# ============================================================

def test_empty_input():
    assert partition_by_commercial_policy([], brand_name=BRAND) == ([], [], GATE_ACTIVE)


def test_all_excluded_returns_empty_quotable_not_crash():
    quotable, excluded, status = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    assert quotable == []
    assert len(excluded) == len(SESSION_194_PRICED_KNOWLEDGE)
    assert status == GATE_ACTIVE


def test_engine_unavailable_fails_open_but_says_so(monkeypatch):
    """引擎不可用 → 放行全部词,但 gate_status 必须变,不许假装核验过。"""
    import builtins
    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "services.commercial_query_policy":
            raise ImportError("simulated outage")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    quotable, excluded, status = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    assert status == GATE_UNAVAILABLE
    assert quotable == SESSION_194_PRICED_KNOWLEDGE   # 不瘫痪
    assert excluded == []


def test_per_keyword_error_does_not_kill_the_keyword(monkeypatch):
    """单个词判定抛错 → 按可报处理(不误杀),其余词照常裁决。"""
    import services.quote_intent_gate as gate
    import services.commercial_query_policy as policy
    real_eval = policy.evaluate

    def flaky(text, **kwargs):
        if text == "AI就业实训基地就业率真实吗":
            raise ValueError("simulated per-keyword failure")
        return real_eval(text, **kwargs)

    monkeypatch.setattr(policy, "evaluate", flaky)
    quotable, excluded, status = partition_by_commercial_policy(
        ["AI就业实训基地就业率真实吗", "AI数字人才培训就业率高吗"], brand_name=BRAND,
    )
    assert status == GATE_ACTIVE
    assert "AI就业实训基地就业率真实吗" in quotable          # 抛错的不误杀
    assert [e["keyword"] for e in excluded] == ["AI数字人才培训就业率高吗"]


# ============================================================
# 4. 人话原因(禁术语泄漏)
# ============================================================

@pytest.mark.parametrize("intent_type", ["knowledge", "seo_fragment", "uncertain", "什么鬼"])
def test_reason_text_is_plain_chinese_without_jargon(intent_type):
    text = describe_policy_exclusion(intent_type)
    assert text
    for jargon in ("knowledge", "seo_fragment", "uncertain", "policy", "intent", "SOV"):
        assert jargon not in text, f"原因文案泄漏工程术语: {jargon}"


# ============================================================
# 5. markdown(对客交付物不许静默缺词)
# ============================================================

def test_markdown_lists_every_excluded_keyword():
    _, excluded, _ = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    md = policy_excluded_markdown(excluded)
    for kw in SESSION_194_PRICED_KNOWLEDGE:
        assert kw in md
    assert "不计费" in md


def test_markdown_empty_when_nothing_excluded():
    """反向对照:没剔词时不许平白多出一段(否则每份报价单都挂个空区块)。"""
    assert policy_excluded_markdown([]) == ""


def test_markdown_says_unverified_on_degrade():
    md = policy_excluded_markdown([], gate_status=GATE_UNAVAILABLE)
    assert "未能完成" in md and "核验" in md


# ============================================================
# 6. 候选池建议(提示要么帮人解决,要么不显示)
# ============================================================

def test_suggestions_only_return_commercial_pool_words():
    suggestions = suggest_commercial_candidates(
        SESSION_194_SNAPSHOT, already_quoted=[], brand_name=BRAND,
    )
    names = [s["keyword"] for s in suggestions]
    for kw in SESSION_194_POOL_COMMERCIAL:
        assert kw in names, f"候选池里的商业词「{kw}」该被推荐"
    for kw in SESSION_194_PRICED_KNOWLEDGE:
        assert kw not in names, f"知识题「{kw}」不该出现在建议里"


def test_suggestions_exclude_already_used_words():
    """反向对照:已经用掉的词不再重复建议。"""
    used = ["AI数字技能就业实训基地哪家靠谱"]
    suggestions = suggest_commercial_candidates(
        SESSION_194_SNAPSHOT, already_quoted=used, brand_name=BRAND,
    )
    assert used[0] not in [s["keyword"] for s in suggestions]


def test_suggestions_empty_pool():
    assert suggest_commercial_candidates([], already_quoted=[], brand_name=BRAND) == []


# ============================================================
# 7. 只读展示区:被剔词绝不进计价明细
# ============================================================

def test_attach_puts_excluded_outside_priced_keywords():
    _, excluded, _ = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    engine_output = {"policy_excluded_keywords": excluded, "policy_gate_status": GATE_ACTIVE}
    pricing_data = {
        "keywords": [{"keyword": "AI数字技能就业实训基地哪家靠谱",
                      "standard": {"price": 2000, "articles": 5}}],
        "tiers": {"standard": {"total_price": 2000}},
    }
    attach_policy_exclusions(pricing_data, engine_output, SESSION_194_SNAPSHOT, brand_name=BRAND)

    priced_names = {k["keyword"] for k in pricing_data["keywords"]}
    for kw in SESSION_194_PRICED_KNOWLEDGE:
        assert kw not in priced_names, f"被剔词「{kw}」漏进了计价明细"
    assert len(pricing_data["excluded_keywords"]) == len(SESSION_194_PRICED_KNOWLEDGE)
    assert pricing_data["suggested_keywords"], "剔了词就该给出可补的商业词"
    # 总价只由计价明细求和 → 结构上不可能含被剔词
    assert pricing_data["tiers"]["standard"]["total_price"] == 2000


def test_attach_without_exclusions_adds_no_suggestions():
    """反向对照:没剔词时不挂建议区(不制造无意义提示)。"""
    pricing_data = {"keywords": [], "tiers": {}}
    attach_policy_exclusions(pricing_data, {"policy_excluded_keywords": []},
                             SESSION_194_SNAPSHOT, brand_name=BRAND)
    assert pricing_data["excluded_keywords"] == []
    assert "suggested_keywords" not in pricing_data


# ============================================================
# 8. 零可报词:给动作,不落 ¥0 报价单
# ============================================================

def test_nothing_quotable_raises_with_actionable_hint():
    _, excluded, _ = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    err = nothing_quotable_error(
        [], {"policy_excluded_keywords": excluded}, SESSION_194_SNAPSHOT, brand_name=BRAND,
    )
    assert err is not None
    assert err.status_code == 422
    # 必须"帮人解决":点名候选池里现成可用的商业词
    assert "AI数字技能就业实训基地哪家靠谱" in err.detail


def test_nothing_quotable_silent_when_something_remains():
    """反向对照:还有可报词时绝不阻断(Owner 拍板:词数不够照常报价)。"""
    _, excluded, _ = partition_by_commercial_policy(
        SESSION_194_PRICED_KNOWLEDGE, brand_name=BRAND,
    )
    err = nothing_quotable_error(
        [{"keyword": "AI数字技能就业实训基地哪家靠谱"}],
        {"policy_excluded_keywords": excluded}, SESSION_194_SNAPSHOT, brand_name=BRAND,
    )
    assert err is None


def test_nothing_quotable_silent_when_gate_excluded_nothing():
    """反向对照:不是意图闸造成的空(如全断供)不由本闸接管,免得盖掉 503 的真因。"""
    assert nothing_quotable_error([], {"policy_excluded_keywords": []},
                                  SESSION_194_SNAPSHOT) is None
    assert nothing_quotable_error([], {}, SESSION_194_SNAPSHOT) is None


def test_nothing_quotable_message_without_pool_suggestions():
    msg = build_nothing_quotable_message([{"keyword": "x"}], [])
    assert "哪家靠谱" in msg   # 没有现成词时给出该怎么写的范例
