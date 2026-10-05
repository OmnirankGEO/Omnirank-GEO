/**
 * 五档视口验收：每页在每个视口无横向溢出、console/page error=0、保存截图。
 * 本文件在全部 5 个视口 project 上运行（窄屏专项另见 narrow.spec.ts，避免条件 skip）。
 */

import { test, expect, type Page } from 'playwright/test';
import path from 'node:path';
import { installObservationMock, collectConsoleErrors } from './mock';

const HOME = '/geo-observation-preview.html';
const EVIDENCE = path.resolve(process.cwd(), '../docs/AI-CONTEXT/GEO_OBSERVATION_FRONTEND_A/evidence');

const PAGES: { route: string; key: string; anchor: string }[] = [
  { route: 'workbench', key: 'workbench', anchor: '被 AI 提到的比例' },
  { route: 'admin', key: 'admin', anchor: '观测治理中心' },
  { route: 'methodology', key: 'methodology', anchor: '方法说明' },
  { route: 'diagnosis', key: 'diagnosis', anchor: 'AI 推荐行为' },
];

async function noHorizontalOverflow(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(sw, `横向溢出：scrollWidth=${sw} clientWidth=${cw}`).toBeLessThanOrEqual(cw + 1);
}

for (const p of PAGES) {
  test(`${p.key}：无横向溢出 + 无 console 错误 + 截图`, async ({ page }, testInfo) => {
    await installObservationMock(page, 'happy');
    const errors = collectConsoleErrors(page);

    await page.goto(`${HOME}#/${p.route}`);
    await expect(page.getByText(p.anchor).first()).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(400); // 图表渲染稳定
    await noHorizontalOverflow(page);

    await page.screenshot({
      path: path.join(EVIDENCE, `${p.key}-${testInfo.project.name.replace('vp-', '')}.png`),
      fullPage: true,
    });
    expect(errors, errors.join('\n')).toHaveLength(0);
  });
}
