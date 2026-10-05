"""[WO_BRAND_QUESTION_LEAK 2026-08-05] 变异自检。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. **至少一个预期用例转红**。零转红 = 这条锁在这个 fixture 下抓不到本 bug。

🔴 文件读写一律走 bytes:``Path.read_text`` / ``write_text`` 在 Windows 上会翻换行
   (读 CRLF→LF、写 LF→CRLF),"读原文再写回"这个本该恒等的还原操作会把整个文件
   从 LF 翻成 CRLF —— 这坑 2026-08-04 与 08-05 各栽过一次。

🔴 锚点一律取**可执行代码**,不取注释:本单的注释里大量出现
   「品牌定向题」「竞品榜」这些字样,拿注释当锚点 = 改注释也能"杀死"变异,
   那种绿是假的(2026-08-05 媒体榜/iOS 两单都踩过"锁抓到自己写的注释")。

跑法: PYTHONUTF8=1 python tests/mutation_runner_brand_question_leak.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SSOT = "services/brand_directed_question.py"
WRITER = "services/report_writer_v2.py"
PRESENT = "services/public_report_presentation.py"
REVIEW = "services/diagnosis_identity_review.py"
KWGEN = "tools/keyword_generator.py"

LOCKS = "tests/test_brand_question_leak_2026_08_05.py"

# (编号, 说明, 文件, 原文, 替换, 预期转红的用例名片段)
MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ── 工单 §4 点名的两条 ────────────────────────────────────────────────
    ("M1", "🔴 §4 点名:文本兜底被去掉 → 标签错标的公司题重新漏网",
     SSOT,
     "    by_text = is_brand_directed_text(question, brand_name, aliases=aliases)\n",
     "    by_text = False\n",
     ["test_competition_module_excludes_the_mislabeled_company_question",
      "test_presentation_excludes_mislabeled_company_question_from_mention_rate",
      "test_funnel_stops_counting_the_company_question_as_scenario_layer",
      "test_relabeled_flag_only_fires_when_label_and_text_disagree"]),

    ("M2", "🔴 §4 点名:竞品剔除被去掉 → 客户回到自己的竞品榜",
     SSOT,
     "        matched, _ = _exact_or_strict_match(target, [name])\n        if matched:\n            return True\n",
     "        matched, _ = _exact_or_strict_match(target, [name])\n        if False:\n            return True\n",
     ["test_client_never_appears_in_its_own_competitor_board",
      "test_legal_suffix_variant_is_covered",
      "test_presentation_drops_client_from_stored_competitor_snapshot"]),

    # ── SSOT 判据本身 ────────────────────────────────────────────────────
    ("M3", "token 最短长度放到 1 → 「深圳」「电梯」变成判据,同行被误剔",
     SSOT,
     "MIN_TOKEN_LENGTH = 3\n",
     "MIN_TOKEN_LENGTH = 1\n",
     ["test_short_tokens_are_dropped_by_length"]),

    ("M4", "行业通用词不再排除 → 「电梯」成 token",
     SSOT,
     "            if token in _GENERIC_TOKENS:\n                continue\n",
     "            if False:\n                continue\n",
     ["test_generic_industry_terms_are_dropped_even_when_long_enough"]),

    ("M5", "去法人后缀这一层没了 → 登记名与回答写法差一截就漏剔(551 竞品榜真因)",
     SSOT,
     "    forms = [base, no_bracket, _strip_legal_suffix(base), _strip_legal_suffix(no_bracket)]\n",
     "    forms = [base, no_bracket]\n",
     ["test_text_rule_covers_registered_name_vs_short_form_in_question"]),

    ("M6", "verbatim 豁免被拿掉 → 自定义题也被平台替用户判品牌题",
     SSOT,
     "    if verbatim:\n        return BrandDirectedVerdict(False, False, False)\n",
     "    if False:\n        return BrandDirectedVerdict(False, False, False)\n",
     ["test_verbatim_mode_exemption_is_preserved",
      "test_competition_module_verbatim_mode_still_exempt"]),

    ("M7", "判据放宽成「题面含任意 2 字片段即算」→ 真竞争题被吞掉",
     SSOT,
     "        if token in normalized_question:\n            return token\n",
     "        if token[:2] in normalized_question:\n            return token\n",
     ["test_text_rule_does_not_swallow_real_competitive_questions"]),

    # ── 漏斗层(§0 第 2 处污染) ───────────────────────────────────────────
    ("M8", "漏斗聚合不再按题面纠正归层 → 场景转化层退回虚高的 4/8",
     REVIEW,
     '            q_type = "brand_awareness"  # 文本为准:题面含品牌名 = 品牌认知层\n',
     "            pass\n",
     ["test_funnel_stops_counting_the_company_question_as_scenario_layer",
      "test_funnel_score_drops_by_the_inflated_scenario_points"]),

    # ── 竞争模块写入侧(§2.2 / §2.3) ──────────────────────────────────────
    ("M9", "竞争分母退回只信标签 → 错标公司题重新进分母",
     WRITER,
     "            layer_key=question_types.get(question),\n            brand_name=brand_name,\n            aliases=_aliases,\n            verbatim=is_verbatim_mode,\n",
     '            layer_key=question_types.get(question),\n            brand_name="",\n            aliases=(),\n            verbatim=is_verbatim_mode,\n',
     ["test_competition_module_excludes_the_mislabeled_company_question",
      "test_competition_module_records_the_upstream_mislabel"]),

    ("M10", "竞品榜剔除退回「归一后精确相等」(改前那条判据)",
     WRITER,
     "                if _is_client_self(name):\n",
     "                if client_norm and norm == client_norm:\n",
     ["test_client_never_appears_in_its_own_competitor_board"]),

    ("M11", "错标留痕计数恒 0 → 出题层错标率观测口失效",
     WRITER,
     '        "brand_directed_relabeled_by_text": len(brand_directed_relabeled_questions),\n',
     '        "brand_directed_relabeled_by_text": 0,\n',
     ["test_competition_module_records_the_upstream_mislabel"]),

    ("M12", "客户自身剔除做成「全剔」→ 同行一起被剔光(反向对照必须抓到)",
     WRITER,
     "                if _is_client_self(name):\n",
     "                if True:\n",
     ["test_real_rivals_survive_the_competitor_exclusion"]),

    ("M13", "3_raw 产物的 layer_key 退回原样透传 → 展示层拿到的还是错标签",
     WRITER,
     '            "layer_key": (None if is_verbatim_mode else _effective_layer_key(question)),\n',
     '            "layer_key": (None if is_verbatim_mode else (question_types.get(question) or "super_tier1")),\n',
     ["test_raw_appendix_emits_corrected_layer_key_into_the_artifact"]),

    # ── 展示层(§3 存量报告自愈) ─────────────────────────────────────────
    ("M14", "展示层不再把品牌名传进逐题判定 → 无 layer_key 的老产物全线失守",
     PRESENT,
     "    rows = list(_iter_results(raw_module, brand_name))\n",
     "    rows = list(_iter_results(raw_module))\n",
     ["test_presentation_excludes_mislabeled_company_question_from_mention_rate",
      "test_presentation_text_rule_also_covers_artifacts_without_layer_key"]),

    ("M15", "展示层不再剔落库快照里的客户自己 → 存量报告 551 刷新后客户还在榜上",
     PRESENT,
     "        if name and brand_name and is_client_own_brand(name, brand_name):\n            continue\n",
     "        if False:\n            continue\n",
     ["test_presentation_drops_client_from_stored_competitor_snapshot"]),

    ("M16", "自定义题豁免被去掉 → verbatim 产物也被平台替用户判",
     PRESENT,
     '    if layer_key is None and layer_label and layer_label.strip() == "自定义":\n        return False\n',
     "    if False:\n        return False\n",
     ["test_presentation_custom_layer_label_stays_exempt"]),

    # ── 出题层(§2.1 · 真凶那处) ─────────────────────────────────────────
    ("M17", "🔴 真凶复原:等槽兜底池不再剔品牌题 → 公司题又被发给非品牌槽位",
     KWGEN,
     "        if q not in questions and not is_brand_directed_text(q, brand_name)\n    ]\n\n    out: list = []\n    replaced = 0\n",
     "        if q not in questions\n    ]\n\n    out: list = []\n    replaced = 0\n",
     ["test_substitution_pool_no_longer_hands_out_the_company_question"]),

    ("M18", "去重只留一道的逻辑被摘掉 → 8 问里又出现两道公司题",
     KWGEN,
     "    surplus = [q for q in directed if q is not keeper and q != keeper]\n",
     "    surplus = []\n",
     ["test_dedupe_collapses_company_questions_to_exactly_one"]),

    ("M19", "保留的那道不再强制改标品牌层 → 单道公司题仍冒充场景层",
     KWGEN,
     "    keeper = next((q for q in directed if qtypes.get(q) == LAYER_BRAND), directed[0])\n    qtypes[keeper] = LAYER_BRAND\n",
     "    keeper = next((q for q in directed if qtypes.get(q) == LAYER_BRAND), directed[0])\n",
     ["test_dedupe_relabels_a_lone_mislabeled_company_question"]),

    ("M20", "去重时静默删题(不做等槽替换)→ 题量缩水,违反 SSOT §9.6",
     KWGEN,
     # 锚点唯一化只能靠**可执行代码**:`if substitute is None:` 在本文件另有一处
     # (_enforce_commercial_questions),带上下一条赋值语句才切得准。
     "            qtypes[question] = LAYER_BRAND\n            out.append(question)\n",
     "            continue\n            out.append(question)\n",
     ["test_dedupe_never_shrinks_the_question_set_when_the_pool_is_exhausted"]),

    ("M21", "去重没接进出题主链(实现了但没人调)",
     KWGEN,
     "                parsed = _dedupe_brand_directed_questions(\n",
     "                parsed = _noop_dedupe_placeholder(\n",
     ["test_dedupe_is_wired_into_the_generation_pipeline"]),

    # ── 出题入口的唯一收口(三个降级出口曾经全部绕过) ──────────────────────
    ("M22", "🔴 降级出口(industry 为空)绕过收口 → 品类词=品牌名那 7 道题重新挂竞争层",
     KWGEN,
     "        return _finalize_business_context(\n            _fallback_business_context(\n                brand_name, brand_name, keywords,\n",
     "        return (\n            _fallback_business_context(\n                brand_name, brand_name, keywords,\n",
     ["test_every_generation_exit_goes_through_the_choke_point",
      "test_degraded_exit_never_fakes_competitive_visibility"]),

    ("M23", "🔴 收口写成直通(每个出口都调它,但它什么都不做)",
     KWGEN,
     "    return _dedupe_brand_directed_questions(\n        parsed, brand_name, industry, keywords,\n        client_location=client_location, business_scope=business_scope,\n        brand_cities=brand_cities,\n    )\n\n\ndef _dedupe_brand_directed_questions(",
     "    return parsed\n\n\ndef _dedupe_brand_directed_questions(",
     ["test_choke_point_actually_applies_the_guard",
      "test_degraded_exit_never_fakes_competitive_visibility"]),

    ("M24", "🔴 LLM 成功那条出口绕过收口(层配额守卫补的公司题就没人归层了)",
     KWGEN,
     "                return _finalize_business_context(\n                    parsed, brand_name, industry, keywords,\n",
     "                return dict(\n                    parsed, **dict(\n",
     ["test_every_generation_exit_goes_through_the_choke_point"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCKS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def red_markers(output: str) -> set[str]:
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
        if line.startswith("ERROR ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN\n" + "=" * 74)

    killed, survived = 0, []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ❌ 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            survived.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ❌ 落盘后文件没变(no-op): {desc}")
            survived.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)

        reds = red_markers(m_out)
        hit = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零转红): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit:
            print(f"[{mid}] ⚠️ 转红但不是预期那些: {desc}\n"
                  f"        预期 {expect}\n        实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 (含预期 {hit}): {desc}")

    print("=" * 74)
    print(f"变异结果: {killed}/{len(MUTATIONS)} 被杀")
    if survived:
        print("存活/无效变异:")
        for mid, desc, why in survived:
            print(f"  - {mid} [{why}] {desc}")
        return 1
    rc2, _ = run_locks()
    print("还原后复跑基线:", "GREEN" if rc2 == 0 else "RED(文件未正确还原!)")
    return 0 if rc2 == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
