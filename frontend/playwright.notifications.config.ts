import { defineConfig } from 'playwright/test';

const viewports = [
  { name: 'w320', width: 320, height: 700 },
  { name: 'w390', width: 390, height: 844 },
  { name: 'tablet768', width: 768, height: 1024 },
  { name: 'desktop1440', width: 1440, height: 900 },
];

export default defineConfig({
  testDir: './tests/notification-center',
  outputDir: process.env.QA_NOTIFICATION_OUTPUT_DIR
    || '../../_qa_frontend_audit/runs/2026-07-17-notification-center/test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4197',
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: viewports.map(({ name, width, height }) => ({
    name,
    use: { viewport: { width, height } },
  })),
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4197',
    url: 'http://127.0.0.1:4197',
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
