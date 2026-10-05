/**
 * regressions.spec — 对抗审计确认项的判别测试。
 * 每条都针对具体修复;删除对应修复应立即转红。
 */
import { test, expect } from 'playwright/test';
import { gotoReady, onlyPrimaryProject, scenarioUrl } from './helpers';

test.describe('审计修复判别', () => {
    test('1024 主摘要与漏斗保持单栏', async ({ page }, testInfo) => {
        test.skip(testInfo.project.name !== 'v1024');
        await gotoReady(page);
        const layout = await page.evaluate(() => {
            const heroGrid = document.querySelector('#overview .grid');
            const funnel = document.querySelector('#funnel ol');
            return {
                heroColumns: heroGrid ? getComputedStyle(heroGrid).gridTemplateColumns.split(' ').length : 0,
                funnelDirection: funnel ? getComputedStyle(funnel).flexDirection : '',
            };
        });
        expect(layout.heroColumns).toBe(1);
        expect(layout.funnelDirection).toBe('column');
    });

    test('reduced-motion:骨架停止 pulse 且就绪数值直接终态', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await page.goto(scenarioUrl('slow'), { waitUntil: 'domcontentloaded' });
        const skeleton = page.locator('[aria-busy="true"]');
        await expect(skeleton).toBeVisible();
        const animationNames = await skeleton.locator('.animate-pulse').evaluateAll((nodes) =>
            nodes.map((node) => getComputedStyle(node).animationName),
        );
        expect(animationNames.every((name) => name === 'none')).toBe(true);
        await expect(page.getByRole('heading', { name: '证据矩阵' })).toBeVisible({ timeout: 10_000 });
        await expect(page.getByText('4 个', { exact: true })).toBeVisible();
    });

    test('Evidence C 只作可能/推测且明确需验证', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        await expect(page.getByText(/只表示可能或推测，需进一步验证/).first()).toBeVisible();
        const body = await page.locator('body').innerText();
        expect(body).not.toContain('C=待验证线索(仅证明覆盖)');
        expect(body).not.toContain('无有效回答，仅证明数据覆盖');
    });

    test('platform 默认使用真实 OmniRank 资产;批准白标加载与失败回退都可读', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        await expect(page.locator('nav').getByText('OmniRank', { exact: true })).toBeVisible();

        await gotoReady(page, 'approved-whitelabel');
        await expect(page.getByTestId('approved-whitelabel-brand').locator('img')).toBeVisible();
        await expect(page.locator('nav').getByText('远山 GEO', { exact: true })).toBeVisible();

        await gotoReady(page, 'broken-whitelabel');
        await expect(page.locator('nav').getByText('远山 GEO', { exact: true })).toBeVisible();
    });

    test('无个性化 30 天数据仍展示明确标注的通用方法', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'generic-plan');
        await expect(page.getByText(/当前改以通用方法展示，不代表针对本品牌的诊断结论/)).toBeVisible();
        await expect(page.getByText('第 1 周', { exact: true })).toBeVisible();
        await expect(page.getByText(/非个性化诊断结论/)).toBeVisible();
    });

    test('DTO score/level 不成对时两者一起 fail-closed', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'dto-incomplete-score-level');
        await expect(page.getByLabel('GEO 综合评分暂无数据')).toBeVisible();
        await expect(page.getByText('等级暂无数据')).toBeVisible();
        await expect(page.getByText('47', { exact: true })).toHaveCount(0);
        await expect(page.getByText('62/100', { exact: true })).toBeVisible();
    });

    test('404 原始 detail 永不进入公开 DOM', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/1', async (route) => {
            await route.fulfill({
                status: 404,
                contentType: 'application/json',
                body: JSON.stringify({ detail: 'postgres stack owner_user_id INTERNAL_SENTINEL' }),
            });
        });
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '链接无效或已过期' })).toBeVisible();
        const body = await page.locator('body').innerText();
        expect(body).not.toMatch(/postgres|owner_user_id|INTERNAL_SENTINEL/i);
    });

    test('not_ready 原始 message/detail 永不进入公开 DOM', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/1', async (route) => {
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    status: 'not_ready',
                    message: 'SELECT secret FROM diagnosis_records',
                    detail: { message: 'upstream_user_id=INTERNAL_SENTINEL' },
                }),
            });
        });
        await page.goto(scenarioUrl('http-probe'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '客户报告尚未就绪' })).toBeVisible();
        const body = await page.locator('body').innerText();
        expect(body).not.toMatch(/SELECT secret|diagnosis_records|upstream_user_id|INTERNAL_SENTINEL/i);
    });
});
