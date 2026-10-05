# -*- coding: utf-8 -*-
"""返工单 REWORK-DIAG-QGATE-2026-08-03 判别锁(R1-R4 · 每格成对对照)。

生产病灶(全部实测,不是推断):
  客户「贵州禾泉酒业」(brand 745 · cities='贵州省遵义市仁怀市（茅台镇）')诊断 519,
  8 道测试题里 7 道是「同 14 字地址前缀 + 换后缀」:

      贵州省遵义市仁怀市（茅台镇）商务送礼酱香酒哪家好？
      贵州省遵义市仁怀市（茅台镇）商务送礼酱香酒哪家靠谱？
      …

  而 LLM 原题其实很好(OEM贴牌 / 基酒批发 / 商务送礼 / 招商代理 / 产区排名 /
  性价比 六个场景,**且每条都带地名**),被矫正闸判 `missing_geo_qualifier` 整条
  换掉。三个病根:
    R1 `_CITY_SUFFIX` 结尾锚定 → 带括号地址剥不动 → `city in raw` **判据恒假**;
    R2 `distill_trade` 只产出 1 个品类词 → 模板池 13 条全同前缀;
    R3 `brands.city_scope='national'` 从没接进链路 → super_tier1 被强加地域;
    R4 判不合格就整条推倒 → 原题的品类信息全丢。

本文件每个"必须命中"都配成对的"必须不命中"(返工单 §4.1 硬要求)。
"""
import pytest

from services.diagnosis_question_quality import (
    LAYER_BRAND,
    LAYER_LOCAL,
    LAYER_SCENARIO,
    SCOPE_NATIONAL,
    SCOPE_REGIONAL,
    build_question_templates,
    distill_trade,
    distill_trades,
    enforce_question_quality,
    geo_tokens,
    has_geo_qualifier,
    normalize_city,
    required_geo_layers,
)

# ── brand 745 生产原始输入(逐字段取自生产库,不是编的)──────────────────────
HEQUAN_CITY = "贵州省遵义市仁怀市（茅台镇）"
HEQUAN_BRAND = "贵州禾泉酒业"
HEQUAN_INDUSTRY = "制造业-酒、饮料和精制茶制造业 / 酱香型白酒生产、白酒销售、酒类定制"
HEQUAN_KEYWORDS = [
    "茅台镇酱酒厂家招商", "酱香型白酒OEM贴牌代工", "53度坤沙基酒批发",
    "高性价比坤沙酒", "商务送礼酱香酒", "茅台镇53度纯粮酱香白酒",
]
# diagnosis 519 的 question_quality.repairs 里逐条抄下来的 LLM 原题
HEQUAN_LLM_ORIGINALS = [
    "仁怀市哪家酒厂能做53度坤沙酒OEM贴牌？",
    "遵义茅台镇纯粮酱酒基酒批发找哪家比较好？",
    "茅台镇适合商务送礼的酱香白酒推荐",
    "仁怀市酱香型白酒招商代理哪家实力强？",
    "茅台镇核心产区老酒厂排名前十有哪些？",
    "贵州仁怀性价比高的坤沙酱酒厂家推荐",
]
# 生产里这 7 道题的共同前缀 —— 修完后不许再出现在任何题面里
HEQUAN_GARBAGE_PREFIX = "贵州省遵义市仁怀市（茅台镇）"


# ===========================================================================
# R1 · normalize_city 补括号形态 + has_geo_qualifier 改 token 命中
# ===========================================================================

def test_r1_normalize_city_handles_bracket_forms():
    """必须命中:带括号的整串地址能解析出主地名。"""
    assert normalize_city(HEQUAN_CITY) != HEQUAN_CITY, (
        "带括号地址原样吐回 → 14 字整串直接拼进题面(生产 brand 745 病灶)"
    )
    assert normalize_city(HEQUAN_CITY) == "仁怀"
    assert normalize_city("深圳市（龙岗区）") != "深圳市（龙岗区）"
    assert normalize_city("深圳市（龙岗区）") == "深圳"
    # 🔴 括号外**没有**行政后缀的形态 —— 这两条才真正压在"剥括号"那一步上。
    # (括号外带"市"时,行政区划切块本身就会跳过括号字符,剥不剥都对 → 只测那种
    #  写法的话,把剥括号整段删掉锁照样绿 = 假绿。)
    assert normalize_city("深圳（南山）") == "深圳"
    assert normalize_city("东莞（长安镇）") == "东莞"


def test_r1_normalize_city_does_not_regress_old_forms():
    """必须不命中:旧行为一格都不许退。"""
    # 老锁原样保留(驰鲸 brand 712)
    assert normalize_city("广东省深圳市龙岗区") == "深圳"
    assert normalize_city("深圳") == "深圳"
    assert normalize_city("深圳市") == "深圳"
    assert normalize_city("全国") == ""
    # [Review §7.1] 括号内**不当主地名** —— 否则「广东省深圳市（龙岗区平湖）」
    # 的模板前缀会变"龙岗区平湖",比修之前更糟(天然 A/B:美构用"深圳"0 replacements)
    assert normalize_city("广东省深圳市（龙岗区平湖）") == "深圳", (
        "括号内容只能进 token 集,不许当主地名"
    )


def test_r1_geo_tokens_split_every_admin_level():
    """必须命中:各级地名都进 token 集。"""
    assert geo_tokens(HEQUAN_CITY) == ("贵州", "遵义", "仁怀", "茅台镇")
    assert geo_tokens("广东省深圳市（龙岗区平湖）") == ("广东", "深圳", "龙岗", "平湖")
    # 必须不命中:非城市值不产出 token(否则 has_geo_qualifier 会乱命中)
    assert geo_tokens("全国") == ()
    assert geo_tokens("") == ()


def test_r1_town_level_token_keeps_suffix_no_brand_collision():
    """[Review §7.2] 镇级 token 保留整词 —— 酱酒行业"茅台"是品牌词。"""
    tokens = geo_tokens(HEQUAN_CITY)
    assert "茅台镇" in tokens, "镇级地名必须进 token 集"
    assert "茅台" not in tokens, (
        "token 剥成'茅台'后,任何提茅台**品牌**的题都会被误判'带地域'而跳过矫正"
    )
    # 必须命中:真的写了镇名 → 算带地域
    assert has_geo_qualifier("茅台镇酱酒哪家好？", HEQUAN_CITY) is True
    # 必须不命中:品牌语境的"茅台"不算地域
    assert has_geo_qualifier("酱香酒和茅台的区别是什么？", HEQUAN_CITY) is False


def test_r1_has_geo_qualifier_accepts_any_token_not_verbatim_whole_string():
    """必须命中:禾泉那 6 条 LLM 原题一条都不该被判缺地域。

    🔴 变异锚点:把 has_geo_qualifier 的 token 命中改回 `city in raw`,本断言必须全红。
    """
    for question in HEQUAN_LLM_ORIGINALS:
        assert has_geo_qualifier(question, HEQUAN_CITY) is True, (
            f"「{question}」本来就带地名,判缺地域会让它被整条换成模板题: {question}"
        )


def test_r1_has_geo_qualifier_still_rejects_truly_geo_free_questions():
    """必须不命中:真没地域的题仍要判缺(否则 R1 等于把守卫拆了)。"""
    assert has_geo_qualifier("酱香型白酒哪家好？", HEQUAN_CITY) is False
    assert has_geo_qualifier("酱香型白酒OEM贴牌代工哪家好？", HEQUAN_CITY) is False
    # 老锁:驰鲸口径不许退
    assert has_geo_qualifier("深圳TikTok代运营哪家好？", "广东省深圳市龙岗区") is True
    assert has_geo_qualifier("TikTok代运营哪家好？", "广东省深圳市龙岗区") is False


# ===========================================================================
# R2 · distill_trade 不许只留一个品类词
# ===========================================================================

def test_r2_multiple_trades_distilled_from_keywords():
    """必须命中:6 个关键词 → 多个品类词。"""
    trades = distill_trades(HEQUAN_INDUSTRY, HEQUAN_KEYWORDS, brand_name=HEQUAN_BRAND)
    assert len(trades) >= 3, f"6 个核心词只炼出 {trades} → 模板池必然同质"
    assert len(set(trades)) == len(trades), "品类词不许重复"


def test_r2_single_keyword_does_not_fabricate_trades():
    """必须不命中:只给 1 个关键词不许凭空编造品类词。"""
    trades = distill_trades(HEQUAN_INDUSTRY, ["商务送礼酱香酒"], brand_name=HEQUAN_BRAND)
    assert trades == ["商务送礼酱香酒"], f"凭空多出品类词: {trades}"


def test_r2_zero_keywords_still_produces_pools_without_raising():
    """必须不命中:0 个关键词不许抛异常、不许出空池。"""
    trades = distill_trades(HEQUAN_INDUSTRY, [], brand_name=HEQUAN_BRAND)
    assert trades and all(t.strip() for t in trades)
    pools = build_question_templates(
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        scope=SCOPE_REGIONAL, keywords=[],
    )
    for layer in (LAYER_BRAND, LAYER_LOCAL, LAYER_SCENARIO):
        assert pools[layer], f"{layer} 模板池为空 → 守卫无题可换"


def test_r2_brand_name_never_leaks_into_trades():
    """必须不命中:品类词里不许混进品牌名。"""
    polluted = HEQUAN_KEYWORDS + [f"{HEQUAN_BRAND}酱酒", "禾泉酒业招商"]
    trades = distill_trades(HEQUAN_INDUSTRY, polluted, brand_name=HEQUAN_BRAND)
    for trade in trades:
        assert HEQUAN_BRAND not in trade, f"品牌名泄漏进品类词: {trade}"
        assert "禾泉酒业" not in trade, f"品牌核心词泄漏进品类词: {trade}"


def test_r2_pools_rotate_by_trade_not_by_suffix():
    """必须命中:模板池里出现 ≥3 个不同品类词。

    🔴 变异锚点:把 _pool 的 `trades[idx % len(trades)]` 改回 `trades[0]`,本断言必须红。
    """
    pools = build_question_templates(
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        scope=SCOPE_REGIONAL, keywords=HEQUAN_KEYWORDS,
    )
    trades = distill_trades(HEQUAN_INDUSTRY, HEQUAN_KEYWORDS, brand_name=HEQUAN_BRAND)
    non_brand = pools[LAYER_LOCAL] + pools[LAYER_SCENARIO]
    used = {t for t in trades if any(t in q for q in non_brand)}
    assert len(used) >= 3, (
        f"模板池只用了 {used} —— 生产病灶就是 13 条模板全用同一个品类词"
    )


def test_r2_pools_never_contain_the_raw_address_string():
    """必须不命中:14 字整串地址不许进任何题面(生产 7 道鬼话题的共同前缀)。"""
    pools = build_question_templates(
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        scope=SCOPE_REGIONAL, keywords=HEQUAN_KEYWORDS,
    )
    for pool in pools.values():
        for question in pool:
            assert HEQUAN_GARBAGE_PREFIX not in question, question


def test_r2_trade_carrying_its_own_geo_is_not_double_prefixed():
    """必须不命中:品类词自带地名时不许再加城市前缀(否则"仁怀茅台镇…"双地名鬼话)。"""
    pools = build_question_templates(
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        scope=SCOPE_REGIONAL, keywords=HEQUAN_KEYWORDS,
    )
    for pool in pools.values():
        for question in pool:
            assert "仁怀茅台镇" not in question, f"双地名前缀: {question}"


def test_r2_fallback_to_suffix_rotation_is_logged_not_silent():
    """必须命中:品类词不足回落后缀轮转时要 log 说明原因(不许静默)。"""
    import logging

    logger = logging.getLogger("GEO-QuestionQuality")
    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        build_question_templates(
            brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
            scope=SCOPE_REGIONAL, keywords=["商务送礼酱香酒"],
        )
        assert any("回落后缀轮转" in r.getMessage() for r in records), (
            f"品类词只有 1 个却没 log 回落原因: {[r.getMessage() for r in records]}"
        )
        # 必须不命中:品类词充足时不许打这条回落 log
        records.clear()
        build_question_templates(
            brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
            scope=SCOPE_REGIONAL, keywords=HEQUAN_KEYWORDS,
        )
        assert not any("回落后缀轮转" in r.getMessage() for r in records), (
            "品类词够用却报了回落 —— 这条 log 成了恒真,等于没判别力"
        )
    finally:
        logger.removeHandler(handler)


def test_r2_single_value_helper_stays_consistent_with_plural():
    """旧单值口径(server.py 竞品主语纠偏在用)必须与新多值口径首项一致。"""
    assert distill_trade(HEQUAN_INDUSTRY, HEQUAN_KEYWORDS) == \
        distill_trades(HEQUAN_INDUSTRY, HEQUAN_KEYWORDS)[0]
    # 老锁:不传核心词时不许是登记名录整串
    assert "制造业" not in distill_trade(HEQUAN_INDUSTRY)
    assert len(distill_trade(HEQUAN_INDUSTRY)) <= 14


# ===========================================================================
# R3 · scope 以 brands.city_scope 为准(只升不降)
# ===========================================================================

class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._cursor = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_brand_row(monkeypatch):
    """把 db.connection.get_connection 换成返回指定 brands 行的假连接。"""
    def _install(row):
        import db.connection as dbconn
        holder = {}

        def _fake_get_connection():
            conn = _FakeConn(row)
            holder["conn"] = conn
            return conn

        monkeypatch.setattr(dbconn, "get_connection", _fake_get_connection)
        return holder
    return _install


def test_r3_brand_city_scope_national_overrides_form_regional(fake_brand_row):
    """必须命中:city_scope='national' → super_tier1 层**不**被要求带地域。

    🔴 变异锚点:把 resolve_effective_business_scope 的 return SCOPE_NATIONAL
    改成 return business_scope,本断言必须红。
    """
    from services.diagnosis_business_scope import resolve_effective_business_scope

    holder = fake_brand_row({"city_scope": "national"})
    # 生产实况:表单静默传 'regional'(brand 745 没有 client_profiles 行)
    effective = resolve_effective_business_scope(745, "regional")
    assert effective == SCOPE_NATIONAL
    assert holder["conn"].closed is True, "连接必须归还(不许泄漏)"
    assert LAYER_SCENARIO not in required_geo_layers(effective), (
        "全国生意的超一级词被强加地域前缀 → 违反超一级词自身定义,漏斗评分失真"
    )


def test_r3_super_tier1_questions_carry_no_geo_prefix_when_national():
    """必须命中:city_scope='national' 生效后出的 super_tier1 题不含地域前缀。"""
    pools = build_question_templates(
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        scope=SCOPE_NATIONAL, keywords=HEQUAN_KEYWORDS,
    )
    for question in pools[LAYER_SCENARIO]:
        assert not question.startswith("仁怀"), f"全国客户的场景题带了城市前缀: {question}"


def test_r3_empty_city_scope_keeps_old_regional_behaviour(fake_brand_row):
    """必须不命中:city_scope='' + 表单 regional → 仍按 regional(旧行为保住)。"""
    from services.diagnosis_business_scope import resolve_effective_business_scope

    fake_brand_row({"city_scope": ""})
    assert resolve_effective_business_scope(745, "regional") == "regional"
    fake_brand_row({"city_scope": None})
    assert resolve_effective_business_scope(745, "") == ""
    # 没有 brand_id 时压根不该查库
    assert resolve_effective_business_scope(None, "regional") == "regional"


def test_r3_dead_default_local_must_not_demote_an_explicit_national_form(fake_brand_row):
    """必须不命中:'local' 是死默认,不许把表单里显式选的 national 打回 regional。

    生产 322 条 local / 8 条 national,且 server.py 自己注释写着"313/314 行是死默认"。
    若 'local' 也算权威值,所有正确勾了「全国生意」的客户都会被静默降级 = 修 A 坏 B。
    """
    from services.diagnosis_business_scope import resolve_effective_business_scope

    fake_brand_row({"city_scope": "local"})
    assert resolve_effective_business_scope(745, "national") == "national"
    # local + 表单 regional → 仍 regional(不升也不降)
    fake_brand_row({"city_scope": "local"})
    assert resolve_effective_business_scope(745, "regional") == "regional"


def test_r3_regional_industry_layer_always_requires_geo():
    """必须命中(任何情况下):决策获客层永远要求带地域。"""
    for scope in (SCOPE_REGIONAL, SCOPE_NATIONAL, "", "hybrid", "unknown"):
        assert LAYER_LOCAL in required_geo_layers(scope), scope


def test_r3_db_failure_is_fail_soft(monkeypatch):
    """必须不命中:查库炸了不许阻断诊断,保持传入值。"""
    import db.connection as dbconn
    from services.diagnosis_business_scope import resolve_effective_business_scope

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbconn, "get_connection", _boom)
    assert resolve_effective_business_scope(745, "regional") == "regional"


# ===========================================================================
# R4 · 替换要保留原题信息,别整条推倒
# ===========================================================================

def _enforce_hequan(questions, types, scope=SCOPE_REGIONAL):
    return enforce_question_quality(
        questions, types, brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY,
        city=HEQUAN_CITY, business_scope=scope, engine_count=4,
        keywords=HEQUAN_KEYWORDS, max_tested_questions=8,
    )


def test_r4_geo_only_problem_is_prefixed_keeping_the_trade_words():
    """必须命中:只缺地域 → 补前缀,品类词保留。

    🔴 变异锚点:删掉 `problems == ["missing_geo_qualifier"]` 那条补前缀分支,
    本断言必须红(退回整条替换 → 品类词丢失)。
    """
    question = "酱香型白酒OEM贴牌代工哪家好？"
    result = _enforce_hequan([question], {question: LAYER_LOCAL})
    prefixed = [q for q in result["questions"] if q.endswith(question)]
    assert prefixed, f"原题被整条换掉了: {result['questions']}"
    assert prefixed[0] == f"仁怀{question}"
    assert "OEM贴牌代工" in prefixed[0], "品类信息丢了 → 等于把好题洗成模板题"
    assert result["repairs"][0]["result"] == "prefixed"


def test_r4_multi_problem_question_is_still_fully_replaced():
    """必须不命中:服务名称式短语(同时触发多个问题)仍走整条替换。"""
    question = "专业服务方案"
    result = _enforce_hequan([question], {question: LAYER_LOCAL})
    assert question not in result["questions"], "服务名称式短语不许只补个地域前缀就放行"
    assert not any(q.endswith(question) for q in result["questions"])
    assert result["repairs"][0]["result"] == "replaced"
    assert "service_name_phrase" in result["repairs"][0]["problems"]


def test_r4_repairs_distinguish_prefixed_from_replaced():
    """必须命中:repairs 里能区分 prefixed / replaced。"""
    geo_only = "酱香型白酒OEM贴牌代工哪家好？"
    junk = "专业服务方案"
    result = _enforce_hequan(
        [geo_only, junk], {geo_only: LAYER_LOCAL, junk: LAYER_LOCAL},
    )
    results = {r["original"]: r["result"] for r in result["repairs"] if r["original"]}
    assert results[geo_only] == "prefixed"
    assert results[junk] == "replaced"


def test_r4_repair_trail_is_not_weakened():
    """必须不命中:留痕条数不许减少(SSOT §9.6 不静默丢词)。"""
    geo_only = "酱香型白酒OEM贴牌代工哪家好？"
    junk = "专业服务方案"
    result = _enforce_hequan(
        [geo_only, junk], {geo_only: LAYER_LOCAL, junk: LAYER_LOCAL},
    )
    changed = [r for r in result["repairs"] if r["original"]]
    assert len(changed) == 2, f"两道题都被改了,留痕却只有 {len(changed)} 条: {result['repairs']}"
    for repair in changed:
        assert repair["replacement"], "改了却没记 replacement = 静默丢词"
        assert repair["problems"]


# ===========================================================================
# 端到端(离线):brand 745 生产原始输入整条过一遍闸
# ===========================================================================

def test_e2e_hequan_llm_questions_survive_the_gate():
    """返工单 §4.2 的落库判据,离线先锁一遍(线上重跑前的判别锁)。"""
    types = dict.fromkeys(HEQUAN_LLM_ORIGINALS[:4], LAYER_LOCAL)
    types.update(dict.fromkeys(HEQUAN_LLM_ORIGINALS[4:], LAYER_SCENARIO))
    brand_q = f"{HEQUAN_BRAND}是做什么的？"
    types[brand_q] = LAYER_BRAND
    result = _enforce_hequan([brand_q] + HEQUAN_LLM_ORIGINALS, types, scope=SCOPE_NATIONAL)

    replaced = [r for r in result["repairs"] if r["result"] in ("replaced", "prefixed")]
    # 判据 1:repairs ≤ 2(现状 7)
    assert len(replaced) <= 2, [r["original"] for r in replaced]
    # 判据 3:题面不含 14 字整串
    for question in result["questions"]:
        assert HEQUAN_GARBAGE_PREFIX not in question, question
    # 判据 2:8 题里 ≥3 个不同品类词(用原题自带的品类信号数)
    signals = ["OEM贴牌", "基酒批发", "商务送礼", "招商代理", "老酒厂", "坤沙"]
    hit = {s for s in signals if any(s in q for q in result["questions"])}
    assert len(hit) >= 3, f"8 题只覆盖了 {hit} 个品类场景"
    # 6 条 LLM 好题一条都不许丢
    for question in HEQUAN_LLM_ORIGINALS:
        assert question in result["questions"], f"好题被换掉了: {question}"


def test_e2e_normal_city_brands_do_not_regress():
    """§4.3 不回退:城市写法本来就正常的品牌,repairs 不许上升。"""
    questions = ["深圳TikTok代运营哪家好？", "深圳TikTok代运营怎么选？", "驰鲸科技是什么公司？"]
    types = {questions[0]: LAYER_LOCAL, questions[1]: LAYER_SCENARIO,
             questions[2]: LAYER_BRAND}
    result = enforce_question_quality(
        questions, types, brand_name="深圳市驰鲸科技有限公司",
        industry="科技推广和应用服务业 / TikTok海外B2B精准获客、外贸社媒全案营销",
        city="广东省深圳市龙岗区", business_scope=SCOPE_REGIONAL, engine_count=5,
        keywords=["TikTok工厂代运营", "TikTok外贸B2B获客", "低成本外贸获客渠道"],
    )
    changed = [r for r in result["repairs"] if r["original"]]
    assert changed == [], f"本来 0 替换的品牌被修出了替换: {changed}"
