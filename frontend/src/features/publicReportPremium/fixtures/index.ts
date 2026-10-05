/**
 * fixtures/index — 赛马场景注册表
 * ⚠️ 仅 race harness 与 Playwright 使用；生产组件禁止 import。
 */
import type { PublicReportPresentationV1 } from '../contract/types';
import type { PublicReportLoadResult } from '../transport/transport';
import { readyFull } from './readyFull';
import { partialData } from './partialData';
import { noBaseline, noCompetitors } from './noCompetitors';
import { longBrandName } from './longBrandName';
import { longContent } from './longContent';
import { manyEvidence } from './manyEvidence';
import omnirankLogo from '@/assets/logo.png';
import { mapDtoToPresentation } from '../transport/mapDto';

export interface FixtureScenario {
    /** 场景说明(harness 场景切换器展示用) */
    readonly label: string;
    /** 模拟网络延迟 ms(默认 120;slow 场景用于 loading 骨架验收) */
    readonly delayMs: number;
    readonly resolve: () => PublicReportLoadResult;
}

function ready(report: PublicReportPresentationV1): PublicReportLoadResult {
    return { kind: 'ready', report };
}

export const FIXTURE_SCENARIOS: Record<string, FixtureScenario> = {
    'ready-full': {
        label: '完整报告(主演示)',
        delayMs: 120,
        resolve: () => ready(readyFull),
    },
    'partial-data': {
        label: '部分数据缺失(单层覆盖/封顶/无竞品)',
        delayMs: 120,
        resolve: () => ready(partialData),
    },
    'no-competitors': {
        label: '无竞品数据',
        delayMs: 120,
        resolve: () => ready(noCompetitors),
    },
    'no-baseline': {
        label: '无行业基准(显式声明)',
        delayMs: 120,
        resolve: () => ready(noBaseline),
    },
    'long-brand': {
        label: '超长品牌名/平台名/结论',
        delayMs: 120,
        resolve: () => ready(longBrandName),
    },
    'long-content': {
        label: '超长 Markdown + 超长证据',
        delayMs: 120,
        resolve: () => ready(longContent),
    },
    'many-evidence': {
        label: '大量证据(96 条分页)',
        delayMs: 120,
        resolve: () => ready(manyEvidence),
    },
    'generic-plan': {
        label: '无个性化 30 天路径',
        delayMs: 120,
        resolve: () => ready({
            ...readyFull,
            thirtyDayPlan: { status: 'unavailable', message: '个性化路径数据尚未开放。' },
        }),
    },
    'approved-whitelabel': {
        label: '已批准白标 logo',
        delayMs: 120,
        resolve: () => ready({
            ...readyFull,
            identity: {
                ...readyFull.identity,
                brandingStatus: 'approved_whitelabel',
                whitelabel: {
                    companyName: '远山增长顾问',
                    productName: '远山 GEO',
                    logoUrl: omnirankLogo,
                    slogan: null,
                    brandColor: null,
                },
            },
        }),
    },
    'broken-whitelabel': {
        label: '白标 logo 加载失败回退',
        delayMs: 120,
        resolve: () => ready({
            ...readyFull,
            identity: {
                ...readyFull.identity,
                brandingStatus: 'approved_whitelabel',
                whitelabel: {
                    companyName: '远山增长顾问',
                    productName: '远山 GEO',
                    logoUrl: '/definitely-missing-public-report-logo.png',
                    slogan: null,
                    brandColor: null,
                },
            },
        }),
    },
    'dto-incomplete-score-level': {
        label: 'DTO 分数等级不成对',
        delayMs: 120,
        resolve: () => ready(mapDtoToPresentation({
            brand_name: '契约降级示例',
            score: 47,
            level: null,
            data_completeness_score: 62,
            keyword_count: 24,
            report_version: 'v2',
        })),
    },
    'not-ready': {
        label: '客户报告尚未就绪(not_ready)',
        delayMs: 120,
        resolve: () => ({
            kind: 'not_ready',
            message: '客户报告尚未就绪，本次客户版数据不足。请稍后刷新，或联系发给你链接的人。',
        }),
    },
    pending: {
        label: '报告生成中(pending)',
        delayMs: 120,
        resolve: () => ({ kind: 'not_ready', message: '报告正在生成中，请稍后刷新查看。' }),
    },
    'invalid-link': {
        label: '链接已失效(invalid_link)',
        delayMs: 120,
        resolve: () => ({ kind: 'invalid_link', message: '链接已失效。请联系发你链接的人重新发送。' }),
    },
    error: {
        label: '加载异常(error)',
        delayMs: 120,
        resolve: () => ({ kind: 'error', message: '报告暂时打不开，请稍后重试。' }),
    },
    slow: {
        label: '慢网 loading(骨架验收)',
        delayMs: 4000,
        resolve: () => ready(readyFull),
    },
};

export const DEFAULT_SCENARIO = 'ready-full';
