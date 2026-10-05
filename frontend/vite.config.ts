import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
// Phase 08.2 (CTO-15.23 2026-05-04) · P0 · 砍 vite-plugin-pwa
// 老板诊断:registerSW.js 在 iOS Safari/Android Chrome 隐私模式下 SW.register() 抛异常
// 没 try/catch · React boot 崩 → 客户白屏(销售链路第一触点 100% 流失)
// 业务无离线需求 · 直接砍掉(选 1)· 不做 try/catch 包(选 2)
// import { VitePWA } from 'vite-plugin-pwa'
import path from 'path'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || 'http://localhost:8000'
  const wsTarget = env.VITE_DEV_WS_TARGET || 'ws://localhost:8000'
  const radixCorePackages = new Set([
    'context', 'compose-refs', 'slot', 'label', 'primitive', 'use-layout-effect',
    'id', 'use-controllable-state', 'use-callback-ref', 'use-escape-keydown',
    'dismissable-layer', 'focus-scope', 'portal', 'presence', 'focus-guards',
    'dialog', 'alert-dialog', 'arrow', 'use-size', 'popper',
  ])
  return {
  base: env.VITE_CDN_URL || '/',
  cacheDir: './output/vite-cache',
  plugins: [
    react(),
    // Phase 08.2 · 砍 vite-plugin-pwa(P0 · 隐私模式白屏修复)
    // 后续若要恢复 PWA · 必须给 registerSW + 所有 storage 调用包 try/catch · 见老板诊断
  ],
  server: {
    // 社媒主线统一使用 5173，避免同时开出 1688/5178/5182 等旧入口导致看见缓存页面。
    port: Number(env.VITE_DEV_PORT || 5173),
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      // SSE流式端点 - 禁用缓冲
      '/api/monitoring/run-stream': {
        target: proxyTarget,
        changeOrigin: true,
        headers: {
          'Accept': 'text/event-stream'
        }
      },
      '/api': {
        target: proxyTarget,
        changeOrigin: true,
        timeout: 600000,  // 10分钟超时（报价引擎需要调用多个外部API）
      },
      // #8 修复：WebSocket 代理，开发环境下前端走 5173 → 代理到后端 8000
      '/ws': {
        target: wsTarget,
        ws: true,
        changeOrigin: true
      }
    },
    watch: {
      // 忽略后端文件变动，防止监测任务触发HMR重置
      ignored: [
        '**/db/**',
        '**/data/**',
        '**/*.db',
        '**/*.sqlite',
        '**/settings.json',
        '**/logs/**'
      ]
    }
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    manifest: true,
    // Phase 08.4 (CTO-15.23 2026-05-04) · P0 · 强制 ES2020 target 兼容 mobile
    // 桌面 OK · 手机白 → 可能默认 esnext target 输出 ES2022+ 语法 · 老 mobile Safari 不支持
    // ES2020 = 主流 mobile (Safari 14+ / Chrome 80+ / Android Chrome 80+) 都支持
    target: 'es2020',
    rollupOptions: {
      output: {
        // Keep route components lazy. Icons are genuinely global; Radix is split
        // per package so route-only primitives are not pulled into the first load.
        manualChunks(id) {
          const normalized = id.replace(/\\/g, '/')
          if (normalized.includes('/node_modules/lucide-react/')) return 'vendor-icons'
          const radixPackage = normalized.match(/\/node_modules\/@radix-ui\/([^/]+)\//)?.[1]
          if (radixPackage) {
            const shortName = radixPackage.replace(/^react-/, '')
            return radixCorePackages.has(shortName) ? 'vendor-radix-core' : 'vendor-radix-routes'
          }
          return undefined
        },
      },
    },
  },
  }
})
