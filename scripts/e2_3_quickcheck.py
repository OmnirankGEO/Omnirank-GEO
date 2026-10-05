"""E2-3 定向快检(pkgE 分母,DSN 与 e2_quickcheck 不同,故单独一支)。

同三道闸 + 基线闸(基线闸是本轮被 E2-2a 那一脚教出来的:
基线红 > 0 时,每一发变异的红集里都混着那几条,「我打断了既有判据」会被读成「变异被杀」)。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_DRIFT_OLD = (
    "        if _drift:\n"
    "            raise _NeedsHuman("
)
_DRIFT_NEW = (
    "        if False:\n"
    "            raise _NeedsHuman("
)

MUTS = {
    # 拆掉重放核一致性 —— 回到「existing 非空就什么都不比」
    "E2-3a": ("services/defensive_geo/activation_materializer.py",
              _DRIFT_OLD, _DRIFT_NEW),
    # 只把 budget_hash 从比对分母里摘掉(比整段拆更精细:证明分母是逐项守的)
    "E2-3b": ("services/defensive_geo/activation_materializer.py",
              '                ("budget_hash", draft.budget_hash),\n',
              ""),
    # 裁定②的声明被删回去
    "E2-3c": ("services/defensive_geo/publish/execution_budget_policy.py",
              "在**冻结那一刻现取**", "在确认时刻锁定"),
    # 裁定①上半:结算改成「现取」= 已冻结的单改道
    "E2-3d": ("services/defensive_geo/publish/publish_funding.py",
              '            user_id=command.get("payer_user_id"),',
              "            user_id=_platform_service_user_id(),"),
    # 裁定①下半:冻结改成读快照 = 滑向被否掉的静态方向
    "E2-3e": ("services/defensive_geo/publish/publish_funding.py",
              "    platform_uid = _platform_service_user_id()",
              "    platform_uid = int(preview_payer_hint or 0)"),
}

TARGET = "tests/defensive_geo_pkge_2026_08_24"
DSN = "postgresql://geo_admin:testpw@localhost:55480/geo_defgeo_pkge_test"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_file_newlines(text: str, needle: str) -> str:
    lf = needle.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    if lf == crlf:
        return lf
    return crlf if text.count(crlf) == 1 and text.count(lf) != 1 else lf


def run_pytest():
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = DSN
    env["DATABASE_URL"] = DSN
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:randomly",
                        "-p", "no:warnings", "-ra"],
                       cwd=str(ROOT), capture_output=True, env=env)
    return (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))


def red_nodes(out: str):
    import re
    return {m.group(2) for m in
            re.finditer(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)", out, re.M)}


def main() -> int:
    base_out = run_pytest()
    base_red = red_nodes(base_out)
    line = [ln for ln in base_out.splitlines() if "passed" in ln or "failed" in ln]
    print("基线 red=%d  %s" % (len(base_red), line[-1] if line else "?"))
    if base_red:
        print("[STOP] 基线就有红,变异结论不算数")
        for r in sorted(base_red):
            print("        . " + r.split("::")[-1][:88])
        return 1

    only = [x.strip() for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x.strip()]
    for mid in (only or list(MUTS)):
        rel, needle, repl = MUTS[mid]
        path = ROOT / rel
        original = path.read_bytes()
        backup = path.with_suffix(path.suffix + ".e23bak")
        backup.write_bytes(original)
        assert sha(backup.read_bytes()) == sha(original), "备份没写成功,拒跑"

        text = original.decode("utf-8")
        needle = _to_file_newlines(text, needle)
        n = text.count(needle)
        if n != 1:
            print("[STOP] %s 锚点命中 %d 次(要求 1)" % (mid, n))
            backup.unlink()
            return 1
        mutated = text.replace(needle, repl, 1).encode("utf-8")
        assert sha(mutated) != sha(original), "%s 零变化 = no-op" % mid
        if rel.endswith(".py"):
            import ast
            try:
                ast.parse(mutated.decode("utf-8"))
            except SyntaxError as exc:
                print("[STOP] %s 语法崩塌(%s)—— 钝杀不是判据" % (mid, exc))
                backup.unlink()
                return 1
        path.write_bytes(mutated)
        try:
            out = run_pytest()
            reds = red_nodes(out)
            tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln]
            print("%-7s red=%-3d %s" % (mid, len(reds), tail[-1] if tail else "?"))
            for r in sorted(reds)[:6]:
                print("        . " + r.split("::")[-1][:88])
        finally:
            tmp = path.with_suffix(path.suffix + ".e23restore")
            tmp.write_bytes(original)
            os.replace(tmp, path)
            assert sha(path.read_bytes()) == sha(original), "还原失败 —— 立刻停"
            backup.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
