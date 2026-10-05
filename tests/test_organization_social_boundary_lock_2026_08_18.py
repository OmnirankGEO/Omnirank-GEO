"""员工席位社媒负向锁 —— 从前端 build 链搬到后端 pytest(并车调解 2026-08-18)。

为什么搬家(不变式一字未动,只换执行层):
  · 锁脚本 `frontend/scripts/verify-organization-social-boundary.mjs` 要读
    `services/organization_route_contract.py` 与 6 个后端 api 文件 —— 在
    frontend-builder 容器里这些文件**不存在**,留在 build 链只有两种下场:
    build 挂掉,或加 SKIP 分支变成裸奔判据(本仓存量教训:
    「引用后端文件的锁不许进前端 build 链」)。
  · 清扫包(9671d66c)的 build 链越界闸把这条规则从"教训"升成了"闸",
    并车时与 orgseats 包(bd5d1a01)的接线方式相撞 —— 两包单看都对,
    合流必须调解。闸的处方就是本文件:挪到后端 pytest,后端测试环境
    同时看得见前后端源码,语义零损失。

🔴 node 不可用 = 红,不是 skip —— skip 是隐形洞(没写 junit 的组和全绿的组
   在比较器眼里一模一样)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "frontend" / "scripts" / "verify-organization-social-boundary.mjs"
ENUM_SCRIPT = REPO_ROOT / "frontend" / "scripts" / "verify-organization-enum-labels.mjs"


def _run_lock(script: Path) -> None:
    node = shutil.which("node")
    assert node, "node 不可用 —— 环境故障必须修环境,不许当 skip 吞掉锁"
    proc = subprocess.run(
        [node, str(script)], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"{script.name} 红(rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )


def test_enum_labels_lock_script_still_exists():
    """同族第二把:枚举映射锁(读 11 个后端 org 服务文件,同理不能留 build 链)。"""
    assert ENUM_SCRIPT.is_file(), f"枚举映射锁脚本消失:{ENUM_SCRIPT}"


def test_enum_labels_lock_passes_from_repo_root():
    _run_lock(ENUM_SCRIPT)


def test_social_boundary_lock_script_still_exists():
    """锁脚本本体必须在 —— 删文件不许静默解除锁。"""
    assert SCRIPT.is_file(), f"社媒负向锁脚本消失:{SCRIPT}"


def test_social_boundary_lock_passes_from_repo_root():
    """原样执行同一把锁。红 = 社媒路由被塞进员工席位面,或解析自证失败。"""
    node = shutil.which("node")
    assert node, "node 不可用 —— 这是环境故障,必须修环境,不许当 skip 吞掉这把锁"
    proc = subprocess.run(
        [node, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"社媒负向锁红(rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
