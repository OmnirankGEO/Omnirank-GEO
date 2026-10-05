#!/usr/bin/env python3
"""变异验证 · W1 返工(规格卡 `##` 要求 + 粗体伪标题修复器)。

每条变异 = "这个包的某一部分被撤掉 / 写歪"的具体形态,跑一遍被它覆盖的锁**必须变红**。

三类:
  · **文案类**:格式要求被换成空话、算出来但不渲染、渲染到了错位置;
  · **修复器类**:门槛闸拆掉、保守约束逐条拆掉、留痕丢掉、判据自己重写一份;
  · **接线类**:四个正文写入点逐个拆 —— 这是本包最容易"看起来做了其实没生效"的部分。
    (「判据打在接线上,不是函数上」这条在本仓已经栽过三次。)

🔴 Windows 四坑已规避(与前两个 runner 同一套):字节读写 / `-B` 禁 pyc /
   存活先分诊「锁弱 vs 空操作」/ 多行锚点按文件真实行尾对齐(`_align_eol`)。

用法:
    TEST_DATABASE_URL=... python tests/mutation_runner_heading_repair_2026_08_08.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCKS = [
    "tests/test_spec_card_real_h2_2026_08_08.py",
    "tests/test_markdown_heading_repair_2026_08_08.py",
    "tests/test_title_promise_gate_2026_08_08.py",
    "tests/test_disclosure_voice_2026_08_08.py",
    "tests/test_tier_downgrade_record_2026_08_08.py",
    "tests/test_topic_style_default_2026_08_09.py",
    "tests/test_gate2_measures_2026_08_09.py",
]

SPEC = "writing/article_type_spec_cards.py"
MHR = "writing/markdown_heading_repair.py"
GEN = "writing/article_generator_service.py"
TOOLS = "tools/article_generator.py"
PLACE = "services/placement_service.py"
WRITER = "writing/article_writer.py"
TPG = "writing/title_promise_gate.py"
SDS = "writing/source_disclosure_style.py"
SAN = "writing/body_internal_marker_sanitizer.py"
LEN = "writing/article_length_contract.py"
CC = "writing/content_cleaner.py"
CFG = "writing/config.py"
HALL = "frontend/src/pages/Writing/WritingHall.tsx"
CONTRACT = "writing/article_style_contract.py"
LINEAGE = "writing/article_lineage.py"
G2M = "writing/gate2_rewrite_measures.py"


# (编号, 说明, 目标文件, 原文, 替换, 应当变红的用例 -k 表达式)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------- 文案类:第一道 ----------------
    (
        "W01", "格式要求只剩例子、祈使句被换成空话 —— 半吊子文案",
        SPEC,
        '    "🔴 **小标题必须是真实 Markdown 的二级标题**:行首写 `## `(两个井号 + 一个空格)"\n    "再接标题文字,单独成行。**禁止用独占一行的 `**粗体**` 当小标题** —— "',
        '    "🔴 **小标题要写清楚一点**。"',
        "states_the_markdown_heading_format or validator_catches or format_rule_sits",
    ),
    (
        "W02", "格式要求算出来了但一律不渲染(经典'函数对了没接线')",
        SPEC,
        "    for names in buckets.values():\n        if HEADING_FEATURE in names:\n            return HEADING_MARKDOWN_RULE\n    return \"\"",
        "    return \"\"",
        "states_the_markdown_heading_format or validator_catches or format_rule_sits",
    ),
    (
        "W03", "渲染出来了但调用点把它丢掉",
        SPEC,
        "    _heading_rule = _heading_format_line(buckets)\n    if _heading_rule:\n        lines.append(_heading_rule)",
        "    _heading_rule = _heading_format_line(buckets)",
        "states_the_markdown_heading_format or validator_catches or format_rule_sits",
    ),
    (
        "W04", "格式要求挪到卡最末尾 —— 与它约束的分级表失去关联",
        SPEC,
        "    _heading_rule = _heading_format_line(buckets)\n    if _heading_rule:\n        lines.append(_heading_rule)\n    lines.append(\"\")\n    lines.append(_evidence_floor_line(card))",
        "    lines.append(\"\")\n    lines.append(_evidence_floor_line(card))\n    lines.append(_heading_format_line(buckets))",
        "format_rule_sits",
    ),
    (
        "W05", "validate 的元判据被删 —— 以后少写这句话没人发现",
        SPEC,
        '        if FEATURE_LABELS[HEADING_FEATURE] in block and "`## `" not in block:\n            errors.append(f"heading_feature_without_markdown_format_rule:{family_code}")',
        "        pass",
        "validator_catches",
    ),
    # ---------------- 修复器类:第二道 ----------------
    (
        "W06", "拆掉「局部」闸 —— 已经写对的文章也被改",
        MHR,
        "    if count_real_h2(text) >= MIN_H2_HEADINGS:\n        return content, []",
        "    if False:\n        return content, []",
        "already_correct_body_is_not_rewritten or article_with_enough_real_headings",
    ),
    (
        "W07", "修复器自己另写一份门槛(第二套数字)",
        MHR,
        "from .article_writer import H2_PATTERN, MIN_H2_HEADINGS",
        "from .article_writer import H2_PATTERN\n\nMIN_H2_HEADINGS: int = 6",
        "threshold_is_imported_not_redefined",
    ),
    (
        "W08", "修复器自己另写一份 H2 正则(与 H4 判定脱钩)",
        MHR,
        "def count_real_h2(content: str) -> int:\n    \"\"\"真实 `## ` 小标题的个数。**与 H4 用的是同一个正则**。\"\"\"\n    return len(H2_PATTERN.findall(content or \"\"))",
        "import re as _re2\n\nH2_PATTERN = _re2.compile(r'^##\\s+', _re2.MULTILINE)\n\n\ndef count_real_h2(content: str) -> int:\n    return len(H2_PATTERN.findall(content or \"\"))",
        "h2_pattern_is_shared_with_the_validator",
    ),
    (
        "W09", "保守约束全拆:什么粗体行都当标题",
        MHR,
        "    stripped = text.strip()\n    if not stripped:\n        return False",
        "    stripped = text.strip()\n    return bool(stripped)\n    if not stripped:\n        return False",
        "conservative_cases_are_not_touched",
    ),
    (
        "W10", "长度上限拆掉 —— 整段被加粗的句子也当标题",
        MHR,
        "    if len(stripped) > MAX_HEADING_CHARS:\n        return False",
        "    pass",
        "conservative_cases_are_not_touched",
    ),
    (
        "W11", "冒号/句末标点判定拆掉 —— `**结论:**` 变成小标题",
        MHR,
        "    if stripped.endswith(_NON_HEADING_TAIL):\n        return False",
        "    pass",
        "conservative_cases_are_not_touched",
    ),
    (
        # 注:前两版都是空操作,原因值得记下来 —— **「整行」这条约束被独立地防了两道**:
        # 正则的 `^...$` 和调用侧的 `.match()`(单行串上 `.match` 天然锚在串首)。
        # 单点改任何一道,另一道还在,行为不变。真形态是「正则显式放行列表项前缀」。
        "W12", "正则显式放行列表项前缀 —— `- **要点**:` 被误当小标题",
        MHR,
        "_WHOLE_LINE_BOLD: Final = re.compile(r'^\\s*\\*\\*(?P<text>[^*].*?)\\*\\*\\s*$')",
        "_WHOLE_LINE_BOLD: Final = re.compile(r'^\\s*[-\\d.]*\\s*\\*\\*(?P<text>[^*].*?)\\*\\*')",
        "conservative_cases_are_not_touched",
    ),
    (
        "W13", "代码围栏不再跳过 —— 示例代码被改坏",
        MHR,
        "        if _FENCE.match(line):\n            in_fence = not in_fence\n            seen_first_nonblank = True\n            continue",
        "        if False:\n            in_fence = not in_fence\n            seen_first_nonblank = True\n            continue",
        "code_fence_contents_are_never_touched",
    ),
    (
        "W14", "文档首行不再让给 H1 —— 与标题归一化抢同一行",
        MHR,
        "        if not seen_first_nonblank:\n            # 文档第一个非空行 = H1 领地,交给标题归一化,这里一律不碰。\n            seen_first_nonblank = True\n            continue",
        "        seen_first_nonblank = True",
        "first_nonblank_line_is_title_territory",
    ),
    (
        "W15", "改了正文但不留痕(静默改写)",
        MHR,
        '        warning["heading_repair"] = note',
        "        pass",
        "repair_is_recorded_not_silent",
    ),
    # ---------------- 接线类:四个写入点 ----------------
    (
        "W16", "保存路径 1/3 的接线被删",
        GEN,
        "            repair_article_for_save(_lineage_article)\n",
        "",
        "save_path_persists_real_markdown_headings or save_path_repairs_before_lineage "
        "or persisted_body_clears or repair_is_recorded_not_silent",
    ),
    (
        "W17", "保存路径 2/3(rewrite)的接线被删",
        GEN,
        "        repair_article_for_save(article)\n",
        "",
        "rewrite_path_persists_real_markdown_headings or rewrite_path_repairs_before_lineage",
    ),
    (
        "W18", "保存路径 3/3(补发链)的接线被删",
        TOOLS,
        "        repair_article_for_save(_save_article)\n",
        "",
        "replacement_path_repairs_before_lineage",
    ),
    (
        "W19", "第 4 个写入点(一键修复)的接线被删",
        PLACE,
        "            fixed_content, _repaired_headings = repair_pseudo_headings(fixed_content)",
        "            _repaired_headings = []",
        "placement_kb_fix_path_repairs_before_update",
    ),
    (
        "W20", "保存链入口把修复结果丢掉(改了但不写回 article)",
        MHR,
        '        article["content"] = repaired_text',
        "        pass",
        "save_path_persists_real_markdown_headings or persisted_body_clears "
        "or rewrite_path_persists_real_markdown_headings",
    ),
    (
        # 注:第一版是「把常量 6 改成 2」,那是**空操作** —— 锁全部按符号写,
        # 改常量值锁跟着变(而且门槛本来就该是可调的)。真正的风险是**判定器另写一个数**,
        # 于是"修复器把小标题补到自己的门槛"与"H4 要求的门槛"对不上。
        "W21", "H4 判定另写一个数(8),与修复器用的门槛脱钩",
        WRITER,
        "    if h2_count < MIN_H2_HEADINGS:",
        "    if h2_count < 8:",
        "persisted_body_clears",
    ),
    # ---------------- `###` 提级(真跑实证逼出来的那一支) ----------------
    (
        "W22", "混合层级保护拆掉 —— 作者选的 `##`+`###` 结构被压平",
        MHR,
        "    if count_real_h2(body) > 0:\n        return content, 0  # 有混合层级,不碰",
        "    if False:\n        return content, 0",
        "mixed_heading_hierarchy_is_never_flattened",
    ),
    (
        "W23", "`###` 条数门槛拆掉 —— 两三个注解也被当成一整层结构提级",
        MHR,
        "    if h3_count < MIN_H2_HEADINGS:\n        return content, 0",
        "    if h3_count < 1:\n        return content, 0",
        "too_few_h3_is_left_alone",
    ),
    (
        "W24", "保存链只修伪标题、不管孤儿 `###`(修一半)",
        MHR,
        "        repaired_text, promoted = promote_orphan_h3(repaired_text)",
        "        promoted = 0",
        "save_chain_records_h3_promotion",
    ),
    # ---------------- ④ 标题兑现闸 ----------------
    (
        "W25", "兑现闸对「兑现不了」也放行(闸恒不触发)",
        TPG,
        "    if delivered >= promised:\n        return title, None",
        "    if True:\n        return title, None",
        "promise_unmet_rewrites_and_records or save_path_persists_the_rewritten_title "
        "or persisted_h1_matches",
    ),
    (
        "W26", "兑现闸对「已兑现」也改标题(写实了反被罚)",
        TPG,
        "    if delivered >= promised:\n        return title, None",
        "    if False:\n        return title, None",
        "promise_met_is_not_punished or save_path_leaves_a_met_promise_alone",
    ),
    (
        "W27", "不知道兑现几家时也照改(错改比漏改更坏)",
        TPG,
        "    if verified_entity_count is None:\n        return title, None",
        "    if verified_entity_count is None:\n        verified_entity_count = 0",
        "unknown_delivered_count_changes_nothing or without_verified_count_is_inert",
    ),
    (
        "W28", "多处承诺时按最弱那句判(放行了最强的那句)",
        TPG,
        "            if best is None or n > best[0]:",
        "            if best is None or n < best[0]:",
        "multiple_promises_take_the_strongest",
    ),
    (
        "W29", "白名单长度不再产出 → 闸在生产上永远惰性",
        GEN,
        '                "verified_entity_count": len(_name_whitelist or []),\n',
        "",
        "generate_single_reports_the_verified_count",
    ),
    (
        "W30", "保存路径的兑现闸挪到 H1 归一化之后(H1 还是旧标题)",
        GEN,
        # 锚点随 W1 返工 ① 更新:闸后多了一句「把正文取回来」。
        "            _raw_title, _promise_note = _apply_title_promise_gate(\n"
        "                _raw_title, article, where=\"_save_article\",\n"
        "            )\n"
        "            _content = article.get('content', _content)\n"
        "            _article_title, _content = _normalize_article_title_and_h1(_raw_title, _content)",
        "            _article_title, _content = _normalize_article_title_and_h1(_raw_title, _content)\n"
        "            _article_title, _promise_note = _apply_title_promise_gate(\n"
        "                _article_title, article, where=\"_save_article\",\n"
        "            )\n"
        "            _content = article.get('content', _content)",
        "save_path_repairs_before_lineage or all_three_save_paths_run_the_gate "
        "or persisted_h1_matches",
    ),
    (
        "W31", "补发链 INSERT 又自己拼一次标题(闸被绕过 · 真出口锁)",
        TOOLS,
        '            _save_article["title"],\n            content,',
        '            f"[补发] {safe_title}",\n            content,',
        "replacement_insert_reads_the_gated_title",
    ),
    # ---------------- ③ 披露话术 ----------------
    (
        "W32", "标签表回到审计腔",
        SDS,
        '    "project": "项目资料",\n    "price_contract": "报价与合同",',
        '    "project": "项目资料｜待抽样复核",\n    "price_contract": "报价/合同｜待逐项确认",',
        "table_and_prose_labels_carry_no_audit_status or polish_source_disclosure_emits",
    ),
    (
        "W33", "表格列名回到「核验依据」",
        SDS,
        'TABLE_SOURCE_HEADER: Final = "资料来源"',
        'TABLE_SOURCE_HEADER: Final = "核验依据"',
        "table_and_prose_labels_carry_no_audit_status or source_disclosure_prompt_forbids",
    ),
    (
        "W34", "🔴 一次性声明改成 evidence_first 认不出来的说法(凭空造返工噪音)",
        SDS,
        '    "以下企业相关信息来自企业提供的业务资料,并标注了各自的资料来源。"',
        '    "资料来源见文中标注。"',
        "enterprise_declaration_is_still_recognised",
    ),
    (
        "W35", "清洗器不认披露话术的变体(v3 的原盲区)",
        SAN,
        '    "待交叉核验", "待抽样复核", "待核原件", "待逐项确认", "待另行确认",\n',
        "",
        "sanitizer_strips_disclosure_variants",
    ),
    (
        "W36", "来源区分被压平(把披露一起删掉了)",
        SDS,
        '    "qualification": "资质文件",\n    "project": "项目资料",',
        '    "qualification": "企业资料",\n    "project": "企业资料",',
        "source_kind_distinction_survives",
    ),
    (
        "W37", "提示词又回去要求「标为待核验」",
        GEN,
        "证据不足时缩小结论范围并写清适用条件，不写“待核验”这类内部审核状态，也不做绝对化名次。",
        "证据不足时标为待核验，不做绝对化名次。",
        "active_generation_prompts_do_not_demand_pending_labels",
    ),
    # ---------------- 顺手项:降档记录 ----------------
    (
        "W38", "降档记录恒 False(字段在但永远不报)",
        LEN,
        '    downgraded = bool(tier == "deep" and floor and chars < floor)',
        "    downgraded = False",
        "deep_plan_short_delivery_is_recorded_as_downgraded",
    ),
    (
        "W39", "降档记录恒 True(达标的深文也被记一笔)",
        LEN,
        '    downgraded = bool(tier == "deep" and floor and chars < floor)',
        '    downgraded = bool(tier == "deep")',
        "deep_plan_met_is_not_recorded_as_downgraded",
    ),
    (
        "W40", "下限另写一个数(与合同的 deep_output_floor 脱钩)",
        LEN,
        '    floor = deep_output_floor(planned_target) if tier == "deep" else 0',
        '    floor = 14000 if tier == "deep" else 0',
        "floor_is_the_contract_floor_not_a_second_number",
    ),
    (
        "W41", "降档记录没进返回字典顶层(落不了库)",
        LEN,
        '        "tier_downgrade": _tier_downgrade(tier, planned_target, chars),\n',
        "",
        "record_rides_along_into_quality_warning",
    ),
    # ---------------- 复审定点返工两处 ----------------
    (
        "W42", "content_cleaner 退回字面量拷贝(子串巧合病复发)",
        CC,
        "            _sds.labeled('price_contract'),",
        "            '资料来源：报价与合同',",
        "three_post_processors_share_one_label_table or consumers_hold_no_label_literal",
    ),
    (
        "W43", "generator_service 退回字面量拷贝",
        GEN,
        '        "公司定价文件": _L("price_contract"),',
        '        "公司定价文件": "资料来源：报价与合同",',
        "three_post_processors_share_one_label_table or consumers_hold_no_label_literal",
    ),
    (
        "W44", "🔴 改 SSOT 的值(裁定点名:值一变必须有锁转红)",
        SDS,
        '    "project": "项目资料",',
        '    "project": "项目佐证材料",',
        "ssot_values_are_pinned",
    ),
    (
        "W45", "SSOT 的列名被改(同上,另一维)",
        SDS,
        'TABLE_SOURCE_HEADER: Final = "资料来源"',
        'TABLE_SOURCE_HEADER: Final = "资料依据"',
        "ssot_values_are_pinned",
    ),
    (
        "W46", "config 降级 prompt 退回「待核验」祈使句(补发链会真拼进 prompt)",
        CFG,
        "- 无独立证据时缩小结论范围、写清适用条件与限制，不写“待核验”这类内部审核状态，也不得补造数据",
        "- 无独立证据时明确写“待核验”，不得补造数据",
        "fallback_content_angle_prompt_has_no_pending_label",
    ),
    # ---------------- W1 返工(2026-08-09)四条裁定 ----------------
    # 🔴 W47/W48 是本批最要紧的两条:上一版 35 条锁全绿,漏的就是这里。
    #    它们撤掉的不是任何函数,是**闸与保存路径之间的两根线**。
    (
        "W47", "🔴 闸之后不把正文取回来(闸对正文空转 —— 上一版的真病灶)",
        GEN,
        "            _content = article.get('content', _content)\n",
        "",
        "persisted_body_has_no_promise_echo_left or feeds_the_gate_the_sanitized_body",
    ),
    (
        "W48", "🔴 闸之前不把清洗后的正文写回(闸在旧正文上改,改完被丢)",
        GEN,
        "            article['content'] = _content\n            _raw_title, _promise_note = _apply_title_promise_gate(",
        "            _raw_title, _promise_note = _apply_title_promise_gate(",
        "persisted_body_has_no_promise_echo_left or feeds_the_gate_the_sanitized_body",
    ),
    (
        "W49", "裸「前+数字」兜底复活(「前十分钟」→「多家分钟」)",
        TPG,
        'rf"前\\s*(?P<n>\\d+|{_CN_ALT})(?![0-9])"\n        rf"(?=\\s*(?:的?\\s*{_ENTITY_NOUN}|[{_CLOSERS}]?\\s*(?:$|[{_SENTENCE_END}])))"',
        'rf"前\\s*(?P<n>\\d+|{_CN_ALT})(?![0-9])"',
        "time_and_period_phrases_are_not_quantity_promises",
    ),
    (
        "W50", "收窄过头:只认实体名词、丢掉句末形态(真标题「…公司前十」漏判)",
        TPG,
        'rf"(?=\\s*(?:的?\\s*{_ENTITY_NOUN}|[{_CLOSERS}]?\\s*(?:$|[{_SENTENCE_END}])))"',
        'rf"(?=\\s*的?\\s*{_ENTITY_NOUN})"',
        "narrowed_pattern_still_catches_real_promises",
    ),
    (
        "W51", "句读字符类退回字面量(全角逗号活不过编辑链路 —— 我自己刚踩过)",
        TPG,
        '    "\\n\\u3002\\uff0c\\u3001\\uff1b\\uff1a\\uff01\\uff1f"   # 。，、；：！?',
        '    "\\n。，、；：！？"',
        "sentence_punctuation_class_is_written_as_codepoints",
    ),
    (
        "W52", "留痕造假:没改正文也落 `body_echo_rewritten` 键",
        GEN,
        "        if echo_notes:\n            note[\"body_echo_rewritten\"] = echo_notes",
        "        note[\"body_echo_rewritten\"] = echo_notes",
        "no_echo_note_when_nothing_was_rewritten",
    ),
    (
        "W53", "小标题又被补上「怎么选」尾巴(「## 入选标准怎么选」)",
        TPG,
        "strip_quantity_promise(body, add_task_cue=False)",
        "strip_quantity_promise(body)",
        "body_echo_is_downgraded_in_headings_and_prose",
    ),
    # ---------------- Owner 新功能(体裁默认)两条锁 ----------------
    (
        "W54", "🔴 请求体不带 user_choices(接线没接 —— 本仓第四例就在 P4)",
        HALL,
        "                    user_choices: userChoicesMap,\n",
        "",
        "request_body_carries_user_choices",
    ),
    (
        "W55", "map 改回调用点内联构造(没手动选过的选题一个都进不了请求体)",
        HALL,
        "            const userChoicesMap = buildUserChoicesMap(topics, writableIds);",
        "            const userChoicesMap: Record<number, string> = {};",
        "user_choices_map_is_built_by_the_shared_builder",
    ),
    (
        "W56", "下拉框默认值退回旧字段组合(显示「系统推荐」却发别的值)",
        HALL,
        # 三个调用点写的是同一句 —— 锚点必须带上前一行才唯一(命中 3 次会被记作未应用)。
        "topicId={topic.id}\n"
        "                                                                                    "
        "currentValue={effectiveUserChoice(topic)}",
        "topicId={topic.id}\n"
        "                                                                                    "
        "currentValue={normalizeUserChoice(topic.user_choice || topic.style_family)}",
        "badge_dropdown_and_request_read_the_same_source",
    ),
    (
        "W57", "展示标签压过用户的显式选择(自由度被吃掉)",
        HALL,
        "    const explicit = normalizeUserChoice(t.user_choice);\n"
        "    if (explicit !== 'auto') return explicit;              // ① 用户说了算(保留自由度)\n"
        "    return userChoiceFromDisplayedStyle(displayedStyleSource(t));  // ② 展示什么默认什么",
        "    const shown = userChoiceFromDisplayedStyle(displayedStyleSource(t));\n"
        "    if (shown !== 'auto') return shown;\n"
        "    return normalizeUserChoice(t.user_choice);",
        "effective_choice_prefers_explicit_user_selection",
    ),
    (
        "W58", "auto 也被塞进请求体(行业配比抽签那条路被堵死)",
        HALL,
        "        if (choice !== 'auto') map[t.id] = choice;",
        "        map[t.id] = choice;",
        "builder_falls_back_to_industry_lottery_when_no_label",
    ),
    (
        "W59", "又抄了一张 style_code → user_choice 的手抄表",
        HALL,
        "    const human = humanizeStyle(raw || '');",
        "    const human = ({ ranking_v2: '榜单', buying_guide: '选购' } as Record<string, string>)[raw || ''] || '';",
        "derivation_has_no_third_mapping_table",
    ),
    (
        "W60", "中文 article_style 别名又归不了族(9 个存量别名静默退回抽签)",
        CONTRACT,
        "    from writing.style_registry import normalize_style_code\n\n"
        "    canonical = normalize_style_code(raw)\n"
        "    return LEGACY_STYLE_TO_FAMILY.get(canonical or \"\")",
        "    return None",
        "chinese_article_style_aliases_resolve_to_a_family or "
        "frontend_default_agrees_with_backend_family",
    ),
    (
        "W61", "别名在本模块又抄了一张手抄表(第四张映射)",
        CONTRACT,
        "    from writing.style_registry import normalize_style_code\n\n"
        "    canonical = normalize_style_code(raw)",
        "    canonical = {'问答FAQ': 'qa_recommendation', '趋势洞察': 'risk_compliance',\n"
        "                 '公司深度报道': 'company_profile'}.get(raw)",
        "alias_resolution_reuses_the_existing_registry_ssot",
    ),
    # ---------------- Gate-2 改写措施全量落地包(2026-08-09) ----------------
    (
        'W62', '🔴 措施块算出来但不拼进 system_prompt(第六次「函数对了没接线」)',
        GEN,
        '                system_prompt = system_prompt + "\\n\\n" + _gate2_block\n',
        '',
        'generation_path_injects_the_block or insert_param_carries_the_measure_tag',
    ),
    (
        'W63', '🔴 lineage 把 injected 写死 True(给归因喂假数据)',
        LINEAGE,
        '            injected=bool(\n                topic.get("_gate2_injected")\n                or article.get("_gate2_injected")\n            ),',
        '            injected=True,',
        'tag_is_honest_when_the_block_was_not_injected or lineage_reads_the_injected_flag',
    ),
    (
        'W64', '打标整段被删(INSERT 参数里就没有 gate2_measures)',
        LINEAGE,
        '        request_snapshot["gate2_measures"] = build_gate2_measure_tag(',
        '        _unused_gate2 = build_gate2_measure_tag(',
        'insert_param_carries_the_measure_tag',
    ),
    (
        'W65', 'G1 判据只数行数,不管这块档案是不是这家客户的',
        G2M,
        '            if brand in "\\n".join(scope):\n                return True',
        '            return True',
        'g1_needs_a_contiguous_field_block',
    ),
    (
        'W66', 'G3 判不了时报 False 而不是 n/a(把无解说成没做)',
        G2M,
        '    if not any(_ADMIN_SUFFIX.search(s) for s in sents):\n        return None',
        '    if not any(_ADMIN_SUFFIX.search(s) for s in sents):\n        return False',
        'g3_reports_not_applicable',
    ),
    (
        'W67', '🔴 给弱依据措施上硬判据(把未证实方向固化成硬约束)',
        G2M,
        '    "G6": lambda t, c, b: _check_g6_brand_in_comparison(c, b),\n}',
        '    "G6": lambda t, c, b: _check_g6_brand_in_comparison(c, b),\n    "G8": lambda t, c, b: True,\n}',
        'weak_measures_have_no_hard_product_gate or measure_set_is_self_consistent',
    ),
    (
        'W68', '措施集漏掉交付单里的一条(K1 没进写作链)',
        G2M,
        '        source_measures=("K1", "F1"),',
        '        source_measures=("F1",),',
        'every_source_measure_from_the_delivery_is_covered',
    ),
    (
        'W69', '内部标志又塞回 client_data(泄进喂给模型的 client_profile)',
        TOOLS,
        '        "brand_id": diagnosis.get("brand_id"),\n        "company_name"',
        '        "brand_id": diagnosis.get("brand_id"),\n        "_gate2_injected": _gate2_injected_fallback,\n        "company_name"',
        'fallback_does_not_leak_internal_flags',
    ),
    (
        'W70', '`_gate2_injected` 默认值被删(异常路径漏记)',
        GEN,
        '        topic["_gate2_injected"] = False\n',
        '',
        'generation_path_injects_the_block',
    ),
    (
        'W71', '补发降级链不注入(那批稿成为没打标的空白)',
        TOOLS,
        '            angle_instruction = (angle_instruction or "") + "\\n\\n" + _g2_block\n',
        '',
        'fallback_replacement_chain_injects_too',
    ),
    (
        'W72', '🔴 G1 判据只认字段行、不认表格形态(生产真实形态会恒假)',
        G2M,
        '    m = _TABLE_ROW.match(line)\n    return bool(m and m.group("a").strip() and m.group("b").strip())',
        '    return False',
        'g1_accepts_the_markdown_table_form',
    ),
]


def _align_eol(fragment: str, text: str) -> str:
    """把锚点/替换文本的行尾对齐到目标文件的行尾(坑 4)。"""
    if "\r\n" in text and "\r\n" not in fragment:
        return fragment.replace("\n", "\r\n")
    return fragment


def _run(expr: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *LOCKS, "-q", "--no-header",
         "-p", "no:randomly", "-k", expr],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=1800,
    )
    return proc.returncode == 0, (proc.stdout or "")[-800:]


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("[ABORT] 需要 TEST_DATABASE_URL(conftest 在 import 业务代码前就要它)")
        return 2

    originals = {rel: (ROOT / rel).read_bytes() for rel in {m[2] for m in MUTATIONS}}
    covered = " or ".join(sorted({m[5] for m in MUTATIONS}))
    green, tail = _run(covered)
    if not green:
        print("[ABORT] 基线就是红的,变异结论无意义\n" + tail)
        return 2
    print("✅ 基线绿(被覆盖的用例集)\n")

    killed, survived = 0, []
    for tag, desc, rel, old, new, expr in MUTATIONS:
        path = ROOT / rel
        text = originals[rel].decode("utf-8")
        old_a = _align_eol(old, text)
        new_a = _align_eol(new, text)
        hits = text.count(old_a)
        if hits != 1:
            print(f"⚠️  {tag} 锚点命中 {hits} 次(预期 1)—— 变异未应用,记作存活")
            survived.append((tag, desc, f"锚点命中 {hits} 次,变异未应用"))
            continue
        mutated = text.replace(old_a, new_a, 1)
        if mutated == text:
            print(f"⚠️  {tag} 变异后与原文逐字相同 —— 空操作,记作存活")
            survived.append((tag, desc, "空操作变异"))
            continue
        try:
            path.write_bytes(mutated.encode("utf-8"))
            ok, tail = _run(expr)
        finally:
            path.write_bytes(originals[rel])
        if ok:
            print(f"❌ {tag} 存活 —— {desc}\n{tail}\n")
            survived.append((tag, desc, "锁没抓到"))
        else:
            killed += 1
            print(f"✅ {tag} 被杀 —— {desc}")

    print(f"\n===== {killed}/{len(MUTATIONS)} 被杀 =====")
    for tag, desc, why in survived:
        print(f"存活 {tag}:{desc} · {why}")
    return 0 if not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
