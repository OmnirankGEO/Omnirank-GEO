import { defineConfig, devices } from 'playwright/test';

// [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11] 报价系数顶栏 + 演示隐私模式 · 真浏览器锁。
// 端口随机 + reuseExistingServer:false —— 本仓多个 worktree 抢同一端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码(2026-08-09 custfb 那次的教训)。
const port = process.env.QA_QUOTE_PRIVACY_PORT || String(46600 + Math.floor(Math.random() * 200));
process.env.QA_QUOTE_PRIVACY_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/quote-pricing-privacy',
  outputDir: '../output/playwright/quote-pricing-privacy',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  reporter: [['line']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', video: 'off' },
  // 工单 §11.15:390px 手机 / 1440px 桌面 / 4K 屏
  projects: [
    { name: 'mobile390', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
    { name: 'desktop1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'uhd3840', use: { ...devices['Desktop Chrome'], viewport: { width: 3840, height: 2160 } } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
