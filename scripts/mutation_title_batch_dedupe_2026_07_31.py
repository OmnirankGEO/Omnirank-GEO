"""变异注入验证 · P4 标题批次级去重(工单 §7 / §8)。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_title_batch_dedupe_2026_07_31.py

三层分流(**三者都计存活**,§8 硬要求):
  A. 仍绿(rc==0)                          → 存活
  B. rc!=0 但**零条断言失败**(失败/错误计数都是 0)→ **假红**,存活
     (崩溃、collection error、语法错冒充"锁咬住了")
  C. 判别力断言 `expect_red` 未出现在失败清单 → 红得不是地方,存活

🔴 **变异注入必须冒烟**(复审 P4 裁定里对方自曝的坑,同样适用于我):
把 `return []` 插进 docstring 中间、行为压根没变,却因为别的原因红了 →
会被读成"已杀"。本脚本对每个变异做三重冒烟:
  ① 锚点**恰好命中 1 次**(0 次 = 没注入,>1 次 = 注入位置不确定);
  ② 注入后 `ast.parse` 通过(语法没被写坏 → 否则判 BROKEN,不计 KILLED);
  ③ 🔴 **注入点不在字符串字面量/注释里**(用 AST 收集所有 str 常量与 `#` 注释
     的**字节**偏移区间,注入偏移落在其中即判 BROKEN)—— 这条专防"改了个注释
     当成改了行为"。

🔴 **hook 有几个调用点就单独摘几次**(复审 P4 裁定①):
`_apply_batch_title_dedupe` 有两个调用点 —— 兜底链与主链。
**合并摘除时,兜底链那条锁一红就报"已杀",正好掩盖主链无锁的缺口。**
所以 M1a/M1b 各摘一次、各自要求不同的锁转红。

还原用**字节级** read/write(不用 `git checkout`:工作区可能有未提交改动;
也不用文本模式:会把 LF 换成 CRLF)。
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
TEST_FILE = "tests/test_title_batch_dedupe_2026_07_31.py"
GEN = "writing/keyword_topic_generator.py"
DED = "writing/title_batch_dedupe.py"


@dataclasses.dataclass(frozen=True)
class Mutation:
    mid: str
    task: str
    layer: str          # 拆的是哪一层
    path: str
    old: str
    new: str
    expect_red: str     # 预期转红的锁(判别力)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        mid="M1a",
        task="摘掉**兜底链**的批次去重 hook(无 API KEY 那条)",
        layer="generate() → _fallback_generate() 之后的接线",
        path=GEN,
        old="            return self._finalize_titles(\n"
            "                await self._apply_batch_title_dedupe(self._fallback_generate())\n"
            "            )",
        new="            return self._finalize_titles(self._fallback_generate())",
        expect_red="test_lock4_provider_failure_fallback_avoids_same_structure",
    ),
    Mutation(
        mid="M1b",
        task="🔴 摘掉**主链**的批次去重 hook(有 API KEY · 生产常态走这条)",
        layer="generate() → _generate_batch() 成功之后的接线",
        path=GEN,
        old="        return self._finalize_titles(await self._apply_batch_title_dedupe(all_topics))",
        new="        return self._finalize_titles(all_topics)",
        expect_red="test_lock5_main_chain_hook_is_wired",
    ),
    Mutation(
        mid="M2",
        task="去掉去重核心(冲突项直接跳过,不重生成)",
        layer="dedupe_topic_titles 的冲突处理分支",
        path=DED,
        old='        report["conflicts"] += 1',
        new='        continue\n        report["conflicts"] += 1',
        expect_red="test_lock1_six_colliding_inputs_end_up_pairwise_distinct_with_regeneration",
    ),
    Mutation(
        mid="M3",
        task="fallback 选模板改回纯 modulo(slot_index % len)",
        layer="pick_diverse_template 的稳定散列 + used_formulas 跳过",
        path=DED,
        # [2026-08-01] 锚点随《兜底模板尊重 title_form》包同步:
        # picker 现在先按形态过滤成 `pool` 再选。变异意图不变(退回纯 modulo)。
        old="    start = _stable_seed(title_anchor_from_purchased_keyword(keyword), style, slot_index) % len(pool)\n"
            "    for offset in range(len(pool)):\n"
            "        candidate = pool[(start + offset) % len(pool)]\n"
            '        if title_formula_skeleton(candidate.replace("{kw}", ""), None) not in used:\n'
            "            return candidate, False\n"
            "    return pool[start], True",
        new="    return templates[slot_index % len(templates)], False",
        expect_red="test_lock4b_fallback_picker_is_not_plain_modulo",
    ),
    Mutation(
        mid="M4",
        task="🔴 去重时允许改写客户购买的关键词(摘掉身份红线复核)",
        layer="dedupe_topic_titles 的 _keyword_identity_preserved 否决",
        path=DED,
        old="            if not _keyword_identity_preserved(candidate, keyword):\n"
            '                report["identity_rejected"] += 1\n'
            "                continue  # 🔴 红线:宁可留重复,也不改客户买的词",
        new="            if False:\n"
            '                report["identity_rejected"] += 1\n'
            "                continue",
        expect_red="test_lock2b_identity_breaking_candidate_is_rejected_even_if_it_dedupes",
    ),
    Mutation(
        mid="M5",
        task="稳定散列换成内置 hash()(跨进程漂移)",
        layer="_stable_seed 的 hashlib.sha256",
        path=DED,
        old='    raw = "\\x1f".join(str(p) for p in parts).encode("utf-8")\n'
            '    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")',
        new='    return abs(hash("\\x1f".join(str(p) for p in parts)))',
        expect_red="test_lock4d_picker_is_stable_across_processes",
    ),
    Mutation(
        mid="M6",
        task="不检查同 quote 已存在的未发布标题",
        layer="dedupe_topic_titles 的 existing_titles 播种",
        path=DED,
        old="    used_exact, used_skeleton, used_semantic = _seed_used(existing_titles)\n"
            "    report: Dict[str, Any] = {",
        new="    used_exact, used_skeleton, used_semantic = _seed_used(())\n"
            "    report: Dict[str, Any] = {",
        expect_red="test_lock1c_existing_unpublished_titles_are_considered",
    ),
    Mutation(
        mid="M7",
        task="🔴 摘掉形态保护(用户锁定形态时仍可翻成问句式)",
        layer="dedupe_topic_titles 的 preserve_form / _same_title_form",
        path=DED,
        old="            if preserve_form and not _same_title_form(candidate, title):\n"
            '                report["form_rejected"] += 1\n'
            "                continue  # 🔴 不得绕过形态层改变用户显式选择的标题形态",
        new="            if False:\n"
            '                report["form_rejected"] += 1\n'
            "                continue",
        expect_red="test_lock4e_dedupe_never_flips_title_form_when_form_is_locked",
    ),
)


def _literal_and_comment_spans(source: str) -> list[tuple[int, int]]:
    """所有字符串字面量 + `#` 注释的**字节**偏移区间。

    注入点落进这些区间 = 改的是注释/文档字符串,**行为没变**,这种"存活/被杀"
    都是假信号。这正是复审 P4 时对方自曝踩的坑。

    🔴 必须在**字节空间**算:`ast` 的 `col_offset` / `end_col_offset` 是
    **UTF-8 字节偏移**,不是字符偏移。本仓注释和字符串几乎全是中文,按字符建
    行首索引会让每个含非 ASCII 的行都算歪 —— 第一版就是这么把一个正常的
    `return` 语句判成"落在字符串里"而误报 BROKEN 的(误报方向是把"被杀"读成
    "存活",偏保守,但仍然是错的)。这里统一用 `bytes`,并让调用方也传字节偏移。
    """
    data = source.encode("utf-8")
    spans: list[tuple[int, int]] = []
    line_starts = [0]
    for byte_index, byte in enumerate(data):
        if byte == 0x0A:  # b"\n"
            line_starts.append(byte_index + 1)

    def offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.end_lineno is None:
                continue
            spans.append((offset(node.lineno, node.col_offset),
                          offset(node.end_lineno, node.end_col_offset)))
    # `#` 注释:用 tokenize 更准,但这里只需保守判断,逐行找注释起点。
    # 同样在字节空间:`line` 先编码再找 b"#"。
    for line_no, line in enumerate(source.splitlines(), start=1):
        raw = line.encode("utf-8")
        hash_pos = raw.find(b"#")
        if hash_pos >= 0:
            start = offset(line_no, hash_pos)
            spans.append((start, start + len(raw) - hash_pos))
    return spans


def smoke(path: Path, source: str, injected_at: int) -> str | None:
    """返回 None 表示冒烟通过;否则返回 BROKEN 原因。"""
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
    """🔴 冒烟门本身的判别力自证(`--selftest`)。

    复审 P4 时对方自曝:把 `return []` 插进 docstring 中间、**行为压根没变**,
    却被读成变异结果。所以"我有冒烟门"这句话本身也要证明 —— 这里喂两个必然
    无效的注入,冒烟门必须**都**判 BROKEN;任何一个漏过去,说明门是摆设。
    """
    ded = (ROOT / DED).read_text(encoding="utf-8")
    cases = [
        ("插进模块 docstring 中间(纯文本,行为不变)",
         "**四类检查(工单 §7 原文)**", "**四类检查 return [] (工单 §7 原文)**"),
        ("插进行尾 `#` 注释里(行为不变)",
         "# 🔴 红线:宁可留重复,也不改客户买的词",
         "# 🔴 红线:宁可留重复 return [] ,也不改客户买的词"),
    ]
    ok = True
    for name, old, new in cases:
        if ded.count(old) != 1:
            print(f"  [自证跳过] {name} —— 锚点命中 {ded.count(old)} 次")
            ok = False
            continue
        injected_at = len(ded[:ded.index(old)].encode("utf-8"))
        mutated = ded.replace(old, new, 1)
        broken = smoke(ROOT / DED, mutated, injected_at)
        verdict = "BROKEN(正确)" if broken else "🔴 漏过(冒烟门是摆设)"
        print(f"  [{verdict}] {name}" + (f" · {broken}" if broken else ""))
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
        original = path.read_bytes()                       # 字节级备份
        text = original.decode("utf-8")
        hits = text.count(mut.old)
        if hits != 1:
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN 锚点命中 {hits} 次(需恰好 1)→ 计为**存活**")
            results.append((mut.mid, "存活", f"BROKEN 锚点命中 {hits} 次"))
            continue
        # 注入点也换算成**字节**偏移(span 在字节空间)
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
            path.write_bytes(original)                     # 字节级还原

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
