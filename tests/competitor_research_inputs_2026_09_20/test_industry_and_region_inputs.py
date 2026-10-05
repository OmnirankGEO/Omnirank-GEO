# -*- coding: utf-8 -*-
"""WO_251 · 竞品调研的两个输入都取错了。

生产日志原句(客户揭阳雅栖酒店):
`[竞品调研] 客户=揭阳滨江南路雅栖酒店 行业=餐饮食品 地区=揭阳环 …`
⇒ 多源交叉验证真的去搜了「**揭阳环** **餐饮食品** 服务商 知名」——
一个不存在的地方 + 一个不是这家客户的行当。

两个输入各错各的:
  ① 行业取**报价快照**(报价 287 是「餐饮食品」,前端分类 bug),
     而品牌现值是「酒店住宿 / 商务出行 / 中端连锁酒店」;
  ② 地区被 `normalize_city` 按「市」切坏 —— 街名「滨江南路」里的市被当成了市级后缀。

🔴 两个错**都不会报错**,只会让结果悄悄偏掉;日志里只写结果不写出处,
   于是「行业=餐饮食品」看起来像客户真的是做餐饮的。
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.diagnosis_question_quality import (      # noqa: E402
    distill_trade, normalize_city,
)

SERVER = REPO / "server.py"


# ══════════════════════════════════════════════════════════════════
# ① 地区:街名里的「市」不许被当成市级后缀
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    # 工单点名的四个(前两个是生产实测的错值)
    ("揭阳滨江南路雅栖酒店", "揭阳"),
    ("广东省揭阳市榕城区滨江南路", "揭阳"),
    ("揭阳", "揭阳"),
    ("揭阳市", "揭阳"),
])
def test_the_four_reported_inputs_all_resolve_to_the_city(raw, expected):
    """🔴 工单逐字:四个输入都应得「揭阳」。前两个改前分别是「揭阳环」「环市」。"""
    assert normalize_city(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("广东省深圳市龙岗区", "深圳"),          # 工单点名:既有行为不变
    ("贵州省遵义市仁怀市", "仁怀"),          # 取最后一个市级块(县级市更贴客户自称)
    ("贵州贵阳", "贵阳"),                    # 无后缀省市连写
    ("广东省深圳市南山区粤海街道", "深圳"),  # 「街道」是行政区划形态,不该切坏
    ("北京市朝阳区建国路", "北京"),
])
def test_existing_behaviour_is_unchanged(raw, expected):
    """🔴 反向对照:改动只该影响"街名含市"那一类,别的一个都不许动。

    少了这一条,把 `normalize_city` 改成"永远返回前两个字"也能让上面全绿。
    """
    assert normalize_city(raw) == expected


def test_a_name_that_starts_with_a_street_word_is_not_cut_to_nothing():
    """🔴 「大道科技」这类以街名词开头的 —— 切完是空,那就不是地址,保持原样。

    这一条钉的是**切点守卫**:没有它,凡是名字里带「道/路/街」的都会被切成空串。
    """
    assert normalize_city("大道科技") == "大道科技"


def test_the_street_cut_actually_happens():
    """🔴 仪器自检:确认"切街道段"这件事真的发生了,而不是碰巧另一条路给对了答案。

    直接验那个正则:它必须在「揭阳滨江南路…」里把切点定在「环」上(下标 2),
    而不是下标 0(那样切完什么都不剩,等于没切)。
    """
    from services.diagnosis_question_quality import _STREET_TAIL_RE
    m = _STREET_TAIL_RE.search("揭阳滨江南路雅栖酒店")
    assert m is not None, "街道段正则没命中 —— 那上面那些绿是别的路径给的"
    assert m.start("name") == 2, (
        "切点在下标 %d(应为 2)—— 非贪婪把城市名也吃进去了" % m.start("name"))


# ══════════════════════════════════════════════════════════════════
# ② 行业:品牌现值优先,报价快照兜底
# ══════════════════════════════════════════════════════════════════

def _resolve():
    import server
    return server._resolve_competitor_industry


def test_the_brand_industry_wins_over_the_quote_snapshot():
    """🔴 本单的全部意义:报价快照是"下单那一刻选了什么",不是"客户是做什么的"。"""
    got, src = _resolve()("餐饮食品", "酒店住宿 / 商务出行 / 中端连锁酒店")
    assert got == "酒店住宿 / 商务出行 / 中端连锁酒店"
    assert src == "brand"


@pytest.mark.parametrize("brand_value", [None, "", "   "])
def test_an_empty_brand_industry_does_not_wipe_the_quote(brand_value):
    """🔴 反向对照:品牌现值为空时**必须**退回报价快照。

    空不算"现值" —— 覆盖过去就是把"取错了"换成"什么都没有",那更糟。
    少了这一条,把函数写成"永远返回品牌值"也能让上面那条绿。
    """
    got, src = _resolve()("餐饮食品", brand_value)
    assert got == "餐饮食品"
    assert src == "quote"


def test_both_empty_yields_empty_and_says_quote():
    got, src = _resolve()(None, None)
    assert got == "" and src == "quote"


# ══════════════════════════════════════════════════════════════════
# ③ 接线与日志出处
# ══════════════════════════════════════════════════════════════════

def _handler():
    tree = ast.parse(io.open(SERVER, encoding="utf-8").read(), "server.py")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == "api_research_competitors":
            return n
    return None


def test_the_handler_uses_the_resolver_and_reads_the_brand_row():
    """🔴 抽了函数还要真的用上它 —— 写了不用等于没写。"""
    fn = _handler()
    assert fn is not None, "找不到 api_research_competitors"
    src = ast.unparse(fn)
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_resolve_competitor_industry"]
    assert len(calls) == 1, "解析函数调 %d 次(应 1 次)" % len(calls)
    assert "SELECT industry FROM brands" in src, "没有去读品牌现值"

    # 🔴 **只钉「调了」不够**:注毒写成
    #    `industry, src = (quote.get('industry') or '', 'quote') or _resolve(...)`
    #    —— 调用**textually 还在**,这条锁照样绿,**而解析函数已经不决定任何事**。
    #    今天第三次同形(前两次:补救闸、铁律锁)。
    #    所以钉:`industry` 这个名字必须**直接**由那次调用赋值,不许被包在别的表达式里。
    assigned = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == "_resolve_competitor_industry"
        and any("industry" in ast.unparse(t) for t in n.targets)
    ]
    assert assigned, (
        "industry 不是**直接**由 _resolve_competitor_industry 赋值的 —— "
        "它可能被包在 `... or 解析(...)` 里,那样解析根本不决定结果")


def test_the_log_line_says_where_each_input_came_from():
    """🔴 日志要说**出处**,不只说结果。

    原来那行只写「行业=餐饮食品 地区=揭阳环」—— 两个值都是错的,
    而光看这行**没有任何迹象**表明它们是从哪来的、是不是兜底来的。
    出处字段就是下次能一眼认出来的那个东西。
    """
    fn = _handler()
    logs = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "info"]
    target = [ast.unparse(n) for n in logs if "竞品调研" in ast.unparse(n)]
    assert target, "找不到竞品调研那行日志"
    line = target[0]
    assert "industry_source" in line, "日志没带行业来源"
    assert "region_source" in line, "日志没带地区来源"


@pytest.mark.parametrize("source", ["cities", "keyword", "distilled"])
def test_every_region_fallback_path_labels_itself(source):
    """🔴 三条地区兜底路各自打标 —— 少一条,那条路走过就查不出来。"""
    src = ast.unparse(_handler())
    assert ('region_source = "%s"' % source) in src or (
        "region_source = '%s'" % source) in src, (
        "地区来源 %r 这条路没有打标" % source)


# ══════════════════════════════════════════════════════════════════
# ④ 与 A 的 WO_250 对表用的读数(不是断言行为,是把事实钉住)
# ══════════════════════════════════════════════════════════════════

def test_hotel_shaped_industries_distil_to_something_searchable():
    """🔴 「酒店/民宿/住宿」蒸馏成什么 —— 与 A 的 WO_250 分类表对表用。

    客户实际值蒸馏成「中端连锁酒店」,是个能搜的词;其余几个原样透传。
    这一条不主张 `distill_trade` 该怎么改,只把**当前事实**钉住:
    哪天蒸馏口径变了,对表的前提也就变了,这里会红。
    """
    assert distill_trade("酒店住宿 / 商务出行 / 中端连锁酒店") == "中端连锁酒店"
    for raw in ("酒店", "民宿", "住宿"):
        got = distill_trade(raw)
        assert got, "%r 蒸馏成了空 —— 检索词会少一截" % raw
