# -*- coding: utf-8 -*-
"""§C 全链互打清扫 · 成对判据(返修单 v3 §E-4:互打消除后**两侧原意图都仍成立**)。

每条 C 项两向锁:正向 = 互打已消除;反向 = 被让路一侧的原意图没有被顺手废掉。
"""
from __future__ import annotations

import inspect
import re

from writing.article_style_contract import STYLE_FAMILIES  # noqa: F401 (夹具引用)
from writing.client_presence_policy import evaluate_client_presence
from writing.evidence_pack import (
    render_evidence_pack_by_entity,
    render_evidence_pack_for_writer,
)
from writing.gate2_rewrite_measures import build_gate2_measure_block
from writing.templates.canonical_family_templates import (
    build_compact_structure_spec,
    build_deep_ranking_structure_spec,
)
from writing.templates.common_rules import COMMON_GUARDRAILS

BRAND = "观山电梯"


def _codes(body: str) -> list[str]:
    return [f.code for f in evaluate_client_presence("t", body, client_brand=BRAND).findings]


# ---------------------------------------------------------------- C4 · 第一人称
def test_c4_narrative_first_person_passes_disclaimer_still_flagged():
    ok = f"## 实测\n\n从个人体验看，{BRAND}的响应速度更直接，两次报修都在 2 小时内上门。"
    assert "article_hedging_stance" not in _codes(ok), (
        "裸「个人体验」被判示弱 —— 规格卡推荐的第一人称测评形态被自家政策打掉(C4)"
    )
    bad = f"{BRAND}不错。以上纯属个人观点。"
    assert "article_hedging_stance" in _codes(bad), "免责套话漏判(C4 反向)"


# ---------------------------------------------------------------- C5 · 头部作用域
_PACK = {"version": "v1", "items": [
    {"evidence_id": "EV-001", "relationship": "support",
     "verification_status": "search_result", "title": "行业观察",
     "url": "https://e.com/1", "publisher": "中国电梯", "published_at": "2025-03",
     "claim": "交付率", "scope": "", "excerpt": "……"},
    {"evidence_id": "EV-002", "relationship": "support",
     "verification_status": "search_result", "title": "选型清单",
     "url": "https://unknown-site.cn/1", "publisher": "unknown-site.cn",
     "published_at": "2026-01", "claim": "载重", "scope": "", "excerpt": "……"},
]}


def test_c5_header_scopes_citation_permission_by_trust():
    out = render_evidence_pack_for_writer(_PACK)
    # 正向:头部不再对全体条目发"直接引用并保留归属"的通行证
    assert "均为真实检索/提交所得。**标「独立第三方" in out
    assert "可作为正文佐证直接引用；" not in out
    # 反向:两侧原意图都在 —— 独立条目仍给归属句;未确认条目仍禁归属
    assert "归属句照抄：据中国电梯 2025 年 3 月" in out
    assert "不得写成外部归属" in out


# ---------------------------------------------------------------- C6 · 逐家分组同源
def test_c6_by_entity_labels_follow_trust_not_blanket_attribution():
    # [R3-1 2026-08-16] 分组块与 for_writer 同门:实体条目补 entity_confirmed
    # 才进分组(本锁判 trust 分档标注同源,与绑定轴正交;四态另有专锁 test_p0_2)。
    pack = {"version": "v1", "items": [
        {**_PACK["items"][0], "entity": "观山电梯", "binding_state": "entity_confirmed"},
        {**_PACK["items"][1], "entity": "观山电梯", "binding_state": "entity_confirmed"},
    ]}
    out = render_evidence_pack_by_entity(pack, ["观山电梯"])
    assert "EV-001" in out, "分组没匹配到条目,后续断言是空的(元判据)"
    # 正向:不再给未核验条目统一发「引用需保留来源归属」指令
    assert "引用需保留来源归属" not in out
    # 未确认来源条目 → 明示禁归属;具名条目 → 归属句照抄(与 writer 块同源)
    assert "不得写外部归属" in out
    assert "归属句照抄:据中国电梯" in out


# ---------------------------------------------------------------- C7 · 裁决二
def test_c7_disclosure_clause_customer_material_removed_others_kept():
    # [R4 §1b 2026-08-11 翻向] Owner 亲裁 P0-2:「商业关系与实测条件必须披露」
    # 首句整删(商业关系披露=软文自爆,生产 47 篇真自爆实证)。恒向锁:
    # 「商业关系」一词不许再出现在公共护栏任何指令里。
    assert "商业关系与实测条件必须披露" not in COMMON_GUARDRAILS
    assert "商业关系" not in COMMON_GUARDRAILS
    assert "客户材料和实测条件必须披露" not in COMMON_GUARDRAILS
    # 反向(保留侧):[R6 §2 适配] 「只在内部用于校核,不进正文」读作删除授权,
    # 已按 Owner 亲裁改「主张底账 + 不作为来源声明 + 不因此删掉该事实」——
    # 自曝清零侧(不作为来源声明/类型词禁令)与伪造身份禁令仍必须在场。
    assert "客户材料是**主张底账**" in COMMON_GUARDRAILS
    assert "不作为可写进正文的来源声明" in COMMON_GUARDRAILS
    assert "但不因此删掉该事实" in COMMON_GUARDRAILS
    assert "不得伪造具体媒体身份" in COMMON_GUARDRAILS


# ---------------------------------------------------------------- C8/C14 · gate2 让位
def test_c8_c14_gate2_exemptions_only_remove_target_measures():
    full = build_gate2_measure_block(BRAND)
    assert "[G8" in full and "[G9" in full and "[G1" in full
    exempted = build_gate2_measure_block(BRAND, exempt_codes=("G8", "G9"))
    assert "[G8" not in exempted and "[G9" not in exempted
    # 反向:其余措施一条不少(G1-G7 原意图保留)
    for code in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert f"[{code}" in exempted, f"豁免殃及了 {code}"


def test_c8_c14_generator_computes_exemptions():
    import writing.article_generator_service as m

    src = inspect.getsource(m)
    assert "exempt_codes=tuple(_gate2_exempt)" in src, "豁免没接进生成链"
    assert '_gate2_exempt.append("G8")' in src
    assert 'topic.get("_effective_add_contact")' in src and '_gate2_exempt.append("G9")' in src, (
        "G9 豁免没挂在客户联系方式开关上 —— 付费功能仍会被 G9 压死(C14)"
    )


# ---------------------------------------------------------------- C9 · 短文清单块
def test_c9_compact_spec_exempts_client_profile_block():
    spec = build_compact_structure_spec({"length_tier": "compact", "minimum_chars": 900,
                                         "maximum_chars": 1500, "target_chars": 1200})
    if not spec:  # 渲染条件不满足时本条不判(其它锁盯渲染条件)
        return
    assert "不要堆清单块" in spec, "负信号原意图被顺手删了(C9 反向)"
    assert "客户档案区块除外" in spec, "G1 在短文上仍被清单块禁令压死(C9)"


# ---------------------------------------------------------------- C10 · 成卡 vs 少写
def test_c10_deep_spec_and_user_message_no_longer_contradict():
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 14000}, whitelist=["观山电梯", "A", "B", "C", "D"],
    )
    assert spec, "深档规格没渲染出来,后续断言是空的(元判据)"
    assert "必须全部成卡" in spec
    assert "有证据分组的卡**每张至少 2 处" in spec, "每卡≥2 没限定到有证据的卡(C10)"
    import writing.article_generator_service as m

    src = inspect.getsource(m)
    assert "成卡名单内的候选不适用本条" in src, "「宁可少写」没给成卡名单让路(C10)"
    assert "宁可少写" in src, "「宁可少写」原意图被顺手删了(C10 反向)"


# ---------------------------------------------------------------- C11 · 缺格 SSOT
def test_c11_missing_cell_rule_converges_to_column_deletion():
    spec = build_deep_ranking_structure_spec(
        {"target_chars": 14000}, whitelist=["观山电梯", "A", "B"],
    )
    assert spec and "整列删除该字段" in spec
    assert "如实写“—”" not in spec, "「写—」占位口径复活(C11)"
    # 反向:证据卡对等侧(client_presence)与卡片留白侧(competitor_name_contract)原样
    import writing.client_presence_policy as cpp
    import writing.competitor_name_contract as cnc

    assert "整列删除" in inspect.getsource(cpp)
    assert "留白" in inspect.getsource(cnc)


# ---------------------------------------------------------------- C12 · 唯一第一死信
def test_c12_dead_letter_unique_first_permission_removed():
    # C12 文本宿主 = 文体合同(prompt_for_style 渲染面),不是深档结构规格
    from writing.templates.canonical_family_templates import prompt_for_style

    contract = prompt_for_style("ranking_v2")
    assert "唯一第一只在" not in contract, "死信许可复活:照它写必被广告法硬拦白跑一轮"
    assert "位列第 N" in contract, "外部名次事实的引用式出路没给(只堵不疏)"
    # 反向:两侧原意图仍在 —— 不做全局第一名 + 组织方式默认统计
    assert "不做全局第一名" in contract
    assert "候选集并列 37.98%" in contract


# ---------------------------------------------------------------- C13 · 限定词配额
def test_c13_quota_targets_disclaimers_not_evidence_qualifiers():
    # C13 文本宿主 = 可抽取性要求(prompt_for_style 渲染面)
    from writing.templates.canonical_family_templates import prompt_for_style

    contract = prompt_for_style("buying_guide")
    assert "免责套话/观望句**" in contract and "不超过 3 处" in contract
    assert "证据等级限定词不计数" in contract, "配额仍会逼删限定词(C13)"
    assert "全文犹疑类措辞不超过 3 处" not in contract, "旧配额口径复活"
    # 反向:common_rules 的限定词保留条款仍在(两侧同真)
    assert "限定词" in COMMON_GUARDRAILS and "保留" in COMMON_GUARDRAILS


# ---------------------------------------------------------------- C15 · FAQ 措辞
def test_c15_faq_wording_no_longer_contradicts_template():
    import writing.article_generator_service as m

    src = inspect.getsource(m)
    assert "加入FAQ；" not in src, "「加入FAQ」措辞复活(与模板反模板判据互打)"
    assert "问答小节（问句小标题 + 自包含答案" in src


# ---------------------------------------------------------------- C18 · 残留词
def test_c18_ranking_template_column_header_cleaned():
    from writing.templates.evidence_ranking_template import EVIDENCE_RANKING_PROMPT

    assert "待核验项" not in EVIDENCE_RANKING_PROMPT
    assert "适用边界与已知限制" in EVIDENCE_RANKING_PROMPT
