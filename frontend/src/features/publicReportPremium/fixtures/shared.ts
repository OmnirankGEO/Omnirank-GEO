/**
 * fixtures/shared — 赛马 fixture 公共构造器
 *
 * ⚠️ 仅 race harness 与 Playwright 使用；生产组件禁止 import。
 * fixture 的分数/等级/等级色严格对齐后端 SSOT(tools/scoring/funnel_score.py
 * FUNNEL_LEVEL_META)，模拟统一后端 presentation 契约会下发的形态，
 * 不虚构任何与 SSOT 冲突的映射关系。
 */
import type {
    EvidenceItem,
    EvidenceVerdict,
    FunnelLayer,
    PublicReportPresentationV1,
    ScoreLevel,
} from '../contract/types';

/** v2 等级 SSOT 元数据(与 funnel_score.py FUNNEL_LEVEL_META 逐值一致) */
export const FIXTURE_LEVEL_META = {
    主导级: { colorHex: '#059669', summary: '你已经是 AI 默认推荐 · 守城为主' },
    健康级: { colorHex: '#16a34a', summary: '主流问答 AI 都给到你 · 持续优化拉高' },
    成长级: { colorHex: '#d97706', summary: '部分关键问题给到你 · 一半流量没打透' },
    边缘级: { colorHex: '#ea580c', summary: 'AI 偶尔提你 · 多数新客触达不到' },
    危急级: { colorHex: '#dc2626', summary: 'AI 几乎只在客户搜品牌名时才提你 · 新客链路基本断' },
    隐形级: { colorHex: '#9f1239', summary: 'AI 完全不认识你 · 增长靠老客单点支撑' },
} as const;

export type FixtureLevelLabel = keyof typeof FIXTURE_LEVEL_META;

export function fixtureLevel(label: FixtureLevelLabel, businessMeaning: string): ScoreLevel {
    return {
        label,
        colorHex: FIXTURE_LEVEL_META[label].colorHex,
        summary: FIXTURE_LEVEL_META[label].summary,
        businessMeaning,
    };
}

export function funnelLayer(partial: Partial<FunnelLayer> & Pick<FunnelLayer, 'key' | 'label'>): FunnelLayer {
    const total = partial.total ?? null;
    return {
        key: partial.key,
        label: partial.label,
        description: partial.description ?? null,
        businessMeaning: partial.businessMeaning ?? null,
        detected: partial.detected ?? null,
        total,
        ratePct: partial.ratePct ?? null,
        score: partial.score ?? null,
        weight: partial.weight ?? null,
        dataSufficient: partial.dataSufficient ?? (total !== null && total >= 5),
        confidence: partial.confidence ?? (total === null ? null : total >= 15 ? 'high' : total >= 5 ? 'medium' : 'low'),
    };
}

export function evidenceItem(
    rowKey: string,
    question: string,
    platformName: string,
    verdict: EvidenceVerdict,
    answerExcerpt: string | null,
    citedDomains: readonly string[],
    evidenceLevel: 'A' | 'B' | 'C' | null,
    testedAt: string | null,
): EvidenceItem {
    return { rowKey, question, platformName, verdict, answerExcerpt, citedDomains, evidenceLevel, testedAt };
}

/** 所有 fixture 共享的方法说明骨架(动态字段由各 fixture 覆盖) */
export const BASE_METHODOLOGY = {
    testedScope:
        '本次诊断围绕品牌真实经营场景构造客户问题，逐一在主流 AI 问答平台实测，记录品牌是否被识别、提及、列入候选与出现正面措辞，并留存回答原文作为证据。',
    timeRange: null as string | null,
    platformScope: null as string | null,
    scoreMethod:
        'GEO 综合评分采用三层加权漏斗：品牌认知层(权重 20)、决策获客层(权重 40)、场景转化层(权重 40)；每层得分为实测命中率 × 权重，空样本层不参与加权。等级由总分按统一阈值映射，全报告同一口径。',
    dataLimits: [
        '本报告是诊断时刻的快照，不代表任何平台的永久排名或效果承诺。',
        '样本不足的指标会明确标注"数据不足"；缺失值显示为"—"，不等于 0。',
        '证据等级含义:A=直接证据(回答点名品牌，可支撑具体结论);B=参考证据(品类旁证，只作倾向性参考);C=待验证线索(只表示可能或推测，需进一步验证)。',
    ] as readonly string[],
    extraNotes: [] as readonly string[],
};

export function baseShell(
    overrides: Pick<PublicReportPresentationV1, 'identity' | 'summary'> &
        Partial<PublicReportPresentationV1>,
): PublicReportPresentationV1 {
    return {
        contractVersion: 'public-report-presentation/v1',
        funnel: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        platforms: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        findings: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        evidence: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        competitive: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        actions: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        thirtyDayPlan: { status: 'unavailable', message: '该部分结构化数据尚未由数据通道开放。' },
        narrativeMd: null,
        methodology: { ...BASE_METHODOLOGY },
        ...overrides,
    };
}
