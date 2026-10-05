"""WO_307 · preflight 5-f Dockerfile FROM 锁摘要门(scripts/gate_dockerfile_from_digest.py)的牙证与对照。

合成仓(模块共享一个,每格从同一基线分叉);门进程内调用,另留一格核「进程内 == CLI」。
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
GATE = ROOT / "scripts" / "gate_dockerfile_from_digest.py"
D = "@sha256:" + "0123456789abcdef" * 4          # 合成摘要,64 位十六进制
D2 = "@sha256:" + "fedcba9876543210" * 4


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_AUTHOR_NAME="b", GIT_AUTHOR_EMAIL="b@example.invalid", GIT_COMMITTER_NAME="b",
               GIT_COMMITTER_EMAIL="b@example.invalid", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("dfdigest")
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    (path / "README.md").write_bytes(b"base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _commit(repo: Path, base: str, files: dict[str, str]) -> str:
    _git(repo, "checkout", "-q", "--detach", base)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


_MOD = None


def _run(repo: Path, ref: str) -> tuple[int, str]:
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("_dfdigest_under_test", GATE)
        _MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MOD)
    saved = {k: os.environ.pop(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE") if k in os.environ}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = _MOD.main(["--repo", str(repo), "--ref", ref])
    finally:
        os.environ.update(saved)
    return rc, out.getvalue()


def _run_cli(repo: Path, ref: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(GATE), "--repo", str(repo), "--ref", ref], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=_env())
    return r.returncode, r.stdout + r.stderr


def _df(*froms: str) -> str:
    return "".join(f"{f}\nRUN echo {i}\n\n" for i, f in enumerate(froms))


@pytest.mark.parametrize("dockerfile, want_rc, needle", [
    (_df("FROM python:3.12-slim"), 1, "Dockerfile:1 FROM python:3.12-slim:没锁摘要"),
    (_df("from python:3.12-slim"), 1, "Dockerfile:1 FROM python:3.12-slim:没锁摘要"),
    (_df(f"FROM python:3.12-slim{D}"), 0, "✅ 1 个 Dockerfile · 1 条 FROM"),
    (_df(f"FROM python{D}"), 0, "✅"),
    (_df(f"FROM node:20-alpine{D} AS frontend-builder", f"FROM python:3.12-slim{D2}",
         "COPY --from=frontend-builder /a /b"), 0, "✅ 1 个 Dockerfile · 2 条 FROM"),
    (_df(f"FROM node:20-alpine{D} AS builder", "FROM builder"), 0, "引用本文件前面的阶段"),
    (_df(f"FROM node:20-alpine{D} as Builder", "FROM BUILDER"), 0, "引用本文件前面的阶段"),
    (_df("FROM builder", f"FROM node:20-alpine{D} AS builder"), 1, "FROM builder:没锁摘要"),
    (_df("FROM scratch"), 0, "scratch 空镜像"),
    ("ARG BASE=python:3.12-slim\n" + _df("FROM ${BASE}"), 1, "镜像名里有变量"),
    (_df("FROM python:3.12-slim@sha256:" + "a" * 63), 1, "不是 64 位小写十六进制"),
    (_df(f"FROM --platform=$BUILDPLATFORM python:3.12-slim{D}"), 0, "✅"),
    (f"FROM \\\n    python:3.12-slim{D}\nRUN true\n", 0, "✅"),
    ("# FROM python:3.12-slim 这只是注释\n" + _df(f"FROM python:3.12-slim{D}"), 0, "✅ 1 个 Dockerfile · 1 条 FROM"),
], ids=["unpinned", "lowercase-from-tag-only", "pinned-with-tag", "pinned-no-tag", "multi-stage-pinned", "stage-ref-exempt",
        "stage-ref-case-insensitive", "stage-name-defined-later-is-not-a-stage", "scratch", "arg-variable",
        "digest-63-hex", "platform-flag", "line-continuation", "comment-ignored"])
def test_each_from_shape(repo, dockerfile, want_rc, needle):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"Dockerfile": dockerfile}))
    assert rc == want_rc, out
    assert needle in out, out


def test_every_dockerfile_in_the_tree_is_scanned(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"Dockerfile": _df(f"FROM python:3.12-slim{D}"),
                                        "deploy/Dockerfile.worker": _df("FROM redis:7-alpine")}))
    assert rc == 1, out
    assert "deploy/Dockerfile.worker:1 FROM redis:7-alpine:没锁摘要" in out, out


def test_no_dockerfile_at_all_is_rc3(repo):
    r, base = repo
    rc, out = _run(r, base)                     # 基线只有 README
    assert rc == 3, out
    assert "一个 Dockerfile 都没找到" in out, out


def test_a_broken_classifier_is_rc3_not_a_pass(repo, monkeypatch):
    """自检的牙证:分类函数坏成「一律已锁」时,门必须 rc 3,不能拿坏尺子量出一片绿。"""
    r, base = repo
    h = _commit(r, base, {"Dockerfile": _df("FROM python:3.12-slim")})
    _run(r, h)                                  # 确保模块已加载
    monkeypatch.setattr(_MOD, "classify", lambda image, stages: ("pinned", "坏尺子"))
    rc, out = _run(r, h)
    assert rc == 3, out
    assert "自检不过" in out, out


@pytest.mark.parametrize("scenario", ["green", "red"])
def test_in_process_and_cli_give_identical_readings(repo, scenario):
    r, base = repo
    df = _df(f"FROM python:3.12-slim{D}") if scenario == "green" else _df("FROM python:3.12-slim")
    h = _commit(r, base, {"Dockerfile": df})
    a, b = _run(r, h), _run_cli(r, h)
    assert (a[0], a[1].strip()) == (b[0], b[1].strip()), f"进程内:\n{a[1]}\nCLI:\n{b[1]}"
