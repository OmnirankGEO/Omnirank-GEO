# -*- coding: utf-8 -*-
"""P0-1 关账:岱林改记平台账的一次性脚本必须保持删除态。

[窗 1 收尾单 · 2026-08-20 · Owner 裁「关账删除」]

## 为什么废

`scripts/switch_dailin_to_platform_billing_2026_08_16.py`(§5 一次性脚本)三条证据:

1. **全仓零可执行调用方** —— 只有 `docs/AI-CONTEXT/DELIVERY_NO_SILENT_RELOAD_2026-08-16.md:172`
   一处 markdown 表格提到它,没有任何 `.py/.sh/.yml` 引用,也不在 prestart 迁移清单里;
2. **一行没跑过** —— 同一份交付单原话:「❌ **一行没跑**(它自己的硬顺序第 1 步就是「本包 deployed」)」;
3. **能力已被产品路径取代**(2026-08-17 R5 返修,晚于脚本):
   `server.py:9161-9176` 的幂等复用分支对 `status IN ('active','paused_low_balance')`
   的既有订阅**同时**改 `billing_mode` 与计费主体,平台账走
   `get_platform_direct_service_user_id()`;`server.py:9031-9040` 是
   admin-only + 取值白名单 + 平台账解析不到就 fail-closed。
   这与脚本的目标集合(`brand_id=592` 那两条、同一 status 集合)与效果**逐格等价**。

## 为什么是删,而不是补 `__main__`

这个文件有 `main()` 却**没有** `if __name__ == "__main__"` —— 直接跑它是
「静默 no-op + exit 0」,而它默认就是 dry-run 口气,极易被当成「dry-run 通过了」。
留着它唯一的作用就是这个陷阱;能力既已被产品路径覆盖,删掉陷阱随文件一起消失。
判据形态同本包 P0-0 先例(`test_p0_0_dead_script_removed.py`)。

🔴 岱林(brand 592)是真实客户品牌:本包**零写操作**,只删仓内文件、不碰生产数据。
   若 592 那两条订阅至今仍不是 `platform`,那是**运维待办**,走上面那条产品路径处理,
   不需要也不应该复活这个脚本。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEAD = "scripts/switch_dailin_to_platform_billing_2026_08_16.py"


def _tracked_files() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    return set(out.split())


def test_dead_script_stays_deleted():
    """🔴 复活即红:文件系统上不许有,git 也不许跟踪。"""
    assert not (ROOT / DEAD).exists(), (
        f"{DEAD} 复活了 —— 它没有 __main__,跑它是「静默 no-op + exit 0」,"
        "极易被当成 dry-run 通过。要恢复这个能力请先说明为什么产品路径"
        "(server.py:9161-9176 幂等复用分支)不够用,并补上入口与冒烟判据。"
    )
    assert DEAD not in _tracked_files(), f"{DEAD} 又被 git 跟踪了"


def test_lock_has_discriminating_power():
    """判据自证:这条锁不是恒真 —— 同一把尺子量一个**确实存在**的脚本必须量得出来。"""
    alive = "scripts/prestart.py"
    assert (ROOT / alive).exists() and alive in _tracked_files(), \
        "参照物不见了,上面那条断言随时可能因为'什么都查不到'而恒绿"


def test_no_executable_caller_remains():
    """全仓不许再有**可执行**的调用方(注释/文档/本测试自身不算)。"""
    out = subprocess.run(["git", "grep", "-n", "switch_dailin_to_platform_billing", "--",
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
        if body.startswith("#") or body.startswith("//"):
            continue
        bad.append(line)
    assert not bad, f"仍有可执行引用(不是注释):{bad}"


def test_the_replacement_product_path_is_still_there():
    """🔴 关账的前提是「能力已被产品路径覆盖」—— 那条路径没了,关账的理由就塌了。

    打的是三处承重点:admin-only 闸、平台账解析、幂等复用分支里**真的会改**
    `billing_mode`。少任何一处,删脚本就变成删掉了唯一的能力。
    """
    source = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
    assert "只有平台管理员可以把监测记到平台账上" in source, "admin-only 闸没了"
    assert "get_platform_direct_service_user_id" in source, "平台账解析没了"
    assert "update_subscription_billing_mode" in source, "改 billing_mode 的写径没了"

    db_source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8", errors="replace")
    assert "def update_subscription_billing_mode" in db_source, "数据层写径没了"
