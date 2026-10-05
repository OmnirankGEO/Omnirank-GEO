import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnv, preview } from 'vite';
import { createPerformanceFixturePlugin } from './performance-fixture.mjs';

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const checkpoint = (label) => { if (process.env.PERF_DEBUG === '1') console.error(`[performance-preview] ${label}`); };
checkpoint('module-loaded');
const repoDir = resolve(frontendDir, '..');
const argument = (name) => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
};
const port = Number(argument('--port') || 4198);
const expectedHead = argument('--expected-head');
const expectedTree = argument('--expected-tree');
const buildNonce = argument('--build-nonce');
const skipBuild = argument('--skip-build') === '1';
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error(`invalid preview port: ${port}`);
if (!/^[a-f0-9]{40}$/.test(expectedHead || '')) throw new Error('missing or invalid --expected-head');
if (!/^[a-f0-9]{40}$/.test(expectedTree || '')) throw new Error('missing or invalid --expected-tree');
if (!/^[a-f0-9]{32,128}$/.test(buildNonce || '')) throw new Error('missing or invalid --build-nonce');

checkpoint('arguments-validated');
const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
const tree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoDir, encoding: 'utf8' }).trim();
if (head !== expectedHead || tree !== expectedTree) {
  throw new Error(`git provenance changed before build (head=${head}, tree=${tree})`);
}
const dirty = execFileSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], {
  cwd: repoDir,
  encoding: 'utf8',
}).trim();
const allowDirty = process.env.PERF_ALLOW_DIRTY === '1';
if (dirty && !allowDirty) {
  throw new Error('performance production build requires a clean worktree');
}
if (skipBuild && !allowDirty) throw new Error('--skip-build is restricted to explicitly dirty development diagnostics');
checkpoint('pre-build-provenance-verified');
const loadedViteEnv = loadEnv('production', frontendDir, 'VITE_');
const effectiveViteEnv = Object.fromEntries(Object.keys({ ...loadedViteEnv, ...process.env })
  .filter(key => key.startsWith('VITE_'))
  .sort()
  .map(key => [key, String(process.env[key] ?? loadedViteEnv[key] ?? '')]));
const viteEnvKeys = Object.keys(effectiveViteEnv);
const viteEnvSha256 = createHash('sha256').update(JSON.stringify(effectiveViteEnv)).digest('hex');

if (!skipBuild) {
  const npmCli = resolve(dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  const build = spawnSync(process.execPath, [npmCli, 'run', 'build'], { cwd: frontendDir, stdio: 'inherit' });
  if (build.error) throw build.error;
  if (build.status !== 0) process.exit(build.status ?? 1);
}

checkpoint('build-complete');
const postBuildHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
const postBuildTree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoDir, encoding: 'utf8' }).trim();
const postBuildDirty = execFileSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: repoDir, encoding: 'utf8' }).trim();
if (postBuildHead !== head || postBuildTree !== tree || postBuildDirty !== dirty) {
  throw new Error('git provenance changed while the production bundle was being built');
}

checkpoint('post-build-provenance-verified');
const manifestPath = resolve(frontendDir, 'dist', '.vite', 'manifest.json');
const manifestText = readFileSync(manifestPath, 'utf8');
const manifest = JSON.parse(manifestText);
const entry = manifest['index.html'];
if (!entry?.isEntry || typeof entry.file !== 'string') {
  throw new Error('Vite manifest does not contain the index.html entry');
}
const entryBytes = readFileSync(resolve(frontendDir, 'dist', entry.file));
const entrySha256 = createHash('sha256').update(entryBytes).digest('hex');
const referencedFiles = new Set();
for (const item of Object.values(manifest)) {
  if (typeof item?.file === 'string') referencedFiles.add(item.file);
  for (const file of [...(item?.css || []), ...(item?.assets || [])]) referencedFiles.add(file);
}
const assetSha256 = Object.fromEntries([...referencedFiles].sort().map(file => [
  file,
  createHash('sha256').update(readFileSync(resolve(frontendDir, 'dist', file))).digest('hex'),
]));
checkpoint('manifest-assets-hashed');
const buildMarker = {
  gitHead: head,
  gitTree: tree,
  buildNonce,
  sourceClean: dirty.length === 0,
  preBuildStatusSha256: createHash('sha256').update(dirty).digest('hex'),
  postBuildStatusSha256: createHash('sha256').update(postBuildDirty).digest('hex'),
  viteEnvKeys,
  viteEnvSha256,
  assetSha256,
  buildSha256: createHash('sha256')
    .update(head)
    .update(tree)
    .update(buildNonce)
    .update(manifestText)
    .update(JSON.stringify(assetSha256))
    .update(viteEnvSha256)
    .digest('hex'),
  entrySha256,
  entryFile: entry.file,
  css: Array.isArray(entry.css) ? entry.css : [],
  manifestEntries: Object.keys(manifest).length,
};
writeFileSync(resolve(frontendDir, 'dist', 'performance-build.json'), `${JSON.stringify(buildMarker, null, 2)}\n`);

const liveProvenancePlugin = {
  name: 'omnirank-live-build-provenance',
  configurePreviewServer(previewServer) {
    previewServer.middlewares.use((req, res, next) => {
      if (new URL(req.url || '/', 'http://127.0.0.1').pathname !== '/__perf/provenance') {
        next();
        return;
      }
      const liveHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
      const liveTree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoDir, encoding: 'utf8' }).trim();
      const liveDirty = execFileSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: repoDir, encoding: 'utf8' }).trim();
      res.statusCode = 200;
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.setHeader('Cache-Control', 'no-store');
      res.end(JSON.stringify({ gitHead: liveHead, gitTree: liveTree, sourceClean: liveDirty.length === 0, statusSha256: createHash('sha256').update(liveDirty).digest('hex'), buildNonce }));
    });
  },
};

// Run Vite in this process. Killing the Playwright webServer parent therefore
// cannot orphan a second preview process or leave its port occupied.
checkpoint('starting-vite-preview');
const server = await preview({
  root: frontendDir,
  logLevel: 'info',
  plugins: [liveProvenancePlugin, createPerformanceFixturePlugin()],
  preview: { host: '127.0.0.1', port, strictPort: true },
});
checkpoint('vite-preview-listening');
let closing = false;
const close = async () => {
  if (closing) return;
  closing = true;
  await new Promise((resolveClose, rejectClose) => {
    server.httpServer.close(error => error ? rejectClose(error) : resolveClose());
  });
};
process.once('SIGINT', () => { void close(); });
process.once('SIGTERM', () => { void close(); });
await new Promise(resolveClose => server.httpServer.once('close', resolveClose));
