# -*- coding: utf-8 -*-
"""R7 返修锁(WO_ARTREC_REWORK_R7_2026-08-12)· 判别依据换代:句法角色。

R4 漏混合句 → R5 漏同义动词 → R6 漏「自指词出现在第三方句子内部」——
三轮同型翻车,病根都是「整句里有没有某个词」。R7 判别落在**句法角色**:
  1. 说话者归属:据/援引 X 介绍/表示/称 之后是被引述方的话,「我们」指受访者;
  2. 自指词的句法角色:作者/笔者 带前缀修饰或后接人名 = 称谓,指第三方;
  3. 关系双方:两端都是第三方实体 → 一律保留,与句中出现什么词无关。

本文件三层锁:
  A. 工单 §5-1 六条原句(§1×2 + §2×1 + §3×2 + 关键回归)逐字行为锁;
  B. 角色矩阵:角色(自指主语/引语内/人名修饰/前缀修饰/第三方)
     × 分句符(逗号/分号/冒号/引号)× 建议共存(有/无)——期望由角色规则
     推导(非 self 角色一律原样;self 角色=关系声明消失、事实与建议保留);
  C. meta:全套返修锁文件禁止 `or True` 尾挂恒真断言。

🔴 把判别退回 R6 实现(裸词形 `_ARTICLE_SELF_RE.search(sentence)` /
建议整句放行 / 无冒号分句)时,A、B 两层必须转红 —— 变异 RE1/RE2/RE3
即此三条退回,证据 `re_mutation_proof.txt`。
"""
import re
from pathlib import Path

import pytest

from writing.content_cleaner import (
    _strip_article_commercial_relation as strip_rel,
    _effective_self_spans,
)


# ───────────────────────── A. 工单六条原句(逐字) ─────────────────────────

def test_r7_a1_quoted_women_is_interviewee_not_article():
    """§1:据 X 介绍 之后的「我们」是受访者 —— 整句原样保留(R6 砍成
    `2024年完成三项标准。` 即为判红回归)。"""
    src = "据岱林生物项目负责人介绍：我们与浙江省药监局合作，2024年完成三项标准。"
    assert strip_rel(src) == src


def test_r7_a2_author_plus_name_is_third_party():
    """§1:「作者张三」的『作者』是人名修饰成分,指第三方 —— 原样保留
    (R6 整句清空即为判红回归)。"""
    src = "作者张三与岱林生物合作完成了2024年行业白皮书。"
    assert strip_rel(src) == src


def test_r7_a3_advice_shields_only_advice_clause():
    """§2:建议语气只保护建议那一小句;自曝从句照摘(R6 整句放行=自曝
    绕过开关,Owner「一年努力全部白费」类)。"""
    src = "本文由栖舍装修委托推广，建议读者先核对合同。"
    out = strip_rel(src)
    assert out == "建议读者先核对合同。"
    assert "委托推广" not in out and "栖舍装修" not in out


def test_r7_a4_colon_splits_clauses():
    """§3:冒号是分句符 —— 摘自曝留事实(R6 整句清空即为判红回归)。"""
    src = "本文由岱林生物赞助：项目于2024年完成验收。"
    assert strip_rel(src) == "项目于2024年完成验收。"


def test_r7_a5_multi_relation_keeps_third_party_no_orphan():
    """§3:多重关系句 —— 摘自曝、留第三方关系与事实,且不留孤儿指代
    (「其」补回先行词「观山电梯」,句首悬空的「报道」剥离)。"""
    src = ("本文受观山电梯委托，报道其与南山区政府合作的无障碍改造项目，"
           "该项目2025年验收。")
    out = strip_rel(src)
    assert out == "观山电梯与南山区政府合作的无障碍改造项目，该项目2025年验收。"
    assert "本文" not in out and "受" not in out.split("，")[0][:6]
    # 孤儿指代禁令:残句不得以悬空的「其/报道其」开头
    assert not re.match(r"(?:报道)?其[与和]", out)


def test_r7_a6_pure_selfblow_still_wiped_regression():
    """回归护栏(§6 禁做:不许借修 §1 放宽纯自爆清除):三类成品仍整句清。"""
    for s in ("本报告由观山电梯委托推广。",
              "本评测由栖舍装修付费赞助。",
              "我们与观山电梯为付费合作关系。"):
        assert strip_rel(s).strip() == "", s


def test_r7_a7_third_party_and_mixed_still_kept_regression():
    """回归护栏:R5/R6 已做对的第三方与混合句行为不许退化。"""
    for s in ("岱林生物受浙江省药监局委托撰写行业白皮书，2024年发布。",
              "万汇广场是观山电梯的商业合作客户，双方已完成三台观光电梯交付。",
              "该设备额定载重1000公斤，提升速度1.75米每秒。",
              "建议与本地服务商合作，签订正规合同。"):
        assert strip_rel(s) == s, s


# ──────────────── B. 角色矩阵(角色 × 分句符 × 建议共存) ────────────────
# 期望由**角色规则**推导,不按模板填:
#   · 角色 ≠ 自指主语 → 关系双方均为第三方 → 整句原样(与分句符/建议无关);
#   · 角色 = 自指主语 → 关系声明消失、硬事实保留、建议小句保留、无孤儿标点。

_SEPS = {"逗号": "，", "分号": "；", "冒号": "：", "引号": "，“引述内容”，"}
_ADVICE_TAIL = "建议读者核对合同资质。"
_FACT = "观山电梯2024年交付三台设备。"

# (角色名, 句子构造函数 src(sep, advice))
def _mk_self(sep, advice):
    return "本文由观山电梯委托推广" + sep + (_ADVICE_TAIL if advice else "") + _FACT

def _mk_quoted(sep, advice):
    return ("据观山电梯负责人介绍：我们与南山区政府合作" + sep
            + (_ADVICE_TAIL if advice else "") + "2024年完成三项验收。")

def _mk_name(sep, advice):
    return ("作者张三与岱林生物合作" + sep
            + (_ADVICE_TAIL if advice else "") + "完成了2024年行业白皮书。")

def _mk_prefix(sep, advice):
    return ("第一作者与岱林生物为合作方" + sep
            + (_ADVICE_TAIL if advice else "") + "2024年联合发布行业标准。")

def _mk_third(sep, advice):
    return ("岱林生物受药监局委托撰写白皮书" + sep
            + (_ADVICE_TAIL if advice else "") + "2024年正式发布。")

_ROLES = [
    ("self_subject", _mk_self),
    ("quoted", _mk_quoted),
    ("name_mod", _mk_name),
    ("prefix_mod", _mk_prefix),
    ("third", _mk_third),
]

_MATRIX = [
    (role, sep_name, advice)
    for role, _ in _ROLES
    for sep_name in _SEPS
    for advice in (False, True)
]


@pytest.mark.parametrize("role,sep_name,advice", _MATRIX)
def test_r7_b_role_matrix(role, sep_name, advice):
    mk = dict(_ROLES)[role]
    src = mk(_SEPS[sep_name], advice)
    out = strip_rel(src)
    if role != "self_subject":
        # 角色规则:关系一方不是本文自身 → 第三方业务事实,原样保留
        assert out == src, f"{role}/{sep_name}/advice={advice} 被误动:{out!r}"
        return
    # 自指主语:关系声明必须消失
    assert "委托推广" not in out and "本文" not in out, f"自曝残留:{out!r}"
    # 硬事实必须保留
    assert "2024年交付三台设备" in out, f"删客户事实:{out!r}"
    # 建议小句保留(建议只保护建议那一小句,但也必须被保护)
    if advice:
        assert "建议读者核对合同资质" in out, f"建议被连坐:{out!r}"
    # 无孤儿标点/空句
    assert not re.match(r"[，,、；;：:]", out), f"孤儿标点:{out!r}"


# ─────────────── B2. 有效自指判别的单元格(直接打判别函数) ───────────────

@pytest.mark.parametrize("sentence,expect_self", [
    ("本文由观山电梯委托推广。", True),
    ("本报告与岱林生物为付费合作关系。", True),
    ("我们与观山电梯签订了合作协议。", True),
    ("笔者认为该项目值得关注。", True),
    ("据岱林生物负责人介绍：我们与药监局合作。", False),   # 引语内
    ("负责人表示，“我们已完成三项验收”。", False),          # 引号内
    ("作者张三与岱林生物合作。", False),                    # 人名修饰
    ("第一作者与岱林生物为合作方。", False),                # 前缀修饰
    ("原作者曾参与该项目验收。", False),                    # 前缀修饰
    ("岱林生物受药监局委托撰写白皮书。", False),            # 无自指词
])
def test_r7_b2_effective_self_spans(sentence, expect_self):
    assert bool(_effective_self_spans(sentence)) is expect_self, sentence


# ───────────── C. meta:返修锁文件禁止 or True 尾挂恒真断言 ─────────────

def test_r7_c_no_tautological_assertions_in_rework_locks():
    """§4:`assert A and B or True` 恒真 —— 全套返修锁扫描门禁。"""
    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for f in sorted(tests_dir.glob("test_rework_r*_locks_*.py")):
        for i, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0]
            if re.search(r"^\s*assert\b.*\bor\s+True\b", line):
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"恒真断言残留:{offenders}"
