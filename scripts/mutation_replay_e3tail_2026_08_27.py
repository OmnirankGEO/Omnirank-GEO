"""E3 族**尾单**四件的重放器(工单「EXTE3-07/13 + 041 + 回填哨兵」验收用)。

三栏,机械分开报数(本仓纪律:栏不合并):
  · **外选**  —— Review 终单里 E3 族剩下的两发存活变异(07/13),逐字重放;
  · **删修复** —— 本单**非判据**的两处改动(041 schema 谓词 / 回填哨兵),
                 各打一发把修复删掉,证明"删掉修复必须变红";
  · **自选**  —— 打本单新判据自己,问它们是不是只认得出那一发的形状。

纪律与上一轮同:锚点 ``count==1`` 否则当场停 · ``.mutbak`` 落盘 ·
``tmp + os.replace`` 原子还原(``mkstemp`` 的 fd 必须关,否则 Windows WinError 32)·
还原后逐字节核 sha · 禁 ``git checkout`` · 串行 · 每发红集写死在题里逐条比对 ·
junit 读不出来直接 ``SystemExit``(「整包起不来」不许记成零新增红)。

🔴 这不是全分母存活裁定 —— 只跑装着期望红集的那几个包。

用法::

    python scripts/mutation_replay_e3tail_2026_08_27.py --pg 55494
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PACKAGES = {
    "woc": {"path": "tests/defgeo_woc_closure_2026_08_25",
            "db": "geo_defgeo_woc_tail_test",
            "env": ("TEST_DATABASE_URL",)},
    "e3": {"path": "tests/defgeo_e3_2026_08_26",
           "db": "geo_e3tail_e3_test",
           "env": ("TEST_DATABASE_URL", "E3_TEST_DATABASE_URL")},
    "w3": {"path": "tests/defensive_geo_w3_2026_08_21",
           "db": "geo_defgeo_w3_tail_test",
           "env": ("TEST_DATABASE_URL",)},
    "p0": {"path": "tests/defgeo_funding_p0_2026_08_25",
           "db": "geo_defgeo_p0_tail_test",
           "env": ("TEST_DATABASE_URL", "DEFGEO_P0FIX_TEST_DSN")},
}

_NEW_SENTINEL = (
    "         ELSE (xpath('/row/c/text()', query_to_xml(\n"
    "                 'SELECT count(*) AS c FROM public.defgeo_monitoring_attempts'\n"
    "                 ' WHERE tenant_owner_user_id <= 0', false, true, '')))[1]::text::int\n"
)
_OLD_SENTINEL = (
    "         ELSE (SELECT COUNT(*) FROM public.defgeo_monitoring_attempts\n"
    "                WHERE tenant_owner_user_id <= 0)\n"
)

#: **外选**栏。锚与替换逐字取自 EXTSEL_E3_DRAFT_2026-08-27.md,不重写。
MUTATIONS = [
    {
        "id": "MUT-EXTE3-07",
        "why": "配对两半:歧义臂改成可重试 + raise 之后放一个死影子喂 AST census",
        "file": "api/defensive_geo_assist_api.py",
        "edits": [
            ('            "VALIDATION_FAILED",\n',
             '            "POLICY_UNAVAILABLE",\n'),
            ('        ) from exc\n    except LegalRepairNotApplied as exc:\n',
             '        ) from exc\n        _safe_error("VALIDATION_FAILED")\n'
             '    except LegalRepairNotApplied as exc:\n'),
        ],
        "packages": ["woc"],
        "expect": {
            "woc": {
                "test_e2_22_the_handler_really_maps_each_outcome_to_the_right_code",
                "test_e2_24_the_repair_handler_has_no_unreachable_statements",
                "test_e2_26_the_ambiguous_arm_is_a_non_retryable_422_over_real_http",
            },
        },
    },
    {
        "id": "MUT-EXTE3-13",
        "why": "CellIdentity 的保守缺省翻成 provider_echo(不传就自动「已证实」)",
        "file": "services/defensive_geo/monitoring/comparability_feed.py",
        "edits": [('    model_source: str = "planned_fallback"\n',
                   '    model_source: str = "provider_echo"\n')],
        "packages": ["e3"],
        "expect": {
            "e3": {"test_e4_25_the_conservative_default_is_pinned_and_load_bearing"},
        },
    },
]

#: **删修复**栏:本单两处非判据改动,各删一次。
FIX_MUTATIONS = [
    {
        "id": "MUT-FIX-041",
        "why": "把 041 的 table_schema='public' 删掉(= 回到修之前)",
        "file": "db/migration_041_defgeo_run_previews_2026_08_21.sql",
        "edits": [("     WHERE table_schema = 'public'\n"
                   "       AND table_name = 'defgeo_diagnosis_run_previews'\n",
                   "     WHERE table_name = 'defgeo_diagnosis_run_previews'\n")],
        "packages": ["p0", "w3"],
        "expect": {
            "p0": {"test_e2_every_information_schema_query_in_the_defgeo_migrations_is_schema_qualified"},
            "w3": {"test_decoy_previews_table_in_another_schema_does_not_trip_041"},
        },
    },
    {
        "id": "MUT-FIX-SENTINEL",
        "why": "把回填脚本的动态计数改回直接子查询(= 回到 -1 哨兵拿不到的那一版)",
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [(_NEW_SENTINEL, _OLD_SENTINEL)],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e5_13_the_missing_table_branch_returns_the_documented_sentinel",
                "test_e5_14_the_sentinel_and_a_clean_ledger_are_two_different_numbers",
            },
        },
    },
]

#: **自选**栏:打本单新判据自己。
SELF_MUTATIONS = [
    {
        "id": "MUT-SELF-T1",
        "why": "歧义阈值调高 ⇒ 多命中不再报歧义。打真 HTTP 臂:它守的是**行为**不是源码形状",
        "file": "services/defensive_geo/legal_repair.py",
        "edits": [("    if occurrences > 1:\n", "    if occurrences > 99:\n")],
        "packages": ["woc"],
        "expect": {
            "woc": {
                "test_e2_10_a_passage_that_appears_twice_is_ambiguous_not_first_hit",
                "test_e2_26_the_ambiguous_arm_is_a_non_retryable_422_over_real_http",
            },
        },
    },
    {
        "id": "MUT-SELF-T2",
        "why": ("把 041 的谓词改成 table_schema='pg_catalog' —— **仍然带 table_schema**。"
                "A 那条枚举锁只看这个词在不在、看不出值对不对 ⇒ 它必须是绿的;"
                "只有 w3 的行为臂抓得住。这一发是「令牌锁 + 行为臂缺一不可」的实证"),
        "file": "db/migration_041_defgeo_run_previews_2026_08_21.sql",
        "edits": [("     WHERE table_schema = 'public'\n",
                   "     WHERE table_schema = 'pg_catalog'\n")],
        "packages": ["p0", "w3"],
        "expect": {
            "p0": set(),
            "w3": {"test_041_still_raises_on_a_real_forbidden_column_in_public"},
        },
    },
    {
        "id": "MUT-SELF-T3",
        "why": "翻转 MODEL_SOURCE_COMPARABLE 本身 —— 证明 e4_25 不是只钉那一行缺省",
        "file": "services/defensive_geo/monitoring/comparability_feed.py",
        "edits": [('MODEL_SOURCE_COMPARABLE = "provider_echo"\n',
                   'MODEL_SOURCE_COMPARABLE = "planned_fallback"\n')],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e4_23_planned_fallback_cells_do_not_enter_the_matched_cohort",
                "test_e4_24_echoed_cells_still_match",
                "test_e4_25_the_conservative_default_is_pinned_and_load_bearing",
            },
        },
    },
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _mktemp(directory: Path | None, suffix: str) -> Path:
    """🔴 ``mkstemp`` 返回的是**已打开**的 fd —— Windows 上不关它,
    后面的 ``os.replace`` / ``unlink`` 会 WinError 32(文件被占用)。"""
    fd, name = tempfile.mkstemp(
        dir=(str(directory) if directory is not None else None), suffix=suffix)
    os.close(fd)
    return Path(name)


def _apply(path: Path, edits) -> None:
    raw = path.read_bytes()
    for anchor, repl in edits:
        a, r = anchor.encode("utf-8"), repl.encode("utf-8")
        hits = raw.count(a)
        if hits != 1:
            raise SystemExit(
                f"停:{path} 上锚点命中 {hits} 次(必须恰 1)。"
                "锚不唯一 = 变异打在哪一处不确定,结果没有证明力。")
        raw = raw.replace(a, r)
    tmp = _mktemp(path.parent, ".muttmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def _restore(path: Path, backup: Path) -> None:
    tmp = _mktemp(path.parent, ".mutrestore")
    tmp.write_bytes(backup.read_bytes())
    os.replace(tmp, path)


def _run(pkg: str, port: int) -> tuple[int, set[str], int]:
    """跑一个包,返回 (退出码, 失败/错误的测试名集合, 总用例数)。"""
    spec = PACKAGES[pkg]
    dsn = f"postgresql://geo_admin:testpw@localhost:{port}/{spec['db']}"
    env = dict(os.environ)
    for key in spec["env"]:
        env[key] = dsn
    out = _mktemp(None, ".junit.xml")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", spec["path"], "-q", "--tb=no",
         f"--junitxml={out}"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        # 🔴 子进程输出是 UTF-8;Windows 上不显式指定,父进程按 GBK 解码,
        #    pytest 一打出中文就 UnicodeDecodeError —— 而它抛在读管道的
        #    **线程**里,主进程只看到半截结果(2026-08-27 实测)。
        encoding="utf-8", errors="replace")
    red: set[str] = set()
    total = 0
    try:
        tree = ET.parse(out)
        for case in tree.iter("testcase"):
            total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                red.add(case.get("name") or "?")
    except (ET.ParseError, FileNotFoundError):
        raise SystemExit(
            f"停:{pkg} 没生成可读的 junit —— 「整包起不来」与「跑了没红」"
            f"长得一模一样,不许当成零新增红。stdout 尾:\n{proc.stdout[-2000:]}")
    finally:
        out.unlink(missing_ok=True)
    return proc.returncode, red, total


def _assert_tree_is_unmutated() -> None:
    """起跑前:树上不许残留**任何一发**的替换文本。

    🔴 2026-08-27 实测的必要性:上一次这个 runner 被中途 kill,``finally`` 的还原
       没跑完,``db/migration_041_…sql`` 被留在**变异态**;而 ``git status`` 只说
       "这个文件被改过"——它本来就被本单改过,所以看起来完全正常。
       残留的变异态如果被当成基线,后面每一发的"新增红"全是假的。
       (同族:被杀的 runner 会把源文件留在半截状态,本仓另有一次留成 0 字节。)
    """
    # 判据是**锚点恰在一次**,不是"替换文本不在":有的替换文本在干净树上本来
    # 就合法存在(MUT-EXTE3-07 的 ``"POLICY_UNAVAILABLE",`` 就是 NotApplied 臂
    # 自己的那一行)。"替换文本不在"会把干净树误判成脏 —— 而误报的闸最后
    # 都会被人绕过去。锚点没了才是"这一发还applied在树上"的硬信号。
    dirty = []
    for mut in list(MUTATIONS) + list(FIX_MUTATIONS) + list(SELF_MUTATIONS):
        raw = (ROOT / mut["file"]).read_text(encoding="utf-8")
        for i, (anchor, _repl) in enumerate(mut["edits"]):
            hits = raw.count(anchor)
            if hits != 1:
                dirty.append(
                    f"{mut['id']} 锚{i} 在 {mut['file']} 上命中 {hits} 次(须恰 1)")
    if dirty:
        joined = "; ".join(sorted(set(dirty)))
        raise SystemExit("停:起跑前树不干净 —— 上一次跑可能被中途杀掉、"
                         "还原没跑完:" + joined)


def main() -> int:
    # 🔴 [窗口B 2026-08-27 · 机制自证抓到] 本脚本**就地改源**却没抢树锁。
    #    同一条病本轮第四次(E3 runner / extsel_e3 复放 / 本脚本)。后果与
    #    2026-08-26 三伤一样:并发下毒落在谁的基线上无法归属,而"读到别人
    #    写了一半的文件"报出来的红,与真发现长得一模一样。
    #    按共享主人翁制就地补,原逻辑一行未动。
    from mutation_tree_lock import tree_lock

    with tree_lock("mutation_replay_e3tail_2026_08_27"):
        return _main_locked()


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", type=int, default=55494, help="本窗口自建 PG 端口")
    args = ap.parse_args()

    _assert_tree_is_unmutated()
    print("起跑前树干净自证:9 发的锚点全在、替换文本一处不留")

    print("=" * 74)
    print("基线(不带变异)")
    print("=" * 74)
    baseline: dict[str, tuple[set[str], int]] = {}
    for pkg in PACKAGES:
        rc, red, total = _run(pkg, args.pg)
        baseline[pkg] = (red, total)
        print(f"  {pkg:4s} rc={rc} 用例={total} 红={sorted(red) or '无'}")
        if red:
            print(f"  🔴 {pkg} 基线不干净 —— 下面的红集差值会被它污染")

    verdicts = []
    queue = ([("外选", m) for m in MUTATIONS]
             + [("删修复", m) for m in FIX_MUTATIONS]
             + [("自选", m) for m in SELF_MUTATIONS])
    for origin, mut in queue:
        path = ROOT / mut["file"]
        backup = path.with_suffix(path.suffix + ".mutbak")
        before = _sha(path)
        shutil.copy2(path, backup)
        print()
        print("=" * 74)
        print(f"[{origin}] {mut['id']}  {mut['file']}")
        if mut.get("why"):
            print(f"  出题理由:{mut['why']}")
        print("=" * 74)
        try:
            _apply(path, mut["edits"])
            after = _sha(path)
            if after == before:
                raise SystemExit("停:变异没改动文件(sha 未变)")
            print(f"  在盘:sha {before} -> {after}(已变)")
            actual: dict[str, set[str]] = {}
            for pkg in mut["packages"]:
                rc, red, total = _run(pkg, args.pg)
                fresh = red - baseline[pkg][0]
                actual[pkg] = fresh
                print(f"  {pkg:4s} rc={rc} 用例={total} 新增红={sorted(fresh) or '无'}")
        finally:
            _restore(path, backup)
            restored = _sha(path)
            ok = restored == before
            print(f"  还原:sha {restored} {'OK 逐字节一致' if ok else 'X 不一致!'}")
            backup.unlink(missing_ok=True)
            if not ok:
                raise SystemExit("停:还原不一致,后面的结果全部不可信")

        exact = all(actual.get(p, set()) == set(names)
                    for p, names in mut["expect"].items())
        killed = any(actual.get(p) for p in mut["packages"])
        verdicts.append((origin, mut["id"], killed, exact, actual))
        print(f"  判定:{'杀' if killed else '存活'} · "
              f"红集{'精确相符' if exact else '**不符**'}")
        if not exact:
            for p, names in mut["expect"].items():
                print(f"    期望 {p}: {sorted(names)}")
                print(f"    实得 {p}: {sorted(actual.get(p, set()))}")

    print()
    print("=" * 74)
    print("汇总")
    print("=" * 74)
    bad = 0
    for origin, mid, killed, exact, actual in verdicts:
        flag = "OK " if (killed and exact) else "!! "
        if not (killed and exact):
            bad += 1
        print(f"  {flag}[{origin}] {mid}: {'杀' if killed else '存活'} · "
              f"{'红集精确' if exact else '红集不符'} · {actual}")
    cols = [("外选", MUTATIONS), ("删修复", FIX_MUTATIONS), ("自选", SELF_MUTATIONS)]
    parts = []
    for name, src in cols:
        rows = [v for v in verdicts if v[0] == name]
        assert len(rows) == len(src), f"{name} 栏发数对不上"
        parts.append(f"{name} {len(rows)} 发 · 杀 {sum(1 for v in rows if v[2])}")
    print("  机械计数(三栏不合并):" + " | ".join(parts)
          + f" | 红集不符合计 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
