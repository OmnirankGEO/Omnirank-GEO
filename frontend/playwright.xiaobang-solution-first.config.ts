import { defineConfig, devices } from 'playwright/test'

// 🔴 独立 config + 独立端口(4186/4187/4188 已被 unified / prefill / session-isolation 占用)。
//   unified 那份的用例总数被 `test_fab_accessibility.py` 钉死在 144 / 156,不能往里加 spec。
export default defineConfig({
  testDir: './tests/xiaobang-solution-first',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  projects: [{
    name: 'solution-first-desktop',
    use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
  }],
  use: {
    baseURL: 'http://127.0.0.1:4189',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4189 --strictPort',
    url: 'http://127.0.0.1:4189',
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
})
