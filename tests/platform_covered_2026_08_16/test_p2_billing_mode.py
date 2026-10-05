# -*- coding: utf-8 -*-
"""§2 付款方可选 —— 🔴 资金类判据必须**双向**。

只证明「平台承担扣 136」= 没证明没砸坏「普通订阅仍扣 brand owner」。
两个方向各自拆锁都要能转红。
"""
from __future__ import annotations

import importlib
import os

import pytest


def _resolver():
    """取 scheduler 里那个闭包函数本体(它定义在 run_daily_monitoring 内部)。

    🔴 不复制一份实现来测 —— 「测量仪器必须与被测实现同口径」是本仓栽过的坑。
    这里用 AST 把闭包源码抠出来在同名环境里 exec,拿到的是**同一段代码**。
    """
    import ast
    import textwrap
    import logging
    src = open("scheduler.py", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_billing_uid_for_sub":
            seg = textwrap.dedent(ast.get_source_segment(src, node))
            ns = {"logger": logging.getLogger("test")}
            exec(compile(seg, "scheduler.py::_billing_uid_for_sub", "exec"), ns)
            return ns["_billing_uid_for_sub"]
    raise AssertionError("找不到 _billing_uid_for_sub —— 判据锚点失效")


BRAND, OWNER, OPERATOR, PLATFORM = 592, 28, 999, 136


def test_platform_mode_charges_platform_account(monkeypatch):
    """正向:billing_mode='platform' → 落 PLATFORM_DIRECT_SERVICE_USER_ID。"""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(PLATFORM))
    fn = _resolver()
    sub = {"id": 92, "user_id": OPERATOR, "brand_id": BRAND, "billing_mode": "platform"}
    assert fn(sub, {BRAND: OWNER}) == PLATFORM, "platform 订阅没扣平台账"


def test_brand_owner_mode_still_charges_brand_owner(monkeypatch):
    """🔴 反向(最重要):sub.user_id ≠ brand owner 的 brand_owner 订阅
    **必须仍然扣 brand owner** —— 证明 2026-06-10 audit P0-4 那条修复没被本单削弱。"""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(PLATFORM))
    fn = _resolver()
    sub = {"id": 1, "user_id": OPERATOR, "brand_id": BRAND, "billing_mode": "brand_owner"}
    got = fn(sub, {BRAND: OWNER})
    assert got == OWNER, f"P0-4 的 brand owner 覆盖被削弱了:扣到了 {got}(应 {OWNER})"
    assert got != PLATFORM, "brand_owner 订阅被错记成平台账"


def test_missing_billing_mode_behaves_exactly_like_before(monkeypatch):
    """加法自证:没有 billing_mode 这个键时,行为与改动前**逐字一致**(走 P0-4 覆盖)。"""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", str(PLATFORM))
    fn = _resolver()
    assert fn({"id": 2, "user_id": OPERATOR, "brand_id": BRAND}, {BRAND: OWNER}) == OWNER


def test_platform_fail_closed_when_unconfigured(monkeypatch):
    """🔴 fail-closed:平台账户没配 → 抛,**绝不回落到品牌归属人**。

    回落 = 「以为平台付、实际扣了服务商」换个地方重演一遍,正是本单要消灭的形态。
    """
    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)
    fn = _resolver()
    sub = {"id": 92, "user_id": OPERATOR, "brand_id": BRAND, "billing_mode": "platform"}
    with pytest.raises(Exception):
        fn(sub, {BRAND: OWNER})


def test_unconfigured_does_not_break_normal_subs(monkeypatch):
    """🔴 反向对照:平台账户没配时,**普通订阅不受影响**照常解析。

    (否则 fail-closed 就变成了"配错一个把所有人都掐掉"。)
    """
    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)
    fn = _resolver()
    assert fn({"id": 1, "user_id": OPERATOR, "brand_id": BRAND,
               "billing_mode": "brand_owner"}, {BRAND: OWNER}) == OWNER


def test_resume_precheck_and_charge_chain_read_the_same_column():
    """🔴 恢复预检与主扣费链必须看**同一个钱包**。

    只在主链认 billing_mode、恢复段不认 → 「按平台余额判恢复、按服务商余额扣钱」的振荡。
    判据打两处取数 SQL 都带上了这一列,以及两处都调同一个解析函数。
    """
    mdb = open("db/monitoring_db.py", encoding="utf-8").read()
    sched = open("scheduler.py", encoding="utf-8").read()
    def _sql_of(fn_name: str) -> str:
        """只取该函数里**真正发给 PG 的 SQL**,不含 docstring / 注释。

        🔴 第一版直接在函数体全文里找 billing_mode —— 变异 MB4 **没转红**:
           我在那个函数的 docstring 里解释了好几遍 billing_mode,
           把 SELECT 列拆掉后**说明文字仍然满足断言**。
           「判据打在讲代码的话上」今天第三次复发,所以这里只取 SQL 实参。
        """
        i = mdb.index("def " + fn_name)
        j = mdb.index(chr(10) + "def ", i + 1)
        body = mdb[i:j]
        out, pos = [], 0
        while True:
            e = body.find("cur.execute(", pos)
            if e < 0:
                break
            a = body.find('"""', e)
            if a < 0:
                break
            b = body.find('"""', a + 3)
            out.append(body[a + 3:b])
            pos = b + 3
        assert out, fn_name + " 里没抓到 SQL —— 判据锚点失效"
        return chr(10).join(out)

    resume_sql = _sql_of("list_paused_subscriptions_for_resume")
    assert "billing_mode" in resume_sql, "恢复预检的**取数 SQL** 没带 billing_mode"

    active_sql = _sql_of("list_active_subscriptions")
    assert active_sql.count("s.billing_mode") == 2, \
        f"两臂的 SQL 没有都带 billing_mode(实测 {active_sql.count('s.billing_mode')} 处)"
    # 两个调用点是同一个解析函数
    assert sched.count("_billing_uid_for_sub(sub, _paused_owner_map)") == 1, "恢复段没走统一解析"
    assert sched.count("_billing_uid_for_sub(sub, _owner_map)") == 1, "主链没走统一解析"
