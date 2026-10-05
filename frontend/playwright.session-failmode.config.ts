import { defineConfig, devices } from 'playwright/test';

// [BUG-3 2026-07-27] 会话确认失败方式的真渲染门。
// 事故形态:/api/auth/me 慢 → requireConfirmedSessionToken() 抛错 → ErrorBoundary → 整页白屏。
// 这里在真实页面上跑"慢 5s"与"401"两种情况,证明用户看到的是 loading / 登录引导,不是白屏。
const port = process.env.QA_SESSION_FAILMODE_PORT || String(44900 + Math.floor(Math.random() * 300));
process.env.QA_SESSION_FAILMODE_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/session-failmode',
  outputDir: '../output/playwright/session-failmode',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [['line']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', video: 'off' },
  projects: [
    { name: 'desktop1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
