import { defineConfig, devices } from 'playwright/test';

// [工单 2026-08-03 ①] 「查看完整回答」行为级锁。
// 端口随机 + reuseExistingServer:false —— 本仓多个 worktree 抢同一端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码(SSOT §8.5 记过这个坑)。
const port = process.env.QA_IDENTITY_FULL_PORT || String(45100 + Math.floor(Math.random() * 200));
process.env.QA_IDENTITY_FULL_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/identity-full-answer',
  outputDir: '../output/playwright/identity-full-answer',
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
