// [P0-2 2026-08-15] 真渲染验收:批量通过失败时,原因必须**在页面上看得见**,
// 且 toast 样式必须分档(全失败 = 非 success)。
//
// 出口闸原文:「失败原因在前端可见(打真渲染,不是 grep 源码)」+
//            「toast:全失败 → 非 success 样式(这条要能被自动化断言)」。
// sonner 把语义写在 DOM 上(`[data-sonner-toast][data-type=...]`),所以样式可机器判定,
// 不靠肉眼看颜色。
//
// 每条「必须命中」都配一条「必须不命中」:
//   · 全失败必须 data-type=error 且**不得**出现 data-type=success;
//   · 全成功必须 success 且**不得**出现明细面板(否则面板是恒显示的,断言没有判别力)。

const { test, expect } = require('playwright/test');

const PAGE = '/admin/geo-placement-flywheel';
const MOCK = 'http://127.0.0.1:8017';

async function setScenario(page, name) {
  const res = await page.request.get(`${MOCK}/__scenario?name=${name}`);
  expect(res.ok()).toBeTruthy();
}

async function openBindingReview(page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('omnirank_token', 'playwright-flywheel-token');
  });
  await page.goto(PAGE);
  // 绑定审核在「高级」控制台里的「媒体建议」二级 tab
  await page.getByRole('tab', { name: '高级' }).click();
  await page.getByRole('tab', { name: '媒体建议' }).click();
  const btn = page.getByRole('button', { name: /一键通过全部行业的建议通过/ });
  await expect(btn).toBeEnabled({ timeout: 30_000 });
  return btn;
}

async function clickApproveAll(page) {
  const btn = await openBindingReview(page);
  await btn.click();
  await page.getByRole('button', { name: /^通过这 \d+ 条$/ }).click();
}

test('全失败:toast 是 error 样式,且四条失败原因逐条渲染出来', async ({ page }) => {
  await setScenario(page, 'all_fail');
  await clickApproveAll(page);

  // ① toast 分档 —— 修前这里走的是 toast.success(绿勾配「已通过 0 条」)
  const toast = page.locator('[data-sonner-toast]').first();
  await expect(toast).toHaveAttribute('data-type', 'error', { timeout: 15_000 });
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0);

  // ② 失败明细真渲染(不是 grep 源码):四条都在,且带媒体名 + 原因文本
  const panel = page.getByTestId('binding-failure-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('binding-failure-item')).toHaveCount(4);
  await expect(panel).toContainText('扬道财经（csdn博客）');
  await expect(panel).toContainText('网易城市（山东）');
  await expect(panel).toContainText('库存不可采购');

  // ③ 可折叠
  await page.getByTestId('binding-failure-toggle').click();
  await expect(page.getByTestId('binding-failure-item')).toHaveCount(0);
  await page.getByTestId('binding-failure-toggle').click();
  await expect(page.getByTestId('binding-failure-item')).toHaveCount(4);
});

test('部分失败:toast 是 warning 样式,明细只列失败那条', async ({ page }) => {
  await setScenario(page, 'partial_fail');
  await clickApproveAll(page);

  const toast = page.locator('[data-sonner-toast]').first();
  await expect(toast).toHaveAttribute('data-type', 'warning', { timeout: 15_000 });
  await expect(page.getByTestId('binding-failure-item')).toHaveCount(1);
});

test('全成功(反向对照):toast 是 success,且明细面板不出现', async ({ page }) => {
  await setScenario(page, 'all_ok');
  await clickApproveAll(page);

  const toast = page.locator('[data-sonner-toast]').first();
  await expect(toast).toHaveAttribute('data-type', 'success', { timeout: 15_000 });
  // 🔴 没有这一条,「面板恒显示」也能让上面两个用例全绿
  await expect(page.getByTestId('binding-failure-panel')).toHaveCount(0);
});
