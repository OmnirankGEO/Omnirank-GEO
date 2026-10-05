"""工单 M-2(效果监测页闪断)源码判别锁 · 2026-07-28。

根因(git 实证 cff43fb6):URL 水合把"URL 没带 brand_id"当"选择应清空"执行
switchClient(null) —— 从侧栏进裸 /monitoring 必清已选客户 → accessScopeKey
连续翻转 → 整页反复清空重拉(闪断)。叠加 ClientContext 403 分支连带清
state + sessionStorage(grant 瞬时校验失败也清)。

行为锁(mock 403/裸 URL 进入 → 选择保持 · 无循环 · 骨架期零身份拉取)在
frontend/tests/frontend-nogo/monitoring-selection-persistence.spec.ts,
变异(回滚两处修复)已实证转红。本文件钉源码面,防换皮回退。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "index.tsx"
CTX = ROOT / "frontend" / "src" / "context" / "ClientContext.tsx"


def _hydration_effect_block(src: str) -> str:
    """URL 水合 effect 的函数体(从注释锚到依赖数组行)。"""
    start = src.index("第二条清空路径修复")
    end = src.index("switchClient, urlBrandId]", start)
    return src[start:end]


def test_bare_url_never_clears_selection():
    """URL 没带 brand_id = 无主张:水合 effect 只在 URL 点名品牌时应用 URL。

    变异(去掉 urlBrandId===null 早退,回到 switchClient(null) 清空)→ 转红。
    """
    src = MON.read_text(encoding="utf-8")
    block = _hydration_effect_block(src)
    assert "if (urlBrandId === null) return;" in block
    assert "switchClient(null" not in block  # 水合路径永不清选择
    # 应用 URL 仍在(演示分享链接 ?brand_id=X 继续生效)
    assert "switchClient(urlBrandId);" in block


def test_bare_url_adoption_writes_with_replace():
    """裸 URL 吸附当前选择用 replace(不制造历史陷阱);sidebar 主动切换保持 push。"""
    src = MON.read_text(encoding="utf-8")
    assert "{ replace: urlBrandId === null }" in src


def test_context_403_branch_keeps_selection_state():
    """403/404 分支不再清除用户已选客户(选择是 UI 状态不是凭证)。

    变异(重新加入 setCurrentBrandId(null)/sessionStorage.removeItem)→ 转红。
    """
    src = CTX.read_text(encoding="utf-8")
    start = src.index("if (status === 403 || status === 404) {")
    end = src.index("} else {", start)
    branch = src[start:end]
    assert "setCurrentBrandId" not in branch
    assert "sessionStorage.removeItem" not in branch
    assert "writePersistedDemoCase(userId, null)" not in branch  # 演示 case 持久化保留
    # 防循环闸保留:自动重选与自动加载都被挡,不构成无界请求循环
    assert "autoSelectionBlockedBrandIdRef.current = brandId;" in branch
    assert "已停止自动重试" in branch


def test_identity_panel_renders_only_after_pending_gate():
    """依赖客户上下文的身份确认卡在 pending 早退之后才可能渲染(骨架期零拉取)。"""
    src = MON.read_text(encoding="utf-8")
    pending_gate = src.index("if (activeAccessMode === 'pending') {")
    panel_render = src.index("<IdentityReviewPanel")
    assert pending_gate < panel_render
    # 渲染守卫必须带 activeBrandId(无品牌绝不挂载,也就没有 30s 轮询)
    assert "{activeBrandId && (" in src[panel_render - 120:panel_render]
