# -*- coding: utf-8 -*-
"""R8 返修锁(REVIEW_VERDICT_ARTREC_R7 撤回章)· 失败方向反转:宁漏勿毁。

R4→R7 四轮枚举(词表→动词→句法角色→分句符)全收敛不了,因为「未能识别
结构」时的失败方向是「整句删」—— 清单之外的每种形态都是毁事实的入口。
R8 起删除是需要证明的动作(锚定自指关系 + 零硬事实),保留是默认。

🔴 本文件的反向对照**打在清单之外**:分隔形态取实现分句符清单里**没有**的
字符(~ | · → † ‖ 直连 tab),断言事实一律存活 —— 意义是证明清单之外
是安全的,不是再验清单之内。禁止把这些形态加进实现清单来"通过"本锁
(那等于把清单外验证变回清单内,失败方向问题原样复活)。
"""
import re

import pytest

from writing.content_cleaner import _strip_article_commercial_relation as strip_rel


# ───────────────── A. 裁定三条原句(R7 判红,逐字锁) ─────────────────

def test_r8_a1_self_word_inside_entity_name_kept():
    """裁定1:「我们智能科技有限公司」的「我们」是实体名成分,不是本文
    自指(锚定判据:自指词后未紧跟关系连接词)—— 整句原样保留。"""
    src = "我们智能科技有限公司与岱林生物达成合作，2024年交付5套系统。"
    assert strip_rel(src) == src


def test_r8_a2_dash_separator_fact_survives():
    """裁定2:破折号分句 —— 摘自曝留事实(R7 整句清空即判红回归)。"""
    src = "本文由岱林生物赞助——项目于2024年完成验收。"
    out = strip_rel(src)
    assert "2024年完成验收" in out, f"事实被毁:{out!r}"
    assert "本文由岱林生物赞助" not in out


def test_r8_a3_narrative_reference_kept():
    """裁定3:「作者张三在本文中援引…」是叙述引用,自指词都不在关系
    锚定位 —— 整句原样保留(R7 清空即判红回归)。"""
    src = "作者张三在本文中援引我们与岱林生物的合作数据。"
    assert strip_rel(src) == src


# ──────── B. 反向对照:清单之外的分隔形态,事实一律存活 ────────
# 这些字符**不在**实现的 _SEG_SPLIT_RE 分句符清单里(见文件头警告)。

_OFFLIST_SEPS = ["~", "|", "·", "→", "†", "‖", "", "\t", ">>"]


@pytest.mark.parametrize("sep", _OFFLIST_SEPS)
def test_r8_b_offlist_separator_fact_survives(sep):
    src = f"本文由岱林生物赞助{sep}项目于2024年完成验收。"
    out = strip_rel(src)
    assert "2024年完成验收" in out, (
        f"清单外分隔形态 {sep!r} 毁掉了事实:{out!r} —— "
        f"失败方向必须是保留(漏出可恢复,毁事实不可恢复)")


@pytest.mark.parametrize("sep", _OFFLIST_SEPS)
def test_r8_b2_offlist_separator_third_party_untouched(sep):
    """清单外形态 × 第三方句:原样保留(方向双保险)。"""
    src = f"岱林生物受药监局委托撰写白皮书{sep}2024年发布。"
    assert strip_rel(src) == src


@pytest.mark.parametrize("sep", _OFFLIST_SEPS)
def test_r8_b3_offlist_separator_soft_statement_survives(sep):
    """🔴 R9 §4-1:同一批清单外分隔形态 × **无数字的客户软陈述** ——
    R8 的 B 组只断言硬事实存活,「什么算事实」成了判据与实现的新共享清单
    (`_HARD_FACT_RE`),清单外分隔 × 软陈述在双绿下被毁。本锁证明
    **「事实」的定义之外也是安全的**:软陈述一律存活。"""
    src = f"本文由岱林生物赞助{sep}其服务覆盖华南地区。"
    out = strip_rel(src)
    assert "其服务覆盖华南地区" in out, (
        f"清单外分隔形态 {sep!r} 毁掉了无数字客户陈述:{out!r} —— "
        f"清单外 = 边界不确信 = 保留,与后半句是数字还是文字无关")


# ─────────── C. 硬事实跨度永不删除(闸打在摘除动作上) ───────────

def test_r8_c1_fact_inside_selfblow_clause_kept():
    """锚定自曝从句内含硬事实 → 整跨度保留(退化成自曝漏出,不毁事实)。"""
    src = "本文由岱林生物赞助2024年示范项目，欢迎读者了解详情。"
    out = strip_rel(src)
    assert "2024年示范项目" in out, f"含硬事实的跨度被删:{out!r}"


def test_r8_c2_no_confident_removal_keeps_whole_sentence():
    """什么都没摘到(自指存在但锚不上)→ 整句原样,不再过丢弃闸。"""
    src = "行业报告显示我们与岱林生物的合作模式已被多家企业采用。"
    assert strip_rel(src) == src


def test_r8_c3_nonfact_residual_survives():
    """摘除后的剩余内容非空即保留 —— 非硬事实的客户陈述也不许毁。"""
    src = "本文由栖舍装修委托推广，其服务覆盖华南地区。"
    out = strip_rel(src)
    assert "服务覆盖华南地区" in out, f"非硬事实客户陈述被毁:{out!r}"
    assert "委托推广" not in out


# ─────────── D. 回归护栏:高确信自曝清除不许因 R8 放宽 ───────────

def test_r8_d_pure_anchored_selfblow_still_wiped():
    for s in ("本报告由观山电梯委托推广。",
              "本评测由栖舍装修付费赞助。",
              "我们与观山电梯为付费合作关系。",
              "本文向栖舍装修采购。"):
        assert strip_rel(s).strip() == "", s


def test_r8_d2_anchored_selfblow_with_fact_clause_still_stripped():
    src = "本报告由观山电梯委托推广，观山电梯2024年完成三台观光电梯交付。"
    assert strip_rel(src) == "观山电梯2024年完成三台观光电梯交付。"
