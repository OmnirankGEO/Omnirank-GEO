# -*- coding: utf-8 -*-
"""重抽保留版式锁 · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3 §6.1-5

`build_content_prompt` 收 `card_index` / `total` / `layout_role` / `role_label`,
而重抽路径原本**一个都不传** —— 重抽出来的那张静默丢掉版式与「第 i/N」标识。
榜单形态下 = 把「第 3/7 家」重抽成一张认不出序号的孤卡,而这种错只有肉眼能发现、
发现时图已经付过费了。
"""
from __future__ import annotations

import ast
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
REDRAW = REPO / "services" / "geo_douyin" / "redraw.py"
PROD = REPO / "services" / "geo_douyin" / "production_task.py"
TPL = REPO / "services" / "geo_douyin" / "card_templates.py"


def _content_prompt_call() -> ast.Call:
    tree = ast.parse(REDRAW.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "build_content_prompt"]
    assert calls, "重抽里找不到 build_content_prompt 调用 —— 判据失效"
    return calls[0]


@pytest.mark.parametrize("kw", ["card_index", "total", "layout_role", "role_label"])
def test_redraw_passes_every_layout_kwarg(kw):
    call = _content_prompt_call()
    names = {k.arg for k in call.keywords}
    assert kw in names, f"重抽没传 {kw} —— 重抽出来的卡会丢版式/序号"


def test_the_four_kwargs_actually_exist_on_the_target():
    """反向对照:判据得盯在真实签名上,函数改了签名要跟着红。"""
    tree = ast.parse(TPL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_content_prompt")
    args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    for kw in ("card_index", "total", "layout_role", "role_label"):
        assert kw in args, f"build_content_prompt 不再收 {kw},本锁需同步"


def test_layout_role_is_persisted_per_card():
    """落库要存 layout_role,否则重抽只能靠回推(老数据才该走回推)。"""
    src = PROD.read_text(encoding="utf-8")
    i = src.find("def _card_row(")
    assert i > 0
    block = src[i: i + 1400]
    assert '"layout_role"' in block, "每卡元数据没落 layout_role"
    assert '"role_label"' in block


def _legacy_post() -> dict:
    """老数据:每卡元数据里**没有** layout_role(那时还没落库)。"""
    cards = [{"idx": 1, "kind": "cover", "role_label": "封面", "prompt": ""}]
    cards += [{"idx": i, "kind": "content", "role_label": f"第{i}张",
               "prompt": ""} for i in range(2, 7)]
    cards += [{"idx": 7, "kind": "closing", "role_label": "收口", "prompt": ""}]
    return {
        "keyword": "深圳载货电梯哪家好", "city": "深圳", "industry_key": "电梯行业",
        "aspect_ratio": "3:4", "style_key": "", "cards": cards,
        "generation_meta": {"content": {
            "cover": {"title": "T", "subtitle": "S"},
            "closing": {"headline": "H", "summary": "M"},
            "cards": [{"entity": f"E{i}", "one_liner": "L", "points": ["a", "b"],
                       "metric": "", "caveat": "C"} for i in range(5)],
        }},
    }


def test_legacy_card_without_layout_role_still_gets_one():
    """🔴 行为锁:老数据没落 layout_role 时必须**回推**,不能留空。

    留空 = 重抽出来的卡没有版式约束 = 与其余几张各说各话。

    ⚠️ 这条原本写成"源码里出现过 plan_roles"型静态锁 —— 被变异 M3
    (删掉 import、留下调用)**活着穿过去了**:名字在调用处还在,静态锁照样绿,
    而运行时是 NameError。所以改成真的跑一次。
    """
    from services.geo_douyin.redraw import build_redraw_prompt
    from services.geo_douyin.series_plan import plan_roles
    post = _legacy_post()
    roles = [str(x.get("layout_role") or "") for x in plan_roles(len(post["cards"]))]
    prompt = build_redraw_prompt(post, 2)          # 第 3 张(内容卡)
    assert prompt, "重抽 prompt 生成失败"
    assert any(r and r in prompt for r in roles), (
        "老数据重抽没拿到任何版式描述 —— 回推路径断了")


def test_redraw_prompt_carries_the_series_progress_marker():
    """反向对照:组内进度「第 i/N」也必须在,否则榜单序号会丢。"""
    from services.geo_douyin.redraw import build_redraw_prompt
    post = _legacy_post()
    prompt = build_redraw_prompt(post, 2)
    n = len(post["cards"])
    assert str(n) in prompt, f"prompt 里找不到总张数 {n}"


def test_fallback_actually_yields_a_nonempty_role():
    """行为面:回推真的能给出非空版式(不是接了个永远返空的函数)。"""
    from services.geo_douyin.series_plan import plan_roles
    plan = plan_roles(7)
    roles = [str(x.get("layout_role") or "") for x in plan]
    assert len(roles) == 7 and all(roles), f"回推给出空版式:{roles}"
    assert len(set(roles)) >= 5, "七张里版式重样太多,组内一致性无从谈起"


def test_redraw_total_is_the_real_card_count_not_a_constant():
    """反向对照:`total` 不能写死 —— 写死的话 5 张的组会标成「第 i/7」。"""
    call = _content_prompt_call()
    kw = {k.arg: k.value for k in call.keywords}
    assert "total" in kw
    assert not isinstance(kw["total"], ast.Constant), "total 被写死成常量"
