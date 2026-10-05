"""外选 E2 存活发的**定向重放** runner(判据洞补齐验收用)。

口径
----
Review 令:「每洞验收 = 对应变异重放必死」。所以这支 runner 只做一件事:
把 Review 终单 V2 里 E2 那几发**原样**打回去(锚点逐字取自终单草稿
`C:/AI-Test/EXTSEL_E2_DRAFT_2026-08-27.md`),看我新补的判据红不红。

🔴 它**不是**证明力来源 —— 这几发是 Review 供的题,我只是执行方。
   自选变异零证明力,这一条不变;这里跑的全是外选。

四道闸(前三道是本仓撕锁的老规矩,第四道是被 E2-2a 那一脚教出来的)
--------------------------------------------------------------
1. **锚点命中必须恰 1 次** —— ≠1 当场停(命中 0 = 锚点过期,命中 >1 = 打到别处)。
2. **零变化 = no-op** —— 变异前后 sha256 必须不同。
3. **语法崩塌不算杀** —— .py 走 `ast.parse`,.sql 走「注释外的 `$$` 必须成对」。
   钝杀会让整包红,读起来跟「判据抓住了」一模一样。
4. **基线闸** —— 跑变异之前先跑一次干净的;基线有红,后面每一发的红集里
   都混着那几条,「我打断了既有判据」会被读成「变异被杀」。

在盘三字段(门9 的教训:绿数守恒够不到「毒到底落没落盘」这一维)
--------------------------------------------------------------
每发打印:`hits`(锚点命中数)· `sha 前≠后` · **回读**(变异串在盘上 / 原串已不在盘上)。
🔴 门9 实测:`readback_needle_gone` 对**插入型**变异是假警报(替换串里含锚串),
   所以这里回读只断言「新串在盘上」,原串是否消失只**打印**不作判据。

用法
----
    python scripts/e2crit_extsel_replay.py                 # 全部
    python scripts/e2crit_extsel_replay.py EXTE2-08,EXTE2-10
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 🔴 Windows 控制台默认 GBK,打 ✓/✗ 会 UnicodeEncodeError —— 而那一炸发生在
#    「毒已经落盘」之后,只是靠 finally 才没把树留脏。报数工具自己崩掉不许再有第二次。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

P0_TARGET = "tests/defgeo_funding_p0_2026_08_25"
PKGE_TARGET = "tests/defensive_geo_pkge_2026_08_24"

P0_ENV = {
    "DEFGEO_P0FIX_TEST_DSN":
        "postgresql://geo_admin:p0fixpass@localhost:55491/defgeo_p0fix_test",
    "TEST_DATABASE_URL":
        "postgresql://geo_admin:p0fixpass@localhost:55491/geo_defgeo_regress_test",
}
PKGE_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_defgeo_pkge_test"
PKGE_ENV = {"TEST_DATABASE_URL": PKGE_DSN, "DATABASE_URL": PKGE_DSN}

# ── 锚点逐字取自 EXTSEL_E2_DRAFT_2026-08-27.md,一个字都没改 ──────────────
_E2_08_OLD = (
    '                _locked_pricing = _lock_pricing_row_for_confirm(\n'
    '                    cur, str(preview["feature_code"]))\n'
    '                _live_version = _pricing_catalog_version_from_row(\n'
    '                    _locked_pricing, str(preview["feature_code"]))\n'
)
_E2_08_NEW = (
    '                _live_version = _live_pricing_catalog_version(\n'
    '                    str(preview["feature_code"]))\n'
    '                _locked_pricing = _lock_pricing_row_for_confirm(\n'
    '                    cur, str(preview["feature_code"]))\n'
)

_E2_12_OLD = (
    "    WHERE table_schema = 'public'\n"
    "      AND table_name = 'diagnosis_runs'\n"
)
_E2_12_NEW = "    WHERE table_name = 'diagnosis_runs'\n"

MUTS = {
    # 草单 MUT-EXTE2-06:FOR SHARE 摘掉 —— 锁语句降级成普通 SELECT。
    # 已被既有判据杀。放在这里当**回归控制**:我重写并发判据之后它必须还是杀。
    "EXTE2-06": (P0_TARGET, "api/defensive_geo_api.py",
                 'FROM feature_pricing WHERE feature_code=%s FOR SHARE',
                 'FROM feature_pricing WHERE feature_code=%s'),
    # 草单 MUT-EXTE2-07:锁行的 feature_code 写死。Review 已裁「冗余」(写者普查
    # 证明可达域内造不出别的值)。留在单里只为**留档**,不期待它被杀。
    "EXTE2-07": (P0_TARGET, "api/defensive_geo_api.py",
                 '        "FROM feature_pricing WHERE feature_code=%s FOR SHARE",\n'
                 '        (str(feature_code),),\n',
                 '        "FROM feature_pricing WHERE feature_code=%s FOR SHARE",\n'
                 '        ("geo_diagnosis",),\n'),
    # 草单 MUT-EXTE2-08:版本比对挪到锁之前 —— 版本又来自锁外的读。
    "EXTE2-08": (P0_TARGET, "api/defensive_geo_api.py", _E2_08_OLD, _E2_08_NEW),
    # 草单 MUT-EXTE2-09:比对键漏一个 —— hash 核了 policy 没核。
    "EXTE2-09": (PKGE_TARGET, "services/defensive_geo/activation_materializer.py",
                 '                ("funding_policy", draft.funding_policy),\n', ""),
    # 草单 MUT-EXTE2-10:声明反向 —— 注释与行为对着写(纯声明毒,行为零变化)。
    "EXTE2-10": (PKGE_TARGET, "services/defensive_geo/activation_materializer.py",
                 "在**冻结那一刻现取**的。",
                 "**并非**在**冻结那一刻现取**的。"),
    # 草单 MUT-EXTE2-12:050 自证 DO 块摘掉 table_schema='public' 谓词。
    "EXTE2-12": (P0_TARGET, "db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
                 _E2_12_OLD, _E2_12_NEW),
}


#: 🔴 **仪器自证,不是外选变异** —— 单独一栏,绝不并进 MUTS 的机械枚举分母。
#:    (门9 记过一次:`origin` 字段撒谎,自选栏得靠人记得再减掉七发。)
#:
#:    验的是 Review 令里那句「屏障同步**隔离触发**」:把并发判据的第二条连接
#:    指向一个连不上的端口 —— 失败必须落在 **fixture(ERROR)** 上、并且只砸中
#:    那一条判据,而**不是**变成 `test_e2_flipping…` 的 assert 红。
#:    「没验到」与「代码坏了」必须长得不一样,这一发就是拿来看它俩长不长得一样的。
SELF_CHECKS = {
    "INSTR-CONN": (P0_TARGET, "tests/defgeo_funding_p0_2026_08_25/test_e2_pricing_row_lock_pg.py",
                   '    conn = psycopg2.connect(pool_world["dsn"])\n'
                   '    conn.autocommit = True\n'
                   '    cur = conn.cursor()\n'
                   '    cur.execute("SET lock_timeout',
                   '    conn = psycopg2.connect(pool_world["dsn"].replace(":55491", ":1"))\n'
                   '    conn.autocommit = True\n'
                   '    cur = conn.cursor()\n'
                   '    cur.execute("SET lock_timeout'),
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def to_file_newlines(text: str, needle: str) -> str:
    """锚点必须走**字节路径**:api/defensive_geo_api.py 是 CRLF,别的是 LF。

    (草单点名过这件事;WOA 上一轮就是在这里被咬过一次。)
    """
    lf = needle.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    if lf == crlf:
        return lf
    if text.count(crlf) == 1 and text.count(lf) != 1:
        return crlf
    return lf


def run_pytest(target: str, extra_env: dict) -> str:
    env = dict(os.environ)
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:randomly",
         "-p", "no:warnings", "-ra"],
        cwd=str(ROOT), capture_output=True, env=env)
    return p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def red_nodes(out: str) -> set:
    return {m.group(2) for m in
            re.finditer(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)", out, re.M)}


def tail(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln or "error" in ln]
    return lines[-1] if lines else "?"


def sql_dollar_blocks_balanced(text: str) -> bool:
    """.sql 的钝杀闸:去掉整行注释后,`$$`/`$name$` 定界符必须成对。"""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    tags = re.findall(r"\$[A-Za-z_]*\$", code)
    counts = {}
    for t in tags:
        counts[t] = counts.get(t, 0) + 1
    return all(v % 2 == 0 for v in counts.values())


def gate_syntax(rel: str, mutated: bytes, mid: str) -> str | None:
    text = mutated.decode("utf-8")
    if rel.endswith(".py"):
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return "%s 变异后语法崩塌(%s)—— 钝杀不是判据" % (mid, exc)
    elif rel.endswith(".sql"):
        if not sql_dollar_blocks_balanced(text):
            return "%s 变异后 $$ 定界符不成对 —— 钝杀不是判据" % mid
    return None


def main() -> int:
    catalog = dict(MUTS)
    catalog.update(SELF_CHECKS)
    only = [x.strip() for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x.strip()]
    ids = only or list(MUTS)          # 默认只跑**外选**;仪器自证要显式点名
    unknown = [i for i in ids if i not in catalog]
    if unknown:
        print("[STOP] 不认识的发号:%r" % unknown)
        return 2
    print("本次:外选 %d 发 · 仪器自证 %d 发(外选机械枚举分母 = %d)"
          % (sum(1 for i in ids if i in MUTS), sum(1 for i in ids if i in SELF_CHECKS),
             len(MUTS)))

    # ── 闸4:基线 ────────────────────────────────────────────────────────
    baselines = {}
    for target, extra in ((P0_TARGET, P0_ENV), (PKGE_TARGET, PKGE_ENV)):
        if not any(catalog[i][0] == target for i in ids):
            continue
        out = run_pytest(target, extra)
        reds = red_nodes(out)
        print("基线 %-38s red=%d  %s" % (target, len(reds), tail(out)))
        for r in sorted(reds):
            print("        . " + r.split("::")[-1][:96])
        if reds:
            print("[STOP] 基线就有红,变异结论不算数")
            return 1
        baselines[target] = reds

    rc = 0
    for mid in ids:
        target, rel, needle_raw, repl_raw = catalog[mid]
        path = ROOT / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        needle = to_file_newlines(text, needle_raw)
        repl = repl_raw.replace("\r\n", "\n")
        if "\r\n" in needle:
            repl = repl.replace("\n", "\r\n")

        hits = text.count(needle)
        if hits != 1:                                    # 闸1
            print("[STOP] %s 锚点命中 %d 次(要求 1)" % (mid, hits))
            return 1
        mutated = text.replace(needle, repl, 1).encode("utf-8")
        if sha(mutated) == sha(original):                # 闸2
            print("[STOP] %s 零变化 = no-op" % mid)
            return 1
        bad = gate_syntax(rel, mutated, mid)             # 闸3
        if bad:
            print("[STOP] " + bad)
            return 1

        path.write_bytes(mutated)
        landed = False
        try:
            on_disk = path.read_bytes().decode("utf-8")
            landed = repl.strip() in on_disk if repl.strip() else (needle not in on_disk)
            out = run_pytest(target, P0_ENV if target == P0_TARGET else PKGE_ENV)
            reds = red_nodes(out) - baselines[target]
            verdict = "杀" if reds else "存活"
            print("%-9s %-4s red=%-3d %s" % (mid, verdict, len(reds), tail(out)))
            print("            在盘:hits=%d · %s≠%s · 回读新串%s"
                  % (hits, sha(original)[:16], sha(mutated)[:16], "✓" if landed else "✗"))
            for r in sorted(reds):
                print("            . " + r.split("::")[-1][:96])
        finally:
            # 🔴 还原必须原子(盘满会把源文件截成 0 字节的那一课),
            #    且**不在 finally 里 return** —— 那会把正在飞的异常吞掉。
            tmp = path.with_suffix(path.suffix + ".e2critrestore")
            tmp.write_bytes(original)
            os.replace(tmp, path)
            restored = sha(path.read_bytes()) == sha(original)
        if not restored:
            print("[STOP] %s 还原失败 —— 立刻停手" % mid)
            return 1
        if not landed:
            print("[STOP] %s 毒没落盘 —— 结论不算数" % mid)
            return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
