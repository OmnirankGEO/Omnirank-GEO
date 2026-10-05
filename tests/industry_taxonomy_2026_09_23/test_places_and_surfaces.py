# -*- coding: utf-8 -*-
"""WO_267 · 五处(+第六处)同键,以及对外接口面。不连库。

「同键」按**行为**锁:派生表必须就是字典算出来的那一份(改回手写副本 ⇒ 红),
各处对同一行业串必须落同一个大类 key。
"""
from __future__ import annotations

import asyncio
import types

import pytest
from fastapi import HTTPException

from services.industry_taxonomy import (
    category_fields, category_keys, display_name, filter_values, get_category, head_fallback_keys,
    load_taxonomy, media_keywords_for, media_keywords_table, resolve_industry,
)

from . import _fixtures as fx


# ══════════════════════════════════════════════════════════════════
# ① 媒体词 · ③ 头部白名单 —— 派生,不许回到手写副本
# ══════════════════════════════════════════════════════════════════

def test_place1_industry_keywords_is_the_dictionary_table():
    from services.placement_service import PlacementService

    assert PlacementService.INDUSTRY_KEYWORDS == media_keywords_table()
    assert set(PlacementService.INDUSTRY_KEYWORDS) <= set(category_keys()), "键不是大类 key(同键)"


def test_place1_keywords_follow_the_resolved_category():
    from services.placement_service import PlacementService

    svc = PlacementService.__new__(PlacementService)
    kws = svc._extract_industry_keywords("光伏组件制造")
    assert kws and set(kws) == set(media_keywords_table()["new_energy"])
    # 品牌上下文同样生效:industry 列写建筑,品牌名带光伏 ⇒ 新能源的词在里面
    kws_ctx = svc._extract_industry_keywords("建筑装饰、装修和其他建筑业", brand={"name": "某光伏科技"})
    assert set(media_keywords_table()["new_energy"]) <= set(kws_ctx)


def test_place3_head_fallback_keys_equal_the_dictionary():
    from services.placement_service import INDUSTRY_HEAD_FALLBACK

    assert set(INDUSTRY_HEAD_FALLBACK) == set(head_fallback_keys())
    # 物流运输从汽车拆出(Review 裁定①):当天原样复制,待策展
    assert INDUSTRY_HEAD_FALLBACK["物流运输"] == INDUSTRY_HEAD_FALLBACK["汽车"]


@pytest.mark.parametrize("raw,expect_key", [
    ("装修建材", "装修建材"),            # 精确命中
    ("跨境物流运输、货代", "物流运输"),    # 原来被 keyword_map 算进汽车
    ("出行服务", "汽车"),                # 05-18 BUG 8 修的那条仍然成立
    ("光伏组件制造", None),              # 新能源没挂白名单 ⇒ 空,不许借邻居
    ("电线电缆制造", None),
    ("农药化肥", None),                  # 旧单字「药」会给医疗头部
])
def test_place3_head_fallback_goes_through_the_dictionary(raw, expect_key):
    from services.placement_service import INDUSTRY_HEAD_FALLBACK, _resolve_industry_head_fallback

    got = _resolve_industry_head_fallback(raw, "")
    assert got == (list(INDUSTRY_HEAD_FALLBACK[expect_key]) if expect_key else [])


# ══════════════════════════════════════════════════════════════════
# ④ 真实写入方 get_industry_category(第六处)
# ══════════════════════════════════════════════════════════════════

def test_place4_writer_returns_new_keys_for_every_prod_industry():
    from db.models import get_industry_category

    keys = set(category_keys())
    for r in fx.all_285():
        k = get_industry_category(r.get("industry") or "")
        assert k in keys, (r["id"], k)
        assert k == resolve_industry(r.get("industry") or "").category_key


def test_place4_writer_uses_brand_context():
    from db.models import get_industry_category

    assert get_industry_category("建筑装饰、装修和其他建筑业") == "construction"
    assert get_industry_category("建筑装饰、装修和其他建筑业", brand={"name": "某光伏科技"}) == "new_energy"


def test_place4_hand_written_category_table_is_gone():
    import db.models as m

    assert not hasattr(m, "INDUSTRY_CATEGORIES"), "db/models 里的手写 11 类表回来了(第二份名字表)"


# ══════════════════════════════════════════════════════════════════
# ⑤ 字段补齐器:industry_category 四处同步 + 闭集 + 代理端跳过
# ══════════════════════════════════════════════════════════════════

def test_place5_industry_category_registered_in_all_four_tables():
    from agents import brand_field_suggester as s

    for table in (s.SUPPORTED_FIELDS, s.FIELD_META, s.FIELD_PROMPTS, s.FIELD_RESULT_KEY):
        assert "industry_category" in table


def test_place5_legacy_value_counts_as_unconfirmed_new_key_as_confirmed():
    from agents import brand_field_suggester as s

    assert s._field_missing({"industry_category": "房产家居"}, {}, "industry_category") is True
    assert s._field_missing({"industry_category": "new_energy"}, {}, "industry_category") is False
    assert s._field_missing({}, {}, "industry_category") is True


def test_place5_card_uses_dictionary_first_and_writes_the_key_through_my_clients():
    from agents import brand_field_suggester as s

    card = asyncio.run(s._suggest_industry_category(
        brand={"id": 629, "name": "某光伏科技"}, industry="建筑装饰、装修和其他建筑业", business="",
        brand_name="某光伏科技", company_name="", brand_id=629))
    assert card["needs_confirmation"] is True
    assert card["action_payload"] == {"endpoint": "/api/my-clients/629", "method": "PUT",
                                      "body": {"industry_category": "new_energy"}}
    assert card["cancel_label"] == "❌ 取消" and card["link_action"]["path"] == "/my-clients/629"
    assert "130" not in card["description"], "大类是从固定清单里选,不该提示付费联网查"


@pytest.mark.parametrize("llm_answer,expect_key", [
    ("new_energy", "new_energy"),     # 闭集内 ⇒ 采纳
    ("新能源", None),                 # 回中文名 ⇒ 不采纳
    ("solar_power", None),            # 自造 key ⇒ 不采纳
    ("other", None),                  # 「其他」不是一个大类答案
])
def test_place5_llm_step_is_closed_set(monkeypatch, llm_answer, expect_key):
    from agents import brand_field_suggester as s

    seen = {}

    async def fake_llm(prompt):
        seen["prompt"] = prompt
        return {"suggested_industry_category": llm_answer, "confidence": "中", "needs_more_info": False}

    monkeypatch.setattr(s, "_llm_infer", fake_llm)
    out = asyncio.run(s._suggest_industry_category(
        brand={"id": 1, "name": "某公司"}, industry="完全判不出的东西xyz", business="",
        brand_name="某公司", company_name="", brand_id=1))
    # 选项清单来自字典(不是手写):每个非 other 的大类 key 都在 prompt 里
    for c in load_taxonomy().categories:
        if c.key != "other":
            assert c.key in seen["prompt"]
    if expect_key:
        assert out["action_payload"]["body"] == {"industry_category": expect_key}
    else:
        assert out["needs_confirmation"] is False and out["follow_up_question"]


def test_place5_agent_mode_still_skips(monkeypatch):
    """(e) 代理端(user_mode='agent')`ensure_brand_fields` 仍直接放行,连品牌都不去取。"""
    from agents import brand_field_suggester as s

    fetched = []

    async def spy(ctx):
        fetched.append(getattr(ctx.deps, "user_mode", None))
        return None

    # (ensure_brand_fields 自己 try/except 了取品牌那一步 —— 所以这里记「有没有去取」,不靠抛异常)
    monkeypatch.setattr(s, "_fetch_brand", spy)
    ctx = types.SimpleNamespace(deps=types.SimpleNamespace(user_mode="agent"))
    assert asyncio.run(s.ensure_brand_fields(ctx, required=["industry_category"])) is None
    assert fetched == [], "代理端不该去取品牌"
    # 对照臂:C 端会走到取品牌那一步(取不到品牌 ⇒ 返回建品牌引导,不是 None)
    ctx_c = types.SimpleNamespace(deps=types.SimpleNamespace(user_mode="c"))
    assert asyncio.run(s.ensure_brand_fields(ctx_c, required=["industry_category"])) is not None
    assert fetched == ["c"]


# ══════════════════════════════════════════════════════════════════
# 谁开了级 0(行业路由前段)—— 声明表冻结(Review Q3 配套①:差分表单列「谁开了级 0」)
# ══════════════════════════════════════════════════════════════════

#: (文件, 调用的入口, `taxonomy=` 实参原文)。多一条少一条都红。
#: 判据直接调解析函数、自己传 taxonomy=True —— 某个端点**忘了开**,别的格子都看不见,只有这张表看得见。
WHO_OPENED_LEVEL0 = sorted([
    # 付费点亮调研三端点 + 历轮查询:开
    ("api/research_selfserve_api.py", "preview_resolve", "True"),               # /draft
    ("api/research_selfserve_api.py", "preview_resolve", "True"),               # /self-serve 冻结前预览
    ("api/research_selfserve_api.py", "resolve_or_create_industry", "True"),    # /active-task
    ("api/research_selfserve_api.py", "resolve_or_create_industry", "True"),    # /rounds
    # 包 B:下单冻结带品牌 ⇒ 开;执行期兜底(冻结件读不出时)不带品牌 ⇒ 不开(v1.0 口径)
    ("api/geo_douyin_api.py", "freeze_industry", "True"),
    ("services/geo_douyin/ranking_router.py", "resolve_readonly", "<absent>"),
    # 主榜:带 brand_id(已鉴权)才开
    ("services/media_effectiveness_board.py", "resolve_readonly", "brand is not None"),
    # 透传:把调用方的开关原样往下传
    ("services/industry_canonical.py", "resolve_readonly", "taxonomy"),
    ("services/research_monitor/industry_resolver.py", "resolve_or_create_industry", "taxonomy"),
    # 包 B 的 LLM 归并(freeze_industry 内部):不开 —— 它只认 resolved_by=llm,开了会被前段短路
    ("services/industry_canonical.py", "resolve_or_create_industry", "<absent>"),
])


def test_who_opened_level0_is_exactly_the_declared_set():
    import ast
    import subprocess

    from . import _fixtures

    root = _fixtures.HERE.parents[1]
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
                           capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    callees = {"resolve_or_create_industry", "preview_resolve", "resolve_readonly", "freeze_industry"}
    got = []
    for f in files:
        if not f or f.startswith(("tests/", "scripts/", "docs/")):
            continue
        try:
            text = (root / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        # [ONESHOT 提速 4] 只解析文本里出现了四个函数名之一的文件:调用 `x(...)` / `m.x(...)` 的名字必然
        #   原样出现在源码里,没出现就不可能命中。原先全仓 ~1300 个 .py 逐个 ast.parse ≈ 6s。
        if not any(c in text for c in callees):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name in callees:
                    kw = next((k for k in node.keywords if k.arg == "taxonomy"), None)
                    got.append((f, name, ast.unparse(kw.value) if kw else "<absent>"))
    assert sorted(got) == WHO_OPENED_LEVEL0, "多 %r 少 %r" % (
        sorted(set(got) - set(WHO_OPENED_LEVEL0)), sorted(set(WHO_OPENED_LEVEL0) - set(got)))


# ══════════════════════════════════════════════════════════════════
# 对外接口面
# ══════════════════════════════════════════════════════════════════

def test_taxonomy_endpoint_gives_keys_names_subcategories_only():
    from api.industry_taxonomy_api import get_industry_taxonomy

    payload = asyncio.run(get_industry_taxonomy())
    assert [c["key"] for c in payload["categories"]] == list(category_keys())
    leaked = {k for c in payload["categories"] for k in c} - {"key", "name", "subcategories"}
    assert not leaked, "路由内部细节出了后端:%r" % leaked


@pytest.mark.parametrize("value,ok", [
    ("new_energy", "new_energy"), ("", ""), ("  ", ""),
    ("房产家居", None), ("随便写的", None),
])
def test_brand_write_accepts_only_new_keys(value, ok):
    from api.brand_api import _validated_industry_category_or_422

    if ok is None:
        with pytest.raises(HTTPException) as ei:
            _validated_industry_category_or_422(value)
        assert ei.value.status_code == 422
    else:
        assert _validated_industry_category_or_422(value) == ok


def test_read_side_translation():
    assert category_fields("房产家居") == {"industry_category_key": "real_estate",
                                           "industry_category_name": get_category("real_estate").name}
    assert category_fields("new_energy")["industry_category_key"] == "new_energy"
    assert category_fields("认不出的值") == {"industry_category_key": None, "industry_category_name": "认不出的值"}
    assert category_fields(None) == {"industry_category_key": None, "industry_category_name": None}
    assert display_name("科技服务") == get_category("tech").name
    fv = filter_values("科技服务")
    assert "tech" in fv and "科技服务" in fv, "按大类筛选要同时匹配新 key 与存量旧值"


def test_research_selfserve_public_resolution_carries_category():
    from api.research_selfserve_api import _resolved_public

    pub = _resolved_public({"industry_name": "新能源", "industry_key": "k", "is_new": True,
                            "resolved_by": "taxonomy", "user_industry_raw": "x",
                            "category": {"key": "new_energy", "name": "新能源"}})
    assert pub["category"] == {"key": "new_energy", "name": "新能源"}


def test_recommend_v2_brand_context_requires_access(monkeypatch):
    """v2 的 brand_id 一直不鉴权(灰度/去重用)。读品牌字段当上下文之前必须过 require_brand_access;
    无权 ⇒ None(只按行业串判),**不 403**(不改公开端点的既有行为)。"""
    import api.publish_api as pa

    def deny(request, brand_id, allow_null=False):
        raise HTTPException(status_code=403, detail="no")

    monkeypatch.setattr(pa, "require_brand_access", deny)
    assert pa._industry_brand_if_accessible(object(), 123) is None
    assert pa._industry_brand_if_accessible(object(), None) is None
