/**
 * states.spec — 页面状态契约
 * loading / ready / not_ready / invalid_link / error + 部分数据 / 无竞品 / 无基准
 */
import { test, expect } from 'playwright/test';
import { gotoReady, onlyPrimaryProject, scenarioUrl } from './helpers';

test.describe('页面状态契约', () => {
    test.beforeEach(({}, testInfo) => onlyPrimaryProject(testInfo));

    test('ready:五态外基线(分数/等级/完整度并存且切割)', async ({ page }) => {
        await gotoReady(page, 'ready-full');
        await expect(page.getByLabel('GEO 综合评分 47 分，满分 100')).toBeVisible();
        await expect(page.getByText('边缘级').first()).toBeVisible();
        await expect(page.getByText('只表示本次判断依据是否充足，不是 GEO 综合评分。', { exact: true }).first()).toBeVisible();
        // 实测平台数来自数据(不硬编码 "四大")
        await expect(page.getByText('实测平台')).toBeVisible();
    });

    test('loading:慢网先出稳定骨架再渲染', async ({ page }) => {
        await page.goto(scenarioUrl('slow'), { waitUntil: 'domcontentloaded' });
        // 骨架(aria-busy)先出现
        await expect(page.locator('[aria-busy="true"]')).toBeVisible();
        // 骨架期不得出现 0 分或假数据
        await expect(page.getByText('GEO 综合评分 0 分')).toHaveCount(0);
        // 最终就绪
        await expect(page.getByRole('heading', { name: '证据矩阵' })).toBeVisible({ timeout: 10_000 });
        await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    });

    test('not_ready:结构化尚未就绪,不展示伪报告', async ({ page }) => {
        await page.goto(scenarioUrl('not-ready'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '客户报告尚未就绪' })).toBeVisible();
        await expect(page.getByText('本次客户版数据不足')).toBeVisible();
        // 不得出现分数环/0 分/报告区块
        await expect(page.getByRole('heading', { name: '证据矩阵' })).toHaveCount(0);
        await expect(page.getByLabel(/GEO 综合评分/)).toHaveCount(0);
        // 刷新按钮可用
        await expect(page.getByRole('button', { name: '刷新看看' })).toBeVisible();
    });

    test('invalid_link:与 not_ready 明确区分', async ({ page }) => {
        await page.goto(scenarioUrl('invalid-link'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '链接无效或已过期' })).toBeVisible();
        await expect(page.getByText('链接已失效')).toBeVisible();
        // 不提供重试(重试无法解决),且与 not_ready 文案不同
        await expect(page.getByRole('button', { name: '刷新看看' })).toHaveCount(0);
        await expect(page.getByRole('heading', { name: '客户报告尚未就绪' })).toHaveCount(0);
    });

    test('error:人话错误 + 重试,不露内部异常', async ({ page }) => {
        await page.goto(scenarioUrl('error'), { waitUntil: 'domcontentloaded' });
        await expect(page.getByRole('heading', { name: '报告暂时打不开' })).toBeVisible();
        await expect(page.getByRole('button', { name: '重新加载' })).toBeVisible();
        // 不得出现堆栈/内部字段字样
        const bodyText = await page.evaluate(() => document.body.innerText);
        expect(bodyText).not.toMatch(/stack|trace|exception|SELECT |INSERT |user_id|diagnosis_records/i);
    });

    test('部分数据:缺失显示 — 而非 0;封顶显式标注', async ({ page }) => {
        await gotoReady(page, 'partial-data');
        await expect(page.getByText('等级已按规则封顶以避免过度承诺').first()).toBeVisible();
        await expect(page.getByText('本层未实测').first()).toBeVisible();
        // 平台卡推荐率为 null → 显示 — 而非 0%
        const cells = await page.getByText('—').all();
        expect(cells.length).toBeGreaterThan(0);
        // 不得出现 "0%" 假零(本场景推荐率全为 null)
        await expect(page.getByText('0 %')).toHaveCount(0);
    });

    test('无竞品:区块诚实空态', async ({ page }) => {
        await gotoReady(page, 'no-competitors');
        await expect(page.getByText('本次实测未形成可信的竞品对照数据，暂不展示竞争格局。')).toBeVisible();
        // 其他区块不受影响
        await expect(page.getByRole('heading', { name: '证据矩阵' })).toBeVisible();
    });

    test('无行业基准:显式声明且不出现参考线文案', async ({ page }) => {
        await gotoReady(page, 'no-baseline');
        await expect(page.getByText(/任何"行业平均"都不会被展示/)).toBeVisible();
        const bodyText = await page.evaluate(() => document.body.innerText);
        expect(bodyText).not.toContain('行业平均值 45');
        expect(bodyText).not.toContain('行业最佳');
    });
});
