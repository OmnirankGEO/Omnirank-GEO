#!/usr/bin/env node
/**
 * 门四判据的**撕锁自证** —— 逐发变异,证明每条锁都真的会红。
 *
 * 纪律与 `mutation_runner_defgeo_legacy_required.mjs` 同源:
 *  · 先跑一发**不变异**的基线;基线不是全绿,后面全部作废;
 *  · 变异必须语法合法、语义精确(整段替废代码只是 blunt kill);
 *  · 还原用备份字节回写并逐字节核对,**不用 `git checkout`**;
 *  · 每发都声明"该红哪几条",红错了地方一样算没杀死。
 *
 * 跑法:cd frontend && node scripts/mutation_runner_defgeo_report_retry.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PAGE = join(ROOT, 'src/pages/Diagnosis/DiagnosisReport.tsx');
const PURE = join(ROOT, 'src/pages/Diagnosis/report/reportLoadFailure.ts');
// 两套判据一起撕:
//  · DOM 本体(真浏览器 · 真组件 · 真取数)
//  · build 链里那几条不需要浏览器的纯逻辑锁(在 gate2 脚本里)
// 只撕 DOM 那套的话,"build 链里守着的那几条到底有没有判别力"就没人证明。
const CRITERIA = [
    join(ROOT, 'scripts/test-defgeo-report-retry.mjs'),
    join(ROOT, 'scripts/test-defgeo-gate2-fixes.mjs'),
];

const sha = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

function runCriteria() {
    let out = '';
    let exit = 0;
    for (const script of CRITERIA) {
        try {
            out += execFileSync(process.execPath, [script], {
                cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 900000,
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
    };
}

const MUTATIONS = [
    {
        id: 'MUT-1', file: PAGE,
        desc: '拿掉自动重试(退回门四之前的行为)',
        from: `const [detailRes, contentRes] = await loadWithOneRetry(() => Promise.all([
                diagnosisApi.getDetail(parseInt(id)),
                diagnosisApi.getContent(parseInt(id)),
            ]));`,
        to: `const [detailRes, contentRes] = await Promise.all([
                diagnosisApi.getDetail(parseInt(id)),
                diagnosisApi.getContent(parseInt(id)),
            ]);`,
        expect: '判据 1(瞬时 503 不上屏)+ 判据 7(自取消)必须红',
        want: (red) => red.some((l) => l.includes('失败态全程未上屏'))
            && red.some((l) => l.includes('不因自取消画成失败态')),
    },
    {
        id: 'MUT-2', file: PAGE,
        desc: '把「重试」按钮摘掉(工单点名的那一发)',
        from: `                    <Button data-testid="report-retry" onClick={retryLoadReport}>重试</Button>\n`,
        to: '',
        expect: '判据 2 的「有重试按钮 / 点击恢复」必须红',
        want: (red) => red.some((l) => l.includes('有「重试」按钮')),
    },
    {
        id: 'MUT-3', file: PAGE,
        desc: '「重试」改成跳首页(不是就地重拉)',
        from: `<Button data-testid="report-retry" onClick={retryLoadReport}>重试</Button>`,
        to: `<Button data-testid="report-retry" onClick={() => navigate("/")}>重试</Button>`,
        expect: '判据 2 的「就地恢复 / 路由不变」必须红',
        want: (red) => red.some((l) => l.includes('就地恢复')) || red.some((l) => l.includes('仍是原路由')),
    },
    {
        id: 'MUT-4', file: PURE,
        desc: '4xx 也当瞬时(什么都重试)',
        from: `    if (typeof status === 'number') {
        return status >= 500 && status <= 599;
    }
    return true;`,
        to: `    if (typeof status === 'number') {
        return true;
    }
    return true;`,
        expect: '判据 5「403 只请求一次」必须红',
        want: (red) => red.some((l) => l.includes('403 只请求了一次')),
    },
    {
        id: 'MUT-5', file: PURE,
        desc: '什么都不当瞬时(自动重试整个失效)',
        from: `    if (typeof status === 'number') {
        return status >= 500 && status <= 599;
    }
    return true;`,
        to: `    if (typeof status === 'number') {
        return false;
    }
    return false;`,
        expect: '判据 1 与判据 7 必须红(与 MUT-1 同侧但入口不同)',
        want: (red) => red.some((l) => l.includes('失败态全程未上屏'))
            && red.some((l) => l.includes('不因自取消画成失败态')),
    },
    {
        id: 'MUT-6', file: PURE,
        desc: '文案里把钱那半句删掉',
        from: `    body: '服务器忙了一下,报告已经生成好了。点「重试」就能接着看,重新加载不额外扣算力。',`,
        to: `    body: '服务器忙了一下,报告已经生成好了。点「重试」就能接着看。',`,
        expect: '判据 3「把钱说死」必须红',
        want: (red) => red.some((l) => l.includes('把钱说死')),
    },
    {
        id: 'MUT-7', file: PURE,
        desc: '文案换回吓人的说法',
        from: `    headline: '刚才没连上,报告还在',`,
        to: `    headline: '加载报告出现异常',`,
        // 🔴 第一次跑我把预期写成"两条都得红",结果只红了一条 —— **预期错了不是锁弱**:
        //    「明说报告没丢」查的是整个失败框的文本,body 里那句「报告已经生成好了」
        //    仍然满足它。要求本来就是"框里得说清楚报告没丢",在哪一句说不重要。
        expect: '判据 3「不含恐吓性表述」必须红(「明说报告没丢」由 body 承担,仍绿是对的)',
        want: (red) => red.some((l) => l.includes('不含恐吓性')),
    },
    {
        id: 'MUT-8', file: PAGE,
        desc: '在成功路径里多渲一个元素(证明"逐字节等价"不是恒真)',
        from: `        <div className="p-4 md:p-6 space-y-6 pb-24 lg:pb-0">`,
        to: `        <div className="p-4 md:p-6 space-y-6 pb-24 lg:pb-0"><span data-mut8 /> `,
        expect: '判据 6「与基线逐字节相同」必须红',
        want: (red) => red.some((l) => l.includes('与基线逐字节相同')),
    },
];

const originals = new Map();
for (const f of [PAGE, PURE]) originals.set(f, readFileSync(f));
console.log(`被测文件:`);
for (const [f, buf] of originals) console.log(`  ${f.replace(ROOT, 'frontend')} sha=${sha(buf)}`);

console.log('\n=== 基线(不变异)===');
const baseline = runCriteria();
console.log(`  绿 ${baseline.green} · 红 ${baseline.red.length} · exit=${baseline.exit}`);
if (baseline.red.length > 0 || baseline.exit !== 0) {
    console.log('🔴 基线就不是全绿,撕锁结果无意义。先修判据。');
    baseline.red.forEach((l) => console.log('   ' + l));
    process.exit(1);
}

let bad = 0;
/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(MUTATIONS[0].file, console.log);

for (const m of MUTATIONS) {
    console.log(`\n=== ${m.id} · ${m.desc} ===`);
    const src = originals.get(m.file).toString('utf8');
    const hits = src.split(m.from).length - 1;
    if (hits !== 1) {
        console.log(`  🔴 变异锚点命中 ${hits} 处(必须恰好 1)—— 这一发作废,不当"杀不掉"`);
        bad++;
        continue;
    }
    try { assertRulerWorks(m.file); } catch (err) { console.log(`  ${err.message}`); process.exit(3); }
    writeFileSync(m.file, src.replace(m.from, m.to), 'utf8');
    if (!syntaxOk(m.file)) {
        console.log(`  ${NOT_LANDED_SYNTAX}`);
        bad++;
        writeFileSync(m.file, src, 'utf8');
        continue;
    }
    let res;
    try {
        res = runCriteria();
    } finally {
        writeFileSync(m.file, originals.get(m.file));
        if (!readFileSync(m.file).equals(originals.get(m.file))) {
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

console.log('\n还原核对:');
let dirty = false;
for (const [f, buf] of originals) {
    const now = readFileSync(f);
    const same = now.equals(buf);
    if (!same) dirty = true;
    console.log(`  ${f.replace(ROOT, 'frontend')} sha=${sha(now)} ${same ? '(逐字节一致)' : '(🔴 不一致)'}`);
}
if (dirty) process.exit(2);

console.log('================================================================');
console.log(bad === 0
    ? `✅ ${MUTATIONS.length} 发变异全部被精确杀死`
    : `🔴 ${bad}/${MUTATIONS.length} 发未达预期`);
process.exit(bad === 0 ? 0 : 1);
