#!/usr/bin/env node
/**
 * 给 `verify-poison-runner-guard.mjs` 下毒 —— 没毒过的锁不能当证据。
 *
 * 🔴 文件名故意**不**叫 `mutation_runner_*`:那道闸的花名册正是按这个前缀枚举的,
 *    叫那个名字会把自己算进分母,而它不是被测对象、是测试器。
 *
 * 每一发都对应闸里的一格,包括那格**花名册锁** ——
 * 从清单里删一行就该红,否则「分母悄悄缩水」这件事没人看得住。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATE = join(ROOT, 'scripts', 'verify-poison-runner-guard.mjs');
const LIB = join(ROOT, 'scripts', 'lib', 'poison-syntax-guard.mjs');
const R251 = join(ROOT, 'scripts', 'mutation_runner_251.mjs');

const POISONS = [
    {
        id: 'G-1', file: GATE, why: '从花名册里删掉一行 ⇒ 分母悄悄缩水',
        from: "    'mutation_runner_180b.mjs', ", to: '    ',
        expect: 'G1a',
    },
    {
        id: 'G-2', file: GATE, why: '🔴 把一个**新** runner 停进待接区(棘轮被绕开)',
        /* 🔴 锚钉在**数组声明行**,不钉最后一个元素。
              上一版锚是 `'…silent_reload_outlets.mjs',\n];`(即「表尾 + 收口」)——
              2026-09-20 花名册补进 243 之后,那个锚**当场失效**,这一发变成「没下成」。
              表的**最后一个元素是它最容易变的部位**,拿它当锚等于把锚绑在活动件上。
              `const PENDING = [` 是声明行:唯一(冻结表叫 `PENDING_FROZEN_…`,
              不含 `const PENDING = [`)、且不随表长变化 ⇒ 也不再需要 `once`。 */
        from: 'const PENDING = [\n',
        to: "const PENDING = [\n    'mutation_runner_218.mjs',\n",
        expect: 'G1d',
        note: '第一版棘轮是 `const CEILING = 37`,而「把 37 改成 99」**一格都不红** —— '
            + '一个和它守的东西放在同一处、谁都能改大的数不是棘轮。现在改成冻结名单:'
            + '不在那张冻结表里的,停不进来。',
    },
    {
        id: 'G-3', file: R251, why: '拆掉一个已接 runner 的**对照臂**',
        from: '    try { assertRulerWorks(p.file); } catch (e) {', to: '    if (false) { const e = { message: 0 };',
        expect: 'G2-control',
    },
    {
        id: 'G-4', file: R251, why: '拆掉那一发牙证(前置从此没东西证明它会红)',
        from: '        expectSyntaxFail: true,', to: '        expectSyntaxFailDISABLED: true,',
        expect: 'G2-tooth',
        note: '🔴 这一发第一轮**存活**:那一格写的是 includes("expectSyntaxFail"),'
            + '而 `expectSyntaxFailDISABLED` 把它满足了 —— 「某串出现过」被无关 token 满足。'
            + '锚改成钉住「这个键真的是 true」之后才咬得住。',
    },
    {
        id: 'G-5', file: LIB, why: '🔴 尺子毒成**恒 false**(第一版的真实病:22 发全被报成「没下成」)',
        from: '    return (out.diagnostics || []).filter((d) => d.file);',
        to: "    return [{ code: 9999, messageText: 'poisoned', file: {} }];",
        expect: 'G3a',
        note: '牙证对这一发**天生瞎**(一把说全世界都是语法错的尺子,牙证满分)—— '
            + '所以 G3a/G3e 那两格(好文件必须绿)才是真正看得见它的。',
    },
    {
        id: 'G-6', file: LIB, why: '尺子毒成**恒 true**(反方向:什么都判得过)',
        from: '    return (out.diagnostics || []).filter((d) => d.file);',
        to: '    return [];',
        expect: 'G3b',
    },
];

const run = () => {
    try {
        const out = execFileSync(process.execPath, [GATE],
            { cwd: ROOT, encoding: 'utf8', timeout: 300000, stdio: ['ignore', 'pipe', 'pipe'] });
        return { rc: 0, out };
    } catch (e) {
        return { rc: e.status === undefined ? -1 : e.status, out: String(e.stdout || '') + String(e.stderr || '') };
    }
};
const redIds = (out) => [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]);

console.log('=== 基线(必须全绿)===');
const base = run();
console.log(`  rc=${base.rc}`);
if (base.rc !== 0) {
    console.log('🔴 基线不绿,注毒结果不作数');
    console.log(base.out);
    process.exit(3);
}

let surprises = 0;
for (const p of POISONS) {
    const original = readFileSync(p.file, 'utf8');
    /* 对照臂:没动过的文件必须判得过。 */
    try { assertRulerWorks(p.file); } catch (e) {
        console.log(`\n${p.id} ${e.message}`);
        process.exit(3);
    }
    const n = original.split(p.from).length - 1;
    if (n === 0) { console.log(`\n${p.id} 🔴 NC:锚没命中,这一发**没下成**`); surprises += 1; continue; }
    writeFileSync(p.file, p.once ? original.replace(p.from, p.to) : original.split(p.from).join(p.to), 'utf8');
    try {
        if (!syntaxOk(p.file)) {
            console.log(`\n${p.id} 🔴 毒把文件写成了语法错 ⇒ 这一发没下成,不算 CAUGHT`);
            surprises += 1;
            continue;
        }
        const r = run();
        const reds = redIds(r.out);
        /* 🔴 这里**不**只看 rc:被毒的是闸自己,它崩掉也会非 0。
              要求「红的正是预期那一格」—— rc=3(闸没跑成)一律算没下成。 */
        const hit = r.rc === 1 && reds.some((x) => x.startsWith(p.expect));
        if (!hit) surprises += 1;
        console.log(`\n${p.id} ${p.why}`);
        console.log(`   NC 命中 ${n} 处 · rc=${r.rc} · ${hit ? `被 ${reds.filter((x) => x.startsWith(p.expect)).join(',')} 抓住`
            : `🔴 没被预期那一格(${p.expect})抓住 —— 红的是 ${reds.join(',') || '(无)'}`}`);
        if (p.note) console.log(`   ${p.note}`);
    } finally {
        writeFileSync(p.file, original, 'utf8');
    }
}

console.log('\n=== 复原自证 ===');
try {
    console.log(execFileSync('git', ['status', '--porcelain',
        'scripts/verify-poison-runner-guard.mjs', 'scripts/lib/poison-syntax-guard.mjs',
        'scripts/mutation_runner_251.mjs'],
    { cwd: ROOT, encoding: 'utf8' }).trim() || '(三个文件都回到下毒前 · 注:新文件未 add 时这里会显示 ??)');
} catch { console.log('(git 读不到)'); }
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
process.exit(surprises === 0 ? 0 : 1);
