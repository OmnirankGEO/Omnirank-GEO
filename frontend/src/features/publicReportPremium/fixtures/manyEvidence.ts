/**
 * fixtures/manyEvidence — 大量证据分页压力场景(96 条证据)
 * ⚠️ 仅 harness/Playwright 使用。确定性生成，无随机。
 */
import { readyFull } from './readyFull';
import type { EvidenceItem, EvidenceVerdict } from '../contract/types';

const PLATFORMS = ['通义千问', 'DeepSeek', '豆包', '元宝'] as const;
// 覆盖统一观测 vNext 判定全集(含旧版 no_answer 兼容)
const VERDICTS: readonly EvidenceVerdict[] = [
    'recommended',
    'conditionally_recommended',
    'candidate',
    'mentioned',
    'criteria_only',
    'refused_no_evidence',
    'not_mentioned',
    'brand_confused',
    'no_answer',
    'engine_error',
];
const QUESTIONS = [
    '上海全屋定制品牌哪家值得推荐?',
    '澜川智能家居怎么样，靠谱吗?',
    '全屋定制避坑指南：报价里有哪些常见套路?',
    '智能家居和全屋定制一起做，选哪家更省心?',
    '澜川智能家居的报价大概在什么区间?',
    '定制柜和成品柜怎么选?',
    '上海智能家居集成商推荐，要能做全屋联动的',
    '全屋定制签合同要注意什么?',
] as const;

function buildItems(): readonly EvidenceItem[] {
    const items: EvidenceItem[] = [];
    for (let i = 0; i < 96; i += 1) {
        const verdict = VERDICTS[i % VERDICTS.length];
        const platform = PLATFORMS[i % PLATFORMS.length];
        const question = QUESTIONS[i % QUESTIONS.length];
        items.push({
            rowKey: `ev-m${String(i + 1).padStart(2, '0')}`,
            question,
            platformName: platform,
            verdict,
            answerExcerpt:
                verdict === 'no_answer' || verdict === 'engine_error'
                    ? null
                    : `第 ${i + 1} 条实测回答节选：围绕「${question}」，平台给出了${verdict === 'recommended' ? '正面措辞' : verdict === 'conditionally_recommended' ? '带条件措辞' : verdict === 'candidate' ? '备选提及' : verdict === 'mentioned' ? '一般提及' : verdict === 'refused_no_evidence' || verdict === 'refused_risk' ? '拒绝直接回答' : '未涉及品牌'}的回答，节选保留原文关键句以供核对。`,
            citedDomains: verdict === 'no_answer' || verdict === 'engine_error' ? [] : [`source-${(i % 9) + 1}.example.cn`],
            evidenceLevel:
                verdict === 'recommended' || verdict === 'conditionally_recommended' || verdict === 'candidate'
                    ? 'A'
                    : verdict === 'mentioned' || verdict === 'criteria_only'
                      ? 'B'
                      : 'C',
            testedAt: '2026-07-12T03:02:00.000Z',
        });
    }
    return items;
}

export const manyEvidence = {
    ...readyFull,
    identity: { ...readyFull.identity, brandName: '澜川智能家居' },
    evidence: {
        status: 'ready' as const,
        data: { totalCount: 96, items: buildItems() },
    },
};
