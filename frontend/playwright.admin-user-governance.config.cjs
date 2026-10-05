const { defineConfig, devices } = require('playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: 'admin-user-governance.spec.cjs',
  outputDir: '../_qa_admin_user_governance/test-results',
  fullyParallel: false,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4178',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python ../tests/admin_user_governance/ui_mock_server.py',
      url: 'http://127.0.0.1:8015/api/auth/me',
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 4178',
      url: 'http://127.0.0.1:4178',
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        VITE_DEV_PROXY_TARGET: 'http://127.0.0.1:8015',
        VITE_DEV_WS_TARGET: 'ws://127.0.0.1:8015',
      },
    },
  ],
});
