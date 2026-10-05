# -*- coding: utf-8 -*-
"""WO_236-c1a(后端半) · 「已排除品牌定向题」这句话必须由**本次实际排除数**驱动。

现场(Review 用真浏览器打开真客户报告 #700 / #726 看到的):
页面脚注逐字写着「口径已排除『直接问本品牌名』这类必然命中的问题,只看真实竞争问题」,
而同区块抬头写「样本口径:12 条有效 AI 回答」,且这两次诊断**题面全是品牌定向题**
⇒ 真排除了的话样本应为 0。实际一条没排,12 条全进了竞争分母。

两处成因,本包钉后端这一处:
  · `report_writer_v2` 把 `denominator_scope` **硬编码**成 "excludes_brand_directed_questions"
    —— 那是一句关于"本代码打算排除"的声明,不是关于"这次到底排没排"的事实;
  · 前端那句脚注是**无条件静态 JSX**(A 同班改,判据在 A 的包里)。

🔴 拆成两半之后有一个新风险,本包负责堵它的后端一侧:
   **后端交了、前端没接,或字段名对不上 ⇒ 两边各自绿,页面照旧骗客户。**
   所以这里钉「后端确实把排除数交出去了」,A 那边钉「前端确实按它渲染」,
   两格分别在两个包里,但**用同一个字段名同一个语义**:

       字段:competitive.data.excludedBrandDirectedCount
       类型:int(>=0)
       语义:被排除出竞争分母的**有效回答条数**(不是题数)
       渲染约定:== 0 ⇒ **不显示**那句脚注;> 0 ⇒ 显示,且句中数字用这个值
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.public_report_presentation import _competitive_section   # noqa: E402

FIELD = "excludedBrandDirectedCount"


def _modules(*, valid_total, brand_directed_valid, denominator_scope):
    return {
        "3_competition": {
            "valid_total": valid_total,
            "brand_directed_valid": brand_directed_valid,
            "denominator_scope": denominator_scope,
            "top_brands": [{"name": "某同行", "count": 3}],
            "client_detected_count": 8,
        }
    }


def _data(section):
    assert isinstance(section, dict), section
    return section.get("data") or {}


# ── 后端交付面 ──────────────────────────────────────────────────────

def test_the_backend_hands_out_the_exclusion_count():
    """🔴 后端确实把排除数交出去了(拆两半之后,这一格堵后端一侧)。"""
    section = _competitive_section(
        _modules(valid_total=20, brand_directed_valid=5,
                 denominator_scope="excludes_brand_directed_questions"),
        brand_name="甲公司")
    data = _data(section)
    assert FIELD in data, "回包里没有 %s —— 前端无从判断该不该显示那句" % FIELD
    assert data[FIELD] == 5, data[FIELD]


def test_zero_exclusions_is_reported_as_zero_not_omitted():
    """🔴 一条没排 ⇒ 字段值是 **0**,不是缺省、不是 None。这正是 #700/#726 修后的那一格。

    🔴 第一版这条**没钉住**,复审一发毒(`else 0` 改成 `else None`)8 项全绿。
       原因不是漏写断言,是**夹具构造了一个我自己的修法让它不可能再出现的状态**:
       我喂的是 `denominator_scope="excludes_brand_directed_questions"` + 排除数 0,
       那组合走的是 `if` 分支(值恰好是 0),从来没碰 `else`。
       而 #700/#726 修好后走的**正是** `else` —— 写入侧算出 `all_valid_answers`。
       名字说的是对的事,构造的是另一件。
       ⇒ 夹具改成修后的真实形态。
    """
    section = _competitive_section(
        _modules(valid_total=12, brand_directed_valid=0,
                 denominator_scope="all_valid_answers"),
        brand_name="甲公司")
    data = _data(section)
    assert data.get(FIELD) == 0, data
    assert data.get(FIELD) is not None, data


def test_a_stale_count_is_not_reported_as_an_exclusion():
    """🔴 `denominator_scope` 说没排、而 `brand_directed_valid` 留着旧值(>0)⇒ 仍报 0。

    这一格让 `else 0` 变成**承重**的:
    · 写成 `else None` ⇒ 这里红(也就是复审那发毒);
    · 写成 `else brand_directed` ⇒ 这里红(会拿陈旧计数宣称排过);
    · 只有「以 scope 为准报 0」能过。
    没有它,那个 `else` 分支就没有任何一条判据说得清它该返回什么。
    """
    section = _competitive_section(
        _modules(valid_total=12, brand_directed_valid=7,
                 denominator_scope="all_valid_answers"),
        brand_name="甲公司")
    data = _data(section)
    assert data.get(FIELD) == 0, (
        "scope 说本次没排,却按陈旧计数报了 %r 条排除 —— 那是拿旧数宣称新事实"
        % data.get(FIELD))


def test_the_field_is_always_an_int_never_none():
    """🔴 恒为 int,永不为 None —— 一种表示,不留第二种。

    同一件事两种表示(None 与 0)正是"两边各自绿"的入口:
    前端少写一个 null 判断就会回落到"按老样子显示",也就是继续宣称排过。
    四种可达组合逐个验。
    """
    combos = [
        (0, "all_valid_answers"),
        (7, "all_valid_answers"),
        (0, "excludes_brand_directed_questions"),
        (5, "excludes_brand_directed_questions"),
    ]
    for brand_directed, scope in combos:
        data = _data(_competitive_section(
            _modules(valid_total=20, brand_directed_valid=brand_directed,
                     denominator_scope=scope),
            brand_name="甲公司"))
        value = data.get(FIELD)
        assert isinstance(value, int), "组合 %r 下拿到 %r(%s)" % (
            (brand_directed, scope), value, type(value).__name__)
        assert value >= 0, value


def test_both_branches_of_the_expression_are_exercised():
    """🔴 分支覆盖自证:`if` 与 `else` 各至少被一格真的走过。

    复审那发毒之所以能活,就是因为我全部夹具都落在同一支上。
    这一条把"两支都走过"本身变成可检查的事实 —— 靠的是两支给出**不同**的值:
    同样的 `brand_directed_valid=7`,scope 说排了 ⇒ 7,scope 说没排 ⇒ 0。
    两个值相等的话这条会红,说明分支没有分辨力(或被谁合并了)。
    """
    taken_if = _data(_competitive_section(
        _modules(valid_total=20, brand_directed_valid=7,
                 denominator_scope="excludes_brand_directed_questions"),
        brand_name="甲公司")).get(FIELD)
    taken_else = _data(_competitive_section(
        _modules(valid_total=20, brand_directed_valid=7,
                 denominator_scope="all_valid_answers"),
        brand_name="甲公司")).get(FIELD)
    assert taken_if == 7, taken_if
    assert taken_else == 0, taken_else
    assert taken_if != taken_else, "两支给出同一个值 —— 分支没有分辨力"


def test_the_prose_scope_does_not_claim_an_exclusion_that_did_not_happen():
    """口语那串 `sampleScope` 在没排时不许出现「已排除」字样。"""
    section = _competitive_section(
        _modules(valid_total=12, brand_directed_valid=0,
                 denominator_scope="excludes_brand_directed_questions"),
        brand_name="甲公司")
    scope = _data(section).get("sampleScope") or ""
    assert "已排除" not in scope, scope
    assert "12 条有效 AI 回答" in scope, scope


def test_the_prose_scope_says_the_number_when_it_did_happen():
    """反向对照:真排过就要说出条数 —— 否则"不显示"会变成永远不显示。"""
    section = _competitive_section(
        _modules(valid_total=20, brand_directed_valid=5,
                 denominator_scope="excludes_brand_directed_questions"),
        brand_name="甲公司")
    scope = _data(section).get("sampleScope") or ""
    assert "已排除 5 条" in scope, scope


# ── 写入侧:denominator_scope 不许再是常量 ────────────────────────────

def test_denominator_scope_is_computed_not_hardcoded():
    """🔴 `report_writer_v2` 里那个值必须**按实际算**,不许是字面量常量。

    按行为认:AST 找到那个键的赋值,断言它**不是**一个常量字符串。
    (按文本 grep 会被注释里的同名字符串满足 —— 本仓吃过这个亏。)
    """
    import ast
    import io

    src = io.open(REPO / "services" / "report_writer_v2.py", encoding="utf-8").read()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "denominator_scope":
                found.append(value)
    assert found, "找不到 denominator_scope 的赋值 —— 判据的前提变了,要重写"
    for value in found:
        assert not isinstance(value, ast.Constant), (
            "denominator_scope 又变回硬编码常量 %r —— 它必须陈述"
            "「这次到底排没排」,不是「本代码打算排除」" % getattr(value, "value", None))


def test_the_detector_would_catch_a_constant():
    """🔴 正样本臂:同一个探测器对一段**硬编码**的写法必须报阳。

    没有这一条,上面那条绿可能只是"探测器什么都没找到"。
    """
    import ast

    tree = ast.parse('x = {"denominator_scope": "excludes_brand_directed_questions"}')
    consts = [v for n in ast.walk(tree) if isinstance(n, ast.Dict)
              for k, v in zip(n.keys, n.values)
              if isinstance(k, ast.Constant) and k.value == "denominator_scope"
              and isinstance(v, ast.Constant)]
    assert consts, "探测器认不出最直白的硬编码写法"


@pytest.mark.parametrize("brand_directed,expected_scope", [
    (0, "all_valid_answers"),
    (3, "excludes_brand_directed_questions"),
])
def test_the_writer_reports_what_actually_happened(brand_directed, expected_scope):
    """写入侧的取值逐格对:没排 ⇒ all_valid_answers;排了 ⇒ excludes_...。

    这里用**同一段源码里的表达式**求值,而不是我另写一份等价逻辑 ——
    另写一份就是第二套实现,两边迟早分家。
    """
    import ast
    import io

    src = io.open(REPO / "services" / "report_writer_v2.py", encoding="utf-8").read()
    tree = ast.parse(src)
    expr = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "denominator_scope":
                expr = value
    assert expr is not None
    code = compile(ast.Expression(body=expr), "<denominator_scope>", "eval")
    assert eval(code, {}, {"brand_directed_valid": brand_directed}) == expected_scope


# ══════════════════════════════════════════════════════════════════
# WO_236-c1c · 顶部「真实问题 N 个」要发**题数**,不是关键词数
# ══════════════════════════════════════════════════════════════════

def _presentation(question_rows, keyword_count):
    """跑真 `build_public_report_presentation`,只喂最小可用的 modules。"""
    from services.public_report_presentation import build_public_report_presentation

    # 🔴 模块键是 `3_raw`(`public_report_presentation.py:564`),不是 `3_raw_ai_appendix`。
    #    我第一版写错了键 ⇒ `_iter_results` 一行都产不出 ⇒ `questionCount` 恒 None
    #    ⇒ 判据读到的是 `keyword_count` 回落值。**夹具够不着被测的那条路**,
    #    今天第三次同族(前两次:构造了修法让它不可能的状态 / 断言了修法让它不再成立的前提)。
    #    这次是最基础的一种:**键名写错,整条路径零输入**,而读数看起来只是"数不对"。
    by_question: dict = {}
    for q, platform in question_rows:
        by_question.setdefault(q, {})[platform] = {
            "platform_key": platform,
            "answer": "回答里提到了%s。" % BRAND_NAME,
            "brand_detected": True,
            "status": "ok",
        }
    # 🔴 顶层要按 `get_client_report_modules` 认的形状包一层 `client.modules`
    #    (`build_public_report_presentation:1082`)。直接喂 modules 字典的话
    #    `_iter_results` 一行都产不出、`questionCount` 恒 None,
    #    而判据读到的是 `keyword_count` 回落值 —— 看起来像"数不对",实则**夹具没跑到路上**。
    modules = {
        "client": {
            "modules": {
                "3_raw": {
                    "tests": [
                        {"question": q, "layer_key": "brand_awareness", "results": results}
                        for q, results in by_question.items()
                    ]
                }
            }
        }
    }
    return build_public_report_presentation(
        modules, industry="光伏", canonical_score=50,
        generated_at=None, keyword_count=keyword_count, brand_name=BRAND_NAME)


BRAND_NAME = "甲公司"


def test_the_headline_question_count_is_questions_not_keywords():
    """🔴 顶部那格发的是**题数**,不是关键词数。

    #700 真客户报告上顶部显示「真实问题 1 个」—— 那个 1 是**关键词数**,
    而同页方法说明写「本次实测 3 个问题」、证据矩阵 12 = 3×4 与后者自洽。
    同一页上两个数打架,顶部那个与谁都对不上。

    夹具刻意让**题数 ≠ 关键词数**(3 道题 / 1 个关键词)——
    两者相等的夹具对这个缺陷天生没有分辨力。
    """
    rows = [(q, p) for q in ("问题甲", "问题乙", "问题丙")
            for p in ("deepseek", "qwen", "kimi", "doubao")]
    payload = _presentation(rows, keyword_count=1)
    summary = payload["summary"]
    assert "testedQuestionCount" in summary, (
        "后端没下发 testedQuestionCount —— 前端只能自己拿别的数顶上,"
        "这正是 #700 那个 1 的来路:%r" % sorted(summary))
    assert summary["testedQuestionCount"] == 3, (
        "发的是 %r,而本次实测 3 道题(关键词只有 1 个)" % summary["testedQuestionCount"])


def test_the_headline_count_agrees_with_the_methodology_sentence():
    """🔴 顶部那个数与方法说明那句**同源** —— 同一页上不许再出现两个问题数。

    这一条比上一条硬:上一条只说"发了题数",这一条说"和另一处印的是同一个数"。
    两处各自取数才是 #700 的真正成因。
    """
    rows = [(q, p) for q in ("问题甲", "问题乙", "问题丙")
            for p in ("deepseek", "qwen", "kimi", "doubao")]
    payload = _presentation(rows, keyword_count=1)
    tested_scope = payload["methodology"]["testedScope"] or ""
    assert "3 个问题" in tested_scope, tested_scope
    assert str(payload["summary"]["testedQuestionCount"]) + " 个问题" in tested_scope, (
        "顶部 %r 与方法说明 %r 不是同一个数"
        % (payload["summary"]["testedQuestionCount"], tested_scope))


def test_no_observation_leaves_the_count_null_not_zero():
    """一条观测都没有 ⇒ `null`,不是 0(契约:缺失一律 null,不得用 0 补齐)。"""
    payload = _presentation([], keyword_count=5)
    assert payload["summary"]["testedQuestionCount"] is None, payload["summary"]
