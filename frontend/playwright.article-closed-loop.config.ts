import { defineConfig } from 'playwright/test';

const viewports = [
  { name: 'phone320', width: 320, height: 700 },
  { name: 'phone390', width: 390, height: 844 },
  { name: 'tablet768', width: 768, height: 1024 },
  { name: 'desktop1366', width: 1366, height: 768 },
  { name: 'desktop1920', width: 1920, height: 1080 },
];

export default defineConfig({
  testDir: './tests/article-closed-loop',
  outputDir: '../_qa_article_closed_loop/playwright-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:4193',
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: viewports.map(({ name, width, height }) => ({
    name,
    use: { viewport: { width, height } },
  })),
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4193',
    url: 'http://127.0.0.1:4193',
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
