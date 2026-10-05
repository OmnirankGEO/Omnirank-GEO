const { defineConfig, devices } = require('playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: 'invrel-inventory.spec.cjs',
  outputDir: '../_qa_invrel/test-results',
  fullyParallel: false,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4179',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python ../tests/inventory_relation_model_2026_08_13/ui_mock_server.py',
      url: 'http://127.0.0.1:8016/api/auth/me',
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 4179',
      url: 'http://127.0.0.1:4179',
      reuseExistingServer: true,
      timeout: 180_000,
      env: {
        VITE_DEV_PROXY_TARGET: 'http://127.0.0.1:8016',
        VITE_DEV_WS_TARGET: 'ws://127.0.0.1:8016',
      },
    },
  ],
});
