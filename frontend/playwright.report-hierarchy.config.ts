import { defineConfig, devices } from 'playwright/test';

// [工单 2026-08-03 ③] 问句标题层级行为锁。端口随机 + 不复用他人 dev server。
const port = process.env.QA_REPORT_HIER_PORT || String(45400 + Math.floor(Math.random() * 200));
process.env.QA_REPORT_HIER_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/report-question-hierarchy',
  outputDir: '../output/playwright/report-hierarchy',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', video: 'off' },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
