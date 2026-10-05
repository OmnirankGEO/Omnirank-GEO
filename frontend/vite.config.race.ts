/**
 * vite.config.race.ts — 赛马候选 A 独立 harness 配置
 *
 * 独立于主 vite.config.ts:不改生产入口/路由/构建;
 * 独立端口 4199,独立产物目录 dist-race-public-report。
 * 用法:
 *   dev:   npx vite --config vite.config.race.ts
 *   build: npx vite build --config vite.config.race.ts
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function customerEventHarnessStub(): Plugin {
    const install = (middlewares: { use: (handler: (req: { method?: string; url?: string; resume: () => void }, res: { statusCode: number; end: () => void }, next: () => void) => void) => void }) => {
        middlewares.use((req, res, next) => {
            if (req.method === 'POST' && req.url?.split('?', 1)[0] === '/api/m3/customer-events/public') {
                req.resume();
                res.statusCode = 204;
                res.end();
                return;
            }
            next();
        });
    };

    return {
        name: 'public-report-race-customer-event-stub',
        configureServer: (server) => install(server.middlewares),
        configurePreviewServer: (server) => install(server.middlewares),
    };
}

export default defineConfig({
    plugins: [react(), customerEventHarnessStub()],
    publicDir: false,
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    server: {
        host: '127.0.0.1',
        port: 4199,
        strictPort: true,
    },
    preview: {
        host: '127.0.0.1',
        port: 4199,
        strictPort: true,
    },
    build: {
        outDir: 'dist-race-public-report',
        emptyOutDir: false,
        target: 'es2020',
        sourcemap: false,
        rollupOptions: {
            input: path.resolve(__dirname, 'race-public-report.html'),
        },
    },
});
