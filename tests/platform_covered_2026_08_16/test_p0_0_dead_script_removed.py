# -*- coding: utf-8 -*-
"""P0-0 拆雷:死回填脚本必须保持删除态。

[WO_MONITORING_PLATFORM_COVERED v1.2 · P0-0]

病史与引爆条件:
  `scripts/backfill_monitoring_extra_optin_2026_08_15.py` 的守卫校验
  「289/662 的 active 手动词**恰好 6 条**」—— 🔴 **校验的是目标集合,不是目标状态**。
  Owner 2026-08-16 已裁定把这 6 条关列对齐(Deploy 已执行,含审计痕迹)。
  ⇒ 一旦有人再跑一次 `--apply`,这 6 条会被**重新打开**,次日起按 130/词/天
    从服务商钱包扣费,而守卫**不会拦**(集合没变,变的是状态)。

为什么删而不是改守卫:一次性回填已完成使命,且全仓零可执行调用方
(manifest 里那处只是注释,prestart 不会跑它)。给无人依赖的死脚本升级守卫 = 给要扔的东西镀金。
判据同 A-1 删除先例。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEAD = "scripts/backfill_monitoring_extra_optin_2026_08_15.py"


def _tracked_files() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    return set(out.split())


def test_dead_script_stays_deleted():
    """🔴 复活即红:文件系统上不许有,git 也不许跟踪。"""
    assert not (ROOT / DEAD).exists(), (
        f"{DEAD} 复活了 —— 一旦有人跑它的 --apply,Owner 已关掉的 6 条手动词会被重新打开并开始扣费。"
        "要恢复这个能力请说明为什么,并先修好『校验目标集合而非目标状态』那个守卫。"
    )
    assert DEAD not in _tracked_files(), f"{DEAD} 又被 git 跟踪了"


def test_lock_has_discriminating_power():
    """判据自证:这条锁不是恒真 —— 同一把尺子量一个**确实存在**的脚本必须量得出来。"""
    alive = "scripts/prestart.py"
    assert (ROOT / alive).exists() and alive in _tracked_files(), \
        "参照物不见了,上面那条断言随时可能因为'什么都查不到'而恒绿"


def test_no_executable_caller_remains():
    """全仓不许再有**可执行**的调用方(注释/文档/本测试自身不算)。"""
    out = subprocess.run(["git", "grep", "-n", "backfill_monitoring_extra_optin", "--",
                          "*.py", "*.sh", "*.yml", "*.yaml"],
                         cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    bad = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path, _, text = line.partition(":")
        if path.startswith("tests/"):
            continue
        body = text.split(":", 1)[-1].strip()
        # 只在**注释行**里提到是允许的(tombstone 就是靠注释传达的)
        if body.startswith("#") or body.startswith("//"):
            continue
        bad.append(line)
    assert not bad, f"仍有可执行引用(不是注释):{bad}"
