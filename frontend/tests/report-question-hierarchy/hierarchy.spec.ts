import { expect, test, type Page } from 'playwright/test';

/**
 * [工单 2026-08-03 ③] 问句标题视觉层级 · 行为级锁(真渲染 + 几何/计算样式断言)。
 * 🔴 不做源码字符串断言 —— 换个等价 class 写法就会假红/假绿。
 *    判据全是 getComputedStyle / getBoundingClientRect 能读出来的事实。
 */

async function mount(page: Page) {
  await page.goto('/login');
  await page.evaluate(async () => {
    const h = await import('/tests/report-question-hierarchy/harness.tsx');
    await (h as { mountReportSections: (ms?: number) => Promise<void> }).mountReportSections(600);
  });
  // 证据行默认折叠在平台表格里 —— 不展开就量不到任何东西
  // (第一版忘了这步,8 条锁全红且看起来像"改坏了",实为夹具没展开)。
  // 🔴 平台表现有**两套布局**:`hidden md:block` 表格 与 `md:hidden` 卡片列表,
  //    两边都渲染同一个 PlatformEvidence。480px 下表格整体隐藏,
  //    若不过滤可见性就会点到隐藏表格里的按钮 → 行永远不可见 → 锁假红。
  //    所以定位器一律只取可见元素,窄宽两档各自量自己那套布局。
  const expand = page.getByRole('button', { name: /展开.*的证据/ }).filter({ visible: true }).first();
  await expand.click();
  await expect(visibleRow(page)).toBeVisible({ timeout: 5_000 });
}

function visibleRow(page: Page) {
  return page.locator('[data-testid="platform-evidence-row"]').filter({ visible: true }).first();
}

async function metrics(page: Page) {
  return page.evaluate(() => {
    // 只量【可见】那一套布局的行(另一套被 md: 断点整体隐藏,量它没有意义)
    const row = Array.from(document.querySelectorAll('[data-testid="platform-evidence-row"]'))
      .find((el) => (el as HTMLElement).offsetParent !== null
                    && (el as HTMLElement).getBoundingClientRect().height > 0);
    if (!row) return null;
    const title = row.querySelector('[data-testid="evidence-question-title"]') as HTMLElement | null;
    const body = row.querySelector('[data-testid="evidence-answer-body"]') as HTMLElement | null;
    const badge = row.querySelector('span') as HTMLElement | null;
    if (!title || !body || !badge) return null;
    const cs = getComputedStyle(title);
    const bodyText = body.querySelector('p') || body;
    const bcs = getComputedStyle(bodyText as Element);
    const tr = title.getBoundingClientRect();
    const br = badge.getBoundingClientRect();
    return {
      titleSize: parseFloat(cs.fontSize),
      titleWeight: parseInt(cs.fontWeight, 10),
      bodySize: parseFloat(bcs.fontSize),
      bodyWeight: parseInt(bcs.fontWeight, 10),
      borderTop: parseFloat(getComputedStyle(body).borderTopWidth),
      titleTop: Math.round(tr.top),
      badgeTop: Math.round(br.top),
      titleLeft: Math.round(tr.left),
      badgeRight: Math.round(br.right),
      rowRight: Math.round(row.getBoundingClientRect().right),
      titleRight: Math.round(tr.right),
    };
  });
}

for (const [label, w] of [['窄档', 480], ['宽档', 1440]] as const) {
  test(`锁1 · ${label}(${w}px):问句必须比答案正文更重更大`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await mount(page);
    const m = await metrics(page);
    expect(m).not.toBeNull();
    // 🔴 判别力核心:改之前问句是 text-xs/font-medium,与正文同级甚至更弱。
    expect(m!.titleSize).toBeGreaterThan(m!.bodySize);
    expect(m!.titleWeight).toBeGreaterThan(m!.bodyWeight);
    expect(m!.titleWeight).toBeGreaterThanOrEqual(600);
  });

  test(`锁2 · ${label}(${w}px):问句与答案正文之间有可见分隔线`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await mount(page);
    const m = await metrics(page);
    expect(m!.borderTop).toBeGreaterThan(0);
  });

  test(`锁3 · ${label}(${w}px):长问句换行时状态徽章仍与首行对齐,不被挤走`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await mount(page);
    const m = await metrics(page);
    // 徽章顶边与问句顶边相差不超过一行(原 flex-wrap 在窄档会把问句整块挤到徽章下一行,
    // 那时 titleTop - badgeTop 会达到徽章高度量级)
    expect(Math.abs(m!.titleTop - m!.badgeTop)).toBeLessThanOrEqual(8);
    // 徽章在问句左侧(同一行内)
    expect(m!.badgeRight).toBeLessThanOrEqual(m!.titleLeft + 1);
  });

  test(`锁4 · ${label}(${w}px):问句不溢出行容器`, async ({ page }) => {
    await page.setViewportSize({ width: w, height: 900 });
    await mount(page);
    const m = await metrics(page);
    expect(m!.titleRight).toBeLessThanOrEqual(m!.rowRight);
  });
}
