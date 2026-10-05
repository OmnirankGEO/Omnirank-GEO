const { defineConfig, devices } = require('playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: 'flywheel-binding-failures.spec.cjs',
  outputDir: '../_qa_flywheel_binding/test-results',
  fullyParallel: false,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4181',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python ../tests/flywheel_binding_liveness_2026_08_15/ui_mock_server.py',
      url: 'http://127.0.0.1:8017/api/auth/me',
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 4181',
      url: 'http://127.0.0.1:4181',
      reuseExistingServer: true,
      timeout: 180_000,
      env: {
        VITE_DEV_PROXY_TARGET: 'http://127.0.0.1:8017',
        VITE_DEV_WS_TARGET: 'ws://127.0.0.1:8017',
      },
    },
  ],
});
