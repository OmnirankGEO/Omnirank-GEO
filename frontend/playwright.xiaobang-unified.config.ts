import { defineConfig, devices } from 'playwright/test'
import { assertPortFreeBeforeServerStarts } from './tests/xiaobang-unified/server-guard'

// 🔴 在 config 求值时就查端口 —— Playwright 是**先起 webServer 再跑 globalSetup**,
//    放 globalSetup 里会把我们自己刚起的 vite 当成占用者(第一次真跑就踩了)。
assertPortFreeBeforeServerStarts()

const identities = ['admin', 'agent', 'normal', 'member'] as const
const sizes = {
  desktop: { width: 1920, height: 1080 },
  fourk: { width: 3840, height: 2160 },
  mobile: { width: 390, height: 844 },
} as const

export default defineConfig({
  testDir: './tests/xiaobang-unified',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  projects: identities.flatMap((identity) => Object.entries(sizes).map(([size, viewport]) => ({
    name: `${identity}-${size}`,
    use: {
      ...devices['Desktop Chrome'],
      viewport,
      isMobile: size === 'mobile',
      hasTouch: size === 'mobile',
    },
  }))),
  use: {
    baseURL: 'http://127.0.0.1:4186',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  // 🔴 R3-P4 ③:基础设施红不许静默当产品红。
  //    端口空闲检查在文件顶部(config 求值期,早于 webServer 启动)。
  //    globalSetup **采集本轮子进程身份**(pid + 创建时间 + 命令行 hash);
  //    globalTeardown 只杀身份逐字段相等者,身份不匹配一律禁杀、只报红。
  //    (R3-P5:「开跑后才出现的就是我们的」这个时序判据已作废 —— 时序不是身份。)
  globalSetup: './tests/xiaobang-unified/server-guard.ts',
  globalTeardown: './tests/xiaobang-unified/global-teardown.ts',
  webServer: {
    // 直接 node 跑 vite 入口,**一层子进程**。原来的 `npm run dev -- …`
    // 是 npm.cmd → node → vite 三层壳,Playwright kill 的是最外层,
    // vite 本体在 Windows 上会活下来 —— Codex 见到的「残留 vite 占着 4186」
    // 就是这么来的。(`npx vite` 仍有一层 npx 壳,所以也不用。)
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4186 --strictPort',
    url: 'http://127.0.0.1:4186',
    timeout: 120_000,
    reuseExistingServer: false,
    // 端口被占时 --strictPort 让 vite 直接退出;stderr 留着才看得见它说了什么。
    stdout: 'pipe',
    stderr: 'pipe',
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
})
