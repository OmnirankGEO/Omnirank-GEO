import { defineConfig, devices } from 'playwright/test'

const port = process.env.QA_HELP_CENTER_PORT || String(45500 + Math.floor(Math.random() * 500))
process.env.QA_HELP_CENTER_PORT = port

export default defineConfig({
  testDir: './tests/help-center',
  outputDir: process.env.QA_HELP_CENTER_OUTPUT_DIR || '../_qa_help_center_artifacts/test-results',
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
