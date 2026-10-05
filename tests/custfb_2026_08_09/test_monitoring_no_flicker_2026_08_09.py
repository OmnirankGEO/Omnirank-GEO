"""客户反馈⑤ 机制 B · 跨页面污染("关键词表短暂清空又恢复")—— 接线锁。

链路(2026-08-09 逐段读代码确认):
  `lib/api.ts:588` 任何命中 brands/customers/my-clients 路由的**写操作成功**都广播失效
    → `ClientContext.loadClientContext(force=true)`
    → 旧写法 `clientContextCache.delete(cacheKey)` 后紧跟 `setClientContext(cached)`,
      而 delete 之后 cached 必然是 null → clientContext **被同步置空**
    → `Monitoring/index.tsx` 的 useLayoutEffect(旧判据含 activeAccessMode)看到
      live→pending 翻转 → 把关键词/发布/趋势/结果 state 全部清空
    → 数据回来再填 = 页面"跳一下"。
  07-28 修过的是 403/404 误清空那条,**200 成功写操作这条通路当时没覆盖**。

本文件是**源码接线锁**,不是行为锁 —— 这两处改动都长在 React 生命周期里,
拿不到真浏览器就没法端到端跑(交付说明里已如实标注这一点)。
所以每条锁都做成"旧形状必须消失 + 新形状必须在"的**双向**判据:
只删不加、或只加不删,都会转红。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLIENT_CONTEXT = ROOT / "frontend" / "src" / "context" / "ClientContext.tsx"
MONITORING_INDEX = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "index.tsx"
IDENTITY_PANEL = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "components" / "IdentityReviewPanel.tsx"


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """只取代码行。注释里现在写满了对旧形状的引用,扫注释必然自伤。"""
    out = []
    in_block = False
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if line.startswith("/*") or line.startswith("{/*"):
            if "*/" not in line:
                in_block = True
            continue
        if line.startswith("//") or line.startswith("*"):
            continue
        out.append((i, raw))
    return out


def _force_block(path: Path) -> str:
    """截出 `if (force) { ... }` 那一段(只在这一段里判,别的地方 delete 缓存是合法的)。"""
    src = path.read_text(encoding="utf-8")
    start = src.index("if (force) {")
    depth, i = 0, start + len("if (force) ")
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("没截到 if (force) 块 —— 结构变了,先看代码再改锁")


# ── 锁1 · force 分支不再删缓存条目(stale-while-revalidate) ────────────────
def test_force_reload_no_longer_drops_cached_context():
    block = _force_block(CLIENT_CONTEXT)
    assert "clientContextCache.delete(" not in block, (
        "force 分支仍在 delete 缓存条目 → setClientContext(null) → 监测页整页清空"
    )


def test_force_reload_still_forces_a_refetch():
    """反向对照:不能靠"把 delete 删掉"了事 —— 必须仍然强制过期,否则变成不刷新。"""
    block = _force_block(CLIENT_CONTEXT)
    assert "clientContextCacheUpdatedAt.delete(" in block, (
        "force 分支既没删缓存也没抹更新时间 → 新鲜度短路会直接返回旧数据,变成不刷新"
    )


# ── 锁2 · 清空判据改成"客户身份真变了" ────────────────────────────────────
def test_clearing_is_gated_on_client_identity_change():
    src = MONITORING_INDEX.read_text(encoding="utf-8")
    assert "clearedIdentityRef" in src and "clientIdentityKey" in src, (
        "清空判据没换成客户身份键"
    )
    # 正向:清空动作确实被包在身份变化判断里
    assert re.search(
        r"if \(clearedIdentityRef\.current !== clientIdentityKey\) \{[\s\S]{0,400}?setKeywords\(\[\]\)",
        src,
    ), "setKeywords([]) 没被包进身份变化判断 —— 清空还是会被 pending 翻转触发"


def test_generation_and_scope_still_bump_every_time():
    """反向对照:丢弃过期响应的判据**不能**跟着一起被门住,否则会串数据。"""
    src = MONITORING_INDEX.read_text(encoding="utf-8")
    guarded = re.search(
        r"if \(clearedIdentityRef\.current !== clientIdentityKey\) \{([\s\S]*?)\n        \}", src)
    assert guarded, "没截到身份变化块"
    body = guarded.group(1)
    assert "monitoringGenerationRef.current += 1" not in body, (
        "generation 自增被关进了身份变化块 —— 过期响应会写进新客户的界面"
    )
    assert "accessScopeRef.current = accessScopeKey" not in body, (
        "accessScopeRef 更新被关进了身份变化块 —— 同上"
    )


# ── 锁3 · 轮询不再无条件 setState ─────────────────────────────────────────
def test_polling_uses_equality_check_before_setstate():
    code = "\n".join(l for _, l in _code_lines(IDENTITY_PANEL))
    assert "identityItemsEqual(prev, next)" in code, "轮询仍无条件 setItems 新引用"
    # 反向对照:旧形状(直接把新数组塞进去)必须消失
    assert not re.search(r"setItems\(Array\.isArray\(payload\.items\)", code), (
        "旧的无条件 setItems 还在"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
