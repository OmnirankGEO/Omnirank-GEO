# -*- coding: utf-8 -*-
"""GEO 文章链一次性全量包 · 变异全杀 runner(2026-08-14)。

逐条把修法拆掉(文本变异)→ 跑对应判别测试,**必须转红且红在指定用例**;
复位后重跑,**必须转绿**。存活(拆了还全绿)>0 = 锁没有判别力,退回。

🔴 Windows 行尾纪律(工单红线 7):所有读写都 `newline=""`,变异-复位
往返后逐文件字节比对,不一致即 FAIL(防 runner 自己把 LF 翻成 CRLF)。

用法:python tests/mutation_runner_oneshot_2026_08_14.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = [sys.executable, "-m", "pytest", "-q", "--no-header", "-x"]

T_P01 = "tests/test_p0_1_evidence_lane_cache_2026_08_14.py"
T_P02 = "tests/test_p0_2_entity_binding_2026_08_14.py"
T_P03 = "tests/test_p0_3_backfills_2026_08_14.py"
T_P11 = "tests/test_p1_1_title_gap_block_2026_08_14.py"
T_P12 = "tests/test_p1_2_title_evidence_advisory_2026_08_14.py"
T_P13 = "tests/test_p1_3_media_form_2026_08_14.py"
T_P14 = "tests/test_p1_4_engine_targeting_2026_08_14.py"
T_P15A = "tests/test_p1_5a_flywheel_guard_2026_08_14.py"
T_P15B = "tests/test_p1_5b_reco_feedback_2026_08_14.py"
T_P2 = "tests/test_p2_asset_and_next_step_2026_08_14.py"
T_R4 = "tests/test_r4_repair_chain_entity_gate_2026_08_16.py"

# (编号, 目标文件, old, new, 测试文件, 必须转红的用例名片段)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------- P0-1
    ("P0-1/M1 拆 title 注入", "writing/evidence_research.py",
     'return " ".join(_TERM_RE.findall(text))[:24].strip()', 'return ""',
     T_P01, "test_article_terms_extracts_title_level_words"),
    ("P0-1/M2 拆 scope 隔离", "writing/evidence_research.py",
     "    bucket = _lane_cache.get(scope)\n    if bucket is None:\n        return None\n    hit",
     "    bucket = next(iter(_lane_cache.values()), None)\n    if bucket is None:\n        return None\n    hit",
     T_P01, "test_different_scope_no_reuse"),
    ("P0-1/M3 拆缓存", "writing/evidence_research.py",
     "def _lane_cache_get(scope: str, kind: str, query: str) -> list[dict[str, Any]] | None:\n    if not scope:",
     "def _lane_cache_get(scope: str, kind: str, query: str) -> list[dict[str, Any]] | None:\n    return None\n    if not scope:",
     T_P01, "test_concurrent_collect_real_entry"),
    # ---------------- P0-2
    ("P0-2/M1 拆形态闸", "writing/primary_advantage.py",
     'return bool(_MALFORMED_SHAPE.search(str(phrase or "")))', "return False",
     T_P02, "test_repr_shape_rejected"),
    ("P0-2/M3 绑定恒 confirmed", "writing/evidence_research.py",
     "    full = _norm_binding_text(entity)\n    if len(full) < 2:",
     "    return BINDING_CONFIRMED\n    full = _norm_binding_text(entity)\n    if len(full) < 2:",
     T_P02, "test_known_wrong_entity_judged_different"),
    # [R5] 统一门谓词整体拆除(所有出口共享 → 全体四态锁红,-x 先红者为准)
    ("P0-2/M4 拆实体轴统一门(共享谓词)", "writing/evidence_pack.py",
     '    binding = str((item or {}).get("binding_state") or "")\n'
     '    if binding:\n'
     '        return binding == "entity_confirmed"\n'
     '    return not str((item or {}).get("entity") or "").strip()',
     "    return True",
     T_P02, "test_writer_render_four_state_gate"),
    # [R5] 各出口绕过唯一消费 API(改走结构访问器 = 未过门)
    ("R3-1/Ma 分组块绕过消费 API", "writing/evidence_pack.py",
     "    items = list(iter_entity_admissible_items(pack))",
     "    items = raw_pack_items(pack)",
     T_P02, "test_by_entity_block_four_state_gate"),
    ("R4/Mc 修复来源提示绕过消费 API+主体核对", "services/span_level_repair.py",
     "    for item in iter_entity_admissible_items(pack):\n"
     "        if not repair_subject_admits(item, target_entity=target_entity,\n"
     "                                     entity_whitelist=_wl):\n"
     "            continue\n"
     "        if item.get(\"verification_status\") not in VERIFIED_STATES:",
     "    for item in (pack.get(\"items\") or []):\n"
     "        if item.get(\"verification_status\") not in VERIFIED_STATES:",
     T_R4, "test_source_hint_four_state_gate"),
    ("R4/Md 数字资格绕过消费 API+主体核对", "services/span_level_repair.py",
     "    for item in iter_entity_admissible_items(pack):\n"
     "        if not repair_subject_admits(item, target_entity=target_entity,\n"
     "                                     entity_whitelist=_wl):\n"
     "            continue\n"
     "        for key in (\"claim\", \"excerpt\", \"title\", \"published_at\", \"publisher\", \"quote\"):",
     "    for item in (pack.get(\"items\") or []):\n"
     "        for key in (\"claim\", \"excerpt\", \"title\", \"published_at\", \"publisher\", \"quote\"):",
     T_R4, "test_number_pool_four_state_gate"),
    ("R5.1/Mi 拆修复链主体核对", "services/span_level_repair.py",
     '    target = str(target_entity or "").strip()\n'
     "    if not target:\n"
     "        return True",
     "    return True",
     T_R4, "test_repair_subject_pair_wrong_home_rejected_own_home_usable"),
    # [R5.2] M5 的绕过体带 raw_pack_items 词,在整文件 -x 下 R5.2-2 收口锁必然
    # 先红(锁在履职);为单独证明**计分行为锁**自身的判别力,定点跑该用例。
    ("P0-2/M5 计分绕过消费 API", "writing/primary_advantage.py",
     "    for item in iter_entity_admissible_items(evidence_pack):",
     "    from writing.evidence_pack import raw_pack_items as _rp\n"
     "    for item in _rp(evidence_pack):",
     T_P02 + "::test_refs_four_state_gate", "test_refs_four_state_gate"),
    # [R5] 白名单计数锁的判别力:四种曾骗过 R4 污点锁的逃逸形态 + rogue helper,
    # 新锁必须全杀(must_fail 全部 = test_prompt_outlet_enumeration_lock)
    ("R3-1/Mb rogue 出口藏进白名单文件(计数上涨)", "writing/evidence_pack.py",
     '        lines.append(f"- 限制：{limitation}")\n    return "\\n".join(lines)',
     '        lines.append(f"- 限制：{limitation}")\n    return "\\n".join(lines)\n\n\n'
     "def render_evidence_pack_rogue(pack: dict[str, Any]) -> str:\n"
     "    lines = []\n"
     '    for item in pack.get("items") or []:\n'
     '        lines.append(str(item.get("claim")))\n'
     '    return "\\n".join(lines)',
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R5/Me 逃逸形态①下标裸触", "writing/primary_advantage.py",
     "    from writing.evidence_pack import iter_entity_admissible_items\n",
     "    from writing.evidence_pack import iter_entity_admissible_items\n"
     '    _probe_me = (evidence_pack or {})["items"] if False else None\n',
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R5/Mf 逃逸形态②单引号 get", "writing/evidence_research.py",
     "    from writing.evidence_pack import raw_pack_items as _raw_items\n",
     "    from writing.evidence_pack import raw_pack_items as _raw_items\n"
     "    _probe_mf = (pack or {}).get('items') if False else None\n",
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R5/Mg 逃逸形态③f-string 无 join", "writing/article_generator_service.py",
     "        from writing.evidence_pack import raw_pack_items as _raw_items2\n",
     "        from writing.evidence_pack import raw_pack_items as _raw_items2\n"
     "        _probe_mg = f'{_evidence_pack.get(\"items\")}' if False else None\n",
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R5.2/Mj raw 访问器读 claim 拼串出口", "writing/primary_advantage.py",
     "    from writing.evidence_pack import iter_entity_admissible_items\n",
     "    from writing.evidence_pack import iter_entity_admissible_items\n"
     "    from writing.evidence_pack import raw_pack_items as _rr\n"
     '    _probe_mj = "\\n".join(str(i.get("claim")) for i in _rr(evidence_pack)) if False else None\n',
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R5/Mh 逃逸形态④helper 拆分", "writing/evidence_pack.py",
     "def set_raw_pack_items(pack: dict[str, Any], items: list) -> None:",
     "def _rogue_split_helper(pack: dict[str, Any]) -> list:\n"
     '    return pack["items"]\n'
     "\n"
     "\n"
     "def set_raw_pack_items(pack: dict[str, Any], items: list) -> None:",
     T_P02, "test_prompt_outlet_enumeration_lock"),
    ("R2-4/Ma 弱token回潮(任意子串)", "writing/evidence_research.py",
     "        for size in range(len(core) - 1, 2, -1):\n            if core[:size] in haystack:\n                return BINDING_UNVERIFIED",
     "        for size in range(len(core) - 1, 2, -1):\n            if any(core[i:i + size] in haystack for i in range(len(core) - size + 1)):\n                return BINDING_UNVERIFIED",
     T_P02, "test_r2_4_same_industry_different_company"),
    ("R2-4/Mb 拆核心词剥离", "writing/evidence_research.py",
     "    changed = True\n    while changed:",
     "    changed = False\n    while changed:",
     T_P02, "test_partial_token_unverified_not_isolated"),
    # ---------------- P0-3
    ("P0-3/M1 拆 outcome 幂等护栏", "scripts/backfill_monitoring_outcome_2026_08_14.py",
     "WHERE id = %s\n                       "
     "AND (target_outcome IS NULL OR target_outcome = 'legacy_unknown')",
     "WHERE id = %s",
     T_P03, "test_outcome_sql_idempotent_guard"),
    ("P0-3/M1b 回填只认 NULL(哨兵盲区复发)", "scripts/backfill_monitoring_outcome_2026_08_14.py",
     "WHERE (r.target_outcome IS NULL OR r.target_outcome = 'legacy_unknown')",
     "WHERE r.target_outcome IS NULL",
     T_P03, "test_outcome_scope_covers_sentinel"),
    ("P0-3/M1c 哨兵 response_status 直传分类器", "scripts/backfill_monitoring_outcome_2026_08_14.py",
     '    if response_status == "legacy_unknown":',
     '    if False:',
     T_P03, "test_legacy_sentinel_response_status_treated_missing"),
    ("P0-3/M2 歧义硬选", "scripts/backfill_publication_article_links_2026_08_14.py",
     '    if len(matched) > 1:\n        return None, "ambiguous"',
     '    if len(matched) > 1:\n        return sorted(matched)[0], "matched"',
     T_P03, "test_ambiguous_never_guessed"),
    ("P0-3/M3 拆 link 幂等护栏", "scripts/backfill_publication_article_links_2026_08_14.py",
     " WHERE id = %s AND article_id IS NULL", " WHERE id = %s",
     T_P03, "test_link_sql_idempotent_guard"),
    ("P0-3/M4 cron 时序错位", "api/scheduler.py",
     "trigger=CronTrigger(hour=3, minute=50, timezone=BEIJING_TZ),\n                id=\"monitoring_outcome_backfill\"",
     "trigger=CronTrigger(hour=5, minute=50, timezone=BEIJING_TZ),\n                id=\"monitoring_outcome_backfill\"",
     T_P03, "test_cron_registered_before_ledger_sync"),
    # ---------------- P1-5a
    ("P1-5a/M1 拆数据闸", "services/flywheel_writing_strategy_choice.py",
     "    if not can_apply:", "    if False:",
     T_P15A, "test_guard_blocks_llm_choice_without_data"),
    ("P1-5a/M2 拆 server 接线", "server.py",
     'can_apply=bool(_guidance_payload.get("can_apply")),', "",
     T_P15A, "test_server_wiring_passes_can_apply"),
    # ---------------- P1-1
    ("P1-1/M1 空块也拼段", "writing/keyword_topic_generator.py",
     'gap_section = f"\\n\\n{gap_block.strip()}" if str(gap_block or "").strip() else ""',
     'gap_section = f"\\n\\n[gap]{gap_block.strip()}"',
     T_P11, "test_empty_block_prompt_byte_identical"),
    ("P1-1/M2 拆样本闸", "services/reco_outcome_feedback.py",
     '    if not agg["available"] or agg["total_observations"] < agg["min_sample"]:\n        return ""',
     '    if not agg["available"]:\n        return ""',
     T_P11, "test_insufficient_sample_returns_empty"),
    ("P1-1/M3 删退回出口", "services/reco_outcome_feedback.py",
     "(打不动),**退回按关键词本身选题**,不得为攻缺口而脱离客户确认的关键词。",
     "(打不动),再想想。",
     T_P11, "test_gap_block_carries_fallback_outlet"),
    ("P1-1/M4 拆 KTG 接线", "writing/keyword_topic_generator.py",
     "_build_title_generator_prompt(self.industry, gap_block=_gap_block)",
     "_build_title_generator_prompt(self.industry)",
     T_P11, "test_ktg_wiring"),
    # ---------------- P1-2
    ("P1-2/M1 无 title 也判", "writing/evidence_precision_policy.py",
     '    if str(title or "").strip():\n'
     '        from writing.evidence_pack import raw_pack_items as _raw_items2',
     "    if True:\n"
     "        from writing.evidence_pack import raw_pack_items as _raw_items2",
     T_P12, "test_no_title_no_new_codes"),
    ("P1-2/M2 advisory 升 hard", "writing/evidence_precision_policy.py",
     '"entity_binding_unverified",\n                "advisory",',
     '"entity_binding_unverified",\n                "warning",',
     T_P12, "test_unverified_binding_yields_advisory"),
    ("P1-2/M3 拆绑定 advisory", "writing/evidence_precision_policy.py",
     "        if _unverified_binding:", "        if False:",
     T_P12, "test_unverified_binding_yields_advisory"),
    ("P1-2/M4 文案回退", "frontend/src/pages/Publishing/PublishCenter.tsx",
     "已生成 · 可先看稿", "未审核 · 可发布",
     T_P12, "test_four_tier_copy_in_frontend"),
    # ---------------- P1-3
    ("P1-3/M1 拆 policy 地板", "services/media_form_adaptation.py",
     '    if forms & _EDITORIAL_FORMS:\n        return "none"',
     '    if False:\n        return "none"',
     T_P13, "test_editorial_forms_floor_policy"),
    ("P1-3/M2 署名塞编辑归属(连结构自证一起拆)", "services/media_form_adaptation.py",
     """    byline = f"—— 本文由 {brand} 发布"
    # 结构性自证:署名模板永不落入编辑归属形态(判据与测试同源,双保险)。
    if _EDITORIAL_ATTRIBUTION.search(byline):  # pragma: no cover - 模板被改坏才会进
        return ""
    return byline""",
     '    return f"据{brand}报道"',
     T_P13, "test_no_editorial_attribution_any_form"),
    ("P1-3/M3 拆发布接线", "api/meijiehezi_api.py",
     "_policy = floor_contact_policy(_policy, _media_forms)",
     "pass",
     T_P13, "test_publish_wiring"),
    ("P1-3/M4 迁移不登记", "db/migration_manifest.py",
     '    "scripts/migration_media_form_2026_08_14.sql",', "",
     T_P13, "test_migration_registered"),
    # ---------------- P1-4
    ("P1-4/M1 第二份手写目录回潮(R2-9)", "writing/engine_targeting.py",
     "        return tuple(_PLATFORM_CONTRACT.keys())",
     '        return ("dashscope", "deepseek", "kimi", "doubao")',
     T_P14, "test_engine_keys_derived_from_ssot"),
    ("P1-4/M2 拆生成注入", "server.py",
     "_t['target_engine'] = _quote_target_engine",
     "pass",
     T_P14, "test_generation_injection_wiring"),
    ("P1-4/M3 拆归一", "server.py",
     '_quote_target_engine = _norm_te((quote or {}).get("target_engine"))',
     '_quote_target_engine = str((quote or {}).get("target_engine") or "")',
     T_P14, "test_generation_injection_wiring"),
    ("P1-4/M4 拆前端缺省档", "frontend/src/pages/Writing/WritingHall.tsx",
     "不定向（默认）", "默认",
     T_P14, "test_frontend_selector_wired"),
    # ---------------- P1-5b
    ("P1-5b/M1 拆样本闸", "services/reco_outcome_feedback.py",
     '    if not agg["available"] or agg["total_observations"] < agg["min_sample"]:\n        return None',
     '    if not agg["available"]:\n        return None',
     T_P15B, "test_lineage_feedback_insufficient_none"),
    ("P1-5b/M2 剥限定语", "services/reco_outcome_feedback.py",
     "口径:仅已配对子样本", "口径:样本",
     T_P15B, "test_coverage_note_qualifier"),
    ("P1-5b/M3 拆 lineage 接线", "writing/article_generator_service.py",
     "reco_feedback=_reco_feedback,", "",
     T_P15B, "test_three_consumption_points_wired"),
    ("P1-5b/M4 样本闸退化成只判0(R2-6 §0①)", "services/reco_outcome_feedback.py",
     '        citation_total < agg["min_sample"]',
     "        citation_total == 0",
     T_P15B, "test_media_feedback_min_sample_boundaries"),
    ("R2-6/Ma 拆域名多样性闸", "services/reco_outcome_feedback.py",
     '        or len(domains) < min_distinct_domains()\n',
     "\n",
     T_P15B, "test_media_feedback_diversity_gate"),
    # ---------------- P2
    ("P2/M1 拆核验门", "writing/brand_evidence_asset.py",
     "            if item.get(\"verification_status\") not in VERIFIED_STATES:\n                continue",
     "            if False:\n                continue",
     T_P2, "test_unverified_status_never_reused"),
    ("P2/M2 confirmed 门退回只排 different(R2-1)", "writing/brand_evidence_asset.py",
     '            if str(item.get("binding_state") or "") != "entity_confirmed":\n'
     "                continue  # R2-1:未确认同主体(含缺 binding)= 候选,不复用",
     '            if str(item.get("binding_state") or "") == "different_entity":\n'
     "                continue  # MUT",
     T_P2, "test_asset_four_state_behavior"),
    ("P2/M3 拆相关性初筛", "writing/brand_evidence_asset.py",
     "            if not q_grams or len(q_grams & hay) < 2:\n                continue",
     "            if False:\n                continue",
     T_P2, "test_unrelated_not_reused"),
    ("P2/M4 拆阶梯次序", "services/writing_next_step.py",
     'if f["topics"] == 0 or f["topics_untitled"] > 0:',
     'if f["topics"] == 0:',
     T_P2, "test_ladder_order"),
    ("P2/M5 拆资产接线", "writing/article_generator_service.py",
     "from writing.brand_evidence_asset import load_brand_verified_evidence",
     "load_brand_verified_evidence = None  # MUT",
     T_P2, "test_wiring"),
    # ---------------- R2-3 single-flight
    ("R2-3/Ma 拆并发共享", "writing/evidence_research.py",
     "    existing = _inflight.get(key)",
     "    existing = None",
     T_P01, "test_single_flight_concurrent_one_call"),
    ("R2-3/Mb 异常不清进行中态", "writing/evidence_research.py",
     "        _inflight.pop(key, None)\n        if not fut.done():\n            fut.set_exception(exc)",
     "        if not fut.done():\n            fut.set_exception(exc)",
     T_P01, "test_single_flight_error_path_not_reused"),
    # ---------------- R2-5 发布优先 + CTA
    ("R2-5/Ma 提示抢回发布前", "services/writing_next_step.py",
     '    if f["eligible_unpublished"] > 0:\n        why',
     '    if False and f["eligible_unpublished"] > 0:\n        why',
     T_P2, "test_ladder_order"),
    ("R2-5/Mb 可发布数又扣提示", "services/writing_next_step.py",
     '                0, facts.get("articles", 0) - facts.get("published", 0)',
     '                0, facts.get("articles", 0) - advisory_open - facts.get("published", 0)',
     T_P2, "test_eligible_not_reduced_by_advisory"),
    ("R2-5/Mc CTA 退化成死文本", "frontend/src/pages/Writing/WritingHall.tsx",
     'data-testid="next-step-cta"',
     'data-testid="next-step-cta-dead"',
     T_P2, "test_wiring"),
    # ---------------- R2-2 账本/闸/恢复
    ("R2-2/Mj 拆旧值账本(恢复的真依赖)", "scripts/backfill_monitoring_outcome_2026_08_14.py",
     "INSERT INTO monitoring_outcome_backfill_journal (",
     "INSERT INTO monitoring_outcome_backfill_journal_MUT (",
     T_P03, "test_backfill_restore_roundtrip_db"),
    ("R2-2/Mg 拆签发闸", "api/scheduler.py",
     'GEO_OUTCOME_BACKFILL_AUTO_APPLY", ""',
     'GEO_MUTATED_GATE_FLAG", ""',
     T_P03, "test_journal_first_and_gated_cron"),
    ("R2-2/Mv 拆恢复版本护栏", "scripts/restore_monitoring_outcome_backfill_2026_08_15.py",
     "                 WHERE id = %s\n                   AND outcome_resolver_version = %s",
     "                 WHERE id = %s",
     T_P03, "test_journal_first_and_gated_cron"),
]


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _run_tests(test_file: str) -> tuple[int, str]:
    proc = subprocess.run(
        PYTEST + [test_file], cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ}, timeout=600,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def main() -> int:
    survivors: list[str] = []
    misfires: list[str] = []
    for label, rel, old, new, test_file, must_fail in MUTATIONS:
        path = ROOT / rel
        original_bytes = path.read_bytes()
        text = _read(path)
        if text.count(old) < 1:
            print(f"[FAIL-SETUP] {label}: 变异锚点不存在(count={text.count(old)})")
            misfires.append(label)
            continue
        _write(path, text.replace(old, new, 1))
        try:
            code, out = _run_tests(test_file)
            if code == 0:
                print(f"[SURVIVED] {label} —— 拆了修法测试仍全绿(锁无判别力)")
                survivors.append(label)
            elif must_fail not in out:
                print(f"[MISFIRE] {label} —— 转红了,但红的不是 {must_fail}")
                misfires.append(label)
            else:
                print(f"[KILLED] {label}")
        finally:
            path.write_bytes(original_bytes)
        if path.read_bytes() != original_bytes:
            print(f"[FAIL-RESTORE] {label}: 复位后字节不一致")
            return 3
    # 全部复位后总回归必须绿
    all_tests = sorted({m[4] for m in MUTATIONS})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *all_tests], cwd=str(ROOT),
        capture_output=True, text=True, env={**os.environ}, timeout=900,
    )
    if proc.returncode != 0:
        print("[FAIL] 复位后总回归不绿:\n" + proc.stdout[-2000:])
        return 3
    print(f"\n==== 变异 {len(MUTATIONS)} 条 · 存活 {len(survivors)} · 哑火 {len(misfires)} ====")
    for s in survivors:
        print("  SURVIVED:", s)
    for m in misfires:
        print("  MISFIRE:", m)
    return 0 if not survivors and not misfires else 1


if __name__ == "__main__":
    raise SystemExit(main())
