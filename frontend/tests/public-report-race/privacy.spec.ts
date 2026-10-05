/**
 * privacy.spec — 隐私与安全扫描
 * DOM / URL / localStorage / sessionStorage / console / pageerror 全链路
 * 覆盖全部场景(每个场景都是独立页面加载)
 */
import { test, expect } from 'playwright/test';
import { collectPageErrors, gotoReady, onlyPrimaryProject, scenarioUrl, unexpectedConsoleErrors } from './helpers';

/** 禁词表:内部身份/服务商关系/上游/成本/下游源名/内部异常 */
const FORBIDDEN_PATTERNS: readonly { name: string; pattern: RegExp }[] = [
    { name: 'user_id', pattern: /user_id/i },
    { name: 'owner_user_id', pattern: /owner_user_id/i },
    { name: 'agent_user_id', pattern: /agent_user_id/i },
    { name: 'upstream_user_id', pattern: /upstream_user_id/i },
    { name: 'resolved_user_id', pattern: /resolved_user_id/i },
    { name: 'service_account_code', pattern: /service_account_code/i },
    { name: 'channel_account_code', pattern: /channel_account_code/i },
    { name: 'relationship_version', pattern: /relationship_version/i },
    { name: 'cost_multiplier', pattern: /cost_multiplier/i },
    { name: 'agent_referral_code', pattern: /agent_referral_code/i },
    { name: 'share_token 字段', pattern: /share_token/i },
    { name: '下游源名(秘塔)', pattern: /秘塔|metaso/i },
    { name: 'provider 内部标识', pattern: /\bprovider\b/i },
    { name: '诊断表名', pattern: /diagnosis_records/i },
    { name: '数据库错误', pattern: /pg_|postgres|sqlite|sqlstate/i },
];

const ALL_SCENARIOS = [
    'ready-full',
    'partial-data',
    'no-competitors',
    'no-baseline',
    'long-brand',
    'long-content',
    'many-evidence',
    'generic-plan',
    'approved-whitelabel',
    'broken-whitelabel',
    'dto-incomplete-score-level',
    'not-ready',
    'pending',
    'invalid-link',
    'error',
];

test.describe('隐私与安全', () => {
    test.beforeEach(({}, testInfo) => onlyPrimaryProject(testInfo));

    for (const scenario of ALL_SCENARIOS) {
        test(`场景 ${scenario}:DOM/存储/URL/console 无内部信息`, async ({ page }) => {
            const errors = collectPageErrors(page);
            await page.goto(scenarioUrl(scenario), { waitUntil: 'domcontentloaded' });
            await page.waitForTimeout(800);

            // 1) DOM(innerHTML 含属性;innerText 为可见文本)
            const dom = await page.evaluate(() => document.documentElement.outerHTML);
            for (const { name, pattern } of FORBIDDEN_PATTERNS) {
                expect(pattern.test(dom), `场景 ${scenario} DOM 命中禁词 ${name}`).toBe(false);
            }

            // 2) URL:不得携带 token 类参数(harness 只有 scenario/bare)
            const url = page.url();
            expect(/[?&](st|token|share_token|user)=/i.test(url), `URL 泄露:${url}`).toBe(false);

            // 3) localStorage / sessionStorage
            const storageDump = await page.evaluate(() => {
                const dump: string[] = [];
                for (let i = 0; i < localStorage.length; i += 1) {
                    const k = localStorage.key(i);
                    dump.push(`L:${k}=${localStorage.getItem(k ?? '')}`);
                }
                for (let i = 0; i < sessionStorage.length; i += 1) {
                    const k = sessionStorage.key(i);
                    dump.push(`S:${k}=${sessionStorage.getItem(k ?? '')}`);
                }
                return dump.join('\n');
            });
            for (const { name, pattern } of FORBIDDEN_PATTERNS) {
                expect(pattern.test(storageDump), `场景 ${scenario} 存储命中禁词 ${name}`).toBe(false);
            }

            // 4) console / pageerror 干净(白标失败回退场景的故意 404 除外,见 helpers)
            expect(errors.pageErrors, `页面异常:${errors.pageErrors.join(';')}`).toHaveLength(0);
            const consoleUnexpected = unexpectedConsoleErrors(errors.consoleErrors);
            expect(consoleUnexpected, `console 错误:${consoleUnexpected.join(';')}`).toHaveLength(0);
        });
    }

    test('Markdown 安全:javascript: 链接被禁用,外链带 rel,图片被占位', async ({ page }) => {
        await gotoReady(page, 'long-content');
        // 叙事区默认折叠,先展开
        await page.getByRole('button', { name: '查看完整分析' }).click();
        // javascript: 链接 → urlTransform 置空,渲染为无 href 元素
        const dangerous = page.getByText('危险的 javascript 链接');
        await expect(dangerous).toBeVisible();
        const tag = await dangerous.evaluate((el) => ({ tag: el.tagName, href: el.getAttribute('href') }));
        expect(tag.tag).not.toBe('A');
        // 裸 HTML 不被解析:<b>加粗</b> 以文本形式可见
        await expect(page.getByText('<b>加粗</b>')).toBeVisible();
        // script 标签不进入 DOM
        await expect(page.locator('.prp-markdown script')).toHaveCount(0);
        // 正常外链:target + rel 双保险
        const safeLink = page.getByRole('link', { name: '正常外链' });
        await expect(safeLink).toHaveAttribute('target', '_blank');
        const rel = await safeLink.getAttribute('rel');
        expect(rel).toContain('noopener');
        expect(rel).toContain('noreferrer');
        // 外部图片默认禁用 → 文字占位,无 <img> 追踪像素
        await expect(page.locator('.prp-markdown img')).toHaveCount(0);
        await expect(page.getByText('[图片未加载: alt]')).toBeVisible();
    });
});
