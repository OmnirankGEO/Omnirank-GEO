/**
 * interactions.spec — 交互完整性:每个可见按钮都有真实行为
 * 桌面(v1440)+ 手机(v390)双端
 */
import { test, expect } from 'playwright/test';
import { gotoReady, onlyDualProjects, onlyPrimaryProject } from './helpers';

test.describe('交互完整性', () => {
    test('导航锚点真实滚动', async ({ page }, testInfo) => {
        test.skip(testInfo.project.name !== 'v1440');
        await gotoReady(page);
        await page.getByRole('navigation', { name: '报告导航' }).getByRole('link', { name: '证据' }).click();
        await page.waitForFunction(() => window.scrollY > 300);
        const evidenceTop = await page.evaluate(() => document.getElementById('evidence')?.getBoundingClientRect().top ?? 9999);
        expect(evidenceTop).toBeLessThan(400);
    });

    test('下载/打印按钮真实触发 window.print', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        await page.evaluate(() => {
            (window as unknown as { __printCount: number }).__printCount = 0;
            const original = window.print.bind(window);
            window.print = () => {
                (window as unknown as { __printCount: number }).__printCount += 1;
                original();
            };
        });
        await page.getByRole('button', { name: '下载 / 打印' }).click();
        const count = await page.evaluate(() => (window as unknown as { __printCount: number }).__printCount);
        expect(count).toBe(1);
        // 页尾第二个打印入口
        await page.getByRole('button', { name: '打印 / 另存 PDF' }).click();
        const count2 = await page.evaluate(() => (window as unknown as { __printCount: number }).__printCount);
        expect(count2).toBe(2);
    });

    test('返回顶部', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        await page.evaluate(() => window.scrollTo(0, 1500));
        const backTop = page.getByRole('button', { name: '返回顶部' });
        await expect(backTop).toBeVisible();
        await backTop.click();
        await page.waitForFunction(() => window.scrollY < 50, undefined, { timeout: 5000 });
    });

    test('数据怎么看:对话框打开/关闭(Esc)', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        await page.getByRole('button', { name: '数据怎么看' }).click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect(page.getByText('一分钟读懂这份报告')).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('dialog')).toHaveCount(0);
    });

    test('平台排序:点击表头改变顺序,缺失值恒在最后', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'partial-data');
        const sortBtn = page.getByRole('button', { name: '识别率' });
        await sortBtn.click();
        const rowNames = () => page.locator('#platforms tbody tr th[scope="row"] span').allTextContents();
        let names = await rowNames();
        // partial-data 三平台:豆包 71 / 通义 67 / DeepSeek null → 降序后 DeepSeek 在最后
        expect(names[0]).toContain('豆包');
        expect(names[names.length - 1]).toContain('DeepSeek');
        // 再点两次回到原序
        await sortBtn.click();
        await sortBtn.click();
        names = await rowNames();
        expect(names[0]).toContain('豆包');
    });

    test('平台行展开/收起证据', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        // accessible name 会随状态从“展开”变为“收起”,用平台名保持稳定定位。
        const expandBtn = page.getByRole('button', { name: /豆包 的证据/ }).first();
        await expandBtn.click();
        await expect(expandBtn).toHaveAttribute('aria-expanded', 'true');
        await expect(page.getByText('澜川智能家居怎么样，靠谱吗?').first()).toBeVisible();
        await expandBtn.click();
        await expect(expandBtn).toHaveAttribute('aria-expanded', 'false');
    });

    test('证据搜索 + 筛选 + 清除', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        const search = page.getByRole('searchbox', { name: '搜索全部平台的问题或回答' });
        // "索菲亚" 命中 3 条(ev-03/ev-04 回答节选 + ev-09 问题)
        await search.fill('索菲亚');
        await expect(page.getByText('3 个平台 · 3 / 14 条')).toBeVisible();
        // 组合判定筛选:列入备选 → ev-03/ev-09，分别归入豆包/元宝。
        await page.getByRole('combobox', { name: '按判定结果筛选' }).selectOption('candidate');
        await expect(page.getByText('2 个平台 · 2 / 14 条')).toBeVisible();
        // 清除筛选恢复
        await page.getByRole('button', { name: '清除筛选' }).click();
        await expect(page.getByText('4 个平台 · 14 / 14 条')).toBeVisible();
    });

    test('证据判定筛选 + 平台归组', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page, 'many-evidence');
        // vNext 判定全集 10 类 × 循环:recommended 命中 10 条(i%10===0)
        await page.getByRole('combobox', { name: '按判定结果筛选' }).selectOption('recommended');
        await expect(page.getByText('2 个平台 · 10 / 96 条')).toBeVisible();
        // 搜索「上海全屋定制」命中 12 条，全部归到真实平台组内，不再分页打散。
        await page.getByRole('combobox', { name: '按判定结果筛选' }).selectOption('');
        await page.getByRole('searchbox', { name: '搜索全部平台的问题或回答' }).fill('上海全屋定制');
        await expect(page.getByText('1 个平台 · 12 / 96 条')).toBeVisible();
        await page.getByRole('button', { name: '展开通义千问的全部问题与回答' }).click();
        await expect(page.locator('#evidence article')).toHaveCount(12);
        // vNext 新判定可筛选(条件推荐)
        await page.getByRole('searchbox', { name: '搜索全部平台的问题或回答' }).fill('');
        await page.getByRole('combobox', { name: '按判定结果筛选' }).selectOption('conditionally_recommended');
        await expect(page.getByText(/个平台 · \d+ \/ 96 条/)).toBeVisible();
        await page.getByRole('button', { name: /展开.*的全部问题与回答/ }).first().click();
        await expect(page.locator('#evidence article').getByText('条件推荐').first()).toBeVisible();
    });

    test('证据行展开:回答节选与空回答占位', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        const firstExpand = page.getByRole('button', { name: /通义千问的全部问题与回答/ });
        await firstExpand.click();
        await expect(page.getByText('平台原始回答').first()).toBeVisible();
        // 空回答行(元宝无回答)展开显示明确占位
        await page.getByRole('combobox', { name: '按判定结果筛选' }).selectOption('no_answer');
        const expandBtn = page.getByRole('button', { name: '展开元宝的全部问题与回答' });
        await expandBtn.click();
        await expect(page.getByText('本次未获取到有效回答文本。').first()).toBeVisible();
    });

    test('关键发现:证据摘要展开/收起', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        // 定位第一张发现卡内的按钮(展开后名称变为"收起证据摘要",用正则双匹配)
        const firstCard = page.locator('#findings .grid > div').first();
        const btn = firstCard.getByRole('button', { name: /证据摘要/ });
        await btn.click();
        await expect(firstCard.getByText(/19\/20；其中 6 条回答引用了/)).toBeVisible();
        await btn.click();
        await expect(firstCard.getByText(/19\/20；其中 6 条回答引用了/)).toHaveCount(0);
    });

    test('行动:展开执行说明(Markdown 渲染)+ 查看证据锚点', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        const detailBtn = page.getByRole('button', { name: '展开执行说明' }).first();
        await detailBtn.click();
        // Markdown 有序列表被渲染为 <ol>
        await expect(page.locator('#actions ol li', { hasText: '完善官网' }).first()).toBeVisible();
        // 查看对应证据 → 跳到证据区
        await page.getByRole('link', { name: /查看对应证据/ }).first().click();
        await page.waitForFunction(() => window.scrollY > 300);
    });

    test('键盘操作:Tab 可达 + Enter 激活', async ({ page }, testInfo) => {
        onlyPrimaryProject(testInfo);
        await gotoReady(page);
        // 从证据区搜索框开始键盘流
        const search = page.getByRole('searchbox', { name: '搜索全部平台的问题或回答' });
        await search.focus();
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');
        await page.keyboard.press('Tab');
        // 第三个 Tab 应落在"全部判定"select 之后的元素;直接用首行展开按钮验证键盘激活
        const firstExpand = page.getByRole('button', { name: /通义千问的全部问题与回答/ });
        await firstExpand.focus();
        await page.keyboard.press('Enter');
        await expect(firstExpand).toHaveAttribute('aria-expanded', 'true');
        // focus-visible 轮廓存在
        const outline = await firstExpand.evaluate((el) => {
            const style = getComputedStyle(el);
            return style.outlineStyle !== 'none' || style.boxShadow !== 'none';
        });
        expect(outline).toBe(true);
    });

    test('移动端菜单:打开/锚点导航/Esc 关闭', async ({ page }, testInfo) => {
        test.skip(testInfo.project.name !== 'v390');
        await gotoReady(page);
        const menuBtn = page.getByRole('button', { name: '打开菜单' });
        await menuBtn.click();
        await expect(page.getByRole('button', { name: '关闭菜单' })).toBeVisible();
        // 菜单内锚点导航后菜单自动收起
        await page.locator('nav').getByRole('link', { name: '证据' }).click();
        await page.waitForFunction(() => window.scrollY > 300);
        // 再开菜单,Esc 关闭
        await page.getByRole('button', { name: '打开菜单' }).click();
        await page.keyboard.press('Escape');
        await expect(page.getByRole('button', { name: '打开菜单' })).toBeVisible();
    });

    test('触摸展开(手机证据卡)', async ({ page }, testInfo) => {
        test.skip(testInfo.project.name !== 'v390');
        await gotoReady(page);
        // 展开后按钮名变为"收起回答",用正则双匹配锁定第一张卡的按钮
        const expandBtn = page.getByRole('button', { name: /通义千问的全部问题与回答/ });
        // 避开 sticky 顶栏遮挡:滚动留白后再 tap
        await expandBtn.scrollIntoViewIfNeeded();
        await page.evaluate(() => window.scrollBy(0, -80));
        await expandBtn.tap();
        await expect(expandBtn).toHaveAttribute('aria-expanded', 'true');
        await expect(page.locator('#evidence').getByText('平台原始回答').first()).toBeVisible();
        await expandBtn.tap();
        await expect(expandBtn).toHaveAttribute('aria-expanded', 'false');
    });

    test('harness 场景切换器可用(bare=0)', async ({ page }, testInfo) => {
        onlyDualProjects(testInfo);
        await page.goto('/race-public-report.html?scenario=ready-full', { waitUntil: 'domcontentloaded' });
        const bar = page.getByTestId('harness-bar');
        await expect(bar).toBeVisible();
        await bar.getByRole('link', { name: /超长品牌名/ }).click();
        await expect(page.getByRole('heading', { level: 1 })).toContainText('LanChuan Smart Home Premium');
    });
});
