/**
 * 服务商工作台逐控件真实性 + 洞察幂等/幂等键/品牌作用域轮询 + 普通浏览零 LLM。
 * 桌面视口跑一次。
 */

import { test, expect, type Page } from 'playwright/test';
import { installObservationMock, collectConsoleErrors, type MockController } from './mock';

const HOME = '/geo-observation-preview.html';


async function open(page: Page): Promise<{ ctrl: MockController; errors: string[] }> {
  const ctrl = await installObservationMock(page, 'happy');
  const errors = collectConsoleErrors(page);
  await page.goto(`${HOME}#/workbench`);
  await expect(page.getByText('被 AI 提到的比例').first()).toBeVisible();
  return { ctrl, errors };
}

test('五 Tab 全部可切换并加载真实数据；平台页不暴露代理实现细节', async ({ page }) => {
  const { ctrl, errors } = await open(page);
  for (const name of ['总览', '平台表现', '问题与证据', '竞争格局', '行动建议']) {
    await page.getByRole('tab', { name }).click();
    await expect(page.getByRole('tab', { name, selected: true })).toBeVisible();
  }
  await page.getByRole('tab', { name: '平台表现' }).click();
  for (const n of ['豆包', '千问', 'DeepSeek', '元宝']) {
    await expect(page.getByText(n, { exact: true }).first()).toBeVisible();
  }
  // section 六：不展示具体秘塔代理实现细节，只用通用采集说明
  await expect(page.getByText('部分结果由 DeepSeek 模型配合秘塔检索代理获得')).toHaveCount(0);
  await expect(page.getByText('官方通道')).toHaveCount(0);
  await expect(page.getByText('不同平台可能采用官方接口或搜索增强方式采集', { exact: false })).toBeVisible();
  // 普通浏览零未知端点误调（501）
  expect(ctrl.counts.unknownApi, '误调不存在端点次数').toBe(0);
  expect(errors, errors.join('\n')).toHaveLength(0);
});

test('历史平台默认折叠，展开后可见 Kimi', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '平台表现' }).click();
  await expect(page.getByText('Kimi')).toHaveCount(0);
  await page.getByRole('button', { name: /历史平台/ }).click();
  await expect(page.getByText('Kimi')).toBeVisible();
});

test('趋势：多断点各切一段（2 条参考线）+ 口径断点标注不被顶部裁切', async ({ page }) => {
  await open(page); // 默认在总览 Tab，趋势可见
  await page.waitForTimeout(400);
  // 后端有 2 个 model_shift_marker → 2 条参考线（只切第一个会退化成 1 条）
  await expect(page.locator('.recharts-reference-line-line')).toHaveCount(2);
  // 断点前后不连成一条线：至少 3 段（seg0 solid + 2 段 dashed）→ dashed 折线存在
  await expect(page.locator('.recharts-line-curve[stroke-dasharray]').first()).toBeAttached();
  // "口径断点" 参考线标注（SVG tspan，exact 以避开图例长句）可见且未被图表顶部裁切
  const label = page.getByText('口径断点', { exact: true });
  await expect(label).toBeVisible();
  const box = await label.boundingBox();
  const chart = page.locator('.recharts-wrapper').first();
  const cbox = await chart.boundingBox();
  expect(!!box && !!cbox && box!.y >= cbox!.y - 1, '口径断点标注被顶部裁切').toBeTruthy();
});

test('竞争格局：本品牌样本内提及占比 + 易混淆品牌（真实数据派生）', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '竞争格局' }).click();
  await expect(page.getByText('样本内品牌提及占比').first()).toBeVisible();
  // fixture 有一条 entity_ambiguous 问题
  await expect(page.getByText('可能混淆的品牌（需人工确认）')).toBeVisible();
});

test('证据抽屉：域名引用（无外链 URL）+ 仅平台通道（隐藏采集实现细节）+ Escape 关闭', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '问题与证据' }).click();
  await page.getByRole('button', { name: '查看证据' }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('证据详情')).toBeVisible();
  // 先证明证据内容真的渲染了（引用来源区 + 域名），再断言隐藏项
  await expect(dialog.getByText('elevatorworld.com')).toBeVisible();
  await expect(dialog.getByText('sgs.gov.cn')).toBeVisible();
  // 域名不是外链（AI-3 只给域名，无完整 URL）
  await expect(dialog.locator('a[href]')).toHaveCount(0);
  // 采集通道只显平台名，隐藏具体代理实现细节
  await expect(dialog.getByText('DeepSeek')).toBeVisible();
  for (const t of ['秘塔', '检索代理', 'metaso']) {
    await expect(dialog.getByText(t, { exact: false })).toHaveCount(0);
  }
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('长问题可展开看全称（无跑马灯）', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '问题与证据' }).click();
  const expandBtn = page.getByRole('button', { name: '展开全部' }).first();
  await expect(expandBtn).toBeVisible();
  await expandBtn.click();
  await expect(page.getByRole('button', { name: '收起' }).first()).toBeVisible();
});

test('生成洞察：X-Request-Id 幂等键 + 品牌作用域轮询 + 进度→完成，20 连点只建 1 个任务', async ({ page }) => {
  const { ctrl } = await open(page);
  const btn = page.getByRole('button', { name: '生成洞察' });
  await Promise.all(Array.from({ length: 20 }, () => btn.click({ force: true }).catch(() => {})));
  await expect(page.getByText('AI 洞察', { exact: false }).first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('推荐率暂时下降', { exact: false })).toBeVisible();
  expect(ctrl.counts.insightCreate, `创建任务应为 1，实际 ${ctrl.counts.insightCreate}`).toBe(1);
  // 创建请求带 X-Request-Id（非仅 body）
  expect(ctrl.lastCreateHeaders['x-request-id'], '缺少 X-Request-Id 幂等键').toBeTruthy();
  // 轮询走品牌作用域路径（否则会被 501 拦截，insightPoll 不会累加到完成）
  expect(ctrl.counts.insightPoll).toBeGreaterThanOrEqual(1);
  // 无误调不存在端点
  expect(ctrl.counts.unknownApi).toBe(0);
});

test('洞察证据引用可打开证据抽屉（非死控件）', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: '生成洞察' }).click();
  await expect(page.getByText('推荐率暂时下降', { exact: false })).toBeVisible({ timeout: 15_000 });
  const chip = page.getByText('主要依据').locator('..').getByRole('button', { name: '查看证据' });
  await chip.first().click();
  await expect(page.getByRole('dialog').getByText('证据详情')).toBeVisible();
});

test('普通浏览（切 Tab / 分页 / 开关抽屉）零 LLM 调用', async ({ page }) => {
  const { ctrl } = await open(page);
  for (const name of ['平台表现', '问题与证据', '竞争格局', '行动建议', '总览']) {
    await page.getByRole('tab', { name }).click();
  }
  await page.getByRole('tab', { name: '问题与证据' }).click();
  await page.getByRole('button', { name: '查看证据' }).first().click();
  await page.keyboard.press('Escape');
  expect(ctrl.counts.insightCreate).toBe(0);
  expect(ctrl.counts.insightPoll).toBe(0);
});

test('统计粒度切换触发真实重取（当日/本周/本月）', async ({ page }) => {
  await open(page);
  const requests: string[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/summary?granularity=')) requests.push(r.url());
  });
  await page.getByRole('button', { name: '本周' }).click();
  await expect.poll(() => requests.some((u) => u.includes('granularity=week'))).toBe(true);
});

test('行动建议：跳转写作带上下文，不自动执行/扣费', async ({ page }) => {
  await open(page);
  await page.getByRole('tab', { name: '行动建议' }).click();
  await expect(page.getByText('不额外扣费').first()).toBeVisible();
  await page.getByRole('button', { name: '去写作' }).first().click();
  await expect(page.getByTestId('nav-intent')).toContainText('将跳转到写作工作台');
});

test('导出概览：下载生成同口径文件（文件名与范围诚实标注为概览）', async ({ page }) => {
  await open(page);
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '导出概览' }).click(),
  ]);
  expect(download.suggestedFilename()).toContain('观测概览');
});
