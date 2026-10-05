"""【V5-B · Review-CTO 裁定 2026-08-29】CRLF 豁免必须是**窄**的那一把。

背景
----
`api/defensive_geo_api.py` 整文件 CRLF(1571 CRLF / 0 裸 LF),``core.autocrlf=false``。
默认 ``core.whitespace`` 把行尾 CR 当 trailing whitespace ⇒ 任何人往它加/改一行,
``git diff --check`` 就返回 2。实测这不是某一单引入的:V4-A 那笔、以及合流尖对
发车底,都是 2。

裁定走 ``.gitattributes`` 加一行,而**不是**整文件转 LF。取值选
``whitespace=cr-at-eol`` 而不是 ``-whitespace`` —— 后者把这个文件的空白检查
整个关掉,前者只放过行尾那个 CR。

为什么要有这条判据
------------------
「窄」是这个处置能被接受的**全部理由**。而理由写在注释里就只是一句话:
哪天有人图省事改成 ``-whitespace``、或者把它扩成 ``api/*``,
注释还在,门已经没了 —— 而且不会有任何东西变红。

🔴 为什么理由写在**这里**而不是 `.gitattributes` 里
--------------------------------------------------
Review 补令(来自窗口B 的预警):`.gitattributes` 只许**追加一行**,不许重排/重写 ——
第 31/32 行 `scripts/test_mutation_*.py text eol=lf` 与
`scripts/mutation_replay_common.py text eol=lf` 被 B 的 e06 判据逐字钉着,
而任何通配 EOL 规则(`* text=auto` / `*.py text eol=lf`)都会让 `git add`
归一化 4 个存量 CRLF 脚本、把 B 的 e03 豁免集打红。
我第一版在那个文件里写了 16 行注释(+16/-0),已按补令收成 **+1/-0**;
理由整段搬到这里 —— 注释可以搬,**判据不能少**。

所以这里把那条边界做成可执行的:
  ① 问 **git 自己**解析出来的属性值(不是 grep ``.gitattributes`` ——
     后面的规则会覆盖前面的,文本匹配看不出最终生效的是哪条);
  ② 值必须**恰好**是 ``cr-at-eol``,不是 ``-whitespace``、也不是更宽的集合;
  ③ 配对的必须不命中:豁免只落在**这一个**路径上,邻近的文件仍是默认口径。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

EXEMPT_PATH = "api/defensive_geo_api.py"

#: 配对的必须不命中 —— 这些文件不许被顺带豁免掉。
NEIGHBOURS = (
    "api/defensive_geo_assist_api.py",
    "api/selection_api.py",
    "services/defensive_geo/copy_registry.py",
)


def _check_attr(path: str) -> str:
    """问 git:这个路径上 ``whitespace`` 属性最终解析成什么。"""
    out = subprocess.run(
        ["git", "check-attr", "whitespace", "--", path],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, "git check-attr 跑不起来:%s" % out.stderr[:300]
    # 形如 "api/x.py: whitespace: cr-at-eol"
    line = out.stdout.strip()
    assert line, "git check-attr 没有输出 —— 判据不可用,不当绿灯"
    return line.rsplit(":", 1)[-1].strip()


def test_00_the_file_is_really_whole_crlf() -> None:
    """先钉住这条豁免的**前提**:它真的是整文件 CRLF。

    哪天有人把它转成 LF 了,这条豁免就没有存在理由,应当一并删掉 ——
    留着一条没有前提的豁免,下一个人只会照抄。
    """
    b = (REPO / EXEMPT_PATH).read_bytes()
    crlf = b.count(b"\r\n")
    lone_lf = b.count(b"\n") - crlf
    assert crlf > 1000 and lone_lf == 0, (
        "%s 不再是整文件 CRLF(CRLF=%d 裸LF=%d)—— 那这条 .gitattributes 豁免"
        "已经没有前提了,应当删掉它,而不是留着" % (EXEMPT_PATH, crlf, lone_lf))


def test_01_exemption_is_the_narrow_one() -> None:
    got = _check_attr(EXEMPT_PATH)
    assert got == "cr-at-eol", (
        "%s 的 whitespace 属性解析成 %r。\n"
        "必须**恰好**是 cr-at-eol:`-whitespace`(或 unset)会把这个文件的空白检查"
        "整个关掉,真的 trailing space 就再也没人管了;而选这条窄豁免的理由,"
        "正是它只放过行尾那个 CR。" % (EXEMPT_PATH, got))
    assert got != "unset", "属性被 unset 了 —— 那等于 -whitespace"


@pytest.mark.parametrize("path", NEIGHBOURS)
def test_02_exemption_does_not_leak_to_neighbours(path) -> None:
    """配对的必须不命中:豁免只在那一个路径上。

    没有这一条,把规则写成 ``api/* whitespace=cr-at-eol`` 也会让上面那条绿 ——
    而那样整个 api 目录的行尾空白就都没人守了。
    """
    got = _check_attr(path)
    assert got == "unspecified", (
        "%s 也被豁免了(%r)—— 这条豁免应当只落在 %s 一个路径上"
        % (path, got, EXEMPT_PATH))


def test_03_narrowness_is_behaviourally_true() -> None:
    """**行为**上证明它是窄的:同一个文件里,行尾 CR 放过、真空格仍红。

    🔴 属性值对不代表行为对。这一条不改工作树 —— 用 ``git stripspace``
    之外的办法:把两份内容喂给 ``git diff --check`` 的等价物 ``git apply --check``
    太绕,所以这里用 ``git diff --no-index --check`` 在**临时文件**上打,
    并且让临时文件带上同一条 attr(靠 ``-c core.whitespace`` 显式复现口径)。
    """
    import tempfile

    body_cr = b"line one\r\nline two\r\n"
    body_cr_space = b"line one\r\nline two   \r\n"
    with tempfile.TemporaryDirectory() as d:
        a = pathlib.Path(d) / "a.txt"
        b = pathlib.Path(d) / "b.txt"
        a.write_bytes(b"line one\r\n")

        # 🔴 判别信号不能只看退出码:`git diff --no-index` 的 **rc=1 是"两个文件有差异"**,
        #    与白空格无关。第一版拿 rc==0 当"干净",于是纯 CRLF 那一臂被判红,
        #    而红的理由完全不对 —— 判据自己先踩了「比错误签名」那一条。
        #    真正的终态是 `--check` 打出来的那行 "trailing whitespace"。
        def check(payload: bytes) -> str:
            b.write_bytes(payload)
            r = subprocess.run(
                ["git", "-c", "core.whitespace=blank-at-eol,space-before-tab,"
                 "blank-at-eof,cr-at-eol",
                 "diff", "--no-index", "--check", str(a), str(b)],
                cwd=REPO, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            return r.stdout

        cr_only = check(body_cr)
        cr_plus_space = check(body_cr_space)

    assert "whitespace" not in cr_only, (
        "同一口径下纯 CRLF 行被报了白空格 —— 豁免没生效:\n%s" % cr_only[:300])
    assert "trailing whitespace" in cr_plus_space, (
        "在 CR 之前塞了真空格却没被报出来 —— 这把豁免比说的宽,"
        "真的 trailing space 已经没人管了:\n%s" % (cr_plus_space[:300] or "(无输出)"))
