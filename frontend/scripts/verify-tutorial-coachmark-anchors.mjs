#!/usr/bin/env node
/**
 * 判据 · 教程 coach mark「指着的控件还在」类门 —— #190 G1(源码层)
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 来由:#176(沙盒价格错,教程死了七天到现场演示才暴)与 #180-B
 * (`step2-online-form` 零消费者,撤个控件就把第二步的引导删光)是**同一类**:
 * 教程状态机推到某阶段,页面上没有任何东西消费它;或消费者指的锚点已被改掉。
 * 在本门之前,全仓唯一守这件事的是 E7 —— 一条为**单个实例**手写的反臂。
 *
 * 🔴 本门的分母来自 `enumerate-tutorial-stages.mjs`(#190 §0)。
 *    那个枚举器**必须先跑通并与手工抽查对上**:上一轮我按一种写法枚举,
 *    报出 7 个假阳性;这一轮又抽查出一种漏掉的**泛型消费**
 *    (侧栏契约按 stage 查表,源码里根本没有字面比较)。
 *    分母窄一点,这门就会变成一个喊狼来了的仪器。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
let bad = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

/**
 * 🔴 **已知的、有理由的例外**。每一条必须写清:为什么、哪一笔改的、该谁清。
 *    没有理由的条目 = 把门关掉。G1d 会检查每条例外都带理由。
 *
 * 🔴 **现在是空的,而且 G1c 只许它空**(#191 · 2026-09-13)。
 *    唯一那条例外(`step3-pub-await-process` 一族)已经清掉:消费者删了、
 *    三个阶段从 `TutorialStage` 与 `VALID_STAGES` 退役。
 *    往这里加新条目 = 关门 —— 新的死引导要么当场修掉,要么单独立卡,
 *    并且**同一笔**把 G1c 的上限从 0 抬到 1 并写清理由与责任人,让抬闸这件事在 diff 里看得见。
 */
const KNOWN_DEAD_GUIDANCE = {};

console.log('G #190 教程 coach mark 锚点类门');

// ── 取分母 ────────────────────────────────────────────────────────
let data;
try {
    const out = execFileSync(process.execPath,
        [join(ROOT, 'scripts/enumerate-tutorial-stages.mjs'), '--json'],
        { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
    data = JSON.parse(out);
} catch (e) {
    console.log('  FAIL G0 🔴 枚举器跑不起来 ⇒ 没有分母,本门无意义:'
        + String((e && e.message) || e).split('\n')[0]);
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

const { allStages, S, C, sMinusC, cMinusS } = data;
ok(allStages.length >= 40,
    'G0a 分母自证:阶段联合里读到了全部阶段(读空的话下面每条都恒真)',
    `${allStages.length} 个`);
ok(Object.keys(S).length >= 30 && Object.keys(C).length >= 30,
    'G0b 分母自证:两张表都非空', `|S|=${Object.keys(S).length} |C|=${Object.keys(C).length}`);

// ── G1a  S−C:推得到却没人消费 ⇒ 教程走到这儿没有下文 ────────────────
{
    const unexpected = sMinusC.filter((k) => !KNOWN_DEAD_GUIDANCE[k]);
    ok(unexpected.length === 0,
        'G1a 🔴 没有「推得到、却没人消费」的阶段 —— 有的话教程会走到那儿然后**没有下文**'
        + '(#180-B 的 `step2-online-form` 就是这一格:撤个控件就把第二步的引导删光)',
        unexpected.length ? unexpected.map((k) => hum(k)).join(' / ') : '0 个');
}

// ── G1b  C−S:有人读却从不产生 ⇒ 死引导 ──────────────────────────────
{
    const unexpected = cMinusS.filter((k) => !KNOWN_DEAD_GUIDANCE[k]);
    ok(unexpected.length === 0,
        'G1b 🔴 没有「有人读、却从不产生」的阶段 —— 有的话那段引导**永远不会出现**,'
        + '而代码看起来是活的(下一个人会照着它改)',
        unexpected.length ? unexpected.map((k) => hum(k)).join(' / ') : '0 个');
    for (const k of cMinusS.filter((k) => KNOWN_DEAD_GUIDANCE[k])) {
        console.log(`  ..   已知例外 ${hum(k)} — ${KNOWN_DEAD_GUIDANCE[k].reason}`);
        console.log(`       该谁清:${KNOWN_DEAD_GUIDANCE[k].owner}`);
    }
}

// ── G1c 例外表不许偷偷变长/变哑 ────────────────────────────────────
{
    const keys = Object.keys(KNOWN_DEAD_GUIDANCE);
    ok(keys.length === 0,
        'G1c 🔴 已知例外**必须为零**(#191 把最后一条清了)。加回任何一条就是在关门 ——'
        + '新的死引导要么当场修掉,要么单独立卡并在同一笔里把这个上限抬起来、写清理由与责任人',
        `${keys.length} 条:${keys.join(',') || '(空)'}`);
    const noReason = keys.filter((k) => {
        const v = KNOWN_DEAD_GUIDANCE[k];
        return !v.reason || !v.owner || !v.since || v.reason.length < 20;
    });
    ok(noReason.length === 0,
        'G1d 每条例外都带**理由 + 哪一笔改的 + 该谁清** —— 没有理由的例外等于把门关掉',
        /* 🔴 例外表为空时本条**空跑**,别把它读成"验过了"(零条恒真)。
           真正钉住"零条"的是 G1c;G1d 只在有人抬闸加例外之后才开始干活。 */
        keys.length === 0 ? '例外表为空 ⇒ 本条空跑(零条恒真)· 钉零条的是 G1c'
            : (noReason.length ? noReason.join(',') : '都带了'));
}

// ── G1e 每个消费者的锚点在源码里真的存在(不是字符串出现) ──────────────
{
    /*
     * 🔴 锚点有两种写法:
     *   ① `targetSelector='[data-sandbox-coach-anchor="X"]'` —— 必须有元素带这个属性;
     *   ② FeatureTooltip 直接**包着**目标(没有 targetSelector)—— 锚点由构造保证。
     * 这里只能查 ①(② 靠 G2 在浏览器里验可见性)。
     */
    const src = readFileSync(join(ROOT, 'src/pages/Writing/WritingHall.tsx'), 'utf8');
    const all = walkSrc();
    const wanted = [...all.matchAll(/targetSelector=['"`]\[data-sandbox-coach-anchor="([^"]+)"\]/g)]
        .map((m) => m[1]);
    ok(wanted.length >= 1, 'G1e0 分母自证:找到了用 targetSelector 的 coach mark',
        `${wanted.length} 个:${wanted.join(',')}`);
    /*
     * 🔴 查"锚点存在"之前必须先把 **targetSelector 自己**从文本里剔掉。
     *    `targetSelector='[data-sandbox-coach-anchor="X"]'` 这一行本身就含有
     *    `data-sandbox-coach-anchor="X"` —— 不剔掉的话,**选择器自己满足了自己**,
     *    把元素上的属性改名它照样绿。注毒 G3b 当场把这条照出来了。
     *    (同族:a-string-anchor-satisfied-by-an-unrelated-line)
     */
    const elementsOnly = all.replace(/targetSelector=['"`]\[[^\]]*\]['"`]/g, '');
    /*
     * 🔴 锚点的赋值有**两种写法**,只认第一种会误报:
     *   ① 字面属性:`data-sandbox-coach-anchor="writing-generate-titles"`
     *   ② **动态**:`data-sandbox-coach-anchor={cond ? 'writing-expand-titles' : undefined}`
     *  我第一版只认 ①,于是把 ② 报成"锚点不存在" —— 又一次**分母比现实窄**
     *  (这张卡里第三次了:消费写法、泛型查表、现在是动态赋值)。
     *  改成:剔掉 targetSelector 自身之后,看这个名字**是否还以字符串形式出现过**。
     *  松一点但不空:注毒把元素上的值改名,名字就从 elementsOnly 里消失 ⇒ 红。
     */
    const missing = wanted.filter((name) =>
        !new RegExp(`["'\`]${name}["'\`]`).test(elementsOnly));
    ok(missing.length === 0,
        'G1e 🔴 每个 targetSelector 指的锚点**真的存在** ——'
        + '改个 testid 名字就会让 spotlight 指着空气,而代码不报错',
        missing.length ? missing.join(',') : '全部对得上');
    void src;
}

/** 把 stage 名翻成人话:门的输出要说「第几步 · 页面 · 指着什么」。 */
function hum(stage) {
    const step = /^step2/.test(stage) ? '第二步(报价)'
        : /^step3-pub/.test(stage) ? '第三步(发布)'
            : /^step3/.test(stage) ? '第三步(写文章)'
                : /^step4/.test(stage) ? '第四步(监测)'
                    : '第一步(体检)';
    return `${step}·${stage}`;
}

function walkSrc() {
    const out = [];
    (function w(dir) {
        for (const n of readdirSync(dir)) {
            const p = join(dir, n);
            if (statSync(p).isDirectory()) w(p);
            else if (/\.(ts|tsx)$/.test(n)) out.push(readFileSync(p, 'utf8'));
        }
    })(join(ROOT, 'src'));
    return out.join('\n');
}
// ── G2 · 可达性:阶段的处理点不许躲在「永不执行的函数」里 ────────────────
//
// 🔴 [#234-a2 2026-09-17] 上面 G1a/G1b 数的是**源码引用**,所以一个待在
//    **零调用点函数**里的消费点,静态上照样算消费点,两张表照样配平。
//    生产上实测到的后果:`WritingHall.renderKnowledgePanel`(391 行,全仓命中 1 = 只有定义)
//    里装着 `step3-upload-kb` 的全部消费点与 `step3-gen-titles` 的唯一生产点,
//    而把用户推进该阶段的 `setTutorialStage('step3-upload-kb')` 在**活代码**里
//    ⇒ **沙盒教程第三步在生产上就是断的**,而这门一直是绿的。
//
// 🔴 「存在 ≠ 可达」在本仓这是第三次(#143 的 price_preview_id 躺在 if 块里 /
//    #222 的编辑入口躺在 `{false && …}` 里 / 这一件)。前两次靠人发现,
//    这一格是第一次把它变成机械拦截。
//
// 判别式(Review 点名):**所在函数有没有调用点**。不完美 —— 它看不见
//    「函数被调用但那个分支永不进入」,但能抓住这一整类;判别式的盲区写在这儿,
//    比装作没有盲区强。
{
    const stageRe = new RegExp("'(" + allStages.map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ")'");
    const srcFiles = [];
    (function w(dir) {
        for (const n of readdirSync(dir)) {
            const p = join(dir, n);
            if (statSync(p).isDirectory()) w(p);
            else if (/\.(ts|tsx)$/.test(n) && n !== 'tutorialStage.ts') srcFiles.push(p);
        }
    })(join(ROOT, 'src'));

    let scanned = 0;
    const offenders = [];
    for (const p of srcFiles) {
        const text = readFileSync(p, 'utf8');
        if (!stageRe.test(text)) continue;          /* 便宜的预筛:这个文件里根本没有阶段常量 */
        scanned += 1;
        const lines = text.split('\n');
        for (let i = 0; i < lines.length; i += 1) {
            const m = lines[i].match(/^\s*(?:const|function)\s+([A-Za-z_$][\w$]*)\s*[=(]/);
            if (!m) continue;
            const name = m[1];
            /* 函数体:从定义行起做大括号配对 */
            let depth = 0; let end = -1;
            for (let j = i; j < lines.length; j += 1) {
                depth += (lines[j].match(/\{/g) || []).length - (lines[j].match(/\}/g) || []).length;
                if (j > i && depth <= 0) { end = j; break; }
            }
            if (end < 0 || end - i < 3) continue;   /* 取不到体 / 太短,不是这类 */
            const body = lines.slice(i + 1, end).join('\n');
            if (!stageRe.test(body)) continue;      /* 体里没有阶段常量,与本格无关 */
            /* 调用点:全仓出现次数减去它自己的定义那一次 */
            const uses = srcFiles.reduce((n, q) => n
                + ((readFileSync(q, 'utf8').match(new RegExp('\\b' + name + '\\b', 'g')) || []).length), 0);
            if (uses <= 1) {
                offenders.push(`${p.slice(ROOT.length + 1).split('\\').join('/')}:${i + 1} ${name}()`
                    + ` 零调用点,却装着阶段 ${(body.match(stageRe) || [])[1]}`);
            }
        }
    }
    ok(scanned > 0,
        'G2a 分母自证:确实扫到了含阶段常量的文件(扫到 0 个的话下面那格恒真)',
        `${scanned} 个文件`);
    ok(offenders.length === 0,
        'G2 🔴 阶段的处理点都在**可达代码**里 —— '
        + '躲在零调用点函数里的处理点,静态上配得平、运行时永不执行,'
        + '教程会走到那一步然后卡死,而 G1a/G1b 看不见',
        offenders.join(' / ') || '零处');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过:教程阶段两张表无缺口(已知例外见上面 .. 行)');
process.exit(0);
