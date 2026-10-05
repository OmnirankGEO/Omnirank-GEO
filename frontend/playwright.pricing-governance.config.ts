import { defineConfig, devices } from 'playwright/test';

// [WP2 §5.4] 用户详情内嵌定价治理面板 + <GovernanceAlert> 的七视口响应式门。
const port = process.env.QA_PRICING_GOV_PORT || String(43920 + Math.floor(Math.random() * 400));
process.env.QA_PRICING_GOV_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

// Owner 指定七视口:超窄手机 → 4K。
const viewports = [
  { name: 'w320', width: 320, height: 720 },
  { name: 'w390', width: 390, height: 844 },
  { name: 'w768', width: 768, height: 1024 },
  { name: 'w1366', width: 1366, height: 768 },
  { name: 'w1440', width: 1440, height: 900 },
  { name: 'w1920', width: 1920, height: 1080 },
  { name: 'w2560', width: 2560, height: 1440 },
];

export default defineConfig({
  testDir: './tests/pricing-governance',
  outputDir: '../output/playwright/pricing-governance',
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
  projects: viewports.map(({ name, width, height }) => ({
    name,
    use: { ...devices['Desktop Chrome'], viewport: { width, height } },
  })),
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
