import { defineConfig, devices } from 'playwright/test'

const port = process.env.QA_TITLE_FAILURE_PORT || String(46900 + Math.floor(Math.random() * 500))
process.env.QA_TITLE_FAILURE_PORT = port

export default defineConfig({
  testDir: './tests/title-failure-visibility',
  outputDir: process.env.QA_TITLE_FAILURE_OUTPUT_DIR || '../_qa_title_failure_artifacts/test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
