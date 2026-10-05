import { defineConfig, devices } from 'playwright/test';

// [R2-5 2026-08-15] 下一步 CTA 真渲染判据(返修单 §1-R2-5:「元素存在」不算,
// 「可点且有效」才算)。配置形态照抄 playwright.frontend-nogo.config.ts。
const port = process.env.QA_NEXTSTEP_PORT || String(43000 + Math.floor(Math.random() * 5000));
process.env.QA_NEXTSTEP_PORT = port;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/next-step-cta',
  outputDir: '../_qa_next_step_cta_artifacts/test-results',
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
  webServer: {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
