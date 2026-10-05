/**
 * Playwright 配置（Frontend-A 观测候选）。
 * - 只跑**生产模式预览包**（vite build → vite preview），绝不复用 dev server：
 *     · 独占随机端口（避免复用到本机残留的 :5199 dev 进程）；
 *     · reuseExistingServer:false（永远由本配置亲自 build+preview，端口忙则 strictPort 直接失败）；
 *     · production-guard.spec 断言 HTML 引用哈希 /assets/ 且无 /@vite/client（证明是构建产物）。
 * - 用 per-project testMatch 分工，**零条件 skip**（section 七）：
 *     · functional（1440 单视口）：功能/状态/任务/文案契约 + 生产包守卫，跑一次；
 *     · 5 个视口项目（320/390/768/1366/1440）：只跑 responsive（页面级溢出/截图）；
 *     · 2 个窄屏项目（320/390）：只跑 narrow（五 Tab 窄屏溢出 + 逐 Tab 移动截图）。
 * - 数据由 tests/geo-observation-a/mock.ts 在网络层注入；生产 bundle 无 fixture。
 */

import { defineConfig } from 'playwright/test';

const RESPONSIVE = /responsive\.spec\.ts$/;
const NARROW = /narrow\.spec\.ts$/;
// [#87 2026-09-05] 原先是**手写白名单** `(states|workbench|admin|tasks|copy-contract|production-guard)`。
// 新加一个 spec 而忘了改这里 ⇒ 它**永远不会被收集**,而「没被收集」与「通过了」在退出码上同形。
// 改成否定式:除 responsive / narrow 两类专用项目外的 spec 一律进 functional —— 新 spec 自动进分母。
const FUNCTIONAL = /\.spec\.ts$/;   // 全收,再用 testIgnore 排掉两类专用(无 lookaround)

// 独占随机端口：优先用环境注入（便于外部编排），否则随机取一个高端口。
// 关键：Playwright 会在主进程 + 每个 worker 子进程各自 import 本配置，
// 若每次都重算随机端口，worker 的 baseURL 会与主进程启动的 webServer 端口不一致
// （ERR_CONNECTION_REFUSED）。因此算一次后写回 process.env，让 worker 继承同一端口。
const PORT = Number(process.env.OBS_PREVIEW_PORT) || 20000 + Math.floor(Math.random() * 20000);
process.env.OBS_PREVIEW_PORT = String(PORT);
const ORIGIN = `http://127.0.0.1:${PORT}`;

const viewports = [
  { name: 'vp-w320', width: 320, height: 700 },
  { name: 'vp-m390', width: 390, height: 844 },
  { name: 'vp-t768', width: 768, height: 1024 },
  { name: 'vp-d1366', width: 1366, height: 768 },
  { name: 'vp-d1440', width: 1440, height: 900 },
];

const narrow = [
  { name: 'narrow-w320', width: 320, height: 700 },
  { name: 'narrow-m390', width: 390, height: 844 },
];

export default defineConfig({
  testDir: './tests/geo-observation-a',
  outputDir: './test-results/geo-observation-a',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [['line']],
  use: {
    baseURL: ORIGIN,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: [
    {
      name: 'functional',
      testMatch: FUNCTIONAL,
      testIgnore: [RESPONSIVE, NARROW],
      use: { viewport: { width: 1440, height: 900 } },
    },
    ...viewports.map(({ name, width, height }) => ({
      name,
      testMatch: RESPONSIVE,
      use: { viewport: { width, height } },
    })),
    ...narrow.map(({ name, width, height }) => ({
      name,
      testMatch: NARROW,
      use: { viewport: { width, height } },
    })),
  ],
  webServer: {
    command: 'npm run obs:serve',
    env: { OBS_PREVIEW_PORT: String(PORT) },
    url: `${ORIGIN}/geo-observation-preview.html`,
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
