"""定向快检:逐发打外选变异 → 只跑我的新判据文件 → 看是不是**那几条**红。

不是最终报数(最终报数用 scripts/mutation_runner_extsel_2026_08_26.py 跑整族分母)。
这一步只为了在写判据的过程中快速拿到「杀没杀掉」的反馈。

纪律:
  · 锚点必须**恰好命中 1 次**,否则停(命中 0 = 考错题;命中 >1 = 改了别处);
  · 备份先落盘,还原后核 sha256(盘满时 write_bytes 会先截断再写 ——
    本仓 2026-08-25 有过把资金链源文件留成 0 字节的实录);
  · 读写一律 `newline=""` 走字节路径:api/defensive_geo_api.py 是 CRLF,
    其余全 LF,用 read_text() 会把两种视图混起来(外选执行方踩过)。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTS = {
    # 🔴 草单只框了 except 臂的**后半行**(括注写「即整句」)。照字面抄会把
    #    `raise PricingCatalogUnreadable(` 留成断头 ⇒ SyntaxError ⇒ 整包红 = 钝杀,
    #    不是判据抓住了(我第一次就这么跑的,红 6 条全中,一眼假)。
    #    按括注取**整句**;整句是多行 ⇒ 锚点必须按目标文件的实际换行翻译
    #    (本文件 1478 CRLF / 5 LF)。
    "EXTA-01": ("api/defensive_geo_api.py",
                'raise PricingCatalogUnreadable(\n'
                '            "价目读取失败 feature=%s: %s" % (feature_code, exc)) from None',
                'return _PRICING_CATALOG_SCHEME + ":db-unreadable"'),
    "EXTA-02": ("api/defensive_geo_api.py",
                '"1" if row.get("requires_paid_points") else "0",',
                '"0",'),
    "EXTA-03": ("api/defensive_geo_api.py",
                "if _frozen_amount != _exact_confirmed:",
                "if _frozen_amount > _exact_confirmed:"),
    "EXTA-04": ("api/defensive_geo_api.py",
                '_frozen_amount = int(result.get("amount") or 0)',
                '_frozen_amount = int(result.get("amount") or _exact_confirmed)'),
    "EXTA-05": ("services/diagnosis_runs.py",
                'tref = run.get("freeze_task_ref"); payer = settlement_payer_user_id(run)',
                'tref = run.get("freeze_task_ref"); payer = run.get("owner_user_id")'),
    "EXTA-06": ("services/diagnosis_runs.py",
                'int(settlement_payer_user_id(run)), int(verify["total"]),',
                'int(run["owner_user_id"]), int(verify["total"]),'),
    "EXTA-07": ("services/diagnosis_runs.py",
                '_cas(run_token, ["running", "commit_pending"], "settlement_manual",',
                '_cas(run_token, ["running"], "settlement_manual",'),
    "EXTA-08": ("services/diagnosis_runs.py",
                "if not (0.0 < ratio < 1.0):",
                "if not (0.0 <= ratio < 1.0):"),
    "EXTA-09": ("services/diagnosis_runs.py",
                '_org_actual, _org_err = None, f"partial_calc_exception:{str(_ope)[:80]}"',
                "_org_actual, _org_err = None, None"),
    "EXTA-14": ("services/diagnosis_runs.py",
                "if snap_pools != pools:",
                "if False and snap_pools != pools:"),
}

TARGET = "tests/defgeo_funding_p0_2026_08_25/test_extsel_gaps_pg.py"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_file_newlines(text: str, needle: str) -> str:
    """把锚点翻译成**目标文件实际用的**换行,谁命中就用谁。

    不猜文件是哪种:两种形态都数,由命中数说话。单行锚点两种形态相同,不受影响。
    """
    lf = needle.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    if lf == crlf:
        return lf
    return crlf if text.count(crlf) == 1 and text.count(lf) != 1 else lf


def _assert_parses(rel: str, mutated: bytes, mid: str) -> None:
    """变异后必须**语法仍然合法**。

    语法崩塌会让整包红 —— 那是钝杀,读起来却和「判据抓住了」一模一样。
    EXTA-01 我第一次就踩了这个:照草单抄半句,红 6 条全中。
    """
    if not rel.endswith(".py"):
        return
    import ast
    try:
        ast.parse(mutated.decode("utf-8"))
    except SyntaxError as exc:
        raise SystemExit("🔴 %s 变异后语法崩塌(%s)—— 那是钝杀不是判据,停" % (mid, exc))


def run_pytest(target: str) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:p0fixpass@localhost:55438/geo_defgeo_regress_test")
    env.setdefault("DEFGEO_P0FIX_TEST_DSN",
                   "postgresql://geo_admin:p0fixpass@localhost:55438/defgeo_p0fix_test")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-m", "pytest", target, "-q", "-p", "no:randomly",
                        "-p", "no:warnings", "-ra"],
                       cwd=str(ROOT), capture_output=True, env=env)
    return p.returncode, (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))


def red_nodes(out: str) -> set[str]:
    import re
    return {m.group(2) for m in
            re.finditer(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)", out, re.M)}


def main() -> int:
    only = [x.strip() for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x.strip()]
    ids = only or list(MUTS)
    target = sys.argv[2] if len(sys.argv) > 2 else TARGET

    for mid in ids:
        rel, needle, repl = MUTS[mid]
        path = ROOT / rel
        original = path.read_bytes()
        backup = path.with_suffix(path.suffix + ".qcbak")
        backup.write_bytes(original)                      # 备份**先落盘**
        assert sha(backup.read_bytes()) == sha(original), "备份没写成功,拒跑"

        text = original.decode("utf-8")
        needle = _to_file_newlines(text, needle)
        n = text.count(needle)
        if n != 1:
            print("🔴 %s 锚点命中 %d 次(要求 1)—— 考错题,停" % (mid, n))
            print("   文件 CRLF=%d · 逐行命中=%s"
                  % (original.count(b"\r\n"),
                     [(ln[:60], text.count(ln)) for ln in needle.splitlines()]))
            backup.unlink()
            return 1
        mutated = text.replace(needle, repl, 1).encode("utf-8")
        assert sha(mutated) != sha(original), "%s 替换后文件零变化 = no-op" % mid
        _assert_parses(rel, mutated, mid)
        path.write_bytes(mutated)
        try:
            rc, out = run_pytest(target)
            reds = red_nodes(out)
            tail = [ln for ln in out.splitlines() if " passed" in ln or " failed" in ln]
            print("%-9s 红 %-2d  %s" % (mid, len(reds), tail[-1] if tail else "?"))
            for r in sorted(reds):
                print("           · " + r.split("::")[-1])
        finally:
            tmp = path.with_suffix(path.suffix + ".qcrestore")
            tmp.write_bytes(original)
            os.replace(tmp, path)
            assert sha(path.read_bytes()) == sha(original), "还原失败 —— 立刻停"
            backup.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
