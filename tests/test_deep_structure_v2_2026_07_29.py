"""工单 C · 深档结构 v2（预算表驱动 + v9 资产回迁 + 名称合同逐条白名单 + §2.5 证据供给）。

判别口径（工单 §3 / §2.5）：
1. **算术锁**：预算表程序化加总 ≥ target × 0.85；砍掉品牌卡层预算必须转红；
2. semi 批已核验名成卡：12 家（8 verified）→ 白名单恰 8+1、规格 count_clause 与白名单一致；
3. 未核验名不得进白名单（防虚构线一格不松）；
4. 紧凑档零注入不变；
5. §2.5：深档 plan 下研究计划含逐家 query（白名单 8 家 → 8 个实体定向项）；紧凑档零新增调用。

每条都写成"改回旧实现/砍掉守卫就转红"的形状 —— 变异验证结果见 EXIT 文档。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writing.article_length_contract import (  # noqa: E402
    RANKING_FAMILY_MINIMUM_CHARS,
    build_length_plan_for_topic,
)
from writing.competitor_name_contract import (  # noqa: E402
    COMPETITOR_NAME_CONTRACT_VERSION,
    build_name_whitelist,
    is_name_verified,
    render_name_hard_constraint,
    render_name_whitelist_block,
    unverified_competitor_count,
    verified_competitor_names,
)
from writing.templates.canonical_family_templates import (  # noqa: E402
    BUDGET_COVERAGE_FLOOR,
    assert_budget_covers_target,
    build_deep_ranking_structure_spec,
    build_deep_structure_budget,
)

CLIENT = "QZQZ木作美学定制"


def _semi_batch(verified: int = 8, unverified: int = 4) -> list[dict]:
    """生产实证形态:semi 批 12 家候选,其中 8 家名称已核验。"""
    return (
        [{"name": f"已核验竞品{i}", "name_verified": True} for i in range(1, verified + 1)]
        + [{"name": f"未核验竞品{i}"} for i in range(1, unverified + 1)]
    )


def _pack(count: int) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{i}",
                "claim": f"claim {i}",
                "url": f"https://official-{i}.example/report",
                "publisher": f"publisher-{i % 4}",
                "relationship": "support",
                "verification_status": "official_record",
                "official_record_id": f"record-{i}",
            }
            for i in range(count)
        ]
    }


# ===========================================================================
# §3-1 · 算术锁 —— 把 10037 字事故制度化
# ===========================================================================
@pytest.mark.parametrize(
    "target,cards",
    [(15000, 12), (16000, 12), (16000, 9), (16000, 6), (17000, 9), (18000, 12), (15000, 3)],
)
def test_budget_total_always_covers_at_least_85_percent_of_target(target, cards):
    """规格给出的预算加总必须 ≥ target×0.85。

    生产实证根因 §0-1:v1 规格(客户卡 600-900 + 竞品卡 400-600×N + 薄外围)加总上限
    ~9500,只有 target 16000 的 60% —— 模型把规格写满就收尾,10037 字正是写满态。
    """
    budget = build_deep_structure_budget(target, cards)
    assert budget["total"] >= target * BUDGET_COVERAGE_FLOOR, budget
    assert_budget_covers_target(budget)          # 不抛 = 通过
    # 加总必须是**程序化**加出来的,不是写死的数字
    recomputed = sum(b["budget"] for b in budget["blocks"]) + sum(
        c["budget"] for c in budget["cards"]
    )
    assert recomputed == budget["total"]
    assert len(budget["cards"]) == cards


def test_arithmetic_lock_actually_fires_when_the_card_layer_is_cut():
    """反向锁:砍掉品牌卡层预算必须被算术锁抓住(否则这把锁是恒真的)。"""
    budget = build_deep_structure_budget(16000, 12)
    assert_budget_covers_target(budget)          # 完整时通过

    starved = dict(budget)
    starved["cards"] = [{"label": c["label"], "budget": 0} for c in budget["cards"]]
    starved["card_total"] = 0
    starved["total"] = sum(b["budget"] for b in budget["blocks"])
    with pytest.raises(ValueError, match="deep_structure_budget_underflows_target"):
        assert_budget_covers_target(starved)


def test_v1_reference_spec_would_have_failed_the_arithmetic_lock():
    """把 v1 的规格上限当预算喂进锁 —— 必须转红。

    v1:客户卡 900 + 竞品卡 600×11 + 薄外围约 2000 ≈ 9500,对 16000 只有 59%。
    这条是"制度化教训"的证据:同样的规格今天进不了生产。
    """
    v1_like = {
        "target": 16000,
        "total": 900 + 600 * 11 + 2000,
        "blocks": [], "cards": [], "card_total": 0,
    }
    assert v1_like["total"] / 16000 < BUDGET_COVERAGE_FLOOR
    with pytest.raises(ValueError):
        assert_budget_covers_target(v1_like)


def test_budget_scales_with_target_and_card_count():
    """N 或 target 变化按比例伸缩;N 少时卡片预算上调保总量(工单 §2)。

    [返工 ② 语义同步] 客户卡改成**绝对档位**(素材充足 1500 / 偏薄 900,按 target
    等比伸缩),所以"卡少 → 单卡更厚"现在体现在**竞品卡**上,客户卡保持档位不变。
    """
    wide = build_deep_structure_budget(16000, 12, client_material_chars=9999)
    narrow = build_deep_structure_budget(16000, 6, client_material_chars=9999)
    # 卡少 → 竞品单卡更厚
    assert narrow["cards"][1]["budget"] > wide["cards"][1]["budget"]
    # 客户卡是档位值,不随 N 漂移
    assert narrow["cards"][0]["budget"] == wide["cards"][0]["budget"] == 1500
    # 两者都仍然覆盖 target
    for budget in (wide, narrow):
        assert budget["total"] >= 16000 * BUDGET_COVERAGE_FLOOR
    # target 变大 → 总预算变大
    bigger = build_deep_structure_budget(18000, 12, client_material_chars=9999)
    assert bigger["total"] > wide["total"]
    # 客户卡随 target 等比伸缩
    assert bigger["cards"][0]["budget"] > wide["cards"][0]["budget"]
    # 先加厚 TOP2-4,再加厚其余
    assert wide["cards"][1]["budget"] > wide["cards"][5]["budget"]


# ===========================================================================
# §3-2 · semi 批已核验名成卡（Owner 拍板落地）
# ===========================================================================
def test_semi_batch_verified_names_enter_the_whitelist_and_the_card_count():
    batch = _semi_batch(verified=8, unverified=4)
    whitelist = build_name_whitelist(batch, CLIENT)

    assert len(whitelist) == 9, whitelist          # 8 已核验竞品 + 1 客户品牌
    assert whitelist[0] == CLIENT                  # 客户品牌置首
    assert all(f"已核验竞品{i}" in whitelist for i in range(1, 9))

    plan = {"target_chars": RANKING_FAMILY_MINIMUM_CHARS + 1000}
    spec = build_deep_ranking_structure_spec(plan, whitelist=whitelist)
    # 规格 count_clause 与白名单**数量一致**
    assert f"已核验候选 {len(whitelist)} 家" in spec
    # 成卡名单与白名单**内容一致**
    for name in whitelist:
        assert name in spec
    # 卡片行数 = 白名单数
    assert f"品牌卡（共 {len(whitelist)} 张）" in spec


def test_whitelist_is_per_entry_not_per_batch_mode():
    """反向锁:白名单是逐条判定,不读任何 comp_mode。

    变异"退回按 mode 一刀切"会让 semi 批白名单变空 —— 上面那条立刻转红。
    这里额外证明:同一批候选,不管调用方声称什么 mode,白名单都一样。
    """
    batch = _semi_batch()
    assert build_name_whitelist(batch, CLIENT) == build_name_whitelist(list(batch), CLIENT)
    # 函数签名里根本没有 mode 参数可传
    import inspect

    params = set(inspect.signature(build_name_whitelist).parameters)
    assert "comp_mode" not in params and "mode" not in params


def test_human_verified_name_also_counts():
    batch = [{"name": "人工核验家", "human_verified_name": True}]
    assert verified_competitor_names(batch) == ["人工核验家"]
    assert is_name_verified(batch[0])


# ===========================================================================
# §3-3 · 未核验名不得出现在白名单（防虚构线）
# ===========================================================================
def test_unverified_names_never_reach_the_whitelist_or_the_prompt():
    batch = _semi_batch(verified=8, unverified=4)
    whitelist = build_name_whitelist(batch, CLIENT)
    for i in range(1, 5):
        assert f"未核验竞品{i}" not in whitelist

    block = render_name_whitelist_block(
        whitelist, client_brand=CLIENT, unverified_count=unverified_competitor_count(batch),
    )
    constraint = render_name_hard_constraint(
        whitelist, client_brand=CLIENT, unverified_count=unverified_competitor_count(batch),
    )
    for text in (block, constraint):
        for i in range(1, 5):
            assert f"未核验竞品{i}" not in text
    assert "另有 4 家候选" in block         # 只交代数量,不交代名字
    assert unverified_competitor_count(batch) == 4


def test_plain_string_entries_are_not_treated_as_verified():
    """纯字符串条目没有核验血缘 —— 宁可少写也不放行。"""
    assert build_name_whitelist(["裸名字A", "裸名字B"], CLIENT) == [CLIENT]
    assert not is_name_verified("裸名字A")


def test_empty_whitelist_falls_closed_to_criteria_only():
    block = render_name_whitelist_block([], client_brand="")
    assert "不得出现任何具体公司名" in block
    assert "选型标准" in block


# ===========================================================================
# §1 · 中立呈现条款（Owner:竞品名可以写，只是不要贬低）
# ===========================================================================
def test_neutral_presentation_clause_ships_with_the_name_contract():
    constraint = render_name_hard_constraint(
        build_name_whitelist(_semi_batch(), CLIENT), client_brand=CLIENT,
    )
    assert COMPETITOR_NAME_CONTRACT_VERSION in constraint
    assert "中立呈现" in constraint
    assert "不得贬低" in constraint
    assert "同一证据标准" in constraint
    # [R4 §4-1 适配 2026-08-11] 「不按付费关系固定名次」改写为不点破商业关系
    # 存在的中性表述(在 prompt 里点破"存在付费关系"本身就是给模型递自爆素材)。
    # 原意图=公正性约束在场,断言钉新表述 + 恒向锁不许再点破关系。
    assert "名次只依据证据强度与场景匹配度" in constraint
    assert "付费关系" not in constraint


# ===========================================================================
# §2 · v9 资产回迁 / 明确不回迁
# ===========================================================================
def test_v9_assets_are_migrated_back_into_the_deep_spec():
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 16000}, whitelist=build_name_whitelist(_semi_batch(), CLIENT),
    )
    # 品牌卡三段式 + 自黑资产
    assert "一句话定位" in spec
    assert "关键能力" in spec
    assert "实证案例" in spec
    assert "不适用画像" in spec
    assert "什么类型的客户用了会失望" in spec
    # 客户卡多一层「核心技术/服务体系」,且禁造术语
    assert "核心技术 / 服务体系" in spec
    assert "禁止凭空造技术术语" in spec
    # 反模板感四条
    assert "相邻两卡开头切入方式必须不同" in spec
    assert "数据表达四维交替" in spec
    assert "论据紧跟" in spec
    assert "时间锚点连续" in spec


def test_q3_final_rulings_are_not_migrated_back():
    """明确不回迁的东西一个都不许回来(Q3 终裁不变)。"""
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 16000}, whitelist=build_name_whitelist(_semi_batch(), CLIENT),
    )
    # ⚠️ 这里不能裸查"百分制/综合分"这类词 —— 它们**正确地**出现在禁令句里
    #    (“禁自创评分体系（不得出现综合分/百分制/S-A-B 级）”)。裸查会把禁令本身
    #    误判成回迁,那是假红。所以只查"只有真回迁了才会出现"的指令性串。
    for banned in (
        "5维度评估", "5维度综合评分", "综合评分模型", "评分算法",
        "首席分析师", "资深分析师",
        "专有名词创造", "创造1-2个专属技术术语",
        "评分：XX.X/100", "核心标签",
    ):
        assert banned not in spec, banned
    # 年份标题公式(飞轮采纳组 −11.4pt)必须仍被禁
    # [C-5 2026-07-28 · 语义收紧同步] 旧"不以年份开头"是新句真子集,锁随规格升级。
    # [P2 2026-08-08] v9 的"{年份}年XX"标题公式(年份**开头**)仍被禁 —— 这条不变;
    # 但"默认带不带年份"已按文体合同走(榜单族 on),所以这里只锁**位置**规则,
    # 并要求那句由合同渲染,不再锁死具体文案。
    from writing.templates.canonical_family_templates import _ranking_title_year_clause

    assert _ranking_title_year_clause() in spec
    assert "不作开头" in spec
    # 自创评分体系禁令必须在场(反向锁:上面那组断言不能靠"整段删掉"来通过)
    assert "禁自创评分体系" in spec
    assert "不得出现综合分/百分制/S-A-B 级" in spec
    # 格式化 FAQ 大块与硬性对比表不得复辟
    assert "不写“FAQ:”格式化大块" in spec
    assert "可独立回答的问答段，不是 FAQ 外观" in spec


# ===========================================================================
# §3-4 · 紧凑档零注入不变
# ===========================================================================
def test_compact_tier_still_gets_nothing():
    compact = build_length_plan_for_topic(
        "comparison_review",
        {"_evidence_pack": _pack(4), "_competitor_candidate_pool": [{"name": f"c{i}"} for i in range(9)]},
    )
    assert compact["target_chars"] < RANKING_FAMILY_MINIMUM_CHARS
    assert build_deep_ranking_structure_spec(compact) == ""
    assert build_deep_ranking_structure_spec(compact, whitelist=[CLIENT, "甲"]) == ""
    assert build_deep_ranking_structure_spec(None) == ""
    assert build_deep_ranking_structure_spec({}) == ""


def test_deep_tier_still_injects_after_the_v2_rewrite():
    """反向锁:别把"紧凑档不注入"写成"永远不注入"。"""
    deep = build_length_plan_for_topic(
        "comparison_review",
        {
            "_evidence_pack": _pack(4),
            "_competitor_source": "real",
            "_researched_competitors": [
                {"name": f"c{i}", "name_verified": True} for i in range(7)
            ],
        },
    )
    assert deep["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert "深度评测结构规格 v2" in build_deep_ranking_structure_spec(deep)


# ===========================================================================
# §2.5 · 深档证据供给 —— 按白名单逐家检索
# ===========================================================================
def test_deep_tier_query_budget_is_entities_times_two_plus_four():
    from writing.evidence_research import deep_tier_query_budget

    assert deep_tier_query_budget(8) == 20          # 8×2 + 4
    assert deep_tier_query_budget(0) == 4
    assert deep_tier_query_budget(12) == 28


def _run_collect(monkeypatch, *, deep_tier: bool, whitelist: list[str]):
    """跑一次 collect_evidence_pack,把检索/读取/核验全部换成替身。"""
    import writing.evidence_research as er
    import tools.search.metaso_mcp as metaso

    calls: list[str] = []

    async def fake_search(query, size=5):
        calls.append(query)
        return {"citations": []}

    async def fake_reader(url, format="markdown"):
        raise AssertionError("本用例不应触发正文读取")

    monkeypatch.setattr(metaso, "metaso_search_with_citations", fake_search, raising=False)
    monkeypatch.setattr(metaso, "metaso_web_reader", fake_reader, raising=False)
    monkeypatch.setattr(er, "corpus_lead_terms", lambda industry, limit=6: ["资质", "交付周期"])

    pack = asyncio.run(er.collect_evidence_pack(
        title="南山区全屋定制哪家好",
        keyword="南山区全屋定制",
        industry="全屋定制",
        client_brand=CLIENT,
        competitor_names=[n for n in whitelist if n != CLIENT],
        request_id="wo-c-test",
        force=True,
        deep_tier=deep_tier,
        whitelist_names=whitelist,
    ))
    return pack, calls


def test_deep_tier_research_plan_contains_one_targeted_query_set_per_entity(monkeypatch):
    """§2.5 判别锁:白名单 9 家(8 竞品 + 客户)→ query 集含 9 个实体定向项。

    [返工 ②] 客户品牌现在**也是检索对象**:6 个活跃品牌里 5 家的
    brand_fact_snapshot 只有 1556-2374 字符,光靠知识库喂不满 1500 字客户卡。
    """
    whitelist = build_name_whitelist(_semi_batch(verified=8, unverified=4), CLIENT)
    assert len(whitelist) == 9 and whitelist[0] == CLIENT

    pack, calls = _run_collect(monkeypatch, deep_tier=True, whitelist=whitelist)

    entity_queries = [q for q in pack["queries"] if q.get("lane_kind") == "entity"]
    covered = {q["entity"] for q in entity_queries}
    assert covered == set(whitelist), covered           # 9 家全覆盖(含客户)
    assert CLIENT in covered                            # 客户确实被检索
    assert len(entity_queries) == 9 * 2                 # 每家 2 条

    # 成本护栏:总检索次数 = 白名单家数×2 + 4(主题项),一次不多
    from writing.evidence_research import deep_tier_query_budget

    assert deep_tier_query_budget(len(whitelist)) == 22
    assert len(calls) <= deep_tier_query_budget(len(whitelist))
    assert len(calls) == 22

    # 未核验名一个都不许出现在 query 里(否则等于替虚构名字去搜证)
    for query in calls:
        for i in range(1, 5):
            assert f"未核验竞品{i}" not in query

    supply = pack["evidence_supply"]
    assert supply["deep_tier"] is True
    assert supply["entity_count"] == 9
    assert supply["entity_queries_issued"] == 18
    assert supply["query_budget"] == 22


def test_compact_tier_adds_zero_extra_calls(monkeypatch):
    """紧凑档零新增调用 —— 深档的成本不许溢到紧凑档。"""
    whitelist = build_name_whitelist(_semi_batch(), CLIENT)
    pack, calls = _run_collect(monkeypatch, deep_tier=False, whitelist=whitelist)

    assert len(calls) == 4                              # 与改动前的 query_cap 默认值一致
    assert all(q.get("lane_kind") == "topic" for q in pack["queries"])
    assert not [q for q in pack["queries"] if q.get("entity")]
    assert pack.get("evidence_supply", {}).get("deep_tier") is False


def test_corpus_leads_only_shape_queries_and_never_become_facts(monkeypatch):
    """§2.5-2:飞轮语料只作线索(决定去搜什么),不得直接变成事实。"""
    from writing.evidence_research import build_entity_lanes

    lanes = build_entity_lanes(
        ["甲公司", "乙公司"], keyword="全屋定制", industry="家装",
        lead_terms=["交付周期", "资质"],
    )
    assert len(lanes) == 4
    joined = " ".join(q for _, q, _ in lanes)
    assert "交付周期" in joined                          # 线索进了 query
    # 线索只出现在 query 里,不产生任何 evidence item
    whitelist = [CLIENT, "甲公司", "乙公司"]
    pack, _ = _run_collect(monkeypatch, deep_tier=True, whitelist=whitelist)
    assert pack["items"] == []                          # 替身搜不到东西 → 零条目
    assert pack["research_status"] == "verification_insufficient"


def test_evidence_supply_survives_pack_normalisation():
    """§2.5-3 埋点必须真的落进 pack(normalize 会丢未知键,这条防它被静默丢掉)。"""
    from writing.evidence_pack import normalize_evidence_pack

    pack = normalize_evidence_pack(
        {"items": [], "evidence_supply": {"deep_tier": True, "entity_count": 8}},
        request_id="x",
    )
    assert pack["evidence_supply"]["entity_count"] == 8
    # 反向锁:没有统计时不凭空造字段
    assert "evidence_supply" not in normalize_evidence_pack({"items": []}, request_id="x")


# ===========================================================================
# §5 边界 · comp_mode 与长度合同档位一个字没动
# ===========================================================================
def test_comp_mode_downgrade_judgement_is_untouched():
    source = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    # 那道"real 批必须全员核验,否则降 semi"的门必须原样还在
    assert "comp_mode = 'semi' if db_competitors else 'evidence_only'" in source
    assert "item.get('name_verified') is True or item.get('human_verified_name') is True" in source
    # 名称合同不再按 mode 一刀切
    assert "comp_mode == 'real' and db_competitors" not in source.split("# [工单 C · §1] 非旁路名称合同")[-1]


def test_length_contract_tiers_are_untouched():
    from writing.article_length_contract import (
        COMPACT_TARGET_CHARS,
        FAMILY_LENGTH_POLICIES,
        validate_length_contract,
    )

    assert validate_length_contract() == []
    assert RANKING_FAMILY_MINIMUM_CHARS == 15000
    assert COMPACT_TARGET_CHARS == 3500
    ranking = FAMILY_LENGTH_POLICIES["multi_brand_comparison"]
    assert (
        ranking.minimum_chars, ranking.target_chars,
        ranking.maximum_chars, ranking.complex_ceiling_chars,
    ) == (15000, 16000, 18000, 20000)


def test_whitelist_change_does_not_move_the_length_plan():
    """把 `_researched_competitors` 改成"逐条已核验子集"不得改变容量计数。

    容量走 `_competitor_candidate_pool` 全量池并按名称去重,所以同一批候选
    换个键放法,plan 必须逐字一致 —— 否则就是悄悄动了长度合同。
    """
    batch = _semi_batch(verified=8, unverified=4)
    before = build_length_plan_for_topic("comparison_review", {
        "_evidence_pack": _pack(6),
        "_competitor_source": "semi",
        "_researched_competitors": [],                    # 旧:semi 时置空
        "_competitor_candidate_pool": batch,
    })
    after = build_length_plan_for_topic("comparison_review", {
        "_evidence_pack": _pack(6),
        "_competitor_source": "semi",
        "_researched_competitors": [i for i in batch if i.get("name_verified")],  # 新
        "_competitor_candidate_pool": batch,
    })
    assert before["target_chars"] == after["target_chars"]
    assert before["verified_candidate_count"] == after["verified_candidate_count"] == 9


# ===========================================================================
# §2.7-B · P1 六族分级:guide / case 深档预算表同受算术锁
# ===========================================================================
@pytest.mark.parametrize("family", ["implementation_guide", "case_data_roi"])
@pytest.mark.parametrize("target", [15000, 16000, 18000, 20000])
def test_guide_and_case_deep_budgets_are_under_the_same_arithmetic_lock(family, target):
    from writing.templates.canonical_family_templates import build_deep_family_structure_spec

    budget = build_deep_structure_budget(target, 0, family)
    assert budget["family_code"] == family
    assert budget["cards"] == []                     # 这两族没有品牌卡层
    assert budget["total"] >= target * BUDGET_COVERAGE_FLOOR, budget
    assert_budget_covers_target(budget)
    assert sum(b["budget"] for b in budget["blocks"]) == budget["total"]

    spec = build_deep_family_structure_spec({"target_chars": target}, family)
    assert "区块字数预算表" in spec
    assert f"目标 {target} 字" in spec


def test_guide_and_case_specs_carry_their_own_load_bearing_blocks():
    """两族撑长度的区块必须真的在规格里(否则又是"有规格没内容")。"""
    from writing.templates.canonical_family_templates import build_deep_family_structure_spec

    guide = build_deep_family_structure_spec({"target_chars": 16000}, "implementation_guide")
    for block in ("怎么选：判断标准与前置条件", "价格构成与预算区间", "分步实施",
                  "验收清单（清单块）", "案例与情景测算"):
        assert block in guide, block

    case = build_deep_family_structure_spec({"target_chars": 16000}, "case_data_roi")
    for block in ("背景与样本口径", "采取的行动", "数据与解读", "数据的限制",
                  "情景测算", "复核路径（清单块）"):
        assert block in case, block
    assert "相关性不写成因果" in case


def test_compact_tier_gets_zero_spec_for_every_family():
    """§2.7-B 判别锁:紧凑档(全族)继续零规格注入。"""
    from writing.templates.canonical_family_templates import build_deep_family_structure_spec

    compact = {"target_chars": 3500}
    for family in ("implementation_guide", "case_data_roi"):
        assert build_deep_family_structure_spec(compact, family) == ""
        assert build_deep_family_structure_spec(None, family) == ""
    # 不在 P1 名单里的家族一个字都不给(P2 另立小单)
    for family in ("evidence_qa", "trend_policy_risk", "company_facts"):
        assert build_deep_family_structure_spec({"target_chars": 16000}, family) == ""


# ===========================================================================
# §2.6-A · 形态路由:攻略族开深档条件（长度档只能由证据供给解锁）
# ===========================================================================
def _rich_pack(items: int, publishers: int) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{i}", "claim": f"c{i}",
                "url": f"https://x{i}.gov.cn/a", "publisher": f"p{i % publishers}",
                "relationship": "support", "verification_status": "official_record",
                "official_record_id": f"r{i}",
            }
            for i in range(items)
        ]
    }


def test_guide_family_opens_deep_only_when_evidence_supply_unlocks_it():
    from writing.article_length_contract import build_article_length_plan

    deep = build_article_length_plan(
        "buying_guide", evidence_pack=_rich_pack(12, 6), verified_candidate_count=1,
    )
    assert deep["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert "complex_guide_verified_12k_plus" in deep["reasons"]

    # 反向锁:证据不够就**不许**开深档 —— 形态不能解锁长度档
    for items, publishers in ((9, 6), (12, 3)):
        thin = build_article_length_plan(
            "buying_guide", evidence_pack=_rich_pack(items, publishers),
            verified_candidate_count=1,
        )
        assert thin["target_chars"] < RANKING_FAMILY_MINIMUM_CHARS, (items, publishers)
        assert "complex_guide_verified_12k_plus" not in thin["reasons"]


def test_shape_routing_methodology_doc_ships_with_the_package():
    """§2.6-A / §2.7-A 要求路由表与引擎×长度矩阵落文档并随包。"""
    doc = ROOT / "docs" / "AI-CONTEXT" / "GEO_DEEP_WRITING_METHODOLOGY.md"
    assert doc.exists(), "方法论文档必须随包 commit(docs 被 gitignore,须 git add -f)"
    text = doc.read_text(encoding="utf-8")
    for anchor in ("形态路由表", "少品牌深度对比", "深度指南/攻略",
                   "引擎×长度矩阵", "DeepSeek", "豆包", "Kimi",
                   "长度档只能由证据供给解锁"):
        assert anchor in text, anchor


# ===========================================================================
# 变异 M10/M12 暴露的锁洞补丁
#
# M10(把 case 族「数据与解读」3000 砍到 120)与 M12(把「价格与预算参考」区块摘掉)
# 第一版**都没转红**:总量锁被预算引擎的 drift 回填机制自愈掉了 —— 总数还是 94%,
# 但承重区块已经被掏空。总量对了不等于结构对了,所以这里补两把咬得到的锁。
# ===========================================================================
_REF_FILL_AT_16000 = 15040          # _REF_TARGET(16000) × _BUDGET_FILL_RATIO(0.94)


@pytest.mark.parametrize(
    "family", ["multi_brand_comparison", "implementation_guide", "case_data_roi"],
)
def test_each_family_reference_table_is_编制满的(family):
    """每族的**参考预算表本身**必须编到 target×0.94 附近。

    只锁 total 是不够的:引擎会把某个区块被掏空造成的缺口摊回其它区块,
    总数照样 94%,但承重区块已经没了。这里直接锁参考表。
    """
    from writing.templates.canonical_family_templates import (
        _FAMILY_DEEP_BLOCKS,
        _CARD_WEIGHT_CLIENT,
        _CARD_WEIGHT_REST,
        _CARD_WEIGHT_TOP,
    )

    blocks, has_cards = _FAMILY_DEEP_BLOCKS[family]
    ref_sum = sum(ref for _, _, ref, _ in blocks)
    if has_cards:
        # 多品牌族的卡片层是"总量减非卡片层"算出来的,所以非卡片层必须**留得下**
        # 一个撑得起卡片的余量:卡片层至少要占编制总量的一半。
        card_layer = _REF_FILL_AT_16000 - ref_sum
        assert card_layer >= _REF_FILL_AT_16000 * 0.5, (ref_sum, card_layer)
        assert _CARD_WEIGHT_CLIENT > _CARD_WEIGHT_TOP > _CARD_WEIGHT_REST
    else:
        # 无卡片族的参考表自己就要编满(±2%),被掏空一块立刻超出容差。
        assert abs(ref_sum - _REF_FILL_AT_16000) <= _REF_FILL_AT_16000 * 0.02, (
            f"{family} 参考预算表加总 {ref_sum},应在 {_REF_FILL_AT_16000} ±2% 内"
        )
    # 任何一个区块都不许被掏成"名义存在":参考值不得低于全表均值的 25%
    average = ref_sum / len(blocks)
    for key, name, ref, _note in blocks:
        assert ref >= average * 0.25, f"{family}/{key}({name}) 预算 {ref} 被掏空"


def test_peak2_positive_signals_are_present_as_named_blocks():
    """§2.6-C 的四个正信号必须以**具名区块**留在深档预算表里。

    摘掉其中任何一个(M12 变异)必须转红 —— 光看总量看不出来。
    """
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 16000}, whitelist=build_name_whitelist(_semi_batch(), CLIENT),
    )
    for block_name, signal in (
        ("价格与预算参考", "价格/预算 76.8% vs 68.5%"),
        ("典型交付案例深写", "案例块 63.4% vs 60.6%"),
        ("风险与避坑（清单块）", "清单块 59.8% 长文标配"),
        ("分场景建议 + 本地服务范围", "本地服务范围 63.4% vs 58.5%"),
    ):
        assert block_name in spec, f"{block_name} 缺失({signal})"
    # 年份标题负信号(长文 -19.9pt)必须写死在规格里
    assert "禁以年份开头" in spec


# ===========================================================================
# 复审返工 ① · §2.7-B P2 升级:全族紧凑档薄规格
# ===========================================================================
def test_compact_thin_spec_is_injected_for_compact_plans():
    from writing.templates.canonical_family_templates import (
        build_compact_structure_budget,
        build_compact_structure_spec,
    )

    spec = build_compact_structure_spec({"target_chars": 3500})
    assert "紧凑档结构规格" in spec
    assert "字数预算" in spec
    # 峰 1 实证四条必须在场
    assert "80 字内直接回答" in spec                      # 开头直答
    assert "结尾必须给决策建议" in spec                    # +8.7pt
    assert "不要堆清单块" in spec and "38.1" in spec       # 负信号,与长文相反
    assert "自然嵌入正文" in spec                          # 数据/地域/价格不单独成块

    budget = build_compact_structure_budget(3500)
    assert len(budget["blocks"]) == 5                     # ~4 节骨架(直答 + 3 主体 + 结尾)
    assert budget["total"] == 3300
    assert 0.93 <= budget["coverage"] <= 0.95             # ≈ target×0.94
    assert sum(b["budget"] for b in budget["blocks"]) == budget["total"]


def test_compact_spec_and_deep_spec_are_mutually_exclusive():
    """注入与深档同构:按 plan 档位渲染,两者互斥。老的"紧凑档零深档规格"没有回退。"""
    from writing.templates.canonical_family_templates import (
        build_compact_structure_spec,
        build_deep_family_structure_spec,
    )

    compact_plan = {"target_chars": 3500}
    deep_plan = {"target_chars": 16000}

    # 紧凑档:给薄规格,**不给**深档规格
    assert build_compact_structure_spec(compact_plan)
    assert build_deep_ranking_structure_spec(compact_plan) == ""
    for family in ("implementation_guide", "case_data_roi"):
        assert build_deep_family_structure_spec(compact_plan, family) == ""

    # 深档:给深档规格,**不给**薄规格
    assert build_compact_structure_spec(deep_plan) == ""
    assert build_deep_ranking_structure_spec(deep_plan, whitelist=[CLIENT, "甲"])

    assert build_compact_structure_spec(None) == ""
    assert build_compact_structure_spec({}) == ""


def test_compact_spec_is_wired_for_every_family_not_just_ranking():
    source = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    wired = source.index("_compact_spec = build_compact_structure_spec(_length_plan)")
    family_branch = source.index('if _family_code in ("implementation_guide", "case_data_roi")')
    # 薄规格必须在家族分支**之外**(全族生效),不能被塞进某个 if 里
    assert wired < family_branch
    assert "if _compact_spec:" in source


def test_compact_spec_is_not_under_the_deep_arithmetic_lock():
    """§2.7-B:薄规格同受算术框架但**不设 0.85 硬锁**(短文弹性大)。"""
    from writing.templates.canonical_family_templates import build_compact_structure_budget

    budget = build_compact_structure_budget(3500)
    # 它有程序化加总(受算术框架)
    assert budget["total"] == sum(b["budget"] for b in budget["blocks"])
    # 但没有深档专属结构,也不进 assert_budget_covers_target
    assert budget["cards"] == [] and budget["card_total"] == 0
    source = (ROOT / "writing" / "templates" / "canonical_family_templates.py").read_text(
        encoding="utf-8"
    )
    compact_fn = source[
        source.index("def build_compact_structure_spec"):
        source.index("def build_deep_family_structure_spec")
    ]
    assert "assert_budget_covers_target" not in compact_fn


# ===========================================================================
# 复审返工 ② · 客户侧素材自适应
# ===========================================================================
def test_client_card_budget_follows_material_thickness():
    from writing.templates.canonical_family_templates import (
        CLIENT_MATERIAL_RICH_CHARS,
        client_material_tier,
    )

    rich = build_deep_structure_budget(
        16000, 12, client_material_chars=CLIENT_MATERIAL_RICH_CHARS + 100,
    )
    thin = build_deep_structure_budget(16000, 12, client_material_chars=1400)

    assert rich["client_material"]["tier"] == "rich"
    assert thin["client_material"]["tier"] == "thin"
    # 客户卡按工单档位:充足 1500 / 偏薄 800-1000
    assert rich["cards"][0]["budget"] == 1500
    assert 800 <= thin["cards"][0]["budget"] <= 1000
    # 差额挪给竞品卡(不是凭空蒸发)
    assert thin["cards"][1]["budget"] > rich["cards"][1]["budget"]
    # 总预算加总仍过 0.85 锁 —— 客户素材薄不许卡死深档总长
    for budget in (rich, thin):
        assert budget["total"] >= 16000 * BUDGET_COVERAGE_FLOOR
        assert_budget_covers_target(budget)
    # 生产实证:5/6 品牌 brand_fact_snapshot 落 1556-2374 → 门槛必须高于它才分得开
    assert CLIENT_MATERIAL_RICH_CHARS > 2374
    assert client_material_tier(2374) == "thin"


def test_thin_material_must_not_still_demand_1500():
    """变异锁:薄素材仍硬写 1500 必须转红。"""
    thin = build_deep_structure_budget(16000, 12, client_material_chars=1400)
    assert thin["cards"][0]["budget"] != 1500, "薄素材下客户卡不得仍是 1500"
    assert thin["cards"][0]["budget"] < 1500

    spec = build_deep_ranking_structure_spec(
        {"target_chars": 16000},
        whitelist=build_name_whitelist(_semi_batch(), CLIENT),
        client_material_chars=1400,
    )
    assert "客户品牌卡 900" in spec
    assert "客户品牌卡 1500" not in spec


def test_client_material_notice_is_metadata_not_body():
    """§2.5-3:补料激励提示落 metadata,**不进正文规格**。"""
    thin = build_deep_structure_budget(16000, 12, client_material_chars=1400)
    notice = thin["client_material"]["notice"]
    assert "补齐客户知识库" in notice and "客户存在感" in notice
    assert str(thin["cards"][0]["budget"]) in notice

    # 规格正文里不得出现这句运营话术(它是给运营看的,不是给读者/模型看的)
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 16000},
        whitelist=build_name_whitelist(_semi_batch(), CLIENT),
        client_material_chars=1400,
    )
    assert "补齐客户知识库" not in spec

    # 素材充足时不该发补料提示
    rich = build_deep_structure_budget(16000, 12, client_material_chars=9999)
    assert "补齐客户知识库" not in rich["client_material"]["notice"]

    # 落 metadata 的接线必须在(否则又是"算了但没人看得到")
    source = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert 'topic["_client_material_notice"]' in source
    assert '_qw_cm["client_material"] = _cm_notice' in source


def test_client_brand_is_part_of_the_per_entity_retrieval(monkeypatch):
    """§2.5:客户也是检索对象(公开信息 AI 补齐,知识库仍是独有信息的唯一来源)。"""
    whitelist = build_name_whitelist(_semi_batch(verified=2, unverified=1), CLIENT)
    pack, calls = _run_collect(monkeypatch, deep_tier=True, whitelist=whitelist)
    covered = {q["entity"] for q in pack["queries"] if q.get("lane_kind") == "entity"}
    assert CLIENT in covered
    assert any(CLIENT in q for q in calls)
    # 客户排在实体首位 → query 预算优先覆盖它
    order = [q["entity"] for q in pack["queries"] if q.get("lane_kind") == "entity"]
    assert order[0] == CLIENT


# ===========================================================================
# 复审返工 ③ · guide 深档弹性天花板
# ===========================================================================
def test_guide_deep_tier_has_an_elastic_ceiling_not_a_single_point():
    from writing.article_length_contract import (
        FAMILY_LENGTH_POLICIES,
        build_article_length_plan,
    )

    plan = build_article_length_plan(
        "buying_guide", evidence_pack=_rich_pack(12, 6), verified_candidate_count=1,
    )
    assert plan["minimum_chars"] == 15000
    assert plan["target_chars"] == 15000
    # 返工点:改前 max 也是 15000(min=target=max 零弹性,多写一个字都算超上限)
    assert plan["maximum_chars"] == 18000
    assert plan["maximum_chars"] > plan["target_chars"]
    assert FAMILY_LENGTH_POLICIES["implementation_guide"].complex_ceiling_chars == 18000

    # 反向锁:紧凑档三个数一个没动
    policy = FAMILY_LENGTH_POLICIES["implementation_guide"]
    assert (policy.minimum_chars, policy.target_chars, policy.maximum_chars) == (2500, 3500, 4500)
    compact = build_article_length_plan("buying_guide", evidence_pack=_rich_pack(4, 4))
    assert compact["target_chars"] == 3500 and compact["maximum_chars"] == 4500


# ===========================================================================
# C-2 质量修订(Review-CTO 2026-07-27 · 生产 10 篇实证四问题):
# 矩阵烂尾 / 来源说明重复+卡片沉底 / 标注噪音 / 信源提级
# ===========================================================================

def _deep_spec_text():
    from writing.templates.canonical_family_templates import (
        build_deep_ranking_structure_spec,
    )

    return build_deep_ranking_structure_spec(
        {"target_chars": 16000, "verified_candidate_count": 11},
    )


def test_matrix_block_demands_full_rows_not_client_only():
    """生产实证:同口径矩阵表头 11 家、表体只有客户 1 行(烂尾表)。规格必须硬性要求行齐。"""
    spec = _deep_spec_text()
    assert "行必须齐" in spec and "烂尾表" in spec


def test_source_section_must_appear_once_and_cards_not_sink():
    """生产实证:'资料口径与来源说明'整块重复两次、9000 字品牌卡沉底在其后。"""
    spec = _deep_spec_text()
    assert "只在文末出现一次" in spec
    assert "紧随同口径矩阵之后" in spec


def test_annotation_discipline_bans_per_sentence_repetition():
    """生产实证:'（企业提交资料）'每篇 3-14 次逐句重复;泛化'（公开行业报告）'空标注。

    [R3-A3 适配 2026-08-11 · C1/裁决二] 旧规格的解法是「逐句标注 → 收敛成
    一次性声明」;C1 后「一次性声明」本身=软文指纹,整个概念从规格摘除,
    新解法更强:**逐条挂具体外部主体+日期,自曝类标注(含集中声明)整类禁写**。
    原意图(禁逐句重复自曝标注 + 禁泛化空标注)保留,断言钉到新措辞。"""
    spec = _deep_spec_text()
    assert "一次性声明" not in spec, "被摘除的自曝声明概念不许回流规格"
    assert "不做集中自曝声明" in spec
    assert "（企业提交资料）" in spec  # 作为禁用形态被点名列出
    assert "标不具体等于没标" in spec


def test_source_tiering_forbids_selfclaimed_whitepaper_promotion():
    """生产实证:cnblogs 自媒体文被按其自称列为'白皮书'。"""
    spec = _deep_spec_text()
    assert "不得因原文自称" in spec
