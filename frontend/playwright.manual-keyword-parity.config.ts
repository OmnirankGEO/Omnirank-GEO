import { defineConfig, devices } from 'playwright/test';

// [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 手动词 UI 与合同词同权 · 真浏览器渲染锁。
// 端口随机 + reuseExistingServer:false —— 本仓多 worktree 抢端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码。
const port = process.env.QA_MK_PARITY_PORT || String(45900 + Math.floor(Math.random() * 200));
process.env.QA_MK_PARITY_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/manual-keyword-parity',
  outputDir: '../output/playwright/manual-keyword-parity',
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
