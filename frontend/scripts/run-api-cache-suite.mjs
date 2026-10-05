import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { randomBytes, randomInt } from 'node:crypto';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = resolve(frontendDir, '..');
const value = name => {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
};
const port = Number(value('--port') || randomInt(44_000, 49_000));
const grep = value('--grep');
const nonce = value('--nonce') || `${Date.now()}-${randomBytes(6).toString('hex')}`;
const outcomeFile = resolve(frontendDir, 'output', 'api-cache', `${nonce}-outcome.json`);
if (existsSync(outcomeFile)) throw new Error(`immutable outcome already exists: ${outcomeFile}`);
mkdirSync(dirname(outcomeFile), { recursive: true });
const gitHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
const env = { ...process.env, API_CACHE_PORT: String(port), API_CACHE_EXTERNAL_SERVER: '1' };
let server;
let testExitCode = null;
let cleanupVerified = false;

async function waitForServer(child, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode != null) throw new Error(`vite exited before ready: ${child.exitCode}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/login`, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch {}
    await new Promise(resolveWait => setTimeout(resolveWait, 200));
  }
  throw new Error(`vite readiness timeout on ${port}`);
}

async function stopTree(child) {
  if (!child) return;
  if (child.exitCode == null) child.kill('SIGTERM');
  const deadline = Date.now() + 4000;
  while (child.exitCode == null && Date.now() < deadline) {
    await new Promise(resolveWait => setTimeout(resolveWait, 100));
  }
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  } else if (child.exitCode == null) {
    child.kill('SIGKILL');
  }
}

try {
  console.error(`[api-cache-runner] start port=${port}`);
  const viteCli = resolve(frontendDir, 'node_modules', 'vite', 'bin', 'vite.js');
  server = spawn(process.execPath, [viteCli, '--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
    cwd: frontendDir, env, stdio: 'inherit', windowsHide: true,
  });
  await waitForServer(server);
  const playwrightCli = resolve(frontendDir, 'node_modules', 'playwright', 'cli.js');
  const args = [
    playwrightCli, 'test', 'tests/api-cache.spec.ts',
    '--config', 'playwright.api-cache.config.ts', '--reporter=line',
  ];
  if (grep) args.push('--grep', grep);
  const tests = spawn(process.execPath, args, {
    cwd: frontendDir, env, stdio: 'inherit', windowsHide: true,
  });
  testExitCode = await new Promise((resolveExit, rejectExit) => {
    tests.once('error', rejectExit);
    tests.once('exit', code => resolveExit(code ?? 1));
  });
  if (testExitCode !== 0) process.exitCode = testExitCode;
} catch (error) {
  console.error(error);
  testExitCode = testExitCode ?? 1;
  process.exitCode = 1;
} finally {
  await stopTree(server);
  try {
    await fetch(`http://127.0.0.1:${port}/login`, { signal: AbortSignal.timeout(500) });
  } catch {
    cleanupVerified = true;
  }
  writeFileSync(outcomeFile, `${JSON.stringify({
    gitHead, port, grep: grep || null, testExitCode, cleanupVerified,
    finishedAt: new Date().toISOString(),
  }, null, 2)}\n`, { flag: 'wx' });
  console.error(`[api-cache-runner] exit=${testExitCode} cleanup=${cleanupVerified} outcome=${outcomeFile}`);
  if (!cleanupVerified) process.exitCode = 1;
}
