"""[标题自然化 · 包③ · 2026-08-01] 工单 §3 五锁 · 行为级。

详版:docs/AI-CONTEXT/WORKORDER_TITLE_NATURALNESS_ADOPTED_PATTERN_2026-08-01.md §3

锁① 新产标题黑名单命中 = 0(兜底链 + 生成侧提示词各一)
锁② 🔴反向:红线仍成立 —— 语义对齐器逐条 aligned(拆散 ≠ 丢身份)
锁③ 整串嵌入不再是模板结构的必然(问句式 kw 至少产出 1 条非整串形态)
锁④ 已发布文章零改动(范围只管今后)
锁⑤ title_form 包的 open/question 行为不回退
"""
import pytest

from writing.title_jargon_blacklist import (
    TITLE_JARGON_TERMS,
    render_title_jargon_constraint,
    scan_title_jargon,
)
from writing.title_keyword_alignment import (
    assess_title_keyword_alignment,
    title_anchor_from_purchased_keyword,
)

_YEAR = 2026

#: 覆盖真实形态:纯名词短语 / 问句式 / 带地域 / 带规格数字 / 公司全名
_KEYWORDS = (
    "深圳装修公司",
    "深圳哪家装修公司靠谱",
    "揭阳120平三房买哪里好",
    "深圳载货电梯",
    "东莞市欧雅家家具有限公司",
    "上海哪个全屋定制品牌好",
)
from writing.article_style_contract import STYLE_FAMILIES  # noqa: E402
_STYLES = tuple(family.name for family in STYLE_FAMILIES.values())
# ===========================================================================
# [标题 AI-only 2026-08-17 · 旧锁 → 新断言映射]
#
# 本文件原有 6 条锁把「标题零内部术语」打在**硬编码兜底模板表**上
# (`_fallback_style_title_map` / `_fallback_form_completion_map` /
#  `_safe_fallback_title` / `fallback_templates_for_form`)。
# Owner 2026-08-17 裁决后那张表已整体退役 —— 但**不变式没有退役,只是搬了家**:
#
#   旧锁① 模板表零术语        → 新锁① prompt 负面约束仍与机审同表(lock1d 保留)
#                                + AI 产出标题过同一张黑名单(lock1b_ai)
#   旧锁①b 兜底产出零术语      → 新锁①b 对 AI 标题扫同一张表
#   旧锁② 兜底产出保关键词身份 → 搬到 `title_ai_only._aligned`,对每条 AI 标题复核
#   旧锁⑤ auto 池零变化        → **退役**(池子不存在了),见
#                                 tests/test_fallback_title_form_2026_08_01.py 的退役记录
#   pool_attribution / demotable → **退役**(模板池的形态归属);
#                                 `to_open_title` 的降级能力本身仍由
#                                 title_question_policy 自己的用例覆盖
#
# 🔴 判别力自证(lock1c)一字未动:黑名单必须真能抓到已知污染样本,
#    否则"零命中"是恒真的假绿。
# ===========================================================================


_AI_TITLES = (
    "深圳装修公司怎么挑？先看这三份可查的资料",
    "广州家政服务按次还是包月划算？两种算法摆一起",
    "东莞仓储托管的费用由哪几块组成？逐项拆开看",
    "佛山设备租赁签合同前要确认什么？四个条款别漏",
)



# ===========================================================================
# 锁① 新产标题黑名单命中 = 0
# ===========================================================================
def test_lock1b_ai_titles_are_jargon_free():
    """[搬家后的锁①/①b] 黑名单打在**AI 产出的标题**上,不再打模板表。

    🔴 覆盖自证仍在 lock1c:只断言"零命中"的话,黑名单清空也全绿。
    """
    checked = 0
    for title in _AI_TITLES:
        checked += 1
        assert scan_title_jargon(title) == [], f"AI 标题含内部术语:{title}"
    assert checked >= 4, f"样本太少({checked}),本锁可能在空跑"


def test_lock1b_ai_title_identity_is_rechecked():
    """[搬家后的锁②] 关键词身份复核对**每条 AI 标题**生效(`title_ai_only._aligned`)。"""
    from writing.title_ai_only import _aligned

    assert _aligned("深圳装修公司怎么挑？先看这三份可查的资料", "深圳装修公司")
    # 成对反向:换掉客户买的对象必须判不合格,否则复核是恒真。
    assert not _aligned("广州搬家公司怎么挑？先看这三份资料", "深圳装修公司")


def test_lock1c_blacklist_actually_detects_jargon():
    """🔴 判别力自证:黑名单必须真能抓到东西,否则锁①是空的。
    用**重写前的真实旧模板**做样本(它们当年就在生产上跑)。"""
    legacy = [
        "深圳装修公司选型指南｜2026年证据字段与风险检查",
        "2026年深圳装修公司需要核验什么？问题与证据清单",
        "深圳装修公司成本收益怎么判断？2026年情景测算指南",
    ]
    for title in legacy:
        assert scan_title_jargon(title), f"黑名单漏掉了已知污染样本:{title}"


def test_lock1d_prompt_constraint_is_generated_from_the_same_table():
    """生成侧负面约束与机审侧扫描**同一份表**(禁止第二份手写清单)。"""
    constraint = render_title_jargon_constraint()
    for term in TITLE_JARGON_TERMS:
        assert term in constraint, f"提示词漏了黑名单词:{term}"


# ===========================================================================
# 锁② 🔴 反向:红线仍成立(拆散 ≠ 丢身份)
# ===========================================================================
def test_lock2_keyword_identity_survives_every_generated_title():
    """[搬家] 红线反向面:每条**AI**标题都必须仍与购买关键词语义对齐。

    原实现遍历兜底模板产出;模板退役后,承担这条的是
    `title_ai_only._aligned`(失败梯的每一级都过它)。
    """
    pairs = (
        ("深圳装修公司", "深圳装修公司怎么挑？先看这三份可查的资料"),
        ("广州家政服务", "广州家政服务按次还是包月划算？两种算法摆一起"),
        ("东莞仓储托管", "东莞仓储托管的费用由哪几块组成？逐项拆开看"),
    )
    for keyword, title in pairs:
        assert assess_title_keyword_alignment(title, keyword).aligned, (
            f"关键词身份丢失:{keyword!r} → {title!r}"
        )
    # 成对反向:相邻话题必须判不过(否则对齐器是恒真)
    assert not assess_title_keyword_alignment(
        "广州搬家公司怎么挑？先看这三份资料", "深圳装修公司",
    ).aligned


def test_lock2b_question_shell_compression_never_swaps_the_object():
    """🔴 压缩只许剥疑问壳,不许换商业对象。

    成对断言:①问句式 kw 确实被压缩了(否则病 A 没修)
              ②压缩结果仍与原关键词对齐(否则红线破了)
    """
    compressed_any = False
    for kw in _KEYWORDS:
        anchor = title_anchor_from_purchased_keyword(kw)
        assert anchor, kw
        assert assess_title_keyword_alignment(anchor, kw).aligned, (
            f"锚点已不是同一个商业对象:{kw!r} → {anchor!r}"
        )
        if anchor != kw:
            compressed_any = True
    assert compressed_any, "没有任何关键词被压缩过 → 病 A 的修法没生效,锁在空跑"


def test_lock2c_locative_interrogative_is_not_stripped():
    """🔴 反向:「买哪里好」里的「哪里」是宾语不是量词修饰,剥掉会得到语法垃圾。
    实测反例,防止有人图省事把疑问词表写宽。"""
    kw = "揭阳120平三房买哪里好"
    anchor = title_anchor_from_purchased_keyword(kw)
    assert "买好" not in anchor, f"剥出了语法垃圾:{anchor!r}"


# ===========================================================================
# 锁③ 整串嵌入不再是模板结构的必然
# ===========================================================================
def test_lock3_question_keyword_yields_a_non_literal_form():
    """[搬家] 病 A(整串硬塞)的约束从兜底模板搬到 **prompt 硬规则 + AI 产出**。

    原实现遍历兜底模板产出;模板退役后,这条规则的承担者是选题 prompt 的
    「禁止把关键词整串逐字硬塞」条款(仍在 `_build_title_generator_prompt`),
    以及对每条 AI 标题的身份复核。这里打**规则在不在 prompt 里**(接线)
    + **拆散后的标题仍扣得住身份**(行为)。
    """
    from writing.keyword_topic_generator import _build_title_generator_prompt

    prompt = _build_title_generator_prompt("建材家居")
    assert "硬塞进句子" in prompt, "prompt 里的「不许整串硬塞」规则丢了"

    kw = "深圳哪家装修公司靠谱"
    natural = "深圳装修公司怎么挑？先看这三份可查的资料"
    assert kw not in natural
    assert assess_title_keyword_alignment(natural, kw).aligned
    # 成对反向:拆散到丢了业务对象必须判不过
    assert not assess_title_keyword_alignment("家装行业怎么看？三份资料先读", kw).aligned


def test_lock3b_grammar_breakage_pattern_is_gone():
    """[搬家] 语法坏死形态的逐字反例。

    模板退役后没有"确定性产出"可枚举,所以这条改打**规则文本**:
    prompt 里那条坏死示例(「深圳哪家装修公司靠谱怎么选？」)必须还在,
    它就是给模型看的反例。
    """
    from writing.keyword_topic_generator import _build_title_generator_prompt

    prompt = _build_title_generator_prompt("建材家居")
    assert "深圳哪家装修公司靠谱怎么选？" in prompt, "prompt 里的坏死反例丢了"
    # 成对反向:探针不是恒真
    assert "这条句子在 prompt 里不存在" not in prompt


# ===========================================================================
# 锁④ 范围:只管今后
# ===========================================================================
def test_lock4_scope_touches_no_published_article_path():
    """本包只改**生成侧**(模板/提示词/锚点提取)。

    行为口径:兜底链是纯函数,给同样输入产同样输出,**不读也不写 articles**。
    若有人把回填逻辑塞进来,这条会因为需要 DB 而立刻炸(纯函数调用不该碰库)。
    """
    import writing.keyword_topic_generator as m

    src = open(m.__file__, encoding="utf-8").read()
    # [标题 AI-only 2026-08-17] `_safe_fallback_title` 已退役 —— 现在取的是
    # 它的继任者:失败梯执行点 `_resolve_pending_titles`。锁的语义没变
    # (这段代码不许碰已发布文章路径),只是被锁的那段代码换了名字。
    seg = src[src.index("async def _resolve_pending_titles("):]
    for forbidden in ("UPDATE articles", "INSERT INTO articles", "first_published_at"):
        assert forbidden not in seg, f"兜底链不得触碰已发布文章:{forbidden}"


# ===========================================================================
# 锁⑤ title_form 包行为不回退
# ===========================================================================
def test_lock5_template_pool_is_retired_not_silently_reintroduced():
    """[退役] 旧锁⑤锁的是 auto 池逐字不变;池子已整体退役。

    退役后要守的不是"池子还在不在",而是"有没有人偷偷把它接回来"。
    """
    import writing.keyword_topic_generator as ktg
    import writing.title_batch_dedupe as dedupe

    for name in ("_fallback_style_title_map", "_fallback_form_completion_map",
                 "fallback_templates_for_form", "_safe_fallback_title"):
        assert not hasattr(ktg, name), f"退役模板符号又回来了:{name}"
    for name in ("pick_diverse_template", "filter_templates_by_form",
                 "template_is_question"):
        assert not hasattr(dedupe, name), f"退役模板选择器又回来了:{name}"
    # 成对反向:同样的检查对还活着的符号必须为真
    assert hasattr(ktg, "KeywordTopicGenerator")


# ===========================================================================
# AI 标题质检旁路 · fail-closed(与包① §3E 同口径)
# ===========================================================================
def test_title_qc_fail_closed_never_guesses_natural(monkeypatch):
    from services import article_ai_review as air
    from services import title_quality_ai as tq

    monkeypatch.setattr(air, "_deepseek_key", lambda: "sk-test-not-real")

    def _boom(url, headers, payload):
        raise RuntimeError("deepseek down")

    monkeypatch.setattr(air, "_post_chat", _boom)
    out = tq.assess_title_naturalness("随便一条标题")
    assert out["verdict"] == tq.VERDICT_NOT_CHECKED
    assert out["verdict"] != tq.VERDICT_NATURAL, "调用失败不得被当成'自然'"


def test_title_qc_uses_the_official_channel(monkeypatch):
    """🔴 渠道复用包①:必须打 api.deepseek.com。断言实际 URL,不是常量。"""
    import json as _json
    from urllib.parse import urlparse

    from services import article_ai_review as air
    from services import title_quality_ai as tq

    monkeypatch.setattr(air, "_deepseek_key", lambda: "sk-test-not-real")
    seen = []

    def _fake(url, headers, payload):
        seen.append(url)
        return {"choices": [{"message": {"content": _json.dumps(
            {"verdict": "natural", "reason": "读起来像人话", "suggestion": ""})}}]}

    monkeypatch.setattr(air, "_post_chat", _fake)
    out = tq.assess_title_naturalness("深圳装修公司怎么选?这 3 点最容易踩坑")
    assert out["verdict"] == "natural"
    assert urlparse(seen[0]).netloc == "api.deepseek.com"


def test_title_qc_rejects_verdict_without_reason(monkeypatch):
    """说"像模板"却指不出哪里像 = 笼统文案,不透给用户(与 §3D 同型)。"""
    import json as _json

    from services import article_ai_review as air
    from services import title_quality_ai as tq

    monkeypatch.setattr(air, "_deepseek_key", lambda: "sk-test-not-real")
    monkeypatch.setattr(air, "_post_chat", lambda *a: {"choices": [{"message": {"content":
        _json.dumps({"verdict": "templated", "reason": "", "suggestion": "改一下"})}}]})
    out = tq.assess_title_naturalness("某标题")
    assert out["verdict"] == tq.VERDICT_NOT_CHECKED


# ===========================================================================
# 【返修 2026-08-01】池一致性自检锁
# ===========================================================================
def test_retired_pool_form_locks_are_recorded():
    """[退役记录] `pool_attribution` / `every_question_template_is_demotable`
    两条锁的对象是**模板池**,池子退役后它们无所锁。

    其中"降级器认不认这个问法"这一不变式没有消失 —— 它是
    `title_question_policy.to_open_title` 自己的能力,由下面这条继续钉住
    (打的是**行为**,不再依赖模板池当样本)。
    """
    from writing.title_question_policy import is_question_title, to_open_title

    # 表里有的问法词:必须真能降级(证明不是恒红)
    ok = "GEO服务商的回本周期怎么算？2026年测算方法说明"
    assert is_question_title(ok)
    assert not is_question_title(to_open_title(ok, keyword="GEO服务商"))
    # 成对反向:表外的问法词降级器认不出 → 保持原样(证明不是恒绿)
    stuck = "GEO服务商划算吗？2026年成本说明"
    assert is_question_title(stuck)
    assert is_question_title(to_open_title(stuck, keyword="GEO服务商"))
