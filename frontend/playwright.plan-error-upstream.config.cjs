/**
 * 黄框上游收敛判据的 Playwright 配置
 * (工单 WO_PLAN_SIDE_MISMATCH_UPSTREAM_2026-09-03)。
 *
 * 与同目录 `playwright.flywheel-binding.config.cjs` 同构 —— 复用仓里既有范式:
 * python 夹具 server + `npm run dev` + 真浏览器。端口错开(4182 / 8018),
 * 免得和飞轮那套并跑时互相抢。
 */

const { defineConfig, devices } = require('playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: 'plan-error-upstream.spec.cjs',
  outputDir: '../_qa_plan_error_upstream/test-results',
  fullyParallel: false,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4182',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python ../tests/plan_error_upstream_2026_09_03/ui_mock_server.py',
      url: 'http://127.0.0.1:8018/api/auth/me',
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 4182',
      url: 'http://127.0.0.1:4182',
      reuseExistingServer: true,
      timeout: 180_000,
      env: {
        VITE_DEV_PROXY_TARGET: 'http://127.0.0.1:8018',
        VITE_DEV_WS_TARGET: 'ws://127.0.0.1:8018',
      },
    },
  ],
});
