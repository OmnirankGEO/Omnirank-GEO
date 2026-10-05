/**
 * 生产预览包守卫：证明 Playwright 跑的是 vite build 产物，而非 dev server。
 * - dev server 的 HTML 会注入 /@vite/client 与 @react-refresh 且脚本指向 /src/*.tsx；
 * - 生产构建的 HTML 引用哈希化的 /assets/*.js。
 * 该守卫失败即说明「58 passed」可能是对着 dev server 跑的假绿，必须转红。
 */

import { test, expect } from 'playwright/test';

const HOME = '/geo-observation-preview.html';

test('生产预览包：HTML 引用哈希 /assets/ 且无 dev-only 注入（不是 dev server）', async ({ page, baseURL }) => {
  const res = await page.request.get(`${baseURL}${HOME}`);
  expect(res.ok(), `预览首页不可达：${res.status()}`).toBeTruthy();
  const html = await res.text();

  // 生产构建：入口脚本是哈希化的 /assets/*.js
  expect(html, 'HTML 未引用哈希 /assets/*.js（疑似跑在 dev server 上）').toMatch(/\/assets\/[\w.-]+\.js/);

  // dev-only 注入一律不得存在
  for (const marker of ['/@vite/client', '@react-refresh', '/src/']) {
    expect(html, `HTML 含 dev 标记「${marker}」——这是 dev server，不是生产包`).not.toContain(marker);
  }
});
