"""工单A(诊断资金链四修)的撕锁 runner —— 逐发变异,记录哪些判据变红。

纪律(每一条都是踩过的坑)
--------------------------
· **禁并发**:本 runner 就地改源文件。同一棵树上同时跑两个 runner
  (或一边跑一边有人编辑)会互相污染,结论作废。
· **cp 备份还原,禁 `git checkout`**:工作树里有别的窗口未提交的改动,
  `git checkout` 会连他们的一起抹掉。
· **锚点先自证唯一**:每个 old 串必须在目标文件里恰好命中 1 次,
  否则就是"考错题"(改到了别处,或者一次改了两处)。
· **行尾按文件本身走**:本仓 LF/CRLF 混用,用 LF 字面量去 replace CRLF 文件是 0 命中。
· **还原后逐字节核对**:还原不干净 = 后面每一发都在污染的树上跑。
· **产物落盘**:每发的完整 nodeid 全集写进 JSON,不靠"我记得它红了"。

变异来源分两栏,**分开报数**
---------------------------
· ``external`` —— 逐条**照抄工单 / Codex 终审报告的条款**造出来的违规。
  出题人不是判据作者:条款是 Review-CTO 与 Codex 写的,我只是把每一条
  翻成"把这条要求拆掉会怎样"。
· ``self`` —— 我自己(判据作者)挑的。这一栏单独算,因为自己出题自己批改不算数。

另配一发 ``liveness`` 活性对照:一个**必然**打红判据的改动。
它全绿意味着尺子根本没在量(判据没跑起来 / 选择器写错 / 库没建成)。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]

# 🔴 [机制令 ① · E1-3 补接] 本 runner **就地改源文件** —— 必须抢树级锁。
#    落地那轮只接了 extsel / wob / survivor 三个,这个漏了。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mutation_tree_lock import tree_lock  # noqa: E402

#: 每发变异要跑的判据面。
#: 含 ``tests/defensive_geo_2026_08_21/test_wp2_funding_matrix.py`` 是因为
#: 资金矩阵那两格的期望值分母长在那里(改矩阵必须让它红)。
TEST_TARGETS = [
    "tests/defgeo_funding_p0_2026_08_25",
    "tests/defensive_geo_2026_08_21/test_wp2_funding_matrix.py",
]

#: 本包自己的库(conftest 会从这个地址派生 postgres 并**新建**一个
#: ``defgeo_p0fix_<hex>_test``,这里给的只是服务器地址 + 命名模板)。
DEFAULT_DSN = "postgresql://geo_admin:p0fixpass@localhost:55437/defgeo_p0fix_test"

#: 🔴 ``tests/defensive_geo_2026_08_21`` 的 conftest 直接连 ``TEST_DATABASE_URL``
#:    **那个库本身**(不新建),库名必须同时含 defgeo 与 test,而且必须**已存在**。
#:    第一次跑本 runner 时我把两者混成一个,于是那 65 条判据全 error(库不存在)
#:    —— 基线门当场拦下,没让"基线是红的"混进变异结论里。那道门是有用的。
DEFAULT_LEGACY_DSN = os.getenv(
    "DEFGEO_REGRESS_DSN",
    "postgresql://geo_admin:p0fixpass@localhost:55437/geo_defgeo_regress_test")

API = "api/defensive_geo_api.py"
RUNS = "services/diagnosis_runs.py"
PROJ = "services/defensive_geo/funding_projection.py"
GUARD = "services/startup_schema_guards.py"
MIG050 = "db/migration_050_diagnosis_payer_identity_2026_08_25.sql"


def M(mid, origin, path, old, new, note):
    return {"id": mid, "origin": origin, "file": path, "old": old, "new": new, "note": note}


MUTATIONS = [
    # ══════════════════════════════════════════════════════════════════════
    # external —— 逐条照抄工单 A-1..A-4 与 Codex P0-1/P0-2/P0-3/P1-4 的条款
    # ══════════════════════════════════════════════════════════════════════
    M("E01", "external", PROJ,
      '        billing_mode_projection="paid",\n        # 对外仍是 exempt_recorded',
      '        billing_mode_projection="exempt",\n        # 对外仍是 exempt_recorded',
      "A-1①:admin 平台格改回借 exempt(Codex P0-1 原形态)"),
    M("E02", "external", PROJ,
      '        billing_mode_projection="paid",     # 同 admin 格,理由见上',
      '        billing_mode_projection="exempt",     # 同 admin 格,理由见上',
      "A-1①:sponsor 平台格改回借 exempt"),
    M("E03", "external", API,
      "        payer_user_id = int(platform_uid)",
      "        payer_user_id = int(tenant)",
      "A-1①:平台腿不再记真实 payer(记成租户)"),
    M("E04", "external", API,
      "                split_snapshot=split_snapshot, payer_user_id=payer_user_id,",
      "                split_snapshot=split_snapshot,",
      "A-1①:confirm 不把 payer 透传进 run 行"),
    M("E05", "external", RUNS,
      '           "payer_user_id=COALESCE(%s::integer, payer_user_id) WHERE run_token=%s")',
      '           "WHERE run_token=%s")',
      "A-1①:回填不写 payer 列(列永远是 NULL)"),
    M("E06", "external", RUNS,
      '                user_id=settlement_payer_user_id(run), freeze_table=run["freeze_backend"],\n'
      '                reason=(f"诊断完成 run={run_token}" if _actual is None',
      '                user_id=run["owner_user_id"], freeze_table=run["freeze_backend"],\n'
      '                reason=(f"诊断完成 run={run_token}" if _actual is None',
      "A-1③:commit_freeze 改回拿 owner 定位(Codex P0-1 第二层)"),
    M("E07", "external", RUNS,
      '                user_id=settlement_payer_user_id(run), freeze_table=run["freeze_backend"],\n'
      '                reason=f"诊断失败/中断退款 run={run_token}",',
      '                user_id=run["owner_user_id"], freeze_table=run["freeze_backend"],\n'
      '                reason=f"诊断失败/中断退款 run={run_token}",',
      "A-1③:release_freeze 改回拿 owner 定位"),
    M("E08", "external", API,
      '    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]\n'
      "    return _PRICING_CATALOG_SCHEME + \":\" + digest",
      '    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]\n'
      "    return _PRICING_CATALOG_SCHEME + \":constant\" + digest[:0]",
      "A-2①:版本又变回常量(改价不再改版本)"),
    M("E09", "external", API,
      "    if _frozen_amount != _exact_confirmed:",
      "    if False and _frozen_amount != _exact_confirmed:",
      "A-2②:删掉金额保险丝(确认 650 实冻 820 照样成交)"),
    M("E10", "external", API,
      '            if preview["pricing_catalog_version"] != _live_version:',
      '            if preview["pricing_catalog_version"] != preview["pricing_catalog_version"]:',
      "A-2②:版本比对恒等(闸永不触发)"),
    M("E11", "external", API,
      "    if _frozen_amount != _exact_confirmed:",
      '    if policy == "personal_wallet" and _frozen_amount != _exact_confirmed:',
      "A-2③:金额校验漏掉平台腿"),
    M("E12", "external", RUNS,
      "        _org_settle_points = _org_ceiling if _org_actual is None else int(_org_actual)",
      "        _org_settle_points = _org_ceiling",
      "A-3①:org 臂改回恒按预留上限结算(Codex P0-3 原形态)"),
    M("E13", "external", RUNS,
      "        if _org_err:\n"
      "            # 与个人腿**同一处置**",
      "        if False and _org_err:\n"
      "            # 与个人腿**同一处置**",
      "A-3②:org 臂忽略疑点/不可判,继续自动结算"),
    M("E14", "external", RUNS,
      "    if _identity_suspected(snap) or _identity_suspected(durable):\n"
      "        return None, IDENTITY_REVIEW_REASON",
      "    if _identity_suspected(snap) or _identity_suspected(durable):\n"
      "        return None, None",
      "A-3②:疑似身份失败改成自动全额结算"),
    M("E15", "external", API,
      '        split_snapshot = result.get("physical_split_snapshot")     # [A-4]\n'
      "        handle = {\n"
      '            "kind": spec.handle_kind,\n'
      "            # 0 价时如实标注",
      "        split_snapshot = None     # [A-4]\n"
      "        handle = {\n"
      '            "kind": spec.handle_kind,\n'
      "            # 0 价时如实标注",
      "A-4:个人腿不再取 billing 返回的三池拆分"),
    M("E16", "external", API,
      "                split_snapshot=split_snapshot, payer_user_id=payer_user_id,",
      "                payer_user_id=payer_user_id,",
      "A-4:confirm 不把 split 透传进 run 行(Codex P1-4 原形态)"),
    M("E17", "external", RUNS,
      "        _persist_freeze_handle(run_token, int(freeze_id), str(freeze_backend),\n"
      "                               split_snapshot, payer_user_id=payer_user_id, _cursor=cur)",
      "        _persist_freeze_handle(run_token, int(freeze_id), str(freeze_backend),\n"
      "                               None, payer_user_id=payer_user_id, _cursor=cur)",
      "A-4:start_run 收到了 split 却不往下传"),
    M("E18", "external", API,
      "    _task_ref = _run_freeze_task_ref(run_token)",
      '    _task_ref = "defgeo_" + run_token',
      "A-1①:冻结 task_ref 与 run 行的三元组锚又对不上"),

    # ══════════════════════════════════════════════════════════════════════
    # self —— 判据作者自己挑的(单独报数)
    # ══════════════════════════════════════════════════════════════════════
    M("S01", "self", RUNS,
      '    if value is None or (isinstance(value, str) and not value.strip()):\n'
      '        value = run.get("owner_user_id")',
      '    if value is None or (isinstance(value, str) and not value.strip()):\n'
      "        value = None",
      "payer 谓词的 owner 回落被删(个人腿全部定位不到)"),
    M("S02", "self", RUNS,
      '    value = run.get("payer_user_id")',
      '    value = run.get("owner_user_id")',
      "payer 谓词优先级反转(永远读 owner)"),
    M("S03", "self", RUNS,
      "    fid, tref = run.get(\"freeze_id\"), run.get(\"freeze_task_ref\")\n"
      "    payer = settlement_payer_user_id(run)          # [A-1] 平台承担腿的冻结不在 owner 名下",
      "    fid, tref = run.get(\"freeze_id\"), run.get(\"freeze_task_ref\")\n"
      "    payer = run.get(\"owner_user_id\")          # [A-1] 平台承担腿的冻结不在 owner 名下",
      "_frozen_total_for_run 改回读 owner"),
    M("S04", "self", RUNS,
      "    fid, tref = run.get(\"freeze_id\"), run.get(\"freeze_task_ref\")\n"
      "    payer = settlement_payer_user_id(run)          # [A-1] 同上:按 payer 定位冻结行",
      "    fid, tref = run.get(\"freeze_id\"), run.get(\"freeze_task_ref\")\n"
      "    payer = run.get(\"owner_user_id\")          # [A-1] 同上:按 payer 定位冻结行",
      "_reserved_split_for_run 改回读 owner"),
    M("S05", "self", RUNS,
      '            b, fid, fstatus = _double_table_locate(settlement_payer_user_id(c), c["freeze_task_ref"])',
      '            b, fid, fstatus = _double_table_locate(c["owner_user_id"], c["freeze_task_ref"])',
      "sweeper 收尸定位改回 owner(平台单会被判成无冻结)"),
    M("S06", "self", RUNS,
      '           "payer_user_id=COALESCE(%s::integer, payer_user_id) WHERE run_token=%s")',
      '           "payer_user_id=%s WHERE run_token=%s")',
      "回填改成无条件写 payer(不传时把真 payer 抹成 NULL)"),
    M("S07", "self", RUNS,
      "    total = (reserved_total_provider or (lambda: _frozen_total_for_run(run)))()",
      "    total = _frozen_total_for_run(run)",
      "共用谓词忽略 provider(org 臂读不到预留总额)"),
    M("S08", "self", GUARD,
      '                 "payer_user_id"}',
      '                 }',
      "启动期 fail-closed 不再要求 payer 列(漏跑迁移变静默)"),
    M("S09", "self", MIG050,
      "    ADD COLUMN IF NOT EXISTS payer_user_id INTEGER;",
      "    ADD COLUMN IF NOT EXISTS payer_uid_typo INTEGER;",
      "迁移 050 加错列名(库里根本没有 payer_user_id)"),
    M("S10", "self", RUNS,
      '    if run["billing_mode"] == "exempt":\n'
      '        ok, final = _terminal_local_txn(run_token, ["running"], "completed_exempt", "published", snapshot)',
      '    if run["billing_mode"] in ("exempt", "paid"):\n'
      '        ok, final = _terminal_local_txn(run_token, ["running"], "completed_exempt", "published", snapshot)',
      "commit_run 的 exempt 短路扩大到 paid(零 billing 调用)"),

    # ══════════════════════════════════════════════════════════════════════
    # liveness —— 活性对照:必然打红。它全绿 = 尺子根本没在量。
    # ══════════════════════════════════════════════════════════════════════
    M("L00", "liveness", PROJ,
      '        handle_kind="platform_cost_ledger",\n'
      "        handle_requires_approval_ref=False,\n"
      '        # 平台账不存在"余额不足"',
      '        handle_kind="wallet_freeze",\n'
      "        handle_requires_approval_ref=False,\n"
      '        # 平台账不存在"余额不足"',
      "活性对照:平台 handle 伪装成钱包冻结(既有判据点名的形态)"),
]


# ══════════════════════════════════════════════════════════════════════════
def _variants(pattern):
    lf = pattern.replace("\r\n", "\n")
    return [lf, lf.replace("\n", "\r\n")]


def apply_mutation(path: pathlib.Path, old: str, new: str) -> None:
    text = io.open(path, encoding="utf-8", newline="").read()
    for o, n in zip(_variants(old), _variants(new)):
        hits = text.count(o)
        if hits == 1:
            io.open(path, "w", encoding="utf-8", newline="").write(text.replace(o, n))
            return
        if hits > 1:
            raise SystemExit("锚点不唯一(命中 %d 次):%r" % (hits, o[:90]))
    raise SystemExit("锚点零命中(LF/CRLF 两种形态都试过):%r" % (old[:120],))


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(io.open(path, "rb").read()).hexdigest()


def run_tests(dsn: str, junit: pathlib.Path, legacy_dsn: str = DEFAULT_LEGACY_DSN):
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = legacy_dsn      # 既有 defgeo 判据包连的就是这一个库
    env["DEFGEO_P0FIX_TEST_DSN"] = dsn         # 本包据此新建一次性库
    cmd = [sys.executable, "-m", "pytest", *TEST_TARGETS, "-q", "-p", "no:warnings",
           "--junitxml", str(junit)]
    proc = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True,
                          errors="replace")
    return proc.returncode, proc.stdout[-4000:]


class RunnerContractError(RuntimeError):
    """跑数本身不可信 —— 与"被测代码有缺陷"是两件事,不许混进结果里。"""


def assert_run_is_trustworthy(rc: int, junit: pathlib.Path, tail: str, what: str):
    """[E1-3① = Codex 二审 §8] 一次 pytest 跑完之后,先问"这次跑数算不算数"。

    🔴 原来这里有三个洞,每一个都会把**没跑成**伪装成**跑成了且全绿**:

      ① ``rc`` 只打印不检查。pytest 的 rc 2/3/4(collect 错、内部错、用法错)
         配上一个空的 / 不存在的 junit,下面 ``or []`` 一折就成了"零红";
      ② junit 缺失时 ``red_nodeids`` 返回 ``None``,``None or []`` = 空红集 ——
         "证据文件没生成"和"证据显示全绿"长得一模一样;
      ③ 变异轮里 ``killed = bool(red)``,于是 ``None`` ⇒ ``killed=False``
         ⇒ 这一发被记成**存活**。存活是要交出去当判据洞用的结论。

    合同:``rc`` 只允许 0(全绿)或 1(有失败);junit 必须真的存在。
    其余一律抛 —— 宁可停,不许把不可信的数写进产物。
    """
    if rc not in (0, 1):
        raise RunnerContractError(
            "%s:pytest rc=%s(不是 0/1)—— 这次根本没跑完(collect 错 / 库没起 / "
            "用法错),不许当成'零红'。尾部输出:\n%s" % (what, rc, tail[-1500:]))
    if not junit.is_file():
        raise RunnerContractError(
            "%s:junit 没生成(%s)—— 没有证据文件就没有红集,不许折成空集。"
            "尾部输出:\n%s" % (what, junit, tail[-1500:]))


def red_nodeids(junit: pathlib.Path):
    """从 junit 取**完整 nodeid 全集** —— 不靠"我记得它红了"。"""
    import xml.etree.ElementTree as ET
    if not junit.is_file():
        return None
    root = ET.parse(str(junit)).getroot()
    out = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            cls = (case.get("classname") or "").replace(".", "/")
            out.append("%s::%s" % (cls, case.get("name")))
    return sorted(out)


def main():
    # 🔴 [机制令 ①] 树级排他锁 —— 并发下毒落在谁的基线上无法归属。
    with tree_lock("mutation_runner_woa_diag_funding"):
        return _main_locked()


def _main_locked():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.getenv("DEFGEO_P0FIX_TEST_DSN", DEFAULT_DSN))
    ap.add_argument("--out", default=str(REPO / "docs" / "AI-CONTEXT"
                                         / "MUTATION_WOA_DIAG_FUNDING_2026-08-25.json"))
    ap.add_argument("--only", default="", help="逗号分隔的变异 id;留空跑全部")
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    plan = [m for m in MUTATIONS if not only or m["id"] in only]

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="woa_mut_"))
    results = []

    # 基线:未变异时必须全绿 —— 基线是红的话,后面每一发的"红"都说明不了什么。
    base_junit = tmp / "baseline.xml"
    rc, tail = run_tests(args.dsn, base_junit)
    # 🔴 [E1-3①] 先问"这次跑数算不算数",再问"红不红"。
    #    rc 原来只打印不检查,junit 缺失原来被 ``or []`` 折成空红集。
    assert_run_is_trustworthy(rc, base_junit, tail, "基线")
    base_red = red_nodeids(base_junit)
    assert base_red is not None                      # 上面那道闸已保证 junit 在
    print("[baseline] rc=%s red=%d" % (rc, len(base_red)))
    if rc != 0 and not base_red:
        raise SystemExit(
            "基线 rc=%s 却一条红都没有 —— 自相矛盾,跑数不可信,拒绝往下跑" % rc)
    if base_red:
        print(tail)
        raise SystemExit("基线就是红的(%d 条)—— 先修基线,变异结论一律作废:%r"
                         % (len(base_red), base_red[:5]))

    for m in plan:
        path = REPO / m["file"]
        backup = tmp / (m["id"] + "_" + path.name + ".bak")
        shutil.copy2(str(path), str(backup))          # cp 备份 · 禁 git checkout
        before_sha = sha(path)
        started = time.time()
        try:
            apply_mutation(path, m["old"], m["new"])
            junit = tmp / (m["id"] + ".xml")
            rc, tail = run_tests(args.dsn, junit)
            # 🔴 [E1-3①] junit 缺失 / rc 不是 0|1 一律抛 ——
            #    原来 red_nodeids 返 None → killed=False → 这一发被记成**存活**。
            #    存活是要交出去当判据洞用的结论，不许由"没跑成"冒充。
            assert_run_is_trustworthy(rc, junit, tail, m["id"])
            red = red_nodeids(junit)
            assert red is not None
            killed = bool(red)
            if rc != 0 and not killed:
                raise RunnerContractError(
                    "%s：rc=%s 却零红 —— 自相矛盾，不许记成存活" % (m["id"], rc))
            results.append({
                **{k: m[k] for k in ("id", "origin", "file", "note")},
                "killed": killed, "rc": rc,
                "red_nodeids": red or [],
                "red_count": len(red or []),
                "seconds": round(time.time() - started, 1),
                "tail": "" if killed else tail[-1500:],
            })
            print("[%s/%s] %s killed=%s red=%d (%.0fs)"
                  % (m["id"], m["origin"], m["note"], killed, len(red or []),
                     time.time() - started))
        finally:
            shutil.copy2(str(backup), str(path))      # 还原
            after_sha = sha(path)
            if after_sha != before_sha:
                raise SystemExit(
                    "还原不干净:%s 的 sha256 %s != %s —— 后面每一发都会在污染的树上跑"
                    % (m["file"], after_sha[:16], before_sha[:16]))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "package": "WO_CODEX_P0_DIAG_FUNDING_2026-08-25 · 工单A",
        "targets": TEST_TARGETS,
        "baseline_red": base_red,
        "totals": {
            origin: {
                "total": sum(1 for r in results if r["origin"] == origin),
                "killed": sum(1 for r in results if r["origin"] == origin and r["killed"]),
                "survived": sum(1 for r in results if r["origin"] == origin and not r["killed"]),
            }
            for origin in sorted({r["origin"] for r in results})
        },
        "results": results,
    }
    io.open(out, "w", encoding="utf-8", newline="\n").write(
        json.dumps(payload, ensure_ascii=False, indent=2))
    print("\n落盘:%s" % out)
    print(json.dumps(payload["totals"], ensure_ascii=False))
    survived = [r["id"] for r in results if not r["killed"]]
    if survived:
        print("存活:%r" % (survived,))
    # 🔴 [E1-3①] 存活就非零退出。原来 main() 无论如何都返 None（rc=0），
    #    CI / 链式脚本只看退出码时，"有判据洞"与"全杀"长得一模一样。
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
