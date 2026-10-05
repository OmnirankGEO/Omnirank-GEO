"""WO_266 · 普查门(preflight 5-d)新加七格的判据:牙证 + 对照臂。

被测物是**仓里这一份** `scripts/gate_test_runner_census.py`,按 `--repo/--ref` 跑在一个
临时合成仓上(与 preflight 5-d 同一条读取路径:全部按 git ref 读,不读工作树)。
合成仓的「主干」是 `refs/remotes/github/main` —— 普查门默认的棘轮基准就叫这个名字。

为什么用合成仓而不是改真仓:七格里有四格要**基准 ≠ 待发**才有东西可比,
而真仓的 github/main 我不许动。合成仓里主干想放哪就放哪。

🔴 每一格都配「只让一个条件决定结果」的输入(谓词有 N 个条件就要有 N 组),
   并且每格旁边都有一条**没动过的对照臂**必须绿 —— 恒红的尺子也能把牙证全过。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CENSUS = ROOT / "scripts" / "gate_test_runner_census.py"

GUARD_M = "合成仓必跑包:红了说明合成仓的必跑判据坏了"
GUARD_A = "合成仓随单包 A:红了说明 A 守的那件事回归了"
GUARD_B = "合成仓随单包 B:红了说明 B 守的那件事回归了"

N_DUMMY = 55     # 普查门分母哨兵要 ≥50 项


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_AUTHOR_NAME="wo266", GIT_AUTHOR_EMAIL="wo266@example.invalid",
               GIT_COMMITTER_NAME="wo266", GIT_COMMITTER_EMAIL="wo266@example.invalid",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, f"git {' '.join(args)} 失败:{r.stderr}"
    return r.stdout.strip()


def _oneshot(budget_line: str | None, *entries: str) -> str:
    head = ["# 合成仓随单登记表", "#"]
    if budget_line is not None:
        head.append(budget_line)
    return "\n".join(head + ["", *entries]) + "\n"


def _entry(path: str, guard: str, origin: str = "WO_900 · 合成签字条") -> str:
    return f"{path}\t{guard}\t{origin}"


BASE_FILES: dict[str, str] = {
    ".github/workflows/ci.yml": (
        "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: run\n        run: |\n          pytest tests/test_d00.py -q\n"),
    "tests/pkg_m/test_m.py": "def test_m():\n    pass\n",
    "tests/pkg_a/test_a.py": "def test_a():\n    pass\n",
    "tests/pkg_b/test_b.py": "def test_b():\n    pass\n",
    "tests/MUST_RUN.txt": f"# 合成必跑集\n# MUST_RUN_BUDGET_SECONDS=300\n\ntests/pkg_m\t{GUARD_M}\n",
    "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                       _entry("tests/pkg_a", GUARD_A),
                                       _entry("tests/pkg_b", GUARD_B)),
    "tests/RETIRED_TESTS.txt": "# 合成退役表\n",
    "tests/UNASSIGNED_FROZEN.txt": "# 合成冻结名单\n\n" + "".join(
        f"tests/test_d{i:02d}.py\n" for i in range(1, N_DUMMY)),
    **{f"tests/test_d{i:02d}.py": "def test_ok():\n    pass\n" for i in range(N_DUMMY)},
    # ⑦ 删除引用者用的料:一个会被删的页面 + 两个引用者(一个在 tests/)+ 一处 docs 提及
    "app/DeletedWidget2.tsx": "export const X = 1\n",
    "app/Consumer.tsx": "import { X } from './DeletedWidget2'\n",
    "app/OldNamedThing.tsx": "export const Y = 'old named thing'\n",
    "app/UsesOld.tsx": "import { Y } from './OldNamedThing'\n",
    "app/SameNameMove1.tsx": "export const Z = 'same name move'\n",
    "app/index.ts": "export * from './Consumer'\n",
    "docs/notes.md": "DeletedWidget2 的历史说明(docs 不在引用者分母里)\n",
}
BASE_FILES["tests/test_d03.py"] = ('P = "app/DeletedWidget2.tsx"\n\n\n'
                                   "def test_ok():\n    pass\n")


class Repo:
    def __init__(self, path: Path):
        self.path = path

    def commit(self, parent: str, changes: dict[str, str | None], msg: str, *, checkout: bool = False) -> str:
        """在 parent 上改文件(None = 删,目录整棵删),提交,返回 sha。

        [ONESHOT 提速 4] 一个 `git fast-import` 进程直接写对象(原先 checkout / add / commit / rev-parse 四次起进程):
        树 = parent 的树 + 这些改动,与原「检出 parent、改工作树、add -A、commit」得到的树相同
        (等价由 .deploy_runtime/speed4_ratchet_equiv.py 在整包每一次提交上逐棵树比过)。
        不动 HEAD / 索引 / 工作树 —— 三者保持彼此一致,后面真 checkout / merge 的格照常工作;
        要让 `HEAD` 指到新提交的格传 checkout=True(真检出一次)。"""
        env = _env()
        who = f"{env['GIT_COMMITTER_NAME']} <{env['GIT_COMMITTER_EMAIL']}> {int(time.time())} +0000"
        m = msg.encode("utf-8")
        buf = [b"commit refs/wo266/fastimport\nmark :1\n",
               f"author {who}\ncommitter {who}\n".encode("utf-8"),
               b"data %d\n" % len(m), m, b"\n", f"from {parent}\n".encode("utf-8")]
        for rel, content in changes.items():
            if content is None:
                buf.append(f"D {rel}\n".encode("utf-8"))
            else:
                data = content.encode("utf-8")
                buf += [f"M 100644 inline {rel}\n".encode("utf-8"), b"data %d\n" % len(data), data, b"\n"]
        buf.append(b"\nget-mark :1\n")
        r = subprocess.run(["git", "-C", str(self.path), "fast-import", "--quiet", "--force"],
                           input=b"".join(buf), capture_output=True, env=env)
        assert r.returncode == 0, f"fast-import 失败:{r.stderr.decode('utf-8', 'replace')}"
        sha = r.stdout.decode("ascii").strip()
        assert len(sha) == 40, sha
        if checkout:
            _git(self.path, "checkout", "-q", "--detach", sha)
        return sha

    def merge_no_ff(self, parent: str, other: str, msg: str) -> str:
        """班次的合法:在 parent 上 `--no-ff` 合进 other,返回合并提交。"""
        _git(self.path, "checkout", "-q", "--detach", parent)
        _git(self.path, "merge", "-q", "--no-ff", "--no-edit", "-m", msg, other)
        return _git(self.path, "rev-parse", "HEAD")

    def census(self, ref: str, *extra: str) -> tuple[int, str]:
        """[AE 提速] 进程内调仓里这一份普查门的 main();与 CLI 等价由
        test_in_process_and_cli_give_identical_readings 逐字核。"""
        return _inproc(["--repo", str(self.path), "--ref", ref, *extra])

    def census_cli(self, ref: str, *extra: str) -> tuple[int, str]:
        r = subprocess.run(
            [sys.executable, str(CENSUS), "--repo", str(self.path), "--ref", ref, *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
        return _red_mark_means_nonzero(r.returncode, r.stdout + r.stderr)


def _red_mark_means_nonzero(rc: int, out: str) -> tuple[int, str]:
    """[Review 09-28 · 门四 · 保险一格] 门的输出里只要出现 🔴,退出码就必须非 0。
    preflight 5-d 按退出码判;单跑门的人也只看退出码 —— 只打印不计红的分支会把人放过去。
    本仓每一格(进程内与 CLI 两种调法)都经过这里,所以这条不变式在全部合成场景上逐次核。"""
    assert not ("🔴" in out and rc == 0), "门打印了 🔴 却返回 0(只打印不计红):\n" + out
    return rc, out


_CENSUS_MOD = None


def _inproc(argv: list[str]) -> tuple[int, str]:
    """进程内跑普查门:按文件路径加载**仓里这一份**脚本(不走 sys.path),捕获 stdout/stderr。
    子进程版用 _env() 去掉了 GIT_DIR 一类变量 —— 这里同样临时拿掉,环境与 CLI 臂一致。"""
    global _CENSUS_MOD
    if _CENSUS_MOD is None:
        spec = importlib.util.spec_from_file_location("_census_under_test", CENSUS)
        _CENSUS_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CENSUS_MOD)
    saved = {k: os.environ.pop(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE") if k in os.environ}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = _CENSUS_MOD.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        os.environ.update(saved)
    return _red_mark_means_nonzero(rc, out.getvalue() + err.getvalue())


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Repo, str]:
    path = tmp_path_factory.mktemp("wo266_synth")
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    r = Repo(path)
    for rel, content in BASE_FILES.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    _git(path, "add", "-A", "--force")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/github/main", base)
    return r, base


def _oneshot_with(budget: str | None = "# ONESHOT_BUDGET_SECONDS=100",
                  a: str | None = None, b: str | None = None) -> str:
    return _oneshot(budget,
                    a if a is not None else _entry("tests/pkg_a", GUARD_A),
                    b if b is not None else _entry("tests/pkg_b", GUARD_B))


# ══════════════════════════════════════════════════════════════════ 对照
@pytest.mark.parametrize("scenario", ["green", "red"])
def test_in_process_and_cli_give_identical_readings(repo, scenario):
    """[AE 提速] 其余格都走进程内调用;这一格核「进程内 == CLI」:退出码与整段输出逐字相同(绿一条、红一条)。"""
    r, base = repo
    changes = ({"README.md": "cli parity\n"} if scenario == "green" else
               {"tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=999", _entry("tests/pkg_a", GUARD_A),
                                                   _entry("tests/pkg_b", GUARD_B))})
    h = r.commit(base, changes, f"cli parity {scenario}")
    rc_in, out_in = r.census(h)
    rc_cli, out_cli = r.census_cli(h)
    assert (rc_in, out_in.strip()) == (rc_cli, out_cli.strip()), f"进程内:\n{out_in}\nCLI:\n{out_cli}"
    assert rc_in == (0 if scenario == "green" else 1), out_in


def test_the_object_cache_does_not_leak_across_runs_on_a_moving_ref(repo):
    """[AE 提速] 普查门在一次运行内缓存 show/ls-tree;`HEAD` 这类会动的名字不许把上一次的读数带进下一次。
    同一进程里:HEAD 在绿的树上跑一次 ⇒ HEAD 挪到登记表改坏的树上再跑 ⇒ 必须红(缓存每次 main() 清空)。"""
    r, base = repo
    green = r.commit(base, {"README.md": "moving ref 1\n"}, "green", checkout=True)
    rc1, out1 = r.census("HEAD")
    assert rc1 == 0, out1
    r.commit(green, {"tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=999", _entry("tests/pkg_a", GUARD_A),
                                                        _entry("tests/pkg_b", GUARD_B))}, "red on HEAD", checkout=True)
    rc2, out2 = r.census("HEAD")
    assert rc2 == 1 and "999" in out2, out2


def test_untouched_tree_is_green_in_every_cell(repo):
    """对照臂:没动过登记表的树 ⇒ 全绿,且七格**各自**打出绿行(不是没打印)。"""
    r, base = repo
    h = r.commit(base, {"README.md": "unrelated\n"}, "unrelated change")
    rc, out = r.census(h)
    assert rc == 0, out
    for must_see in ("✅ 无未登记的新项", "✅ 随单登记 2 条,每条都写了「守什么」",
                     "✅ 时长预算 100s(未改", "✅ 冻结名单相对基准新增 0 项",
                     "✅ 运行者守恒:基准登记 3 项", "⚪ 退役登记 0 条",
                     "⚪ 删除引用者:本班没有删除或改名的文件",
                     "✅ 必跑集预算 300s(未改"):
        assert must_see in out, f"缺绿行:{must_see}\n{out}"
    assert "🔴" not in out, out


# ══════════════════════════════════════════════════════════════════ 既有格(WO 要求 4 第一条)
def test_a_new_red_package_left_unregistered_is_red(repo):
    r, base = repo
    h = r.commit(base, {"tests/pkg_new/test_new.py": "def test_red():\n    assert False\n"},
                 "new unregistered package")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "新增测试包**没登记运行者**:tests/pkg_new" in out, out


# ══════════════════════════════════════════════════════════════════ ① 随单登记格式
def test_the_old_two_column_format_is_red(repo):
    """🔴 牙证:存量五条就是这种两栏。只看「第二栏非空」的写法会让它们全部过关。"""
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(
        a="tests/pkg_a\tWO_255 · C:/AI-Test/某签字条.md(14 passed)")}, "old format")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "`tests/pkg_a` 是 2 栏" in out, out
    assert "`tests/pkg_b`" not in out, "没动的那条也红了 —— 尺子不分对象"


# 🔴 参数化一律给 ASCII id:G2b 红了只打 pytest 尾部几行,中文 id 会被转义成 \uXXXX,
#    现场读不出是哪一格红(我自己的注毒脚本就被它骗过一次,把「咬住」读成了「存活」)。
@pytest.mark.parametrize("line, needle", [
    (_entry("tests/pkg_a", "守点东西"), "不到 8 个字"),
    (_entry("tests/pkg_a", "WO_255 · 见签字条里的说明文字", "WO_255 · 签字条"), "栏位串了"),
    (_entry("tests/pkg_a", GUARD_A, "某签字条路径(无工单号)"), "「出处」没有 `WO_数字`"),
    ("tests/pkg_a\t" + GUARD_A + "\tWO_900\t多出来的一栏", "是 4 栏"),
    (_entry("tests/pkg_a", "", "WO_900 · 签字条"), "不到 8 个字"),
], ids=["guard-too-short", "guard-is-wo-tag", "origin-lacks-wo", "four-columns", "guard-empty"])
def test_each_guard_condition_alone_turns_it_red(repo, line, needle):
    """每个条件单独决定结果:其余条件都满足,只坏这一条 ⇒ 红,且红在这一条上。"""
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(a=line)}, "bad guard")
    rc, out = r.census(h)
    assert rc == 1, out
    assert needle in out, out


def test_a_guard_that_merely_mentions_a_work_order_is_fine(repo):
    """「栏位串了」的对照臂:守什么里**提到**工单号是正常写法,只有整栏长成出处形状才红。

    🔴 第一版规则是「以 WO_数字 开头就红」,preflight 臂 B1 当场误伤了一句正常的守什么。
    """
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(
        a=_entry("tests/pkg_a", "WO_247 清空登记表之后,公开快照门仍不认过渡豁免"))},
        "guard mentions a WO")
    rc, out = r.census(h)
    assert rc == 0, out


# ══════════════════════════════════════════════════════════════════ ② 预算行
@pytest.mark.parametrize("budget_line, needle", [
    (None, "没有预算行"),
    ("# ONESHOT_BUDGET_SECONDS = 100", "没有预算行"),        # 多了空格
    ("# ONESHOT_BUDGET_SECONDS=0100", "没有预算行"),         # 前导零(bash 会读成八进制)
    ("# ONESHOT_BUDGET_SECONDS=0", "没有预算行"),
    ("# ONESHOT_BUDGET_SECONDS=100 ", "没有预算行"),         # 行尾空格
    ("# ONESHOT_BUDGET_SECONDS=100\n# ONESHOT_BUDGET_SECONDS=100", "有 2 行预算"),
], ids=["missing", "spaces-around-eq", "leading-zero", "zero", "trailing-space", "duplicated"])
def test_the_budget_line_has_exactly_one_rigid_shape(repo, budget_line, needle):
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(budget=budget_line)},
                 "budget shape")
    rc, out = r.census(h)
    assert rc == 1, out
    assert needle in out, out


# ══════════════════════════════════════════════════════════════════ ③ 预算棘轮
def test_raising_the_budget_is_red(repo):
    """🔴 棘轮格的毒 = 走「合法出口」:把预算改大。只大 1 秒也必须红。"""
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(
        budget="# ONESHOT_BUDGET_SECONDS=101")}, "raise budget")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "时长预算被改大:100s → 101s" in out, out


def test_lowering_or_keeping_the_budget_is_green(repo):
    r, base = repo
    for val, note in (("99", "下调 100s → 99s"), ("100", "未改")):
        h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(
            budget=f"# ONESHOT_BUDGET_SECONDS={val}")}, f"budget {val}")
        rc, out = r.census(h)
        assert rc == 0, out
        assert note in out, out


def test_a_base_without_a_budget_line_is_grey_not_red(repo):
    """引入预算的那一班:基准上还没有预算行 ⇒ ⚪ 并说明,不红(否则引入它的班永远过不去)。"""
    r, base = repo
    old = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(budget=None)},
                   "pre-266 base")
    _git(r.path, "update-ref", "refs/remotes/github/main", old)
    try:
        h = r.commit(old, {"tests/ONESHOT_RUNS.txt": _oneshot_with()}, "introduce budget")
        rc, out = r.census(h)
        assert rc == 0, out
        assert "⚪ 预算棘轮无基准" in out and "本班引入预算 100s" in out, out
    finally:
        _git(r.path, "update-ref", "refs/remotes/github/main", base)


def test_the_base_is_the_merge_base_not_main_itself(repo):
    """主干在本包分叉之后又把预算降到 80:本包没动预算(100)⇒ 不许判成「改大」。"""
    r, base = repo
    main2 = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot_with(
        budget="# ONESHOT_BUDGET_SECONDS=80")}, "main lowers budget later")
    _git(r.path, "update-ref", "refs/remotes/github/main", main2)
    try:
        h = r.commit(base, {"README.md": "forked before main lowered it\n"}, "old fork")
        rc, out = r.census(h)
        assert rc == 0, out
        assert f"基准 {base[:12]}" in out, out
    finally:
        _git(r.path, "update-ref", "refs/remotes/github/main", base)


def test_an_unresolvable_base_is_rc3_not_green(repo):
    """基准取不到 ⇒ 需要基准的三格没跑成 ⇒ rc=3;不需要基准的格照常出读数。"""
    r, base = repo
    h = r.commit(base, {"README.md": "x\n"}, "x")
    rc, out = r.census(h, "--base", "refs/remotes/nope/main")
    assert rc == 3, out
    assert "没跑成·rc=3" in out and "解析不了" in out, out
    assert "✅ 随单登记 2 条" in out, "不需要基准的格被连坐成没读数了"


# ══════════════════════════════════════════════════════════════════ ④ 冻结名单只许减
def test_stuffing_a_new_package_into_the_frozen_list_is_red(repo):
    """🔴 WO_263 的洞:新包直接塞进冻结名单,**旧判据照样绿**(本格之前没有任何一格在验)。"""
    r, base = repo
    frozen = BASE_FILES["tests/UNASSIGNED_FROZEN.txt"] + "tests/pkg_new\n"
    h = r.commit(base, {"tests/pkg_new/test_new.py": "def test_x():\n    pass\n",
                        "tests/UNASSIGNED_FROZEN.txt": frozen}, "stuff frozen")
    rc, out = r.census(h)
    assert "✅ 无未登记的新项" in out, "旧判据这里本来就是绿的 —— 这正是要补的洞"
    assert rc == 1, out
    assert "冻结名单比基准多了 1 项:tests/pkg_new" in out, out


def test_frozen_list_that_should_shrink_but_did_not_is_red(repo):
    """[Review 09-28 · 门四] 冻结项已登记了运行者、却还留在冻结名单里 ⇒ 退出码 1。
    原来这里只打印一行 🔴、退出码 0(A 24f121aa6 就是这么漏过去的)。"""
    r, base = repo
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_a", GUARD_A), _entry("tests/pkg_b", GUARD_B),
        _entry("tests/test_d01.py", "合成仓随单文件 d01:红了说明 d01 守的那件事回归了"))},
        "register d01 but keep it in the frozen list")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "冻结名单该缩没缩" in out and "tests/test_d01.py" in out, out


def test_frozen_list_shrunk_together_with_the_registration_is_green(repo):
    """对照臂:同一次登记、同时把那一行从冻结名单删掉 ⇒ 绿(上一格的红来自「没删那一行」,不是登记本身)。"""
    r, base = repo
    frozen = BASE_FILES["tests/UNASSIGNED_FROZEN.txt"].replace("tests/test_d01.py\n", "")
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_a", GUARD_A), _entry("tests/pkg_b", GUARD_B),
        _entry("tests/test_d01.py", "合成仓随单文件 d01:红了说明 d01 守的那件事回归了")),
        "tests/UNASSIGNED_FROZEN.txt": frozen}, "register d01 and drop it from the frozen list")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "冻结名单该缩没缩" not in out, out


# ══════════════════════════════════════════════════════════════════ ⑤ 运行者守恒
def test_moving_an_oneshot_package_into_the_frozen_list_is_red(repo):
    r, base = repo
    frozen = BASE_FILES["tests/UNASSIGNED_FROZEN.txt"] + "tests/pkg_a\n"
    h = r.commit(base, {"tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_b", GUARD_B)),
        "tests/UNASSIGNED_FROZEN.txt": frozen}, "oneshot -> frozen")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "冻结名单比基准多了 1 项:tests/pkg_a" in out, out
    assert "运行者守恒:`tests/pkg_a`(基准登记于 ONESHOT)" in out, out


def test_deleting_an_oneshot_package_with_its_line_is_red(repo):
    """「红了就连包带登记行一起删」—— 既有判据看不见(没有陈账、没有新项)。"""
    r, base = repo
    h = r.commit(base, {"tests/pkg_a": None, "tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_b", GUARD_B))}, "delete pkg_a")
    rc, out = r.census(h)
    assert "✅ 无未登记的新项" in out, "既有判据在这里是绿的 —— 本格要补的就是它"
    assert rc == 1, out
    assert "运行者守恒:`tests/pkg_a`(基准登记于 ONESHOT)" in out, out


def test_promoting_to_must_run_is_a_legal_exit(repo):
    r, base = repo
    h = r.commit(base, {
        "tests/MUST_RUN.txt": (f"# 合成必跑集\n# MUST_RUN_BUDGET_SECONDS=300\n\n"
                               f"tests/pkg_m\t{GUARD_M}\ntests/pkg_a\t{GUARD_A}\n"),
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                           _entry("tests/pkg_b", GUARD_B))}, "promote a")
    rc, out = r.census(h)
    assert rc == 0, out


def test_positive_retirement_with_a_running_successor_is_a_legal_exit(repo):
    r, base = repo
    h = r.commit(base, {
        "tests/pkg_a": None,
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                           _entry("tests/pkg_b", GUARD_B)),
        "tests/RETIRED_TESTS.txt": "# 合成退役表\n\ntests/pkg_a\ttests/pkg_m/test_m.py\t"
                                   "A 守的事已并入必跑包 M 的 test_m\n"}, "retire a")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "✅ 退役登记 1 条,接替者都指得出" in out, out


# ══════════════════════════════════════════════════════════════════ ⑥ 退役接替
@pytest.mark.parametrize("retired_line, needle", [
    ("tests/pkg_a\tA 已经不需要了", "不是 3 栏"),
    ("tests/pkg_a\ttests/pkg_zz_nope\t接替者根本不存在", "在待发树里不存在"),
    ("tests/pkg_a\ttests/pkg_a\t接替者写成它自己", "写的是它自己"),
    ("tests/pkg_a\ttests/test_d05.py\t接替者自己在冻结名单里", "自己没有运行者"),
], ids=["two-columns", "successor-missing", "successor-is-self", "successor-unrun"])
def test_retirement_without_a_real_successor_is_red(repo, retired_line, needle):
    r, base = repo
    h = r.commit(base, {
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                           _entry("tests/pkg_b", GUARD_B)),
        "tests/RETIRED_TESTS.txt": f"# 合成退役表\n\n{retired_line}\n"}, "bad retire")
    rc, out = r.census(h)
    assert rc == 1, out
    assert needle in out, out


# ── ⑥ · WO_297:接替者写成 `文件::格`(与第 1 栏对称)────────────────────────────
CELLS_FILE = ("class TestGroup:\n    def test_in_class(self):\n        pass\n\n\n"
              "def test_top(x=None):\n    pass\n")


def _retire_to_node(r, base, succ: str) -> str:
    return r.commit(base, {
        "tests/pkg_m/test_cells.py": CELLS_FILE,
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_b", GUARD_B)),
        # 第 1 栏退整个 pkg_a(随单登记里拿掉了它),接替者写成格级 —— 只让接替者这一个条件决定红绿
        "tests/RETIRED_TESTS.txt": f"# 合成退役表\n\ntests/pkg_a\t{succ}\t合成:格级接替\n",
    }, f"retire to {succ}")


@pytest.mark.parametrize("succ", [
    "tests/pkg_m/test_cells.py::test_top",
    "tests/pkg_m/test_cells.py::TestGroup::test_in_class",
    "tests/pkg_m/test_cells.py::test_top[param-1]",
    "tests/pkg_m/test_cells.py::TestGroup",
], ids=["top-level-def", "class-method", "parametrized-suffix", "class-itself"])
def test_a_node_level_successor_that_exists_and_runs_is_green(repo, succ):
    r, base = repo
    rc, out = r.census(_retire_to_node(r, base, succ))
    assert rc == 0, out
    assert "✅ 退役登记 1 条,接替者都指得出" in out, out


@pytest.mark.parametrize("succ, needle", [
    ("tests/pkg_m/test_cells.py::test_nope", "接替者格 `test_nope` 在 `tests/pkg_m/test_cells.py` 里不存在"),
    ("tests/pkg_m/test_cells.py::TestGroup::test_nope", "接替者格 `TestGroup::test_nope` 在"),
    ("tests/pkg_m/test_gone.py::test_top", "在待发树里不存在(文件本身就不在)"),
    ("tests/test_d05.py::test_ok", "自己没有运行者"),
], ids=["cell-missing", "method-missing", "file-missing", "cell-exists-but-file-unrun"])
def test_a_node_level_successor_that_is_wrong_is_red(repo, succ, needle):
    r, base = repo
    rc, out = r.census(_retire_to_node(r, base, succ))
    assert rc == 1, out
    assert needle in out, out
    assert out.count("🔴") == 2, out      # 只有这一条 + 「WO_266 格共 1 条」汇总,没有别的红


# ── ⑥ · WO_289:tests/ 外的接替者按名核 build 链 ────────────────────────────────
PKG_CHAIN = ('{\n  "scripts": {\n    "build": "tsc -b && node scripts/guard_x.mjs'
             ' && node scripts/other_x.mjs.off && npm run arms_x",\n'
             '    "arms_x": "node scripts/armed_x.mjs"\n  }\n}\n')
FRONT_FILES = {
    "frontend/package.json": PKG_CHAIN,
    "frontend/scripts/guard_x.mjs": "// 在链上(第 2 步)\n",
    "frontend/scripts/other_x.mjs": "// 链上只有它的 .off 变体 —— 子串相同、名字不同\n",
    "frontend/scripts/armed_x.mjs": "// 只经 npm run 间接调用\n",
    "scripts/guard_x.mjs": "// 仓根同名文件:链上那个是 frontend/ 下的\n",
}
WARN_OFF_CHAIN = "也不在 build 链上"


def _retire_to(r, base, succ: str, extra: dict[str, str | None] | None = None) -> str:
    return r.commit(base, {
        **FRONT_FILES, **(extra or {}),
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                           _entry("tests/pkg_b", GUARD_B)),
        "tests/RETIRED_TESTS.txt": f"# 合成退役表\n\ntests/pkg_a\t{succ}\t合成:接替者在 tests/ 外\n",
    }, f"retire to {succ}")


def test_a_successor_named_on_the_build_chain_is_counted_as_run(repo):
    r, base = repo
    rc, out = r.census(_retire_to(r, base, "frontend/scripts/guard_x.mjs"))
    assert rc == 0, out
    assert "接替者 `frontend/scripts/guard_x.mjs` 由 build 链第 2 步跑" in out, out
    assert WARN_OFF_CHAIN not in out, out
    assert "✅ 退役登记 1 条,接替者都指得出" in out, out


@pytest.mark.parametrize("succ", [
    "frontend/scripts/other_x.mjs",    # 链上只有 other_x.mjs.off:子串命中、名字不中
    "frontend/scripts/armed_x.mjs",    # 只在 npm run 间接那一层:本格不展开,按不在链上
    "scripts/guard_x.mjs",             # 仓根同名:链上路径相对 frontend/,不是它
], ids=["substring-only", "indirect-npm-run", "same-name-outside-frontend"])
def test_a_successor_not_named_on_the_build_chain_still_warns(repo, succ):
    r, base = repo
    rc, out = r.census(_retire_to(r, base, succ))
    assert rc == 0, out                      # ⚠️ 只黄不红,三态不变
    assert f"接替者 `{succ}` 在 tests/ 之外、{WARN_OFF_CHAIN}" in out, out
    assert "由 build 链第" not in out, out


@pytest.mark.parametrize("pkg", [None, "{ not json\n", '{"scripts": {"dev": "vite"}}\n'],
                         ids=["package-json-missing", "not-json", "no-build-script"])
def test_an_unreadable_build_chain_is_rc3_not_a_pass(repo, pkg):
    r, base = repo
    rc, out = r.census(_retire_to(r, base, "frontend/scripts/guard_x.mjs",
                                  {"frontend/package.json": pkg}))
    assert rc == 3, out
    assert "要查 build 链" in out, out


def test_a_tests_successor_never_reads_the_build_chain(repo):
    """对照臂:接替者全在 tests/ 下时 build 链坏了也不关 ⑥ 的事(原判据原样)。"""
    r, base = repo
    h = r.commit(base, {
        "frontend/package.json": "{ not json\n",
        "tests/ONESHOT_RUNS.txt": _oneshot("# ONESHOT_BUDGET_SECONDS=100",
                                           _entry("tests/pkg_b", GUARD_B)),
        "tests/RETIRED_TESTS.txt": "# 合成退役表\n\ntests/pkg_a\ttests/pkg_m/test_m.py\t"
                                   "合成:由必跑包接替\n"}, "retire to tests successor")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "✅ 退役登记 1 条,接替者都指得出" in out, out
    assert "build 链" not in out, out


# ══════════════════════════════════════════════════════════════════ ⑦ 删除引用者
def test_deleting_a_file_leaves_untouched_referencers_red(repo):
    """🔴 09-08 那一笔的形状:删了页面、一个引用者都没动 ⇒ 红,且逐个点名(含 tests/)。"""
    r, base = repo
    h = r.commit(base, {"app/DeletedWidget2.tsx": None}, "delete widget only")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "删了 `app/DeletedWidget2.tsx`,还有 2 个文件提到 `DeletedWidget2`" in out, out
    assert "app/Consumer.tsx" in out and "tests/test_d03.py" in out, out
    assert "docs/notes.md" not in out, "docs 不该进引用者分母"


def test_deleting_a_file_and_touching_every_referencer_is_green(repo):
    r, base = repo
    h = r.commit(base, {"app/DeletedWidget2.tsx": None,
                        "app/Consumer.tsx": "export const X = 1\n",
                        "tests/test_d03.py": "def test_ok():\n    pass\n"}, "delete + fix refs")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "✅ 删除引用者:本班删/改名 1 个文件" in out, out


def test_a_touched_referencer_that_still_names_the_file_is_let_through(repo):
    """本班动过、却仍提到被删文件的引用者 —— 典型是**同班写的肯定式退役锁** ⇒ 放行。

    🔴 没有这一格,「本班动过 = 看过」那条放行条件**没有任何输入够得着**:
       上一格里改过的引用者恰好都不再提那个名字。注毒实测:把放行条件整条删掉,
       全包照样绿(P21 存活)—— 不是锁没牙,是没有夹具走到那条分支。
    """
    r, base = repo
    lock = ("from pathlib import Path\n\n\n"
            "def test_widget_is_retired():\n"
            "    assert not Path('app/DeletedWidget2.tsx').exists()\n")
    h = r.commit(base, {"app/DeletedWidget2.tsx": None,
                        "app/Consumer.tsx": "export const X = 1\n",
                        "tests/test_d03.py": lock}, "delete + retirement lock naming it")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "✅ 删除引用者:本班删/改名 1 个文件" in out, out


def test_touching_a_referencer_elsewhere_while_its_reference_line_stays_is_red(repo):
    """🔴 Review 09-23 裁定 ②:「本班动过」要落在**引用行本身**,文件别处改一行不算看过。

    两个引用者都被本班改了(各加一行无关内容),但读被删文件的那一行原样留着 ⇒ 红。
    """
    r, base = repo
    h = r.commit(base, {
        "app/DeletedWidget2.tsx": None,
        "app/Consumer.tsx": "import { X } from './DeletedWidget2'\nexport const unrelated = 2\n",
        "tests/test_d03.py": ('P = "app/DeletedWidget2.tsx"\n\n\n'
                              "def test_ok():\n    pass\n\n\ndef test_unrelated():\n    pass\n")},
        "touch referencers elsewhere only")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "`app/Consumer.tsx` 本班动过,但提到 `DeletedWidget2` 的 1 行原样没动" in out, out
    assert "`tests/test_d03.py` 本班动过,但提到 `DeletedWidget2` 的 1 行原样没动" in out, out


def test_a_rename_that_changes_the_name_counts_as_a_removal(repo):
    r, base = repo
    h = r.commit(base, {"app/OldNamedThing.tsx": None,
                        "app/NewNamedThing.tsx": "export const Y = 'old named thing'\n"},
                 "rename")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "删了 `app/OldNamedThing.tsx`" in out and "app/UsesOld.tsx" in out, out


def test_generic_names_and_same_name_moves_are_named_not_passed_as_clean(repo):
    """太泛的名字不搜、只搬目录的不判 —— 都要**点名**,且不许打 ✅(没搜 ≠ 干净)。"""
    r, base = repo
    h = r.commit(base, {"app/index.ts": None,
                        "app/SameNameMove1.tsx": None,
                        "lib/SameNameMove1.tsx": "export const Z = 'same name move'\n"},
                 "generic delete + same-name move")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "一个键都没搜" in out and "✅ 删除引用者" not in out, out
    assert "没搜(文件名太泛" in out and "app/index.ts" in out, out
    assert "没判(只搬目录不改名" in out and "app/SameNameMove1.tsx→lib/SameNameMove1.tsx" in out, out


# ══════════════════════════════════════════════════════════════════ ⑧ 必跑集预算(裁定 ①)
def _must_run(budget_line: str | None = "# MUST_RUN_BUDGET_SECONDS=300", extra: str = "") -> str:
    head = "# 合成必跑集\n" + (budget_line + "\n" if budget_line is not None else "") + extra
    return head + f"\ntests/pkg_m\t{GUARD_M}\n"


@pytest.mark.parametrize("budget_line, needle", [
    (None, "tests/MUST_RUN.txt 没有预算行"),
    ("# MUST_RUN_BUDGET_SECONDS = 300", "tests/MUST_RUN.txt 没有预算行"),
    ("# MUST_RUN_BUDGET_SECONDS=300\n# MUST_RUN_BUDGET_SECONDS=300", "有 2 行预算"),
], ids=["missing", "spaces-around-eq", "duplicated"])
def test_the_must_run_budget_line_has_exactly_one_rigid_shape(repo, budget_line, needle):
    r, base = repo
    h = r.commit(base, {"tests/MUST_RUN.txt": _must_run(budget_line)}, "must-run budget shape")
    rc, out = r.census(h)
    assert rc == 1, out
    assert needle in out, out


def test_raising_the_must_run_budget_in_a_pure_single_commit_is_named_not_red(repo):
    """合法出口:单独一笔、只改预算这一行 ⇒ 不红,但打 ⚠️ 点名那一笔,要求签字条点名。"""
    r, base = repo
    h = r.commit(base, {"tests/MUST_RUN.txt": _must_run("# MUST_RUN_BUDGET_SECONDS=400")},
                 "raise must-run budget alone")
    rc, out = r.census(h)
    assert rc == 0, out
    assert f"必跑集预算上调 300s → 400s:单独一笔 {h[:12]}" in out, out
    assert "签字条必须点名" in out, out


def test_raising_the_must_run_budget_alongside_other_changes_is_red(repo):
    """同一笔里顺手调大预算(还改了别的)⇒ 红:调预算只能单独一笔。"""
    r, base = repo
    h = r.commit(base, {"tests/MUST_RUN.txt": _must_run("# MUST_RUN_BUDGET_SECONDS=400"),
                        "README.md": "same commit also changes this\n"}, "raise + other")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "没有走「单独一笔、只改这一行」" in out and h[:12] in out, out


def test_a_pure_raise_merged_into_a_train_with_no_ff_is_still_recognised(repo):
    """班次用 --no-ff 合 WO 分支:合并提交的差分带着整条分支 —— 不许拿它判「不纯」。"""
    r, base = repo
    pure = r.commit(base, {"tests/MUST_RUN.txt": _must_run("# MUST_RUN_BUDGET_SECONDS=400")},
                    "pure raise on a WO branch")
    other = r.commit(pure, {"README.md": "another commit on the same WO branch\n"}, "other")
    m = r.merge_no_ff(base, other, "train merges the WO branch")
    rc, out = r.census(m)
    assert rc == 0, out
    assert f"单独一笔 {pure[:12]}" in out, out


def test_a_pure_raise_is_recognised_even_when_the_merge_itself_differs_from_both_parents(repo):
    """班次那一侧也改过 MUST_RUN.txt(文件末尾加一行),再 --no-ff 合进「纯上调 + 另一笔改 README」的 WO 分支。

    这时合并提交在 MUST_RUN.txt 上与两个父提交都不同 ⇒ 按路径列提交时它**不会**被默认简化掉;
    拿它相对第一亲本的差分判纯度,会带上 README ⇒ 误判「不纯」而红。
    🔴 上一格(班次侧没改过这个文件)够不着这条:git 默认就把「与某个父提交在该文件上相同」的
       合并提交略过了 —— 注毒「去掉 --no-merges」在那一格照样绿(P29 第一轮存活)。
    """
    r, base = repo
    pure = r.commit(base, {"tests/MUST_RUN.txt": _must_run("# MUST_RUN_BUDGET_SECONDS=400")},
                    "pure raise on a WO branch")
    other = r.commit(pure, {"README.md": "another commit on the same WO branch\n"}, "other")
    train = r.commit(base, {"tests/MUST_RUN.txt": _must_run() + "# 班次侧在文件末尾加了一行注释\n"},
                     "train side edits MUST_RUN.txt elsewhere")
    m = r.merge_no_ff(train, other, "train merges the WO branch")
    rc, out = r.census(m)
    assert rc == 0, out
    assert f"单独一笔 {pure[:12]}" in out, out


def test_a_raise_hidden_inside_a_merge_commit_is_red(repo):
    """合并提交里顺手把预算调大(evil merge):范围里找不到做这次上调的单独一笔 ⇒ 红。

    🔴 没有这一格,「找不到单独一笔」那条红**没有任何夹具够得着**(注毒 P30 会存活)。
    """
    r, base = repo
    other = r.commit(base, {"README.md": "a branch that never touches MUST_RUN.txt\n"}, "branch")
    _git(r.path, "checkout", "-q", "--detach", base)
    _git(r.path, "merge", "-q", "--no-ff", "--no-commit", other)
    (r.path / "tests" / "MUST_RUN.txt").write_bytes(
        _must_run("# MUST_RUN_BUDGET_SECONDS=400").encode("utf-8"))
    _git(r.path, "add", "tests/MUST_RUN.txt")
    _git(r.path, "commit", "-q", "-m", "evil merge raises the budget")
    m = _git(r.path, "rev-parse", "HEAD")
    rc, out = r.census(m)
    assert rc == 1, out
    assert "找不到做这次上调的单独一笔" in out, out


def test_lowering_the_must_run_budget_is_green(repo):
    r, base = repo
    h = r.commit(base, {"tests/MUST_RUN.txt": _must_run("# MUST_RUN_BUDGET_SECONDS=250"),
                        "README.md": "lowering may ride along with anything\n"}, "lower")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "✅ 必跑集预算 250s(下调 300s → 250s" in out, out


def test_a_base_without_a_must_run_budget_is_grey_not_red(repo):
    r, base = repo
    old = r.commit(base, {"tests/MUST_RUN.txt": _must_run(None)}, "pre-266 must-run")
    _git(r.path, "update-ref", "refs/remotes/github/main", old)
    try:
        h = r.commit(old, {"tests/MUST_RUN.txt": _must_run()}, "introduce must-run budget")
        rc, out = r.census(h)
        assert rc == 0, out
        assert "⚪ 必跑集预算无基准" in out and "本班引入 300s" in out, out
    finally:
        _git(r.path, "update-ref", "refs/remotes/github/main", base)


# ══════════════════════════════════════════════════════════════════ WO_296 部分覆盖目录逐文件落位
# 合成一个「部分覆盖」目录:workflow 只选 tests/pkg_p/test_cov_*.py,pkg_p 里另有一个 test_other.py。
PARTIAL = {
    ".github/workflows/p.yml": ("name: p\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
                                "      - name: run\n        run: |\n          pytest tests/pkg_p/test_cov_*.py -q\n"),
    "tests/pkg_p/test_cov_a.py": "def test_a():\n    pass\n",
    "tests/pkg_p/test_other.py": "def test_o():\n    pass\n",
}


def test_an_unregistered_file_in_a_partial_dir_is_still_named(repo):
    r, base = repo
    rc, out = r.census(r.commit(base, dict(PARTIAL), "partial dir"))
    assert rc == 0, out
    assert "⚠️  tests/pkg_p 只被**部分**覆盖" in out and "**1 个四类都不在**" in out, out
    assert "        · tests/pkg_p/test_other.py" in out, out


def test_a_file_registered_in_oneshot_inside_a_partial_dir_is_placed_not_stale(repo):
    r, base = repo
    rc, out = r.census(r.commit(base, {**PARTIAL, "tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_a", GUARD_A), _entry("tests/pkg_b", GUARD_B),
        _entry("tests/pkg_p/test_other.py", "合成:部分覆盖目录里那个不在选择器中的文件"))}, "place it"), )
    assert rc == 0, out
    assert "✅ tests/pkg_p 只被**部分**覆盖" in out and "没有落空的" in out, out
    assert "✓ tests/pkg_p/test_other.py → ONESHOT" in out, out
    assert "陈账" not in out, out


@pytest.mark.parametrize("path", ["tests/pkg_a/test_a.py", "tests/pkg_p/test_cov_a.py"],
                         ids=["not-in-a-partial-dir", "already-covered-by-workflow"])
def test_file_level_registration_elsewhere_is_still_stale(repo, path):
    """文件级登记只认「部分覆盖目录里、不在选择器中」的文件;别处的子路径照旧按陈账红。"""
    r, base = repo
    rc, out = r.census(r.commit(base, {**PARTIAL, "tests/ONESHOT_RUNS.txt": _oneshot(
        "# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_a", GUARD_A), _entry("tests/pkg_b", GUARD_B),
        _entry(path, "合成:不该被接受的文件级登记"))}, "stale file entry"))
    assert rc == 1, out
    assert f"ONESHOT 登记了 `{path}`,但 tests/ 顶层没有这一项(陈账)" in out, out


# ══════════════════════════════════════════════════════════════════ 真仓对照
def test_the_real_oneshot_registry_has_guards_and_exactly_one_budget_line():
    """真登记表(按 HEAD 读,不受工作树里跑测试留下的杂物影响)自己的形状:三栏、恰好一行预算。

    🔴 第一版在这里跑**整份**普查器并断言 rc==0。preflight 臂 B6(新包故意红且不登记)
       实测:5-d 因「新包没登记」红 —— 对;**G2b 的 ONESHOT 段也跟着红一格**,红的就是这一格。
       同一个原因在两道门各报一次,而 ONESHOT 的红行会把人引去看本包的「守什么」,读偏。
       ⇒ 这里只验**只有它能验、且不依赖基准**的那两件:真登记表的三栏格式与预算行形状。
       普查门的其余判据每班由 preflight 5-d 真跑,不在这里复述。
    """
    text = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:tests/ONESHOT_RUNS.txt"],
                          capture_output=True, env=_env()).stdout.decode("utf-8")
    assert text.strip(), "HEAD 上读不到 tests/ONESHOT_RUNS.txt"
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys,json;"
         f"s=importlib.util.spec_from_file_location('c',{str(CENSUS)!r});"
         "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
         "t=sys.stdin.read();"
         "print(json.dumps({'errs':m._oneshot_entry_errors(t),'bud':m._budget_values(t),"
         "'n':len(m._entry_lines(t))}))"],
        input=text, capture_output=True, text=True, encoding="utf-8", env=_env())
    assert probe.returncode == 0, probe.stderr
    got = __import__("json").loads(probe.stdout.strip().splitlines()[-1])
    assert got["n"] > 0, "真登记表一条都没有 —— 读错文件了?"
    assert got["errs"] == [], got["errs"]
    assert len(got["bud"]) == 1, f"真登记表的预算行应恰好一行,读到 {got['bud']}"


def test_the_real_must_run_registry_has_exactly_one_budget_line():
    """真必跑集登记表(按 HEAD 读)恰好一行预算 —— 同上,只验它自己的形状。"""
    text = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:tests/MUST_RUN.txt"],
                          capture_output=True, env=_env()).stdout.decode("utf-8")
    assert text.strip(), "HEAD 上读不到 tests/MUST_RUN.txt"
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys,json;"
         f"s=importlib.util.spec_from_file_location('c',{str(CENSUS)!r});"
         "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
         "t=sys.stdin.read();"
         "print(json.dumps(m._budget_values(t, m.MUST_RUN_BUDGET_LINE_RE)))"],
        input=text, capture_output=True, text=True, encoding="utf-8", env=_env())
    assert probe.returncode == 0, probe.stderr
    got = __import__("json").loads(probe.stdout.strip().splitlines()[-1])
    assert len(got) == 1, f"真必跑集的预算行应恰好一行,读到 {got}"


# ══════════════════════════════════════════════════════════════════ ⑦ 注释行白名单(Review 09-28 · E3a)
# 保护文件里的 `#` 注释行、.sql 里的 `--` 注释行提到被删文件时,可以登记进白名单放行;非注释引用照样红。
# 这几格各自造一个基准 A(加被删文件 + 引用者),再在 A 上删文件,用 `--base A` 让引用者成为「本班没动」的。
ALLOW = "tests/REFERENCER_COMMENT_ALLOW.txt"
SQL_COMMENT = "-- 先跑 GizmoWidget9 的导入脚本"
PY_COMMENT = "# 历史说明:GizmoWidget9 里的旧扣费入口"


def _allow(*rows: tuple[str, ...]) -> str:
    """三元组 = (文件, 行原文, 理由),类型默认 comment;四元组 = (文件, 类型, 行原文, 理由)。"""
    out = []
    for r in rows:
        f_, kind, line, why = (r[0], "comment", r[1], r[2]) if len(r) == 3 else r
        out.append(f"{f_}\t{kind}\t{line}\t{why}\n")
    return "# 合成白名单(四栏:文件 / 类型 / 行原文 / 理由)\n" + "".join(out)


def _gizmo(r, base, extra: dict[str, str], allow: str | None, b_extra: dict[str, str] | None = None):
    a = r.commit(base, {"app/GizmoWidget9.tsx": "export const G = 1\n", **extra}, "base A with referencers")
    changes: dict[str, str | None] = {"app/GizmoWidget9.tsx": None, **(b_extra or {})}
    if allow is not None:
        changes[ALLOW] = allow
    b = r.commit(a, changes, "delete GizmoWidget9")
    return r.census(b, "--base", a)


_SQL = {"scripts/mig_gizmo.sql": f"{SQL_COMMENT}\nCREATE TABLE gz (x int);\n"}
_PROT = {"db/wallet_db.py": f"{PY_COMMENT}\nX = 1\n"}


def test_allowlisted_comment_lines_in_sql_and_a_protected_file_are_let_through(repo):
    r, base = repo
    rc, out = _gizmo(r, base, {**_SQL, **_PROT}, _allow(
        ("scripts/mig_gizmo.sql", SQL_COMMENT, "迁移 sql 改了会被 preflight 第 2 关判红"),
        ("db/wallet_db.py", PY_COMMENT, "保护文件不为改一行注释去动")))
    assert rc == 0, out
    assert "注释行白名单放行 2 处" in out, out


def test_the_same_comment_lines_without_an_allowlist_are_red(repo):
    # 对照臂:同一形状不登记 ⇒ 照旧红(白名单不是默认放行)
    r, base = repo
    rc, out = _gizmo(r, base, {**_SQL, **_PROT}, None)
    assert rc == 1, out
    assert "还有 2 个文件提到 `GizmoWidget9`" in out, out


def test_an_allowlist_entry_on_a_non_comment_line_is_red(repo):
    r, base = repo
    code = "SELECT 1 FROM gz; -- GizmoWidget9"
    rc, out = _gizmo(r, base, {"scripts/mig_gizmo.sql": code + "\n"},
                     _allow(("scripts/mig_gizmo.sql", code, "想把一行代码塞进注释白名单")))
    assert rc == 1, out
    assert "不是纯注释" in out and "还有 1 个文件提到 `GizmoWidget9`" in out, out


def test_an_allowlist_entry_outside_protected_files_and_sql_is_red(repo):
    r, base = repo
    rc, out = _gizmo(r, base, {"services/other_mod.py": f"{PY_COMMENT}\n"},
                     _allow(("services/other_mod.py", PY_COMMENT, "普通 .py 的注释不该进白名单")))
    assert rc == 1, out
    assert "不是保护文件也不是 .sql" in out, out


def test_a_stale_allowlist_entry_is_red(repo):
    r, base = repo
    rc, out = _gizmo(r, base, _SQL, _allow(
        ("scripts/mig_gizmo.sql", SQL_COMMENT, "迁移 sql 改了会被 preflight 第 2 关判红"),
        ("scripts/mig_gizmo.sql", "-- 这行文件里根本没有", "陈条目:原行早就改掉了")))
    assert rc == 1, out
    assert "陈条目" in out, out


def test_a_code_reference_next_to_an_allowlisted_comment_is_still_red(repo):
    r, base = repo
    rc, out = _gizmo(r, base, {"db/wallet_db.py": f"{PY_COMMENT}\nNAME = 'GizmoWidget9'\n"},
                     _allow(("db/wallet_db.py", PY_COMMENT, "保护文件不为改一行注释去动")))
    assert rc == 1, out
    assert "还有 1 个文件提到 `GizmoWidget9`" in out and "db/wallet_db.py" in out, out


# ── [Review 09-28 二次] 本班**新冒出来的**引用:原先只核基准里原有的引用行,这三种都看不见 ──
_SQL_ALLOW = ("scripts/mig_gizmo.sql", SQL_COMMENT, "迁移 sql 改了会被 preflight 第 2 关判红")
_PROT_ALLOW = ("db/wallet_db.py", PY_COMMENT, "保护文件不为改一行注释去动")


def test_a_new_code_line_in_an_allowlisted_sql_file_is_red(repo):
    # 镜像 Review 毒 ②:白名单里那个 .sql 末尾加一行非注释的 SELECT
    r, base = repo
    rc, out = _gizmo(r, base, _SQL, _allow(_SQL_ALLOW),
                     b_extra={"scripts/mig_gizmo.sql": f"{SQL_COMMENT}\nCREATE TABLE gz (x int);\nSELECT 'GizmoWidget9';\n"})
    assert rc == 1, out
    assert "`scripts/mig_gizmo.sql` 仍有 1 处提到它且不是白名单注释行" in out, out


def test_a_new_code_line_added_to_a_protected_file_is_red(repo):
    # 镜像 Review 毒 ③:保护文件末尾加一行代码引用被删文件
    r, base = repo
    rc, out = _gizmo(r, base, _PROT, _allow(_PROT_ALLOW),
                     b_extra={"db/wallet_db.py": f"{PY_COMMENT}\nX = 1\n_Y = 'GizmoWidget9'\n"})
    assert rc == 1, out
    assert "`db/wallet_db.py` 仍有 1 处提到它且不是白名单注释行" in out, out


def test_a_brand_new_non_test_file_naming_the_deleted_file_is_red(repo):
    r, base = repo
    rc, out = _gizmo(r, base, _SQL, _allow(_SQL_ALLOW),
                     b_extra={"services/new_mod.py": "LEGACY = 'GizmoWidget9'\n"})
    assert rc == 1, out
    assert "`services/new_mod.py` 仍有 1 处提到它" in out, out


def test_touching_an_allowlisted_file_without_a_new_reference_stays_green(repo):
    # 对照臂:同一个白名单 .sql 本班改了(加一行无关内容),白名单那行本身不红
    r, base = repo
    rc, out = _gizmo(r, base, _SQL, _allow(_SQL_ALLOW),
                     b_extra={"scripts/mig_gizmo.sql": f"{SQL_COMMENT}\nCREATE TABLE gz (x int);\n-- 无关的一行\n"})
    assert rc == 0, out
    assert "注释行白名单放行 1 处" in out, out


# ── [Review 09-28 · B1b-2b] 白名单第二类 e2_token_spec:只收 E2 社媒模块状态表之内、路径已登 RETIRED_PATHS 的行 ──
E2_FILE = "services/xiaobang_command_contract.py"
E2_TEST = "tests/oss_e2_dead_guard_2026_09_22/test_social_module_axis_is_live_or_retired.py"
E2_ROW1 = '("app.GizmoWidget9", "app/GizmoWidget9.tsx"),'
E2_ROW2 = '("app/GizmoWidget9", "app/GizmoWidget9.tsx"),'


def _e2_contract(extra_outside: str = "") -> str:
    return ("_SOCIAL_MODULE_TOKEN_SPECS: tuple[tuple[str, str], ...] = (\n"
            f"    {E2_ROW1}\n    {E2_ROW2}\n)\n" + extra_outside)


def _e2_test(retired: tuple[str, ...]) -> str:
    return f"RETIRED_PATHS: tuple[str, ...] = {retired!r}\n\n\ndef test_x():\n    pass\n"


_E2_ALLOW = (
    (E2_FILE, "e2_token_spec", E2_ROW1, "拒绝名单保留 token,路径已登退役"),
    (E2_FILE, "e2_token_spec", E2_ROW2, "拒绝名单保留 token,路径已登退役"),
)


def _e2_case(r, base, *, retired_in_b: tuple[str, ...], outside: str = ""):
    """同真实班次的形状:A = 删文件前(合同表 + E2 格已登记运行者、RETIRED_PATHS 为空);
    B = 删文件 + 同笔改 E2 格的 RETIRED_PATHS + 白名单 e2 两行。"""
    reg = _oneshot("# ONESHOT_BUDGET_SECONDS=100", _entry("tests/pkg_a", GUARD_A), _entry("tests/pkg_b", GUARD_B),
                   _entry("tests/oss_e2_dead_guard_2026_09_22", "合成 E2 状态轴接替格:红了说明社媒模块状态表失去接替把守"))
    return _gizmo(r, base, {E2_FILE: _e2_contract(outside), E2_TEST: _e2_test(()), "tests/ONESHOT_RUNS.txt": reg},
                  _allow(*_E2_ALLOW), b_extra={E2_TEST: _e2_test(retired_in_b)})


def test_e2_token_spec_rows_inside_the_table_are_let_through(repo):
    # 对照臂:表内合规两行(形状对、路径已登 RETIRED_PATHS)⇒ 绿
    r, base = repo
    rc, out = _e2_case(r, base, retired_in_b=("app/GizmoWidget9.tsx",))
    assert rc == 0, out
    assert f"{E2_FILE}(`GizmoWidget9`)" in out, out


def test_e2_token_spec_path_not_registered_as_retired_is_red(repo):
    r, base = repo
    rc, out = _e2_case(r, base, retired_in_b=())
    assert rc == 1, out
    assert "没登进 E2 的 RETIRED_PATHS" in out, out


def test_e2_token_spec_same_shape_row_outside_the_table_is_red(repo):
    # 同文件表外出现同一行(同形状)⇒ 条目本身判红,且那一处引用不被放行
    r, base = repo
    rc, out = _e2_case(r, base, retired_in_b=("app/GizmoWidget9.tsx",),
                       outside=f"\nLEAK = [\n    {E2_ROW1}\n]\n")
    assert rc == 1, out
    assert "表外也出现了" in out, out


# ══════════════════════════════════════════════════════════ ⑦ 撞名改判(Review 09-28 · B2 前置)
# 形状:E0 上提时**新建同名文件、旧件留在包里**(git 认不出 rename)。删旧件时,按文件名 `advisor_llm` 搜会把
# **正确引用新件**的在役文件也判成残留 ⇒ 撞名的键改按被删文件的完整路径(点分 + 斜杠)判,整包没了再加包路径。
# 合成包用短名 `tools/oldx`(无 __init__):不让「__init__ 按目录名取键」替新规则把红咬下来 —— 牙只能来自新规则。
LIFT_FILES = {
    "tools/oldx/advisor_llm.py": "def X():\n    return 1\n",
    "services/llm/__init__.py": "",
    "services/llm/advisor_llm.py": "def X():\n    return 2\n",
    "app/uses_new.py": "from services.llm.advisor_llm import X\n",
}


@pytest.fixture(scope="module")
def lift_repo(tmp_path_factory) -> tuple[Repo, str]:
    path = tmp_path_factory.mktemp("wo266_lift")
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    r = Repo(path)
    for rel, content in {**BASE_FILES, **LIFT_FILES}.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content.encode("utf-8"))
    _git(path, "add", "-A", "--force")
    _git(path, "commit", "-q", "-m", "base with old pkg + lifted same-stem module")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/github/main", base)
    return r, base


def test_same_stem_lift_old_dotted_path_import_is_red(lift_repo):
    """牙证 ①:删旧件后,在役文件还写 `from tools.oldx.advisor_llm import X` ⇒ 红(完整点分路径键)。"""
    r, base = lift_repo
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None,
                        "app/leftover.py": "from tools.oldx.advisor_llm import X\n"}, "delete old + leftover dotted import")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "撞名改判" in out and "`tools.oldx.advisor_llm`" in out, out
    assert "app/leftover.py" in out, out


def test_same_stem_lift_old_package_import_is_red(lift_repo):
    """牙证 ②:`from tools.oldx import advisor_llm` ⇒ 红(整包没了,包路径键接住)。"""
    r, base = lift_repo
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None,
                        "app/leftover.py": "from tools.oldx import advisor_llm\n"}, "delete old + leftover package import")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "`tools.oldx`" in out and "app/leftover.py" in out, out


def test_same_stem_lift_new_path_import_is_green(lift_repo):
    """对照臂 ③:只剩引用**新件**的 `from services.llm.advisor_llm import X` ⇒ 绿。
    改判之前这格是红的(按文件名 `advisor_llm` 搜会把 app/uses_new.py 判成没动过的残留引用者)。"""
    r, base = lift_repo
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None}, "delete old only")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "撞名改判" in out and "services/llm/advisor_llm.py" in out, out


def test_same_stem_escape_hatch_needs_a_real_nonempty_live_file(lift_repo):
    """防逃生:同名「在役文件」是空文件 ⇒ 不算撞名,照旧按文件名判 ⇒ 引用它的在役文件判红。"""
    r, base = lift_repo
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None,
                        "services/llm/advisor_llm.py": "   \n"}, "delete old + hollow out the lifted file")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "撞名改判" not in out, out
    assert "提到 `advisor_llm`" in out and "app/uses_new.py" in out, out


# ── [Review 09-28 二审] 撞名改判的两处补:① 包还在时兄弟文件的相对导入仍按裸名判 ② 斜杠写法守住 ──
def _mk_lift(tmp_path_factory, name: str, extra: dict[str, str]) -> tuple[Repo, str]:
    """独立合成仓:BASE_FILES + LIFT_FILES + extra 作为主干。"""
    path = tmp_path_factory.mktemp(name)
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    r = Repo(path)
    for rel, content in {**BASE_FILES, **LIFT_FILES, **extra}.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content.encode("utf-8"))
    _git(path, "add", "-A", "--force")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/github/main", base)
    return r, base


def test_same_stem_partial_package_sibling_relative_import_is_red(tmp_path_factory):
    """只删包里一个文件、包还在:兄弟文件 `from .advisor_llm import X` ⇒ 红(裸名在包目录下仍适用)。"""
    r, base = _mk_lift(tmp_path_factory, "lift_sib", {"tools/oldx/sibling.py": "from .advisor_llm import X\n"})
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None}, "delete one file; sibling keeps a relative import")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "兄弟文件仍按裸名" in out and "tools/oldx/sibling.py" in out, out


def test_same_stem_partial_package_other_dirs_still_judged_by_path(tmp_path_factory):
    """对照:包还在,但包外的在役文件只引新件 ⇒ 绿(裸名只在包目录下适用,不回到全仓误伤)。"""
    r, base = _mk_lift(tmp_path_factory, "lift_sib_ctl", {"tools/oldx/sibling.py": "Y = 1\n"})
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None}, "delete one file; nobody in the package names it")
    rc, out = r.census(h)
    assert rc == 0, out


def test_same_stem_slash_path_reference_is_red(tmp_path_factory):
    """斜杠写法:脚本里 `python tools/oldx/advisor_llm.py` ⇒ 红。"""
    r, base = _mk_lift(tmp_path_factory, "lift_slash", {})
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None,
                        "scripts/run_old.sh": "python tools/oldx/advisor_llm.py\n"}, "delete old + slash-path leftover")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "scripts/run_old.sh" in out, out


# ── [Review 09-28] 白名单第三类 homonym(同词不同物)──
HOMO_BASE = {
    "tools/oldy/lumen_gauge.py": "def run():\n    return 1\n",
    "app/geo_homonym.py": "lumen_gauge: object = None\n",
    "app/geo_pathline.py": 'NOTE = "tools/oldy lumen_gauge"\n',
    "app/geo_importline.py": "import lumen_gauge\n",
}
ALLOW = "tests/REFERENCER_COMMENT_ALLOW.txt"


def _homo_case(tmp_path_factory, name: str, entry: str | None, only: str) -> tuple[int, str]:
    """只让一个在役文件提到 lumen_gauge(其余两个清掉),删 tools/oldy/lumen_gauge.py,按需写一条白名单。"""
    r, base = _mk_lift(tmp_path_factory, name, HOMO_BASE)
    changes: dict[str, str | None] = {"tools/oldy/lumen_gauge.py": None}
    for f in ("app/geo_homonym.py", "app/geo_pathline.py", "app/geo_importline.py"):
        if f != only:
            changes[f] = None
    if entry is not None:
        changes[ALLOW] = entry
    h = r.commit(base, changes, f"homonym case {name}")
    return r.census(h)


def test_homonym_entry_lets_a_same_word_field_through(tmp_path_factory):
    """合规条目 ⇒ 绿,且读数单列;同一输入不写条目 ⇒ 红(证明是条目放的行,不是本来就绿)。"""
    rc0, out0 = _homo_case(tmp_path_factory, "homo_none", None, "app/geo_homonym.py")
    assert rc0 == 1 and "app/geo_homonym.py" in out0, out0
    entry = "app/geo_homonym.py\thomonym\tlumen_gauge: object = None\t另一个类的字段名,指观测解释引擎实例,不是被删的旧模块\n"
    rc, out = _homo_case(tmp_path_factory, "homo_ok", entry, "app/geo_homonym.py")
    assert rc == 0, out
    assert "同词不同物放行" in out and "app/geo_homonym.py" in out, out


def test_homonym_entry_on_a_line_with_the_deleted_package_path_is_red(tmp_path_factory):
    entry = 'app/geo_pathline.py\thomonym\tNOTE = "tools/oldy lumen_gauge"\t硬说是同词不同物,但行里带着被删文件的包路径\n'
    rc, out = _homo_case(tmp_path_factory, "homo_path", entry, "app/geo_pathline.py")
    assert rc == 1, out
    assert "含本班被删文件或其包的路径" in out, out


def test_homonym_entry_on_an_import_line_is_red(tmp_path_factory):
    entry = "app/geo_importline.py\thomonym\timport lumen_gauge\t硬说是同词不同物,但这是一条导入行\n"
    rc, out = _homo_case(tmp_path_factory, "homo_import", entry, "app/geo_importline.py")
    assert rc == 1, out
    assert "import / from 行不收 homonym" in out, out


# ── [Review 10-02 签 · WO_322] homonym 收 import 行:来源既不以被删键结尾、也不含被删路径形 ──
#    删 web/social/EmptyState.tsx;web/admin 下三种 import 行各自单独成为唯一引用者。
HOMO_IMPORT_BASE = {
    "web/social/EmptyState.tsx": "export function EmptyState() { return null }\n",
    "web/admin/ui.tsx": "export function EmptyState() { return null }\n",
    "web/admin/Campaigns.tsx": "import { FadeIn, EmptyState } from './ui'\n",
    "web/admin/BadKey.tsx": "import { EmptyState } from '../social/EmptyState'\n",
    "web/admin/BadPath.tsx": "import { FadeIn, EmptyState } from 'web/social/ui'\n",
}


#: ui.tsx 自己定义同名组件的那一行(非 import 行)—— 三格都登记它,让红只来自被测的那一行
_UI_DEF_ENTRY = ("web/admin/ui.tsx\thomonym\texport function EmptyState() { return null }\t"
                 "在役 ui 模块自己定义的同名空态组件(非 import 行,旧规则就收)\n")


def _homo_import_case(tmp_path_factory, name: str, entry: str | None, only: str) -> tuple[int, str]:
    r, base = _mk_lift(tmp_path_factory, name, HOMO_IMPORT_BASE)
    changes: dict[str, str | None] = {"web/social/EmptyState.tsx": None}
    for f in ("web/admin/Campaigns.tsx", "web/admin/BadKey.tsx", "web/admin/BadPath.tsx"):
        if f != only:
            changes[f] = None
    if entry is not None:
        changes[ALLOW] = entry
    h = r.commit(base, changes, f"homonym import case {name}")
    return r.census(h)


def test_homonym_import_line_from_a_live_sibling_is_let_through(tmp_path_factory):
    """对照:同目录在役模块的 import 行(来源 './ui')登记后 ⇒ 绿;不登记 ⇒ 红(证明是条目放的行)。"""
    rc0, out0 = _homo_import_case(tmp_path_factory, "hi_none", None, "web/admin/Campaigns.tsx")
    assert rc0 == 1 and "web/admin/Campaigns.tsx" in out0, out0
    entry = ("web/admin/Campaigns.tsx\thomonym\timport { FadeIn, EmptyState } from './ui'\t"
             "同目录在役 ui 模块导出的同名空态组件,不是被删的社媒空态文件\n" + _UI_DEF_ENTRY)
    rc, out = _homo_import_case(tmp_path_factory, "hi_ok", entry, "web/admin/Campaigns.tsx")
    assert rc == 0, out
    assert "同词不同物放行" in out and "web/admin/Campaigns.tsx" in out, out


def test_homonym_import_line_whose_source_ends_with_the_deleted_key_is_red(tmp_path_factory):
    """牙证 ①:从被删文件本身导入同名(`../social/EmptyState`)⇒ 登记了也红。"""
    entry = ("web/admin/BadKey.tsx\thomonym\timport { EmptyState } from '../social/EmptyState'\t"
             "硬说是同词不同物,但来源就是被删文件本身\n" + _UI_DEF_ENTRY)
    rc, out = _homo_import_case(tmp_path_factory, "hi_key", entry, "web/admin/BadKey.tsx")
    assert rc == 1, out
    assert "以被删键" in out, out
    assert out.count("🔴 删除引用者·注释行白名单") == 1, out


def test_homonym_import_line_whose_source_has_the_deleted_path_is_red(tmp_path_factory):
    """牙证 ②:同一行把来源换成含被删包路径的 `web/social/ui` ⇒ 登记了也红。"""
    entry = ("web/admin/BadPath.tsx\thomonym\timport { FadeIn, EmptyState } from 'web/social/ui'\t"
             "硬说是同词不同物,但来源落在被删文件的包路径下\n" + _UI_DEF_ENTRY)
    rc, out = _homo_import_case(tmp_path_factory, "hi_path", entry, "web/admin/BadPath.tsx")
    assert rc == 1, out
    assert "含被删路径" in out, out
    assert out.count("🔴 删除引用者·注释行白名单") == 1, out


# ── [Review 09-28 · B2 前置 三] 引用者分母排除 agent-test-artifacts*/(只读历史证据),带运行者保险 ──
ART_BASE = {
    "tools/oldz/legacy_widget_mod.py": "def w():\n    return 1\n",
}


def _mk_art(tmp_path_factory, name: str, extra: dict[str, str]) -> tuple[Repo, str]:
    path = tmp_path_factory.mktemp(name)
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    r = Repo(path)
    for rel, content in {**BASE_FILES, **ART_BASE, **extra}.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content.encode("utf-8"))
    _git(path, "add", "-A", "--force")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/remotes/github/main", base)
    return r, base


def test_artifact_dir_referencer_is_not_counted(tmp_path_factory):
    """历史证据目录下的引用者 ⇒ 不计、绿,且读数单列(文件数 / 文件×键数)。"""
    r, base = _mk_art(tmp_path_factory, "art_ok",
                      {"agent-test-artifacts-x/run1/report.json": '{"note": "legacy_widget_mod was called"}\n'})
    h = r.commit(base, {"tools/oldz/legacy_widget_mod.py": None}, "delete module; only the artifact dir names it")
    rc, out = r.census(h)
    assert rc == 0, out
    assert "历史证据不判" in out and "1 个文件" in out, out


def test_the_same_reference_outside_the_artifact_dir_is_red(tmp_path_factory):
    """对照:同样的引用放到目录外 ⇒ 红(排除只认这个目录,不是对这个键放水)。"""
    r, base = _mk_art(tmp_path_factory, "art_ctl",
                      {"app/report.json": '{"note": "legacy_widget_mod was called"}\n'})
    h = r.commit(base, {"tools/oldz/legacy_widget_mod.py": None}, "delete module; an ordinary file names it")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "app/report.json" in out, out


def test_a_runner_that_points_into_the_artifact_dir_is_red(tmp_path_factory):
    """保险:workflow 里跑了该目录下的脚本 ⇒ 不排除、判红(证明「没人跑」不是靠文件后缀猜的)。"""
    ci = BASE_FILES[".github/workflows/ci.yml"] + "          python agent-test-artifacts-x/_runner.py\n"
    r, base = _mk_art(tmp_path_factory, "art_runner",
                      {"agent-test-artifacts-x/run1/report.json": '{"note": "legacy_widget_mod was called"}\n',
                       "agent-test-artifacts-x/_runner.py": "print('legacy_widget_mod')\n"})
    h = r.commit(base, {"tools/oldz/legacy_widget_mod.py": None,
                        ".github/workflows/ci.yml": ci}, "delete module; CI runs a script in the artifact dir")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "运行者引用了 agent-test-artifacts*" in out and ".github/workflows/ci.yml" in out, out


# ── [Review 09-28 · gate 3 PASS 附议 · 随 B2 交] 运行者保险逐面各一格:七个运行者面各放一行指向 agent-test-artifacts*,
#   每一面都必须单独判红(原先只有 workflow 那一面有格;其余六面的牙没被单独证过)。
def _pkg_with_art() -> str:
    import json as _json
    d = _json.loads(PKG_CHAIN)
    d["scripts"]["art_x"] = "python ../agent-test-artifacts-x/_runner.py"
    return _json.dumps(d, ensure_ascii=False, indent=2) + "\n"


ART_RUNNER_SURFACES = {
    "must_run": ("tests/MUST_RUN.txt", lambda: BASE_FILES["tests/MUST_RUN.txt"] + "# 顺带跑 agent-test-artifacts-x/_runner.py\n"),
    "oneshot": ("tests/ONESHOT_RUNS.txt", lambda: BASE_FILES["tests/ONESHOT_RUNS.txt"] + "# 顺带跑 agent-test-artifacts-x/_runner.py\n"),
    "package_json_build": ("frontend/package.json", _pkg_with_art),
    "scheduler": ("api/scheduler.py", lambda: "# 定时任务里调 agent-test-artifacts-x/_runner.py\nJOBS = []\n"),
    "root_sh": ("run_all.sh", lambda: "#!/bin/sh\npython agent-test-artifacts-x/_runner.py\n"),
    "makefile": ("Makefile", lambda: "art:\n\tpython agent-test-artifacts-x/_runner.py\n"),
    "docker_compose": ("docker-compose.yml", lambda: "services:\n  art:\n    command: python agent-test-artifacts-x/_runner.py\n"),
}


@pytest.mark.parametrize("surface", sorted(ART_RUNNER_SURFACES))
def test_each_runner_surface_pointing_into_the_artifact_dir_is_red(tmp_path_factory, surface):
    rel, make = ART_RUNNER_SURFACES[surface]
    r, base = _mk_art(tmp_path_factory, f"art_surf_{surface}",
                      {"agent-test-artifacts-x/run1/report.json": '{"note": "legacy_widget_mod was called"}\n',
                       "agent-test-artifacts-x/_runner.py": "print('legacy_widget_mod')\n"})
    h = r.commit(base, {"tools/oldz/legacy_widget_mod.py": None, rel: make()},
                 f"delete module; {rel} runs a script in the artifact dir")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "运行者引用了 agent-test-artifacts*" in out and rel in out, out


def test_runner_surface_arm_without_the_runner_line_is_green(tmp_path_factory):
    """对照臂:同一合成仓、同样删模块、只有证据目录提它、七个面都不提 ⇒ 绿(上面七格的红来自那一行,不是别的)。"""
    r, base = _mk_art(tmp_path_factory, "art_surf_ctl",
                      {"agent-test-artifacts-x/run1/report.json": '{"note": "legacy_widget_mod was called"}\n',
                       "agent-test-artifacts-x/_runner.py": "print('legacy_widget_mod')\n",
                       "api/scheduler.py": "JOBS = []\n", "run_all.sh": "#!/bin/sh\necho ok\n",
                       "Makefile": "ok:\n\techo ok\n", "docker-compose.yml": "services: {}\n"})
    h = r.commit(base, {"tools/oldz/legacy_widget_mod.py": None}, "delete module; no runner surface names the artifact dir")
    rc, out = r.census(h)
    assert rc == 0, out


# ── [Review 09-28 · 可选两格] ① 子目录兄弟的上跳相对导入 ② 同词不同物条目的原行改了 ⇒ 陈条目 ──
def test_same_stem_partial_package_subdir_sibling_parent_relative_import_is_red(tmp_path_factory):
    """包还在:子目录里的兄弟 `from ..advisor_llm import X` ⇒ 红(裸名在包目录下含子目录都适用)。"""
    r, base = _mk_lift(tmp_path_factory, "lift_subsib", {"tools/oldx/sub/__init__.py": "",
                                                          "tools/oldx/sub/child.py": "from ..advisor_llm import X\n"})
    h = r.commit(base, {"tools/oldx/advisor_llm.py": None}, "delete one file; a subdir sibling keeps a parent-relative import")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "tools/oldx/sub/child.py" in out, out


def test_homonym_entry_whose_line_changed_is_reported_stale(tmp_path_factory):
    """条目写的是旧行原文,文件里那行已改 ⇒ 红且报陈条目(条目不会因为换了个说法就一直放行)。"""
    entry = "app/geo_homonym.py\thomonym\tlumen_gauge: object = None\t另一个类的字段名,指观测解释引擎实例,不是被删的旧模块\n"
    r, base = _mk_lift(tmp_path_factory, "homo_stale", HOMO_BASE)
    h = r.commit(base, {"tools/oldy/lumen_gauge.py": None, "app/geo_pathline.py": None, "app/geo_importline.py": None,
                        "app/geo_homonym.py": "lumen_gauge: object = 0\n", ALLOW: entry},
                 "homonym entry names the old line; the line has changed")
    rc, out = r.census(h)
    assert rc == 1, out
    assert "陈条目" in out and "app/geo_homonym.py" in out, out


# ── [Review 09-28 · 门五] 泛名键(trace / chat_db / document …)不再只「没搜」:按完整路径 + 包内相对导入形判 ──
GEN_BASE = {
    "tools/oldg/trace.py": "def f():\n    return 1\n",
    "tools/oldg/keeper.py": "X = 1\n",
}


def _gen_case(tmp_path_factory, name: str, extra: dict[str, str | None]) -> tuple[int, str]:
    r, base = _mk_art(tmp_path_factory, name, GEN_BASE)
    return r.census(r.commit(base, {"tools/oldg/trace.py": None, **extra}, f"generic stem case {name}"))


def test_generic_stem_full_dotted_path_reference_is_red(tmp_path_factory):
    """泛名文件删了,别处按完整点分路径 import 它 ⇒ 红(原来这里只报「没搜」、退出码 0)。"""
    rc, out = _gen_case(tmp_path_factory, "gen_dotted", {"app/uses_trace.py": "from tools.oldg.trace import f\n"})
    assert rc == 1, out
    assert "app/uses_trace.py" in out and "泛名改判" in out, out


def test_generic_stem_slash_path_reference_is_red(tmp_path_factory):
    rc, out = _gen_case(tmp_path_factory, "gen_slash", {"scripts/run_trace.sh": "python tools/oldg/trace.py\n"})
    assert rc == 1, out
    assert "scripts/run_trace.sh" in out, out


def test_generic_stem_sibling_relative_import_is_red(tmp_path_factory):
    """包还在:兄弟文件 `from .trace import f` 只写裸名 ⇒ 按相对导入形判红。"""
    rc, out = _gen_case(tmp_path_factory, "gen_rel", {"tools/oldg/keeper.py": "from .trace import f\nX = 1\n"})
    assert rc == 1, out
    assert "tools/oldg/keeper.py" in out, out


def test_generic_stem_as_an_ordinary_word_is_green(tmp_path_factory):
    """对照臂:泛名在别处只是普通词 / 属性名(`stack trace`、`self.trace()`,包内包外都有)⇒ 绿 ——
    证明上面三格的红来自那几种引用形,不是见词就红。"""
    rc, out = _gen_case(tmp_path_factory, "gen_word", {
        "app/logger.py": 'MSG = "print the stack trace"\n',
        "tools/oldg/keeper.py": "class K:\n    def run(self):\n        return self.trace()\n",
    })
    assert rc == 0, out
    assert "泛名改判" in out, out


def test_single_segment_generic_path_is_still_listed_as_not_searched(tmp_path_factory):
    """完整路径只剩一段(仓根的 `utils.py`)⇒ 点分 / 斜杠形都是普通词,仍列「没搜」,不装作搜过。"""
    r, base = _mk_art(tmp_path_factory, "gen_top", {"utils.py": "X = 1\n"})
    rc, out = r.census(r.commit(base, {"utils.py": None}, "delete a top-level generic file"))
    assert rc == 0, out
    assert "没搜" in out and "utils.py" in out, out


# ── [Review 09-28 · 门五补] 白名单第四类 forbid_list(锁里禁止已删模块回流的名单行)──
FB_BASE = {
    "tools/oldf/legacy_forbid_mod.py": "def f():\n    return 1\n",
    "tools/live_forbid_x.py": "Y = 1\n",
}
FB_LINE = 'BANNED = ("tools.oldf.legacy_forbid_mod",)  # 已删,留在禁用名单里防回流'


def _fb_case(tmp_path_factory, name: str, lock_rel: str, lock_line: str, entry: str | None) -> tuple[int, str]:
    r, base = _mk_art(tmp_path_factory, name, {**FB_BASE, lock_rel: lock_line + "\n"})
    changes: dict[str, str | None] = {"tools/oldf/legacy_forbid_mod.py": None}
    if entry is not None:
        changes[ALLOW] = entry
    return r.census(r.commit(base, changes, f"forbid list case {name}"))


def _fb_entry(rel: str, line: str) -> str:
    return f"{rel}\tforbid_list\t{line}\t锁的禁用名单:禁止已删模块回流\n"


def test_forbid_list_entry_lets_a_lock_name_list_through(tmp_path_factory):
    """锁里的禁用名单行没登记 ⇒ 红;登记 forbid_list ⇒ 绿(证明是条目放的行,不是本来就绿)。"""
    rel = "tests/pkg_a/test_lock.py"
    rc0, out0 = _fb_case(tmp_path_factory, "fb_none", rel, FB_LINE, None)
    assert rc0 == 1 and rel in out0, out0
    rc, out = _fb_case(tmp_path_factory, "fb_ok", rel, FB_LINE, _fb_entry(rel, FB_LINE))
    assert rc == 0, out


def test_forbid_list_entry_outside_tests_is_red(tmp_path_factory):
    rel = "app/banned.py"
    rc, out = _fb_case(tmp_path_factory, "fb_out", rel, FB_LINE, _fb_entry(rel, FB_LINE))
    assert rc == 1, out
    assert "不在 tests/ 下" in out, out


def test_forbid_list_naming_a_live_module_is_red(tmp_path_factory):
    rel = "tests/pkg_a/test_lock.py"
    line = 'BANNED = ("tools.oldf.legacy_forbid_mod", "tools.live_forbid_x")'
    rc, out = _fb_case(tmp_path_factory, "fb_live", rel, line, _fb_entry(rel, line))
    assert rc == 1, out
    assert "仍在" in out and "tools.live_forbid_x" in out, out


def test_forbid_list_on_an_import_line_is_red(tmp_path_factory):
    rel = "tests/pkg_a/test_lock.py"
    line = "from tools.oldf import legacy_forbid_mod"
    rc, out = _fb_case(tmp_path_factory, "fb_import", rel, line, _fb_entry(rel, line))
    assert rc == 1, out
    assert "行形状" in out, out
