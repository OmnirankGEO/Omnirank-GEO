"""变异注入验证 · P3b SSE 可恢复批任务(工单 §6 后半 / §8)。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_p3b_repair_job_2026_08_01.py
    python scripts/mutation_p3b_repair_job_2026_08_01.py --selftest

分流规则、冒烟门口径、字节级还原全部与
`scripts/mutation_p3a_batch_review_2026_08_01.py` 一致(同一套机器,只换变异清单)。
非行为区口径:注释 + Python 裸字符串表达式语句(docstring)。
**不**把普通字符串常量算作非行为 —— 本包有变异专门改事件名与字段名,那是真行为。
`ast.col_offset` 全程按 UTF-8 字节算。
"""
from __future__ import annotations

import ast
import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_p3b_repair_job_2026_08_01.py"

JOB = "services/article_repair_job.py"
TSX = "frontend/src/pages/Writing/WritingHall.tsx"
SRV = "server.py"


@dataclasses.dataclass(frozen=True)
class Mutation:
    mid: str
    task: str
    layer: str
    path: str
    old: str
    new: str
    expect_red: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        mid="N1",
        task="🔴 摘掉幂等去重(同一条 finding 会被修两次、provider 被调两次)",
        layer="run_repair_job 的 attempted_keys",
        path=JOB,
        old="                    if key in job.attempted_keys:",
        new="                    if False:",
        expect_red="test_lock3_same_finding_is_never_repaired_twice_in_one_job",
    ),
    Mutation(
        mid="N2",
        task='🔴 恢复「文章不存在」分支的双计数(done 冲过 total)',
        layer="run_repair_job 的 article is None 分支",
        path=JOB,
        old="                    article_ok = False\n",
        new="                    article_ok = False\n                    job.failed_count += 1\n                    job.done += 1\n",
        expect_red="test_lock6b_missing_article_is_counted_exactly_once",
    ),
    Mutation(
        mid="N3",
        task="🔴 摘掉重连归属校验(拿到别人 job_id 就能看别人的修复流)",
        layer="attach_or_create 的 owner 比对",
        path=JOB,
        old="            if existing.owner_user_id != owner_user_id:",
        new="            if False:",
        expect_red="test_lock5_reconnecting_with_someone_elses_job_id_is_refused",
    ),
    Mutation(
        mid="N4",
        task="事件类型白名单失效(可以发前端不认识的 kind)",
        layer="RepairJob.emit 的 kind 校验",
        path=JOB,
        old="        if kind not in JOB_EVENT_KINDS:",
        new="        if False:",
        expect_red="test_lock7_unregistered_event_kind_is_rejected",
    ),
    Mutation(
        mid="N5",
        task="🔴 往事件里塞伪造百分比",
        layer="RepairJob.emit 的返回体",
        path=JOB,
        old='            "message": message,',
        new='            "message": message,\n            "percent": int(100 * self.done / max(1, self.total)),',
        expect_red="test_lock2b_no_forged_progress_fields_anywhere",
    ),
    Mutation(
        mid="N6",
        task="修不完也统一报 completed(不出 partial,骗用户已修好)",
        layer="run_repair_job 的 partial / completed 分支",
        path=JOB,
        old='            if failed_here or (verdict or {}).get("remaining_hard"):',
        new="            if False:",
        expect_red="test_lock1d_partial_when_some_findings_fail",
    ),
    Mutation(
        mid="N7",
        task="locating 不再过滤高风险(用户会看到一排必然失败的 repairing)",
        layer="locate_repairable_findings 的 ai_repairable 过滤",
        path=JOB,
        old='            if not span.get("ai_repairable"):',
        new="            if False:",
        expect_red="test_lock8_locating_skips_spans_that_ai_must_not_repair",
    ),
    Mutation(
        mid="N8",
        task="🔴 自造一套 finding_id(与库里的 span_repair_quota 键对不上,库级幂等失效)",
        layer="locate_repairable_findings 的幂等键",
        path=JOB,
        old='                "finding_id": finding_fingerprint(str(card.get("code") or ""), matched),',
        new='                "finding_id": f"{card.get(\'code\')}#{len(matched)}",',
        expect_red="test_lock8b_finding_id_is_the_existing_stable_fingerprint",
    ),
    Mutation(
        mid="N9",
        task="🔴 SSE 端点自己复制一份修复实现(不再复用单篇处理函数)",
        layer="api_batch_repair_stream 的依赖注入",
        path=SRV,
        old="            return await api_repair_article_finding(",
        new="            return await _inlined_repair_copy(",
        expect_red="test_lock10_sse_endpoint_reuses_the_existing_single_finding_handler",
    ),
    Mutation(
        mid="N10",
        task="前端自己算百分比(服务端没给的数字前端编)",
        layer="BatchReviewPanel 的进度块",
        path=TSX,
        old="                        修复进度:{repairProgress.done}/{repairProgress.total} 篇",
        new="                        修复进度:{Math.round(repairProgress.done / repairProgress.total * 100)}%",
        expect_red="test_lock11_frontend_draws_only_the_server_supplied_counts",
    ),
    Mutation(
        mid="N11",
        task="重连只带 job_id 不带游标(整段事件重放一遍)",
        layer="runRepairAllStream 的请求体",
        path=TSX,
        old="                    last_event_id: resumeJobId ? repairSeqRef.current : 0,",
        new="",
        expect_red="test_lock13_reconnect_sends_both_job_id_and_cursor",
    ),
)


# ---------------------------------------------------------------- 冒烟门

def _line_start_offsets(data: bytes) -> list[int]:
    starts = [0]
    for index, byte in enumerate(data):
        if byte == 0x0A:
            starts.append(index + 1)
    return starts


def _py_nonbehavioral_spans(source: str) -> list[tuple[int, int]]:
    data = source.encode("utf-8")
    line_starts = _line_start_offsets(data)

    def offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    spans: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) and node.end_lineno is not None:
            spans.append((offset(node.lineno, node.col_offset),
                          offset(node.end_lineno, node.end_col_offset)))
    for line_no, line in enumerate(source.splitlines(), start=1):
        raw = line.encode("utf-8")
        hash_pos = raw.find(b"#")
        if hash_pos >= 0:
            start = offset(line_no, hash_pos)
            spans.append((start, start + len(raw) - hash_pos))
    return spans


def _ts_nonbehavioral_spans(source: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"/\*.*?\*/", source, flags=re.S):
        spans.append((len(source[:match.start()].encode("utf-8")),
                      len(source[:match.end()].encode("utf-8"))))
    for match in re.finditer(r"^[ \t]*//.*$", source, flags=re.M):
        spans.append((len(source[:match.start()].encode("utf-8")),
                      len(source[:match.end()].encode("utf-8"))))
    return spans


def smoke(path: Path, source: str, injected_at: int) -> str | None:
    if path.suffix == ".py":
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return f"注入把语法写坏了: {exc}"
        spans = _py_nonbehavioral_spans(source)
    else:
        spans = _ts_nonbehavioral_spans(source)
    for start, end in spans:
        if start <= injected_at < end:
            return "注入点落在注释/docstring 里 —— 行为没变,结果是假信号"
    return None


def run_tests() -> tuple[int, int, list[str], str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-p", "no:randomly", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    out = proc.stdout + proc.stderr
    failed_ids = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
    n_failed = sum(int(m) for m in re.findall(r"(\d+) failed", out))
    n_error = sum(int(m) for m in re.findall(r"(\d+) error", out))
    return proc.returncode, n_failed + n_error, failed_ids, out


def selftest() -> int:
    """冒烟门自证:两个必判 BROKEN(docstring / 注释),一个必须放行(真行为)。"""
    job = (ROOT / JOB).read_text(encoding="utf-8")
    must_break = [
        ("插进模块 docstring 中间(行为不变)",
         "**1. 工作单元是 finding,不是文章。**", "**1. 工作单元 return [] 是 finding,不是文章。**"),
        ("插进 `#` 行注释里(行为不变)",
         "#: 单个任务最多处理多少篇", "#: 单个任务 return [] 最多处理多少篇"),
    ]
    must_pass = [
        ("改事件字段名(**真行为**)",
         '            "message": message,', '            "msg": message,'),
    ]
    ok = True
    for name, old, new in must_break:
        if job.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {job.count(old)} 次")
            ok = False
            continue
        at = len(job[:job.index(old)].encode("utf-8"))
        broken = smoke(ROOT / JOB, job.replace(old, new, 1), at)
        print(f"  [{'BROKEN(正确)' if broken else '🔴 漏过(冒烟门是摆设)'}] {name}"
              + (f" · {broken}" if broken else ""))
        ok = ok and bool(broken)
    for name, old, new in must_pass:
        if job.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {job.count(old)} 次")
            ok = False
            continue
        at = len(job[:job.index(old)].encode("utf-8"))
        broken = smoke(ROOT / JOB, job.replace(old, new, 1), at)
        print(f"  [{'通过(正确)' if not broken else f'🔴 误判 BROKEN({broken})'}] {name}")
        ok = ok and not broken
    print("\n冒烟门自证:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if not os.getenv("TEST_DATABASE_URL"):
        print("需要 TEST_DATABASE_URL(指向独立测试库)")
        return 2

    print("=" * 96)
    rc, n_bad, _, out = run_tests()
    print(f"BASELINE: rc={rc} 失败+错误={n_bad}  {out.strip().splitlines()[-1] if out.strip() else ''}")
    if rc != 0 or n_bad:
        print("基线不绿,变异结果无意义,停止。")
        return 1

    results: list[tuple[str, str, str]] = []
    for mut in MUTATIONS:
        path = ROOT / mut.path
        original = path.read_bytes()
        text = original.decode("utf-8")
        hits = text.count(mut.old)
        if hits != 1:
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN 锚点命中 {hits} 次(需恰好 1)→ 计为**存活**")
            results.append((mut.mid, "存活", f"BROKEN 锚点命中 {hits} 次"))
            continue
        injected_at = len(text[:text.index(mut.old)].encode("utf-8"))
        mutated = text.replace(mut.old, mut.new, 1)
        broken = smoke(path, mutated, injected_at)
        if broken:
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN {broken} → 计为**存活**")
            results.append((mut.mid, "存活", f"BROKEN {broken}"))
            continue
        try:
            path.write_bytes(mutated.encode("utf-8"))
            rc, n_bad, failed_ids, out = run_tests()
            last = out.strip().splitlines()[-1] if out.strip() else ""
            if rc == 0:
                verdict, why = "存活", "A类:仍绿"
            elif n_bad == 0:
                verdict, why = "存活", f"B类:假红(rc={rc} 但零条断言失败/错误)"
            elif not any(mut.expect_red in fid for fid in failed_ids):
                verdict, why = "存活", f"C类:判别力不成立,期望 {mut.expect_red} 未转红"
            else:
                verdict, why = "被杀", f"rc={rc} 失败+错误={n_bad} · 判别力命中 {mut.expect_red}"
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  层: {mut.layer}\n  {verdict} | {why}\n  {last}")
            results.append((mut.mid, verdict, why))
        finally:
            path.write_bytes(original)

    print("\n" + "=" * 96)
    for mid, verdict, why in results:
        print(f"  [{verdict}] {mid}  {why}")
    survived = [r for r in results if r[1] != "被杀"]
    print(f"\n被杀 {len(results)-len(survived)}/{len(results)} · 存活 {len(survived)}")

    rc, n_bad, _, out = run_tests()
    print(f"\n还原后复跑: rc={rc} 失败+错误={n_bad} — "
          f"{out.strip().splitlines()[-1] if out.strip() else ''}")
    return 0 if not survived and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
