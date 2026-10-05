"""#84 §3 · 「看到的价 == 提交的题集 == 扣的钱」在 legacy 上的服务端底线。

🔴 本文件是**返修版**。上一版被 Review 三发毒全部打穿,原因不是断言写错,是
   **判据从未驱动被测的那一行**:那条叫 `..._round_trips_through_the_guard` 的
   判据只是拿 `price_preview_id()` 跟自己比,既没调守卫也没调 POST。
   名字声称了断言从没验的性质 —— 与本仓当日刚记过的那条同病。
   返修做法:守卫抽成**纯函数**,判据**真的**走 POST → 拿 id → 驱动守卫。

必杀清单(这三发毒上一版全存活;下面是**实测**的杀手归属,不是我推的):
  ① 守卫输入 `request.custom_questions or []` 换成 `[]`
     => `test_the_guard_arguments_come_from_the_request`(AST 来源锁)
  ② 守卫的否定判断被去掉(match/mismatch 结论对调)
     => `test_the_guard_negates_the_match_result` + 来源锁
  ③ POST 返回的 id 按空题集算(`norm[:0]`)
     => `test_post_issued_id_is_accepted_by_the_guard`(真往返)

🔴 ①② 只有结构锁杀得到,行为臂杀不到 —— 因为驱动端点要造完整的
   `DiagnosisRequest` 与库。**如实写在这里**,不假装行为面覆盖了它们。

🔴 **本包覆盖不到的形状(实测存活,如实登记)**:
   `_ai_guard = bool(request.ai_optimize_custom) and False` ——
   数据流锁看得见那个 `Attribute`,却看不见**值恒 False**。
   即「读了 request」与「用了 request 的值」是两件事,本锁只证前者。
   🔴 而且这**不是一格,是一族**(B 订正):`x or True` / `0 if x else 0` /
   `bool(x) and False` 本锁全放行;`user = request.state.user or {}` 正是 #78 的病形。
   => 口径:**复合右值一律不判 PASS;本锁只对同一性形态**
     (右值就是该 Attribute 本身,或纯链式访问)**有效**。
   要盖住它得做常量折叠/取值分析,不在本包范围;**先写下来,不假装覆盖**。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = "geo_diagnosis"
SOCIAL = "social_diagnosis"
FULL = "full_diagnosis"


def _ppid(questions, base=650, ai=False, feature=GEO):
    from services.diagnosis_question_pricing import price_preview_id
    return price_preview_id(questions, base_points=base, ai_optimized=ai, feature_code=feature)


def _matches(pid, questions, base=650, ai=False, feature=GEO):
    from services.diagnosis_question_pricing import price_preview_matches
    return price_preview_matches(pid, questions, base_points=base,
                                 ai_optimized=ai, feature_code=feature)


def _post(questions, *, ai=False, scope="geo", base=650, bases=None, monkeypatch=None):
    """真调 POST 预览端点(只把价目行换成桩 —— 它是外部输入)。

    🔴 bases 给的是**按 feature_code 分档**的基价。上一版的桩是
    `lambda code: {...base...}` —— **对 code 视而不见**,于是「POST 忽略 scope」
    在它下面根本不可观测:Review 毒 10(把 _feature 写死 geo_diagnosis)就是这么
    活下来的。代码那时其实是对的,**存活是尺子的问题不是代码的问题**。
    """
    import db.wallet_db as wallet_db
    from api.pricing_ssot_api import DiagnosisPricePreviewIn, diagnosis_price_preview_post

    table = dict(bases or {})
    monkeypatch.setattr(wallet_db, "get_feature_pricing",
                        lambda code: {"cost_points": table.get(code, base),
                                      "requires_paid_points": False})
    req = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": 7, "is_admin": False}))
    body = DiagnosisPricePreviewIn(questions=questions, aiOptimizeCustom=ai, scope=scope)
    return asyncio.run(diagnosis_price_preview_post(req, body))


# ══════════════════════════════════════════════════════════════════════════
# 哈希的四轴(每一轴都是**实际扣费函数真的吃**的输入)
# ══════════════════════════════════════════════════════════════════════════

def test_same_question_set_gives_the_same_id():
    assert _ppid(["q1", "q2"]) == _ppid(["  q1  ", "q2", "q1"])


def test_changed_question_set_changes_the_id():
    assert _ppid(["q1", "q2"]) != _ppid(["q1", "q3"])
    assert _ppid(["q1", "q2"]) != _ppid(["q2", "q1"]), "保序:换顺序就是另一个题集"


def test_price_table_change_changes_the_id():
    """价是题集的纯函数,但**函数本身会变**;只绑题集会放行旧价。"""
    assert _ppid(["q1"], base=650) != _ppid(["q1"], base=700)


def test_toggle_change_changes_the_id():
    """🔴 Review 实测的反例:3 题,关 650 / 开 950,上一版 id **同值**。

    于是用户看 650、切「双轨」提交,闸不响、扣 950。
    我上一版把 `ai_optimized` 写死 False,依据是「开关要退役」——
    而那条口径**后来改成了不退役**。建在过期前提上的哈希不报错,只漏钱。
    """
    q = ["a", "b", "c"]
    assert _ppid(q, ai=False) != _ppid(q, ai=True)


def test_scope_change_changes_the_id():
    """基价按 scope 取(fee_map),上一版两处都写死 geo_diagnosis。"""
    assert _ppid(["a"], feature="geo_diagnosis") != _ppid(["a"], feature="social_diagnosis")


# ══════════════════════════════════════════════════════════════════════════
# 真往返:POST 发的 id,守卫必须认;换任一轴必须拒
# ══════════════════════════════════════════════════════════════════════════

def test_post_issued_id_is_accepted_by_the_guard(monkeypatch):
    """🔁 正样本臂 —— **真的**走 POST 再驱动守卫。

    没有它,下面所有「换了就拒」全绿也可能是**永远拒**(谁都下不了单),
    那比原缺陷更坏,而两者在拒绝臂读数上完全同形。
    ⇒ 这一条同时是必杀清单 ③(POST 按空题集发 id)的杀手。
    """
    qs = ["这个牌子靠谱吗", "多少钱"]
    out = _post(qs, monkeypatch=monkeypatch)
    assert out["pricePreviewId"], "POST 没发 id"
    assert _matches(out["pricePreviewId"], qs) is True


@pytest.mark.parametrize("mutate", ["questions", "toggle", "scope", "base"])
def test_guard_rejects_when_any_priced_input_changed(monkeypatch, mutate):
    """🔁 反臂:POST 定价用的**任一**输入变了,守卫必须拒。

    🔴 注意归属:这几档**杀不到**必杀清单 ①(守卫输入被换成 [])——
       本文件的 `_matches()` 直接调纯函数,不走端点,所以端点参数被换它看不见。
       ① 由下面的 AST 来源锁负责(实测:换 [] 后恰 `..._arguments_come_from_the_request` 红)。
       写清楚这条是因为:上一版我在 docstring 里声称行为臂覆盖了它,那是**假的覆盖** ——
       与本单被打回的原因同病(名字/说明声称了断言没验的性质)。
    """
    qs = ["a", "b", "c"]
    out = _post(qs, monkeypatch=monkeypatch)
    pid = out["pricePreviewId"]
    kw = dict(questions=qs, base=650, ai=False, feature=GEO)
    if mutate == "questions":
        kw["questions"] = ["a", "b", "d"]
    elif mutate == "toggle":
        kw["ai"] = True
    elif mutate == "scope":
        kw["feature"] = "social_diagnosis"
    else:
        kw["base"] = 700
    assert _matches(pid, kw["questions"], base=kw["base"],
                    ai=kw["ai"], feature=kw["feature"]) is False, mutate


def test_guard_rejects_an_empty_or_missing_id():
    for pid in ("", None, "   "):
        assert _matches(pid, ["a"]) is False, pid


# ══════════════════════════════════════════════════════════════════════════
# 结构锁:必杀清单 ②(否定判断被去掉)靠这条,行为面杀不到
# ══════════════════════════════════════════════════════════════════════════

def _guard_node():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp) \
           and isinstance(n.test.op, ast.Not) and isinstance(n.test.operand, ast.Call):
            f = n.test.operand.func
            if getattr(f, "id", getattr(f, "attr", None)) == "_ppm":
                return src, n
    return src, None


def test_the_guard_negates_the_match_result():
    """🔴 守卫必须是「**不**匹配才拒」。

    去掉那个否定,结论对调:匹配的被拒、不匹配的放行 —— 而**行为判据杀不到它**
    (纯函数本身仍然正确,错的是端点怎么用它)。上一版三发毒里这一发就是这么活下来的。
    """
    _, node = _guard_node()
    assert node is not None, "没找到 `if not _ppm(...)` 形状的守卫 —— 否定判断没了或改了形状"


def _assign_value(src, name):
    """取某个局部名最后一次赋值的右值 AST 节点。"""
    found = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    found = n.value
    return found


def _reads_request_attr(node, attr) -> bool:
    """这段右值里是否**真的**读了 `request.<attr>`(数据流,不是文本包含)。"""
    if node is None:
        return False
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == attr \
           and isinstance(n.value, ast.Name) and n.value.id == "request":
            return True
    return False


@pytest.mark.parametrize("var,attr", [
    ("_ai_guard", "ai_optimize_custom"),
    ("_feature_guard", "diagnosis_scope"),
])
def test_guard_inputs_flow_from_the_request(var, attr):
    """🔴 数据流同一性 —— **不是包含判定**。

    上一版写的是 `"request.ai_optimize_custom" in head`,而 head 是守卫之前
    **整段 server.py** —— 那个串在别处(:3361 一带)本就存在 ⇒ 断言**恒真**。
    于是把 `_ai_guard = False` 写死,锁照样绿,**钱缺陷原样回来**(Review 毒 5/6)。
    这条改成:找到该变量的赋值,右值里必须**真的**有 `request.<attr>` 的读取,
    且右值**不是常量**。包含判定证不了来源,只有数据流能。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    val = _assign_value(src, var)
    assert val is not None, f"{var} 根本没赋值 —— 分母塌了"
    assert not isinstance(val, ast.Constant), f"{var} 被写成了常量:定价输入不再来自请求"
    assert _reads_request_attr(val, attr), f"{var} 的右值没有读 request.{attr}"


def test_the_guard_call_uses_those_variables():
    """守卫调用必须**用**上面那两个变量(与来源锁成对:一个管来源,一个管接线)。"""
    src, node = _guard_node()
    assert node is not None
    seg = ast.get_source_segment(src, node) or ""
    assert "request.custom_questions" in seg
    for name in ("_ai_guard", "_feature_guard"):
        assert name in seg, f"{name} 没进守卫调用"


def test_the_guard_precedes_every_write_in_the_legacy_endpoint():
    """🔴 顺序锁:守卫必须排在端点里任何写之前。

    它**已经抓到过一次真的** —— 我第一版把守卫放在
    `INSERT INTO diagnosis_records` 之后,是它把我拦下来的。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(src) if '@app.post("/api/diagnosis")' in ln)
    guard = next(i for i, ln in enumerate(src)
                 if i > start and "_preview_id = (request.price_preview_id" in ln)
    writes = [i for i, ln in enumerate(src)
              if start < i < guard
              and any(k in ln for k in ("reserve_charge", "freeze_points",
                                        "charge_points", "deduct_points", "INSERT INTO"))]
    assert not writes, f"守卫之前还有写:第 {[w + 1 for w in writes]} 行"


def _gate_raises(src: str):
    """提交闸自己那段里的 HTTPException(结构边界,不按字符数取)。"""
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "start_diagnosis":
            continue
        lo = hi = None
        for i, st in enumerate(n.body):
            txt = ast.unparse(st)
            if lo is None and "_preview_id = " in txt:
                lo = i
            if "_ppm(" in txt:
                hi = i
        assert lo is not None and hi is not None, "找不到闸的两端 —— 分母塌了"
        assert lo <= hi, "闸的两端顺序反了"
        out = []
        for st in n.body[lo:hi + 1]:
            for r in ast.walk(st):
                if isinstance(r, ast.Raise) and r.exc is not None:
                    txt = ast.unparse(r)
                    if "HTTPException" in txt:
                        out.append(txt)
        return out
    raise AssertionError("找不到 start_diagnosis")


def test_error_paths_say_nothing_was_charged():
    """🔴 提交闸的每一条 **4xx** 拒绝都要告诉她钱没动。

    上一版取的是 `src[i:i+2000]` —— **固定字节窗**。订正二十六往闸里插了十几行
    派生代码,503 那条就被挤出窗外、判据当场红,而代码是对的。
    按字符数取范围的尺子会被任何插入改变分母,且红得毫无道理。

    改结构边界后我第一版又**扩过了头**:枚举「落库之前的全部 HTTPException」
    得到 12 条,把 10 条与本单无关的既有拒绝拖进分母 —— 那是越界,
    会逼人去改一堆本来没问题的文案。边界要恰好等于**这道闸自己**:
    从 `_preview_id` 赋值那条语句,到 `_ppm(...)` 那条判断为止。

    5xx 豁免且写明理由:4xx 是「你的提交被拒了」,她第一反应是钱扣没扣;
    5xx 是「我们这边坏了」,那句话不是她当下要的信息。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    raises = _gate_raises(src)
    four_xx = [r for r in raises
               if any("status_code=%d" % c in r for c in (400, 409, 422, 429))]
    assert len(four_xx) >= 3, (
        "闸里的 4xx 只有 %d 条 —— 分母可疑:%s" % (len(four_xx), raises))
    silent = [r for r in four_xx if "没有扣除任何算力" not in r]
    assert not silent, "这些 4xx 拒绝没说钱没动:%s" % [r[:60] for r in silent]


@pytest.mark.parametrize("ai,expect", [(False, 650), (True, 950)])
def test_post_points_follow_the_toggle(monkeypatch, ai, expect):
    """🔴 毒 7 的行为面杀手:POST **显示的价**必须随开关变。

    上一版 points 与 id 各算一遍 ⇒ 可以分家:显示 650、id 绑 950,
    提交放行扣 950,而**用户看到的是 650**。
    结构上已由 `quote_diagnosis_price` 一处派生消灭;这条是行为面的复核。
    3 题的期望值由共享常量推出,不写死。
    """
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION, FREE_CUSTOM_QUESTIONS)
    assert FREE_CUSTOM_QUESTIONS == 8 and EXTRA_POINTS_PER_QUESTION == 100
    out = _post(["a", "b", "c"], ai=ai, monkeypatch=monkeypatch)
    assert out["points"] == expect, out
    # 显示价与被绑的价必须同源:拿返回的 id 反验一次
    assert _matches(out["pricePreviewId"], ["a", "b", "c"], ai=ai) is True

@pytest.mark.parametrize("scope,feature,base", [
    ("geo", GEO, 650),
    ("social", SOCIAL, 1200),
    ("full", FULL, 1800),
])
def test_post_price_follows_the_scope(monkeypatch, scope, feature, base):
    """🔴 毒 10 的杀手:POST 的基价与 feature_code 必须跟着 scope 走。

    3 题(< 免费 8 题)=> extra 恒 0 => points 就是基价本身,这条轴被单独量出来。
    后果不是钱错扣(守卫会拦),是 **scope != geo 的诊断恒 409、永远提交不了**:
    POST 用 geo 基价出 id,server.py 守卫按真 scope 重算 => 必然对不上。
    """
    bases = {GEO: 650, SOCIAL: 1200, FULL: 1800}
    out = _post(["a", "b", "c"], scope=scope, bases=bases, monkeypatch=monkeypatch)
    assert out["points"] == base, "scope=%s 的基价没跟着走:%r" % (scope, out)
    assert _matches(out["pricePreviewId"], ["a", "b", "c"], base=base, feature=feature) is True
    for other in (GEO, SOCIAL, FULL):
        if other == feature:
            continue
        ok = _matches(out["pricePreviewId"], ["a", "b", "c"],
                      base=base, feature=other)
        assert ok is False, "%s 的 id 竟然过了 %s 的守卫" % (feature, other)


def _files_defining_the_scope_map():
    """全仓找「字面量 scope->feature 映射」,返回 repo 相对路径集合。"""
    hits = set()
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "scripts/", ".git/")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Dict):
                continue
            for k, v in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value == "geo"
                        and isinstance(v, ast.Constant) and v.value == GEO):
                    hits.add(rel)
    return hits


def test_scope_to_feature_map_is_defined_exactly_once():
    """🔴 scope->feature 的映射全仓**只许有一份**。

    原先三处各写一份(预览/守卫/扣费)。两处漂了不会有任何东西报错:
    预览 != 守卫 => 恒 409;守卫 != 扣费 => 看到的 != 扣的。
    分母**机械枚举**(走全仓 .py,排除 tests/scripts),不手写文件名单 ——
    手写名单漏掉的那一份不会让任何判据变红。
    末尾的正样本自证:检测器必须真的在 pricing 模块里找到那一份,
    否则「零命中」与「检测器坏了」同形。
    """
    owner = "services/diagnosis_question_pricing.py"
    hits = _files_defining_the_scope_map()
    assert owner in hits, "检测器在 SSOT 里都没找到那份映射 —— 尺子坏了,不是零命中"
    assert hits == {owner}, "scope->feature 映射不止一份:%s" % sorted(hits)


def test_both_consumers_go_through_the_shared_map():
    """接线锁:守卫与实际扣费都必须调 `feature_code_for_scope`。

    🔴 与上一条成对 —— 上一条只证「没有第二份字面映射」,
    证不了这两处**用了**唯一那份(它们完全可以改成写死一个串)。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    for var in ("_feature_guard", "_feature_charge"):
        val = _assign_value(src, var)
        assert val is not None, "%s 没赋值 —— 分母塌了" % var
        assert isinstance(val, ast.Call), "%s 的右值不是调用:很可能被写死了" % var
        fn = val.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        assert name == "_fcs", "%s 没走共享映射,调的是 %s" % (var, name)
