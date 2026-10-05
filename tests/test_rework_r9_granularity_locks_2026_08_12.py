# -*- coding: utf-8 -*-
"""R9 返修锁(WO_ARTREC_REWORK_R9 · REVIEW_VERDICT_ARTREC_R8 NO-GO)。

R8 把「摘除」变成需要证明的动作,但摘除的**粒度**仍由分隔符清单决定:
清单外分隔符 = 无法确信从句边界 = seg 覆盖整句,「摘一个从句」和「删掉
整句」共用了同一份证明门槛 —— 两个动作风险不对等,不能共用。

R9:seg 覆盖整句(没识别到任何内部边界)时,整句级摘除要一道更强证明 ——
**自曝跨度(有效自指 + 关系短语 span 并集)之外不再有实质内容才可删**;
否则整句保留(退化为自曝漏出)。规则与后半句是数字还是文字无关。

变异 RG1(粒度闸拆除)由本文件接杀,证据 `rg_mutation_proof.txt`。
"""
import pytest

from writing.content_cleaner import (
    _strip_article_commercial_relation as strip_rel,
    _selfblow_covers_sentence,
    _effective_self_spans,
)


# ─────────── A. 裁定 §2 三条真实形态(逐字,R8 判红) ───────────

@pytest.mark.parametrize("src", [
    "本文由岱林生物赞助/其服务覆盖华南地区。",
    "本文由岱林生物赞助·其服务覆盖华南地区。",
    "本文由岱林生物赞助→其品牌定位高端市场。",
])
def test_r9_a_verdict_three_sentences_kept_verbatim(src):
    assert strip_rel(src) == src, "清单外分隔 × 软陈述必须整句保留(漏出)"


# ─────── B. Review 探针 C 组 8 条清单外原句(逐字进锁,§4-2) ───────

@pytest.mark.parametrize("sep", ["/", "※", "～", "&", "◆", "~", "|", "·"])
def test_r9_b_probe_group_c_offlist_soft_statement(sep):
    src = f"本文由岱林生物赞助{sep}其服务覆盖华南地区。"
    assert strip_rel(src) == src


@pytest.mark.parametrize("sep", ["，", "；", "：", "、", "—"])
def test_r9_b2_inlist_separator_still_precise(sep):
    """对照:清单内分隔符仍精确摘自曝留陈述(粒度闸不许影响从句级)。"""
    src = f"本文由岱林生物赞助{sep}其服务覆盖华南地区。"
    assert strip_rel(src) == "其服务覆盖华南地区。"


# ─────── C. 纯自曝整删回归(§4-3:Review 实验版翻车处,单独列) ───────

@pytest.mark.parametrize("src", [
    "本报告由观山电梯委托推广。",     # 工单坑 1:非贪婪留尾巴「推广」
    "本评测由栖舍装修付费赞助。",
    "我们与观山电梯为付费合作关系。",  # strong/declarative 重叠拼接
    "本文向栖舍装修采购。",
])
def test_r9_c_pure_selfblow_still_wiped(src):
    assert strip_rel(src).strip() == "", src


# ─────────── D. 粒度闸判定函数单元格(span 并集正确性) ───────────

@pytest.mark.parametrize("sentence,covers", [
    ("本报告由观山电梯委托推广。", True),      # 坑 1:并集补上「推广」
    ("本评测由栖舍装修付费赞助。", True),
    ("我们与观山电梯为付费合作关系。", True),   # strong+declarative 重叠
    ("本文向栖舍装修采购。", True),
    ("观山电梯是本文的商业合作客户。", True),   # 倒装声明式:主语实体并入
    ("本文涉及的客户关系为委托推广。", True),   # 短胶水「涉及的」桥接
    ("本文由岱林生物赞助/其服务覆盖华南地区。", False),
    ("本文由岱林生物赞助→其品牌定位高端市场。", False),
    ("本文由岱林生物赞助※项目于2024年完成验收。", False),
])
def test_r9_d_covers_sentence_unit(sentence, covers):
    spans = _effective_self_spans(sentence)
    assert _selfblow_covers_sentence(sentence, spans) is covers, sentence


@pytest.mark.parametrize("src", [
    # 「其品牌」不是胶水:间隙含分隔样字符 = 边界证据,不桥
    "本文由岱林生物赞助/其品牌是行业合作伙伴。",
    # 自曝跨度之间夹着独立陈述:>3 字不桥
    "本文/服务覆盖华南/由岱林生物赞助。",
    # 3 字短软陈述也存活(桥接只吃 span 之间的纯文字胶水,后缀永不桥)
    "本文由岱林生物赞助·服务好。",
])
def test_r9_d2_bridge_never_eats_client_statements(src):
    """桥接护栏:倒装式/胶水桥只为覆盖自曝短语自身的碎片,客户陈述
    (被分隔字符隔开的、位于前后缀的、极短的)一律不许被并入覆盖。"""
    assert strip_rel(src) == src, src


# ─────────────── E. 方向护栏:双自曝与换行行为不回退 ───────────────

def test_r9_e_double_selfblow_across_offlist_still_stripped():
    """有清单内边界时从句级照常工作:双自曝(跨清单外分隔)整段摘除。"""
    src = "本文由岱林生物赞助※本报告由栖舍装修委托，2024年验收。"
    assert strip_rel(src) == "2024年验收。"


def test_r9_e2_newline_pure_selfblow_line_still_wiped():
    """换行分隔:纯自曝行(行内全覆盖)仍消失,事实行保留。"""
    src = "本文由岱林生物赞助\n项目于2024年完成验收。"
    assert strip_rel(src) == "\n项目于2024年完成验收。"
