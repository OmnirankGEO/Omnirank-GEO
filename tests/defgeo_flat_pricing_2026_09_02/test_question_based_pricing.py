"""防御/混合线按**题**计价 —— 不按格、不按平台(Owner 2026-09-02 拍板)。

生产实证(Deploy 普查):修前 defensive 12 格 = **7,800** · hybrid 20 格 = **13,000**,
base 一律 650。admin 显示「平台承担」看不出来,真服务商会被真扣 ——
20 格那笔是 legacy 一次诊断的 **20 倍**。

Owner 定的口径:与 legacy **同规则** —— 基价来自现役 ``feature_pricing``,
加价按题(超过 ``FREE_CUSTOM_QUESTIONS`` 的部分每题 ``EXTRA_POINTS_PER_QUESTION``;
开 AI 优化则每题都算),**按题不按平台**,hybrid 按一次诊断计。
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


# ══════════════════════════════════════════════════════════════════════════
# ① 规则本体:Owner 口径逐格
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("questions,expected_extra", [
    (0, 0),      # 一道都没有
    (1, 0),      # 阈值内
    (5, 0),      # 🔴 Owner 手机上那次就是 5 题 —— 两种解读在这里同解,
                 #    所以他的实测**区分不了**「一口价」与「legacy 同规则」。
    (8, 0),      # 恰好在阈值上:不加价
    (9, 100),    # 越过阈值一题
    (20, 1200),  # Owner 那次的题量若真到 20:650 + 1200 = 1850
])
def test_extra_follows_the_legacy_question_rule(questions, expected_extra):
    from services.diagnosis_question_pricing import extra_points_for_questions

    assert extra_points_for_questions(questions, ai_optimized=False) == expected_extra


def test_ai_optimized_charges_every_question():
    """开了 AI 优化则每题都加价 —— 与 legacy 逐值同解。

    防御线目前**没有**这个开关(见 ``_price_for_plan`` 注释),
    但规则本体要按 legacy 完整实现:将来接上开关时改的是实参,不是规则。
    """
    from services.diagnosis_question_pricing import extra_points_for_questions

    assert extra_points_for_questions(5, ai_optimized=True) == 500
    assert extra_points_for_questions(1, ai_optimized=True) == 100
    assert extra_points_for_questions(0, ai_optimized=True) == 0


def test_dirty_counts_never_produce_negative_money():
    """脏值不许把钱算成负的。"""
    from services.diagnosis_question_pricing import extra_points_for_questions

    for bad in (None, 0, -1, -99):
        assert extra_points_for_questions(bad, ai_optimized=False) == 0
        assert extra_points_for_questions(bad, ai_optimized=True) == 0


# ══════════════════════════════════════════════════════════════════════════
# ② 反向臂:必须**不是**一口价,也必须**不**随平台数变
# ══════════════════════════════════════════════════════════════════════════
def test_it_is_not_a_flat_price():
    """反向臂 —— 若实现成「任何题数都 650」,这条必须红。

    🔴 这条是 Owner 二选一里**没被选中**的那一解。留着它,是因为
       「5 题 = 650」在两解下都成立:只钉 5 题的话,一口价实现照样全绿。
    """
    from services.diagnosis_question_pricing import extra_points_for_questions

    assert extra_points_for_questions(20, ai_optimized=False) > 0, (
        "20 题仍然零加价 —— 那是「一口价」那一解,不是 Owner 定的 legacy 同规则")


def test_platform_count_never_enters_pricing():
    """价格与平台数**无关** —— 这是本次 P0 的核心。

    🔴 分母是**签名**:``_price_for_plan`` 不许再有任何以格数/平台数为名的形参。
       只断言"某次调用的数值对"挡不住"下次又把格数传进来"。
    """
    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_price_for_plan")
    args = [a.arg for a in fn.args.args]
    assert "planned_cells" not in args, (
        f"_price_for_plan 又收格数了:{args} —— 格数 = 题数 × 平台数,"
        f"拿它计价就是把一次诊断按平台卖 N 遍")
    assert any("question" in a for a in args), args

    # 调用点也要传题数,不是格数(签名对了、实参传错一样错)
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_price_for_plan")
    seg = ast.get_source_segment(src, call) or ""
    assert "planned_cells" not in seg, f"调用点仍在传格数:{seg}"
    assert "total_count" in seg, f"调用点没传题数:{seg}"


# ══════════════════════════════════════════════════════════════════════════
# ③ 接线:两条线必须调**同一个**规则函数,常量只有一处
# ══════════════════════════════════════════════════════════════════════════
def test_both_lines_call_the_shared_rule():
    """legacy 与防御线都要调 ``extra_points_for_questions``。

    🔴 同一条计价规则写两处,必有一处没人验 —— 而那一处漂了就是钱漂了,
       方向还不定:多收要退款,少收是收入漏。
    """
    for rel in (("server.py",), ("api", "defensive_geo_api.py")):
        src = _read(*rel)
        assert "extra_points_for_questions" in src, (
            f"{'/'.join(rel)} 没调共享规则函数")


def test_the_rule_constants_are_defined_exactly_once():
    """阈值与单价只许有一处定义;旧的内联算式必须零残留。

    分母是**全仓 .py**(去掉判据自己),机械枚举,不手抄文件名。
    """
    import pathlib

    hits_const, hits_inline = [], []
    for p in pathlib.Path(REPO).rglob("*.py"):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith("tests/") or "/.venv/" in rel or rel.startswith(".venv/"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "FREE_CUSTOM_QUESTIONS = " in text or "EXTRA_POINTS_PER_QUESTION = " in text:
            hits_const.append(rel)
        # 旧内联形态:max(0, <n> - 8) * 100 / <n> * 100
        if "- 8) * 100" in text or "custom_q_count * 100" in text:
            hits_inline.append(rel)
    assert hits_const == ["services/diagnosis_question_pricing.py"], hits_const
    assert not hits_inline, f"旧内联算式还在:{hits_inline}"


# ══════════════════════════════════════════════════════════════════════════
# ④ 在途 preview:改规则必须改 pricing_catalog_version
# ══════════════════════════════════════════════════════════════════════════
def test_the_real_function_mixes_the_rule_version(monkeypatch):
    """行为臂:**调生产那个函数**,换规则版本 ⇒ 串必须跟着变。

    🔴 这条是补上去的。上一版这里是两条**没有牙**的锁,Review 亲毒
       (把 canonical 里的 ``str(PRICING_RULE_VERSION),`` 换成 ``"",``)16 条全绿:
       · 一条是**裸串锁** —— 断言源码段里出现过 ``PRICING_RULE_VERSION``,
         而函数体内那行 ``from … import PRICING_RULE_VERSION`` 就把它顶住了
         (谓词存在 ≠ 有牙);
       · 一条是**自构中间值** —— 判据自己重拼 canonical 串再断言自己算的结果,
         **从未调用生产函数**。它证明的是「我对 hash 输入的理解」,
         不是「生产函数真的混了规则版本」。
       而这正是防「在途 preview 按旧规则冻结」的那道机制。

    生产函数里是**局部** import,所以 patch 模块属性即可 —— 每次调用都会重新取。
    """
    import services.diagnosis_question_pricing as rules
    from api.defensive_geo_api import _pricing_catalog_version_from_row

    row = {"feature_code": "geo_diagnosis", "cost_points": 650,
           "requires_paid_points": False}

    monkeypatch.setattr(rules, "PRICING_RULE_VERSION", "axis-A")
    a = _pricing_catalog_version_from_row(row)
    monkeypatch.setattr(rules, "PRICING_RULE_VERSION", "axis-B")
    b = _pricing_catalog_version_from_row(row)
    assert a != b, (
        "换了规则版本而 pricing_catalog_version 没变 —— 规则轴根本没进 hash;"
        "后果是改规则时在途 preview 不会被判 SNAPSHOT_CHANGED,按旧规则冻结")

    # 价目内容那三轴必须**同时**还活着:别为了加规则轴把老轴挤掉。
    monkeypatch.setattr(rules, "PRICING_RULE_VERSION", "axis-A")
    assert _pricing_catalog_version_from_row(
        {**row, "requires_paid_points": True}) != a, "requires_paid_points 轴死了"
    assert _pricing_catalog_version_from_row(
        {**row, "cost_points": 651}) != a, "cost_points 轴死了"
    assert _pricing_catalog_version_from_row(
        {**row, "feature_code": "social_diagnosis"}) != a, "feature_code 轴死了"


def test_the_real_function_still_produces_the_legacy_prefix():
    """**生产函数**对生产那一行算出的串 = 旧三项 + 当前规则版本。

    🔴 主语是生产函数,不是我重写的公式(上一版就栽在这)。
       同时钉住 Deploy 在**生产**实读到的修前串:它证明旧三项的**内容与顺序**
       一个字没动 —— 我是往 canonical **追加**规则版本,不是换一套算法。
       追加而不是替换,是为了保住既有那条「翻 requires_paid_points ⇒ 409」的判别力。
    """
    import hashlib

    from api.defensive_geo_api import _PRICING_CATALOG_SCHEME, _pricing_catalog_version_from_row
    from services.diagnosis_question_pricing import PRICING_RULE_VERSION

    legacy_only = hashlib.sha256(b"geo_diagnosis|650|0").hexdigest()[:32]
    assert _PRICING_CATALOG_SCHEME + ":" + legacy_only == (
        "defgeo-pricing-v2:ec9f40ed0e2c37b15b784ff5eff989bb"), (
        "旧三项的拼法与生产实读值对不上 —— 下面那条断言也就失去了参照")

    expected = _PRICING_CATALOG_SCHEME + ":" + hashlib.sha256(
        ("geo_diagnosis|650|0|" + str(PRICING_RULE_VERSION)).encode()).hexdigest()[:32]
    got = _pricing_catalog_version_from_row(
        {"feature_code": "geo_diagnosis", "cost_points": 650,
         "requires_paid_points": False})
    assert got == expected, f"生产函数算出来的不是「旧三项 + 规则版本」:{got}"
    assert got != "defgeo-pricing-v2:ec9f40ed0e2c37b15b784ff5eff989bb", (
        "改了规则而版本仍等于生产修前值 —— 在途 preview 不会被判 SNAPSHOT_CHANGED")


def test_the_canonical_list_names_the_rule_version_identifier():
    """结构臂:canonical 那个列表里必须**恰有一项就是**规则版本标识符。

    🔴 用**同一性**不用包含判定:``"PRICING_RULE_VERSION" in 源码段`` 会被函数体内
       那行 import 顶成恒真(Review 实测)。这里只认两种形态 ——
       ``PRICING_RULE_VERSION`` 或 ``str(PRICING_RULE_VERSION)`` ——
       ``str(X)[:0]`` / ``(X, "")[1]`` 这类包装形是 ``Subscript``,一律不认。
       (包装形能不能真把值废掉由上面的行为臂负责;这条负责"它必须在列表里"。)
    """
    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_pricing_catalog_version_from_row")
    joins = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "join" and isinstance(n.func.value, ast.Constant)
             and n.func.value.value == "|" and n.args
             and isinstance(n.args[0], ast.List)]
    assert len(joins) == 1, f"canonical 的 '|'.join([...]) 不唯一:{len(joins)}"

    def names_the_version(el):
        if isinstance(el, ast.Name):
            return el.id == "PRICING_RULE_VERSION"
        if (isinstance(el, ast.Call) and isinstance(el.func, ast.Name)
                and el.func.id == "str" and len(el.args) == 1):
            return names_the_version(el.args[0])
        return False

    elems = joins[0].args[0].elts
    hit = [i for i, el in enumerate(elems) if names_the_version(el)]
    assert len(hit) == 1, (
        "canonical 列表里没有(或不止一个)规则版本项:"
        + repr([ast.dump(e)[:60] for e in elems]))
    assert len(elems) == 4, f"canonical 项数变了({len(elems)}):旧三项必须原样保留"


def test_the_defensive_line_passes_ai_optimized_false():
    """防御线传给共享规则的必须是 ``ai_optimized=False``。

    🔴 这一格是「抽出同源函数」自己引入的新面:规则本体对了,**实参可以单独漂**。
       改成 ``True`` 则每题都加价 —— 5 题从 650 变 1,150,而上面每一条判据
       都还是绿的(它们钉的是规则本体,不是防御线传了什么)。

    只证到「源码里写的是 False」这一步:这是同一行上的字面量关键字实参,
    源码与运行时之间没有可漂的东西。它**不**证明关键字在运行时绑到了那个形参
    (本仓记过 AST 锁证不了 kwarg 绑定)—— 那一层由
    ``test_extra_follows_the_legacy_question_rule`` 从规则侧覆盖。
    """
    src = _read("api", "defensive_geo_api.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_price_for_plan")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "extra_points_for_questions"]
    assert len(calls) == 1, f"_price_for_plan 里调了 {len(calls)} 次共享规则"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "ai_optimized" in kw, f"没显式传 ai_optimized:{ast.dump(calls[0])}"
    val = kw["ai_optimized"]
    assert isinstance(val, ast.Constant) and val.value is False, (
        "防御线传的不是 ai_optimized=False —— 防御线没有 AI 优化开关,"
        "传 True 等于给每道题都加价")


def test_defensive_entry_prices_five_questions_at_base_only(monkeypatch):
    """行为臂:**经防御线真入口** ``_price_for_plan``,5 题 ⇒ 只收基价。

    与上面那条 AST 锁配对 —— AST 证「源码里写的是 False」,这条证
    「真跑一遍确实按未开 AI 优化算」。``ai_optimized=True`` 时 5 题会多出
    500(基价 650 ⇒ 1,150),所以这条能把那一发毒抓住。

    🔴 期望值不写死 650:基价从**桩返回的那个数**派生。写死就变成我跟自己
       对话 —— 桩改成别的数它照样绿,而它本来该跟着变。加价那半反过来:
       1,200 与桩无关(规则只看题数),所以它必须是常数。

    🔴 桩只供**外部输入**(价目行),不代被测代码算任何东西;并且断言桩真被
       调过 —— 否则 ``_price_for_plan`` 走了别的路时这条会以「没红」的样子
       骗过去。
    """
    import db.wallet_db as wallet_db
    from api.defensive_geo_api import _price_for_plan

    UNIT = 4242            # 故意不是 650:期望值必须从这个数派生
    calls = []

    def fake_pricing(feature_code):
        calls.append(feature_code)
        return {"cost_points": UNIT, "requires_paid_points": False}

    monkeypatch.setattr(wallet_db, "get_feature_pricing", fake_pricing)

    base, extra = _price_for_plan("geo_diagnosis", 5)
    assert calls == ["geo_diagnosis"], f"价目桩没被调到:{calls}"
    assert base == UNIT, f"基价没走现役价目表:{base}"
    assert extra == 0, (
        f"5 题多收了 {extra} —— 防御线没有 AI 优化开关,不该逐题加价")

    base20, extra20 = _price_for_plan("geo_diagnosis", 20)
    assert (base20, extra20) == (UNIT, 1200), (base20, extra20)
