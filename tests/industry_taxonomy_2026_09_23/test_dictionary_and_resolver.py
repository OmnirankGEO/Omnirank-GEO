# -*- coding: utf-8 -*-
"""WO_267 · 行业大类字典 SSOT 与两步判定。

客户反馈:光伏客户点亮调研被路由到建筑。那条品牌的 industry 列写的是「建筑装饰、装修和其他建筑业」,
光伏只在品牌名 / 备注 / 种子词里 —— 只看 industry 列时,路到建筑是「正确地算错」。
判据全部用 Review 09-23 从生产只读导出的**原串**(base64、不含品牌名;品牌上下文以
「其它列含光伏」这个布尔代入),不从截图抄。
"""
from __future__ import annotations

import copy
import json

import pytest

from services.industry_taxonomy import (
    OTHER_KEY, TAXONOMY_PATH, TaxonomyError, build_taxonomy, category_keys, load_taxonomy,
    resolve_industry, translate_legacy,
)

from . import _fixtures as fx


# ══════════════════════════════════════════════════════════════════
# 夹具是那一份生产导出
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,sha", sorted(fx.FILES.items()))
def test_fixture_file_is_the_prod_export(name, sha):
    assert fx.sha256_of(name) == sha, "夹具被改过 —— 下面冻结的读数只对原件成立"


# ══════════════════════════════════════════════════════════════════
# 字典自洽(每个校验条件各一组只让它决定结果的输入)
# ══════════════════════════════════════════════════════════════════

def _raw():
    return json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))


def test_real_dictionary_loads():
    tax = load_taxonomy()
    assert OTHER_KEY in category_keys() and len(tax.categories) >= 21


def _mutated(fn):
    raw = copy.deepcopy(_raw())
    fn(raw)
    return raw


def _cat(raw, key):
    return next(c for c in raw["categories"] if c["key"] == key)


@pytest.mark.parametrize("label,mutate", [
    ("大类 key 重复", lambda r: r["categories"].append(copy.deepcopy(_cat(r, "food")))),
    ("单字别名", lambda r: _cat(r, "healthcare")["aliases"].append("药")),
    ("一个别名属于两个大类", lambda r: _cat(r, "tech")["aliases"].append("食品")),
    ("other 挂了别名", lambda r: _cat(r, "other")["aliases"].append("通用")),
    ("旧名映射到两个大类", lambda r: _cat(r, "tech")["legacy_names"].append("餐饮")),
    ("一个调研 slug 被两个大类认领", lambda r: _cat(r, "tech").__setitem__(
        "research_industry_slug", _cat(r, "food")["research_industry_slug"])),
    ("缺兜底大类 other", lambda r: r.__setitem__(
        "categories", [c for c in r["categories"] if c["key"] != "other"])),
    ("legacy_secondary 指向不存在的大类", lambda r: r["legacy_secondary"].__setitem__(
        "房产家居", {"secondary": "no_such_key", "needs_review": True})),
])
def test_each_consistency_rule_rejects_its_own_violation(label, mutate):
    with pytest.raises(TaxonomyError):
        build_taxonomy(_mutated(mutate))
    build_taxonomy(_raw())      # 对照臂:原件照样过


# ══════════════════════════════════════════════════════════════════
# 六处旧名 → 大类(差分声明表,冻结:多一条少一条都红)
# ══════════════════════════════════════════════════════════════════

#: ①③⑤ 在代码里是**文本**(经 resolve_industry);②④⑥ 是**存量值 / 调研行名**(经读侧 translate_legacy)。
OLD_TEXT_NAMES = {
    # ① placement_service.INDUSTRY_KEYWORDS 旧键
    "房地产": "real_estate", "汽车": "auto", "教育": "education", "医疗": "healthcare",
    "金融": "finance", "科技": "tech", "食品": "food", "旅游": "travel", "母婴": "maternal",
    "时尚": "fashion_beauty", "装修": "construction",
    # ③ keyword_map 目标 = INDUSTRY_HEAD_FALLBACK 键(+ 新拆出的物流运输)
    "装修建材": "construction", "教育培训": "education", "医疗健康": "healthcare",
    "科技数码": "tech", "食品餐饮": "food", "时尚美妆": "fashion_beauty", "母婴亲子": "maternal",
    "旅游酒店": "travel", "金融理财": "finance", "法律商务": "legal_business",
    "电商零售": "ecommerce", "文娱游戏": "entertainment_media", "物流运输": "logistics",
    # ⑤ brand_field_suggester.INDUSTRY_PROMPT 的示例名
    "餐饮": "food", "GEO 服务": "geo_marketing", "美容/SPA": "fashion_beauty",
    "SaaS/CRM": "tech", "奶茶": "food",
}

#: (大类 key, 次类, needs_review)
OLD_STORED_NAMES = {
    # ② geo_research_industries 生产 19 行
    "电梯行业": ("equipment", None, False), "GEO 优化服务": ("geo_marketing", None, False),
    "汽车": ("auto", None, False), "教育培训": ("education", None, False),
    "房地产": ("real_estate", None, False), "时尚美妆": ("fashion_beauty", None, False),
    "文娱游戏": ("entertainment_media", None, False), "科技数码": ("tech", None, False),
    "法律商务": ("legal_business", None, False), "通用": ("other", None, True),
    "旅游酒店": ("travel", None, False), "企业服务": ("enterprise_services", None, False),
    "食品餐饮": ("food", None, False), "母婴亲子": ("maternal", None, False),
    "geo服务": ("geo_marketing", None, False), "金融理财": ("finance", None, False),
    "电商零售": ("ecommerce", None, False), "装修建材": ("construction", None, False),
    "医疗健康": ("healthcare", None, False),
    # ④ brands.industry_category 生产 12 值 + ⑥ db/models 旧表 11 键(并集)
    "其他": ("other", None, True), "自媒体": ("entertainment_media", None, False),
    "科技服务": ("tech", None, True), "房产家居": ("real_estate", "construction", True),
    "餐饮食品": ("food", None, False), "汽车出行": ("auto", None, False),
    "文旅娱乐": ("travel", "entertainment_media", True), "消费电子": ("tech", None, False),
    "餐饮": ("food", None, False), "金融服务": ("finance", None, False),
}


def test_every_old_text_name_routes_to_its_declared_category():
    got = {n: resolve_industry(n).category_key for n in OLD_TEXT_NAMES}
    assert got == OLD_TEXT_NAMES


def test_every_old_stored_name_translates_to_its_declared_category():
    got = {}
    for n in OLD_STORED_NAMES:
        r = translate_legacy(n)
        got[n] = (r.category_key, r.secondary, r.needs_review) if r else None
    assert got == OLD_STORED_NAMES


def test_the_19_research_rows_in_the_declaration_are_exactly_prod():
    prod = {r["name"] for r in fx.research_rows()}
    assert len(prod) == 19
    assert prod <= set(OLD_STORED_NAMES), "生产调研行有名字没进声明表:%r" % sorted(prod - set(OLD_STORED_NAMES))


def test_every_research_slug_exists_in_prod_or_is_a_pending_row():
    """锁:字典里每个调研行 slug,要么是生产现有行的 slug,要么是「待建行」且 = slug_for_name(行名)
    (种子脚本就按它建;不一致 ⇒ 建出来的行字典找不到)。"""
    from services.research_monitor.industry_registry import slug_for_name

    tax = load_taxonomy()
    prod_slugs = {r["slug"] for r in fx.research_rows()}
    bad = []
    for c in tax.categories:
        s = c.research_industry_slug
        if not s:
            bad.append((c.key, "没有调研行 slug"))
        elif s in prod_slugs:
            continue
        elif c.research_industry_name in tax.pending_research_rows and s == slug_for_name(c.research_industry_name):
            continue
        else:
            bad.append((c.key, s))
    assert not bad, bad
    assert sorted(tax.pending_research_rows) == ["农业农资", "工业制造", "新能源", "物流运输"]


# ══════════════════════════════════════════════════════════════════
# 光伏类夹具(生产原串 20 行)
# ══════════════════════════════════════════════════════════════════

def _resolve_fixture(r):
    return resolve_industry(r["industry"], context=["光伏"] if r["other_cols_pv"] else [])


#: 20 行逐条期望 (主类, 次类)。126「新能源汽车线缆」按 Review 裁定② 最长整词归汽车,不归新能源。
FIXTURE_EXPECTED = {
    25: ("industrial", "new_energy"), 30: ("construction", None), 126: ("industrial", "auto"),
    250: ("construction", "geo_marketing"), 254: ("construction", None), 348: ("construction", None),
    411: ("construction", None), 436: ("construction", None), 581: ("construction", None),
    586: ("construction", None), 615: ("construction", None), 633: ("construction", None),
    845: ("new_energy", "industrial"), 866: ("construction", None), 873: ("construction", None),
    926: ("construction", None), 960: ("construction", None), 968: ("construction", None),
    995: ("new_energy", "construction"), 997: ("construction", None),
}


def test_fixture_rows_route_exactly_as_declared():
    got = {r["id"]: (_resolve_fixture(r).category_key, _resolve_fixture(r).secondary)
           for r in fx.brand_fixtures()}
    assert got == FIXTURE_EXPECTED


def test_pv_storage_charging_or_pv_context_never_lands_in_construction():
    """凡含 光伏 / 储能 / 充电桩,或其它列含光伏:新能源必在主类或次类,主类永不为建筑。"""
    checked = 0
    for r in fx.brand_fixtures():
        if not (r["ind_pv"] or r["ind_es"] or r["ind_cp"] or r["other_cols_pv"]):
            continue
        res = _resolve_fixture(r)
        checked += 1
        assert res.category_key != "construction", (r["id"], res)
        assert "new_energy" in (res.category_key, res.secondary), (r["id"], res)
    assert checked >= 2, "夹具里带光伏 / 储能 / 充电桩标记的行没被检查到 —— 尺子取错了对象"


def test_customer_995_shape_context_promotes_new_energy_over_the_industry_column():
    """客户那条的形状:industry 列 = 建筑装饰,光伏只在其它列。"""
    row = next(r for r in fx.brand_fixtures() if r["id"] == 995)
    assert row["other_cols_pv"] is True
    with_ctx = resolve_industry(row["industry"], context=["光伏"])
    assert (with_ctx.category_key, with_ctx.secondary) == ("new_energy", "construction")
    # 对照臂:不给上下文 ⇒ 就是建筑(证明决定结果的是上下文,不是别的)
    assert resolve_industry(row["industry"]).category_key == "construction"


def test_bipv_primary_new_energy_secondary_construction():
    res = resolve_industry("光伏建筑一体化")
    assert (res.category_key, res.secondary) == ("new_energy", "construction")


@pytest.mark.parametrize("text,expected", [
    ("光伏电站运维", "new_energy"),
    ("储能系统集成", "new_energy"),
    ("农药化肥批发", "agriculture"),          # 旧 keyword_map 单字「药」会拉进医疗
    ("maintenance services", OTHER_KEY),      # 旧 get_industry_category 的 "ai" in "maintenance"
    ("食品制造业", "food"),                   # 门类词「制造业」不许压过具体的头
    ("制造业", "industrial"),
    ("批发业 / 西药批发", "healthcare"),
    ("互联网和相关服务 / 高端商务出行", "auto"),
])
def test_no_cross_category_substring_or_bigram_fallback(text, expected):
    assert resolve_industry(text).category_key == expected


def test_other_always_needs_review():
    assert resolve_industry("完全判不出的东西xyz").needs_review
    assert resolve_industry("随便", brand_override="other").needs_review
    assert translate_legacy("其他").needs_review


# ══════════════════════════════════════════════════════════════════
# 生产 285 条全量读数(冻结)
# ══════════════════════════════════════════════════════════════════

def _frozen_285():
    out = {}
    for line in (fx.HERE / "frozen_285_routing_2026-09-23.tsv").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("id\t"):
            continue
        rid, key, sec, rv = line.split("\t")
        out[int(rid)] = (key, None if sec == "-" else sec, rv == "1")
    return out


def test_all_285_prod_industries_route_exactly_as_frozen():
    frozen = _frozen_285()
    rows = fx.all_285()
    assert len(rows) == len(frozen) == 285
    got = {}
    for r in rows:
        res = resolve_industry(r.get("industry") or "", context=fx.context_of(r))
        got[r["id"]] = (res.category_key, res.secondary, res.needs_review)
    diff = {k: (frozen.get(k), got.get(k)) for k in set(frozen) | set(got) if frozen.get(k) != got.get(k)}
    assert not diff, "与冻结读数不一致(冻结, 现在):%r" % dict(sorted(diff.items())[:20])
