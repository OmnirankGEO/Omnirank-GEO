"""变异检验 · 未送达调用不产生成本账(WO 2026-08-08 · 资金)。

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。

跑法(需 TEST_DATABASE_URL 指向 throwaway PG):
    python tests/mutation_runner_undelivered_not_billed_2026_08_08.py
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
LOCK_SUITE = "tests/test_undelivered_not_billed_2026_08_08.py"
TRACKER = "tools/llm_call_tracker.py"
BREAKER = "tools/search/provider_circuit_breaker.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M01", "归零整条去掉(= bug 原样:未送达照记 ¥0.05)",
        TRACKER,
        "        if self.undelivered:\n            return 0.0\n",
        "        if False:\n            return 0.0\n",
    ),
    (
        "M02", "estimate_cost 恒返 0(把整张成本表清零 —— 反向对照必须抓住)",
        TRACKER,
        "        if self.undelivered:\n            return 0.0\n",
        "        if True:\n            return 0.0\n",
    ),
    (
        "M03", "except 里不再置位(标记永远是 False)",
        TRACKER,
        "        if _is_undelivered_exception(e):\n            ctx.undelivered = True\n",
        "        if False:\n            ctx.undelivered = True\n",
    ),
    (
        "M04", "判据恒真(ReadTimeout / 业务异常也被当成未送达 → 少记成本)",
        TRACKER,
        "    return bool(types) and isinstance(exc, types)\n",
        "    return True\n",
    ),
    (
        "M05", "判据坏掉时回落成「不计费」(方向反了 —— 会静默少算)",
        TRACKER,
        "    except Exception:  # pragma: no cover - 依赖缺失时保守计费\n        return False\n",
        "    except Exception:  # pragma: no cover\n        return True\n",
    ),
    (
        "M06", "在 tracker 里私藏第二份异常清单(不再复用唯一定义)",
        TRACKER,
        "        from tools.search.provider_circuit_breaker import undelivered_error_types\n\n        types = undelivered_error_types()\n",
        "        import httpx\n\n        types = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)\n",
    ),
    (
        "M07", "不写 metadata.undelivered(对账筛不出来 · 方案 a 的可观测性没了)",
        TRACKER,
        '            ctx.metadata = {**(ctx.metadata or {}), "undelivered": True}\n',
        "            pass\n",
    ),
    (
        "M08", "改成方案 (b):未送达干脆不落行(行数口径变化 —— 必须被锁拦住)",
        TRACKER,
        "            await asyncio.to_thread(\n                _write_log_row,\n",
        "            if ctx.undelivered:\n                raise RuntimeError('skip row')\n"
        "            await asyncio.to_thread(\n                _write_log_row,\n",
    ),
    (
        "M09", "把熔断包那份唯一定义改窄(联动判据:本包必须跟着变)",
        BREAKER,
        "        from services.marketing.image_client import _connect_error_types\n\n        return _connect_error_types()\n",
        "        import httpx\n\n        return (httpx.ProxyError,)\n",
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
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode == 5:
        return "NO_TESTS"
    return "KILLED" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    print("── 基线自检:未变异时锁必须全绿 ──")
    if _run_locks() != "SURVIVED":
        print("[ERR ] 基线就不是绿的 → 变异结果无意义,先修基线")
        return 2
    print("   OK 基线全绿\n")

    killed = bad = 0
    for code, note, rel, anchor, replacement in MUTATIONS:
        target = ROOT / rel
        original = target.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(anchor)
        if hits != 1:
            print(f"{code}  [BAD ] ANCHOR_BAD  {note}(锚点命中 {hits} 次,应为 1)")
            bad += 1
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="") as h:
                h.write(text.replace(anchor, replacement))
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
