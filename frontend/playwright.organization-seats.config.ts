import { defineConfig, devices } from 'playwright/test';

const viewports = [
  ['w320', 320, 700],
  ['w390', 390, 844],
  ['tablet768', 768, 1024],
  ['desktop1366', 1366, 768],
  ['desktop1440', 1440, 900],
  ['desktop1920', 1920, 1080],
  ['desktop2560', 2560, 1440],
] as const;

export default defineConfig({
  testDir: './tests/organization-internal-seats',
  outputDir: process.env.ORGANIZATION_SEATS_UI_OUTPUT_DIR
    || '../_qa_organization_internal_seats/test-results',
  fullyParallel: false,
  workers: 4,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4197',
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: viewports.map(([name, width, height]) => ({
    name,
    use: { ...devices['Desktop Chrome'], viewport: { width, height } },
  })),
  webServer: {
    command: 'npm run preview -- --host 127.0.0.1 --port 4197 --strictPort',
    url: 'http://127.0.0.1:4197',
    reuseExistingServer: process.env.ORGANIZATION_SEATS_REUSE_SERVER === '1',
    timeout: 120_000,
  },
});
