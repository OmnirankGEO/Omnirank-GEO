"""WO_202 · 仓级控制字符闸(后端)。

工单 `C:\\AI-Test\\WO_202_REPO_CONTROL_CHAR_GATE_RV_2026-09-13.md`
(sha256 前 16 位 `89ed76ba65fedb81`)。

来由:09-13 同族事故三起 —— #195 A 的判据里 `反斜杠b` 被改写脚本吃成退格 0x08,
于是**否定断言恒真恒绿**(全仓四处);C 当天 heredoc / 非 raw 字符串两次同病。
前端已有 `verify-no-control-chars` 进 build 链,后端没有;
C 在 #196 包里加的 `_no_control_chars()` 只罩那一个包。

🔴 这类字节的坏处不是"读起来怪",是**看不见**:
   `反斜杠b` 被吃成 0x08 之后,`!/…/` 这种否定断言恒为真,
   判据看着在跑、其实什么都没验。肉眼、code review、diff 都发现不了。
"""
import io
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 工单 §1 指定的分母 glob(受管文件,不用磁盘 glob —— 未跟踪的垃圾不该进分母)。
LS_FILES_PATHSPEC = ["*.py", "*.sql", "*.sh", "tests/**", "scripts/**"]

#: 🔴 与前端那把闸分工:前端只扫 `frontend/`,本闸不扫(工单 §1 末条)。
#:    两把闸各扫各的,重叠会让"谁该修"变成扯皮。
EXCLUDE_PREFIXES = ("frontend/",)

#: 禁止出现的字节:C0 控制字符去掉 TAB(0x09)/ LF(0x0A)/ CR(0x0D)。
FORBIDDEN_BYTES = frozenset(
    list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)))

#: **二进制资产**的扩展名。这不是白名单 —— 它只决定「解不出 UTF-8 的文件
#: 允不允许被跳过」。一个解不出 UTF-8 的 `.py` 不在这个集合里,
#: 于是必须走下面的逐文件白名单,**藏不住**。
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pptx", ".docx", ".xlsx",
    ".zip", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3", ".wav", ".webp",
})

#: 🔴 逐文件白名单:**路径 + 理由**,不许目录级、不许「其它」(工单 §1)。
#:    目录级白名单的坏处是它会把**将来**放进那个目录的真源码一起赦免,
#:    而那正是这把闸要抓的东西。
CONTROL_CHAR_WHITELIST = {
    "tools/scoring/geo_scorer_backup.py": (
        "历史损坏文件:头部有 4 字节垃圾(12 a9 f4 01)、整文件 CRLF、"
        "**不是合法 UTF-8**、含 25178 个控制字节。全仓**无任何 import**"
        "(只有 scripts/research/naive_time_business_day_census.py:157 的注释"
        "把它当已知损坏文件提了一句)。属存量清理,已列入 WO_205;"
        "在那之前显式赦免,不让它把这把闸变成恒红。"
    ),
}


def _managed_files():
    out = subprocess.run(
        ["git", "ls-files", "--"] + LS_FILES_PATHSPEC,
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, "git ls-files 失败:%s" % out.stderr[-200:]
    files = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    return [f for f in files if not f.startswith(EXCLUDE_PREFIXES)]


def _scan(path):
    """返回 (命中列表, 是否可解码)。命中形如 (偏移, 字节)。"""
    data = io.open(str(ROOT / path), "rb").read()
    try:
        data.decode("utf-8")
        decodable = True
    except UnicodeDecodeError:
        decodable = False
    hits = [(i, b) for i, b in enumerate(data) if b in FORBIDDEN_BYTES]
    return hits, decodable


@pytest.fixture(scope="session")
def scan_result():
    files = _managed_files()
    assert files, "分母为空 —— git ls-files 没给出任何文件,闸没跑成"
    text_hits, skipped_assets, undecodable_other = {}, [], []
    scanned = 0
    for f in files:
        hits, decodable = _scan(f)
        if not decodable:
            ext = os.path.splitext(f)[1].lower()
            (skipped_assets if ext in BINARY_EXTENSIONS
             else undecodable_other).append(f)
            continue
        scanned += 1
        if hits:
            text_hits[f] = hits
    return {"files": files, "scanned": scanned, "hits": text_hits,
            "assets": skipped_assets, "undecodable_other": undecodable_other}


# ════════════════════════════════════════════════════════════════
# G1 · 全绿,且分母打印
# ════════════════════════════════════════════════════════════════
def test_no_managed_text_file_contains_a_control_char(scan_result, capsys):
    """受管文本文件里**一个控制字节都不许有**;报文给到 `文件:偏移:字节`。"""
    hits = {f: v for f, v in scan_result["hits"].items()
            if f not in CONTROL_CHAR_WHITELIST}
    lines = []
    for f, v in sorted(hits.items()):
        for off, b in v[:5]:
            lines.append("%s:%d:0x%02x" % (f, off, b))
        if len(v) > 5:
            lines.append("%s:… 共 %d 处" % (f, len(v)))
    assert not lines, (
        "受管文件里有控制字节(它们看不见,会让否定断言恒真):" + chr(10)
        + chr(10).join(lines))


def test_the_denominator_is_printed_and_accounts_for_every_file(scan_result, capsys):
    """🔴 分母要**打印**,而且**每个文件都有去处**:扫了 / 资产跳过 / 白名单。

    少了这一条,「0 命中」可以来自「一个文件都没扫」——
    本仓已经因为"0 失败其实是没跑"栽过不止一次。
    """
    r = scan_result
    total = len(r["files"])
    accounted = r["scanned"] + len(r["assets"]) + len(r["undecodable_other"])
    with capsys.disabled():
        print(chr(10) + "[WO_202] 受管文件 %d = 扫描 %d + 二进制资产 %d + 非 UTF-8 其它 %d"
              % (total, r["scanned"], len(r["assets"]), len(r["undecodable_other"])))
    assert total > 0
    assert r["scanned"] > 0, "一个文本文件都没扫到 —— 闸没跑成,不是仓库干净"
    assert accounted == total, (
        "有 %d 个文件既没被扫、也没归类 —— 分母漏了" % (total - accounted))


def test_every_undecodable_non_asset_file_is_explicitly_whitelisted(scan_result):
    """🔴 解不出 UTF-8 **又不是已知二进制扩展名**的文件,必须逐个白名单。

    这一条是「按扩展名跳过资产」那个规则的**堵漏**:
    一个损坏到解不出 UTF-8 的 `.py`(仓里就有一个)扩展名不在资产集合里,
    于是它落到这里 —— 要么显式赦免并写清理由,要么红。
    没有这条的话,"按内容判断二进制"就成了一个能藏 `.py` 的目录级白名单。
    """
    strays = [f for f in scan_result["undecodable_other"]
              if f not in CONTROL_CHAR_WHITELIST]
    assert not strays, (
        "这些文件解不出 UTF-8、扩展名又不是已知二进制资产,必须逐个定性:%s" % strays)


# ════════════════════════════════════════════════════════════════
# G3 · 白名单的**结构**:只许文件路径 + 必须有理由
# ════════════════════════════════════════════════════════════════
def test_the_whitelist_holds_only_file_paths_with_reasons():
    """白名单只许**具体文件**,不许目录;每条都要有理由;文件必须还在。

    目录级白名单会把**将来**放进该目录的真源码一起赦免 —— 而那正是这把闸
    要抓的东西。「其它」同理:它是把没读懂的那条藏起来的地方。
    """
    for path, reason in CONTROL_CHAR_WHITELIST.items():
        full = ROOT / path
        assert not path.endswith("/"), "目录级白名单:%s" % path
        assert full.is_file(), (
            "白名单指着一个不存在的文件:%s —— 文件删了就该同步删表" % path)
        assert reason and len(reason.strip()) >= 20, (
            "白名单条目没有像样的理由:%s" % path)
        assert "其它" not in reason and "其他" not in reason, (
            "理由写成了「其它」:%s" % path)


def test_the_whitelist_does_not_cover_clean_files():
    """白名单里的文件必须**真的**有控制字节。

    赦免一个干净文件不会红,但它会让白名单越积越长,
    最后没人知道哪一条还成立 —— 表变成墓地,分母就烂了。
    """
    for path in CONTROL_CHAR_WHITELIST:
        hits, _decodable = _scan(path)
        assert hits, "%s 已经干净了 —— 从白名单里删掉它" % path


# ════════════════════════════════════════════════════════════════
# G4 · 反向对照:制表 / 换行 / 回车**不算**
# ════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("byte,name", [(0x09, "TAB"), (0x0A, "LF"), (0x0D, "CR")])
def test_ordinary_whitespace_is_not_flagged(byte, name):
    """少了它,「一律禁 C0」会把每个文件都判红 —— 恒红等于没有闸。"""
    assert byte not in FORBIDDEN_BYTES, "%s 不该被当成控制字符" % name


def test_the_forbidden_set_is_the_c0_range_minus_whitespace():
    """正面钉住集合本身:C0 全域减去 TAB/LF/CR,一个不多一个不少。

    只钉"TAB 不在里面"的话,把整个集合缩成空集也能绿。
    """
    expected = set(range(0x00, 0x20)) - {0x09, 0x0A, 0x0D}
    assert set(FORBIDDEN_BYTES) == expected
