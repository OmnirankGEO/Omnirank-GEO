"""变异检验 · 套餐档位码 flagship/premium(WO §8 快修 2026-08-08)。

自坏防线:清 `__pycache__` + `-p no:cacheprovider` · 锚点命中 ≠1 报 ANCHOR_BAD ·
退出码 5 单独报 NO_TESTS · 写回用原始 bytes 逐字还原。

跑法:python tests/mutation_runner_tier_target_2026_08_08.py
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
LOCK_SUITE = "tests/test_tier_target_flagship_2026_08_08.py"
SSOT = "services/monitoring_tier_target.py"
MON = "api/monitoring_api.py"
SRV = "server.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M01", "删掉 legacy 别名(= 工单字面的「改名」写法 · 会把唯一在监测的 premium 客户从 75% 打回 65%)",
        SSOT,
        'LEGACY_TIER_ALIASES: Dict[str, str] = {\n    "premium": "flagship",\n}',
        'LEGACY_TIER_ALIASES: Dict[str, str] = {}',
    ),
    (
        "M02", "别名反向(新码 → 老码 · 等于往库里写 legacy)",
        SSOT,
        '    "premium": "flagship",\n',
        '    "flagship": "premium",\n',
    ),
    (
        "M03", "数值改回手抄(不再从 SSOT 派生 —— 这就是病因本身)",
        SSOT,
        '    raw = str(cfg.get("ai_probability") or "").strip().rstrip("%")\n',
        '    raw = {"入门版": "50", "标准版": "65", "旗舰版": "75"}.get(cfg.get("label"), "65")\n',
    ),
    (
        "M04", "is_known_tier_code 恒真(报告链的 entry 兜底被架空)",
        SSOT,
        '    code = str(raw or "").strip().lower()\n    if not code:\n        return False\n    return LEGACY_TIER_ALIASES.get(code, code) in _tier_config()\n',
        '    return True\n',
    ),
    (
        "M05", "认不出的码不再回落 standard(直接把生码透出去)",
        SSOT,
        '    return code if code in _tier_config() else DEFAULT_TIER_CODE\n',
        '    return code\n',
    ),
    (
        "M06", "monitoring_api 把本地表加回来(接线断,拷贝复活)",
        MON,
        'from services.monitoring_tier_target import tier_target as _tier_target_ssot\n',
        'TIER_TARGET = {\n    "entry": {"name": "入门版", "target_rate": 50},\n'
        '    "standard": {"name": "标准版", "target_rate": 65},\n'
        '    "premium": {"name": "旗舰版", "target_rate": 75},\n}\n'
        'from services.monitoring_tier_target import tier_target as _tier_target_ssot\n',
    ),
    (
        "M07", "档位白名单把 flagship 去掉(回到拒绝规范码的状态)",
        SRV,
        '    if tier not in ("entry", "standard", "flagship", "premium"):\n',
        '    if tier not in ("entry", "standard", "premium"):\n',
    ),
    (
        "M08", "档位白名单把 legacy premium 去掉(前端下拉框当场报无效套餐)",
        SRV,
        '    if tier not in ("entry", "standard", "flagship", "premium"):\n',
        '    if tier not in ("entry", "standard", "flagship"):\n',
    ),
    (
        "M09", "报告链兜底从 entry 改成 standard(重开 CTO-15.23 的自动升档误导)",
        SRV,
        '_tier_target_ssot(tier_code if _tier_known(tier_code) else "entry")',
        '_tier_target_ssot(tier_code)',
    ),
    (
        "M10", "把代理等级维度也一起换成 flagship(粗暴全仓替换 —— 会算错转换配额)",
        "config/v3_3_1_flags.py",
        '    if tier == "premium":\n',
        '    if tier == "flagship":\n',
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
