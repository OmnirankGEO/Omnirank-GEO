#!/usr/bin/env node
/**
 * WO_253 注毒。
 *
 * 🔴 **本 runner 比之前多一道前置:下毒之后先验语法。**
 *    来源:C 2026-09-20 自陈 —— 他第一版的毒是**语法错**,
 *    runner 把门的 rc=1 读成「CAUGHT」,差点据此下结论。
 *    我全部 runner 都用 `caught = rc === 1` 判,**同一个洞我也有**。
 *    ⇒ 毒落盘后先 transpile 一遍;语法不过就报「毒没下成(语法错)」,
 *      **绝不算 CAUGHT** —— 「毒没下成」与「锁咬住了」在 rc 上完全同形。
 *    Z9 是这道前置自己的牙证:一发**故意写坏语法**的毒,必须被它拦下。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const SF = join(ROOT, 'src/lib/saveFeedback.ts');
const SN = join(ROOT, 'src/lib/serviceNotice.ts');
const PAGE = join(ROOT, 'src/pages/Brand/BrandDetailPage.tsx');

const POISONS = [
    {
        id: 'Z1', file: PAGE, why: '保存改回**无条件**绿(原缺陷字面复现)',
        from: "      const fb = saveFeedback(payload as Record<string, unknown>, putRes?.data as Record<string, unknown>);\n      if (fb.kind === 'missing') toast.error(fb.message);\n      else toast.success(fb.message);",
        to: "      toast.success('已保存');",
        expect: 'W2',
    },
    {
        id: 'Z2', file: SF, why: '🔴 `persisted` 用 `|| []` 压成一种(键不存在也当成"一个都没落")',
        from: "    if (!response || !hasKey(response, 'persisted')) {",
        to: '    if (!response) {',
        expect: 'S3',
        note: '后果反向而且更贵:后端那一段还没上线时,**每一次保存都变红字**。',
    },
    {
        id: 'Z3', file: SF, why: '不看"提交了没有",凡 persisted 缺就红',
        from: '    const submitted = CONTACT_FIELDS.filter((f) => filled(payload?.[f]));',
        to: '    const submitted = [...CONTACT_FIELDS];',
        expect: 'S4',
    },
    {
        id: 'Z4', file: SN, why: 'expired 提示不带到期日',
        from: '            ? `服务期已于 ${when} 到期,确认链已生成`',
        to: '            ? `服务期已到期,确认链已生成`',
        expect: 'N1',
    },
    {
        id: 'Z5', file: SN, why: '🔴 认不出的 state 猜成 expired(对客户说一句不成立的服务期结论)',
        from: "    if (state !== 'active' && state !== 'never_paid' && state !== 'expired') return null;",
        to: "    if (state !== 'active' && state !== 'never_paid' && state !== 'expired') return { state: 'expired' };",
        expect: 'N4',
    },
    {
        id: 'Z6', file: PAGE, why: '服务期信息塞回 catch(出口跟着被吞)',
        from: '      setServiceNotice(noticeView(parseServiceNotice(\n        (result as unknown as Record<string, unknown>)?.service_notice)));',
        to: '',
        expect: 'W3',
    },
    {
        id: 'Z7', file: PAGE, why: '落库后不重拉详情(回显的还是本地表单值)',
        from: '      if (fb.shouldReload) setDetailNonce((n) => n + 1);',
        to: '',
        expect: 'W2b',
    },
    {
        id: 'Z9', file: SF, why: '🔴 **故意写坏语法** —— 这一发是那道语法前置自己的牙证',
        from: '    const submitted = CONTACT_FIELDS.filter((f) => filled(payload?.[f]));',
        to: '    const submitted = CONTACT_FIELDS.filter((f) => filled(payload?.[f]);',
        expectSyntaxFail: true,
        note: '不加前置的话,它会让门 rc=1,被读成「锁咬住了」—— '
            + '而实际上**什么都没测到**。C 2026-09-20 就栽在这里。',
    },
];

const run = () => {
    try {
        const out = execFileSync(process.execPath, [join(ROOT, 'scripts', 'verify-save-feedback-and-notice.mjs')],
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
        const parses = syntaxOk(p.file);
        if (p.expectSyntaxFail) {
            /* 这一发就是来验前置的:它**必须**被前置拦下,而不是走到门那一步。 */
            const good = !parses;
            if (!good) surprises += 1;
            console.log(`\n${p.id} ${p.why}`);
            console.log(`   NC 命中 ${n} 处 · ${good ? '✅ 被**语法前置**拦下(未计入 CAUGHT)' : '🔴 前置没拦住 —— 那道前置是瞎的'}`);
            if (p.note) console.log(`   ${p.note}`);
            continue;
        }
        if (!parses) {
            console.log(`\n${p.id} 🔴 毒把文件写成了语法错 ⇒ **这一发没下成**,不算 CAUGHT(需重写这发毒)`);
            surprises += 1;
            continue;
        }
        const r = run();
        const reds = redIds(r.out);
        const caught = r.rc === 1;
        if (!caught) surprises += 1;
        console.log(`\n${p.id} ${p.why}`);
        console.log(`   NC 命中 ${n} 处 · 语法通过 · ${caught ? `被 ${reds.join(',') || 'rc=1'} 抓住` : `🔴 存活(rc=${r.rc})`}`);
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
        'src/lib/saveFeedback.ts', 'src/lib/serviceNotice.ts', 'src/pages/Brand/BrandDetailPage.tsx'],
    { cwd: ROOT, encoding: 'utf8' }).trim() || '(三个文件都回到下毒前)');
} catch { console.log('(git 读不到)'); }
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
