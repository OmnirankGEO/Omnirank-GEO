"""变异检验 · 证据检索供应商熔断(WO 熔断单 2026-08-08)。

每条变异 = 「有人把这个改回去/改坏」的一种具体写法。锁必须**当场转红**。
存活的先分诊「锁写松了」还是「变异是空操作」,两者修法不同。

跑法(需要 TEST_DATABASE_URL 指向 throwaway PG,与锁套件同一个):
    python tests/mutation_runner_provider_breaker_2026_08_08.py

自坏防线(都是本周实证踩过的坑,写进 runner 而不是靠人记得):
  1. 每次跑前清 ``__pycache__`` + ``-p no:cacheprovider``(缓存会把 KILLED 误报成 SURVIVED);
  2. 锚点出现次数 ≠ 1 → ANCHOR_BAD 并整体失败(抓不到 = 变异没落盘);
  3. pytest 退出码 5 单独报 NO_TESTS,不许算 KILLED(那是判据没跑);
  4. 写回用原始 bytes 逐字还原,不经行尾转换。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
LOCK_SUITE = "tests/test_evidence_provider_circuit_breaker_2026_08_08.py"

BREAKER = "tools/search/provider_circuit_breaker.py"
METASO = "tools/search/metaso_mcp.py"
DOUBAO = "tools/search/doubao_search.py"

#: (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── 闸本体 ────────────────────────────────────────────────────────────
    (
        "M01", "闸恒放行(= 完全没做这个包 · 454 笔那种形状回来)",
        BREAKER,
        "        tripped, _reason = st[\"counter\"].is_tripped()\n        if not tripped:\n            return False, None\n",
        "        tripped, _reason = st[\"counter\"].is_tripped()\n        if True:\n            return False, None\n",
    ),
    (
        "M02", "阈值 5 → 50(悄悄放大到烧 10 倍才停)",
        BREAKER,
        "CONSECUTIVE_UNDELIVERED_THRESHOLD = 5",
        "CONSECUTIVE_UNDELIVERED_THRESHOLD = 50",
    ),
    (
        "M03", "熔断改成全局共享(不再按供应商 —— 秘塔挂了把豆包也停掉)",
        BREAKER,
        "def _slot(provider: str) -> dict:\n    st = _state.get(provider)\n",
        "def _slot(provider: str) -> dict:\n    provider = \"__all__\"\n    st = _state.get(provider)\n",
    ),
    (
        "M04", "送达不再清零(抖一下就把供应商停到进程结束)",
        BREAKER,
        "        st[\"counter\"].record_success()\n        st[\"opened_at\"] = None\n        st[\"probing\"] = False\n",
        "        st[\"opened_at\"] = None\n        st[\"probing\"] = False\n",
    ),
    (
        "M05", "半开不限一个探针(并发一次漏一批)",
        BREAKER,
        "        if st[\"probing\"]:\n            return True, \"open_probe_in_flight\"\n        st[\"probing\"] = True\n",
        "        st[\"probing\"] = True\n",
    ),
    (
        "M06", "探针失败不重新计时(退化成不限速重试)",
        BREAKER,
        "            if not was_tripped or st[\"probing\"]:\n                st[\"opened_at\"] = time.monotonic()\n",
        "            if not was_tripped:\n                st[\"opened_at\"] = time.monotonic()\n",
    ),
    (
        "M07", "把失败率那条臂放开(业务级失败也能停供应商 —— 用错判据)",
        BREAKER,
        "_RATE_ARM_DISABLED_MIN_PROCESSED = 10 ** 9",
        "_RATE_ARM_DISABLED_MIN_PROCESSED = 100",
    ),
    (
        "M08", "未送达判据恒真(把 JSON 解析错、超时读错这类**已送达**的也算上)",
        BREAKER,
        "    types = undelivered_error_types()\n    return bool(types) and isinstance(exc, types)\n",
        "    return True\n",
    ),
    (
        "M09", "不发告警(闸生效了但没人知道 —— 工单要的是熔断**加**告警)",
        BREAKER,
        "    if just_tripped:\n        logger.error(",
        "    if False:\n        logger.error(",
    ),
    (
        "M10", "恢复后不收告警(开/合闭环断一半,告警永远挂着)",
        BREAKER,
        "    if resolve:\n        logger.warning(\"[ProviderBreaker] %s 已恢复送达,合闸\", provider)\n        _resolve_alert(provider)\n",
        "    if False:\n        logger.warning(\"[ProviderBreaker] %s 已恢复送达,合闸\", provider)\n        _resolve_alert(provider)\n",
    ),
    (
        "M11", "告警通道故障不再兜(告警落库失败把主链一起带下水)",
        BREAKER,
        "    except Exception as exc:\n        # 告警通道自身故障不能再吞 —— 至少留 error 日志(日志是最后一道人眼面)\n        logger.error(\"[ProviderBreaker] 告警落库失败: %s\", exc)\n",
        "    except Exception as exc:\n        raise\n",
    ),
    # ── 接线 ──────────────────────────────────────────────────────────────
    (
        "M12", "秘塔侧摘掉闸(函数在,接线断 —— 本周最常见的形状)",
        METASO,
        "        _skip, _skip_reason = should_skip(PROVIDER_METASO)\n        if _skip:\n",
        "        _skip, _skip_reason = (False, None)\n        if _skip:\n",
    ),
    (
        "M13", "豆包侧摘掉闸",
        DOUBAO,
        "        _skip, _skip_reason = should_skip(PROVIDER_DOUBAO)\n        if _skip:\n",
        "        _skip, _skip_reason = (False, None)\n        if _skip:\n",
    ),
    (
        "M14", "秘塔侧把闸挪到重试循环**外**(第 2 次重试照样烧钱)",
        METASO,
        "    for attempt in range(max_retries):\n        # [WO 熔断单 2026-08-08]",
        "    for attempt in range(max_retries):\n      if attempt == 0:\n        # [WO 熔断单 2026-08-08]",
    ),
    (
        "M15", "秘塔侧不记未送达(计数器永远是 0,闸永远不跳)",
        METASO,
        "            if is_undelivered(e):\n                record_undelivered(PROVIDER_METASO, f\"{type(e).__name__}: {str(e)[:120]}\")\n",
        "            if False:\n                record_undelivered(PROVIDER_METASO, f\"{type(e).__name__}: {str(e)[:120]}\")\n",
    ),
    (
        "M16", "秘塔侧把「已送达」也记成未送达(HTTP 500 会把供应商停掉)",
        METASO,
        "            if is_undelivered(e):\n",
        "            if True:\n",
    ),
    (
        "M17", "秘塔侧不记送达(合闸这条路断了,恢复不了)",
        METASO,
        "                    record_delivered(PROVIDER_METASO)\n",
        "                    pass\n",
    ),
    (
        "M18", "豆包侧不记未送达",
        DOUBAO,
        "            if is_undelivered(exc):\n                record_undelivered(PROVIDER_DOUBAO, last_error)\n",
        "            if False:\n                record_undelivered(PROVIDER_DOUBAO, last_error)\n",
    ),
]


def _purge_pycache() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
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
    print("── 基线自检:未变异时锁必须全绿 ──")
    baseline = _run_locks()
    if baseline != "SURVIVED":
        print(f"[ERR ] 基线就不是绿的({baseline}) → 变异结果无意义,先修基线")
        return 2
    print("   OK 基线全绿\n")

    killed = 0
    bad = 0
    for code, note, rel_path, anchor, replacement in MUTATIONS:
        target = ROOT / rel_path
        original = target.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(anchor)
        if hits != 1:
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(锚点命中 {hits} 次,应为 1)")
            bad += 1
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(text.replace(anchor, replacement))
            verdict = _run_locks()
        finally:
            target.write_bytes(original)
        if verdict == "KILLED":
            killed += 1
        icon = {"KILLED": "[KILL]", "SURVIVED": "[LIVE]", "NO_TESTS": "[NONE]"}[verdict]
        print(f"{code}  {icon} {verdict:9} {note}")

    total = len(MUTATIONS)
    print(f"\n变异 {total} 条 · KILLED {killed} · ANCHOR_BAD {bad} · 其余 {total - killed - bad}")
    if killed != total:
        print("🔴 有变异没被杀死 —— 先分诊「锁写松了」还是「变异是空操作」")
        return 1
    print("✅ 全部 KILLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
