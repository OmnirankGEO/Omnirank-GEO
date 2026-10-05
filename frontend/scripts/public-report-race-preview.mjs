/** Build the visual race harness, then serve the production output for Playwright. */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const viteCli = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url));

function run(args) {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [viteCli, ...args], { stdio: 'inherit', shell: false });
        child.once('error', reject);
        child.once('exit', (code) => {
            if (code === 0) resolve();
            else reject(new Error(`command failed with exit code ${code ?? 'unknown'}`));
        });
    });
}

await run(['build', '--config', 'vite.config.race.ts']);

const preview = spawn(
    process.execPath,
    [viteCli, 'preview', '--config', 'vite.config.race.ts', '--host', '127.0.0.1', '--port', '4199'],
    { stdio: 'inherit', shell: false },
);

const stop = () => {
    if (!preview.killed) preview.kill();
};
process.once('SIGINT', stop);
process.once('SIGTERM', stop);
preview.once('exit', (code) => process.exit(code ?? 0));
preview.once('error', (error) => {
    console.error(error);
    process.exit(1);
});
