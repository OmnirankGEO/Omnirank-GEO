/**
 * screenshots.spec — 交付截图清单
 * 桌面第一屏 / 桌面证据矩阵 / 2K 完整布局 / 1024 紧凑布局 /
 * 390 手机首屏 / 390 手机证据展开 / not_ready / 超长品牌名 / 打印预览证据
 * 产物:C:/AI-Test/_qa_public_report_race_a/screenshots/
 */
import { test, expect } from 'playwright/test';
import { gotoReady, scenarioUrl } from './helpers';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.resolve(__dirname, '../../../../_qa_public_report_race_a/screenshots');

test.describe('交付截图', () => {
    test('按场景产出交付截图', async ({ page }, testInfo) => {
        const project = testInfo.project.name;
        const shot = (name: string) => page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
        const shotFull = (name: string) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
        /** 滚动全页触发所有 whileInView 动效并落定,避免整页截图抓到隐藏态 */
        const settleAll = async () => {
            await page.evaluate(async () => {
                const step = window.innerHeight * 0.7;
                for (let y = 0; y < document.body.scrollHeight; y += step) {
                    window.scrollTo(0, y);
                    await new Promise((r) => setTimeout(r, 60));
                }
                window.scrollTo(0, 0);
            });
            await page.waitForTimeout(700);
        };

        if (project === 'v1440') {
            // 桌面第一屏(等入场/计数动效落定)
            await gotoReady(page);
            await page.evaluate(() => window.scrollTo(0, 0));
            await page.waitForTimeout(1000);
            await shot('01-desktop-1440-first-screen');
            // 桌面证据矩阵(展开一行)
            await page.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().click();
            await page.locator('#evidence').scrollIntoViewIfNeeded();
            await page.waitForTimeout(500);
            await shot('02-desktop-1440-evidence-matrix');
            // 深色主题 + Markdown 证据展开(与浅色报告分离)
            await gotoReady(page, 'long-content');
            await page.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().click();
            await page.getByRole('button', { name: '切换为深色报告' }).click();
            await page.locator('#evidence').scrollIntoViewIfNeeded();
            await page.waitForTimeout(500);
            await shot('02b-desktop-1440-dark-markdown-evidence');
            await page.getByRole('button', { name: '切换为浅色报告' }).click();
            // 管理员批准后的白标必须出现在左上角
            await gotoReady(page, 'approved-whitelabel');
            await expect(page.getByTestId('approved-whitelabel-brand')).toBeVisible();
            await page.evaluate(() => window.scrollTo(0, 0));
            await page.waitForTimeout(500);
            await shot('02c-desktop-1440-approved-whitelabel');
            // 桌面整页
            await gotoReady(page);
            await settleAll();
            await shotFull('03-desktop-1440-full-page');
            // not_ready
            await page.goto(scenarioUrl('not-ready'), { waitUntil: 'domcontentloaded' });
            await expect(page.getByRole('heading', { name: '客户报告尚未就绪' })).toBeVisible();
            await shot('08-desktop-1440-not-ready');
            // 超长品牌名
            await gotoReady(page, 'long-brand');
            await page.waitForTimeout(1000);
            await shot('09-desktop-1440-long-brand');
            // 打印预览(证据区 print emulation)
            await gotoReady(page);
            await page.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().click();
            await page.locator('#evidence').scrollIntoViewIfNeeded();
            await page.emulateMedia({ media: 'print' });
            await page.waitForTimeout(400);
            await shot('10-desktop-1440-print-preview-evidence');
        }

        if (project === 'v2560') {
            // 2K 完整布局
            await gotoReady(page);
            await page.waitForTimeout(1000);
            await shot('04-2k-2560-first-screen');
            await settleAll();
            await shotFull('04-2k-2560-full-page');
        }

        if (project === 'v1024') {
            // 1024 紧凑布局
            await gotoReady(page);
            await page.waitForTimeout(1000);
            await shot('05-compact-1024-first-screen');
            await settleAll();
            await shotFull('05-compact-1024-full-page');
        }

        if (project === 'v390') {
            // 手机首屏
            await gotoReady(page);
            await page.evaluate(() => window.scrollTo(0, 0));
            await page.waitForTimeout(1000);
            await shot('06-mobile-390-first-screen');
            // 手机证据展开
            await page.locator('#evidence').scrollIntoViewIfNeeded();
            await page.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().tap();
            await page.waitForTimeout(500);
            await shot('07-mobile-390-evidence-expanded');
            // 手机整页
            await settleAll();
            await shotFull('06-mobile-390-full-page');
        }

        // 其余视口:整页截图一张(响应式矩阵证据)
        if (!['v1440', 'v2560', 'v1024', 'v390'].includes(project)) {
            await gotoReady(page);
            await settleAll();
            await shotFull(`${project}-full-page`);
        }
    });
});
