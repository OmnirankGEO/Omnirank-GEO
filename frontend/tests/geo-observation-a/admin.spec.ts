/**
 * 管理员治理中心：五 Tab、只读健康、AI-2 写接口 capability-pending（无假写按钮）、
 * 模型漂移（真实）、行业基线（真实 + 样本不足）、内容机会（真实）。
 * 桌面视口跑一次。
 */

import { test, expect, type Page } from 'playwright/test';
import { installObservationMock, collectConsoleErrors } from './mock';

const HOME = '/geo-observation-preview.html';


async function open(page: Page) {
  const ctrl = await installObservationMock(page, 'happy');
  const errors = collectConsoleErrors(page);
  await page.goto(`${HOME}#/admin`);
  await expect(page.getByRole('heading', { name: '观测治理中心' })).toBeVisible();
  return { ctrl, errors };
}

test('五 Tab 全部可切换；运行概览含 readiness + 真实计数（无发明字段）', async ({ page }) => {
  const { ctrl, errors } = await open(page);
  await expect(page.getByText('需要关注').first()).toBeVisible(); // readiness=attention_required
  for (const name of ['运行概览', '平台与模型', '样本治理', '行业趋势', '内容与媒体机会']) {
    await page.getByRole('tab', { name }).click();
    await expect(page.getByRole('tab', { name, selected: true })).toBeVisible();
  }
  await page.getByRole('tab', { name: '运行概览' }).click();
  await expect(page.getByText('待审核晋升')).toBeVisible();
  await expect(page.getByText('平台健康摘要')).toBeVisible();
  // 不发明 source_events/feature_flags 等 AI-3 未提供的字段
  await expect(page.getByText('三来源事件量')).toHaveCount(0);
  await expect(page.getByText('功能开关')).toHaveCount(0);
  expect(ctrl.counts.unknownApi, '误调不存在端点').toBe(0);
  expect(errors, errors.join('\n')).toHaveLength(0);
});

test('平台与模型：只读健康 + 写 capability-pending（无写按钮）+ 真实模型漂移', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '平台与模型' }).click();
  await expect(page.getByText('豆包').first()).toBeVisible();
  // 无平台启停写按钮，只读 + 说明
  await expect(page.getByRole('button', { name: /调整为停用|调整为启用|确认停用|确认启用/ })).toHaveCount(0);
  await expect(page.getByText('平台启停由治理配置接口提供', { exact: false })).toBeVisible();
  // 真实模型漂移（/model-shifts）
  await expect(page.getByText('模型版本漂移')).toBeVisible();
  await expect(page.getByText(/模型版本 hy3/)).toBeVisible();
});

test('样本治理：capability-pending 只读 + 无审核写按钮/理由表单', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '样本治理' }).click();
  await expect(page.getByText('样本治理概况')).toBeVisible();
  await expect(page.getByText('样本晋升审核、撤回等写操作由治理接口提供', { exact: false })).toBeVisible();
  // 无审核队列写控件
  await expect(page.getByRole('button', { name: /批准|拒绝晋升|提交|撤回证据/ })).toHaveCount(0);
  await expect(page.locator('#gov-reason')).toHaveCount(0);
});

test('行业趋势：输入行业键查询真实基线', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '行业趋势' }).click();
  await page.getByLabel('行业键').fill('elevator_service');
  await page.getByRole('button', { name: '查询' }).click();
  await expect(page.getByText('elevator_service').first()).toBeVisible();
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
});

test('内容与媒体机会：可见主题 + 跳转写作', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '内容与媒体机会' }).click();
  await expect(page.getByText('复杂产线改造案例')).toBeVisible();
  await page.getByRole('button', { name: '去写作工作台' }).first().click();
  await expect(page.getByTestId('nav-intent')).toContainText('将跳转到写作工作台');
});
