import { expect, test } from 'playwright/test';
import { expectNoHorizontalOverflow, gotoReady, onlyPrimaryProject } from './helpers';

test.describe('报告主题、证据与数据口径', () => {
    test('报告默认锁定浅色，且可显式切换为深色', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.addInitScript(() => {
            document.documentElement.classList.add('dark');
            localStorage.removeItem('omnirank-public-report-theme');
        });
        await gotoReady(page);

        const report = page.locator('[data-public-report-theme]');
        await expect(report).toHaveAttribute('data-public-report-theme', 'light');
        await expect(page.getByRole('button', { name: '切换为深色报告' })).toBeVisible();
        expect(await report.evaluate((node) => getComputedStyle(node).backgroundColor))
            .toMatch(/^(?:rgb\(248, 250, 252\)|oklch\(0\.984\b)/);

        await page.getByRole('button', { name: '切换为深色报告' }).click();
        await expect(report).toHaveAttribute('data-public-report-theme', 'dark');
        await expect(page.getByRole('button', { name: '切换为浅色报告' })).toBeVisible();
        expect(await report.evaluate((node) => getComputedStyle(node).backgroundColor)).toBe('rgb(2, 6, 23)');
    });

    test('证据回答按安全 Markdown 渲染，展开面板在明暗主题都保持对比', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'long-content');

        const evidence = page.locator('#evidence');
        await evidence.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().click();
        const detail = evidence.locator('.prp-expand-surface').first();
        await expect(detail.locator('strong')).toContainText('明确结论');
        await expect(detail.locator('ol li')).toHaveCount(3);
        expect(await detail.evaluate((node) => getComputedStyle(node).backgroundColor))
            .toMatch(/^(?:rgb\(248, 250, 252\)|oklch\(0\.984\b)/);

        await page.getByRole('button', { name: '切换为深色报告' }).click();
        expect(await detail.evaluate((node) => getComputedStyle(node).backgroundColor)).toBe('rgb(17, 24, 39)');
        const textColor = await detail.locator('.prp-markdown').evaluate((node) => getComputedStyle(node).color);
        expect(textColor).toBe('rgb(226, 232, 240)');
    });

    test('指标缺失明确显示未采集，不再与真实 0 混淆', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'partial-data');
        const platforms = page.locator('#platforms');
        await expect(platforms.getByText('未采集', { exact: true }).first()).toBeVisible();
        await expect(platforms.getByText(/“未采集”不等于 0/)).toBeVisible();
    });

    test('320/390 已批准白标与主题、打印、菜单入口不重叠', async ({ page }, testInfo) => {
        test.skip(!['v320', 'v390'].includes(testInfo.project.name));
        await gotoReady(page, 'approved-whitelabel');

        const brand = page.getByTestId('approved-whitelabel-brand');
        await expect(brand).toBeVisible();
        await expect(brand).toContainText('远山 GEO');
        await expect(page.getByRole('button', { name: '切换为深色报告' })).toBeVisible();
        await expect(page.getByRole('button', { name: '下载 / 打印' })).toBeVisible();
        await expect(page.getByRole('button', { name: '打开菜单' })).toBeVisible();
        await expectNoHorizontalOverflow(page);

        const brandBox = await brand.boundingBox();
        const controlsBox = await page.getByRole('button', { name: '切换为深色报告' }).boundingBox();
        expect(brandBox).not.toBeNull();
        expect(controlsBox).not.toBeNull();
        expect(brandBox!.x + brandBox!.width).toBeLessThanOrEqual(controlsBox!.x);
    });
});
