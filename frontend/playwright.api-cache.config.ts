import { randomInt } from 'node:crypto';
import { defineConfig } from 'playwright/test';

const port = Number(process.env.API_CACHE_PORT || randomInt(44_000, 49_000));
process.env.API_CACHE_PORT = String(port);
const baseURL = `http://127.0.0.1:${port}`;
const useExternalServer = process.env.API_CACHE_EXTERNAL_SERVER === '1';

export default defineConfig({
  testDir: './tests',
  testMatch: 'api-cache.spec.ts',
  workers: 1,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: useExternalServer ? undefined : {
    command: `node ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${baseURL}/login`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
