#!/usr/bin/env node
/**
 * WO_250 注毒 —— 证明行业分类那道门有牙。
 *
 * 🔴 毒从**交付抬头承诺的每一样**来:①酒店不再是餐饮 ②最长优先(治类)
 *    ③不留单字 ④一处定义 ⑤差分声明表两个方向都咬。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const LIB = join(ROOT, 'src/lib/industries.ts');
const OQF = join(ROOT, 'src/pages/Quote/OnlineQuoteFlow.tsx');

const POISONS = [
    {
        id: 'N1', file: LIB, why: '把单字 `酒` 加回餐饮食品(原缺陷的字面复现)',
        from: "keywords: ['餐饮', '食品', '饮料', '酒业'",
        to: "keywords: ['餐饮', '食品', '饮料', '酒', '酒业'",
        expect: 'I3',
    },
    {
        id: 'N2', file: LIB, why: '🔴 退回**表顺序优先**(最长优先是本单治类的那一条)',
        from: '        .sort((a, b) => b.kw.length - a.kw.length);',
        to: '        .slice();',
        expect: 'I8',
        note: '单独看它像"只是换个排序",实际把「更具体的词」重新交给表顺序裁决 —— '
            + '正是「酒 截 酒店」的成因。',
    },
    {
        id: 'N3', file: LIB, why: '偷偷塞回一个单字关键词(`丝` 在服装纺织)',
        from: "'纺织', '面料'", to: "'纺织', '丝', '面料'",
        expect: 'I3',
    },
    {
        id: 'N4', file: OQF, why: '把第二份表加回来(合一被撤销)',
        from: "import { INDUSTRY_CATEGORIES, normalizeIndustry as normalizeIndustryShared } from '@/lib/industries';",
        to: "import { normalizeIndustry as normalizeIndustryShared } from '@/lib/industries';\n"
            + "const INDUSTRY_CATEGORIES = [{ label: '制造业', keywords: ['制造'] }] as const;",
        expect: 'I5',
    },
    {
        id: 'N5', file: LIB, why: '删掉 `私厨` ⇒ 声明表里那一条不再成立(表与现实脱钩)',
        from: "'外卖', '私厨'", to: "'外卖'",
        expect: 'I7b',
    },
    {
        id: 'N6', file: LIB, why: '悄悄新增一个关键词 ⇒ 产生**未声明**的分类变化',
        from: "'景区', '文化'", to: "'景区', '会展', '文化'",
        expect: 'I7',
    },
    {
        id: 'N7', file: LIB, why: '把「酒店」从文旅娱乐挪走 ⇒ 客户那一条又不对了',
        from: "'旅游', '酒店', '住宿'", to: "'旅游', '住宿'",
        expect: 'I1b',
        note: '🔴 注意它**不会**让 I1 红(酒店住宿里还有「住宿」,仍不是餐饮)—— '
            + '所以"不是餐饮"这一格不够,必须有 I1b 钉住它到底归了哪一类。',
    },
    {
        id: 'NSYN', file: LIB, why: '🔴 **故意写坏语法** —— 这一发是那道语法前置自己的牙证',
        from: 'export function classifyWith(',
        to: 'export function classifyWith((',
        expectSyntaxFail: true,
        note: '不加前置的话,它会让门 rc=1,被读成「锁咬住了」—— 而实际上**什么都没测到**。C 2026-09-20 就栽在这里。',
    },
];

const run = () => {
    try {
        const out = execFileSync(process.execPath,
            [join(ROOT, 'scripts', 'verify-industry-classifier.mjs')],
            { cwd: ROOT, encoding: 'utf8', timeout: 300000, stdio: ['ignore', 'pipe', 'pipe'] });
        return { rc: 0, out };
    } catch (e) {
        return { rc: e.status === undefined ? -1 : e.status, out: String(e.stdout || '') + String(e.stderr || '') };
    }
};
/* 🔴 以 rc 为准;名字只用来说清是谁红的。 */
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
        'src/lib/industries.ts', 'src/pages/Quote/OnlineQuoteFlow.tsx'],
    { cwd: ROOT, encoding: 'utf8' }).trim() || '(两个文件都回到下毒前)');
} catch { console.log('(git 读不到)'); }
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
