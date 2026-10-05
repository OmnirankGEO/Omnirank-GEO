"""[BUG-3 · 2026-07-27] 会话确认失败不得再放大成整页白屏 —— 8 个调用点的契约

行为层的三态判别测试是可执行的：`frontend/scripts/test-authoritative-session-failmode.mjs`
（esbuild 转译后真跑，31 条断言，已挂进 `npm run build`）。
本文件守的是**调用点覆盖**：光改 lib 不看调用方，就是"后端修了、前端命中 0"那种假修复。

事故根因：`requireConfirmedSessionToken()` 在 token 未被 /api/auth/me 确认时直接 throw
→ 冒泡到 ErrorBoundary → 整页白屏；客户门户（PortalDashboard）表现为"数据全丢失"。

🔴 安全红线（本文件必须守住）：禁止 fail-open —— 任何调用点都不得在未确认时使用 token。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"


def _read(rel: str) -> str:
    return (FE / rel).read_text(encoding="utf-8")


# 工单 §3.5 列的 8 个调用点（`lib/api.ts` 占 3 处）
ASYNC_CALL_SITES = [
    "lib/api.ts",
    "context/AuthContext.tsx",
    "lib/v35w2Api.ts",
    "pages/Diagnosis/DiagnosisProgress.tsx",
    "pages/Diagnosis/NewDiagnosis.tsx",
    "pages/Portal/PortalDashboard.tsx",
    "services/m3/signalsPriority.ts",
]


@pytest.mark.parametrize("rel", ASYNC_CALL_SITES)
def test_call_sites_await_instead_of_throwing(rel):
    """每个调用点都改成等待版；同步版 require 不得残留在业务调用点。"""
    src = _read(rel)
    assert "awaitConfirmedSessionToken" in src or "peekConfirmedSessionToken" in src, \
        f"{rel} 未接入等待/只读版,会话在途仍会抛错炸页面"
    assert "requireConfirmedSessionToken" not in src, \
        f"{rel} 仍在用会抛错的同步版"


@pytest.mark.parametrize("rel", ASYNC_CALL_SITES)
def test_await_calls_are_actually_awaited(rel):
    """`awaitConfirmedSessionToken()` 必须被 await —— 漏 await 会拿到 Promise 当 token。"""
    src = _read(rel)
    for m in re.finditer(r"awaitConfirmedSessionToken\(\)", src):
        head = src[max(0, m.start() - 40):m.start()]
        assert "await" in head, f"{rel} 有一处 awaitConfirmedSessionToken() 没 await"


def test_portal_dashboard_token_link_path_unchanged():
    """走 token 链接的客户(有 portal_token)本来就不受影响,这条路径不能被改坏。"""
    src = _read("pages/Portal/PortalDashboard.tsx")
    assert "localStorage.getItem('portal_token') ||" in src


def test_diagnosis_progress_falls_back_to_polling():
    """WS 连接前等确认;确认不了要落到轮询,而不是让页面崩。"""
    src = _read("pages/Diagnosis/DiagnosisProgress.tsx")
    assert "const connectWs = async () =>" in src
    ws = src[src.index("const connectWs = async () =>"):]
    ws = ws[:ws.index("ws.onopen")]
    assert "startPolling();" in ws, "确认失败时必须有轮询兜底出口"


# ============================================================
# 🔴 fail-open 安全锁
# ============================================================

def test_session_lib_never_returns_unconfirmed_token():
    """lib 层:两个取 token 的出口都必须比对 confirmedToken 后才返回。"""
    src = _read("lib/authoritativeSession.ts")

    def body(fn: str) -> str:
        start = src.index(f"export function {fn}(") if f"export function {fn}(" in src \
            else src.index(f"export async function {fn}(")
        rest = src[start:]
        return rest[:rest.index("\n}\n") + 2]

    for fn in ("requireConfirmedSessionToken", "awaitConfirmedSessionToken", "peekConfirmedSessionToken"):
        b = body(fn)
        # 每个出口都必须有"与 confirmedToken 比对"的守卫，缺了就是 fail-open
        assert "confirmedToken" in b, f"{fn} 缺少 confirmedToken 守卫（fail-open）"
        # 返回 token 的语句之前必须先出现该守卫，不能"等到了就 return stored"
        for m in re.finditer(r"return (stored|settled);", b):
            assert "confirmedToken" in b[:m.start()], \
                f"{fn} 有一处 return 未经 confirmedToken 守卫 —— 这是 fail-open"


def test_three_phase_state_machine_exists():
    src = _read("lib/authoritativeSession.ts")
    for phase in ("'pending'", "'confirmed'", "'failed_unauthorized'", "'failed_unreachable'", "'anonymous'"):
        assert phase in src, f"缺少确认状态 {phase}"
    assert "AuthoritativeSessionUnavailableError" in src
    assert "waitForSessionConfirmation" in src


def test_retry_constants_are_named_not_magic():
    """重试次数/间隔写成常量(工单 §3.3),AuthContext 引用同一份,防两处口径漂移。"""
    lib = _read("lib/authoritativeSession.ts")
    assert "SESSION_CONFIRM_RETRY_BACKOFF_MS = 2000" in lib
    assert "SESSION_CONFIRM_WAIT_TIMEOUT_MS" in lib
    auth = _read("context/AuthContext.tsx")
    assert "SESSION_CONFIRM_RETRY_BACKOFF_MS" in auth, "AuthContext 仍在用魔法数 2000"
    assert "setTimeout(resolve, 2000)" not in auth


def test_auth_context_marks_all_three_outcomes():
    """401 → unauthorized;超时/5xx → unreachable;起飞 → pending。三条都要打标。"""
    src = _read("context/AuthContext.tsx")
    assert src.count("markSessionConfirmationFailed('unauthorized')") >= 3, \
        "硬 401 / 二次确认 401 / success=false 三条清 token 路径都要标 unauthorized"
    assert src.count("markSessionConfirmationFailed('unreachable')") >= 2, \
        "退避重试耗尽 与 裸 401 刷新后仍失败 两条软失败路径都要标 unreachable"
    assert "markSessionConfirmationPending()" in src, "/me 起飞要标 pending(retryAuth 重试尤其依赖)"


def test_error_boundary_gives_recoverable_exit_not_stack_trace():
    """最后一道防线:可恢复的会话错误要给人话 + 可操作出口,不是 stack trace。"""
    src = (FE / "App.tsx").read_text(encoding="utf-8")
    assert "isRecoverableSessionError" in src
    idx = src.index("isRecoverableSessionError(this.state.error)")
    # 只取这个 if 分支本身（到常规错误分支开始为止），别越界到下面的 stack trace 渲染
    branch = src[idx:src.index("const errMsg", idx)]
    assert "重试" in branch and "重新登录" in branch, "必须有用户可达的出口"
    assert "stack trace" not in branch.lower(), "可恢复分支不得暴露 stack trace"
    assert "errStack" not in branch and "compStack" not in branch, \
        "可恢复分支不得渲染 error stack / component stack"


def test_executable_three_phase_suite_is_wired_into_build():
    """判别测试必须挂进 build,否则复检方在合并树上跑不到。"""
    pkg = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    assert "test-authoritative-session-failmode.mjs" in pkg
    assert (ROOT / "frontend" / "scripts" / "test-authoritative-session-failmode.mjs").exists()
