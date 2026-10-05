/**
 * 防御型 GEO · 发布 v2 façade 的**前端契约面**(UI-34 / UI-35 / UI-36 + §0.5.5 U-1/U-2/U-4/U-5/U-7)。
 *
 * 服务端 SSOT = `api/defensive_publish_api.py` 的 DTO(全部 camelCase wire、`extra='forbid'`)。
 * 这里只做三件事,一件都不多:
 *   ① 把 wire 形状写成 TS 类型(**不加字段、不改名** —— 改名等于给同一概念开第二个叫法);
 *   ② 提供 `serverCopy()`:上屏文案**只能**来自服务端,拿不到就如实标注「后端没下发」,
 *      **绝不由前端编一句**(U-1:内部枚举裸串上屏 = 验收红;U-2:前端不得自造译文);
 *   ③ 提供只读投影(取推荐候选、取备选列表),不做任何排序/评分/价格计算(§9.1)。
 *
 * 🔴 这里**没有**任何 `Record<enum, 中文>` 状态词表。
 *    后端 `services/defensive_geo/copy_registry.py` 是对外文案的唯一 SSOT;
 *    在前端再抄一份就是「同一谓词写两处 ⇒ 必有一处没人验」的标准形态。
 *    唯一的例外在 `EVIDENCE_KIND_OPTIONS`(见下方说明:那是**本表单自己的输入选项**,
 *    不是任何服务端状态的译名),且已在交付单里挂号。
 */

import { looksLikeInternalEnum } from '@/lib/defensiveGeoPresentation';

// ══════════════════════════════════════════════════════════════════════════
// 服务端 wire 类型 —— 逐字对齐 api/defensive_publish_api.py
// ══════════════════════════════════════════════════════════════════════════

/** `_recovery_action()` / `confirmability().nextAction` / `legal_gate.repair_action()` 同形。 */
export interface TypedAction {
    kind: string;
    /** 🔴 按钮文案的唯一来源。后端可能下发空串(见交付单挂号项),前端**不补**。 */
    label: string;
    actionRef: string;
    capability: string;
    target: { kind: string; id?: string; page?: string; passageRef?: string };
}

/** `media_identity.PublicMediaIdentity.provider_dto()`。 */
export interface PublicMediaIdentity {
    publicMediaOptionId: string;
    publicMediaKey: string;
    canonicalRootDomainKey: string;
    publicMediaName: string;
    publicRootDomainLabel: string;
    /** 🔴 闭集枚举(authority_anchor / strong_vertical / broad_discovery / official_owned)。
     *  后端**没有**下发它的人话译名 —— 所以本文件不提供 role→中文 的映射,
     *  页面用 `<ServerLabel>` 如实留白。详见交付单。 */
    mediaRole: string;
}

/** `decision_snapshot.Candidate.wire()`。 */
export interface MediaCandidate extends PublicMediaIdentity {
    /** `kind` 是内部枚举(question_importance…),**只渲染 label**。 */
    reasonFacts: Array<{ kind: string; label: string }>;
    exactPoints: number;
    /** 只有 `snapshot.decision` 有这一格。 */
    publishItemRequestId?: string;
}

export interface BudgetFacet {
    capPoints: number;
    reservedPoints: number;
    committedPoints: number;
    remainingPoints: number;
}

/** `decision_snapshot.freeze_snapshot()` 的冻结面。只列页面真正读的格。 */
export interface FrozenSnapshot {
    contractVersion: string;
    decisionSnapshotId: string;
    publishSlotId: string;
    snapshotVersion: number;
    expiresAt: string;
    acceptedSnapshotId: string;
    serviceProjectionId: string;
    planItemKey: string;
    articleRevisionId: string;
    globalBudget: BudgetFacet;
    scopeBudget: BudgetFacet;
    decision: MediaCandidate;
    alternatives: MediaCandidate[];
    totalExactPoints: number;
    fundingPolicy: string;
    approvalRequirement: string;
    canonicalHash: string;
}

/** `decision_snapshot.confirmability()`。 */
export interface Confirmability {
    canConfirm: boolean;
    fundingPolicy: string;
    approvalState: string;
    reasonCode: string | null;
    nextAction: TypedAction | null;
    /**
     * 🔴 **后端当前不下发这一格**(census 实测:`confirmability()` 的返回里没有它,
     *    而 `copy_registry._REASON_EXPLANATION` 明明有全部译文)。
     *    这里留成可选字段是为了「后端补上的那天前端立刻能渲染」,
     *    而不是让前端从 `reasonCode` 反推一句话 —— 反推就是自造文案。
     */
    publicExplanation?: string;
}

export interface BudgetBlocker {
    scope: string;
    requiredExactPoints: number;
    remainingPoints: number;
    deltaPoints: number;
    budgetSnapshotId: string;
    budgetVersion: number;
}

/** `_adjustment_options()`。`publicReason/publicChange/publicLoss` 全是服务端文案。 */
export interface AdjustmentOption {
    kind: string;
    fundingPolicy: string;
    scope: string;
    deltaPoints: number;
    publicReason: string;
    publicChange: string;
    publicLoss: string | null;
    newCustomerSnapshotRequired: boolean;
    reconfirmPublishDecision: boolean;
    nextAction: TypedAction;
}

/** `SnapshotResponse`。 */
export interface SnapshotResponse {
    lifecycle: string;
    snapshot: FrozenSnapshot;
    confirmability: Confirmability;
    budgetBlockers: BudgetBlocker[];
    adjustmentOptions: AdjustmentOption[];
    originalCommandId: string | null;
    statusUrl: string | null;
    supersessionKind: string | null;
    supersededBySnapshotId: string | null;
    supersededBySnapshotHash: string | null;
}

/** `ConfirmResponse`(confirm / override 共用同一形状)。 */
export interface ConfirmResponse {
    publishCommandId: string;
    decisionSnapshotId: string;
    decisionSnapshotHash: string;
    commandCanonicalHash: string;
    exactSettlementPoints: number;
    statusUrl: string;
    idempotentReplay: boolean;
    commandState: string;
    commandReason: string | null;
    fundingPolicy: string;
    fundingState: string;
    /** 🔴 U-4「已扣 / 已冻结」那句话的唯一来源。 */
    fundingStateLabel: string;
    nextAction: TypedAction;
    item: {
        publishItemRequestId: string;
        media: PublicMediaIdentity;
        exactSettlementPoints: number;
        state: string;
        reason: string | null;
        nextAction: TypedAction;
    };
}

export interface LegalRuleHit {
    ruleId: string;
    ruleVersion: string;
    passageRef: string;
    /** UI-36:广告法命中要把**原句**摆出来,不能只说「有问题」。 */
    passageExcerpt: string;
    articleRevisionId: string;
    repairActionKind: string;
}

/** `CommandStatusResponse`。 */
export interface CommandStatusResponse {
    publishCommandId: string;
    decisionSnapshotId: string;
    decisionSnapshotHash: string;
    commandCanonicalHash: string;
    /** 🔴 UI-34 单调守卫的轴。 */
    statusVersion: number;
    updatedAt: string;
    commandState: string;
    /** 🔴 状态文字的唯一来源(quarantined 那句「费用已冻结、不会多扣」就在这里)。 */
    commandStateLabel: string;
    reason: string | null;
    fundingPolicy: string;
    fundingState: string;
    fundingStateLabel: string;
    nextAction: TypedAction | null;
    item: {
        publishItemRequestId: string;
        publicationSettlement: {
            canonicalPublicationState: string;
            settlementDirection: string;
        };
        availability: string;
        publicUrl: string | null;
        reason: string | null;
        verificationClues: unknown[];
        legalRuleHit: LegalRuleHit | null;
        nextAction: TypedAction | null;
    };
}

/** `ReviewQueueResponse`。 */
export interface ReviewQueueEntry {
    publishCommandId: string;
    tenantOwnerId: number;
    brandId: number;
    fundingPolicy: string;
    fundingState: string;
    fundingStateLabel: string;
    exactSettlementPoints: number;
    canonicalPublicationState: string;
    externalStartAt: string | null;
    providerCallCount: number;
    pendingSeconds: number;
    /** 🔴 「超没超 7 天」由服务端判,前端**不拿 pendingSeconds 自己除**。 */
    isStale: boolean;
    reviewEntryCount: number;
    reason: string | null;
}

export interface ReviewActionCatalogItem {
    action: string;
    /** 服务端文案已内含资金方向(「确认已执行（扣除这笔算力）」/「确认未执行（退回这笔算力）」)。 */
    label: string;
    reasonRequired: boolean;
}

export interface ReviewQueueResponse {
    entries: ReviewQueueEntry[];
    totalPending: number;
    staleOverDays: number;
    actionCatalog: ReviewActionCatalogItem[];
}

export interface ReviewActionResponse {
    publishCommandId: string;
    action: string;
    fundingStateBefore: string;
    fundingStateAfter: string;
    fundingStateLabel: string;
    entryId: number;
}

// ══════════════════════════════════════════════════════════════════════════
// U-1 / U-2 上屏闸 —— 服务端文案的唯一读法
// ══════════════════════════════════════════════════════════════════════════

/** 服务端文案的判定结果。`missing` 分三种,页面据此决定「留白」还是「禁用出口」。 */
export type ServerCopyVerdict =
    | { ok: true; text: string }
    | { ok: false; why: 'absent' | 'blank' | 'internal_enum'; raw: string };

/**
 * 读一句服务端文案。
 *
 * 🔴 三条都不许回落成「编一句」:
 *   · 没这个字段        → `absent`
 *   · 下发了空串        → `blank`(后端 `assert_public_copy_clean` 判空为红,
 *                        但 `legal_gate.repair_action(label=...)` 这条路**没过那道门**)
 *   · 下发了内部枚举形态 → `internal_enum`(判形态不判词表,与
 *                        `looksLikeInternalEnum` 共用同一实现,不另起第二套)
 */
export function serverCopy(text: unknown): ServerCopyVerdict {
    if (typeof text !== 'string') return { ok: false, why: 'absent', raw: '' };
    const trimmed = text.trim();
    if (!trimmed) return { ok: false, why: 'blank', raw: text };
    if (looksLikeInternalEnum(trimmed)) return { ok: false, why: 'internal_enum', raw: text };
    return { ok: true, text: trimmed };
}

/** 一个 typed action 现在能不能真的点。label 缺失 = 死 CTA,**禁用而不是编个名字**。 */
export function actionIsRenderable(action: TypedAction | null | undefined): boolean {
    return !!action && serverCopy(action.label).ok;
}

// ══════════════════════════════════════════════════════════════════════════
// 只读投影 —— 不排序、不评分、不算价
// ══════════════════════════════════════════════════════════════════════════

/**
 * 备选媒体列表 = `snapshot.alternatives` 去掉与 decision **同 optionId** 的那一条。
 * 顺序**原样保留**(MED-21:换序也要改 hash,所以顺序是服务端权威的一部分)。
 */
export function alternativeCandidates(snapshot: FrozenSnapshot): MediaCandidate[] {
    const chosen = snapshot.decision?.publicMediaOptionId;
    return (snapshot.alternatives || []).filter((c) => c.publicMediaOptionId !== chosen);
}

/**
 * U-7:`insufficient_points` 时默认展开的那一个。
 * **取服务端给的第一条**(`_adjustment_options()` 的产出顺序即推荐顺序:
 * top_up → request_approval → contact_platform_budget_owner → reduce_customer_delivery
 * → cancel_no_charge 恒在最后)。前端不重排、不挑选。
 */
export function recommendedOption(options: AdjustmentOption[]): AdjustmentOption | null {
    return options.length > 0 ? options[0] : null;
}

export function otherOptions(options: AdjustmentOption[]): AdjustmentOption[] {
    return options.length > 1 ? options.slice(1) : [];
}

/**
 * 🔴 本表单**自己的**输入选项,不是任何服务端状态的译名。
 *
 * 服务端 `ProviderEvidenceBody.evidence_kind` 是一个四值 Literal,且
 * **没有**配套的 catalog 端点或 copy registry 条目 —— 与 `actionCatalog`
 * (队列页那三个按钮的文案由服务端下发)形成对照。这里的中文是「请用户选哪一类凭证」
 * 的题面,属于表单 chrome;但它确实是一处**前端持有的枚举字面量**,已在交付单挂号,
 * 建议后端补一个 evidence-kind catalog,补上之后这张表就删。
 */
export const EVIDENCE_KIND_OPTIONS: ReadonlyArray<{ value: string; text: string }> = [
    { value: 'screenshot_url', text: '发布页面截图链接' },
    { value: 'order_number', text: '媒体方给的订单号' },
    { value: 'contact_log', text: '与媒体方的沟通记录' },
    { value: 'other', text: '其他' },
];
