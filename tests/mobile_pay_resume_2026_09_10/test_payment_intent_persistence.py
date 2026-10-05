"""#169 · 支付出口要留得住、找得回、不重复付。

事故形态:手机外部浏览器跳去虎皮椒付款,没付成回来,单停在 pending。
根因面(本包钉的那一半):**支付出口从来不落库** —— 建单时返给前端就扔了,
所以任何"第二次看这张单"(幂等重试 / order-status 恢复)都只能拿到 None。

🔴 本包不钉"用户为什么没付成"(那是 §2.1 前端与 #170 验签的事),
   只钉"回来之后还找不找得到那张单的出口"。
"""

from __future__ import annotations

import ast
import io
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_058_recharge_payment_intent_urls_2026_09_10.sql"
WALLET = REPO / "api" / "wallet_api.py"
WORKBENCH = REPO / "api" / "agent_workbench_api.py"

#: 会向外部支付提供商**下新单**的客户端。查询类(query_order)不在此列 ——
#: 它是既有的救援路径,与"恢复出口"无关,本包不拿它当违规。
_ORDER_CREATING_CLIENTS = ("create_xunhupay_order", "create_native_order")


def _src(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8").read()


def _func(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name} —— 判据失去分母")


# ══════════════════════════════════ 迁移

def test_migration_058_is_additive_and_carries_no_dml():
    """058 只加列,不搬数据。

    🔴 迁移体内禁 DML 是本仓红线:一支迁移同时改结构又改数据时,
       失败后没人说得清库停在哪一半。
    """
    assert MIGRATION.is_file(), "迁移 058 不在盘上"
    sql = _src(MIGRATION)
    body = "\n".join(
        ln for ln in sql.splitlines() if not ln.strip().startswith("--")
    )

    for col in ("payment_url_mobile", "payment_url_qrcode", "code_url"):
        assert re.search(
            rf"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+{col}\b", body, re.I
        ), f"058 没有以 additive 方式加列 {col}"

    for dml in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP TABLE"):
        assert not re.search(rf"\b{dml}\b", body, re.I), (
            f"058 迁移体里出现 {dml} —— 迁移体内禁 DML"
        )


def test_migration_058_is_registered_in_the_manifest():
    """在盘上但没登记 = 永远不会被执行(而判据会以为它生效了)。"""
    manifest = _src(REPO / "db" / "migration_manifest.py")
    assert MIGRATION.name in manifest, "058 没登记进 migration_manifest"


# ══════════════════════════════════ 写入侧

@pytest.mark.parametrize("path,func", [
    (WORKBENCH, "_start_agent_inventory_payment"),
    (WALLET, "create_recharge"),
])
def test_both_create_paths_persist_the_payment_intent(path, func):
    """两条建单路都要把拿到的出口存下来。

    ⚠️ 这里锁的是**调用发生**,不是调用正确 —— 正确性由下面的顺序判据
       与真库判据分担。单独看本条不足以证明功能可用。
    """
    # 🔴 不 skip:函数改名 = 判据失去锚点,那是**红**不是"本次未评估"。
    #    skip 在读数里与通过同色,而这里的"没找到"恰恰意味着没人在守这条路。
    node = _func(path, func)
    calls = [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_persist_payment_intent_urls"
    ]
    assert calls, f"{path.name}::{func} 没有把支付出口落库 —— 用户回来就找不到这张单"


def test_the_intent_is_persisted_after_the_provider_answers():
    """出口要在**拿到 provider 响应之后**写。

    🔴 这条与 `_persist_actual_payment_channel`(必须写在**之前**)方向相反,
       而两者都对:
         · 路由是**退款依据** —— 崩在中间也必须留痕 ⇒ 之前写;
         · URL 是 provider **给的** —— 之前根本还不存在 ⇒ 之后写。
       把这两件事写成"同一个规矩"是错的,所以本判据显式钉住顺序。
    """
    node = _func(WORKBENCH, "_start_agent_inventory_payment")
    persist_line = min(
        n.lineno for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_persist_payment_intent_urls"
    )
    provider_lines = [
        n.lineno for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) in _ORDER_CREATING_CLIENTS
    ]
    assert provider_lines, "这条路径上找不到任何下单调用 —— 判据失去分母"
    assert persist_line > max(provider_lines), (
        "支付出口写在了 provider 应答之前 —— 那时 URL 还不存在,只会存下 None"
    )


# ══════════════════════════════════ 幂等重试(#169 §5.3)

def test_the_idempotent_replay_reads_the_stored_intent_instead_of_returning_none():
    """同 key 二发要回填出口,而不是返三个 None。

    🔴 这是原缺陷本身:老代码在 `reused=True` 分支把三个字段写死成 None,
       用户重试拿到一张没有任何支付出口的单 —— 点了没反应。
    """
    # 🔴 不用正则切片再 reparse:切出来的片段自带缩进,单独 parse 必炸,
    #    而"炸了"和"没找到"在读数里同形。直接在函数 AST 里找 `if reused:`。
    node = _func(WORKBENCH, "agent_inventory_purchase")
    branch_node = next(
        (n for n in ast.walk(node)
         if isinstance(n, ast.If) and getattr(n.test, "id", "") == "reused"),
        None,
    )
    assert branch_node is not None, "找不到 reused 分支 —— 判据失去分母(分支重构后需同步)"
    branch = ast.dump(ast.Module(body=branch_node.body, type_ignores=[]))

    # 🔴 不锁"这个名字出现过":import 行留着、调用删掉,名字照样在
    #    ——注毒实测该写法**完全没牙**。改锁"响应里那三个字段到底由什么喂"。
    resp = next(
        (n for n in ast.walk(branch_node)
         if isinstance(n, ast.Call)
         and getattr(n.func, "id", "") == "AgentPurchaseCreateResponse"),
        None,
    )
    assert resp is not None, "reused 分支里找不到响应构造 —— 判据失去分母"
    fed = {k.arg: k.value for k in resp.keywords}
    for field in ("payment_url_mobile", "payment_url_qrcode", "code_url"):
        v = fed.get(field)
        assert v is not None, f"reused 分支没有传 {field}"
        assert not (isinstance(v, ast.Constant) and v.value is None), (
            f"reused 分支把 {field} 写死为 None —— 用户同 key 重试拿到一张没有出口的单"
        )
        assert isinstance(v, ast.Subscript), (
            f"{field} 不是从已存的行取出来的(期望 _urls[...] 形态,实得 {type(v).__name__})"
        )
    for client in _ORDER_CREATING_CLIENTS:
        assert client not in branch, (
            f"reused 分支调用了 {client} —— 同一张单不许产生第二个外部支付意向"
        )


# ══════════════════════════════════ 恢复端点

def test_order_status_does_not_confirm_existence_to_a_non_owner():
    """非所有者一律 404。

    403 等于回答了"这张单存在" —— 订单号可枚举时那是一条信息泄露。
    """
    node = _func(WALLET, "get_order_status")
    src = ast.get_source_segment(_src(WALLET), node) or ""
    m = re.search(r'user\["user_id"\].*?raise HTTPException\((\d+)', src, re.S)
    assert m, "找不到所有者校验 —— 任何人都能读任何订单"
    assert m.group(1) == "404", f"非所有者返回 {m.group(1)},应为 404(不泄露存在性)"


def test_a_paid_order_is_not_handed_a_way_to_pay_again():
    """已 paid 的单不带支付出口 —— 给了就是诱导重复支付。"""
    # 🔴 不锁正则:本函数上方那段既有的 wechat 救援里有**同款条件**,
    #    注毒实测它能让这条恒绿。改成:找到真正装配支付出口的那个 if,
    #    再看它的条件是不是 pending。
    node = _func(WALLET, "get_order_status")
    guard = None
    for n in ast.walk(node):
        if not isinstance(n, ast.If):
            continue
        body = ast.dump(ast.Module(body=n.body, type_ignores=[]))
        if "payment_url_mobile" in body:
            guard = n
            break
    assert guard is not None, "找不到装配支付出口的分支 —— 判据失去分母"
    cond = ast.dump(guard.test)
    assert "payment_status" in cond and "pending" in cond, (
        "支付出口没有按 pending 收口 —— 已付的单也会拿到可付款链接;实得条件:"
        + ast.unparse(guard.test)
    )


def test_order_status_creates_no_new_external_payment_intent():
    """恢复是**重取**,不是重新下单。"""
    node = _func(WALLET, "get_order_status")
    src = ast.get_source_segment(_src(WALLET), node) or ""
    for client in _ORDER_CREATING_CLIENTS:
        assert client not in src, f"order-status 调用了 {client} —— 恢复不许产生新支付意向"


# ══════════════════════════════════ ① 路由留痕(Review P5 补)

def test_the_procurement_path_persists_the_route_before_the_provider_is_called():
    """🔴 结构臂:进货路径必须在**向供应商下单之前**写 actual_payment_channel。

    Review 2026-09-10 注毒 P5 发现:把这个调用整条删掉,本包 9 条**全绿** ——
    也就是说 #169 ① 这条主交付物当时**没有任何判据在守**,
    而它正是「今天这单 channel 留 NULL」的修法本身。

    顺序是承重的:路由是**退款依据**,写在供应商之后,一旦崩在中间
    就会留下「外部已可支付、平台却不知道该按哪条通道退」的订单。
    """
    node = _func(WORKBENCH, "_start_agent_inventory_payment")
    persist_lines = [
        n.lineno for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_persist_actual_payment_channel"
    ]
    assert persist_lines, (
        "进货路径没有写 actual_payment_channel —— 退款时不知道这单走的哪条通道"
    )
    # 🔴 行号顺序 ≠ 执行顺序:`_xunhupay_fallback` 是**嵌套函数**,它的 def
    #    在上面(1337),真正执行却在下面。第一版直接比行号,被这个骗到、
    #    对着正确的代码报红。要比的是**执行点**:顶层的下单调用,
    #    加上"含下单调用的嵌套 helper 被调用的那一行"。
    inner = {
        n.name for n in ast.walk(node)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node
        and any(isinstance(c, ast.Call)
                and getattr(c.func, "id", getattr(c.func, "attr", "")) in _ORDER_CREATING_CLIENTS
                for c in ast.walk(n))
    }

    def _outer_calls(fn):
        """只走本函数体,不进嵌套 def(它们的执行点在调用处,不在定义处)。

        🔴 必须**真剪枝**:`ast.walk` 是广度遍历,在循环里 `continue`
           只是不 yield 那个节点,它的子树照样被走 —— 第一版就是这么
           把嵌套函数里的下单调用又捞了回来,判据继续对着正确代码报红。
        """
        stack = list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue                      # 整棵子树不要
            if isinstance(n, ast.Call):
                yield n
            stack.extend(ast.iter_child_nodes(n))

    provider_lines = [
        n.lineno for n in _outer_calls(node)
        if getattr(n.func, "id", getattr(n.func, "attr", "")) in (_ORDER_CREATING_CLIENTS + tuple(inner))
    ]
    assert provider_lines, "这条路径上找不到任何下单执行点 —— 判据失去分母"
    assert min(persist_lines) < min(provider_lines), (
        "路由写在了供应商下单之后 —— 崩在中间会留下"
        "「外部已可支付、平台不知按哪条通道退款」的订单"
    )


def test_the_route_is_actually_written_before_the_provider_runs():
    """🔴 行为臂:真跑一遍,记录调用顺序。

    结构臂只证明「源码里那行在上面」;它挡不住"写在上面但从未执行"
    (比如被一个恒假分支罩住)。这条真调函数,按**实际发生的顺序**判。
    """
    import asyncio
    import importlib

    wallet = importlib.import_module("api.wallet_api")
    workbench = importlib.import_module("api.agent_workbench_api")
    wechat = importlib.import_module("services.wechat_pay")

    order: list[str] = []

    def _rec_channel(order_id, channel):
        order.append(f"persist_channel:{channel}")

    def _rec_urls(order_id, **kw):
        order.append("persist_urls")

    async def _fake_native(**kw):
        order.append("provider:create_native_order")
        return {"code_url": "weixin://wxpay/fake"}

    olds = (wallet._persist_actual_payment_channel,
            wallet._persist_payment_intent_urls,
            wechat.create_native_order)
    wallet._persist_actual_payment_channel = _rec_channel
    wallet._persist_payment_intent_urls = _rec_urls
    wechat.create_native_order = _fake_native
    try:
        asyncio.run(workbench._start_agent_inventory_payment(
            agent_user_id=1,
            order_id="AIPTESTORDER0001",
            chosen={"amount_cents": 10000, "base_points": 1000,
                    "bonus_points": 0, "quote_fingerprint": "a" * 64},
            chosen_channel="wechat_native",
        ))
    finally:
        (wallet._persist_actual_payment_channel,
         wallet._persist_payment_intent_urls,
         wechat.create_native_order) = olds

    assert any(s.startswith("persist_channel:") for s in order), (
        f"整条路径跑完都没写 actual_payment_channel;实际调用序列={order}"
    )
    i_ch = next(i for i, s in enumerate(order) if s.startswith("persist_channel:"))
    i_pv = order.index("provider:create_native_order")
    assert i_ch < i_pv, f"路由写在供应商之后;实际调用序列={order}"
