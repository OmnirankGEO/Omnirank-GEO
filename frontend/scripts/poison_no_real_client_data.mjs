#!/usr/bin/env node
/**
 * 给 `verify-no-real-client-data.mjs` 下毒 —— 没毒过的锁不能当证据。
 *
 * 🔴 两发**打在不同的层**,这是本单的要害:
 *   · P1 源文件:往 `mockData.ts` 塞一个白名单外的主体 ⇒ 源头那格必须红;
 *   · P2 **构建产物**:往 `dist/assets/*.js` 里注入同形串 ⇒ 终点那格必须红。
 *     P2 不能用改源码再 build 来做 —— 那样测的还是"源头能不能拦住",
 *     而 G3 的命题是「**不管怎么进来的**,发出去的东西里不许有」。
 *     所以直接在产物上下毒:任何绕过源头的路径(新的 `?raw`、新夹具、
 *     第三方包里带的),终点这一格都得看得见。
 *
 * 🔴 毒串一律用**虚构**名字。真实客户名不进这个文件 —— 它会进开源仓。
 *
 * 文件名不叫 `mutation_runner_*`:那个前缀是花名册闸的枚举范围,
 * 这一份不是被那道闸管的对象。
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATE = join(ROOT, 'scripts', 'verify-no-real-client-data.mjs');
const MOCK = join(ROOT, 'src', 'sandbox', 'mockData.ts');
const DIST = join(ROOT, 'dist', 'assets');

/** 虚构的「白名单外」主体 —— 形状命中,但不在白名单里。 */
const FAKE = '虚构未授权科技有限公司';

const run = () => {
    try {
        execFileSync(process.execPath, [GATE], { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
        return { rc: 0, out: '' };
    } catch (e) {
        return { rc: e.status === undefined ? -1 : e.status, out: String(e.stdout || '') + String(e.stderr || '') };
    }
};
/* 🔴 读数器不许被装饰绊倒:判据名可能以 emoji 开头,
   `(\S+)` 会把 emoji 当成 ID。跳过行首的非字母数字装饰再取。 */
const redIds = (out) => [...out.matchAll(/^\s*FAIL\s+[^A-Za-z0-9]*([A-Za-z0-9][^\s]*)/gm)].map((m) => m[1]);

console.log('=== 基线(必须全绿)===');
const base = run();
console.log(`  rc=${base.rc}`);
if (base.rc !== 0) {
    console.log('🔴 基线不绿,注毒结果不作数');
    console.log(base.out.split('\n').filter((l) => l.includes('FAIL')).join('\n'));
    process.exit(3);
}

let surprises = 0;
const report = (id, why, want, r) => {
    const reds = redIds(r.out);
    const hit = r.rc === 1 && reds.some((x) => x.startsWith(want));
    if (!hit) surprises += 1;
    console.log(`\n${id} ${why}`);
    console.log(`   rc=${r.rc} · ${hit ? `被 ${reds.filter((x) => x.startsWith(want)).join(',')} 抓住`
        : `🔴 没被预期那一格(${want})抓住 —— 红的是 ${reds.join(',') || '(无)'}`}`);
};

/* ── P1 源文件 ────────────────────────────────────────────────────── */
{
    const original = readFileSync(MOCK, 'utf8');
    try {
        writeFileSync(MOCK, `${original}\nexport const __poison = '${FAKE}';\n`, 'utf8');
        report('P1', `源文件:往 mockData.ts 塞一个白名单外的主体(${FAKE})`, 'W256-1b', run());
    } finally {
        writeFileSync(MOCK, original, 'utf8');
    }
}

/* ── P2 构建产物(终点判据自己的牙)───────────────────────────────── */
{
    let target = null;
    try {
        const js = readdirSync(DIST).filter((f) => f.endsWith('.js'));
        if (js.length === 0) throw new Error('dist/assets 里没有 js');
        target = join(DIST, js.sort()[0]);
    } catch (e) {
        console.log(`\n3 没跑成:拿不到构建产物 —— ${e.message}`);
        console.log('   (P2 打的就是产物这一层,没有产物时**不能**当它通过)');
        process.exit(3);
    }
    const original = readFileSync(target, 'utf8');
    try {
        writeFileSync(target, `${original}\n/* ${FAKE} */\n`, 'utf8');
        report('P2', '🔴 构建产物:往一个 chunk 注入同形串(绕过源头的路径,终点必须看得见)',
            'W256-3b', run());
    } finally {
        writeFileSync(target, original, 'utf8');
    }
}

console.log('\n=== 复原自证 ===');
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
process.exit(surprises === 0 ? 0 : 1);
