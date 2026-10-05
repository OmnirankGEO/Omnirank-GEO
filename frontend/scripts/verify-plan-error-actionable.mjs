#!/usr/bin/env node
/**
 * 判据 · 题单校验提示可操作化(WO_QUESTION_PLAN_PROMPT_2026-09-02 §3.1.6)。
 *
 * 五组:
 *   A 纯逻辑三臂 —— `planSideGuard` 的三条规则各正反两向(不起浏览器)
 *   B 去重臂     —— 同一句「没有扣除任何算力」在 JSX 里只能出现一次
 *   C 修复动作臂 —— planError 渲染必须带一个可点的修复按钮
 *   D 补位可见臂 —— 补位题必须进**渲染源**,不能只进送出/计价源
 *   E dist 锚    —— 构建产物里必须真有这些标记(源码有 ≠ 产物有)
 *
 * 🔴 每组都配**必须不命中**的一半;只有必中臂成立时,必不中臂才有判别力
 *    (必中臂垮了的话,两臂会因"什么都没提取到"一起变绿)。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src', 'pages', 'Diagnosis');
const PAGE = join(SRC, 'NewDiagnosis.tsx');
const GUARD = join(SRC, 'launch', 'planSideGuard.ts');

let bad = 0;
let notEvaluated = 0;   // 🔴 「未评估」与「失败」分开:退出码不同,读的人才分得开
const ok = (cond, label, extra = '') => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}${extra ? ' · ' + extra : ''}`);
    if (!cond) bad += 1;
};

// ── A 纯逻辑三臂 ────────────────────────────────────────────────────
const guard = await import(new URL('../src/pages/Diagnosis/launch/planSideGuard.ts', import.meta.url).href)
    .catch(() => null);
if (!guard) {
    // .ts 不能直接 import ⇒ 退到源码结构核验,但**明说降级**,不假装跑过。
    const g = readFileSync(GUARD, 'utf8');
    ok(/mode !== 'hybrid'/.test(g), 'A0 wouldEmptySide 仅在 hybrid 生效(源码级)');
    ok(/raw === 'defensive' \|\| raw === 'offensive'/.test(g),
        'A0 sideFromTarget 只认两个合法值,认不出返回 null(源码级)');
    notEvaluated += 1;
    console.log('  ⚠️ A0 **降级 ⇒ 未评估**:没能真 import .ts,三条规则的正反六臂一条都没执行,'
        + '只剩字符串包含。降级与跑过同形,所以它既不是通过也不是失败。'
        + ' 解法:Node ≥22.6 的 --experimental-strip-types 或 tsx。');
} else {
    const { sideCounts, wouldEmptySide, sideFromTarget } = guard;
    const D = { modeSide: 'defensive', text: '甲' };
    const O = { modeSide: 'offensive', text: '乙' };
    ok(sideCounts([D, O, { modeSide: 'defensive', text: '  ' }]).defensive === 1,
        'A1 空白题不计数(后端也不认)');
    ok(wouldEmptySide('hybrid', [D, O], O) === 'offensive', 'A2 删掉某侧最后一道 ⇒ 报出那一侧');
    ok(wouldEmptySide('hybrid', [D, O, O], O) === null, 'A2 反臂:该侧还有别的题 ⇒ 不拦');
    ok(wouldEmptySide('defensive', [D], D) === null, 'A3 单模式下不拦(拦了就是把正常操作判成错误)');
    ok(sideFromTarget({ side: 'offensive' }) === 'offensive', 'A4 认得出侧别');
    ok(sideFromTarget({ side: '不认识' }) === null, 'A4 反臂:认不出必须返回 null,**不猜**');
}

// ── B 去重臂 ────────────────────────────────────────────────────────
const page = readFileSync(PAGE, 'utf8');
const noChargeInJsx = (page.match(/>没有扣除任何算力。</g) || []).length;
ok(noChargeInJsx === 0, 'B1 「没有扣除任何算力。」不再由前端硬编码(registry 句尾已含)',
    `实得 ${noChargeInJsx} 处`);
ok(/planError\.userSentence/.test(page), 'B1 配对臂:服务端那一句仍在渲染(否则等于把提示删了)');

// ── C 修复动作臂 ────────────────────────────────────────────────────
ok(/data-testid="plan-error-fix"/.test(page), 'C1 planError 带一个可点的修复按钮(:83 提示二选一)');
ok(/onClick=\{applyPlanErrorFix\}/.test(page), 'C2 按钮接到修复动作,不是死按钮');
ok(/nextActionLabel \?\? /.test(page), 'C3 文案优先服务端 label,前端只兜底(§15.8)');

// ── D 补位可见臂 ────────────────────────────────────────────────────
ok(/suggested=\{suggestedWithFiller\}/.test(page),
    'D1 补位题进**渲染源** —— 看不见的题不能进计价');
ok(/const defensiveDraft: DraftQuestion\[\] = \[\.\.\.suggestedWithFiller/.test(page),
    'D2 送出源与渲染源同一个数组(题数/价格必然一致)');
ok(!/suggested=\{suggestedDefensive\}/.test(page),
    'D3 反臂:不得再把未含补位的旧数组传给编辑器');
ok(/isFiller: true/.test(page), 'D4 补位题打了 isFiller 标记(「系统补位」徽标据此渲染)');

// ── E dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ E0 **未构建 ⇒ 未评估**:没有 dist,产物锚一条都没验。'
        + '「跳过」不是「通过」——退出码必须分得开。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    // 🔴 **有序**,不是并列:必不中臂的判别力**寄生在**必中臂上。
    //    若产物根本没被提取到(旧 dist / 读成压缩字节 / 路径不对),
    //    「编造锚不命中」也会绿 —— 而绿的原因是"什么都没提取到"。
    //    所以必中臂不过就**不评估**必不中臂,并明说尺子不可用。
    const e1 = js.includes('plan-error-fix');
    const e2 = js.includes('plan-side-inline-hint') && js.includes('question-filler-badge');
    ok(e1, 'E1 修复按钮的锚在产物里(源码有 ≠ 产物有)');
    ok(e2, 'E2 内联提示 + 「系统补位」徽标的锚都在产物里');
    if (e1 && e2) {
        ok(!js.includes('zzq-必然不存在的锚'), 'E3 反臂:编造的锚必须不命中');
    } else {
        notEvaluated += 1;
        console.log('  ⚠️ E3 未评估:E1/E2 未成立 ⇒ 提取可能整体为空,'
            + '此时「编造锚不命中」不携带任何信息(尺子可用性未知)');
    }
}

// 🔴 三态退出码:0 全过 / 1 有失败 / 3 有未评估。
//    「未评估」独立成一档 —— 下一个人(含 Deploy 预检)只看退出码,
//    而把「跳过」印成绿是今天已经栽过的形状。
if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估(要绿必须先构建 / 让 A 组真 import)`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
