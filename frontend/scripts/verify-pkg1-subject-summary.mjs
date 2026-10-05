#!/usr/bin/env node
/**
 * 判据 · 包一阶段② 诊断对象四项摘要 + 打标就地确认(订正六 / 订正十 / 订正十补)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 本阶段不碰提交体与计价 —— 那些在阶段③⑤,钉这里只会永远"未评估"。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const rdRepo = (p) => readFileSync(join(REPO, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const SUMMARY = strip(rd('src/pages/Diagnosis/launch/SubjectSummary.tsx'));
const FIELDS = strip(rd('src/pages/Diagnosis/launch/subjectFields.ts'));
const GATE = strip(rd('src/pages/Diagnosis/launch/defensiveLaunchGate.ts'));

// ── A 🔴 跨层锁:打标规则的前端复述必须逐项等于后端那份 ────────────────
/**
 * 前端这份是**同一条规则的第二份**。它复述的必须是**今天真在跑的**那份
 * (`utils/is_test_brand.py`,三个写入点都调它),不是 CLAUDE.md 写的 5 项。
 * 两者当前差 3 项(demo / debug / sandbox),见 #99。
 */
let pySet = null;
try {
    const py = rdRepo('utils/is_test_brand.py');
    const m = py.match(/TEST_NAME_PATTERNS\s*=\s*re\.compile\(r"([^"]+)"/);
    pySet = m ? m[1].split('|').map((x) => x.trim()).filter(Boolean).sort() : null;
} catch { /* 下面按未评估处理 */ }
const mod = await import(new URL('../src/pages/Diagnosis/launch/testBrandName.ts', import.meta.url).href)
    .catch(() => null);

if (!pySet || !mod) {
    notEvaluated += 1;
    console.log(`  ⚠️ A0 **未评估**:后端模式集合或前端模块没取到 (py=${JSON.stringify(pySet)} mod=${!!mod})`
        + ' ⇒ 跨层锁没跑,不能声称两份一致。');
} else {
    const tsSet = [...mod.TEST_NAME_PATTERNS].sort();
    ok(JSON.stringify(tsSet) === JSON.stringify(pySet),
        `A1 🔴 前端模式集合 == 后端 is_test_brand.py (${JSON.stringify(tsSet)} vs ${JSON.stringify(pySet)})`);
    ok(pySet.length >= 5, `A2 正样本臂:后端解析出 ${pySet.length} 项(解析空会让 A1 恒真)`);
    // 🔴 行为臂:光比集合不够,还要比**判定结果**
    const CASES = [
        ['Contest 传媒', true], ['Protest 科技', true], ['Latest 潮流', true],
        ['Sandbox 家居', true], ['Debug 网络', true],
        ['测试科技有限公司', true], ['XX验收部', true],
        ['浙江岱林', false], ['Best Buy 电器', false], ['杭州云上科技', false],
    ];
    const wrong = CASES.filter(([n, want]) => mod.looksLikeTestBrand(n) !== want);
    ok(wrong.length === 0,
        `A3 行为臂:10 个样本判定与后端语义(子串 · 不加词边界 · 忽略大小写)一致`
        + (wrong.length ? ` —— 不一致:${wrong.map(([n]) => n).join(',')}` : ''));
    ok(mod.matchedTestPatterns('Contest 传媒').includes('test'),
        'A4 提示要指名道姓:命中项可列出(她才知道改哪个字)');
}

// ── B 纯逻辑臂:四项齐的判定 ─────────────────────────────────────────
const fmod = await import(new URL('../src/pages/Diagnosis/launch/subjectFields.ts', import.meta.url).href)
    .catch(() => null);
if (!fmod) {
    notEvaluated += 1;
    console.log('  ⚠️ B0 **未评估**:没能 import subjectFields.ts,纯逻辑臂没跑。');
} else {
    const c = fmod.subjectComplete;
    ok(c({ brandName: 'A', industry: '装修', clientLocation: '深圳', businessScope: 'regional' }),
        'B1 区域生意四项齐 ⇒ 可进摘要态');
    ok(!c({ brandName: 'A', industry: '装修', clientLocation: '', businessScope: 'regional' }),
        'B1 反臂:区域生意缺城市 ⇒ 不进摘要态(区域客户必须出带地域的题)');
    ok(c({ brandName: 'A', industry: '装修', clientLocation: '', businessScope: 'national' }),
        'B2 🔴 全国生意**不要求城市** —— 否则全国客户永远进不了摘要态');
    ok(!c({ brandName: '', industry: '装修', clientLocation: '深圳', businessScope: 'regional' }),
        'B3 反臂:没品牌名不进摘要态');
    ok(fmod.scopeShortLabel('regional') === '区域生意' && fmod.scopeShortLabel('national') === '全国生意',
        'B4 范围短名来自单源');
    ok(fmod.scopeShortLabel('zzz') === '', 'B4 反臂:未知值给空串,不编一个出来');
}

// ── C 结构臂 ────────────────────────────────────────────────────────
ok(/subjectSummaryMode \? \(/.test(PAGE) && /<SubjectSummary/.test(PAGE),
    'C1 摘要态渲染 SubjectSummary');
ok(/\{!subjectSummaryMode && \(/.test(PAGE),
    'C1 配套:表单态整块收起(两态互斥,不是叠加)');
ok(/data-testid="subject-summary"/.test(SUMMARY)
    && /data-testid="subject-summary-city"/.test(SUMMARY)
    && /data-testid="subject-summary-industry"/.test(SUMMARY)
    && /data-testid="subject-summary-scope"/.test(SUMMARY)
    && /data-testid="subject-edit"/.test(SUMMARY),
    'C2 摘要态稳定锚齐全(B 的两把尺子里"摘要态读 textContent"那把靠它)');
ok(!/useState|useRef|useMemo/.test(SUMMARY),
    'C3 🔴 摘要组件**不持任何状态** —— 存快照就会「看到的 ≠ 提交的」(与 #62 同形)');
ok(/BUSINESS_SCOPE_OPTIONS\.map/.test(PAGE),
    'C4 下拉选项走单源(与摘要短名同一份,写两份必漂)');
ok(!/区域生意（只在本地\/周边接单）/.test(PAGE),
    'C4 反臂:页面里不再硬写那两句 option 文案');
ok(/lastSummarizedNameRef/.test(PAGE),
    'C5 摘要态下改品牌名 ⇒ 退回表单态(否则三项还是上一个客户的值而她看不出来)');

// ── D 打标就地确认 + 拦截 ───────────────────────────────────────────
ok(/data-testid="test-brand-confirm"/.test(PAGE), 'D1 命中打标规则时出现就地确认条');
ok(/data-testid="test-brand-accept"/.test(PAGE) && /data-testid="test-brand-rename"/.test(PAGE),
    'D1 配套:两个动作(仍要用 / 换个名字)—— 只警告不给出路 = 死胡同');
/**
 * 🔴 D2 是**行为锁**,不是名字锁。
 *    第一版写的是 `/testNameUnacknowledged/.test(GATE)` —— 把判定行整行删掉后
 *    它**仍然绿**,因为那个名字还留在 interface 声明里。
 *    「名字出现在文件里」证明不了「闸真的会因为它拦人」。
 */
const gmod = await import(new URL('../src/pages/Diagnosis/launch/defensiveLaunchGate.ts', import.meta.url).href)
    .catch(() => null);
if (!gmod) {
    notEvaluated += 1;
    console.log('  ⚠️ D2 **未评估**:没能 import defensiveLaunchGate.ts,行为锁没跑。');
} else {
    // 一个本来**可以启动**的输入,只切换这一个字段,结果必须翻转
    const base = {
        mode: 'offensive', isFormComplete: true, totalDiagnosisCost: 650,
        defensiveQuestionCount: 0, hasBrandId: true, loading: false, planPending: false,
    };
    const openOk = gmod.isLaunchBlocked({ ...base, testNameUnacknowledged: false });
    const blocked = gmod.isLaunchBlocked({ ...base, testNameUnacknowledged: true });
    ok(openOk === false,
        'D2 正样本臂:同一份输入在**已确认**时是可以启动的(否则下一条的"被拦住"不携带信息)');
    ok(blocked === true,
        'D2 🔴 行为锁:只把 testNameUnacknowledged 翻成 true ⇒ **真的挡住**(不是只显示一句话)');
}
ok(/if \(testNameUnacknowledged\) return '这个名字会被当作测试客户/.test(PAGE),
    'D2 配套:灰按钮必须自带人话(顺序 = 拦截判定顺序)');
ok(/const isNewBrandName = !!formData\.brandName\.trim\(\) && !selectedBrandId;/.test(PAGE),
    'D3 只对**新品牌**提示 —— 已存在品牌的 is_test 早就定了,对它提示是错的');
ok(/useEffect\(\(\) => \{ setTestNameAck\(false\); \}, \[formData\.brandName\]\);/.test(PAGE),
    'D4 换了名字 ⇒ 确认作废(否则她确认过一次,之后任何名字都不再提示)');

// ── E 🔴 红臂:基线 e19fe1496 上都不成立 ─────────────────────────────
const BASE = 'e19fe1496';
let basePage = null;
try {
    basePage = execFileSync('git', ['show', `${BASE}:frontend/src/pages/Diagnosis/NewDiagnosis.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (basePage) {
    ok(!/subjectSummaryMode/.test(basePage), 'E1 红臂:改动前没有摘要态');
    ok(!/test-brand-confirm/.test(basePage), 'E2 红臂:改动前没有打标提示');
    ok(/区域生意（只在本地\/周边接单）/.test(basePage), 'E3 红臂:改动前 option 文案是硬写的');
    ok(/formData\.clientLocation/.test(basePage),
        'E4 配对臂:基线里本来就有的锚确实在(否则 E1/E2 的"没有"只是取到空内容)');
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const f1 = js.includes('subject-summary');
    ok(f1, 'F1 摘要锚进了产物');
    ok(js.includes('test-brand-confirm'), 'F2 打标提示锚进了产物');
    if (f1) ok(!js.includes('zzq-必然不存在的锚'), 'F3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ F3 未评估:F1 未成立 ⇒ 提取可能整体为空'); }
}

if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
