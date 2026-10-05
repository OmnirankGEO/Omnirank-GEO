import { defineConfig, devices } from 'playwright/test'

// 🔴 独立 config + 独立端口:
//   · `playwright.xiaobang-unified.config.ts` 的用例总数被 `test_fab_accessibility.py`
//     钉死在 144 / 156(FAB 矩阵的分母),往那份里加 spec 会把那两条判据打红;
//   · 4186 / 4187 已被 unified / prefill 占用,这里用 4188。
export default defineConfig({
  testDir: './tests/xiaobang-session-isolation',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  projects: [{
    name: 'isolation-desktop',
    use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
  }],
  use: {
    baseURL: 'http://127.0.0.1:4188',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4188 --strictPort',
    url: 'http://127.0.0.1:4188',
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
})
