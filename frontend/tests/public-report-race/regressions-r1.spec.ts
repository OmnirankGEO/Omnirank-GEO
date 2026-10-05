/**
 * regressions-r1.spec — R1 对抗审核 CONFIRMED 修复的判别测试
 * 每条对应一个 R1 修复;删除对应修复应立即转红。
 */
import { test, expect } from 'playwright/test';
import { gotoReady, onlyPrimaryProject, scenarioUrl } from './helpers';
import { readyFull } from '../../src/features/publicReportPremium/fixtures/readyFull';
import { partialData } from '../../src/features/publicReportPremium/fixtures/partialData';

test.describe('R1 修复判别', () => {
    test('R1C-1 未批准白标:页尾与顶导航都不展示品牌字段', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/1', async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    report: {
                        brand_name: '门控测试品牌',
                        score: 47,
                        level: '边缘级',
                        content: '',
                        keyword_count: 24,
                        report_version: 'v2',
                        branding_status: 'platform',
                        whitelabel: {
                            company_name: 'UNAPPROVED-CO-未批公司',
                            product_name: 'UNAPPROVED-PROD-未批产品',
                            slogan: 'UNAPPROVED-SLOGAN-未批标语',
                            logo_url: 'https://evil.example.com/l.png',
                        },
                    },
                }),
            });
        });
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { level: 1 })).toContainText('门控测试品牌');
        const body = await page.locator('body').innerText();
        expect(body).not.toContain('UNAPPROVED-CO-未批公司');
        expect(body).not.toContain('UNAPPROVED-PROD-未批产品');
        expect(body).not.toContain('UNAPPROVED-SLOGAN-未批标语');
        // logo 也不请求
        await expect(page.locator('img[src*="evil.example.com"]')).toHaveCount(0);
    });

    test('R1B-2 帮助对话框 Esc 关闭后焦点归还触发按钮', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        const helpBtn = page.getByRole('button', { name: '数据怎么看' }).first();
        await helpBtn.click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
        const focused = await page.evaluate(() => document.activeElement?.textContent?.trim());
        expect(focused).toContain('数据怎么看');
    });

    test('R1B-3 reduced-motion:main 区域零运行动画且内容直接可读', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await gotoReady(page);
        // 首屏立即终态(无渐显过程)
        const running = await page.evaluate(() =>
            document
                .getAnimations()
                .filter((a) => a.playState === 'running' || a.playState === 'pending').length,
        );
        expect(running).toBe(0);
        // 展开交互也是瞬时(reduced 下 MotionExpand 纯条件渲染)
        const firstCard = page.locator('#findings .grid > div').first();
        await firstCard.getByRole('button', { name: /证据摘要/ }).click();
        const runningAfterExpand = await page.evaluate(() =>
            document
                .getAnimations()
                .filter((a) => a.playState === 'running' || a.playState === 'pending').length,
        );
        expect(runningAfterExpand).toBe(0);
        await expect(firstCard.getByText(/19\/20；其中 6 条回答引用了/)).toBeVisible();
    });

    test('R1B-4 390 手机端:摘要 KPI 与平台指标严格单列', async ({ page }, testInfo) => {
        test.skip(testInfo.project.name !== 'v390');
        await gotoReady(page);
        const layout = await page.evaluate(() => {
            const kpi = document.querySelector('#overview .grid.grid-cols-1');
            const metricGrids = Array.from(
                document.querySelectorAll('#platforms ul .grid'),
            ).map((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
            return {
                kpiExists: Boolean(kpi),
                kpiColumns: kpi ? getComputedStyle(kpi).gridTemplateColumns.split(' ').length : 0,
                metricGrids,
            };
        });
        expect(layout.kpiExists).toBe(true);
        expect(layout.kpiColumns).toBe(1);
        expect(layout.metricGrids.length).toBeGreaterThan(0);
        expect(layout.metricGrids.every((n) => n === 1)).toBe(true);
    });

    test('R1A-1 partial-data 与后端 SSOT 一致:75 分 + 成长级 + 封顶标注', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'partial-data');
        await expect(page.getByLabel('GEO 综合评分 75 分，满分 100')).toBeVisible();
        await expect(page.getByText('成长级').first()).toBeVisible();
        await expect(page.getByText(/等级已按规则封顶/).first()).toBeVisible();
        // 后端不可能产生的 38 分不得出现
        await expect(page.getByText('38', { exact: true })).toHaveCount(0);
    });

    test('R1A-1/2 fixture 算术与后端 funnel 公式自洽(node 级)', async ({}, testInfo) => {
        onlyPrimaryProject(testInfo);
        // readyFull:三层得分之和 ≈ 总分(后端 round 到整数,容差 0.5)
        if (readyFull.funnel.status !== 'ready') throw new Error('readyFull funnel 必须 ready');
        const layerSum = readyFull.funnel.data.layers.reduce((acc, l) => acc + (l.score ?? 0), 0);
        expect(Math.abs(layerSum - (readyFull.funnel.data.totalScore ?? 0))).toBeLessThanOrEqual(0.5);
        // 每层 score ≈ ratePct/100 × weight(容差 0.15,后端 round 1 位小数)
        for (const layer of readyFull.funnel.data.layers) {
            if (layer.ratePct === null || layer.weight === null || layer.score === null) continue;
            const expected = (layer.ratePct / 100) * layer.weight;
            expect(Math.abs(layer.score - expected)).toBeLessThanOrEqual(0.15);
        }
        // partialData:总分=层得分=75,等级=成长级,封顶=true(与后端重归一+封顶一致)
        if (partialData.funnel.status !== 'ready') throw new Error('partialData funnel 必须 ready');
        expect(partialData.funnel.data.totalScore).toBe(75);
        expect(partialData.summary.geoScore).toBe(75);
        expect(partialData.summary.scoreLevel?.label).toBe('成长级');
        expect(partialData.summary.trust.levelCapped).toBe(true);
    });

    test('R1C-2 已批准白标的 http: 明文 logo 被拒,回退文字标识', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/1', async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'success',
                    report: {
                        brand_name: '明文 logo 测试',
                        score: 47,
                        level: '边缘级',
                        content: '',
                        keyword_count: 24,
                        report_version: 'v2',
                        branding_status: 'approved_whitelabel',
                        whitelabel: {
                            company_name: '明文公司',
                            product_name: null,
                            slogan: null,
                            logo_url: 'http://insecure.example.com/logo.png',
                        },
                    },
                }),
            });
        });
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { level: 1 })).toContainText('明文 logo 测试');
        await expect(page.locator('nav img[src*="insecure.example.com"]')).toHaveCount(0);
        await expect(page.locator('nav').getByText('明文公司', { exact: true })).toBeVisible();
    });
});
