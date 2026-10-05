import { defineConfig, devices } from 'playwright/test';

// [工单 2026-07-29] 协议门禁死循环 + 静默阻断 · 行为级判别门。
// 全部真渲染真点击 —— 本单明确不接受源码字符串断言。
const port = process.env.QA_LOGIN_GATE_PORT || String(44700 + Math.floor(Math.random() * 200));
process.env.QA_LOGIN_GATE_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/login-gate',
  outputDir: '../output/playwright/login-gate',
  fullyParallel: false,
  workers: 1,
  retries: 0,
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
