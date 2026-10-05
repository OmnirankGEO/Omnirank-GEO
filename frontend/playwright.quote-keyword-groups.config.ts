import { defineConfig, devices } from 'playwright/test';

// 报价关键词「地域与商业意图双向反转」· T5 前端分组真浏览器锁。
// 端口随机 + reuseExistingServer:false —— 本仓多个 worktree 抢同一端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码(2026-08-09 custfb 那次的教训)。
const port = process.env.QA_QUOTE_GROUPS_PORT || String(46200 + Math.floor(Math.random() * 200));
process.env.QA_QUOTE_GROUPS_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/quote-keyword-groups',
  outputDir: '../output/playwright/quote-keyword-groups',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  reporter: [['line']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', video: 'off' },
  // 工单 §6 前端第 6 条:320 / 390 / 768 / 1440 四视口
  projects: [
    { name: 'mobile320', use: { ...devices['Desktop Chrome'], viewport: { width: 320, height: 720 } } },
    { name: 'mobile390', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
    { name: 'tablet768', use: { ...devices['Desktop Chrome'], viewport: { width: 768, height: 1024 } } },
    { name: 'desktop1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
