"""WO_DIAGNOSIS_MENTION_VARIANT_AND_QUESTION_FITNESS 2026-08-06 · 三节判据锁。

判据全部来自工单的「必须命中 / 必须不命中」两栏 + 反向对照要求。
**每条正向断言都配了反向对照** —— 只跑阳性 = 恒真作废(工单 §1.3 原话)。

生产取证基线(2026-08-06 只读实查,全部写进用例做样本):
  · 诊断 561 · brand 799「阿强小龙虾」· brand_aliases 0 行 / company_name 空 /
    brand_display_names 空 → 三源只有一个形态;
    32 格里 4 YES(原文含全称)+ 16 UNKNOWN(invalid_matched_text)+ 12 NO,
    **23 格原文写的是「阿强龙虾」**;
  · 诊断 553 · 表单 client_location = "贵州贵阳" → question_quality.city 同值,
    repairs 里 2 条 prefixed 造出「贵州贵阳贵州劳务派遣…」;
  · 561 的 repairs 5 条 prefixed 造出「贵州贵阳贵阳小龙虾…」。
"""
from __future__ import annotations

import pytest

from services.brand_identity_resolver import (
    BrandIdentity,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
)
from services.brand_name_near_miss import (
    NEAR_MISS_MIN_LENGTH,
    find_near_miss_candidates,
    near_miss_candidates_for_identity,
)

# ── 生产真实样本 ────────────────────────────────────────────────────────────
AQIANG = "阿强小龙虾"
AQIANG_VARIANT = "阿强龙虾"  # 561 里 23/32 格原文的写法
#: 561 某 NO 格落库的 mentioned_brands(逐字)
AQIANG_MENTIONED = ["阿强龙虾", "大嘴龙虾", "阿杜炒蟹", "丝丝牵挂龙虾馆", "陈莽莽烤肉龙虾馆"]

#: 08-05 城市别名假阳性 P0 的阴性样本集(近失层对它们必须 0 命中)
FALSE_POSITIVE_NEGATIVE_SAMPLES = [
    # ① 括号城市被拆成别名 → "深圳" 绝不能当品牌变体候选
    ("全域上榜（深圳）科技有限公司", ["深圳", "深圳市", "广东", "科技有限公司"]),
    # ② 551 那格:富士恒 ≠ 晨光富士(编辑距离 3,方向也不同)
    ("深圳市晨光富士电梯有限公司", ["深圳市富士恒电梯有限公司", "富士电梯", "深圳"]),
    # ③ 飞轮包实证的编辑距离误伤:2 字词之间差一个字太常见
    ("大型设备租赁", ["大鹏", "大型"]),
]


# ══════════════════════════════════════════════════════════════════════════
# §1 假阴性 · 近失检出层
# ══════════════════════════════════════════════════════════════════════════

def test_aqiang_variant_is_detected_as_near_miss():
    """【必须命中】「阿强龙虾」被标为疑似同品牌变体。"""
    found = find_near_miss_candidates([AQIANG], AQIANG_MENTIONED)
    assert [item.display for item in found] == [AQIANG_VARIANT], (
        "561 的 NO 格 mentioned_brands 里就有「阿强龙虾」,近失层必须挑出它"
    )
    assert found[0].trusted_form == AQIANG
    assert found[0].distance == 1


def test_near_miss_does_not_pick_other_shops_in_the_same_answer():
    """【反向对照】同一份名单里的**别家店**一个都不许进候选。

    没有这条,上一条用例可以被"把 mentioned_brands 整个返回"骗过去 —— 那样
    代理会看到 5 个候选,等于把判断成本原样丢回给人,闭环白做。
    """
    found = find_near_miss_candidates([AQIANG], AQIANG_MENTIONED)
    displays = {item.display for item in found}
    for other in ("大嘴龙虾", "阿杜炒蟹", "丝丝牵挂龙虾馆", "陈莽莽烤肉龙虾馆"):
        assert other not in displays, f"{other} 是别家店,不该进本品牌的待确认候选"


@pytest.mark.parametrize("trusted,candidates", FALSE_POSITIVE_NEGATIVE_SAMPLES)
def test_near_miss_is_silent_on_the_2026_08_05_false_positive_set(trusted, candidates):
    """【必须不命中 · 反向对照】08-05 假阳性案例集当阴性样本跑,必须 0 命中。

    工单 §1.3 原话:「只跑阳性 = 恒真作废」。这一组就是那个阴性侧。
    """
    found = find_near_miss_candidates([trusted], candidates)
    assert found == (), f"{trusted} 不该把 {[c for c in candidates]} 判成变体,实得 {found}"


def test_pure_place_name_is_never_a_brand_variant_candidate():
    """【必须不命中】纯地名(哪怕长度够、距离够)也不许当品牌变体候选。

    🔴 这条与上面那组的分工:上面那组的「深圳」是被**长度闸**挡掉的,
    地名闸删掉照样全绿(变异 runner 实测抓出)。这里用一个**长度够 4、
    与可信名只差一字**的真城市名,把地名闸单独钉住 ——
    这就是 08-05 城市别名 P0 的一般形态。
    """
    assert find_near_miss_candidates(["石家庄市场"], ["石家庄市"]) == ()
    assert find_near_miss_candidates(["乌鲁木齐"], ["乌鲁木齐市"]) == ()
    # 🔴 上面两条其实**通用词闸也能挡**(两道闸覆盖面高度重合,变异 runner 当场
    #    证明了这一点:删掉地名闸它们照样绿)。地名闸真正独有的那一格是
    #    **带标点/空格的城市名** —— 归一后等于城市名,但去行政前缀那一步按原串
    #    走,剥不干净 → 通用词闸放行。LLM 抽出来的名字带空格/间隔号是常态
    #    (resolver 自己有 _NORMALIZE_RE 就是为这个)。把独有格钉住,
    #    这条闸才不是摆设。
    assert find_near_miss_candidates(["石家庄市场"], ["石家庄 市"]) == (), (
        "带空格的纯城市名同样不许当品牌变体候选(地名闸的独有覆盖面)"
    )


def test_short_names_never_near_miss():
    """【必须不命中】长度闸 —— 2/3 字词之间差一个字不构成变体信号。

    🔴 两侧都要钉。第一版只钉了可信侧(``["大型"] vs ["大鹏"]``),变异把
    **候选侧**的长度闸删掉照样全绿 —— 被自己的变异 runner 当场抓出来。
    「一条判据挡住了,不代表另一条判据有用」。
    """
    # 可信侧短 → 出局
    assert find_near_miss_candidates(["大型"], ["大鹏"]) == ()
    assert find_near_miss_candidates(["盛邦"], ["盛帮"]) == ()
    # 候选侧短 → 同样出局(可信名够长,只有候选是 3 字截短)
    assert find_near_miss_candidates(["晨光富士"], ["观光富"]) == ()
    assert NEAR_MISS_MIN_LENGTH == 4, "下限改了就要重新论证「大型→大鹏」还挡不挡得住"


def test_storefront_suffix_form_has_a_near_miss_exit():
    """【必须命中】≤5 字品牌 + 后缀形态(「阿强小龙虾万象店」)有出口。

    工单 §1.1 的结构性一刀:5 字品牌名连"真包含"路径都是死的(短方 5 < 6),
    确定性层也接不住(右边界是「万」,不在安全后缀表里)。出口只能开在待确认侧。
    """
    found = find_near_miss_candidates([AQIANG], ["阿强小龙虾万象店"])
    assert [item.display for item in found] == ["阿强小龙虾万象店"]


def test_extension_rule_has_a_bound():
    """【反向对照】受限扩展不是"包含就算" —— 超出上限必须不命中。"""
    assert find_near_miss_candidates([AQIANG], ["阿强小龙虾加盟连锁管理总部"]) == ()


def test_near_miss_never_uses_split_bracket_aliases_as_the_baseline():
    """【必须不命中】近失基准只吃原串,不走 ``all_trusted_names``。

    ``all_trusted_names`` 过 ``split_brand_aliases``,会把解析器派生出来的形态
    (去括号名 / 括号别名)一并吐出来。近失是**给人看的待确认候选**,基准放宽
    一格,候选噪声就放大一圈。

    🔴 [返工 2026-08-06 · 跟随括号品牌 P0 修复] 本用例的两处**前提断言**已翻转:
    原来钉的是「(深圳)」「(龙岗区平湖)」**会**被拆成独立可信别名 —— 那是
    08-05 P0 的病灶行为。P0 修复(`_parenthetical_identity_alias`)把行政区划
    内容挡在别名之外后,前提反过来了:它们**不再**出现在 ``all_trusted_names``。
    前提断言跟着钉新行为,**保护性断言(近失候选为空)一个字不动**。
    """
    identity = BrandIdentity(
        brand_id=1,
        canonical_names=("全域上榜（深圳）科技有限公司",),
    )
    assert "深圳" not in identity.all_trusted_names, (
        "前提(已随 P0 修复翻转):括号城市不再进可信别名"
    )
    assert near_miss_candidates_for_identity(identity, ["深圳湾", "深圳市"]) == ()

    long_bracket = BrandIdentity(
        brand_id=1,
        canonical_names=("全域上榜（龙岗区平湖）科技有限公司",),
    )
    assert "龙岗区平湖" not in long_bracket.all_trusted_names, (
        "前提(已随 P0 修复翻转):区/镇复合地名同样不再进可信别名 —— "
        "这一格就是本次返工的验收探针"
    )
    assert near_miss_candidates_for_identity(long_bracket, ["龙岗区平湖社区"]) == (), (
        "拿括号拆出来的地名当近失基准 = 重开 08-05 假阳性"
    )

    # 🔴 P0 修复把地名挡在源头之后,上面两组**不再能区分**两种基准
    #    (两边都返回空)—— 若只留它们,「基准改回 all_trusted_names」这条变异
    #    会从此恒绿 = 变异 runner 发一张假的及格证。
    #    所以补一个 P0 修复**之后仍然**只有基准取法能决定结果的样本:
    #    真别名「(KONE)」是合法拆分,去括号名「通力电梯」只存在于
    #    ``all_trusted_names``,而「通力电梯厂」与它只差一个字。
    kone = BrandIdentity(brand_id=1, canonical_names=("通力电梯（KONE）",))
    assert "通力电梯" in kone.all_trusted_names, "前提:真别名的拆分行为没被改坏"
    assert near_miss_candidates_for_identity(kone, ["通力电梯厂"]) == (), (
        "近失基准取了解析器派生的去括号名 = 基准放宽了一格"
    )


def test_rejected_names_do_not_come_back_to_the_queue():
    """【必须不命中】人工判过「不是」的名字不再回到待确认。"""
    identity = BrandIdentity(
        brand_id=1, canonical_names=(AQIANG,), rejected_aliases=(AQIANG_VARIANT,)
    )
    assert near_miss_candidates_for_identity(identity, [AQIANG_VARIANT]) == ()


# ── §1 · 判定契约本体不许放宽 ──────────────────────────────────────────────

def test_verdict_contract_is_not_loosened_by_the_near_miss_layer():
    """🔴【必须不命中 · 工单铁律】未确认前**不许**直接算提到。

    这是本单最容易改错的一格:让 verdict 跟着近失走 = 重开 08-05 假阳性。
    """
    identity = BrandIdentity(brand_id=1, canonical_names=(AQIANG,))
    answer = "贵阳夜宵推荐：阿强龙虾（观山湖店）、大嘴龙虾。"
    decision = BrandIdentityResolver(identity).resolve_local(answer)
    assert decision.verdict is not BrandVerdict.YES, "近失绝不能让确定性层判命中"


def test_six_char_containment_floor_is_unchanged():
    """【必须不命中】``_exact_or_strict_match`` 的 6 字门槛与全等契约逐字不动。"""
    from tools.ai_visibility.ai_tester import _exact_or_strict_match

    # 全等仍然命中
    assert _exact_or_strict_match(AQIANG, [AQIANG])[0] is True
    # 差一字仍然**不**命中(近失不是命中)
    assert _exact_or_strict_match(AQIANG, [AQIANG_VARIANT])[0] is False
    # 短方 < 6 的包含仍然**不**命中(防盛邦/广东法制类误中)
    assert _exact_or_strict_match("盛邦国际物流有限公司", ["盛邦"])[0] is False
    # 短方 >= 6 的包含仍然命中(旧行为)
    assert _exact_or_strict_match("深圳市晨光富士电梯有限公司", ["晨光富士电梯"])[0] is True


@pytest.mark.asyncio
async def test_invalid_matched_text_carries_the_candidate_out():
    """【必须命中】复核层被打回的候选不再就地蒸发。

    561 的 16 格走的就是这条路:复核层交回「阿强龙虾」→ 被
    ``_is_plausible_verified_match`` 正确拒绝 → 旧代码连同候选一起丢掉,
    待确认卡片零候选可点。
    """
    identity = BrandIdentity(brand_id=1, canonical_names=(AQIANG,))
    answer = "贵阳夜宵推荐：阿强龙虾（观山湖店）人气很高，还有大嘴龙虾。"

    async def _verifier(*, identity, evidence_windows):
        return VerificationResult(BrandVerdict.YES, "matched", matched_text=AQIANG_VARIANT)

    decision = await BrandIdentityResolver(identity, verifier=_verifier).resolve(answer)
    assert decision.verdict is BrandVerdict.UNKNOWN, "判定仍是 UNKNOWN(契约不放宽)"
    assert decision.reason == "invalid_matched_text"
    assert decision.near_miss_alias == AQIANG_VARIANT, "候选必须被带出来"
    assert decision.matched_alias is None, (
        "🔴 near_miss_alias 不许写进 matched_alias —— 后者是命中语义,"
        "下游按位置自动升格'明确推荐'(08-05 假阳性的放大链路)"
    )


@pytest.mark.asyncio
async def test_hallucinated_matched_text_is_not_carried_out():
    """【反向对照】复核层编的、原文里没有的名字**不算**近失候选。"""
    identity = BrandIdentity(brand_id=1, canonical_names=(AQIANG,))
    answer = "贵阳夜宵推荐：阿强龙虾人气很高。"

    async def _verifier(*, identity, evidence_windows):
        return VerificationResult(BrandVerdict.YES, "matched", matched_text="根本不存在的店名")

    decision = await BrandIdentityResolver(identity, verifier=_verifier).resolve(answer)
    assert decision.verdict is BrandVerdict.UNKNOWN
    assert decision.near_miss_alias is None


# ── §1 · 待确认卡片闭环 ────────────────────────────────────────────────────

def _no_cell(mentioned):
    """一格已判 NO、原文含变体的落库单元格(561 的 7 个 NO 格形态)。"""
    return {
        "brand_verdict": "NO",
        "brand_detected": False,
        "detection_reason": "证据中为'阿强龙虾'，与'阿强小龙虾'专有前缀不同，非同一品牌。",
        "detection_method": "deepseek_v4_flash_structured",
        "full_response": "贵阳小龙虾夜宵推荐：阿强龙虾、大嘴龙虾。",
        "mentioned_brands": list(mentioned),
    }


def test_legacy_no_cell_gets_a_clickable_candidate():
    """【必须命中】存量 NO 格现算候选 —— 不重跑引擎、不重扣费。"""
    from services.diagnosis_identity_review import cell_candidates, cell_near_miss

    identity = BrandIdentity(brand_id=1, canonical_names=(AQIANG,))
    cell = _no_cell(AQIANG_MENTIONED)
    candidates, _snippet = cell_candidates(cell, identity=identity)
    assert AQIANG_VARIANT in candidates, "确认按钮要有值可点"
    assert [item["display"] for item in cell_near_miss(cell, identity=identity)] == [
        AQIANG_VARIANT
    ]


def test_no_cell_stays_no_until_a_human_confirms():
    """🔴【必须不命中】露出候选**不等于**改判。分母/verdict 一个都不许动。"""
    from services.diagnosis_identity_review import classify_cell_state

    identity = BrandIdentity(brand_id=1, canonical_names=(AQIANG,))
    cell = _no_cell(AQIANG_MENTIONED)
    assert classify_cell_state(cell, identity=identity) == "NO"
    assert cell["brand_detected"] is False


def test_confirmed_alias_flips_the_cell_to_a_mention():
    """【必须命中】确认后重判该题 = 提到(纯本地,零 provider 调用)。

    模拟 ``decide_brand_cell`` 第 10 步构造的 ``post_identity``。
    """
    cell = _no_cell(AQIANG_MENTIONED)
    post_identity = BrandIdentity(
        brand_id=1, canonical_names=(AQIANG,), trusted_aliases=(AQIANG_VARIANT,)
    )
    decision = BrandIdentityResolver(post_identity).resolve_local(cell["full_response"])
    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias == AQIANG_VARIANT


# ══════════════════════════════════════════════════════════════════════════
# §2 提名型闸
# ══════════════════════════════════════════════════════════════════════════

WO_CASE_Q = "贵州贵阳贵州劳务派遣服务商怎么选才不踩坑？"

#: 07-23 SSOT 的四个已裁例(§2.3 反向对照要求逐条跑)
SSOT_ADJUDICATED = [
    ("深圳装修公司哪个好", True),      # 商业
    ("哪个更适合中小企业", True),      # 商业
    ("深圳装修公司靠谱吗", True),      # 商业(供应商尽调)
    ("预算怎么做", False),             # 知识
]


def test_wo_case_is_rejected_by_the_nomination_gate():
    """【必须命中】「服务商怎么选才不踩坑」在诊断出题层被判废。"""
    from services.nomination_question_policy import is_nomination_question

    ok, reason = is_nomination_question(WO_CASE_Q)
    assert ok is False
    assert reason == "NO_NOMINATION_SIGNAL"


def test_wo_case_verdict_on_the_quote_side_is_byte_identical():
    """🔴【必须不命中】同一问题在报价侧 evaluate() 结论**逐字不变**。

    工单 §2.1 明令:闸没坏,不要动报价侧的判定。这条锁的是"没动"。
    """
    from services.commercial_query_policy import evaluate

    decision = evaluate(WO_CASE_Q)
    assert decision.commercial_delivery_eligible is True
    assert decision.intent_type == "commercial"
    assert decision.reason_codes == ("GATE:PROVIDER_OBJECT",)


@pytest.mark.parametrize("text,commercial_expected", SSOT_ADJUDICATED)
def test_nomination_is_a_subset_of_commercial(text, commercial_expected):
    """🔴【反向对照 · 整节作废条件】提名型 ⊂ 商业。

    工单 §2.3 原话:「若有任何一例被提名型闸放行但 SSOT 判知识,说明闸叠错层了,
    整节作废」。这条把那个作废条件变成一个会自己转红的断言。
    """
    from services.commercial_query_policy import evaluate
    from services.nomination_question_policy import is_nomination_question

    commercial = evaluate(text).commercial_delivery_eligible
    assert commercial is commercial_expected, "前提:SSOT 语义没被本单改动"
    nomination, _ = is_nomination_question(text)
    assert not (nomination and not commercial), (
        f"{text!r} 被提名闸放行却不是商业题 —— 闸叠错层"
    )


def test_nomination_gate_delegates_before_deciding():
    """【必须不命中】⊂ 关系是**结构**保证:商业闸不过就直接出局。"""
    from services.nomination_question_policy import is_nomination_question

    # "预算怎么做" 是知识题,即使硬塞一个提名信号词也不能靠提名闸翻身
    ok, reason = is_nomination_question("预算怎么做")
    assert (ok, reason) == (False, "NOT_COMMERCIAL")


def test_method_only_questions_have_no_nomination_signal():
    """【必须命中】方法论问法一律无提名信号(答案不点名厂商)。"""
    from services.nomination_question_policy import has_nomination_signal

    for text in (
        "劳务派遣服务商怎么选",
        "劳务派遣一般怎么收费",
        "劳务派遣口碑怎么判断",
        "劳务外包多少钱",
        "人力资源外包怎么选？有哪些注意点",
    ):
        assert has_nomination_signal(text) is False, f"{text} 不该被判提名型"


def test_nomination_signals_fire_on_real_production_questions():
    """【反向对照】真实生产题面必须**能**被判提名型 —— 否则闸就是恒假。"""
    from services.nomination_question_policy import has_nomination_signal

    for text in (
        "贵阳小龙虾夜宵店哪家好吃？",            # 561 实题
        "贵阳本地人推荐的小龙虾店有哪些？",      # 561 实题
        "贵阳晚上吃小龙虾去哪里比较热闹？",      # 561 实题
        "全国连锁的人力资源外包公司有哪些？",    # 553 实题
        "国内做劳务外包排名前十的公司",          # 553 实题
        "劳务派遣一般怎么收费？哪家性价比高",    # 混合问法:后半句必然点名
    ):
        assert has_nomination_signal(text) is True, f"{text} 应判提名型"


def test_every_template_pool_entry_passes_the_nomination_gate():
    """🔴【必须命中】模板池自己必须全是提名型。

    「出口修了、兜底池还在产」是 brandq 包刚踩过的坑(等槽替换的供给方
    自己产不合格题 = 白修)。这条把它变成机械判据。
    """
    from services.diagnosis_question_quality import build_question_templates
    from services.nomination_question_policy import is_nomination_question

    bad = []
    for scope in ("regional", "national"):
        for industry, keywords in (
            ("小龙虾夜宵餐饮", ["小龙虾夜宵", "龙虾馆"]),
            ("人力资源服务", ["劳务派遣", "人力资源外包"]),
        ):
            pools = build_question_templates(
                brand_name=AQIANG, industry=industry, city="贵州贵阳",
                scope=scope, keywords=keywords,
            )
            for layer, pool in pools.items():
                for question in pool:
                    ok, reason = is_nomination_question(question, brand_name=AQIANG)
                    if not ok:
                        bad.append(f"{scope}/{layer}: {question} ({reason})")
    assert bad == [], "模板池里还有测不出品牌的题:\n" + "\n".join(bad)


def test_fallback_business_context_pool_passes_the_nomination_gate():
    """🔴【必须命中】等槽替换池(降级出口的产物)同样必须全提名型。"""
    from services.nomination_question_policy import is_nomination_question
    from tools.keyword_generator import _fallback_business_context

    bad = []
    for scope in ("regional", "national"):
        pool = _fallback_business_context(
            AQIANG, "小龙虾夜宵餐饮", ["小龙虾夜宵"],
            client_location="贵州贵阳", business_scope=scope,
        )
        for question in pool["real_user_questions"]:
            ok, reason = is_nomination_question(question, brand_name=AQIANG)
            if not ok:
                bad.append(f"{scope}: {question} ({reason})")
    assert bad == [], "兜底池里还有测不出品牌的题:\n" + "\n".join(bad)


def test_research_keyword_fallback_pool_is_nomination_typed():
    """【必须命中】品牌档案的调研词兜底池也换成提名型。

    这些词是下游出题 prompt 的「关键词」输入 —— 它产「怎么选/多少钱/避坑」,
    LLM 就照着出测不出提及的题(553 的形态)。
    """
    from api.brand_api import _sanitize_business_probe_keywords
    from services.nomination_question_policy import has_nomination_signal

    keywords = _sanitize_business_probe_keywords([], "邦芒", "劳务派遣", "贵阳")
    assert keywords, "兜底池不能是空的"
    bad = [kw for kw in keywords if not has_nomination_signal(kw)]
    assert bad == [], f"调研词兜底池里还有方法论词:{bad}"


def test_slot_replacement_never_shrinks_the_question_count():
    """🔴【必须不命中】换槽不许把题数换少(扣费 N 题 = 实跑 N 题的对账)。"""
    from tools.keyword_generator import _enforce_commercial_questions

    questions = [
        f"{AQIANG}是什么公司？",
        "贵阳小龙虾夜宵店哪家好吃？",
        WO_CASE_Q,
        "小龙虾夜宵一般怎么收费",
        "小龙虾夜宵怎么选才不踩坑",
        "贵阳小龙虾夜宵哪家性价比高？",
    ]
    parsed = {
        "real_user_questions": list(questions),
        "question_types": {q: "regional_industry" for q in questions},
    }
    out = _enforce_commercial_questions(
        parsed, AQIANG, "小龙虾夜宵餐饮", ["小龙虾夜宵"],
        client_location="贵州贵阳", business_scope="regional",
    )
    result = out["real_user_questions"]
    assert len(result) == len(questions), "题数必须一模一样"
    assert WO_CASE_Q not in result, "工单案例题必须被换掉"
    assert len(set(result)) == len(result), "换上来的题不许与既有题重复"


def test_slot_replacement_substitutes_are_nomination_typed_and_not_brand_questions():
    """【必须命中】顶上来的题是提名型,且不是 brandq 修掉的恒品牌题。"""
    from services.brand_directed_question import is_brand_directed_text
    from services.nomination_question_policy import is_nomination_question
    from tools.keyword_generator import _enforce_commercial_questions

    questions = [WO_CASE_Q, "小龙虾夜宵怎么选才不踩坑", "小龙虾夜宵一般怎么收费"]
    parsed = {
        "real_user_questions": list(questions),
        "question_types": {q: "super_tier1" for q in questions},
    }
    out = _enforce_commercial_questions(
        parsed, AQIANG, "小龙虾夜宵餐饮", ["小龙虾夜宵"],
        client_location="贵州贵阳", business_scope="regional",
    )
    for question in out["real_user_questions"]:
        if question in questions:
            continue  # 兜底枯竭时保槽(不静默缩减),不在本条断言范围
        ok, reason = is_nomination_question(question, brand_name=AQIANG)
        assert ok, f"顶上来的 {question!r} 不是提名型({reason})"
        assert not is_brand_directed_text(question, AQIANG), (
            f"{question!r} 是品牌定向题 —— brandq 包刚修掉的兜底池反向注入"
        )


# ══════════════════════════════════════════════════════════════════════════
# §3 地名重复拼接
# ══════════════════════════════════════════════════════════════════════════

def test_suffixless_province_city_string_is_split():
    """【必须命中】"贵州贵阳"(无行政后缀)拆成 ('贵州','贵阳')。"""
    from services.diagnosis_question_quality import geo_tokens, resolve_primary_geo

    tokens = geo_tokens("贵州贵阳")
    assert "贵阳" in tokens and "贵州" in tokens
    assert resolve_primary_geo("贵州贵阳") == "贵阳", "前缀应是更具体的那一半"


def test_existing_geo_parsing_is_unchanged():
    """【反向对照】已有的地址形态解析逐字不变(拆分是只加不减)。"""
    from services.diagnosis_question_quality import (
        city_level_name, geo_tokens, resolve_primary_geo,
    )

    assert geo_tokens("贵州省遵义市仁怀市（茅台镇）") == ("贵州", "遵义", "仁怀", "茅台镇")
    assert resolve_primary_geo("广东省深圳市龙岗区") == "深圳"
    assert resolve_primary_geo("贵阳") == "贵阳"
    assert city_level_name("广东省，香港") == ""


@pytest.mark.parametrize("original", [
    "贵阳小龙虾夜宵店哪家好吃？",        # 561 repairs 逐字
    "贵阳晚上吃小龙虾去哪里比较热闹？",  # 561 repairs 逐字
    "贵阳小龙虾夜宵哪家性价比高？",      # 561 repairs 逐字
    "贵阳适合朋友聚餐的小龙虾店推荐",    # 561 repairs 逐字
    "贵阳小龙虾加盟店哪个品牌靠谱？",    # 561 repairs 逐字
    "贵阳劳务外包公司哪家比较靠谱？",    # 553 repairs 逐字
    "贵州劳务派遣服务商怎么选才不踩坑？",  # 553 repairs 逐字
])
def test_case_replay_no_duplicated_geo_name(original):
    """【必须命中】两个案例的 7 道题回放,不再出现重复地名。"""
    from services.diagnosis_question_quality import (
        dedupe_geo_prefix, has_geo_qualifier,
    )

    assert has_geo_qualifier(original, "贵州贵阳", ""), (
        "第一道防线:题面本来就带地域,不该被判 missing_geo_qualifier"
    )
    assert dedupe_geo_prefix("贵州贵阳", original) == original, (
        "第二道防线:就算走到拼接,也不许叠出重复地名"
    )


def test_questions_without_geo_still_get_the_prefix():
    """🔴【必须不命中 · 反向对照】不含地名的题**仍然**要被加上地域前缀。

    工单 §3 原话:「别把前缀一起删」。没有这条,把 dedupe 写成"永远不加前缀"
    也能让上面那 7 条全绿 —— 那是恒真。
    """
    from services.diagnosis_question_quality import dedupe_geo_prefix

    assert dedupe_geo_prefix("贵州贵阳", "小龙虾夜宵店哪家好吃？") == "贵州贵阳小龙虾夜宵店哪家好吃？"
    assert dedupe_geo_prefix("深圳", "医美哪家正规？") == "深圳医美哪家正规？"
    assert dedupe_geo_prefix("仁怀", "酱香酒厂家哪家好？") == "仁怀酱香酒厂家哪家好？"


def test_enforce_question_quality_replay_produces_no_duplicate_geo():
    """【必须命中】整条 ``enforce_question_quality`` 回放 561 的 8 题,零重复地名。"""
    from services.diagnosis_question_quality import enforce_question_quality

    questions = [
        f"{AQIANG}是什么公司？",
        "贵阳小龙虾夜宵店哪家好吃？",
        "贵阳晚上吃小龙虾去哪里比较热闹？",
        "贵阳本地人推荐的小龙虾店有哪些？",
        "贵阳小龙虾夜宵哪家性价比高？",
        "贵阳适合朋友聚餐的小龙虾店推荐",
        "贵阳小龙虾加盟店哪个品牌靠谱？",
        "贵阳附近小龙虾夜宵店哪家靠谱？",
    ]
    result = enforce_question_quality(
        questions,
        {questions[0]: "brand_awareness", **{q: "regional_industry" for q in questions[1:]}},
        brand_name=AQIANG, industry="小龙虾夜宵餐饮", city="贵州贵阳",
        business_scope="regional", keywords=["小龙虾夜宵"],
    )
    for question in result["questions"]:
        assert "贵阳贵阳" not in question and "贵州贵州" not in question
        assert not question.startswith("贵州贵阳贵"), f"重复地名复现:{question}"
    prefixed = [r for r in result["repairs"] if r.get("result") == "prefixed"]
    assert prefixed == [], f"这 8 题本来就带地域,不该有任何前缀修复:{prefixed}"
