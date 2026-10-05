"""变异注入验证 · P3a 批量审核入口 + 两处并入项(工单 §6 / §8)。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_p3a_batch_review_2026_08_01.py
    python scripts/mutation_p3a_batch_review_2026_08_01.py --selftest   # 冒烟门自证

三层分流(**三者都计存活**,§8 硬要求):
  A. 仍绿(rc==0)                                   → 存活
  B. rc!=0 但**零条断言失败**(失败/错误计数都是 0)  → **假红**,存活
     (崩溃、collection error、语法错冒充"锁咬住了")
  C. 判别力断言 `expect_red` 未出现在失败清单        → 红得不是地方,存活

🔴 **变异注入必须冒烟**(P4 复审里对方自曝的坑,同样适用于我):
把 `return []` 插进 docstring 中间、行为压根没变,却因为别的原因红了 →
会被读成"已杀"。每个变异做三重冒烟:
  ① 锚点**恰好命中 1 次**(0 次 = 没注入,>1 次 = 注入位置不确定);
  ② 注入后语法没被写坏(.py 走 ast.parse;.tsx 见下);
  ③ 🔴 注入点不落在**非行为区**。

🔴 "非行为区"的口径比 P4 那版**更窄**,这是本轮的订正:
P4 把**所有**字符串字面量都算作非行为区。那对它够用,对本包不够 ——
本包有一个变异专门改字典里的取值串(`"publication_h0_state": "operator_hard"`),
那是**如假包换的行为**,按 P4 的口径会被误判 BROKEN、误计"存活"。
所以这里只把两类算作非行为:
  · `#` / `//` / `/* */` 注释;
  · Python 里**裸字符串表达式语句**(`ast.Expr` 且值是 str)= docstring 与游离串。
既仍然能抓住 P4 那个 docstring 陷阱(--selftest 现场证明),又不会把真行为误伤。

🔴 `ast` 的 `col_offset` / `end_col_offset` 是 **UTF-8 字节**偏移,不是字符偏移。
本仓注释与 docstring 几乎全是中文,按字符建行首索引会让每个含非 ASCII 的行都算歪。
全程在 `bytes` 空间算,调用方也传字节偏移。

还原用**字节级** read/write(不用 `git checkout`:工作区可能有未提交改动;
也不用文本模式:会把 LF 换成 CRLF,diff 虚增几千行)。
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
TEST_FILE = "tests/test_p3a_batch_review_2026_08_01.py"

SVC = "services/article_batch_review.py"
TSX = "frontend/src/pages/Writing/WritingHall.tsx"
DDB = "db/diagnosis_db.py"
STATIC = "tests/test_c4_static_assertions_2026_08_01.py"


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
        task="🔴 摘掉逐项隔离(异常直接冒泡,整批中断)",
        layer="run_batch 的 per-item except",
        path=SVC,
        old="            failed_count += 1\n    return {",
        new="            raise\n            failed_count += 1\n    return {",
        expect_red="test_lock1_one_item_failure_does_not_abort_or_swallow_the_others",
    ),
    Mutation(
        mid="M2",
        task="🔴 摘掉一键通过的 H0 护栏(什么档都放行)",
        layer="assert_bulk_pass_allowed 的 h0 != clear 否决",
        path=SVC,
        old="    if h0 != H0_CLEAR:",
        new="    if False:",
        expect_red="test_lock2_bulk_pass_guard_rejects_every_h0_class",
    ),
    Mutation(
        mid="M3",
        task="🔴 去掉「跳过已人工签发」(批量机审会抹掉全部签发)",
        layer="review_one_article 的 human_approved 前置判断",
        path=SVC,
        old='        if str((row or {}).get("article_human_review_status") or "") == "approved":',
        new="        if False:",
        expect_red="test_lock4_batch_review_never_wipes_an_existing_human_signoff",
    ),
    Mutation(
        mid="M4",
        task="失败分级失效(4xx 也标成可重试)",
        layer="_retryable 的状态码分档",
        path=SVC,
        old="        return not (status is not None and 400 <= int(status) < 500)",
        new="        return True",
        expect_red="test_lock3_retryable_grading_by_status_class",
    ),
    Mutation(
        mid="M5",
        task="normalize_ids 不去重(total 与真实处理数对不上)",
        layer="normalize_ids 的 seen 集合",
        path=SVC,
        old="        if value in seen:\n            continue",
        new="        if False:\n            continue",
        expect_red="test_lock6_counts_are_real_and_self_consistent",
    ),
    Mutation(
        mid="M6",
        task="🔴 前端批量审核切到**收费**的整篇重写端点",
        layer="BatchReviewPanel 的 authFetch URL",
        path=TSX,
        old="            const resp = await authFetch(`/api/articles/batch-review`, {",
        new="            const resp = await authFetch(`/api/writing/rewrite`, {",
        expect_red="test_lock5_every_network_call_from_the_batch_panel_is_zero_charge",
    ),
    Mutation(
        mid="M7",
        task="🔴 结果 Map 改成整体覆盖(上一批的成功被后一批抹掉)",
        layer="BatchReviewPanel 的 mergeResults",
        path=TSX,
        old="            const next = { ...prev };",
        new="            const next: Record<number, BatchItemResult> = {};",
        expect_red="test_lock7c_results_map_is_merged_never_wholesale_reset",
    ),
    Mutation(
        mid="M8",
        task="分组改回拿机审快照自己推断(与服务端判定漂开)",
        layer="reviewGroupOf 的三态读取",
        path=TSX,
        old="    const h0 = topic.publication_h0_state;",
        new="    const h0 = topic.article_review_status === 'blocked' ? 'legal_hard' : topic.publication_h0_state;",
        expect_red="test_lock7d_grouping_reads_only_the_tristate_never_reinfers",
    ),
    Mutation(
        mid="M11",
        task="🔴 无脑渲染「确认」按钮(L3 卡片上给一颗必然失败的按钮)",
        layer="BatchReviewPanel 每卡确认按钮的条件渲染",
        path=TSX,
        old="                                                {topic.advisory_state === 'open' ? (",
        new="                                                {true ? (",
        expect_red="test_lock10e_confirm_button_is_not_rendered_where_it_can_only_fail",
    ),
    Mutation(
        mid="M9",
        task="改名只改一半(fail-closed 兜底仍回旧档名)",
        layer="db/diagnosis_db.py 的 fail-closed 兜底值 —— 并入项 1",
        path=DDB,
        old='"publication_h0_state": "operator_hard",',
        new='"publication_h0_state": "tenant_or_funds",',
        expect_red="test_lock8b_no_stale_tenant_or_funds_value_left_in_the_tree",
    ),
    Mutation(
        mid="M10",
        task="给拆出来的静态文件加回 fixture(无库环境又会整体 ERROR)",
        layer='test_c4_static_assertions 的「无 fixture」约束 —— 并入项 2',
        path=STATIC,
        old="def test_admin_quality_panel_hard_only_uses_real_path():",
        new="import pytest\n\n\n@pytest.fixture(scope=\"module\", autouse=True)\ndef _reintroduced_fixture():\n    yield\n\n\ndef test_admin_quality_panel_hard_only_uses_real_path():",
        expect_red="test_lock9b_static_file_has_no_fixture_and_no_env_dependent_import",
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
    """Python 的非行为区(**字节**偏移):裸字符串表达式语句 + `#` 注释。

    刻意**不**把普通字符串常量算进来 —— 见模块 docstring 里对 P4 口径的订正。
    """
    data = source.encode("utf-8")
    line_starts = _line_start_offsets(data)

    def offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    spans: list[tuple[int, int]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # 裸字符串表达式语句 = docstring / 游离串:删了改了都不影响行为
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
    """TS/TSX 的非行为区(**字节**偏移):`//` 行注释 + `/* */` 块注释。

    字符串字面量**不算**非行为 —— 本包最关键的一个变异就是换 URL 串。
    """
    data = source.encode("utf-8")
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"/\*.*?\*/", source, flags=re.S):
        spans.append((len(source[:match.start()].encode("utf-8")),
                      len(source[:match.end()].encode("utf-8"))))
    for match in re.finditer(r"^[ \t]*//.*$", source, flags=re.M):
        spans.append((len(source[:match.start()].encode("utf-8")),
                      len(source[:match.end()].encode("utf-8"))))
    return spans


def smoke(path: Path, source: str, injected_at: int) -> str | None:
    """返回 None 表示冒烟通过;否则返回 BROKEN 原因。"""
    if path.suffix == ".py":
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return f"注入把语法写坏了: {exc}"
        spans = _py_nonbehavioral_spans(source)
    else:
        # .tsx 没有免费的语法检查器可用在这一层;真正的把关是变异跑完后
        # 那一轮 tsc(交付流程里单独跑),这里只挡"改的是注释"。
        spans = _ts_nonbehavioral_spans(source)
    for start, end in spans:
        if start <= injected_at < end:
            return "注入点落在注释/docstring 里 —— 行为没变,结果是假信号"
    return None


# ---------------------------------------------------------------- 跑测试

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
    """🔴 冒烟门本身的判别力自证(`--selftest`)。

    验证工具自身也必须被验证 —— "我有冒烟门"这句话本身要证明。
    三组用例:
      · 前两组**必须判 BROKEN**(docstring 注入 / 注释注入,行为没变);
      · 第三组**必须判通过**(改字典取值串 = 真行为)—— 这一组专门证明本轮
        把口径收窄的订正是有效的:按 P4 那版"所有字符串都算非行为",它会被
        误判 BROKEN,于是 M9 这个真变异会被误计"存活"。
    """
    svc = (ROOT / SVC).read_text(encoding="utf-8")
    ddb = (ROOT / DDB).read_text(encoding="utf-8")

    must_break = [
        ("插进模块 docstring 中间(纯文本,行为不变)", SVC, svc,
         "三条硬性质:", "三条硬性质 return [] :"),
        ("插进 `#` 行注释里(行为不变)", SVC, svc,
         "# 真实计数,不做百分比", "# 真实计数 return [] ,不做百分比"),
    ]
    must_pass = [
        ("改字典里的取值串(**真行为** · P4 那版口径会误判 BROKEN)", DDB, ddb,
         '"publication_h0_state": "operator_hard",', '"publication_h0_state": "tenant_or_funds",'),
    ]

    ok = True
    for name, rel, source, old, new in must_break:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次(需恰好 1)")
            ok = False
            continue
        injected_at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / rel, source.replace(old, new, 1), injected_at)
        verdict = "BROKEN(正确)" if broken else "🔴 漏过(冒烟门是摆设)"
        print(f"  [{verdict}] {name}" + (f" · {broken}" if broken else ""))
        ok = ok and bool(broken)

    for name, rel, source, old, new in must_pass:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次(需恰好 1)")
            ok = False
            continue
        injected_at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / rel, source.replace(old, new, 1), injected_at)
        verdict = "通过(正确)" if not broken else f"🔴 误判 BROKEN({broken})"
        print(f"  [{verdict}] {name}")
        ok = ok and not broken

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
