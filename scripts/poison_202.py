# -*- coding: utf-8 -*-
"""WO_202 注毒闸 —— 仓级控制字符闸。

工单 §2 点名三发:
  G2 往任一测试文件注入一个 0x08 ⇒ 红,且报文指到该文件与偏移
  G3 白名单加一条**目录级**路径 ⇒ 结构臂红(白名单只许文件路径)
  G4 反向:注入 TAB / LF / CR ⇒ **不红**
外加打「分母」与「白名单不许赦免干净文件」的两发,和一发阴性对照。

🔴 注入用 `bytes([8])` 拼,源码里一个反斜杠都不出现 ——
   这个脚本要是自己被反斜杠转义坑了,它就变成了它要抓的那个病。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/repo_hygiene_2026_09_13"
EXPECTED_N = 9
TESTFILE = PKG + "/test_no_control_chars.py"
VICTIM = "tests/g1_contract_gate_2026_09_15/test_g1_gate.py"

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_g1_gate_test"
ENV = dict(os.environ)
# 仓根 conftest 在 import 期就要求 TEST_DATABASE_URL —— 本包自己不碰库,
# 但没有它整场收不到用例(rc=4、0 个 FAILED),会被读成"全绿"。
ENV["TEST_DATABASE_URL"] = _DSN
ENV["DATABASE_URL"] = _DSN
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["PYTHONUTF8"] = "1"
_SUMMARY = re.compile(r"\b\d+ (passed|failed|error|errors|skipped)\b")


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def rb(path):
    return io.open(os.path.join(ROOT, path), "rb").read()


def wb(path, data):
    io.open(os.path.join(ROOT, path), "wb").write(data)


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    if not [l for l in out.splitlines() if _SUMMARY.search(l) and " in " in l]:
        raise RuntimeError("pytest 没给摘要行 —— 仪器坏了:" + out[-1200:])
    failed = {l.split()[1].split(" - ")[0] for l in out.splitlines()
              if (l.startswith("FAILED ") or l.startswith("ERROR "))
              and len(l.split()) > 1 and "::" in l.split()[1]}
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total, out


def _inject_byte(b):
    """往受害文件的第一行末尾塞一个字节,返回还原用的原始内容。"""
    def apply():
        data = rb(VICTIM)
        nl = data.index(b"\n")
        wb(VICTIM, data[:nl] + bytes([b]) + data[nl:])
    return apply


def _whitelist_dir():
    """把白名单里加一条**目录级**路径。"""
    def apply():
        s = io.open(os.path.join(ROOT, TESTFILE), encoding="utf-8").read()
        anchor = "CONTROL_CHAR_WHITELIST = {"
        s = s.replace(anchor, anchor + chr(10)
                      + '    "scripts/": "目录级赦免 —— 毒:这一条必须让结构臂红",', 1)
        io.open(os.path.join(ROOT, TESTFILE), "w", encoding="utf-8",
                newline=chr(10)).write(s)
    return apply


def _whitelist_clean_file():
    """白名单赦免一个**干净**文件。"""
    def apply():
        s = io.open(os.path.join(ROOT, TESTFILE), encoding="utf-8").read()
        anchor = "CONTROL_CHAR_WHITELIST = {"
        s = s.replace(anchor, anchor + chr(10)
                      + '    "utils/is_test_brand.py": "毒:这个文件已经修干净了,'
                        "赦免它应当让「白名单不许赦免干净文件」那条红\",", 1)
        io.open(os.path.join(ROOT, TESTFILE), "w", encoding="utf-8",
                newline=chr(10)).write(s)
    return apply


def _shrink_forbidden():
    """把禁止集合缩成空集(「一个控制字符都没查出来」的另一种造假法)。"""
    def apply():
        s = io.open(os.path.join(ROOT, TESTFILE), encoding="utf-8").read()
        s = s.replace(
            "FORBIDDEN_BYTES = frozenset(" + chr(10)
            + "    list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)))",
            "FORBIDDEN_BYTES = frozenset()", 1)
        io.open(os.path.join(ROOT, TESTFILE), "w", encoding="utf-8",
                newline=chr(10)).write(s)
    return apply


# (名字, 受影响文件, 施毒函数, 应当红吗, 说明)
POISONS = [
    ("NC-阴性对照:判据文件加一句无害注释(应绿)", TESTFILE,
     lambda: _append_comment(), False,
     "字节变了、行为没变 —— 绿才对。"),

    ("G2-往判据文件注入一个 0x08", VICTIM, _inject_byte(0x08), True,
     "工单点名那一发:这正是 09-13 三起事故的字节。报文要指到文件与偏移。"),

    ("G2b-注入 0x1b(ESC)", VICTIM, _inject_byte(0x1B), True,
     "只挡 0x08 不够:同族的其它 C0 字节一样看不见。"),

    ("G3-白名单加一条目录级路径", TESTFILE, _whitelist_dir(), True,
     "目录级赦免会把**将来**放进该目录的真源码一起放过 —— "
     "而那正是这把闸要抓的东西。"),

    ("G3b-白名单赦免一个已经干净的文件", TESTFILE, _whitelist_clean_file(), True,
     "赦免干净文件不会红,但白名单会越积越长,最后没人知道哪条还成立。"),

    ("G5-把禁止集合缩成空集", TESTFILE, _shrink_forbidden(), True,
     "「一个都没查出来」的另一种造假法:尺子清零。"),

    ("G4-注入 TAB(应当**不红**)", VICTIM, _inject_byte(0x09), False,
     "反向对照:制表符是正常字符。这一发红了说明闸会恒红,而恒红等于没有闸。"),

    ("G4b-注入 CR(应当**不红**)", VICTIM, _inject_byte(0x0D), False,
     "同上:CRLF 在本仓是常见行尾。"),
]


def _append_comment():
    s = io.open(os.path.join(ROOT, TESTFILE), encoding="utf-8").read()
    io.open(os.path.join(ROOT, TESTFILE), "w", encoding="utf-8",
            newline=chr(10)).write(s + chr(10) + "# 阴性对照注入的一行" + chr(10))


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = [p for p in POISONS
                if not only or any(p[0].startswith(k) for k in only)]
    paths = sorted({p[1] for p in selected})
    base_sha = {p: sha(p) for p in paths}
    base_raw = {p: rb(p) for p in paths}

    failed, total, _out = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空 —— 红基线会让每发毒都像命中")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if not only and total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, apply, should_red, _why in selected:
        apply()
        psha = sha(path)
        if psha == base_sha[path]:
            wb(path, base_raw[path])
            rows.append((name, "毒没下成", "sha 没变"))
            continue
        try:
            pf, ptotal, out = run()
        finally:
            wb(path, base_raw[path])
            assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        ok = bool(pf) == should_red
        verdict = ("红(锁有牙)" if pf else "绿") + ("" if ok else "  !! 与预期不符")
        detail = "sha %s->%s · 收集 %d · %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:90] or "无红")
        # G2 还要求报文指到文件与偏移
        if name.startswith("G2") and pf:
            has_loc = (VICTIM in out) and re.search(r":\d+:0x[0-9a-f]{2}", out)
            detail += " · 报文含 文件:偏移:字节 = %s" % bool(has_loc)
        rows.append((name, verdict, detail))

    print("")
    for name, verdict, detail in rows:
        print("%-44s %-24s %s" % (name, verdict, detail))
    f2, t2, _ = run()
    print("")
    print("还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
