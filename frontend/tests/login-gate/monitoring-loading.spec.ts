import { expect, test, type Page, type Route } from 'playwright/test';

/**
 * 工单 2026-07-29 §5 · 锁 6(监测中心「仅这 0 条待确认」)
 *
 * 事故形态:`if (!loading && items.length === 0) return null;` 把加载态漏在判空之外。
 * 数据还没回来时 items 恒为 [],于是先渲染出"仅这 0 条待确认" —— 把一个还没查出来的
 * 结论当成结论告诉用户。
 *
 * 🔴 行为级:真的用 React 渲染这个组件,断言 DOM。不看源码字符串。
 *    组件挂在监测页深处(要品牌上下文+鉴权),所以这里直接在浏览器里挂载它本身,
 *    渲染路径与线上完全一致(同一份 tsx、同一个 React、同一套 hooks)。
 */

const ENDPOINT = /\/api\/monitoring\/identity-reviews/;

async function mountPanel(page: Page): Promise<string> {
  await page.goto('/login');
  return page.evaluate(async () => {
    const harness = await import('/tests/login-gate/identityReviewHarness.tsx');
    return (harness as {
      mountIdentityReviewPanel: (brandId: number, settleMs?: number) => Promise<string>;
    }).mountIdentityReviewPanel(4242, 600);
  });
}

test('锁6 · 加载中且 0 条 → 组件不渲染(不出现"仅这 0 条待确认")', async ({ page }) => {
  // 请求挂住不返回 → 组件全程停在 loading=true / items=[]
  await page.route(ENDPOINT, async () => { await new Promise(() => {}); });

  const html = await mountPanel(page);
  expect(html).not.toContain('待确认');
  expect(html).not.toContain('仅这');
  expect(html.trim()).toBe('');
  await expect(page.getByTestId('identity-review-panel')).toHaveCount(0);
});

test('锁6b · 数据回来且有 1 条 → 组件正常渲染(反向锁:不是把面板改成永不显示)', async ({ page }) => {
  await page.route(ENDPOINT, (route: Route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [{
        result_id: 9001, keyword: '测试关键词', engine: 'qwen', answer_excerpt: '示例回答',
        candidates: ['甲品牌', '乙品牌'], detected: null, brand_name: '甲品牌',
      }],
    }),
  }));

  const html = await mountPanel(page);
  expect(html).toContain('待确认');
  await expect(page.getByTestId('identity-review-panel')).toBeVisible();
});

test('锁6c · 数据回来但 0 条 → 仍然不渲染(空态本来就该静默)', async ({ page }) => {
  await page.route(ENDPOINT, (route: Route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }),
  }));

  const html = await mountPanel(page);
  expect(html.trim()).toBe('');
});
