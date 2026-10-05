/**
 * 五项固定评测任务 + 隐私（DOM/URL/storage）扫描 + 用户端黑话扫描 + 任务截图。
 * 桌面视口跑一次。
 * 说明：任务5的"带 reason 的审核写"因 AI-2 治理接口未交付改为 capability-pending 只读核验（返工指令 §五）。
 */

import { test, expect, type Page } from 'playwright/test';
import path from 'node:path';
import { installObservationMock } from './mock';
import { FORBIDDEN_USER_TERMS, FORBIDDEN_MARKETING_TERMS, FORBIDDEN_PRIVACY_TOKENS, FORBIDDEN_CHANNEL_DETAIL } from './forbidden';

const HOME = '/geo-observation-preview.html';
const EVIDENCE = path.resolve(process.cwd(), '../docs/AI-CONTEXT/GEO_OBSERVATION_FRONTEND_A/evidence');


function shot(page: Page, name: string) {
  return page.screenshot({ path: path.join(EVIDENCE, `${name}.png`), fullPage: true });
}

async function scanPrivacyAndJargon(page: Page) {
  const text = await page.evaluate(() => document.body.innerText);
  for (const term of FORBIDDEN_USER_TERMS) {
    expect(text.toLowerCase(), `用户端出现工程黑话："${term}"`).not.toContain(term.toLowerCase());
  }
  for (const term of FORBIDDEN_MARKETING_TERMS) {
    expect(text, `出现营销空话："${term}"`).not.toContain(term);
  }
  for (const token of FORBIDDEN_PRIVACY_TOKENS) {
    expect(text, `DOM 出现隐私字段："${token}"`).not.toContain(token);
  }
  for (const token of FORBIDDEN_CHANNEL_DETAIL) {
    expect(text, `DOM 出现采集实现细节："${token}"`).not.toContain(token);
  }
  const url = page.url();
  const store = await page.evaluate(() => JSON.stringify({ l: { ...localStorage }, s: { ...sessionStorage } }));
  for (const token of FORBIDDEN_PRIVACY_TOKENS) {
    expect(url).not.toContain(token);
    expect(store).not.toContain(token);
  }
}

test('任务1：判断"被提到/被直接推荐"变化，并找到样本数与口径断点说明', async ({ page }) => {
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/workbench`);
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
  await expect(page.getByText('被直接推荐的比例').first()).toBeVisible();
  await expect(page.getByText('本次分析样本数')).toBeVisible();
  await expect(page.getByText('128', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('数据口径已更新').first()).toBeVisible();
  await scanPrivacyAndJargon(page);
  await shot(page, 'task1-workbench-overview');
});

test('任务2：找到证据不足的问题 → 打开证据 → 看到应补的资料（域名引用，无外链）', async ({ page }) => {
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/workbench`);
  await page.getByRole('tab', { name: '问题与证据' }).click();
  await expect(page.getByText('因证据不足，AI 暂不推荐具体品牌').first()).toBeVisible();
  await page.getByRole('button', { name: '查看证据' }).nth(1).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('还缺哪些证据')).toBeVisible();
  await expect(dialog.getByText('项目验收数据')).toBeVisible();
  await expect(dialog.getByText('这个客户本次问答', { exact: false })).toBeVisible();
  await shot(page, 'task2-evidence-drawer');
  await scanPrivacyAndJargon(page);
});

test('任务3：平台页比较四平台 + 识别历史 Kimi；不暴露代理实现细节', async ({ page }) => {
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/workbench`);
  await page.getByRole('tab', { name: '平台表现' }).click();
  for (const n of ['豆包', '千问', 'DeepSeek', '元宝']) {
    await expect(page.getByText(n, { exact: true }).first()).toBeVisible();
  }
  await expect(page.getByText('部分结果由 DeepSeek 模型配合秘塔检索代理获得')).toHaveCount(0);
  await page.getByRole('button', { name: /历史平台/ }).click();
  await expect(page.getByText('Kimi')).toBeVisible();
  await shot(page, 'task3-platforms');
  await scanPrivacyAndJargon(page);
});

test('任务4：生成洞察 → 失败 → 重试成功；确定性内容始终可读', async ({ page }) => {
  const ctrl = await installObservationMock(page, 'insight_fail');
  await page.goto(`${HOME}#/workbench`);
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
  await page.getByRole('button', { name: '生成洞察' }).click();
  await expect(page.getByText('AI 洞察暂不可用，图表和证据仍可正常查看。')).toBeVisible();
  await shot(page, 'task4a-insight-failed');
  ctrl.scenario = 'happy';
  await page.getByRole('button', { name: '重试' }).click();
  await expect(page.getByText('推荐率暂时下降', { exact: false })).toBeVisible({ timeout: 15_000 });
  expect(ctrl.counts.insightCreate).toBe(2); // 失败 1 + 重试 1
  await shot(page, 'task4b-insight-completed');
  await scanPrivacyAndJargon(page);
});

test('任务5：管理员查看待审/只读健康/模型漂移；审核写为 capability-pending（无假按钮）', async ({ page }) => {
  await installObservationMock(page, 'happy');
  await page.goto(`${HOME}#/admin`);
  await expect(page.getByText('需要关注').first()).toBeVisible();
  // 只读平台健康
  await page.getByRole('tab', { name: '平台与模型' }).click();
  await expect(page.getByText('模型版本漂移')).toBeVisible();
  await expect(page.getByText('平台启停由治理配置接口提供', { exact: false })).toBeVisible();
  // 样本治理：capability-pending，无假审核按钮
  await page.getByRole('tab', { name: '样本治理' }).click();
  await expect(page.getByText('样本晋升审核、撤回等写操作由治理接口提供', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: /批准|拒绝晋升|提交/ })).toHaveCount(0);
  await shot(page, 'task5-admin-governance');
  await scanPrivacyAndJargon(page);
});
