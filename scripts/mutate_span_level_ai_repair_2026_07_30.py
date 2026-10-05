"""[工单 span 级 AI 免费修复 2026-07-30 · §7] 变异验证:每个变异注入后必须有锁转红。

跑法:
    python scripts/mutate_span_level_ai_repair_2026_07_30.py

做法(不用 git checkout 还原 —— 那会冲掉未提交改动,本仓踩过):
每个变异都在内存里把目标文件正文替换一次、写盘、跑锁、**用备份原文写回**,
finally 里再校验一遍**字节**与备份一致(写回失败就立刻停,绝不留残留)。

🔴 **必须走 bytes,不许用 Path.write_text/read_text**:Windows 上 write_text 会把
`\n` 翻成 `\r\n`,一次"还原"就把 server.py 30460 行全改成 CRLF —— diff 变成
"全文重写",复审看不出真改动、部署侧也可能因换行 diff 出事。本仓已有同型教训
(PS `>` 生成 patch 变 CRLF 毁 apply)。这里读写都用 read_bytes/write_bytes,
只在内存里按 utf-8 解码做字符串替换。

统计口径:FAILED 与 ERROR **都算转红**(ERROR 不是 PASS,但也不是绿);
一个变异如果全绿 = SURVIVED = 该锁没有判别力,必须如实打印出来。

🔴 一处**如实记录的例外**(见 EXPECTED_SURVIVORS):变异 ①b 单独删掉
「除 span 外逐字节相同」校验会**全绿存活**,原因是 splice 本身按构造就只动
[start,end) —— 校验在正确构造下是冗余的第二道。它的判别力由另外两处证明:
  · 变异 ①a(保留校验、把 splice 改成整段替换)→ 转红;
  · test_lock1_isolation_check_has_discriminating_power(构造一个长度/结构全绿、
    只有 span 外字节被改的 candidate,只有该校验能拦)。
把这条写在这里而不是删掉变异,是因为"单点变异存活 ≠ 锁假绿",但也**不许**
悄悄不报(本仓踩过"单点 SURVIVED 被当成假绿"和"锁假绿被当成通过"两个方向)。
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

try:  # Windows 控制台默认 GBK,打印 ✅/🔴 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "span_level_repair.py"
GATE = ROOT / "services" / "article_review_gate.py"
SERVER = ROOT / "server.py"
TEST = "tests/test_span_level_ai_repair_2026_07_30.py"

# (编号, 说明, 目标文件, 原串, 变异串, 期望转红的锁)
MUTATIONS: list[tuple[str, str, Path, str, str, str]] = [
    (
        "①a",
        "把 span 贴回改成整段替换(保留全部校验)→ 证明 span 隔离校验真的在拦",
        ENGINE,
        'candidate = splice_span(original, located["start"], located["end"], new_span)',
        'candidate = splice_span(original, 0, len(original), new_span)',
        "锁 1(span 级隔离)",
    ),
    (
        "①b",
        "去掉「除 span 外逐字节相同」校验",
        ENGINE,
        '''    if not verify_span_isolation(original, repaired, start, end, new_span):
        return "repair_touched_outside_span"''',
        '''    if False:
        return "repair_touched_outside_span"''',
        "锁 1(span 级隔离)· 预期存活",
    ),
    (
        "②",
        "去掉结构 diff 校验",
        ENGINE,
        '''    if structure_fingerprint(original) != structure_fingerprint(repaired):
        return "repair_structure_changed"''',
        '''    if False:
        return "repair_structure_changed"''',
        "锁 2(结构不变)",
    ),
    (
        "②b",
        "去掉长度带校验",
        ENGINE,
        '''    if not check_length(old_span, new_span):
        return "repair_span_length_out_of_band"''',
        '''    if False:
        return "repair_span_length_out_of_band"''',
        "锁 3(长度约束)",
    ),
    (
        "③",
        "修完不重判直接放行",
        ENGINE,
        """    if still_violates(
        paragraph_text, code,
        evidence_pack or None,
        finding.get("brand_fact_snapshot"),
    ):
        return {**fail, "reason": "still_violating"}""",
        """    if False:
        return {**fail, "reason": "still_violating"}""",
        "锁 4(重判放行)",
    ),
    (
        "④",
        "CANNOT_FIX_WITHOUT_FABRICATION 仍然落改动",
        ENGINE,
        """    if CANNOT_FIX_MARKER in new_span:
        # 🔴 诚实出口:零改动转人工,不算失败(§4.4 / 锁 5)。
        return {**fail, "reason": "cannot_fix_without_fabrication"}""",
        """    if False:
        return {**fail, "reason": "cannot_fix_without_fabrication"}""",
        "锁 5(不许编造兜底)",
    ),
    (
        "⑤a",
        "高风险类也照修(引擎层放开)",
        ENGINE,
        """    if not route["ai_repairable"]:
        # §2.1:高风险类缺的是人工签发,不是措辞 —— 这里**永不调模型**。
        return {**fail, "reason": route["block_reason"]}""",
        """    if False:
        return {**fail, "reason": route["block_reason"]}""",
        "锁 6(高风险不给 AI 修)",
    ),
    (
        "⑤b",
        "高风险类也渲染 AI 修复按钮(发布门 actions 层放开)",
        GATE,
        """            ai_repair_blocked = article_ai_repair_blocked(
                str(row.get("industry") or ""), str(row.get("title") or ""),
            )""",
        """            ai_repair_blocked = False""",
        "锁 6(出口按钮)",
    ),
    (
        "⑤c",
        "端点不再按高风险拒绝(整层拿掉:整篇级 + span 级两个 if 是同一层,"
        "只删一个会被另一个掩住 → 必须整层变异)",
        SERVER,
        """    if article_ai_repair_blocked(_repair_industry, _repair_title):
        return {"success": False, **high_risk_no_ai_repair_alert("high_risk_article")}
    if span_ai_repair_blocked(payload.matched_text):
        return {"success": False, **high_risk_no_ai_repair_alert("high_risk_span")}""",
        """    if False:
        return {"success": False, **high_risk_no_ai_repair_alert("high_risk_article")}""",
        "锁 6(端点层)",
    ),
    (
        "⑥",
        "免费额度改成无限",
        ENGINE,
        "FREE_REPAIRS_PER_FINDING: Final = 2",
        "FREE_REPAIRS_PER_FINDING: Final = 10**9",
        "锁 7(免费额度)",
    ),
    (
        "⑦",
        "无来源时允许新数字(锁 8 的计数+证据成员两条一起放开)",
        ENGINE,
        '''    if not check_numbers(old_span, new_span, evidence_numbers, allow_more=allow_kept_anchor):
        return "repair_introduced_number"''',
        '''    if False:
        return "repair_introduced_number"''',
        "锁 8(不得出现新数字)",
    ),
]


#: 预期存活的变异(理由见模块 docstring)。存活**照样打印**,只是不算失败;
#: 反过来:如果它意外转红,同样要提示 —— 说明构造前提变了,注释得跟着改。
EXPECTED_SURVIVORS: dict[str, str] = {
    "①b": "splice 按构造只动 [start,end),该校验是冗余第二道;判别力由 ①a + "
           "test_lock1_isolation_check_has_discriminating_power 证明",
}


def run_locks() -> tuple[int, int, int, str, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:warnings", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (proc.stdout or "") + (proc.stderr or "")
    failed = tail.count("FAILED ")
    errors = tail.count("ERROR ")
    names = [
        line.split("::")[-1].split(" ")[0]
        for line in tail.splitlines()
        if line.startswith("FAILED ") or line.startswith("ERROR ")
    ]
    last = [line for line in tail.splitlines() if " passed" in line or " failed" in line or " error" in line]
    return failed, errors, proc.returncode, (last[-1] if last else "(无汇总行)"), names


def assert_targets_match_head() -> bool:
    """开跑前先确认目标文件与 git HEAD 字节一致。

    为什么要这道门:脚本被 kill(超时/Ctrl-C/任务被回收)时 `finally` 不一定跑到,
    磁盘上会**残留一个变异**。下一次跑就会把"带变异的代码"当成基线 —— 那时
    该变异必然"存活",看起来像锁失效,实际是环境脏。这种脏基线比跑不出结果危险。
    另注:脚本运行**期间**目标文件本来就是脏的(正在注入),别在这时去看
    `git status` 然后以为残留了 —— 要看就等它跑完。
    """
    dirty: list[str] = []
    for path in {ENGINE, GATE, SERVER}:
        rel = path.relative_to(ROOT).as_posix()
        head = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True,
        )
        if head.returncode != 0:
            print(f"  ⚠️ 取不到 HEAD:{rel},跳过完整性检查")
            continue
        if head.stdout.replace(b"\r\n", b"\n") != path.read_bytes().replace(b"\r\n", b"\n"):
            dirty.append(rel)
    if dirty:
        print("🔴 目标文件与 HEAD 不一致,可能是上一次被 kill 后残留的变异:")
        for rel in dirty:
            print(f"  - {rel}")
        print("  先 `git diff` 看清楚(有意的改动就用 --allow-dirty 跑,残留就 git checkout 掉)")
        return False
    return True


def main() -> int:
    if "--allow-dirty" not in sys.argv and not assert_targets_match_head():
        return 3
    print("=== 基线(未变异)===")
    failed, errors, rc, summary, names = run_locks()
    print(f"  {summary}")
    if rc != 0:
        print("🔴 基线本身不绿,先修基线再谈变异(ERROR 不是 PASS)")
        return 1

    survived: list[str] = []
    for tag, desc, path, old, new, lock in MUTATIONS:
        # 🔴 bytes 进 bytes 出:Path.write_text 在 Windows 会把整个文件翻成 CRLF
        raw = path.read_bytes()
        source = raw.decode("utf-8")
        if old not in source:
            print(f"\n=== 变异 {tag} · {desc}")
            print(f"  🔴 注入锚点没找到({path.name})→ 变异脚本已过期,必须修脚本再跑")
            survived.append(f"{tag}(锚点失效)")
            continue
        assert source.count(old) == 1, f"变异 {tag} 的锚点不唯一,会误伤"
        try:
            path.write_bytes(source.replace(old, new, 1).encode("utf-8"))
            # import 冒烟:变异后模块必须还能 import(否则"红"是语法错不是锁生效)
            smoke = subprocess.run(
                [sys.executable, "-c",
                 "import services.span_level_repair, services.article_review_gate"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if smoke.returncode != 0:
                print(f"\n=== 变异 {tag} · {desc}")
                print("  🔴 变异后 import 就崩了 → 这次转红不算锁生效")
                print("  " + (smoke.stderr or "").strip().splitlines()[-1][:160])
                survived.append(f"{tag}(import 崩)")
                continue
            failed, errors, rc, summary, names = run_locks()
            print(f"\n=== 变异 {tag} · {desc}")
            print(f"  期望转红:{lock}")
            print(f"  结果:{summary}  (FAILED={failed} ERROR={errors})")
            for name in names:
                print(f"    转红用例:{name}")
            if rc == 0:
                if tag in EXPECTED_SURVIVORS:
                    print(f"  ⚠️ SURVIVED(已记录的冗余层):{EXPECTED_SURVIVORS[tag]}")
                else:
                    print("  🔴 SURVIVED:全绿 = 该锁没有判别力")
                    survived.append(f"{tag} {desc}")
            else:
                print("  ✅ 转红")
                if tag in EXPECTED_SURVIVORS:
                    print("  ⚠️ 该变异原本预期存活,现在转红了 → 构造前提变了,"
                          "EXPECTED_SURVIVORS 里的说明必须重写")
        finally:
            path.write_bytes(raw)
            assert path.read_bytes() == raw, f"{path} 还原失败(字节级不一致)!"

    print("\n=== 汇总 ===")
    if survived:
        print("🔴 以下变异存活(锁需要加强 / 或如实记进 EXIT):")
        for item in survived:
            print("  -", item)
        return 2
    print("✅ 全部变异均被锁抓住")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
