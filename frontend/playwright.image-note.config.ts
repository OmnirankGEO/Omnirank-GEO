import { defineConfig } from 'playwright/test'

/**
 * GEO 图文合同链三页 · 真浏览器行为测试(规格 01 §8 / §10)
 *
 * 🔴 为什么必须有这一套:规格 01 §10 不可签收条件最后一条写死 ——
 *    「只有 TSX 字符串锁,没有真实浏览器行为测试」= 不可签收。
 *    源码扫描证明不了「320px 上主 CTA 够得着」「Tab 顺序与视觉一致」
 *    「未知态没有重试按钮」这些**只在渲染后才存在**的事实。
 */
export default defineConfig({
  testDir: './tests/image-note',
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4192',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4192',
    url: 'http://127.0.0.1:4192',
    timeout: 180_000,
    reuseExistingServer: false,
  },
})
