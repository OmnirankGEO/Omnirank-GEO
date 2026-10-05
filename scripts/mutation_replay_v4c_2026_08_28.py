# -*- coding: utf-8 -*-
"""工单 V4-C(Codex fix-of-fix NO-GO @ 48979fb5c)C-1/C-2/C-3 的重放器。

两栏,机械分开报数(本仓纪律:栏不合并):
  · **删修复** —— 每条把本单的修复删掉/退回旧实现,证明"回到缺陷态必须变红"。
                 工单验收逐字要的就是这一栏:每条修复配一发能杀旧实现的判据。
  · **自选**   —— 打本单新判据自己,问它们是不是只认得出那一发的形状
                 (判别力自证:恒真的判据在这一栏会存活)。

纪律(与前两轮同,不重述理由,只列):锚点 ``count==1`` 否则当场停 ·
``.mutbak`` 落盘 · ``tmp + os.replace`` 原子还原(``mkstemp`` 的 fd 必须关,
否则 Windows WinError 32)· 还原后逐字节核 sha · 禁 ``git checkout`` · 串行 ·
每发红集写死在题里逐条比对 · junit 读不出来直接 ``SystemExit``
(「整包起不来」不许记成零新增红)· 起跑前树干净自证。

🔴 这不是全分母存活裁定 —— 只跑装着期望红集的那几个包。

🔴 **每个包的 DSN 环境变量必须写全**(2026-08-28 实测的坑):
   ``tests/defensive_geo_w4_2026_08_22/conftest.py`` 的 ``_url()`` 是
   ``os.getenv("DEFGEO_W4_TEST_DB_URL") or DEFAULT_THROWAWAY_URL``
   —— 它**根本不读** ``TEST_DATABASE_URL``。只设后者时它静默落到默认端口
   55475,那是**别窗**的容器,并在那里 ``DROP SCHEMA public CASCADE``。
   全绿、条数正常、基线与变异臂的差值也自洽 —— 四个信号全部正常,
   因为两臂打在同一个错库上。所以 ``PACKAGES[*]["env"]`` 是逐包核过 conftest
   的**全集**,不是惯例名。

用法::

    python scripts/mutation_replay_v4c_2026_08_28.py --pg 55495
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

# [V5-B B-2/B-3] 硬门谓词一律走公共模块 —— 上一轮 V4-C 有闸、V3-C 没有,
# 正因为两支各写一份。判据 test_mutation_replayer_hardgate_contract 机械枚举
# scripts/mutation_replay_*.py,谁再自己写一份就红。
from mutation_replay_common import (  # noqa: E402
    assert_baseline_clean,
    assert_every_dsn_var_is_declared,
    assert_mutation_arm_trustworthy,
    assert_tree_is_unmutated,
    dsn_env,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PACKAGES = {
    "w2": {"path": "tests/defensive_geo_w2_2026_08_21",
           "db": "geo_defgeo_w2_v4c_test",
           "env": ("TEST_DATABASE_URL",)},
    "e3": {"path": "tests/defgeo_e3_2026_08_26",
           "db": "geo_e3_v4c_test",
           "env": ("TEST_DATABASE_URL", "E3_TEST_DATABASE_URL")},
    # 🔴 [V4-C · C-3] w4 有**两个** DSN 变量,不是一个:
    #    conftest 认 DEFGEO_W4_TEST_DB_URL,而 test_v2_monitoring_http_pg.py:34
    #    另外认 DEFGEO_W4_HTTP_DB_URL(默认 55475 = 别窗容器)。
    #    上一轮我只补了前者 —— 同一个坑的第二形态,那 9 条 HTTP 判据一直打在别人的库上。
    #    现在 env 是**逐包 AST 枚举** conftest 与全部判据文件得到的全集,不是惯例名。
    # 🔴 这个包的两个 DSN 变量必须指向**两个不同的库**:conftest 与
    #    test_v2_monitoring_http_pg 各自 DROP SCHEMA 重建自己那一份 schema,
    #    指到同一个库上两边会互相拆台。所以 env 在这里是 var→db 的映射。
    "w4": {"path": "tests/defensive_geo_w4_2026_08_22",
           "db": "geo_defgeo_w4_v4c_test",
           "env": {"DEFGEO_W4_TEST_DB_URL": "geo_defgeo_w4_v4c_test",
                   "DEFGEO_W4_HTTP_DB_URL": "geo_defgeo_w4http_v4c_test",
                   "TEST_DATABASE_URL": "geo_defgeo_w4_v4c_test"}},
    "pkgh": {"path": "tests/defensive_geo_pkgh_2026_08_23",
             "db": "geo_defgeo_pkgh_v4c_test",
             "env": ("TEST_DATABASE_URL",)},
    "woc": {"path": "tests/defgeo_woc_closure_2026_08_25",
            "db": "geo_defgeo_woc_v4c_test",
            "env": ("TEST_DATABASE_URL",)},
}

#: **删修复**栏:每条把 V4-C 的修复退回缺陷态。
FIX_MUTATIONS = [
    {
        "id": "MUT-V4C-C1a",
        "why": "census 的「没有今天的 owner」优先级分支失效 ⇒ 该格改落进付款人档",
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [("             WHEN b.owner_user_id IS NULL OR b.owner_user_id <= 0\n"
                   "                  THEN 'manual_no_owner_today'\n",
                   "             WHEN FALSE\n"
                   "                  THEN 'manual_no_owner_today'\n")],
        "packages": ["e3"],
        "expect": {"e3": {
            "test_e5_20_the_four_buckets_are_mutually_exclusive_and_exhaustive",
            "test_e5_21_owner_null_plus_a_payer_lands_in_exactly_one_bucket",
        }},
    },
    {
        "id": "MUT-V4C-C1b",
        "why": "把一条按今天 owner 回填的 UPDATE 加回脚本(= §2 退役被撤销)",
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [("-- ── §2 回填:**已退役,不留可执行残骸** ──────────────────────────────────────\n",
                   "UPDATE public.monitoring_run_cells c\n"
                   "   SET tenant_owner_user_id = b.owner_user_id\n"
                   "  FROM public.brands b\n"
                   " WHERE c.tenant_owner_user_id IS NULL\n"
                   "   AND b.id = c.brand_id AND b.owner_user_id > 0;\n"
                   "-- ── §2 回填:**已退役,不留可执行残骸** ──────────────────────────────────────\n")],
        "packages": ["e3"],
        "expect": {"e3": {
            "test_e5_01_the_script_yields_exactly_two_executable_census_statements",
            "test_e5_22_a_transferred_brand_is_not_treated_as_corroborated",
            "test_e5_23_running_the_whole_script_changes_no_tenant_at_all",
            "test_e5_24_the_script_contains_no_tenant_write_even_in_comments",
        }},
    },
    {
        # 🔴 [工单 V5-C 重锚] 只动**锚点**这一条轴:语义与本发原来一模一样
        #    (「把冻结裁剪整段摘掉」),红集因为被测代码收口而变宽 ——
        #    V5-C 把 evidence 与出现率并到同一把刀(``_rows_by_cell``)上,
        #    所以这一刀摘掉之后,两条链一起失守。
        #    红集不是猜的:下面这几条各自打的是哪个数,见交付文逐条对照。
        "id": "MUT-V4C-C2a",
        "why": "摘掉 _rows_by_cell 的冻结裁剪 ⇒ evidence 与出现率同时脱离冻结面",
        "file": "services/defensive_geo/monitoring/card_producer.py",
        "edits": [('        if frozen is not None:\n'
                   '            rows = [r for r in rows\n'
                   '                    if not _carries_a_result(r)\n'
                   '                    or int(r.monitoring_result_id) in frozen]\n', "")],
        "packages": ["w4"],
        "expect": {"w4": {
            "test_an_answered_attempt_outside_the_frozen_set_does_not_count",
            "test_an_ambiguous_result_outside_the_frozen_set_does_not_reach_the_report",
            "test_every_terminal_state_is_bound_exactly_when_it_carries_a_result",
            "test_only_the_frozen_raw_results_count",
            "test_an_empty_frozen_result_set_is_a_blank_not_a_zero",
            "test_the_frozen_set_is_applied_before_canonical_selection",
        }},
    },
    {
        "id": "MUT-V4C-C2b",
        "why": "把 scoped-out 的监测快照条目加回面板(= 回到 not_ready 占位)",
        "file": "services/defensive_geo/customer_links.py",
        "edits": [("    panel = [\n        _entry(kind=\"diagnosis_report\"",
                   "    panel = [\n        _entry(kind=\"monitoring_snapshot\", url=None,"
                   " brand_name=brand_name,\n               status=\"not_ready\","
                   " blocked_reason_key=\"customer_link_not_ready\"),\n"
                   "        _entry(kind=\"diagnosis_report\"")],
        "packages": ["pkgh"],
        "expect": {"pkgh": {"test_scoped_out_kinds_never_reach_the_panel"}},
    },
    {
        "id": "MUT-V4C-C2c",
        "why": "文案改回承诺「做完会自动出现」",
        "file": "services/defensive_geo/copy_registry.py",
        "edits": [('        "这一类链接现在还没有；要等前一步做完才会有。",',
                   '        "这一类链接要等前一步做完才会有；做完会自动出现在这里。",')],
        "packages": ["pkgh"],
        "expect": {"pkgh": {
            "test_the_not_ready_copy_promises_nothing_about_automatic_appearance",
            "test_frontend_copy_file_is_byte_identical_to_generator",
        }},
    },
]

#: **自选**栏:打本单新判据自己,问它们有没有判别力。
SELF_MUTATIONS = [
    {
        # 🔴 [工单 V5-C 重锚] 同一发,同一个语义,换了落点:
        #    V5-C 之后"受不受冻结约束"这件事收到 ``_carries_a_result`` 一个
        #    谓词上,所以这一发直接把那个谓词打成恒真 ——
        #    于是**没有 result 行**的终态(平台报错)也被整类筛掉。
        "id": "MUT-V4C-S1",
        "why": ("把「带 result 行才受冻结约束」放大成「所有 attempt 都受约束」"
                " —— 平台报错会被整类筛掉。打 C-1 豁免的边界臂"),
        "file": "services/defensive_geo/monitoring/card_producer.py",
        "edits": [("    return r.monitoring_result_id is not None\n",
                   "    return True\n")],
        "packages": ["w4"],
        "expect": {"w4": {
            "test_engine_errors_are_not_filtered_away_by_the_frozen_set",
            "test_every_terminal_state_is_bound_exactly_when_it_carries_a_result",
        }},
    },
    {
        "id": "MUT-V4C-S2",
        "why": ("让脚本自报的 partition_ok **说谎**(数据闭合却报 false)——"
                "打「Deploy 读的那个布尔不许说谎」那一位。"
                "注:反方向(写成 OR TRUE 恒真)在单一 CASE 下**抓不到** ——"
                "划分结构上不可能破,两个信号永远不冲突。那种存活是**冗余**"
                "不是洞:真正的守卫是判据自己加出来的那个和"),
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [("     = COUNT(*))                                                          AS partition_ok\n",
                   "     = COUNT(*) + 1)                                                      AS partition_ok\n")],
        "packages": ["e3"],
        "expect": {"e3": {
            "test_e5_20_the_four_buckets_are_mutually_exclusive_and_exhaustive",
            "test_e5_21_owner_null_plus_a_payer_lands_in_exactly_one_bucket",
            "test_e5_22_a_transferred_brand_is_not_treated_as_corroborated",
        }},
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
    env, dsn = dsn_env(dict(os.environ), spec, port)
    out = _mktemp(None, ".junit.xml")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", spec["path"], "-q", "--tb=no",
         "-p", "no:randomly", f"--junitxml={out}"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        # 🔴 子进程输出是 UTF-8;Windows 上不显式指定,父进程按 GBK 解码,
        #    pytest 一打出中文就 UnicodeDecodeError —— 而它抛在读管道的
        #    **线程**里,主进程只看到半截结果。
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


def main() -> int:
    # 🔴 就地改源的脚本**必须**抢树锁(常驻 census
    #    ``scripts/test_mutation_tree_lock.py`` 守这一条)。
    from mutation_tree_lock import tree_lock

    with tree_lock("mutation_replay_v4c_2026_08_28"):
        return _main_locked()


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", type=int, default=55495, help="本窗口自建 PG 端口")
    args = ap.parse_args()

    muts_all = list(FIX_MUTATIONS) + list(SELF_MUTATIONS)
    assert_tree_is_unmutated(ROOT, muts_all)
    print(f"起跑前自证:{len(muts_all)} 发的锚点全在"
          "(与 HEAD 逐字节一致 —— 锚命中 1 次**不代表**干净)")
    assert_every_dsn_var_is_declared(ROOT, PACKAGES, args.pg, label="V4-C")

    print("=" * 74)
    print("基线(不带变异)")
    print("=" * 74)
    baseline: dict[str, tuple[set[str], int]] = {}
    observed: dict[str, tuple[int, set[str]]] = {}
    for pkg in PACKAGES:
        rc, red, total = _run(pkg, args.pg)
        baseline[pkg] = (red, total)
        observed[pkg] = (rc, red)
        print(f"  {pkg:4s} rc={rc} 用例={total} 红={sorted(red) or '无'}")
    # [V4-C C-3 → V5-B 收编公共] 基线脏 ⇒ 在第一发变异之前非零退出、零落盘。
    #  理由与实测都写在 mutation_replay_common.assert_baseline_clean。
    assert_baseline_clean(observed)

    verdicts = []
    queue = ([("删修复", m) for m in FIX_MUTATIONS]
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
                # [V5-B B-3①] 先判臂可不可信,再算红集差值:rc=2 配一份
                # 只含预期红集的 partial junit,旧代码照样报「红集精确」返回 0。
                assert_mutation_arm_trustworthy(
                    mut["id"], pkg, rc=rc, total=total,
                    baseline_total=baseline[pkg][1])
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
    cols = [("删修复", FIX_MUTATIONS), ("自选", SELF_MUTATIONS)]
    parts = []
    for name, src in cols:
        rows = [v for v in verdicts if v[0] == name]
        assert len(rows) == len(src), f"{name} 栏发数对不上"
        parts.append(f"{name} {len(rows)} 发 · 杀 {sum(1 for v in rows if v[2])}")
    print("  机械计数(两栏不合并):" + " | ".join(parts)
          + f" | 红集不符合计 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
