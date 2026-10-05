import { defineConfig, devices } from 'playwright/test';

const port = process.env.QA_NOGO_PORT || String(43000 + Math.floor(Math.random() * 5000));
// Playwright evaluates the config in worker processes as well. Pin the chosen
// exclusive port so every worker and the webServer share the same value.
process.env.QA_NOGO_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;
const useExternalServer = process.env.QA_NOGO_EXTERNAL_SERVER === '1';

export default defineConfig({
  testDir: './tests/frontend-nogo',
  outputDir: process.env.QA_NOGO_OUTPUT_DIR || '../_qa_frontend_nogo_artifacts/test-results',
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
