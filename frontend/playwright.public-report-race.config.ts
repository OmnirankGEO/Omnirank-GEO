/**
 * playwright.public-report-race.config.ts — 赛马候选 A 验收配置
 *
 * 八视口硬门:320×700 / 390×844 / 768×1024 / 1024×768 / 1366×768 /
 *            1440×900 / 1920×1080 / 2560×1440
 * webServer:race harness(vite.config.race.ts · 端口 4199)
 * 产物输出到仓外 C:/AI-Test/_qa_public_report_race_a
 */
import { defineConfig, devices } from 'playwright/test';

const QA_ROOT = '../../_qa_public_report_race_a';

export default defineConfig({
    testDir: './tests/public-report-race',
    outputDir: `${QA_ROOT}/results`,
    timeout: 45_000,
    expect: { timeout: 10_000 },
    fullyParallel: true,
    retries: 0,
    workers: 4,
    reporter: [['list'], ['html', { outputFolder: `${QA_ROOT}/html-report`, open: 'never' }]],
    use: {
        baseURL: 'http://127.0.0.1:4199',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
        locale: 'zh-CN',
        timezoneId: 'Asia/Shanghai',
    },
    projects: [
        { name: 'v320', use: { ...devices['Desktop Chrome'], viewport: { width: 320, height: 700 } } },
        { name: 'v390', use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 }, hasTouch: true } },
        { name: 'v768', use: { ...devices['Desktop Chrome'], viewport: { width: 768, height: 1024 } } },
        { name: 'v1024', use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } } },
        { name: 'v1366', use: { ...devices['Desktop Chrome'], viewport: { width: 1366, height: 768 } } },
        { name: 'v1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
        { name: 'v1920', use: { ...devices['Desktop Chrome'], viewport: { width: 1920, height: 1080 } } },
        { name: 'v2560', use: { ...devices['Desktop Chrome'], viewport: { width: 2560, height: 1440 } } },
    ],
    webServer: {
        command: 'node scripts/public-report-race-preview.mjs',
        url: 'http://127.0.0.1:4199/race-public-report.html',
        reuseExistingServer: false,
        timeout: 90_000,
    },
});
