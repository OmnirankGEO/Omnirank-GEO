import { expect, test, type Page, type Route } from 'playwright/test';

/**
 * [工单 2026-08-03 ①] 「查看完整回答」· 行为级锁(真渲染真点击,不看源码字符串)
 *
 * 🔴 生产实测把工单前提订正了两处,锁按**真实数据形态**写:
 *   1. `identity_evidence_snippet` 全表 613 行恒为 NULL —— ±420 证据窗口从未落库;
 *      卡片里显示的其实是 `response_snippet` = 全文前 500 字逐字前缀。
 *   2. 待确认条目的 `identity_candidates` 恒为空数组。
 *   所以主锁跑的是 seen_prefix 形态(生产现状),另配一条 evidence_window 形态的锁,
 *   保证窗口将来落库时定位自动切到窗口、不被兜底吃掉。
 */

const LIST = '/api/monitoring/identity-reviews';
const HEAD = '甲乙丙丁戊'.repeat(100);                    // 500 字 —— 卡片里已经能看到的部分
const TAIL = '这一段在片段之外，用户原本看不到。'.repeat(20); // 只存在于全文
const FULL = HEAD + TAIL;
const WINDOW = FULL.slice(600, 660);
// 🔴 初版 TAIL 只有一句,FULL 不足 600 字 → WINDOW 是空串,锁4 的
//    `expect(marked).toBe(WINDOW)` 变成 ''==='' 的恒真断言。数据前提必须先钉死。
if (WINDOW.length !== 60) throw new Error('测试数据前提破了:窗口切片必须是 60 字');

type Shape = 'seen_prefix' | 'evidence_window' | 'no_anchor';

async function install(page: Page, shape: Shape) {
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(body),
    });

    if (url.pathname.endsWith('/full-response')) {
      if (shape === 'evidence_window') {
        return json({ status: 'success', result_id: 9001, full_response: FULL,
          anchor: 'evidence_window', anchor_start: 600, anchor_end: 660 });
      }
      if (shape === 'no_anchor') {
        return json({ status: 'success', result_id: 9001, full_response: FULL,
          anchor: 'none', anchor_start: null, anchor_end: null });
      }
      return json({ status: 'success', result_id: 9001, full_response: FULL,
        anchor: 'seen_prefix', anchor_start: HEAD.length, anchor_end: FULL.length });
    }

    if (url.pathname.endsWith(LIST)) {
      return json({ status: 'success', count: 1, items: [{
        id: 9001, keyword: '测试关键词', platform: 'qwen',
        response_snippet: HEAD,               // 生产形态:500 字前缀
        identity_candidates: [],              // 生产形态:空数组
        identity_evidence_snippet: null,      // 生产形态:恒 NULL
        identity_evidence_hash: 'h1', identity_decision_version: 1,
        tested_at: '2026-08-03T00:00:00Z',
      }] });
    }
    return json({ success: true, items: [], data: {} });
  });
}

async function mount(page: Page) {
  await page.goto('/login');
  await page.evaluate(async () => {
    const harness = await import('/tests/login-gate/identityReviewHarness.tsx');
    await (harness as { mountIdentityReviewPanel: (b: number, ms?: number) => Promise<string> })
      .mountIdentityReviewPanel(4242, 600);
  });
}

test('锁1 · 卡片上有「查看完整回答」入口,点开能看到片段之外的内容', async ({ page }) => {
  await install(page, 'seen_prefix');
  await mount(page);

  // 展开前:全文里独有的那段绝不能已经在页面上(否则这条锁证明不了"点开才看得到")
  await expect(page.getByTestId('full-answer-9001')).toHaveCount(0);
  expect(await page.locator('body').innerText()).not.toContain(TAIL);

  const toggle = page.getByTestId('toggle-full-answer-9001');
  await expect(toggle).toBeVisible();
  await toggle.click();

  const full = page.getByTestId('full-answer-9001');
  await expect(full).toBeVisible();
  await expect(full).toContainText(TAIL);
  await expect(full).toContainText(`完整回答共 ${FULL.length} 字`);
});

test('锁2 · 定位锚点落在「已看过的部分」之后,不是落在开头', async ({ page }) => {
  await install(page, 'seen_prefix');
  await mount(page);
  await page.getByTestId('toggle-full-answer-9001').click();

  const anchor = page.getByTestId('full-answer-anchor-9001');
  await expect(anchor).toBeVisible();

  // 🔴 判别力核心:锚点**之前**的正文必须正好是那 500 字片段,
  //    锚点**之后**必须包含片段外的内容。定位错段时这两条同时会红。
  const split = await page.evaluate(() => {
    const box = document.querySelector('[data-testid="full-answer-9001"] .whitespace-pre-wrap');
    const mark = document.querySelector('[data-testid="full-answer-anchor-9001"]');
    if (!box || !mark) return null;
    const all = box.textContent || '';
    const at = all.indexOf(mark.textContent || '');
    return { beforeLen: at, after: all.slice(at + (mark.textContent || '').length) };
  });
  expect(split).not.toBeNull();
  expect(split!.beforeLen).toBe(HEAD.length);
  expect(split!.after).toContain(TAIL);
});

test('锁3 · 锚点真的被滚进视口(不是渲染出来就算)', async ({ page }) => {
  await install(page, 'seen_prefix');
  await mount(page);
  await page.getByTestId('toggle-full-answer-9001').click();
  await expect(page.getByTestId('full-answer-anchor-9001')).toBeInViewport();
});

test('锁4 · 证据窗口有值时用窗口定位,不被 seen_prefix 兜底吃掉', async ({ page }) => {
  await install(page, 'evidence_window');
  await mount(page);
  await page.getByTestId('toggle-full-answer-9001').click();

  const marked = await page.getByTestId('full-answer-anchor-9001').textContent();
  expect(marked).toBe(WINDOW);
  expect(marked!.length).toBe(60);        // 防恒真:空串不算命中
  expect(marked).not.toBe(TAIL);          // 反向:不是兜底那一段
});

test('锁5 · 定位不到时仍然给全文(不阻断),只是不高亮', async ({ page }) => {
  await install(page, 'no_anchor');
  await mount(page);
  await page.getByTestId('toggle-full-answer-9001').click();

  await expect(page.getByTestId('full-answer-9001')).toContainText(TAIL);
  await expect(page.getByTestId('full-answer-anchor-9001')).toHaveCount(0);
});

test('锁6 · 收起后全文从 DOM 消失(展开态是真状态,不是一直挂着靠 CSS 藏)', async ({ page }) => {
  await install(page, 'seen_prefix');
  await mount(page);
  const toggle = page.getByTestId('toggle-full-answer-9001');
  await toggle.click();
  await expect(page.getByTestId('full-answer-9001')).toBeVisible();
  await toggle.click();
  await expect(page.getByTestId('full-answer-9001')).toHaveCount(0);
});

test('锁8 · 面板是四边圆角卡,不是 border-y 全出血带 [工单 ② · Owner 拍板]', async ({ page }) => {
  await install(page, 'seen_prefix');
  await mount(page);

  const box = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="identity-review-panel"]');
    if (!el) return null;
    const cs = getComputedStyle(el);
    const px = (v: string) => Math.round(parseFloat(v) || 0);
    return {
      top: px(cs.borderTopWidth), bottom: px(cs.borderBottomWidth),
      left: px(cs.borderLeftWidth), right: px(cs.borderRightWidth),
      radius: px(cs.borderTopLeftRadius),
      padLeft: px(cs.paddingLeft), padRight: px(cs.paddingRight),
    };
  });
  expect(box).not.toBeNull();
  // 🔴 判别力全在左右两条边上:border-y 时它们恒为 0,圆角卡才 > 0。
  //    只断言"有上下边框"是恒真的(改之前也满足),所以不能那样写。
  expect(box!.left).toBeGreaterThan(0);
  expect(box!.right).toBeGreaterThan(0);
  expect(box!.radius).toBeGreaterThan(0);
  // 上下边框仍在(反向:别把边框整个删掉当成"改好了")
  expect(box!.top).toBeGreaterThan(0);
  expect(box!.bottom).toBeGreaterThan(0);
  // 横向内边距不能因为改样式又丢了(那正是 M-1 ⑤ 修过的老毛病)
  expect(box!.padLeft).toBeGreaterThan(0);
  expect(box!.padRight).toBeGreaterThan(0);
});

test('锁7 · 列表接口不夹带全文(全文只能按 result_id 单独拉)', async ({ page }) => {
  await install(page, 'seen_prefix');
  const calls: string[] = [];
  page.on('request', r => { if (r.url().includes('identity-reviews')) calls.push(r.url()); });
  await mount(page);

  // 只挂载、不点开时:只能打列表,不能打 full-response
  expect(calls.some(u => u.includes('/full-response'))).toBe(false);
  await page.getByTestId('toggle-full-answer-9001').click();
  await expect(page.getByTestId('full-answer-9001')).toBeVisible();
  // 点开后才打,且带 brand_id(RBAC 需要)
  const full = calls.filter(u => u.includes('/full-response'));
  expect(full.length).toBe(1);
  expect(full[0]).toContain('brand_id=4242');
});
