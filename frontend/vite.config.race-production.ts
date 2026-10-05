/** Fixture-free production candidate bundle probe; output is outside the worktree. */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
    plugins: [react()],
    publicDir: false,
    resolve: { alias: { '@': path.resolve(__dirname, './src') } },
    build: {
        outDir: path.resolve(__dirname, '../../_qa_public_report_race_a/production-candidate-bundle'),
        emptyOutDir: true,
        target: 'es2020',
        sourcemap: false,
        rollupOptions: {
            input: path.resolve(__dirname, 'race-public-report-production.html'),
        },
    },
});
