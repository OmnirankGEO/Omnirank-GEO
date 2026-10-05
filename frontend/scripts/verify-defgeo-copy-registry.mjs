/**
 * 防御型 GEO 前端文案漂移门 —— 生成物必须与后端 registry **逐字节**一致。
 *
 * 生成器:``scripts/defgeo_census/emit_frontend_copy.py``(SSOT 侧)
 * 生成物:``frontend/src/lib/defensiveGeoCopy.ts``
 *
 * 🔴 [工单 V5-C · C-4 · Codex fix-of-fix2 P3-1] 这个文件以前**不存在**。
 *    生成器抬头和它生成的 TS 抬头都点名引用了它、还写错了它挂在哪一道链上,
 *    而仓库里根本没有这个文件 —— 于是「漂了就红」这句话一直是空的,
 *    只有后端 pytest 那一条在守。引用一道不存在的门,比没有门更糟:
 *    大家以为有人在看。
 *
 * 🔴 [工单 V5-C2 · Codex fix-of-fix3 P3-NEW-2] 上一句里那个**写错的归属**
 *    当时没跟着改,于是三份文案继续指着一条它并不在的链。
 *    现在三处统一按下面这一段的事实写,并由
 *    ``test_the_verifier_gates_are_exactly_the_wiring_we_claim`` 拿
 *    ``package.json`` 当唯一事实源钉住 —— 接线改了,文案必须同笔改。
 *
 * 🔴 为什么**不进** ``npm run build``
 *    ``Dockerfile`` 的 frontend-builder 是 ``node:20-alpine``,没有 python,
 *    而且那一层只 ``COPY frontend/`` —— 后端 registry 根本不在上下文里。
 *    把它接进 build 就是让生产镜像构建必然失败。所以按本仓既有惯例
 *    (``verify:publish-layout-interaction`` / ``verify:silent-reload`` 同款),
 *    它接在 ``npm run lint`` 前置 + 独立 ``npm run verify:defgeo-copy``。
 *
 * 🔴 **失败一律非零**,包括"python 找不到"。
 *    工具没跑成而退 0,和"跑了且通过"在 CI 上长得一模一样 ——
 *    本仓为这个形态记过不止一次(tsc 缺二进制 rc=0 的那次)。
 *
 * 用法::
 *
 *     node frontend/scripts/verify-defgeo-copy-registry.mjs
 *     node frontend/scripts/verify-defgeo-copy-registry.mjs --ts <路径>   # 判据注毒用
 */

import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repoDir = resolve(here, '..', '..');
const GENERATOR = join('scripts', 'defgeo_census', 'emit_frontend_copy.py');
const DEFAULT_TS = join(repoDir, 'frontend', 'src', 'lib', 'defensiveGeoCopy.ts');

const argOf = (name) => {
    const i = process.argv.indexOf(name);
    return i >= 0 ? process.argv[i + 1] : undefined;
};

/**
 * 🔴 [工单 V5-C2 · Codex fix-of-fix3 P3-NEW-2] 失败**抛异常**,不 `process.exit()`。
 *
 * `process.exit()` 立即终止进程,**不跑 finally** —— 而临时目录的清理就在
 * 下面那个 finally 里。于是每走一次失败臂,`os.tmpdir()` 就多一个
 * `defgeo-copy-*` 留在盘上。这道门天生是"该红就红"的,所以泄漏会
 * **随着它尽职工作而累积**:越好用漏得越多。
 *
 * 现在:抛 → 外层 catch 打印并把 `process.exitCode` 设成同一个码 →
 * finally 照常清理 → 进程自然退出并带走那个码。退出码语义逐值不变。
 */
class GateFailure extends Error {
    constructor(code, message) {
        super(message);
        this.name = 'GateFailure';
        this.code = code;
    }
}

const fail = (code, message) => {
    throw new GateFailure(code, message);
};

// ── ① 找一个能跑生成器的解释器 ────────────────────────────────────────────
// 顺序:显式 PYTHON → python3 → python → py -3。全找不到 = 非零退出,
// 不是"跳过"。
const candidates = [];
if (process.env.PYTHON) candidates.push([process.env.PYTHON, []]);
candidates.push(['python3', []], ['python', []], ['py', ['-3']]);

const outDir = mkdtempSync(join(tmpdir(), 'defgeo-copy-'));
const outFile = join(outDir, 'defensiveGeoCopy.ts');
let generated = null;
const tried = [];
try {
    for (const [exe, prefix] of candidates) {
        const proc = spawnSync(exe, [...prefix, GENERATOR, '--out', outFile], {
            cwd: repoDir,
            encoding: 'buffer',
        });
        if (proc.error) {
            tried.push(`${exe}: ${proc.error.code || proc.error.message}`);
            continue;
        }
        if (proc.status !== 0) {
            const detail = Buffer.concat([
                proc.stdout || Buffer.alloc(0),
                proc.stderr || Buffer.alloc(0),
            ]).toString('utf8');
            fail(2, `生成器非零退出(${exe}, rc=${proc.status}):\n${detail}`);
        }
        try {
            generated = readFileSync(outFile);
        } catch (err) {
            fail(2, `生成器报成功却没写出文件(${exe}):${err.message}`);
        }
        break;
    }
    if (generated === null) {
        fail(
            2,
            '找不到可用的 python 解释器,这道门没有跑成。\n' +
                `试过:${tried.join(' | ')}\n` +
                '🔴 这里**不退 0**:工具没跑成而报绿,与"跑了且通过"在 CI 上分不开。\n' +
                '设 PYTHON=<解释器路径> 再跑。',
        );
    }

    // ── ② 逐字节比对 ────────────────────────────────────────────────────
    const tsPath = argOf('--ts') || DEFAULT_TS;
    let onDisk;
    try {
        onDisk = readFileSync(tsPath);
    } catch (err) {
        fail(2, `读不到生成物 ${tsPath}:${err.message}`);
    }

    if (!onDisk.equals(generated)) {
        let where = '(长度不同)';
        const n = Math.min(onDisk.length, generated.length);
        for (let i = 0; i < n; i += 1) {
            if (onDisk[i] !== generated[i]) {
                where = `第 ${i} 字节:盘上 0x${onDisk[i].toString(16)} / ` +
                    `应为 0x${generated[i].toString(16)}`;
                break;
            }
        }
        fail(
            1,
            `${tsPath} 与后端 copy registry 漂了。\n` +
                `盘上 ${onDisk.length} 字节 / 生成 ${generated.length} 字节;${where}\n` +
                '🔴 这份文件是**生成物**:改文案去改 services/defensive_geo/copy_registry.py,\n' +
                '   然后重新跑 python scripts/defgeo_census/emit_frontend_copy.py。',
        );
    }

    process.stdout.write(
        `[defgeo-copy] OK ${onDisk.length} 字节与后端 registry 逐字节一致\n`,
    );
} catch (err) {
    // 🔴 只接管本门自己的失败。别的异常(真 bug)原样抛出去 ——
    //    把它们也折成一个整齐的退出码,等于把崩溃伪装成"判红"。
    if (!(err instanceof GateFailure)) throw err;
    process.stderr.write(`[defgeo-copy] ${err.message}\n`);
    process.exitCode = err.code;
} finally {
    rmSync(outDir, { recursive: true, force: true });
}
