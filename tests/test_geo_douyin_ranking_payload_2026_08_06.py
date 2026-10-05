# -*- coding: utf-8 -*-
"""榜单冻结合同锁 · `WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06` v3

每一组都配**成对的反向对照**(必须命中面 + 必须不命中面)——
只写"必须命中"的锁抓不出"锚点撞到别处"那一类,而那一类永远不会变红。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from services.geo_douyin import ranking_payload as rp


REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "services" / "geo_douyin" / "ranking_payload.py"


# ===========================================================================
# 一 · 实体归并边界(P1-3)
# ===========================================================================

@pytest.mark.parametrize("a,b", [
    ("深圳市恒通电梯有限公司", "恒通电梯"),
    ("惠州市远大电梯有限公司", "远大电梯"),
    ("揭阳市大昀地产有限公司", "大昀地产"),
    ("深圳市晨光富士电梯有限公司", "晨光富士电梯"),
    ("浙江岱林生物技术股份有限公司", "岱林生物技术"),
])
def test_safe_merge_joins_same_entity_written_differently(a, b):
    """确定性合并:同一主体的不同书写形式必须合。"""
    assert rp.safe_merge_key(a) == rp.safe_merge_key(b) != ""


@pytest.mark.parametrize("a,b", [
    # 🔴 地域词吃掉主体身份 —— 与 split_brand_aliases 那个 P0 同形态
    ("北京银行", "上海银行"),
    ("广州酒家", "深圳酒家"),
    ("四川航空", "广东航空"),
    ("北京银行", "银行"),
    # 🔴 行业名词是身份的一部分(brand_identity_resolver.py:147-150 的 P0 边界)
    ("通力", "通力电梯"),
    ("日立", "日立电梯"),
    ("通力", "巨人通力"),
    ("深圳市晨光富士电梯", "惠州富士电梯有限公司"),
    # 剥到只剩纯通名的键没有身份意义
    ("深圳市电梯有限公司", "电梯"),
])
def test_safe_merge_never_collapses_distinct_entities(a, b):
    """反向对照:这些**绝不能**被合。合错代价远大于漏合。"""
    assert rp.safe_merge_key(a) != rp.safe_merge_key(b)


@pytest.mark.parametrize("a,b,same", [
    ("通力", "通力电梯", True),
    ("日立", "日立电梯", True),
    ("通力", "巨人通力", False),
    ("晨光富士电梯", "富士电梯", False),
    ("北京银行", "上海银行", False),
])
def test_family_key_is_display_dedupe_only(a, b, same):
    assert (rp.family_key(a) == rp.family_key(b)) is same


def test_family_key_must_not_be_used_to_aggregate():
    """接线锁:`family_key` 只许出现在去重展示处,不许参与任何计数加总。

    判据形态说明:`select_entities` 里 `family_key` 的结果只能进 `seen_family` /
    `dropped`,**不得**出现在 `mention_count` / `engine_count` 的赋值右侧。
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "select_entities")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        targets = ast.dump(ast.Module(body=[ast.Expr(t) for t in node.targets],
                                      type_ignores=[]))
        if "mention_count" in targets or "engine_count" in targets:
            assert "family_key" not in ast.dump(node.value), (
                "family_key 参与了计数加总 —— 它只允许用于去重展示")


# ===========================================================================
# 二 · 广告法:词表同源 + 语境匹配(P1-1)
# ===========================================================================

@pytest.mark.parametrize("text", [
    "这是我第一次做GEO", "第一步先看诊断报告", "已经进入第一梯队",
    "第一眼看上去还行", "第一时间响应", "最大化收益", "最大限度降低风险",
])
def test_ad_law_does_not_fire_on_benign_context(text):
    """🔴 反向对照:实测 28% 误报的那一批必须不命中。

    「锚点撞到别处」与「锚点没命中」表现相反、危害相同 —— 后者会红,前者永远不红。
    """
    assert rp.absolute_law_hits(text) == []


@pytest.mark.parametrize("text", [
    "全国第一的服务商", "行业最好的选择", "国内首选品牌",
    "世界级技术团队", "排名第一", "国家级资质", "史上最强方案",
])
def test_ad_law_fires_on_real_absolute_claims(text):
    assert rp.absolute_law_hits(text), f"漏检:{text}"


def test_ad_law_wordlist_comes_from_signed_pack_not_a_local_list():
    """🔴 接线锁:词表必须来自 Owner 签发的版本化包,**不许本模块自建第二份**。

    形态判据:模块内不得出现「国家级/最高级/最佳」这类词的字面量集合;
    `absolute_law_hits` 必须真的调用 `legal_pack()`。
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "absolute_law_hits")
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "legal_pack" in calls, "absolute_law_hits 没有从签发包取词表"

    # 反向面:模块里不得有自建的广告法词表常量
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            continue
        literals = {e.value for e in node.value.elts if isinstance(e, ast.Constant)
                    and isinstance(e.value, str)}
        overlap = literals & {"国家级", "最高级", "最佳", "最好", "第一", "顶级"}
        assert len(overlap) < 3, f"疑似自建广告法词表:{overlap}"


def test_signed_pack_actually_covers_the_terms_the_old_list_missed():
    """签发包必须真的含有旧 15 词表缺的那 12 个 —— 否则同源改造没有意义。"""
    from services.marketing.guards import legal_pack
    words = set(legal_pack().get("ad_law") or ())
    missing_before = {"最好", "最优", "最强", "第一", "第一名", "全国第一",
                      "首个", "首选", "世界级", "史上最"}
    assert missing_before <= words, f"签发包没覆盖:{missing_before - words}"


# ===========================================================================
# 三 · 来源驱动清洗(P1-3)
# ===========================================================================

def test_verifiable_source_is_kept_even_when_it_reads_strong():
    """🔴 反向对照:**有可核验来源的表述必须放行**,不得因为字面强就误杀。"""
    got = rp.clean_phrases(
        ["信通院《GEO服务能力评价要求》国家标准的核心起草单位"],
        source_urls=["https://example.gov.cn/spec"])
    assert got[0].kept is True
    assert got[0].source_type == rp.SOURCE_VERIFIABLE


@pytest.mark.parametrize("phrase", [
    "技术评分高达99.5分", "客户续费率96%", "TOP10榜单品牌",
    "10强服务商", "500强供应商", "A级资质", "承诺关键词进首页",
])
def test_ai_paraphrased_third_party_claims_do_not_reach_the_image(phrase):
    """仅 AI 转述的第三方榜单结论/资质宣称 → 不上图(替第三方虚假宣传的风险)。"""
    got = rp.clean_phrases([phrase])
    assert got[0].kept is False
    assert got[0].source_type == rp.SOURCE_AI_PARAPHRASE
    assert got[0].reason


@pytest.mark.parametrize("phrase", ["合规", "国家标准", "24小时响应", "1-16吨载重",
                                    "非标井道改造", "本地维保网点"])
def test_neutral_feature_tags_survive_cleaning(phrase):
    """反向对照:中性特征词是榜单的主要信息,**不得被清洗器一并杀掉**。"""
    got = rp.clean_phrases([phrase])
    assert got[0].kept is True, f"误杀中性特征词:{phrase}"


def test_legal_absolute_is_dropped_not_raised():
    """清洗是**降级**不是拒绝 —— 永远不抛异常、不中断整条内容(永不中断铁律)。"""
    got = rp.clean_phrases(["国内首个AI原生GEO系统", "合规"])
    assert got[0].kept is False and got[0].source_type == rp.SOURCE_LEGAL_ABSOLUTE
    assert got[1].kept is True          # 同一批里的其它条目不受牵连


def test_cleaning_happens_before_freezing_by_construction():
    """接线锁:`FrozenRankingPayload` 不得自己调用清洗 —— 清洗必须在构建前完成。

    否则"冻结快照不可变"就是假的(渲染期还能改内容)。
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "FrozenRankingPayload")
    names = {n.func.id for n in ast.walk(cls)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "clean_phrases" not in names
    assert "classify_phrase" not in names


# ===========================================================================
# 四 · 聚合合同七要素(P1-2)· 本单最容易被绕过的一条
# ===========================================================================

def _item():
    """🔴 2026-08-06 返工:`source` 必须带**完整的同行四元组**。

    改前这个 fixture 只给 engine + rank —— 而 `rank_statement` 现在要求
    engine/rank/query/observed_at **同行齐全**才敢断言引擎与名次(缺元就退回
    中性表述)。fixture 不补齐的话,这条锁测的就不再是它想测的东西。
    四元组本身的判别力另有 `test_geovid_ranking_samerow_2026_08_06.py` 专管。
    """
    return rp.RankingItem(rank=1, display_name="泓动数据",
                          source={"engine": "DeepSeek", "recommendation_rank": 2,
                                  "query": "GEO优化公司推荐",
                                  "observed_at": "2026-08-06T14:10:36+08:00"})


def test_incomplete_aggregation_must_not_say_industry_ranking():
    """🔴 七要素不齐 → 产出里**绝不能**出现「综合/行业/全国排名」。"""
    c = rp.AggregationContract(query="深圳载货电梯哪家好")
    assert not c.complete
    said = rp.rank_statement(_item(), c)
    for banned in ("综合排名", "行业第", "全国第", "行业排名"):
        assert banned not in said, f"七要素不齐却说了「{banned}」:{said}"
    assert "DeepSeek" in said and "第 2" in said, said


def test_complete_aggregation_may_say_composite_ranking():
    """反向对照:七要素齐时**必须**允许说综合排名,否则这条闸就是恒真的。"""
    c = rp.AggregationContract(
        query="深圳载货电梯哪家好", candidate_set="共识≥2引擎", window="近30天",
        engine_scope=("deepseek", "kimi"), name_normalization="safe_merge_key v1",
        tie_rule="并列取提及数高", algo_version="rank-v1")
    assert c.complete and c.missing() == []
    assert "综合排名" in rp.rank_statement(_item(), c)


@pytest.mark.parametrize("drop", list(rp.AGGREGATION_ELEMENTS))
def test_every_one_of_the_seven_elements_is_load_bearing(drop):
    """七要素**每一条**都必须承重 —— 少任何一条都不许说综合排名。

    这条防的是"七要素写进文档但代码只查其中三条"。
    """
    kw = dict(query="q", candidate_set="c", window="w", engine_scope=("e",),
              name_normalization="n", tie_rule="t", algo_version="v")
    kw[drop] = () if drop == "engine_scope" else ""
    c = rp.AggregationContract(**kw)
    assert not c.complete and drop in c.missing()
    assert "综合排名" not in rp.rank_statement(_item(), c)


def test_cross_engine_consensus_is_a_count_not_a_ranking():
    """跨引擎共识是**计数事实**,任何时候都可以说(反向对照:不许被一起禁掉)。"""
    assert rp.consensus_statement(4) == "4 个 AI 都提到"
    assert rp.consensus_statement(1) == ""
    assert "排名" not in rp.consensus_statement(4)


# ===========================================================================
# 五 · 冻结与持久化八要素(P1-4)
# ===========================================================================

def test_contract_hash_is_stable_and_content_bound():
    a = rp.FrozenRankingPayload(template_id="top3_provider", title="T", items=(_item(),))
    b = rp.FrozenRankingPayload(template_id="top3_provider", title="T", items=(_item(),))
    assert a.contract_hash() == b.contract_hash()
    b.title = "T2"
    assert a.contract_hash() != b.contract_hash()


def test_idempotency_key_binds_post_and_contract():
    p = rp.FrozenRankingPayload(template_id="t", title="T")
    k1, k2 = p.idempotency_key(19), p.idempotency_key(20)
    assert k1 != k2 and k1.startswith("ranking:19:")


def test_source_snapshot_is_carried_not_referenced():
    """🔴 举证链必须**自带快照** —— `keyword_insights` 会被 ON CONFLICT DO UPDATE 覆盖。"""
    it = rp.RankingItem(source={"engine": "DeepSeek", "recommendation_rank": 2,
                                "extractor_version": "answer_entity_v2_2026-07-03",
                                "llm_model": "qwen3.7-max",
                                "observed_at": "2026-08-06T14:10:36+08:00",
                                "entity_row_ids": [1, 2]})
    d = it.to_dict()["source"]
    for k in ("engine", "recommendation_rank", "extractor_version",
              "llm_model", "observed_at", "entity_row_ids"):
        assert k in d, f"举证链缺 {k}"


def test_only_ranking_form_may_use_ranking_wording():
    """D12④:无据分支切形态后**必须改名**,不许挂「排行榜」把客户放榜外。"""
    assert rp.FrozenRankingPayload(form=rp.FORM_RANKING).allows_ranking_wording()
    assert not rp.FrozenRankingPayload(form=rp.FORM_SCENARIO).allows_ranking_wording()
    assert not rp.FrozenRankingPayload(form=rp.FORM_MATRIX).allows_ranking_wording()


# ===========================================================================
# 六 · 候选选取(留痕完整性)
# ===========================================================================

_CANDS = [
    {"entity_name": "深圳市恒通电梯有限公司", "mention_count": 36, "engine_count": 4},
    {"entity_name": "恒通电梯", "mention_count": 19, "engine_count": 4},
    {"entity_name": "通力", "mention_count": 24, "engine_count": 4},
    {"entity_name": "通力电梯", "mention_count": 18, "engine_count": 4},
    {"entity_name": "快意电梯", "mention_count": 31, "engine_count": 4},
    {"entity_name": "康力电梯", "mention_count": 26, "engine_count": 4},
    {"entity_name": "奥的斯", "mention_count": 25, "engine_count": 4},
    {"entity_name": "某小厂", "mention_count": 2, "engine_count": 1},
]


def test_deterministic_merge_sums_counts():
    picked, _ = rp.select_entities(_CANDS, want=4)
    top = picked[0]
    assert top["entity_name"] == "深圳市恒通电梯有限公司"
    assert top["mention_count"] == 55        # 36 + 19,确定性合并才允许加总
    assert set(top["_merged_names"]) == {"深圳市恒通电梯有限公司", "恒通电梯"}


def test_family_duplicate_is_recorded_with_its_real_reason():
    """🔴 同族去重必须**独立于名额**留痕 —— 否则"被去重"和"没轮到"事后分不开。"""
    _, dropped = rp.select_entities(_CANDS, want=4)
    fam = [d for d in dropped if d["name"] == "通力电梯"]
    assert fam and fam[0].get("same_family_as") == "通力"
    assert "名额" not in fam[0]["reason"]


def test_consensus_floor_excludes_single_engine_candidates():
    _, dropped = rp.select_entities(_CANDS, want=4)
    assert any(d["name"] == "某小厂" and "共识" in d["reason"] for d in dropped)


def test_family_dedupe_never_sums_across_families():
    """反向对照:同族的两条**不得**被加总(那会造出一个不存在的提及数)。"""
    picked, _ = rp.select_entities(_CANDS, want=6)
    by_name = {p["entity_name"]: p for p in picked}
    if "通力" in by_name:
        assert by_name["通力"]["mention_count"] == 24    # 不是 24+18


def test_entity_count_is_clamped_not_rejected():
    """数量越界是**夹取**不是报错(工单 §1.4:默认值,不是判废闸)。"""
    for want, expect_max in ((1, rp.ENTITY_COUNT_MIN), (99, rp.ENTITY_COUNT_MAX)):
        picked, _ = rp.select_entities(_CANDS, want=want)
        assert len(picked) <= expect_max


# ===========================================================================
# 七 · 零硬拦(P1-1 执行层级)
# ===========================================================================

def test_module_never_raises_on_content_problems():
    """🔴 内容类问题一律降级,**不抛异常** —— 永不中断对话铁律。"""
    nasty = ["国内首个", "TOP10榜单品牌", "", None, "全国第一", "客户续费率96%"]
    out = rp.clean_phrases(nasty)          # 不抛
    assert any(c.kept for c in out) or all(not c.kept for c in out)


def test_no_blocking_verbs_in_module():
    """形态判据:本模块不得出现 raise HTTPException / blocked / reject 这类硬拦语义。"""
    src = MODULE_PATH.read_text(encoding="utf-8")
    body = re.sub(r'"""[\s\S]*?"""', "", src)      # 剥 docstring,别撞自己写的说明
    body = re.sub(r"#.*", "", body)                # 剥注释
    for banned in ("HTTPException", "raise ValueError", "blocked", "reject"):
        assert banned not in body, f"模块里出现硬拦语义:{banned}"
