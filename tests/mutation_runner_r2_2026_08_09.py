"""变异检验 · R2 四件(sync 路径 / 异常族扩面 / metaso 接缝 / regenerating 看门狗)。

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。

跑法(需 TEST_DATABASE_URL 指向 throwaway PG):
    python tests/mutation_runner_r2_2026_08_09.py
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
SUITES = (
    "tests/test_undelivered_r2_sync_and_families_2026_08_09.py "
    "tests/test_metaso_key_seam_and_regen_watchdog_2026_08_09.py"
).split()

TRACKER = "tools/llm_call_tracker.py"
BREAKER = "tools/search/provider_circuit_breaker.py"
METASO = "tools/search/metaso_mcp.py"
SCHED = "api/scheduler.py"
DIAG = "db/diagnosis_db.py"
SERVER = "server.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── (a) sync 路径 ────────────────────────────────────────────────────
    (
        "M01", "sync 版不再置位(= R1 的漏面原样:8 个调用点全裸奔)",
        TRACKER,
        "        if exc_val is not None and _is_undelivered_exception(exc_val):\n"
        "            self.ctx.undelivered = True\n",
        "        if False:\n            self.ctx.undelivered = True\n",
    ),
    (
        "M02", "sync 版判据换成「只要有异常就算未送达」(少记成本)",
        TRACKER,
        "        if exc_val is not None and _is_undelivered_exception(exc_val):\n",
        "        if exc_val is not None:\n",
    ),
    (
        "M03", "sync 版不写 metadata(对账筛不出来)",
        TRACKER,
        '            self.ctx.metadata = {**(self.ctx.metadata or {}), "undelivered": True}\n',
        "            pass\n",
    ),
    # ── (b) 异常族扩面 ───────────────────────────────────────────────────
    (
        "M04", "去掉 aiohttp 族(competition_analyzer / api_5118 两条链回到裸奔)",
        BREAKER,
        "    return types + _aiohttp_no_send_types()",
        "    return types",
    ),
    (
        "M05", "aiohttp 收得太宽(把 ClientConnectionError 收进来 → 连上又断也算未送达)",
        BREAKER,
        "        return (aiohttp.ClientConnectorError,)\n",
        "        return (aiohttp.ClientConnectionError,)\n",
    ),
    (
        "M06", "去掉 ImageConnectFailed(重试用尽那条标记回到照记)",
        BREAKER,
        "        types = _connect_error_types() + (ImageConnectFailed,)\n",
        "        types = _connect_error_types()\n",
    ),
    (
        "M07", "aiohttp 取不到时抛错(应当 fail-open 成空元组)",
        BREAKER,
        "    except Exception:  # pragma: no cover - 环境没装 aiohttp\n        return ()\n",
        "    except Exception:  # pragma: no cover\n        raise\n",
    ),
    # ── (②) metaso 接缝 ─────────────────────────────────────────────────
    (
        "M08", "api_key 改回 import 期读死(第三次咬人原样复现)",
        METASO,
        '    override = METASO_MCP_CONFIG.get("api_key")\n    if override:\n        return str(override)\n    return str(os.environ.get("METASO_API_KEY") or "")\n',
        '    return str(METASO_MCP_CONFIG.get("api_key") or "")\n',
    ),
    (
        "M09", "显式覆盖失效(测试接缝没了 —— setitem 被 env 盖掉)",
        METASO,
        '    override = METASO_MCP_CONFIG.get("api_key")\n    if override:\n        return str(override)\n',
        '    override = None\n    if override:\n        return str(override)\n',
    ),
    # ── (③) regenerating 看门狗 ─────────────────────────────────────────
    (
        "M10", "看门狗不再捡 regenerating(孤儿根因原样保留)",
        SCHED,
        "                WHERE status='regenerating' AND article_id IS NULL\n",
        "                WHERE status='regenerating' AND article_id IS NULL AND FALSE\n",
    ),
    (
        "M11", "🔴 恢复态改成 pending(违反 charge-on-success · 用户重选=二次扣费)",
        SCHED,
        "                SET status='failed',\n"
        "                    fail_reason=COALESCE(fail_reason, '选题重生成超时未回执(看门狗回收,可重新生成)')\n",
        "                SET status='pending'\n",
    ),
    (
        "M12", "去掉 article_id 护栏(已出稿的也被推 failed)",
        SCHED,
        "                WHERE status='regenerating' AND article_id IS NULL\n",
        "                WHERE status='regenerating'\n",
    ),
    (
        "M13", "阈值 24h → 1 分钟(正在跑的当场误杀)",
        SCHED,
        "                  AND COALESCE(regenerate_started_at, created_at) < NOW() - INTERVAL '24 hours'\n",
        "                  AND COALESCE(regenerate_started_at, created_at) < NOW() - INTERVAL '1 minute'\n",
    ),
    (
        "M14", "顺手把 writing 那支也改成 failed(绕过 write_timeout 的防双扣设计)",
        SCHED,
        "                SET status='write_timeout'\n",
        "                SET status='failed'\n",
    ),
    # ── [返工 2026-08-09] 判据锚 + 三个入态点盖戳 ─────────────────────────
    (
        "M15", "🔴 判据锚改回 created_at(= 被复审打回的那一版:老选题刚重生成即误杀)",
        SCHED,
        "                  AND COALESCE(regenerate_started_at, created_at) < NOW() - INTERVAL '24 hours'\n",
        "                  AND created_at < NOW() - INTERVAL '24 hours'\n",
    ),
    (
        "M16", "去掉 COALESCE 回落(存量在途行列为 NULL → 6122-6133 永远捡不起来)",
        SCHED,
        "                  AND COALESCE(regenerate_started_at, created_at) < NOW() - INTERVAL '24 hours'\n",
        "                  AND regenerate_started_at < NOW() - INTERVAL '24 hours'\n",
    ),
    (
        "M17", "sink 1:regenerate_topic 不盖戳(列加了但没接线 → 恒回落 created_at)",
        DIAG,
        "                regenerate_count = regenerate_count + 1,\n                regenerate_started_at = NOW()\n",
        "                regenerate_count = regenerate_count + 1\n",
    ),
    (
        "M18", "sink 2:智能补足**建行** INSERT 不盖戳",
        SERVER,
        "                    status, is_optimize, fail_reason, created_at, regenerate_started_at\n"
        "                ) VALUES (%s, %s, %s, '标题生成中...', %s, 'batch_distribution', %s, %s,\n"
        "                          'regenerating', TRUE, NULL, NOW(), NOW())\n",
        "                    status, is_optimize, fail_reason, created_at\n"
        "                ) VALUES (%s, %s, %s, '标题生成中...', %s, 'batch_distribution', %s, %s,\n"
        "                          'regenerating', TRUE, NULL, NOW())\n",
    ),
    (
        "M19", "sink 3:智能补足**重试** UPDATE 不盖戳(改的是老行 → 又一条误杀路径)",
        SERVER,
        "            UPDATE topics SET status='regenerating', optimized_title='标题生成中...', fail_reason=NULL,\n"
        "                              regenerate_started_at=NOW()\n",
        "            UPDATE topics SET status='regenerating', optimized_title='标题生成中...', fail_reason=NULL\n",
    ),
    (
        "M20", "建表兜底不登记这一列(生产 column does not exist · 看门狗整支静默失败)",
        DIAG,
        '        _safe_add_column(cursor, "topics", "regenerate_started_at", "TIMESTAMP DEFAULT NULL")\n',
        "",
    ),
]


def _purge_pycache() -> None:
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def _run_locks() -> str:
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "-p", "no:cacheprovider", *SUITES],
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
