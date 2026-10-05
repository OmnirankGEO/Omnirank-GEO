#!/usr/bin/env node
/**
 * WO_243 甲 注毒 —— 证明那一臂有牙,以及哪几面没牙。
 *
 * 🔴 列毒来自**交付抬头承诺的每一样**,不是我判据覆盖的那些面:
 *    ① 那句话来自后端(不是第二份硬编码);② 按**整串码**判,不按状态码、不按前缀;
 *    ③ 档位与下单被隐藏;④ 后端没出声时明说没出声;⑤ 兜底不撤。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const IC = join(ROOT, 'src/pages/Agent/InventoryCenter.tsx');
const HOOK = join(ROOT, 'src/hooks/usePricingSSOT.ts');

const POISONS = [
    {
        id: 'M1', file: IC, why: '按**状态码**判,不按码判 ⇒ 目录没发布也被说成"你不需要进货"',
        from: "      if (!catalogResult.ok && catalogResult.code === 'PLATFORM_DIRECT_NO_PROCUREMENT') {",
        to: '      if (!catalogResult.ok && catalogResult.status === 409) {',
        expect: 'D1',
    },
    {
        id: 'M2', file: IC, why: '按**前缀**判 ⇒ 近名码 PLATFORM_DIRECT_NOT_READY(故障)被当成正常态',
        from: "      if (!catalogResult.ok && catalogResult.code === 'PLATFORM_DIRECT_NO_PROCUREMENT') {",
        to: "      if (!catalogResult.ok && String(catalogResult.code).startsWith('PLATFORM_DIRECT')) {",
        expect: 'E1',
    },
    {
        id: 'M3', file: IC, why: '把后端那句换回前端硬编码的第二份',
        from: '                    {platformDirectNotice}',
        to: '                    平台仓库不向自身进货。原始库存发行、人工调整和价目表管理必须通过管理员入口完成。',
        expect: 'A1',
    },
    {
        id: 'M4', file: HOOK, why: '码的读取点退回 `detail.code`(归一化之后恒 undefined)',
        from: '  const detailObject = structured && typeof structured === \'object\'',
        to: '  const detailObject = detail && typeof detail === \'object\'',
        expect: 'A1',
    },
    {
        id: 'M5', file: IC, why: '撤掉 is_admin 兜底 ⇒ 后端那笔上线前,档位会摆到平台账号面前',
        from: '  const procurementBlocked = isPlatformWarehouseMode || platformDirectNotice !== null;',
        to: '  const procurementBlocked = platformDirectNotice !== null;',
        expect: 'F2',
    },
    {
        id: 'M6', file: IC, why: '"还没拿到"改成拿旧话顶上 ⇒ 两种状态又被压成一种',
        from: '                    <span>尚未取到平台口径说明 · 下单入口已按兜底规则关闭</span>',
        to: '                    <span>平台仓库不向自身进货。</span>',
        expect: 'F1b',
    },
    {
        id: 'M7', file: HOOK, why: '把新码从 KNOWN_CODES 里删掉(类型仍在,不报类型错)',
        from: "  'PLATFORM_DIRECT_NO_PROCUREMENT',\n  'NO_PUBLISHED_RETAIL',",
        to: "  'NO_PUBLISHED_RETAIL',",
        expect: 'A1',
    },
];

const run = () => {
    try {
        const out = execFileSync(process.execPath,
            [join(ROOT, 'scripts', 'test-admin-procurement-notice-render.mjs')],
            { cwd: ROOT, encoding: 'utf8', timeout: 900000, stdio: ['ignore', 'pipe', 'pipe'] });
        return { rc: 0, out };
    } catch (e) {
        return { rc: e.status === undefined ? -1 : e.status, out: String(e.stdout || '') + String(e.stderr || '') };
    }
};
/* 🔴 以 rc 为准,名字只用来说是谁红的 —— 正则写错会把每一发毒都报成"存活"。 */
const redIds = (out) => [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]);

console.log('=== 基线(必须全绿,红基线会让每发毒都像命中)===');
const base = run();
console.log(`  rc=${base.rc}`);
if (base.rc !== 0) {
    console.log('🔴 基线不绿,注毒结果不作数');
    console.log(base.out.split('\n').filter((l) => l.includes('FAIL')).join('\n'));
    process.exit(3);
}

let surprises = 0;
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(POISONS[0].file, console.log);

for (const p of POISONS) {
    const original = readFileSync(p.file, 'utf8');
    const n = original.split(p.from).length - 1;
    if (n === 0) { console.log(`\n${p.id} 🔴 NC:锚没命中,这一发**没下成**(不是"有牙")`); surprises += 1; continue; }
    try { assertRulerWorks(p.file); } catch (err) { console.log(`\n${p.id} ${err.message}`); process.exit(3); }
    writeFileSync(p.file, original.split(p.from).join(p.to), 'utf8');
    try {
        if (!syntaxOk(p.file)) {
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
    } finally {
        writeFileSync(p.file, original, 'utf8');
    }
}

console.log('\n=== 复原自证 ===');
try {
    const st = execFileSync('git', ['status', '--porcelain',
        'src/pages/Agent/InventoryCenter.tsx', 'src/hooks/usePricingSSOT.ts'],
    { cwd: ROOT, encoding: 'utf8' });
    console.log(st.trim() || '(两个文件都回到下毒前)');
} catch { console.log('(git 读不到)'); }
console.log(`收尾复跑 rc=${run().rc}`);
console.log(surprises === 0 ? '\n没有意外' : `\n🔴 ${surprises} 发出乎预料`);
