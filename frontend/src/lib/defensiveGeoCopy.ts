/**
 * 防御型 GEO 前端文案 —— **机械生成,不要手改**。
 *
 * 生成器:``scripts/defgeo_census/emit_frontend_copy.py``
 * SSOT   :``services/defensive_geo/copy_registry.py``(诊断域)
 * 门     :``frontend/scripts/verify-defgeo-copy-registry.mjs``
 *          (``npm run lint`` 前置 / 单跑 ``npm run verify:defgeo-copy``)
 *
 * 为什么前端还要有一份
 * --------------------
 * 大多数文案由服务端随 DTO 下发(``userLabel`` / ``publicExplanation``),
 * 前端只渲染。但有一批提示在**用户还没发任何请求时**就要上屏
 * (例如「所属行业选填」那条 inline 提示)。这些如果让前端自己写,
 * 就会长出第二套口径 —— U-1 明令禁止。
 *
 * 所以这份文件是**导出物**:改文案去改后端 registry,然后重新生成。
 * 手改这里会被上面那道门当场判红(``npm run lint`` 时就会碰到)。
 */

/* eslint-disable */

export const DEFGEO_COPY_REGISTRY_VERSION = "defensive-geo-copy-v11";

/** 每条文案在后端 registry 里的坐标。门拿它逐条回查。 */
export const DEFGEO_COPY_SOURCE: Record<string, readonly [string, string]> = {
    questionTooLong: ["reason", "question_too_long"],
    fiveCardSummaryUnavailable: ["reason", "five_card_summary_unavailable"],
    openDeliveryTodo: ["action", "open_delivery_todo"],
    confirmNextPending: ["action", "confirm_next_pending"],
    deliveryTodoIntro: ["reason", "delivery_todo_intro"],
    deliveryTodoAllDone: ["reason", "delivery_todo_all_done"],
    confirmUnchangedPrice: ["action", "confirm_unchanged_price"],
    reviewNewPrice: ["action", "review_new_price"],
    openApprovedConfirm: ["action", "open_approved_confirm"],
    previewRebuiltPriceUnchanged: ["reason", "preview_rebuilt_price_unchanged"],
    previewRebuiltPriceChanged: ["reason", "preview_rebuilt_price_changed"],
    mediaReplacedSameTier: ["reason", "media_replaced_same_tier"],
    mediaDowngradedNeedsReconfirm: ["reason", "media_downgraded_needs_reconfirm"],
    legalRuleHitRepairable: ["reason", "legal_rule_hit_repairable"],
    openCustomerLinks: ["action", "open_customer_links"],
    copyCustomerLink: ["action", "copy_customer_link"],
    reissueCustomerLink: ["action", "reissue_customer_link"],
    customerLinkExpired: ["reason", "customer_link_expired"],
    customerLinkRevoked: ["reason", "customer_link_revoked"],
    customerLinkNotReady: ["reason", "customer_link_not_ready"],
    aiAutofillFacts: ["action", "ai_autofill_facts"],
    confirmAiFacts: ["action", "confirm_ai_facts"],
    editFactsMyself: ["action", "edit_facts_myself"],
    cancelAiAutofill: ["action", "cancel_ai_autofill"],
    aiFillIndustry: ["action", "ai_fill_industry"],
    brandFactsMissing: ["reason", "brand_facts_missing"],
    aiAutofillNeedsConfirm: ["reason", "ai_autofill_needs_confirm"],
    industryOptionalDefensive: ["reason", "industry_optional_defensive"],
    viewLegalRepairCandidates: ["action", "view_legal_repair_candidates"],
    applyLegalRepair: ["action", "apply_legal_repair"],
    editLegalRepair: ["action", "edit_legal_repair"],
    retryPublishConfirm: ["action", "retry_publish_confirm"],
    legalRepairApplied: ["reason", "legal_repair_applied"],
    pricingDeactivatedAfterPreview: ["reason", "pricing_deactivated_after_preview"],
    policyUnavailable: ["reason", "policy_unavailable"],
    newPreviewAction: ["action", "new_preview"],
    topUpAction: ["action", "top_up"],
    contactSupportAction: ["action", "contact_support"],
    insufficientPoints: ["reason", "insufficient_points"],
};

export const DEFGEO_COPY = {
    questionTooLong: "第 {ordinal} 题太长了：{chars} 字，上限 {limit} 字。「{excerpt}…」—— 请把这道题改短一点再确认。（常见原因：客户名很长，系统按名字拼出来的题就超了。）",
    fiveCardSummaryUnavailable: "摘要卡片这次没能生成，下面是完整报告，内容一条都不少。",
    openDeliveryTodo: "去看这一单还有哪些要确认",
    confirmNextPending: "确认下一项",
    deliveryTodoIntro: "这一单要确认的媒体方案都在这里；每一项各自确认、各自计费，不会一次性全扣。",
    deliveryTodoAllDone: "这一单没有等你确认的项目了。",
    confirmUnchangedPrice: "价格没变，直接确认",
    reviewNewPrice: "看看新价格再确认",
    openApprovedConfirm: "去确认这一单",
    previewRebuiltPriceUnchanged: "刚才那一步已过期，已经帮你重新算过：价格没变，直接确认就行。",
    previewRebuiltPriceChanged: "刚才那一步已过期，已经帮你重新算过；这次的价格有变化，请再确认一次。",
    mediaReplacedSameTier: "原媒体临时没档期，已经自动换成同角色、同预算的媒体；费用和交付承诺都没变，你不用做任何事。",
    mediaDowngradedNeedsReconfirm: "原媒体临时没档期，费用已退回；换同级媒体请再确认一次。",
    legalRuleHitRepairable: "这句话可能违反广告法（已标出位置），没有扣费；点这里修好这一句，重新确认即可发布。",
    openCustomerLinks: "发给客户",
    copyCustomerLink: "复制链接和话术",
    reissueCustomerLink: "重新签发一个新链接",
    customerLinkExpired: "这个链接已经过期了，点一下就能重新签发一个新的发给客户；不额外扣算力。",
    customerLinkRevoked: "这个链接已经收回了，点一下可以重新签发一个新的发给客户；不额外扣算力。",
    customerLinkNotReady: "这一类链接现在还没有；要等前一步做完才会有。",
    aiAutofillFacts: "让 AI 联网帮你查",
    confirmAiFacts: "就用这些资料",
    editFactsMyself: "我自己填",
    cancelAiAutofill: "先不查",
    aiFillIndustry: "让 AI 帮你查行业",
    brandFactsMissing: "还差几项公司资料。可以让 AI 联网帮你查全套，也可以自己填；查之前会先告诉你要花多少算力。",
    aiAutofillNeedsConfirm: "这些是 AI 查到的，还没有生效；你确认之后才会写进这家公司的资料。",
    industryOptionalDefensive: "先守住品牌这条线不填所属行业也能做；填上结果会更准，可以让 AI 帮你查。",
    viewLegalRepairCandidates: "看 AI 改好的版本",
    applyLegalRepair: "用这一句，重新确认",
    editLegalRepair: "再改一下这句话",
    retryPublishConfirm: "回去重新确认这一篇",
    legalRepairApplied: "这一句已经改进稿子里了，回去重新确认一次就能继续；这一步没有扣算力。",
    pricingDeactivatedAfterPreview: "这一项现在暂时不能开始。重新发起一次就能继续；没有扣除任何算力。",
    policyUnavailable: "这条付费方式暂时用不了，帮你转给平台客服处理。",
    newPreviewAction: "重新发起体检",
    topUpAction: "去充值算力",
    contactSupportAction: "联系平台客服",
    insufficientPoints: "你的算力不够这次体检。充值之后回来重新发起一次即可，不会重复扣算力。",
} as const;

export type DefgeoCopyKey = keyof typeof DEFGEO_COPY;
