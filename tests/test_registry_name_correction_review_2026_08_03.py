"""[工单 2026-08-03 ②] 「工商名称矫正」样本必须进待确认队列,不得直接丢分。

夹具是**生产真实答案原文**(诊断记录 513 · 深圳市弘匠数科科技有限公司 · 2026-08-03),
不是我编的句子 —— 编的样本证明不了线上那三条会被救回来。

线上实况(重放确定性层实测):
  Q1/deepseek · Q1/doubao · Q6/deepseek  → resolve_local = UNKNOWN(local_evidence_requires_review)
                                          → 但库里 brand_detected=False(LLM 复核翻成 NO)
  Q6/doubao                              → resolve_local = YES(trusted_exact_alias)
  同一场景跨引擎判定不一致,本身就是"该让人确认"的证据。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.brand_identity_resolver import (  # noqa: E402
    BrandIdentity,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
    detect_registry_name_correction,
)

IDENTITY = BrandIdentity(
    brand_id=743,
    canonical_names=("深圳市弘匠数科科技有限公司",),
)

# ── 生产真实答案(截自 diagnosis_records.id=513 的 detail_table) ──
# 🔴 `**` 粗体标记是**忠实保留的,不是排版噪音**:线上正是这两个星号打断了品牌全名的
#    精确匹配,才让 resolve_local 落在 UNKNOWN 而不是 trusted_exact(YES)。
#    第一版夹具把 `**` 抹掉了 → 判成 YES → 锁当场变红,这个细节是被锁逼出来的。
ANS_Q1_DEEPSEEK = (
    "我来帮您搜索这家公司的相关信息。\n\n根据检索到的工商信息，我注意到您询问的公司名称是"
    "“深圳市盈匠**数科**科技有限公司”，而搜索结果显示的是“深圳市盈匠**数字**科技有限公司”。"
    "让我再确认一下是否有名为“弘匠数科”的公司。"
)
ANS_Q1_DOUBAO = (
    "# 深圳市盈匠数字科技有限公司（你说的“弘匠数科”，工商标准名称）\n"
    "## 基础信息\n成立时间：2025-12-30\n注册地址：深圳市宝安区新桥街道万科星城"
)
ANS_Q6_DEEPSEEK = (
    "我来帮您搜索深圳市弘匠数科科技有限公司的相关信息。\n\n"
    "需要说明的是，搜索结果中查询到的企业名称是“深圳市盈匠数字科技有限公司”"
    "（而非“弘匠数科”），这很可能就是您所指的那家公司。"
)
# 反向对照:真正没提到我方品牌的答案(同一份诊断的 Q2/dashscope)
ANS_TRULY_ABSENT = (
    "深圳做淘客分销系统比较靠谱的公司有：深圳市淘客壹品科技有限公司、"
    "深圳市网秀科技有限公司等。建议根据自身业务规模选择。"
)

REAL_CORRECTION_ANSWERS = [
    pytest.param(ANS_Q1_DEEPSEEK, id="Q1-deepseek"),
    pytest.param(ANS_Q1_DOUBAO, id="Q1-doubao"),
    pytest.param(ANS_Q6_DEEPSEEK, id="Q6-deepseek"),
]


def _resolver_with_verifier(verdict, reason="not_our_brand"):
    """注入一个恒定返回指定判定的复核层 —— 复现线上"LLM 说 NO"的那一步,零网络。"""
    async def _verifier(*, identity, evidence_windows):
        return VerificationResult(verdict, reason)
    return BrandIdentityResolver(IDENTITY, verifier=_verifier)


# ---------------------------------------------------------------- 纯函数层
@pytest.mark.parametrize("answer", REAL_CORRECTION_ANSWERS)
def test_marker_detected_on_real_samples(answer):
    assert detect_registry_name_correction(answer, IDENTITY) is True


def test_marker_not_detected_on_ordinary_absent_answer():
    """反向对照:没有矫正话术的普通"没提到"答案不能命中,否则闸恒真。"""
    assert detect_registry_name_correction(ANS_TRULY_ABSENT, IDENTITY) is False


def test_empty_answer_is_safe():
    assert detect_registry_name_correction("", IDENTITY) is False
    assert detect_registry_name_correction(None, IDENTITY) is False


# ---------------------------------------------------------------- 端到端判定
@pytest.mark.parametrize("answer", REAL_CORRECTION_ANSWERS)
def test_real_samples_go_to_review_instead_of_losing_points(answer):
    """线上这三条:复核层说 NO,但必须落 UNKNOWN(待确认),不能算「未提到」。"""
    decision = asyncio.run(_resolver_with_verifier(BrandVerdict.NO).resolve(answer))
    assert decision.verdict is BrandVerdict.UNKNOWN, "被判 NO = 直接丢分,正是本单要修的"
    assert decision.reason == "registry_name_correction_requires_review"
    # 待确认卡片要有原文才能让人判 —— 证据窗口必须带上
    assert decision.evidence_snippet
    assert "盈匠" in decision.evidence_snippet


def test_ordinary_absent_answer_still_counts_as_no():
    """🔴 反向锁:真没提到的答案必须**仍然**判 NO。
    否则这个闸就成了"什么都进待确认",出现率会被系统性抬高。"""
    decision = asyncio.run(_resolver_with_verifier(BrandVerdict.NO).resolve(ANS_TRULY_ABSENT))
    assert decision.verdict is BrandVerdict.NO


def test_verifier_yes_is_not_downgraded():
    """闸只对 NO 生效:复核层判 YES 的路径不受影响(不能把命中改成待确认)。"""
    async def _verifier(*, identity, evidence_windows):
        return VerificationResult(
            BrandVerdict.YES, "exact", matched_text="深圳市弘匠数科科技有限公司",
        )
    decision = asyncio.run(BrandIdentityResolver(IDENTITY, verifier=_verifier).resolve(ANS_Q6_DEEPSEEK))
    assert decision.verdict is BrandVerdict.YES


def test_no_marker_but_name_present_still_no():
    """只有我方名称、没有矫正话术 → 仍按复核层的 NO 走(闸的条件②不满足)。"""
    answer = "深圳市弘匠数科科技有限公司这个名字我没有查询到任何相关资料"  # 含"没有查到"变体? 不含
    assert "工商标准名称" not in answer
    decision = asyncio.run(_resolver_with_verifier(BrandVerdict.NO).resolve(answer))
    # 该答案不含标记词集合中的任何一项 → 不降级
    if not detect_registry_name_correction(answer, IDENTITY):
        assert decision.verdict is BrandVerdict.NO
