import { defineConfig, devices } from 'playwright/test';

// 客户反馈⑤(Review 2026-08-09 P1-3)· 监测中心"写操作后不闪白"真浏览器锁。
// 端口随机 + reuseExistingServer:false —— 本仓多个 worktree 抢同一端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码。
const port = process.env.QA_MONITORING_FLICKER_PORT || String(45500 + Math.floor(Math.random() * 200));
process.env.QA_MONITORING_FLICKER_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/monitoring-no-flicker',
  outputDir: '../output/playwright/monitoring-no-flicker',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
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
