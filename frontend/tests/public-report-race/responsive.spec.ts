/**
 * responsive.spec — 八视口硬门
 * 每个视口:无横向溢出 / 关键区块可见 / 可滚动到底 / 按钮在视口内 / 表格不撑破页面
 */
import { test, expect } from 'playwright/test';
import {
    collectPageErrors,
    expectNoHorizontalOverflow,
    gotoReady,
    scenarioUrl,
    unexpectedConsoleErrors,
} from './helpers';

const REQUIRED_SECTIONS = [
    '决策漏斗',
    '平台表现',
    '关键发现',
    '证据矩阵',
    '竞争格局',
    '优先行动',
    '30 天建议路径',
    '方法说明',
];

test.describe('八视口 · 完整报告', () => {
    test('无横向溢出 + 全部区块可达 + 可滚动到底', async ({ page }, testInfo) => {
        const errors = collectPageErrors(page);
        await gotoReady(page, 'ready-full');

        await expectNoHorizontalOverflow(page);

        for (const name of REQUIRED_SECTIONS) {
            await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
        }

        // 自然滚动到底(页尾可见)
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await expect(page.getByText('报告生成时间')).toBeVisible();
        await expectNoHorizontalOverflow(page);

        // 回顶后首屏按钮在视口内(按钮不漂移)
        await page.evaluate(() => window.scrollTo(0, 0));
        const printBtn = page.getByRole('button', { name: '下载 / 打印' });
        await expect(printBtn).toBeVisible();
        const box = await printBtn.boundingBox();
        const viewport = testInfo.project.use.viewport as { width: number };
        expect(box).not.toBeNull();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width + 1);

        expect(errors.pageErrors, `页面异常:${errors.pageErrors.join(';')}`).toHaveLength(0);
        const consoleUnexpected = unexpectedConsoleErrors(errors.consoleErrors);
        expect(consoleUnexpected, `console 错误:${consoleUnexpected.join(';')}`).toHaveLength(0);
    });

    test('长品牌名 + 长平台名:完整可见且不溢出', async ({ page }) => {
        await gotoReady(page, 'long-brand');
        await expectNoHorizontalOverflow(page);
        // 品牌名完整渲染(不是省略号截断)
        const h1 = page.getByRole('heading', { level: 1 });
        await expect(h1).toContainText('LanChuan Smart Home Premium');
        // 长平台名完整可见(桌面表格与移动卡片随断点切换,取可见实例)
        await expect(
            page.getByText('通义千问(国际版长名称压力测试平台)Qwen-International-LongName').filter({ visible: true }).first(),
        ).toBeVisible();
    });

    test('超长 Markdown + 超长证据:表格容器内滚动,页面不溢出', async ({ page }) => {
        await gotoReady(page, 'long-content');
        // 叙事区默认折叠(评审 P2-6),先展开再验证 Markdown 排版
        await page.getByRole('button', { name: '查看完整分析' }).click();
        await expectNoHorizontalOverflow(page);
        // Markdown 宽表存在且未撑破版心
        const tableWrap = page.locator('.prp-md-table-wrap').first();
        await expect(tableWrap).toBeVisible();
        // 长英文串换行(不溢出)
        await expect(page.getByText(/Supercalifragilistic/).first()).toBeVisible();
        await expectNoHorizontalOverflow(page);
    });

    test('大量证据(96 条):按四个平台归组且布局稳定', async ({ page }) => {
        await gotoReady(page, 'many-evidence');
        await expectNoHorizontalOverflow(page);
        await expect(page.getByText('4 个平台 · 96 / 96 条')).toBeVisible();
        await expect(page.getByRole('button', { name: /展开.*的全部问题与回答/ })).toHaveCount(4);
        await page.getByRole('button', { name: '展开元宝的全部问题与回答' }).click();
        await expect(page.locator('#evidence article')).toHaveCount(24);
        await expectNoHorizontalOverflow(page);
    });
});
