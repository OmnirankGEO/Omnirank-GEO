#!/usr/bin/env node
/** WO_251 §4 注毒 —— 毒来自交付抬头承诺的每一样(现值优先 / 三种空 / 缺字段≠null / 一处口径 / 沙盒)。 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const LIB = join(ROOT, 'src/lib/clientDisplay.ts');
const MON = join(ROOT, 'src/pages/Monitoring/index.tsx');
const MOCK = join(ROOT, 'src/sandbox/mockData.ts');

const POISONS = [
    {
        id: 'Q1', file: LIB, why: '🔴 撤掉「键在不在」这道闸 ⇒ 后端明说 null 时也回落快照',
        from: "    if (!hasKey(row, 'brand_current_name') && !blank(row.brand_name)) return (row.brand_name as string).trim();",
        to: "    if (!blank(row.brand_name)) return (row.brand_name as string).trim();",
        expect: 'D3c',
        note: '后果:后端明说"两级都没有"时,前端还去显示那个**过期的**快照名 —— '
            + '正是本单要治的病,只是换了个触发条件。',
    },
    {
        id: 'Q2', file: LIB, why: '反过来:键不存在时也不回落 ⇒ 演示态从"旧名"变成"品牌 #id"',
        from: "    if (!hasKey(row, 'brand_current_name') && !blank(row.brand_name)) return (row.brand_name as string).trim();",
        to: '',
        expect: 'D3',
    },
    {
        id: 'Q8', file: LIB, why: '🔴 键判退回**值判**(`hasOwnProperty` → `=== undefined`)',
        from: "    if (!hasKey(row, 'brand_current_name') && !blank(row.brand_name)) return (row.brand_name as string).trim();",
        to: "    if (row.brand_current_name === undefined && !blank(row.brand_name)) return (row.brand_name as string).trim();",
        expect: 'D3d',
        note: '🔴 对 JSON 负载两种写法**结果相同** ⇒ 这次改动本身一开始**没有任何判据看得见**,'
            + '直到补了「键在、值是 undefined」那一格(D3d)。冗余不等于可以不验。',
    },
    {
        id: 'Q3', file: LIB, why: '「空」只判 null,不判空串/仅空白',
        from: "const blank = (v: unknown): boolean => typeof v !== 'string' || v.trim() === '';",
        to: 'const blank = (v: unknown): boolean => v === null || v === undefined;',
        expect: 'D2',
    },
    {
        id: 'Q4', file: LIB, why: '名字默认值直接给「未命名客户」,不给 `品牌 #<id>`',
        from: "    if (id !== null && id !== undefined && String(id).trim() !== '') return `品牌 #${String(id).trim()}`;",
        to: '',
        expect: 'D2',
    },
    {
        id: 'Q5', file: LIB, why: '现值优先被改成快照优先',
        from: "    if (!blank(row.brand_current_name)) return (row.brand_current_name as string).trim();",
        to: "    if (!blank(row.brand_name)) return (row.brand_name as string).trim();",
        expect: 'D1',
    },
    {
        id: 'Q6', file: MON, why: '监测页改回**直接渲染快照字段**(口径又变回两份)',
        from: '{clientDisplayName(selectedClientInfo)}',
        to: '{selectedClientInfo.brand_name}',
        expect: 'D5',
    },
    {
        id: 'Q7', file: MOCK, why: '沙盒夹具去掉现值两列 ⇒ 教程只走回落那一支',
        from: '            brand_current_name: DEMO_BRAND_NAME,\n            brand_current_industry: DEMO_INDUSTRY,\n',
        to: '',
        expect: 'D6',
    },
    {
        id: 'QSYN', file: LIB, why: '🔴 **故意写坏语法** —— 这一发是那道语法前置自己的牙证',
        from: "const blank = (v: unknown): boolean => typeof v !== 'string' || v.trim() === '';",
        to: "const blank = (v: unknown): boolean => typeof v !== 'string' || v.trim() === ;",
        expectSyntaxFail: true,
        note: '不加前置的话,它会让门 rc=1,被读成「锁咬住了」—— 而实际上**什么都没测到**。C 2026-09-20 就栽在这里。',
    },
];

const run = () => {
    try {
        const out = execFileSync(process.execPath, [join(ROOT, 'scripts', 'verify-client-display.mjs')],
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
    console.log(base.out.split('\n').filter((l) => l.includes('FAIL')).join('\n'));
    process.exit(3);
}

let surprises = 0;
for (const p of POISONS) {
    const original = readFileSync(p.file, 'utf8');
    /* 🔴 对照臂:**没动过的文件**必须判得过 —— 恒 false 的尺子会把每一发都报成「没下成」。 */
    try { assertRulerWorks(p.file); } catch (e) {
        console.log(`\n${p.id} ${e.message}`);
        process.exit(3);
    }
    const n = original.split(p.from).length - 1;
    if (n === 0) { console.log(`\n${p.id} 🔴 NC:锚没命中,这一发**没下成**(不是"有牙")`); surprises += 1; continue; }
    writeFileSync(p.file, original.split(p.from).join(p.to), 'utf8');
    try {
        /* 🔴 下毒后先验语法:「毒没下成」与「锁咬住了」在 rc 上完全同形。 */
        const parses = syntaxOk(p.file);
        if (p.expectSyntaxFail) {
            /* 这一发就是来验这道前置的:它**必须**被前置拦下,不该走到门那一步。 */
            if (parses) surprises += 1;
            console.log(`\n${p.id} ${p.why}`);
            console.log(`   NC 命中 ${n} 处 · ${parses ? '🔴 前置没拦住 —— 那道前置是瞎的' : '✅ 被**语法前置**拦下(未计入 CAUGHT)'}`);
            if (p.note) console.log(`   ${p.note}`);
            continue;
        }
        if (!parses) {
            console.log(`\n${p.id} ${NOT_LANDED_SYNTAX}`);
            surprises += 1;
            continue;
        }
        const r = run();
        const reds = redIds(r.out);
        const caught = r.rc === 1;
        if (!caught) surprises += 1;
        console.log(`\n${p.id} ${p.why}`);
        console.log(`   NC 命中 ${n} 处 · ${caught ? `被 ${reds.join(',') || 'rc=1'} 抓住` : `🔴 存活(rc=${r.rc})`}`);
        if (caught && p.expect && !reds.some((x) => x.startsWith(p.expect))) {
            console.log(`   ⚠️ 红的不是预期那一格(预期 ${p.expect})—— 结论对,理由可能是编的`);
        }
        if (p.note) console.log(`   ${p.note}`);
    } finally {
        writeFileSync(p.file, original, 'utf8');
    }
}

console.log('\n=== 复原自证 ===');
try {
    console.log(execFileSync('git', ['status', '--porcelain',
        'src/lib/clientDisplay.ts', 'src/pages/Monitoring/index.tsx', 'src/sandbox/mockData.ts'],
    { cwd: ROOT, encoding: 'utf8' }).trim() || '(三个文件都回到下毒前)');
} catch { console.log('(git 读不到)'); }
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
