"""WO_REFERRAL_CHAIN_2026-08-05 · 变异自检。

三关缺一即判无效变异:锚点唯一命中 / 落盘真变 / 至少一个预期用例转红。
🔴 bytes 读写(Windows 上 read_text/write_text 翻行尾,08-04/05 两次实栽)。
🔴 SKIP(锚点≠1)不算通过,也不许标 SURVIVED。

跑法: python tests/mutation_runner_referral_chain.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

PROMO = "frontend/src/pages/Agent/PromotionCenter.tsx"
REFAPI = "api/referral_api.py"
AUTHAPI = "api/auth_api.py"
SHAREAPI = "api/share_api.py"
LOGIN = "frontend/src/pages/Login/LoginPage.tsx"
SERVER = "server.py"

PY_TESTS = "tests/test_referral_chain_2026_08_05.py"
JS_LOCK = "scripts/test-referral-wiring.mjs"

# (编号, 说明, 文件, 锚点, 替换, 跑什么)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    ("R1", "§1.2 回退:推广中心不再调 /api/referral/team(字面拆开=接线消失)",
     PROMO,
     "      const res = await authFetch('/api/referral/team');",
     "      const res = await authFetch('/api/referral/' + 'team');",
     "js"),

    ("R2", "§1.3 破坏:/team 只返 L1(与 /stats 的 l2 计数失洽)",
     REFAPI,
     "            WHERE rl.referrer_id = %s\n            ORDER BY rl.level, rl.created_at DESC",
     "            WHERE rl.referrer_id = %s AND rl.level = 1\n            ORDER BY rl.level, rl.created_at DESC",
     "py"),

    ("R3", "§2 隐私破坏:display_name 空时裸露 username(手机号)",
     AUTHAPI,
     '                name = "你的邀请人"',
     '                name = (row.get("username") or "")',
     "py"),

    ("R4", "§2.2 回退:归因 cookie 有效期回到 1 小时",
     SHAREAPI,
     "        _attribution_max_age = 7 * 24 * 3600",
     "        _attribution_max_age = 3600",
     "py"),

    ("R5", "§2 前端回退:识别到归因仍强制手填推荐码(扫码用户被空框拦死)",
     LOGIN,
     "    if (!referral.trim() && !attribution?.recognized) {",
     "    if (!referral.trim()) {",
     "py"),

    ("R6", "§3 治理抹除:未显式声明的启动告警降级消失",
     SERVER,
     '                "[L0治理] %s 未在 env 显式声明,当前按代码默认值 %s 生效 —— "',
     '                "%s 未在 env 显式声明,当前按代码默认值 %s 生效 —— "',
     "py"),

    ("R7", "§2.2 破坏:展示 cookie 变成 HttpOnly(前端读不到,回显链断)",
     SHAREAPI,
     "            \"omnirank_invite_display\",\n            code,\n            max_age=_attribution_max_age,\n            httponly=False,",
     "            \"omnirank_invite_display\",\n            code,\n            max_age=_attribution_max_age,\n            httponly=True,",
     "py"),
]


def run_py() -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header", "-x"],
                       cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return r.returncode == 0


def run_js() -> bool:
    r = subprocess.run(["node", JS_LOCK], cwd=REPO / "frontend",
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, shell=False)
    return r.returncode == 0


def main() -> int:
    results: list[tuple[str, str]] = []

    if not run_py():
        print("❌ 基线 pytest 红,变异结果无意义 — 先修基线")
        return 2
    if not run_js():
        print("❌ 基线 js 接线锁红,变异结果无意义 — 先修基线")
        return 2
    print("✅ 基线双绿(pytest + js 接线锁)\n")

    for mid, desc, rel, old, new, kind in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
        hits = original.count(old_b)
        if hits != 1:
            results.append((mid, f"SKIP(锚点命中 {hits} ≠ 1)"))
            print(f"⚠️  {mid} SKIP:锚点命中 {hits} 次 · {desc}")
            continue
        mutated = original.replace(old_b, new_b)
        try:
            path.write_bytes(mutated)
            green = run_py() if kind == "py" else run_js()
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print(f"🔴 {mid} 还原失败!{rel} 与原文不一致,人工介入")
            return 3
        if green:
            results.append((mid, "SURVIVED"))
            print(f"❌ {mid} SURVIVED(变异后仍绿,锁抓不到):{desc}")
        else:
            results.append((mid, "KILLED"))
            print(f"✅ {mid} KILLED:{desc}")

    killed = sum(1 for _, s in results if s == "KILLED")
    survived = [m for m, s in results if s == "SURVIVED"]
    skipped = [m for m, s in results if s.startswith("SKIP")]
    print(f"\n==== 变异结果:{killed} killed / {len(survived)} survived / {len(skipped)} skip ====")
    if survived or skipped:
        if survived:
            print("SURVIVED:", ", ".join(survived))
        if skipped:
            print("SKIP(锚点问题,不算通过):", ", ".join(skipped))
        return 1
    print("✅ 全部变异被击杀,锁有判别力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
