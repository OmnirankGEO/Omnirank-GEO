const { expect, test } = require('playwright/test');

const viewports = [
  { width: 320, height: 720 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1366, height: 900 },
  { width: 1440, height: 900 },
];

async function openGovernance(page, width, height) {
  await page.setViewportSize({ width, height });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'playwright-admin-token');
    localStorage.setItem('omnirank_screenshot_mode', '1');
  });
  await page.goto('/admin/users');
  await expect(page.getByTestId('admin-user-governance-page')).toBeVisible();
  // The product intentionally auto-opens the first user and hides the list below lg.
  // Desktop keeps both panes, so select the deterministic fixture row there.
  if (await page.getByTestId('user-row-124').isVisible()) {
    await page.getByTestId('user-row-124').click();
  }
  await expect(page.getByTestId('tab-overview')).toBeVisible();
}

for (const viewport of viewports) {
  test(`管理员用户治理布局 ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    const consoleErrors = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await openGovernance(page, viewport.width, viewport.height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await expect(page.locator('h2[title="林若晴 · 华东区品牌增长与数字化运营中心"]')).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`admin-user-${viewport.width}x${viewport.height}.png`),
      fullPage: true,
    });
    expect(consoleErrors).toEqual([]);
  });
}

test('移动端键盘可完成危险操作预览并用 Escape 退出', async ({ page }) => {
  await openGovernance(page, 390, 844);
  await page.getByRole('button', { name: '服务关系' }).click();
  await expect(page.getByTestId('tab-relationships')).toBeVisible();
  await page.getByRole('button', { name: '修改归属' }).click();
  const dialog = page.getByTestId('governance-change-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('修改前', { exact: true })).toBeVisible();
  await expect(dialog.getByText('修改后', { exact: true })).toBeVisible();
  const reason = page.getByLabel('操作原因（必填）');
  await reason.focus();
  await page.keyboard.type('客户书面确认转由专用服务商承接');
  await expect(page.getByRole('button', { name: '确认并保存证据' })).toBeEnabled();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('服务商渠道关系展示独立版本与系数控制', async ({ page }) => {
  await openGovernance(page, 1440, 900);
  await page.getByTestId('user-row-28').click();
  await page.getByRole('button', { name: '服务关系' }).click();
  const relationships = page.getByTestId('tab-relationships');
  await expect(relationships.getByText('直属供货关系', { exact: true })).toBeVisible();
  await expect(relationships.getByText('平台直营', { exact: true })).toHaveCount(0);
  await expect(page.getByText('1.2', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '调整直属上游' }).click();
  const dialog = page.getByTestId('governance-change-dialog');
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel('进货系数')).toHaveValue('1.2');
  await expect(dialog.getByText('历史订单和账务数据不变。', { exact: false })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('管理员可重置密码且接口回包不返回密码材料', async ({ page }) => {
  await openGovernance(page, 1440, 900);
  await page.getByRole('button', { name: '权限与安全' }).click();
  await page.getByRole('button', { name: '重置登录密码' }).click();
  const dialog = page.getByTestId('password-reset-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('至少输入 8 个字符')).toBeVisible();
  await page.getByLabel('临时密码').fill('123456');
  await page.getByLabel('再次输入').fill('123456');
  await page.getByLabel('操作原因（必填）').fill('用户完成身份核验后申请管理员重置');
  await expect(page.getByRole('button', { name: '确认重置' })).toBeDisabled();
  const secret = 'Playwright-Temporary-Password-2026';
  await page.getByLabel('临时密码').fill(secret);
  await page.getByLabel('再次输入').fill(secret);
  await expect(dialog.getByText('密码格式符合要求')).toBeVisible();
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === 'PUT' && response.url().endsWith('/password'));
  await page.getByRole('button', { name: '确认重置' }).click();
  const response = await responsePromise;
  const body = await response.text();
  expect(body).not.toContain(secret);
  expect(body).not.toContain('password_hash');
  await expect(dialog).toBeHidden();
});

test('最高管理员算力校正有明确非收入提示并提交版本化请求', async ({ page }) => {
  await openGovernance(page, 1440, 900);
  await page.getByRole('button', { name: '钱包与账单' }).click();
  await page.getByRole('button', { name: '校正算力' }).click();
  const dialog = page.getByTestId('wallet-adjustment-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('不形成充值收入', { exact: false })).toBeVisible();
  await page.getByLabel('算力类型').click();
  await page.getByRole('option', { name: '付费算力' }).click();
  await page.getByLabel('校正方式').click();
  await page.getByRole('option', { name: '增加' }).click();
  await page.getByLabel('算力数量').fill('1300');
  await page.getByLabel('核验原因（必填）').fill('核对支付凭证后修正历史到账差异');
  const requestPromise = page.waitForRequest((request) =>
    request.method() === 'PUT' && request.url().endsWith('/wallet-adjustment'));
  await page.getByRole('button', { name: '确认校正' }).click();
  const request = await requestPromise;
  const payload = request.postDataJSON();
  expect(payload).toEqual(expect.objectContaining({
    point_type: 'paid', operation: 'add', amount: 1300, expected_version: 1,
  }));
  expect(request.headers()['x-request-id']).toContain('admin-user-wallet-');
  await expect(dialog).toBeHidden();
});
