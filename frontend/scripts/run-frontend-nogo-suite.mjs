import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { randomBytes, randomInt } from 'node:crypto';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = resolve(frontendDir, '..');
const value = (name) => {
    const index = process.argv.indexOf(name);
    return index >= 0 ? process.argv[index + 1] : undefined;
};
const spec = value('--spec');
const grep = value('--grep');
if (!spec) throw new Error('--spec is required');
const port = Number(value('--port') || randomInt(44_000, 49_000));
const nonce = value('--nonce') || `${Date.now()}-${randomBytes(6).toString('hex')}`;
const outcomeFile = resolve(
    frontendDir,
    'output',
    'frontend-nogo',
    `${nonce}-outcome.json`,
);
if (existsSync(outcomeFile)) throw new Error(`immutable outcome already exists: ${outcomeFile}`);
mkdirSync(dirname(outcomeFile), { recursive: true });

const gitHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoDir, encoding: 'utf8' }).trim();
const env = {
    ...process.env,
    QA_NOGO_PORT: String(port),
    QA_NOGO_EXTERNAL_SERVER: '1',
    QA_NOGO_OUTPUT_DIR: resolve(frontendDir, 'output', 'frontend-nogo', nonce),
};
let server;
let testExitCode = null;
let cleanupVerified = false;

async function waitForServer(child, timeoutMs = 120_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (child.exitCode != null) throw new Error(`vite exited before ready: ${child.exitCode}`);
        try {
            const response = await fetch(`http://127.0.0.1:${port}/login`, {
                signal: AbortSignal.timeout(1000),
            });
            if (response.ok) return;
        } catch {}
        await new Promise((resolveWait) => setTimeout(resolveWait, 200));
    }
    throw new Error(`vite readiness timeout on ${port}`);
}

async function stopTree(child) {
    if (!child) return;
    if (child.exitCode == null) child.kill('SIGTERM');
    const deadline = Date.now() + 4000;
    while (child.exitCode == null && Date.now() < deadline) {
        await new Promise((resolveWait) => setTimeout(resolveWait, 100));
    }
    if (process.platform === 'win32') {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } else if (child.exitCode == null) {
        child.kill('SIGKILL');
    }
}

try {
    console.error(`[nogo-runner] start port=${port} spec=${spec}`);
    const viteCli = resolve(frontendDir, 'node_modules', 'vite', 'bin', 'vite.js');
    server = spawn(process.execPath, [viteCli, '--host', '127.0.0.1', '--port', String(port)], {
        cwd: frontendDir,
        env,
        stdio: 'inherit',
        windowsHide: true,
    });
    await waitForServer(server);
    const playwrightCli = resolve(frontendDir, 'node_modules', 'playwright', 'cli.js');
    const testArgs = [
        playwrightCli,
        'test',
        spec,
        '--config',
        'playwright.frontend-nogo.config.ts',
        '--reporter=line',
    ];
    if (grep) testArgs.push('--grep', grep);
    const tests = spawn(process.execPath, testArgs, {
        cwd: frontendDir,
        env,
        stdio: 'inherit',
        windowsHide: true,
    });
    testExitCode = await new Promise((resolveExit, rejectExit) => {
        tests.once('error', rejectExit);
        tests.once('exit', (code) => resolveExit(code ?? 1));
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
    const outcome = {
        gitHead,
        spec,
        grep: grep || null,
        port,
        nonce,
        testExitCode,
        cleanupVerified,
        finishedAt: new Date().toISOString(),
    };
    writeFileSync(outcomeFile, `${JSON.stringify(outcome, null, 2)}\n`, { flag: 'wx' });
    console.error(`[nogo-runner] exit=${testExitCode} cleanup=${cleanupVerified} outcome=${outcomeFile}`);
    if (!cleanupVerified) process.exitCode = 1;
}
