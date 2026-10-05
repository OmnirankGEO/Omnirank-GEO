"""【A-1 类锁】``services/diagnosis_runs.py`` 里**每一处**冻结定位都必须走 payer 谓词。

为什么要机械枚举
----------------
payer ≠ owner 只在防御型 GEO 平台承担腿上发生,而"哪些地方会拿 user 去定位冻结"
散在十几处(结算 / 收尸 / 人工处置 / 双表去重 / 退款流水核验 / 降级扣费 / 拆分回读)。
**手抄一份清单漏掉的那一处不会让任何判据变红**(本仓 2026-08-19 实证)——
它只会在某个平台单上表现为"永远结不掉"或"收尸时查空→cancelled_no_freeze,
冻结原样挂着零退款"。

所以分母**从 AST 机械枚举**:凡是
  · 调 ``commit_freeze / release_freeze``(动钱原语),或
  · 调本模块三个冻结定位器(``_verify_freeze_exists`` / ``_locate_all_freezes`` /
    ``_double_table_locate``),或
  · ``cur.execute`` 的 SQL 里出现冻结表 / 双表模板
的调用点,一个不落地检查:**它的实参源码里不许出现 ``owner_user_id``**。

这条锁配了自己的**必须不命中**(见文件末尾):拿一段人造的"用了 owner"的源码
喂给同一个 census 函数,它必须判红。没有那一半,这把锁是死是活看不出来。
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

MODULE = "services/diagnosis_runs.py"

#: 动钱原语 + 本模块的冻结定位器。名字是**从被测模块里取的**,不是抄的:
#: 下面 ``test_census_denominator_is_not_empty`` 会证明这些名字真的还在。
_MONEY_CALLS = ("commit_freeze", "release_freeze")
_LOCATORS = ("_verify_freeze_exists", "_locate_all_freezes", "_double_table_locate")

#: 冻结表 / 双表 SQL 模板的识别面。``{table}`` 是 ``_FREEZE_TABLE`` 的 f-string 形态。
_FREEZE_SQL_MARKS = ("point_freezes", "customer_credit_freezes", "{table}")

#: payer 谓词允许的写法:直接调、或者一个明确叫 payer 的局部。
_PAYER_TOKENS = ("settlement_payer_user_id", "payer")


def _source():
    import services.diagnosis_runs as dr
    return inspect.getsource(dr)


def _sql_text(call: ast.Call) -> str:
    """把一次 ``execute`` 的第一个实参还原成可搜索的文本(拼接/f-string 都吃)。"""
    if not call.args:
        return ""
    out = []
    for node in ast.walk(call.args[0]):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.Name):
            out.append("{%s}" % node.id)
    return " ".join(out)


def census(source: str):
    """返回 [(标签, 该调用点的实参源码), ...] —— **分母由 AST 机械得出**。"""
    tree = ast.parse(source)
    lines = source.splitlines()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = None
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in _MONEY_CALLS:
            label = "money:" + name
        elif name in _LOCATORS:
            label = "locate:" + name
        elif name == "execute":
            sql = _sql_text(node)
            if any(mark in sql for mark in _FREEZE_SQL_MARKS):
                label = "sql:execute"
        if label is None:
            continue
        seg = "\n".join(lines[node.lineno - 1: (node.end_lineno or node.lineno)])
        found.append((label + "@L%d" % node.lineno, seg))
    return found


def test_census_denominator_is_not_empty():
    """🔴 分母自证:枚举不到东西的 census 是一把恒绿的锁。

    这条同时钉住三个定位器的**名字还在** —— 有人改名后 census 会静默变空,
    而空 census 与"全部合规"长得一模一样。
    """
    import services.diagnosis_runs as dr

    for name in _LOCATORS:
        assert hasattr(dr, name), (
            "定位器 %s 不在了 —— census 的识别面已经过期,这把锁正在恒绿" % name)
    hits = census(_source())
    assert len(hits) >= 10, (
        "只枚举到 %d 个冻结定位点,分母可疑(改名/重构会让 census 静默变空):%r"
        % (len(hits), [h[0] for h in hits]))
    kinds = {h[0].split("@")[0] for h in hits}
    assert {"money:commit_freeze", "money:release_freeze"} <= kinds, (
        "两个动钱原语至少各要枚举到一次:%r" % (sorted(kinds),))


def test_every_freeze_lookup_uses_the_payer_predicate():
    """本体:每一个枚举到的调用点都不许拿 owner 当 user。"""
    offenders = [(label, seg) for label, seg in census(_source())
                 if "owner_user_id" in seg]
    assert not offenders, (
        "这些冻结定位点仍然拿 owner_user_id 当 payer —— 平台承担腿的冻结不在 owner "
        "名下,它们会查空(结算永远结不掉 / 收尸误判成无冻结并 cancelled_no_freeze):\n"
        + "\n".join("  · %s\n%s" % (label, seg) for label, seg in offenders))


def test_money_primitives_are_fed_by_the_payer_predicate_by_name():
    """两个动钱原语更严一档:它们的 ``user_id=`` 必须**明确**来自 payer 谓词。

    "没出现 owner_user_id" 只是必要条件 —— 传一个恰好叫 ``uid`` 的东西同样能过。
    动钱那两处必须能一眼看出钱是从谁账上动的。
    """
    hits = [(label, seg) for label, seg in census(_source())
            if label.startswith("money:")]
    assert hits, "分母为 0"
    bad = [(label, seg) for label, seg in hits
           if not any(tok in seg for tok in _PAYER_TOKENS)]
    assert not bad, (
        "动钱原语的 user_id 不是从 payer 谓词来的:\n"
        + "\n".join("  · %s\n%s" % (label, seg) for label, seg in bad))


# ═══════════════════════════════════════════════════════════════════════════
# 配对的必须不命中:同一个 census 喂人造违规源码,必须判红
# ═══════════════════════════════════════════════════════════════════════════
_VIOLATING_SOURCE = textwrap.dedent(
    '''
    async def bad_settlement(run):
        r = await commit_freeze(
            freeze_id=run["freeze_id"], task_ref=run["freeze_task_ref"],
            user_id=run["owner_user_id"], freeze_table=run["freeze_backend"],
            reason="x")
        cur.execute("SELECT amount_total FROM point_freezes WHERE id=%s AND user_id=%s",
                    (run["freeze_id"], run["owner_user_id"]))
        return r
    ''')

_CLEAN_SOURCE = textwrap.dedent(
    '''
    async def good_settlement(run):
        r = await commit_freeze(
            freeze_id=run["freeze_id"], task_ref=run["freeze_task_ref"],
            user_id=settlement_payer_user_id(run), freeze_table=run["freeze_backend"],
            reason="x")
        cur.execute("SELECT amount_total FROM point_freezes WHERE id=%s AND user_id=%s",
                    (run["freeze_id"], settlement_payer_user_id(run)))
        return r
    ''')


@pytest.mark.parametrize("source,expect_offenders", [
    (_VIOLATING_SOURCE, True),
    (_CLEAN_SOURCE, False),
])
def test_the_census_itself_has_discriminating_power(source, expect_offenders):
    """尺子是活的吗:违规源码必须被抓出来,合规源码必须一条都不报。

    只验前者会漏掉"恒报红"的坏尺子,只验后者会漏掉"恒报绿"的坏尺子。
    """
    hits = census(source)
    assert hits, "census 连人造样本都枚举不到 —— 识别面坏了"
    offenders = [h for h in hits if "owner_user_id" in h[1]]
    assert bool(offenders) is expect_offenders, (hits, offenders)
