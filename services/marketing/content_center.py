"""Pure contracts and LLM seams for the GEO acquisition content center."""
from __future__ import annotations

import json
import inspect
import re
from datetime import datetime, timezone
from typing import Optional

from services.marketing import guards, product_facts
from services.marketing.content_angles import ANGLE_POOL, normalize_angle
from services.marketing.guards import scan_forbidden
from services.marketing.prompt_composer import VisualBrief, VisualPromptComposer


CHANNEL_CONTRACTS: dict[str, dict] = {
    "professional_poster": {
        "label": "专业海报",
        "ratio": "3:4",
        "image_slots": [{"slot": "professional_poster", "size": "3:4"}],
        "copy_fields": ["title", "body", "cta", "tags"],
        "boundary": "成熟 B2B 海报；一个标题、一个事实角度、一个行动。",
    },
    "moments": {
        "label": "朋友圈",
        "ratio": "1:1",
        "image_slots": [{"slot": "moments", "size": "1:1"}],
        "grid_image_slots": [
            {"slot": f"moments_grid_{index}", "size": "1:1"} for index in range(1, 10)
        ],
        "copy_fields": ["title", "tones", "tags"],
        "boundary": "输出克制、专业、朋友式三种语气；不硬销、不公开报价。",
    },
    "xiaohongshu": {
        "label": "小红书",
        "ratio": "3:4",
        "image_slots": [
            {"slot": "xhs_cover", "size": "3:4"},
            {"slot": "xhs_card_1", "size": "3:4"},
            {"slot": "xhs_card_2", "size": "3:4"},
        ],
        "copy_fields": ["title", "body", "tags", "card_outline"],
        "boundary": "封面与卡片原生编排；标题清楚，不伪装亲历或客户评价。",
    },
    "douyin": {
        "label": "抖音",
        "ratio": "9:16",
        "image_slots": [{"slot": "douyin_cover", "size": "9:16"}],
        "copy_fields": ["title", "script", "shots", "tags"],
        "boundary": "30–60 秒；一个具体服务商人群、一个卖点、一个私域行动。",
    },
    "private_chat": {
        "label": "私聊邀约",
        "ratio": None,
        "image_slots": [{"slot": "private_chat_scene", "size": "9:16"}],
        "copy_fields": ["opening", "follow_up", "objection_reply"],
        "boundary": "低压力邀约；先给诊断价值，不裸报价，不伪造客户对话；聊天示意图必须显著标注‘示例对话’。",
    },
    "infographic": {
        "label": "信息图",
        "ratio": "3:4",
        "image_slots": [{"slot": "infographic", "size": "3:4"}],
        "copy_fields": ["title", "body", "tags"],
        "boundary": "单图信息图；只呈现可核验事实与步骤，没有证据的数字不上图。",
    },
    "diagnosis_case": {
        "label": "诊断案例卡",
        "ratio": "3:4",
        "image_slots": [{"slot": "diagnosis_case", "size": "3:4"}],
        "copy_fields": ["title", "facts", "source_note", "cta", "tags"],
        "boundary": "只使用冻结诊断/监测事实；没有证据时按常青口径生成，不写诊断数字。",
    },
    "deal_poster": {
        "label": "喜报海报",
        "ratio": "3:4",
        "image_slots": [{"slot": "deal_poster", "size": "3:4"}],
        "copy_fields": ["title", "body", "tags"],
        "boundary": "只呈现客户确认单里的成交事实；不夸大金额、不伪造客户名与订单号。",
    },
    "deal_chat": {
        "label": "聊天晒单",
        "ratio": "9:16",
        "image_slots": [{"slot": "deal_chat_scene", "size": "9:16"}],
        "copy_fields": ["title", "body", "tags"],
        "boundary": "基于已打码确认的真实聊天素材；聊天界面必须显著标注‘示例对话’；不得还原被遮挡信息。",
    },
    "deal_data_card": {
        "label": "数据卡片",
        "ratio": "3:4",
        "image_slots": [{"slot": "deal_data_card", "size": "3:4"}],
        "copy_fields": ["title", "facts", "body", "tags"],
        "boundary": "只呈现确认单中已确认的成交数字；待确认的数字不上卡。",
    },
    "deal_story": {
        "label": "签约故事长图",
        "ratio": "9:16",
        "image_slots": [{"slot": "deal_story", "size": "9:16"}],
        "copy_fields": ["title", "body", "tags"],
        "boundary": "按确认单讲完整签约过程；时间、金额、行业与确认单一致，不添油加醋。",
    },
    "deal_feedback_card": {
        "label": "反馈卡片",
        "ratio": "3:4",
        "image_slots": [{"slot": "deal_feedback_card", "size": "3:4"}],
        "copy_fields": ["title", "body", "tags"],
        "boundary": "客户认可点逐字来自确认单可用原话；不伪造客户评价。",
    },
}

DEAL_CHANNELS = frozenset({
    "deal_poster", "deal_chat", "deal_data_card", "deal_story", "deal_feedback_card",
})

MOMENTS_LAYOUTS = ("single", "grid")

# 视觉风格白名单(与前端 VISUAL_STYLES 同键):选择冻结进 geo_snapshot,
# build_visual_brief 按冻结值装配,不在服务端硬编码。
VISUAL_STYLES: dict[str, str] = {
    "business": "商务简洁的成熟中文 B2B 品牌视觉；浅色干净、留白克制、真实业务画面",
    "bright": "明亮活泼的中文消费品牌视觉；高亮通透、色彩轻快、积极向上的真实场景",
    "tech_dark": "Linear 风格的成熟中文 SaaS 品牌视觉；深色、清晰、克制、真实业务画面",
    "festive": "喜庆红金的中文营销视觉；红色主调、金色点缀、成交喜报氛围、真实业务画面",
}
DEFAULT_VISUAL_STYLE = "tech_dark"


def normalize_visual_style(value: Optional[str]) -> str:
    style = str(value or DEFAULT_VISUAL_STYLE).strip().lower()
    if style not in VISUAL_STYLES:
        raise ValueError("visual_style_invalid")
    return style

QUICK_TASKS = {
    "promote_geo": "推广自己的 GEO 服务",
    "diagnosis_case": "发布一份真实诊断案例",
    "explain_geo": "向还不了解 GEO 的老板解释什么是 GEO",
    "invite_trial": "邀请潜在客户体验一次 GEO 诊断",
    "showcase_deal": "晒出一笔真实成交",
}


_NEGATED_BOUNDARY_RE = re.compile(
    r"(?:不愿意|不|无|拒绝|没有)(?:做出|作出|提供|接受)?(?:任何)?(?:结果|排名|收益)?(?:保证|承诺)"
)


def scan_geo_claims(text: str) -> dict:
    """Keep explicit no-promise language while still blocking positive claims."""
    normalized = _NEGATED_BOUNDARY_RE.sub("明确服务边界", str(text or ""))
    return scan_forbidden(normalized)


# ---------------------------------------------------------------------------
# 合规口径(Owner 2026-07-22 裁决,最高优先级):
#   只有法律明令禁止的内容硬拦截(errors)——广告法违禁极限词、违法内容;
#   凡有解释空间的一律放行,系统职责是醒目提醒(warnings),不是阻断。
# 功能正确性检查(QR payload/hash 一致、EXIF 剥离、数据隔离、属主校验)
# 不是合规审查,保持硬约束,不在本表。
# ---------------------------------------------------------------------------
WARNING_MESSAGES: dict[str, str] = {
    # —— 文案 QA(claim_evidence_qa)——
    "forbidden_claim": "文案含承诺/保证类或措辞偏强（如「唯一/领先」）表述，请确认都能兑现再发布",
    "number_without_evidence": "文案里的数字没有对应证据来源，发布前请核对",
    "testimonial_without_evidence": "文案包含客户评价类表述，但没有对应证据，请确认其真实性",
    "unlabelled_simulated_chat": "聊天示意内容建议显著标注「示例对话」，避免被当成真实记录",
    "contact_forbidden_when_none": "联系方式已关闭，但文案里出现了联系方式类内容，请确认是否需要",
    "explicit_contact_missing": "已开启联系方式，但文案里没有出现你填写的联系方式",
    # —— 视觉 QA(visual_qa)——
    "brand_name_missing": "成图里没有出现品牌名，确认是否需要品牌露出",
    "brand_name_forbidden_when_anonymous": "匿名晒单的成图里疑似出现了品牌名，请检查后再发布",
    "exact_copy_missing": "成图上的文字与文案不完全一致，请核对",
    "visual_number_without_evidence": "成图上出现了没有证据来源的数字，请核对",
    "contact_visible_when_disabled": "联系方式已关闭，但成图里疑似出现联系方式/二维码，请检查",
    "contact_text_missing": "成图里没有出现你填写的联系方式",
    # —— 策略/渠道/草稿层 ——
    "strategy_promise_claim": "策略里有承诺/保证类表述，请确认都能兑现",
    "diagnosis_case_requires_frozen_evidence": "案例卡引用的诊断数据建议先发布诊断报告；本版已按常青口径生成",
    "ai_extraction_beyond_materials": "AI 提取的内容超出了你提供的材料，请核对",
    "redaction_manual_review_suggested": "素材疑似有隐私内容但未能自动定位，建议人工复核一遍打码",
    "unsigned_catalog_advisory": "法律禁止目录包暂时不可用，相关词项只作提醒；请人工核对，并联系平台恢复签发目录",
    "ai_copy_legal_blocked_fallback": "AI 生成的文案含法律红线词，已拦截并改用兜底口径文案",
}

# 法律红线硬拦截(error)人话原因:仅广告法违禁极限词/违法内容走这层。
ERROR_MESSAGES: dict[str, str] = {
    "forbidden_claim": "文案含广告法违禁极限词或违法内容，必须删除后才能生成",
    "strategy_contains_forbidden_claim": "文案含广告法违禁极限词或违法内容，请改掉后再提交",
    "asset_copy_edit_rejected": "改后的文案含广告法违禁极限词或违法内容，请改掉后再保存",
    # —— 功能正确性硬失败(非合规审查,保持原有硬约束)——
    "image_decode_failed": "图片文件损坏或无法读取",
    "channel_dimensions_invalid": "成图尺寸与渠道要求不一致",
    "visual_qa_unavailable": "视觉质检服务暂时不可用，成图未交付",
    "qr_payload_mismatch_or_unreadable": "成品二维码与你上传的二维码不一致，成品不可用",
    "qr_decoder_unavailable": "二维码解码服务不可用",
    "invalid_qr_image": "二维码图片无法读取",
    "internal_lineage_visible": "成图包含内部标识信息，已拦截",
    "live_authority_revoked_after_provider": "品牌授权已撤销，成图未交付",
    # 🔴 [#113] 租约被抢**不是**授权撤销 —— 她的授权好好的。
    #    文案不许说授权,也不许让她以为是自己的问题。
    "worker_lease_lost_after_provider": "这次生成被中断了(系统侧任务交接失败)，成图未交付，算力不会白扣",
    "live_authority_check_failed_after_provider": "这次生成被中断了，成图未交付，算力不会白扣",
}


# ---------------------------------------------------------------------------
# 审核提示五问合同(SSOT §6,2026-07-23 Owner 签发):
#   任何审核类错误/提醒必须同时回答 ① 哪处有问题(message)② 为什么影响交付
#   (reason)③ AI 可以局部修什么(repair_hint)④ 有权限的人如何继续(actions)
#   ⑤ 规则版本(rule_version = legal_pack_version 或 qa_rules_version;随审计
#   marketing_events 落五要素)。只有失败码、红色整行、无处理按钮一律 NO-GO。
# ---------------------------------------------------------------------------
QA_RULES_VERSION = "geo-content-qa/1.0.0"

# 规则版本归属:法律红线类条目透法律禁止目录包版本,其余透 QA 规则版本。
_LEGAL_RULE_CODES = frozenset({
    "forbidden_claim", "strategy_contains_forbidden_claim", "asset_copy_edit_rejected",
})


def _rule_version_for(code: str) -> str:
    if code in _LEGAL_RULE_CODES:
        return guards.legal_pack_version()
    return QA_RULES_VERSION


WARNING_REASONS: dict[str, str] = {
    "forbidden_claim": "承诺/保证或措辞偏强的表述兑现不了时，会直接损害这份内容的可信度和交付验收",
    "number_without_evidence": "没有证据来源的数字发布出去无法自证，客户质疑时内容站不住",
    "testimonial_without_evidence": "客户评价类表述没有证据会被当成伪造口碑，影响交付",
    "unlabelled_simulated_chat": "聊天示意不标注「示例对话」容易被当成真实记录",
    "contact_forbidden_when_none": "当前设置不展示联系方式，文案里出现联系方式与设置不一致",
    "explicit_contact_missing": "已开启联系方式但文案没写，成品会丢失转化入口",
    "brand_name_missing": "成图没有品牌名，品牌露出缺失会影响传播归因",
    "brand_name_forbidden_when_anonymous": "匿名晒单出现品牌名会违背匿名承诺",
    "exact_copy_missing": "成图文字与文案不一致，客户按文案核对时会对不上",
    "visual_number_without_evidence": "图上出现没有证据来源的数字，发布前需要核对来源",
    "contact_visible_when_disabled": "设置不展示联系方式但图上疑似出现，与设置冲突",
    "contact_text_missing": "图上没有你填写的联系方式，转化入口丢失",
    "strategy_promise_claim": "策略里的承诺/保证或措辞偏强表述会传导到所有渠道文案",
    "diagnosis_case_requires_frozen_evidence": "案例卡没有引用已发布诊断，数字可信度不足；本版已按常青口径生成",
    "ai_extraction_beyond_materials": "AI 提取内容超出你提供的材料，直接发布有编造风险",
    "redaction_manual_review_suggested": "疑似隐私内容未能自动定位，可能有漏打码",
    "unsigned_catalog_advisory": "未签发的目录无权硬拦内容；但相关词项仍可能是法律红线，需要人工判断",
    "ai_copy_legal_blocked_fallback": "带法律红线词的文案不能进入交付，兜底口径保证内容仍可用",
}

WARNING_REPAIR_HINTS: dict[str, str] = {
    "forbidden_claim": "AI 可以只改写含这些词的句子，其余内容原样保留",
    "number_without_evidence": "AI 可以把没有依据的数字删掉或换成有证据的表述",
    "testimonial_without_evidence": "AI 可以删除评价表述或改为客观描述",
    "unlabelled_simulated_chat": "AI 可以在聊天内容开头加上「示例对话」标注",
    "contact_forbidden_when_none": "AI 可以删除文案中的联系方式",
    "explicit_contact_missing": "AI 可以把联系方式补进文案结尾",
    "brand_name_missing": "AI 可以重新生成这张图并保留品牌名",
    "brand_name_forbidden_when_anonymous": "AI 可以重新生成这张图并去掉品牌名",
    "exact_copy_missing": "AI 可以重新生成这张图并逐字保留文案",
    "visual_number_without_evidence": "AI 可以重新生成这张图并去掉无依据数字",
    "contact_visible_when_disabled": "AI 可以重新生成这张图并去掉联系方式/二维码",
    "contact_text_missing": "AI 可以重新生成这张图并加上你填写的联系方式",
    "strategy_promise_claim": "AI 可以只改写策略中含承诺词的格子",
    "diagnosis_case_requires_frozen_evidence": "发布诊断报告后，AI 可以重做这张案例卡并引用冻结事实",
    "ai_extraction_beyond_materials": "AI 可以把超出材料的字段改回你提供的原文",
    "redaction_manual_review_suggested": "AI 无法自动补打，需要你人工框选复核",
    "unsigned_catalog_advisory": "AI 可以删除这些词项重写该句；平台恢复签发目录后会重新按硬拦口径扫描",
    "ai_copy_legal_blocked_fallback": "AI 可以删除红线词后重新生成该渠道文案",
}

ERROR_REASONS: dict[str, str] = {
    "forbidden_claim": "广告法违禁极限词/违法内容是法律红线，这份内容不能带着它生成或发布",
    "strategy_contains_forbidden_claim": "广告法违禁极限词/违法内容是法律红线，会传导到所有渠道文案",
    "asset_copy_edit_rejected": "广告法违禁极限词/违法内容是法律红线，带着它保存即违规交付",
    "image_decode_failed": "图片损坏就不存在可交付的成品",
    "channel_dimensions_invalid": "尺寸不对，渠道平台无法正确展示这张图",
    "visual_qa_unavailable": "未经视觉质检的图不能交付（功能正确性底线）",
    "qr_payload_mismatch_or_unreadable": "成品二维码扫不出你的内容就是不可用成品",
    "qr_decoder_unavailable": "无法验证成品二维码与你上传的是否一致",
    "invalid_qr_image": "二维码图无法读取，无法进入成品",
    "internal_lineage_visible": "成图泄漏内部标识信息，属于数据隔离红线",
    "live_authority_revoked_after_provider": "品牌授权已撤销，继续交付会越权",
    # 🔴 [#113] 租约被抢不是授权撤销 —— 三张表都要有新码,
    #    只补一张的话,另外两张会落到兜底文案或直接露出原始 code。
    "worker_lease_lost_after_provider": "系统侧任务交接失败,成图未交付",
    "live_authority_check_failed_after_provider": "授权校验没通过,成图未交付",
}

ERROR_REPAIR_HINTS: dict[str, str] = {
    "forbidden_claim": "AI 可以删除违禁极限词并重写该句",
    "strategy_contains_forbidden_claim": "AI 可以只改写策略中含违禁词的格子",
    "asset_copy_edit_rejected": "AI 可以删除违禁极限词并重写该句",
    "image_decode_failed": "AI 无法修复损坏文件，只能重新生成",
    "channel_dimensions_invalid": "AI 可以按渠道要求尺寸重新生成",
    "visual_qa_unavailable": "质检服务恢复后 AI 可以自动重试",
    "qr_payload_mismatch_or_unreadable": "AI 可以重新生成并逐像素保留你上传的二维码",
    "qr_decoder_unavailable": "解码服务恢复后 AI 可以自动重试",
    "invalid_qr_image": "AI 无法修复，请换一张清晰的二维码原图",
    "internal_lineage_visible": "AI 可以重新生成并去除内部标识",
    "live_authority_revoked_after_provider": "AI 不能绕过授权撤销，需要恢复授权后再生成",
    # 🔴 [#113] 租约被抢不是授权撤销 —— 三张表都要有新码,
    #    只补一张的话,另外两张会落到兜底文案或直接露出原始 code。
    "worker_lease_lost_after_provider": "算力不会白扣;重做一次即可,不是你的设置问题",
    "live_authority_check_failed_after_provider": "算力不会白扣;稍后重做一次",
}

_DEFAULT_WARNING_REASON = "这条提醒不阻断生成，但发布前需要人工确认"
_DEFAULT_WARNING_REPAIR_HINT = "AI 可以只修正未通过的部分，其余内容原样保留"
_DEFAULT_ERROR_REASON = "该问题不解决就不存在可交付的成品"
_DEFAULT_ERROR_REPAIR_HINT = "AI 可以只修正未通过的部分"

WARNING_ACTIONS: dict[str, list] = {
    "contact_forbidden_when_none": [
        {"id": "open_settings", "label": "去高级设置开启联系方式"},
        {"id": "edit_copy", "label": "删掉文案里的联系方式"},
        {"id": "confirm_continue", "label": "确认不需要，继续"},
    ],
    "strategy_promise_claim": [
        {"id": "edit_strategy", "label": "修改策略"},
        {"id": "confirm_continue", "label": "确认能兑现，继续"},
    ],
    "diagnosis_case_requires_frozen_evidence": [
        {"id": "publish_diagnosis", "label": "去发布诊断报告"},
        {"id": "confirm_continue", "label": "先用常青版本，继续"},
    ],
    "ai_extraction_beyond_materials": [
        {"id": "edit_sheet", "label": "修改确认单"},
        {"id": "confirm_continue", "label": "核对无误，继续"},
    ],
    "redaction_manual_review_suggested": [
        {"id": "review_redaction", "label": "去复核打码"},
        {"id": "confirm_continue", "label": "已复核，继续"},
    ],
}

ERROR_ACTIONS: dict[str, list] = {
    "forbidden_claim": [{"id": "edit_copy", "label": "删除违禁词后重试"}],
    "strategy_contains_forbidden_claim": [{"id": "edit_strategy", "label": "改掉违禁词后重新提交"}],
    "asset_copy_edit_rejected": [{"id": "edit_copy", "label": "改掉违禁词后再保存"}],
    "image_decode_failed": [{"id": "retry_component", "label": "重做这张图"}],
    "channel_dimensions_invalid": [{"id": "retry_component", "label": "按渠道尺寸重做"}],
    "visual_qa_unavailable": [{"id": "retry_later", "label": "稍后重试"}],
    "qr_payload_mismatch_or_unreadable": [
        {"id": "retry_component", "label": "重做这张图"},
        {"id": "reupload_qr", "label": "重新上传二维码"},
    ],
    "qr_decoder_unavailable": [{"id": "retry_later", "label": "稍后重试"}],
    "invalid_qr_image": [{"id": "reupload_qr", "label": "换一张二维码原图"}],
    "internal_lineage_visible": [{"id": "retry_component", "label": "重做这张图"}],
    "live_authority_revoked_after_provider": [{"id": "contact_admin", "label": "联系平台处理"}],
    # 🔴 [#113] 租约被抢不是授权撤销 —— 三张表都要有新码,
    #    只补一张的话,另外两张会落到兜底文案或直接露出原始 code。
    "worker_lease_lost_after_provider": [{"id": "retry_component", "label": "重做这张图"}],
    "live_authority_check_failed_after_provider": [{"id": "retry_component", "label": "重做这张图"}],
}

_DEFAULT_WARNING_ACTIONS = [
    {"id": "edit_copy", "label": "修改文案"},
    {"id": "confirm_continue", "label": "确认无误，继续"},
]
_DEFAULT_ERROR_ACTIONS = [{"id": "retry_component", "label": "修正后重试"}]


def warning_entry(code: str, **extra) -> dict:
    """一条提醒(warn-not-block):五问合同五字段齐 + 其余上下文字段透传。"""
    entry = {
        "code": code,
        "message": WARNING_MESSAGES.get(code, "请核对后再发布"),
        "reason": WARNING_REASONS.get(code, _DEFAULT_WARNING_REASON),
        "repair_hint": WARNING_REPAIR_HINTS.get(code, _DEFAULT_WARNING_REPAIR_HINT),
        "actions": [dict(action) for action in WARNING_ACTIONS.get(code, _DEFAULT_WARNING_ACTIONS)],
        "rule_version": _rule_version_for(code),
    }
    entry.update(extra)
    return entry


def error_entry(code: str, **extra) -> dict:
    """一条法律红线/功能正确性硬失败:五问合同五字段齐 + 其余上下文字段透传。"""
    entry = {
        "code": code,
        "message": ERROR_MESSAGES.get(code, "该内容不能发布"),
        "reason": ERROR_REASONS.get(code, _DEFAULT_ERROR_REASON),
        "repair_hint": ERROR_REPAIR_HINTS.get(code, _DEFAULT_ERROR_REPAIR_HINT),
        "actions": [dict(action) for action in ERROR_ACTIONS.get(code, _DEFAULT_ERROR_ACTIONS)],
        "rule_version": _rule_version_for(code),
    }
    entry.update(extra)
    return entry


def audit_quintuple(*, operator_user_id: int, reason: str, rule_version: str,
                    content_before, content_after) -> dict:
    """「人工确认继续」类路径的审计五要素(SSOT §2.4 / §6.5):
    {operator_user_id, at, reason, rule_version, content_before, content_after}。
    落 marketing_events payload 时整体嵌入,谁确认、何时、为什么、按哪版规则、
    改了什么，五件齐。"""
    return {
        "operator_user_id": int(operator_user_id),
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or ""),
        "rule_version": str(rule_version or ""),
        "content_before": content_before,
        "content_after": content_after,
    }


def channel_contract(channel: str) -> dict:
    try:
        return {"channel": channel, **CHANNEL_CONTRACTS[channel]}
    except KeyError as exc:
        raise ValueError("unsupported_marketing_channel") from exc


def normalize_moments_layout(value: str) -> str:
    layout = str(value or "single").strip().lower()
    if layout not in MOMENTS_LAYOUTS:
        raise ValueError("moments_layout_invalid")
    return layout


def channel_image_slots(channel: str, *, moments_layout: str = "single") -> list[dict]:
    """Resolve image slots; moments grid expands to nine 1:1 tiles."""
    contract = channel_contract(channel)
    if channel == "moments" and str(moments_layout or "single") == "grid":
        return [dict(slot) for slot in contract.get("grid_image_slots") or []]
    return [dict(slot) for slot in contract["image_slots"]]


def diagnosis_case_evidence_ok(evidence: dict) -> bool:
    """案例卡只接受诊断/系统核验出处的冻结事实（渠道边界，不得放宽）。"""
    facts = (evidence or {}).get("facts") or []
    if not facts:
        return False
    allowed = {"diagnosis", "system_verified"}
    for fact in facts:
        provenance = str(fact.get("provenance") or "diagnosis") if isinstance(fact, dict) else "diagnosis"
        if provenance not in allowed:
            return False
    return True


def decide_trend(*, requested: bool, verified_trend: Optional[dict] = None) -> dict:
    """Use a trend only when server-produced verification evidence is complete.

    Current user DTO never accepts a ``verified`` flag.  Callers may inject a
    verified record only from a trusted trend provider in a future adapter.
    """
    now = datetime.now(timezone.utc)
    base = {
        "requested": bool(requested),
        "used": False,
        "source": None,
        "observed_at": None,
        "decided_at": now.isoformat(),
        "reason": "not_requested" if not requested else "verified_trend_unavailable",
        "relevance": {"audience": False, "business": False, "evidence": False, "fresh": False},
    }
    trend = verified_trend or {}
    if not requested or trend.get("verification_source") != "server":
        return base
    required = ("source_url", "observed_at", "topic")
    if not all(trend.get(key) for key in required):
        base["reason"] = "trend_evidence_incomplete"
        return base
    try:
        observed = datetime.fromisoformat(str(trend["observed_at"]).replace("Z", "+00:00"))
        fresh = 0 <= (now - observed.astimezone(timezone.utc)).total_seconds() <= 7 * 86400
    except (TypeError, ValueError):
        fresh = False
    rel = {
        "audience": bool(trend.get("audience_relevant")),
        "business": bool(trend.get("business_relevant")),
        "evidence": bool(trend.get("evidence_supported")),
        "fresh": fresh,
    }
    base["relevance"] = rel
    if all(rel.values()):
        base.update({
            "used": True,
            "source": str(trend["source_url"])[:500],
            "observed_at": str(trend["observed_at"]),
            "topic": str(trend["topic"])[:120],
            "reason": "all_relevance_gates_passed",
        })
    else:
        base["reason"] = "relevance_gate_failed"
    return base


def normalize_contact(mode: str, *, text: str = "", qr_reference: Optional[dict] = None) -> dict:
    mode = str(mode or "none").strip().lower()
    if mode not in {"none", "text", "qr"}:
        raise ValueError("invalid_contact_mode")
    if mode == "none":
        return {"mode": "none", "text": "", "qr_reference": None}
    if mode == "text":
        value = str(text or "").strip()
        if not value:
            raise ValueError("contact_text_required")
        return {"mode": "text", "text": value[:120], "qr_reference": None}
    if not qr_reference or not qr_reference.get("reference_id") or not qr_reference.get("payload_hash"):
        raise ValueError("valid_qr_reference_required")
    qr_org = qr_reference.get("organization_id")
    return {
        "mode": "qr",
        "text": "",
        "qr_reference": {
            "reference_id": str(qr_reference["reference_id"]),
            "payload_hash": str(qr_reference["payload_hash"]),
            "file_sha256": str(qr_reference.get("file_sha256") or ""),
            # 上传时的组织上下文随凭证冻结(2026-07-23 P1-1):生成读取侧与
            # job actor_snapshot 组织一致性校验,fail-closed。
            "organization_id": int(qr_org) if qr_org is not None else None,
            "uploaded_at": qr_reference.get("uploaded_at"),
        },
    }


def _fallback_strategy(brief: str, teacher: dict) -> dict:
    compact = " ".join(str(brief or "").split())[:500]
    audience = "有客户资源、想增加 GEO 服务的服务商老板或运营"
    if "制造" in compact:
        audience = "服务制造业客户的营销服务商"
    elif "律所" in compact or "律师" in compact:
        audience = "服务律所客户的运营或企业服务商"
    elif "装修" in compact:
        audience = "手上有装修老板资源的服务商"
    action = "领取一次 GEO 诊断"
    if "解释" in compact or "什么是" in compact:
        action = "先看懂一份 GEO 诊断示例"
    if "体验" in compact:
        action = "预约一次低门槛体验"
    angle = "客户已经开始问 AI，但答案里可能还没有他的品牌"
    if "案例" in compact or "诊断" in compact:
        angle = "用可追溯诊断证据说明品牌在 AI 答案里的真实位置"
    return {
        "audience": audience,
        "audience_status": "客户决策入口正在从传统搜索扩展到 AI 问答",
        "action_resistance": "担心 GEO 讲不清、交付没有证据，或被当成空概念",
        "human_problem": angle,
        "core_angle": angle,
        "single_value": "把诊断、内容和监测变成客户能看见的证据链",
        "evidence_statement": "仅使用用户选择并已冻结的真实资料；没有证据就明确写无数据",
        "single_action": action,
        "source_brief": compact,
        "teacher_id": teacher["teacher_id"],
        "teacher_version": teacher["version"],
        "source": "deterministic_fallback",
    }


async def interpret_brief(brief: str, teacher: dict, *, facts_pack: Optional[dict] = None,
                          quick_task: Optional[str] = None) -> dict:
    brief = " ".join(str(brief or "").split()).strip()
    if len(brief) < 4:
        raise ValueError("brief_too_short")
    system = (
        "你是可替换的 GEO 获客策略导师。"
        f"方法:{teacher['system_method']}\n"
        f"边界:{'；'.join(teacher.get('channel_boundaries') or [])}\n"
        "严格返回 JSON，字段 audience,audience_status,action_resistance,human_problem,"
        "core_angle,single_value,evidence_statement,single_action。"
        "不虚构诊断数字、客户评价、排名或收益；没有证据写‘待选择真实证据’。"
        "一项内容只保留一个受众、一个卖点、一个行动。"
    )
    # 产品事实 SSOT 注入:平台已核验能力可直接引用,策略层不再只有用户输入的事实。
    if facts_pack:
        selected = product_facts.select_facts(facts_pack, quick_task=quick_task, limit=5)
        if selected:
            system += (
                "\n平台已核验产品事实(provenance=system_verified,可直接引用,"
                "不得超出这些事实写能力或数字):"
                f"{json.dumps(product_facts.prompt_projection(selected), ensure_ascii=False, sort_keys=True)}"
            )
    prompt = f"用户的一句话需求：{brief[:500]}"
    try:
        from tools.multi_llm_caller import call_llm_with_fallback

        raw = await call_llm_with_fallback(f"{system}\n\n{prompt}", verbose=False)
        if raw:
            import json_repair

            data = json_repair.loads(raw)
            keys = (
                "audience", "audience_status", "action_resistance", "human_problem",
                "core_angle", "single_value", "evidence_statement", "single_action",
            )
            if isinstance(data, dict) and all(str(data.get(key) or "").strip() for key in keys):
                out = {key: str(data[key]).strip()[:240] for key in keys}
                if scan_geo_claims(" ".join(out.values()))["passed"]:
                    out.update({
                        "source_brief": brief[:500],
                        "teacher_id": teacher["teacher_id"],
                        "teacher_version": teacher["version"],
                        "source": "ai",
                    })
                    return out
    except Exception:
        pass
    return _fallback_strategy(brief, teacher)


def _safe_tag(value: str) -> str:
    return "#" + re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value)[:12]


def _deal_fact_line(evidence: dict) -> str:
    facts = [
        f"{str(fact.get('label') or '')}{str(fact.get('value') or '')}"
        for fact in (evidence.get("facts") or [])
        if isinstance(fact, dict) and fact.get("value")
    ]
    return "；".join(facts) if facts else "成交细节以客户确认单为准"


# fallback 需引用事实包事实的角度(Owner:商业逻辑/行业观察/经营理念不再是空话)。
_FACT_ANCHOR_ANGLES = frozenset({"business_logic", "industry_observation", "philosophy"})


def _fallback_fact_anchor(facts_pack: Optional[dict], angle: Optional[str]) -> str:
    """为回落文案选一条事实包事实(provenance=system_verified)。

    只在商业逻辑/行业观察/经营理念角度引用:这三类角度讲观点,没有事实锚点
    就是空话;成交事实/靠谱人设角度的事实锚点来自确认单与冻结证据,不抢。
    """
    if angle not in _FACT_ANCHOR_ANGLES or not facts_pack:
        return ""
    selected = product_facts.select_facts(facts_pack, angle=angle, limit=1)
    if not selected:
        return ""
    fact = selected[0]
    if str(fact.get("provenance") or "") != "system_verified":
        return ""
    return str(fact.get("one_liner") or "").strip()


def _angle_body(angle: Optional[str], *, anchor: str, value: str, action: str,
                suffix: str = "", fact: str = "") -> str:
    """按角度产出结构不同的兜底正文(不只换标题:叙事骨架随角度变)。

    事实锚点(anchor)原样保留;角度只决定怎么讲。angle=None 走 deal_fact
    之外的调用方保持原 v1 结构,不经过本函数。``fact``(产品事实包一句话,
    provenance=system_verified)只织入商业逻辑/行业观察/经营理念三类角度,
    其余角度传空串保持原结构。
    """
    fact_clause = f"{fact}。" if fact else ""
    if angle == "business_logic":
        return (f"算一笔明白账：{anchor}。\n\n{fact_clause}这笔账为什么这么算：{value}。\n\n{action}。{suffix}").strip()
    if angle == "philosophy":
        return (f"我做这门生意，一直认一个理：{anchor}。\n\n{fact_clause}所以我的做法很简单：{value}。\n\n{action}。{suffix}").strip()
    if angle == "trust_persona":
        return (f"说说我平时是怎么干活的：{anchor}。\n\n我的习惯是：{value}。\n\n{action}。{suffix}").strip()
    if angle == "industry_observation":
        return (f"最近行业里一个看得见的变化：{anchor}。\n\n{fact_clause}我的判断（事实归事实，判断归判断）：{value}。\n\n{action}。{suffix}").strip()
    # deal_fact / 未知角度:成交事实复盘结构
    return (f"一件刚发生的真事：{anchor}。\n\n经过有一说一：{value}。\n\n{action}。{suffix}").strip()


def _fallback_deal_content(channel: str, strategy: dict, evidence: dict, contact: dict,
                           *, tags: list, suffix: str, angle: Optional[str] = None,
                           fact: str = "") -> dict:
    """Deterministic deal-showcase copy: only confirmed sheet facts, no invention.

    ``fact``(产品事实包一句话)只织入商业逻辑/行业观察/经营理念角度;
    成交事实本身仍以确认单为准。"""
    del contact  # 联系方式归一已在 suffix 完成;晒成交回落文案不额外读 contact
    # 舒老师方法论核心「先把术语翻成人话」:兜底文案也用真人分享口吻(第一人称、口语化),
    # 禁公文腔/系统声明腔("如实讲清楚""未经平台核验""已如实标注"这类一律不写)。
    what = strategy.get("human_problem") or "一笔真实成交"
    value = strategy.get("single_value") or "过程有一说一，不吹不编"
    fact_line = _deal_fact_line(evidence)
    praise = next(
        (
            str(fact.get("value") or "")
            for fact in (evidence.get("facts") or [])
            if isinstance(fact, dict) and fact.get("key") == "customer_praise" and fact.get("value")
        ),
        "",
    )
    if channel == "deal_poster":
        lead = fact_line if (what and what in fact_line) else f"{what}。{fact_line}"
        if angle:
            body = _angle_body(
                angle, anchor=lead, value=value,
                action=f"客户最认的一点：{praise}" if praise else "感谢信任，继续把活干好",
                suffix=suffix, fact=fact,
            )
        else:
            body = f"{lead}。{value}。{suffix}".strip()
        return {
            "title": "成交喜报",
            "body": body,
            "tags": tags,
        }
    if channel == "deal_chat":
        if angle:
            body = f"示例对话：{_angle_body(angle, anchor=fact_line, value=value, action='聊天里的真实原话', suffix=suffix, fact=fact)}"
        else:
            body = f"示例对话：{fact_line}。{value}。{suffix}".strip()
        return {
            "title": "客户成交记录",
            "body": body,
            "tags": tags,
        }
    if channel == "deal_data_card":
        if angle:
            body = _angle_body(
                angle, anchor="数字全部来自客户确认单", value=value,
                action="有一说一，不吹不编", suffix=suffix, fact=fact,
            )
        else:
            body = f"数字是确认单上的，有一说一。{suffix}".strip()
        return {
            "title": "成交数据卡",
            "facts": list(evidence.get("facts") or []),
            "body": body,
            "tags": tags,
        }
    if channel == "deal_story":
        lead = fact_line if (what and what in fact_line) else f"{what}。{fact_line}"
        if angle:
            body = _angle_body(
                angle, anchor=lead, value=strategy.get("core_angle") or value,
                action=f"客户最认的一点：{praise or value}", suffix=suffix, fact=fact,
            )
        else:
            body = (
                f"{lead}。\n\n怎么谈成的：{strategy.get('core_angle') or value}。\n\n"
                f"客户最认的一点：{praise or value}。{suffix}"
            ).strip()
        return {
            "title": "签约故事",
            "body": body,
            "tags": tags,
        }
    if angle:
        body = _angle_body(
            angle, anchor=f"客户的原话：{praise or value}", value="一个字都没改",
            action="感谢信任，继续把活干好", suffix=suffix, fact=fact,
        )
    else:
        body = f"{praise or value}。客户的原话，没改一个字。{suffix}".strip()
    return {
        "title": "客户反馈",
        "body": body,
        "tags": tags,
    }


_ANGLE_CARD_OUTLINES: dict[str, list] = {
    "deal_fact": ["这笔成交发生了什么", "经过有一说一", "下一步先做什么"],
    "business_logic": ["客户为什么愿意买", "这笔账怎么算", "下一步先做什么"],
    "philosophy": ["我一直认的那个理", "所以我的手怎么做", "下一步先做什么"],
    "trust_persona": ["我平时怎么干活", "出了岔子怎么处理", "下一步先做什么"],
    "industry_observation": ["行业里正在变什么", "我的判断是什么", "下一步先做什么"],
}


def fallback_channel_content(channel: str, strategy: dict, evidence: dict, contact: dict,
                             *, angle: Optional[str] = None,
                             facts_pack: Optional[dict] = None) -> dict:
    """确定性兜底文案。``angle``(角度池 key)让同一主题的兜底正文按角度换叙事
    骨架(不只换标题);angle=None 保持 v1 原结构(存量 job 无角度时行为不变)。
    ``facts_pack``(geo_snapshot.product_facts 冻结子集)让商业逻辑/行业观察/
    经营理念三类角度至少引用一条平台已核验事实(provenance=system_verified),
    不再是空话。"""
    angle = normalize_angle(angle)
    audience = strategy.get("audience") or "服务商"
    problem = strategy.get("human_problem") or strategy.get("core_angle") or "客户问 AI 时，答案里有没有他的品牌"
    value = strategy.get("single_value") or "用真实诊断把下一步讲清楚"
    action = strategy.get("single_action") or "领取一次 GEO 诊断"
    suffix = f"\n{contact['text']}" if contact.get("mode") == "text" else ""
    tags = [_safe_tag(x) for x in ("GEO", "AI搜索", "服务商获客")]
    fact = _fallback_fact_anchor(facts_pack, angle)
    if channel == "moments":
        if angle:
            tones = {
                "restrained": _angle_body(angle, anchor=problem, value="先看证据，再决定要不要做", action=action, suffix=suffix, fact=fact),
                "professional": _angle_body(angle, anchor=f"给{audience}的一条建议：{problem}", value=f"{value}；不打包票，只把现状、依据和下一步说清", action=action, suffix=suffix, fact=fact),
                "friendly": _angle_body(angle, anchor="如果你也被客户问过‘AI 为什么没提到我’", value="先别讲概念，跑一份诊断看 AI 原话再聊", action=action, suffix=suffix, fact=fact),
            }
        else:
            tones = {
                "restrained": f"最近常遇到一个真实问题：{problem}。先看诊断证据，再决定要不要做。{action}。{suffix}".strip(),
                "professional": f"给{audience}的一条建议：{value}。不保证结果，只把现状、依据和下一步说清。{action}。{suffix}".strip(),
                "friendly": f"如果你也被客户问过‘AI 为什么没提到我’，可以先别讲概念。跑一份诊断，看 AI 原话再聊。{action}。{suffix}".strip(),
            }
        return {
            "title": problem,
            "tones": tones,
            "tags": tags,
        }
    if channel == "xiaohongshu":
        if angle:
            body = _angle_body(
                angle, anchor="很多老板已经开始直接问 AI 选谁，但品牌有没有被提到，要看真实回答",
                value=f"{value}；哪些是实测，哪些是推断，分开讲", action=action, suffix=suffix, fact=fact,
            )
            card_outline = list(_ANGLE_CARD_OUTLINES.get(angle) or _ANGLE_CARD_OUTLINES["deal_fact"])
        else:
            body = f"很多老板已经开始直接问 AI 选谁，但品牌有没有被提到，要看真实回答。\n\n{value}。哪些是实测，哪些是推断，分开讲。\n\n{action}。{suffix}".strip()
            card_outline = ["客户正在怎么问 AI", "诊断里能看到什么", "下一步先做什么"]
        return {
            "title": problem,
            "body": body,
            "tags": tags,
            "card_outline": card_outline,
        }
    if channel == "douyin":
        if angle:
            script = _angle_body(
                angle, anchor=f"{problem}？别急着讲概念，先看真实回答",
                value=f"{value}；没有数据就说没有，不拿漂亮数字糊弄", action=action, suffix=suffix, fact=fact,
            )
        else:
            script = f"{problem}？\n别急着讲 GEO。先把客户会问的话，原样放进 AI 里，看它怎么回答。\n{value}。没有数据就说没有，不拿漂亮数字糊弄。\n{action}。{suffix}".strip()
        return {
            "title": problem,
            "script": script,
            "shots": ["真人对屏幕开场", "展示脱敏 AI 原话或诊断页", "回到真人给出下一步"],
            "tags": tags,
        }
    if channel == "private_chat":
        if angle:
            opening = _angle_body(
                angle, anchor=f"最近在帮服务商梳理一个问题：{problem}",
                value="如果你愿意，我先按你的品牌跑一份诊断，看真实答案再聊，不急着定方案",
                action=action, suffix=suffix, fact=fact,
            )
        else:
            opening = f"最近在帮服务商梳理一个问题：{problem}。如果你愿意，我可以先按你的品牌跑一份诊断，看真实答案再聊，不急着定方案。{suffix}".strip()
        return {
            "opening": opening,
            "follow_up": "诊断出来后，我会把 AI 原话、同行和缺料分开标清。没有数据的地方也会明确写出来。",
            "objection_reply": "可以先不做长期方案。先看一份诊断是否对你的客户有用，再决定下一步。",
        }
    if channel == "infographic":
        if angle:
            body = _angle_body(
                angle, anchor=problem,
                value=f"{value}；哪些是可核验事实、哪些还没有数据，分开标注",
                action=action, suffix=suffix, fact=fact,
            )
        else:
            body = f"{value}。哪些是可核验事实、哪些还没有数据，分开标注。{suffix}".strip()
        return {
            "title": problem,
            "body": body,
            "tags": tags,
        }
    if channel in DEAL_CHANNELS:
        return _fallback_deal_content(channel, strategy, evidence, contact, tags=tags, suffix=suffix, angle=angle, fact=fact)
    source_note = evidence.get("source_note") or "本内容未引用客户诊断数字"
    facts = list(evidence.get("facts") or [])
    return {
        "title": problem,
        "body": _angle_body(angle, anchor=problem, value=value, action=action, fact=fact) if angle else value,
        "facts": facts,
        "source_note": source_note,
        "cta": action + suffix,
        "tags": tags,
    }


def provider_safe_evidence(evidence: dict) -> dict:
    """Public/provider wording is deliberately separated from internal lineage."""
    return {
        "has_evidence": bool(evidence.get("facts")),
        "source_type": str(evidence.get("source_type") or "none"),
        "source_note": str(evidence.get("source_note") or "本内容未引用客户诊断数字"),
        "facts": list(evidence.get("facts") or []),
        "chat_authorized": bool(evidence.get("chat_authorized")),
    }


def provider_safe_contact(contact: dict) -> dict:
    mode = str(contact.get("mode") or "none")
    return {
        "mode": mode,
        "text": str(contact.get("text") or "") if mode == "text" else "",
        "qr_attached": mode == "qr",
    }


def provider_safe_brand(brand: dict, *, anonymize: bool = False) -> dict:
    """Whitelist presentation facts; never forward future lineage fields.

    ``anonymize=True``(匿名晒单)置空 name/logo:服务商品牌标识不进 provider
    prompt,visual_qa 的 brand_name_missing 检查也因 name 为空而自然跳过。
    """
    if anonymize:
        return {
            "name": "",
            "product_name": "",
            "logo_url": "",
            "brand_color": str(brand.get("brand_color") or "")[:40],
            "slogan": "",
        }
    return {
        "name": str(brand.get("name") or "")[:120],
        "product_name": str(brand.get("product_name") or "")[:120],
        "logo_url": str(brand.get("logo_url") or "")[:500],
        "brand_color": str(brand.get("brand_color") or "")[:40],
        "slogan": str(brand.get("slogan") or "")[:160],
    }


async def generate_channel_content(
    channel: str,
    *,
    strategy: dict,
    evidence: dict,
    trend: dict,
    contact: dict,
    teacher: dict,
    angle: Optional[str] = None,
    facts_pack: Optional[dict] = None,
    before_provider_call=None,
) -> tuple[dict, dict]:
    """Generate one channel-native contract and run its claim/evidence gate.

    Strategy-teacher boundaries and the existing hot-loaded marketing packs are
    composed with an explicit channel contract.  A deterministic, honest
    fallback remains available when the language-model seam is unavailable.
    ``angle``(角度池 key,冻结自 geo_snapshot.angles)决定这一渠道从哪个角度
    讲同一主题——表达观点的自由发挥,事实锚点仍以冻结证据/确认单为准。
    ``facts_pack``(geo_snapshot.product_facts 冻结子集,pack_id+version 可溯)
    把平台已核验产品事实注入渠道 prompt 与回落文案,文案不再只有用户输入的事实。
    """
    contract = channel_contract(channel)
    angle = normalize_angle(angle)
    # Owner 2026-07-22:案例卡无冻结证据不再 422 硬拦;按常青口径生成
    # (回落文案不写诊断数字),并落 warning 提醒"建议先发布诊断报告"。
    diagnosis_evergreen = channel == "diagnosis_case" and not diagnosis_case_evidence_ok(evidence)
    safe_evidence = provider_safe_evidence(evidence)
    base = fallback_channel_content(channel, strategy, safe_evidence, contact, angle=angle, facts_pack=facts_pack)
    prompt = ""
    try:
        from services.marketing.skill_packs import assemble_system_prompt

        prompt = assemble_system_prompt(
            rule_key=f"geo_content_{channel}",
            task_hint=f"{contract['label']}渠道成品",
        )
    except Exception:
        pass
    prompt += (
        "\n\n你负责渠道表达，不修改策略导师冻结的受众、唯一卖点、真实证据或唯一行动。"
        f"\n策略导师:{teacher['name']} {teacher['version']}"
        f"\n导师方法:{teacher.get('system_method') or ''}"
        "\n文风铁律:像一个真人在自己的朋友圈/小红书/抖音分享，第一人称、口语化；"
        "先把术语翻成人话，禁止公文腔、系统提示腔、免责声明腔和 AI 套话；"
        "不得出现「已脱敏」「未经平台核验」「如实标注」「事实以…为准」这类系统自我声明。"
        f"\n渠道合同:{json.dumps(contract, ensure_ascii=False, sort_keys=True)}"
        f"\n冻结策略:{json.dumps(strategy, ensure_ascii=False, sort_keys=True)}"
        f"\n冻结证据:{json.dumps(safe_evidence, ensure_ascii=False, sort_keys=True)}"
        f"\n热点决策:{json.dumps(trend, ensure_ascii=False, sort_keys=True)}"
        f"\n联系方式合同:{json.dumps(provider_safe_contact(contact), ensure_ascii=False, sort_keys=True)}"
        "\n严格返回一个 JSON 对象，键必须符合 copy_fields。"
        "没有真实数据不得写数字、排名、客户证言或收益；聊天模拟必须显著写‘示例对话’。"
        "热点 used=false 时写 evergreen，不暗示近期热搜。"
        "朋友圈必须提供 restrained/professional/friendly 三种语气；"
        "抖音必须提供 30–60 秒口播和镜头；小红书必须提供标题、正文、标签和卡片提纲。"
    )
    if angle:
        spec = ANGLE_POOL[angle]
        prompt += (
            f"\n内容角度:{spec['name']}({angle})"
            f"\n角度指导:{spec['guidance']}"
            "\n围绕该角度组织整篇正文的叙事结构(不是只换标题);"
            "事实锚点(金额/行业/客户原话)仍以冻结证据与确认单为准,角度只是表达观点的自由发挥。"
        )
    # 产品事实 SSOT 注入(按渠道/角度选相关条目,控 token):平台已核验能力
    # 可直接引用,文案因此有真实产品事实支撑;不得超出这些事实写能力或数字。
    if facts_pack:
        selected_facts = product_facts.select_facts(facts_pack, channel=channel, angle=angle, limit=5)
        if selected_facts:
            prompt += (
                f"\n产品事实库:{facts_pack.get('pack_id')}@{facts_pack.get('version')}"
                "(provenance=system_verified,平台已核验事实,可直接引用,"
                "不得超出这些事实写能力、数字或效果):"
                f"{json.dumps(product_facts.prompt_projection(selected_facts), ensure_ascii=False, sort_keys=True)}"
            )
    candidate = None
    call_llm = None
    guard_rejected = None
    try:
        from tools.multi_llm_caller import ProviderCallGuardRejected, call_llm_with_fallback
        call_llm = call_llm_with_fallback
        guard_rejected = ProviderCallGuardRejected
    except Exception:
        pass
    if call_llm is not None and before_provider_call is not None:
        # Keep a caller-independent preflight, then the durable caller repeats
        # this check immediately before every fallback provider POST.
        guarded = before_provider_call()
        if inspect.isawaitable(guarded):
            await guarded
    try:
        raw = await call_llm(
            prompt,
            verbose=False,
            before_provider_call=before_provider_call,
        ) if call_llm is not None else None
        if raw:
            import json_repair

            parsed = json_repair.loads(raw)
            required = set(contract["copy_fields"])
            if isinstance(parsed, dict) and required.issubset(parsed):
                candidate = parsed
    except Exception as exc:
        if guard_rejected is not None and isinstance(exc, guard_rejected):
            raise
        candidate = None
    payload = candidate or base
    qa = claim_evidence_qa(payload, evidence=safe_evidence, contact=contact, channel=channel)
    if not qa["passed"] and candidate is not None:
        # 只有 errors(法律红线)允许丢弃 AI 候选回落兜底;warnings 一律不得触发丢弃。
        # 丢弃必须透出提醒(2026-07-23 外部审查 P2-1):不得静默换掉 AI 版。
        payload = base
        qa = claim_evidence_qa(payload, evidence=safe_evidence, contact=contact, channel=channel)
        qa = {
            **qa,
            "warnings": (qa.get("warnings") or [])
            + [warning_entry("ai_copy_legal_blocked_fallback")],
        }
    if diagnosis_evergreen:
        qa = {
            **qa,
            "warnings": (qa.get("warnings") or [])
            + [warning_entry("diagnosis_case_requires_frozen_evidence")],
        }
    return payload, qa


def claim_evidence_qa(payload: dict, *, evidence: dict, contact: dict, channel: str) -> dict:
    """文案 QA 分层(Owner 2026-07-22;治理对齐 SSOT 2026-07-23):errors=法律红线硬拦;
    warnings=提醒不阻断。

    ``passed`` 只看 errors。硬拦只有法律禁止目录包命中项(广告法违禁极限词 +
    违法内容,guards.HARD_FLAG_KINDS);承诺词/灰词(唯一/领先等)/无证据数字/未标注
    聊天/联系方式提醒全部进 warnings——提醒放行,每条带五问合同五字段。
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    errors: list[dict] = []
    warnings: list[dict] = []
    forbidden = scan_geo_claims(text)
    flags = forbidden.get("flags") or {}
    hard_detail = {kind: flags[kind] for kind in guards.HARD_FLAG_KINDS if flags.get(kind)}
    if hard_detail:
        # 唯一硬拦截:法律禁止目录包命中项(Owner 签发版本化,人工不可绕过)。
        errors.append(error_entry("forbidden_claim", detail=hard_detail))
    rest = {kind: hits for kind, hits in flags.items() if kind not in guards.HARD_FLAG_KINDS}
    unsigned_hits = rest.pop(guards.UNSIGNED_CATALOG_FLAG, None)
    if unsigned_hits:
        # 法律目录包未签发/不可用:命中项只提醒(未签发目录无权硬拦),告警日志在 guards 层。
        warnings.append(warning_entry(guards.UNSIGNED_CATALOG_FLAG, detail=unsigned_hits))
    if rest:
        # 承诺词/灰词/裸指标/术语红线:提醒不阻断。
        warnings.append(warning_entry("forbidden_claim", detail=rest))

    allowed_numbers = {str(value) for value in (30, 60) if channel == "douyin"}
    if contact.get("mode") == "text":
        allowed_numbers.update(re.findall(r"\d+(?:\.\d+)?%?", str(contact.get("text") or "")))
    for fact in evidence.get("facts") or []:
        for number in re.findall(r"\d+(?:\.\d+)?%?", str(fact.get("value") if isinstance(fact, dict) else fact)):
            allowed_numbers.add(number)
    observed = set(re.findall(r"\d+(?:\.\d+)?%?", text))
    unknown_numbers = sorted(n for n in observed if n not in allowed_numbers)
    if unknown_numbers:
        warnings.append(warning_entry("number_without_evidence", detail=unknown_numbers))

    if not evidence.get("facts") and re.search(r"客户(说|反馈|评价|证言)|用户(说|反馈|评价)", text):
        warnings.append(warning_entry("testimonial_without_evidence"))
    if re.search(r"(聊天记录|对话截图|客户对话)", text) and "示例对话" not in text and not evidence.get("chat_authorized"):
        warnings.append(warning_entry("unlabelled_simulated_chat"))

    if contact.get("mode") == "none":
        if re.search(r"(?:1[3-9]\d{9})|(?:微信|手机号|电话|二维码|加我|联系(?:我|我们)?)", text):
            warnings.append(warning_entry("contact_forbidden_when_none"))
    elif contact.get("mode") == "text" and contact.get("text") not in text:
        warnings.append(warning_entry("explicit_contact_missing"))

    return {"passed": not errors, "errors": errors, "warnings": warnings}


def build_visual_brief(
    *,
    slot: dict,
    channel: str,
    strategy: dict,
    content: dict,
    evidence: dict,
    trend: dict,
    contact: dict,
    brand: dict,
    visual_style: Optional[str] = None,
    anonymize_brand: bool = False,
    angle: Optional[str] = None,
    logo_attached: bool = False,
    facts_pack: Optional[dict] = None,
) -> VisualBrief:
    """Assemble the eight-element VisualBrief for one channel image slot.

    All v1 channel semantics are preserved: Chinese copy is rendered directly
    by gpt-image-2, program compositing stays forbidden, and contact=none keeps
    every contact word on the prohibited list.  ``visual_style`` comes from the
    frozen geo_snapshot(用户高级设置),非法值 fail-closed 回落默认;``anonymize_brand``
    (匿名晒单)让品牌标识与品牌名/客户名约束同时作用于 provider 输入与 QA。
    ``angle``(冻结角度)进 subject.angle;``logo_attached`` 表示本次确实有
    品牌 logo 参考图垫入 image_urls,prompt 才要求忠实保留(读取失败静默降级
    为纯文字品牌名时=False,不要求保留)。``facts_pack``(冻结产品事实子集)
    进 subject.product_facts 摘要,成图的事实表达与文案同源。
    """
    contract = channel_contract(channel)
    angle = normalize_angle(angle)
    is_chat_scene = str(slot.get("slot") or "") in {"private_chat_scene", "deal_chat_scene"}
    is_deal_channel = channel in DEAL_CHANNELS
    exact_text = {
        key: value for key, value in content.items()
        if key in {"title", "body", "cta", "source_note"} and isinstance(value, str)
    }
    if "tones" in content:
        exact_text["body"] = str((content.get("tones") or {}).get("restrained") or "")
    if channel == "douyin":
        exact_text["body"] = str(content.get("script") or "").split("\n")[0][:60]
    if contact.get("mode") == "text":
        # 联系方式开启时才进图；原文逐字呈现。
        exact_text["contact"] = str(contact.get("text") or "")
    if is_chat_scene:
        exact_text["scene_label"] = "示例对话"

    constraints = [
        "第三方 logo", "额外文字", "重复文字", "伪造客户评价", "伪造排名或收益",
        "未经证据支持的数字", "裁切标题", "无法扫码的二维码",
    ]
    if contact.get("mode") == "none":
        constraints.extend(["联系方式", "手机号", "微信号", "二维码", "联系我"])
    if is_chat_scene:
        constraints.append("伪装真实客户对话；聊天界面必须显著标注‘示例对话’")
        constraints.append("真实用户头像、昵称与个人信息")
    if is_deal_channel:
        constraints.append("还原、猜测或补全已打码遮挡的姓名、头像、金额与订单信息")
        constraints.append("AI 生成/情景演示等免责标注（晒成交场景不强制，仅聊天示意图保留‘示例对话’）")
        constraints.append("用星号(*)代替打码；敏感信息一律用马赛克色块遮挡")
        constraints.append("「已脱敏」「脱敏」等字样出现在画面上；打码效果本身就是脱敏，不额外标注")
    if anonymize_brand:
        # 匿名晒单:服务商品牌名与客户名一律不上图(与服务端确认单掩码同口径)。
        constraints.append("任何服务商品牌名、品牌 logo、客户名与客户公司名")

    composition = "一个强标题，其次是一句证据说明，最后只保留一个行动；中文逐字准确"
    if is_chat_scene:
        composition = (
            "手机聊天界面示意图；聊天气泡排版、光线均匀；"
            "界面显著位置标注‘示例对话’；不出现真实头像、昵称或联系方式；中文逐字准确"
        )
    elif is_deal_channel:
        composition = (
            "一个短而有力的标题，其次是成交事实说明，最后只保留一个行动；"
            "随附已打码真实素材仅作版式与事实参考，遮挡区域保持马赛克色块且不可还原，"
            "不使用星号代替打码，画面不出现「已脱敏」字样；中文逐字准确"
        )

    try:
        style_text = VISUAL_STYLES[normalize_visual_style(visual_style)]
    except ValueError:
        # 冻结快照之外的调用方给了非法风格:fail-closed 回默认,不静默丢弃合法选择。
        style_text = VISUAL_STYLES[DEFAULT_VISUAL_STYLE]
    # 产品事实摘要(冻结子集按渠道/角度再精选,一句话级,控 token):
    # 成图只表达已核验事实,与文案同源同版本。
    facts_summary = ""
    if facts_pack:
        visual_facts = product_facts.select_facts(facts_pack, channel=channel, angle=angle, limit=3)
        facts_summary = "；".join(
            str(fact.get("one_liner") or "") for fact in visual_facts if fact.get("one_liner")
        )[:120]
    return VisualBrief(
        task_type="simulated_chat_scene" if is_chat_scene else "channel_marketing_image",
        subject={
            "channel": contract["label"],
            "slot": slot["slot"],
            "audience": strategy.get("audience"),
            "marketing_goal": strategy.get("single_action"),
            "single_core_message": strategy.get("single_value"),
            "evidence_expression": (
                provider_safe_evidence(evidence).get("facts")
                or "无可引用数字；不得画诊断数字或客户证言"
            ),
            "contact_mode": contact.get("mode"),
            "qr_reference": (
                {"attached": True, "instruction": "逐像素忠实保留随请求附带的用户二维码；不得重绘或编造"}
                if contact.get("mode") == "qr" else None
            ),
            "trend": trend if trend.get("used") else {"used": False, "instruction": "生成 evergreen 内容，不提近期热点"},
            "deal_assets": (
                {"attached": True, "instruction": "随附已打码真实成交素材作为垫图参考；遮挡区域保持马赛克不可还原"}
                if is_deal_channel else None
            ),
            "angle": (
                {
                    "key": angle,
                    "name": ANGLE_POOL[angle]["name"],
                    "guidance": ANGLE_POOL[angle]["visual"],
                }
                if angle else None
            ),
            "brand_logo": (
                {
                    "attached": True,
                    "instruction": (
                        "随附品牌 logo 参考图：在左上角或合适位置忠实保留该 logo，"
                        "不重绘、不变形、不改配色；logo 之外不添加其他品牌标识"
                    ),
                }
                if logo_attached and not anonymize_brand else None
            ),
            "product_facts": (
                {
                    "pack": f"{facts_pack.get('pack_id')}@{facts_pack.get('version')}",
                    "provenance": "system_verified",
                    "summary": facts_summary or None,
                }
                if facts_pack else None
            ),
        },
        visual_style=style_text,
        composition_and_lighting=composition,
        exact_visible_text=exact_text,
        platform_ratio=str(slot.get("size") or ""),
        brand_assets=provider_safe_brand(brand, anonymize=anonymize_brand),
        privacy_and_truth_constraints=tuple(constraints),
    )


def visual_prompt(
    *,
    slot: dict,
    channel: str,
    strategy: dict,
    content: dict,
    evidence: dict,
    trend: dict,
    contact: dict,
    brand: dict,
    visual_style: Optional[str] = None,
    anonymize_brand: bool = False,
    angle: Optional[str] = None,
    logo_attached: bool = False,
    facts_pack: Optional[dict] = None,
) -> str:
    brief = build_visual_brief(
        slot=slot, channel=channel, strategy=strategy, content=content,
        evidence=evidence, trend=trend, contact=contact, brand=brand,
        visual_style=visual_style, anonymize_brand=anonymize_brand,
        angle=angle, logo_attached=logo_attached, facts_pack=facts_pack,
    )
    return VisualPromptComposer().compose(brief)
