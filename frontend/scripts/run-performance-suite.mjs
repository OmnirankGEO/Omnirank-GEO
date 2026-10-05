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
const has = name => process.argv.includes(name);
const suite = value('--suite') || 'site';
const formal = has('--formal');
const skipBuild = has('--skip-build');
if (!['site', 'failure'].includes(suite)) throw new Error(`unsupported --suite ${suite}`);
if (formal && skipBuild) throw new Error('formal evidence may never skip the current-HEAD production build');
const spec = suite === 'site' ? 'tests/performance/site-performance.spec.ts' : 'tests/performance/failure-modes.spec.ts';
const gitHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
const gitTree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoDir, encoding: 'utf8' }).trim();
const dirty = execFileSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: repoDir, encoding: 'utf8' }).trim();
if (formal && dirty) throw new Error('formal performance command requires a clean worktree before the build');
const port = Number(value('--port') || randomInt(44_000, 49_000));
const nonce = value('--nonce') || `${new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14)}-${randomBytes(8).toString('hex')}`;
const buildNonce = randomBytes(24).toString('hex');
const suiteName = formal ? `${suite}-formal` : `${suite}-diagnostic`;
const reportFile = resolve(frontendDir, 'output', 'playwright-performance', 'reports', `${gitHead}-${nonce}-${suiteName}.json`);
const outcomeFile = resolve(frontendDir, 'output', 'playwright-performance', 'reports', `${gitHead}-${nonce}-${suiteName}-outcome.json`);
if (existsSync(reportFile) || existsSync(outcomeFile)) throw new Error(`immutable report nonce already exists: ${nonce}`);
mkdirSync(dirname(reportFile), { recursive: true });
const inherited = { ...process.env };
const env = {
  ...inherited,
  PERF_PORT: String(port),
  PERF_BUILD_NONCE: buildNonce,
  PERF_REPORT_NONCE: nonce,
  PERF_REPORT_FILE: reportFile,
  PERF_SUITE: suiteName,
  PERF_EXTERNAL_SERVER: '1',
  PERF_COMMAND: `node scripts/run-performance-suite.mjs --suite ${suite}${formal ? ' --formal' : ''}${skipBuild ? ' --skip-build' : ''}`,
  PERF_MATRIX: formal ? '1' : (inherited.PERF_MATRIX || '0'),
  PERF_RUNS: formal ? '10' : (inherited.PERF_RUNS || '1'),
  PERF_WORKERS: formal ? '4' : (inherited.PERF_WORKERS || '1'),
  PERF_TEST_TIMEOUT_MS: formal ? '240000' : (inherited.PERF_TEST_TIMEOUT_MS || '180000'),
};
if (!formal) env.PERF_ALLOW_DIRTY = '1';
const previewArgs = [
  'scripts/performance-preview.mjs',
  '--port', String(port),
  '--expected-head', gitHead,
  '--expected-tree', gitTree,
  '--build-nonce', buildNonce,
];
if (skipBuild) previewArgs.push('--skip-build', '1');
let preview;
let testExitCode = null;
let cleanupVerified = false;
const startedAt = new Date().toISOString();

async function waitForServer(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode != null) throw new Error(`preview exited before readiness with ${child.exitCode}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/login`, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch {}
    await new Promise(resolveWait => setTimeout(resolveWait, 250));
  }
  throw new Error(`preview readiness timeout on exclusive port ${port}`);
}

async function stopTree(child) {
  if (!child || child.exitCode != null) return;
  child.kill('SIGTERM');
  const deadline = Date.now() + 5000;
  while (child.exitCode == null && Date.now() < deadline) await new Promise(resolveWait => setTimeout(resolveWait, 100));
  if (child.exitCode == null && process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  } else if (child.exitCode == null) {
    child.kill('SIGKILL');
  }
}

try {
  console.error(`[perf-runner] starting exclusive preview port=${port} head=${gitHead} suite=${suiteName}`);
  preview = spawn(process.execPath, previewArgs, { cwd: frontendDir, env, stdio: 'inherit', windowsHide: true });
  await waitForServer(preview, formal ? 900_000 : 360_000);
  console.error(`[perf-runner] preview ready port=${port}`);
  const playwrightCli = resolve(frontendDir, 'node_modules', 'playwright', 'cli.js');
  const forwarded = process.argv.slice(process.argv.indexOf('--') + 1);
  const testArgs = [playwrightCli, 'test', spec, '--config', 'playwright.performance.config.ts'];
  if (process.argv.includes('--')) testArgs.push(...forwarded);
  const tests = spawn(process.execPath, testArgs, { cwd: frontendDir, env, stdio: 'inherit', windowsHide: true });
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
  await stopTree(preview);
  try {
    await fetch(`http://127.0.0.1:${port}/login`, { signal: AbortSignal.timeout(500) });
  } catch {
    cleanupVerified = true;
  }
  const finalHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
  const finalTree = execFileSync('git', ['rev-parse', 'HEAD^{tree}'], { cwd: repoDir, encoding: 'utf8' }).trim();
  const finalDirty = execFileSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: repoDir, encoding: 'utf8' }).trim();
  const outcome = {
    suite: suiteName,
    formal,
    gitHead,
    gitTree,
    nonce,
    port,
    reportFile,
    startedAt,
    finishedAt: new Date().toISOString(),
    testExitCode,
    cleanupVerified,
    finalHead,
    finalTree,
    sourceStable: finalHead === gitHead && finalTree === gitTree && finalDirty === dirty,
    sourceClean: finalDirty.length === 0,
  };
  writeFileSync(outcomeFile, `${JSON.stringify(outcome, null, 2)}\n`, { flag: 'wx' });
  console.error(`[perf-runner] exit=${testExitCode} cleanup=${cleanupVerified} outcome=${outcomeFile}`);
  if (!cleanupVerified || !outcome.sourceStable || (formal && !outcome.sourceClean)) process.exitCode = 1;
}
