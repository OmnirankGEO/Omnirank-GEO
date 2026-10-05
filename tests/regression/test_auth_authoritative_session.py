from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_token_rotation_uses_authoritative_me_and_callers_await_it():
    context = (ROOT / "frontend/src/context/AuthContext.tsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")
    start = context.index("const updateToken = useCallback")
    end = context.index("// 权限检查", start)
    update_block = context[start:end]

    assert "updateToken: (newToken: string) => Promise<void>" in context
    assert "await establishAuthoritativeSession(newToken)" in update_block
    assert "saveToken(newToken)" not in update_block
    assert "setToken(newToken)" not in update_block

    assert "ce.detail.authoritativeReady = authoritativeReady" in context
    assert "if (!refreshDetail.authoritativeReady)" in api
    assert "const authoritativeReady = refreshDetail.authoritativeReady" in api
    assert "await authoritativeReady" in api
    assert "if (localStorage.getItem(TOKEN_KEY) !== d.token)" in api

    password_page = (
        ROOT / "frontend/src/pages/Login/ChangePasswordPage.tsx"
    ).read_text(encoding="utf-8")
    assert "await updateToken(res.data.token)" in password_page
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 第二处调用方(社媒工作台设置页的改密弹窗)随宿主整删
