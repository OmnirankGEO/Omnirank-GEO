# -*- coding: utf-8 -*-
"""[Review-CTO 2026-07-26] 演示案例选择必须活过换页与刷新（Owner 生产实测）。

症状：左上角选好演示案例 → 打开效果监测 → 左上角自己空了、页面没数据，必须再选一次。

根因：token/权限刷新会轮换 authorizationScope，把 clients 列表清空重取；这个窗口里
自动加载 effect 已经跑起来，`clientsRef` 是空的 → 解析不到该品牌的
`access_mode='demo'` → 按**真实品牌**请求演示品牌 → 403 → 403 分支连带清掉
`currentBrandId` 与 sessionStorage。

修法锁定（源码级，前端无 Python 运行时可断言）：
  1. 列表未就绪时的回退链存在：进程内保留的选择 → tab 级持久化 case_id → 真实；
  2. 成功加载后 case_id 与品牌选择同源持久化；
  3. 只在**真失效**处清持久化（换人登录 / 403·404 / 品牌不在列表），
     switchClient 的过渡性清空与 provider 卸载**不得**清持久化，否则等于没修。
"""
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "frontend/src/context/ClientContext.tsx"


@pytest.fixture(scope="module")
def src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_fallback_chain_exists_when_client_list_not_ready(src: str):
    assert "getActiveDemoSelection" in src, "未引入进程内保留选择作为回退"
    assert "readPersistedDemoCaseId" in src, "未引入 tab 级持久化 case_id 回退"
    # 回退必须发生在 resolvedAccess 里，且优先于 'real'
    idx_fallback = src.index("fallbackDemoCaseId")
    idx_real = src.index("{ mode: 'real' as const }")
    assert idx_fallback < idx_real, "回退必须排在 real 之前，否则演示品牌仍会被当真实品牌请求"


def test_case_id_persisted_on_successful_demo_load(src: str):
    assert "writePersistedDemoCase(userId, nextDemoSelection)" in src


def test_persistence_cleared_only_on_real_invalidation(src: str):
    # 三处真失效必须清
    assert src.count("writePersistedDemoCase(userId, null)") >= 3, (
        "换人登录 / 403·404 / 品牌不在列表 三处都要清持久化"
    )
    # 过渡性清空处不得清持久化：switchClient 预清 与 provider 卸载
    unmount_line = "useEffect(() => () => setActiveDemoSelection(null), [])"
    assert unmount_line in src, "卸载清理被改动，需重新评估"
    tail = src[src.index(unmount_line):src.index(unmount_line) + 200]
    assert "writePersistedDemoCase" not in tail, (
        "provider 卸载不得清持久化——那正是换页丢选择的老病根"
    )


def test_persisted_key_is_user_scoped(src: str):
    assert "demoCaseStorageKey" in src and "${SELECTION_KEY_PREFIX}demo:${userId}" in src, (
        "持久化键必须按 userId 隔离，防跨账号继承"
    )
