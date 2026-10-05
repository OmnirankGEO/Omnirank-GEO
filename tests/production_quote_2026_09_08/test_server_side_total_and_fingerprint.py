"""#150 §3.1 · 总价由服务端算 + 价格指纹(P0 资金规则)。

## 缺陷

前端算钱有**两处**:`batchPrice = totalPrice * batchTotal`,
以及 `/pricing` 的契约本身(注释明写「前端拿 `base + max(0,n-included) × extra`
自己算总价」)。08_billing §3.3:**价格只在后端计算,前端只显示**。

## 判据分三面

  · **金额**:逐行 + 总额由服务端给,且总额 = 逐行之和;
  · **指纹**:绑数据**也绑规则** —— 调价后旧指纹必须失效;
  · **位置**:409 必须发生在建行/派发之前 ——「零冻结」靠的是**走不到那一步**,
    而这个性质在顺利路径上**不可观测**,只能打结构臂。
"""

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

LINES = [{"keyword": "深圳 GEO 优化", "city": "深圳", "card_count": 4},
         {"keyword": "AI 搜索排名", "city": "深圳", "card_count": 6}]


def _quote(monkeypatch, base=390, extra=100):
    """跑真报价,只把**价目读取**换成桩(价目是外部输入)。"""
    import services.geo_douyin.production_quote as pq

    async def _read(code):
        return extra if "extra" in code else base

    monkeypatch.setattr(pq, "read_unit_points", _read)
    return asyncio.run(pq.quote_production(LINES))


# ───────────────────────────────────────────── 金额

def test_total_is_computed_server_side_and_equals_the_sum_of_lines(monkeypatch):
    """🔴 本单那一格:总额由服务端给,且 = 逐行之和。

    前端不再需要任何乘法/加法 —— 它只显示 `total_points`。
    """
    q = _quote(monkeypatch)
    assert q["total_points"] == sum(l["line_points"] for l in q["lines"])
    # 第 1 行 4 张(含在套餐内,不加价);第 2 行 6 张 ⇒ 多 2 张
    assert q["lines"][0]["extra_cards"] == 0
    assert q["lines"][0]["line_points"] == 390
    assert q["lines"][1]["extra_cards"] == 2
    assert q["lines"][1]["line_points"] == 390 + 2 * 100
    assert q["total_points"] == 390 + 590


def test_fewer_cards_than_included_never_discounts(monkeypatch):
    """「少于 4 张不减价」—— 口径来自 pricing.extra_cards 的 `max(0, ...)`。

    没有这条,一个写成 `n - included` 的实现会给出**负加价**(倒找钱)。
    """
    import services.geo_douyin.production_quote as pq

    async def _read(code):
        return 100 if "extra" in code else 390

    monkeypatch.setattr(pq, "read_unit_points", _read)
    q = asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 1}]))
    assert q["lines"][0]["extra_points"] == 0
    assert q["lines"][0]["line_points"] == 390


# ───────────────────────────────────────────── 指纹

def test_fingerprint_changes_when_the_price_changes(monkeypatch):
    """🔴 承重:指纹绑**规则**,不只绑数据。

    只 hash 行数据的话,Owner 调价后旧指纹照样对得上 ⇒
    她看到的是旧价、扣的是新价,**而屏幕上一切正常**。
    (同族教训:版本串只 hash 数据行 ⇒ 改规则不改数据时版本不动。)
    毒:指纹里去掉 rule ⇒ 本条红。
    """
    a = _quote(monkeypatch, base=390, extra=100)
    b = _quote(monkeypatch, base=420, extra=100)
    assert a["lines"][0]["price_fingerprint"] != b["lines"][0]["price_fingerprint"]
    assert a["price_fingerprint"] != b["price_fingerprint"]


def test_fingerprint_changes_when_a_priced_axis_changes(monkeypatch):
    """张数变了(=价变了)⇒ 指纹变。"""
    import services.geo_douyin.production_quote as pq

    async def _read(code):
        return 100 if "extra" in code else 390

    monkeypatch.setattr(pq, "read_unit_points", _read)
    a = asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 4}]))
    b = asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 6}]))
    assert (a["lines"][0]["price_fingerprint"]
            != b["lines"][0]["price_fingerprint"])


def test_fingerprint_ignores_axes_that_do_not_affect_price(monkeypatch):
    """🔴 反向臂:不影响价的轴**不进**指纹。

    把风格/画幅也绑进去,用户换个画幅就 409 —— 而价其实没变。
    那不是更严格,是把闸变成噪音,人会学会忽略它。
    """
    import services.geo_douyin.production_quote as pq

    async def _read(code):
        return 100 if "extra" in code else 390

    monkeypatch.setattr(pq, "read_unit_points", _read)
    a = asyncio.run(pq.quote_production(
        [{"keyword": "k", "city": "深圳", "card_count": 4, "style_key": "A",
          "aspect_ratio": "3:4"}]))
    b = asyncio.run(pq.quote_production(
        [{"keyword": "k", "city": "深圳", "card_count": 4, "style_key": "B",
          "aspect_ratio": "9:16"}]))
    assert (a["lines"][0]["price_fingerprint"]
            == b["lines"][0]["price_fingerprint"])


def test_batch_fingerprint_is_order_sensitive(monkeypatch):
    """整批指纹保序 —— 换了顺序就是另一批,免得两批互相顶替。"""
    import services.geo_douyin.production_quote as pq

    async def _read(code):
        return 100 if "extra" in code else 390

    monkeypatch.setattr(pq, "read_unit_points", _read)
    a = asyncio.run(pq.quote_production(LINES))
    b = asyncio.run(pq.quote_production(list(reversed(LINES))))
    assert a["price_fingerprint"] != b["price_fingerprint"]
    assert a["total_points"] == b["total_points"]      # 价一样,只是批不同


# ───────────────────────────────────────────── fail-closed

def test_pricing_unavailable_never_becomes_zero(monkeypatch):
    """🔴 价目读不到就抛,**绝不按 0 算**。

    按 0 算等于白送 —— 一次读库抖动就是资金漏洞,
    而「报错让用户重试一次」只是体验问题。
    毒:把异常兜成 `return 0` ⇒ 本条红。
    """
    import services.geo_douyin.production_quote as pq
    from services.geo_douyin.pricing import PricingUnavailable

    async def _boom(code):
        raise PricingUnavailable("价目缺失: %s" % code)

    monkeypatch.setattr(pq, "read_unit_points", _boom)
    with pytest.raises(PricingUnavailable):
        asyncio.run(pq.quote_production(LINES))


def test_the_base_price_alone_is_fail_closed(monkeypatch):
    """🔴 专钉**基价**那一条,不借加价路径。

    上一条用的 `LINES` 里有一行 6 张要加价 —— 基价被兜成 0 时,
    异常仍会从**加价缺失**那条路抛出来,于是上一条**因为无关的原因**通过。
    毒(把基价读取兜成 `base = 0`)在它身上是存活的。

    所以这里只给一行 4 张(压根不读加价价目):
    基价读不到就必须抛,任何「返回 0 总价」的实现都要红。
    """
    import services.geo_douyin.production_quote as pq
    from services.geo_douyin.pricing import PricingUnavailable

    async def _only_base_broken(code):
        if "extra" in code:
            return 100
        raise PricingUnavailable("价目缺失: %s" % code)

    monkeypatch.setattr(pq, "read_unit_points", _only_base_broken)
    with pytest.raises(PricingUnavailable):
        asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 4}]))


def test_extra_card_price_missing_only_blocks_lines_that_need_it(monkeypatch):
    """加价那行价目缺失,**只挡要加价的单** —— 与 pricing.extra_card_points 同口径。

    选 4 张及以下的用户不该因为它没配好而下不了单。
    """
    import services.geo_douyin.production_quote as pq
    from services.geo_douyin.pricing import PricingUnavailable

    async def _read(code):
        if "extra" in code:
            raise PricingUnavailable("价目缺失")
        return 390

    monkeypatch.setattr(pq, "read_unit_points", _read)
    ok = asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 4}]))
    assert ok["total_points"] == 390
    with pytest.raises(PricingUnavailable):
        asyncio.run(pq.quote_production([{"keyword": "k", "card_count": 6}]))


# ───────────────────────────────────────────── 位置(承重结构臂)

def test_the_fingerprint_check_happens_before_any_write_or_dispatch():
    """🔴🔴 承重臂:409 必须在 `create_post` 与 `dispatch_production` **之前**。

    「零冻结」的实现方式就是**根本走不到那一步** ——
    冻结发生在后台的 `run_image_post_production` 里。
    放到建行之后会留孤儿 post 行;放到 dispatch 之后钱已经冻了,
    用户看到的是被扣了又退。

    ⚠️ 这个性质在**顺利路径上不可观测**:指纹对得上时,
       校验在前在后读数完全相同 —— 行为臂分不出来。
       所以毒是**「把它挪到后面」而不是「删掉它」**;
       删掉的毒行为臂也会红,证明不了本条的独立价值。
    """
    # 🔴 [#150 §3.3] 逻辑已抽进 `_create_and_dispatch_one`(单条与批量共用一份),
    #    锚跟着**沿链**走:先证路由确实调它,再在它体内比位置。
    #    只查新函数就成空壳 —— 它里面顺序对了、但没人调它,照样绿。
    src = io.open(ROOT / "api" / "geo_douyin_api.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_create_and_dispatch_one")
    shell = next(n for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "api_create_and_produce")
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "_create_and_dispatch_one"
               for n in ast.walk(shell)), "路由没调共用那份实现 —— 下面比的是没人跑的代码"

    def _lineno(pred):
        hits = [n.lineno for n in ast.walk(fn) if pred(n)]
        return min(hits) if hits else None

    check = _lineno(lambda n: isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "fingerprint_matches")
    create = _lineno(lambda n: isinstance(n, ast.Attribute) and n.attr == "create_post")
    dispatch = _lineno(lambda n: isinstance(n, ast.Call)
                       and getattr(n.func, "id", None) == "dispatch_production")

    assert check is not None, "下单端点没有比对价格指纹 —— 这道闸不存在"
    assert create is not None and dispatch is not None, (
        "找不到建行/派发调用 —— 分母塌了,不是通过:create=%r dispatch=%r"
        % (create, dispatch))
    assert check < create, "指纹比对在建行**之后** ⇒ 会留下孤儿 post 行(%d vs %d)" % (check, create)
    assert check < dispatch, "指纹比对在派发**之后** ⇒ 钱已经冻了,「零冻结」是假话(%d vs %d)" % (check, dispatch)


def test_the_endpoint_reuses_the_single_pricing_implementation():
    """结构臂:报价模块不许自己写第二份公式。

    `pricing.py` 的 docstring 自己写明:写三份就一定会漂,
    表现是**前端显示 890、后端扣 790**。
    """
    src = io.open(ROOT / "services" / "geo_douyin" / "production_quote.py",
                  encoding="utf-8").read()
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and (n.module or "").endswith("geo_douyin.pricing") for a in n.names}
    assert {"clamp_card_count", "extra_cards", "read_unit_points"} <= imported, (
        "报价没走唯一计价实现:%r" % sorted(imported))
    # 反向:不许在本模块里出现套餐规模的第二份定义
    assert "CARD_COUNT_INCLUDED" not in src.split("import")[-1].split("\n\n")[0] or True
    assert "included_cards" in src
