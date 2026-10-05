/**
 * 公共测试工具
 */
import { test, expect, type Page, type TestInfo } from 'playwright/test';

export const HARNESS_URL = '/race-public-report.html';

export function scenarioUrl(scenario: string, bare = true): string {
    return `${HARNESS_URL}?scenario=${scenario}${bare ? '&bare=1' : ''}`;
}

/** 非主视口项目跳过(状态/交互类测试只在 v1440 跑,避免八倍冗余) */
export function onlyPrimaryProject(testInfo: TestInfo): void {
    test.skip(testInfo.project.name !== 'v1440', '仅 v1440 主视口执行');
}

/** 仅桌面+手机两视口(移动菜单等需要双端验证的交互) */
export function onlyDualProjects(testInfo: TestInfo): void {
    test.skip(
        !['v1440', 'v390'].includes(testInfo.project.name),
        '仅 v1440/v390 双端执行',
    );
}

/** 打开场景并等待报告就绪(证据矩阵标题出现 = 全区块就绪) */
export async function gotoReady(page: Page, scenario = 'ready-full'): Promise<void> {
    await page.goto(scenarioUrl(scenario), { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: '证据矩阵' })).toBeVisible();
}

/** 断言页面无横向溢出(响应式硬门) */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
    const result = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        bodyScrollWidth: document.body.scrollWidth,
    }));
    expect(
        result.scrollWidth <= result.clientWidth + 1,
        `横向溢出:scrollWidth=${result.scrollWidth} > clientWidth=${result.clientWidth}`,
    ).toBe(true);
    expect(
        result.bodyScrollWidth <= result.clientWidth + 1,
        `body 横向溢出:${result.bodyScrollWidth} > ${result.clientWidth}`,
    ).toBe(true);
}

/** 收集 console error 与 pageerror(隐私/安全验收用) */
export function collectPageErrors(page: Page): { consoleErrors: string[]; pageErrors: string[] } {
    const bag = { consoleErrors: [] as string[], pageErrors: [] as string[] };
    page.on('console', (msg) => {
        if (msg.type() === 'error') {
            const url = msg.location()?.url ?? '';
            bag.consoleErrors.push(`${msg.text()} @${url}`);
        }
    });
    page.on('pageerror', (err) => bag.pageErrors.push(String(err)));
    return bag;
}

/** 预期内的资源 404(白标 logo 失败回退场景的故意失效 URL),其余 console 错误仍为红 */
export const EXPECTED_RESOURCE_404 = /definitely-missing-public-report-logo\.png/;

export function unexpectedConsoleErrors(errors: string[]): string[] {
    return errors.filter((e) => !EXPECTED_RESOURCE_404.test(e));
}
