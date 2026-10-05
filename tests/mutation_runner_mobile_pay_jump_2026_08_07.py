"""变异检验 · WO_MOBILE_PAY_JUMP 2026-08-07(移动端支付点了没反应)。

每条变异 = 「有人把这个修改回去/改坏」的一种具体写法。锁必须**当场转红**。
全部 KILLED 才算锁有判别力;任何一条 SURVIVED = 那条锁是摆设,必须补硬。

跑法:
    TEST_DATABASE_URL=postgresql://... python tests/mutation_runner_mobile_pay_jump_2026_08_07.py
    (锁本身不碰库,TEST_DATABASE_URL 只是 conftest 的入场券,给个 throwaway 串即可)

🔴 三条自坏防线(沿用 mention_variant runner 的做法,都是实证踩过的坑):
  1. ``.pyc`` 缓存会把「已杀死」误报成「存活」→ 每次跑前清 ``__pycache__``
     并 ``-p no:cacheprovider``;
  2. **锚点先自检**:锚字符串出现次数 ≠ 1 就报 ANCHOR_BAD 并整体失败 ——
     锚点抓不到 = 变异根本没落盘,"全部 KILLED" 是假的;
  3. **SKIP ≠ SURVIVED ≠ KILLED**:退出码 5 单独报 NO_TESTS,不许算成杀死。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_SUITE = "tests/test_mobile_pay_jump_2026_08_07.py"

ROUTING = "services/payment_routing.py"
PAY_ENV = "frontend/src/lib/paymentEnv.ts"
TELEMETRY = "frontend/src/lib/paymentTelemetry.ts"
ANALYTICS = "frontend/src/lib/analytics.ts"
INVENTORY = "frontend/src/pages/Agent/InventoryCenter.tsx"
BUYCREDIT = "frontend/src/pages/Customer/BuyCredit.tsx"

#: (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── §2.2 桌面微信路由 ────────────────────────────────────────────────
    (
        "M01", "把桌面微信这条整个删掉(退回 08-07 事故形态:Mac 微信又走 jsapi)",
        ROUTING,
        "    if is_desktop_wechat_browser(user_agent):\n        return WECHAT_NATIVE\n",
        "",
    ),
    (
        "M02", "顺序调反(桌面微信先被 is_wechat_browser 捞走 = 修了等于没修)",
        ROUTING,
        "    if is_desktop_wechat_browser(user_agent):\n        return WECHAT_NATIVE\n"
        "    if is_wechat_browser(user_agent):\n        return WECHAT_JSAPI\n",
        "    if is_wechat_browser(user_agent):\n        return WECHAT_JSAPI\n"
        "    if is_desktop_wechat_browser(user_agent):\n        return WECHAT_NATIVE\n",
    ),
    (
        "M03", "只留 windowswechat(Mac 微信 —— 正是案例 A 那台 —— 重新漏检)",
        ROUTING,
        '    "windowswechat",\n    "macwechat",\n',
        '    "windowswechat",\n',
    ),
    (
        "M04", "桌面判据退化成「只要是微信就算桌面」(移动微信被误伤,违 §3 铁律)",
        ROUTING,
        "    return any(token in ua for token in DESKTOP_WECHAT_UA_TOKENS)",
        '    return "micromessenger" in ua',
    ),
    (
        "M05", "桌面微信改判成 xunhupay(私账链接送进微信内 · 且不是工单要的扫码)",
        ROUTING,
        "    if is_desktop_wechat_browser(user_agent):\n        return WECHAT_NATIVE\n",
        "    if is_desktop_wechat_browser(user_agent):\n        return XUNHUPAY\n",
    ),
    (
        "M06", "把「微信内不许显式点第三方支付链接」那道闸跟着放开",
        ROUTING,
        "    if requested == XUNHUPAY and is_wechat_browser(user_agent):",
        "    if requested == XUNHUPAY and is_mobile_wechat_browser(user_agent):",
    ),
    # [开源 E3 · B4 · 2026-09-28] M06b / M06c 退役:两条都改订阅支付模块的防御断言,该模块随订阅产品面整文件删除
    # ── 跨语言镜像 ───────────────────────────────────────────────────────
    (
        "M07", "前端 token 表偷偷改一个(前后端判据打架 · 界面自相矛盾)",
        PAY_ENV,
        "export const DESKTOP_WECHAT_UA_TOKENS = ['windowswechat', 'macwechat'] as const;",
        "export const DESKTOP_WECHAT_UA_TOKENS = ['windowswechat'] as const;",
    ),
    (
        "M08", "前端 isMobileUa 不再先摘桌面微信",
        PAY_ENV,
        "  if (isDesktopWechatUa(ua)) return false;\n",
        "",
    ),
    # ── §2.3 移动端同窗跳 ────────────────────────────────────────────────
    (
        "M09", "进货弹窗把 target=\"_blank\" 写回去(案例 B 的形态)",
        INVENTORY,
        "              {...payLinkTargetProps()}\n",
        '              target="_blank" rel="noreferrer"\n',
    ),
    (
        "M10", "C 端弹窗把 target=\"_blank\" 写回去",
        BUYCREDIT,
        "                {...payLinkTargetProps()}\n",
        '                target="_blank" rel="noreferrer"\n',
    ),
    # ── §2.1 三个埋点 ────────────────────────────────────────────────────
    (
        "M11", "clicked 改回 debounce 上报(跳走之后队列没机会 flush = 埋了等于没埋)",
        TELEMETRY,
        "  try { trackEventNow(PAY_LINK_CLICKED, payload(ctx, extra)); } catch",
        "  try { trackEvent(PAY_LINK_CLICKED, payload(ctx, extra)); } catch",
    ),
    (
        "M12", "left 改回 debounce 上报",
        TELEMETRY,
        "  try { trackEventNow(PAY_JUMP_LEFT_PAGE, payload(ctx, extra)); } catch",
        "  try { trackEvent(PAY_JUMP_LEFT_PAGE, payload(ctx, extra)); } catch",
    ),
    (
        "M13", "离页只听 pagehide 不听 visibilitychange 也不听 pagehide(只留 beforeunload)",
        TELEMETRY,
        "    window.addEventListener('pagehide', onPageHide);",
        "    window.addEventListener('beforeunload', onPageHide);",
    ),
    (
        "M14", "把 shown 埋点从进货弹窗摘掉",
        INVENTORY,
        "    trackPayDialogShown({\n      surface: 'agent_inventory',",
        "    void ({\n      surface: 'agent_inventory',",
    ),
    (
        "M15", "点击时不再 arm(clicked 有、left 永远不发 → 分不清「没点」和「跳被拦」)",
        INVENTORY,
        "                armJumpTracking();\n",
        "",
    ),
    (
        "M16", "flush 退回「先 splice 再判 token」(拿不到 token 时整批被静默吞掉)",
        ANALYTICS,
        "    const token = getConfirmedSessionToken() || '';\n"
        "    if (!token) return;\n"
        "    const batch = EVENTS_QUEUE.splice(0, FLUSH_MAX_BATCH);\n",
        "    const batch = EVENTS_QUEUE.splice(0, FLUSH_MAX_BATCH);\n"
        "    const token = getConfirmedSessionToken() || '';\n"
        "    if (!token) return;\n",
    ),
    # ── §2.4 移动视口可点性 ──────────────────────────────────────────────
    (
        "M17", "拿掉支付弹窗的高度上限(内容高过手机视口时按钮滚不到 = 点不着)",
        BUYCREDIT,
        '<DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">',
        "<DialogContent>",
    ),
    (
        "M18", "只留 max-h 不留 overflow(溢出照样滚不动)",
        INVENTORY,
        '<DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">',
        '<DialogContent className="max-h-[calc(100dvh-2rem)]">',
    ),
    # ── §3 必须不命中(反向铁律)────────────────────────────────────────
    (
        "M19", "动了 C 端微信内自动跳:改成 window.open(微信内必被拦)",
        BUYCREDIT,
        "      window.location.href = info.payment_url_mobile!;\n    }, 1000);",
        "      window.open(info.payment_url_mobile!);\n    }, 1000);",
    ),
    (
        "M20", "删掉微信内自动跳的防循环标记(用户跳回来会被反复踢走)",
        BUYCREDIT,
        "    sessionStorage.setItem(jumpedKey, '1');\n",
        "",
    ),
]


def _purge_pycache() -> None:
    """清 ``__pycache__`` —— 不清会把「已杀死」误报成「存活」(单向偏差)。"""
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    """跑锁套件;返回 KILLED / SURVIVED / NO_TESTS。"""
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", LOCK_SUITE],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL(conftest 的入场券 · 本锁不碰库)")
        return 2

    print("── 基线自检:未变异时锁必须全绿 ──")
    baseline = _run_locks()
    if baseline != "SURVIVED":
        print(f"🔴 基线就不是绿的({baseline}) → 变异结果无意义,先修基线")
        return 2
    print("   ✅ 基线全绿\n")

    results: list[tuple[str, str, str]] = []
    for code, note, rel_path, anchor, replacement in MUTATIONS:
        target = ROOT / rel_path
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(anchor)
        if occurrences != 1:
            results.append((code, "ANCHOR_BAD", f"{note}(锚点命中 {occurrences} 次,应为 1)"))
            print(f"{code}  🔴 ANCHOR_BAD  {note}(命中 {occurrences} 次)")
            continue
        try:
            # 🔴 newline="" —— write_text 会按平台改行尾,整份写成 CRLF 会让后续锚点全失配
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(original.replace(anchor, replacement))
            verdict = _run_locks()
        finally:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(original)
        icon = {"KILLED": "✅", "SURVIVED": "🔴", "NO_TESTS": "🔴"}[verdict]
        results.append((code, verdict, note))
        print(f"{code}  {icon} {verdict:9} {note}")

    _purge_pycache()
    killed = sum(1 for _, v, _ in results if v == "KILLED")
    total = len(results)
    print(f"\n变异 {total} 条 · 杀死 {killed} · 存活/异常 {total - killed}")
    bad = [(c, v, n) for c, v, n in results if v != "KILLED"]
    if bad:
        print("🔴 下列变异未被任何锁抓住 —— 锁是摆设,必须补硬:")
        for code, verdict, note in bad:
            print(f"   {code} [{verdict}] {note}")
        return 1
    print("✅ 全部杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
