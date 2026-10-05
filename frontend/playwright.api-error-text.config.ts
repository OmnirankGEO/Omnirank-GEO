import { defineConfig, devices } from 'playwright/test';

// [F-2] 静默失败 / dict detail 渲染 判别门。
const port = process.env.QA_F2_PORT || String(44300 + Math.floor(Math.random() * 300));
process.env.QA_F2_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/api-error-text',
  outputDir: '../output/playwright/api-error-text',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure', video: 'off' },
  projects: [
    { name: 'desktop1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'phone390', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
