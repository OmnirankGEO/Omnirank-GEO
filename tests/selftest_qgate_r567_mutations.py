# -*- coding: utf-8 -*-
"""变异自检:证明 REWORK-DIAG-QGATE **R5/R6/R7** 的判别锁真的有判别力。

跑法(在仓库根)::

    PYTHONPATH=. python tests/selftest_qgate_r567_mutations.py

机制沿用 R1-R4 的同名脚本(``selftest_qgate_rework_mutations.py``),逐条复用它的
``read_raw`` / ``write_raw`` / ``strip_comments_and_docstrings`` —— 那三个函数是
2026-08-03 行尾返修的**根因修复本体**(``open(newline="")`` 两端都不翻译),
在这里重写一遍等于把那个坑重新挖开。

每条变异做三件事(缺一不算过):
  1. **锚点存在性** —— old 串必须在源码里恰好命中 1 次。找不到/多处 = 锚点漂移;
  2. **非 no-op 自检** —— 变异后源码必须真的变了;
  3. **锁必须转红** —— 指定的测试在变异后必须 FAILED。仍绿 = 锁没打到点上。

外加两条全局检查:
  · **行尾守卫**(字节计数,不用 grep —— 那个判据对纯 LF 文件假阳性);
  · **死函数扫描**:新增公开函数必须有 ≥1 个非测试真实调用点。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.selftest_qgate_rework_mutations import (  # noqa: E402
    read_raw,
    strip_comments_and_docstrings,
    write_raw,
)

GATE = ROOT / "services" / "diagnosis_question_quality.py"
SCOPE = ROOT / "services" / "diagnosis_business_scope.py"
KWGEN = ROOT / "tools" / "keyword_generator.py"
WORKFLOW = ROOT / "workflows" / "diagnosis_workflow.py"
LOCKS = "tests/test_diagnosis_question_gate_r567_2026_08_04.py"

# (编号, 说明, [(文件, old, new), ...], 必须转红的测试)
MUTATIONS = [
    # ── R5 ────────────────────────────────────────────────────────────────
    (
        "M1-R5", "token 池不再并入 brands.cities(退回只吃表单值 = 529 病灶)",
        [(GATE,
          "    tokens = list(geo_tokens(city))\n"
          "    for token in geo_tokens(brand_cities):\n"
          "        if token not in tokens:\n"
          "            tokens.append(token)\n"
          "    return tuple(tokens)",
          "    return geo_tokens(city)",
          )],
        ["test_r5_merged_token_pool_contains_archive_place_names",
         "test_r5_question_with_archive_place_name_counts_as_geo_qualified",
         "test_r5_529_scenario_end_to_end_no_double_place_name"],
    ),
    (
        "M2-R5", "主地名不再回落档案市级(表单省级时原样用省名)",
        [(GATE,
          "    archive_city = city_level_name(brand_cities)\n"
          "    if archive_city and archive_city != form_town:",
          "    archive_city = \"\"\n    if archive_city and archive_city != form_town:",
          )],
        ["test_r5_primary_geo_falls_back_to_archive_city_when_form_is_province_level",
         "test_r5_529_scenario_end_to_end_no_double_place_name"],
    ),
    (
        # 🔴 这条是"修 A 坏 B"方向的变异:让档案**无条件**覆盖表单。
        #    返工单字面就是这么写的("主地名优先档案解析出的市级"),所以必须有一条
        #    锁挡住它 —— 否则我这处有意偏离等于没写。
        "M3-R5", "档案无条件优先(表单已到市级也被覆盖 · 返工单字面口径)",
        [(GATE,
          "    if not _is_province_level(city, form_town):\n        return form_town",
          "    if False:\n        return form_town",
          )],
        ["test_r5_form_city_level_value_wins_over_archive"],
    ),
    (
        # 🔴 不能写成 `if False:` —— city_level_name 对"贵阳"本来就返回空串,
        #    单独禁用它是 **no-op**。要证明这条防线,变异必须**真的**把省名也当市级。
        "M4-R5", "省名被当成市级(_is_province_level 恒假 → 529 修不好)",
        [(GATE,
          "    return normalized in provinces or f\"{normalized}省\" in provinces",
          "    return False",
          )],
        ["test_r5_primary_geo_falls_back_to_archive_city_when_form_is_province_level"],
    ),
    (
        "M5-R5", "表单没地名时也用档案开闸(给从不出地域题的客户静默开闸)",
        [(GATE,
          "    form_town = normalize_city(city)\n    if not form_town:\n        return \"\"",
          "    form_town = normalize_city(city)\n    if not form_town:\n"
          "        return city_level_name(brand_cities)",
          )],
        ["test_r5_no_form_place_name_keeps_gate_closed"],
    ),
    (
        "M6-R5", "enforce 判地域时不传 brand_cities(纯函数修了但调用方没接上)",
        [(GATE,
          "and not has_geo_qualifier(text, city, brand_cities):",
          "and not has_geo_qualifier(text, city):",
          )],
        ["test_r5_529_scenario_end_to_end_no_double_place_name"],
    ),
    (
        "M7-R5", "模板池不接 brand_cities(前缀用广东、判地域用深圳,两套地名)",
        [(GATE,
          "        keywords=keywords, brand_cities=brand_cities,\n    )",
          "        keywords=keywords,\n    )",
          )],
        ["test_r5_529_scenario_end_to_end_no_double_place_name"],
    ),
    (
        "M8-R5", "workflow 取了档案却不往下传(R1-R4 踩过的同型接线断点)",
        [(WORKFLOW,
          "                brand_cities=brand_cities,\n",
          "",
          )],
        ["test_wiring_workflow_passes_brand_cities_to_business_analysis"],
    ),
    (
        "M9-R5", "workflow 压根不取档案",
        [(WORKFLOW,
          "    brand_cities = resolve_brand_cities(brand_id)",
          "    brand_cities = \"\"",
          )],
        ["test_wiring_workflow_resolves_brand_cities_from_archive"],
    ),
    (
        "M10-R5", "落库快照不再记两个 city 输入(529 复盘就是卡在这)",
        [(GATE,
          "        \"city_inputs\": {\n            \"form\": str(city or \"\"),",
          "        \"city_inputs\": {\n            \"form\": \"\",",
          )],
        ["test_r5_snapshot_records_both_city_inputs"],
    ),
    # ── R6 ────────────────────────────────────────────────────────────────
    (
        "M11-R6", "分类学串判废被拆掉(登记整串直接进题面)",
        [(GATE,
          "    cand = colloquialize_trade(head)\n    if looks_like_taxonomy_term(cand):\n        return \"\"",
          "    cand = colloquialize_trade(head)\n    if False:\n        return \"\"",
          )],
        ["test_r6_pure_l1_industry_yields_no_trade"],
    ),
    (
        # 🔴 第一版只改了「名录命中」那一支,结果**仍绿** —— 因为
        #    ``looks_like_taxonomy_term`` 还有一条独立的 "/" 分支在挡。
        #    (同型第三次:变异只拆了防线的一半,剩下那半照样兜住。)
        #    真变异 = L2 取段失效 + ``looks_like_taxonomy_term`` **整个**失效。
        "M12-R6", "L2 取段失效 + 分类学判废整个失效(两处同时改,才真能吐出整串)",
        [(GATE,
          "    tail = re.split(r\"[/／]\", raw)[-1].strip()\n"
          "    tail = re.split(r\"[-—－]\", tail)[-1].strip()",
          "    tail = raw",
          ),
         (GATE,
          "    raw = str(text or \"\").strip()\n"
          "    if not raw:\n        return True\n"
          "    if re.search(r\"[/／]\", raw):\n        return True\n"
          "    if raw in _INDUSTRY_L1_NAMES or raw in _INDUSTRY_TAXONOMY_EXTRA:\n        return True\n"
          "    return False",
          "    return False",
          )],
        ["test_r6_taxonomy_string_never_reaches_a_question"],
    ),
    (
        "M13-R6", "口语化被跳过(医疗美容服务 原样进题面)",
        [(GATE,
          "    mapped = _TRADE_COLLOQUIAL.get(raw)\n    if mapped:\n        return mapped",
          "    if False:\n        return \"\"",
          )],
        ["test_r6_l2_segment_is_colloquialized"],
    ),
    (
        "M14-R6", "品牌经营词回落被拆掉",
        [(GATE,
          "    from_brand = trade_from_brand_name(brand_name, cores)\n    if from_brand:",
          "    from_brand = \"\"\n    if from_brand:",
          )],
        ["test_r6_falls_back_to_brand_operating_word"],
    ),
    (
        "M14b-R6", "品牌名本身是分类名录用语时也去扫词表(生产 4 个品牌 name='制造业')",
        [(GATE,
          "    if looks_like_taxonomy_term(name):\n",
          "    if False:\n",
          )],
        ["test_r6_brand_name_that_is_itself_a_taxonomy_term_yields_nothing"],
    ),
    (
        "M15-R6", "炼不出品类词时退回旧的 '相关服务' 占位(而不是宁缺毋滥)",
        [(GATE,
          "        str(industry or \"\")[:60], len(list(keywords or [])), str(brand_name or \"\")[:30],\n    )\n    return []",
          "        str(industry or \"\")[:60], len(list(keywords or [])), str(brand_name or \"\")[:30],\n    )\n    return [\"相关服务\"]",
          )],
        ["test_r6_unusable_trade_empties_template_pool_rather_than_emitting_nonsense"],
    ),
    (
        "M16-R6", "空品类词时模板池不置空(退回会拼鬼话的老路)",
        [(GATE,
          "        if not trades:\n            return []\n        out: list[str] = []",
          "        if not trades:\n            return [p.format(geo=geo, trade=\"相关服务\") for p in patterns]\n        out: list[str] = []",
          )],
        ["test_r6_unusable_trade_empties_template_pool_rather_than_emitting_nonsense"],
    ),
    (
        "M17-R6", "distill_trade 单值口径被卷进 R6(server.py 竞品调研被顺手改掉)",
        [(GATE,
          "    return distill_trades(industry, keywords, limit=1, for_question=False)[0]",
          "    return (distill_trades(industry, keywords, limit=1) or [\"相关服务\"])[0]",
          )],
        ["test_r6_distill_trade_single_value_keeps_legacy_contract"],
    ),
    (
        "M18-R6", "出题 prompt 的示例退回喂 industry 登记整串(529 那两道题的真凶)",
        [(KWGEN,
          "如\"{_city or '某市'}{_trade}哪家好\"、\n\"{_trade}怎么选\"",
          "如\"{_city or '某市'}{industry}哪家好\"、\n\"{industry}怎么选\"",
          )],
        ["test_wiring_llm_prompt_no_longer_feeds_raw_industry_as_example"],
    ),
    (
        "M19-R6", "降级出题退回拼 industry 整串",
        [(KWGEN,
          "            f\"{detected_city}{trade}哪家靠谱？\",\n            f\"{detected_city}{trade}哪家服务好？\",",
          "            f\"{detected_city}{industry}哪家靠谱？\",\n            f\"{detected_city}{industry}哪家服务好？\",",
          )],
        ["test_wiring_fallback_context_uses_distilled_trade",
         "test_fallback_context_end_to_end_has_no_taxonomy_string"],
    ),
    # ── R7 ────────────────────────────────────────────────────────────────
    (
        "M20-R7", "B2B 后缀对所有行业开(返工单点名的病灶:医美被出'适合中小企业')",
        [(GATE,
          "    elif audience == AUDIENCE_B2B:\n"
          "        # 区域客户的场景层也必须带地域，否则等于把他丢进全国竞品堆里比",
          "    elif True:\n"
          "        # 区域客户的场景层也必须带地域，否则等于把他丢进全国竞品堆里比",
          )],
        ["test_r7_consumer_industry_never_gets_smb_suffix",
         "test_r7_529_added_question_is_not_b2b"],
    ),
    (
        # 🔴 反方向变异:把 B2B 后缀整个删掉,上面那条锁照样绿(它只断言"不出现")。
        #    这条证明 test_r7_b2b_industry_keeps_smb_suffix 那格反向对照有判别力。
        "M21-R7", "B2B 后缀被整个删掉(证明'不出现'那格不是靠删干净蒙过去的)",
        [(GATE,
          "            \"{geo}{trade}哪家方案更适合中小企业？\",",
          "            \"{geo}{trade}哪家做得快？\",",
          )],
        ["test_r7_b2b_industry_keeps_smb_suffix"],
    ),
    (
        "M22-R7", "受众判定默认改成 B2B(默认关变默认开)",
        [(GATE,
          "    if b2b_hits > consumer_hits:\n        return AUDIENCE_B2B\n    return AUDIENCE_CONSUMER",
          "    if consumer_hits > b2b_hits:\n        return AUDIENCE_CONSUMER\n    return AUDIENCE_B2B",
          )],
        ["test_r7_unknown_industry_defaults_to_consumer_suffix"],
    ),
    (
        "M23-R7", "C 端决策获客层末条退回说'公司'",
        [(GATE,
          "        \"{geo}哪些{trade}公司口碑好？\" if audience == AUDIENCE_B2B\n"
          "        else \"{geo}{trade}哪家人气高？\",",
          "        \"{geo}哪些{trade}公司口碑好？\",",
          )],
        ["test_r7_consumer_local_pool_avoids_company_noun"],
    ),
    (
        "M24-R7", "降级出题的超一级词不再分受众",
        [(KWGEN,
          "            (f\"{trade}公司排名TOP5\" if audience == AUDIENCE_B2B\n"
          "             else f\"{trade}排名前十有哪些？\"),",
          "            f\"{trade}公司排名TOP5\",",
          )],
        ["test_r7_fallback_national_consumer_avoids_b2b_nouns"],
    ),
    # ── 不回退 ────────────────────────────────────────────────────────────
    (
        "M25-745", "745 的镇级 token 又被剥后缀(R1 §7.2 回退)",
        [(GATE,
          "    if chunk.endswith(_SUBCITY_KEEP_WHOLE):\n        return chunk",
          "    if chunk.endswith(_SUBCITY_KEEP_WHOLE):\n        return chunk[:-1]",
          )],
        ["test_r1_town_level_token_still_not_stripped"],
    ),
    (
        "M26-爬档案", "resolve_brand_cities 不再 fail-soft(查库炸了就抛)",
        [(SCOPE,
          "    except Exception as err:  # fail-soft:查不到档案绝不阻断诊断\n"
          "        print(f\"   ⚠️ 读取 brands.cities 失败(退回只用表单区域 · 不影响诊断): {err}\")\n"
          "        return \"\"",
          "    except Exception as err:\n        raise",
          )],
        ["test_wiring_brand_cities_reader_is_fail_soft"],
    ),
]

# 新增公开函数 → 必须有的非测试真实调用点(防"零调用零测试照样过审")
NEW_PUBLIC_FUNCS = {
    "merged_geo_tokens": ["services/diagnosis_question_quality.py"],
    "resolve_primary_geo": ["services/diagnosis_question_quality.py",
                            "tools/keyword_generator.py"],
    "city_level_name": ["services/diagnosis_question_quality.py"],
    "industry_audience": ["services/diagnosis_question_quality.py",
                          "tools/keyword_generator.py"],
    "looks_like_taxonomy_term": ["services/diagnosis_question_quality.py"],
    "colloquialize_trade": ["services/diagnosis_question_quality.py"],
    "trade_from_industry": ["services/diagnosis_question_quality.py"],
    "trade_from_brand_name": ["services/diagnosis_question_quality.py"],
    "resolve_brand_cities": ["workflows/diagnosis_workflow.py"],
}

EOL_GUARDED = (
    "services/diagnosis_question_quality.py",
    "services/diagnosis_business_scope.py",
    "tools/keyword_generator.py",
    "workflows/diagnosis_workflow.py",
    "tests/test_diagnosis_question_gate_r567_2026_08_04.py",
    "tests/selftest_qgate_r567_mutations.py",
)


def run_tests(names: list[str]) -> tuple[bool, str]:
    expr = " or ".join(names)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCKS, "-q", "-k", expr, "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",  # Windows 默认 gbk 会在中文断言消息上炸
        env={**os.environ, "PYTHONPATH": ".", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode == 0, (proc.stdout or "")[-500:]


def main() -> int:
    failures: list[str] = []

    green, tail = run_tests(["test_"])
    if not green:
        print("🔴 基线就不绿,变异自检无意义:\n" + tail)
        return 1
    print("✅ 基线全绿")

    # ── 行尾守卫(字节计数 · 非 grep)────────────────────────────────────
    # 🔴 `grep -c $'\r$'` 对**纯 LF 文件**在本仓 Git Bash 下报满屏假阳性,
    #    2026-08-03 我差点拿它去反驳返修单。唯一可信口径 = 字节计数。
    print("\n── 行尾守卫(字节计数 · 非 grep)──")
    for rel in EOL_GUARDED:
        raw = (ROOT / rel).read_bytes()
        cr = raw.count(b"\r")
        if cr:
            failures.append(f"行尾: {rel} 有 {cr} 个 CR(生产树是 LF)")
            print(f"  🔴 {rel}: CR={cr}")
        else:
            print(f"  ✅ {rel}: CR=0 · LF={raw.count(b'\n')}")
    # 反向对照:同一判据必须能报出 CRLF,否则上面全是假绿
    if b"\r\n".count(b"\r") != 1:
        failures.append("行尾判据自身失效")
    else:
        print("  ✅ 反向对照:同一判据对 b'\\r\\n' 报 CR=1(判据有判别力)")

    # ── 死函数扫描 ──────────────────────────────────────────────────────
    print("\n── 死函数扫描(剥注释/docstring 后)──")
    for func, expected_files in NEW_PUBLIC_FUNCS.items():
        callers: list[str] = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(("tests/", ".venv/")) or "site-packages" in rel:
                continue
            try:
                code = strip_comments_and_docstrings(read_raw(path))
            except Exception:
                continue
            for m in re.finditer(rf"\b{re.escape(func)}\s*\(", code):
                before = code[max(0, m.start() - 12):m.start()]
                if before.rstrip().endswith("def"):
                    continue
                callers.append(rel)
                break
        callers = sorted(set(callers))
        if not callers:
            failures.append(f"死函数: {func} 零真实调用点(测试不算)")
            print(f"  🔴 {func}: 零调用点")
        else:
            print(f"  ✅ {func}: {callers}")
        for expected in expected_files:
            if expected not in callers:
                failures.append(f"{func} 期望在 {expected} 有调用点,实际 {callers}")

    # ── 逐条变异 ────────────────────────────────────────────────────────
    print("\n── 变异(每条:锚点存在 → 真改了 → 锁转红)──")
    for tag, desc, edits, must_red in MUTATIONS:
        originals = {path: read_raw(path) for path, _, _ in edits}
        drifted = False
        pending: dict[Path, str] = dict(originals)
        for path, old, new in edits:
            hits = pending[path].count(old)
            if hits != 1:
                failures.append(f"{tag} 锚点命中 {hits} 次(必须恰好 1 次)——锚点漂移了")
                print(f"  🔴 {tag} 锚点命中 {hits} 次: {desc}")
                drifted = True
                break
            pending[path] = pending[path].replace(old, new, 1)
        if drifted:
            continue
        if all(pending[p] == originals[p] for p in originals):
            failures.append(f"{tag} 是 no-op 变异(源码没变)")
            print(f"  🔴 {tag} no-op")
            continue
        for path, text in pending.items():
            write_raw(path, text)
        try:
            green, tail = run_tests(must_red)
        finally:
            for path, text in originals.items():
                write_raw(path, text)
        if green:
            failures.append(f"{tag} 变异后锁仍绿 —— 锁没打到点上,重写: {desc}")
            print(f"  🔴 {tag} 仍绿: {desc}")
        else:
            print(f"  ✅ {tag} 已转红: {desc}")

    # ── 复原后必须重新全绿 ──────────────────────────────────────────────
    green, tail = run_tests(["test_"])
    if not green:
        failures.append("复原后不再全绿 —— 自检脚本把源码改坏了")
        print("\n🔴 复原后不绿:\n" + tail)
    else:
        print("\n✅ 复原后仍全绿")

    print("\n" + "=" * 60)
    if failures:
        print(f"🔴 变异自检失败 {len(failures)} 条:")
        for f in failures:
            print("   -", f)
        return 1
    print(f"✅ 变异自检全过:{len(MUTATIONS)} 条变异全部转红 · "
          f"{len(NEW_PUBLIC_FUNCS)} 个新函数全有真实调用点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
