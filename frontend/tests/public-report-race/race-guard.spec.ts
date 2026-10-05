/**
 * race-guard.spec — 请求竞态守卫判别测试(三轮审查 P1)
 *
 * 判别点:A 挂起 → 切 B → B 先返回 → A 后返回,页面必须始终保留 B;
 * 连续快速切换以最后一次为准;旧响应(含卸载后)不得落状态。
 */
import { test, expect } from 'playwright/test';
import { onlyPrimaryProject, scenarioUrl } from './helpers';

function reportPayload(brandName: string, score: number, level: string) {
    return {
        status: 'success',
        report: {
            brand_name: brandName,
            score,
            level,
            content: '',
            keyword_count: 8,
            report_version: 'v2',
            data_completeness_score: 50,
            created_at: '2026-07-12T02:30:00.000Z',
            branding_status: 'platform',
        },
    };
}

test.describe('请求竞态守卫', () => {
    test('慢 A 后返回不覆盖先到的 B(代际守卫)', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/100', async (route) => {
            await new Promise((r) => setTimeout(r, 1600));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告A-旧慢响应', 12, '隐形级')),
            });
        });
        await page.route('**/api/public/report/200', async (route) => {
            await new Promise((r) => setTimeout(r, 120));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告B-新快响应', 88, '主导级')),
            });
        });

        await page.goto(scenarioUrl('race-lab'), { waitUntil: 'domcontentloaded' });
        // A 还在挂起时切到 B
        await page.getByRole('button', { name: '切到报告 B' }).click();
        await expect(page.getByRole('heading', { level: 1 })).toContainText('报告B-新快响应');
        // 等 A 的慢响应返回后,页面必须仍是 B
        await page.waitForTimeout(2000);
        await expect(page.getByRole('heading', { level: 1 })).toContainText('报告B-新快响应');
        await expect(page.getByText('报告A-旧慢响应')).toHaveCount(0);
    });

    test('连续快速切换 A→B→A→B:以最后一次为准', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/100', async (route) => {
            await new Promise((r) => setTimeout(r, 500));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告A-旧慢响应', 12, '隐形级')),
            });
        });
        await page.route('**/api/public/report/200', async (route) => {
            await new Promise((r) => setTimeout(r, 80));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告B-新快响应', 88, '主导级')),
            });
        });

        await page.goto(scenarioUrl('race-lab'), { waitUntil: 'domcontentloaded' });
        await page.getByRole('button', { name: '切到报告 B' }).click();
        await page.getByRole('button', { name: '切到报告 A' }).click();
        await page.getByRole('button', { name: '切到报告 B' }).click();
        await expect(page.getByRole('heading', { level: 1 })).toContainText('报告B-新快响应');
        // 全部慢响应落地后仍必须是 B
        await page.waitForTimeout(1200);
        await expect(page.getByRole('heading', { level: 1 })).toContainText('报告B-新快响应');
        await expect(page.getByText('报告A-旧慢响应')).toHaveCount(0);
    });

    test('卸载后迟到的响应不产生 pageerror', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        const errors: string[] = [];
        page.on('pageerror', (err) => errors.push(String(err)));
        await page.route('**/api/public/report/100', async (route) => {
            await new Promise((r) => setTimeout(r, 900));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告A-迟到响应', 12, '隐形级')),
            });
        });
        await page.goto(scenarioUrl('race-lab'), { waitUntil: 'domcontentloaded' });
        // A 挂起中离开页面(卸载),A 迟到响应不得造成异常
        await page.goto(scenarioUrl('not-ready'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '客户报告尚未就绪' })).toBeVisible();
        await page.waitForTimeout(1400);
        expect(errors).toHaveLength(0);
    });

    test('有效报告切到失效链接后,旧响应不得覆盖失效状态', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await page.route('**/api/public/report/100', async (route) => {
            await new Promise((r) => setTimeout(r, 900));
            await route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(reportPayload('报告A-不应重新出现', 88, '主导级')),
            });
        });

        await page.goto(scenarioUrl('race-lab'), { waitUntil: 'domcontentloaded' });
        await page.getByRole('button', { name: '切到失效链接' }).click();
        await expect(page.getByText('链接已失效。请联系发你链接的人重新发送。')).toBeVisible();

        // 旧实现没有在非法 reportId 分支递增代际或取消 A,因此这里会被 A 覆盖。
        await page.waitForTimeout(1300);
        await expect(page.getByText('链接已失效。请联系发你链接的人重新发送。')).toBeVisible();
        await expect(page.getByText('报告A-不应重新出现')).toHaveCount(0);
    });
});
