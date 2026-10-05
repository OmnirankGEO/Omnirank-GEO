/**
 * 独立预览/验证构建配置（Frontend-A 观测候选）。
 * - 只构建 geo-observation-preview.html 这一入口，与主 App 生产包完全隔离。
 * - 输出到 dist-observation-preview/，用于 Playwright 与隐私/无 lookbehind 扫描。
 * - 生产模式（vite build）产出压缩包；包内无任何 fixture（数据仅测试网络层注入）。
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// 预览端口由环境变量注入（Playwright 用独占随机端口，避免复用到残留 dev server）。
const PREVIEW_PORT = Number(process.env.OBS_PREVIEW_PORT || 5199);

export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: PREVIEW_PORT,
    strictPort: true,
    host: '127.0.0.1',
  },
  preview: {
    port: PREVIEW_PORT,
    strictPort: true,
    host: '127.0.0.1',
  },
  build: {
    target: 'es2020',
    outDir: 'dist-observation-preview',
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, 'geo-observation-preview.html'),
    },
  },
});
