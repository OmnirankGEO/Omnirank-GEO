# -*- coding: utf-8 -*-
"""WO_232 §3.1 · 关键词是公司注册名时,标题里的自然缩写不算换商业对象。

现场(Owner 截图 2026-09-17):真客户品牌 995「广东星衍朗科技有限公司」买了自己的
完整注册名,写作大厅 4 个关键词的选题全标「失败 · TITLE_AI_UNAVAILABLE」。
容器日志:`reason=conflicting_business_qualifier` ×2 批。**AI 一次都没失败** ——
拒掉标题的是我们自己的关键词身份复核:它是为「医用→家用」这类换限定词设计的,
把「丢掉『有限公司』」也读成了换商业对象。

🔴 本组判据的**两侧**必须同时立着:
   放行侧(公司名缩写)与拒绝侧(换限定词)。只立放行侧的话,
   我把 replace 守卫整条删掉也会全绿 —— 那正是这道守卫存在的理由。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from writing.title_keyword_alignment import (  # noqa: E402
    assess_title_keyword_alignment,
    company_name_variants,
)

BRAND = "广东星衍朗科技有限公司"
KEYWORD = "广东星衍朗科技有限公司"

#: WO §2 本机重放实录的三组(前两组在基线上 FAIL,第一组基线就 PASS)。
REPLAY_PASS = [
    ("广东星衍朗科技有限公司怎么样?光伏一站式服务实力解析", "原样含全名(基线已 PASS)"),
    ("广东星衍朗科技怎么样?光伏一站式服务实力解析", "丢「有限公司」(基线 FAIL)"),
    ("星衍朗科技有限公司靠谱吗?光伏一站式服务实力解析", "丢「广东」(基线 FAIL)"),
]


@pytest.mark.parametrize("title,note", REPLAY_PASS)
def test_the_three_replayed_titles_now_pass(title, note):
    verdict = assess_title_keyword_alignment(title, KEYWORD, brand_names=(BRAND,))
    assert verdict.aligned, "%s —— 仍被判 %s:%s" % (note, verdict.reason, title)


@pytest.mark.parametrize("title,note", REPLAY_PASS[1:])
def test_the_same_titles_still_fail_without_the_brand_name(title, note):
    """🔴 放宽的**唯一入口**是"关键词核 = 这家公司的注册名"。

    不给品牌名 ⇒ 老行为一字不变。没有这一条,我就证明不了放宽被锁在
    这家公司自己的名字上 —— 也就分不清"修好了"和"把守卫拆了"。
    """
    verdict = assess_title_keyword_alignment(title, KEYWORD)
    assert not verdict.aligned, "没有品牌名也放行了 —— 放宽没锁住入口:%s" % title


@pytest.mark.parametrize("title,note", [
    ("光伏一站式服务怎么选?行业解析", "只剩行业泛词"),
    ("广东某光伏科技有限公司怎么样?", "换了字号 —— 是另一家公司"),
    ("广东星衍朗贸易有限公司怎么样?", "换了行业词 —— 注册名不同"),
])
def test_a_different_company_still_fails(title, note):
    verdict = assess_title_keyword_alignment(title, KEYWORD, brand_names=(BRAND,))
    assert not verdict.aligned, "%s 竟然放行了:%s" % (note, title)


@pytest.mark.parametrize("keyword,title", [
    ("光伏板清洗服务", "广东星衍朗科技怎么样?一文看懂"),
    ("光伏逆变器选型", "广东星衍朗科技有限公司靠谱吗?"),
])
def test_the_brand_name_cannot_stand_in_for_a_different_keyword(keyword, title):
    """🔴 品牌名出现在标题里,**不能**替客户买的那个词交差。

    放宽的入口是「关键词核 == 这家公司的注册名」。少了这道等值判断,
    任何关键词只要标题里带上品牌名就会被判成对齐 —— 那正是本模块抬头
    写着要拒的「brand-only headline」,而且客户买的词一个字都没出现。
    这一条钉的就是那道等值判断本身。
    """
    verdict = assess_title_keyword_alignment(title, keyword, brand_names=(BRAND,))
    assert not verdict.aligned, (
        "标题只讲品牌、没讲客户买的词「%s」,却被放行(%s)" % (keyword, verdict.reason))


#: 反臂:换限定词一路**一个字都没放宽**。WO §3.1 点名 医用→家用 必须仍 FAIL。
ANTI_ARM = [
    ("医用隔离器推荐", "家用隔离器怎么选"),
    ("载货电梯维保", "乘客电梯维保怎么选"),
    ("工业清洗服务", "家用清洗服务怎么选"),
]


@pytest.mark.parametrize("keyword,title", ANTI_ARM)
@pytest.mark.parametrize("brands", [(), (BRAND,)], ids=["no-brand", "with-brand"])
def test_a_swapped_qualifier_still_fails(keyword, title, brands):
    """🔴 两种 brand_names 都跑:传了品牌名也不许把换限定词带过去。

    只跑「不传」那一臂的话,新参数把守卫打穿了我也看不见
    —— 样本要落在缺口上,而缺口正是新参数那条路。
    """
    verdict = assess_title_keyword_alignment(title, keyword, brand_names=brands)
    assert not verdict.aligned, "换限定词被放行了(brands=%r):%s → %s" % (
        brands, keyword, title)


def test_the_variant_table_is_closed_and_reviewable():
    """缩写全集是**可枚举、可复核**的,不是模糊匹配。

    逐条钉住:全名 / 去省 / 去组织形式 / 去行业词 —— 以及它**不含**
    任何短到会在无关标题里乱命中的串。
    """
    variants = company_name_variants(BRAND)
    assert "广东星衍朗科技有限公司" in variants
    assert "星衍朗科技有限公司" in variants   # 去省
    assert "广东星衍朗科技" in variants       # 去组织形式
    assert "星衍朗科技" in variants           # 去省 + 去组织形式
    assert all(len(v) >= 3 for v in variants), variants
    # 只剩行政区划或只剩行业词都不算这家公司
    assert "广东" not in variants and "科技" not in variants, variants
    # 🔴 **字号单独不算**:「广东星衍朗」是「广东星衍朗贸易有限公司」的前缀,
    #    那是另一家注册公司。行政区划 + 字号 + 行业合起来才唯一。
    #    这一条是被下面 test_a_different_company_still_fails 抓出来才加的
    #    —— 工单 §3.1 的后缀清单里原本写着「科技」。
    assert "星衍朗" not in variants and "广东星衍朗" not in variants, variants


def test_a_city_prefixed_name_also_abbreviates():
    """市/州靠结构标记认(「深圳市」带「市」字),不靠枚举全国城市。"""
    name = "深圳市星衍朗科技有限公司"
    variants = company_name_variants(name)
    assert "星衍朗科技有限公司" in variants, variants
    assert assess_title_keyword_alignment(
        "星衍朗科技怎么样?", name, brand_names=(name,)).aligned


def test_the_version_was_bumped():
    """判法变了,版本号要跟着变 —— 否则事后归因分不清是哪一版判的。

    (这条不是形式主义:`title_alignment_version` 会落进每一条 topic,
     生产上正是靠它把「07-23 引入」这件事查清楚的。)
    """
    from writing.title_keyword_alignment import TITLE_KEYWORD_ALIGNMENT_VERSION

    assert TITLE_KEYWORD_ALIGNMENT_VERSION.endswith("v3"), TITLE_KEYWORD_ALIGNMENT_VERSION


# ── c232' · 核 = 注册名 + 尾巴(复审 R3 那发毒打出来的面)────────────────

TAILED_KEYWORD = "广东星衍朗科技有限公司是做什么的"


@pytest.mark.parametrize("title,note", [
    ("星衍朗科技有限公司是做什么的?光伏一站式服务解析", "去省 + 同一尾巴"),
    ("广东星衍朗科技是做什么的?光伏一站式服务解析", "去组织形式 + 同一尾巴"),
    ("广东星衍朗科技有限公司是做什么的?一文看懂", "原样"),
])
def test_a_tailed_keyword_passes_with_the_same_tail(title, note):
    """核 = 注册名 + 尾巴时,「受控缩写 + **同一条尾巴**」放行。

    (复审实测:改之前「星衍朗科技有限公司是做什么的」FAIL
     keyword_scope_or_object_drift —— 缩写那一路只认"核逐字等于注册名"。)
    """
    verdict = assess_title_keyword_alignment(title, TAILED_KEYWORD, brand_names=(BRAND,))
    assert verdict.aligned, "%s 仍被判 %s:%s" % (note, verdict.reason, title)


@pytest.mark.parametrize("title,note", [
    ("广东星衍朗科技怎么样?光伏一站式服务解析", "缩写在、尾巴换了"),
    ("星衍朗科技有限公司靠谱吗?", "缩写在、尾巴换了(另一种)"),
    ("广东星衍朗科技有限公司光伏一站式服务解析", "缩写在、尾巴没了"),
])
def test_a_tailed_keyword_fails_when_the_tail_is_gone(title, note):
    """🔴 这一格是**复审 R3 那发毒的落点**:把入口从「相等」放宽成「包含」时,
    31 条判据全绿 —— 「== 是唯一入口」当时只住在注释里,没有任何判据钉它。

    客户买的是「这家公司**是做什么的**」。标题只提公司、不答那件事,
    就不是他买的那个词。尾巴一个字都不许变。
    """
    verdict = assess_title_keyword_alignment(title, TAILED_KEYWORD, brand_names=(BRAND,))
    assert not verdict.aligned, "%s 竟然放行(%s):%s" % (note, verdict.reason, title)


def test_the_tail_must_sit_right_after_the_abbreviation():
    """🔴 尾巴要**紧跟**缩写。飘到标题别处不算 —— 那就成了"两个串都出现过"。"""
    title = "广东星衍朗科技怎么样?顺便说说它是做什么的"
    verdict = assess_title_keyword_alignment(title, TAILED_KEYWORD, brand_names=(BRAND,))
    assert not verdict.aligned, verdict


def test_the_registered_name_must_be_a_prefix_of_the_core():
    """注册名不在核的开头时不走缩写路 —— 说不清尾巴是什么,就 fail-closed。"""
    weird = "光伏行业广东星衍朗科技有限公司"
    verdict = assess_title_keyword_alignment(
        "广东星衍朗科技怎么样?", weird, brand_names=(BRAND,))
    assert not verdict.aligned, verdict
