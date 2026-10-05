# -*- coding: utf-8 -*-
"""WO_233-c1 分母腿:全仓「由分数反推等级」的调用点固化。

工单要求「逐个判断是否同病;不在本单改的要列出并说明为什么不改」。
把这份判断**钉成判据** —— 新增一个反推点而没人复核时,这条会红。

🔴 判定按**调用**枚举(AST),不按 grep 文本:注释和 docstring 里提到
`get_funnel_meta` 的行有一堆,文本计数会把它们算进分母
(本仓 text-matching-a-structured-field-fabricates-plausible-counts)。
"""
from __future__ import annotations

import ast
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 「拿一个分数换一个等级」的函数名。
_DERIVERS = {"get_funnel_meta", "_get_funnel_level", "get_level", "get_meta",
             "get_level_meta"}

#: 生产代码里允许存在的反推点,**逐个写明为什么不改**。
#: 格式:(文件, 函数名) -> 理由
#: 🔴 连**次数**一起钉。只钉 (文件, 函数名) 的话,同一文件里多加一次调用不产生新键
#:   —— 注毒台的 P7(在 report_metrics 里再插一个反推点)当场存活。
#:   分母的粒度不够细,就等于给「新增一处反推」留了一条不报警的路。
_KNOWN = {
    ("services/report_v2_score.py", "get_funnel_meta"): (
        1, "本单当事人。v2 对客读取口,已改为:反推值只作上限,存储的封顶等级(不高于它)优先。"),
    ("services/report_v2_score.py", "get_level"): (
        1, "v1(5 维旧体系)路径。v1 没有封顶概念,维持老行为不信存储文本 —— 不同病。"),
    ("services/report_metrics.py", "get_level_meta"): (
        1, "v1 5 维总分 → 等级。它自己就是算分方,不是读取方,没有封顶可推翻 —— 不同病。"),
}

_SKIP_TOP = {"tests", "scripts", "frontend", "docs", "data", "node_modules",
             "agent-test-artifacts", "agent-test-artifacts-round2",
             "agent-test-artifacts-round3", "tools"}


def _production_py():
    import subprocess
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-300:]
    files = []
    for line in out.stdout.splitlines():
        rel = line.strip()
        if rel and rel.split("/", 1)[0] not in _SKIP_TOP:
            files.append(rel)
    return files


def _enclosing_func(tree, lineno):
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= lineno:
            if best is None or n.lineno > best.lineno:
                best = n
    return best.name if best else "<module>"


def test_every_score_to_level_derivation_is_accounted_for():
    """🔴 分母:生产代码里每一个反推点都在已知名单里,且名单里每条都还活着。"""
    files = _production_py()
    assert len(files) > 200, "只扫到 %d 个生产 .py —— 扫描器坏了" % len(files)

    found = {}
    for rel in files:
        try:
            src = (REPO / rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue          # 本仓已知 tools/scoring/geo_scorer_backup.py,且 tools/ 已排除
        try:
            tree = ast.parse(src, rel)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name in _DERIVERS:
                found[(rel, name)] = found.get((rel, name), 0) + 1

    unknown = sorted(k for k in found if k not in _KNOWN)
    assert not unknown, (
        "出现了名单外的「由分数反推等级」调用点,必须逐个判断是否同病:\n  %s"
        % "\n  ".join("%s :: %s()" % k for k in unknown))

    stale = sorted(k for k in _KNOWN if k not in found)
    assert not stale, (
        "名单里这些反推点已经不存在了,名单比问题活得久:\n  %s"
        % "\n  ".join("%s :: %s()" % k for k in stale))

    drift = sorted((k, _KNOWN[k][0], found[k]) for k in _KNOWN if found[k] != _KNOWN[k][0])
    assert not drift, (
        "反推点的**次数**变了(同一文件里多/少了一次调用 —— 键不变,所以不会报未知):\n  %s"
        % "\n  ".join("%s :: %s() 期望 %d 次,实测 %d 次" % (k[0], k[1], e, g)
                      for k, e, g in drift))


def test_the_scanner_can_see_a_new_derivation():
    """🔴 正样本臂:同一个探测器对一段**新增**的反推调用必须认出来。

    没有这一条,上面那条绿可能只是"探测器什么都没扫到"
    —— 而 0 个未知点和 0 次扫描读数一模一样。
    """
    src = "def f(score):\n    return get_funnel_meta(score)['level']\n"
    tree = ast.parse(src)
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in _DERIVERS]
    assert hits, "探测器认不出最普通的一种反推写法"


def test_the_report_writer_uses_the_capped_level_not_a_re_derived_one():
    """🔴 `report_writer_v2` 不在名单里,因为它**不反推** —— 它直接用评分器给的等级。

    这条把那个判断钉住:哪天有人把它改成「按分数反推」,报告正文就会和
    对客读取口再次分家,而那正是本单在修的病。
    """
    src = io.open(REPO / "services" / "report_writer_v2.py", encoding="utf-8").read()
    tree = ast.parse(src)
    derivers = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and (getattr(n.func, "id", None) in _DERIVERS
                     or getattr(n.func, "attr", None) in _DERIVERS)]
    assert not derivers, (
        "report_writer_v2 开始自己反推等级了(行 %s)—— 它应当直接用 funnel['level']"
        % [n.lineno for n in derivers])
    assert 'funnel["level"]' in src or "funnel['level']" in src, (
        "report_writer_v2 不再直接读 funnel['level'] —— 这条判据的前提变了,要重写")
