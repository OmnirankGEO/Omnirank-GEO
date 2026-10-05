# -*- coding: utf-8 -*-
"""WO_241 丙(③)· 退费必须退**自己那一笔**,不是"这个功能最近那一笔"。

事实:`server.py:api_generate_titles` 里五处 `refund_points` 都不传 `charge_tx_id`
⇒ 走 `middleware/billing.py:1084` 自己警告的 **newest-by-feature**。
同一用户两批 `topic_gen` 并发时,**一批失败会退到另一批那笔上**
(该 docstring 把它记作 R2-CAN-039)。

不改金额、不改口径 —— 但它退的是**别人那一笔的钱**。

🔴 观测点:退费行写进 `point_transactions`,`type='refund'` 且
   **`order_id = 被退那笔 consume 的 id(文本)`**(见 billing.py 的
   `_legacy_consume_columns`)。所以"退了哪一笔"是可以逐行读出来的,
   不用靠余额差额去推。

🔴 本包必须跑**真 PostgreSQL**:newest-by-feature 是一条 SQL 的行为,
   假对象复现不出来 —— 那样判据证明的是"我的假对象按我想的那样工作"。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
USER_ID = 990419          # 本包专用,避开真实 id
FEATURE = "topic_gen"


def _conn():
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(scope="module", autouse=True)
def _schema():
    # 🔴 **不许 try/except: pass**。第一版我把建表包在 `except: pass` 里,
    #    建表真的失败了(函数名没查、凭记忆写的),而读数是"relation does not exist"
    #    出现在**四条判据**上,像是判据坏了 —— 正是本轮一直在修的那个形状:
    #    **吞掉异常让"没跑成"伪装成"跑了但结果不对"**。建不起来就当场炸。
    # 依赖顺序:`user_wallets` 有指向 `users` 的外键 ⇒ 先建 auth 侧。
    # (这一条是上一版报 `relation "users" does not exist` 才看见的 ——
    #  而它之所以看得见,正是因为把 `except: pass` 去掉了。)
    # 🔴 **不一张一张追表**,也不靠各模块的 init 拼。
    #    我前三版是「报哪张缺就补哪张」(users → brands → billing_debt_offset_outbox → …)
    #    —— 那是在手搓一套 schema,而手搓的那套迟早与生产不一样
    #    (WO_242 里已经栽过一次:自建 quotes 和 init_db 的打架)。
    #
    #    本库由**生产 schema 快照**灌成(仓内既有:
    #    `tests/inventory_distribution_chain_2026_08_12/prod_schema_2026-08-12.sql`),
    #    一次到位且形状就是生产的形状。本 fixture 只**断言它在**,不自己建 ——
    #    建库那一步在包外(见交付单),这里若发现缺表就**当场炸**,
    #    绝不 `except: pass`(那会让"没准备好"伪装成"判据红了")。
    c = _conn()
    cur = c.cursor()
    need = ("users", "user_wallets", "point_transactions", "feature_pricing",
            "billing_debt_offset_outbox")
    cur.execute("SELECT " + ", ".join("to_regclass('%s') AS %s" % (t, t) for t in need))
    row = cur.fetchone()
    missing = [t for t in need if row[t] is None]
    assert not missing, (
        "测试库缺表 %s —— 请先灌生产 schema 快照(见交付单),"
        "本包不手搓 schema" % (missing,))
    # 价目表是数据不是结构,快照里没有 ⇒ 按生产同一条路径 seed。
    import db.wallet_db as wdb
    wdb.seed_feature_pricing()
    cur.execute("SELECT cost_points FROM feature_pricing WHERE feature_code = %s",
                (FEATURE,))
    pricing = cur.fetchone()
    assert pricing, "%s 没有价目行 —— 扣费跑不起来,判据失去前提" % FEATURE
    c.close()
    yield


@pytest.fixture()
def wallet():
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置 —— 本包必须连真库")
    c = _conn()
    cur = c.cursor()
    cur.execute("DELETE FROM point_transactions WHERE user_id = %s", (USER_ID,))
    cur.execute("DELETE FROM user_wallets WHERE user_id = %s", (USER_ID,))
    cur.execute("DELETE FROM users WHERE id = %s", (USER_ID,))
    # 🔴 列名/非空约束是**查出来的**,不是一列一列猜的:
    #    `information_schema.columns WHERE is_nullable='NO' AND column_default IS NULL`
    #    ⇒ users 必填 = username / password_hash / display_name(其余有默认值)。
    #    我前一版凭记忆只写了三列里的两列,被 NotNullViolation 逐列教了一次 ——
    #    本仓「取数前必看键名,不许凭记忆写字段」。
    cur.execute(
        "INSERT INTO users (id, username, password_hash, display_name)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (USER_ID, "rv_refund_%d" % USER_ID, "x", "RV 退费测试"),
    )
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s, %s, %s)",
        (USER_ID, 100000, 0),
    )
    c.commit()
    yield c
    try:
        c.close()
    except Exception:
        pass


def _charge():
    from middleware.billing import deduct_points
    res = asyncio.run(deduct_points(USER_ID, FEATURE))
    assert isinstance(res, dict) and res.get("charge_tx_id"), res
    return int(res["charge_tx_id"])


def _refund(*, charge_tx_id):
    from middleware.billing import refund_points
    return asyncio.run(
        refund_points(USER_ID, FEATURE, reason="测试", charge_tx_id=charge_tx_id))


def _refund_targets(conn):
    """已发生的退费各自指向哪一笔 consume(`order_id` = 那笔的 id 文本)。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id FROM point_transactions "
        "WHERE user_id = %s AND type = 'refund' ORDER BY id",
        (USER_ID,),
    )
    return [r["order_id"] for r in cur.fetchall()]


def test_the_fixture_really_makes_two_distinct_charges(wallet):
    """🔴 前提自检:两笔扣费的 id 必须不同,否则下面"退对了没"无从谈起。"""
    a = _charge()
    b = _charge()
    assert a != b, (a, b)


def test_refund_with_charge_tx_id_hits_its_own_charge(wallet):
    """🔴 带 `charge_tx_id` ⇒ 退的是**自己那笔**(即使它不是最近一笔)。"""
    first = _charge()
    second = _charge()                      # 更近的一笔,属于"另一批"
    _refund(charge_tx_id=first)
    targets = _refund_targets(wallet)
    assert targets, "没有产生任何退费行 —— 判据失去观测点"
    assert all(t == str(first) for t in targets), (
        "退费指向 %s,而本批那笔是 %s(更近的一笔是 %s)" % (targets, first, second))


def test_without_the_id_it_refunds_the_wrong_charge(wallet):
    """🔴 反向对照:**不传** id 时必须退到"最近那一笔"——也就是**别的批次**。

    没有这一条,上面那条可能只是"这个库里怎么退都对"。
    它同时也是这条缺陷真实存在的实证:R2-CAN-039 不是理论风险。
    """
    first = _charge()
    second = _charge()
    _refund(charge_tx_id=None)              # 旧行为:newest-by-feature
    targets = _refund_targets(wallet)
    assert targets, "没有产生任何退费行 —— 反向对照失去观测点"
    assert all(t == str(second) for t in targets), (
        "不传 id 却没有退到最近那笔:targets=%s first=%s second=%s "
        "—— 那么本包证明不了『带 id 才退对』" % (targets, first, second))
    assert not any(t == str(first) for t in targets)


def test_the_handler_now_passes_the_id_at_every_refund_site(wallet):
    """🔴 五处退费**一处都不能漏** —— 漏一处就是漏一条会退错钱的路径。"""
    import ast
    import io
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read(), "server.py")
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "api_generate_titles"), None)
    assert fn is not None, "找不到 api_generate_titles"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
             == "refund_points"]
    assert len(calls) == 5, "退费点 %d 处(应 5 处)" % len(calls)
    missing = [n.lineno for n in calls
               if "charge_tx_id" not in {k.arg for k in n.keywords}]
    assert not missing, "这些退费点没传 charge_tx_id,行 %s" % (missing,)

    # 🔴 **钉了「传了这个参数」还不够,要钉「它拿得到真值」**。
    #    注毒把扣费结果那两行删掉之后,`_topic_charge_tx_id` 恒为 None,
    #    五处照样"传了 charge_tx_id=None" ⇒ 行为退回 newest-by-feature,
    #    而上面那条**全绿**。本仓:钉了「调用了谁」≠ 钉了「拿什么调的」。
    fnsrc = ast.unparse(fn)
    assert "charge_tx_id" in fnsrc and ".get('charge_tx_id')" in fnsrc.replace('"', "'"), (
        "没有从扣费结果里取出 charge_tx_id —— 那五处传的永远是 None")
    passed_names = {ast.unparse(k.value) for n in calls for k in n.keywords
                    if k.arg == "charge_tx_id"}
    assert len(passed_names) == 1, "五处传的不是同一个变量:%s" % (passed_names,)
    var = passed_names.pop()
    assigned_from_charge = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == var for t in n.targets)
        and "charge_tx_id" in ast.unparse(n.value)
    ]
    assert assigned_from_charge, (
        "%s 从来没有从扣费结果里被赋过值 —— 它恒为 None,等于没传" % var)
