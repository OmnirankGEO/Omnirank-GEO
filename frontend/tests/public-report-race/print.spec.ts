/**
 * print.spec — 打印排版验收
 *  - print 媒体下摘要区转浅色可读、导航/筛选隐藏
 *  - 页面无横向溢出(print emulation)
 *  - page.pdf 真实产出 A4 文件
 */
import { test, expect } from 'playwright/test';
import { expectNoHorizontalOverflow, gotoReady, onlyPrimaryProject } from './helpers';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const QA_ROOT = path.resolve(__dirname, '../../../../_qa_public_report_race_a');

test.describe('打印排版', () => {
    test.beforeEach(({}, testInfo) => onlyPrimaryProject(testInfo));

    test('R1B-1 harness 服务产物包含 print.css 规则(@page/防撕裂)', async ({ page }) => {
        await gotoReady(page);
        const audit = await page.evaluate(() => {
            let pageRules = 0;
            let breakAvoid = 0;
            let colorAdjust = 0;
            for (const sheet of Array.from(document.styleSheets)) {
                let rules: CSSRule[] = [];
                try {
                    rules = Array.from(sheet.cssRules);
                } catch {
                    continue;
                }
                for (const rule of rules) {
                    if (rule.type === CSSRule.PAGE_RULE) pageRules += 1;
                    if (rule.cssText.includes('break-inside: avoid')) breakAvoid += 1;
                    if (rule.cssText.includes('print-color-adjust')) colorAdjust += 1;
                }
            }
            return { pageRules, breakAvoid, colorAdjust };
        });
        expect(audit.pageRules, '@page A4 规则缺失(print.css 未进构建)').toBeGreaterThanOrEqual(1);
        expect(audit.breakAvoid, 'break-inside: avoid 缺失').toBeGreaterThanOrEqual(1);
        expect(audit.colorAdjust, 'print-color-adjust 缺失').toBeGreaterThanOrEqual(1);
    });

    test('print 媒体:摘要区转白底、交互控件隐藏、无溢出', async ({ page }) => {
        await gotoReady(page);
        await page.emulateMedia({ media: 'print' });

        // 摘要区背景转白
        const heroBg = await page.evaluate(() => {
            const el = document.getElementById('overview');
            return el ? getComputedStyle(el).backgroundColor : null;
        });
        expect(heroBg).toBe('rgb(255, 255, 255)');

        // 导航/筛选/分页/返回顶部隐藏
        await expect(page.getByRole('navigation', { name: '报告导航' })).toBeHidden();
        await expect(page.getByRole('searchbox', { name: '搜索问题或回答内容' })).toBeHidden();
        await expect(page.getByRole('button', { name: '返回顶部' })).toBeHidden();

        // 分数环仍可读(文本存在)
        await expect(page.getByText('GEO 综合评分', { exact: true }).first()).toBeVisible();

        // Markdown 表格不跑出版心(print 下 table-layout fixed 生效)
        await gotoReady(page, 'long-content');
        await page.emulateMedia({ media: 'print' });
        await expectNoHorizontalOverflow(page);
    });

    test('page.pdf:产出 A4 PDF 文件', async ({ page }) => {
        await gotoReady(page);
        const pdfPath = path.join(QA_ROOT, 'print', 'report-ready-full.pdf');
        await page.pdf({
            path: pdfPath,
            format: 'A4',
            printBackground: true,
            margin: { top: '14mm', bottom: '14mm', left: '12mm', right: '12mm' },
        });
        const fs = await import('node:fs');
        const stat = fs.statSync(pdfPath);
        expect(stat.size).toBeGreaterThan(50_000); // 非空壳 PDF
    });
});
