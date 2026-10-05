# -*- coding: utf-8 -*-
"""变异 runner · 文章可信度与推荐效果最终包(自曝清零 + D5/D6-A + 法律单点化 + 禁畏缩)。

每条变异 = 一次定点文本替换。跑锁,**期望转红**;仍绿 = 该处没有判别力。

[最终接管 2026-08-10] 相比旧归档版的三处适配(都是终态语义变了,不是删覆盖):
  · 旧 M39-M46(行业聚合推荐信号块)整组移除 —— 该模块按工单 §9 不移植,
    换成 D6-A 组(MA1-MA7);
  · D5 组锚点对齐 fail-closed 三态版(裸域名不再回退《标题》,M52 换向);
  · 新增 legal 单点化组(ML1-ML4)与禁畏缩组(MH1-MH2)。

🔴 Windows 三坑(踩过,逐条防):
  1. `Path.write_text()` 默认 newline=None → 整文件 CRLF 翻转,污染 diff。
     全程 `newline=""` 写回。
  2. 子进程必须带 `TEST_DATABASE_URL`,否则 conftest 直接 RuntimeError → 全体 ABORT
     被误读成"全杀"。
  3. 替换必须**验证真的发生过**(old 在文件里恰好 1 次),否则"没替换成功"
     会伪装成"锁杀掉了"。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = [
    "tests/test_selfdisclose_zero_2026_08_10.py",
    "tests/test_evidence_attribution_supply_2026_08_10.py",
    "tests/test_primary_advantage_2026_08_10.py",
    "tests/test_prompt_contract_consistency_2026_08_10.py",
    "tests/test_legal_catalog_single_source_2026_08_10.py",
    "tests/test_article_stance_no_hedging_2026_08_10.py",
    # [返修 R4 2026-08-11] 行为级锁:防「字面在语义死」(第八例形态)
    "tests/test_r4_behavior_locks_2026_08_11.py",
    # [返修 §0 2026-08-11] 裁决一专项三锁(不删不降级/三类瑕疵仍清/改写优先)
    "tests/test_owner_ruling_locks_2026_08_11.py",
    # [返修 R3 2026-08-11] 裁定书四条阻上线的专项锁(A1 降档/A2 prompt 侧门/A4 C1 真锁)
    "tests/test_rework_r3_verdict_locks_2026_08_11.py",
    # [返修 R4 2026-08-11] Owner 亲裁三件(商业自爆清除/亲测放开/免责清理)+ 已裁边界
    "tests/test_rework_r4_owner_locks_2026_08_11.py",
    # [返修 R5 2026-08-12] 三档夹具铁律(纯形态/纯事实/混合句)+ 谁与谁判别 + 事实闸
    "tests/test_rework_r5_threetier_locks_2026_08_12.py",
    # [返修 R6 2026-08-12] 判据换代:矩阵夹具(笛卡尔积)+ 组装后 messages 锁
    "tests/test_rework_r6_matrix_locks_2026_08_12.py",
    # [返修 R7 2026-08-12] 判别依据换代:句法角色(引语/人名修饰/关系双方)
    # × 分句符 × 建议共存;RE1/RE2/RE3 三条退回变异由它接杀
    "tests/test_rework_r7_role_locks_2026_08_12.py",
    # [返修 R8 2026-08-12] 失败方向反转(宁漏勿毁):清单外反向对照 +
    # 硬事实闸 + 锚定证明;RF1/RF2/RF3 三条退回变异由它接杀
    "tests/test_rework_r8_failsafe_locks_2026_08_12.py",
    # [返修 R9 2026-08-13] 摘除粒度闸:整句级摘除要比从句级更强的证明
    # (自曝跨度并集外无实质内容);RG1 粒度闸拆除由它接杀
    "tests/test_rework_r9_granularity_locks_2026_08_12.py",
]

W = "writing/"
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ---- 自曝清零:SSOT 降级 / 删除式清洗 ----
    ("M01", f"{W}source_disclosure_style.py",
     'def prose_attribution(kind: str) -> str:', 'def prose_attribution(kind: str) -> str:\n    return DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS.get(kind, "据企业提供的资料")'),
    ("M02", f"{W}source_disclosure_style.py",
     'def table_label(kind: str) -> str:', 'def table_label(kind: str) -> str:\n    return DEPRECATED_TABLE_SOURCE_LABELS.get(kind, "企业资料")'),
    ("M03", f"{W}source_disclosure_style.py",
     'def parenthesized(kind: str) -> str:', 'def parenthesized(kind: str) -> str:\n    return f"（{TABLE_SOURCE_HEADER}：{DEPRECATED_TABLE_SOURCE_LABELS.get(kind, chr(0))}）"'),
    ("M04", f"{W}source_disclosure_style.py",
     '    out.extend(DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS.values())',
     '    out.extend(("据企业提供的资质文件", "据企业提供的资料"))  # 字面量拷贝'),
    ("M05", f"{W}source_disclosure_style.py",
     '    out = _DANGLING_MID_PUNCT.sub("", out)', '    pass'),
    ("M06", f"{W}content_cleaner.py",
     '    return _blank_table_type_word_cells(content)', '    return content'),
    ("M07", f"{W}content_cleaner.py",
     '    for pattern in _SELF_DISCLOSURE_PATTERNS:\n        content = _sds.strip_self_disclosure(content, pattern)',
     '    pass'),
    ("M09", f"{W}content_cleaner.py",
     "                line = _TABLE_TYPE_WORD_CELL.sub(r\"\\1\\2\", line)", "                pass"),
    # ---- 判定豁免拆除 / 邻近来源收窄 ----
    ("M10", f"{W}evidence_first_policy.py",
     '        if _SELF_SOURCE_MARKER_RE.search(seg):\n            continue  # 我方单方材料,不是外部来源',
     '        pass'),
    ("M11", f"{W}evidence_first_policy.py",
     'r"来源[:：]|公开披露|监管|司法|工商|财报|年报|实测方法|样本)"',
     'r"来源[:：]|公开披露|监管|司法|工商|财报|年报|实测方法|样本|截至\\s*20\\d{2})"'),
    ("M12", f"{W}evidence_first_policy.py",
     '    _self_disclosed = _ENTERPRISE_SOURCE_DECLARATION_RE.search(body_scan)',
     '    _self_disclosed = None'),
    ("M13", f"{W}evidence_first_policy.py",
     '        if evidence_mode != "no_evidence" and _has_nearby_source(body_scan, match):\n            continue',
     '        if evidence_mode != "no_evidence":\n            continue  # 豁免复活'),
    ("M22", f"{W}evidence_first_policy.py",
     '            line = _ANONYMOUS_AUTHORITY_RE.sub("未具名材料", line)',
     '            line = _ANONYMOUS_AUTHORITY_RE.sub("未具名材料", line)\n            line = f"{line.rstrip()}（来源未具名，需进一步核验。）"'),
    # ---- 清洗后重跑判定(顺序修复)----
    ("M14", f"{W}article_generator_service.py",
     '            rejudge_after_sanitize(\n                _article_title, _content, article, topic, where="_save_article",\n            )',
     '            pass  # 接线拆掉'),
    ("M15", f"{W}article_generator_service.py",
     '        rejudge_after_sanitize(\n            article.get("title") or "", article_content, article, topic,\n            where="rewrite_article",\n        )',
     '        pass  # 接线拆掉'),
    ("M16", f"{W}post_sanitize_rejudge.py",
     '    qw["evidence"] = trust.warning_payload()', '    pass  # 不覆盖陈旧结论'),
    ("M18", f"{W}post_sanitize_rejudge.py",
     '    qw[DELTA_KEY] = {', '    qw["_wrong_key"] = {'),
    # ---- prompt 合同(模板侧)----
    ("M19", f"{W}templates/canonical_family_templates.py",
     '  **禁止**写“企业相关信息来自企业提交资料（截至 X 年 X 月）”“未经独立核验”',
     '  客户资料来源在文首**一次性声明**（“企业提交资料（截至 X 年 X 月），未经独立核验”），'),
    ("M20", f"{W}templates/common_rules.py",
     '- 证据冲突时并列呈现冲突与口径；证据不足时先检索真实公开信源增援，挂不上就改写成',
     '- 证据冲突时并列呈现冲突与口径；证据不足时明确写“尚无足够公开证据”，然后'),
    ("M21", f"{W}source_disclosure_style.py",
     '  “据企业提供的资料”“据企业提供的资质文件”“据企业提供的项目资料”',
     '  （此处原为禁令清单，已删）'),
    ("M47", f"{W}templates/common_rules.py",
     '- 🔴 **凡是承担推荐、比较或名次结论的实体（客户与竞品同等对待），必须至少有一项可核验的支持事实**',
     '- 尽可能给每个实体一项事实'),
    ("M53", f"{W}templates/common_rules.py",
     '## 五、证据不足时的降级写法（**不是停写**）',
     '## 五、出稿硬门'),
    # ---- D4 立场纪律 ----
    ("M49", f"{W}evidence_pack.py",
     '"**严禁**把它当成支持证据引用;"', '""'),
    # ---- 返工 ①:主语判别(盲删会把第三方来源披露删空)----
    ("M33", f"{W}content_cleaner.py",
     '    return bool(_SELF_MATERIAL_NOUN.search(span))', '    return True'),
    ("M34", f"{W}content_cleaner.py",
     '        return "" if _is_self_side_source(span) else span',
     '        return span'),
    ("M35", f"{W}content_cleaner.py",
     '        from writing.evidence_first_policy import _SELF_SOURCE_MARKER_RE\n'
     '        if _SELF_SOURCE_MARKER_RE.search(span):\n'
     '            return True',
     '        pass  # 不再复用姊妹模块'),
    ("M37", f"{W}content_cleaner.py",
     '    content = _strip_self_side_source_sentences(content)', '    pass'),
    # ---- 返工 ②:全站契约第 3 条 ----
    ("M38", f"{W}evidence_first_policy.py",
     '但**正文里只写具体来源方 + 日期**',
     '企业提交资料可集中披露，再按企业档案/项目资料/资质资料/报价或合同等事实类型简洁标注'),
    # ---- D5 信源增援(fail-closed 三态终态)----
    ("M23", f"{W}evidence_pack.py",
     '''    prose = f"据{publisher}{(' ' + date) if date else ''}"''',
     '    prose = ""'),
    ("M24", f"{W}evidence_pack.py",
     '    if not publisher or publisher.lower() in {g.lower() for g in _GENERIC_PUBLISHER}:\n        return TRUST_UNVERIFIED',
     '    pass'),
    ("M51", f"{W}evidence_pack.py",
     '    if client_domains and _same_site(item.get("url"), client_domains):\n        return TRUST_SELF',
     '    pass'),
    ("M52", f"{W}evidence_pack.py",
     '    if _looks_like_domain(publisher):\n        mapped = carrier_for_domain(publisher)\n        if mapped:\n            return mapped["tier"]\n        return TRUST_UNVERIFIED',
     '    pass  # 裸域名一律当独立第三方(R7 映射与 fail-closed 双双被拆)'),
    ("M54", f"{W}evidence_pack.py",
     '    if trust != TRUST_INDEPENDENT:',
     '    if False:'),
    ("M25", f"{W}evidence_pack.py",
     '''            f"来源可信状态：{_trust_label} | "''',
     '            ""'),
    ("M28", f"{W}evidence_pack.py",
     r'    m = re.match(r"(\d{4})[-/年](\d{1,2})", text)', '    m = None'),
    ("M29", f"{W}body_internal_marker_sanitizer.py",
     '        prose = attribution.get(eid)\n'
     '        if not prose:\n'
     '            return ""          # manifest 里没有归属 → 退回删除',
     '        prose = attribution.get(eid) or "据公开记录"'),
    ("M30", f"{W}body_internal_marker_sanitizer.py",
     '        return f"（{prose}）"', '        return ""'),
    ("M31", f"{W}body_internal_marker_sanitizer.py",
     '        cleaned, markers = sanitize_article_body(\n'
     '            original, article.get("evidence_pack"),\n'
     '            self_names=self_names_from_article(article),\n'
     '        )',
     '        cleaned, markers = sanitize_article_body(original)'),
    ("M32", f"{W}evidence_precision_policy.py",
     '就近挂上**来源方 + 日期**的自然归属句',
     '在同一段或同一表格行末标注已核验编号，例如“……〔EV-001〕”'),
    # ---- D6-A 主优势接线 ----
    ("MA1", f"{W}primary_advantage.py",
     '    ranked.sort(key=lambda item: (-item["score"], item["advantage"]))',
     '    pass  # 不排序'),
    ("MA2", f"{W}primary_advantage.py",
     '        if _VAGUE_PHRASE.search(phrase) and not _SPEC_NUMBER.search(phrase):\n            return',
     '        pass'),
    ("MA3", f"{W}primary_advantage.py",
     '        if str(item.get("relationship") or "") != "support":\n            continue  # 只有明确支持关系才计分(refute 是 D4 红线,background 是 R6)',
     '        pass'),
    ("MA4", f"{W}primary_advantage.py",
     '"- **只选一条**与本篇问题最相关、客户事实撑得住、一句话能说清的主优势,"',
     '"",'),
    ("MA5", f"{W}primary_advantage.py",
     '        "reco_feedback": None,', '        "reco_feedback": "done",'),
    ("MA6", f"{W}evidence_research.py",
     '    for _hint in (advantage_hints or [])[:2]:',
     '    for _hint in []:'),
    ("MA7", f"{W}primary_advantage.py",
     '    return 0.5 * _overlap(cand, _bigrams(keyword)) + 0.5 * _overlap(cand, _bigrams(question))',
     '    return _overlap(cand, _bigrams(question))'),
    # ---- 法律单点化 ----
    ("ML1", "services/marketing/legal_context.py",
     '    if term == "第一" and _ORDINAL_AFTER.match(after):\n        return True',
     '    pass'),
    ("ML2", "services/marketing/legal_context.py",
     '    if _TECH_PARAM_AFTER.match(after):\n        return True',
     '    pass'),
    ("ML3", "services/marketing/guards.py",
     '    adlaw = sorted({hit.term for hit in find_absolute_violations(text, terms=pack["ad_law"])})',
     '    adlaw = _find_words(text, pack["ad_law"])'),
    ("ML4", f"{W}evidence_first_policy.py",
     'LEGACY_UNSIGNED_RANKING_SUPPLEMENT = (\n    "排名第一",',
     'LEGACY_UNSIGNED_RANKING_SUPPLEMENT = (\n    "第一品牌", "排名第一",'),
    # ---- R4 · 「字面在语义死」形态(Review 变异实证 Y1a/Y2a · 行为锁必杀) ----
    ("Y1A", f"{W}article_generator_service.py",
     "            if _adv_block:", "            if False and _adv_block:"),
    ("Y2A", f"{W}article_generator_service.py",
     "            rejudge_after_sanitize(",
     "            if False: rejudge_after_sanitize("),
    # ---- R3 · 增援预算掐死形态(接线锁必杀) ----
    ("Y3A", "writing/evidence_research.py",
     "    if _hint_lanes:\n        lanes = list(lanes) + _hint_lanes",
     "    pass  # 增援 lane 回到截断点之前 = 结构性必死"),
    # ---- §0 裁决一 · 防新删除机器(MC1:摘归属退化成整行删) ----
    ("MC1", f"{W}source_disclosure_style.py",
     '    out = pattern.sub("", str(text or ""))',
     '    out = chr(10).join(ln for ln in str(text or "").splitlines() if not pattern.search(ln))'),
    # ---- R3 裁定书四条(2026-08-11)----
    # RA1:六域名降档退回(搜狐号软文重新拿到「据搜狐网报道」门户待遇)
    ("RA1", "config/verified_domain_carriers.json",
     '    "sohu.com": {"carrier": "搜狐网", "tier": "platform_ugc"},',
     '    "sohu.com": {"carrier": "搜狐网", "tier": "portal_media"},'),
    # RA2:删除指令旧文案复活(prompt 侧门:让模型删掉除方法论外的全部内容)
    ("RA2", f"{W}evidence_precision_policy.py",
     '            "本篇没有外部已核验来源；客户自有事实照写、不删不降级，"\n'
     '            "如需增强可信度，可换用能挂上公开信源的事实或补充可核验的公开信息。",',
     '            "没有可支撑专业断言的已核验证据；正文应只保留方法、问题清单和待核验项。",'),
    # RA4:C1 豁免血缘化退回(Z2 更狠形态:brand_supported 恒 False)
    ("RA4", f"{W}evidence_precision_policy.py",
     '            brand_supported = (',
     '            brand_supported = False and ('),
    # RA4b:Z2 第一形态 —— 恢复「必须先写企业声明才豁免」前置
    ("RA4b", f"{W}evidence_precision_policy.py",
     '            brand_supported = (',
     '            brand_supported = ("企业提交资料" in content) and ('),
    # ---- R4 Owner 亲裁三件(2026-08-11)----
    # RB1:恢复 P0-1 全仓唯一「正面命令自曝」指令行
    ("RB1", f"{W}config.py",
     '- 行动建议采用通用语言："选择具备XX能力的服务商"，不要直接点名\n"""',
     '- 行动建议采用通用语言："选择具备XX能力的服务商"，不要直接点名\n'
     '- 文末声明如有"商业关联"则坦诚承认，不要用虚假的"无关联"\n"""'),
    # RB2:恢复「不得冒充亲测」禁令(亲测放开被退回)
    ("RB2", f"{W}templates/common_rules.py",
     '做法与限制。体验式表达',
     '做法与限制；没有一手经验不得冒充亲测。体验式表达'),
    # (R4 的 RB3 旧锚随 R6 结构重构消亡,已在 R5/R6 段重锚为「空泛为准保护拆除」)
    # RB4(R5 换锚:旧关键词正则已被「谁与谁」判别替代):纯自曝放行
    # (R8 重锚:整句删捷径统一收进从句路径,纯自曝放行=摘除动作整体拆除)
    ("RB4", f"{W}content_cleaner.py",
     "                if removable and not _HARD_FACT_RE.search(text):",
     "                if False:  # (RB4 变异:纯自曝放行 —— 摘除动作拆除)"),
    # ---- R5 三条(R6 重锚:实现按裁定换代)----
    # RC1:第三方判别退回 is_self≡True(第三方业务事实也被当自曝 → 删除机器复活)
    # (R7 重锚:is_self 布尔位换代为 _effective_self_spans 角色判别)
    ("RC1", f"{W}content_cleaner.py",
     "            self_spans = _effective_self_spans(sentence)",
     "            self_spans = [(0, 1)]  # (RC1 变异:关系句一律当自曝)"),
    # ---- R7 三条(工单 §5-4:退回 R6 实现的三个轴,新锁必转红)----
    # RE1:还原裸词形自指 —— 引语内「我们」/「作者张三」重新被判自曝
    ("RE1", f"{W}content_cleaner.py",
     "            self_spans = _effective_self_spans(sentence)",
     "            self_spans = [m.span() for m in _ARTICLE_SELF_RE.finditer(sentence)]"
     "  # (RE1 变异:裸词形,不看句法角色)"),
    # RE2(R8 重锚):还原建议整句放行 —— 自曝与建议同句时整句漏出
    ("RE2", f"{W}content_cleaner.py",
     "            self_spans = _effective_self_spans(sentence)",
     "            self_spans = _effective_self_spans(sentence)\n"
     "            if _ADVICE_MOOD_RE.search(sentence):\n"
     "                kept.append(sentence)\n"
     "                continue  # (RE2 变异:建议整句放行)"),
    # RE3(R8 重锚):分句符清单砍回逗号分号 —— 冒号/顿号/破折号句连坐
    ("RE3", f"{W}content_cleaner.py",
     "_SEG_SPLIT_RE = re.compile(r'[^，,；;：:、\\s—…()（）]+[，,；;：:、\\s—…()（）]*')",
     "_SEG_SPLIT_RE = re.compile(r'[^，,；;]+[，,；;]?')  # (RE3 变异:只认逗号分号)"),
    # ---- R8 三条(宁漏勿毁的三个轴,退回必转红)----
    # RF1:硬事实闸拆除 —— removable 即摘,清单外分隔形态重新毁事实
    ("RF1", f"{W}content_cleaner.py",
     "                if removable and not _HARD_FACT_RE.search(text):",
     "                if removable:  # (RF1 变异:硬事实闸拆除)"),
    # RF2:锚定拆回「任意位置命中即自指关系」—— 实体名/叙述引用重新被毁
    ("RF2", f"{W}content_cleaner.py",
     "        if s == lead and e <= end and _REL_CONNECTIVE_RE.match(sentence, e):",
     "        if start <= s < end:  # (RF2 变异:任意位置命中即锚定)"),
    # RF3:残句保留退回「须含事实/建议」—— 非硬事实客户陈述重新被丢
    ("RF3", f"{W}content_cleaner.py",
     "            if re.sub(r'[\\s。！？，,、；;：:…—*#\\-]+', '', residual):",
     "            if residual and (_HARD_FACT_RE.search(residual)\n"
     "                             or _ADVICE_MOOD_RE.search(residual)):"
     "  # (RF3 变异:残句事实闸复活)"),
    # ---- R9 一条(工单 §4-5:粒度闸拆除必红)----
    # RG1:整句级摘除退回与从句级共用门槛 —— 清单外分隔 × 软陈述重新被毁
    ("RG1", f"{W}content_cleaner.py",
     "                if not _selfblow_covers_sentence(sentence, self_spans):",
     "                if False:  # (RG1 变异:粒度闸拆除 —— 整句与从句共用门槛)"),
    # RC2:免责改整句删(事实闸拆除,混合句事实连坐)
    ("RC2", f"{W}content_cleaner.py",
     "            residual = ''.join(\n"
     "                seg for seg in segs\n"
     "                if not (_DISCLAIMER_PRED_RE.search(seg)\n"
     "                        and not _HARD_FACT_RE.search(seg))\n"
     "            )",
     "            residual = ''  # (RC2 变异:命中免责谓词即整句删)"),
    # RC3:ags 客户事实准入门槛复活(与 Owner 亲裁反向)
    ("RC3", f"{W}article_generator_service.py",
     "——**但不因此删掉该事实**。' + chr(10)",
     "。要写进正文，必须另找具体外部来源方 + 日期；找不到就按降级阶梯收短或不写。' + chr(10)"),
    # ---- R6 三条(2026-08-12 · 判据换代)----
    # RD1:自指词族砍回只认「本文」(本报告/本评测自曝重新漏出)
    ("RD1", f"{W}content_cleaner.py",
     "_ARTICLE_SELF_RE = re.compile(\n"
     "    r'本文|本篇|本报告|本内容|本评测|本稿|本报道|这篇文章|笔者|作者|我们'\n"
     ")",
     "_ARTICLE_SELF_RE = re.compile(r'本文')  # (RD1 变异:词表砍回)"),
    # RD3:evidence_first(最高优先级契约)恢复「换事实,或不写这条」删除授权
    ("RD3", f"{W}evidence_first_policy.py",
     "客户单方材料是**主张底账**：正文不写“据企业资料／企业提供／客户资料”这类来源声明；能挂具体外部来源方 + 日期就挂，挂不上就用不需要外部归属的表达（工况区间／行业通行口径／可复算推导）——但不因此删掉该事实。",
     "客户单方材料只能在内部用于校核，**不进正文**；找不到公开信源就换信源、换事实，或不写这条。"),
    # RB3(R6 重锚):空泛「为准」保护拆除(具名业务限定「以门店测量为准」被当免责删)
    ("RB3", f"{W}content_cleaner.py",
     "    r'请?以(?:实际|最终)(?:情况|结果)?为准|'",
     "    r'请?以[^，,；;。！？\\n]{0,10}?为准|'"),
    # ---- 禁畏缩 ----
    ("MH1", f"{W}client_presence_policy.py",
     '    ("for_reference_only",\n     r"仅供参考|仅作参考|不构成(?:任何)?(?:购买|投资|决策)?建议"),',
     ''),
    ("MH2", f"{W}client_presence_policy.py",
     '            code="article_hedging_stance",\n            severity="advisory",',
     '            code="article_hedging_stance",\n            severity="hard",'),
]


def run_lock() -> tuple[bool, str]:
    env = dict(os.environ)
    if not env.get("TEST_DATABASE_URL"):
        raise SystemExit("先设 TEST_DATABASE_URL(指向名字里带 test 的测试库)")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *LOCKS, "-q", "--no-header", "-x"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    return p.returncode == 0, (p.stdout or "")[-400:]


def main() -> int:
    ok, out = run_lock()
    if not ok:
        print("❌ 基线就红,变异无意义:\n" + out)
        return 2
    print("✅ 基线绿。开始变异。\n")

    killed, survived, aborted = [], [], []
    for code, rel, old, new in MUTATIONS:
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            aborted.append((code, f"锚点命中 {src.count(old)} 次(需恰好 1)"))
            print(f"  {code} ⚠️  ABORT · 锚点命中 {src.count(old)} 次")
            continue
        try:
            path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
            green, tail = run_lock()
            if green:
                survived.append((code, rel))
                print(f"  {code} 🔴 存活 · {rel}")
            else:
                killed.append(code)
                print(f"  {code} ✅ 杀死 · {rel}")
        finally:
            path.write_text(src, encoding="utf-8", newline="")

    print(f"\n{'='*62}")
    print(f"杀死 {len(killed)}/{len(MUTATIONS)} · 存活 {len(survived)} · ABORT {len(aborted)}")
    for c, r in survived:
        print(f"  🔴 存活:{c} {r}")
    for c, r in aborted:
        print(f"  ⚠️ ABORT:{c} {r}")
    ok2, _ = run_lock()
    print(f"复位后基线:{'✅ 绿' if ok2 else '❌ 红(文件没还原干净!)'}")
    return 0 if (not survived and not aborted and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
