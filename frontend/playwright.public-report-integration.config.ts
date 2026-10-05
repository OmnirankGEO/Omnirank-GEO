import { defineConfig, devices } from 'playwright/test';

export default defineConfig({
    testDir: './tests/public-report-integration',
    outputDir: '../../_qa_public_report_unified/results',
    timeout: 45_000,
    expect: { timeout: 10_000 },
    workers: 1,
    retries: 0,
    reporter: [['list']],
    use: {
        ...devices['Desktop Chrome'],
        baseURL: 'http://127.0.0.1:4307',
        viewport: { width: 1440, height: 900 },
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
        locale: 'zh-CN',
    },
    webServer: {
        command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 4307',
        url: 'http://127.0.0.1:4307',
        reuseExistingServer: false,
        timeout: 60_000,
    },
});
