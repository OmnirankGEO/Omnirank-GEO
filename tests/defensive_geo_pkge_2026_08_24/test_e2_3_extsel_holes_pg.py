"""【外选 E2 · 判据洞补齐】MUT-EXTE2-09:重放核一致性的**比对分母**只被打过 1/3。

外选草单原文
------------
    删掉 ``("funding_policy", draft.funding_policy),`` 这一行 ⇒ 已存预算行的
    ``funding_policy`` 被库侧改动(hash 列未同步动)时,重放静默复用。
    表面理由极像"合法瘦身":policy 变必带动 digest 变(policy 在 sha256 分母里),
    "hash 已覆盖 policy" —— 但那只覆盖**推导轴**,盖不住**行篡改轴**。

判据洞
------
``test_e2_3_replay_against_a_drifted_budget_goes_to_human_not_silent_reuse``
只 UPDATE ``budget_hash`` 一列造漂移;三个比对键只有一个被判据打过。
另外两个(``execution_budget_snapshot_id`` / ``funding_policy``)从上线起没人验。

补法(两层,缺一层就还有缝)
--------------------------
① **逐键行为判据**:每一个比对键**单独**漂移(别的键一个字不动)都必须转人工。
   分母 = 全部三个键,不是"我记得的那个"。
② **机械枚举锁**:代码里真正比的那几列,必须与①覆盖的那几列**逐字相等**。
   少一列 → ①里对应那条红;多一列(有人加了新键却没写判据)→ ②红。
   两条一起把「谁在守这根轴」这件事变成可机械对账的。
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from tests.defensive_geo_pkge_2026_08_24 import _seed

pytestmark = pytest.mark.integration

#: 🔴 重放核一致性的**全部**比对键。这个元组同时是①的分母和②的期望集 ——
#:    写两份必有一份漏改(同一谓词写两处必有一处没人验)。
DRIFT_KEYS = ("execution_budget_snapshot_id", "budget_hash", "funding_policy")


def _one_pending_row(cur):
    from services.defensive_geo import activation_materializer as _mat
    rows = _mat._claim_pending(cur, limit=1)
    assert rows, "没有待物化的 outbox 行 —— 夹具没造起来,判据会因为错误的原因绿"
    return rows[0]


def _poison_for(column, current):
    """给一列造一个**确定不同**的值,长度落在该列的上限内。

    值从**当前值**派生,不是我猜一个字面量:猜的那个万一恰好等于当前值,
    "漂移"就没造出来,判据会因为世界没造对而绿(本仓 08-26 刚栽过一次:
    样本恰好让新旧两式相等,零判别力)。
    """
    if column == "budget_hash":                     # CHARACTER(64)
        return ("e2drift" + "0" * 57)[:64]
    return (str(current)[:18] + "_e2drift")[:32]    # VARCHAR(32) 是最窄的那个


@pytest.mark.parametrize("column", DRIFT_KEYS)
def test_e2_3_each_comparison_key_drifting_alone_goes_to_human(db, column):
    """🔴 三个比对键,**逐个单独**漂移都必须转人工。

    造法:先真物化一次(签出预算),再只把库里那一列改掉(别的列一个字不动),
    然后拿同一条 outbox 行再物化一次。

    为什么"hash 已经覆盖了 policy"这个理由不成立
    --------------------------------------------
    ``budget_hash`` 的分母里确实有 ``fundingPolicy``,所以**推导侧**改 policy
    必然换 digest。但这条判据造的是**行被改**:库里的 policy 列被动了、hash 列没动。
    这不是假想 —— 人工修数据、半截回滚、另一条代码路径 UPDATE 都长这样。
    覆盖推导轴 ≠ 覆盖行篡改轴,而重放核一致性守的正是后者。
    """
    from services.defensive_geo import activation_materializer as _mat

    _seed.install_base_rows()
    _seed.seed_accepted_snapshot(publications=2, activate=True)

    # 🔴 seed 在**它自己的连接**上提交;db 这条连接可能早已开着事务,
    #    那个快照里没有刚落的 outbox 行。先 rollback 换一个新快照再 claim。
    db.rollback()
    cur = db.cursor()
    row = _one_pending_row(cur)
    first = _mat.materialize_one(cur, row)
    snap_id = first["executionBudgetSnapshotId"]
    assert snap_id, first

    cur.execute("SELECT %s AS v FROM defgeo_provider_execution_budgets "
                "WHERE execution_budget_snapshot_id = %%s" % column, (snap_id,))
    current = cur.fetchone()["v"]
    poison = _poison_for(column, current)
    assert str(poison).strip() != str(current).strip(), (
        "毒值与原值相等(%r)—— 漂移根本没造出来,这条判据会因为世界没造对而绿"
        % (current,))

    cur.execute("UPDATE defgeo_provider_execution_budgets SET %s = %%s "
                "WHERE execution_budget_snapshot_id = %%s" % column, (poison, snap_id))
    assert cur.rowcount == 1, "没改到那一行 —— 这条判据没造出漂移态"

    with pytest.raises(_mat._NeedsHuman) as err:
        _mat.materialize_one(cur, row)
    assert column in str(err.value), (
        "转人工了,但理由里没点名是 %s 漂了:%s —— "
        "不点名的转人工会让运维查错方向" % (column, err.value))


def test_e2_3_the_replay_comparison_keys_are_exactly_the_ones_under_criteria():
    """机械枚举锁:代码里真正比的那几列,必须与 ``DRIFT_KEYS`` **逐字相等**。

    两个方向都要红:
      · 少一列(外选 MUT-EXTE2-09 那一手:把 ``funding_policy`` 从比对面摘掉)——
        看起来像"合法瘦身",而摘掉的那一列从此没人守;
      · 多一列(有人加了新的比对键却没给它写判据)—— 新键上线即无判据,
        而漏掉的那一项不会让任何判据变红。

    分母从 AST 机械取,不手抄:手抄的清单漏掉的那一项同样不会让任何判据变红。
    """
    from services.defensive_geo import activation_materializer as _mat

    tree = ast.parse(textwrap.dedent(inspect.getsource(_mat)))
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_drift" for t in node.targets):
            continue
        comp = node.value
        assert isinstance(comp, ast.ListComp), (
            "`_drift` 不再是一个列表推导,这条锁的取法失效了:%s"
            % ast.dump(comp)[:200])
        it = comp.generators[0].iter
        assert isinstance(it, ast.Tuple), (
            "`_drift` 遍历的不是字面量元组,取不到比对键:%s" % ast.dump(it)[:200])
        found = tuple(
            pair.elts[0].value for pair in it.elts
            if isinstance(pair, ast.Tuple) and isinstance(pair.elts[0], ast.Constant))
        break

    assert found is not None, (
        "在 activation_materializer 里找不到 `_drift` 的赋值 —— "
        "重放核一致性那一段被挪走或改名了,这条锁在守空气")
    assert set(found) == set(DRIFT_KEYS), (
        "代码比的是 %r,判据覆盖的是 %r —— 两边必须逐字相等:"
        "代码少一列 = 那一列从此没人守;代码多一列 = 新键上线即无判据"
        % (list(found), list(DRIFT_KEYS)))
