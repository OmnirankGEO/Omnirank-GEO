import { defineConfig, devices } from 'playwright/test';

const port = process.env.QA_WHITELABEL_PORT || String(46000 + Math.floor(Math.random() * 500));
process.env.QA_WHITELABEL_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/whitelabel-scope',
  outputDir: process.env.QA_WHITELABEL_OUTPUT_DIR || '../_qa_whitelabel_scope_artifacts/test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
