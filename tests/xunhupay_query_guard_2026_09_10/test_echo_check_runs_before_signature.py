"""#170 · 虎皮椒主动查询:回显先于验签,且验签是**条件**的不是**取消**的。

## 事实基础(Deploy 只读取证,2026-09-10)

厂商文档 https://www.xunhupay.com/doc/api/search.html 写着查询响应带顶层 `hash`
(成功与失败示例都带)。**实测不带**:同一 host、`XUNHUPAY_QUERY_URL` 未设、
顶层键原样 `['data','errcode','errmsg']`、嵌套 data 10 键、
五种签名候选键(hash/sign/signature/sig/checksum)全无
⇒ 排除"打错网关"与"探针漏看",是**网关实际行为与文档不符**。

## 本包钉三件

1. **订单号回显校验先于验签** —— 改前它排在恒 False 的验签之后,**从未执行过**;
2. **签了就必须验**:响应带 `hash` 时,签名不对必须拒(不是"反正实测没有就不管");
3. **回调路径逐字不动** —— 回调是钱到账的凭据,查询只是我们主动问一句,风险不对等。

🔴 第 2 条是这次改动最容易被做丢的:裁定说"条件验签",很容易实现成
   "反正没有签名,那就都放行"。那样厂商哪天开始签名、或中间人剥掉签名字段,
   我们都不会知道 —— **降级必须由对方的响应形状触发,不能由我们的假设触发**。
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import io
import pathlib
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MOD = REPO / "services" / "xunhupay.py"

_SECRET = "test-appsecret-#170"


def _fn(name: str) -> ast.AST:
    tree = ast.parse(io.open(MOD, encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name} —— 判据失去分母")


# ══════════════════════════════════ 结构臂

def test_the_echo_check_is_positioned_before_any_signature_decision():
    """订单号回显校验必须排在任何签名判断之前。

    改前顺序是"先验签、再校验订单号",而查询响应取不到 hash ⇒ 验签恒 False
    ⇒ 回显校验**从来没执行过**。一个从不运行的守卫和不存在没有区别。

    ⚠️ 只看行号不够(#169 实测被嵌套 def 骗过一次),真正的证明是下面的行为臂。
    """
    fn = _fn("query_xunhupay_order")
    echo = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Compare) and "response_order_id" in ast.unparse(n)]
    sig = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Call)
           and getattr(n.func, "id", "") in ("_find_query_signature",
                                             "_query_signature_matches")]
    assert echo, "找不到订单号回显校验 —— 判据失去分母"
    assert sig, "找不到签名判断 —— 条件验签被整个拿掉了?"
    assert min(echo) < min(sig), "回显校验仍排在签名判断之后 —— 它会被恒假的闸挡住"


def test_the_callback_path_is_untouched_and_still_verifies_unconditionally():
    """🔴 回调路径**逐字不动**:仍然无条件验签,没有任何"条件"分流。

    回调是钱到账的凭据。查询只是我们主动去问一句 —— 两者风险不对等,
    绝不许因为查询这条路降级了,就顺手把回调也放宽。
    """
    fn = _fn("verify_xunhupay_callback")
    src = ast.unparse(fn)
    assert "_generate_hash" in src, "回调验签不再算签名 —— 这是钱的凭据,不许放行"
    for leak in ("_find_query_signature", "UNSIGNED", "unsigned"):
        assert leak not in src, f"回调验签里出现了查询路径的降级逻辑:{leak}"

    # 🔴 不锁 `"return False" in src`:注毒实测**没牙** —— 把早退的
    #    `return False` 改成 `return True`,函数里还有另一处
    #    (APPSECRET 未配置那条)让字符串照样命中。**同名文本无裁定权**。
    #    直接调它:没有 hash 的回调必须被拒。
    xp = importlib.import_module("services.xunhupay")
    old_secret = xp.XUNHUPAY_APPSECRET
    xp.XUNHUPAY_APPSECRET = _SECRET
    try:
        assert xp.verify_xunhupay_callback({"errcode": 0}) is False, (
            "无 hash 的回调被放行了 —— 回调是钱到账的凭据,不许无条件通过"
        )
        good = {"errcode": 0, "errmsg": "ok"}
        good["hash"] = xp._generate_hash(good, _SECRET)
        assert xp.verify_xunhupay_callback(good) is True, (
            "正确签名的回调被拒 —— 上一条会变成恒真,证明不了任何事"
        )
        bad = dict(good, hash="0" * 32)
        assert xp.verify_xunhupay_callback(bad) is False, "错误签名的回调被放行"
    finally:
        xp.XUNHUPAY_APPSECRET = old_secret


# ══════════════════════════════════ 行为臂

def _run(payload: dict, order_id: str, *, strict: bool, url="https://api.xunhupay.com/x"):
    """真跑 `query_xunhupay_order`,只桩**边界**(HTTP 会话 + 环境),不桩被测逻辑。

    🔴 特别不桩 `verify_xunhupay_callback` / `_query_signature_matches` ——
       它们正是本包要验的东西,桩掉就等于什么都没验(#150 栽过)。
    """
    xp = importlib.import_module("services.xunhupay")

    class _Resp:
        async def text(self):
            import json
            return json.dumps(payload)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Sess:
        def post(self, *a, **kw): return _Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    import aiohttp as _real
    old = (xp.aiohttp, xp.XUNHUPAY_APPID, xp.XUNHUPAY_APPSECRET,
           __import__("os").environ.get("XUNHUPAY_QUERY_URL"))
    xp.aiohttp = types.SimpleNamespace(
        ClientSession=lambda *a, **kw: _Sess(), ClientTimeout=lambda **kw: None)
    xp.XUNHUPAY_APPID, xp.XUNHUPAY_APPSECRET = "APPID-170", _SECRET
    __import__("os").environ["XUNHUPAY_QUERY_URL"] = url
    try:
        return asyncio.run(xp.query_xunhupay_order(order_id, strict=strict))
    finally:
        xp.aiohttp, xp.XUNHUPAY_APPID, xp.XUNHUPAY_APPSECRET = old[0], old[1], old[2]
        if old[3] is None:
            __import__("os").environ.pop("XUNHUPAY_QUERY_URL", None)
        else:
            __import__("os").environ["XUNHUPAY_QUERY_URL"] = old[3]


def _unsigned(order="MY_ORDER"):
    """实测形状:顶层 3 键,嵌套 data,**无任何签名键**。"""
    return {"errcode": 0, "errmsg": "success!",
            "data": {"status": "OD", "out_trade_order": order,
                     "transaction_id": "T1", "total_amount": "100.00"}}


def _signed(order="MY_ORDER", *, good=True):
    """文档形状:顶层带 `hash`。"""
    xp = importlib.import_module("services.xunhupay")
    body = {"errcode": 0, "errmsg": "success!",
            "data": {"status": "OD", "out_trade_order": order}}
    top = {k: v for k, v in body.items() if k != "data"}
    body["hash"] = xp._generate_hash(top, _SECRET) if good else "0" * 32
    return body


def test_an_unsigned_response_is_accepted_only_via_echo_and_https():
    """实测形状(无签名):订单号回显对得上 ⇒ 放行。"""
    out = _run(_unsigned(), "MY_ORDER", strict=True)
    assert out and out.get("status") == "OD"


def test_an_unsigned_response_with_a_wrong_order_is_rejected():
    """无签名 + 回显不一致 ⇒ 拒。这是这条路上唯一还在挡"网关回了别人那张单"的守卫。"""
    with pytest.raises(RuntimeError, match="订单号不一致"):
        _run(_unsigned("SOMEONE_ELSE"), "MY_ORDER", strict=True)


def test_an_unsigned_response_over_plain_http_is_rejected():
    """无签名时,传输层是仅剩的真凭据 ⇒ 非 HTTPS 必须拒。"""
    with pytest.raises(RuntimeError, match="非 HTTPS"):
        _run(_unsigned(), "MY_ORDER", strict=True, url="http://api.xunhupay.com/x")


def test_an_unsigned_response_raises_a_warning_and_counts():
    """降级要**可观测**:厂商到底签没签,运维要能看见。"""
    xp = importlib.import_module("services.xunhupay")
    before = xp.UNSIGNED_QUERY_RESPONSES
    _run(_unsigned(), "MY_ORDER", strict=True)
    assert xp.UNSIGNED_QUERY_RESPONSES == before + 1, (
        "未签名响应没有被计数 —— 降级变成了静默的既成事实"
    )


def test_a_signed_response_with_a_bad_signature_is_still_rejected():
    """🔴 承重条:**签了就必须验**。

    这条防的是把"条件验签"做成"反正实测没有就都放行" ——
    那样厂商开始签名、或中间人剥掉签名字段,我们都不会知道。
    降级必须由**对方的响应形状**触发,不能由我们的假设触发。
    """
    with pytest.raises(RuntimeError, match="签名无效"):
        _run(_signed(good=False), "MY_ORDER", strict=True)


def test_a_signed_response_with_a_good_signature_passes():
    """正样本:签名对得上 ⇒ 放行(证明上一条不是恒红)。"""
    out = _run(_signed(good=True), "MY_ORDER", strict=True)
    assert out and out.get("status") == "OD"


def test_a_signed_response_does_not_take_the_unsigned_downgrade_path():
    """签名响应不许走降级计数 —— 否则"条件"是假的。"""
    xp = importlib.import_module("services.xunhupay")
    before = xp.UNSIGNED_QUERY_RESPONSES
    _run(_signed(good=True), "MY_ORDER", strict=True)
    assert xp.UNSIGNED_QUERY_RESPONSES == before, (
        "带签名的响应也被记成未签名 —— 条件分流没生效"
    )


def test_non_strict_returns_none_instead_of_raising():
    """非 strict 语义不变:丢弃,不抛。"""
    assert _run(_unsigned("SOMEONE_ELSE"), "MY_ORDER", strict=False) is None
    assert _run(_signed(good=False), "MY_ORDER", strict=False) is None
