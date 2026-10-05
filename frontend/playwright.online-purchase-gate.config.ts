import { defineConfig, devices } from 'playwright/test'

const port = process.env.QA_PURCHASE_GATE_PORT || String(46200 + Math.floor(Math.random() * 500))
process.env.QA_PURCHASE_GATE_PORT = port

export default defineConfig({
  testDir: './tests/online-purchase-gate',
  outputDir: process.env.QA_PURCHASE_GATE_OUTPUT_DIR || '../_qa_purchase_gate_artifacts/test-results',
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
