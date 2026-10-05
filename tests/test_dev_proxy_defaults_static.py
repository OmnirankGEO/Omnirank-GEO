from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VITE_CONFIG = ROOT / "frontend" / "vite.config.ts"


def test_vite_dev_proxy_target_is_env_overridable():
    """dev 代理目标必须可由 env 覆盖(不同开发者/容器端口不同)。

    [Review-CTO 2026-07-26 修] 原断言写死 `localhost:8001` + 一句英文注释串,
    与仓库现状(`localhost:8000`)不符,交付即红——那是本机开发端口偏好,不是
    产品不变式。真正要锁的是"可覆盖"这件事本身;具体默认端口不锁死,
    避免为一个本地便利项制造假红。
    """
    src = VITE_CONFIG.read_text(encoding="utf-8")

    assert "env.VITE_DEV_PROXY_TARGET ||" in src
    assert "env.VITE_DEV_WS_TARGET ||" in src
