/**
 * fixtures/longBrandName — 超长品牌名/超长平台名/超长文案压力场景
 * ⚠️ 仅 harness/Playwright 使用。
 */
import { readyFull } from './readyFull';
import type { PlatformPerformance } from '../contract/types';

const LONG_BRAND =
    '澜川智能家居全屋定制高端整装一体化解决方案旗舰品牌(上海徐汇滨江总店)LanChuan Smart Home Premium Whole-House Customization';

const longPlatforms: readonly PlatformPerformance[] = [
    {
        platformName: '豆包',
        validSamples: 96,
        detectionRatePct: 68,
        mentionRatePct: 45,
        recommendRatePct: 12,
        citationCount: 23,
        dataStatus: '稳定',
        updatedAt: '2026-07-12T05:41:00.000Z',
    },
    {
        platformName: '通义千问(国际版长名称压力测试平台)Qwen-International-LongName',
        validSamples: 96,
        detectionRatePct: 61,
        mentionRatePct: 38,
        recommendRatePct: 9,
        citationCount: 17,
        dataStatus: '稳定',
        updatedAt: '2026-07-12T05:41:00.000Z',
    },
];

export const longBrandName = {
    ...readyFull,
    identity: {
        ...readyFull.identity,
        brandName: LONG_BRAND,
        industry: '全屋定制 / 智能家居 / 高端整装 / 商业空间设计 / 智能家居系统集成与运维服务',
    },
    platforms: { status: 'ready' as const, data: longPlatforms },
    summary: {
        ...readyFull.summary,
        headline:
            '这是一条故意拉长的一句话结论，用来验证深墨色摘要区在超长文案下的换行、留白与可读性:AI 在品牌词上已经认识你，但在客户真正做选择的问题里，还很少把你放进候选，接下来三十天最该解决的是让新客户在选择阶段看见你。',
    },
};
