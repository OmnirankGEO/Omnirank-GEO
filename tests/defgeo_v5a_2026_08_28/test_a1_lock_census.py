"""【A-1 = Codex fix-of-fix2 P1-3】canonical quote 锁漏掉 archive / restore 写者。

Codex 原文
----------
    本轮新增的 brand advisory lock 覆盖了 3 个 quote INSERT 入口,但
    ``canonical_quote_id()`` 还依赖 ``deleted_at``。``archive_quote`` /
    ``restore_quote`` 会改变该谓词,却没有拿同一把锁。

    真 PG16 确定性并发反例:轮换通过 canonical guard 后阻塞;并发 archive 提交;
    轮换随后仍成功给 quote 6 签发新 token,但此时 canonical 已变成 quote 5。
    旧 token 被撤销,新 active token 却指向已归档 quote 6。

我先证伪、再机械枚举的结果(与工单/Codex 都有出入,如实记在这里)
------------------------------------------------------------------
工单点了 3 个谓词写者(``artifact_archive`` 两处 + Review 补的
``brand_api.py:1863`` 品牌恢复级联)。我按「全仓生产 ``.py`` 里所有写 quotes
的 SQL 字面量」机械枚举,得到的是 **4 处**:

  ① ``api/brand_api.py::delete_client``            —— 品牌软删级联 ``deleted_at=CURRENT_TIMESTAMP``
  ② ``api/brand_api.py::restore_deleted_client``   —— 品牌恢复级联 ``deleted_at=NULL``(= Review 补的那处)
  ③ ``services/artifact_archive.py::archive_quote``
  ④ ``services/artifact_archive.py::restore_quote``

**①是 Codex 与 Review 都没列的那一处**。它和②在同一个文件里,方向相反:
一个把整批报价打成已删,一个把它们放回来 —— 两个都翻 canonical 谓词。
这正是「手写清单会漏,而漏掉的那一处不会让任何判据变红」:
如果我照工单的 3 条改,①会一直是敞开的窗口,且没有任何东西会红。

所以本判据的形态是**集合相等**,不是"这几处有锁"。
"""
from __future__ import annotations

import pytest

# 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-4] 按**文件路径**载入,不走
#    `from tests.defgeo_v5a_2026_08_28 import _census`。
#    反例:宿主装了一个正规的 `site-packages/tests` 包、而本仓 `tests/` 又不是包时,
#    那句 import 会解析到**别人的** tests,collection 直接 ModuleNotFoundError。
#    失败关闭不是假绿,但它会让整包在别的机器上跑不起来。
#    路径载入对 sys.path 顺序免疫;载入后再断言它确实来自本仓(见 test_00)。
import importlib.util as _ilu
import pathlib as _pl

_spec = _ilu.spec_from_file_location(
    "defgeo_v5a_census", _pl.Path(__file__).with_name("_census.py"))
C = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(C)


# ══════════════════════════════════════════════════════════════════════════
# 冻结集 —— 每一项都写清"为什么它在这里"
# ══════════════════════════════════════════════════════════════════════════
#: canonical 谓词写者。集合**相等**,不是包含:新增一处必须由人过一眼。
FROZEN_PREDICATE_WRITERS = {
    ("api/brand_api.py", "delete_client"),
    ("api/brand_api.py", "restore_deleted_client"),
    ("services/artifact_archive.py", "archive_quote"),
    ("services/artifact_archive.py", "restore_quote"),
}

#: 建报价入口(V4-A 上锁时三处;[开源 E3 · B3a · 2026-09-28] C 端计划建写作项目那处随端点删除,剩两处)。
#: canonical 也被"多一份报价"改变。
FROZEN_INSERT_SITES = {
    ("db/diagnosis_db.py", "save_quote"),
    ("server.py", "api_quick_create_writing_project"),
}

#: SET 子句由 f-string 拼出来的站点。扫描器看不见它写哪些列,
#: 所以单列出来 + 配一条"它拼得出来的列名里没有谓词列"的伴随断言。
FROZEN_DYNAMIC_SET = {
    ("services/quote_numeric_repair.py", "repair_quote_numeric_fields"),
}

#: 字符串里出现「UPDATE quotes」但后面根本没有 SET —— 是散文不是 SQL。
#: 钉住它是为了:将来再多一处散文,得有人确认那真是散文,而不是一条被拼接切断的真 SQL。
#: [开源 E3 · WO_323 G3a · 2026-10-02] 原唯一一处(一键激活端点的 docstring)随端点删,集合现为空;多出任何一处照样红。
FROZEN_PROSE: set = set()


@pytest.fixture(scope="module")
def sites():
    return C.quotes_write_sites()


# ══════════════════════════════════════════════════════════════════════════
# 00 · 分母活性 —— 先证明扫描器真的扫到了东西
# ══════════════════════════════════════════════════════════════════════════
def test_00a_census_module_comes_from_this_repo() -> None:
    """载进来的 `_census` 必须是**本仓这一份**。

    路径载入已经排除了第三方 `tests` 包遮蔽,这条把结果钉死:
    万一哪天有人把它改回名字 import,而机器上又恰好有个同名包,
    判据会拿着别人的扫描器给本仓打分 —— 那种绿最贵。
    """
    import pathlib

    here = pathlib.Path(__file__).resolve()
    mod = pathlib.Path(C.__file__).resolve()
    assert mod.parent == here.parent, "_census 不是本目录这一份:%s" % mod
    assert C.REPO == here.parents[2], "_census.REPO 指向了别的树:%s" % C.REPO


def test_00_denominator_is_alive(sites) -> None:
    """零分母恒绿是本仓最贵的假绿形态。先把分母本身钉住。"""
    updates = [s for s in sites if s.verb == "UPDATE"]
    inserts = [s for s in sites if s.verb == "INSERT"]
    assert len(updates) >= 60, "UPDATE quotes 只扫到 %d 处 —— 分母塌了" % len(updates)
    assert len(inserts) == 2, "INSERT INTO quotes 扫到 %d 处(期望 2)" % len(inserts)
    assert len({s.rel for s in sites}) >= 8, "写 quotes 的文件只有 %d 个 —— 分母塌了" % len(
        {s.rel for s in sites})


def test_01_scanner_has_discriminating_power() -> None:
    """扫描器自证:合成一条**没上锁的谓词写者**,它必须被认出来。

    自己写的工具没被验过,等于把结论建在一个从没红过的东西上。
    这里同时打三种它必须区分的形态。
    """
    good = C.scan_source("fake.py", "\n".join([
        "def evil(cur, b):",
        '    cur.execute("""UPDATE quotes SET deleted_at=NULL WHERE brand_id=%s""", (b,))',
    ]))
    assert [s.func for s in C.predicate_writers(good)] == ["evil"], \
        "谓词写者认不出来 —— 扫描器没有区分力"

    # ② 只改非谓词列的,不该被认成谓词写者
    benign = C.scan_source("fake.py", "\n".join([
        "def ok(cur, q):",
        '    cur.execute("UPDATE quotes SET writing_status=%s WHERE id=%s", (1, q))',
    ]))
    assert C.predicate_writers(benign) == [], "把非谓词写者也算进来了 —— 会把人引去改无关的地方"

    # ③ 子查询里的 WHERE 不能当断句点,否则它后面的赋值列会被漏掉(漏 = 假绿)
    nested = C.scan_source("fake.py", "\n".join([
        "def tricky(cur, q):",
        '    cur.execute("""UPDATE quotes SET total_keywords = (',
        "        SELECT count(*) FROM confirmed_keywords k WHERE k.quote_id=quotes.id",
        '    ), deleted_at = NULL WHERE id=%s""", (q,))',
    ]))
    assert [s.func for s in C.predicate_writers(nested)] == ["tricky"], \
        "括号深度没算对 —— 子查询后面的 deleted_at 被漏掉了"

    # ④ 散文(没有 SET)必须被标出来,而不是当成一条列为空的 SQL 悄悄放过
    prose = C.scan_source("fake.py", '"""这个函数一次性 UPDATE quotes 的若干字段。"""')
    assert prose and prose[0].has_set is False, "散文没被标成 has_set=False"


# ══════════════════════════════════════════════════════════════════════════
# 02 · 谓词写者集合相等 + 每一处都在事务内拿得到锁
# ══════════════════════════════════════════════════════════════════════════
def test_02_predicate_writer_set_is_frozen(sites) -> None:
    got = {(s.rel, s.func) for s in C.predicate_writers(sites)}
    assert got == FROZEN_PREDICATE_WRITERS, (
        "写 quotes 且触及 canonical 谓词列(deleted_at/brand_id/created_at/id)的站点集合变了。\n"
        "  新增: %s\n  消失: %s\n"
        "新增的每一处都能改变「这个品牌当前那一份报价是哪一份」,必须拿品牌级序列化点;"
        "消失的那一处要确认是真删了还是被改写得扫不出来了。"
        % (sorted(got - FROZEN_PREDICATE_WRITERS), sorted(FROZEN_PREDICATE_WRITERS - got)))


@pytest.mark.parametrize("rel,func", sorted(FROZEN_PREDICATE_WRITERS))
def test_03_every_predicate_writer_takes_the_brand_lock(rel, func) -> None:
    """逐档参数化 —— 不是"其中有几处拿了",是**每一处**都拿。"""
    assert func in C.functions_reaching_lock(rel), (
        "%s::%s 翻 canonical 谓词却拿不到品牌级序列化点。\n"
        "轮换可以在它的 canonical 复核之后、写 token 之前被这一处插进来 ——"
        "于是新签的 active token 指向一个刚被归档(或刚被恢复而不再 canonical)的报价。"
        % (rel, func))


@pytest.mark.parametrize("rel,func", sorted(FROZEN_INSERT_SITES))
def test_04_every_insert_site_still_takes_the_brand_lock(rel, func) -> None:
    """V4-A 那三处不许回退。本条是**存量保护**,不是本单新增能力。"""
    assert func in C.functions_reaching_lock(rel), "%s::%s 的建报价锁被摘了" % (rel, func)


def test_05_insert_site_set_is_frozen(sites) -> None:
    got = {(s.rel, s.func) for s in sites if s.verb == "INSERT"}
    assert got == FROZEN_INSERT_SITES, (
        "建报价入口集合变了(新增: %s / 消失: %s)—— 新入口不拿锁,窗口就从那里漏回来"
        % (sorted(got - FROZEN_INSERT_SITES), sorted(FROZEN_INSERT_SITES - got)))


# ══════════════════════════════════════════════════════════════════════════
# 06 · 锁序:品牌锁必须排在本函数的行锁之前
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("rel,func", sorted(FROZEN_PREDICATE_WRITERS | FROZEN_INSERT_SITES))
def test_06_lock_is_acquired_before_the_statement_it_protects(rel, func, sites) -> None:
    """锁必须在**它保护的那条语句之前**拿到。对 7 处全部成立,零 skip。

    先写的是"品牌锁先于行锁",结果 7 档里 6 档 skip(要么锁在 helper 里、
    要么这个函数根本没有 FOR UPDATE 字面量)—— 那种判据只证明了它跑到的那一档。
    改成这个谓词之后每一档都真的在断言:**写下去的时候锁在手上**。
    """
    lock_at = C.lock_acquisition_line(rel, func)
    assert lock_at is not None, "%s::%s 里找不到拿锁的那一步" % (rel, func)
    stmt_lines = [s.lineno for s in sites if s.rel == rel and s.func == func]
    assert stmt_lines, "%s::%s 在分母里找不到它那条语句 —— 锚点过期" % (rel, func)
    assert lock_at < min(stmt_lines), (
        "%s::%s 在第 %d 行才拿锁,而它写 quotes 是在第 %d 行 —— 那条语句是裸的"
        % (rel, func, lock_at, min(stmt_lines)))


def test_06b_brand_lock_precedes_row_locks_where_both_exist() -> None:
    """锁序统一:**品牌级 advisory 锁排在行锁之前**。

    这条不是洁癖。锁序不一致是死锁的定义:A 先行锁后品牌锁、B 先品牌锁后行锁,
    两边就能互相等。而每条路径**最多只拿一个品牌**的这把锁,所以
    advisory↔advisory 之间不成环;只要它恒排在最前,与后面那些锁也不成环。

    分母 = 同时有(直接锁调用)和(FOR UPDATE/SHARE 字面量)的那些函数。
    分母本身也断言非空 —— 空集恒绿。
    """
    checked = []
    for rel, func in sorted(FROZEN_PREDICATE_WRITERS | FROZEN_INSERT_SITES):
        lock_at = C.lock_call_line(rel, func)
        row_at = C.first_row_lock_line(rel, func)
        if lock_at is None or row_at is None:
            continue
        checked.append((rel, func, lock_at, row_at))
        assert lock_at < row_at, (
            "%s::%s 先拿了行锁(第 %d 行)才拿品牌锁(第 %d 行)—— 锁序与别的路径反了"
            % (rel, func, row_at, lock_at))
    assert checked, "没有一处同时有直接锁调用与行锁字面量 —— 这条断言的分母是空的"


# ══════════════════════════════════════════════════════════════════════════
# 07 · 两个豁免集:动态 SET 与散文。冻结 + 各配一条真断言
# ══════════════════════════════════════════════════════════════════════════
def test_07_dynamic_set_sites_are_frozen(sites) -> None:
    got = {(s.rel, s.func) for s in sites if s.dynamic}
    assert got == FROZEN_DYNAMIC_SET, (
        "SET 由 f-string 拼出来的站点集合变了(新增: %s)。扫描器看不见它写哪些列 ——"
        "新增一处必须由人确认它拼不出谓词列。"
        % sorted(got - FROZEN_DYNAMIC_SET))


def test_08_dynamic_set_fragments_never_touch_predicate_columns() -> None:
    """豁免不是免检:那个函数**拼得出来的列名**里不许有谓词列。

    ``repair_quote_numeric_fields`` 是本仓记过的那种"通用写入器"形态
    (``UPDATE quotes SET {', '.join(updates)}``)。它今天只拼
    ``monthly_price`` / ``total_articles``;哪天有人 append 一句
    ``deleted_at = %s``,这条当场红。
    """
    import ast
    import pathlib
    import re

    rel, func = sorted(FROZEN_DYNAMIC_SET)[0]
    tree = ast.parse((C.REPO / rel).read_text(encoding="utf-8", errors="replace"))
    frag_cols: set[str] = set()
    seen_func = False
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name != func:
            continue
        seen_func = True
        for lineno, s, _dyn in C._string_nodes(n):
            for m in re.finditer(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*%s\s*$", s):
                frag_cols.add(m.group(1))
    assert seen_func, "%s 里找不到 %s —— 豁免集的锚点过期了" % (rel, func)
    assert frag_cols, "一个 `col = %s` 片段都没扫到 —— 断言恒真,等于没验"
    bad = frag_cols & C.PREDICATE_COLUMNS
    assert not bad, "%s::%s 现在拼得出谓词列 %s —— 它必须拿品牌锁" % (rel, func, sorted(bad))
    assert pathlib.Path(C.REPO / rel).is_file()


def test_09_prose_sites_are_frozen(sites) -> None:
    got = {(s.rel, s.func) for s in sites if s.verb == "UPDATE" and not s.has_set}
    assert got == FROZEN_PROSE, (
        "「字符串里写着 UPDATE quotes 但没有 SET」的站点集合变了(新增: %s)。"
        "新增的那一处得有人确认它真是散文,而不是被字符串拼接切断的一条真 SQL。"
        % sorted(got - FROZEN_PROSE))


# ══════════════════════════════════════════════════════════════════════════
# 10 · 没有别的动词能改这张表
# ══════════════════════════════════════════════════════════════════════════
def test_10_no_hard_delete_writer_exists() -> None:
    """``DELETE FROM quotes`` 也会翻 canonical 谓词 —— 现役一处都没有,钉住它。

    有一天真出现了,它同样得拿锁;而这条会先红,让人看见。
    """
    import re

    hits = []
    for p in C.production_py():
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            import ast
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for lineno, s, _dyn in C._string_nodes(tree):
            if re.search(r"DELETE\s+FROM\s+quotes\b", s, re.I):
                hits.append((p.relative_to(C.REPO).as_posix(), lineno))
    assert hits == [], "出现了硬删 quotes 的写者 %s —— 它也翻 canonical 谓词,必须拿锁" % hits
