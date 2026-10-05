"""变异注入验证 · 工单 `WORKORDER_TITLE_QUESTION_AND_LENGTH_2026-07-29` §3。

工单三条硬要求，本脚本逐条落实：

1. **变异注入后必须 import 冒烟通过** —— 每个变异先跑一次 `import` 冒烟；
   注入把语法搞坏（历史踩坑：尾逗号被卷进注释导致 SyntaxError）时脚本判 `BROKEN`
   并**不计入 KILLED**，因为那证明的是"我把文件写坏了"，不是"锁咬住了"。
2. **FAILED 与 ERROR 两类都统计** —— 只 `grep -c FAILED` 会把 collection error
   当成 0 失败，从而把"锁没咬住"读成"锁咬住了"。这里直接读 pytest 的退出码 +
   解析 summary 的 failed/errored 两个数。
3. **变异只拆一层不算** —— 每个变异都拆到该锁**真正依赖**的那一层
   （见每条 `layer` 字段），不是改个常量了事。

还原用 `cp` 备份，**不用 `git checkout`**（工作区可能有未提交改动）。

用法::

    python scripts/mutation_title_length_2026_07_29.py
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_title_question_and_length_2026_07_29.py"


@dataclasses.dataclass(frozen=True)
class Mutation:
    mid: str
    task: str
    layer: str          # 拆的是哪一层
    path: str
    old: str
    new: str
    expect_red: str     # 预期转红的锁


MUTATIONS: tuple[Mutation, ...] = (
    # ---------------- T1 ----------------
    Mutation(
        "M1", "T1", "收敛层本体：把提升成问句的改写整段摘掉",
        "writing/title_question_policy.py",
        "            if after != before and is_question_title(after):\n"
        "                topic[title_key] = after",
        "            if False:\n"
        "                topic[title_key] = after",
        "锁 1 / 锁 2",
    ),
    Mutation(
        "M2", "T1", "配额层：去掉「在场家族保底≥1」（榜单族会被摊成 0）",
        "writing/title_question_policy.py",
        "    for code in zero_families:\n"
        "        donor = max(",
        "    for code in []:\n"
        "        donor = max(",
        "锁 2b",
    ),
    Mutation(
        "M3", "T1", "比例层：把默认比例改成 0（等价于「关掉问句化」）",
        "writing/title_question_policy.py",
        "DEFAULT_QUESTION_RATIO_PERCENT: Final = 70",
        "DEFAULT_QUESTION_RATIO_PERCENT: Final = 0",
        "锁 1 / 可配置锁",
    ),
    Mutation(
        "M4", "T1", "接线层：主链不再调用收敛入口",
        "writing/keyword_topic_generator.py",
        "        return self._apply_title_question_policy(all_topics)",
        "        return all_topics",
        "锁 1 / 锁 2",
    ),
    Mutation(
        "M5", "T1", "用户优先层：忽略 user_specified，照样抽签改写",
        "writing/title_question_policy.py",
        "    if user_specified:\n"
        "        report[\"question_after\"] = report[\"question_before\"]",
        "    if False:\n"
        "        report[\"question_after\"] = report[\"question_before\"]",
        "锁 3（用户 > 默认）",
    ),
    Mutation(
        "M6", "T1", "SSOT 层：问句判定改用自己的第二份正则（口径漂移）",
        "writing/title_question_policy.py",
        "    return has_question_form(title)",
        "    return str(title or '').endswith('?')",
        "同源锁 / 锁 1",
    ),
    Mutation(
        "M7", "T1", "诊断链接线：TopicDispatcher 不再收敛",
        "writing/topic_dispatcher.py",
        "        enforce_question_ratio(topics, title_key=\"title\", keyword_key=\"keyword\")",
        "        pass",
        "诊断链锁",
    ),
    # ---------------- T2 ----------------
    Mutation(
        "M8", "T2", "判定层：删掉紧凑档产出下限判定",
        "writing/article_length_contract.py",
        "        if tier == \"compact\" and chars < max(COMPACT_RESOLUTION_MIN_CHARS, planned_minimum):",
        "        if False:",
        "T2 锁 1",
    ),
    Mutation(
        "M9", "T2", "判定层：删掉深档产出下限判定",
        "writing/article_length_contract.py",
        "        elif tier == \"deep\" and chars < deep_output_floor(planned_target):",
        "        elif False:",
        "T2 锁 2",
    ),
    Mutation(
        "M10", "T2", "算术层：深档下限系数 0.85 → 0（等于不设线）",
        "writing/article_length_contract.py",
        "DEEP_OUTPUT_COVERAGE_FLOOR: Final = 0.85",
        "DEEP_OUTPUT_COVERAGE_FLOOR: Final = 0.0",
        "T2 锁 2",
    ),
    Mutation(
        "M11", "T2", "留痕层：spec_met 恒 True（判了但不留痕）",
        "writing/article_length_contract.py",
        "        \"spec_met\": not spec_failure_codes,",
        "        \"spec_met\": True,",
        "T2 锁 1/2/3",
    ),
    Mutation(
        "M12", "T2", "中段判定层：低谷区判定恒 False（8000 字静默通过）",
        "writing/article_length_contract.py",
        "    return lo < int(char_count or 0) < hi",
        "    return False",
        "T2 锁 3",
    ),
    Mutation(
        "M13", "T2", "规格层：紧凑档结构规格整块不发（退回只有字数指令）",
        "writing/templates/canonical_family_templates.py",
        "    budget = build_compact_structure_budget(target)\n"
        "    return f\"\"\"",
        "    budget = build_compact_structure_budget(target)\n"
        "    return \"\"\n"
        "    return f\"\"\"",
        "§2.2 紧凑档规格锁",
    ),
    Mutation(
        "M14", "T2", "算术锁层：预算加总下限校验拆掉",
        "writing/templates/canonical_family_templates.py",
        "    if total < floor:\n"
        "        raise ValueError(",
        "    if False:\n"
        "        raise ValueError(",
        "T2 锁 2b",
    ),
    Mutation(
        "M15", "T2", "档位边界层：DEEP_TIER_MIN_CHARS 挪到 4500（紧凑/深档边界错位）",
        "writing/article_length_contract.py",
        "DEEP_TIER_MIN_CHARS: Final = AVOIDANCE_BAND[1]",
        "DEEP_TIER_MIN_CHARS: Final = 4500",
        "T2 互斥锁",
    ),
    # ---------------- T3 ----------------
    Mutation(
        "M16", "T3", "规格层：预算表不再给可数的小节数（退回只给字数）",
        "writing/templates/canonical_family_templates.py",
        "    lines = [\"| 区块 | 字数预算 | 小节数 | 要求 |\", \"|---|---:|---:|---|\"]",
        "    lines = [\"| 区块 | 字数预算 | 要求 |\", \"|---|---:|---|\"]",
        "T3 节数锁",
    ),
    Mutation(
        "M17", "T3", "密度层：每小节字数常数改成 5000（节数被压成个位数）",
        "writing/templates/canonical_family_templates.py",
        "MEASURED_CHARS_PER_SECTION: Final = 450",
        "MEASURED_CHARS_PER_SECTION: Final = 5000",
        "T3 节数锁",
    ),
    Mutation(
        "M18", "T3", "矛盾指令层：增写框架退回带「不得新增事实」的禁令",
        "writing/article_generator_service.py",
        "    if str(repair_mode or \"precision\") == \"expand\":",
        "    if False:",
        "T3 矛盾指令锁",
    ),
    Mutation(
        "M19", "T3", "接线层：组装 user_message 时不再按模式分流",
        "writing/article_generator_service.py",
        "                build_repair_preamble(topic.get('_repair_mode'))",
        "                build_repair_preamble('precision')",
        "T3 接线锁",
    ),
    Mutation(
        "M20", "T3", "输出上限层：深档 max_tokens 退回 16000（档位区分失效）",
        "writing/article_generator_service.py",
        "    return 32000 if target >= _DEEP_TIER_MIN_CHARS else 16000",
        "    return 16000",
        "T3 输出上限锁",
    ),
    # ---------------- T4 ----------------
    Mutation(
        "M21", "T4", "零图零占位层：恢复 D11 ④ 的保底占位",
        "writing/article_generator_service.py",
        "_NO_ASSET_IMAGE_PLACEHOLDERS: tuple = ()",
        "_NO_ASSET_IMAGE_PLACEHOLDERS: tuple = ("
        "\"[NEED_IMAGE role=brand_intro purpose=x status=awaiting_client_asset]\",)",
        "T4 零图零占位锁",
    ),
    Mutation(
        "M22", "T4", "反向要图层：文案退回「请到素材中心上传」",
        "writing/article_generator_service.py",
        "    \"该品牌当前没有已确认可外发的图片素材，本篇按零图交付（正文不留任何配图占位）。\"",
        "    \"该品牌暂无已授权图片，请到素材中心为该品牌上传图片并确认授权后自动填入。\"",
        "T4 禁反向要图锁",
    ),
    Mutation(
        "M23", "T4", "同批台账层：选中后不写回台账（跨文章去重失效）",
        "services/article_image_selector.py",
        "        if batch_used is not None:\n"
        "            batch_used.add(asset[\"id\"])",
        "        if False:\n"
        "            batch_used.add(asset[\"id\"])",
        "T4 同批不重复锁",
    ),
    Mutation(
        "M24", "T4", "台账过滤层：不再排除同批已用图",
        "services/article_image_selector.py",
        "        remaining = [a for a in assets if a.get(\"id\") not in batch_used]",
        "        remaining = list(assets)",
        "T4 同批不重复锁",
    ),
    Mutation(
        "M25", "T4", "真实渲染数层：软删素材也算渲染成功（回到虚报）",
        "services/image_placeholder.py",
        "        elif a.get(\"status\") != \"active\":\n"
        "            reasons[\"asset_removed\"] = reasons.get(\"asset_removed\", 0) + 1",
        "        elif False:\n"
        "            reasons[\"asset_removed\"] = reasons.get(\"asset_removed\", 0) + 1",
        "T4 渲染数真因锁",
    ),
    Mutation(
        "M26", "T4", "口径一致层：渲染数直接返回标记数（不看素材状态）",
        "services/image_placeholder.py",
        "    result[\"rendered_count\"] = rendered",
        "    result[\"rendered_count\"] = total",
        "T4 渲染数真因锁",
    ),
)


def _run_pytest() -> tuple[int, int, int, str]:
    """跑判别锁。返回 (exit_code, failed, errored, tail)。

    **failed 与 errored 两个数分别解析** —— 工单 §3 明确要求。
    """
    env = dict(os.environ)
    env.setdefault(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:55990/test_geo_agentscope",
    )
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = errored = 0
    for count, word in re.findall(r"(\d+) (failed|error|errors)", out):
        if word == "failed":
            failed += int(count)
        else:
            errored += int(count)
    return proc.returncode, failed, errored, out.strip().splitlines()[-1] if out.strip() else ""


def _import_smoke() -> tuple[bool, str]:
    """变异后的 import 冒烟。语法被写坏时这里就会暴露，而不是被当成"锁咬住"。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c",
         "import writing.title_question_policy, writing.title_formula_library, "
         "writing.article_length_contract, writing.keyword_topic_generator, "
         "writing.topic_dispatcher, writing.article_generator_service, "
         "writing.templates.canonical_family_templates, "
         "services.article_image_selector, services.image_placeholder"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stderr or "").strip().splitlines()[-1] if proc.returncode else ""


def main() -> int:
    print("=" * 78)
    print("变异注入验证 · 工单 T1-T4 · 基线锁必须先全绿")
    code, failed, errored, tail = _run_pytest()
    print(f"[baseline] exit={code} failed={failed} errored={errored} | {tail}")
    if code != 0:
        print("🔴 基线判别锁未全绿，变异结果无意义，中止。")
        return 2

    results: list[tuple[Mutation, str, str]] = []
    for mut in MUTATIONS:
        target = ROOT / mut.path
        backup = target.with_suffix(target.suffix + f".mutbak_{mut.mid}")
        shutil.copy2(target, backup)          # cp 备份 —— 不用 git checkout
        try:
            src = target.read_text(encoding="utf-8")
            if mut.old not in src:
                results.append((mut, "NO_ANCHOR", "变异锚点未命中（代码已漂移，变异无效）"))
                continue
            target.write_text(src.replace(mut.old, mut.new, 1), encoding="utf-8")

            ok, err = _import_smoke()
            if not ok:
                results.append((mut, "BROKEN", f"import 冒烟失败：{err[:120]}"))
                continue

            code, failed, errored, tail = _run_pytest()
            status = "KILLED" if code != 0 else "SURVIVED"
            detail = f"exit={code} failed={failed} errored={errored} | {tail[:90]}"
            results.append((mut, status, detail))
        finally:
            shutil.copy2(backup, target)
            backup.unlink(missing_ok=True)

    print("\n" + "=" * 78)
    print(f"{'ID':<5} {'任务':<4} {'结果':<9} 拆的层 / 明细")
    print("-" * 78)
    killed = survived = broken = no_anchor = 0
    for mut, status, detail in results:
        icon = {"KILLED": "🟢", "SURVIVED": "🔴", "BROKEN": "⚠️", "NO_ANCHOR": "⚠️"}[status]
        print(f"{mut.mid:<5} {mut.task:<4} {icon} {status:<8} {mut.layer}")
        print(f"{'':<5} {'':<4} {'':<10} → {detail}")
        killed += status == "KILLED"
        survived += status == "SURVIVED"
        broken += status == "BROKEN"
        no_anchor += status == "NO_ANCHOR"
    print("-" * 78)
    print(f"总计 {len(results)}：KILLED {killed} · SURVIVED {survived} · "
          f"BROKEN {broken} · NO_ANCHOR {no_anchor}")
    print("注：BROKEN/NO_ANCHOR **不计入** KILLED —— 它们证明的是注入本身有问题，")
    print("    不是锁咬住了（工单 §3：import 冒烟 + FAILED/ERROR 两类都统计）。")
    return 0 if survived == 0 and broken == 0 and no_anchor == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
