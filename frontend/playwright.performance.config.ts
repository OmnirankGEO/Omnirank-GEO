import { defineConfig } from 'playwright/test';
import { execFileSync } from 'node:child_process';
import { randomBytes, randomInt } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import { resolve } from 'node:path';

const gitHead = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
const gitTree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { encoding: 'utf8' }).trim();
// Keep below Windows' common Hyper-V/WSL dynamic exclusion ranges (usually
// starting near 49,674); strictPort still fails closed if a chosen port is busy.
const port = Number(process.env.PERF_PORT || randomInt(44_000, 49_000));
// Playwright evaluates this config again inside worker processes. Pin the
// parent's random choice into their inherited environment so every page uses
// the one strict preview server started for this run.
process.env.PERF_PORT = String(port);
const buildNonce = process.env.PERF_BUILD_NONCE || randomBytes(24).toString('hex');
process.env.PERF_BUILD_NONCE = buildNonce;
const baseURL = `http://127.0.0.1:${port}`;
const externalServer = process.env.PERF_EXTERNAL_SERVER === '1';
const suiteName = String(process.env.PERF_SUITE || 'performance').replace(/[^a-z0-9_-]+/gi, '-');
const reportNonce = String(process.env.PERF_REPORT_NONCE || buildNonce).replace(/[^a-z0-9_-]+/gi, '-');
const reportFile = resolve(process.env.PERF_REPORT_FILE || `./output/playwright-performance/reports/${gitHead}-${reportNonce}-${suiteName}.json`);
if (existsSync(reportFile) && process.env.PERF_ALLOW_REPORT_OVERWRITE !== '1') throw new Error(`immutable performance report already exists: ${reportFile}`);
const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
const browsersJson = JSON.parse(readFileSync(new URL('./node_modules/playwright-core/browsers.json', import.meta.url), 'utf8'));
const chromiumDescriptor = browsersJson.browsers.find((item: { name?: string }) => item.name === 'chromium');
const browserVersion = `Chromium ${chromiumDescriptor?.browserVersion || 'unknown'} (revision ${chromiumDescriptor?.revision || 'unknown'})`;

const required = [
  { name: 'w320', width: 320, height: 700 },
  { name: 'w390', width: 390, height: 844 },
  { name: 'tablet768', width: 768, height: 1024 },
  { name: 'desktop1440', width: 1440, height: 900 },
];

const extended = [
  ...required,
  { name: 'desktop1024', width: 1024, height: 768 },
  { name: 'wide1920', width: 1920, height: 1080 },
];

export default defineConfig({
  testDir: './tests/performance',
  outputDir: `./output/playwright-performance/results/${gitHead}-${reportNonce}-${suiteName}`,
  fullyParallel: true,
  workers: Math.max(1, Number(process.env.PERF_WORKERS || (process.env.PERF_MATRIX === '1' ? '8' : '2'))),
  retries: 0,
  timeout: Math.max(30_000, Number(process.env.PERF_TEST_TIMEOUT_MS || 180_000)),
  reporter: [
    ['line'],
    ['./scripts/strict-performance-reporter.mjs'],
    ['json', { outputFile: reportFile }],
  ],
  metadata: {
    gitHead,
    gitTree,
    buildNonce,
    reportNonce,
    suiteName,
    reportFile,
    buildMode: 'forced-clean-head-production',
    browser: browserVersion,
    playwright: packageJson.devDependencies?.playwright,
    os: `${os.type()} ${os.release()} ${os.arch()}`,
    network: 'Fast 4G: 150ms RTT, 1.6Mbps down, 750Kbps up; identical cold/warm',
    command: process.env.PERF_COMMAND || process.argv.join(' '),
    runs: Number(process.env.PERF_RUNS || '1'),
    workers: Number(process.env.PERF_WORKERS || (process.env.PERF_MATRIX === '1' ? '8' : '2')),
  },
  use: {
    baseURL,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: extended.map(({ name, width, height }) => ({
    name,
    use: { viewport: { width, height } },
  })),
  webServer: externalServer ? undefined : {
    command: `node scripts/performance-preview.mjs --port ${port} --expected-head ${gitHead} --expected-tree ${gitTree} --build-nonce ${buildNonce}`,
    url: `${baseURL}/login`,
    reuseExistingServer: false,
    timeout: 300_000,
  },
});
