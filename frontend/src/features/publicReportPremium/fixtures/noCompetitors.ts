/**
 * fixtures/noCompetitors + noBaseline — 无竞品数据 / 无行业基准场景
 * ⚠️ 仅 harness/Playwright 使用。
 */
import { readyFull } from './readyFull';

export const noCompetitors = {
    ...readyFull,
    competitive: {
        status: 'empty' as const,
        message: '本次实测未形成可信的竞品对照数据，暂不展示竞争格局。',
    },
    identity: { ...readyFull.identity, brandName: '北屿宠物医院' },
};

export const noBaseline = {
    ...readyFull,
    identity: { ...readyFull.identity, brandName: '南屿健身工作室' },
    // 方法说明中显式声明无行业基准(后端已移除合成基准，前端不得展示参考线)
    methodology: {
        ...readyFull.methodology,
        extraNotes: [
            '当前报告不包含行业平均值或参考线：没有带样本版本的行业基准数据，任何"行业平均"都不会被展示。',
        ],
    },
};
