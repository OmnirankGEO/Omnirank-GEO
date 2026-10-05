"""#147-B · 出题生成器的**品牌侧输入**两侧同源,且不许再分家。

## 改之前

`analyze_client_business` 两个调用点喂的东西**严格不同**:预览端跑
`keywords=[]`、无 `client_location`、无 `flywheel_material` —— 正是工作流注释里
写的那个「只能靠品牌名瞎猜(驰鲸案例 0 命中根因之一)」的配置。

## 为什么不是「把预览端的参数补齐」

补齐只修**今天这一次**。下次有人给出题器加第 6 个品牌侧输入,两边照样分家,
**而且不会有任何东西变红** —— 那正是本包要消灭的那根轴:
两侧从**同一处**取值,再用数据流锁钉住「谁也别手写」。
"""

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _fn(path, name):
    """取某个函数的 AST 节点。"""
    src = io.open(ROOT / path, encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError("%s 里没有 %s —— 分母塌了,不是通过" % (path, name))


def _generator_calls(path):
    """**按 AST 取 Call 节点**,不数名字出现次数。

    🔴 裸串会被 `import` 行顶住(#139 栽过):`from x import analyze_client_business`
       让名字始终在场,毒把调用删了判据照样绿。
    """
    src = io.open(ROOT / path, encoding="utf-8").read()
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nm == "analyze_client_business":
                out.append(n)
    return out


def _is_question_site(call):
    """出题调用点 vs 关键词预填调用点。

    `server.py` 还有一个**关键词预填**端点也调生成器(它只要
    `search_keyword_groups`,不出题)—— 它不在本锁的分母里。
    """
    kws = {k.arg for k in call.keywords if k.arg}
    return "business_scope" in kws or "question_framing" in kws


# ---------------------------------------------------------------- 约束 6:数据流锁

#: 出题调用点**允许**手写的显式实参 —— 白名单,不是黑名单。
#:
#: 🔴 用黑名单(「不许手写 BRAND_SIDE_KEYS 里的键」)对**新增**的第 6 个品牌侧输入
#:    结构性失明:新键不在旧集合里,锁一声不吭 —— 而那正是本包要防的那件事。
#:    白名单反过来:任何新实参都会红,加它的人**必须**先决定
#:    「这是表单期的(进白名单)还是品牌侧的(进 helper)」。
#:    这条红是**设计意图**,不是误报。
_ALLOWED_EXPLICIT = {
    "brand_name", "industry",        # helper 的**输入**,不是它的产出
    "additional_info",               # 表单期:用户填的补充说明 / 上传文件
    "business_scope",                # 表单期:全国 / 区域
    "question_framing",              # 🔴 两侧刻意不同的那一项(见下方正向锁)
}


def test_both_call_sites_unpack_the_shared_brand_side_dict():
    """🔴 承重锁:两个调用点的品牌侧实参**一律**从共享那份展开。

    毒 A:给工作流加第 6 个品牌侧输入(任何新的显式实参)⇒ 红。
    毒 B:把 `**_brand_side` 改回手写 `keywords=keywords` ⇒ 红。
    """
    from services.diagnosis_generator_inputs import BRAND_SIDE_KEYS

    # 白名单与品牌侧键集**不许相交** —— 相交就等于给某个品牌侧输入开了手写后门。
    assert not (_ALLOWED_EXPLICIT & set(BRAND_SIDE_KEYS)), (
        "白名单里混进了品牌侧键:%r"
        % sorted(_ALLOWED_EXPLICIT & set(BRAND_SIDE_KEYS)))

    sites = {"server.py": _generator_calls("server.py"),
             "workflows/diagnosis_workflow.py":
                 _generator_calls("workflows/diagnosis_workflow.py")}

    total = 0
    for path, calls in sites.items():
        qs = [c for c in calls if _is_question_site(c)]
        assert qs, "%s 里没有出题调用点 —— 分母塌了,不是通过" % path
        for c in qs:
            total += 1
            explicit = {k.arg for k in c.keywords if k.arg}
            extra = explicit - _ALLOWED_EXPLICIT
            assert not extra, (
                "%s 的出题调用点手写了实参 %r。若它是**品牌侧**输入,必须进 helper "
                "(否则预览端拿不到,两侧再次分家而没有任何东西变红);"
                "若它是**表单期**才有的,把它加进 _ALLOWED_EXPLICIT 并说明理由。"
                % (path, sorted(extra)))
            assert any(k.arg is None for k in c.keywords), (
                "%s 的出题调用点没有 `**` 展开共享那份" % path)
    assert total >= 2, "出题调用点少于 2 个 —— 分母塌了:%d" % total


def test_the_key_tuple_equals_what_the_helper_actually_returns():
    """`BRAND_SIDE_KEYS` 必须**恰等于** helper 返回的 kwargs 键集。

    两者漂开时:锁按旧集合比,新加的那一项**不在任何判据的分母里**。
    """
    import services.diagnosis_generator_inputs as gi

    fn = _fn("services/diagnosis_generator_inputs.py", "brand_side_generator_inputs")
    dicts = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]
    keyed = [{k.value for k in d.keys if isinstance(k, ast.Constant)} for d in dicts]
    assert set(gi.BRAND_SIDE_KEYS) in keyed, (
        "BRAND_SIDE_KEYS=%r 与函数体里的 kwargs 字典对不上:%r"
        % (list(gi.BRAND_SIDE_KEYS), keyed))


# ------------------------------------------------- 约束 4:工作流侧是行为保持的抽取

def _call_helper(**kw):
    import services.diagnosis_generator_inputs as gi
    return asyncio.run(gi.brand_side_generator_inputs(**kw))


async def _none():
    return None


@pytest.fixture
def no_db(monkeypatch):
    """把库读与飞轮换成桩:本组验的是**取值来源与透传**,不是库。"""
    import services.diagnosis_business_scope as bs
    import services.diagnosis_generator_inputs as gi
    monkeypatch.setattr(bs, "resolve_brand_cities", lambda bid: "深圳市南山区")
    monkeypatch.setattr(gi, "_flywheel_material", lambda *a, **k: _none())
    return True


def test_given_keywords_pass_through_verbatim(no_db):
    """🔴 行为保持那一格:调用方给的词**原样透传**,不重解析。"""
    kwargs, meta = _call_helper(brand_id=1, brand_name="B", industry="I",
                                client_location="深圳", keywords=["甲", "乙"])
    assert kwargs["keywords"] == ["甲", "乙"]
    assert meta["keyword_source"] == "given"


def test_empty_keywords_are_not_silently_re_resolved(no_db, monkeypatch):
    """🔴🔴 抽取最容易踩的那一格:调用方给了**空列表**。

    今天工作流把 `keywords=[]` **原样**喂给生成器。若 helper 在这里替它跑阶梯,
    「调用方给了空词」就变成「按品牌名重新造词」甚至抛 `NoKeywordSource` ——
    那是**行为改变**,不是抽取。而顺利路径上两者读数完全相同(都返回了一个
    非空 kwargs),只有喂进 LLM 的词不同 ⇒ 行为臂看不见。

    毒:把 `resolve_missing_keywords` 的默认值翻成 True ⇒ 本条红。
    """
    import services.diagnosis_generator_inputs as gi

    def _boom(*a, **k):
        raise AssertionError("工作流侧不许重解析关键词")
    monkeypatch.setattr(gi, "_resolve_keywords", _boom)

    kwargs, meta = _call_helper(brand_id=1, brand_name="B", industry="I",
                                client_location="深圳", keywords=[])
    assert kwargs["keywords"] == []
    assert meta["keyword_source"] == "given"


def test_preview_side_opts_in_to_the_ladder(no_db, monkeypatch):
    """预览端**显式**置 True 才走阶梯 —— 正样本臂,证上一条不是恒真。"""
    import services.diagnosis_generator_inputs as gi
    monkeypatch.setattr(gi, "_resolve_keywords",
                        lambda *a, **k: (["阶梯词"], "last_diagnosis"))
    kwargs, meta = _call_helper(brand_id=1, brand_name="B", industry="I",
                                client_location="深圳", keywords=None,
                                resolve_missing_keywords=True)
    assert kwargs["keywords"] == ["阶梯词"]
    assert meta["keyword_source"] == "last_diagnosis"


def test_workflow_call_site_does_not_opt_into_re_resolution():
    """结构臂:工作流那个调用点必须**显式** `resolve_missing_keywords=False`。

    🔴 这个性质在顺利路径上**不可观测**(词非空时两种取值产出相同),
       所以只能打源码 —— 它不是补充,它才是承重的那条。
    """
    fn = _fn("workflows/diagnosis_workflow.py", "run_diagnosis_workflow")
    body = ast.unparse(fn)
    assert "brand_side_generator_inputs(" in body, "工作流没接共享那份"
    assert "resolve_missing_keywords=False" in body, (
        "工作流没有显式关掉重解析 —— 默认值一旦被改,它会跟着变")


# ----------------------------------------- 约束 1:framing 的两侧差异必须**保留**

def test_the_two_sides_deliberately_differ_on_question_framing():
    """🔴 正向锁:`question_framing` 只在**预览端**出现,工作流不传。

    它是增长/防守两侧的**来源**,不是「没对齐」。哪天有人为了「统一」
    把它也塞进共享那份,两侧就没了 —— 本条红。
    """
    from services.diagnosis_generator_inputs import BRAND_SIDE_KEYS
    assert "question_framing" not in BRAND_SIDE_KEYS, (
        "framing 被并进品牌侧共享输入了 —— 增长/防守两侧会塌成一侧")

    prev = [c for c in _generator_calls("server.py")
            if any(k.arg == "question_framing" for k in c.keywords)]
    assert prev, "预览端不再传 framing —— 防守题产不出来了"

    for c in _generator_calls("workflows/diagnosis_workflow.py"):
        assert not any(k.arg == "question_framing" for k in c.keywords), (
            "工作流开始传 framing 了 —— 它取默认才是实跑口径")


# --------------------------------------------- 约束 2:缓存键覆盖每一个会改输出的输入

_BASE_SIDE = {"keywords": ["词"], "client_location": "深圳",
              "brand_cities": "深圳", "flywheel_material": None}


def test_cache_key_moves_when_the_resolved_keywords_move():
    """🔴 键必须随**已解析关键词**变化。

    毒:把 `brand_side` 从键里去掉 ⇒ 本条红。
    不加这条的话,「用户又跑了一次诊断 ⇒ 词变了」不会改变键,
    30 分钟内继续返旧题,**而屏幕上一切正常**。
    """
    import server
    brand = {"name": "B", "industry": "I", "cities": "深圳"}
    a = server._suggest_cache_key(brand, "full", "regional",
                                  {**_BASE_SIDE, "keywords": ["旧词"]})
    b = server._suggest_cache_key(brand, "full", "regional",
                                  {**_BASE_SIDE, "keywords": ["新词"]})
    assert a != b, "词换了键没动 —— 她会拿到上一次的题"


@pytest.mark.parametrize("field,other", [
    ("client_location", "广州"),
    ("brand_cities", "广州市天河区"),
    ("flywheel_material", {"source": "x"}),
])
def test_cache_key_moves_for_every_brand_side_input(field, other):
    """逐项枚举:**每一个**品牌侧输入都要进键,不是只有关键词。"""
    import server
    brand = {"name": "B", "industry": "I", "cities": "深圳"}
    a = server._suggest_cache_key(brand, "full", "regional", _BASE_SIDE)
    b = server._suggest_cache_key(brand, "full", "regional",
                                  {**_BASE_SIDE, field: other})
    assert a != b, "%s 变了键没动" % field


def test_the_parametrized_fields_cover_every_brand_side_key():
    """机械核对:参数化字段 + keywords **恰好**覆盖 BRAND_SIDE_KEYS。

    🔴 手写分母漏掉的那一项不会让任何判据变红 —— 所以分母要从 SSOT 反查。
    """
    from services.diagnosis_generator_inputs import BRAND_SIDE_KEYS
    assert set(_BASE_SIDE) == set(BRAND_SIDE_KEYS), (
        "品牌侧键集变了,缓存键判据的分母没跟上:%r vs %r"
        % (sorted(_BASE_SIDE), sorted(BRAND_SIDE_KEYS)))
