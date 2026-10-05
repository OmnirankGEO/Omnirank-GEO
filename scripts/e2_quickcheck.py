"""E2 定向快检:逐发打「删掉修复」的变异 → 跑判据包 → 看是不是**那几条**红。

三道闸(与 extsel quickcheck 同源,其中第三道是被 EXTA-01 那一脚逼出来的):
  · 锚点命中 ≠ 1 → 停(考错题 / 改了别处);
  · 替换后文件零变化 → 停(no-op 被记成「存活」);
  · 变异后 ast.parse 不过 → 停(语法崩塌是钝杀,不是判据抓住了)。
备份先落盘,还原后核 sha256(盘满时 write_bytes 会先截断再写)。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_RAISE_OLD = (
    "        raise ValueError(\n"
    '            "degraded 判定的计数不自洽(planned=%r succeeded=%r)· 拒绝按 float 回猜计费"\n'
    "            % (verdict.planned, verdict.succeeded))"
)

#: E2-2b:把 confirm 改回「用另一条连接算版本」= 修复前形态。
#: 多行锚点写成显式拼接,别用 heredoc 塞 —— bash heredoc 会吃掉反斜杠,
#: 本轮已经被它坑过四次。
_LOCKED_GATE_OLD = (
    "                _locked_pricing = _lock_pricing_row_for_confirm(\n"
    '                    cur, str(preview["feature_code"]))\n'
    "                _live_version = _pricing_catalog_version_from_row(\n"
    '                    _locked_pricing, str(preview["feature_code"]))'
)
_LOCKED_GATE_NEW = (
    "                _live_version = _live_pricing_catalog_version(\n"
    '                    str(preview["feature_code"]))'
)

MUTS = {
    # ── E2-1 站点一(结算侧)──────────────────────────────────────────────
    "E2-1a": ("services/diagnosis_runs.py",
              "    actual = max(1, min(total, exact))",
              "    actual = max(1, min(total, int(total * ratio)))"),
    "E2-1b": ("services/diagnosis_runs.py",
              '        return None, "delivery_verdict_counts_missing"',
              "        return None, None"),
    "E2-1c": ("services/diagnosis_runs.py",
              '        return None, "delivery_verdict_ratio_counts_disagree"',
              "        pass"),
    "E2-1d": ("services/diagnosis_runs.py",
              "    if not isinstance(raw_planned, int) or not isinstance(raw_succeeded, int):",
              "    if False:"),
    # ── E2-1 站点二(contract helper)────────────────────────────────────
    "E2-1e": ("services/diagnosis_sample_contract.py",
              "    return max(1, min(total, (total * succeeded) // planned))",
              "    return max(1, min(total, int(total * verdict.billable_ratio)))"),
    "E2-1f": ("services/diagnosis_sample_contract.py",
              _RAISE_OLD,
              "        return max(1, min(total, int(total * verdict.billable_ratio)))"),
    # -- E2-4 启动守卫 / 迁移自证 ------------------------------------------
    "E2-4a": ("services/startup_schema_guards.py",
              "        if _wrong:",
              "        if False:"),
    "E2-4b": ("services/startup_schema_guards.py",
              "            \"WHERE table_schema='public' AND table_name='diagnosis_runs'\")",
              "            \"WHERE table_name='diagnosis_runs'\")"),
    "E2-4c": ("db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
              "    IF v_type <> 'integer' THEN",
              "    IF FALSE THEN"),
    "E2-4d": ("db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
              "    IF v_nullable <> 'YES' THEN",
              "    IF FALSE THEN"),
    # -- E2-2 价目行锁 ------------------------------------------------------
    "E2-2a": ("api/defensive_geo_api.py",
              "FROM feature_pricing WHERE feature_code=%s FOR SHARE",
              "FROM feature_pricing WHERE feature_code=%s"),
    "E2-2b": ("api/defensive_geo_api.py", _LOCKED_GATE_OLD, _LOCKED_GATE_NEW),
}

TARGET = "tests/defgeo_funding_p0_2026_08_25"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_file_newlines(text: str, needle: str) -> str:
    lf = needle.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    if lf == crlf:
        return lf
    return crlf if text.count(crlf) == 1 and text.count(lf) != 1 else lf


def _assert_parses(rel: str, mutated: bytes, mid: str) -> None:
    if not rel.endswith(".py"):
        return
    import ast
    try:
        ast.parse(mutated.decode("utf-8"))
    except SyntaxError as exc:
        raise SystemExit("[STOP] %s 变异后语法崩塌(%s)—— 钝杀不是判据" % (mid, exc))


def run_pytest(target: str):
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:p0fixpass@localhost:55438/geo_defgeo_regress_test")
    env.setdefault("DEFGEO_P0FIX_TEST_DSN",
                   "postgresql://geo_admin:p0fixpass@localhost:55438/defgeo_p0fix_test")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-m", "pytest", target, "-q", "-p", "no:randomly",
                        "-p", "no:warnings", "-ra"],
                       cwd=str(ROOT), capture_output=True, env=env)
    return (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))


def red_nodes(out: str):
    import re
    return {m.group(2) for m in
            re.finditer(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)", out, re.M)}


def main() -> int:
    only = [x.strip() for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x.strip()]

    # 🔴 基线闸(本轮被这一脚教出来的):先在**未变异**的树上跑一次。
    #    基线红 > 0 的话,每一发变异的红集里都会混着那几条 —— 我就把
    #    「我自己的改动打断了两条既有判据」读成了「变异被杀掉了」。
    #    E2-2a 当时报 red=3,其中 2 条是基线红。
    #    报数栏永远不能自证,必须先有一个「这一轮的地板在哪」的旁证。
    base_out = run_pytest(TARGET)
    base_red = red_nodes(base_out)
    base_line = [ln for ln in base_out.splitlines() if "passed" in ln or "failed" in ln]
    print("基线 red=%d  %s" % (len(base_red), base_line[-1] if base_line else "?"))
    if base_red:
        print("[STOP] 基线就有 %d 条红,变异结论在基线绿之前不算数:" % len(base_red))
        for r in sorted(base_red):
            print("        . " + r.split("::")[-1][:88])
        return 1

    for mid in (only or list(MUTS)):
        rel, needle, repl = MUTS[mid]
        path = ROOT / rel
        original = path.read_bytes()
        backup = path.with_suffix(path.suffix + ".e2bak")
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
        _assert_parses(rel, mutated, mid)
        path.write_bytes(mutated)
        try:
            out = run_pytest(TARGET)
            reds = red_nodes(out)
            tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln]
            print("%-7s red=%-3d %s" % (mid, len(reds), tail[-1] if tail else "?"))
            for r in sorted(reds)[:6]:
                print("        . " + r.split("::")[-1][:88])
            if len(reds) > 6:
                print("        . (+%d more)" % (len(reds) - 6))
        finally:
            tmp = path.with_suffix(path.suffix + ".e2restore")
            tmp.write_bytes(original)
            os.replace(tmp, path)
            assert sha(path.read_bytes()) == sha(original), "还原失败 —— 立刻停"
            backup.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
