# -*- coding: utf-8 -*-
"""WO_282-C · 蒸馏选题入口必须把 industry_key 用「写语料时的同一个归一器」归一,few-shot 才能按行业命中语料。

病灶:`api/geo_douyin_api.py::api_distill_topics` 原样取 `req.industry_key` 往下派发,
`load_fewshot` 与 `detect_sample_taint` 都拿它对 douyin_adopted_corpus.industry_key 精确匹配;
而语料的 key 是飞轮 `services.media_entity_flywheel.normalize_industry_key` 的 13 个枚举(+ general)。
⇒ 前端送品牌档案里的自由文本(旧页与 WO_282-F1 都是)时,「同行业少样本」「行业内串味检查」一律落空。
下单链(`freeze_industry` / `card_templates._ikey`)早就归一,蒸馏入口一直没有。

判据(不连库、不起服务):调用**真的**入口函数,把副作用(品牌鉴权 / 余额预检 / 在飞占位 / 建任务行 / 派发)
换成桩,截下派发给后台的 industry_key。
  ① 必须等于 normalize_industry_key(原文)(下单链用的同一个归一器;空值仍落 general);
     别名命中的自由文本必须落在语料那 13 个枚举里。生产形状的样本「家居制造业 / 高端整木全屋定制/木作高定行业」
     与 tests/test_geo_douyin_industry_key_wiring_2026_08_06.py 用的是同一串。
     「深圳/全国 · AI搜索优化」这类别名表里没有的文本,归一器给的是 slug(不是枚举)—— 入口照样与下单链一致,
     不在本单改词表(改词表 = 改语料语义)。
  ② 反臂:把入口改回原样取值(源码文本替换,临时模块执行)⇒ 别名样本拿到原文、不在枚举里 ⇒ 红;
     同一套执行方式跑没改过的源码 ⇒ 绿(证明红来自那一行,不是临时模块本身)。
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "api" / "geo_douyin_api.py"

ALIASED = [  # (原文, 应归一到的语料枚举)
    ("酒店民宿", "tourism_hotel"),
    ("家居制造业 / 高端整木全屋定制/木作高定行业", "home_improvement"),
    ("  教育培训  ", "education"),
]
PASS_THROUGH = ["", "general", "深圳/全国 · AI搜索优化"]  # 只要求与归一器逐字一致

NEW_LINE = '        industry_key = normalize_industry_key(req.industry_key or "")\n'
OLD_LINE = '        industry_key = (req.industry_key or "").strip() or "general"\n'


def _path():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _real_module():
    _path()
    import api.geo_douyin_api as mod
    assert pathlib.Path(mod.__file__).resolve() == SRC.resolve(), f"import 到的不是本工作树的文件:{mod.__file__}"
    return mod


def _module_from_source(source: str, name: str):
    _path()
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    sys.modules[name] = mod
    try:
        exec(compile(source, str(SRC), "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


def _dispatched_key(mod, monkeypatch, raw: str) -> str:
    """跑一次真入口,返回派发给后台任务的 industry_key。"""
    import db.geo_douyin_db as ddb
    import middleware.billing as billing
    import services.geo_douyin.distill_task as dt

    captured = {}

    async def _name(brand_id):
        return "测试品牌"

    async def _balance(user_id, code):
        return None

    monkeypatch.setattr(mod, "require_brand_access", lambda request, brand_id: None)
    monkeypatch.setattr(mod, "fetch_brand_display_name", _name)
    monkeypatch.setattr(billing, "check_balance_only", _balance)
    monkeypatch.setattr(dt, "try_acquire_inflight", lambda brand_id: True)
    monkeypatch.setattr(dt, "release_inflight", lambda brand_id: None)
    monkeypatch.setattr(dt, "stale_after_seconds", lambda: 600)
    monkeypatch.setattr(dt, "dispatch_distill", lambda **kw: captured.update(kw))
    monkeypatch.setattr(ddb, "reap_stale_distill_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(ddb, "create_distill_task", lambda **kw: 4242)
    req = mod.DistillTopicsRequest(brand_id=7, keywords=["测试词"], industry_key=raw)
    request = types.SimpleNamespace(state=types.SimpleNamespace(user={"user_id": 1, "is_admin": False}))
    out = asyncio.run(mod.api_distill_topics(req, request))
    assert out.get("task_id") == 4242 and "industry_key" in captured, (out, captured)
    return captured["industry_key"]


def problems(mod, monkeypatch) -> list:
    _path()
    from services.media_entity_flywheel import _INDUSTRY_ALIASES, normalize_industry_key

    enums = set(_INDUSTRY_ALIASES)
    out = []
    for raw, want in ALIASED:
        got = _dispatched_key(mod, monkeypatch, raw)
        if got != want or got not in enums:
            out.append(f"别名样本 {raw!r} ⇒ 派发 {got!r}(应为语料枚举 {want!r})")
    for raw in PASS_THROUGH:
        got = _dispatched_key(mod, monkeypatch, raw)
        if got != normalize_industry_key(raw):
            out.append(f"样本 {raw!r} ⇒ 派发 {got!r}(应与归一器一致:{normalize_industry_key(raw)!r})")
    return out


def test_entry_dispatches_the_normalized_industry_key(monkeypatch):
    assert problems(_real_module(), monkeypatch) == []


def test_arm_raw_passthrough_turns_red(monkeypatch):
    source = SRC.read_text(encoding="utf-8")
    assert source.count(NEW_LINE) == 1, "反臂没下成:入口那一行不是恰好 1 处"
    old = _module_from_source(source.replace(NEW_LINE, OLD_LINE, 1), "wo282c_raw_arm")
    bad = problems(old, monkeypatch)
    from services.media_entity_flywheel import normalize_industry_key
    # 旧写法 = 原文去空白、空了落 general;凡与归一器结果不同的样本都该红(3 条别名样本 + 那条 slug 样本)
    want_red = [raw for raw, _ in ALIASED] + [raw for raw in PASS_THROUGH
                                               if ((raw.strip() or "general") != normalize_industry_key(raw))]
    assert len(bad) == len(want_red) and all(any(repr(r) in b for b in bad) for r in want_red), bad
    same = _module_from_source(source, "wo282c_control")
    assert problems(same, monkeypatch) == []
