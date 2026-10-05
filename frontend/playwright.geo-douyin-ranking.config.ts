import { defineConfig } from 'playwright/test'

/**
 * GEO 抖音图文「同行榜单」入口 · 真实 UI 请求体断言
 *
 * 🔴 为什么必须单独有这一套:上一版的"端到端"是直接 `await run_image_post_production(
 *    content_form="ranking")` —— 那是**后端函数测试**,它证明不了用户在页面上点得出来。
 *    实际情况正是:后端 `content_form=="ranking"` 早就通了,而创建页根本不发这个字段,
 *    于是整条榜单链只有 API 可达。本套用真浏览器点页面、在网络层抓 POST /posts 的
 *    请求体,断言 `content_form` 真的带上了。
 */
export default defineConfig({
  testDir: './tests/geo-douyin-ranking',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4186',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4186',
    url: 'http://127.0.0.1:4186',
    timeout: 180_000,
    reuseExistingServer: false,
  },
})
