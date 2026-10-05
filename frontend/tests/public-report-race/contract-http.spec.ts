/**
 * contract-http.spec — 真实链路(httpTransport → mapDto)契约判别测试
 *
 * 针对二轮审查 P1-1 / P1-2:不再用 fixture 证明映射器正确,
 * 全部经 http-probe 场景 + page.route mock 真实 HTTP DTO 驱动。
 */
import { test, expect } from 'playwright/test';
import { gotoReady, onlyPrimaryProject, scenarioUrl } from './helpers';

/** 11 类判定(vNext 全集) */
const ALL_VERDICTS = [
    ['recommended', '明确推荐'],
    ['conditionally_recommended', '条件推荐'],
    ['candidate', '列入备选'],
    ['mentioned', '仅提到'],
    ['criteria_only', '只给标准'],
    ['refused_no_evidence', '证据不足拒答'],
    ['refused_risk', '风险拒答'],
    ['not_mentioned', '未提到'],
    ['brand_confused', '品牌混淆'],
    ['engine_error', '引擎异常'],
    ['no_answer', '无有效回答'],
] as const;

function evidenceItem(verdict: string, i: number) {
    return {
        rowKey: `ev-http-${i}`,
        question: `真实链路问题 ${i}?`,
        platformName: '豆包',
        verdict,
        answerExcerpt: verdict === 'no_answer' || verdict === 'engine_error' ? null : `第 ${i} 条真实回答节选`,
        citedDomains: verdict === 'no_answer' || verdict === 'engine_error' ? [] : ['real.example.cn'],
        evidenceLevel: 'A',
        testedAt: '2026-07-12T03:02:00.000Z',
    };
}

function baseReport(presentation: unknown) {
    return {
        status: 'success',
        report: {
            brand_name: '真实链路品牌',
            score: 63,
            level: '成长级',
            content: '',
            keyword_count: 11,
            report_version: 'v2',
            data_completeness_score: 62,
            data_completeness_breakdown: { level: '可用', missing_summary: null },
            created_at: '2026-07-12T02:30:00.000Z',
            branding_status: 'platform',
            presentation,
        },
    };
}

async function mockReport(page: import('playwright/test').Page, body: unknown) {
    await page.route('**/api/public/report/1', async (route) => {
        await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify(body),
        });
    });
}

test.describe('真实 HTTP DTO 契约(二轮 P1)', () => {
    test('P1-1 11 类判定全部通过真实映射器进入证据矩阵', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await mockReport(
            page,
            baseReport({
                evidence: {
                    status: 'ready',
                    data: {
                        totalCount: ALL_VERDICTS.length,
                        items: ALL_VERDICTS.map(([verdict], i) => evidenceItem(verdict, i + 1)),
                    },
                },
            }),
        );
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        // 证据区必须 ready(不是"暂无数据"降级)
        await expect(page.getByRole('heading', { name: '证据矩阵' })).toBeVisible();
        await expect(page.getByText('1 个平台 · 11 / 11 条')).toBeVisible();
        await page.getByRole('button', { name: '展开豆包的全部问题与回答' }).click();
        for (const [, label] of ALL_VERDICTS) {
            await expect(page.locator('#evidence article').getByText(label, { exact: true }).first()).toBeVisible();
        }
        // 空回答占位与其他判定在同一个平台组完整展示。
        await expect(page.getByText('本次未获取到有效回答文本。').first()).toBeVisible();
    });

    test('P1-1b 未知判定值仍 fail-closed(整区降级,不混入)', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await mockReport(
            page,
            baseReport({
                evidence: {
                    status: 'ready',
                    data: {
                        totalCount: 1,
                        items: [evidenceItem('maybe_probably', 1)],
                    },
                },
            }),
        );
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByText('该部分结构化数据尚未由数据通道开放').first()).toBeVisible();
    });

    test('P1-2 presentation.identity/summary/methodology 点亮首屏与方法区', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await mockReport(
            page,
            baseReport({
                identity: { industry: '全屋定制 / 智能家居' },
                summary: {
                    headline: '真实链路的一句话结论:品牌词稳,新客断。',
                    testedPlatformCount: 4,
                    validAnswerCount: 384,
                    trust: { partialSample: true, levelCapped: true, confidence: 'medium' },
                    nextAction: '先补齐权威品牌证据。',
                },
                methodology: {
                    testedScope: '真实链路方法说明:本次实测 11 题。',
                    timeRange: '2026-07-12(快照)',
                    platformScope: '豆包(以实跑为准)',
                    scoreMethod: '三层加权漏斗口径。',
                    dataLimits: ['缺失值显示为"—",不等于 0。'],
                    extraNotes: [],
                },
            }),
        );
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        // 首屏:行业/结论/平台数/有效回答/可信标注/下一步
        await expect(page.getByText('全屋定制 / 智能家居')).toBeVisible();
        await expect(page.getByText('真实链路的一句话结论:品牌词稳,新客断。')).toBeVisible();
        await expect(page.getByText('4 个', { exact: true })).toBeVisible();
        await expect(page.getByText('384 条', { exact: true })).toBeVisible();
        await expect(page.getByText(/等级已按规则封顶/)).toBeVisible();
        await expect(page.getByText(/本次为部分样本/)).toBeVisible();
        await expect(page.getByText(/先补齐权威品牌证据。/)).toBeVisible();
        // 方法区
        await expect(page.getByText('真实链路方法说明:本次实测 11 题。')).toBeVisible();
        await expect(page.getByText('三层加权漏斗口径。')).toBeVisible();
        await expect(page.getByText('缺失值显示为"—",不等于 0。')).toBeVisible();
    });

    test('P1-2b 非法 summary 载荷降级为 null,不污染其余区块', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await mockReport(
            page,
            baseReport({
                summary: { testedPlatformCount: 'four', trust: 'bogus' },
                evidence: { status: 'ready', data: { totalCount: 1, items: [evidenceItem('recommended', 1)] } },
            }),
        );
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        // 非法 summary → 平台数显示 —;证据区正常
        await expect(page.getByText('实测平台')).toBeVisible();
        await page.getByRole('button', { name: '展开豆包的全部问题与回答' }).click();
        await expect(page.locator('#evidence article').getByText('明确推荐', { exact: true }).first()).toBeVisible();
    });

    test('后端只下发证据窗口时明确披露加载上限', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await mockReport(
            page,
            baseReport({
                evidence: {
                    status: 'ready',
                    data: {
                        totalCount: 384,
                        items: Array.from({ length: 100 }, (_, i) => evidenceItem('mentioned', i + 1)),
                    },
                },
            }),
        );
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByText('1 个平台 · 100 / 384 条（当前加载前 100 条）')).toBeVisible();
    });
});

test.describe('P2-3 移动端首屏信息顺序', () => {
    test('320 视口:评分环先于样本规模;1440 桌面布局不变', async ({ page }, testInfo) => {
        test.skip(!['v320', 'v1440'].includes(testInfo.project.name));
        await gotoReady(page);
        const order = await page.evaluate(() => {
            const ring = document.querySelector('#overview [role="img"][aria-label*="GEO 综合评分"]');
            const kpiLabels = Array.from(document.querySelectorAll('#overview p')).find(
                (p) => p.textContent === '实测平台',
            );
            if (!ring || !kpiLabels) return null;
            const ringRect = ring.getBoundingClientRect();
            const kpiRect = kpiLabels.getBoundingClientRect();
            return { ringTop: ringRect.top, kpiTop: kpiRect.top, ringLeft: ringRect.left, kpiLeft: kpiRect.left };
        });
        expect(order).not.toBeNull();
        if (testInfo.project.name === 'v320') {
            expect(order!.ringTop, '320 下评分环必须先于样本规模').toBeLessThan(order!.kpiTop);
        } else {
            // 桌面:评分在右侧(x 更大),KPI 在左下
            expect(order!.ringLeft).toBeGreaterThan(order!.kpiLeft);
        }
    });
});
