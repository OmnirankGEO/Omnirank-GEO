"""[P4 标题批次级去重 · 2026-07-31] 工单 §7 四锁 · 行为级,禁源码串断言。

锁 ① 同批注入 6 个必然撞公式的输入 → 保存后两两不同且无完整模板复现
锁 ② 🔴反向:关键词身份不变(每篇标题仍含购买关键词锚)
锁 ③ 只重生成冲突项(非冲突项内容**逐字**不变)
锁 ④ provider 失败路径也不产生同结构标题

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[标题 AI-only 2026-08-17 · 旧锁 → 新断言映射]

Owner 裁决把硬编码兜底模板整体退役。本文件有 4 条锁的**被测对象是模板池本身**,
它们不是"改断言"能救的:

| 旧锁 | 语义 | 去向 |
|---|---|---|
| ④b picker 不是纯 modulo | 模板选择器按身份散列 | **退役**(选择器已删) |
| ④c picker 跳过已用公式 | 同上 | **退役** |
| ④d picker 跨进程稳定 | `hashlib` 不用内置 hash() | **退役**;`_stable_seed` 随选择器一起删 |
| complete_template_matcher | 完整模板匹配口径 | **搬家** → 签名源改成
  `tests/fixtures/retired_title_templates_2026_08_17.py`(退役模板全集),
  判据变成「运行时产出零命中退役签名」,见 test_title_ai_only_2026_08_17.py §3.1 |

没退役的:①/②/③ 三条打的是**去重行为**,与标题从哪来无关 —— 逐条保留,
只把"造冲突输入"的手段从"调兜底模板"换成"直接写两条一样的 AI 标题"。
④ 的语义(provider 失败路径不产同结构标题)现在由「provider 失败 = 显式失败,
一条标题都不产」承担,断言随之改写。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

变异:去掉批次去重→①红;去重时改写关键词→②红
"""
import asyncio
import re

import pytest

from writing.keyword_topic_generator import KeywordTopicGenerator
from writing.title_batch_dedupe import (
    detect_title_conflicts,
    dedupe_topic_titles,
    normalize_exact,
    title_formula_skeleton,
)
from writing.title_keyword_alignment import (
    assess_title_keyword_alignment,
    title_anchor_from_purchased_keyword,
)
from tests.fixtures.retired_title_templates_2026_08_17 import (
    template_signature_hits,
)

_YEAR = 2026


def _matches_complete_template(title: str) -> bool:
    """[搬家] 判据从"渲染主表模板正则"换成"命中退役模板签名"。

    模板表已不在运行时,签名源搬进 tests/fixtures/。语义不变:
    产出里出现退役模板的特征串 = 模板复现。
    """
    return bool(template_signature_hits(title))


def _topic(idx, keyword, style, title, slot=0):
    return {
        "keyword_id": idx, "original_keyword": keyword, "article_style": style,
        "optimized_title": title, "slot_index": slot, "angle": f"角度{slot + 1}",
    }


# ===========================================================================
# 锁 ① 同批 6 个必然撞公式的输入 → 两两不同且无完整模板复现
# ===========================================================================
def _colliding_batch():
    """6 个**同关键词同文体**的槽位,标题逐字相同 —— 必然全冲突。

    [标题 AI-only 2026-08-17] 原实现调 `_safe_fallback_title` 造这个冲突;
    模板退役后直接写一条 AI 形态的标题复制 6 份,冲突强度完全一样。
    """
    style = "选购与多品牌比较"
    kw = "深圳装修公司"
    base = "深圳装修公司怎么挑？先看这三份可查的资料"
    return [_topic(i, kw, style, base, slot=i) for i in range(6)]


def test_lock1_six_colliding_inputs_end_up_pairwise_distinct_with_regeneration():
    """工单锁 ①:同批 6 个必然撞模板的输入 → 保存后**两两不同且无完整模板复现**。

    🔴 前提说清楚:每个文体族的兜底模板池**只有 3 条**。6 条同族标题要做到
    "两两不同 **且** 不复现任何一条完整模板",**结构化兜底在数学上做不到** ——
    必须靠重生成(生产里就是 `_regenerate_conflicting_titles` 那一次 LLM 调用)。
    所以本锁走"重生成可用"的正常生产路径;无重生成的降级路径由下一条锁单独钉,
    两条分开才不会用一条锁掩盖另一种状态。
    """
    topics = _colliding_batch()
    # 前置自证:不去重的话它们**确实**全撞(否则本锁在锁空气)
    assert len({normalize_exact(t["optimized_title"]) for t in topics}) == 1

    used_counter = {"n": 0}

    def _regenerate(topic, used_titles):
        """替身重生成:保住购买关键词,换一个**不是模板**的结构。"""
        used_counter["n"] += 1
        kw = topic["original_keyword"]
        variants = [
            f"{kw}要看哪些资质？逐项核对清单",
            f"报价差一倍时，{kw}该怎么比",
            f"签约前，{kw}有哪些坑要避开",
            f"{kw}的服务范围到底包含什么",
            f"验收{kw}时最容易被忽略的环节",
            f"预算有限，{kw}怎么排优先级",
        ]
        return [variants[(used_counter["n"] - 1) % len(variants)]]

    report = dedupe_topic_titles(topics, regenerate=_regenerate)
    titles = [t["optimized_title"] for t in topics]

    assert len({normalize_exact(x) for x in titles}) == len(titles), "两两必须不同"
    assert not any(_matches_complete_template(x) for x in titles[1:]), (
        "冲突项不得再复现完整兜底模板"
    )
    assert report["unresolved"] == 0
    # 红线仍在
    for t in topics:
        assert assess_title_keyword_alignment(
            t["optimized_title"], t["original_keyword"]).aligned


def test_lock1_degraded_without_regeneration_reports_honestly():
    """降级路径(AI 候选不可用):6 条逐字相同的标题一条都分化不出来。

    🔴 此时**必须如实上报 unresolved**,不许静默假装去重成功 —— 本仓的
    "no silent caps"口径。

    [标题 AI-only 2026-08-17] 断言从"能吃满 3 条模板"改成"一条都换不掉":
    模板池退役后,没有 AI 候选就**没有任何替换来源**,这正是裁决要的形态 ——
    宁可留一个重复的 AI 标题,也不换成十个客户共用的模板串。
    """
    topics = _colliding_batch()
    report = dedupe_topic_titles(topics)          # 无 regenerate、无候选

    assert report["conflicts"] == 5               # 首条不算冲突
    assert report["regenerated"] == 0, "没有 AI 候选却换了标题 = 一定是模板复活"
    assert report["unresolved"] == 5, report
    unresolved = [t for t in topics if t.get("title_dedupe_status") == "unresolved"]
    assert len(unresolved) == report["unresolved"]
    # 产出里零模板签名
    assert not [t for t in topics if _matches_complete_template(t["optimized_title"])]


def test_lock1b_exact_duplicates_are_always_resolved():
    """完全重复是**必须**修掉的一类 —— 只要 AI 给得出候选。

    [标题 AI-only 2026-08-17] 原断言"完全重复必被消除"隐含了一个前提:
    模板池永远兜得住。模板退役后前提不成立,所以这条锁拆成成对两半:
      · 有 AI 候选 → 必须真的换掉(能力还在);
      · 没有 AI 候选 → 必须如实记 unresolved,**不许拿模板顶**(裁决要的形态)。
    """
    kw = "深圳装修公司"
    same = "深圳装修公司怎么选？多品牌同字段比较方法"
    topics = [_topic(i, kw, "选购与多品牌比较", same, slot=i) for i in range(3)]
    report = dedupe_topic_titles(topics, candidates_by_index={
        1: ["深圳装修公司报价怎么比？逐项对齐口径再谈价"],
        2: ["深圳装修公司验收要看什么？分区清单与常见争议"],
    })
    titles = [normalize_exact(t["optimized_title"]) for t in topics]
    assert len(set(titles)) == len(titles), "有 AI 候选时完全重复必须被消除"
    assert report["regenerated"] == 2, report

    # 成对反向:没有候选时不许静默"修好"
    topics2 = [_topic(i, kw, "选购与多品牌比较", same, slot=i) for i in range(3)]
    report2 = dedupe_topic_titles(topics2)
    assert report2["regenerated"] == 0, "没有 AI 候选却换了标题 = 模板复活"
    assert report2["unresolved"] == 2, report2


def test_lock1c_existing_unpublished_titles_are_considered():
    """工单:必须同时检查同 quote/project **已存在的未发布标题**。
    只跟本批比 = 第二批原样复现第一批。"""
    kw = "深圳装修公司"
    existing = ["深圳装修公司怎么选？多品牌同字段比较方法"]
    topics = [_topic(0, kw, "选购与多品牌比较", existing[0])]
    report = dedupe_topic_titles(
        topics, existing_titles=existing,
        # [标题 AI-only 2026-08-17] 替换来源只能是 AI 候选(模板池已退役)。
        candidates_by_index={0: ["深圳装修公司报价怎么比？逐项对齐口径再谈价"]},
    )
    assert report["conflicts"] == 1
    assert normalize_exact(topics[0]["optimized_title"]) != normalize_exact(existing[0])

    # 成对反向:探测口径确实把"已存在标题"算进去了 —— 不传 existing 就不该有冲突
    topics2 = [_topic(0, kw, "选购与多品牌比较", existing[0])]
    assert dedupe_topic_titles(topics2)["conflicts"] == 0


# ===========================================================================
# 锁 ② 🔴 反向:关键词身份不变
# ===========================================================================
def test_lock2_purchased_keyword_identity_survives_dedupe():
    """去重后每篇标题仍必须扣住客户购买的关键词 —— 红线。

    [标题 AI-only 2026-08-17] 冲突输入原本由 `_safe_fallback_title` 造;
    模板退役后改成直接写一条 AI 形态标题复制两份,冲突强度一样,
    红线判据(对齐器 + 锚点在场)一个字未动。
    """
    cases = [
        ("深圳载货电梯", "选购与多品牌比较",
         "深圳载货电梯怎么挑？先看载重、井道和维保三项",
         "深圳载货电梯报价怎么算？三块成本拆开看"),
        ("揭阳120平三房买哪里好", "证据型问答",
         "揭阳120平三房买哪里好？先按通勤和配套两条线筛",
         "揭阳120平三房怎么比？把户型和单价放一张表"),
        ("南山区全屋定制公司", "选购与多品牌比较",
         "南山区全屋定制公司怎么挑？看板材、工期和售后",
         "南山区全屋定制公司报价差在哪？逐项对齐再谈价"),
        ("东莞市欧雅家家具有限公司", "企业事实与品牌说明",
         "东莞市欧雅家家具有限公司做什么？能力边界一次讲清",
         "东莞市欧雅家家具有限公司公开信息怎么查？三个入口"),
    ]
    topics = []
    candidates = {}
    for i, (kw, style, title, alt) in enumerate(cases):
        base = len(topics)
        topics += [_topic(i, kw, style, title, slot=0),
                   _topic(i, kw, style, title, slot=1)]   # 制造冲突
        candidates[base + 1] = [alt]
    dedupe_topic_titles(topics, candidates_by_index=candidates)
    for t in topics:
        kw = t["original_keyword"]
        title = t["optimized_title"]
        assert assess_title_keyword_alignment(title, kw).aligned, (
            f"关键词身份被改坏: {kw!r} -> {title!r}"
        )
        anchor = title_anchor_from_purchased_keyword(kw)
        assert anchor and anchor in title, f"购买关键词锚 {anchor!r} 必须仍在标题里"
    # 产出零模板签名
    assert not [t for t in topics if _matches_complete_template(t["optimized_title"])]


def test_lock2b_identity_breaking_candidate_is_rejected_even_if_it_dedupes():
    """🔴 反向的反向:候选就算完美去重,只要改坏了关键词身份也必须被否决,
    宁可留一个重复标题。"""
    kw = "深圳载货电梯"
    same = "深圳载货电梯怎么选？多品牌同字段比较方法"
    topics = [_topic(0, kw, "选购与多品牌比较", same, slot=0),
              _topic(1, kw, "选购与多品牌比较", same, slot=1)]
    report = dedupe_topic_titles(
        topics,
        # 这个"重生成"把客户买的词换成了别的行业词 —— 必须被丢弃
        candidates_by_index={1: ["广州客梯怎么挑选？服务商横向对比"]},
    )
    assert report["identity_rejected"] >= 1, "改坏关键词身份的候选必须被记为否决"
    assert "客梯" not in topics[1]["optimized_title"]
    assert assess_title_keyword_alignment(topics[1]["optimized_title"], kw).aligned


# ===========================================================================
# 锁 ③ 只重生成冲突项(非冲突项逐字不变)
# ===========================================================================
def test_lock3_only_conflicting_items_are_touched():
    topics = [
        _topic(0, "深圳装修公司", "选购与多品牌比较", "深圳装修公司怎么选？多品牌同字段比较方法"),
        _topic(1, "广州家政服务", "方法与实施指南", "广州家政服务如何落地？实施步骤与验收指南"),
        _topic(2, "东莞仓储托管", "趋势、政策与风险分析", "东莞仓储托管趋势如何判断？政策与风险分析"),
        # 第 4 条与第 1 条完全重复 → 只有它该被改
        _topic(3, "深圳装修公司", "选购与多品牌比较", "深圳装修公司怎么选？多品牌同字段比较方法", slot=1),
    ]
    before = [t["optimized_title"] for t in topics]
    report = dedupe_topic_titles(
        topics,
        # [标题 AI-only 2026-08-17] 替换来源只能是 AI 候选。
        candidates_by_index={3: ["深圳装修公司报价怎么比？逐项对齐口径再谈价"]},
    )

    assert topics[0]["optimized_title"] == before[0]
    assert topics[1]["optimized_title"] == before[1]
    assert topics[2]["optimized_title"] == before[2]
    assert topics[3]["optimized_title"] != before[3]
    assert report["changed_topic_indexes"] == [3], "只有冲突项可以被改"
    # 非冲突项连状态字段都不该被加(逐字不变)
    for t in topics[:3]:
        assert "title_dedupe_status" not in t


def test_lock3b_detect_is_side_effect_free():
    """探测阶段不得改任何东西(否则"只重生成冲突项"无从谈起)。"""
    topics = [
        _topic(0, "深圳装修公司", "选购与多品牌比较", "深圳装修公司怎么选？多品牌同字段比较方法"),
        _topic(1, "深圳装修公司", "选购与多品牌比较", "深圳装修公司怎么选？多品牌同字段比较方法", slot=1),
    ]
    snapshot = [dict(t) for t in topics]
    conflicts = detect_title_conflicts(topics)
    assert [c["index"] for c in conflicts] == [1]
    assert topics == snapshot, "探测必须无副作用"


# ===========================================================================
# 锁 ④ provider 失败路径也不产生同结构标题
# ===========================================================================
@pytest.fixture()
def no_llm(monkeypatch):
    """🔴 强制走 provider 失败路径,且**不打真实 LLM**。

    第一版没有这个 fixture:本机 `.env` 里有真 key,测试直接**发了一次真实 LLM 请求**
    —— 既花钱、又让断言随模型输出漂移(那一跑 conflicts 恰好是 0,断言红得毫无意义)。
    这里把主链与兜底链的 key 都清空:`generate()` 走 `if not api_key` 分支进
    `_fallback_generate()`,`_regenerate_conflicting_titles` 因无 key 直接返回空。
    """
    import writing.llm_utils as llm_utils

    monkeypatch.setattr(llm_utils, "get_llm_config",
                        lambda *a, **k: ("", "", "", ""), raising=False)
    monkeypatch.setattr(llm_utils, "get_fallback_llm_config",
                        lambda *a, **k: ("", "", "", ""), raising=False)
    return True


def test_lock4_provider_failure_produces_no_title_at_all(no_llm):
    """[改写] provider 全失败 → **一条标题都不产**,而不是产出同结构模板标题。

    原锁 ④ 断言的是"兜底路径产出的标题必须两两不同";Owner 2026-08-17 裁决后
    兜底路径不再产标题 —— 断言随之变成"零产出 + 显式失败记账"。
    这比原锁更强:同结构不可能出现,因为根本没有非 AI 产出。
    """
    keywords = [
        {"id": i, "keyword": kw, "required_articles": 2}
        for i, kw in enumerate([
            "深圳装修公司", "广州装修公司", "东莞装修公司",
            "佛山装修公司", "珠海装修公司", "中山装修公司",
        ])
    ]
    gen = KeywordTopicGenerator(
        keywords=keywords, brand_name="测试品牌", industry="建材家居",
        force_chinese_style="选购与多品牌比较",
    )
    gen._existing_quote_titles = lambda: []          # 无 DB
    topics = asyncio.run(gen.generate())             # 无 API KEY → AI 失败梯全灭

    assert topics == [], f"AI 全灭却仍产出标题:{topics[:2]}"
    report = gen.title_failure_report
    assert report is not None, "失败必须记账,不许静默返回空"
    assert report["requested"] == 12
    assert report["produced"] == 0
    assert report["failed"] == 12
    # 去重 hook 仍接在链上(空输入也要有报告,摘掉即 None)
    assert gen.title_dedupe_report is not None, (
        "generate() 必须把批次去重接进链路(hook 不得被摘)"
    )


def test_lock4e_dedupe_never_flips_title_form_when_form_is_locked():
    """🔴 回归 A/B 逼出来的真 bug 的定锁 —— **锁在 P4 真正拥有的那一层**。

    用户显式指定 `title_form` 时形态收敛层会早退不介入(用户 > 默认)。我第一版的
    去重在此时把陈述式标题换成了 `…怎么选？` 这类**问句式**兜底模板,等于绕过形态层
    改掉用户的显式选择。修法 = `preserve_form=bool(self.title_form)`。

    ✅ [钩子已闭合 · 2026-08-01] P4 交付时这里留了一条说明:"刻意不走端到端
    `generate()` 断言全批没有问句,因为那会连带断言一个**上线前就存在**的缺陷
    —— 兜底模板集本身不看 `title_form`(基线 eac2200b 上 open 跑兜底 6/6 全问句)"。
    该缺陷已由《兜底模板尊重 title_form 2026-08-01》包修复,端到端断言随之补上:
    见 `tests/test_fallback_title_form_2026_08_01.py` 的
    `test_lock1_fallback_chain_open_produces_zero_questions`(兜底链)与
    `test_lock1b_main_chain_open_produces_zero_questions`(主链)。
    本条**仍只钉去重层自己的契约**(开了 preserve_form 就一条都不许翻形态),
    两层分开锁,不互相掩盖。
    """
    from writing.title_question_policy import is_question_title

    kw = "深圳装修公司"
    open_form = "深圳装修公司选型指南｜2026年证据字段与风险检查"   # 陈述式
    assert not is_question_title(open_form)
    topics = [_topic(i, kw, "选购与多品牌比较", open_form, slot=i) for i in range(3)]

    report = dedupe_topic_titles(topics, preserve_form=True)
    assert report["conflicts"] == 2
    for t in topics:
        assert not is_question_title(t["optimized_title"]), (
            f"preserve_form=True 下不得翻成问句式: {t['optimized_title']}"
        )

    # [2026-08-01 订正] 这里原本断 `report["form_rejected"] > 0`,用"否决计数器"
    # 当作"形态守卫有牙"的证据。《兜底模板尊重 title_form》包让 picker **直接按
    # 形态出候选**(不再是先出错形态、再被否决),于是该计数器正常为 0 —— 行为是
    # **变好了**,旧断言却红。断"机制被走过"而不是断"性质成立",就是这种脆法。
    # 改成**直接喂一个错形态候选**:守卫必须当场否决,这比计数器更硬,
    # 且与候选从哪来无关。
    probe = [_topic(i, kw, "选购与多品牌比较", open_form, slot=i) for i in range(2)]
    probe_report = dedupe_topic_titles(
        probe, preserve_form=True,
        candidates_by_index={1: ["深圳装修公司怎么选？2026年多品牌同字段比较方法"]},
    )
    assert probe_report["form_rejected"] >= 1, "喂进错形态候选时守卫必须否决"
    assert not is_question_title(probe[1]["optimized_title"])

    # 反向:关掉保护时,去重可以自由换结构(常规链路靠形态层最后统一收敛)
    # [标题 AI-only 2026-08-17] 原断言是"关掉形态保护后候选面更大,应能多修几条"——
    # 那个"更大的候选面"来自模板池。池子退役后,关不关形态保护都没有额外来源,
    # 两边都只能是 0。这条改成打**形态保护本身的开关语义**:
    # 关掉后同一个问句式候选不再被 form_rejected 挡掉,而是被真正采纳。
    topics2 = [_topic(i, kw, "选购与多品牌比较", open_form, slot=i) for i in range(2)]
    report2 = dedupe_topic_titles(
        topics2, preserve_form=False,
        candidates_by_index={1: ["深圳装修公司怎么选？多品牌同字段比较方法"]},
    )
    assert report2["form_rejected"] == 0, "关掉形态保护后不该再有形态否决"
    assert report2["regenerated"] == 1, (
        f"关掉形态保护后同一个候选必须被采纳,实际报告={report2}"
    )


# ===========================================================================
# 锁 ⑤ · 🔴 **主链**接线(复审 P4 裁定①:我原来的接线锁只盖了兜底链)
# ===========================================================================
class _FakeResponse:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): return None
    def json(self): return self._payload


class _FakeAsyncClient:
    """只替最外层 HTTP 端点,链路其余部分全真跑(与
    tests/test_title_question_and_length_2026_07_29.py 同款口径)。"""
    def __init__(self, payload): self._payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def post(self, *a, **k): return _FakeResponse(self._payload)


def _same_formula_llm_payload(count: int, keyword: str = "GEO服务商") -> dict:
    """替身返回**同一公式 × N**(只有尾号不同)—— 正是工单 §7 要消灭的形态。"""
    import json as _json
    return {"choices": [{"message": {"content": _json.dumps({"topics": [
        {
            "keyword_id": 1, "slot_index": i, "original_keyword": keyword,
            "optimized_title": f"2026年十大{keyword}权威盘点{i}",
            "article_style": "选购与多品牌比较", "angle": f"角度{i}",
        }
        for i in range(count)
    ]}, ensure_ascii=False)}}]}


def test_lock5_main_chain_hook_is_wired(monkeypatch):
    """🔴 [复审 P4 裁定① · 返修补锁] 走**有 key 的主链**(`generate()` 里
    `_generate_batch` 成功那条),不是无 key 兜底链。

    为什么必须单独一条:`_apply_batch_title_dedupe` 有**两个**调用点 ——
    :425(兜底链)与 :447(主链)。我原来的接线锁走无 key 全兜底,只钉住 :425;
    而**生产 .env 有 key,`generate()` 正常走的恰是 :447**。复审自建变异实证:
    单摘 :447 → 44 用例照绿存活 = 主链被重构绕开时锁全绿、生产去重静默失效。
    这里用同公式替身跑主链,断 dedupe 报告真的产生了 —— 摘 :447 即红。
    """
    import writing.keyword_topic_generator as ktg
    import writing.llm_utils as llm_utils

    payload = _same_formula_llm_payload(8)
    monkeypatch.setattr(ktg.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(payload))
    monkeypatch.setattr(llm_utils, "get_llm_config",
                        lambda *a, **k: ("http://stub", "key", "model", "prov"), raising=False)
    monkeypatch.setattr(llm_utils, "get_fallback_llm_config",
                        lambda *a, **k: ("http://stub", "key", "model", "prov"), raising=False)

    gen = KeywordTopicGenerator(
        [{"id": 1, "keyword": "GEO服务商", "required_articles": 8}],
        "QZQZ", "全屋定制",
    )
    gen._existing_quote_titles = lambda: []
    topics = asyncio.run(gen.generate())

    assert len(topics) == 8
    report = gen.title_dedupe_report
    assert report is not None, (
        "主链(:447)必须接批次去重 —— 报告为 None 说明 hook 被绕开"
    )
    assert "error" not in report, f"去重异常: {report.get('error')}"
    assert report["total"] == 8
    # 替身给的是同一公式 × 8,主链走到这里必然探测到冲突;conflicts==0 = hook 是空壳
    assert report["conflicts"] > 0, (
        f"同公式×8 经主链必须探测到冲突,实际报告={report}"
    )
    assert report["regenerated"] + report["unresolved"] == report["conflicts"]
    # 红线仍在
    for t in topics:
        assert assess_title_keyword_alignment(
            t["optimized_title"], t["original_keyword"]).aligned


# ===========================================================================
# [退役记录 2026-08-17] ④b / ④c / ④d 三条 picker 锁
#
# 被锁对象 `pick_diverse_template` / `_stable_seed` 随硬编码模板表一起退役
# (它们的唯一数据源就是那张表)。这里留一条**接线锁**代替:
# 谁把它们接回来,这条就红。
# ===========================================================================
def test_retired_picker_symbols_stay_retired():
    import writing.title_batch_dedupe as dedupe
    import writing.keyword_topic_generator as ktg

    for name in ("pick_diverse_template", "filter_templates_by_form",
                 "template_is_question", "_stable_seed"):
        assert not hasattr(dedupe, name), f"退役的模板选择器又回来了:{name}"
    for name in ("_fallback_style_title_map", "_safe_fallback_title"):
        assert not hasattr(ktg, name), f"退役的模板表又回来了:{name}"
    # 成对反向:同一检查对还活着的符号必须为真(防恒真)
    assert hasattr(dedupe, "dedupe_topic_titles")


# ===========================================================================
# 补充:完整模板匹配口径(避免用片段匹配把定级/锁判夸大)
# ===========================================================================
def test_complete_template_matcher_does_not_use_fragment_matching():
    """[搬家] 判据源从运行时主表改成 tests/fixtures 的退役签名全集。

    语义未变:锁里凡是判"有没有复现模板",必须能抓到真模板、
    又不能把正常 AI 标题误判。
    """
    sample = "广州装修公司选购指南｜2026年少走弯路的挑法"   # 退役主表的第 3 条
    assert _matches_complete_template(sample), "抓不到真模板 → 判据恒真"
    # 正常 LLM 标题不该被误判(否则判据恒红,同样没判别力)
    assert not _matches_complete_template("深圳装修公司怎么挑？先看这三份可查的资料")
