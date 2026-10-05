#!/usr/bin/env node
/**
 * 判据 · 包一阶段⑤a 服务端价唯一化(订正七④ / 订正二十一 / #84 §3)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 本笔只做**价**那一半:删前端镜像 + 只用 POST 取价 + 绑 pricePreviewId +
 * 价没就绪不许确认。「删旧关键词框 + 题单→keywords 映射」是 ⑤b,
 * 它会牵动 PPTX 文案的跨文件锁(裁定 §4),与本笔不同批 —— 写在这里免得被当成漏做。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const PREV = strip(rd('src/pages/Diagnosis/launch/diagnosisPricePreview.ts'));
const GATE = strip(rd('src/pages/Diagnosis/launch/defensiveLaunchGate.ts'));

// ── A 🔴 前端不算钱:镜像必须消失,且不许有替代物 ────────────────────
ok(!/customExtraCost/.test(PAGE), 'A1 🔴 前端计价镜像 customExtraCost 已删');
ok(/const totalDiagnosisCost = pricePreview\?\.points \?\? null;/.test(PAGE),
    'A2 唯一的价直接来自服务端响应的 points,没有中间加工');
for (const [n, src] of [['PAGE', PAGE], ['PREV', PREV]]) {
    ok(!/EXTRA_POINTS_PER_QUESTION|\*\s*100\b|FREE_CUSTOM_QUESTIONS/.test(src),
        `A3 反臂 ${n}:没有任何加价算术(每题 100 / 免费 8 这类常量一个都不许出现)`);
}
ok(!/points\s*[+\-*/]/.test(PAGE) && !/points\s*[+\-*/]/.test(PREV),
    'A4 反臂:没有对 points 做算术');

// ── B 🔴 只用 POST,GET 不进前端 ─────────────────────────────────────
/**
 * GET 恒 `ai_optimized=False`,POST 用真实开关 ⇒ 双轨开时 GET 显 650、POST 绑 950。
 * 用 GET 显价就是「看到 650 扣 950」。订正二十一:GET 不进前端。
 */
ok(/method: 'POST'/.test(PREV), 'B1 取价用 POST');
ok(!/questionCount=/.test(PREV) && !/questionCount=/.test(PAGE),
    'B1 🔴 反臂:前端**不出现** GET 的 `questionCount=` 查询串 —— GET 不进前端');
ok((PREV.match(/\/api\/pricing\/diagnosis-preview/g) || []).length >= 1,
    'B2 端点路径正确(/api/pricing/…,不是被 {id} 吞掉的那条旧路径)');
ok(!/\/api\/diagnosis\/price-preview/.test(PREV) && !/\/api\/diagnosis\/price-preview/.test(PAGE),
    'B2 反臂:旧路径不再出现(它与 /api/diagnosis/{id:int} 同前缀,靠注册顺序才不被吞)');

// ── C 🔴 绑住「看到的价 == 提交的题集 == 扣的钱」──────────────────────
/**
 * 🔴 [#143① 2026-09-07] **C1 已搬走,不在这里留空壳。**
 *
 * 原来这里是:`ok(/payload.price_preview_id = .../.test(PAGE), 'C1 提交体带 price_preview_id')`
 * —— 一条**存在性**判定。那一行确实一直存在,只是躺在 `if (自定义题数 > 0)` 里,
 * 0 道题时根本不执行,而服务端无条件要它 ⇒ 增长模式必 400。判据全绿,缺陷上线,
 * Owner 在真客户品牌上撞到。**存在 ≠ 可达**(五层病谱第 ④ 层)。
 *
 * 接替它的是 `verify-p0-preview-id-and-suggest.mjs` 的 A 段:判「赋值**不在**那个条件块的
 * 块体内」(大括号配对),并配合成样本反向对照。那把锁**严格强于**这一条,
 * 所以这里**删掉**而不是并存 —— 同一个谓词两处,必有一处没人维护。
 * 新址已进 `npm run build` 链,不是挂在没人跑的地方。
 */
/**
 * 🔴 C2 原写成文件级存在锁 —— 而取价有**两个**调用点(首次取价的 effect + 409 后重取)。
 *    只把其中一处换成别的题集,全文件仍匹配 ⇒ 仍绿,而那一支会每次 409。
 *    改成计数:两处都必须用同一份题集。
 */
const Q_HITS = (PAGE.match(/questions: ownQuestions\.unique/g) || []).length;
ok(Q_HITS === 2,
    `C2 🔴 取价的**每一处**都用提交体那份题集(实得 ${Q_HITS} 处:首次取价 + 409 重取)`);
/**
 * 🔴 [#143① 2026-09-07 重锚] 原来这条只往**后**看 600 字符 ——
 *    它默认了 `price_preview_id` 排在 `custom_questions` 之后。
 *    #143① 把赋值挪到条件块**之前**(它必须无条件执行),于是这条锁红了 ——
 *    **红的是方向假设,不是缺陷**。这类「判据与正确做法互斥」的红最危险:
 *    照着它改回去就等于把缺陷放回来。
 *    意图(两者相邻、肉眼可比对)保留,改成**双向**邻近。
 */
const payloadIdx = PAGE.indexOf('payload.custom_questions = ownQuestions.unique');
const previewIdx = PAGE.indexOf('payload.price_preview_id =');
ok(payloadIdx >= 0 && previewIdx >= 0 && Math.abs(payloadIdx - previewIdx) <= 1200,
    'C2 配套:两者相邻(任一方向),肉眼可比对 —— 服务端拿 custom_questions 去比这个 id 的哈希',
    `间距 ${payloadIdx >= 0 && previewIdx >= 0 ? Math.abs(payloadIdx - previewIdx) : 'n/a'} 字符`);
ok(/err\?\.response\?\.status === 409/.test(PAGE),
    'C3 409(题集或价又变了)⇒ 当场重取新价,不让她自己猜要刷新');

// ── D 🔴 价没就绪不许确认(继承 launch-confirm-blocked-stale 语义)────
const g = await import(new URL('../src/pages/Diagnosis/launch/defensiveLaunchGate.ts', import.meta.url).href)
    .catch(() => null);
if (!g) {
    notEvaluated += 1;
    console.log('  ⚠️ D0 **未评估**:没能 import defensiveLaunchGate.ts,行为锁没跑。');
} else {
    const base = {
        mode: 'offensive', isFormComplete: true, totalDiagnosisCost: 650,
        defensiveQuestionCount: 0, hasBrandId: true, loading: false, planPending: false,
    };
    ok(g.isLaunchBlocked({ ...base, priceNotReady: false }) === false,
        'D1 正样本臂:价就绪时同一份输入可以启动(否则下一条不携带信息)');
    ok(g.isLaunchBlocked({ ...base, priceNotReady: true }) === true,
        'D2 🔴 行为锁:只把 priceNotReady 翻成 true ⇒ **真的挡住**');
}
ok(/const priceNotReady = pricePending \|\| !priceMatchesInput\(pricePreview, customQuestionCount\);/.test(PAGE),
    'D3 就绪判定 = 没在飞 且 回来的那份对得上当前题数');
ok(/p\.questionCount === Math\.max\(0, currentQuestionCount \| 0\)/.test(PREV),
    'D3 配套:对不对得上比的是**服务端回显的 questionCount** —— 用响应自己带的数,'
    + '才不会把上一次的价当成这一次的');
ok(/if \(priceNotReady\) return '正在按你现在的题单重新算价/.test(PAGE),
    'D4 灰按钮自带人话(顺序 = 拦截判定顺序)');
ok(/pricePending \? '正在按你的题单算价…'/.test(PAGE),
    'D5 「正在重新算」是**按钮自己**的瞬态,不是挂着一个上一份输入的旧价');

// ── E 取不到价不显示数字 ────────────────────────────────────────────
ok(/kind: 'unavailable'/.test(PREV) && /没有扣除任何算力/.test(PREV),
    'E1 503 与网络失败都明说「没有扣除任何算力」');
ok(/typeof d\.points !== 'number' \|\| typeof d\.pricePreviewId !== 'string'/.test(PREV),
    'E2 🔴 响应缺字段 ⇒ 当作没拿到价;**不拿 estimate 顶替 points**(它们同源,但顶替会掩盖响应异常)');

// ── F 🔴 红臂:基线上都不成立 ────────────────────────────────────────
const BASE = 'e19fe1496';
let basePage = null;
try {
    basePage = execFileSync('git', ['show', `${BASE}:frontend/src/pages/Diagnosis/NewDiagnosis.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ F0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (basePage) {
    ok(/const customExtraCost =/.test(basePage), 'F1 🔴 红臂:改动前那面前端算价的镜子确实在');
    ok(/customQuestionCount \* 100/.test(basePage), 'F2 红臂:改动前前端确实在做加价乘法');
    ok(!/price_preview_id/.test(basePage), 'F3 红臂:改动前提交体没有 price_preview_id');
    ok(/totalDiagnosisCost/.test(basePage),
        'F4 配对臂:基线里本来就有的锚确实在(否则 F3 的"没有"只是取到空内容)');
}

// ── G dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ G0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const g1 = js.includes('/api/pricing/diagnosis-preview');
    ok(g1, 'G1 取价端点进了产物');
    ok(js.includes('launch-price-points'), 'G2 按钮显价锚进了产物');
    if (g1) ok(!js.includes('zzq-必然不存在的锚'), 'G3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ G3 未评估:G1 未成立 ⇒ 提取可能整体为空'); }
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
