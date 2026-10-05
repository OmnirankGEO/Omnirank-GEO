import { defineConfig, devices } from 'playwright/test';

export default defineConfig({
  testDir: './tests/organization-internal-seats',
  testMatch: 'demo-customer.spec.ts',
  outputDir: 'output/playwright/demo-wysiwyg/test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:4297',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  webServer: {
    // The app root is wrapped in React.StrictMode. Use Vite dev here so the
    // setup -> cleanup -> setup lifecycle is exercised by the formal portal suite.
    command: 'npm run dev -- --host 127.0.0.1 --port 4297 --strictPort',
    url: 'http://127.0.0.1:4297',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
