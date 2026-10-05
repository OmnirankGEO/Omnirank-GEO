#!/usr/bin/env node
/**
 * G2b DOM 判据的**撕锁自证** —— 逐发变异,证明每条锁都真的会红。
 *
 * 全绿本身不是证据:判据可能压根没打到被测那一行(2026-08 连栽多次:
 * 夹具自己构造中间值 / 第二把锁把第一把遮住 / 断言命中的是解释它的注释)。
 * 所以每条结论都要有一发变异专门把它打红,并且**只把它打红**。
 *
 * 纪律:
 *  · 变异必须**语法合法、语义精确** —— 整段替成废代码只是 blunt kill,
 *    证明不了判据在看那一行(2026-08-21 迁移变异那次的教训)。
 *  · 还原**不用 `git checkout`** —— 用备份字节回写并逐字节核对,
 *    免得把工作树里别的改动一起卷掉。
 *  · 先跑一发**不变异**的对照:基线不是全绿,后面全部作废。
 *
 * 跑法:cd frontend && node scripts/mutation_runner_defgeo_legacy_required.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PAGE = join(ROOT, 'src/pages/Diagnosis/NewDiagnosis.tsx');
// 两套判据一起撕:
//  · DOM 本体(真浏览器 form.checkValidity() / requestSubmit())
//  · build 链里那条不需要浏览器的 AST 接线锁(在 gate2 脚本里)
// 只撕 DOM 那套的话,"接线锁是不是也在守着"就没人证明。
const CRITERIA = [
    join(ROOT, 'scripts/test-defgeo-legacy-required.mjs'),
    join(ROOT, 'scripts/test-defgeo-gate2-fixes.mjs'),
];

const sha = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

/** 跑一次判据,回 { red: [失败条目文本], green: n, exit } */
function runCriteria() {
    let out = '';
    let exit = 0;
    for (const script of CRITERIA) {
        try {
            out += execFileSync(process.execPath, [script], {
                cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 600000,
            });
        } catch (err) {
            out += String(err.stdout || '') + String(err.stderr || '');
            exit = exit || (err.status ?? 1);
        }
    }
    const lines = out.split(/\r?\n/);
    return {
        red: lines.filter((l) => l.includes('\u{1F534}')).map((l) => l.trim()),
        green: lines.filter((l) => l.includes('✅')).length,
        exit,
        out,
    };
}

const MUTATIONS = [
    {
        id: 'MUT-1',
        desc: '退回返修前:无条件 required(=G2b 没做)',
        from: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={!isDefensiveFlow}`,
        to: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required`,
        expect: '防御/混合两侧必须红;legacy 侧仍绿',
        want: (red) => red.some((l) => l.includes('defensive')) && red.some((l) => l.includes('hybrid'))
            && !red.some((l) => l.includes('offensive') || l.includes('不带参')),
    },
    {
        id: 'MUT-2',
        desc: '判定取反:防御必填 / legacy 不必填',
        from: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={!isDefensiveFlow}`,
        to: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={isDefensiveFlow}`,
        expect: '两侧都必须红(正反各自独立成锁)',
        want: (red) => red.some((l) => l.includes('defensive')) && red.some((l) => l.includes('offensive')),
    },
    {
        id: 'MUT-3',
        desc: 'hybrid 漏回 legacy(只放行 defensive)',
        from: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={!isDefensiveFlow}`,
        to: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={campaignMode !== 'defensive'}`,
        expect: 'hybrid 红、defensive 绿 —— 证明 hybrid 是**单独**被钉住的',
        want: (red) => red.some((l) => l.includes('hybrid')) && !red.some((l) => l.includes('defensive:')),
    },
    {
        id: 'MUT-4',
        desc: '一律不必填(legacy 保护被拆)',
        from: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={!isDefensiveFlow}`,
        to: `onChange={(e) => setFormData({ ...formData, keywords: e.target.value })}
                                required={false}`,
        expect: 'legacy 侧必须红 —— 证明"legacy 仍拦"不是恒真',
        want: (red) => red.some((l) => l.includes('offensive')) && red.some((l) => l.includes('不带参')),
    },
    {
        id: 'MUT-5',
        desc: '给 <form> 加 noValidate(原生校验整个关掉)',
        from: '<form onSubmit={handleSubmit} className="space-y-5">',
        to: '<form noValidate onSubmit={handleSubmit} className="space-y-5">',
        // 🔴 这一发第一次跑时"红得不对",查出来是我的预期错了不是锁弱:
        //    `checkValidity()` **不受 noValidate 影响**,只有真的 requestSubmit
        //    才看得出差别。判据因此补了 submitFired 一条,现在两处都会红。
        expect: 'noValidate 活性条 + legacy 的 submitFired 必须红',
        want: (red) => red.some((l) => l.includes('noValidate'))
            && red.some((l) => l.includes('submit 事件不 fire')),
    },
    {
        id: 'MUT-6',
        desc: '把「所属行业」的必填拿掉',
        // 🔴 [包H 2026-08-24] 原锚点是 #industry 上那个**裸** required —— Owner 裁定后
        //    它已变成 `required={!isDefensiveFlow}`,锚点失效(实测:变异produced 坏源码,
        //    判据报"打包真组件失败" = 这一发**没跑**,不是"杀不掉")。
        //    重新瞄准:把行业改成**一律不必填**,legacy 那一臂必须红。
        from: `                                        onFocus={() => setIndustryDropOpen(true)}\n                                        required={!isDefensiveFlow}`,
        to: `                                        onFocus={() => setIndustryDropOpen(true)}`,
        expect: 'legacy 侧「行业为空仍被拦」必须红 —— 证明那条期望锁不是恒绿摆设',
        want: (red) => red.some((l) => l.includes('行业')),
    },
    {
        id: 'MUT-7',
        desc: '给「客户所在地」也加 required(混进第二个空必填格)',
        from: `                                    onChange={(e) => setFormData({ ...formData, clientLocation: e.target.value })}`,
        to: `                                    onChange={(e) => setFormData({ ...formData, clientLocation: e.target.value })}\n                                    required`,
        expect: '「:invalid 恰好是 #keywords 一格」必须红 —— 证明那条锁真的在数分母',
        want: (red) => red.some((l) => l.includes('恰好是')),
    },
];

// ── 基线 ────────────────────────────────────────────────────────
const original = readFileSync(PAGE);
console.log(`被测文件 ${PAGE.replace(ROOT, 'frontend')} sha=${sha(original)}\n`);

console.log('=== 基线(不变异)===');
const baseline = runCriteria();
console.log(`  绿 ${baseline.green} · 红 ${baseline.red.length} · exit=${baseline.exit}`);
if (baseline.red.length > 0 || baseline.exit !== 0) {
    console.log('🔴 基线就不是全绿,撕锁结果无意义。先修判据。');
    baseline.red.forEach((l) => console.log('   ' + l));
    process.exit(1);
}

// ── 逐发变异 ─────────────────────────────────────────────────────
let bad = 0;
const src = original.toString('utf8');

/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(PAGE, console.log);

for (const m of MUTATIONS) {
    console.log(`\n=== ${m.id} · ${m.desc} ===`);
    const hits = src.split(m.from).length - 1;
    if (hits !== 1) {
        console.log(`  🔴 变异锚点命中 ${hits} 处(必须恰好 1)—— 这一发作废,不当"杀不掉"`);
        bad++;
        continue;
    }
    try { assertRulerWorks(PAGE); } catch (err) { console.log(`  ${err.message}`); process.exit(3); }
    writeFileSync(PAGE, src.replace(m.from, m.to), 'utf8');
    if (!syntaxOk(PAGE)) {
        console.log(`  ${NOT_LANDED_SYNTAX}`);
        bad++;
        writeFileSync(PAGE, src, 'utf8');
        continue;
    }
    let res;
    try {
        res = runCriteria();
    } finally {
        // 🔴 用备份字节回写,不 git checkout;并逐字节核对还原成功。
        writeFileSync(PAGE, original);
        const back = readFileSync(PAGE);
        if (!back.equals(original)) {
            console.log('  🔴🔴 还原失败!工作树已被污染,立即停手。');
            process.exit(2);
        }
    }
    console.log(`  期望:${m.expect}`);
    console.log(`  实测:绿 ${res.green} · 红 ${res.red.length} · exit=${res.exit}`);
    res.red.forEach((l) => console.log('     ' + l));
    if (res.red.length === 0) {
        console.log('  🔴 变异存活 —— 这一条锁证明不了任何事');
        bad++;
    } else if (!m.want(res.red)) {
        console.log('  🔴 红了,但红的不是该红的那几条 —— 判据指错了地方');
        bad++;
    } else {
        console.log('  ✅ 杀死,且红的正是该红的那几条');
    }
}

// ── 收尾:再确认工作树干净 ─────────────────────────────────────────
const finalBuf = readFileSync(PAGE);
console.log(`\n还原核对 sha=${sha(finalBuf)} ${finalBuf.equals(original) ? '(逐字节一致)' : '(🔴 不一致)'}`);
if (!finalBuf.equals(original)) process.exit(2);

console.log('================================================================');
console.log(bad === 0
    ? `✅ ${MUTATIONS.length} 发变异全部被精确杀死`
    : `🔴 ${bad}/${MUTATIONS.length} 发未达预期`);
process.exit(bad === 0 ? 0 : 1);
