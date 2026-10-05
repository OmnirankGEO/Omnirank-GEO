"""变异注入验证 · 包③(标题自然化)· 详版工单 §3 变异要求。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_title_naturalness_2026_08_01.py
    python scripts/mutation_title_naturalness_2026_08_01.py --selftest

框架复用 P3a(同包①,不 fork)。工单点名的三个变异方向都在:
  · 黑名单清空        → M1
  · 对齐器旁路        → M2 / M3
  · 模板换回旧文案    → M4
另加 AI 质检旁路 fail-closed 被改成猜 natural(M5)。
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

_FRAMEWORK = ROOT / "scripts" / "mutation_p3a_batch_review_2026_08_01.py"
_spec = importlib.util.spec_from_file_location("_mut_framework_title", _FRAMEWORK)
_fw = importlib.util.module_from_spec(_spec)
sys.modules["_mut_framework_title"] = _fw   # dataclass 解析注解需要模块已登记
_spec.loader.exec_module(_fw)
Mutation = _fw.Mutation
smoke = _fw.smoke

BL = "writing/title_jargon_blacklist.py"
ALIGN = "writing/title_keyword_alignment.py"
GEN = "writing/keyword_topic_generator.py"
TQ = "services/title_quality_ai.py"

TEST_FILES = (
    "tests/test_title_naturalness_2026_08_01.py",
    "tests/test_fallback_title_form_2026_08_01.py",
    "tests/test_title_batch_dedupe_2026_07_31.py",
    "tests/test_legal_prohibition_single_source.py",
)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        mid="M1",
        task="🔴 黑名单清空(术语约束失效,提示词与机审都不再拦)",
        layer="_JARGON_RE(扫描器本体)",
        path=BL,
        # 🔴 第一版只把 18 个词里的第 1 个换掉,其余仍能命中 → 变异**假存活**。
        # 要让"黑名单失效"这件事真的发生,得打在扫描器本体上。
        old='_JARGON_RE: Final = re.compile("|".join(re.escape(t) for t in TITLE_JARGON_TERMS))',
        new='_JARGON_RE: Final = re.compile("__never_matches__")',
        expect_red="test_lock1c_blacklist_actually_detects_jargon",
    ),
    Mutation(
        mid="M2",
        task="🔴 疑问壳压缩旁路(病 A 回归:关键词又整串硬塞)",
        layer="title_anchor_from_purchased_keyword 的压缩分支",
        path=ALIGN,
        old="    if compressed and compressed != anchor and len(compressed) >= 3:",
        new="    if False:",
        expect_red="test_lock2b_question_shell_compression_never_swaps_the_object",
    ),
    Mutation(
        mid="M3",
        task="🔴 疑问词表写宽(把「哪里」加回去 → 剥出语法垃圾)",
        layer="_QUESTION_SHELL_LEAD_RE",
        path=ALIGN,
        old=r'    r"(?:哪家|哪個|哪个|哪些|哪种|哪種|哪款|什么样的|什麼樣的|怎样的|怎樣的)"',
        new=r'    r"(?:哪家|哪個|哪个|哪些|哪种|哪種|哪款|哪里|哪裡|什么样的|什麼樣的|怎样的|怎樣的)"',
        expect_red="test_lock2c_locative_interrogative_is_not_stripped",
    ),
    Mutation(
        mid="M4",
        task="🔴 模板换回旧文案(内部术语回流到标题)",
        layer="_fallback_style_title_map 选购族",
        path=GEN,
        old='            f"{year}年{{kw}}选购指南｜少走弯路的挑法",',
        new='            f"{{kw}}选型指南｜{year}年证据字段与风险检查",',
        expect_red="test_lock1_fallback_templates_contain_zero_internal_jargon",
    ),
    Mutation(
        mid="M5",
        task="🔴 标题质检 fail-closed 失效(调用挂了就猜 natural)",
        layer="assess_title_naturalness 的异常分支",
        path=TQ,
        old='        return {"verdict": VERDICT_NOT_CHECKED, "reason": "标题质检未完成(服务暂时不可用)",\n                "suggestion": ""}',
        new='        return {"verdict": VERDICT_NATURAL, "reason": "标题质检未完成(服务暂时不可用)",\n                "suggestion": ""}',
        expect_red="test_title_qc_fail_closed_never_guesses_natural",
    ),
)


def run_tests() -> tuple[int, int, list[str], str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TEST_FILES,
         "-q", "-p", "no:randomly", "-p", "no:cacheprovider", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    out = proc.stdout + proc.stderr
    failed_ids = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
    n_failed = sum(int(m) for m in re.findall(r"(\d+) failed", out))
    n_error = sum(int(m) for m in re.findall(r"(\d+) error", out))
    return proc.returncode, n_failed + n_error, failed_ids, out


def selftest() -> int:
    src = (ROOT / BL).read_text(encoding="utf-8")
    ok = True
    must_break = [
        ("插进模块 docstring(行为不变)", src,
         "枚举堵不完",
         "枚举堵不完 return []"),
        ("插进 `#:` 注释(行为不变)", src,
         "#: 从生产 47 条污染样本归纳。",
         "#: 从生产 47 条污染样本 return [] 归纳。"),
    ]
    must_pass = [
        ("改元组里的取值串(**真行为**:黑名单少一个词)", src,
         '    "情景测算",', '    "情景測算",'),
    ]
    for name, source, old, new in must_break:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次")
            ok = False
            continue
        at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / BL, source.replace(old, new, 1), at)
        print(f"  [{'BROKEN(正确)' if broken else '🔴 漏过'}] {name}")
        ok = ok and bool(broken)
    for name, source, old, new in must_pass:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次")
            ok = False
            continue
        at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / BL, source.replace(old, new, 1), at)
        print(f"  [{'通过(正确)' if not broken else f'🔴 误判 BROKEN({broken})'}] {name}")
        ok = ok and not broken
    print("\n冒烟门自证:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    print("=" * 96)
    rc, n_bad, _, out = run_tests()
    last = out.strip().splitlines()[-1] if out.strip() else ""
    print(f"BASELINE: rc={rc} 失败+错误={n_bad}  {last}")
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
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN 锚点命中 {hits} 次 → **存活**")
            results.append((mut.mid, "存活", f"锚点命中 {hits} 次"))
            continue
        at = len(text[:text.index(mut.old)].encode("utf-8"))
        mutated = text.replace(mut.old, mut.new, 1)
        broken = smoke(path, mutated, at)
        if broken:
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n  BROKEN {broken} → **存活**")
            results.append((mut.mid, "存活", f"BROKEN {broken}"))
            continue
        try:
            path.write_bytes(mutated.encode("utf-8"))
            rc, n_bad, failed_ids, out = run_tests()
            last = out.strip().splitlines()[-1] if out.strip() else ""
            if rc == 0:
                verdict, why = "存活", "A类:仍绿"
            elif n_bad == 0:
                verdict, why = "存活", f"B类:假红(rc={rc} 但零条断言失败)"
            elif not any(mut.expect_red in fid for fid in failed_ids):
                verdict, why = "存活", f"C类:期望 {mut.expect_red} 未转红"
            else:
                verdict, why = "被杀", f"rc={rc} 失败+错误={n_bad} · 命中 {mut.expect_red}"
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
    last = out.strip().splitlines()[-1] if out.strip() else ""
    print(f"\n还原后复跑: rc={rc} 失败+错误={n_bad} — {last}")
    return 0 if not survived and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
