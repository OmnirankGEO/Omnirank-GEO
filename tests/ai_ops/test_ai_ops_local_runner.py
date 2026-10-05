"""本机 Codex Runner(services/ai_ops/local_codex_runner)测试。

不联网:RunnerApi 用 FakeApi 替身;codex_runner 全 monkeypatch。纯单元,不需要 DB。
"""
import pytest

from services.ai_ops import local_codex_runner as lr


class FakeApi:
    """记录调用;可配置 claim 响应。"""

    def __init__(self, claim_response=None):
        self.calls: list[tuple[str, dict]] = []
        self.claim_response = claim_response

    def post(self, path, payload, timeout=30):
        self.calls.append((path, payload))
        if path == "/claim":
            return self.claim_response
        return {"ok": True}

    def paths(self):
        return [p for p, _ in self.calls]


def _config(monkeypatch, **env):
    defaults = {
        "AI_OPS_SERVER_URL": "https://example.com",
        "AI_OPS_RUNNER_TOKEN": "test-runner-token-1234567890",
    }
    defaults.update(env)
    for k in ("AI_OPS_LOCAL_CODEX_ENABLED", "AI_OPS_LOCAL_CODEX_POLL_SECONDS",
              "AI_OPS_REPO_PATH", "AI_OPS_WORKTREE_ROOT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)
    return lr.LocalConfig()


# ==========================================
# HTTPS 校验:生产必须 https;仅 localhost/127.0.0.1 允许 http
# ==========================================

def test_server_url_https_ok():
    assert lr.validate_server_url("https://omnirank.top") == "https://omnirank.top"


def test_server_url_http_localhost_ok():
    assert lr.validate_server_url("http://localhost:8000") == "http://localhost:8000"
    assert lr.validate_server_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"


def test_server_url_http_non_localhost_rejected():
    """http 非 localhost 直接拒绝启动(token 明文跑公网 http = 裸奔)。"""
    with pytest.raises(SystemExit):
        lr.validate_server_url("http://omnirank.top")
    with pytest.raises(SystemExit):
        lr.validate_server_url("http://192.168.1.10:8000")
    with pytest.raises(SystemExit):
        lr.validate_server_url("ftp://localhost")
    with pytest.raises(SystemExit):
        lr.validate_server_url("")


# ==========================================
# 默认安全 + 配置
# ==========================================

def test_enabled_defaults_false(monkeypatch):
    config = _config(monkeypatch)
    assert config.enabled is False          # F:默认关,只心跳不领任务
    assert config.poll_seconds == 60        # A:默认 60s


def test_poll_clamp_and_fallback(monkeypatch):
    assert _config(monkeypatch, AI_OPS_LOCAL_CODEX_POLL_SECONDS="abc").poll_seconds == 60
    assert _config(monkeypatch, AI_OPS_LOCAL_CODEX_POLL_SECONDS="1").poll_seconds == 10
    assert _config(monkeypatch, AI_OPS_LOCAL_CODEX_POLL_SECONDS="9999").poll_seconds == 600


# ==========================================
# G-3:enabled=false 只心跳不 claim
# ==========================================

def test_disabled_only_heartbeat(monkeypatch):
    config = _config(monkeypatch)           # enabled 默认 false
    api = FakeApi()
    did = lr.run_once(config, api)
    assert did is False
    assert api.paths() == ["/heartbeat"]    # 没有 /claim


def test_heartbeat_payload_honest(monkeypatch):
    config = _config(monkeypatch)
    api = FakeApi()
    lr.send_heartbeat(config, api)
    path, payload = api.calls[0]
    assert path == "/heartbeat"
    assert payload["env_enabled"] is False  # 未授权如实上报(控制塔显示"在线·未授权执行")
    assert payload["ssh_runner_enabled"] is False


# ==========================================
# 断网/服务器拒绝:静默,不抛,任务留服务器队列
# ==========================================

def test_claim_network_failure_silent(monkeypatch):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true")
    api = FakeApi(claim_response=None)      # post 返 None = 网络失败/被拒
    did = lr.run_once(config, api)          # 不抛异常
    assert did is False
    assert api.paths() == ["/heartbeat", "/claim"]


def test_claim_blocked_reason_silent(monkeypatch):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true")
    api = FakeApi(claim_response={"task": None, "reason": "kill_switch_on"})
    assert lr.run_once(config, api) is False


# ==========================================
# 执行链路:mock codex → artifacts + finish(fix 带 patch)
# ==========================================

def _mock_codex(monkeypatch, diff=""):
    from services.ai_ops import codex_runner
    monkeypatch.setattr(codex_runner, "create_worktree", lambda repo, wt, ref="HEAD": wt)
    monkeypatch.setattr(codex_runner, "assert_no_secrets", lambda root: None)
    monkeypatch.setattr(codex_runner, "remove_worktree", lambda repo, wt: None)
    monkeypatch.setattr(codex_runner, "run_codex",
                        lambda *a, **kw: {"returncode": 0, "last_message": "诊断结论X", "stdout": ""})
    monkeypatch.setattr(codex_runner, "collect_git_diff", lambda wt: ("1 file changed", diff))


def test_execute_fix_uploads_patch_and_finishes(monkeypatch, tmp_path):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true",
                     AI_OPS_REPO_PATH=str(tmp_path / "repo"),
                     AI_OPS_WORKTREE_ROOT=str(tmp_path / "wt"),
                     AI_OPS_ARTIFACT_ROOT=str(tmp_path / "art"))
    _mock_codex(monkeypatch, diff="diff --git a/x b/x\n+line\n")
    api = FakeApi()
    lr.execute_task({"id": 7, "kind": "fix", "instruction": "修一下"}, "## 上下文", config, api)
    paths = api.paths()
    assert "/artifacts" in paths and "/finish" in paths and "/fail" not in paths
    finish_payload = dict(api.calls)["/finish"]
    assert finish_payload["patch_diff"].startswith("diff --git")   # patch 上传 → 服务器进审批
    assert finish_payload["task_id"] == 7


def test_execute_diagnose_no_patch(monkeypatch, tmp_path):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true",
                     AI_OPS_REPO_PATH=str(tmp_path / "repo"),
                     AI_OPS_WORKTREE_ROOT=str(tmp_path / "wt"),
                     AI_OPS_ARTIFACT_ROOT=str(tmp_path / "art"))
    _mock_codex(monkeypatch)
    api = FakeApi()
    lr.execute_task({"id": 8, "kind": "diagnose", "instruction": "查一下"}, "## 上下文", config, api)
    finish_payload = dict(api.calls)["/finish"]
    assert finish_payload["patch_diff"] == ""       # 诊断不产 patch


def test_execute_error_reports_fail(monkeypatch, tmp_path):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true",
                     AI_OPS_REPO_PATH=str(tmp_path / "repo"),
                     AI_OPS_WORKTREE_ROOT=str(tmp_path / "wt"))
    from services.ai_ops import codex_runner
    monkeypatch.setattr(codex_runner, "create_worktree",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("git 不可用")))
    api = FakeApi()
    lr.execute_task({"id": 9, "kind": "diagnose", "instruction": "x"}, "ctx", config, api)
    fail_payload = dict(api.calls)["/fail"]
    assert "git 不可用" in fail_payload["error"]


def test_missing_repo_path_fails_fast(monkeypatch):
    config = _config(monkeypatch, AI_OPS_LOCAL_CODEX_ENABLED="true")   # 无 REPO_PATH
    api = FakeApi()
    lr.execute_task({"id": 10, "kind": "fix", "instruction": "x"}, "ctx", config, api)
    assert "/fail" in api.paths()
