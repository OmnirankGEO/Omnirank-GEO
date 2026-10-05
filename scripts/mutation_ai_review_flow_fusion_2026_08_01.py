"""变异注入验证 · 包①(内容审核 AI 化 + 核验流融合)· 工单 §5 锁 9。

用法::

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_ai_review_flow_fusion_2026_08_01.py
    python scripts/mutation_ai_review_flow_fusion_2026_08_01.py --selftest

🔴 **框架复用而非 fork**:三层分流(仍绿 / 假红 / 红得不是地方)、AST 非行为区口径、
字节级还原、冒烟门自证,全部来自 P3a 已交付的
`scripts/mutation_p3a_batch_review_2026_08_01.py`。抄一份等于让两处判别力口径各自漂移
(本仓踩过同型:测试里硬编码一份集合副本 → 改了真集合照样绿)。这里只定义**本包自己**
的变异清单与测试集。

工单 §5 锁 9 点名的两个变异方向都在:
  · 把降级改回硬拦        → M1 / M7
  · 把 H0 保留类也降级    → M2

§3A 降级最容易做废的三处:提前 return 绕过 operator_hard(M3)、
留痕失去区分度(M4/M5)、留痕反过来打废发布事务(M6)。

§3E AI 评估层四处:fail-closed 被改成猜 L1(M8)、渠道换成代理版(M9)、
幂等失效(M10)、笼统 finding 不再被拒(M11)。

§3D 批跑两处:选取口径取错列致集合恒空且不报错(M12)、
L3 占比分母混进 not_checked 致 AI 全挂反而放行铺全量(M13)。
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

# ---- 复用 P3a 框架(按文件路径加载,不依赖 scripts/ 是不是包) -------------
_FRAMEWORK = ROOT / "scripts" / "mutation_p3a_batch_review_2026_08_01.py"
_spec = importlib.util.spec_from_file_location("_mut_framework", _FRAMEWORK)
_fw = importlib.util.module_from_spec(_spec)
# 🔴 必须先登记进 sys.modules 再 exec:框架里的 @dataclass 会用
# `sys.modules[cls.__module__].__dict__` 解析注解,模块没登记时它拿到 None 直接炸。
sys.modules["_mut_framework"] = _fw
_spec.loader.exec_module(_fw)
Mutation = _fw.Mutation
smoke = _fw.smoke

GATE = "services/article_review_gate.py"
AI = "services/article_ai_review.py"
BATCH = "services/article_ai_review_batch.py"
TSX = "frontend/src/pages/Writing/WritingHall.tsx"

TEST_FILES = (
    "tests/test_ai_review_flow_fusion_2026_08_01.py",
    "tests/test_publish_gate_tristate_2026_07_31.py",
    "tests/test_p07_publish_gate_trio.py",
)


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        mid="M1",
        task="🔴 把降级改回硬拦(内容提示重新让 eligible=False)",
        layer="_verdict 的 eligible 单点派生",
        path=GATE,
        old='        "eligible": h0_state == H0_CLEAR,',
        new='        "eligible": h0_state == H0_CLEAR and not notices,',
        expect_red="test_lock1_every_downgraded_content_class_publishes_without_review",
    ),
    Mutation(
        mid="M2",
        task="🔴 把 H0 保留类也降级(资金/租户/对象完整性一起放行)",
        layer="_verdict 的 eligible 单点派生",
        path=GATE,
        old='        "eligible": h0_state == H0_CLEAR,',
        new='        "eligible": True,',
        expect_red="test_lock2_operator_hard_still_raises",
    ),
    Mutation(
        mid="M3",
        task="🔴 降级过界:内容提示就地 return clear,绕过后面的 operator_hard",
        layer="blocked 分支之后的求值顺序",
        path=GATE,
        old='        if machine == "rewrite_required":',
        new=(
            "        if content_notices:\n"
            '            return _verdict_here({"reason": "blocked"},\n'
            "                                 review_state=review_state, h0_state=H0_CLEAR)\n"
            '        if machine == "rewrite_required":'
        ),
        expect_red="test_lock2b_downgrade_does_not_leak_into_operator_hard_when_both_present",
    ),
    Mutation(
        mid="M4",
        task="留痕失去区分度(无提示也写行)",
        layer="_record_publication_notice_audit 的空提示短路",
        path=GATE,
        old="    if not notices:\n        # 锁 4 的反向面:无未处理提示的发布,一行都不许写。\n        return False",
        new="    if False:\n        return False",
        expect_red="test_lock4b_no_audit_row_when_publishing_without_notice",
    ),
    Mutation(
        mid="M5",
        task="留痕根本不写(证据链静默消失)",
        layer="_record_publication_notice_audit 读提示",
        path=GATE,
        old='    notices = verdict.get("content_notices") or []',
        new="    notices = []",
        expect_red="test_lock4_silent_audit_written_when_publishing_with_open_notice",
    ),
    Mutation(
        mid="M6",
        task="🔴 摘掉 SAVEPOINT(留痕写失败会打废调用方事务 → 发布反而失败)",
        layer="_record_publication_notice_audit 借用 cursor 的事务保护",
        path=GATE,
        old='            cursor.execute("SAVEPOINT geo_pub_notice_audit")',
        new="            pass",
        expect_red="test_lock4d_audit_failure_never_blocks_publishing",
    ),
    Mutation(
        mid="M7",
        task="🔴 提示被吞(content_notices 恒空 = 机审白跑,UI 无从提示)",
        layer="_verdict 的提示载体",
        path=GATE,
        old='        "content_notices": notices,',
        new='        "content_notices": [],',
        expect_red="test_lock1_every_downgraded_content_class_publishes_without_review",
    ),
    # ---- §3E AI 评估层 ----
    Mutation(
        mid="M8",
        task="🔴 fail-closed 失效:AI 挂了就猜一个 L1(把不可用伪装成审核通过)",
        layer="evaluate_article_content 的调用异常分支",
        path=AI,
        old='        return {"level": LEVEL_NOT_CHECKED, "summary": "AI 核查未完成(服务暂时不可用)", "findings": []}',
        new='        return {"level": LEVEL_AUTO_PASS, "summary": "AI 核查未完成(服务暂时不可用)", "findings": []}',
        expect_red="test_lock7_ai_failure_stops_at_not_checked_and_still_publishes",
    ),
    Mutation(
        mid="M9",
        task="🔴 换成代理版渠道(§2 明令禁止 OpenRouter/DashScope 的 deepseek 代理)",
        layer="DEEPSEEK_BASE_URL",
        path=AI,
        old='DEEPSEEK_BASE_URL: Final = "https://api.deepseek.com"',
        new='DEEPSEEK_BASE_URL: Final = "https://openrouter.ai/api"',
        expect_red="test_lock8_calls_official_deepseek_host_only",
    ),
    Mutation(
        mid="M10",
        task="幂等失效(每次都重新调模型 + 重复落审计行)",
        layer="review_article 的已有结论短路",
        path=AI,
        old="    if not force:\n        existing = get_existing_conclusion(cursor, article_id, chash)",
        new="    if False:\n        existing = get_existing_conclusion(cursor, article_id, chash)",
        expect_red="test_lock8b_idempotent_by_content_hash",
    ),
    Mutation(
        mid="M11",
        task="🔴 §3D 失效:没有具体触发点的笼统 finding 也照单透给客户",
        layer="_normalize_findings 的 trigger 非空过滤",
        path=AI,
        old="        if not trigger:\n            continue",
        new="        if False:\n            continue",
        expect_red="test_lock7c_vague_finding_is_rejected",
    ),
    # ---- §3D 存量批跑 ----
    Mutation(
        mid="M12",
        task="🔴 选取口径取错列(pending_human_review 去人审列取 → 集合恒空且不报错)",
        layer="PILOT_SQL 的机审列条件",
        path=BATCH,
        old="     WHERE a.article_review_status = 'pending_human_review'\n       AND COALESCE(a.article_human_review_status, '') = ''\n     ORDER BY a.id\n\"\"\"",
        new="     WHERE a.article_human_review_status = 'pending_human_review'\n     ORDER BY a.id\n\"\"\"",
        expect_red="test_lock9_pilot_scope_uses_machine_column_not_human",
    ),
    Mutation(
        mid="M13",
        task="🔴 L3 占比分母混进 not_checked(AI 全挂反而让判据放行铺全量)",
        layer="run_batch 的 l3_ratio 分母",
        path=BATCH,
        old='        "l3_ratio": (l3 / graded) if graded else None,',
        new='        "l3_ratio": (l3 / total) if total else None,',
        expect_red="test_lock9d_l3_ratio_denominator_excludes_not_checked",
    ),
    # ---- §3C 前端核验流 ----
    Mutation(
        mid="M14",
        task="🔴 轮数字样漏回 UI(「一键修复(剩 N 轮)」= 内部概念泄漏)",
        layer="WritingHall 一键修复按钮文案",
        path=TSX,
        old="{busy ? '修复中…' : '一键修复'}",
        new="{busy ? '修复中…' : `一键修复(剩 ${roundsLeft} 轮)`}",
        expect_red="test_lock5_repair_button_leaks_no_round_count",
    ),
    Mutation(
        mid="M15",
        task="🔴 把轮数上限整个删掉(不再防不收敛 —— 比泄漏数字严重得多)",
        layer="WritingHall 一键修复按钮 disabled 判据",
        path=TSX,
        old="disabled={busy || roundsLeft <= 0}",
        new="disabled={busy}",
        expect_red="test_lock5b_round_cap_is_internalised_not_deleted",
    ),
    Mutation(
        mid="M16",
        task="🔴 徽章文案退回「待人工确认」(承诺一个已不存在的阻断)",
        layer="REVIEW_STATUS_BADGES 的 pending_human_review 文案",
        path=TSX,
        old="        label: '有待核查项',",
        new="        label: '待人工确认',",
        expect_red="test_lock5e_badges_no_longer_claim_publication_will_be_blocked",
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
    """冒烟门自证:必须判 BROKEN 的两组 + 必须判通过的一组(成对,单向证明不了)。"""
    gate = (ROOT / GATE).read_text(encoding="utf-8")
    ok = True

    must_break = [
        ("插进模块 docstring(纯文本,行为不变)", gate,
         "Article review state and the explicitly enabled publication hard gate.",
         "Article review state return [] and the explicitly enabled publication hard gate."),
        ("插进 `#` 行注释(行为不变)", gate,
         "#: 一篇文章可能同时命中多类。",
         "#: 一篇文章 return [] 可能同时命中多类。"),
    ]
    must_pass = [
        ("改字典里的取值串(**真行为**:提示类别被改名)", gate,
         'NOTICE_LEGAL: Final = "legal_hard"',
         'NOTICE_LEGAL: Final = "legal_soft"'),
    ]

    for name, source, old, new in must_break:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次(需恰好 1)")
            ok = False
            continue
        injected_at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / GATE, source.replace(old, new, 1), injected_at)
        print(f"  [{'BROKEN(正确)' if broken else '🔴 漏过(冒烟门是摆设)'}] {name}"
              + (f" · {broken}" if broken else ""))
        ok = ok and bool(broken)

    for name, source, old, new in must_pass:
        if source.count(old) != 1:
            print(f"  [自证失败] {name} —— 锚点命中 {source.count(old)} 次(需恰好 1)")
            ok = False
            continue
        injected_at = len(source[:source.index(old)].encode("utf-8"))
        broken = smoke(ROOT / GATE, source.replace(old, new, 1), injected_at)
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
            print(f"\n{'='*96}\n[{mut.mid}] {mut.task}\n"
                  f"  BROKEN 锚点命中 {hits} 次(需恰好 1)→ 计为**存活**")
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
    last = out.strip().splitlines()[-1] if out.strip() else ""
    print(f"\n还原后复跑: rc={rc} 失败+错误={n_bad} — {last}")
    return 0 if not survived and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
