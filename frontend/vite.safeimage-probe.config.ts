/**
 * 取证专用构建 —— 返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §5。
 * 只把 `SafeMarkdownImageProbe` 打成一个独立页面供 Playwright 真渲染取证；
 * 不进生产包（生产入口是 index.html / main.tsx，与此无关）。
 */
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: {
    outDir: 'dist-safeimage-probe',
    emptyOutDir: true,
    rollupOptions: { input: path.resolve(__dirname, 'safeimage-probe.html') },
  },
})
