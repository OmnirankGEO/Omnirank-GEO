/**
 * fixtures/partialData — 部分数据缺失场景
 * 漏斗单层覆盖触发等级封顶、竞品无可靠分母、多项指标缺失(显示 "—" 而非 0)
 * ⚠️ 仅 harness/Playwright 使用。
 */
import { baseShell, evidenceItem, fixtureLevel, funnelLayer } from './shared';

export const partialData = baseShell({
    identity: {
        brandName: '青屿咖啡烘焙',
        industry: null,
        diagnosedAt: '2026-07-10T08:00:00.000Z',
        dataUpdatedAt: null,
        reportVersionLabel: 'v2 · 客户版',
        whitelabel: null,
        brandingStatus: 'platform',
    },
    summary: {
        // 与后端 SSOT 一致：仅品牌层 9/12 有样本 → 权重重归一后 75(健康级),
        // 单层覆盖触发封顶规则 → 成长级(funnel_score.py:213-221)
        geoScore: 75,
        scoreLevel: fixtureLevel(
            '成长级',
            '本次只有品牌认知层有足够样本，等级已按规则封顶，避免对缺失部分过度承诺。',
        ),
        dataCompletenessScore: 31,
        dataCompletenessLevel: '严重不足',
        dataCompletenessMissingSummary: '缺少行业资料、关键词覆盖与竞品输入，本次结论仅覆盖品牌词问题。',
        headline: null,
        testedPlatformCount: 4,
        testedQuestionCount: 24,
        validAnswerCount: 96,
        trust: { partialSample: true, levelCapped: true, confidence: 'medium' },
        nextAction: null,
    },
    funnel: {
        status: 'ready',
        data: {
            totalScore: 75,
            trust: { partialSample: true, levelCapped: true, confidence: 'medium' },
            layers: [
                funnelLayer({
                    key: 'brand',
                    label: '品牌认知层',
                    description: '客户主动搜你的品牌名,AI 能否答对。',
                    businessMeaning: '底线 · 守不住，老客回头都会丢。',
                    detected: 9,
                    total: 12,
                    ratePct: 75,
                    score: 75,
                    weight: 20,
                }),
                funnelLayer({
                    key: 'local',
                    label: '决策获客层',
                    description: '客户搜「行业+地区」「行业+选哪家」时,AI 是否把你推进选项。',
                    businessMeaning: '本次未实测，原因：关键词覆盖不足。',
                    detected: null,
                    total: null,
                    dataSufficient: false,
                    confidence: null,
                }),
                funnelLayer({
                    key: 'scenario',
                    label: '场景转化层',
                    description: '客户搜「具体方案 / 对比 / 避坑」等高决策意图词时,AI 是否引用你。',
                    businessMeaning: '本次未实测，原因：关键词覆盖不足。',
                    detected: null,
                    total: null,
                    dataSufficient: false,
                    confidence: null,
                }),
            ],
        },
    },
    platforms: {
        status: 'ready',
        data: [
            {
                platformName: '豆包',
                validSamples: 24,
                detectionRatePct: 71,
                mentionRatePct: 42,
                recommendRatePct: null,
                citationCount: 6,
                dataStatus: '样本有限',
                updatedAt: '2026-07-10T08:00:00.000Z',
            },
            {
                platformName: '通义千问',
                validSamples: 24,
                detectionRatePct: 67,
                mentionRatePct: null,
                recommendRatePct: null,
                citationCount: 4,
                dataStatus: '样本有限',
                updatedAt: '2026-07-10T08:00:00.000Z',
            },
            {
                platformName: 'DeepSeek',
                validSamples: 24,
                detectionRatePct: null,
                mentionRatePct: null,
                recommendRatePct: null,
                citationCount: null,
                dataStatus: '部分失败',
                updatedAt: '2026-07-10T08:00:00.000Z',
            },
        ],
    },
    findings: {
        status: 'ready',
        data: [
            {
                category: 'profile_gap',
                text: '本次诊断仅覆盖品牌词问题，行业与场景类问题未实测，无法判断新客链路状态。',
                evidenceLevel: null,
                sampleCount: 12,
                scope: '全部平台 · 品牌词类问题',
                detail: '资料完整度 31/100,缺少行业深度资料与关键词覆盖。',
                insufficientNote: '数据不足，以下结论仅供参考。',
            },
        ],
    },
    evidence: {
        status: 'ready',
        data: {
            totalCount: 3,
            items: [
                evidenceItem('ev-p1', '青屿咖啡烘焙的豆子怎么样?', '豆包', 'mentioned',
                    '青屿咖啡烘焙是一家本地精品烘焙店，公开资料不多，评价整体正面。',
                    ['qingyu-coffee.example.cn'], 'B', '2026-07-10T08:01:00.000Z'),
                evidenceItem('ev-p2', '青屿咖啡烘焙的豆子怎么样?', '通义千问', 'candidate',
                    '如果喜欢中浅烘的果酸风味，青屿咖啡烘焙可以列入尝试清单。',
                    ['qingyu-coffee.example.cn'], 'A', '2026-07-10T08:02:00.000Z'),
                evidenceItem('ev-p3', '青屿咖啡烘焙的豆子怎么样?', 'DeepSeek', 'no_answer',
                    null, [], 'C', null),
            ],
        },
    },
    competitive: {
        status: 'empty',
        message: '本次样本不足以形成可靠的竞品对比，只展示事实，不给结论。',
    },
    actions: {
        status: 'ready',
        data: [
            {
                priorityLabel: 'P0',
                title: '先补齐诊断资料',
                why: '本次仅品牌词问题有样本，其余两层未实测，当前分数不能代表完整状态。',
                evidenceRowKeys: [],
                impactScope: '报告可信度',
                suggestedPeriod: '第 1 周',
                detailMd: '补齐行业资料与关键词覆盖后重新诊断，再看完整漏斗。',
            },
        ],
    },
    thirtyDayPlan: { status: 'unavailable', message: '资料补齐并完成完整诊断后，再生成个性化 30 天路径。' },
    narrativeMd: '## 本次诊断说明\n\n受资料完整度限制，本次只对品牌词问题做了实测。**当前分数不能代表完整状态**,补齐资料后建议重新诊断。',
    methodology: {
        testedScope: '本次仅覆盖品牌词类问题(24 题 × 4 平台)，行业与场景类问题因关键词覆盖不足未实测。',
        timeRange: '2026-07-10(诊断当天快照)',
        platformScope: '豆包、通义千问、DeepSeek(以本次实际实测为准)',
        scoreMethod:
            'GEO 综合评分采用三层加权漏斗；本次仅品牌认知层有样本，权重重归一后得分 75(达「健康级」)，按封顶规则降为「成长级」，避免对未实测部分过度承诺。',
        dataLimits: [
            '本次结论仅覆盖品牌词问题，不能推断新客链路状态。',
            '缺失值显示为"—"，不等于 0。',
            '竞品对比因无可靠分母未给出。',
        ],
        extraNotes: [],
    },
});
