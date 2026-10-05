# -*- coding: utf-8 -*-
"""工单 V3-C(Codex 三审 NO-GO @ 80c2fe565)四条 P1 + 一条 P2 的重放器。

两栏,机械分开报数(本仓纪律:栏不合并):
  · **删修复** —— 每条把本单的修复删掉/退回旧实现,证明"回到缺陷态必须变红"。
                 工单验收逐字要的就是这一栏:「每条 P1 配一发能杀旧实现的判据」。
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

    python scripts/mutation_replay_v3c_2026_08_28.py --pg 55495
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

# [V5-B B-2 · Codex P1-7] 本支上一轮**没有**基线硬门、也没有逐变量 DSN 枚举,
# 而 V4-C 有 —— 同一条谓词写两处,必有一处没人验。闸统一搬进公共模块,
# 判据 test_mutation_replayer_hardgate_contract 机械枚举所有复放器。
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
           "db": "geo_defgeo_w2_v3c_test",
           "env": ("TEST_DATABASE_URL",)},
    "e3": {"path": "tests/defgeo_e3_2026_08_26",
           "db": "geo_e3_v3c_test",
           "env": ("TEST_DATABASE_URL", "E3_TEST_DATABASE_URL")},
    # 🔴 [V5-B B-2] 这两个变量必须指向**两个不同的库**:conftest 与
    #    test_v2_monitoring_http_pg 各自 DROP SCHEMA public CASCADE 重建自己那份,
    #    指到同一个库上两边互相拆台 —— 而全绿/条数/两臂差值/rc 四个信号全部正常。
    #    上一轮本支写成 `for key in spec["env"]: env[key] = dsn`(一个 DSN 灌所有 key)。
    "w4": {"path": "tests/defensive_geo_w4_2026_08_22",
           "db": "geo_defgeo_w4_v3c_test",
           # 🔴 DEFGEO_W4_TEST_DB_URL 是这个包**唯一**认的那个;漏了它就落别窗容器。
           # 🔴 [V4-C 补] 这个包有**两个** DSN 变量:conftest 认
           #    DEFGEO_W4_TEST_DB_URL,而 test_v2_monitoring_http_pg.py 另外认
           #    DEFGEO_W4_HTTP_DB_URL(默认 55475 = 别窗容器)。漏后者时
           #    那 9 条 HTTP 判据整轮打在别人的库上,而四个信号全部正常。
           "env": {"DEFGEO_W4_TEST_DB_URL": "geo_defgeo_w4_v3c_test",
                   "DEFGEO_W4_HTTP_DB_URL": "geo_defgeo_w4http_v3c_test",
                   "TEST_DATABASE_URL": "geo_defgeo_w4_v3c_test"}},
    "woc": {"path": "tests/defgeo_woc_closure_2026_08_25",
            "db": "geo_defgeo_woc_v3c2_test",
            "env": ("TEST_DATABASE_URL",)},
}

# ── 各处修复的"新/旧"文本对 —————————————————————————————————————
_C2_NEW = '        payload["clusters_selection"] = _canonical_clusters(clusters_selection)\n'
_C2_OLD = (
    '        payload["clusters_selection"] = [\n'
    '            {"cluster_id": str(getattr(c, "cluster_id", None)\n'
    '                               or (c or {}).get("cluster_id") or ""),\n'
    '             "covered_count": int(getattr(c, "covered_count", None)\n'
    '                                  if getattr(c, "covered_count", None) is not None\n'
    '                                  else (c or {}).get("covered_count") or 0)}\n'
    '            for c in clusters_selection\n'
    '        ]\n'
)

_C1_NEW = (
    "-- UPDATE public.monitoring_run_cells c\n"
    "--    SET tenant_owner_user_id = b.owner_user_id\n"
    "--   FROM public.brands b,\n"
    "--        public.monitoring_keyword_settlements s\n"
    "--  WHERE c.tenant_owner_user_id IS NULL\n"
    "--    AND b.id = c.brand_id\n"
    "--    AND b.owner_user_id > 0\n"
    "--    AND s.settlement_reference = c.settlement_reference\n"
    "--    AND s.billing_user_id = b.owner_user_id;\n"
)
_C1_OLD = (
    "-- UPDATE public.monitoring_run_cells c\n"
    "--    SET tenant_owner_user_id = s.billing_user_id\n"
    "--   FROM public.brands b,\n"
    "--        public.monitoring_keyword_settlements s\n"
    "--  WHERE c.tenant_owner_user_id IS NULL\n"
    "--    AND b.id = c.brand_id\n"
    "--    AND b.owner_user_id > 0\n"
    "--    AND s.settlement_reference = c.settlement_reference\n"
    "--    AND s.billing_user_id > 0;\n"
)

_C4_CUTOFF = (
    '    if cutoff_at is not None:\n'
    '        where += " AND terminal_at IS NOT NULL AND terminal_at <= %s"\n'
    '        params.append(cutoff_at)\n'
)


#: **删修复**栏:每条 P1/P2 各退回一次缺陷态。
FIX_MUTATIONS = [
    # 🔴 [退役 @ V4-C · 2026-08-28] MUT-V3C-C1 锚在 §2 那条 UPDATE 上。
    #    V4-C 按 Codex fix-of-fix 把 §2 **整段退役**(自动回填一律取消),
    #    锚点随之消失 —— 这是**锚点过期**,不是残留,更不是缺陷。
    #    继任者:``scripts/mutation_replay_v4c_2026_08_28.py`` 的
    #    MUT-V4C-C1a(分档优先级)与 MUT-V4C-C1b(把回填加回去)。
    {
        "id": "MUT-V3C-C2",
        "why": "canonical 退回按不存在的 cluster_id 取值 ⇒ 对 Pydantic 调 .get ⇒ 500",
        "file": "services/defensive_geo/acceptance_evidence.py",
        "edits": [(_C2_NEW, _C2_OLD)],
        "packages": ["w2"],
        "expect": {
            "w2": {
                "test_e5_v2_cluster_confirm_over_real_http_lands_evidence",
                "test_e5_the_http_criterion_really_takes_the_cluster_leg",
                "test_e5_reconciler_labels_the_acceptance_evidence_of_every_orphan",
            },
        },
    },
    {
        "id": "MUT-V3C-C3",
        "why": "血缘谓词退回「给了 model 就算 provider_echo」(计划值恒被标成已证实)",
        "file": "services/monitoring_lineage.py",
        "edits": [("    if declared == MODEL_SOURCE_PROVIDER_ECHO and echoed:\n",
                   "    if echoed:\n")],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e4_21b_a_bare_model_without_a_declaration_is_not_an_echo",
                "test_e7_21_planned_model_is_planned_fallback_and_not_complete",
                "test_e7_23_the_production_shaped_call_really_lands_planned_fallback",
                "test_e7_25_cohort_refuses_planned_and_admits_echo",
            },
        },
    },
    {
        "id": "MUT-V3C-C4",
        "why": "摘掉 attempt 的 cutoff 约束 ⇒ 冻结报告又会被迟到 retry 改写",
        "file": "services/defensive_geo/monitoring/attempt_ledger.py",
        "edits": [(_C4_CUTOFF, "")],
        "packages": ["w4"],
        "expect": {
            "w4": {
                "test_a_late_attempt_does_not_change_the_frozen_cards",
                "test_the_live_view_does_see_the_late_attempt",
                "test_an_attempt_still_in_flight_is_not_in_the_frozen_face",
            },
        },
    },
    {
        "id": "MUT-V3C-C5",
        "why": "输入类拒绝退回可重试 503(前端会对永远不会成功的请求死循环)",
        "file": "api/defensive_geo_assist_api.py",
        "edits": [(
            '        raise _safe_error(\n'
            '            "VALIDATION_FAILED",\n'
            '            reason_key="legal_repair_input_rejected",\n',
            '        raise _safe_error(\n'
            '            "POLICY_UNAVAILABLE",\n'
            '            reason_key="legal_repair_input_rejected",\n')],
        "packages": ["woc"],
        "expect": {
            "woc": {
                "test_e2_28_a_plain_input_rejection_is_a_non_retryable_422_over_real_http",
                "test_e2_29_the_three_outcomes_split_by_who_can_fix_it",
            },
        },
    },
]

#: **自选**栏:打本单新判据自己,问它们有没有判别力。
SELF_MUTATIONS = [
    {
        "id": "MUT-V3C-S1",
        "why": ("对账器把档位写死成 proven —— 打 C-2 后半的两臂判据。"
                "只验 proven 那一臂的话这发会存活"),
        "file": "services/defensive_geo/activation_outbox.py",
        "edits": [('        r["acceptance_evidence"] = _ae.classify_acceptance(r)\n',
                   '        r["acceptance_evidence"] = _ae.EVIDENCE_PROVEN\n')],
        "packages": ["w2"],
        "expect": {
            "w2": {"test_e5_reconciler_labels_the_acceptance_evidence_of_every_orphan"},
        },
    },
    {
        "id": "MUT-V3C-S2",
        "why": ("adapter 恒报 planned_fallback(丢掉真回显)—— 打 C-3 的判别力臂。"
                "只有「有回显」那一臂抓得住,证明它不是恒真"),
        "file": "tools/monitoring/batch_monitor.py",
        "edits": [('        echo = str(data.get("echoed_model") or "").strip()\n',
                   '        echo = ""\n')],
        "packages": ["e3"],
        "expect": {
            "e3": {"test_e7_24_the_probe_would_notice_a_real_echo"},
        },
    },
    # 🔴 [退役 @ V5-C · 2026-08-28] MUT-V3C-S3 锚在出现率**自己那份** SQL 谓词
    #    (``extra += " AND a.monitoring_result_id = ANY(%s)"``)上。
    #    V5-C 的 C-2 把冻结裁剪并进 ``card_producer._rows_by_cell`` 单点,
    #    出现率与 evidence 从此共用同一把刀 —— 于是"只让出现率脱离冻结面"
    #    这件事在结构上**不再可表达**,这一发没有落点了。
    #
    #    继任者:**MUT-V4C-C2a**(摘掉 `_rows_by_cell` 的冻结裁剪)。
    #    本发原来的两条期望红
    #      · test_only_the_frozen_raw_results_count
    #      · test_an_empty_frozen_result_set_is_a_blank_not_a_zero
    #    都已逐条落在 C2a 的期望红集里 —— 覆盖没有丢,是**搬了家**。
    #    (本仓记过:并特例回主路会让终态标记搬家,退役必须写继任者,
    #     否则下一个人只看到"少了一发"。)
    #
    # 🔴 [退役 @ V4-C · 2026-08-28] MUT-V3C-S4 锚在 §1 的
    #    ``cells_backfillable`` 档上,该档已随「取消自动回填」一并取消。
    #    继任者:V4-C 的 MUT-V4C-S2(partition_ok 说谎)。
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

    with tree_lock("mutation_replay_v3c_2026_08_28"):
        return _main_locked()


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", type=int, default=55495, help="本窗口自建 PG 端口")
    args = ap.parse_args()

    muts_all = list(FIX_MUTATIONS) + list(SELF_MUTATIONS)
    assert_tree_is_unmutated(ROOT, muts_all)
    print(f"起跑前自证:{len(muts_all)} 发的锚点全在"
          "(与 HEAD 逐字节一致 —— 锚命中 1 次**不代表**干净)")
    assert_every_dsn_var_is_declared(ROOT, PACKAGES, args.pg, label="V3-C")

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
    # [V5-B B-2 · Codex P1-7] 上一轮这里只 print 一句警告就继续跑 ——
    # Codex 反例:rc=5/red=∅ 与 rc=1/red={baseline_red} 两种都已进到第一笔 artifact。
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
                # [V5-B B-3①] 红集裁定前先判臂可不可信(rc 白名单 + 条数守恒)。
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
