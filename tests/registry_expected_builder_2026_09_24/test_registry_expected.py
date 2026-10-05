"""WO_295 · 登记表期望文件构建器(scripts/registry_expected.py)的牙证与对照。

每格一个临时合成仓:基线提交 → 分出「现役」「改形支」「交付」,只改一处,让一个条件决定结果。
🔴 病例(必做成夹具):09-24 AC —— 现役上 A 改了 card_templates 那行「守什么」措辞,
   改形支没碰那一行;旧的手写构建器按「基线行赢」把已上线措辞回退。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "registry_expected.py"

MR, OS, RT = "tests/MUST_RUN.txt", "tests/ONESHOT_RUNS.txt", "tests/RETIRED_TESTS.txt"
MR_HEAD = "# 必跑集(合成)\n#\n# 说明一\n# 说明二\n# 说明三\n#\n"
BASE = {
    MR: MR_HEAD + "tests/a\t守 a\ntests/card\t卡片旧措辞\n",
    OS: "# 随单(合成)\n#\ntests/o1\t旧守什么\n",
    RT: "# 退役(合成)\n",
}
OS_3COL = "# 随单(合成)\n# 三栏\n#\ntests/o1\t旧守什么\tWO_900 · 签字条\n"


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_AUTHOR_NAME="b", GIT_AUTHOR_EMAIL="b@example.invalid", GIT_COMMITTER_NAME="b",
               GIT_COMMITTER_EMAIL="b@example.invalid", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


_TOOL_MOD = None


def _inproc(argv: list[str]) -> tuple[int, str]:
    """进程内跑工具:按文件路径加载仓里这一份(不走 sys.path),临时拿掉 GIT_DIR 一类变量(与 CLI 臂同环境)。"""
    global _TOOL_MOD
    if _TOOL_MOD is None:
        spec = importlib.util.spec_from_file_location("_registry_expected_under_test", TOOL)
        _TOOL_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_TOOL_MOD)
    saved = {k: os.environ.pop(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE") if k in os.environ}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = _TOOL_MOD.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        os.environ.update(saved)
    return rc, out.getvalue() + err.getvalue()


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.base = self.commit(None, BASE)

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_env())
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, parent: str | None, files: dict[str, str | None]) -> str:
        if parent:
            self.git("checkout", "-q", "--detach", parent)
        for rel, text in files.items():
            p = self.path / rel
            if text is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(text.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "c")
        return self.git("rev-parse", "HEAD")

    def _argv(self, live: str, reshape: str, deliveries, extra, out: Path) -> list[str]:
        args = ["--repo", str(self.path), "--live", live, "--reshape", reshape, "--out", str(out), *extra]
        for d in deliveries:
            args += ["--delivery", d]
        return args

    def run(self, live: str, reshape: str, *deliveries: str, extra: tuple[str, ...] = ()) -> tuple[int, str, Path]:
        """[AE 提速] 进程内调仓里这一份工具的 main();与 CLI 等价由 test_in_process_and_cli_give_identical_readings 核。"""
        out = self.path.parent / f"out_{self.path.name}"
        rc, text = _inproc(self._argv(live, reshape, deliveries, extra, out))
        return rc, text, out

    def run_cli(self, live: str, reshape: str, *deliveries: str, extra: tuple[str, ...] = ()) -> tuple[int, str, Path]:
        out = self.path.parent / f"out_cli_{self.path.name}"
        r = subprocess.run([sys.executable, str(TOOL), *self._argv(live, reshape, deliveries, extra, out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
        return r.returncode, r.stdout + r.stderr, out

    @staticmethod
    def read(out: Path, name: str) -> str:
        return (out / f"rv_expected_{name}.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Repo:
    """[AE 提速] 整个模块共用一个合成仓:每格都从同一个基线提交分叉、不改基线,互不影响。"""
    return Repo(tmp_path_factory.mktemp("regexp") / "r")


def test_in_process_and_cli_give_identical_readings(repo):
    """[AE 提速] 其余格走进程内;这一格核「进程内 == CLI」:退出码、整段输出(去掉输出目录名)、三张期望文件逐字节相同。"""
    live = repo.commit(repo.base, {MR: BASE[MR].replace("卡片旧措辞", "卡片新措辞(已上线)")})
    reshape = repo.commit(repo.base, {OS: OS_3COL})
    rc_in, out_in, d_in = repo.run(live, reshape)
    rc_cli, out_cli, d_cli = repo.run_cli(live, reshape)
    norm = lambda s, d: s.replace(str(d), "<OUT>").strip()
    assert (rc_in, norm(out_in, d_in)) == (rc_cli, norm(out_cli, d_cli)), f"进程内:\n{out_in}\nCLI:\n{out_cli}"
    for n in ("MUST_RUN", "ONESHOT_RUNS", "RETIRED_TESTS"):
        assert (d_in / f"rv_expected_{n}.txt").read_bytes() == (d_cli / f"rv_expected_{n}.txt").read_bytes(), n


def test_the_card_case_keeps_the_live_wording(repo):
    """病例:现役改了 card 的措辞,改形支加预算行、把随单表改三栏、并**顺手改了另一行 a 的措辞**。
    「只有一边改过取那一边」两个方向都在这一格里:card 只有现役改 ⇒ 取现役;a 只有改形支改(同形)⇒ 取改形支。
    (Review 09-24:原夹具没有「只有改形支改、同形」的行,「只有 X 改过取 X」被毒成 False 时本格照绿。)"""
    live = repo.commit(repo.base, {MR: BASE[MR].replace("卡片旧措辞", "卡片新措辞(已上线)")})
    reshape = repo.commit(repo.base, {
        MR: BASE[MR].replace("# 说明三\n", "# 说明三\n# MUST_RUN_BUDGET_SECONDS=300\n")
                    .replace("守 a\n", "守 a(改形支顺手改的措辞)\n"),
        OS: OS_3COL})
    rc, out, d = repo.run(live, reshape)
    assert rc == 0, out
    must = repo.read(d, "MUST_RUN")
    assert "tests/card\t卡片新措辞(已上线)\n" in must, must
    assert "卡片旧措辞" not in must, must
    assert "# MUST_RUN_BUDGET_SECONDS=300\n" in must, must
    assert must == MR_HEAD.replace("# 说明三\n", "# 说明三\n# MUST_RUN_BUDGET_SECONDS=300\n") \
        + "tests/a\t守 a(改形支顺手改的措辞)\ntests/card\t卡片新措辞(已上线)\n", must
    assert "tests/o1\t旧守什么\tWO_900 · 签字条" in repo.read(d, "ONESHOT_RUNS")


def test_both_changed_with_different_shapes_the_reshaped_side_wins(repo):
    live = repo.commit(repo.base, {OS: BASE[OS].replace("旧守什么", "现役改过的守什么")})
    reshape = repo.commit(repo.base, {OS: OS_3COL})
    rc, out, d = repo.run(live, reshape)
    assert rc == 0, out
    assert "tests/o1\t旧守什么\tWO_900 · 签字条" in repo.read(d, "ONESHOT_RUNS")
    assert "都改了且不同形 ⇒ 取与改形支同形(3 栏)" in out, out


def test_both_changed_with_the_same_shape_is_red(repo):
    reshape = repo.commit(repo.base, {RT: "# 退役(合成)\n# 三栏\n"})
    d1 = repo.commit(repo.base, {MR: BASE[MR].replace("守 a", "交付一的说法")})
    d2 = repo.commit(repo.base, {MR: BASE[MR].replace("守 a", "交付二的说法")})
    rc, out, _ = repo.run(repo.base, reshape, d1, d2)
    assert rc == 1, out
    assert "都改了且同形 ⇒ 要人定" in out, out


def test_a_delivery_that_appends_and_deletes_is_taken_and_attributed(repo):
    reshape = repo.commit(repo.base, {RT: "# 退役(合成)\n# 三栏\n"})
    d1 = repo.commit(repo.base, {MR: MR_HEAD + "tests/card\t卡片旧措辞\ntests/new\t新登记\n"})
    rc, out, d = repo.run(repo.base, reshape, d1)
    assert rc == 0, out
    must = repo.read(d, "MUST_RUN")
    assert must.endswith("tests/card\t卡片旧措辞\ntests/new\t新登记\n"), must
    assert "tests/a\t" not in must, must
    assert f"增 `tests/new` ← {d1[:9]}" in out and f"删 `tests/a` ← {d1[:9]}" in out, out


def test_a_live_row_left_in_the_old_shape_is_red(repo):
    live = repo.commit(repo.base, {OS: BASE[OS] + "tests/o2\t现役新加的两栏行\n"})
    reshape = repo.commit(repo.base, {OS: OS_3COL})
    rc, out, _ = repo.run(live, reshape)
    assert rc == 1, out
    assert "`tests/o2` 是 2 栏,改形支是 3 栏" in out, out


def test_header_changed_on_both_sides_in_different_places_merges_cleanly(repo):
    live = repo.commit(repo.base, {MR: BASE[MR].replace("# 说明一\n", "# 说明一(现役改过)\n")})
    reshape = repo.commit(repo.base, {MR: BASE[MR].replace("# 说明三\n#\n", "# 说明三\n#\n# MUST_RUN_BUDGET_SECONDS=300\n")})
    rc, out, d = repo.run(live, reshape)
    assert rc == 0, out
    must = repo.read(d, "MUST_RUN")
    assert "# 说明一(现役改过)\n" in must and "# MUST_RUN_BUDGET_SECONDS=300\n" in must, must
    assert "逐行三方合并干净" in out, out


def test_header_conflict_is_red_and_names_the_lines_it_would_lose(repo):
    live = repo.commit(repo.base, {MR: BASE[MR].replace("# 说明二\n", "# 说明二:现役写的新事实\n")})
    reshape = repo.commit(repo.base, {MR: BASE[MR].replace("# 说明二\n", "# 说明二:改形支的说法\n")})
    rc, out, d = repo.run(live, reshape)
    assert rc == 1, out
    assert "- # 说明二:现役写的新事实" in out, out
    rc2, out2, d2 = repo.run(live, reshape, extra=("--accept-header-loss", "MUST_RUN"))
    assert rc2 == 0, out2
    assert "已用 --accept-header-loss 确认可丢" in out2, out2
    assert "# 说明二:改形支的说法\n" in repo.read(d2, "MUST_RUN")


def test_an_unresolvable_sha_is_rc3(repo):
    rc, out, _ = repo.run(repo.base, "0" * 40)
    assert rc == 3, out


def test_the_exit_audit_catches_a_row_its_source_does_not_have(repo):
    """出口核自己的牙证:构建逻辑若算错(某行写成来源里没有的样子),出口核必须红;原样则不红。"""
    d1 = repo.commit(repo.base, {MR: BASE[MR] + "tests/new\t新登记\n"})
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import registry_expected as re_mod
    finally:
        sys.path.pop(0)
    re_mod.REPO = str(repo.path)
    head, rows, order = re_mod.parse(repo.base, MR)
    good = dict(rows, **{"tests/new": "tests/new\t新登记"})
    bad = dict(rows, **{"tests/new": "tests/new\t新登记(被构建器改坏)"})
    src = {k: "live" for k in rows} | {"tests/new": d1}
    args = (head, rows, order, head, "live", {})
    red_ok, prov_ok = re_mod.audit("MUST_RUN", MR, *args, good, src, order + ["tests/new"])
    red_bad, _ = re_mod.audit("MUST_RUN", MR, *args, bad, src, order + ["tests/new"])
    assert red_ok == [] and any("tests/new" in p for p in prov_ok), (red_ok, prov_ok)
    assert red_bad and "指不出来源" in red_bad[0], red_bad
