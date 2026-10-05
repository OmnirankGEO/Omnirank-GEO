"""诊断链桥接点的品牌归属 —— 「admin 代跑他人品牌复制出一条同名副本」。

生产实证(3edfc0031,03:41:36):admin(112)从下拉选**已有**品牌 812(owner 113)
跑防御诊断,一分钟后 ``brands`` 多出 id=936 同名、``owner_user_id=112``。

机制(三处拼起来):
  · ``workflows/diagnosis_workflow.py`` ``effective_owner_user_id``
    在非组织路径 = **发起人**;
  · 桥接点两处逐字同形:``int(brand_id) if organization_identity is not None
    else get_or_create_brand(..., owner_user_id=effective_owner_user_id)``
    —— ``organization_identity is None`` 时**把手上已有的 brand_id 丢掉**;
  · ``get_or_create_brand`` 按 **(name, owner_user_id)** 去重,跨 owner 同名合法
    ⇒ 不命中 ⇒ INSERT 新行。

判它是缺陷不是设计的硬证据:同一个三元表达式的**另一支**用的就是 ``int(brand_id)``,
且 ``run_diagnosis_workflow`` 的签名把该参数逐字写作
「现有品牌可信身份 SSOT;新建品牌可为空」。id 在手上,被丢掉了。
"""

from __future__ import annotations

import ast
import io
import os

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKFLOW_REL = os.path.join("workflows", "diagnosis_workflow.py")

OWNER = 113          # 品牌真正的属主(服务商)
INITIATOR = 112      # 代跑的人(admin)
NAME = "QA判据_代跑归属_20260831"


def _rows(cur) -> int:
    cur.execute("SELECT COUNT(*) AS n FROM public.brands")
    return int(cur.fetchone()["n"])


def _seed_existing_brand(cur) -> int:
    cur.execute(
        "INSERT INTO public.brands (name, owner_user_id, brand_type, industry) "
        "VALUES (%s,%s,'client','测试行业') RETURNING id", (NAME, OWNER))
    return int(cur.fetchone()["id"])


def test_the_criteria_really_talk_to_the_throwaway_db(cur):
    """活性自证:被测代码自己开的那条连接必须落在**本包**这一次性库上。

    🔴 ``get_or_create_brand`` 走 ``db.connection.get_connection()``,
       不收 cursor。它要是连到别的库,下面所有「行数」断言测的都是别人家的表,
       而四个信号(通过/无报错/有输出/退出 0)一个都不会异常。本仓记过这个形态。
    """
    from db import connection as _conn

    dsn = str(getattr(_conn, "DATABASE_URL", "") or os.environ.get("DATABASE_URL", ""))
    dbname = dsn.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    assert "diagbrand" in dbname and "test" in dbname, (
        f"被测代码连的是 {dbname!r},不是本包的一次性库 —— 判据没有对准对象")


# ══════════════════════════════════════════════════════════════════════════
# ① 现象锚:936 那一行是怎么长出来的(修前修后都绿 —— 它测的是原语的既有行为)
# ══════════════════════════════════════════════════════════════════════════
def test_reproduces_the_936_shape_when_the_known_id_is_dropped(cur):
    """把 brand_id 丢掉、改按「名字 + 发起人」建档 ⇒ 多出一条同名副本。

    🔴 如实标注:本条**修前修后都绿**。它锚的是"一旦这么调就会产生 936",
       不是回归锁 —— 真正守住修复的是下面 ②③ 与接线两条。
       没有它的话,后面那些"行数不变"的断言无法与"这个库根本插不进去"区分。
    """
    from db.diagnosis_db import get_or_create_brand

    original = _seed_existing_brand(cur)
    before = _rows(cur)

    dup = get_or_create_brand(NAME, "测试行业", None, owner_user_id=INITIATOR)

    assert _rows(cur) == before + 1, "按名+发起人建档竟然没多出行 —— 现象锚失效"
    assert dup != original, (dup, original)
    cur.execute("SELECT owner_user_id FROM public.brands WHERE id=%s", (dup,))
    assert int(cur.fetchone()["owner_user_id"]) == INITIATOR


# ══════════════════════════════════════════════════════════════════════════
# ② 行为:brand_id 已知 ⇒ 一行都不许多
# ══════════════════════════════════════════════════════════════════════════
def test_a_known_brand_id_creates_no_new_row_and_is_returned_as_is(cur):
    from workflows.diagnosis_workflow import _resolve_bridge_brand_id

    original = _seed_existing_brand(cur)
    before = _rows(cur)

    got = _resolve_bridge_brand_id(
        brand_id=original, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)

    assert got == original, (
        f"传入的可信身份 {original} 没被采用,返回了 {got} —— "
        f"下游 v2 报告 / stage log / 信任资产会挂到别的品牌上")
    assert _rows(cur) == before, (
        f"brand_id 已知却仍建了档:{before} → {_rows(cur)}(生产上就是 936 那一行)")


def test_a_known_brand_id_wins_even_when_the_initiator_differs(cur):
    """代跑场景本身:发起人 ≠ 属主,仍必须用传入的 id。"""
    from workflows.diagnosis_workflow import _resolve_bridge_brand_id

    original = _seed_existing_brand(cur)
    before = _rows(cur)
    got = _resolve_bridge_brand_id(
        brand_id=original, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)
    assert got == original
    assert _rows(cur) == before
    cur.execute("SELECT owner_user_id FROM public.brands WHERE id=%s", (got,))
    assert int(cur.fetchone()["owner_user_id"]) == OWNER, (
        "品牌属主被改写了 —— 本修复只做定位,不许动归属")


# ══════════════════════════════════════════════════════════════════════════
# ③ 反向臂:真的没有 brand_id 时,兜底建档**必须还在**(防修法退化成砍功能)
# ══════════════════════════════════════════════════════════════════════════
def test_without_a_brand_id_the_fallback_still_creates_one(cur):
    from workflows.diagnosis_workflow import _resolve_bridge_brand_id

    before = _rows(cur)
    got = _resolve_bridge_brand_id(
        brand_id=None, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)
    assert isinstance(got, int) and got > 0
    assert _rows(cur) == before + 1, "没有 id 时不建档 = 把新品牌诊断整条砍了"


def test_the_fallback_reuses_the_same_owner_row_on_a_rerun(cur):
    """兜底不是"每次都插" —— 同一 owner 同名重跑必须复用。

    没有这一条,③ 与「无条件 INSERT」无法区分。
    """
    from workflows.diagnosis_workflow import _resolve_bridge_brand_id

    first = _resolve_bridge_brand_id(
        brand_id=None, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)
    after_first = _rows(cur)
    second = _resolve_bridge_brand_id(
        brand_id=None, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)
    assert second == first
    assert _rows(cur) == after_first


@pytest.mark.parametrize("falsy", [None, 0, ""])
def test_every_falsy_brand_id_falls_through_to_the_fallback(cur, falsy):
    """「没有 id」的**全部**表达形式都要走兜底。

    🔴 分母是 falsy 的三种真实来源:未传(None)、前端 `?? 0` 兜出来的 0、
       以及空串。只钉 None 的话,`0` 会被 `int(0)` 当成一个**存在的品牌 id**
       传给下游 —— 那是比建副本更糟的一种错(挂到不存在的品牌上)。
    """
    from workflows.diagnosis_workflow import _resolve_bridge_brand_id

    before = _rows(cur)
    got = _resolve_bridge_brand_id(
        brand_id=falsy, brand_name=NAME, industry="测试行业",
        industry_category=None, owner_user_id=INITIATOR)
    assert got > 0
    assert _rows(cur) == before + 1


# ══════════════════════════════════════════════════════════════════════════
# ④ 接线:两处桥接点必须调**同一个** resolver,且不得自己去建档
# ══════════════════════════════════════════════════════════════════════════
def _workflow_ast():
    src = io.open(os.path.join(REPO, WORKFLOW_REL), encoding="utf-8",
                  newline="").read()
    return src, ast.parse(src)


def _enclosing_functions(tree):
    """node -> 它所在的最内层函数名。用于把"谁在调"钉成同一性,不是包含判定。"""
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner.setdefault(node, fn.name)
    return owner


def test_both_bridge_sites_use_the_single_resolver():
    """``bridge_brand_id`` 的**每一次**赋值,右手边必须就是那次 resolver 调用。

    🔴 写**同一性**不写包含:断言 RHS 这个节点本身是
       ``_resolve_bridge_brand_id(...)`` 的 Call,而不是"这一段里出现过它"。
       包含判定挡不住 ``x = old_expr or _resolve_bridge_brand_id(...)`` 这种短路壳。
    """
    _src, tree = _workflow_ast()
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "bridge_brand_id"
                for t in node.targets)
    ]
    assert len(sites) >= 2, (
        f"只找到 {len(sites)} 处 bridge_brand_id 赋值 —— 分母塌了,"
        f"本条会因为「没有反例」而恒绿")
    bad = []
    for node in sites:
        rhs = node.value
        ok = (isinstance(rhs, ast.Call)
              and isinstance(rhs.func, ast.Name)
              and rhs.func.id == "_resolve_bridge_brand_id")
        if not ok:
            bad.append(ast.dump(rhs)[:120])
    assert not bad, (
        f"有桥接点没走单点 resolver(共 {len(sites)} 处):{bad}。"
        f"同一谓词写两处,必有一处没人验 —— 本缺陷就是这么来的")

    # 🔴 [P1-2 · Codex 终审] 光锁"右手边是那个调用"**不够**:
    #    把第一处改成 ``brand_id=None`` 其余不动,上面全部断言照旧通过,11 条全绿
    #    —— 而那正是本缺陷的形状(手上有可信 id 却不用)。本仓记过:
    #    「AST 锁证不了 kwarg 绑定」。所以这里逐个 keyword 钉**同一性**:
    #    形参名 → 实参必须是同名的那个 Name,不许是 None / 常量 / 别的变量。
    EXPECTED = {
        "brand_id": "brand_id",                      # ← 可信身份 SSOT,丢了就是本缺陷
        "brand_name": "brand_name",
        "industry": "industry",
        "industry_category": "industry_category",
        "owner_user_id": "effective_owner_user_id",
    }
    for i, node in enumerate(sites):
        call = node.value
        assert not call.args, (
            f"第 {i + 1} 处用了位置实参 —— resolver 是 keyword-only,"
            f"位置实参会在运行时炸,但更要紧的是它绕开了下面这套逐名核对")
        got = {}
        for kw in call.keywords:
            assert kw.arg is not None, f"第 {i + 1} 处出现 **kwargs 展开,无法逐名核对"
            got[kw.arg] = kw.value
        assert set(got) == set(EXPECTED), (
            f"第 {i + 1} 处的实参名集合变了:{sorted(got)} != {sorted(EXPECTED)}")
        for name, want in EXPECTED.items():
            v = got[name]
            assert isinstance(v, ast.Name) and v.id == want, (
                f"第 {i + 1} 处 {name}= 传的不是 {want},而是 "
                f"{ast.dump(v)[:80]} —— 本缺陷(brands.id=936 被复制出同名副本)"
                f"就是 brand_id 这一格没传对造成的")


def test_only_the_resolver_may_create_a_brand_in_this_module():
    """``get_or_create_brand`` 在本模块里**只能**出现在 resolver 内部。

    分母 = 本模块 AST 里**每一个**叫这个名字的节点(含 import 那一行),
    逐个回溯它所在的最内层函数。这样"换个地方再抄一次"当场红。
    """
    _src, tree = _workflow_ast()
    owner = _enclosing_functions(tree)
    seen, outside = [], []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.asname or node.name
        if name != "get_or_create_brand":
            continue
        where = owner.get(node, "<模块层>")
        seen.append(where)
        if where != "_resolve_bridge_brand_id":
            outside.append(where)
    # 分母活性:一个都找不到说明尺子坏了(名字改了/解析没走到),不是"很干净"。
    assert seen, "本模块一次都没提到 get_or_create_brand —— 分母塌了,本条恒绿"
    assert not outside, (
        f"这些位置绕过 resolver 直接建档:{sorted(set(outside))};"
        f"全部出现处 = {sorted(set(seen))}")


@pytest.mark.parametrize("site_index", [0, 1])
def test_each_bridge_site_really_hands_the_known_id_to_the_resolver(site_index):
    """行为臂:把桥接点那条语句**原样取出来执行**,看 resolver 到底收到了什么。

    🔴 为什么不是"再写一遍 AST 断言":上面那条锁的是**源码结构**,
       它答的是"写得对不对";这条答的是"跑起来传的是什么"。两者会分开坏:
       结构对而实参在运行时被别的东西改掉、或者将来有人把 kwarg 换成
       ``brand_id=(brand_id if X else None)`` —— 那仍是个 Name?不是,
       但如果换成 ``brand_id=_alias`` 而 ``_alias = None``,结构锁看不出来。

    🔴 为什么不跑 ``run_diagnosis_workflow``:它要 LLM、要外部检索、要整条链,
       在判据里跑不动。所以取的是**生产文件里那条语句的源码本身**
       (``ast.get_source_segment``,不是我重写的等价物),在一个受控命名空间里执行:
       局部名全给哨兵值,``_resolve_bridge_brand_id`` 换成间谍。
       语句一个字都没被我改写,所以"它传了什么"就是生产传什么。
    """
    src, tree = _workflow_ast()
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "bridge_brand_id"
                for t in node.targets)
    ]
    sites.sort(key=lambda n: n.lineno)
    assert len(sites) >= 2, f"桥接点只有 {len(sites)} 处 —— 分母塌了"

    import textwrap
    stmt = textwrap.dedent(ast.get_source_segment(src, sites[site_index]))
    assert "_resolve_bridge_brand_id" in stmt, stmt

    received = {}

    def _spy(**kwargs):
        received.update(kwargs)
        return 4242

    SENTINELS = {
        "brand_id": 936,                      # 生产实证那一条:必须原样到达
        "brand_name": "全域上榜(深圳)科技有限公司",
        "industry": "AI搜索优化/GEO服务",
        "industry_category": "b2b_service",
        "effective_owner_user_id": 113,
    }
    ns = dict(SENTINELS)
    ns["_resolve_bridge_brand_id"] = _spy
    exec(compile(stmt, "<bridge-site>", "exec"), ns)   # noqa: S102 —— 就是要跑生产那一行

    assert received, "resolver 一次都没被调到 —— 这条判据没有驱动到被测的那一行"
    assert received.get("brand_id") == 936, (
        f"桥接点传给 resolver 的 brand_id 是 {received.get('brand_id')!r},不是手上那个 936 —— "
        f"这正是 brands.id=936 被复制出同名副本的成因")
    assert received.get("owner_user_id") == 113, received
    assert received.get("brand_name") == SENTINELS["brand_name"], received
    assert ns["bridge_brand_id"] == 4242, "resolver 的返回值没被接住"
