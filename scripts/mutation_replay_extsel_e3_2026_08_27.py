"""外选 E3 族存活变异 · **重放器**(工单「判据洞补齐」验收用)。

它做什么
--------
把 Review 终单里 E3 族**存活**的那几发变异逐字重放一遍,证明本单新补的判据
真的把它们杀掉了。每发的期望红集**写死在这里**,跑完与实际红集逐条比对 ——
"变红了"不算数,"红的是我说的那几条"才算。

它不做什么
----------
🔴 这不是全分母存活裁定。本单只跑**装着期望红集的那几个包**;
   "存活/冗余"的裁定由 Review 在合流尖上按全分母跑外选终单。
   (「存活」这个词离开分母就没有意义 —— 本仓记过。)

纪律(逐条对应本仓记过的坑)
--------------------------
· 锚点唯一性:``src.count(anchor)`` 必须 == 1,不等于就**当场停**,不猜;
· 备份落盘(``.mutbak``),还原用 ``tmp + os.replace`` 原子替换 ——
  盘满时 ``open(w)`` 会把源文件截成 0 字节(本仓 2026-08-25 真发生过);
· 还原后**逐字节**核 sha,不核就等于没还原;
· **禁 git checkout** 撤销变异(会把别的改动一起带走);
· 串行,一次一发;库层的那一发(SQL 脚本)不改库,只改文件 ——
  判据自己在事务里摆库、跑完回滚。

用法
----
    python scripts/mutation_replay_extsel_e3_2026_08_27.py --pg 55493
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

# 🔴 Windows 控制台默认 GBK:打印 ✓/✗/═ 会 UnicodeEncodeError,而这句
#    如果炸在 finally 的还原打印上,`.mutbak` 就不会被清掉(2026-08-27 实测)。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# ── 包 → 环境变量(每个包自己的一次性库;库名安全栓由各包 conftest 自己验)──
PACKAGES = {
    "e3": {
        "path": "tests/defgeo_e3_2026_08_26",
        "db": "geo_e3crit_e3_test",
        "env": ("TEST_DATABASE_URL", "E3_TEST_DATABASE_URL"),
    },
    "pkgf": {
        "path": "tests/defensive_geo_pkgf_2026_08_23",
        "db": "geo_defgeo_pkgf_crit_test",
        "env": ("TEST_DATABASE_URL", "DEFGEO_PKGF_TEST_DB_URL"),
    },
    "w2": {
        "path": "tests/defensive_geo_w2_2026_08_21",
        "db": "geo_defgeo_w2_crit_test",
        "env": ("TEST_DATABASE_URL",),
    },
}

#: 每发 = (id, 文件, [(锚, 替换), ...], 跑哪些包, 期望红集{包: {测试名}})
#: 锚与替换**逐字取自** `C:\\AI-Test\\EXTSEL_E3_DRAFT_2026-08-27.md`,不重写。
MUTATIONS = [
    {
        "id": "MUT-EXTE3-03",
        "file": "db/monitoring_db.py",
        "edits": [(
            '                        tenant_owner_user_id=_cell_row.get("tenant_owner_user_id"),\n',
            '                        tenant_owner_user_id=int(actor_user_id),\n',
        )],
        "packages": ["e3", "pkgf"],
        "expect": {
            "e3": {"test_e1_44_no_caller_may_pass_anything_but_the_frozen_cell_column"},
            "pkgf": {"test_identity_review_appends_answered_through_the_live_chain",
                     "test_identity_review_by_a_non_owner_admin_also_appends_answered"},
        },
    },
    {
        "id": "MUT-EXTE3-05",
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [(
            "                WHERE tenant_owner_user_id <= 0)\n",
            "                WHERE tenant_owner_user_id < 0)\n",
        )],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e5_10_the_zero_census_sees_every_fabricated_tenant",
                "test_e5_11_a_bare_zero_alone_is_enough_to_stop_the_line",
            },
        },
    },
    {
        "id": "MUT-EXTE3-11",
        "file": "api/selection_api.py",
        "edits": [(
            '        "token_subject": _ae.token_subject_of(quote_id=quote_id, brand_id=brand_id),\n',
            '        "token_subject": None,\n',
        )],
        "packages": ["w2"],
        "expect": {
            "w2": {"test_e4_confirm_lands_every_irreproducible_acceptance_fact"},
        },
    },
    {
        "id": "MUT-EXTE3-15",
        "file": "api/defensive_geo_report_api.py",
        "edits": [(
            '                         _window["sampling_window_start"],\n'
            '                         _window["sampling_window_end"]))\n',
            '                         _window["sampling_window_end"],\n'
            '                         _window["sampling_window_start"]))\n',
        )],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e6_02_the_window_is_bound_lower_bound_first",
                "test_e6_10_a_cell_inside_the_sampling_window_is_really_picked_up",
            },
        },
    },
]


#: **自选**栏 —— 与外选分开报数(本仓纪律:两栏不合并)。
#: 出题目的不是再证一遍那四个洞,而是打**本单新判据自己**的每一组:
#: 每一发都瞄准一个新加的判据组,证明它不是只认得出那一发外选变异的形状。
SELF_MUTATIONS = [
    {
        "id": "MUT-SELF-01",
        "why": "打 e6 的 revision 取向(五卡绑的是不是**最新那一版**快照)",
        "file": "api/defensive_geo_report_api.py",
        "edits": [(
            '                        " ORDER BY revision DESC, id DESC LIMIT 1",\n',
            '                        " ORDER BY revision ASC, id DESC LIMIT 1",\n',
        )],
        "packages": ["e3"],
        "expect": {
            "e3": {
                "test_e6_01_the_two_report_queries_are_uniquely_locatable",
                "test_e6_12_the_latest_revision_wins",
            },
        },
    },
    {
        "id": "MUT-SELF-02",
        "why": "打 e5 的 §1 分档(待回填总数取反,三档之和就对不上)",
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "edits": [(
            "      WHERE tenant_owner_user_id IS NULL)                                     AS cells_tenant_null,\n",
            "      WHERE tenant_owner_user_id IS NOT NULL)                                 AS cells_tenant_null,\n",
        )],
        "packages": ["e3"],
        "expect": {
            "e3": {"test_e5_20_the_four_cell_counts_split_by_what_they_can_be_rebuilt_from"},
        },
    },
    {
        "id": "MUT-SELF-03",
        "why": "打 w2 修好的那条:它守的不止 token_subject 一列(actor 也在分母里)",
        "file": "api/selection_api.py",
        "edits": [(
            '        "actor": _ae.actor_of(token),\n',
            '        "actor": None,\n',
        )],
        "packages": ["w2"],
        "expect": {
            "w2": {"test_e4_confirm_lands_every_irreproducible_acceptance_fact"},
        },
    },
    {
        "id": "MUT-SELF-04",
        "why": ("打调用方锁的**另一种谎形**:不提 actor,改递格上另一列。"
                "🔴 实测它降级成了「租户取不到」——"
                "那条 SELECT 的投影是 (plan_hash, task_id, tenant_owner_user_id),"
                "**没有 brand_id** ⇒ .get() 返 None ⇒ 谓词抛 ⇒ 整跳不落账。"
                "所以 pkgF 两条身份判据都红在 len(rows)==2,不是红在归属值上。"
                "第一版我按「记成 9101」预测,红集不符 —— 预测错的是我,不是判据。"),
        "file": "db/monitoring_db.py",
        "edits": [(
            '                        tenant_owner_user_id=_cell_row.get("tenant_owner_user_id"),\n',
            '                        tenant_owner_user_id=_cell_row.get("brand_id"),\n',
        )],
        "packages": ["e3", "pkgf"],
        "expect": {
            "e3": {"test_e1_44_no_caller_may_pass_anything_but_the_frozen_cell_column"},
            "pkgf": {"test_identity_review_appends_answered_through_the_live_chain",
                     "test_identity_review_by_a_non_owner_admin_also_appends_answered"},
        },
    },
    {
        "id": "MUT-SELF-05",
        "why": "打 cell 类调用点锁(在调用点把冻结列剥掉 —— MUT-EXTE3-04 的形状换个写点)",
        "file": "db/monitoring_db.py",
        "edits": [(
            "        open_for_claim(cur, dict(row))\n",
            '        open_for_claim(cur, {k: v for k, v in dict(row).items()'
            ' if k != "tenant_owner_user_id"})\n',
        )],
        "packages": ["e3"],
        "expect": {
            "e3": {"test_e1_45_no_caller_may_rebuild_the_cell_dict_at_the_call_site"},
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
        cwd=str(ROOT), env=env, capture_output=True, text=True)
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
    # 🔴 [窗口B 2026-08-27 合流尖机制自证抓到] 本脚本**就地改源**却没抢树锁。
    #    同一条病本轮是第三次(E3 runner / 本脚本);后果与 2026-08-26 三伤一样:
    #    并发下毒落在谁的基线上无法归属,而"读到别人写了一半的文件"报出来的红
    #    与真发现长得一模一样。按共享主人翁制就地补,原逻辑一行未动。
    from mutation_tree_lock import tree_lock

    with tree_lock("mutation_replay_extsel_e3_2026_08_27"):
        return _main_locked()


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", type=int, default=55493, help="本窗口自建 PG 端口")
    args = ap.parse_args()

    print("═" * 74)
    print("基线(不带变异)")
    print("═" * 74)
    baseline: dict[str, tuple[set[str], int]] = {}
    for pkg in PACKAGES:
        rc, red, total = _run(pkg, args.pg)
        baseline[pkg] = (red, total)
        print(f"  {pkg:5s} rc={rc} 用例={total} 红={sorted(red) or '无'}")
        if red:
            print(f"  🔴 {pkg} 基线不干净 —— 下面的红集差值会被它污染")

    verdicts = []
    queue = ([("外选", m) for m in MUTATIONS]
             + [("自选", m) for m in SELF_MUTATIONS])
    for origin, mut in queue:
        path = ROOT / mut["file"]
        backup = path.with_suffix(path.suffix + ".mutbak")
        before = _sha(path)
        shutil.copy2(path, backup)
        print()
        print("═" * 74)
        print(f"[{origin}] {mut['id']}  {mut['file']}")
        if mut.get("why"):
            print(f"  出题理由:{mut['why']}")
        print("═" * 74)
        try:
            _apply(path, mut["edits"])
            after = _sha(path)
            if after == before:
                raise SystemExit("停:变异没改动文件(sha 未变)")
            print(f"  在盘:sha {before} → {after}(已变)")
            actual: dict[str, set[str]] = {}
            for pkg in mut["packages"]:
                rc, red, total = _run(pkg, args.pg)
                fresh = red - baseline[pkg][0]
                actual[pkg] = fresh
                print(f"  {pkg:5s} rc={rc} 用例={total} 新增红={sorted(fresh) or '无'}")
        finally:
            _restore(path, backup)
            restored = _sha(path)
            ok = restored == before
            print(f"  还原:sha {restored} {'✓ 逐字节一致' if ok else '✗ 不一致!'}")
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
    print("═" * 74)
    print("汇总")
    print("═" * 74)
    bad = 0
    for origin, mid, killed, exact, actual in verdicts:
        flag = "OK " if (killed and exact) else "!! "
        if not (killed and exact):
            bad += 1
        print(f"  {flag}[{origin}] {mid}: {'杀' if killed else '存活'} · "
              f"{'红集精确' if exact else '红集不符'} · {actual}")
    ext = [v for v in verdicts if v[0] == "外选"]
    slf = [v for v in verdicts if v[0] == "自选"]
    print(f"  机械计数(两栏不合并):"
          f"外选重放 {len(ext)} 发 · 杀 {sum(1 for v in ext if v[2])} | "
          f"自选 {len(slf)} 发 · 杀 {sum(1 for v in slf if v[2])} | "
          f"红集不符合计 {bad}")
    assert len(ext) == len(MUTATIONS) and len(slf) == len(SELF_MUTATIONS)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
