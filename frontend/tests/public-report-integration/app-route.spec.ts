import { expect, test, type Page } from 'playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const repoRoot = path.resolve(process.cwd(), '..');
const V2_BODY = JSON.parse(execFileSync(
    'python',
    [path.join(repoRoot, 'tests/fixtures/build_public_report_presentation_fixture.py')],
    { cwd: repoRoot, encoding: 'utf8' },
)) as unknown;

async function mockReport(page: Page, id: string, body: unknown, status = 200) {
    await page.route(`**/api/public/report/${id}*`, (route) => route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(body),
    }));
}

test.beforeEach(async ({ page }) => {
    await page.route('**/api/m3/customer-events/public', (route) => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
    }));
});

test('正式 App 路由让 V2 使用新版报告并正确渲染 Markdown', async ({ page }) => {
    await mockReport(page, '450', V2_BODY);
    const openedRequest = page.waitForRequest((request) => {
        if (!request.url().endsWith('/api/m3/customer-events/public') || request.method() !== 'POST') {
            return false;
        }
        try {
            const payload = request.postDataJSON() as { event_type?: unknown; diagnosis_id?: unknown };
            return payload.event_type === 'opened' && payload.diagnosis_id === 450;
        } catch {
            return false;
        }
    });
    await page.goto('/public/report/450?st=public-token', { waitUntil: 'domcontentloaded' });

    await expect(page.getByRole('heading', { name: '澜川智能家居', exact: true })).toBeVisible();
    await expect(page.getByText('GEO 品牌诊断报告', { exact: true })).toBeVisible();
    await expect(page.locator('#overview').getByText('品牌已被识别，但推荐证据仍需补齐。')).toBeVisible();
    await expect(page.getByText('智能家居', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '查看完整分析' }).click();
    await expect(page.getByRole('heading', { name: '客户报告正文' })).toBeVisible();
    await expect(page).toHaveTitle('澜川智能家居 GEO 诊断报告 · AI 搜索表现');
    await expect(page.locator('body')).not.toContainText('4 大主流 AI 引擎');
    await openedRequest;

    const html = await page.content();
    expect(html).not.toContain('/@vite/client');
    expect(html).not.toContain('@react-refresh');
});

test('正式 App 路由保留历史 V1 报告', async ({ page }) => {
    await mockReport(page, '451', {
        status: 'success',
        report: {
            brand_name: '历史报告品牌', score: 28, level: '危急级',
            content: '## 历史 V1 报告正文\n\n保留旧链接兼容。',
            report_version: 'v1', has_html: false, keyword_count: 1,
            created_at: '2026-07-19T12:00:00', branding_status: 'platform',
        },
    });

    await page.goto('/public/report/451?st=legacy-token', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: '历史 V1 报告正文' })).toBeVisible();
    await expect(page.getByText('保留旧链接兼容。')).toBeVisible();
});

test('无效链接不会落入历史报告或展示旧客户内容', async ({ page }) => {
    await mockReport(page, '999', { detail: 'internal-path-sentinel' }, 404);
    await page.goto('/public/report/999?st=bad-token', { waitUntil: 'domcontentloaded' });
    await expect(page.getByText('链接已失效', { exact: false }).first()).toBeVisible();
    await expect(page.locator('body')).not.toContainText('internal-path-sentinel');
    await expect(page.locator('body')).not.toContainText('历史 V1 报告正文');
});

test('390px 下新版正式路由无横向溢出', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockReport(page, '450', V2_BODY);
    await page.goto('/public/report/450?st=public-token');
    await expect(page.getByRole('heading', { name: '澜川智能家居', exact: true })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
});
