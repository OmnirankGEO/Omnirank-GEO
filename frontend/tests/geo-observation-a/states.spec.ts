/**
 * 规定状态硬门：loading / empty / error / 403 / 409 / 423 / 503 / 样本不足 / 数据口径断点 / 洞察失败。
 * 仅在桌面视口跑一次（功能性覆盖）。
 * 409/423：AI-2 写接入后由真实写触发；当前用 driven mock 证明状态处理器可达（capability-pending）。
 */

import { test, expect, type Page } from 'playwright/test';
import { installObservationMock, collectConsoleErrors, type Scenario } from './mock';

const HOME = '/geo-observation-preview.html';


async function goto(page: Page, route: string, scenario: Scenario = 'happy') {
  const ctrl = await installObservationMock(page, scenario);
  const errors = collectConsoleErrors(page);
  await page.goto(`${HOME}#/${route}`);
  return { ctrl, errors };
}

test('数据口径断点：默认 fixture 显示口径更新（后端文案）+ 不做跨期对比', async ({ page }) => {
  await goto(page, 'workbench');
  await expect(page.getByText('数据口径已更新').first()).toBeVisible();
  await expect(page.getByText('本周期暂不与上周期直接比较')).toBeVisible();
  await expect(page.getByText('本周期不做跨期对比').first()).toBeVisible();
});

test('503：观测不可用 → 错误态 + 重试，不显示 0 或免费', async ({ page }) => {
  const { errors } = await goto(page, 'workbench', 'summary_503');
  await expect(page.getByText('观测数据暂不可用，请稍后重试。')).toBeVisible();
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible();
  await expect(page.getByText('被 AI 提到的比例')).toHaveCount(0);
  expect(errors, errors.join('\n')).toHaveLength(0);
});

test('product=false/readiness 未开闸：现役监测页回退旧洞察，不以 503 覆盖', async ({ page }) => {
  await installObservationMock(page, 'summary_503');
  await page.goto(`${HOME}?verify-legacy-fallback=1#/workbench`);
  await expect(page.getByRole('region', { name: '现役数据洞察回退' })).toHaveText('现役数据洞察保持可用');
  await expect(page.getByText('观测数据暂不可用，请稍后重试。')).toHaveCount(0);
});

test('403：无权限 → 不渲染部分数据', async ({ page }) => {
  await goto(page, 'workbench', 'summary_403');
  await expect(page.getByText('你没有查看这项数据的权限。')).toBeVisible();
  await expect(page.getByText('被 AI 提到的比例')).toHaveCount(0);
});

test('409：冲突状态处理器可达', async ({ page }) => {
  await goto(page, 'workbench', 'summary_409');
  await expect(page.getByText('数据已被其他操作更新，请刷新后重试。')).toBeVisible();
});

test('423：环境覆盖状态处理器可达', async ({ page }) => {
  await goto(page, 'workbench', 'summary_423');
  await expect(page.getByText('当前设置由运行环境统一管理，不能在这里修改。')).toBeVisible();
});

test('未选客户：空态引导', async ({ page }) => {
  await goto(page, 'workbench-nobrand');
  await expect(page.getByText('先选择一个客户')).toBeVisible();
});

test('无数据：样本数 0 + 比例显示"—"（不当 0%）+ 各 Tab 空态', async ({ page }) => {
  await goto(page, 'workbench', 'summary_empty');
  await expect(page.getByText('本次分析样本数')).toBeVisible();
  // 比例为 null → "—"，绝不显示 0%
  await expect(page.getByText('0%')).toHaveCount(0);
  await expect(page.getByText('—').first()).toBeVisible();
  await page.getByRole('tab', { name: '平台表现' }).click();
  await expect(page.getByText('本周期还没有平台数据')).toBeVisible();
});

test('样本不足：管理员行业基线 status=insufficient_samples（真实 200+status）', async ({ page }) => {
  await goto(page, 'admin', 'industry_insufficient');
  await page.getByRole('tab', { name: '行业趋势' }).click();
  await page.getByLabel('行业键').fill('elevator_service');
  await page.getByRole('button', { name: '查询' }).click();
  await expect(page.getByText('样本不足，暂不下结论。').first()).toBeVisible();
});

test('洞察失败：显示"暂不可用"，图表与证据仍可查看', async ({ page }) => {
  await goto(page, 'workbench', 'insight_fail');
  await page.getByRole('button', { name: '生成洞察' }).click();
  await expect(page.getByText('AI 洞察暂不可用，图表和证据仍可正常查看。')).toBeVisible();
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
});
