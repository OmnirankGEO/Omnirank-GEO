import { build } from 'vite';
import { resolve } from 'node:path';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let report;
await build({
  root: frontendDir,
  configFile: resolve(frontendDir, 'vite.config.ts'),
  logLevel: 'warn',
  build: { write: false },
  plugins: [{
    name: 'omnirank-entry-module-report',
    generateBundle(_options, bundle) {
      const entry = Object.values(bundle).find(item => item.type === 'chunk' && item.isEntry);
      if (!entry || entry.type !== 'chunk') throw new Error('entry chunk not found');
      report = {
        fileName: entry.fileName,
        rawBytes: entry.code.length,
        modules: Object.entries(entry.modules)
          .map(([id, meta]) => ({ id: id.replace(/\\/g, '/'), renderedLength: meta.renderedLength, originalLength: meta.originalLength }))
          .sort((a, b) => b.renderedLength - a.renderedLength),
      };
    },
  }],
});
if (!report) throw new Error('entry bundle report was not generated');
console.log(JSON.stringify(report, null, 2));
