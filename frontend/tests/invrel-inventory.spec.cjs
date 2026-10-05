/**
 * invrel 包 · 浏览器验收(工单 §4.2 库存按钮 / §4.5 所有提示都有出口)
 *
 * 🔴 §4.5 明令:「不允许仅靠源码字符串断言冒充浏览器行为」。
 *    所以这里全部打**真渲染**:按钮真的在、真的可点、点了真的换 tab、焦点真的落到档位上。
 */
const { expect, test } = require('playwright/test');

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: '4k', width: 3840, height: 2160 },
  { name: 'mobile', width: 390, height: 844 },
];

async function useScenario(page, name) {
  const res = await page.request.get(`http://127.0.0.1:8016/__scenario?name=${name}`);
  expect(res.ok()).toBeTruthy();
}

async function openInventory(page, { width, height }) {
  await page.setViewportSize({ width, height });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'playwright-agent-token');
    localStorage.setItem('omnirank_screenshot_mode', '1');
  });
  await page.goto('/agent/inventory');
}

for (const vp of VIEWPORTS) {
  test(`库存 0 时无死按钮 · 去进货可点并聚焦档位 ${vp.name} ${vp.width}x${vp.height}`, async ({ page }, testInfo) => {
    await useScenario(page, 'zero');
    await openInventory(page, vp);

    const goPurchase = page.getByTestId('selfuse-go-purchase');
    await expect(goPurchase).toBeVisible({ timeout: 20000 });
    // 🔴 核心:这个按钮**不是** disabled(改造前是 `disabled={!hasInventory}` 的灰色死按钮)
    await expect(goPurchase).toBeEnabled();
    // 反向对照:库存 0 时不该同时出现"转为可用算力"
    await expect(page.getByTestId('selfuse-convert')).toHaveCount(0);

    await goPurchase.click();
    // 点完真的落到进货区的**首个可选档位**上(不是只滚动)。
    // ⚠️ 用 waitForFunction 而不是 click 后立刻 evaluate:焦点是在 tab 内容挂载后
    //    下一帧才落定,瞬时快照在 4K 上必定拍空(视口越大提交越晚)。
    await page.waitForFunction(
      () => document.activeElement?.hasAttribute('data-purchase-focus') === true,
      undefined,
      { timeout: 5000 },
    );
    // 反向对照:确实存在可选档位 —— 否则上面等到的可能只是兜底输入框换了个身份
    expect(await page.locator('[data-purchase-focus]').count()).toBeGreaterThan(0);

    // 页面不得横向滚动(§4.2 三视口)
    expect(await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1
    )).toBe(true);

    await page.screenshot({
      path: testInfo.outputPath(`invrel-zero-${vp.name}.png`),
      fullPage: true,
    });
  });
}

test('库存 > 0 显示转换按钮(反向对照:证明上面的"去进货"不是恒显示)', async ({ page }) => {
  await useScenario(page, 'stocked');
  await openInventory(page, VIEWPORTS[0]);
  await expect(page.getByTestId('selfuse-convert')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('selfuse-go-purchase')).toHaveCount(0);
});

test('余额接口失败 → 出现「重新读取」且点击真的重新请求', async ({ page }) => {
  await useScenario(page, 'error');
  await openInventory(page, VIEWPORTS[0]);

  const retry = page.getByTestId('selfuse-retry');
  await expect(retry).toBeVisible({ timeout: 20000 });
  await expect(retry).toBeEnabled();

  // 🔴 判据打**真实网络请求**,不是"按钮存在就算数"
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/agent/inventory/balance'), { timeout: 15000 }),
    retry.click(),
  ]);
  expect(request.url()).toContain('/api/agent/inventory/balance');
});

test('🔴 搜索命中我的下线服务商 · 关系卡显示 effect_note 与真实出口', async ({ page }) => {
  await useScenario(page, 'stocked');
  await openInventory(page, VIEWPORTS[0]);

  await page.getByRole('tab', { name: '线下划拨' }).click();
  await page.getByRole('button', { name: '划拨' }).first().click();

  const search = page.getByPlaceholder('输入手机号 / 客户名 / 客户ID 搜索');
  await expect(search).toBeVisible({ timeout: 15000 });
  await search.fill('13900000002');
  await page.getByRole('button', { name: '搜索' }).click();

  // 改造前这里返回空("查无此人")—— 现在必须命中并标出关系态。
  // ⚠️ 不能用 getByText('下线服务商'):上方那段说明文字里也有这四个字,
  //    `.first()` 会命中 <p> 而不是结果行,点了什么也不会发生(测试自身的坑)。
  const row = page.getByRole('button').filter({ hasText: 'ID 946002' }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await expect(row).toContainText('我的下线服务商');
  await row.click();

  // §P0-3 第 4 条:提交前必须看得到"这次会发生什么"
  await expect(page.getByText(/进 TA 的库存算力/).first()).toBeVisible();
  // §0.3:出口必须真实可点
  await expect(page.getByRole('link', { name: /查看渠道关系/ })).toBeVisible();

  // 🔴 [P0 热修 §5] 同一个人只渲染**一张**卡:选中后结果行里不该再留一份。
  await expect(page.getByText('ID 946002')).toHaveCount(1);
});

// ============================================================
// P0 热修 · WO_INVREL_P0_HOTFIX §6 第 7 条
// ============================================================

async function openAllocateWithDownstream(page, vp) {
  await useScenario(page, 'stocked');
  await openInventory(page, vp);
  await page.getByRole('tab', { name: '线下划拨' }).click();
  await page.getByRole('button', { name: '划拨' }).first().click();
  const search = page.getByPlaceholder('输入手机号 / 客户名 / 客户ID 搜索');
  await expect(search).toBeVisible({ timeout: 15000 });
  await search.fill('13900000002');
  await page.getByRole('button', { name: '搜索' }).click();
  const row = page.getByRole('button').filter({ hasText: 'ID 946002' }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
}

for (const vp of VIEWPORTS) {
  test(`🔴 下线服务商可直接供货 · 不再被阻断 ${vp.name} ${vp.width}x${vp.height}`, async ({ page }, testInfo) => {
    await openAllocateWithDownstream(page, vp);

    // [xfer 工单 v2 §R2] 三桶已收敛成两格:第一格叫「充值算力」,提示语说的是**落点**。
    //   (旧断言打的是「工具算力 / 诊断、写作等功能消耗」—— 那是单账本收敛后
    //    已经不存在的区别,2026-08-17 随 §R2 一起改掉。)
    await expect(page.getByText('充值算力')).toBeVisible();
    await expect(page.getByText('进对方的库存算力 · 从你的充值库存扣')).toBeVisible();

    // 目标是下线时,按钮文案与合计行都切成"供货"
    await expect(page.getByTestId('allocate-submit')).toHaveText('确认供货');
    await page.getByLabel('充值算力').fill('10000');
    // [P1-A] 合计行:消掉"是不是总数"的疑问
    await expect(page.getByTestId('allocate-total')).toContainText('本次共供货');
    // 数字用站内 formatPoints 渲染(万为单位),这里断言它**跟着输入变**,
    // 而不是写死某个字面量 —— 写死等于把格式化实现也钉进判据。
    await expect(page.getByTestId('allocate-total')).toContainText('1.00万');

    // 🔴 真的能提交 —— 断言打**真实网络请求**,不是"按钮没禁用就算数"
    const [request] = await Promise.all([
      page.waitForRequest((r) => r.url().includes('/api/agent/inventory/supply-downstream'), { timeout: 15000 }),
      page.getByTestId('allocate-submit').click(),
    ]);
    expect(request.method()).toBe('POST');
    expect(JSON.parse(request.postData() || '{}').paid_points).toBe(10000);

    expect(await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1
    )).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`invrel-supply-${vp.name}.png`),
      fullPage: true,
    });
  });
}

test('🔴 [P1-B] 关系识别结果不得用红色错误样式渲染', async ({ page }) => {
  const errorToasts = [];
  await openAllocateWithDownstream(page, VIEWPORTS[0]);
  // 识别结果("这是你的下线服务商")必须出现在结果卡里,而不是 error toast
  await expect(page.getByText('这是你的下线服务商').first()).toBeVisible();
  for (const el of await page.locator('[data-sonner-toast][data-type="error"]').all()) {
    errorToasts.push(await el.textContent());
  }
  expect(errorToasts.filter((t) => (t || '').includes('下线服务商'))).toEqual([]);
});

test('🔴 陌生账号与不存在账号:搜索结果同为空(不可区分)', async ({ page }) => {
  await useScenario(page, 'stocked');
  await openInventory(page, VIEWPORTS[0]);

  await page.getByRole('tab', { name: '线下划拨' }).click();
  await page.getByRole('button', { name: '划拨' }).first().click();
  const search = page.getByPlaceholder('输入手机号 / 客户名 / 客户ID 搜索');
  await expect(search).toBeVisible({ timeout: 15000 });

  for (const keyword of ['13900000005', '13900009999']) {
    await search.fill(keyword);
    await page.getByRole('button', { name: '搜索' }).click();
    await expect(page.getByText('没有找到该账号')).toBeVisible({ timeout: 10000 });
  }
});


// ============================================================
// P0-C · WO_INVREL_P0_HOTFIX §9.5 接线判据
// 🔴 目的:防止「端点在、没人点」的死功能再次发生。
//    删掉补录按钮的 onClick(或整张卡),本用例必须转红。
// ============================================================

test('🔴 [P0-C] 亮灯的人工归属有可点的「补录凭证」入口且真的发请求', async ({ page }) => {
  await useScenario(page, 'admin');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'playwright-admin-token');
    localStorage.setItem('omnirank_screenshot_mode', '1');
  });
  await page.goto('/admin/users');

  // 进入「服务关系」页
  await page.getByRole('button', { name: '服务关系' }).click();

  // 亮灯 → 补录卡必须出现,且按钮可点(不是灰色死按钮)
  const card = page.getByTestId('backfill-card');
  await expect(card).toBeVisible({ timeout: 20000 });
  const open = page.getByTestId('backfill-open');
  await expect(open).toBeEnabled();
  await open.click();

  // 必填理由:没填之前提交必须是禁用的(反向对照 —— 否则"必填"是句空话)
  await expect(page.getByTestId('backfill-submit')).toBeDisabled();
  await page.getByTestId('backfill-reason').fill('2026-07 双方线下确认由该服务商承接');
  await expect(page.getByTestId('backfill-submit')).toBeEnabled();

  // 🔴 判据打**真实网络请求**,不是"按钮存在就算数"
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/commercial-service-binding/audit-backfill'), { timeout: 15000 }),
    page.getByTestId('backfill-submit').click(),
  ]);
  expect(request.method()).toBe('POST');
  expect(JSON.parse(request.postData() || '{}').reason).toContain('线下确认');
});


// ============================================================
// xfer 工单 v2(2026-08-17)· §4 判据 1/2/3 的浏览器侧
// 🔴 全部打**渲染后的 DOM**,不 grep 源码。
// ============================================================

/** 打开划拨弹窗并搜索选中某个目标。 */
async function pickTarget(page, keyword, rowMarker) {
  await useScenario(page, 'stocked');
  await openInventory(page, VIEWPORTS[0]);
  await page.getByRole('tab', { name: '线下划拨' }).click();
  await page.getByRole('button', { name: '划拨' }).first().click();
  const search = page.getByPlaceholder('输入手机号 / 客户名 / 客户ID 搜索');
  await expect(search).toBeVisible({ timeout: 15000 });
  await search.fill(keyword);
  await page.getByRole('button', { name: '搜索' }).click();
  const row = page.getByRole('button').filter({ hasText: rowMarker }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
  return row;
}

test('🔴 [§R1] 服务商目标的标题是 display_name,绝不是名下品牌名', async ({ page }, testInfo) => {
  // 夹具**故意**让后端仍旧返回泄露的品牌名(见 ui_mock_server.PROVIDER_LEAKED_BRAND),
  // 前端这一道必须自己顶住 —— 否则本判据在旧前端上也会绿,等于没判据。
  await pickTarget(page, '13900000002', 'ID 946002');

  // 🔴 顺序有讲究:**先**打一条两臂都存在的语义断言(弹窗渲染文本里有没有那个品牌名)。
  //    先打新加的 data-testid 的话,旧前端上只会报"element not found",
  //    看不出到底是"标题错了"还是"页面根本没渲染" —— 那种红没有诊断价值。
  const dialogText = await page.getByRole('dialog').innerText();
  expect(dialogText).not.toContain('贵州省禾椒香食品有限公司');
  // 反向对照(判别力自证):证明夹具真的把它发过来了,不是"网络里就没有这个词"。
  const payload = await page.evaluate(async () => {
    const r = await fetch('/api/agent/customers/lookup?q=13900000002&limit=8');
    return await r.text();
  });
  expect(payload).toContain('贵州省禾椒香食品有限公司');

  // 再钉标题这个元素本身 —— 防止将来品牌名只是被挪到别处而标题仍然错。
  const title = page.getByTestId('target-title');
  await expect(title).toBeVisible();
  await expect(title).toHaveText('下线服务商');

  await page.screenshot({ path: testInfo.outputPath('xfer-r1-provider-title.png'), fullPage: true });
});

test('🔴 [§R1 双向] 普通客户目标的标题仍然是品牌名', async ({ page }) => {
  await pickTarget(page, '13900000003', 'ID 946003');
  // 两臂都成立的语义断言先行(同上)。
  const dialogText = await page.getByRole('dialog').innerText();
  // 客户卡的标题 = 品牌名(这一位对客户是**该显示**的 —— 反向对照:
  // 证明 §R1 不是把所有人的品牌名都砍了)
  expect(dialogText).toContain('普通客户的品牌');
  // §R3:余额行不再有"发布"这一格(旧前端渲染的是「剩余: 算力 … · 发布 … · 赠送 …」)
  expect(dialogText).not.toContain('发布');

  await expect(page.getByTestId('target-title')).toHaveText('普通客户的品牌');
  // 余额两个数来自客户 `user_wallets`,与夹具给的 52000 / 4200 对上
  const balance = page.getByTestId('customer-balance');
  await expect(balance).toBeVisible();
  // formatPoints(v35w2Api): >=10000 走「万」两位小数,否则原样 String(v)
  await expect(balance).toContainText('充值 5.20万');   // 52000
  await expect(balance).toContainText('赠送 4200');     // 4200
});

test('🔴 [§R2] 划拨/供货弹窗恰好两个算力输入框 · 渲染文本无「发布算力」', async ({ page }, testInfo) => {
  await pickTarget(page, '13900000003', 'ID 946003');

  // 🔴 恰好两个 —— 打**渲染后的 DOM**,不是数源码里的 <Input>。
  //    作用域取整个 dialog(两臂都存在的选择器):旧前端是 3 个,失败信息直接
  //    告诉你"expected 2, received 3",而不是"找不到我新加的 testid"。
  const dialogBoxes = page.getByRole('dialog').locator('input[type="number"]');
  await expect(dialogBoxes).toHaveCount(2);

  const dialogText = await page.getByRole('dialog').innerText();
  expect(dialogText).not.toContain('发布算力');
  expect(dialogText).not.toContain('工具算力');
  expect(dialogText).not.toContain('只能用于发布投放');
  // 反向对照:两格的名字确实在(不是把整块删了导致上面三条恒真)
  expect(dialogText).toContain('充值算力');
  expect(dialogText).toContain('赠送算力');
  const grid = page.getByTestId('allocate-points-grid').locator('input[type="number"]');
  await expect(grid).toHaveCount(2);
  await expect(page.getByTestId('alloc-recharge')).toBeVisible();
  await expect(page.getByTestId('alloc-bonus')).toBeVisible();

  await page.screenshot({ path: testInfo.outputPath('xfer-r2-two-boxes.png'), fullPage: true });
});

test('🔴 [§R2] 两格 → 请求体:tool_points=充值 · publish_points 恒 0 · bonus_points=赠送', async ({ page }) => {
  await pickTarget(page, '13900000003', 'ID 946003');

  await page.getByLabel('充值算力').fill('30000');
  await page.getByLabel('赠送算力').fill('5000');
  await expect(page.getByTestId('allocate-total')).toContainText('本次共划拨');

  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/agent/inventory/allocate-offline'), { timeout: 15000 }),
    page.getByTestId('allocate-submit').click(),
  ]);
  const body = JSON.parse(request.postData() || '{}');
  expect(request.method()).toBe('POST');
  expect(body.tool_points).toBe(30000);
  expect(body.bonus_points).toBe(5000);
  // 🔴 废字段仍在请求里(后端请求模型不动),但恒 0
  expect(body).toHaveProperty('publish_points');
  expect(body.publish_points).toBe(0);
});

test('🔴 [§R2] 供货给下线也是两格,充值格整额进 paid_points', async ({ page }) => {
  await pickTarget(page, '13900000002', 'ID 946002');

  await expect(page.getByRole('dialog').locator('input[type="number"]')).toHaveCount(2);
  await page.getByLabel('充值算力').fill('10000');
  await page.getByLabel('赠送算力').fill('1000');

  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/agent/inventory/supply-downstream'), { timeout: 15000 }),
    page.getByTestId('allocate-submit').click(),
  ]);
  const body = JSON.parse(request.postData() || '{}');
  expect(body.paid_points).toBe(10000);
  expect(body.bonus_points).toBe(1000);
});


// ============================================================
// 残留 1(Review-CTO 增量授权 2026-08-17)· 撤回弹窗同形收敛
// 🔴 撤回是资金**反向**流,判据与划拨侧同形:恰两框 / 请求体 / 双向标题。
// ============================================================

/** 打开「撤回算力」弹窗并选中目标。 */
async function pickRevokeTarget(page, keyword, rowMarker) {
  await useScenario(page, 'stocked');
  await openInventory(page, VIEWPORTS[0]);
  await page.getByRole('tab', { name: '线下划拨' }).click();
  await page.getByRole('button', { name: '撤回算力' }).first().click();
  const search = page.getByPlaceholder('输入手机号 / 客户名 / 客户ID 搜索');
  await expect(search).toBeVisible({ timeout: 15000 });
  await search.fill(keyword);
  await page.getByRole('button', { name: '搜索' }).click();
  const row = page.getByRole('button').filter({ hasText: rowMarker }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
}

test('🔴 [残留1] 撤回弹窗恰好两个算力输入框 · 渲染文本无「发布算力」', async ({ page }, testInfo) => {
  await pickRevokeTarget(page, '13900000003', 'ID 946003');

  // 作用域取整个 dialog(两臂都存在的选择器):旧前端是 3 个,
  // 失败信息直接是 "expected 2, received 3",而不是"找不到我新加的 testid"。
  await expect(page.getByRole('dialog').locator('input[type="number"]')).toHaveCount(2);

  const dialogText = await page.getByRole('dialog').innerText();
  expect(dialogText).toContain('线下撤回');
  expect(dialogText).not.toContain('发布算力');
  // 反向对照:两格的名字确实在(不是把整块删了导致上面那条恒真)
  expect(dialogText).toContain('充值算力');
  expect(dialogText).toContain('赠送算力');

  const grid = page.getByTestId('revoke-points-grid').locator('input[type="number"]');
  await expect(grid).toHaveCount(2);
  await expect(page.getByTestId('revoke-recharge')).toBeVisible();
  await expect(page.getByTestId('revoke-bonus')).toBeVisible();

  await page.screenshot({ path: testInfo.outputPath('xfer-revoke-two-boxes.png'), fullPage: true });
});

test('🔴 [残留1] 撤回两格 → 请求体:tool_points=充值 · publish_points 恒 0 · bonus_points=赠送', async ({ page }) => {
  await pickRevokeTarget(page, '13900000003', 'ID 946003');

  await page.getByLabel('充值算力').fill('12000');
  await page.getByLabel('赠送算力').fill('2000');
  await page.getByLabel('原因').fill('客户线下退款');

  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/agent/inventory/revoke-offline'), { timeout: 15000 }),
    page.getByTestId('revoke-submit').click(),
  ]);
  const body = JSON.parse(request.postData() || '{}');
  expect(request.method()).toBe('POST');
  expect(body.tool_points).toBe(12000);
  expect(body.bonus_points).toBe(2000);
  // 🔴 废字段仍在请求里(后端请求模型不动),但恒 0
  expect(body).toHaveProperty('publish_points');
  expect(body.publish_points).toBe(0);
  expect(body.reason).toBe('客户线下退款');
});

test('🔴 [残留1 双向] 撤回弹窗:普通客户标题=品牌名 · 服务商标题=display_name', async ({ page }) => {
  // 🔴 先打两臂都存在的语义断言,再打新加的 data-testid —— 顺序反了的话
  //    旧前端只会报 "element not found",看不出是标题错了还是页面没渲染。

  // ① 服务商 —— 夹具仍在回泄露的品牌名(见 ui_mock_server.PROVIDER_LEAKED_BRAND),
  //    撤回弹窗与划拨弹窗共用 CustomerPicker,这一道必须同样顶住。
  await pickRevokeTarget(page, '13900000002', 'ID 946002');
  const providerText = await page.getByRole('dialog').innerText();
  expect(providerText).not.toContain('贵州省禾椒香食品有限公司');
  await expect(page.getByTestId('target-title')).toHaveText('下线服务商');

  // ② 普通客户 —— 品牌名是**该显示**的(反向对照:证明上面不是把品牌名一刀切)
  await pickRevokeTarget(page, '13900000003', 'ID 946003');
  const customerText = await page.getByRole('dialog').innerText();
  expect(customerText).toContain('普通客户的品牌');
  await expect(page.getByTestId('target-title')).toHaveText('普通客户的品牌');
});
