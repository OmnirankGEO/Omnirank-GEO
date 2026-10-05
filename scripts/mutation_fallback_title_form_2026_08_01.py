"""变异注入验证 · 兜底模板尊重 title_form(工单 §3)。

体例照搬 `scripts/mutation_title_batch_dedupe_2026_07_31.py`(P4 返修版):
三层分流 + 三重冒烟门 + `--selftest` + 字节级还原。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_fallback_title_form_2026_08_01.py
    python scripts/mutation_fallback_title_form_2026_08_01.py --selftest

工单 §3 四变异:
  摘 form 过滤      → 锁①红
  删补进去的模板    → 锁④红
  过滤方向反接      → 锁②红
  auto 误挂过滤     → 锁③红
另加两条(本包自查出来的真风险):
  M5 过滤后为空时降级回全池 → 锁⑤红("为了凑数破 form")
  M6 主链调用点漏传 title_form → 锁①b 红(P4 教训:hook 有几个调用点锁几条)
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
TEST_FILE = "tests/test_fallback_title_form_2026_08_01.py"
GEN = "writing/keyword_topic_generator.py"
DED = "writing/title_batch_dedupe.py"


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
        mid="M1",
        task="摘掉 form 过滤(picker 无视 title_form)",
        layer="pick_diverse_template 的 filter_templates_by_form",
        path=DED,
        old="    pool, filtered = filter_templates_by_form(templates, title_form)",
        new="    pool, filtered = templates, False",
        expect_red="test_lock1_fallback_chain_open_produces_zero_questions",
    ),
    Mutation(
        mid="M2",
        task="删掉补进去的形态补全模板(全问句族 + open 重新无解)",
        layer="_fallback_form_completion_map",
        path=GEN,
        old='    return {\n        "案例、数据与 ROI": [\n'
            '            f"{{kw}}投入产出测算口径｜{year}年数据来源与复核路径",\n'
            '            f"{{kw}}成本收益情景测算｜{year}年假设、边界与验证方法",\n'
            "        ],\n    }",
        new="    return {}",
        expect_red="test_lock4_all_question_family_still_serves_open_form",
    ),
    Mutation(
        mid="M3",
        task="🔴 过滤方向反接(open 给问句 / question 给陈述)",
        layer="filter_templates_by_form 的 want_question",
        path=DED,
        old='    want_question = form == "question"',
        new='    want_question = form == "open"',
        expect_red="test_lock2_question_form_yields_all_question_titles",
    ),
    Mutation(
        mid="M4",
        task="🔴 auto 误挂过滤(auto 也走 open 过滤 → auto 分布被改)",
        layer="filter_templates_by_form 的 auto 早退",
        path=DED,
        old='    form = str(title_form or "").strip().lower()\n'
            '    if form not in ("open", "question"):\n'
            "        return templates, False",
        new='    form = str(title_form or "").strip().lower()\n'
            '    if form not in ("open", "question"):\n'
            '        form = "open"',
        expect_red="test_lock3b_auto_picker_output_is_deterministic_and_unfiltered",
    ),
    Mutation(
        mid="M5",
        task="🔴 过滤后为空时降级回全池(为了凑数破 form)",
        layer="pick_diverse_template 的 '不回退全池' 分支",
        path=DED,
        old="    if filtered and not pool:\n"
            "        # 该族在该形态下无模板可用。**不回退全池**(回退=给用户他没选的形态)。\n"
            "        # 锁 6 保证每族两种形态都有,所以这条分支实际不可达。\n"
            '        return "", True',
        new="    if filtered and not pool:\n        pool = templates",
        expect_red="test_lock5b_empty_filtered_pool_never_falls_back_to_wrong_form",
    ),
    Mutation(
        mid="M6",
        task="🔴 主链调用点漏传 title_form(:1027 语义安全修复那条)",
        layer="_parse_response 里 _safe_fallback_title 的接线",
        path=GEN,
        old="                        topic[\"article_style\"],\n"
            "                        slot_index,\n"
            "                        title_form=self.title_form,\n"
            "                    )",
        new="                        topic[\"article_style\"],\n"
            "                        slot_index,\n"
            "                    )",
        expect_red="test_lock1b_main_chain_open_produces_zero_questions",
    ),
)


def _literal_and_comment_spans(source: str) -> list[tuple[int, int]]:
    """字符串字面量 + `#` 注释的**字节**偏移区间。

    🔴 字节不是字符:`ast` 的 col_offset 是 UTF-8 字节偏移,本仓全中文,
    按字符算会让含非 ASCII 的行 span 全歪(P4 返修时踩过并订正)。
    """
    data = source.encode("utf-8")
    spans: list[tuple[int, int]] = []
    line_starts = [0]
    for byte_index, byte in enumerate(data):
        if byte == 0x0A:
            line_starts.append(byte_index + 1)

    def offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.end_lineno is None:
                continue
            spans.append((offset(node.lineno, node.col_offset),
                          offset(node.end_lineno, node.end_col_offset)))
    for line_no, line in enumerate(source.splitlines(), start=1):
        raw = line.encode("utf-8")
        hash_pos = raw.find(b"#")
        if hash_pos >= 0:
            start = offset(line_no, hash_pos)
            spans.append((start, start + len(raw) - hash_pos))
    return spans


def smoke(source: str, injected_at: int) -> str | None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return f"注入把语法写坏了: {exc}"
    for start, end in _literal_and_comment_spans(source):
        if start <= injected_at < end:
            return "注入点落在字符串字面量/注释里 —— 行为没变,结果是假信号"
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
    """冒烟门自证:两个必然无效的注入必须都判 BROKEN。"""
    src = (ROOT / DED).read_text(encoding="utf-8")
    cases = [
        ("插进模块 docstring(纯文本,行为不变)",
         "**四类检查(工单 §7 原文)**", "**四类检查 return [] (工单 §7 原文)**"),
        ("插进行尾 `#` 注释(行为不变)",
         "# 用分隔符拼:直接连起来会让",
         "# 用分隔符拼 return [] :直接连起来会让"),
    ]
    ok = True
    for name, old, new in cases:
        if src.count(old) != 1:
            print(f"  [自证跳过] {name} —— 锚点命中 {src.count(old)} 次")
            ok = False
            continue
        injected_at = len(src[:src.index(old)].encode("utf-8"))
        broken = smoke(src.replace(old, new, 1), injected_at)
        print(f"  [{'BROKEN(正确)' if broken else '🔴 漏过(冒烟门是摆设)'}] {name}")
        ok = ok and bool(broken)
    print("")
    print("冒烟门自证:", "PASS" if ok else "FAIL")
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
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN 锚点命中 {hits} 次 → 计为**存活**")
            results.append((mut.mid, "存活", f"BROKEN 锚点命中 {hits} 次"))
            continue
        injected_at = len(text[:text.index(mut.old)].encode("utf-8"))
        mutated = text.replace(mut.old, mut.new, 1)
        broken = smoke(mutated, injected_at)
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
