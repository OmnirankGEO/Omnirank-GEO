"""#185 c2 判据 · 契约层 —— 防御型(公司词)是**对外方向**,不是第七个家族。

本文件不碰库(纯逻辑),分三组:
  ① 题面:八问逐字、品牌名必入、容量不凑数;
  ② 契约:落库值不折叠、正文家族落到 company_facts、家族注册表仍是六个;
  ③ 系统推荐配比**零扰动** —— 与 PROD_TIP 5784baa9e 实测值逐字节比。

🔴 第 ③ 组是本单最贵的一格:配比一被扰动,**每个存量项目**的推荐篇数都变,
   而且不报错。所以它不是"断言键集对",是**冻结基线**逐字节比。
"""
import pytest

from writing.article_style_contract import (
    OUTWARD_CHOICE_TO_FAMILY,
    STYLE_FAMILIES,
    USER_CHOICE_OPTIONS,
    USER_CHOICE_SELECTABLE,
    USER_CHOICE_SELECTABLE_LABELS,
    family_for_user_choice,
    generation_style_for_user_choice,
    normalize_user_choice,
    validate_style_contract,
)
from writing.defensive_questions import (
    DEFENSIVE_QUESTIONS,
    UNCONVERTED_REASONS,
    defensive_capacity,
    defensive_titles,
    facts_hint_for,
    plan_defensive_titles,
    title_carries_brand,
)

BRAND = "康之康"


# ════════════════════════════════════════════════════════════════
# ① 题面
# ════════════════════════════════════════════════════════════════
def test_eight_questions_are_verbatim_and_ordered():
    """八问逐字取自 WO_185 §6,**顺序即出题顺序**。

    🔴 第六问是「适合谁·不适合谁」不是「适合谁」:转述里丢过后半句,
       而前端徽章按问名匹配,差半句就对不上 —— 而那种对不上只表现为
       "徽章有时候不显示",没人会把它归因到少了三个字。
    """
    assert len(DEFENSIVE_QUESTIONS) == 8
    assert DEFENSIVE_QUESTIONS[0] == "怎么样"
    assert DEFENSIVE_QUESTIONS[5] == "适合谁·不适合谁"
    assert DEFENSIVE_QUESTIONS[7] == "团队·流程"
    assert len(set(DEFENSIVE_QUESTIONS)) == 8, "八问有重复"


def test_first_eight_titles_cover_every_question_once_and_carry_the_brand():
    """WO C1′ 主臂:前 8 条覆盖八问各一、无重复、每条含品牌名。"""
    out = defensive_titles(BRAND, 8)
    assert len(out) == 8
    assert [r["question"] for r in out] == list(DEFENSIVE_QUESTIONS)
    assert len({r["title"] for r in out}) == 8, "出现了重复标题"
    for r in out:
        assert title_carries_brand(r["title"], BRAND), (
            "标题 %r 没有品牌名 —— 没有主语就不是公司词" % r["title"])


def test_titles_beyond_eight_use_variants_and_never_pad():
    """>8 走变体,**变体用完就停**。

    凑出来的重复题会让客户看到两条几乎一样的标题,那比少两篇更难解释 ——
    所以宁可少给。这条钉的是「不循环回第一问凑满 n」。
    """
    cap = defensive_capacity()
    assert defensive_titles(BRAND, cap + 3) == defensive_titles(BRAND, cap)
    out = defensive_titles(BRAND, cap)
    assert len(out) == cap
    assert len({r["title"] for r in out}) == cap, "凑数了:出现重复标题"
    for r in out:
        assert title_carries_brand(r["title"], BRAND)


def test_capacity_is_derived_not_hardcoded():
    """容量 = 八问 + 变体,由**列表长度**算。

    前端「最多 N 篇」与配比封顶都读它。两边各写死一个 14,
    加一个变体就会对不上 —— 而对不上只表现为"配比里能选 15、实际出 14",
    没有任何东西会红。
    """
    assert defensive_capacity() == len(defensive_titles(BRAND, 999))


def test_no_brand_name_yields_nothing():
    """没有品牌名就没有「公司词」。返空,而不是出一条没有主语的题。"""
    assert defensive_titles("", 8) == []
    assert defensive_titles("   ", 8) == []
    assert defensive_titles(None, 8) == []


# ════════════════════════════════════════════════════════════════
# ① b 容量合同:超出的篇必须被**点名**
# ════════════════════════════════════════════════════════════════
def test_overflow_and_fixed_slots_are_named_not_silently_dropped():
    """17 篇(含 1 个固定槽)⇒ 14 转 + 1 fixed_slot + 2 capacity_exceeded。

    🔴 「静默换成普通题」是本单最危险的坏法:「全部设为防御型」就成了假话,
       而用户从列表上看不出来(标题本来就各不相同)。所以未转换的必须**逐篇点名**。
    """
    topics = [{"id": i} for i in range(1, 18)]
    topics[2]["is_fixed"] = True          # id=3 是系统固定槽
    applied, unconverted = plan_defensive_titles(BRAND, topics)

    assert len(applied) == defensive_capacity() == 14
    assert len(applied) + len(unconverted) == 17, "有篇既没转也没被点名 —— 静默丢了"
    reasons = {r["topic_id"]: r["reason"] for r in unconverted}
    assert reasons[3] == "fixed_slot", "固定槽被动了(Review 09-13 裁定不动它)"
    assert sorted(k for k, v in reasons.items() if v == "capacity_exceeded") == [16, 17]
    assert {r["reason"] for r in unconverted} <= set(UNCONVERTED_REASONS)


def test_fixed_slot_never_consumes_capacity():
    """反向对照:固定槽被跳过,**不占**容量。

    少了它,「把固定槽也算进 eligible 再丢掉」也能让上面那条绿,
    而那会让实际转成防御的篇数少一篇。
    """
    plain = [{"id": i} for i in range(1, 15)]
    with_fixed = [{"id": 0, "is_fixed": True}] + plain
    assert len(plan_defensive_titles(BRAND, plain)[0]) == 14
    assert len(plan_defensive_titles(BRAND, with_fixed)[0]) == 14


def test_no_brand_name_names_every_topic_as_not_retitlable():
    applied, unconverted = plan_defensive_titles("", [{"id": 1}, {"id": 2}])
    assert applied == []
    assert [r["reason"] for r in unconverted] == ["not_retitlable"] * 2


# ════════════════════════════════════════════════════════════════
# ② 契约:落库值不折叠 / 正文走 company_facts / 家族仍是六个
# ════════════════════════════════════════════════════════════════
def test_user_choice_persists_unfolded():
    """Review 判据①:`topics.user_choice` 落 `defensive_company`,**不折成家族别名**。

    🔴 折了的话它与普通 company_facts 篇逐字相同,而 `style_code` 本来就相同
       (共用 brand_softarticle)—— 两个可区分的列都相同之后,
       「哪些篇是防御型」就**永久无法恢复**。
    """
    assert normalize_user_choice("defensive_company", allow_auto=False) == "defensive_company"
    assert normalize_user_choice("defensive_company") == "defensive_company"


def test_body_contract_falls_back_to_company_facts():
    """正文家族 = company_facts;生成样式与普通 company_facts 篇**逐字相同**。

    相同是**有意的**(共用「介绍公司」六段模板),也正因为相同,
    side 不能由 style_code 派生 —— 见 `test_direction_is_not_derivable_from_style_code`。
    """
    assert family_for_user_choice("defensive_company") == "company_facts"
    assert (generation_style_for_user_choice("defensive_company")
            == generation_style_for_user_choice("company_facts")
            == "brand_softarticle")


def test_direction_is_not_derivable_from_style_code():
    """Review 判据②的契约侧:两个方向的 style_code 相同 ⇒ 必须另有区分依据。

    这条不是"测某个函数",是把**为什么需要 user_choice** 钉下来:
    哪天有人"简化"成按 style_code 派生,这条会红并且解释原因。
    """
    assert (generation_style_for_user_choice("defensive_company")
            == generation_style_for_user_choice("company_facts"))
    assert normalize_user_choice("defensive_company") != normalize_user_choice("company_facts")


def test_defensive_is_not_a_seventh_family():
    """家族注册表仍是**六个**。

    🔴 多一个键,`FAMILY_LENGTH_POLICIES` / 家族提示词 / 标题年份规则 / 规格卡
       就各缺一份 —— 缺的那份**不报错**,只是让防御篇用默认合同生成,
       正好不是「介绍公司」六段。(2026-09-13 实测:c1 曾经做成家族,
       当场被十条既有判据抓住。)
    """
    assert "defensive_company" not in STYLE_FAMILIES
    assert len(STYLE_FAMILIES) == 6
    assert USER_CHOICE_OPTIONS == frozenset(("auto", *STYLE_FAMILIES))
    assert "defensive_company" not in USER_CHOICE_OPTIONS


def test_selectable_is_strictly_wider_than_options():
    """能选的 ⊋ 配比域。两个集合分开是**故意的**,不是冗余。"""
    assert USER_CHOICE_SELECTABLE > USER_CHOICE_OPTIONS
    assert USER_CHOICE_SELECTABLE - USER_CHOICE_OPTIONS == {"defensive_company"}
    assert set(USER_CHOICE_SELECTABLE_LABELS) == USER_CHOICE_SELECTABLE - {"auto"}
    assert USER_CHOICE_SELECTABLE_LABELS["defensive_company"] == "防御型(公司词)"


def test_contract_self_validation_is_clean():
    """合同自洽校验必须无错 —— 它是这个新概念唯一的门。"""
    assert validate_style_contract() == []


def test_family_lookup_for_legacy_styles_was_not_hijacked():
    """反向对照:两个历史样式仍归 company_facts。

    c1 差一点在这里出事:新家族若声明 `allowed_legacy_styles`,
    `LEGACY_STYLE_TO_FAMILY` 那个**后者覆盖前者**的字典推导会把
    `brand_softarticle` 从 company_facts 翻走,存量文章的徽章 / 配比 /
    效果报表 / 飞轮聚合全部被静默重新分类。
    """
    from writing.article_style_contract import family_for_style
    assert family_for_style("brand_softarticle") == "company_facts"
    assert family_for_style("company_profile") == "company_facts"


# ════════════════════════════════════════════════════════════════
# ③ 系统推荐配比零扰动(冻结基线)
# ════════════════════════════════════════════════════════════════
#: 🔴 取自 **PROD_TIP 5784baa9e 实测**(把本单改过的四个文件按字节退回该尖,
#:    跑同一组用例得到)。不是我在改后的树上抄的数 ——
#:    在改后的树上抄,等于用被测对象给自己出考题。
FROZEN_MIX_ON_PROD_TIP = [
    (("教育培训", 17), {"case_data_roi": 1, "company_facts": 1, "evidence_qa": 3,
                        "implementation_guide": 2, "multi_brand_comparison": 9,
                        "trend_policy_risk": 1}),
    (("教育培训", 6), {"case_data_roi": 1, "company_facts": 0, "evidence_qa": 1,
                       "implementation_guide": 1, "multi_brand_comparison": 3,
                       "trend_policy_risk": 0}),
    (("医疗健康", 12), {"case_data_roi": 1, "company_facts": 0, "evidence_qa": 2,
                        "implementation_guide": 2, "multi_brand_comparison": 3,
                        "trend_policy_risk": 4}),
    ((None, 9), {"case_data_roi": 1, "company_facts": 0, "evidence_qa": 1,
                 "implementation_guide": 1, "multi_brand_comparison": 5,
                 "trend_policy_risk": 1}),
    (("法律商务", 20), {"case_data_roi": 2, "company_facts": 1, "evidence_qa": 3,
                        "implementation_guide": 4, "multi_brand_comparison": 4,
                        "trend_policy_risk": 6}),
]


@pytest.mark.parametrize("case, expected", FROZEN_MIX_ON_PROD_TIP,
                         ids=[f"{i}-{n}" for (i, n), _ in FROZEN_MIX_ON_PROD_TIP])
def test_recommended_mix_is_byte_identical_to_prod_tip(case, expected):
    """Review 判据③:系统推荐配比与基线**逐字节同**。

    🔴 这是本单最贵的一格。配比被扰动 ⇒ **每个存量项目**的推荐篇数都变,
       而且不报错、不红、用户只觉得"推荐好像跟以前不一样"。
       所以比的是整个 dict,不是"键集对"。
    """
    from writing.direction_distribution import compute_recommended_user_choice_distribution
    industry, count = case
    assert compute_recommended_user_choice_distribution(industry, count) == expected


def test_defensive_has_zero_weight_in_the_recommended_mix():
    """防御型**不在推荐配比的定义域里** —— 要用必须用户显式点。"""
    from writing.direction_distribution import compute_recommended_user_choice_distribution
    for (industry, count), _ in FROZEN_MIX_ON_PROD_TIP:
        got = compute_recommended_user_choice_distribution(industry, count)
        assert "defensive_company" not in got
        assert set(got) == set(STYLE_FAMILIES)


def test_custom_distribution_accepts_defensive_but_caps_it():
    """反向对照:配比**校验**认它(能选),配比**推荐**不认它(零权重)。

    一个用 SELECTABLE、一个用 OPTIONS,用错哪个都不报错:
      · 校验用窄的 -> 用户选了被 400 拒,而前端明明给了这个选项;
      · 推荐用宽的 -> 存量项目的推荐篇数全变。
    """
    from writing.direction_distribution import (
        DistributionValidationError,
        is_user_choice_option,
        validate_user_choice_distribution,
    )
    assert is_user_choice_option("defensive_company") is True

    cap = defensive_capacity()
    validate_user_choice_distribution(
        {"defensive_company": cap, "evidence_qa": 1},
        configurable_count=cap + 1, industry="教育培训")

    with pytest.raises(DistributionValidationError) as e:
        validate_user_choice_distribution(
            {"defensive_company": cap + 1, "evidence_qa": 1},
            configurable_count=cap + 2, industry="教育培训")
    msg = str(e.value)
    assert str(cap) in msg and "防御型" in msg
    for jargon in ("user_choice", "distribution", "capacity", "defensive_company"):
        assert jargon not in msg, "报错里出现了工程词 %r(元指令第 11 条)" % jargon


def test_unknown_direction_is_still_rejected():
    """反向对照:放宽的只有防御型这一个值,别的照旧拒。"""
    from writing.direction_distribution import (
        DistributionValidationError, validate_user_choice_distribution)
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution(
            {"ranking_v2": 1}, configurable_count=1, industry="教育培训")


# ════════════════════════════════════════════════════════════════
# ④ 缺事实提示(c5)
# ════════════════════════════════════════════════════════════════
def test_facts_hint_names_real_columns_only():
    """提示里点名的栏位必须是 `client_profiles` **真有**的列。

    🔴 工单只写了「来源 client_profile / 知识库 / brands 字段」,没点到列。
       照转述写会出现库里根本没有的字段名 —— 那种提示永远为「缺」,
       用户补不了也看不懂。这条按生产 schema 核。
    """
    import io as _io
    import pathlib
    from writing.defensive_questions import QUESTION_FACT_FIELDS

    dump = pathlib.Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
    text = _io.open(dump, encoding="utf-8").read()
    block = text.split("CREATE TABLE public.client_profiles (", 1)[1].split(");", 1)[0]
    cols = {ln.strip().split(" ")[0].strip(",") for ln in block.splitlines() if ln.strip()}
    for question, fields in QUESTION_FACT_FIELDS.items():
        for f in fields:
            assert f in cols, "「%s」点名了 client_profiles 里没有的列 %r" % (question, f)


def test_facts_hint_is_plain_language_and_never_blocks():
    """有事实就不提示;没事实给人话,并且**只提示不阻断**。"""
    facts = {"company_intro": "我们是一家…", "success_cases": "案例 A"}
    ok = facts_hint_for("怎么样", facts)
    assert ok["has_facts"] is True and ok["hint"] == ""

    miss = facts_hint_for("靠谱吗·口碑", facts)
    assert miss["has_facts"] is False
    assert "客户评价" in miss["hint"] and "testimonials" not in miss["hint"]

    # 没有结构化列的两问走另一句(不是"缺事实",是"没这栏")
    none_col = facts_hint_for("价格·收费", facts)
    assert none_col["missing"] == [] and "品牌知识库" in none_col["hint"]


def test_every_question_has_a_hint_rule():
    """八问**逐问**都要有规则 —— 少一问就是那一问永远没有提示,而不会红。"""
    from writing.defensive_questions import QUESTION_FACT_FIELDS
    assert set(QUESTION_FACT_FIELDS) == set(DEFENSIVE_QUESTIONS)
    for q in DEFENSIVE_QUESTIONS:
        assert facts_hint_for(q, {})["hint"], "「%s」没有任何提示文案" % q


def test_blank_strings_do_not_count_as_facts():
    """反向对照:空串 / 空白不算有事实。

    少了它,「列存在即算有」也能让主臂绿 —— 而空字段正是最常见的情况。
    """
    assert facts_hint_for("怎么样", {"company_intro": "   "})["has_facts"] is False
    assert facts_hint_for("怎么样", {"company_intro": ""})["has_facts"] is False
    assert facts_hint_for("怎么样", {"company_intro": "有内容"})["has_facts"] is True
