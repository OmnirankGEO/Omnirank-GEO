# -*- coding: utf-8 -*-
"""WO_228-c1 · 不许在导入期**接管** `sys.stdout` / `sys.stderr` 的 buffer。

被治的缺陷:`server.py` 导入期做

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

`TextIOWrapper` **接管**底下的 buffer,包装器被丢弃/回收时连带 `close()` 它。
pytest 下那个 buffer 就是**输出捕获用的临时文件** —— 一旦被关,
此后**每条判据的 teardown** 都抛 `ValueError: I/O operation on closed file`。

实测(只撤那一处,在最小复现对上):基线 34 -> 撤掉 0 -> 还原 34。
整树规模:48 份 → **898** 条 teardown 错,且**把分母也吃掉了**
(看起来只有 8 passed,真实规模 889 条)。

🔴 本包**不跑那 48 份**(那要 40 秒且依赖一次性库)。行为面的锁在
   `scripts`/交付单里以复现器形式留档;这里钉的是**结构**:
   谁都不许再把这个形状写回来。两条机制不同,一起才盖得住。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: 🔴 **改用 AST,不用正则。**
#:    第一版正则 `=\s*io\.TextIOWrapper\(\s*sys\.(stdout|stderr)\.buffer`
#:    只认**字面量那一种拼法**。注毒实测:写成
#:        setattr(sys, name, io.TextIOWrapper(_s.buffer, ...))
#:    (变量间接一层 + setattr)它就**完全看不见** —— 毒下成了、锁仍绿。
#:    判据里每一个「形状假设」都是自陈盲区;这里的假设是「一定写成 sys.stdout.buffer」。
#:    AST 直接看「**装给了谁**」和「**装的是不是 TextIOWrapper 调用**」,
#:    与怎么拼写无关,顺带天然无视注释。
#: 只扫**判据会 import 到**的生产面。`scripts/` 里那些一次性脚本属存量,
#: 见 WO_228 §4 的冻结基数(只减不增)。
SCANNED_DIRS = ("api", "db", "services", "tools", "writing", "workflows",
                "agents", "utils", "middleware", "auth", "config")


def _takeover_sites(source: str):
    """返回「把某个流的 `.buffer` 接管后装到 `sys.stdout/stderr`」的行号。

    🔴 **不看 callee 叫什么。** 判别的是**行为**:
       ① 值是一个调用,且它的参数里出现 `<任意>.buffer`;
       ② 这个值被**装到** `sys.stdout` / `sys.stderr` 上
          —— 直接赋值 / 元组解包 / `setattr(sys, …)` 三种都算。

    历史(每一版都被一发毒打穿,记着免得再回去):
      · v1 正则认 `= io.TextIOWrapper(sys.stdout.buffer` —— `setattr` + 变量间接即盲;
      · v2 AST 认 callee 名 == `TextIOWrapper` —— `_w = io.TextIOWrapper` 别名即盲;
      · v3(本版)只认行为,名字一概不问。
    **前两版都是「按名字认身份」,而名字匹配不是归属。**
    """
    import ast

    def _is_buffer_attr(node):
        return isinstance(node, ast.Attribute) and node.attr == "buffer"

    def _takes_over_a_buffer(node):
        """值是不是「参数里带 <x>.buffer 的调用」。"""
        if not isinstance(node, ast.Call):
            return False
        for a in node.args:
            if _is_buffer_attr(a):
                return True
        for kw in node.keywords:
            if _is_buffer_attr(kw.value):
                return True
        return False

    def _is_sys_std(node):
        return (isinstance(node, ast.Attribute)
                and node.attr in ("stdout", "stderr")
                and isinstance(node.value, ast.Name) and node.value.id == "sys")

    hits = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                # sys.stdout = f(x.buffer)
                if _is_sys_std(tgt) and _takes_over_a_buffer(node.value):
                    hits.append(node.lineno)
                # sys.stdout, sys.stderr = f(a.buffer), g(b.buffer)  —— 元组解包
                if isinstance(tgt, (ast.Tuple, ast.List)):
                    vals = (node.value.elts
                            if isinstance(node.value, (ast.Tuple, ast.List))
                            else [node.value] * len(tgt.elts))
                    for sub_t, sub_v in zip(tgt.elts, vals):
                        if _is_sys_std(sub_t) and _takes_over_a_buffer(sub_v):
                            hits.append(node.lineno)
        # setattr(sys, <任意>, f(x.buffer))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) == 3
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "sys"
                and _takes_over_a_buffer(node.args[2])):
            hits.append(node.lineno)
    return sorted(set(hits))


def _code(path) -> str:
    """🔴 在**剥掉注释**的代码上扫。

    第一版直接扫原始文本 ⇒ `server.py` 被自己**引用旧写法的那句注释**命中,
    判据恒红。这是 WO_205 那个「断言被注释满足」的**反面**:
    注释让一条**否定**断言永远不成立。同一个根子:匹配的是文本不是代码。
    """
    from tests._shared.source_slice import code_only

    return code_only(io.open(path, encoding="utf-8").read())


def _py_files():
    yield REPO / "server.py"
    for d in SCANNED_DIRS:
        base = REPO / d
        if base.is_dir():
            for p in base.rglob("*.py"):
                if "__pycache__" not in p.parts:
                    yield p


def test_server_does_not_take_over_the_stdio_buffer():
    """🔴 正面钉住被修的那一处。"""
    hits = _takeover_sites(io.open(REPO / "server.py", encoding="utf-8").read())
    assert not hits, (
        "server.py 第 %s 行又把 TextIOWrapper 装回 sys.stdout/stderr —— "
        "这正是 WO_228 修掉的东西:它会关掉底下的 buffer,"
        "而 pytest 下那是捕获用的临时文件" % hits)


def test_server_still_configures_utf8_via_reconfigure():
    """**反向对照**:不能靠「把那两行删了」来满足上一条。

    没有这一条的话,谁把编码修复整段删掉,上一条照样绿 ——
    而 Windows 控制台会重新开始在 emoji 上崩。
    """
    # 🔴 在**剥掉注释**的代码上断言。注毒实测:直接读原始文本时,
    #    `reconfigure(` / `encoding='utf-8'` / `line_buffering=` 全都出现在
    #    我自己写的**注释**里 ⇒ 把整段编码修复删掉,这条照样绿。
    #    「断言被注释满足」在本仓是累犯形态,而我正是在刚修过它的文件里又犯一次。
    src = _code(REPO / "server.py")
    assert "reconfigure(" in src, "server.py 不再配置 stdio 编码了 —— 上一条锁没有区分力"
    assert "encoding='utf-8'" in src or 'encoding="utf-8"' in src
    # 🔴 `line_buffering` 必须**显式**写出来:不写的话 reconfigure 保留流当前的值,
    #    而真实终端上 stderr 本来是 line_buffering=True,那就和改前不一样了。
    assert "line_buffering=" in src, (
        "reconfigure 没有显式钉 line_buffering —— 改前后的缓冲行为会不一样")


def test_no_production_module_takes_over_the_stdio_buffer():
    """🔴 整个生产面都不许有这个形状(不是只管 server.py 一处)。

    工单点名的那一处不是缺陷类本身;`scripts/` 下另有 11 处同形,
    属存量冻结名单(只减不增),见 WO_228 §4。
    """
    offenders = []
    for path in _py_files():
        try:
            sites = _takeover_sites(io.open(path, encoding="utf-8").read())
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        if sites:
            offenders.append("%s:%s" % (
                str(path.relative_to(REPO)).replace("\\", "/"), sites))
    assert not offenders, (
        "这些生产模块在接管 stdio 的 buffer,会在 pytest 下关掉捕获流:\n  %s"
        % "\n  ".join(sorted(offenders)))


def test_the_regex_actually_matches_the_old_shape():
    """🔴 **正样本**:判据的锚必须真能认出被修掉的那个写法。

    没有这一条,上面三条可能只是「正则从来匹配不到任何东西」而恒绿 ——
    那种锁和没有锁一样,还更像有。
    """
    import sys as _sys  # noqa: F401  (给下面的样本代码一个合法上下文)

    # ① 最基本的旧写法
    assert _takeover_sites(
        "import io, sys\n"
        "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')\n"
    ), "认不出最基本的旧写法 —— 这几条锁全是假的"

    # ② setattr + 变量间接(v1 正则在这里瞎掉,我自己的 P4 才发现)
    assert _takeover_sites(
        "import io, sys\n"
        "for n in ('stdout','stderr'):\n"
        "    s = getattr(sys, n)\n"
        "    setattr(sys, n, io.TextIOWrapper(s.buffer, encoding='utf-8'))\n"
    ), "认不出 setattr + 变量间接那一种"

    # ③ 🔴 **别名 + setattr**(v2 按 callee 名认,在这里瞎掉 —— 复审那发毒)
    assert _takeover_sites(
        "import io, sys\n"
        "_w = io.TextIOWrapper\n"
        "setattr(sys, 'stdout', _w(sys.stdout.buffer, encoding='utf-8'))\n"
    ), "认不出别名 + setattr —— 换个名字就绕过去了"

    # ④ 元组解包
    assert _takeover_sites(
        "import io, sys\n"
        "sys.stdout, sys.stderr = (io.TextIOWrapper(sys.stdout.buffer), io.TextIOWrapper(sys.stderr.buffer))\n"
    ), "认不出元组解包那一种"

    # ⑤ 关键字传 buffer
    assert _takeover_sites(
        "import io, sys\n"
        "sys.stdout = io.TextIOWrapper(buffer=sys.stdout.buffer)\n"
    ), "认不出关键字传 buffer 那一种"

    # ⑥ 新写法不许被当成违规(否则恒红)
    assert not _takeover_sites(
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=False)\n"
    ), "把新写法也当成违规 —— 会恒红"

    # ⑦ 反向:只是**读** buffer、没装回 sys.std*,不算违规
    assert not _takeover_sites(
        "import io, sys\n"
        "local = io.TextIOWrapper(sys.stdout.buffer)\n"
        "local.write('x')\n"
    ), "把「没装回 sys.std* 的局部包装」也当成违规 —— 过宽"


def test_reconfigure_keeps_the_same_stream_object():
    """🔴 行为面的最小证据:`reconfigure` **不换对象**。

    这条不依赖那 48 份复现器,任何机器上都能跑:
    换对象才会出现「旧对象被回收时关掉 buffer」,不换就不会。
    """
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="gbk", errors="strict",
                              line_buffering=True)
    before = id(stream)
    stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=False)
    assert id(stream) == before, "reconfigure 换了对象 —— 前提不成立"
    assert stream.encoding.lower() in ("utf-8", "utf8")
    assert stream.errors == "replace"
    assert stream.line_buffering is False
    assert not buf.closed, "reconfigure 关掉了底下的 buffer —— 那就没解决问题"

    # 反向对照:老写法**确实**会接管并在回收时关掉 buffer
    buf2 = io.BytesIO()
    inner = io.TextIOWrapper(buf2, encoding="gbk")
    wrapper = io.TextIOWrapper(inner.buffer, encoding="utf-8", errors="replace")
    wrapper.close()
    assert buf2.closed, "老写法没关掉 buffer —— 那这条对照没有区分力"
