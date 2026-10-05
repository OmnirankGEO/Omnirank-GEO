import { defineConfig, devices } from 'playwright/test';

// [WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY 2026-08-17 Part② 判据1]
// 行业 chip 的 DOM 判据必须打**真渲染**,不是正则扫源码写法(换皮即绕过)。
const port = process.env.QA_PUBIND_PORT || String(43800 + Math.floor(Math.random() * 200));
process.env.QA_PUBIND_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;
const useExternalServer = process.env.QA_PUBIND_EXTERNAL_SERVER === '1';

export default defineConfig({
  testDir: './tests/publish-industry',
  outputDir: process.env.QA_PUBIND_OUTPUT_DIR || '../_qa_publish_industry_artifacts/test-results',
  fullyParallel: false,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL,
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: useExternalServer ? undefined : {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
