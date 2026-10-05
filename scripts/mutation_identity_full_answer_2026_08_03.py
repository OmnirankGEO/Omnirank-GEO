#!/usr/bin/env python3
"""[工单 2026-08-03 ①] 变异自检 —— 证明那几条锁真能抓到本 bug。

规矩(SSOT §6.1.5 / 工具箱 README):**每个"必须命中"都要配一个"必须不命中"**。
所以本脚本先跑一次未变异的基线,要求全绿;再逐个注入变异,要求**指定的锁转红**。
基线不绿 → 后面的红说明不了任何事,直接中止。

跑法:python scripts/mutation_identity_full_answer_2026_08_03.py
退出码:0=全部变异都被抓到 · 1=有变异存活(锁没有判别力)
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 🔴 Windows 控制台默认 GBK:子进程输出里的中文/emoji 会让 subprocess 解码抛异常,
#    自己 print 的 ✅ 也编码不出去。两头都钉成 UTF-8,否则脚本还没跑到判定就先崩。
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "frontend/src/pages/Monitoring/components/IdentityReviewPanel.tsx"
ANCHOR = ROOT / "db/monitoring_db.py"

PY_ENV = {**os.environ, "TEST_DATABASE_URL": "postgresql://noconnect:noconnect@127.0.0.1:5499/geo_agentscope_test"}


def run_backend() -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_identity_full_answer_anchor_2026_08_03.py", "-q"],
        cwd=ROOT, env=PY_ENV, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


def run_frontend() -> bool:
    # 🔴 Windows 上 node_modules/.bin/playwright 是 sh 脚本,CreateProcess 起不来
    #    (WinError 193)。要用 .cmd 那份。
    base = ROOT / "frontend/node_modules/.bin"
    exe = base / ("playwright.cmd" if sys.platform == "win32" else "playwright")
    if not exe.exists():
        raise RuntimeError(f"playwright 可执行文件不存在:{exe}")
    r = subprocess.run(
        [str(exe), "test", "--config", "playwright.identity-full-answer.config.ts"],
        cwd=ROOT / "frontend", capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    )
    return r.returncode == 0


# (名字, 目标文件, 原文, 替换成, 用哪层锁判, 说明)
MUTATIONS = [
    (
        "M1 拿掉展开入口",
        PANEL,
        'data-testid={`toggle-full-answer-${item.id}`}',
        'data-testid={`toggle-full-answer-REMOVED-${item.id}`}',
        run_frontend,
        "入口没了 → 锁1/2/3/4/5/6/7 全该红",
    ),
    (
        "M2 高亮定位错段(锚点固定落在开头)",
        PANEL,
        "const start = full.anchor !== 'none' && typeof full.start === 'number' ? full.start : null;",
        "const start = full.anchor !== 'none' && typeof full.start === 'number' ? 0 : null;",
        run_frontend,
        "定位到开头 → 锁2「锚点前正好是那 500 字」该红",
    ),
    (
        "M3 收起态改成 CSS 藏(全文一直挂在 DOM 里)",
        PANEL,
        "{expandedIds[item.id] && fullAnswers[item.id] ? (() => {",
        "{fullAnswers[item.id] ? (() => {",
        run_frontend,
        "锁6「收起后从 DOM 消失」该红",
    ),
    (
        "M6 越界修复回退(圆角卡改回 border-y 全出血带)",
        PANEL,
        'className="mb-5 min-w-0 overflow-hidden rounded-xl border border-amber-500/30 bg-amber-500/5 p-4"',
        'className="mb-5 min-w-0 overflow-hidden border-y border-amber-500/30 bg-amber-500/5 px-4 py-4"',
        run_frontend,
        "锁8「左右边框宽度 > 0」该红",
    ),
    (
        "M4 兜底吃掉证据窗口(窗口有值也走 seen_prefix)",
        ANCHOR,
        '    window = str(evidence_snippet or "")\n    if window:',
        '    window = str(evidence_snippet or "")\n    if False and window:',
        run_backend,
        "锁 test_evidence_window_wins_when_present 该红",
    ),
    (
        "M5 片段非前缀时硬造一个边界",
        ANCHOR,
        "    if seen and full.startswith(seen) and len(seen) < len(full):",
        "    if seen and len(seen) < len(full):",
        run_backend,
        "锁 test_snippet_not_a_prefix_yields_no_anchor 该红",
    ),
]


def main() -> int:
    print("=== 基线(未变异)必须全绿,否则后面的红没有意义 ===")
    be, fe = run_backend(), run_frontend()
    print(f"  后端锁 {'✅' if be else '🔴'} · 前端锁 {'✅' if fe else '🔴'}")
    if not (be and fe):
        print("🔴 基线不绿,中止 —— 先修锁本身")
        return 1

    survived = []
    for name, path, old, new, runner, why in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"🔴 {name}:锚点串在源码里找不到(实现改过名?)——变异无效,当作失败")
            survived.append(name)
            continue
        backup = tempfile.mktemp(suffix=".bak")
        shutil.copyfile(path, backup)
        try:
            path.write_text(src.replace(old, new, 1), encoding="utf-8")
            green = runner()
            if green:
                print(f"🔴 {name} 存活(锁仍全绿)—— {why}")
                survived.append(name)
            else:
                print(f"✅ {name} 被抓到 —— {why}")
        finally:
            shutil.copyfile(backup, path)
            os.unlink(backup)

    print()
    if survived:
        print(f"🔴 {len(survived)}/{len(MUTATIONS)} 个变异存活:{survived}")
        return 1
    print(f"✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全杀,锁有判别力")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
