"""services/ai_ops/codex_runner.py 测试(fake subprocess,不真调 Codex,无 DB)。"""
import types

import pytest

from services.ai_ops import codex_runner as cr


class _FakeRunner:
    """记录被调用的命令,返回可控结果。"""
    def __init__(self, returncode=0, stdout="done", stderr=""):
        self.calls = []
        self._rc = returncode
        self._out = stdout
        self._err = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self._rc, stdout=self._out, stderr=self._err)


# ==========================================
# 密钥文件扫描 · fail closed
# ==========================================

def test_find_secret_files_detects_env_and_pem(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".env.example").write_text("SECRET=")   # tracked 模板 · 白名单(P0-1)
    (tmp_path / "apiclient_key.pem").write_text("-----BEGIN PRIVATE KEY-----")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)")
    hits = cr.find_secret_files(str(tmp_path))
    assert ".env" in hits
    assert "apiclient_key.pem" in hits
    assert ".env.example" not in hits          # P0-1:tracked 模板不误判 fail closed
    assert "src/app.py" not in hits and "src\\app.py" not in hits


def test_assert_no_secrets_ok_with_only_env_example(tmp_path):
    (tmp_path / ".env.example").write_text("DATABASE_URL=")
    cr.assert_no_secrets(str(tmp_path))         # 只有模板 → 不抛(P0-1)


def test_collect_git_diff_includes_untracked(tmp_path):
    """P0-2:Codex 新增但未 git add 的文件也要进 diff。"""
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "t")
    (repo / "existing.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    # 模拟 Codex:新增未 add 的文件 + 改已有文件
    (repo / "new_module.py").write_text("def added():\n    return 1\n")
    (repo / "existing.py").write_text("x = 2\n")
    _stat, diff = cr.collect_git_diff(str(repo))
    assert "new_module.py" in diff              # untracked 新文件进 diff
    assert "existing.py" in diff


def test_assert_no_secrets_raises_on_env(tmp_path):
    (tmp_path / ".env").write_text("X=1")
    with pytest.raises(cr.SecretLeakError):
        cr.assert_no_secrets(str(tmp_path))


def test_run_codex_fail_closed_when_secret_present(tmp_path):
    (tmp_path / "pub_key.pem").write_text("k")
    fake = _FakeRunner()
    with pytest.raises(cr.SecretLeakError):
        cr.run_codex(str(tmp_path), "diagnose",
                     sandbox="read-only",
                     last_message_path=str(tmp_path / "m.md"),
                     runner=fake)
    assert fake.calls == []   # 密钥存在时绝不调用 Codex


def test_run_codex_refuses_active_repo(tmp_path):
    """worktree 解析成活 repo(forbid_paths)时拒绝跑。"""
    fake = _FakeRunner()
    with pytest.raises(cr.SecretLeakError):
        cr.run_codex(str(tmp_path), "diagnose",
                     sandbox="read-only",
                     last_message_path=str(tmp_path / "m.md"),
                     forbid_paths=[str(tmp_path)],
                     runner=fake)
    assert fake.calls == []


# ==========================================
# 命令构造 · --cd 指向 worktree
# ==========================================

def test_build_codex_cmd_shape():
    cmd = cr.build_codex_cmd(
        codex_bin="codex", worktree_path="/wt", sandbox="read-only",
        last_message_path="/wt/m.md", prompt="hi", image_path="/wt/s.png", model="gpt-x",
    )
    assert cmd[:2] == ["codex", "exec"]
    assert "--cd" in cmd and cmd[cmd.index("--cd") + 1] == "/wt"
    assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--json" in cmd
    assert "--image" in cmd and cmd[cmd.index("--image") + 1] == "/wt/s.png"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-x"
    assert cmd[-1] == "hi"


def test_build_codex_cmd_rejects_bad_sandbox():
    with pytest.raises(ValueError):
        cr.build_codex_cmd(codex_bin="codex", worktree_path="/wt", sandbox="yolo",
                           last_message_path="/m", prompt="x")


def test_run_codex_passes_worktree_not_active_repo(tmp_path):
    """clean worktree(无密钥)· fake runner · 断言 --cd 是 worktree。"""
    wt = tmp_path / "worktrees" / "task-1"
    wt.mkdir(parents=True)
    (wt / "README.md").write_text("clean")
    fake = _FakeRunner(returncode=0, stdout="ok")
    last = tmp_path / "m.md"
    result = cr.run_codex(str(wt), "diagnose",
                          sandbox="read-only",
                          last_message_path=str(last),
                          forbid_paths=[str(tmp_path / "active_repo")],
                          runner=fake)
    assert result["returncode"] == 0
    assert len(fake.calls) == 1
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--cd") + 1] == str(wt)
    assert str(tmp_path / "active_repo") != str(wt)


def test_run_codex_reads_last_message(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "keep.txt").write_text("x")
    last = tmp_path / "last.md"

    def runner_writes(cmd, **kwargs):
        # 模拟 codex 写 --output-last-message
        idx = cmd.index("--output-last-message")
        with open(cmd[idx + 1], "w", encoding="utf-8") as f:
            f.write("根因: 空指针")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    result = cr.run_codex(str(wt), "diagnose", sandbox="read-only",
                          last_message_path=str(last), runner=runner_writes)
    assert "根因" in result["last_message"]


# ==========================================
# 截图:先下载成本地文件再 --image
# ==========================================

def test_download_signed_image_writes_local_file(tmp_path, monkeypatch):
    dest = tmp_path / "shot.png"

    class _Resp:
        content = b"PNGDATA"
        def raise_for_status(self):
            return None

    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=30: _Resp())
    path = cr.download_signed_image("https://signed.example/x?sig=1", str(dest))
    assert path == str(dest)
    assert dest.read_bytes() == b"PNGDATA"
