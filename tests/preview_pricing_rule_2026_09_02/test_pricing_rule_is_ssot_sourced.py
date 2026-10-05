"""题单响应里的 `pricingRule` —— 三个数必须来自 SSOT,不许是字面量。

Owner 要在「一道题都还没有」时就显示「起步价 X · 超 N 题每题 Y」。在此之前
没有任何接口给这三个数,前端只能写死 —— 而价目一改,写死的文案当场变成骗人。
所以这里发的是**计价规则的参数**,不是某次的价格。

🔴 本包钉的是**同一性**:`basePoints` 必须与真实算价同源(改价目就跟着变),
   `freeQuestions` / `extraPerQuestion` / `ruleVersion` 必须就是
   `services/diagnosis_question_pricing` 的那三个常量(改常量就跟着变)。
   只断言"等于 650 / 8 / 100"是把当前值写进判据 —— 那样价目改了判据才红,
   而它本该跟着变。
"""

from __future__ import annotations

import ast
import io
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*parts: str) -> str:
    with io.open(os.path.join(REPO, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


@pytest.fixture
def rule(monkeypatch):
    """真调 `_pricing_rule_out()`,只把价目行换成桩(它是外部输入)。"""
    import db.wallet_db as wallet_db

    from api.defensive_geo_api import _pricing_rule_out

    seen = []

    def fake_pricing(feature_code):
        seen.append(feature_code)
        return {"cost_points": 4242, "requires_paid_points": False}

    monkeypatch.setattr(wallet_db, "get_feature_pricing", fake_pricing)
    out = _pricing_rule_out()
    assert seen, "价目桩没被调到 —— 基价不是从价目表来的"
    return out, seen


def test_base_points_comes_from_the_live_price_row(rule):
    """基价 = 现役价目行的 `cost_points`,**派生自桩**而不是写死的数。

    桩故意用 4242 而不是 650:写死 650 的话,桩改成别的数它照样绿,
    而它本该跟着变。
    """
    out, seen = rule
    assert out.base_points == 4242, f"基价没跟着价目行走:{out.base_points}"
    assert seen == ["geo_diagnosis"], f"查的不是诊断那一行:{seen}"


def test_the_two_rule_numbers_are_the_shared_constants(rule):
    """免费题数与每题加价 = `diagnosis_question_pricing` 的常量本身。

    断言的是**同一性**(等于那个常量),不是当前值 —— 改常量,响应跟着变,
    判据仍绿;而如果有人在 API 层写死一份,这条当场红。
    """
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION,
        FREE_CUSTOM_QUESTIONS,
        PRICING_RULE_VERSION,
    )

    out, _ = rule
    assert out.free_questions == FREE_CUSTOM_QUESTIONS
    assert out.extra_per_question == EXTRA_POINTS_PER_QUESTION
    assert out.rule_version == PRICING_RULE_VERSION


def test_the_rule_follows_the_constants_when_they_change(monkeypatch):
    """把常量改掉,响应必须跟着变 —— 这条才真的证明"同源"。

    上一条只证明"此刻相等";相等可能是巧合(两边各写了同一个数)。
    这条改一边看另一边动不动,巧合活不下来。
    """
    import db.wallet_db as wallet_db
    import services.diagnosis_question_pricing as rules

    from api.defensive_geo_api import _pricing_rule_out

    monkeypatch.setattr(
        wallet_db, "get_feature_pricing",
        lambda fc: {"cost_points": 111, "requires_paid_points": False})
    monkeypatch.setattr(rules, "FREE_CUSTOM_QUESTIONS", 3)
    monkeypatch.setattr(rules, "EXTRA_POINTS_PER_QUESTION", 7)
    monkeypatch.setattr(rules, "PRICING_RULE_VERSION", "probe-v9")

    out = _pricing_rule_out()
    assert (out.base_points, out.free_questions,
            out.extra_per_question, out.rule_version) == (111, 3, 7, "probe-v9"), out


def test_no_literal_rule_numbers_leak_into_the_api_module():
    """API 模块里不许出现规则数字的字面量。

    分母是 `_pricing_rule_out` 的**整段源码**:三个数只能以名字出现。
    """
    src = _read("api", "defensive_geo_api.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_pricing_rule_out")
    nums = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, int)
            and not isinstance(n.value, bool) and n.value != 0]
    assert not nums, f"_pricing_rule_out 里出现了字面量数字 {nums} —— 规则数只能来自常量"
    for name in ("FREE_CUSTOM_QUESTIONS", "EXTRA_POINTS_PER_QUESTION",
                 "PRICING_RULE_VERSION"):
        assert name in (ast.get_source_segment(src, fn) or ""), f"没引用 {name}"


# ══════════════════════════════════════════════════════════════════════════
# 分母 + 契约面
# ══════════════════════════════════════════════════════════════════════════
def test_every_question_plan_response_carries_the_rule():
    """分母 = `QuestionPlanResponse` 的**每一个**构造点,机械枚举。

    🔴 这条是有来历的:我第一次改的时候用了一个没带 count 断言的整串替换,
       它打中 **4 处**(两处 `QuestionPlanResponse` + 两处 `RunPreviewResponse`),
       而 `_Strict` 禁多余字段 —— run-preview 会当场 500。
       字段是必填的,漏一个构造点则那条路径 500;多插一处则那条路径 500。
       所以两个方向都要钉:**恰好**这些构造点带,别的一个都不许带。
    """
    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    plan_calls = [n for n in calls if n.func.id == "QuestionPlanResponse"]
    assert len(plan_calls) >= 2, f"构造点只有 {len(plan_calls)} 个 —— 分母塌了"

    missing = [n.lineno for n in plan_calls
               if not any(k.arg == "pricingRule" for k in n.keywords)]
    assert not missing, f"这些 QuestionPlanResponse 构造点漏了 pricingRule(会 500):{missing}"

    strays = {n.func.id for n in calls
              if n.func.id != "QuestionPlanResponse"
              and any(k.arg == "pricingRule" for k in n.keywords)}
    assert not strays, f"pricingRule 被塞进了不该有它的响应模型:{sorted(strays)} —— _Strict 会 500"


def test_the_response_model_declares_the_field():
    """响应模型上真的有这一格(不是只在构造点传了个 FastAPI 会丢掉的键)。"""
    from api.defensive_geo_api import PricingRuleOut, QuestionPlanResponse

    assert "pricing_rule" in QuestionPlanResponse.model_fields
    f = QuestionPlanResponse.model_fields["pricing_rule"]
    assert f.alias == "pricingRule", f.alias
    assert f.is_required(), "做成可选的话,漏填就变成静默缺席而不是当场炸"
    for name, alias in [("base_points", "basePoints"),
                        ("free_questions", "freeQuestions"),
                        ("extra_per_question", "extraPerQuestion"),
                        ("rule_version", "ruleVersion")]:
        assert PricingRuleOut.model_fields[name].alias == alias


def test_the_error_path_does_not_carry_the_rule():
    """反向臂:422 那条路不带 `pricingRule`。

    错误信封是 `_safe_error` 造的,与响应模型是两条路。若哪天有人"顺手"
    把规则塞进错误信封,那就等于在她**填错**的时候给她报价 —— 不是这一格的职责,
    也会让信封的白名单形同虚设。
    """
    import api.defensive_geo_api as mod

    err = mod._safe_error("VALIDATION_FAILED", reason_key="plan_empty")
    assert "pricingRule" not in err.detail
    assert "pricing_rule" not in err.detail
    assert err.status_code == 422
    # 配对的必须命中:这条信封该有的东西还在(否则"不带 pricingRule"
    # 可能只是因为信封整个是空的)
    assert err.detail.get("publicExplanation"), err.detail


def test_the_feature_code_literal_has_no_new_sites():
    """诊断功能编码的字面量:**冻结**在已知的三处,不许再长第四处。

    🔴 本来想写"只许一处",实测有 3 处:常量定义 + 两个 catalog_version 函数的
       **默认形参值**。后两处我**不改** —— 本次工单明写不动 canonical/catalog_version,
       而把默认值的写法从字面量换成常量虽然值不变,也是在动那两个函数。
       所以改成冻结豁免:已知的两处按**函数名**点名放行,新增任何一处当场红。

    🔴 豁免必须点名到函数,不能只数个数:只断言"== 3"的话,
       有人删掉一个默认值、又在别处新加一个字面量,总数还是 3,锁看不见。
    """
    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)

    FROZEN_DEFAULT_SITES = {
        "_live_pricing_catalog_version",
        "_pricing_catalog_version_from_row",
    }

    exempt_nodes = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in FROZEN_DEFAULT_SITES:
            for d in fn.args.defaults + fn.args.kw_defaults:
                if isinstance(d, ast.Constant) and d.value == "geo_diagnosis":
                    exempt_nodes.add(id(d))

    stray = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == "geo_diagnosis"
             and id(n) not in exempt_nodes
             and n.lineno != _constant_def_line(src)]
    assert not stray, (
        f'"geo_diagnosis" 在这些行又出现了字面量:{stray} —— '
        f"应当用 DIAGNOSIS_FEATURE_CODE;同一个谓词写两处必有一处没人验")

    # 自证:豁免名单不是空的(空名单会让上面那条变成"只放行常量定义",
    # 看起来更严,实际会在存量上恒红,红到没人看)
    assert len(exempt_nodes) == 2, f"冻结豁免抓到 {len(exempt_nodes)} 处,预期 2"


def _constant_def_line(src: str) -> int:
    """`DIAGNOSIS_FEATURE_CODE = "geo_diagnosis"` 那一行 —— 唯一的定义处。"""
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "DIAGNOSIS_FEATURE_CODE"):
            return n.value.lineno
    raise AssertionError("找不到 DIAGNOSIS_FEATURE_CODE 的定义")


# ══════════════════════════════════════════════════════════════════════════
# [#84 §1 · 2026-09-05] 只读价预览 `GET /api/pricing/diagnosis-preview`
#
# 它与三标签、legacy 共用同一条规则。判据要证的是「同一条」,不是「各自算对」——
# 各自算对而规则不同源,今天两边数一样、改一次规则就分家,那天没有判据会红。
# ══════════════════════════════════════════════════════════════════════════

def _preview(monkeypatch, n, base=4242, user=None):
    """真调端点函数,只把价目行换成桩(外部输入)。"""
    import asyncio
    import types

    import db.wallet_db as wallet_db
    from api.pricing_ssot_api import (
        DiagnosisPricePreviewIn,
        diagnosis_price_preview_post,
    )

    monkeypatch.setattr(wallet_db, "get_feature_pricing",
                        lambda code: {"cost_points": base, "requires_paid_points": False})
    req = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": 7, "is_admin": False} if user is None else user))
    # 🔴 GET 变体已按订正二十一删除:它恒 ai_optimized=False,与 POST 算法不同
    #    => 同一份输入两个价。这些判据改驱动唯一的那个入口。
    body = DiagnosisPricePreviewIn(questions=[f"q{i}" for i in range(n)], mode="growth")
    return asyncio.run(diagnosis_price_preview_post(req, body))


def test_preview_base_comes_from_the_live_price_row(monkeypatch):
    """基价来自价目行,不是写死的 650。桩给 4242 就必须读出 4242。"""
    out = _preview(monkeypatch, 0)
    assert out["breakdown"]["base"] == 4242, out
    assert out["points"] == 4242, out


@pytest.mark.parametrize("n,extra_mult", [(0, 0), (1, 0), (8, 0), (9, 1), (12, 4)])
def test_preview_extra_follows_the_shared_rule(monkeypatch, n, extra_mult):
    """≤8 同价、第 9 道起每道加价 —— 期望值由**共享常量**算出,不写死数字。

    🔴 写死 750/1050 会让这条判据在常量改动那天变成**恒红**(锚绑在旧世界上),
       而恒红与真缺陷在退出码上同形。
    """
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION, FREE_CUSTOM_QUESTIONS)
    assert FREE_CUSTOM_QUESTIONS == 8, "本参数化按免费额度=8 挑的点,额度变了要重挑"
    out = _preview(monkeypatch, n)
    assert out["breakdown"]["extra"] == extra_mult * EXTRA_POINTS_PER_QUESTION, out
    assert out["points"] == out["breakdown"]["base"] + out["breakdown"]["extra"], out


def test_preview_estimate_is_the_same_number_as_points(monkeypatch):
    """🔴 `estimate` 是给余额预检用的**同一个数**,不是第二个数。

    它一旦独立算,就是「同一谓词写两处」—— 两处漂开那天不会有任何判据变红。
    """
    for n in (0, 9, 20):
        out = _preview(monkeypatch, n)
        assert out["estimate"] == out["points"], (n, out)


def test_preview_reuses_the_shared_function_and_has_no_literal_rule_numbers():
    """🔁 接线锁:端点整段源码里不许出现规则数字,且必须引用共享函数。

    只断言「算出来对」不够 —— 端点自己写一份 `max(0, n-8)*100` 今天也算得对,
    而那正是本单要消灭的东西(前端那面镜子就是这么来的)。
    """
    src = _read("api", "pricing_ssot_api.py")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "diagnosis_price_preview_post")
    seg = ast.get_source_segment(src, fn) or ""
    nums = [c.value for c in ast.walk(fn)
            if isinstance(c, ast.Constant) and isinstance(c.value, int)
            and not isinstance(c.value, bool) and c.value not in (0, 401, 503)]
    assert not nums, f"端点里出现了字面量数字 {nums} —— 规则数只能来自常量/共享函数"
    for name in ("quote_diagnosis_price", "get_feature_pricing"):
        assert name in seg, f"端点没引用 {name}"


def test_preview_refuses_when_the_price_row_is_unreadable(monkeypatch):
    """价目读不到 ⇒ 503,**不编一个数**。

    🔴 返 0 或返一个默认价都比 503 坏:她会照着一个我们并不知道的价做决定。
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _preview(monkeypatch, 3, base=0)
    assert ei.value.status_code == 503


def test_preview_requires_login(monkeypatch):
    """未登录 ⇒ 401(价目是对内口径,不做匿名出口)。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _preview(monkeypatch, 3, user={})
    assert ei.value.status_code == 401


def test_preview_route_is_not_shadowed_by_the_id_route():
    """🔴 路由影子:字面路径必须**排在**任何会吞掉它的 `{id}` 路由之前。

    实测过一次:`/api/diagnosis/{id}`(id: int)注册序号 1507/1508、本 router 在 857;
    若把本端点挂成 `/api/diagnosis/price-preview`,哪天 router 注册位置一调,
    请求就落到 `{id}` 上把路径当 int 解析 ⇒ 422,**且不会继续往下找路由**。
    换前缀是消灭这根轴;这条判据守的是「别有人再把它挪回去」。
    """
    import server
    paths = [getattr(r, "path", "") for r in server.app.routes]
    assert "/api/pricing/diagnosis-preview" in paths, "端点没注册上"
    for i, p in enumerate(paths):
        if p == "/api/pricing/diagnosis-preview":
            mine = i
            break
    swallowers = [i for i, p in enumerate(paths)
                  if p.startswith("/api/pricing/{") or p == "/api/pricing/{code}"]
    assert all(mine < i for i in swallowers), (
        f"本端点(序号 {mine})排在同前缀的路径参数路由 {swallowers} 之后 —— 会被吞掉")
