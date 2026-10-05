#!/usr/bin/env python
"""外选变异终单(EXTSEL_FINAL 2026-08-26 · Review-CTO 亲裁签发)· **37 发**。

═══════════════════════════════════════════════════════════════════════
纪律(逐条来自终单;违反任何一条都会让这一轮的数字失效)
═══════════════════════════════════════════════════════════════════════
 · **机械枚举对账 = 37**:A 15 + C 15 + B7 7。合并静默丢发是已发生过的事故
   (MUT-AUD-12),所以发数不许手抄 —— 从 JSON + 本文件的 B7 段各自机械数,
   两边加起来必须正好 37,不到就 SystemExit。
 · **每发执行前再验一次锚点唯一**(`count == 1`),≠1 立刻停下报 Review,
   不静默跳过、不"顺手改个锚点继续跑"。
 · 备份**落盘** `.mutbak` + 还原走 `tmp + os.replace`(原子);还原后逐字节
   核 sha,不一致立刻停。禁 `git checkout`。
 · **串行**:本 runner 就地改源文件。起跑前扫全树 `.mutbak`,发现残留即拒跑
   (说明另一个 runner 正在跑或上一轮崩了)。
 · 「预测被杀」发按**精确红集合**判:溢出与欠红都记 FAIL 定性;
   「预测存活」发被杀的,把**杀它的判据名**记回单上。

分母口径(如实写明,别让读者以为跑的是全仓)
------------------------------------------------
每发只跑**它那一族**的判据包(A 族 / C 族 / B7 族)。理由是终单里每一发的
预测都是按本族判据写的,而跑满七包 × 37 发 ≈ 4 小时。
🔴 但**存活发**会自动补跑一次**全七包**:存活才是结论(= 判据洞),
   而"存活"最容易的假因就是分母太小(本仓记过:分母比结论小,结论就是假的)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutation_tree_lock import (  # noqa: E402  两道机制闸,见该模块顶部
    cache_is_fresh, criteria_fingerprint, tree_lock)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".tiprun"
AC_JSON = STATE / "extsel_ac.json"

MIN_FREE_BYTES = 512 * 1024 * 1024

# 🔴 [E1 · 2026-08-27] 七个 DSN **可由环境变量覆盖**,默认值逐字不变。
#
#    起因是实测踩到的:全分母基线报 1309 绿 / 1 红 —— C 包 test_c3_12 违反
#    ``chk_defgeo_attempt_tenant_owner_positive``。查证下来那条 CHECK
#    **在本仓任何 .sql / .py 里都不存在**,却真实存在于共享的 C 库与 pkgE 库中,
#    而我的改动面不含 monitoring / attempt ledger 那条链 ——
#    是**别的窗口**把它直接加到了共享测试库上,共享库**漂到了代码前面**。
#
#    ``.mutlock`` 守的是**树**,守不住**共享数据库**。DSN 写死 = 每个窗口被钉在
#    同一批库上,这是跨窗口冲突的结构性来源。给每个 DSN 一个环境变量出口,
#    窗口可以指向自己的一次性库;不传就与改动前逐字相同。
def _dsn(env: str, default: str) -> str:
    return os.environ.get(env, default)


DSN_A = _dsn("EXTSEL_DSN_A",
             "postgresql://geo_admin:p0fixpass@localhost:55438/geo_defgeo_regress_test")
DSN_A_P0FIX = _dsn("EXTSEL_DSN_A_P0FIX",
                   "postgresql://geo_admin:p0fixpass@localhost:55438/defgeo_p0fix_test")
DSN_C1 = _dsn("EXTSEL_DSN_C1",
              "postgresql://geo_admin:testpw@localhost:55484/geo_defgeo_woc_test")
DSN_C2 = _dsn("EXTSEL_DSN_C2",
              "postgresql://geo_admin:testpw@localhost:55485/xbexec_seed_test")
DSN_B = _dsn("EXTSEL_DSN_B",
             "postgresql://geo_admin:testpw@localhost:55850/geo_defgeo_wob_test")
DSN_E = _dsn("EXTSEL_DSN_E",
             "postgresql://geo_admin:testpw@localhost:55480/geo_defgeo_pkge_test")
DSN_W3 = _dsn("EXTSEL_DSN_W3",
              "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w3c_test")

#: 族 → [(pytest 路径, DSN, 额外 env)]
FAMILIES: dict[str, list[tuple[str, str, dict[str, str]]]] = {
    "A": [("tests/defgeo_funding_p0_2026_08_25", DSN_A,
           {"DEFGEO_P0FIX_TEST_DSN": DSN_A_P0FIX}),
          ("tests/defensive_geo_2026_08_21", DSN_A, {})],
    "C": [("tests/defgeo_woc_closure_2026_08_25", DSN_C1, {}),
          ("tests/xiaobang_execute_2026_08_20", DSN_C2, {})],
    "B7": [("tests/defgeo_wob_publish_funding_2026_08_25", DSN_B, {}),
           ("tests/defensive_geo_pkge_2026_08_24", DSN_E, {})],
}

#: 存活发的**补跑**分母:七包全集。
FULL_DENOMINATOR: list[tuple[str, str, dict[str, str]]] = [
    *FAMILIES["A"], *FAMILIES["C"], *FAMILIES["B7"],
    ("tests/defensive_geo_w3_2026_08_21", DSN_W3, {}),
]

# ══════════════════════════════════════════════════════════════════════════
# B-7 七发(Review 亲挑 · 终单正文)
# ══════════════════════════════════════════════════════════════════════════
RECON = "services/defensive_geo/publish/reconciler.py"
WORKER = "services/defensive_geo/publish/publish_worker.py"
TRANSPORT = "services/defensive_geo/publish/provider_transport.py"
MIG051 = "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql"

B7_MUTATIONS: list[dict] = [
    {
        "id": "MUT-EXTB7-01", "family": "B7", "file": RECON, "predict": "区分力发",
        "title": "⑦ 权威零接单谓词松弛(call_count <= 1)",
        "from": "AND c.provider_call_count = 0",
        "to": "AND c.provider_call_count <= 1",
    },
    {
        "id": "MUT-EXTB7-02", "family": "B7", "file": RECON, "predict": "被杀",
        "title": "⑦ 候选集放回隔离单",
        "from": "NOT IN ('completed','failed','cancelled','quarantined')",
        "to": "NOT IN ('completed','failed','cancelled')",
        # 🔴 [Review 裁定 2026-08-26] r2_64 是「凡会结算的收敛项候选集都必须排除
        #    quarantined」那条**机械枚举锁**,摘掉 ⑦ 的 quarantined 它该红 ——
        #    并入预期红集(第一轮就是因为它超出范围而按终单停机)。
        "expect_subset_of": ["test_r2_60", "test_r2_62", "test_r2_64"],
    },
    {
        "id": "MUT-EXTB7-03", "family": "B7", "file": RECON, "predict": "被杀",
        "title": "④ 隔离闸拆除",
        "from": "AND command_state <> 'quarantined'",
        "to": "AND TRUE",
        # 🔴 [Review 令 ①] 预期红集 = ④ 行为/活性对照 ∪ r2_64;
        #    **r2_64 不红就停下报 Review** —— 它不红意味着那条枚举锁只
        #    盯着 ⑦ 一处,「同一纪律的两处落点」实际只堵了一处。
        # 🔴 订正(2026-08-26):我上一版把 ④ 的行为臂写成了 `r2_61` ——
        #    那是 **⑦** 的活性对照。④ 这一对是 r2_62(行为)+ r2_63(活性对照)。
        #    因此第一次跑出的「超出预测红集」是我的**转录错**,不是发现:
        #    实测红集 {r2_62, r2_64} 正好等于 Review 令的「④行为 ∪ r2_64」。
        "expect_subset_of": ["test_r2_62", "test_r2_63", "test_r2_64"],
        "expect_must_include": ["test_r2_64"],
    },
    {
        "id": "MUT-EXTB7-04", "family": "B7", "file": WORKER, "predict": "被杀",
        "title": "_settle_row 上限永不触发",
        "from": "terminal = attempt_count >= MAX_ATTEMPTS",
        "to": "terminal = False",
    },
    {
        "id": "MUT-EXTB7-05", "family": "B7", "file": WORKER, "predict": "区分力发",
        "title": "_settle_row 耗尽单标成结清(假完成)",
        "from": ('_store.settle_outbox(cur, outbox_id=outbox_id, claim_token=claim_token,\n'
                 '                                     status="needs_review", '
                 'last_error=reason[:2000])'),
        "to": ('_store.settle_outbox(cur, outbox_id=outbox_id, claim_token=claim_token,\n'
               '                                     status="settled", '
               'last_error=reason[:2000])'),
    },
    {
        "id": "MUT-EXTB7-06", "family": "B7", "file": TRANSPORT, "predict": "区分力发",
        "title": "枚举锁分母层级检验:真实调用点摘掉 strict kwarg",
        # 🔴 终单只给了"该文件内传参处",没给字面锚点 —— 我 grep -n 定位到
        #    唯一一处(L229)并把整行连同缩进逐字取下,执行前还会再验一次唯一。
        "from": "                strict_order_ref_binding=True,\n",
        "to": "",
    },
    {
        "id": "MUT-EXTB7-07", "family": "B7", "file": MIG051, "predict": "被杀",
        "title": "051 index-guard 的 RAISE 改静默跳过",
        # 🔴 终单给的是**前缀**定位。整条 RAISE 跨 6 行(带 USING ERRCODE),
        #    整块取下换成 RETURN;(= 静默跳建)。
        "from": (
            "        RAISE EXCEPTION '[index-guard] uq_defgeo_pcmd_provider_order_ref "
            "已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',\n"
            "            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) "
            "FROM pg_class c\n"
            "               LEFT JOIN pg_index i ON i.indexrelid = c.oid\n"
            "               LEFT JOIN pg_class t ON t.oid = i.indrelid\n"
            "              WHERE c.relname = 'uq_defgeo_pcmd_provider_order_ref'\n"
            "                AND c.relnamespace = 'public'::regnamespace)\n"
            "            USING ERRCODE = 'duplicate_object';\n"
        ),
        "to": "        RETURN;\n",
    },
]

#: EXTC-02 是**文件 + 真库双打**(终单执行法):改迁移文件之外,还要把判据库上
#: 那条同名 CHECK 先 DROP 掉,让本轮 conftest 重放按弱化版重装。
#: 还原 = 逆序:先还原文件,再跑一遍**还原后的** 052(它的 ADD 包在
#: `IF NOT EXISTS` 里,所以会把原始三列版装回来),最后核约束数 = 1。
DB_DOUBLE_HIT = {
    "MUT-EXTC-02": {
        "dsn": DSN_C1,
        "drop": ("ALTER TABLE public.defgeo_activation_outbox "
                 "DROP CONSTRAINT IF EXISTS defgeo_activation_outbox_payer_group"),
        "restore_migration": "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
        # 🔴 核**定义**不核数量:数量在弱化版下同样是 1(见下方还原块的订正说明)。
        "verify": ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE conname = 'defgeo_activation_outbox_payer_group'"),
        "verify_contains": ("payer_user_id IS NOT NULL",
                            "payer_funding_policy IS NOT NULL",
                            "payer_principal_kind IS NOT NULL"),
    },
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _assert_disk_headroom() -> None:
    free = shutil.disk_usage(ROOT).free
    if free < MIN_FREE_BYTES:
        raise SystemExit(
            f"🔴 可用空间 {free / 1024 / 1024:.0f} MB < "
            f"{MIN_FREE_BYTES / 1024 / 1024:.0f} MB —— 拒绝开跑。"
            "(2026-08-25 实测:盘满时炸在**还原**那一步,源文件被留成 0 字节。)")


def _assert_no_concurrent_runner() -> None:
    stale = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mutbak")]
    stale += [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mutrestore")]
    if stale:
        raise SystemExit(
            f"🔴 树上有残留备份 {stale} —— 要么另一个 runner 正在跑(本仓明文禁并发),"
            "要么上一轮崩在还原上。先人工核对再来。")


#: 绿数相对基线塌方多少算「钝杀」(模块崩了 / 整包起不来),而不是判据抓住了。
BLUNT_KILL_GREEN_DROP = 0.10


def _fit_newlines(text: str, needle: str) -> tuple[str, dict[str, int]]:
    """把锚点翻译成**目标文件实际用的**换行,返回 (选中的形态, 各形态命中数)。

    🔴 本仓记过「Windows 读改写脚本 LF→CRLF」,这次是它的**读侧**镜像:
       `read_text()` 会把 `\r\n` 归一成 `\n`,`read_bytes().decode()` 不会。
       抽取器用前者验锚点(绿),runner 用后者匹配(红)——
       **同一个文件的两种视图**,预检和实跑各说各话。
       所以这里不猜文件是哪种,两种形态都数,由命中数说话。
    """
    lf = needle.replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")
    counts = {"lf": text.count(lf), "crlf": text.count(crlf)} if lf != crlf else \
             {"lf": text.count(lf), "crlf": text.count(lf)}
    if lf == crlf:                      # 单行锚点:两种形态本来就一样
        return lf, {"single_line": counts["lf"]}
    if counts["crlf"] == 1 and counts["lf"] != 1:
        return crlf, counts
    return lf, counts


def _fit_newlines_bytes(raw: bytes, needle: str) -> tuple[bytes, dict[str, int]]:
    """**字节路径**版换行适配 —— 锚点匹配全程不 decode。

    🔴 [Review 令 ④③] `read_text()`(universal newlines)与
       `read_bytes().decode()` 是同一文件的**两种视图**:前者把 `\r\n` 归一成
       `\n`,后者不会。我的预检用前者、runner 用后者 ⇒ **预检绿、实跑红**
       (2026-08-26 实测两次,全仓唯一 CRLF 文件 `api/defensive_geo_api.py`)。
       字节路径只有一种视图,这一类假象结构上不可能再发生。
    """
    lf = needle.replace("\r\n", "\n").encode("utf-8")
    crlf = lf.replace(b"\n", b"\r\n")
    if lf == crlf:
        return lf, {"single_line": raw.count(lf)}
    counts = {"lf": raw.count(lf), "crlf": raw.count(crlf)}
    if counts["crlf"] == 1 and counts["lf"] != 1:
        return crlf, counts
    return lf, counts


def _fit_repl_bytes(raw: bytes, needle: bytes, repl: str) -> bytes:
    """替换串的行尾 —— **不许**用 `_fit_newlines_bytes` 拼。

    🔴 [2026-08-27 与窗口A 对账挖出来的] `_fit_newlines_bytes` 靠
       `raw.count(s)` 决定 LF/CRLF。对**锚**成立(锚必在文件里);
       对**替换**不成立 —— 替换按定义不在文件里,两个计数恒 0,
       `counts["crlf"] == 1` 永假 ⇒ **永远落回 LF**。
       后果:多行替换被写成 LF 注进 CRLF 文件,盘上留下混合行尾。
       裁定没被翻(两发都是存活),但**在盘证据与别人的对不上**,
       而混合行尾在本仓是有前科的(同一份文件两种读取视图 ⇒ 预检绿实跑红)。

    规则:①锚自己带行尾就跟锚(局部最可靠);②锚是单行就看文件整体;
         ③文件本身混合行尾 + 锚是单行 ⇒ **停机**,不猜。
    """
    lf = repl.replace("\r\n", "\n").encode("utf-8")
    if b"\n" not in lf:
        return lf                                  # 单行替换没有行尾可谈
    if b"\r\n" in needle:
        return lf.replace(b"\n", b"\r\n")
    if b"\n" in needle:
        return lf                                  # 锚是纯 LF
    crlf_n = raw.count(b"\r\n")
    lone = raw.count(b"\n") - crlf_n
    if crlf_n and not lone:
        return lf.replace(b"\n", b"\r\n")
    if crlf_n and lone:
        raise SystemExit(
            "🔴 文件混合行尾 + 锚是单行 + 替换是多行 —— 行尾无从推断。"
            "猜错就在盘上留混合行尾,停机比猜好。")
    return lf


def _on_disk_proof(path: Path, *, anchor_hits: int, before: bytes,
                   needle: bytes, repl: bytes, expected: bytes | None = None) -> dict:
    """[Review 令 ④①] 「毒真的落盘了」的三字段实证。

    绿数守恒只护「测试都跑了」。毒**没**落盘时:源码原封 → 全绿 → 绿数
    恰好等于基线 → 被记「存活」。它与「毒落了但没人红」观测完全相同,
    所以存活入账必须另带这三字段,缺一不可。
    """
    disk = path.read_bytes()
    proof = {
        "anchor_hits": anchor_hits,
        "sha_before": sha(before)[:16],
        "sha_after": sha(disk)[:16],
        "readback_needle_gone": needle not in disk,
        "readback_repl_present": (repl in disk) if repl else True,
    }
    if expected is not None:
        # 🔴 [2026-08-27] 逐字节等价 —— 比「needle 不见了」更硬,且对**插入型**
        #    变异同样成立。MUT-EXTE2-10 的替换**包含**锚
        #    (`…现取…` → `**并非**…现取…`),落盘完全成功却被
        #    `readback_needle_gone` 判成"没落盘",runner 按纪律停机。
        #    原规则默认了「替换不含锚」:适用域比使用域小。
        proof["sha_expected"] = sha(expected)[:16]
        proof["exact_match"] = (path.read_bytes() == expected)
        proof["ok"] = bool(
            proof["anchor_hits"] == 1
            and proof["sha_before"] != proof["sha_after"]
            and proof["exact_match"])
        return proof
    proof["ok"] = bool(
        proof["anchor_hits"] == 1
        and proof["sha_before"] != proof["sha_after"]
        and proof["readback_needle_gone"]
        and proof["readback_repl_present"])
    return proof


def _syntax_ok(path: Path) -> tuple[bool, str]:
    """变异后的文件必须仍然**语法合法**。

    本仓纪律:「变异语法合法、语义精确;整段替成废代码只是 blunt kill,不算」。
    .sql 不做解析(留给库自己报),.py 走 ast.parse。
    """
    if path.suffix != ".py":
        return True, ""
    import ast
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    return True, ""


def _restore(path: Path, original: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".mutrestore")
    tmp.write_bytes(original)
    os.replace(tmp, path)
    if sha(path.read_bytes()) != sha(original):
        raise SystemExit(f"🔴 {path} 还原后字节不一致 —— 立刻停")


#: 🔴 只认 pytest 短摘要里的**节点**形态(必须含 `.py::`)。
#:    不加这个约束,`ERROR    GEO-Server:server.py:593 ...` 这种**日志行**
#:    会被当成一条红 —— 红集一脏,「精确红集合逐条比对」就整个失效。
#: 🔴 也要认「整包起不来」那种不带 `::` 的形态(`ERROR tests/…/conftest.py`)——
#:    收紧成必须含 `.py::` 之后,最严重的那类红反而被挡在了外面。
#:    `tests/` 前缀把 `ERROR    GEO-Server:server.py:593` 这类**日志行**排除在外。
_NODE = re.compile(r"^(FAILED|ERROR)\s+(tests/\S+|\S*\.py::\S+)")


def _one_target(target: str, dsn: str, extra: dict[str, str]) -> tuple[set[str], int, int]:
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = dsn
    env["DATABASE_URL"] = dsn
    env.update(extra)
    proc = subprocess.run(
        # 🔴 `-rf` **只报 failed 不报 error** —— 2026-08-26 实测:EXTA-15 造成
        #    30 条 fixture setup ERROR,短摘要里一条都没有,于是它以
        #    「存活 红0/绿426」的形态混进结论。必须 `-ra`。
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:warnings",
         "--no-header", "-ra"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    red: set[str] = set()
    for line in out.splitlines():
        m = _NODE.match(line.strip())
        if m:
            nodeid = m.group(2)
            red.add(nodeid.split("/")[-1])
    green = 0
    gm = re.search(r"(\d+) passed", out)
    if gm:
        green = int(gm.group(1))
    # 收集错误(collection error 也算红:整包起不来不是"没有缺陷")
    if proc.returncode not in (0, 1):
        red.add(f"__RUNNER_ERROR__::{target}::rc={proc.returncode}")
    return red, green, proc.returncode


def run_family(targets) -> tuple[set[str], int]:
    red: set[str] = set()
    green = 0
    for target, dsn, extra in targets:
        r, g, _ = _one_target(target, dsn, extra)
        red |= r
        green += g
    return red, green


def _psql(dsn: str, sql: str) -> str:
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(sql)
        try:
            return str(cur.fetchall())
        except Exception:
            return ""
    finally:
        conn.close()


#: 🔴 **显式改写**(不静默改锚点):草单围栏抽窄、真实范围写在围栏后括注里的那几发。
#:    每一条都要写清「草单原话是什么 / 为什么按这个范围」。
AMENDMENTS: dict[str, dict] = {
    "MUT-EXTA-11": {
        # 草单标签:「替换成(**仅第二行**)」。锚点两行连体只为定位,
        # 注释行必须原样留下 —— 照字面替换会连注释一起删,那是第二处改动。
        "from": ('        # fundingState 说的是「你要不要付钱」—— 两件事,不许互相翻译。\n'
                 '        confirm_funding_state="exempt_recorded",'),
        "to": ('        # fundingState 说的是「你要不要付钱」—— 两件事,不许互相翻译。\n'
               '        confirm_funding_state="frozen",'),
        "why": "草单标签写明仅替第二行;注释行原样保留",
    },
    "MUT-EXTA-13": {
        # 草单标签:「替换成(**仅第一行**)」。四行锚点是为了跟 L1041 那处消歧
        # (第 1 行在文件里出现 2 次)。照字面把四行替成一行 ⇒ dict 字面量断头,
        # `unexpected indent`(实测)。所以后三行原样接回。
        "from": ('        split_snapshot = result.get("physical_split_snapshot")     # [A-4]'
                 '\n        handle = {\n            "kind": spec.handle_kind,\n            # ref 指向**真实存在的那一行**,不再是编出来的串。'),
        "to": ('        split_snapshot = None     # [A-4]'
               '\n        handle = {\n            "kind": spec.handle_kind,\n            # ref 指向**真实存在的那一行**,不再是编出来的串。'),
        "why": "草单标签写明仅替第一行;后三行是消歧用的上下文,原样接回",
    },
    "MUT-EXTA-15": {
        # 草单的替换值是**散文**:「整行删除」。照字面会把这四个汉字写进 manifest。
        # 真实语义 = 把这一行(连同缩进与换行)整条摘掉。
        "from": '    "db/migration_050_diagnosis_payer_identity_2026_08_25.sql",\n',
        "to": "",
        "why": "草单替换值是散文「整行删除」,按其语义取整行(含缩进与换行)",
    },
    "MUT-EXTA-01": {
        # 草单围栏只放了 except 臂的后半行;括注写明「即 except 臂**整句**
        # `raise PricingCatalogUnreadable(...) from None`」。按围栏字面替换会产出
        # `raise PricingCatalogUnreadable(return ...` —— 语法都不合法(第一轮实测:
        # 绿 456 → 71,模块 import 崩了)。这里按括注给的**整句**范围取。
        "from": ('        raise PricingCatalogUnreadable(\n'
                 '            "价目读取失败 feature=%s: %s" % (feature_code, exc)) from None'),
        "to": '        return _PRICING_CATALOG_SCHEME + ":db-unreadable"',
        "why": "草单围栏抽窄:真实范围由围栏后括注给出(except 臂整句)",
    },
}


def _looks_like_prose(value: str) -> bool:
    """替换值里有中文、却一个代码符号都没有 ⇒ 多半是把**指令**当成代码抄了。

    (EXTA-15 的替换值原文就是「整行删除」四个字。语法闸能抓到它,
     但抓不到 EXTA-11 那种"删掉一行注释、语法照样合法"的形态 ——
     所以形态闸与语法闸两道都要,谁也替不了谁。)
    """
    import re as _re
    if not _re.search(r"[\u4e00-\u9fff]", value):
        return False
    return not _re.search(r"[=(){}\[\]\"';:]", value)


def load_all() -> list[dict]:
    """🔴 **机械枚举**:A/C 从 JSON 数,B7 从本文件的列表数。合计必须 = 37。"""
    if not AC_JSON.exists():
        raise SystemExit(f"🔴 缺 {AC_JSON} —— 先跑抽取脚本把两份草单机械抽成 JSON")
    ac = json.loads(AC_JSON.read_text(encoding="utf-8"))
    by_fam: dict[str, int] = {}
    for r in ac:
        by_fam[r["family"]] = by_fam.get(r["family"], 0) + 1
    total = len(ac) + len(B7_MUTATIONS)
    print(f"机械枚举:A={by_fam.get('A', 0)} C={by_fam.get('C', 0)} "
          f"B7={len(B7_MUTATIONS)} 合计={total}")
    if by_fam.get("A") != 15 or by_fam.get("C") != 15 or len(B7_MUTATIONS) != 7:
        raise SystemExit(f"🔴 分族计数不对:{by_fam} + B7 {len(B7_MUTATIONS)} —— "
                         "终单写死 A15/C15/B7-7")
    if total != 37:
        raise SystemExit(f"🔴 合并后实际发数 = {total},终单是 37 —— "
                         "MUT-AUD-12 那次就是这么静默丢的,停下核对")
    for r in ac:
        r.setdefault("family", "A")
    applied = []
    for r in [*ac, *B7_MUTATIONS]:
        am = AMENDMENTS.get(r["id"])
        if am:
            r["from"], r["to"] = am["from"], am["to"]
            r["amended"] = am["why"]
            applied.append(r["id"])
    prose = [r["id"] for r in [*ac, *B7_MUTATIONS]
             if _looks_like_prose(r["to"]) and r["id"] not in AMENDMENTS]
    if prose:
        raise SystemExit(
            f"🔴 这些发的**替换值像散文不像代码**:{prose} —— "
            "多半是把草单里的指令(如「整行删除」)当成代码抄了。"
            "停下人工核对,补进 AMENDMENTS 并写明理由。")
    if applied:
        print(f"⚠️  按 AMENDMENTS 显式改写范围:{applied}(理由记在 runner 里)")
    return [*ac, *B7_MUTATIONS]


def main() -> int:
    # 🔴 [Review 机制令 2026-08-26 ①] 树级排他锁。口头串行守不住(实测第二次:
    #    2026-08-26 三伤,窗口 A/C/B 同时在这棵树里,我的基线读到别人写了一半的
    #    判据文件报 5 红,单跑一条都复现不了)。撕锁 runner 就地改源文件,
    #    并发下毒落在谁的基线上无法归属 —— 所以拒跑,不是警告。
    with tree_lock("mutation_runner_extsel_2026_08_26"):
        return _main_locked()


def _main_locked() -> int:
    _assert_disk_headroom()
    _assert_no_concurrent_runner()
    muts = load_all()

    only = {x.strip() for x in os.environ.get("EXT_ONLY", "").split(",") if x.strip()}
    fam_filter = {x.strip() for x in os.environ.get("EXT_FAMILY", "").split(",") if x.strip()}
    order = {"A": 0, "C": 1, "B7": 2}          # 终单规定的执行顺序
    muts.sort(key=lambda m: (order[m["family"]], m["id"]))

    STATE.mkdir(parents=True, exist_ok=True)
    results_path = STATE / "extsel_results.json"
    results: list[dict] = []
    if results_path.exists():
        results = json.loads(results_path.read_text(encoding="utf-8"))

    # 🔴 [Review 机制令 2026-08-26 ②] 结果缓存键并入**判据文件集指纹**。
    #    A 的缓存重放事故:补完判据重跑,runner 见 id 已在结果文件里就跳过,
    #    于是**新判据从没面对过那些变异**,而结果文件长得跟跑过一模一样。
    #    指纹的文件集从 pytest 目标机械枚举 —— 手抄清单漏掉的那一项
    #    不会让任何判据变红。
    fps: dict[str, str] = {}
    for fam in ("A", "C", "B7"):
        fp, meta = criteria_fingerprint([t for t, _d, _e in FAMILIES[fam]])
        fps[fam] = fp
        print(f"判据指纹[{fam}] {fp} · {meta['n_files']} 个判据文件")

    prior = {r["id"]: r for r in results}
    stale = [mid for mid, r in prior.items()
             if not cache_is_fresh(r, fps.get(r.get("family", ""), "<无>"))]
    if stale:
        print(f"♻️  判据已变,{len(stale)} 条缓存结果作废、必须重跑:{sorted(stale)}")
    done = {mid for mid in prior if mid not in stale}

    baselines: dict[str, tuple[set[str], int]] = {}
    for fam in ("A", "C", "B7"):
        if fam_filter and fam not in fam_filter:
            continue
        red, green = run_family(FAMILIES[fam])
        baselines[fam] = (red, green)
        print(f"基线[{fam}] 绿 {green} / 红 {len(red)} {sorted(red) if red else ''}")
        if red:
            raise SystemExit(f"🔴 基线[{fam}]不是全绿 —— 后面全部作废")

    for mut in muts:
        mid = mut["id"]
        if only and mid not in only:
            continue
        if fam_filter and mut["family"] not in fam_filter:
            continue
        if mid in done:
            print(f"⏭  {mid} 已有结果,跳过(删 {results_path.name} 可重跑)")
            continue

        path = ROOT / mut["file"]
        if not path.exists():
            raise SystemExit(f"🔴 {mid} 的文件不存在:{mut['file']} —— 停下报 Review")
        original = path.read_bytes()
        text = original.decode("utf-8")
        # 🔴 执行前**再验一次**锚点唯一(终单硬要求)。
        #    [Review 令 ④③] 全程走**字节路径**:decode 出来的 str 与磁盘字节
        #    是两种视图,预检与实跑各说各话的前科就出在这儿。
        needle, nl_counts = _fit_newlines_bytes(original, mut["from"])
        repl = _fit_repl_bytes(original, needle, mut["to"])
        n = original.count(needle)
        if n != 1:
            # 🔴 停下之前先把**现场**取下来:光说"命中 0 次"没法判是草单错、
            #    抽取错,还是文件当时被别的东西改过(第一轮 EXTA-13 就是这样,
            #    事后单独验又是 1 —— 没有现场就只能猜)。
            leftovers = [str(p.relative_to(ROOT))
                         for p in ROOT.rglob("*.mutbak")] + \
                        [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.mutrestore")]
            per_line = [(ln[:70], original.count(ln.encode("utf-8")))
                        for ln in mut["from"].replace("\r\n", "\n").split("\n")
                        if ln.strip()]
            crlf_n = original.count(b"\r\n")
            raise SystemExit(
                f"🔴 {mid} 锚点命中 {n} 次(要求 1)——**停下报 Review**,"
                f"不自行改锚点继续跑。file={mut['file']}\n"
                f"    file_sha={sha(original)[:16]}  残留备份={leftovers or '无'}\n"
                f"    换行:文件 CRLF={crlf_n} · 各形态命中={nl_counts}\n"
                f"    逐行命中={per_line}\n"
                f"    anchor={mut['from'][:200]!r}")

        hit = DB_DOUBLE_HIT.get(mid)
        backup = path.with_suffix(path.suffix + ".mutbak")
        backup.write_bytes(original)
        mutated = original.replace(needle, repl, 1)
        if mutated == original:
            # 🔴 替换后文件一个字节都没变 = 这一发**根本不是变异**。
            #    2026-08-26 实测踩过:我给 051 插的 `ELSIF FALSE THEN NULL;`
            #    永远不匹配,控制流照走真分支 —— 判据当然不红,
            #    而它会以「存活」的形态混进判据洞清单。
            raise SystemExit(
                f"🔴 {mid} 替换后文件无变化 —— 这一发是 no-op,不是变异。停下报 Review。")
        path.write_bytes(mutated)
        proof = _on_disk_proof(path, anchor_hits=n, before=original,
                               needle=needle, repl=repl)
        if not proof["ok"]:
            _restore(path, original)
            backup.unlink(missing_ok=True)
            raise SystemExit(
                f"🔴 {mid} **变异没有真的落盘** —— 停下报 Review。\n"
                f"    在盘实证 = {proof}\n"
                "    (毒没落盘会全绿、绿数恰好等于基线,与「存活」观测完全相同。)")
        syntax_ok, syntax_err = _syntax_ok(path)
        try:
            if not syntax_ok:
                red, green = set(), -1
            else:
                if hit:
                    _psql(hit["dsn"], hit["drop"])
                red, green = run_family(FAMILIES[mut["family"]])
        finally:
            _restore(path, original)
            backup.unlink(missing_ok=True)
            if hit:
                # 🔴🔴 [工单C 2026-08-26 订正] 原实现「还原文件 → 重跑迁移 → 核约束数=1」
                #    **结构上还原不了**:052 的 ADD CONSTRAINT 包在
                #    `IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=...)` 里,
                #    按**名字**幂等。跑完变异之后库上那条(弱化版)还在 ⇒ 重跑迁移直接跳过
                #    ⇒ 判据库被**永久**留在弱化 schema 上,而 `verify` 数出来仍然是 1、
                #    打印「还原后约束数 = 1」,一切看起来正常。
                #    实测:2026-08-26 我接手时 `geo_defgeo_woc_test` 上那条 CHECK 的
                #    第二支只剩两列 —— 也就是说 EXTC-02 之后跑的每一轮(含 76/76 基线)
                #    都是在**弱化 schema** 上测的。
                #    订正两点:① 先 DROP 再重装(幂等还原);② 核**定义文本**不核数量。
                _psql(hit["dsn"], hit["drop"])
                mig = (ROOT / hit["restore_migration"]).read_text(encoding="utf-8")
                _psql(hit["dsn"], mig)
                got = _psql(hit["dsn"], hit["verify"])
                print(f"    [{mid}] 双打还原后约束定义 = {got}")
                for _needed in hit.get("verify_contains", ()):
                    if _needed not in str(got):
                        raise SystemExit(
                            f"🔴 {mid} 双打**没还原干净**:约束定义里缺 {_needed!r}\n"
                            f"    实得 {got}\n"
                            "    这一轮之后的所有结果都作废(schema 已不是基线那副)。")

        if not syntax_ok:
            print(f"🔴 {mid} 变异后**语法不合法** —— 这是出题/抽取错,不是判据洞。"
                  f"停下报 Review。\n    {syntax_err}")
            results.append({"id": mid, "family": mut["family"], "verdict": "INVALID",
                            "reason": syntax_err, "title": mut.get("title", "")})
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            return 3

        base_red, base_green = baselines[mut["family"]]
        # 🔴 **绿数守恒**(主防线,形态无关):真存活的发,绿数必须**等于**基线。
        #    少一条就说明有测试**没跑**,而"没跑"在红集正则下长得跟"跑了没红"
        #    一模一样。红集正则永远会有下一个漏网形态(实测两次了),这条不会。
        if not (red - base_red) and green != base_green:
            print(f"🔴 {mid} 红 0 但绿数 {base_green} → {green} —— 有测试**没跑**,"
                  "不许按存活记。停下报 Review。")
            results.append({"id": mid, "family": mut["family"],
                            "verdict": "GREEN_NOT_CONSERVED",
                            "green": green, "base_green": base_green,
                            "title": mut.get("title", "")})
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            return 5
        if green < base_green * (1 - BLUNT_KILL_GREEN_DROP):
            print(f"🔴 {mid} 绿数塌方 {base_green} → {green} —— 这是**钝杀**"
                  "(模块崩了/整包起不来),不算判据抓住。停下报 Review。")
            results.append({"id": mid, "family": mut["family"], "verdict": "BLUNT_KILL",
                            "green": green, "base_green": base_green,
                            "red": sorted(red - base_red)[:20],
                            "title": mut.get("title", "")})
            results_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            return 4
        new_red = sorted(red - base_red)
        killed = bool(new_red)
        rec = {
            "id": mid, "family": mut["family"], "title": mut.get("title", ""),
            "predict": mut["predict"], "killed": killed,
            "red": new_red, "green": green,
            # 🔴 [Review 令 ④①] 在盘实证随每一条结果入账 ——
            #    没有这三字段的「存活」一律记**未验**。
            "on_disk_proof": proof,
            # 🔴 [Review 机制令 ②] 这条结果是**面对哪一版判据**跑出来的。
            #    判据一变指纹就变,这条自动作废,下轮必须重跑。
            "criteria_fp": fps[mut["family"]],
        }

        # 「预测被杀」且终单给了红集范围的,逐条比对
        must = mut.get("expect_must_include")
        if must:
            absent = [x for x in must if not any(x in r for r in new_red)]
            rec["missing_required_red"] = absent
        subset = mut.get("expect_subset_of")
        if subset:
            outside = [r for r in new_red
                       if not any(s in r for s in subset)]
            rec["outside_predicted_subset"] = outside

        # 缓存失效重跑的,把旧条目**替换**掉,不许一个 id 两条结果并存。
        results = [r for r in results if r.get("id") != mid]
        results.append(rec)
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        flag = "💀 杀" if killed else "🟢 存活"
        print(f"{flag} {mid} [{mut['family']}] 预测={mut['predict']} · "
              f"红 {len(new_red)} / 绿 {green}")
        if new_red:
            print("    红集:" + ", ".join(new_red[:8]) +
                  (" …" if len(new_red) > 8 else ""))
        if rec.get("missing_required_red"):
            print(f"    🔴 终单点名必须红的判据没红:{rec['missing_required_red']} "
                  "—— 按 Review 令「不红停下报 Review」")
            return 6
        if subset and rec.get("outside_predicted_subset"):
            print(f"    🔴 超出终单预测红集范围:{rec['outside_predicted_subset']} "
                  "—— 按终单「不符当场停」")
            return 2

    print("═" * 72)
    survivors = [r for r in results if not r["killed"]]
    print(f"完成 {len(results)} 发 · 杀 {len(results) - len(survivors)} · "
          f"存活 {len(survivors)}")
    if survivors:
        print("存活(= 判据洞候选,需补跑全分母确认):")
        for r in survivors:
            print(f"  · {r['id']} [{r['family']}] {r['title'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
