#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""变异实验:逐条证明本包的判据**真的会响**。

用法(在包树根目录):  python tests/article_self_report_2026_08_19/run_mutations.py

每个变异 = 把本包的一处修复**改回病灶形态**,然后跑指定判据,期望它变红。
全绿的判据如果杀不动任何一个变异,说明它在守一个恒真的东西。

🔴 撤销一律用**内存里的原文**还原,**禁止 git checkout** ——
   checkout 还原到上次提交,会连带毁掉工作树里未提交的东西(本仓两次栽在这)。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 🔴 Windows 控制台默认 GBK,打第一个 ✅ 就 UnicodeEncodeError 把整轮实验带走
#    (而且 finally 已经还原过文件,看起来像"跑完了")。焊在脚本里,别靠记得传环境变量。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[2]
PKG = "tests/article_self_report_2026_08_19"

# 🔴 本文件里只要出现 `UPDATE <表名>` 这样的**完整字面量**,写入方普查就会把
#    run_mutations.py 本身当成一个 publish_records 的写入方 —— 而"识别出所有写入方"
#    正是那道普查存在的理由,被自己的变异清单触发是很荒谬的假阳性。
#    处置与 `test_publish_records_writer_census.py` 同源:**运行时拼装,不加白名单**。
#    白名单会开一个"把文件塞进豁免就能绕过普查"的后门,那比假阳性坏得多。
_TBL = "publish" + "_records"

#: 跨包闸自己所在的判据文件 —— R5 的几发变异打的是**闸本身的形状**。
FP = f"{PKG}/test_funding_path_excludes_self_report_pg16.py"

# (名字, 文件, 原文片段, 变异后片段, 期望变红的判据 node)
MUTATIONS = [
    # ── R2 §① 探针不许给自己签 verified ────────────────────────────────────
    (
        "M1 探针直接签 verified(R1 的病灶形态 · 攻击者同标题页即可骗过)",
        "services/publication_url_verifier.py",
        'return {"state": STATE_CONTENT_MATCHED, "reason": "content_fingerprint_matched",',
        'return {"state": STATE_VERIFIED, "reason": "content_fingerprint_matched",',
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_attacker_same_title_page_only_reaches_content_matched",
    ),
    (
        "M2 库级锁拿掉(verified 不再要求权威来源)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "CHECK (public_url_verification_state <> 'verified'\n"
        "           OR public_url_verification_source IN ('provider_receipt', 'human_attestation'));",
        "CHECK (public_url_verification_state IS NOT NULL);",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_probe_can_never_write_verified_even_if_code_tries",
    ),
    (
        "M3 service 层断言拿掉(只剩库那一道)",
        "services/publication_url_verifier.py",
        "    assert state != STATE_VERIFIED or source in AUTHORITY_SOURCES_FOR_VERIFIED, (",
        "    assert state in VERIFICATION_STATES or True, (",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_service_layer_also_refuses_probe_signed_verified",
    ),
    (
        "M4 探针路径又去升终态事实(快照 + outbox)",
        "services/publication_url_verifier.py",
        "    # 🔴 这里**没有**升终态事实的调用。content_matched 不落快照、不进 outbox。\n"
        "    return {\"state\": verdict[\"state\"], **detail}",
        "    _promote_verified_fact_with_cursor(cur, record, public_url)\n"
        "    return {\"state\": verdict[\"state\"], **detail}",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_probe_path_never_promotes_terminal_fact",
    ),
    # ── R2 §① 平台域清单 ──────────────────────────────────────────────────
    (
        "M5 入口域不校验平台清单(什么域都给 content_matched)",
        "services/publication_url_verifier.py",
        # 🔴 R3 后这行在 `_reprobe_availability_with_cursor` 里也有一处同形状 →
        #    单行锚命中 2 次会被 runner 判「没验」。多锚下一行,锁死探针主路径。
        "    if not url_is_within_platform_domain(platform, public_url):\n"
        "        # 🔴 R2 §①:域不在平台清单里,**连 content_matched 都不给**。",
        "    if False and not url_is_within_platform_domain(platform, public_url):\n"
        "        # 🔴 R2 §①:域不在平台清单里,**连 content_matched 都不给**。",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_url_outside_platform_domain_gets_nothing",
    ),
    (
        "M6 落地域不校验(站内短链 302 到攻击者站点即可绕过)",
        "services/publication_url_verifier.py",
        "    if final_url and not url_is_within_platform_domain(platform, final_url):",
        "    if False and final_url and not url_is_within_platform_domain(platform, final_url):",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_redirect_landing_outside_platform_domain_is_rejected",
    ),
    (
        "M7 域匹配退化成子串(yoojia.com 会被当成 jia.com 的子域)",
        "services/extension_platform_registry.py",
        "        if d and (host == d or host.endswith(\".\" + d)):",
        "        if d and d in host:",
        f"{PKG}/test_platform_domain_matching.py::test_sibling_domain_is_not_within_platform",
    ),
    # ── R2 §① 人工核实的 actor / 证据 / 审计 ──────────────────────────────
    (
        "M8 人工核实不留证据 hash",
        "services/publication_url_verifier.py",
        "                  evidence_sha256=evidence_sha256, evidence_note=(note or None)[:2000]",
        "                  evidence_sha256=None, evidence_note=(note or None)[:2000]",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_human_attestation_leaves_actor_evidence_and_audit",
    ),
    (
        "M9 空证据也放行",
        "services/publication_url_verifier.py",
        # 🔴 R3 后可达轴人工登记也有同一句校验 → 多锚它后面那行注释区分开。
        "    if len(evidence_text) < 16:\n"
        "        # 🔴 \"留了个空字符串当证据\"= 没有证据。",
        "    if False:\n"
        "        # 🔴 \"留了个空字符串当证据\"= 没有证据。",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_attestation_without_real_evidence_is_refused",
    ),
    (
        "M10 审计表的 CHECK 拿掉(可以有无证据的人工核实流水)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "            CHECK (verification_source <> 'human_attestation'\n"
        "                   OR (actor_user_id IS NOT NULL AND evidence_sha256 IS NOT NULL));",
        "            CHECK (verification_source IS NOT NULL);",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_audit_table_refuses_human_attestation_without_evidence",
    ),
    # ── R2 §②③ 用户面双轴 ────────────────────────────────────────────────
    (
        "M11 发布轴把 content_matched 也算成已发布",
        "services/publication_receipt_projection.py",
        '    if str(verification_state or "").strip().lower() == "verified":',
        '    if str(verification_state or "").strip().lower() in ("verified", "content_matched"):',
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_content_matched_is_not_published_anywhere",
    ),
    # ── [WO_273 · 2026-09-23] 6 发变异肯定式退役 ───────────────────────────
    # M12 / M13(发布结果轮询端点、插件历史列表三动作)、M19(浏览器写入方落 verified)、
    # M36 / M37(发布结果的缓存命中路径、WS 写入方塞核实态)、M41(前端两动作调插件端点):
    # 目标代码随插件后端**整体删除**(M41 的目标前端调用由 A 同单去掉),它们打的判据同单退役。
    # 接替:tests/extension_retirement_2026_09_23 的路由 / 模块缺席锁(面不许回来),
    #      test_publish_records_writer_census.py::test_declared_writers_match_repo(自报写入方不许再冒出)。
    (
        "M14 聚合 published-articles 不分已核实/未核实",
        "api/meijiehezi_api.py",
        "        verified_ids = sorted({r[\"article_id\"] for r in axis_rows\n"
        "                               if r[\"axis\"] == PUBLICATION_VERIFIED})",
        "        verified_ids = sorted({r[\"article_id\"] for r in axis_rows})",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_published_articles_keeps_dedupe_but_splits_verified",
    ),
    (
        "M15 聚合 article-publish-stats 又把自报当 status=2",
        "api/meijiehezi_api.py",
        "                    CASE WHEN pr.public_url_verification_state = 'verified'\n"
        "                         THEN 2 ELSE 3 END AS status,",
        "                    2 AS status,",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_article_publish_stats_does_not_call_it_published",
    ),
    (
        "M16 统一记录列表又把未核实的写成「已发布」/completed",
        "db/meijiehezi_db.py",
        # 🔴 R3 §⑤ 给这段 CASE 加了 COALESCE → 按新原文重锚(不是退役这一发)。
        "                    WHEN COALESCE(pr.status, '') = 'success' THEN 'reported_unverified'\n"
        "                    WHEN COALESCE(pr.status, '') = 'failed' THEN 'rejected'",
        "                    WHEN COALESCE(pr.status, '') = 'success' THEN 'completed'\n"
        "                    WHEN COALESCE(pr.status, '') = 'failed' THEN 'rejected'",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_unified_record_list_labels_and_family",
    ),
    (
        "M17 §③ 防重占位被一起收紧(用户会重复发同一篇)",
        "api/meijiehezi_api.py",
        "                WHERE pr.user_id = %s::TEXT\n"
        "                  AND pr.status = 'success'\n"
        "                  AND pr.article_id IS NOT NULL\n"
        "            ) t",
        "                WHERE pr.user_id = %s::TEXT\n"
        "                  AND pr.status = 'success'\n"
        "                  AND pr.public_url_verification_state = 'verified'\n"
        "                  AND pr.article_id IS NOT NULL\n"
        "            ) t",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_published_articles_keeps_dedupe_but_splits_verified",
    ),
    (
        "M18 SQL 轴与 Python 轴漂移(各写一份 CASE 的经典后果)",
        "services/publication_receipt_projection.py",
        # 🔴 R3 §⑤ 加 COALESCE 后重锚。
        "    \"WHEN COALESCE({r}.public_url_verification_state, '') = 'verified' \"",
        "    \"WHEN COALESCE({r}.public_url_verification_state, '') <> 'pending' \"",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_sql_axis_matches_python_axis",
    ),
    # ── 写入方 / census / 接线(R1 保留下来的) ────────────────────────────
    (
        "M20 严格链闸退回「来源位」(效果归因/KPI)",
        "services/strict_article_outcomes.py",
        "AND p.public_url_verification_state = 'verified'",
        "AND p.public_url_reported_explicitly IS TRUE",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_attacker_same_title_page_only_reaches_content_matched",
    ),
    (
        "M21 交付槽权威闸退回「来源位」(客户可见核验链)",
        "services/article_delivery_plan.py",
        'if str(fact.get("public_url_verification_state") or "") != "verified":',
        'if not bool(fact.get("public_url_verification_state")):',
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_forged_self_report_rejected_by_delivery_authority",
    ),
    (
        "M22 census 扫描器漏掉 UPDATE 动词",
        f"{PKG}/test_publish_records_writer_census.py",
        'r"(INSERT\\s+INTO|UPDATE|DELETE\\s+FROM)\\s+" + TABLE + r"\\b"',
        'r"(INSERT\\s+INTO|DELETE\\s+FROM)\\s+" + TABLE + r"\\b"',
        f"{PKG}/test_publish_records_writer_census.py::test_census_scanner_catches_a_forged_writer",
    ),
    (
        "M23 迁移没进 manifest(上线后永远不跑)",
        "db/migration_manifest.py",
        '    "scripts/migration_publish_records_url_verification_2026_08_19.sql",\n]',
        "]",
        f"{PKG}/test_verifier_is_wired_and_ssrf_safe.py::test_migration_is_registered_in_manifest",
    ),
    (
        "M24 cron 没接线(核实队列永远没人跑)",
        "server.py",
        'id="publication-url-verification", coalesce=True,',
        'id="publication-url-verification-DISABLED", coalesce=True,',
        f"{PKG}/test_verifier_is_wired_and_ssrf_safe.py::test_cron_registers_the_verification_job",
    ),
    # ── R2 §⑤ 资金路径(监测入池 = 扣费) ────────────────────────────────
    (
        "M26 资金闸失守:导出的 occurrence 口径退回裸 success",
        "services/publication_receipt_projection.py",
        "SELF_REPORT_OCCURRENCE_SQL = PUBLICATION_VERIFIED_SQL",
        "SELF_REPORT_OCCURRENCE_SQL = \"({r}.status = 'success')\"",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_exported_gates_are_the_one_line_fix",
    ),
    (
        "M27 body proof 退回来源位(图文包当前形状)",
        "services/publication_receipt_projection.py",
        # 🔴 这一行 R3 后在本文件出现 2 次(已核实谓词 + body proof)→ 多锚上一行,
        #    锁死 body proof 那一处。
        '    "({r}.submitted_content_snapshot_hash IS NOT NULL"\n'
        """    " AND COALESCE({r}.public_url_verification_state, '') = 'verified')\"""",
        '    "({r}.submitted_content_snapshot_hash IS NOT NULL"\n'
        '    " AND {r}.public_url_reported_explicitly IS TRUE)"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_exported_gates_are_the_one_line_fix",
    ),
    (
        "M28 监测入池闸被换掉(monitoring_db 直接读 publish_records)",
        "db/monitoring_db.py",
        # 🔴 那个四行块在 monitoring_db 里出现 **3 次**(9004 回填 / 9600 调度 /
        #    9668 blocked),单行锚命中 3 次 → runner 判「锚点失配 · 没验」
        #    (它不肯替我假装杀掉,这一点是对的)。往上连到
        #    keyword_monitor_subscriptions 子查询才唯一 —— 打的正是**调度**那处,
        #    也就是扣费入口。
        "                        WHERE kms.keyword_id = ck.id\n"
        "                    )\n"
        "              )\n"
        "              AND EXISTS (\n"
        "                  SELECT 1 FROM articles a\n"
        "                  WHERE a.quote_id = q.id\n"
        "                    AND a.first_published_at IS NOT NULL",
        "                        WHERE kms.keyword_id = ck.id\n"
        "                    )\n"
        "              )\n"
        "              AND EXISTS (\n"
        "                  SELECT 1 FROM articles a\n"
        "                  WHERE a.quote_id = q.id\n"
        "                    AND (a.first_published_at IS NOT NULL\n"
        "                         OR EXISTS (SELECT 1 FROM publish_records rx\n"
        "                                     WHERE rx.article_id = a.id"
        " AND rx.status = 'success'))",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_raw_self_report_never_enters_billing_pool",
    ),
    (
        "M25 核实器自开一条出站路径(绕过 SSRF 防线)",
        "services/publication_url_verifier.py",
        "from services.safe_https_probe import (",
        "import requests  # noqa\nfrom services.safe_https_probe import (",
        f"{PKG}/test_verifier_is_wired_and_ssrf_safe.py::test_verifier_does_not_open_a_second_outbound_path",
    ),

    # =====================================================================
    # R3 改动面(Review-CTO 2026-08-19 R3 七条)
    # =====================================================================
    # ── §① verified 单调不可降级 ─────────────────────────────────────────
    (
        "M29 收口处的终态守卫拿掉(P0 原形:重核把已核实写回 needs_action)",
        "services/publication_url_verifier.py",
        "    if current == STATE_VERIFIED and state != STATE_VERIFIED:",
        "    if False:",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_write_state_chokepoint_refuses_downgrade",
    ),
    (
        "M30 重核不再改道可达轴(探针又回去改写核实轴)",
        "services/publication_url_verifier.py",
        "    if from_state == STATE_VERIFIED:\n"
        "        return _reprobe_availability_with_cursor(cur, record, public_url, fetch)",
        "    if False:\n"
        "        return _reprobe_availability_with_cursor(cur, record, public_url, fetch)",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_verified_survives_admin_reprobe",
    ),
    (
        "M31 库级单调触发器摘掉(只剩 Python 那一道)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        # R4 把建触发器挪到文件末尾(§A 的清洗要在它不存在时跑),锚点跟着搬。
        "CREATE TRIGGER trg_publish_records_verification_monotonic\n"
        "    BEFORE UPDATE ON publish_records\n"
        "    FOR EACH ROW EXECUTE FUNCTION publish_records_verification_is_monotonic();",
        "CREATE TRIGGER trg_publish_records_verification_monotonic\n"
        "    BEFORE INSERT ON publish_records\n"
        "    FOR EACH ROW EXECUTE FUNCTION publish_records_verification_is_monotonic();",
        # 🔴 原来指向 test_db_trigger_refuses_downgrade_even_from_raw_sql,R4 加了
        #    verified_at 的 CHECK 之后那条判据即使没有触发器也红(CHECK 替它挡了),
        #    于是这一发**存活**、把"触发器没人验"这件事暴露出来。改指向真正只有
        #    触发器能拦的那一格:一条 UPDATE 同时改两列。
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_raw_sql_cannot_downgrade_by_clearing_verified_at_too",
    ),
    (
        "M32 触发器不再保住 verified_at(raw UPDATE 能把它清成 NULL)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "    IF OLD.public_url_verified_at IS NOT NULL THEN\n"
        "        NEW.public_url_verified_at := OLD.public_url_verified_at;\n"
        "    END IF;",
        "    IF FALSE THEN\n"
        "        NEW.public_url_verified_at := OLD.public_url_verified_at;\n"
        "    END IF;",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_raw_sql_cannot_null_out_verified_at",
    ),
    (
        "M33 可达轴结论恒 available(失效信息被吞)",
        "services/publication_url_verifier.py",
        "    availability = _PROBE_REASON_TO_AVAILABILITY.get(verdict_reason,\n"
        "                                                     AVAILABILITY_UNREACHABLE)",
        "    availability = AVAILABILITY_AVAILABLE",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_verified_survives_admin_reprobe",
    ),
    (
        "M34 探针可以自行判定 retracted(人工才能下的结论被机器签了)",
        "services/publication_url_verifier.py",
        '    "probe_failed": AVAILABILITY_UNREACHABLE,',
        '    "probe_failed": AVAILABILITY_RETRACTED,',
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_verified_survives_admin_reprobe",
    ),
    (
        "M35 人工撤回顺手把核实态也改了(下架被倒推成没发过)",
        "services/publication_url_verifier.py",
        "    _write_availability(cur, record_id, availability, detail,\n"
        "                        source=SOURCE_HUMAN_ATTESTATION)\n"
        "    _append_audit(cur, record_id, actor_user_id=int(actor_user_id), actor_kind=\"human\",",
        "    _write_state(cur, record_id, STATE_NEEDS_ACTION, source=SOURCE_HUMAN_ATTESTATION,\n"
        "                 method=\"m\", detail=detail)\n"
        "    _write_availability(cur, record_id, availability, detail,\n"
        "                        source=SOURCE_HUMAN_ATTESTATION)\n"
        "    _append_audit(cur, record_id, actor_user_id=int(actor_user_id), actor_kind=\"human\",",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_human_retraction_lands_on_availability_axis",
    ),
    # ── §② 缓存命中路径 —— WO_273 已退役(M36 / M37 见上方说明)──────────────
    # ── §③ 权限档与用户面动作 ────────────────────────────────────────────
    # [WO_273] 原 M38「can_attest 不看身份」:两个动作位随插件后端退役整体不再下发,锚点已不存在,
    #   换成 M38b —— 打的是同一格判据的新形状:动作位被悄悄加回来(点不动的动作又出现在用户面上)。
    (
        "M38b 统一记录列表又下发「重新核实」动作位(后端端点已删,点不动)",
        "db/meijiehezi_db.py",
        "            'can_view', (public_url IS NOT NULL AND public_url <> '')\n"
        "        ))",
        "            'can_view', (public_url IS NOT NULL AND public_url <> ''),\n"
        "            'can_reverify', (publication_axis = 'reported_success_unverified')\n"
        "        ))",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_unified_record_list_labels_and_family",
    ),
    (
        "M39 状态白名单又漏掉 reported_unverified(「待核实」筛选一点就报错)",
        "db/meijiehezi_db.py",
        '                             "reported_unverified", "rejected", "withdrawn", "refunded"}:',
        '                             "rejected", "withdrawn", "refunded"}:',
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_reported_unverified_status_filter_round_trips",
    ),
    (
        "M40 API 那层的 Literal 漏掉 reported_unverified(库放行了也是 422)",
        "api/meijiehezi_api.py",
        '    status: Literal["", "pending", "in_progress", "completed",\n'
        '                    "reported_unverified", "rejected", "withdrawn", "refunded"] = "",',
        '    status: Literal["", "pending", "in_progress", "completed",\n'
        '                    "rejected", "withdrawn", "refunded"] = "",',
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_api_status_literal_accepts_reported_unverified",
    ),
    # ── §④ 新列进 safe-add / 契约 ────────────────────────────────────────
    (
        "M42 权威来源列漏出 startup safe-add(冷启动那条路上没有它)",
        # [WO_273 · 改指向] safe-add 随插件后端退役按字节原样搬到这里,锚点原文不变。
        "db/publish_records_schema.py",
        '            ("public_url_verification_source", "VARCHAR(40)"),',
        '            ("public_url_report_source_v2", "VARCHAR(40)"),',
        f"{PKG}/test_r3_cache_probe_and_surfaces.py::test_new_column_is_in_startup_safe_add",
    ),
    (
        "M43 权威来源列漏出 readiness 契约",
        "services/geo_article_v14_schema_contract.py",
        '        "public_url_verification_source": ColumnContract("character varying(40)"),',
        "",
        f"{PKG}/test_r3_cache_probe_and_surfaces.py::test_new_column_is_in_schema_contract",
    ),
    # ── §⑤ NULL 档 ───────────────────────────────────────────────────────
    (
        "M44 发布轴 SQL 去掉 COALESCE(NULL status 掉进下一格)",
        "services/publication_receipt_projection.py",
        "    \"CASE WHEN COALESCE({r}.status, '') <> 'success' THEN 'not_published' \"",
        "    \"CASE WHEN {r}.status <> 'success' THEN 'not_published' \"",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_sql_axis_matches_python_axis",
    ),
    (
        "M45 未核实谓词去掉 COALESCE(NULL 核实态的行从这一档整个消失)",
        "services/publication_receipt_projection.py",
        "PUBLICATION_REPORTED_UNVERIFIED_SQL = (\n"
        "    \"(COALESCE({r}.status, '') = 'success'\"\n"
        "    \" AND COALESCE({r}.public_url_verification_state, '') <> 'verified')\"\n"
        ")",
        "PUBLICATION_REPORTED_UNVERIFIED_SQL = (\n"
        "    \"({r}.status = 'success'\"\n"
        "    \" AND {r}.public_url_verification_state <> 'verified')\"\n"
        ")",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_boolean_predicates_have_no_null_hole",
    ),
    (
        "M46 统一列表那份手写 CASE 去掉 COALESCE(与 SSOT 岔开)",
        "db/meijiehezi_db.py",
        "                    WHEN COALESCE(pr.status, '') <> 'success' THEN 'not_published'",
        "                    WHEN pr.status <> 'success' THEN 'not_published'",
        f"{PKG}/test_dual_axis_user_surfaces_pg16.py::test_unified_list_null_status_row_is_not_called_published",
    ),
    # ── §⑥ 相对 Location ─────────────────────────────────────────────────
    (
        "M47 重定向不 urljoin(正常站内 302 被记成「探不动」)",
        "services/publication_url_verifier.py",
        "            current = urljoin(current, location)",
        "            current = location",
        f"{PKG}/test_r3_cache_probe_and_surfaces.py::test_relative_location_is_resolved_against_current_url",
    ),

    # =====================================================================
    # R4 改动面(Review-CTO 2026-08-19 R4 五条)
    # =====================================================================
    # ── §A verified_at 的库级 CHECK ──────────────────────────────────────
    (
        "M48 verified_at 的 CHECK 拿掉(可以给没核实过的行直接写核实时间)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "    ADD CONSTRAINT publish_records_verified_at_requires_verified_state\n"
        "    CHECK (public_url_verified_at IS NULL\n"
        "           OR public_url_verification_state = 'verified');",
        "    ADD CONSTRAINT publish_records_verified_at_requires_verified_state\n"
        "    CHECK (TRUE);",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_verified_at_cannot_be_set_on_an_unverified_row",
    ),
    (
        "M49 清洗 UPDATE 挪回触发器之后(重放时空转 → 老库上装 CHECK 直接失败)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        ("DROP TRIGGER IF EXISTS trg_publish_records_verification_monotonic"
         " ON publish_records;\n"
         f"\nUPDATE {_TBL}\n"
         "   SET public_url_verified_at = NULL"),
        (f"\nUPDATE {_TBL}\n"
         "   SET public_url_verified_at = NULL"),
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_migration_nulls_out_preexisting_contradictory_verified_at",
    ),
    # ── §B retracted 的库级后盾 ──────────────────────────────────────────
    (
        "M50 retracted 的库级 CHECK 拿掉(只剩 Python 那一句自觉)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "    ADD CONSTRAINT publish_records_retracted_requires_human_source\n"
        "    CHECK (public_url_availability_state <> 'retracted'\n"
        "           OR public_url_availability_source = 'human_attestation');",
        "    ADD CONSTRAINT publish_records_retracted_requires_human_source\n"
        "    CHECK (TRUE);",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_probe_source_cannot_write_retracted_at_db_level",
    ),
    (
        "M51 收口处不再校验人工来源(探针能自己签 retracted)",
        "services/publication_url_verifier.py",
        "    assert (availability not in AVAILABILITY_HUMAN_ONLY_STATES\n"
        "            or source == SOURCE_HUMAN_ATTESTATION), (",
        "    assert (availability in AVAILABILITY_STATES\n"
        "            or source == SOURCE_HUMAN_ATTESTATION), (",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_write_availability_chokepoint_refuses_probe_signed_retraction",
    ),
    (
        "M52 可达轴不留来源(谁下的结论查不出来)",
        "scripts/migration_publish_records_url_verification_2026_08_19.sql",
        "    ADD CONSTRAINT publish_records_availability_state_needs_source\n"
        "    CHECK (public_url_availability_state IS NULL\n"
        "           OR public_url_availability_source IS NOT NULL);",
        "    ADD CONSTRAINT publish_records_availability_state_needs_source\n"
        "    CHECK (TRUE);",
        f"{PKG}/test_self_report_not_terminal_pg16.py::test_availability_state_without_source_is_refused",
    ),
    # ── §C 用户面裸 status 绿勾 ──────────────────────────────────────────
    # ── §D 第五桶 ────────────────────────────────────────────────────────
    (
        "M55 映射表漏掉第五个主状态(文章从所有 tab 消失 · R2 的原形)",
        "frontend/src/pages/Publishing/publishCenterScopeLogic.ts",
        "  reported_success_unverified: 'reportedUnverified',\n",
        "",
        f"{PKG}/test_r4_frontend_axis_parity.py::test_every_backend_primary_has_a_frontend_bucket",
    ),
    (
        "M56 桶算出来了但没接到 tab(点不到 = 照样看不见)",
        "frontend/src/pages/Publishing/PublishCenter.tsx",
        "                  key: 'reportedUnverified',",
        "                  key: 'published',",
        f"{PKG}/test_r4_frontend_axis_parity.py::test_reported_unverified_bucket_is_wired_all_the_way_to_a_tab",
    ),
    # ── §E 跨包闸的形状 ──────────────────────────────────────────────────
    (
        "M57 臂身退回非贪婪截断(COALESCE 形状的臂全部隐形)",
        "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py",
        # R5 把两个扫描循环并成**一个**实现(同一谓词写两处必有一处没人验),
        # 所以这行现在全仓唯一,锚点跟着简化。
        "            arm = m.group(0) + _arm_body(text, m.end())",
        "            arm = m.group(0) + text[m.end():m.end() + 900].split(')')[0]",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_occurrence_scanner_catches_a_coalesce_shaped_unguarded_arm",
    ),
    (
        # 🔴 R6 ② 重写 `_cmp_operand` 之后,这一发原来钉的那一行没了 —— **重锚不退役**:
        #    守的命题一个字没变(别名可选,无别名的臂也要看得见),只是搬了地方。
        "M58 列比较形状退回强制别名前缀(无别名的臂隐形)",
        "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py",
        '    col = r"(?:\\w+\\.)?\\b" + column + r"\\b(?:\\s*::\\s*\\w+)?"',
        '    col = r"\\w+\\." + column + r"\\b(?:\\s*::\\s*\\w+)?"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_scanner_sees_arms_without_a_table_alias",
    ),
    (
        "M59 扫描前不剥注释(注释里的分号截断臂身 / 注释能假冒核实闸)",
        "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py",
        "        text = _blank_out_comments(raw_text)\n"
        "        assert len(text) == len(raw_text)",
        "        text = raw_text",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_comment_cannot_forge_a_verified_gate",
    ),

    # =====================================================================
    # R5 改动面(Review-CTO 2026-08-19 R5 四条)
    # =====================================================================
    (
        "M60 起点退回只认 FROM(JOIN 进来的臂全部隐形)",
        FP,
        '    r"(?:FROM|(?:INNER\\s+|LEFT\\s+|RIGHT\\s+|FULL\\s+|CROSS\\s+)?"\n'
        '    r"(?:OUTER\\s+)?JOIN)\\s+(?:public\\.)?publish_records\\b"',
        '    r"FROM\\s+(?:public\\.)?publish_records\\b"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_scanner_catches_an_unguarded_join_arm",
    ),
    (
        "M61 起点不认 schema 限定(public.publish_records 隐形)",
        FP,
        '    r"(?:OUTER\\s+)?JOIN)\\s+(?:public\\.)?publish_records\\b"',
        '    r"(?:OUTER\\s+)?JOIN)\\s+publish_records\\b"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_scanner_catches_a_schema_qualified_arm",
    ),
    (
        "M62 剥注释不认块注释(/* */ 里能伪造核实闸)",
        FP,
        '_LINE_COMMENT = re.compile(r"/\\*.*?\\*/|(?:--|#)[^\\n]*", re.DOTALL)',
        '_LINE_COMMENT = re.compile(r"(?:--|#)[^\\n]*")',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_block_comment_cannot_forge_a_verified_gate",
    ),
    (
        "M63 派生表形态又见右括号就停(臂身截空 · 整条臂隐形)",
        FP,
        "                if _DERIVED_TABLE_TAIL.match(text, i):\n"
        "                    i += 1\n"
        "                    continue\n"
        "                break",
        "                break",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_scanner_catches_an_unguarded_derived_table_arm",
    ),
    (
        # 🔴 这一发原来打的是 `(?!(?:AND|OR))` 那个负向断言,**存活了** ——
        #    因为承重的根本不是它,而是后面那个"必须跟子句关键字"的 lookahead。
        #    存活把这件事暴露出来:那个负向断言够不到,已删。现在打承重的那段。
        "M64 派生表判别的子句关键字表放宽成「任意字符」(臂身越界吃进下一条臂的闸)",
        FP,
        '    r"(?=(?:WHERE|ON|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|GROUP|HAVING|ORDER|LIMIT"\n'
        '    r"|UNION|EXCEPT|INTERSECT)\\b)",',
        '    r"(?=\\w)",',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_derived_table_scan_still_stops_at_a_sibling_exists",
    ),
    (
        "M65 豁免退回整文件(meijiehezi_api 4523 行整体免检)",
        FP,
        "            reason = OCCURRENCE_SCAN_EXEMPT.get((rel, fn))",
        "            reason = OCCURRENCE_SCAN_EXEMPT.get((rel, fn)) or (\n"
        "                'whole-file' if rel in {r for r, _f in OCCURRENCE_SCAN_EXEMPT}\n"
        "                else None)",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_exemption_is_scoped_to_the_function_not_the_file",
    ),
    (
        "M66 豁免名单里塞一条没有对应违规臂的死条目(白开的后门)",
        FP,
        "OCCURRENCE_SCAN_EXEMPT: dict[tuple[str, str], str] = {",
        "OCCURRENCE_SCAN_EXEMPT: dict[tuple[str, str], str] = {\n"
        "    ('services/publication_url_verifier.py', 'sweep_pending_public_urls'):\n"
        "        '死条目:这个函数里的臂根本不违规,豁免它纯属白开口子,应当被锁抓到。',",
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py::test_every_exemption_is_actually_used",
    ),
    # ── R6 ② 等价谓词形态:每种写法各一发 ────────────────────────────────
    (
        "M67 起点识别去掉 IN 分支(`status IN ('success')` 的臂重新隐形)",
        FP,
        '        + r"|" + operand + r"\\s*\\bIN\\s*\\([^)]*" + lit',
        '        + r"|" + operand + r"\\s*\\bNEVERMATCH\\s*\\([^)]*" + lit',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_scanner_catches_every_equivalent_success_form[in_list]",
    ),
    (
        "M68 去掉反写分支(`'success' = rx.status` 隐形)",
        FP,
        r'        + r"|" + lit + r"\s*=\s*" + operand',
        r'        + r"|" + lit + r"\s*=\s*NEVERMATCH" + operand',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_scanner_catches_every_equivalent_success_form[reversed]",
    ),
    (
        "M69 去掉 ANY(ARRAY) 分支",
        FP,
        r'        + r"|" + operand + r"\s*=\s*ANY\s*\(\s*ARRAY\s*\[[^\]]*" + lit',
        r'        + r"|" + operand + r"\s*=\s*NOPE\s*\(\s*ARRAY\s*\[[^\]]*" + lit',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_scanner_catches_every_equivalent_success_form[any_array]",
    ),
    (
        "M70 operand 去掉 ::text 转型",
        FP,
        '    col = r"(?:\\w+\\.)?\\b" + column + r"\\b(?:\\s*::\\s*\\w+)?"',
        '    col = r"(?:\\w+\\.)?\\b" + column + r"\\b"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_scanner_catches_every_equivalent_success_form[cast]",
    ),
    (
        "M71 干掉「有包裹」那条分支(LOWER / 裸括号 形态全部隐形)",
        FP,
        '        + col\n        + r"(?:\\s*,\\s*\'[^\']*\')?"',
        '        + r"NEVERMATCH"\n        + r"(?:\\s*,\\s*\'[^\']*\')?"',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_scanner_catches_every_equivalent_success_form[lower]",
    ),
    (
        # 🔴 这一发打的是"反着开的闸"那条路:认了 NOT IN,
        #    `state NOT IN ('verified')` 就会被当成闸 —— 假绿,方向最贵的那种。
        "M72 IN 前面允许 NOT(否定形态被当成断言/闸)",
        FP,
        '        + r"|" + operand + r"\\s*\\bIN\\s*\\([^)]*" + lit',
        '        + r"|" + operand + r"\\s*(?:NOT\\s+)?\\bIN\\s*\\([^)]*" + lit',
        f"{PKG}/test_funding_path_excludes_self_report_pg16.py"
        "::test_negated_predicates_are_not_read_as_assertions",
    ),
    (
        "M73 开括号退成 {0,3}(允许没开却闭 · COUNT(status)='success' 会命中)",
        FP,
        '|(?<![\\w.])\\(\\s*){1,3}"',
        '|(?<![\\w.])\\(\\s*){0,3}"',
        "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py::test_cmp_shape_matches_both_bare_and_coalesce[COUNT(rx.status) = 'success'-False-False]",
    ),
    # ── [WO_273 · 2026-09-23] A 同单(21afc6084)造成的 8 发肯定式退役 ─────────────────────
    # M53 / M54(订单管理「自发记录」子分页)· M74 / M75(发布中心自助模式轮询与恢复)·
    # M76 / M77(publishSelfAxis.ts)· M78 / M83(verify-publish-self-axis.mjs 那把接线闸):
    # 被变异的代码(或被测的闸本身)随插件退役整块删除,锚点 / 目标文件已不存在(B 在 A+C 合并树上机械核出)。
    # 接替:build 链第 69 步 `frontend/scripts/test-self-publish-retired.mjs`(每次 bake 跑;
    #   S1 自助模式键 = 0 · S2 frontend/src 的 `/api/extension` = 0 · S4 已删文件不许回来 · S5 界面无「自助」;
    #   它自带毒臂 C1–C4 与分母自证 S0,接替 M78 / M83 那把旧闸的变异);
    #   行为臂 `test-self-publish-retired-render.mjs` 的 A3(订单管理无「自发记录」)。
    # ── R6 补充件:fallback 那条静默旧路 ──────────────────────────────────
    # [WO_273 · 重锚] 被测代码还在,只是原 `pw:fallback` 的浏览器配置随插件退役删了;A 把 fallback 判据
    #   原样搬进了行为臂 H1(未核实 ⇒ 第五桶)+ 对照臂 H2(已核实 ⇒ 已分发)。三发都在 A+C 合并树上
    #   真跑过:退出码 1、红的是 H1(不是 3),还原后回 0。
    (
        "M79 分组 fallback 退回裸占位集(未核实的自报进已分发桶)",
        "frontend/src/pages/Publishing/publishCenterScopeLogic.ts",
        "    const k = articleKey(a);\n"
        "    if (verifiedIds.has(k)) return true;\n"
        "    return publishedIds.has(k) && !reportedIds.has(k);",
        "    return publishedIds.has(articleKey(a));",
        "mjs:scripts/test-self-publish-retired-render.mjs",
    ),
    (
        "M80 第五桶不认 fallback(未核实的文章从所有 tab 里消失)",
        "frontend/src/pages/Publishing/publishCenterScopeLogic.ts",
        "    return reportedIds.has(articleKey(a)) && !verifiedIds.has(articleKey(a));",
        "    return false;",
        "mjs:scripts/test-self-publish-retired-render.mjs",
    ),
    (
        "M81 前端不接端点的核实位(两个新集合永远是空集)",
        "frontend/src/pages/Publishing/PublishCenter.tsx",
        "        setReportedUnverifiedArticleIds(new Set(d.reported_success_unverified_article_ids || []));",
        "        setReportedUnverifiedArticleIds(new Set());",
        "mjs:scripts/test-self-publish-retired-render.mjs",
    ),
    (
        "M82 重投弹窗把未核实并回「已发布」(对没核实过的文章印已发布)",
        "frontend/src/pages/Publishing/publishCenterScopeLogic.ts",
        "      else if (st.status === 'reported_success_unverified') state = 'reported_unverified';",
        "      else if (st.status === 'reported_success_unverified') state = 'published';",
        "mjs:scripts/test-publish-center-scope.mjs",
    ),
    (
        # 开括号那一侧的第二件承重物:裸括号前面不许是标识符。
        # 去掉它 = `COUNT(` 的那个括号被当成"裸括号",聚合又被当成状态判断。
        "M84 去掉裸括号的 lookbehind(COUNT( 的括号被当成裸括号)",
        FP,
        '|(?<![\\w.])\\(\\s*){1,3}"',
        '|\\(\\s*){1,3}"',
        "tests/article_self_report_2026_08_19/test_funding_path_excludes_self_report_pg16.py::test_cmp_shape_matches_both_bare_and_coalesce[COUNT(rx.status) = 'success'-False-False]",
    ),
]


def run(node: str) -> str:
    """跑一条判据,返回 "green" / "red" / "unusable"。

    🔴 [WO_273] 原来返回 bool(rc==0 即绿、其余一律算「红 = 杀死」)。本仓的 .mjs 判据是三态退出码
       (0 过 / 1 有失败 / 3 判据不可用),`test-self-publish-retired-render.mjs` 起不来浏览器或拿不到
       构建产物时退 3 —— 旧写法会把「根本没测」记成「杀死」。所以 mjs / pw 两种:脚本或配置不在、
       或退 3 ⇒ "unusable",单列,不许混进杀死(B 09-23 提醒)。pytest 判据口径不变。

    node 有三种形态 —— 本包的判据不全是 pytest:
      · `tests/...::test_x`  → pytest(默认)
      · `pw:<grep>`          → 真浏览器判据(R6 ① 的五个消费点),起 chromium 跑
      · `mjs:<script>`       → build 链里的静态接线闸

    🔴 前端的行为判据**必须**能进这张表。R6 ① 改的是用户面,而用户面的缺陷
       正是"源码里分支还在、屏幕上却是绿的"那一类 —— 只用源码锁做变异实验,
       等于用一把量不到病灶的尺子证明尺子有用。
    """
    if node.startswith("pw:"):
        if not (ROOT / "frontend" / "playwright.publish-self-axis.config.ts").exists():
            return "unusable"
        proc = subprocess.run(
            ["npx", "playwright", "test",
             "--config=playwright.publish-self-axis.config.ts",
             "--grep", node[3:]],
            cwd=ROOT / "frontend", capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=True,
        )
        return "green" if proc.returncode == 0 else "red"
    if node.startswith("mjs:"):
        argv = node[4:].split()
        if not (ROOT / "frontend" / argv[0]).exists():
            return "unusable"
        proc = subprocess.run(
            ["node", *argv],
            cwd=ROOT / "frontend", capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=True,
        )
        if proc.returncode == 3:
            return "unusable"
        return "green" if proc.returncode == 0 else "red"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return "green" if proc.returncode == 0 else "red"


def main() -> int:
    print("基线:先确认全部判据是绿的(不然变异结果没意义)")
    if run(f"{PKG}/") != "green":
        print("🔴 基线就不绿,先修基线")
        return 2

    survived = []
    for name, rel, old, new, node in MUTATIONS:
        path = ROOT / rel
        backup = path.read_text(encoding="utf-8", newline="")
        if backup.count(old) != 1:
            print(f"⚠️  {name}:锚点命中 {backup.count(old)} 次(期望 1)—— 变异没打上,记作**没验**")
            survived.append((name, "锚点失配"))
            continue
        path.write_text(backup.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            verdict = run(node)
        finally:
            path.write_text(backup, encoding="utf-8", newline="")  # 🔴 内存还原,不用 git
        if verdict == "green":
            print(f"🔴 存活  {name}\n        判据 {node} 在病灶形态下**照样绿**")
            survived.append((name, "存活"))
        elif verdict == "unusable":
            print(f"⚠️  {name}:判据不可用(脚本 / 配置不在,或退出码 3)—— 记作**没验**,不是杀死")
            survived.append((name, "判据不可用"))
        else:
            print(f"✅ 杀死  {name}")

    print()
    if survived:
        print(f"🔴 {len(survived)}/{len(MUTATIONS)} 个变异存活:")
        for n, why in survived:
            print(f"   · {n}  [{why}]")
        return 1
    print(f"✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全杀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
