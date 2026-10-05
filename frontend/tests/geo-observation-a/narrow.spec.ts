/**
 * 窄屏专项（只在 narrow-w320 / narrow-m390 两个项目运行 → 零条件 skip）：
 * - 工作台/治理中心逐 Tab 在窄屏均无横向溢出；
 * - 工作台逐 Tab 移动端截图（补齐非首屏 Tab 的移动证据）。
 */

import { test, expect, type Page } from 'playwright/test';
import path from 'node:path';
import { installObservationMock } from './mock';

const HOME = '/geo-observation-preview.html';
const EVIDENCE = path.resolve(process.cwd(), '../docs/AI-CONTEXT/GEO_OBSERVATION_FRONTEND_A/evidence');

const TAB_KEY: Record<string, string> = {
  总览: 'overview',
  平台表现: 'platforms',
  问题与证据: 'questions',
  竞争格局: 'competition',
  行动建议: 'actions',
};

async function noHorizontalOverflow(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(sw, `横向溢出：scrollWidth=${sw} clientWidth=${cw}`).toBeLessThanOrEqual(cw + 1);
}

test('workbench 五 Tab 在窄屏均无横向溢出 + 每 Tab 截图', async ({ page }, testInfo) => {
  const vp = testInfo.project.name.replace('narrow-', '');
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/workbench`);
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
  for (const tab of ['总览', '平台表现', '问题与证据', '竞争格局', '行动建议']) {
    await page.getByRole('tab', { name: tab }).click();
    await page.waitForTimeout(250);
    await noHorizontalOverflow(page);
    await page.screenshot({ path: path.join(EVIDENCE, `workbench-${TAB_KEY[tab]}-${vp}.png`), fullPage: true });
  }
});

test('admin 五 Tab 在窄屏均无横向溢出', async ({ page }) => {
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/admin`);
  await expect(page.getByRole('heading', { name: '观测治理中心' })).toBeVisible();
  for (const tab of ['运行概览', '平台与模型', '样本治理', '行业趋势', '内容与媒体机会']) {
    await page.getByRole('tab', { name: tab }).click();
    await page.waitForTimeout(250);
    await noHorizontalOverflow(page);
  }
});
