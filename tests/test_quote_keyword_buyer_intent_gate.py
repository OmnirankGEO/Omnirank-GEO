"""Regression coverage for quote keyword buyer-intent enforcement."""

from pathlib import Path

import pytest

from tools.keyword_expander import KeywordExpander


NON_DECISION_KEYWORDS = [
    "AI就业实训基地有企业合作吗",
    "AI数字人才培训项目实战多吗",
    "AI就业实训基地提供简历面试辅导吗",
    "AI数字技能就业实训基地案例分享",
    "AI数字人才培训就业率高吗",
    "AI就业实训基地就业率真实吗",
    "AI数字人才培训就业率对比分析",
    "AI就业实训行业发展趋势",
    "AI培训课程价格政策解读",
    "AI培训课程费用报销流程",
    "B2B营销服务价格趋势",
    "B2B营销项目报价流程",
    "工业机器人价格走势",
    "工业设备采购预算怎么做",
    "培训机构价格是什么意思",
    "项目预算怎么做",
    "不推荐AI就业实训机构的原因",
    "AI就业实训机构排名真实吗",
    "AI就业实训价格政策有哪些",
    "AI就业实训服务价格走势",
    "AI培训机构选择标准",
    # ---- SSOT geo-commercial-intent-governance-v1.0 §六.2 强制排除 ----
    "电梯是什么",
    "AI行业未来趋势",
    "就业率真实吗",
    "激光设备",
    "GEO和SEO有什么区别",
    "工业机器人选型标准",
    "B2B营销服务商评估标准",
    "工业机器人厂家资质要求",
    "B2B营销服务商有成功案例吗",
    "深圳装修公司有别墅案例吗",
    "AI就业实训设备怎么选",
    "ERP服务商有哪些选择标准",
    "工业机器人厂家有哪些资质要求",
    "ERP系统排名算法",
    "CRM报价怎么计算",
    "ERP价格由什么组成",
    "工业机器人供应商对比方法",
    "不同装修案例对比",
    "工业机器人国家标准对比",
    "AI培训效果真实性对比",
    "装修服务收费包含什么",
    "AI培训机构成功案例",
]

BUYER_DECISION_KEYWORDS = [
    "AI就业实训机构排名前十",
    "有企业合作案例的AI就业实训机构推荐",
    "深圳AI就业实训机构哪家好",
    "AI数字人才培训项目报价",
    "AI就业实训服务商对比",
    "工业机器人供应商对比分析",
    "ERP供应商对比分析",
    "B2B营销服务商有哪些",
    "工业机器人厂家有哪些",
    "有医药行业案例的ERP供应商有哪些",
    "AI就业实训供应商名录",
    "AI就业实训平台哪个值得买",
    "AI就业实训课程买哪一个",
    "有靠谱的电梯维修公司吗",
    "有预算内的ERP服务商吗",
    "工业机器人哪个型号好",
    "哪款CRM适合小团队",
    "制造企业用什么ERP系统",
    "适合小团队的CRM产品有哪些",
    "制造企业ERP方案有哪些",
    "有ISO资质的ERP供应商有哪些",
    "不同装修公司对比",
    "工业机器人供应商对比",
    "AI培训机构对比",
    "装修服务多少钱",
    "有案例的AI培训机构推荐",
    "AI培训机构对比",
    "深圳埃尔法长租包月",
    # ---- SSOT geo-commercial-intent-governance-v1.0 §3.1 · 2026-07-23 ----
    # "怎么选/如何挑选"是合法选型问题(答案会出现具体机构/服务商),
    # 原一刀切拒绝规则已废止(见归档索引 A);带供给方名词即放行。
    "AI就业实训机构如何挑选",
    "AI培训机构怎么选",
    # ---- §六.1 强制商业通过矩阵 ----
    "深圳电梯厂家哪家靠谱",
    "预算3000元以内GEO服务商怎么选",
    "细胞治疗隔离器品牌对比与采购建议",
    "揭阳商务酒店推荐，哪家性价比高",
    "创客教室激光设备采购",
    "学校激光切割机采购方案",
]


@pytest.mark.parametrize("keyword", NON_DECISION_KEYWORDS)
def test_information_checks_are_not_quote_keywords(keyword):
    assert KeywordExpander()._check_geo_feasibility(keyword) is False


@pytest.mark.parametrize("keyword", BUYER_DECISION_KEYWORDS)
def test_explicit_supplier_or_purchase_decisions_are_quote_keywords(keyword):
    assert KeywordExpander()._check_geo_feasibility(keyword) is True


@pytest.mark.asyncio
async def test_expand_rejects_information_batch_and_refills_from_customer_seed(monkeypatch):
    expander = KeywordExpander()

    async def fake_llm_expand(**_kwargs):
        return [
            (keyword, "认知")
            for keyword in [*NON_DECISION_KEYWORDS, "AI就业实训机构排名前十"]
        ]

    async def fake_5118_expand(**_kwargs):
        return []

    async def fake_llm_filter(**_kwargs):
        return []

    monkeypatch.setattr(expander, "_llm_expand", fake_llm_expand)
    monkeypatch.setattr(expander, "_5118_expand", fake_5118_expand)
    monkeypatch.setattr(expander, "_llm_filter", fake_llm_filter)

    result = await expander.expand_keywords(
        core_keywords=["AI就业实训机构"],
        industry="教育培训",
        city="全国",
        target_count=20,
    )

    keywords = {item["keyword"] for item in result["keywords"]}
    rejected = {item["keyword"] for item in result["rejected_keywords"]}
    assert result["success"] is True
    assert result["summary"]["minimum_expected"] == 8
    assert result["summary"]["rejected_non_decision_total"] == len(NON_DECISION_KEYWORDS)
    assert result["summary"]["supplemented"] >= 7
    assert result["summary"]["actual_count"] == len(result["keywords"])
    assert result["summary"]["actual_count"] == result["summary"]["minimum_expected"]
    assert result["summary"]["requested_count"] == 20
    assert result["summary"]["delivery_status"] in {"complete", "partial"}
    assert "AI就业实训机构排名前十" in keywords
    assert keywords.isdisjoint(NON_DECISION_KEYWORDS)
    assert rejected == set(NON_DECISION_KEYWORDS)
    assert all(item["geo_recommend"] is False for item in result["rejected_keywords"])
    assert all(item["default_selected"] is False for item in result["rejected_keywords"])
    assert all(item["rejection_reason"] for item in result["rejected_keywords"])
    assert all(item["geo_recommend"] is True for item in result["keywords"])
    assert all(item["scope_match"] is True for item in result["keywords"])
    assert all(item["default_selected"] is True for item in result["keywords"])
    assert all(
        expander._check_geo_feasibility(item["keyword"])
        for item in result["keywords"]
    )


def test_rule_supplements_are_purchase_decisions_only():
    expander = KeywordExpander()
    supplements = expander._build_rule_supplements(
        core_keywords=["AI就业实训机构", "AI数字人才培训项目"],
        city="全国",
        limit=30,
    )

    assert len(supplements) >= 8
    assert all(
        expander._check_geo_feasibility(item["keyword"])
        for item in supplements
    )
    assert not any("选购建议" in item["keyword"] for item in supplements)


def test_supplier_seed_supplements_do_not_repeat_supplier_objects():
    expander = KeywordExpander()
    supplements = expander._build_rule_supplements(
        core_keywords=["工业机器人供应商", "AI培训机构"],
        city="全国",
        limit=30,
    )
    keywords = [item["keyword"] for item in supplements]

    assert "工业机器人供应商供应商推荐" not in keywords
    assert "工业机器人供应商厂家推荐" not in keywords
    assert "AI培训机构机构推荐" not in keywords
    assert "工业机器人供应商推荐" in keywords
    assert "AI培训机构有哪些" in keywords
    assert all(
        expander._check_geo_feasibility(item["keyword"])
        for item in supplements
    )


def test_quote_ui_allows_operator_release_of_excluded_terms():
    """[工单 2026-07-26 P0-4] 「未进入交付」区不再是只读物理隔离:

    - 默认不选 + 显示原因(不变)
    - 但每条都给「放行到交付」入口,放行后进候选并带审计理由
    - 只有硬边界(hard_block / human_override_allowed=false)才渲染成"不可放行"
    - 提交侧过滤条件从 `geo_recommend !== false` 改成 `!isHardBlockedKeyword(k)`
    """
    source = (
        Path(__file__).parents[1]
        / "frontend" / "src" / "pages" / "Quote" / "OnlineQuoteFlow.tsx"
    ).read_text(encoding="utf-8")

    assert "function isHardBlockedKeyword" in source
    assert "放行到交付" in source
    assert "不可放行" in source
    assert "setReleasedKws(prev => new Set(prev).add(kw.keyword));" in source
    assert ".filter(k => selectedKws.has(k.keyword) && !isHardBlockedKeyword(k))" in source
    # 旧的物理隔离过滤必须已经拆掉,否则放行按钮点了也提交不上去
    assert ".filter(k => selectedKws.has(k.keyword) && k.geo_recommend !== false)" not in source
    # [WO_QUOTE_KEYWORD_GEO_COMMERCIAL_DOUBLE_INVERSION 2026-08-09 · T5]
    #   合同变了,这两条判据跟着变 —— **不是绕过**:
    #   ① 放行审计从一句通用话改成"放行了哪一类判定"(区名 + 原因码),
    #      只写"操作员放行"复盘时等于没写;
    #   ② 待确认区从 `geo_recommend !== false` 一个布尔拆成六个 reason_group,
    #      可选区判据随之改成 `isDelivered`(default_selected 或已放行)。
    #   🔴 这两条是**前端源码字符串断言**(工单 §8 点名不许拿它冒充端到端)。
    #      真行为锁在 `frontend/tests/quote-keyword-groups/groups.spec.ts`:
    #      真浏览器点一次放行 → 词进选中集合 → 提交 payload 里带审计原因,
    #      并已用"换回生产尖那版前端 → 5 条全红"证过判别力。这里只留防拆签名。
    assert "区明确放行并决定继续" in source
    assert "REVIEW_GROUP_META[reviewGroupOf(k)]?.title" in source
    # 放行后该词回到可选区
    assert "|| releasedKws.has(keyword.keyword)" in source or "releasedKws.has(k.keyword)" in source
    assert "const isDelivered = (k: ExpandedKeyword) =>" in source


def test_quote_ui_exposes_scope_lock_for_manual_confirmation():
    """[工单 P0-1] 范围锁定裁决必须在报价页露出、可人工改、确认后回传复用。"""
    source = (
        Path(__file__).parents[1]
        / "frontend" / "src" / "pages" / "Quote" / "OnlineQuoteFlow.tsx"
    ).read_text(encoding="utf-8")

    assert "选词范围锁定" in source
    assert "const [scopeLock, setScopeLock]" in source
    assert "const [scopeLockConfirmed, setScopeLockConfirmed]" in source
    assert "MARKET_LEVEL_LABELS" in source
    assert "确认并重新扩词" in source
    # 扩词请求必须带 brand_id / diagnosis_id(服务端据此取诊断摘要)
    assert "brand_id: selectedBrandId || undefined," in source
    assert "diagnosis_id: selectedDiagnosisId || undefined," in source
    assert (
        "scope_lock: lockOverride ?? (scopeLockConfirmed && scopeLock ? scopeLock : undefined),"
        in source
    )
    assert "if (data?.scope_lock && !lockOverride) setScopeLock(data.scope_lock as ScopeLock);" in source
    # setState 异步:确认按钮必须把改过的裁决直接传进 handleExpand,不能靠闭包里的旧值
    assert "handleExpand(scopeLock);" in source
    assert "const handleExpand = async (lockOverride?: ScopeLock) => {" in source
    # onClick 不能裸挂 handleExpand,否则 MouseEvent 会被当成 lockOverride 传进去
    assert "onClick={handleExpand}" not in source


def test_quote_ui_defaults_to_recommended_candidates_only():
    source = (
        Path(__file__).parents[1]
        / "frontend"
        / "src"
        / "pages"
        / "Quote"
        / "OnlineQuoteFlow.tsx"
    ).read_text(encoding="utf-8")

    assert "function recommendedKeywordSet" in source
    assert ".filter(isDefaultSelectedKeyword)" in source
    assert "keyword.scope_match !== false" in source
    assert "geo_recommend: false" in source
    assert "本地兜底（待确认）" in source
    assert "setSelectedKws(recommendedKeywordSet(kws));" in source
    assert "setSelectedKws(recommendedKeywordSet(fallback));" in source
    assert "setSelectedKws(recommendedKeywordSet(expanded));" not in source
    assert "const recommendedCount = recommendedKeywordSet(kws).size;" in source
    assert "if (kws.length < minimumExpected)" not in source
    assert "个待人工确认" in source
    assert "function mergeRecommendedAndReviewCandidates" in source
    assert "Number.MAX_SAFE_INTEGER" in source
    assert "review_override_reason" in source
    # [T5 2026-08-09] 快照合同版本随三轴决策上线而 bump —— 改规则必同步改测试。
    assert "review_version: 'keyword-delivery-decision-v1'" in source


def test_sandbox_candidates_use_the_same_buyer_intent_ssot():
    source = (Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8")
    sandbox_source = (
        Path(__file__).parents[1] / "frontend" / "src" / "sandbox" / "mockData.ts"
    ).read_text(encoding="utf-8")

    assert "feasibility = KeywordExpander()._check_geo_feasibility" in source
    assert '"rejected_keywords": rejected' in source
    assert '"geo_recommend": True' not in source[
        source.index("if u and u.get('user_id') == 51:"):
        source.index("except Exception as _demo_err:")
    ]
    sandbox_block = sandbox_source[
        sandbox_source.index("export function getSandboxKeywordsExpand()"):
        sandbox_source.index("export function resetSandboxQuoteState()")
    ]
    assert "SANDBOX_CONTRACT_KEYWORDS.includes(kw)" in sandbox_block
    assert "geo_recommend: true" not in sandbox_block


class TestAveragePriceKnowledgeForms:
    """[Review-CTO 2026-07-27 · Owner 点名 session 201 残留] 泛均价问法 = 知识题。

    "深圳TikTok代运营公司收费一般多少"借"公司/收费"信号漏过唯一引擎,
    但 AI 对均价问法只回行情区间不点名商家。价格**决策**形必须继续放行
    ——价格询盘是 6 层矩阵正当层,一刀切杀价格词就拐回老护栏。
    """

    def test_average_price_forms_are_knowledge(self):
        from services.commercial_query_policy import buyer_intent_eligible

        for kw in (
            "深圳TikTok代运营公司收费一般多少",   # 生产 session 201 原词
            "TikTok代运营行业收费一般是多少",
            "外贸代运营大概多少钱",
            "TikTok代运营平均价格",
            "装修市场行情",
            "一般收费是多少",
        ):
            assert not buyer_intent_eligible(kw), kw

    def test_decision_price_forms_stay_eligible(self):
        from services.commercial_query_policy import buyer_intent_eligible

        for kw in (
            "TikTok外贸获客代运营公司收费对比",   # 生产 session 201 同批合格词
            "深圳TikTok代运营一般怎么收费",       # 诊断守卫模板依赖,不得误杀
            "深圳TikTok代运营哪家性价比高",
            "TikTok代运营报价",
            "预算3万TikTok代运营选哪家",
        ):
            assert buyer_intent_eligible(kw), kw
