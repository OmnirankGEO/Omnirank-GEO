"""【A-3 = Codex fix-of-fix2 P1-5】``CustomerLinkNotExtendable`` 逃逸 —— 异常全集 census。

Codex 原文
----------
    ``customer_links_reissue`` 只捕获 ``CustomerLinkObjectDrifted``。当面板读取后
    session 被并发推进到 confirmed,``CustomerLinkNotExtendable`` 会直接穿出为通用 500。

我的证伪结果:坐实,但**形状要说准**
------------------------------------
底 ``5f5884893`` 上它确实没人接。不过它穿出去之后并不是**裸** 500 文本 ——
本包挂了 ``TypedErrorRoute``,兜底那一支会把它翻成 typed ``INTERNAL_ERROR``(500)。
所以准确的说法是:**码错了、出口错了**,不是"信封形状坏了"。

为什么码错就够严重:``INTERNAL_ERROR`` 对用户的意思是「我们这边坏了,稍后再试」。
可这件事再试一万次也不会好 —— 会话不会自己退回选词态。她拿到的是一句
**确定性错误的建议**,这比一个难看的 500 更糟。

分母
----
不是"把这一个异常接住"就完了。``customer_links`` 模块定义的**每一个**自定义异常,
在端点层都必须有映射,或者落进显式豁免集。漏掉的那一个不会让任何判据变红。
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

LINKS = "services/defensive_geo/customer_links.py"
ASSIST = "api/defensive_geo_assist_api.py"

#: 模块定义的自定义异常 → 哪个端点函数接住它。
#: 集合相等,不是包含 —— 新加一个异常类必须同时决定"谁接它"。
FROZEN_EXCEPTION_MAPPING = {
    "CustomerLinkNotExtendable": "customer_links_reissue",
    "CustomerLinkObjectDrifted": "customer_links_reissue",
    "CustomerLinksUnavailable": "customer_links",     # GET 面板那条
}

#: 显式豁免(有意不在端点层接的)。现役为空 —— 空集也要写出来并断言它是空,
#: 否则"没有豁免"和"忘了写豁免集"长得一模一样。
FROZEN_EXEMPT: set[str] = set()


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


def test_00_exception_class_denominator_is_alive() -> None:
    classes = C.customer_link_exception_classes(LINKS)
    assert len(classes) >= 3, "只扫到 %d 个自定义异常类 —— 分母塌了:%s" % (
        len(classes), classes)


def test_01_class_scanner_has_discriminating_power(tmp_path) -> None:
    """扫描器自证:新加一个异常类必须被扫出来,普通类不能被误算进来。"""
    import pathlib

    src = "\n".join([
        "class SomethingBoom(RuntimeError): ...",
        "class AlsoBoom(Exception): ...",
        "class NotAnError:  ...",
        "class Helper(object): ...",
    ])
    p = tmp_path / "fake_links.py"
    p.write_text(src, encoding="utf-8")
    old = C.REPO
    try:
        C.REPO = pathlib.Path(tmp_path)
        got = C.customer_link_exception_classes("fake_links.py")
    finally:
        C.REPO = old
    assert got == ["AlsoBoom", "SomethingBoom"], \
        "异常类扫描器区分不了异常类与普通类:%s" % got


def test_02_every_custom_exception_is_mapped_or_exempt() -> None:
    """全集分区:每个异常要么被某个端点接住,要么显式豁免。不留第三类。"""
    classes = set(C.customer_link_exception_classes(LINKS))
    mapped = set(FROZEN_EXCEPTION_MAPPING)
    assert classes == mapped | FROZEN_EXEMPT, (
        "customer_links 的异常类集合变了。\n  新增: %s\n  消失: %s\n"
        "新增的每一个都必须决定端点层怎么翻;不决定的话它会走 route class 的兜底,"
        "变成一句「我们这边坏了,稍后再试」——而那句话对业务态异常永远是错的。"
        % (sorted(classes - (mapped | FROZEN_EXEMPT)),
           sorted((mapped | FROZEN_EXEMPT) - classes)))
    assert FROZEN_EXEMPT == set(), \
        "出现了豁免项 %s —— 豁免必须写清理由,不能默默增长" % sorted(FROZEN_EXEMPT)


@pytest.mark.parametrize("exc,handler", sorted(FROZEN_EXCEPTION_MAPPING.items()))
def test_03_handler_really_catches_it(exc, handler) -> None:
    """逐档参数化:那个 handler 的 ``except`` 子句里**真的**写着这个名字。

    用 AST 读 ``except`` 子句,不是 ``exc in src``:
    我在注释里写了 ``CustomerLinkNotExtendable`` 这个词好几次,
    裸串锁会被自己的注释喂绿。
    """
    caught = C.names_caught_by(ASSIST, handler)
    assert exc in caught, (
        "%s::%s 没有接住 %s(它接的是 %s)—— 这个异常会走 route class 兜底,"
        "对用户变成 500「稍后再试」。" % (ASSIST, handler, exc, sorted(caught) or "无"))


def test_04_reissue_returns_conflict_not_internal_error() -> None:
    """那一支翻出来的必须是 ``SNAPSHOT_CHANGED``(409),不是新造码、不是 500。

    工单允许「复用 SNAPSHOT_CHANGED + new_preview/refresh」或者走
    copy_registry 全套注册新码。这里选了复用 —— 两个异常说的是同一句话
    (你看到的那一份已经不是现在这一份了),新造码要新文案、新注册、
    再来一轮闭集核对,而语义完全同义。
    """
    import ast

    src = (C.REPO / ASSIST).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "customer_links_reissue":
            continue
        for h in [x for x in ast.walk(n) if isinstance(x, ast.ExceptHandler)]:
            names = set()
            if h.type is not None:
                targets = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
                names = {getattr(t, "id", getattr(t, "attr", "")) for t in targets}
            if "CustomerLinkNotExtendable" not in names:
                continue
            codes, kinds = set(), set()
            for sub in ast.walk(h):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    fname = getattr(f, "id", getattr(f, "attr", None))
                    if fname == "_safe_error" and sub.args:
                        a0 = sub.args[0]
                        if isinstance(a0, ast.Constant):
                            codes.add(a0.value)
                    if fname == "_action" and sub.args:
                        a0 = sub.args[0]
                        if isinstance(a0, ast.Constant):
                            kinds.add(a0.value)
            assert codes == {"SNAPSHOT_CHANGED"}, \
                "NotExtendable 这一支翻出来的错误码是 %s(期望 SNAPSHOT_CHANGED)" % (codes or "无")
            assert kinds & {"new_preview", "refresh"}, \
                "这一支没有给可执行的下一步(nextAction=%s)—— 拒绝必须自带出口" % (kinds or "无")
            return
        raise AssertionError("customer_links_reissue 里没有一支 except 接 CustomerLinkNotExtendable")
    raise AssertionError("找不到 customer_links_reissue —— 锚点过期")


def test_05_not_extendable_is_raised_by_the_domain_layer() -> None:
    """配对的必须命中:域层**真的**会抛它。

    如果 ``reissue_link`` 哪天不再抛这个异常了,上面那几条"接住了"的断言
    会变成**零分母恒绿** —— 接住一个永远不会来的东西。
    """
    import ast

    src = (C.REPO / LINKS).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    raised = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            f = n.exc.func
            raised.add(getattr(f, "id", getattr(f, "attr", "")))
    assert "CustomerLinkNotExtendable" in raised, \
        "域层不再抛 CustomerLinkNotExtendable —— 端点那几条判据就成了零分母"
    assert "CustomerLinkObjectDrifted" in raised, "域层不再抛 CustomerLinkObjectDrifted"
