"""A 类判据的 **pytest 入口** —— 让浏览器那族判据能被 `--collect-only` 收到。

真正的断言在 `frontend/tests/plan-error-upstream.spec.cjs`(真浏览器 + 真 Vite 应用),
这里只负责把它挂进 pytest 的分母。仓里既有范式(飞轮包)是"spec 用 npx 单独跑",
于是那族判据**不在 pytest 的分母里** —— 谁不知道有这么个配置文件,就永远不会跑它。
本文件补上这一步。

## 环境不具备时:**红,不 skip**

`frontend/node_modules` 没装时本用例**失败**而不是 skip。理由:
skip 在汇总里长得像"没问题",而它其实是"没验" —— 这两件事的处置完全不同。
代价是"跑不起来"与"被测对象坏了"的退出码同形,所以失败信息里
**明写是哪一种**,让读报文的人一眼分得开。

CI 上没有 node/浏览器的作业请用 `-m "not browser"` **显式**排除,
让"我们决定不跑它"留在命令行里,而不是藏在一个静默的 skip 里。

🔴 本仓 `frontend` 有一处**预存**的 peer 依赖冲突(`echarts-gl` 要 echarts@^5,
   而项目是 ^6),`npm ci` 直接失败 ⇒ 装依赖要 `npm ci --legacy-peer-deps`。
   这不是本单引入的,也不在本单修复范围内,记在这里免得下一个人以为是判据坏了。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
CONFIG = "playwright.plan-error-upstream.config.cjs"

pytestmark = pytest.mark.browser


def _preflight() -> list[str]:
    """跑不起来的原因清单。空 = 环境齐备。"""
    missing: list[str] = []
    if shutil.which("npx") is None:
        missing.append("npx 不在 PATH(需要 Node 20+)")
    if not (FRONTEND / "node_modules" / "playwright").is_dir():
        missing.append("frontend/node_modules/playwright 不存在"
                       "(先跑 `npm --prefix frontend ci --legacy-peer-deps`)")
    if not (FRONTEND / CONFIG).is_file():
        missing.append(f"{CONFIG} 不存在")
    if not (FRONTEND / "tests" / "plan-error-upstream.spec.cjs").is_file():
        missing.append("spec 文件不存在")
    return missing


def test_plan_error_never_appears_on_normal_paths():
    """黄框在正常路径上不出现 + 撤销可逆 + 反向对照 + 服务端仍拒时黄框带按钮。

    五条断言全在 spec 里;这里只跑它并把失败原文带回来。
    """
    missing = _preflight()
    if missing:
        pytest.fail(
            "【环境不具备,不是被测对象坏了】浏览器判据没能运行:\n  - "
            + "\n  - ".join(missing)
            + "\n(这条是**红**不是 skip:skip 在汇总里长得像没问题,"
              "而它其实是'没验'。要显式不跑请用 -m \"not browser\"。)")

    env = {**os.environ, "CI": "1"}
    proc = subprocess.run(
        ["npx", "playwright", "test", "--config", CONFIG],
        cwd=FRONTEND, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900, shell=(os.name == "nt"),
    )
    assert proc.returncode == 0, (
        "【被测对象红了】playwright 判据未通过(退出码 "
        f"{proc.returncode}):\n{proc.stdout[-6000:]}\n{proc.stderr[-2000:]}")
