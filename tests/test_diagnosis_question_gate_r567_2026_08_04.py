# -*- coding: utf-8 -*-
"""返工单 REWORK-DIAG-QGATE R5/R6/R7 判别锁(2026-08-04 · 每格成对对照)。

生产病灶(全部 RO 实测,不是推断)—— 客户「深圳港融医疗美容」brand 737:

    brands.cities   = '广东省深圳市龙岗区'   (档案,从没变过)
    brands.industry = '卫生和社会工作 / 医疗美容服务'

  · 诊断 509(08-01) 表单 client_location = '广东省深圳市龙岗区'(= 档案)
      → 闸内 city 快照 '深圳' · repairs **0 条** · 8 道题全干净
  · 诊断 529(08-04) 表单 client_location = '广东省，香港'
      → 闸内 city 快照 '广东' · repairs **5 条**:
          4 × prefixed  「深圳做私密整形…」→「广东深圳做私密整形…」  (双地名赘字)
          1 × added     「广东深圳龙岗医美哪家方案更适合中小企业？」  (医美被出 B2B 后缀)
        另有 2 道 LLM 抄 prompt 示例出来的
          「广东卫生和社会工作 / 医疗美容服务哪家靠谱？」            (分类学串进题面)
        —— 这 2 道**带地域又带"哪家靠谱"**,选词矫正闸判它们完全合格、一个字不改。

  同一品牌 / 同一份档案 / 同一套规则,509 与 529 的差别**只在表单值** ——
  这是一组天然 A/B,证明病根是「地域 token 池只吃表单值」,不是矫正规则本身。

三条修法(对应 R5 / R6 / R7):
  R5 地域 token 池 = 表单值 ∪ brands.cities;主地名在表单没到市级时回落档案市级;
     题面已含任一档案地名 token → 不再前缀。
  R6 品类词回落禁吐行业分类名录用语;回落链 L2 段 → 口语化 → 品牌经营词 → 宁缺毋滥(留痕)。
  R7 「哪家方案更适合中小企业」这类 B2B 后缀**默认关闭**,只对 B2B 行业启用。

本文件每个"必须命中"都配成对的"必须不命中"(返工单 §4.1 硬要求)。
"""
import ast
import inspect
import pathlib

import pytest

from services.diagnosis_question_quality import (
    AUDIENCE_B2B,
    AUDIENCE_CONSUMER,
    LAYER_BRAND,
    LAYER_LOCAL,
    LAYER_SCENARIO,
    SCOPE_NATIONAL,
    SCOPE_REGIONAL,
    build_question_templates,
    city_level_name,
    colloquialize_trade,
    distill_trade,
    distill_trades,
    enforce_question_quality,
    has_geo_qualifier,
    industry_audience,
    looks_like_taxonomy_term,
    merged_geo_tokens,
    normalize_city,
    resolve_primary_geo,
    trade_from_brand_name,
    trade_from_industry,
)

# ── brand 737 生产原始输入(逐字段取自生产库)────────────────────────────────
GANGRONG_BRAND = "深圳港融医疗美容"
GANGRONG_INDUSTRY = "卫生和社会工作 / 医疗美容服务"
GANGRONG_ARCHIVE_CITIES = "广东省深圳市龙岗区"      # brands.cities
GANGRONG_FORM_529 = "广东省，香港"                  # 529 的表单值(病灶)
GANGRONG_FORM_509 = "广东省深圳市龙岗区"            # 509 的表单值(= 档案 · 对照组)
GANGRONG_KEYWORDS_529 = [
    "深圳产后漏尿做3D生物束带效果好吗", "深圳私密整形术后恢复期多久", "深圳私密整形推荐",
    "深圳产后修复去哪个医院好", "私密整形多少钱", "私密激光和手术哪个好",
    "深圳龙岗私密整形", "深圳菲蜜丽价格", "深圳3D生物束带效果", "深圳龙岗医美",
    "深圳龙岗私密整形医院哪家好", "深圳私密漂红哪家医院好", "深圳私密医美哪家好",
    "深圳私密激光", "深圳私密抗衰", "深圳菲蜜丽哪家医院好", "深圳产后修复哪家好",
    "深圳3D生物束带哪家医院做", "医美医院怎么选", "深圳私密整形",
]
# 529 的 LLM 原题(把闸加上去的"广东"前缀还原掉 = LLM 实际交上来的样子)
GANGRONG_LLM_ORIGINALS = [
    "深圳港融医疗美容是做什么的？",
    "深圳做私密整形哪家医院比较靠谱？",
    "深圳港融医疗美容是什么公司？",
    "深圳菲蜜丽私密激光哪家医院做得好？",
    "深圳私密漂红和抗衰哪家好？",
    "广东地区做女性私密修复正规的医美机构有哪些？",
]
GANGRONG_TYPES = {
    GANGRONG_LLM_ORIGINALS[0]: LAYER_BRAND,
    GANGRONG_LLM_ORIGINALS[1]: LAYER_LOCAL,
    GANGRONG_LLM_ORIGINALS[2]: LAYER_LOCAL,
    GANGRONG_LLM_ORIGINALS[3]: LAYER_LOCAL,
    GANGRONG_LLM_ORIGINALS[4]: LAYER_LOCAL,
    GANGRONG_LLM_ORIGINALS[5]: LAYER_LOCAL,
}

# brand 745(R1-R4 的病灶客户)—— 本包必须**不回退**
HEQUAN_BRAND = "贵州禾泉酒业"
HEQUAN_INDUSTRY = "制造业-酒、饮料和精制茶制造业 / 酱香型白酒生产、白酒销售、酒类定制"
HEQUAN_CITY = "贵州省遵义市仁怀市（茅台镇）"
HEQUAN_KEYWORDS = [
    "茅台镇酱酒厂家招商", "酱香型白酒OEM贴牌代工", "53度坤沙基酒批发",
    "高性价比坤沙酒", "商务送礼酱香酒", "茅台镇53度纯粮酱香白酒",
]
HEQUAN_LLM_ORIGINALS = [
    "仁怀市哪家酒厂能做53度坤沙酒OEM贴牌？",
    "遵义茅台镇纯粮酱酒基酒批发找哪家比较好？",
    "茅台镇适合商务送礼的酱香白酒推荐",
    "仁怀市酱香型白酒招商代理哪家实力强？",
    "茅台镇核心产区老酒厂排名前十有哪些？",
    "贵州仁怀性价比高的坤沙酱酒厂家推荐",
]


def _run_529(**overrides):
    kwargs = dict(
        brand_name=GANGRONG_BRAND, industry=GANGRONG_INDUSTRY,
        city=GANGRONG_FORM_529, business_scope=SCOPE_REGIONAL,
        engine_count=4, keywords=GANGRONG_KEYWORDS_529, max_tested_questions=8,
        brand_cities=GANGRONG_ARCHIVE_CITIES,
    )
    kwargs.update(overrides)
    return enforce_question_quality(list(GANGRONG_LLM_ORIGINALS), dict(GANGRONG_TYPES), **kwargs)


# ===========================================================================
# R5 · 地域 token 池 = 表单值 ∪ brands.cities;主地名回落档案市级
# ===========================================================================

def test_r5_merged_token_pool_contains_archive_place_names():
    """必须命中:表单只给了"广东/香港",档案的"深圳/龙岗"也必须在 token 池里。

    🔴 变异锚点:把 merged_geo_tokens 里的合并循环删掉(只返回表单 token),本断言必须红。
    """
    tokens = merged_geo_tokens(GANGRONG_FORM_529, GANGRONG_ARCHIVE_CITIES)
    assert "广东" in tokens and "香港" in tokens, f"表单地名丢了: {tokens}"
    assert "深圳" in tokens, f"档案的市级地名没进 token 池: {tokens}"
    assert "龙岗" in tokens, f"档案的区级地名没进 token 池: {tokens}"


def test_r5_form_only_token_pool_is_the_broken_baseline():
    """必须不命中(反向对照):不给 brand_cities 时,池子里就是**没有**"深圳"。

    这一格证明上面那格测的是"合并"这件事本身,不是"深圳恰好从别处混进来了"。
    """
    tokens = merged_geo_tokens(GANGRONG_FORM_529, "")
    assert "深圳" not in tokens and "龙岗" not in tokens, (
        f"没传档案却拿到了档案地名 → 上一格的断言没有判别力: {tokens}"
    )


def test_r5_question_with_archive_place_name_counts_as_geo_qualified():
    """必须命中:题面含档案地名"深圳" → 算带地域 → **不再前缀**。"""
    text = "深圳做私密整形哪家医院比较靠谱？"
    assert has_geo_qualifier(text, GANGRONG_FORM_529, GANGRONG_ARCHIVE_CITIES) is True
    # 反向对照:同一道题在"只吃表单值"的口径下被判缺地域 —— 这就是 529 的病灶
    assert has_geo_qualifier(text, GANGRONG_FORM_529) is False


def test_r5_question_without_any_place_name_still_judged_missing_geo():
    """必须不命中:真没地域的题,合并 token 池后仍要判缺地域(闸不许被合并弄废)。"""
    assert has_geo_qualifier("私密整形多少钱？", GANGRONG_FORM_529, GANGRONG_ARCHIVE_CITIES) is False
    assert has_geo_qualifier("医美医院怎么选？", GANGRONG_FORM_529, GANGRONG_ARCHIVE_CITIES) is False


def test_r5_primary_geo_falls_back_to_archive_city_when_form_is_province_level():
    """必须命中:表单只到省级("广东省，香港")→ 主地名用档案的市级"深圳"。

    🔴 变异锚点:把 resolve_primary_geo 的 `return archive_city` 改成
    `return form_town`,本断言必须红。
    """
    assert resolve_primary_geo(GANGRONG_FORM_529, GANGRONG_ARCHIVE_CITIES) == "深圳"
    # 反向对照:同一表单值不给档案 → 仍是省级"广东"(病灶原样)
    assert resolve_primary_geo(GANGRONG_FORM_529, "") == "广东"


def test_r5_form_city_level_value_wins_over_archive():
    """必须不命中:表单**已到市级**时,档案不许覆盖它(修 A 坏 B 的防线)。

    生产分布:37 个品牌的 brands.cities 与最近一次诊断表单值不一致,其中十余个
    是**表单值才对**(brand 126 档案"江苏省扬州市宝应县夏集镇" / 表单"哈尔滨市";
    brand 76 档案"中国·深圳市龙岗区" / 表单"贵阳")。档案无条件优先 = 把这些客户
    的诊断城市改成他们没选的城市。
    """
    assert resolve_primary_geo("哈尔滨市", "江苏省扬州市宝应县夏集镇") == "哈尔滨"
    assert resolve_primary_geo("贵阳", "中国·深圳市龙岗区") == "贵阳"
    # 509 的对照:表单 = 档案 → 结果与 R1-R4 上线时一致
    assert resolve_primary_geo(GANGRONG_FORM_509, GANGRONG_ARCHIVE_CITIES) == "深圳"


def test_r5_no_form_place_name_keeps_gate_closed():
    """必须不命中:表单是"全国"/空 → 主地名仍为空,**矫正闸保持关闭**。

    不因为档案里有地名,就给一个从来不出地域题的客户开闸(旧行为逐字节保住)。
    """
    assert resolve_primary_geo("全国", GANGRONG_ARCHIVE_CITIES) == ""
    assert resolve_primary_geo("", GANGRONG_ARCHIVE_CITIES) == ""
    assert resolve_primary_geo("海外", GANGRONG_ARCHIVE_CITIES) == ""


def test_r5_city_level_name_distinguishes_province_from_city():
    """必须命中/不命中成对:city_level_name 只认市级。"""
    assert city_level_name("广东省深圳市龙岗区") == "深圳"
    assert city_level_name("贵州省遵义市仁怀市（茅台镇）") == "仁怀"
    assert city_level_name("哈尔滨市") == "哈尔滨"
    # 反向:只有省级 / 非城市值 → 空串(normalize_city 会兜底吐"广东",这里必须不吐)
    assert city_level_name("广东省，香港") == ""
    assert city_level_name("广东省") == ""
    assert city_level_name("全国") == ""
    assert city_level_name("") == ""
    assert normalize_city("广东省，香港") == "广东", "对照:normalize_city 仍会兜底吐省名"


def test_r5_529_scenario_end_to_end_no_double_place_name():
    """必须命中(病灶原样复现):529 全套输入 → **一条双地名赘字都不许有**。"""
    result = _run_529()
    assert result["city"] == "深圳", f"主地名没修好: {result['city']}"
    for question in result["questions"]:
        assert "广东深圳" not in question, f"双地名赘字仍在: {question}"
    # 4 条 prefixed 修复必须全部消失
    prefixed = [r for r in result["repairs"] if r["result"] == "prefixed"]
    assert prefixed == [], f"仍在前缀改写 LLM 好题: {prefixed}"
    # LLM 的 5 道非品牌层原题必须原样保留(R4 的"不整条推倒"不许回退)
    for original in GANGRONG_LLM_ORIGINALS:
        assert original in result["questions"], f"LLM 原题被换掉了: {original}"


def test_r5_529_baseline_without_archive_still_reproduces_the_bug():
    """必须不命中(反向对照):不传 brand_cities → 4 条双地名赘字**原样复现**。

    这一格是上一格的判别力证明:没有它,"没有广东深圳"可能只是因为题面本来就不会那样拼。
    """
    result = _run_529(brand_cities="")
    assert result["city"] == "广东"
    doubled = [q for q in result["questions"] if "广东深圳" in q]
    assert len(doubled) >= 4, f"基线里应当能复现 4 条双地名,实际 {len(doubled)}: {doubled}"


def test_r5_509_scenario_does_not_regress():
    """必须不命中:509 场景(表单 = 档案)修完后仍然 0 条替换/前缀。"""
    result = _run_529(city=GANGRONG_FORM_509)
    assert result["city"] == "深圳"
    bad = [r for r in result["repairs"] if r["result"] in ("prefixed", "replaced")]
    assert bad == [], f"509 场景出现了本不该有的矫正: {bad}"


def test_r5_snapshot_records_both_city_inputs():
    """必须命中:落库快照必须同时记下表单值与档案值(529 复盘卡在这里)。"""
    snapshot = _run_529()["city_inputs"]
    assert snapshot["form"] == GANGRONG_FORM_529
    assert snapshot["brand_cities"] == GANGRONG_ARCHIVE_CITIES
    assert "深圳" in snapshot["geo_tokens"]


# ===========================================================================
# R6 · 品类词回落禁吐行业分类名录用语
# ===========================================================================

def test_r6_taxonomy_string_never_reaches_a_question():
    """必须命中:industry 是登记整串时,题面里不许出现它。

    🔴 变异锚点:把 trade_from_industry 的 `if looks_like_taxonomy_term(cand): return ""`
    删掉,本断言必须红。
    """
    pools = build_question_templates(
        brand_name=GANGRONG_BRAND, industry=GANGRONG_INDUSTRY,
        city=GANGRONG_ARCHIVE_CITIES, scope=SCOPE_REGIONAL, keywords=[],
    )
    for layer, questions in pools.items():
        for question in questions:
            assert GANGRONG_INDUSTRY not in question, f"{layer} 出现分类学整串: {question}"
            assert "卫生和社会工作" not in question, f"{layer} 出现门类名: {question}"


def test_r6_l2_segment_is_colloquialized():
    """必须命中:取 L2 段并口语化 —— 医疗美容服务 → 医美。"""
    assert trade_from_industry(GANGRONG_INDUSTRY) == "医美"
    assert colloquialize_trade("医疗美容服务") == "医美"
    assert colloquialize_trade("餐饮业") == "餐饮"
    # 反向:口语化不许把门类名"缩短成鬼话"("制造业"→"制造"不是口语化)
    assert colloquialize_trade("制造业") == "制造业"
    assert looks_like_taxonomy_term(colloquialize_trade("制造业")) is True


def test_r6_pure_l1_industry_yields_no_trade():
    """必须命中:industry 恰好是门类名(生产有 6 个品牌 industry='制造业')→ 炼不出品类词。"""
    assert trade_from_industry("制造业") == ""
    assert trade_from_industry("卫生和社会工作") == ""
    assert trade_from_industry("租赁和商务服务业") == ""
    # 反向对照:正常品类词不许被误杀
    assert trade_from_industry("医疗美容服务") == "医美"
    assert trade_from_industry("特色烤鱼正餐服务") == "特色烤鱼正餐"


def test_r6_falls_back_to_brand_operating_word():
    """必须命中:industry 不可用 → 退品牌经营词(深圳市恒通电梯有限公司 → 电梯)。"""
    assert trade_from_brand_name("深圳市恒通电梯有限公司") == "电梯"
    assert trade_from_brand_name(GANGRONG_BRAND) == "医美"
    assert distill_trades("制造业", [], brand_name="深圳市恒通电梯有限公司") == ["电梯"]
    # 反向对照:品牌名里没有任何品类线索 → 不许硬造
    assert trade_from_brand_name("贵州禾泉酒业") == ""
    assert trade_from_brand_name("") == ""


def test_r6_brand_name_that_is_itself_a_taxonomy_term_yields_nothing():
    """必须不命中:品牌名**本身**是分类名录用语时,回落链也得空手。

    生产实证:有 4 个品牌的 ``name`` 逐字就是 `制造业`(id 161/408/409/413,
    且 is_test=false)。不挡的话扫词表会命中 B2B 标记 `制造` → 吐出
    "深圳制造哪家好？" —— 和 R6 要挡的鬼话是同一类,只是短了两个字。
    🔴 变异锚点:删掉 trade_from_brand_name 里的 looks_like_taxonomy_term 守卫,
    本断言必须红。
    """
    assert trade_from_brand_name("制造业") == ""
    assert distill_trades("制造业", [], brand_name="制造业") == []
    assert trade_from_brand_name("卫生和社会工作") == ""
    # 反向对照:正常品牌名仍然认得出经营词(证明这条守卫不是把回落链整个关了)
    assert trade_from_brand_name("桔子酒店") == "酒店"
    assert trade_from_brand_name("深圳市恒通电梯有限公司") == "电梯"


def test_r6_unusable_trade_empties_template_pool_rather_than_emitting_nonsense():
    """必须命中:炼不出品类词 → 非品牌层模板池置空(宁可层配额补不齐)。

    🔴 变异锚点:把 `_pool` 的 `if not trades: return []` 删掉,会抛 IndexError
    或吐出鬼话,本断言必须红。
    """
    pools = build_question_templates(
        brand_name="某某厂", industry="制造业", city="深圳市",
        scope=SCOPE_REGIONAL, keywords=[],
    )
    assert pools[LAYER_LOCAL] == [], f"炼不出品类词还在出题: {pools[LAYER_LOCAL]}"
    assert pools[LAYER_SCENARIO] == []
    # 反向对照:品牌认知层不受影响(问的是品牌本身,不需要品类词)
    assert len(pools[LAYER_BRAND]) == 3
    # 旧版这里会吐 "相关服务" —— 明确锁死不许回来
    for questions in pools.values():
        for question in questions:
            assert "相关服务" not in question, f"'相关服务'占位词回来了: {question}"


def test_r6_empty_pool_does_not_shrink_existing_questions():
    """必须不命中:模板池空不许把 LLM 原题弄丢(SSOT §9.6 不静默丢词)。

    锁的是"不缩减",不是"逐字不变" —— 只缺地域的题仍会被 R4 补前缀(那是**保留**
    原题的品类信息,不是丢词),所以断言用「原文是某道题的子串」而不是「原文在列表里」。
    """
    result = enforce_question_quality(
        list(GANGRONG_LLM_ORIGINALS), dict(GANGRONG_TYPES),
        brand_name="某某厂", industry="制造业", city="深圳市",
        business_scope=SCOPE_REGIONAL, engine_count=4, keywords=[],
        brand_cities="",
    )
    assert len(result["questions"]) >= len(GANGRONG_LLM_ORIGINALS), (
        f"题数缩了: {len(result['questions'])} < {len(GANGRONG_LLM_ORIGINALS)}"
    )
    for original in GANGRONG_LLM_ORIGINALS:
        assert any(original in q for q in result["questions"]), f"题被丢了: {original}"
    # 非品牌层模板池空 → 那两层一条 replaced/added 都不该有(没东西可顶替);
    # 品牌认知层不吃品类词,它的补齐照常 —— 这一格顺带锁住"只置空该置空的那两层"。
    non_brand = [
        r for r in result["repairs"]
        if r["result"] in ("replaced", "added") and r["layer"] != LAYER_BRAND
    ]
    assert non_brand == [], f"模板池已空却仍在替换/补齐: {non_brand}"
    assert any(
        r["result"] == "added" and r["layer"] == LAYER_BRAND for r in result["repairs"]
    ), "品牌认知层被误伤(它不需要品类词)"


def test_r6_distill_trade_single_value_keeps_legacy_contract():
    """必须不命中:``distill_trade`` 只服务 server.py 竞品调研,口径必须与 R6 前一致。

    它拿到的结果会进**检索 query**(不是题面),R6 的"禁吐分类学串"是题面规则,
    不该顺手改掉一条只是共用了这个函数的检索路径。
    """
    assert distill_trade("制造业") == "制造业"
    assert distill_trade(GANGRONG_INDUSTRY) == "医疗美容服务"
    assert distill_trade("") == "相关服务"
    # 反向对照:出题面那条路径就是不一样(证明上面锁的是"两条路径分开"这件事)
    assert distill_trades("制造业", [], brand_name="某某厂") == []
    assert distill_trades(GANGRONG_INDUSTRY, [], brand_name=GANGRONG_BRAND) == ["医美"]


def test_r6_keywords_still_win_over_industry_fallback():
    """必须不命中:有可用核心词时**根本不走** industry 回落链(745 不回退)。"""
    trades = distill_trades(HEQUAN_INDUSTRY, HEQUAN_KEYWORDS, brand_name=HEQUAN_BRAND)
    assert len(trades) >= 2, f"745 的多品类词轮转(R2)被弄坏了: {trades}"
    assert "商务送礼酱香酒" in trades or "基酒批发" in trades, trades


# ===========================================================================
# R7 · B2B 后缀只对 B2B 行业启用
# ===========================================================================

def test_r7_consumer_industry_never_gets_smb_suffix():
    """必须命中:医美 / 餐饮 / 到店类的场景层不许出现"适合中小企业"。

    🔴 变异锚点:把 build_question_templates 里
    `elif audience == AUDIENCE_B2B:` 改成 `elif True:`,本断言必须红。
    """
    for industry in (GANGRONG_INDUSTRY, "餐饮业 / 特色烤鱼正餐服务",
                     "住宿业 / 旅游民宿业", "体育健身 / 瑜伽普拉提培训"):
        pools = build_question_templates(
            brand_name="某机构", industry=industry, city="深圳市",
            scope=SCOPE_REGIONAL, keywords=[],
        )
        for question in pools[LAYER_SCENARIO] + pools[LAYER_LOCAL]:
            assert "中小企业" not in question, f"{industry} 被出了 B2B 后缀: {question}"
            assert "值得合作" not in question, f"{industry} 被出了 B2B 后缀: {question}"


def test_r7_b2b_industry_keeps_smb_suffix():
    """必须不命中(反向对照):真 B2B 行业仍然拿得到"适合中小企业"。

    没有这一格,上一格可能只是因为该后缀被整个删掉了。
    """
    pools = build_question_templates(
        brand_name="深圳驰鲸科技",
        industry="科技推广和应用服务业 / TikTok海外B2B精准获客、外贸社媒全案营销",
        city="深圳市", scope=SCOPE_REGIONAL, keywords=["TikTok工厂代运营"],
    )
    joined = " ".join(pools[LAYER_SCENARIO])
    assert "中小企业" in joined, f"B2B 客户丢了 B2B 后缀: {pools[LAYER_SCENARIO]}"


def test_r7_audience_classification():
    """必须命中/不命中成对:受众判定。"""
    assert industry_audience(GANGRONG_INDUSTRY) == AUDIENCE_CONSUMER
    assert industry_audience("餐饮业 / 特色烤鱼正餐服务") == AUDIENCE_CONSUMER
    assert industry_audience("文化艺术业 / 传统民乐（尺八）乐器研发销售") == AUDIENCE_CONSUMER
    assert industry_audience("科技推广和应用服务业 / TikTok海外B2B精准获客") == AUDIENCE_B2B
    assert industry_audience("专用设备制造业 / 生命科学仪器、制药装备") == AUDIENCE_B2B
    assert industry_audience("软件和信息技术服务业 / 电商SaaS工具") == AUDIENCE_B2B


def test_r7_unknown_industry_defaults_to_consumer_suffix():
    """必须命中:判不出受众 → 默认 C 端/通用后缀(代价不对称,取代价小的一侧)。

    B2B 后缀用在 C 端客户身上是**直接出鬼话**(529);通用后缀用在 B2B 客户身上
    至多是不够精准。所以默认关 B2B,而不是默认开。
    """
    assert industry_audience("") == AUDIENCE_CONSUMER
    assert industry_audience("其他") == AUDIENCE_CONSUMER
    pools = build_question_templates(
        brand_name="某公司", industry="其他", city="成都市",
        scope=SCOPE_REGIONAL, keywords=["高端商业公馆"],
    )
    for question in pools[LAYER_SCENARIO]:
        assert "中小企业" not in question, question


def test_r7_consumer_local_pool_avoids_company_noun():
    """必须命中:C 端的决策获客层末条不说"公司"("深圳哪些医美公司口碑好"不像人话)。"""
    pools = build_question_templates(
        brand_name=GANGRONG_BRAND, industry=GANGRONG_INDUSTRY, city="深圳市",
        scope=SCOPE_REGIONAL, keywords=["深圳龙岗医美"],
    )
    assert not any("公司口碑好" in q for q in pools[LAYER_LOCAL]), pools[LAYER_LOCAL]
    # 反向对照:B2B 仍然说"公司"
    b2b_pools = build_question_templates(
        brand_name="深圳驰鲸科技", industry="科技推广和应用服务业 / TikTok外贸B2B获客",
        city="深圳市", scope=SCOPE_REGIONAL, keywords=["TikTok工厂代运营"],
    )
    assert any("公司口碑好" in q for q in b2b_pools[LAYER_LOCAL]), b2b_pools[LAYER_LOCAL]


def test_r7_fallback_national_consumer_avoids_b2b_nouns():
    """必须命中:降级出题的**全国**分支,C 端客户不许拿到"公司排名TOP5"这类 B2B 名词。

    这一格是补上来的:原来只锁了 regional 分支,导致"national 分支不分受众"这条变异
    仍绿 —— **锁跑不到的分支等于没锁**。
    🔴 变异锚点:把 national 分支的 `f"{trade}公司排名TOP5" if audience == AUDIENCE_B2B`
    改成无条件,本断言必须红。
    """
    import tools.keyword_generator as kg

    ctx = kg._fallback_business_context(
        "某医美机构", GANGRONG_INDUSTRY, ["深圳龙岗医美"],
        client_location="深圳市", business_scope=SCOPE_NATIONAL,
    )
    joined = " ".join(ctx["real_user_questions"])
    assert "公司排名TOP5" not in joined, joined
    assert "中国最靠谱的" not in joined, joined
    assert "排名前十有哪些？" in joined, joined
    # 反向对照:真 B2B 客户的全国分支仍然说"公司排名TOP5"
    b2b = kg._fallback_business_context(
        "深圳驰鲸科技", "科技推广和应用服务业 / TikTok海外B2B精准获客",
        ["TikTok工厂代运营"], client_location="深圳市", business_scope=SCOPE_NATIONAL,
    )
    b2b_joined = " ".join(b2b["real_user_questions"])
    assert "公司排名TOP5" in b2b_joined, b2b_joined


def test_r7_529_added_question_is_not_b2b():
    """必须命中(病灶原样复现):529 那条 added 题不再是"适合中小企业"。"""
    result = _run_529()
    added = [r["replacement"] for r in result["repairs"] if r["result"] == "added"]
    for question in added:
        assert "中小企业" not in question, f"补进来的题仍是 B2B 后缀: {question}"
    assert result["audience"] == AUDIENCE_CONSUMER


# ===========================================================================
# 745(R1-R4 病灶客户)不回退
# ===========================================================================

def test_r1_r4_hequan_still_fixed():
    """必须不命中:745 的 6 条 LLM 好题仍然一条不换、也不出 14 字地址前缀。"""
    types = {q: LAYER_LOCAL for q in HEQUAN_LLM_ORIGINALS}
    result = enforce_question_quality(
        list(HEQUAN_LLM_ORIGINALS), types,
        brand_name=HEQUAN_BRAND, industry=HEQUAN_INDUSTRY, city=HEQUAN_CITY,
        business_scope=SCOPE_NATIONAL, engine_count=4, keywords=HEQUAN_KEYWORDS,
        max_tested_questions=8, brand_cities=HEQUAN_CITY,
    )
    for original in HEQUAN_LLM_ORIGINALS:
        assert original in result["questions"], f"745 的好题又被换掉了: {original}"
    for question in result["questions"]:
        assert HEQUAN_CITY not in question, f"14 字地址前缀回来了: {question}"
    assert result["city"] == "仁怀"


def test_r1_town_level_token_still_not_stripped():
    """必须不命中:镇级 token 仍保留整词(Review §7.2 茅台镇不许剥成茅台)。"""
    assert "茅台镇" in merged_geo_tokens(HEQUAN_CITY, HEQUAN_CITY)
    assert "茅台" not in merged_geo_tokens(HEQUAN_CITY, HEQUAN_CITY)
    assert has_geo_qualifier("酱香酒和茅台的区别是什么？", HEQUAN_CITY, HEQUAN_CITY) is False
    assert has_geo_qualifier("遵义茅台镇纯粮酱酒哪家好？", HEQUAN_CITY, HEQUAN_CITY) is True


# ===========================================================================
# 接线锁 —— "改了纯函数但调用方没接上" 是 R1-R4 踩过的坑
# ===========================================================================

def _workflow_ast():
    """解析 workflows/diagnosis_workflow.py —— **不 import**。

    该模块 import 链上 ``db/diagnosis_db.py`` 是模块级 ``init_db()``(import 就连库),
    在没有库的环境里 import 会直接炸;而这条锁要判的是"接线在不在",与库无关。
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "workflows" / "diagnosis_workflow.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_diagnosis_workflow":
            return tree, node, text
    raise AssertionError("workflows/diagnosis_workflow.py 里找不到 run_diagnosis_workflow")


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _generator_inputs_ast():
    """解析 services/diagnosis_generator_inputs.py 的 ``brand_side_generator_inputs``。

    🔴 [#147-B · 2026-09-07] 品牌侧输入抽成了预览端与实跑**共享的一份**,
       `resolve_brand_cities` 与喂给生成器的那几项都搬进了这里。
       下面两条锁钉的性质**一条没变**,只是跨了两个文件 ——
       所以判据跟着**沿链走完**,不是删掉,也不是只查新文件。
    """
    path = (pathlib.Path(__file__).resolve().parents[1]
            / "services" / "diagnosis_generator_inputs.py")
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "brand_side_generator_inputs"):
            return tree, node, text
    raise AssertionError("services/diagnosis_generator_inputs.py 里找不到 "
                         "brand_side_generator_inputs")


def test_wiring_workflow_resolves_brand_cities_from_archive():
    """必须命中:``brand_cities`` 由 ``brand_id`` 经 ``resolve_brand_cities`` 取出。

    结构判定(AST),不是整文件文本窗口 —— 文本窗口挡不住 ``if False and …`` 这类
    "词还在但不执行"的改法。

    #147-B 之后这条链是两段,**两段都要查**:
        run_diagnosis_workflow(brand_id=brand_id) → brand_side_generator_inputs
                                                  → resolve_brand_cities(brand_id)
    只查第二段就成空壳:helper 里写着那行、但主链根本不调它,照样绿。

    🔴 变异锚点:主链不再调 helper / helper 里删掉那次取值 / 实参从 brand_id
       改成 None —— 任一,本断言必须红。
    """
    _tree, fn, _text = _workflow_ast()
    # 第一段:主链把 brand_id 交给共享那份
    handoff = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and _call_name(node) == "brand_side_generator_inputs"
        and any(kw.arg == "brand_id" and isinstance(kw.value, ast.Name)
                and kw.value.id == "brand_id" for kw in node.keywords)
    ]
    assert handoff, "主链没把 brand_id 交给共享的品牌侧输入 —— 后面那段无从谈起"

    # 第二段:共享那份真的用 brand_id 去取 brands.cities
    _t2, helper, _x = _generator_inputs_ast()
    found = [
        node for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and _call_name(node) == "resolve_brand_cities"
        and [a for a in node.args if isinstance(a, ast.Name) and a.id == "brand_id"]
    ]
    assert found, "共享的品牌侧输入没有用 brand_id 去取 brands.cities"


def test_wiring_workflow_passes_brand_cities_to_business_analysis():
    """必须命中:``brand_cities`` 真的传到了 ``analyze_client_business``。

    🔴 变异锚点:helper 不再产出这一项 / 主链不再展开共享那份 —— 本断言必须红
    (取了不传 = 白修 —— R1-R4 踩过一次:enforce 里传的是归一后的 town,
     token 改造整个被抹掉)。

    #147-B 之后它不再是显式 keyword,而是从共享那份 ``**`` 展开 ——
    所以两端都查:helper 产出这个键 + 主链把那份展开给生成器。
    """
    _tree, fn, _text = _workflow_ast()
    calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _call_name(node) == "analyze_client_business"
    ]
    assert calls, "workflow 里找不到 analyze_client_business 调用"

    # 产出端:共享那份的 kwargs 字典里有 brand_cities
    _t2, helper, _x = _generator_inputs_ast()
    produced = any(
        isinstance(k, ast.Constant) and k.value == "brand_cities"
        for d in ast.walk(helper) if isinstance(d, ast.Dict) for k in d.keys
        if isinstance(k, ast.Constant)
    )
    assert produced, "共享的品牌侧输入没产出 brand_cities"

    # 消费端:主链把那份原样展开(而不是手写实参 —— 手写就会再次两侧分家)
    unpacked = any(kw.arg is None and isinstance(kw.value, ast.Name)
                   and "brand_side" in kw.value.id
                   for call in calls for kw in call.keywords)
    explicit = any(
        kw.arg == "brand_cities" and isinstance(kw.value, ast.Name)
        and kw.value.id == "brand_cities"
        for call in calls for kw in call.keywords)
    assert unpacked or explicit, "取了 brands.cities 却没往业务分析传"


def test_wiring_workflow_ast_scoping_is_discriminating():
    """反向对照:证明上面两格**真的切到了那个函数体**,不是在整文件里瞎搜。

    ``resolve_effective_business_scope`` 的 import 语句在**模块顶层**(函数体外)。
    若切函数体没切对(退化成整文件),下面这条就会命中 → 上面两格失去判别力。
    """
    tree, fn, _text = _workflow_ast()
    imports_in_fn = [
        alias.name for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom) for alias in node.names
    ]
    assert "resolve_brand_cities" not in imports_in_fn, (
        "函数体里出现了模块级 import → AST 切块没切对,上面两格是假绿"
    )
    module_imports = [
        alias.name for node in tree.body
        if isinstance(node, ast.ImportFrom) for alias in node.names
    ]
    assert "resolve_brand_cities" in module_imports, "模块级没 import resolve_brand_cities"


def test_wiring_keyword_generator_threads_brand_cities_to_the_gate():
    """必须命中:keyword_generator 的三段接线都在(少任何一段 = 白修)。"""
    import tools.keyword_generator as kg

    assert "brand_cities" in inspect.signature(kg.analyze_client_business).parameters
    assert "brand_cities" in inspect.signature(kg._enforce_question_quality).parameters
    assert "brand_cities" in inspect.signature(kg._enforce_commercial_questions).parameters
    assert "brand_cities" in inspect.signature(kg._fallback_business_context).parameters
    # 真的传下去了,而不是只加了个参数
    assert "brand_cities=brand_cities" in inspect.getsource(kg._enforce_question_quality)
    assert "brand_cities=brand_cities" in inspect.getsource(kg._enforce_commercial_questions)


def test_wiring_gate_signature_accepts_brand_cities():
    """必须命中:闸本身收得下 brand_cities,且默认值保住旧行为。"""
    params = inspect.signature(enforce_question_quality).parameters
    assert "brand_cities" in params
    assert params["brand_cities"].default == "", "默认值必须是空串(不传 = 旧行为)"
    params2 = inspect.signature(build_question_templates).parameters
    assert "brand_cities" in params2 and params2["brand_cities"].default == ""


def test_wiring_llm_prompt_no_longer_feeds_raw_industry_as_example():
    """必须命中:出题 prompt 的示例不许再拿 industry 登记整串当范例。

    529 的两道分类学串题**不是矫正闸放进去的** —— 是 LLM 照抄 prompt 示例
    `f'"{_city}{industry}哪家好"'` 抄出来的,而且它们带地域又带"哪家靠谱",
    闸判它们完全合格。这条不修,下游怎么补都没用。
    """
    import tools.keyword_generator as kg

    source = inspect.getsource(kg.analyze_client_business)
    # 定位 user_prompt 里的示例行(不是整文件搜 —— 整文件搜"{industry}"会命中
    # system_prompt 的"行业：{industry}"那行,那行是**应该**给原文的)
    assert '{_city or \'某市\'}{_trade}哪家好' in source, "prompt 示例没改成炼过的品类词"
    assert '{_city or \'某市\'}{industry}哪家好' not in source, "旧的整串示例还在"
    assert "禁止把它原样抄进问题里" in source, "没有显式告诉 LLM 不许抄登记名录"


def test_wiring_fallback_context_uses_distilled_trade():
    """必须命中:降级出题(也是商业兜底池)不许再拼 industry 整串。"""
    import tools.keyword_generator as kg

    source = inspect.getsource(kg._fallback_business_context)
    assert "{detected_city}{trade}哪家靠谱？" in source
    assert "{detected_city}{industry}哪家靠谱？" not in source, "旧的整串拼接还在"


def test_fallback_context_end_to_end_has_no_taxonomy_string():
    """必须命中(行为级,不是文本级):降级出题的 8 道题里没有分类学整串。"""
    import tools.keyword_generator as kg

    ctx = kg._fallback_business_context(
        GANGRONG_BRAND, GANGRONG_INDUSTRY, GANGRONG_KEYWORDS_529,
        client_location=GANGRONG_FORM_529, business_scope=SCOPE_REGIONAL,
        brand_cities=GANGRONG_ARCHIVE_CITIES,
    )
    for question in ctx["real_user_questions"]:
        assert GANGRONG_INDUSTRY not in question, question
        assert "卫生和社会工作" not in question, question
        assert "中小企业" not in question, question
    assert ctx["identified_regions"] == ["深圳"], ctx["identified_regions"]
    # 反向对照:不传档案 → 城市退回省级"广东"(证明上面那格锁的是 R5 的合并)
    ctx2 = kg._fallback_business_context(
        GANGRONG_BRAND, GANGRONG_INDUSTRY, GANGRONG_KEYWORDS_529,
        client_location=GANGRONG_FORM_529, business_scope=SCOPE_REGIONAL,
    )
    assert ctx2["identified_regions"] == ["广东"], ctx2["identified_regions"]


def test_wiring_brand_cities_reader_is_fail_soft(monkeypatch):
    """必须不命中:读档案炸了不许阻断诊断(退回空串 = 只吃表单值的旧行为)。"""
    import db.connection as dbconn
    from services.diagnosis_business_scope import resolve_brand_cities

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbconn, "get_connection", _boom)
    assert resolve_brand_cities(737) == ""
    # 没有 brand_id 时压根不该查库
    assert resolve_brand_cities(None) == ""
    assert resolve_brand_cities(0) == ""
