import { defineConfig } from 'playwright/test';

const viewports = [
  { name: 'w320', width: 320, height: 700 },
  { name: 'w375', width: 375, height: 667 },
  { name: 'w390', width: 390, height: 844 },
  { name: 'tablet768', width: 768, height: 1024 },
  { name: 'desktop1366', width: 1366, height: 768 },
  { name: 'desktop1440', width: 1440, height: 900 },
];

export default defineConfig({
  testDir: './tests/frontend-privacy-final',
  outputDir: process.env.QA_FRONTEND_PRIVACY_OUTPUT_DIR
    || '../../_qa_frontend_audit/runs/2026-07-15-frontend-privacy-final/test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4187',
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: viewports.map(({ name, width, height }) => ({
    name,
    use: { viewport: { width, height } },
  })),
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4187',
    url: 'http://127.0.0.1:4187',
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
