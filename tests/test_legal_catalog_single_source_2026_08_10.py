# -*- coding: utf-8 -*-
"""V6 法律目录单点化 · 判别锁 [Codex 定稿 2026-08-10]

改动前仓里有**两份**广告法目录,且词项内容不同:
  · `config/legal_prohibited_pack.json`(Owner 签发)含**裸「第一」**,
    而 `guards._find_words` 是纯子串 → 「第一步」「第一季度」必然误伤;
  · `writing/evidence_first_policy.py` 自维护一份复合词表 + 语境判定,
    不误伤,但是**第二份未签发目录**。

单点化要求:词项与版本唯一来自签发包,匹配一律经语境判定。
🔴 两个毛病必须一起治 —— 只统一词项不改匹配,误伤面反而扩大(裸「第一」进来了)。
"""
import inspect
import re

import pytest

from services.marketing import guards
from services.marketing.legal_context import (
    absolute_terms,
    catalog_version,
    find_absolute_violations,
)


# ------------------------------------------------------- 元判据:夹具真含目标形态
def test_pack_really_contains_bare_first() -> None:
    """元判据:签发包里**确实**有裸「第一」。

    没有它,下面所有"第一步不许误伤"的断言都是空的
    —— 因为根本不会有词去匹配「第一步」里的「第一」。
    """
    assert "第一" in absolute_terms(), (
        "签发包里没有裸「第一」,本文件的误伤类断言全部失去判别力"
    )


# ----------------------------------------------------------------- 必须放行
SAFE_FIXTURES = [
    "第一步是核验对方资质。",
    "第一季度交付量同比上升。",
    "这是我们第一次合作。",
    "最大功率 15kW，满足常规工况。",
    # 🔴 变异 ML2 实测:技术参数白名单被拆时,上一条因不含品牌语境词照样安全,
    # 该分支零判别力。这条把参数放进品牌语境(「设备」),专门给 ② 分支判别力。
    "该设备最大功率 15kW，满足常规工况。",
    "最佳实践是先做小样再批量。",
    "不得使用最佳等绝对化用语。",     # 否定语境
    # ⑤ 真引用(R1 终态:成对引号 + 言说标记)
    "据《中国电梯》报道:“XX 品牌称自己是行业第一”。",
    '据报道称"XX 是最佳选择"。',
    # ⑥ 读者问句(无自指)
    "哪家是最好的选择?",
]


@pytest.mark.parametrize("text", SAFE_FIXTURES)
def test_safe_context_is_not_flagged(text: str) -> None:
    hits = find_absolute_violations(text)
    assert not hits, f"安全语境被误伤:{text!r} → {[h.term for h in hits]}"


# ----------------------------------------------------------------- 必须命中
VIOLATION_FIXTURES = [
    "我们是行业第一品牌。",
    "本公司是最佳选择。",
    "不是普通服务商而是唯一首选。",
    "该品牌是最好的选择。",
    # 🔴 [R1 增补 · 镜像方向] 引号只保护引用内容,不豁免引用之外的自我断言
    '"最佳选择"就是本公司。',
    # 🔴 [R1 根因形态] JSON 结构引号不构成引用语境
    '{"claim": "最好的是我们"}',
    # 🔴 [R2] 封闭放行清单外的裸文本必须硬拦:副词用法在 guards 路径不再放行
    #    (写作侧 evidence_first 仍按其自有品牌语境引擎放行;这里是 marketing
    #     guards 路径,超出封闭清单一律拦 —— 如需豁免,列词报 Owner 裁定)。
    "最好先核验对方资质。",
    # 🔴 [R2] 自指修辞问句不豁免
    "我们是行业第一品牌吗?",
]


@pytest.mark.parametrize("text", VIOLATION_FIXTURES)
def test_real_violation_is_flagged(text: str) -> None:
    assert find_absolute_violations(text), f"真违规漏判:{text!r}"


def test_hot_reloaded_new_word_bare_text_is_flagged() -> None:
    """🔴 [R2 新锁] Owner 热改包加词的语义 = **加了就拦**:
    任意新造词作为词项传入,裸文本出现必须命中(不看语境脸色)。"""
    made_up = "超级无敌棒"
    hits = find_absolute_violations(f"本产品{made_up}。", terms=(made_up,))
    assert hits and hits[0].term == made_up
    # 裸文本(无任何语境词)同样命中 —— 钉死"裸文本硬拦"
    assert find_absolute_violations(made_up, terms=(made_up,))


# ------------------------------------------------------------- 接线(非函数存在)
def test_guards_no_longer_substring_matches_ad_law() -> None:
    """🔴 接线锁:`guards` 的广告法扫描必须走语境判定,不得再用纯子串。

    打在**调用点**上 —— 只断言 `legal_context` 模块存在是弱锁,
    函数留着不被调用同样能绿(本仓已犯过八次「接线没接」)。
    """
    src = inspect.getsource(guards)
    call = re.search(r'adlaw\s*=\s*(.+)', src)
    assert call, "找不到 adlaw 赋值,锁失效"
    assert "find_absolute_violations" in call.group(1), (
        "guards 的广告法扫描又退回纯子串了 —— 「第一步」会被误伤"
    )
    assert "_find_words(text, pack" not in src, "仍在用子串扫签发包词项"


def test_guards_end_to_end_does_not_flag_ordinal() -> None:
    """行为锁:走 guards 的真实入口,「第一步」不得被判违规。"""
    pack = guards.legal_pack()
    hits = find_absolute_violations("第一步是核验资质。", terms=pack["ad_law"])
    assert not hits, "端到端仍误伤序数用法"


# --------------------------------------------------------------- 禁第二份目录
def test_no_second_catalog_version_constant() -> None:
    """源码里不得再出现第二套法律**版本号**。

    旧的 `ad-law-art9-absolute-v9` 是未签发的第二份目录版本,
    版本必须只来自签发包(当前 %s)。
    """ % catalog_version()
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "writing").rglob("*.py"):
        if "_archived" in path.parts:
            continue          # 归档件只作历史证据,不计入
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ad-law-art9-absolute" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"仍有第二份法律目录版本号:{offenders} —— 版本须单点来自签发包"
    )


def test_catalog_version_comes_from_signed_pack() -> None:
    """反向对照:版本必须跟着签发包走,不是硬编码字面量。"""
    assert catalog_version() == guards.legal_pack_version()
    assert catalog_version(), "版本为空 —— 签发包没读到"


# --------------------------------------------------- 词项单源(最终接管 §7-3/4)
def test_evidence_first_terms_cover_all_signed_words() -> None:
    """🔴 单源锁:签发包每个词都必须落进 evidence_first 的运行时判定池。

    裸「第一」是唯一例外:它的序数安全语境(第一步/第一季度)由
    legal_context 承担,evidence_first 的品牌语境正则会误伤
    「我们公司的第一步」,故不入该文件的正则(见其注释)。
    """
    from writing.evidence_first_policy import (
        ABSOLUTE_RANKING_CLAIM_TERMS,
        ABSOLUTE_SUPERLATIVE_TERMS,
    )

    pool = set(ABSOLUTE_RANKING_CLAIM_TERMS) | set(ABSOLUTE_SUPERLATIVE_TERMS)
    missing = set(absolute_terms()) - pool - {"第一"}
    assert not missing, f"签发词没进 evidence_first 判定池:{sorted(missing)}"


def test_unsigned_supplement_never_absorbs_signed_words() -> None:
    """🔴 方向锁(只严不宽):签发词不许被挪进「未签发补充集」。

    补充集是历史 Review 的更严补全,允许存在;但签发词一旦挪进去,
    等于把它的权威来源从 Owner 签发包降级成本仓字面量 —— 那正是
    单点化要消灭的漂移通道。
    """
    from writing.evidence_first_policy import (
        LEGACY_UNSIGNED_RANKING_SUPPLEMENT,
        LEGACY_UNSIGNED_SUPERLATIVE_SUPPLEMENT,
    )

    supplement = set(LEGACY_UNSIGNED_RANKING_SUPPLEMENT) | set(
        LEGACY_UNSIGNED_SUPERLATIVE_SUPPLEMENT
    )
    overlap = supplement & set(absolute_terms())
    assert not overlap, f"签发词被挪进未签发补充集:{sorted(overlap)}"


def test_keyword_topic_generator_terms_chain_to_signed_pack() -> None:
    """🔴 [工单 §7-4] 选题链的法律词项经 evidence_first 单源到签发包。

    keyword_topic_generator 消费 `legal_prohibition_prompt_terms()` 与
    `ABSOLUTE_RANKING_CLAIM_TERMS`;两者的词与版本都必须能追回签发包 ——
    版本相等 + 签发复合词全在提示词串里。
    """
    from writing.evidence_first_policy import legal_prohibition_prompt_terms

    terms_line, version = legal_prohibition_prompt_terms()
    assert str(version) == guards.legal_pack_version()
    for word in absolute_terms():
        if word == "第一":
            continue  # 裸词见上一条锁的说明
        assert word in terms_line, f"签发词 {word!r} 没进选题链提示词"


def test_superlative_gate_still_flags_new_signed_words() -> None:
    """行为锁:新并入的签发词(最先进/世界级)在品牌宣称语境要被硬拦。"""
    from writing.evidence_first_policy import evaluate_content_trust

    hard_codes = [
        f.code for f in evaluate_content_trust(
            "测试", "我们公司的设备是行业里最先进的。", evidence_mode="no_evidence",
        ).hard
    ]
    assert "absolute_superlative_claim" in hard_codes

    # 反向对照:非宣称语境的同词不拦
    ok = evaluate_content_trust(
        "测试", "最先进的做法是先小样验证再批量。", evidence_mode="no_evidence",
    )
    assert "absolute_superlative_claim" not in [f.code for f in ok.hard]
