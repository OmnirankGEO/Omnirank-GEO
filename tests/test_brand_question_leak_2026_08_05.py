"""判别测试 · 公司题重复 + 漏网进竞争格局(WO_BRAND_QUESTION_LEAK_2026-08-05)。

坐实的事故(2026-08-06 01:14 生产只读现取,不是推断):

  报告 551「深圳市晨光富士电梯」8 问里有**两道**指向公司的题:
    · 「深圳市晨光富士电梯有限公司是做什么的？」 layer_key=brand_awareness  ← 认对了
    · 「深圳市晨光富士电梯是什么公司？」         layer_key=super_tier1      ← 漏网
  漏斗落库快照:brand 4/4 · local 1/20 · **scenario 4/8** —— 场景转化层那 4 个命中
  全部来自漏网那道公司题(问公司自己,AI 当然提到公司),该层 20/40 分是虚的。
  竞品榜落库快照第 3 名是「深圳市晨光富士电梯有限公司」(4 次)= 客户自己。

  影响面(同一次只读现取):有 layer_key 的 32 份 v2 报告里 **22 份**中招(69%),
  且漏网题**永远是同一句模板**「{品牌}是什么公司？」。

🔴 工单 §1 把根因判成「LLM 生成时归错层」,**用仓库真函数复现证伪了**
   (``scripts/probe_brand_question_leak_2026_08_05.py``,逐字复现 551 的 8 题):
   真凶是 ``tools/keyword_generator._enforce_commercial_questions`` 的**等槽兜底池**——
   池子来自 ``_fallback_business_context``,其第 0 条恒为 ``f"{brand_name}是什么公司？"``。
   任何非品牌槽位的题被商业意图闸判废时顶上来的就是它,**还原样继承被替换槽的层标签**。
   LLM 打的那道品牌题标签自始至终是对的。

跑法:
    PYTHONUTF8=1 python -m pytest -q tests/test_brand_question_leak_2026_08_05.py
变异自检(证明这些断言有判别力):
    PYTHONUTF8=1 python tests/mutation_runner_brand_question_leak.py
"""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ── 551 生产实测常量(只读现取,勿改) ─────────────────────────────────────────
BRAND_551 = "深圳市晨光富士电梯"
Q_BRAND_LABELLED = "深圳市晨光富士电梯有限公司是做什么的？"   # 标签对
Q_BRAND_LEAKED = "深圳市晨光富士电梯是什么公司？"             # 标签错(super_tier1)
Q_RIVALS_551 = (
    "深圳观光电梯定制哪家靠谱？",
    "深圳别墅电梯安装哪家公司服务好？",
    "深圳旧楼加装电梯找谁比较放心？",
    "深圳电梯维保公司哪家响应速度快？",
    "深圳非标井道电梯定制哪家好？",
    "深圳观光电梯定制一般怎么收费？",
)
# 551 竞品榜落库快照(客户自己排第 3)
TOP_BRANDS_551 = (
    ("深圳市恒通电梯有限公司", 6),
    ("深圳天祥电梯工程有限公司", 5),
    ("深圳市晨光富士电梯有限公司", 4),   # ← 客户自己
    ("富士电梯(中国)有限公司", 3),
    ("深圳华商电梯工程有限公司", 3),
)


# ══════════════════════════════════════════════════════════════════════════
# A. 文本判据 SSOT(services/brand_directed_question.py)
# ══════════════════════════════════════════════════════════════════════════

def test_text_rule_catches_both_company_questions_of_551():
    """两道公司题**都**要被文本判据认出来 —— 包括标签写着 super_tier1 的那道。"""
    from services.brand_directed_question import is_brand_directed_text

    assert is_brand_directed_text(Q_BRAND_LABELLED, BRAND_551)
    assert is_brand_directed_text(Q_BRAND_LEAKED, BRAND_551)


@pytest.mark.parametrize("question", Q_RIVALS_551)
def test_text_rule_does_not_swallow_real_competitive_questions(question):
    """反向对照:551 真正的竞争面 6 题一道都不能被当成品牌题。

    没有这条,"把所有题都判成品牌题"也能让上面那条绿 —— 那是恒真判据。
    """
    from services.brand_directed_question import is_brand_directed_text

    assert not is_brand_directed_text(question, BRAND_551)


def test_short_tokens_are_dropped_by_length():
    """「电梯」「深圳」这种 2 字词绝不能成为判据 token。

    成了的话「深圳市恒通电梯有限公司」这种正经同行会被当客户自己剔掉。
    """
    from services.brand_directed_question import brand_directed_tokens

    assert brand_directed_tokens("电梯") == ()
    assert brand_directed_tokens("深圳") == ()
    assert brand_directed_tokens("科技") == ()
    # 正常品牌名必须出 token(否则上一行的"全空"是因为函数坏了,不是因为过滤对了)
    assert brand_directed_tokens(BRAND_551) == (BRAND_551,)


def test_generic_industry_terms_are_dropped_even_when_long_enough():
    """行业通用词过滤是**独立于长度**的一道闸。

    🔴 判据必须用 ≥3 字的通用词:2 字的(电梯/科技)本来就被 MIN_TOKEN_LENGTH 拦掉,
    拿它们当断言 = 删掉通用词过滤也照样绿(变异 M4 实测存活,就是这么暴露的)。
    """
    from services.brand_directed_question import brand_directed_tokens

    assert brand_directed_tokens("制造业") == ()
    assert brand_directed_tokens("服务业") == ()
    # 反向对照:同样 3 字的非通用词必须留下
    assert brand_directed_tokens("简小悦") == ("简小悦",)


def test_legal_suffix_variant_is_covered():
    """登记名与回答里的写法差一截法人后缀,是本单竞品榜漏剔的直接原因。"""
    from services.brand_directed_question import is_client_own_brand

    assert is_client_own_brand("深圳市晨光富士电梯有限公司", BRAND_551)
    assert is_client_own_brand("深圳市晨光富士电梯", "深圳市晨光富士电梯有限公司")


def test_fallback_matcher_agrees_with_ai_tester():
    """两份同规则实现必须逐条一致 —— 对方改规则要让这条转红,而不是本地静默走偏。

    降级实现存在的理由:``ai_tester`` 拖 ``agentscope``/``mcp``,版本一漂就 import 不到
    (2026-08-05 在 python:3.12 干净容器里实测到)。那时若降级成"归一后相等",
    客户又会回到自家竞品榜 —— 正是本单要修的那条判据。
    """
    from tools.ai_visibility.ai_tester import _exact_or_strict_match
    from services.brand_directed_question import _fallback_exact_or_strict_match

    corpus = [
        (BRAND_551, "深圳市晨光富士电梯有限公司"),
        (BRAND_551, "深圳市晨光富士电梯"),
        (BRAND_551, "深圳市恒通电梯有限公司"),
        (BRAND_551, "富士电梯(中国)有限公司"),
        (BRAND_551, "奥的斯"),
        ("碧玉良缘", "新城碧玉良缘珠宝城"),          # 短方 4 字 → 规则明确不认
        ("深圳市驰鲸科技有限公司", "驰鲸科技"),
        ("贵州禾泉酒业", "贵州禾泉酒业有限公司"),
        ("全域上榜（深圳）科技有限公司", "全域上榜科技"),
        ("", "深圳市恒通电梯有限公司"),
        (BRAND_551, ""),
    ]
    for target, candidate in corpus:
        primary = _exact_or_strict_match(target, [candidate])[0] if target and candidate else False
        assert _fallback_exact_or_strict_match(target, candidate) is bool(primary), \
            f"两份实现分歧: target={target!r} candidate={candidate!r}"


def test_text_rule_covers_registered_name_vs_short_form_in_question():
    """品牌登记的是全称、题面用的是去后缀简称 —— 文本判据必须照样命中。

    生产实证:547「贵州晨曦慧远教育科技有限公司」/ 535「佛山市顺德区万嘉澜不锈钢
    制品有限公司」都是登记全称,题面写法各不相同。
    没有这条,``_candidate_forms`` 里的去后缀那一层就是死代码(变异 M5 实测存活)。
    """
    from services.brand_directed_question import is_brand_directed_text

    registered = "深圳市晨光富士电梯有限公司"
    assert is_brand_directed_text("深圳市晨光富士电梯是什么公司？", registered)
    assert is_brand_directed_text("深圳市晨光富士电梯怎么样？靠谱吗", registered)
    # 反向对照:同行题不许被这层放宽带出来
    assert not is_brand_directed_text("深圳非标井道电梯定制哪家好？", registered)


@pytest.mark.parametrize("rival", [n for n, _ in TOP_BRANDS_551 if "晨光富士" not in n])
def test_real_rivals_are_not_mistaken_for_the_client(rival):
    """反向对照:551 榜上真同行一个都不能被剔。含「富士电梯(中国)有限公司」——
    它与客户共享「富士电梯」字样,是最容易被宽判据误杀的那个。"""
    from services.brand_directed_question import is_client_own_brand

    assert not is_client_own_brand(rival, BRAND_551)


def test_verbatim_mode_exemption_covers_relabeling_only():
    """自定义/逐字模式的豁免**只覆盖「不替客户重新分层」**,不覆盖题面判据。

    🔴 [WO_236-c1b · 2026-09-17 改判 · 原名 test_verbatim_mode_exemption_is_preserved]
       本条原来断言「verbatim ⇒ 一律判 False」,连题面判据一起关掉。
       改判**不是**推翻 §2.2 —— 恰恰相反:

       §2.2 原文的**本单核心**是「brand-directed 判定增加**与标签独立的文本判据**:
       题面含品牌名即判品牌题」;末尾那句「现有 verbatim/custom 模式豁免保留」
       是挂靠旧行为的一句沿用,**不是重新论证过的产品决定**。
       原实现让 verbatim 把**核心那一半**也关掉了,等于用"不替她分层"的理由
       去决定"算不算竞争样本"——两件事被混成一件。

       代价(生产只读实证):#700 / #726 两份**真客户**报告
       `diagnosis_mode="verbatim"` + `question_origins` 缺失 ⇒ 全部题豁免 ⇒
       三道品牌定向题 × 4 平台 = 12 条全进竞争分母,页面给出「被提及 8/10 次」+ 竞品排行,
       而脚注还宣称「口径已排除直接问本品牌名的问题」。

       ⇒ 保留标签半(不替客户重新分层),恢复文本半(题面说了算)。
       **本条保留不删**:删了就看不到这里曾经有过一个相反的写法,也看不到它为什么改。
    """
    from services.brand_directed_question import resolve_brand_directed

    verdict = resolve_brand_directed(
        Q_BRAND_LEAKED, layer_key="super_tier1", brand_name=BRAND_551, verbatim=True
    )
    # 题面含品牌名 ⇒ 仍判品牌题(§2.2 的核心那一半)
    assert verdict.is_brand_directed is True
    assert verdict.by_text is True
    # 而标签那一半在 verbatim 下**不用** —— 豁免仍然活着,只是缩回它该管的范围
    assert verdict.by_label is False

    # 非 verbatim 时标签判据生效 —— 两种模式必须能分辨,否则"豁免"已名存实亡
    planned = resolve_brand_directed(
        Q_BRAND_LEAKED, layer_key="super_tier1", brand_name=BRAND_551, verbatim=False
    )
    assert planned.is_brand_directed is True
    assert planned.by_label is False, "该题标签是 super_tier1,标签判据本就不该命中"


def test_verbatim_still_refuses_to_relabel_by_layer():
    """🔴 豁免**没被整个撤掉**:题面不是品牌题、但标签写着 brand_awareness 时,
    verbatim 下仍不剔(不替客户重新分层),非 verbatim 下剔。

    没有这一条,把 verbatim 分支整个删掉也会让上面那条绿 ——
    那就不是"缩回该管的范围",是"豁免没了"。
    """
    from services.brand_directed_question import (
        is_brand_directed_text, resolve_brand_directed,
    )

    question = Q_RIVALS_551[0]
    assert not is_brand_directed_text(question, BRAND_551), "样本前提不成立"

    as_verbatim = resolve_brand_directed(
        question, layer_key="brand_awareness", brand_name=BRAND_551, verbatim=True)
    as_planned = resolve_brand_directed(
        question, layer_key="brand_awareness", brand_name=BRAND_551, verbatim=False)
    assert as_verbatim.is_brand_directed is False
    assert as_planned.is_brand_directed is True
    assert as_verbatim.is_brand_directed != as_planned.is_brand_directed


def test_relabeled_flag_only_fires_when_label_and_text_disagree():
    """留痕口径:标签已对 → 不算错标;标签错 → 算。这个数就是上游错标率。"""
    from services.brand_directed_question import resolve_brand_directed

    right = resolve_brand_directed(
        Q_BRAND_LABELLED, layer_key="brand_awareness", brand_name=BRAND_551
    )
    wrong = resolve_brand_directed(
        Q_BRAND_LEAKED, layer_key="super_tier1", brand_name=BRAND_551
    )
    assert right.relabeled_by_text is False
    assert wrong.relabeled_by_text is True


def test_alias_source_stays_canonical_not_resolver_derived_aliases():
    """品牌题归类必须只读确认原串,不能继承解析器的派生身份名称。"""
    src = (REPO / "services" / "brand_directed_question.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "all_trusted_names" not in attrs | names
    assert "split_brand_aliases" not in attrs | names


# ══════════════════════════════════════════════════════════════════════════
# B. 出题层(§2.1)· 真凶那处
# ══════════════════════════════════════════════════════════════════════════

def _llm_parsed_with_two_company_questions() -> dict:
    questions = [Q_BRAND_LABELLED, *Q_RIVALS_551, Q_BRAND_LEAKED]
    types = {Q_BRAND_LABELLED: "brand_awareness"}
    for q in Q_RIVALS_551[:5]:
        types[q] = "regional_industry"
    types[Q_RIVALS_551[5]] = "super_tier1"
    types[Q_BRAND_LEAKED] = "super_tier1"
    return {"real_user_questions": list(questions), "question_types": dict(types)}


def test_substitution_pool_no_longer_hands_out_the_company_question():
    """真凶锁:等槽兜底池里**不能有**品牌定向题。

    改前:池子第 0 条恒为 f"{brand}是什么公司？",任何槽位判废都会被它顶上,
    并继承那个槽的层标签 —— 22/32 份报告的漏网题都是这么来的。
    """
    from tools.keyword_generator import (
        _enforce_commercial_questions,
        _fallback_business_context,
    )
    from services.brand_directed_question import is_brand_directed_text

    raw_pool = _fallback_business_context(
        BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )["real_user_questions"]
    # 前提自证:兜底模板**本来就**含一道公司题(否则本条锁锁的是空气)
    assert any(is_brand_directed_text(q, BRAND_551) for q in raw_pool)

    # 用一道必被商业意图闸判废的知识题占住场景槽,逼出等槽替换
    parsed = {
        "real_user_questions": [Q_BRAND_LABELLED, *Q_RIVALS_551, "电梯的曳引比是什么原理？"],
        "question_types": {
            Q_BRAND_LABELLED: "brand_awareness",
            **{q: "regional_industry" for q in Q_RIVALS_551[:5]},
            Q_RIVALS_551[5]: "super_tier1",
            "电梯的曳引比是什么原理？": "super_tier1",
        },
    }
    out = _enforce_commercial_questions(
        parsed, BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    directed = [q for q in out["real_user_questions"] if is_brand_directed_text(q, BRAND_551)]
    assert len(directed) == 1, f"等槽替换又发了一道公司题: {directed}"
    assert out["question_types"][directed[0]] == "brand_awareness"


def test_dedupe_collapses_company_questions_to_exactly_one():
    """§2.1 验收:出题产物里公司题**恰好 1 道**,且挂品牌层;题量不缩减。"""
    from tools.keyword_generator import _dedupe_brand_directed_questions
    from services.brand_directed_question import is_brand_directed_text

    parsed = _llm_parsed_with_two_company_questions()
    before = len(parsed["real_user_questions"])
    out = _dedupe_brand_directed_questions(
        parsed, BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    questions = out["real_user_questions"]
    directed = [q for q in questions if is_brand_directed_text(q, BRAND_551)]
    assert len(directed) == 1
    assert directed[0] == Q_BRAND_LABELLED, "该留的是标签已挂品牌层的那道"
    assert out["question_types"][directed[0]] == "brand_awareness"
    assert len(questions) == before, "不许静默缩减题量(SSOT §9.6)"


def test_dedupe_relabels_a_lone_mislabeled_company_question():
    """只有一道公司题、但标签挂在场景层 → 改标品牌层(文本为准)。"""
    from tools.keyword_generator import _dedupe_brand_directed_questions

    parsed = {
        "real_user_questions": [Q_BRAND_LEAKED, *Q_RIVALS_551],
        "question_types": {
            Q_BRAND_LEAKED: "super_tier1",
            **{q: "regional_industry" for q in Q_RIVALS_551},
        },
    }
    out = _dedupe_brand_directed_questions(
        parsed, BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    assert out["question_types"][Q_BRAND_LEAKED] == "brand_awareness"


def test_dedupe_never_shrinks_the_question_set_when_the_pool_is_exhausted(monkeypatch):
    """兜底池枯竭时**保槽不缩减**(SSOT §9.6 不静默丢词),但必须改标品牌层。

    宁可品牌层多一道,也绝不让它继续冒充场景层去污染竞争面。
    这个分支正常 fixture 跑不到(池子有 7 条非品牌题),只能把池子打空来逼。
    """
    import tools.keyword_generator as kg
    from services.brand_directed_question import is_brand_directed_text

    monkeypatch.setattr(
        kg, "_fallback_business_context",
        lambda *a, **k: {"real_user_questions": [], "question_types": {}},
    )
    parsed = _llm_parsed_with_two_company_questions()
    before = list(parsed["real_user_questions"])
    out = kg._dedupe_brand_directed_questions(
        parsed, BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    assert len(out["real_user_questions"]) == len(before), "题量被静默缩减了"
    assert set(out["real_user_questions"]) == set(before), "池空时不许换题"
    directed = [q for q in out["real_user_questions"] if is_brand_directed_text(q, BRAND_551)]
    assert len(directed) == 2, "前提自证:池空 → 两道公司题都还在"
    for q in directed:
        assert out["question_types"][q] == "brand_awareness", \
            f"留下的公司题必须挂品牌层,不许继续冒充场景层: {q}"


def test_dedupe_leaves_clean_question_sets_untouched():
    """反向对照:一道公司题 + 标签已对 → 原样返回,不许乱动别人的题。"""
    from tools.keyword_generator import _dedupe_brand_directed_questions

    parsed = {
        "real_user_questions": [Q_BRAND_LABELLED, *Q_RIVALS_551],
        "question_types": {
            Q_BRAND_LABELLED: "brand_awareness",
            **{q: "regional_industry" for q in Q_RIVALS_551},
        },
    }
    out = _dedupe_brand_directed_questions(
        dict(parsed), BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    assert out["real_user_questions"] == parsed["real_user_questions"]
    assert out["question_types"] == parsed["question_types"]


def _analyze_client_business_node() -> ast.AsyncFunctionDef:
    tree = ast.parse((REPO / "tools" / "keyword_generator.py").read_text(encoding="utf-8"))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "analyze_client_business"
    )
    return node


def test_every_generation_exit_goes_through_the_choke_point():
    """🔴 出题入口的**每一个** return 都必须过收口 —— 这条不变量由锁保证,不靠人记得。

    2026-08-05 实测:这个入口有 4 个出口,其中 3 个降级出口
    (无 DASHSCOPE_API_KEY / industry 为空 / LLM 异常)直接 return 兜底产物,
    绕过品牌名过滤、商业意图闸、公司题去重、选词质量守卫 —— 一个都没走。
    将来任何人加第 5 个出口,漏了收口这条就转红。
    """
    node = _analyze_client_business_node()
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    assert len(returns) >= 4, f"出口数变了({len(returns)}),先确认是不是有人加/删了分支"
    for ret in returns:
        assert isinstance(ret.value, ast.Call), ast.dump(ret)
        assert isinstance(ret.value.func, ast.Name), ast.dump(ret)
        assert ret.value.func.id == "_finalize_business_context", \
            f"第 {ret.lineno} 行的出口绕过了收口: {ast.dump(ret.value.func)}"


def test_choke_point_actually_applies_the_guard():
    """反向对照:光有"每个出口都调收口"还不够 —— 收口自己必须真做事。

    没有这条,把 ``_finalize_business_context`` 写成 ``return parsed`` 也能让上一条绿。
    """
    from tools.keyword_generator import _finalize_business_context
    from services.brand_directed_question import is_brand_directed_text

    parsed = _llm_parsed_with_two_company_questions()
    out = _finalize_business_context(
        parsed, BRAND_551, "电梯制造与安装", ["观光电梯"],
        client_location="深圳", business_scope="区域",
    )
    directed = [q for q in out["real_user_questions"] if is_brand_directed_text(q, BRAND_551)]
    assert len(directed) == 1


@pytest.mark.parametrize("brand", ["奥特莱斯", "碧玉良缘"])
def test_degraded_exit_never_fakes_competitive_visibility(brand):
    """降级出口最重的那个形态:``industry`` 为空 → 代码把 ``brand_name`` 当 industry 传下去
    → ``distill_trades`` 炼不出品类词时最后一道回落是 ``str(industry)`` = **品牌名**
    → 8 道题全含品牌名、7 道挂竞争层 → 必然命中 → 报告给出接近满分的假可见度。

    「碧玉良缘」是真实生产品牌(报告 478/476/439/403)。
    修完后:题量不变(不静默丢词),但含品牌名的题一律归品牌认知层 ——
    竞争面变 0 样本,漏斗按"部分层无样本"折算并标注。
    不假装数据完整,好过凑一个满分。
    """
    from tools.keyword_generator import (
        _fallback_business_context,
        _finalize_business_context,
    )
    from services.brand_directed_question import is_brand_directed_text

    raw = _fallback_business_context(
        brand, brand, ["折扣店"], client_location="深圳", business_scope="区域",
    )
    # 前提自证:不过收口时确实是被污染的(否则这条锁锁的是空气)
    raw_bad = [
        q for q in raw["real_user_questions"]
        if is_brand_directed_text(q, brand)
        and raw["question_types"].get(q) != "brand_awareness"
    ]
    assert raw_bad, "前提自证失败:兜底产物本应含错层的品牌题"

    out = _finalize_business_context(
        raw, brand, brand, ["折扣店"],
        client_location="深圳", business_scope="区域",
    )
    assert len(out["real_user_questions"]) == len(raw["real_user_questions"]), "题量被静默缩减"
    for q in out["real_user_questions"]:
        if is_brand_directed_text(q, brand):
            assert out["question_types"][q] == "brand_awareness", \
                f"含品牌名的题仍挂在竞争层,会虚报可见度: {q}"


def test_degraded_exit_funnel_flags_thin_coverage():
    """收口把竞争题归到品牌层之后,漏斗必须**显性标出覆盖过薄**,而不是照样发一份漂亮报告。

    污染态(8 题全被当竞争题且全命中)= 100 分 · 主导级 · partial_sample 干净 —— 看不出任何问题。
    收口态(全归品牌层)= partial_sample/level_capped 都为真 · 等级封顶到成长级 ·
                        竞争两层显示「本层未实测」。

    ⚠️ 本条**不断言总分变小**:重归一会把品牌层权重放大到 100,总分仍是 100。
    那是 funnel_score 的既有设计,本工单边界明令不改评分权重/漏斗结构 —— 属已上报的残留风险。
    这里钉住的是"薄覆盖必须被标出来"这几个标志,谁把 audit#7 封顶去掉就转红。
    """
    from tools.scoring.funnel_score import calculate_funnel_score

    polluted = calculate_funnel_score(
        brand_detected=4, brand_total=4,
        local_detected=20, local_total=20,
        scenario_detected=8, scenario_total=8,
    )
    assert polluted["level_meta"]["partial_sample"] is False, "前提自证:污染态标志是干净的"
    assert polluted["level"] == "主导级"

    guarded = calculate_funnel_score(
        brand_detected=40, brand_total=40,
        local_detected=0, local_total=0,
        scenario_detected=0, scenario_total=0,
    )
    assert guarded["level_meta"]["partial_sample"] is True
    assert guarded["level_meta"]["level_capped"] is True
    assert guarded["level"] == "成长级", "单层覆盖不许冒充主导/健康级"
    notes = {l["key"]: l["sample_note"] for l in guarded["layers"]}
    assert notes["local"] == "本层未实测"
    assert notes["scenario"] == "本层未实测"


def test_dedupe_is_wired_into_the_generation_pipeline():
    """接线锁:实现了但没接进出题主链 = 白写(2026-08-05 推荐链那单的教训)。

    并且必须在层配额守卫**之前**调 —— 之后调会让被腾空的层低于样本下限。
    """
    src = (REPO / "tools" / "keyword_generator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.append(node.func.id)
    assert "_dedupe_brand_directed_questions" in called, "去重函数没有任何调用点"

    dedupe_line = src.index("parsed = _dedupe_brand_directed_questions(")
    quality_line = src.index("parsed = _enforce_question_quality(")
    assert dedupe_line < quality_line, "去重必须排在层配额守卫之前"


# ══════════════════════════════════════════════════════════════════════════
# C. 漏斗(§0 第 2 处污染)· 场景转化层 4/8 的直接来源
# ══════════════════════════════════════════════════════════════════════════

def _detail_table_551() -> list[dict]:
    """复刻 551 的命中格局:两道公司题 4 引擎全中,竞争面 24 格只有 1 格中。"""
    engines = ("dashscope", "deepseek", "doubao", "yuanbao")

    def row(question, detected_engines):
        return {
            "question": question,
            "results": {
                e: {
                    "brand_detected": e in detected_engines,
                    "answer_summary": "已作答",
                    "mentioned_brands": (
                        ["深圳市晨光富士电梯有限公司"] if e in detected_engines else
                        ["深圳市恒通电梯有限公司"]
                    ),
                }
                for e in engines
            },
        }

    table = [row(Q_BRAND_LABELLED, engines)]
    table += [row(q, ()) for q in Q_RIVALS_551[:4]]
    table.append(row(Q_RIVALS_551[4], ("deepseek",)))   # 非标井道那 1 格
    table.append(row(Q_RIVALS_551[5], ()))
    table.append(row(Q_BRAND_LEAKED, engines))          # 漏网公司题 · 4/4 必中
    return table


def _question_types_551() -> dict:
    types = {Q_BRAND_LABELLED: "brand_awareness", Q_BRAND_LEAKED: "super_tier1"}
    for q in Q_RIVALS_551[:5]:
        types[q] = "regional_industry"
    types[Q_RIVALS_551[5]] = "super_tier1"
    return types


def test_funnel_stops_counting_the_company_question_as_scenario_layer():
    """§0 第 2 处:场景转化层 4/8=50% 的 4 个命中全部来自公司题。

    传 brand_name 后必须变成 品牌层 8/8 · 场景层 0/4 —— 场景层得分从虚高的 20/40 归 0。
    """
    from services.diagnosis_identity_review import aggregate_dimension_stats

    detail, types = _detail_table_551(), _question_types_551()

    before = aggregate_dimension_stats(detail, types)
    assert (before["super_tier1"]["detected"], before["super_tier1"]["total"]) == (4, 8), \
        "前提自证:不传品牌名时必须复现 551 落库的 4/8,否则本条锁锁的不是这个 bug"

    after = aggregate_dimension_stats(detail, types, brand_name=BRAND_551)
    assert (after["super_tier1"]["detected"], after["super_tier1"]["total"]) == (0, 4)
    assert (after["brand_awareness"]["detected"], after["brand_awareness"]["total"]) == (8, 8)
    # 决策获客层不受影响(只动该动的)
    assert before["regional_industry"] == after["regional_industry"]


def test_funnel_score_drops_by_the_inflated_scenario_points():
    """总分虚高约 20 分(工单 §0)—— 修完必须掉下来,且掉的正是场景层那 40 分权重的满格。"""
    from services.diagnosis_identity_review import aggregate_dimension_stats
    from tools.scoring.funnel_score import calculate_funnel_score

    detail, types = _detail_table_551(), _question_types_551()

    def score(stats):
        return calculate_funnel_score(
            brand_detected=stats["brand_awareness"]["detected"],
            brand_total=stats["brand_awareness"]["total"],
            local_detected=stats["regional_industry"]["detected"],
            local_total=stats["regional_industry"]["total"],
            scenario_detected=stats["super_tier1"]["detected"],
            scenario_total=stats["super_tier1"]["total"],
        )["total_score"]

    inflated = score(aggregate_dimension_stats(detail, types))
    honest = score(aggregate_dimension_stats(detail, types, brand_name=BRAND_551))
    assert inflated - honest >= 15, f"虚高分没掉下来: {inflated} → {honest}"


def test_funnel_backward_compatible_without_brand_name():
    """不传 brand_name 时行为必须与改前逐字一致(老调用方零影响)。"""
    from services.diagnosis_identity_review import aggregate_dimension_stats

    detail, types = _detail_table_551(), _question_types_551()
    assert aggregate_dimension_stats(detail, types) == aggregate_dimension_stats(
        detail, types, brand_name=""
    )


# ══════════════════════════════════════════════════════════════════════════
# D. 竞争模块(§2.2 分类兜底 + §2.3 竞品榜双保险)· 写入侧
# ══════════════════════════════════════════════════════════════════════════

def _report_data_551() -> dict:
    return {
        "brand_name": BRAND_551,
        "diagnosis_data": {
            "ai_visibility_data": {
                "detail_table": _detail_table_551(),
                "question_types": _question_types_551(),
                "engine_stats": {},
            }
        },
    }


def test_competition_module_excludes_the_mislabeled_company_question():
    """§2.2:标签写着 super_tier1 的公司题不得进竞争分母。

    551 实证 valid_total=28(= 32 格 − 品牌题 4 格),真实竞争面应是 24 格。
    """
    from services.report_writer_v2 import build_module_3_competition

    module = build_module_3_competition(_report_data_551())
    assert module["valid_total"] == 24, "两道公司题各 4 格都要出分母"
    assert module["brand_directed_valid"] == 8
    assert module["client_detected_count"] == 1, "真实竞争面 24 格里只有 1 格命中"


def test_competition_module_records_the_upstream_mislabel():
    """§2.2 留痕:错标 1 道 → 计数 1(按题计,不按格计)。"""
    from services.report_writer_v2 import build_module_3_competition

    module = build_module_3_competition(_report_data_551())
    assert module["brand_directed_relabeled_by_text"] == 1


def test_client_never_appears_in_its_own_competitor_board():
    """§2.3:客户自己(含法人后缀差异)绝不进自家竞品榜。"""
    from services.report_writer_v2 import build_module_3_competition

    module = build_module_3_competition(_report_data_551())
    names = [b["name"] for b in module["top_brands"]]
    assert "深圳市晨光富士电梯有限公司" not in names
    assert BRAND_551 not in names


def test_real_rivals_survive_the_competitor_exclusion():
    """反向对照:剔客户不能连同行一起剔 —— 否则"剔光"也能让上一条绿。"""
    from services.report_writer_v2 import build_module_3_competition

    module = build_module_3_competition(_report_data_551())
    names = [b["name"] for b in module["top_brands"]]
    assert "深圳市恒通电梯有限公司" in names


def test_competition_module_in_verbatim_mode_still_excludes_brand_directed():
    """verbatim 模式下,品牌定向题**仍然**被排除出竞争分母。

    🔴 [WO_236-c1b · 2026-09-17 改判 · 原名 test_competition_module_verbatim_mode_still_exempt]
       本条原来断言 `brand_directed_valid == 0` 且 `valid_total == 32`
       —— 也就是 verbatim 下一条都不剔、32 格全进分母。
       那正是 #700/#726 在真客户报告上出事的那条路(理由见
       `test_verbatim_mode_exemption_covers_relabeling_only` 的说明)。
       豁免缩回「不替客户重新分层」之后,题面含品牌名的两道题照剔。
       **保留不删**:它是这条口径改过一次的唯一痕迹。
    """
    from services.report_writer_v2 import build_module_3_competition

    data = _report_data_551()
    data["diagnosis_data"]["ai_visibility_data"]["diagnosis_mode"] = "verbatim"
    module = build_module_3_competition(data)
    # 与非 verbatim 同一读数:两道公司题 × 4 格 = 8 格出分母,真实竞争面 24 格
    assert module["brand_directed_valid"] == 8
    assert module["valid_total"] == 24
    # 反向对照:非 verbatim 下逐格相同 —— 两条路现在给出同一个答案,
    # 说明"谁写的"不再影响"算不算竞争样本"(而它仍影响"归哪一层")
    planned = build_module_3_competition(_report_data_551())
    assert (module["brand_directed_valid"], module["valid_total"]) == (
        planned["brand_directed_valid"], planned["valid_total"])


def test_raw_appendix_emits_corrected_layer_key_into_the_artifact():
    """3_raw 产物里的 layer_key / layer 是展示层唯一能读到的分层信号。

    上游错标必须在**写进客户产物之前**按题面纠正 —— 否则新报告落库时
    带的仍是 super_tier1,存量数据又多一份脏快照。
    (这条真正跑 ``build_module_3_raw_ai_appendix``;E 节那些用例是手写产物,
     跑不到这段代码 —— 变异 M13 实测存活就是这么暴露的。)
    """
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    module = build_module_3_raw_ai_appendix(_report_data_551())
    by_question = {t["question"]: t for t in module["tests"]}
    assert by_question[Q_BRAND_LEAKED]["layer_key"] == "brand_awareness"
    assert by_question[Q_BRAND_LEAKED]["layer"] == "品牌认知层"
    # 反向对照:真竞争题的层不许被改
    assert by_question[Q_RIVALS_551[5]]["layer_key"] == "super_tier1"
    assert by_question[Q_RIVALS_551[0]]["layer_key"] == "regional_industry"


def test_raw_appendix_keeps_verbatim_artifacts_unlayered():
    """verbatim 产物照旧不分层(layer_key=None / layer=自定义)。"""
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    data = _report_data_551()
    data["diagnosis_data"]["ai_visibility_data"]["diagnosis_mode"] = "verbatim"
    module = build_module_3_raw_ai_appendix(data)
    assert all(t["layer_key"] is None for t in module["tests"])
    assert all(t["layer"] == "自定义" for t in module["tests"])


# ══════════════════════════════════════════════════════════════════════════
# E. 展示层(§3 存量报告自愈)· 读时计算的那一半
# ══════════════════════════════════════════════════════════════════════════

def _presentation_551(*, layer_key_for_leaked: str | None = "super_tier1") -> dict:
    from services.public_report_presentation import build_public_report_presentation

    def test_row(question, layer_key, detected):
        return {
            "question": question,
            "layer_key": layer_key,
            "results": [
                {
                    "engine": e,
                    "status": "answered",
                    "brand_detected": detected,
                    "full_response": "已作答,内容略。",
                }
                for e in ("dashscope", "deepseek", "doubao", "yuanbao")
            ],
        }

    return build_public_report_presentation(
        {
            "funnel": {"layers": [
                {"key": "scenario", "label": "场景转化层", "detected": 4, "total": 8}
            ]},
            "client": {"modules": {
                "1": {"conclusion_text": "形成了可核验的客户结论。"},
                "3_raw": {"tests": [
                    test_row(Q_BRAND_LABELLED, "brand_awareness", True),
                    test_row(Q_BRAND_LEAKED, layer_key_for_leaked, True),
                    *[test_row(q, "regional_industry", False) for q in Q_RIVALS_551[:5]],
                    test_row(Q_RIVALS_551[5], "super_tier1", False),
                ]},
                "3_competition": {
                    "top_brands": [{"name": n, "count": c} for n, c in TOP_BRANDS_551],
                    "valid_total": 28,
                    "client_detected_count": 5,
                    "brand_directed_valid": 4,
                    "denominator_scope": "excludes_brand_directed_questions",
                },
            }},
        },
        industry="电梯制造与安装",
        canonical_score=43,
        generated_at=datetime(2026, 8, 6, 1, 3),
        keyword_count=8,
        brand_name=BRAND_551,
    )


def test_presentation_excludes_mislabeled_company_question_from_mention_rate():
    """§0 第 1 处:平台提及率 14.3% 全部来自漏网公司题 → 展示层必须把它排除。"""
    presentation = _presentation_551()
    rows = presentation["platforms"]["data"]
    assert rows, "前提自证:平台行不能为空"
    for row in rows:
        # 24 格竞争面全部未命中 → 提及率 0%,不是被公司题顶起来的 14.3%
        assert row["mentionRatePct"] == 0, row


def test_presentation_drops_client_from_stored_competitor_snapshot():
    """§3 关键:竞品榜是**落库快照**,展示层再剔一次 → 存量报告刷新即自愈,零迁移。"""
    presentation = _presentation_551()
    names = [c["name"] for c in presentation["competitive"]["data"]["competitors"]]
    assert "深圳市晨光富士电梯有限公司" not in names
    assert "深圳市恒通电梯有限公司" in names, "反向对照:同行必须还在"


def test_presentation_text_rule_also_covers_artifacts_without_layer_key():
    """老产物根本没有 layer_key(生产 189 份 v2 报告里只有 32 份有)。
    无标签时文本判据是唯一防线。"""
    presentation = _presentation_551(layer_key_for_leaked=None)
    for row in presentation["platforms"]["data"]:
        assert row["mentionRatePct"] == 0, row


def test_presentation_custom_layer_label_stays_exempt():
    """verbatim 产物 layer='自定义' 且无 layer_key → 沿用既有豁免,不替用户判。"""
    from services.public_report_presentation import _is_brand_directed_test

    assert _is_brand_directed_test(
        {"question": Q_BRAND_LEAKED, "layer": "自定义"}, BRAND_551
    ) is False
    # 反向对照:同一道题在非自定义产物里必须判是
    assert _is_brand_directed_test(
        {"question": Q_BRAND_LEAKED, "layer_key": "super_tier1"}, BRAND_551
    ) is True


def test_presentation_without_brand_name_behaves_exactly_as_before():
    """brand_name 缺省 → 逐字回到旧口径(向后兼容)。"""
    from services.public_report_presentation import _is_brand_directed_test

    assert _is_brand_directed_test({"question": Q_BRAND_LEAKED, "layer_key": "super_tier1"}) is False
    assert _is_brand_directed_test({"question": Q_BRAND_LEAKED, "layer_key": "brand_awareness"}) is True


# ══════════════════════════════════════════════════════════════════════════
# F. 接线锁 · 防"实现了但没人调"
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "relpath",
    [
        "services/report_writer_v2.py",
        "services/public_report_presentation.py",
        "services/diagnosis_identity_review.py",
        "tools/keyword_generator.py",
    ],
)
def test_every_consumer_actually_imports_the_ssot(relpath):
    """直接消费点都必须走同一个判据模块,不许各写各的(2026-08-02 死函数那单的教训)。

    ``diagnosis_identity_decision.py`` **不在这张表里**:它是间接消费方(把品牌名
    与别名转传给 ``aggregate_dimension_stats``,自己不做判定)。它的接线由下面
    ``test_identity_decision_forwards_brand_identity_into_the_aggregator`` 单独钉。
    """
    tree = ast.parse((REPO / relpath).read_text(encoding="utf-8"))
    modules = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "services.brand_directed_question" in modules, relpath


def test_identity_decision_forwards_brand_identity_into_the_aggregator():
    """身份复核重算漏斗时也要传品牌名 —— 否则复核完的分仍带着那 20 分虚高。

    同时钉死别名取的是 ``canonical_names``/``trusted_aliases`` 原串,
    不是 ``all_trusted_names``:问题分类只接受确认原串,不继承解析器派生身份名称。
    """
    src = (REPO / "services" / "diagnosis_identity_decision.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "aggregate_dimension_stats"
    ]
    assert len(calls) == 1
    kwargs = {kw.arg for kw in calls[0].keywords}
    assert {"brand_name", "brand_aliases"} <= kwargs, kwargs
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    consts = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "all_trusted_names" not in attrs | consts


def test_workflow_passes_brand_name_into_the_funnel_aggregator():
    """漏斗聚合器拿不到 brand_name 就等于没修 —— 主链两个调用点都要传。"""
    src = (REPO / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "aggregate_dimension_stats"
    ]
    assert len(calls) == 2, f"调用点数量变了: {len(calls)}"
    for call in calls:
        assert any(kw.arg == "brand_name" for kw in call.keywords), ast.dump(call)
