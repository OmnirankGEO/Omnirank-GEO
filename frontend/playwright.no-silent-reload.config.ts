import { defineConfig, devices } from 'playwright/test';

// [WO_NO_SILENT_RELOAD_DIRTY_GUARD 2026-08-16] 脏表单守卫 · 真浏览器行为锁。
// 端口随机 + reuseExistingServer:false —— 本仓多 worktree 抢端口时,
// 别人的 dev server 照样返 200,验的就成了别人树的代码。
const port = process.env.QA_NO_SILENT_RELOAD_PORT || String(46100 + Math.floor(Math.random() * 200));
process.env.QA_NO_SILENT_RELOAD_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/no-silent-reload',
  outputDir: '../output/playwright/no-silent-reload',
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
