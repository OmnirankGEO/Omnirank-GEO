#!/usr/bin/env node
/**
 * 判据 · #178 客户选词页「全部排除」死胡同。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 **缺陷形态**(Owner 09-12 图 1):客户在 `/s/<token>` 选了业务方向,方向下的词被
 *    `_partition_delivery_exclusions` 全部排除 ⇒ 后端 400 ⇒ 前端 `toast.error(detail)`。
 *    客户看到一句「请联系报价方补充商业选型问题」然后**什么都做不了** ——
 *    页面上既没有"联系报价方"这个动作,也进不到有自定义词入口的下一步。
 *    治理 SSOT :133-134 原文:不得是**无出口的全局拒绝**。
 *
 * 🔴 **本单最容易长出来的假绿**(所以 B 段是真调函数,不是 grep):
 *    ① 卡片画出来了,但那张卡上的「提交」打的还是 submit-business-lines ⇒
 *       再提交一遍业务方向 = 一模一样的全排除结果 ⇒ 从死胡同变成死循环,
 *       而"卡片在不在"的锁全绿;
 *    ② 绿条「已提交,正在准备方案」照挂 ⇒ 比原来的死胡同更有欺骗性
 *       (客户安心去等一个永远不来的方案),而"卡片在不在"的锁也全绿;
 *    ③ 卡片只活在 POST 响应的 React state 里 ⇒ 客户一刷新就没了(GET 不带那几个字段)。
 *    这三条都是「结构在、语义反」,只有真跑判定函数 + 钉住调用边才看得见。
 *
 * 🔴 **分母**:前端机械枚举,不用工单给的条数。
 *
 * 与 C 的分工:后端那五条(200 不再 400 / brand_name 传参 / 通知幂等 / 不落库 / 服务商侧回归)
 * 在 C 的 pytest 里。本文件只读 `frontend/`,一个跨层字都不读 —— 越界闸的理由是对的。
 * 跨层那条边(前端发的 `notify-no-deliverable` 必须是后端真有的端点、submit-keywords 必须
 * 允许 `business_lines_submitted` 态)见交付物 §10,归 C 的包。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
let notEvaluated = 0;
const failed = [];
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) { bad += 1; failed.push(label.split(' ')[0]); }
};

const PAGE = 'src/pages/Selection/SelectionPage.tsx';
const CARD = 'src/pages/Selection/components/AllExcludedExitCard.tsx';
const BAR = 'src/pages/Selection/components/BottomActionBar.tsx';
const MOD = 'src/pages/Selection/utils/allExcludedExits.ts';

/**
 * 去注释但**保行号**。数的是代码,不是我自己写的解释 ——
 * 本窗已经被「字符串锚命中注释」咬过五次以上。
 */
const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

// ══ M 元判据:先证仪器能用(不可用就报红,**不许 SKIP**) ═════════════════
let ts;
try {
    ts = require_('typescript');
} catch (e) {
    console.log('  FAIL M0 🔴 取不到 typescript,判定函数一条都跑不了:' + String((e && e.message) || e));
    console.log('       修法:cd frontend && npm ci --legacy-peer-deps');
    console.log('       🔴 不要给它加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

let M;
try {
    const src = rd(MOD);
    const js = ts.transpileModule(src, {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a178-'));
    const file = join(tmp, 'allExcludedExits.mjs');
    writeFileSync(file, js, 'utf8');
    M = await import(pathToFileURL(file).href);
} catch (e) {
    console.log('  FAIL M1 🔴 判定模块 import 不进来(它必须是零 import 的纯函数):'
        + String((e && e.message) || e));
    process.exit(1);
}
ok(typeof M.readAllExcluded === 'function'
    && typeof M.reconcileFromPage === 'function'
    && typeof M.submitGateState === 'function'
    && typeof M.shouldClaimSubmitted === 'function'
    && typeof M.notifyState === 'function'
    && typeof M.rewriteExamples === 'function',
    'M2 元判据:六个判定函数都真的导出了(下面 B 段是真调用它们)');

// ══ A 分母:两个提交面都得接上,一个都不许漏 ═══════════════════════════
console.log('\nA 分母(机械枚举)');
const pageRaw = rd(PAGE);
const page = decomment(pageRaw);
{
    const submitSites = [...page.matchAll(/fetch\(`\/api\/s\/\$\{token\}\/(submit-[a-z-]+)`/g)]
        .map((m) => m[1]);
    ok(submitSites.length === 2,
        'A1 客户页的提交端点恰好 2 个(工单只点了这两个;多出来的第三个说明分母变了,回来重判)',
        submitSites.join(' '));
    // 每个 res.ok 分支都必须问一次「是不是全排除」——漏一个就是那一面仍然死着
    const parseCount = (page.match(/readAllExcluded\(/g) || []).length;
    ok(parseCount >= submitSites.length,
        'A2 🔴 每个提交面的 200 分支都解析了 all_excluded(漏一面 = 那一面照旧死胡同)',
        `readAllExcluded 调用 ${parseCount} 处 / 提交面 ${submitSites.length} 个`);
    // 反臂:别把 all_excluded 从计数推出来 —— 正常提交也会带 delivery_excluded_keywords
    ok(!/all_excluded[^\n]*length\s*>\s*0/.test(page)
        && !/delivery_excluded_keywords[^\n]*\.length\s*>\s*0[^\n]*allExcluded/.test(page),
        'A3 反臂:allExcluded 不由「排除条数>0」推出来(正常提交也会带排除条目,'
        + '推出来 ⇒ 每次部分排除都误弹出口卡)');
}

// ══ B 判定函数真跑(行为臂 · 状态矩阵) ════════════════════════════════
console.log('\nB 判定函数真跑');
const ALL_EXCLUDED_BODY = {
    all_excluded: true,
    status: 'business_lines_submitted',
    // 🔴 C 09-12 更正:submit-business-lines 的 selected_count 数的是**业务方向数**(≥1),
    //    恒 0 的是 keywords_auto_selected。夹具照真契约写 —— 若有人日后拿
    //    `selected_count === 0` 当"全排除"的判据,B1 会当场红。
    selected_count: 1,
    keywords_auto_selected: 0,
    delivery_excluded_keywords: [
        { id: 11, keyword: '钢琴怎么保养', reason: '知识类问法', kind: 'knowledge_term_not_deliverable', policy_version: 'v1.0' },
        { id: 12, keyword: '钢琴有几个键', reason: '百科类问法', kind: 'knowledge_term_not_deliverable', policy_version: 'v1.0' },
        { id: 13, keyword: '钢琴', reason: '裸词无法判断意图', kind: 'needs_clarification', policy_version: 'v1.0' },
    ],
    next_action: { kind: 'add_commercial_keywords', notify_sent: true },
};
{
    const s = M.readAllExcluded(ALL_EXCLUDED_BODY);
    ok(s.allExcluded === true && s.status === 'business_lines_submitted',
        'B1 读出全排除态 + status 原样透传(不与固定 status 绑死,C 09-12 已警告过)',
        `status='${s.status}'`);
    ok(s.total === 3 && s.notDeliverable.length === 2 && s.needsClarification.length === 1,
        'B2 🔴 两类 kind 分开(needs_clarification 澄清后可重提,另一类不可改选;'
        + '混一起会让客户对着不可改的词反复改)',
        `total=${s.total} 不可交付=${s.notDeliverable.length} 需澄清=${s.needsClarification.length}`);
    ok(s.notifySent === true && s.nextActionKind === 'add_commercial_keywords',
        'B3 next_action 逐字段读出(notify_sent 决定出口②直接显示「已通知」还是给按钮)');

    // 自定义词那份(submit-keywords 才有,无 id)也要并进来
    const s2 = M.readAllExcluded({
        all_excluded: true, status: 'selecting',
        delivery_excluded_keywords: [],
        delivery_excluded_custom_keywords: [
            { keyword: '钢琴是什么', reason: '知识类', kind: 'knowledge_term_not_deliverable' },
        ],
        next_action: { kind: 'add_commercial_keywords', notify_sent: false },
    });
    ok(s2.total === 1 && s2.notDeliverable.length === 1 && s2.notifySent === false,
        'B4 自定义词的排除清单(无 id 那份)也进卡片 —— 客户刚写的词被砍了必须看得见');

    // 反臂:正常提交(带部分排除、无 all_excluded)绝不能立起卡片
    const s3 = M.readAllExcluded({
        status: 'keywords_submitted',
        delivery_excluded_keywords: [{ id: 9, keyword: '钢琴怎么保养', reason: '知识类', kind: 'knowledge_term_not_deliverable' }],
    });
    ok(s3.allExcluded === false,
        'B5 🔴 反臂:部分排除(有排除条目但无 all_excluded)**不**进出口卡态 ——'
        + '否则每一次正常提交都弹一张"死胡同"卡');

    // 🔴 C 09-12 更正③:POST 在非全排除时 next_action 是 **null**(键在、值为 null),
    //    而 GET 非全排除时**键根本不在**。两边形状不同是故意的 —— 两种都不许炸。
    const s4 = M.readAllExcluded({ status: 'keywords_submitted', next_action: null, delivery_excluded_keywords: [] });
    ok(s4.allExcluded === false && s4.notifySent === false && s4.nextActionKind === '',
        'B5b next_action=null(POST 非全排除的真形状)读得动且不进新态');

    // 🔴 selected_count 不是"空"的判据:submit-business-lines 那边它数的是业务方向数(≥1)。
    //    这一格钉住"我没拿它当判据" —— 拿它判会把正常的单方向提交误判成全排除。
    const s5 = M.readAllExcluded({ ...ALL_EXCLUDED_BODY, selected_count: 3 });
    ok(s5.allExcluded === true,
        'B5c selected_count 取值不影响判定(它在两个端点上量纲不同,不能当"空"的判据)');

    // 垃圾输入不许抛:客户页白屏比死胡同更糟
    let threw = '';
    try {
        M.readAllExcluded(null);
        M.readAllExcluded(undefined);
        M.readAllExcluded('nope');
        M.readAllExcluded({ all_excluded: true, delivery_excluded_keywords: 'not-an-array', next_action: 7 });
        M.readAllExcluded({ all_excluded: true, delivery_excluded_keywords: [null, {}, { keyword: 'x' }] });
    } catch (e) { threw = String((e && e.message) || e); }
    ok(threw === '',
        'B6 边界:null / 字符串 / 错类型 / 缺字段都不抛(客户页白屏比死胡同更糟)', threw || '零异常');
    const sJunk = M.readAllExcluded({ all_excluded: true, delivery_excluded_keywords: [null, {}, { keyword: 'x' }] });
    ok(sJunk.total === 1,
        'B7 边界:没有 keyword 的空行被丢掉,不渲染成一条空 bullet', `total=${sJunk.total}`);
}

// B8 🔴 刷新存活:这是"卡片闪一下就没了"那个缺陷的锁
console.log('  -- B8 刷新存活矩阵(reconcileFromPage) --');
{
    const prev = M.readAllExcluded(ALL_EXCLUDED_BODY);
    const cells = [
        ['GET 不带 all_excluded(后端补字段之前) · 已有卡片', prev, { status: 'business_lines_submitted', keywords: [] }, 'keep'],
        ['GET 不带 all_excluded · 本来没有卡片', null, { status: 'selecting' }, 'null'],
        ['GET 明说 all_excluded=true', null, { all_excluded: true, status: 'business_lines_submitted', delivery_excluded_keywords: ALL_EXCLUDED_BODY.delivery_excluded_keywords, next_action: { kind: 'add_commercial_keywords', notify_sent: false } }, 'set'],
        ['GET 明说 all_excluded=false(客户已补词 ⇒ 卡片必须收掉)', prev, { all_excluded: false, status: 'keywords_submitted' }, 'null'],
    ];
    let allOk = true;
    const rows = [];
    for (const [name, p, body, want] of cells) {
        const out = M.reconcileFromPage(p, body);
        const got = out === null ? 'null' : (out === p ? 'keep' : 'set');
        const good = want === 'keep' ? out === p : (want === 'null' ? out === null : (out !== null && out !== p && out.allExcluded === true));
        rows.push(`${good ? '✓' : '✗'} ${name} ⇒ ${got}(要 ${want})`);
        if (!good) allOk = false;
    }
    for (const r of rows) console.log('     ' + r);
    ok(allOk,
        'B8 🔴 GET 没带字段时**保留**已有卡片、明说 false 时收掉 ——'
        + '无条件覆盖会让卡片被紧随其后的 fetchData 抹掉(客户一刷新就回到死胡同,且零报错)',
        `${cells.length} 格`);
}

// B8f GET 漏 reason 时不许退回"冤枉客户"那一档
{
    const prevNoKw = M.readAllExcluded({
        all_excluded: true, reason: 'no_keywords_for_lines', status: 'business_lines_submitted',
        delivery_excluded_keywords: [], next_action: { kind: 'add_commercial_keywords', notify_sent: false },
    });
    const merged = M.reconcileFromPage(prevNoKw, {
        all_excluded: true, status: 'business_lines_submitted', delivery_excluded_keywords: [],
    });
    ok(!!merged && merged.reason === 'no_keywords_for_lines',
        'B8f 🔴 GET 带了 all_excluded 却漏 reason ⇒ 保住已知那一档,不退回默认 ——'
        + '退回去说的是「这 0 个问法不会让 AI 推荐」,那是在冤枉客户(这个方向压根没配词)',
        merged ? merged.reason : 'null');
    // 反臂:GET **明确**给了 reason 就以 GET 为准(落库真相优先,不许被陈旧 prev 顶住)
    const overridden = M.reconcileFromPage(prevNoKw, {
        all_excluded: true, reason: 'all_excluded', status: 'selecting',
        delivery_excluded_keywords: [{ id: 1, keyword: '钢琴怎么保养', reason: '知识类', kind: 'knowledge_term_not_deliverable' }],
    });
    ok(!!overridden && overridden.reason === 'all_excluded',
        'B8g 反臂:GET 明确给了 reason ⇒ 以 GET 为准(否则这是个只进不出的粘滞状态)');
}

// B9 提交闸:disabled 与原因同生同死(整个矩阵扫不变量,不是钉一句文案)
console.log('  -- B9 提交闸矩阵(submitGateState) --');
{
    let cells = 0;
    let violated = [];
    let enabledWhenHasWords = null;
    let disabledWhenZero = null;
    for (const allExcluded of [true, false]) {
        for (const deliverableCount of [0, 1, 5]) {
            for (const submitting of [false, true]) {
                cells += 1;
                const g = M.submitGateState({ allExcluded, deliverableCount, submitting });
                // 🔴 承重不变量:点不动就必须有一句就地可见的原因
                if (g.disabled && !(g.reason && g.reason.length > 0)) {
                    violated.push(`allExcluded=${allExcluded} n=${deliverableCount} submitting=${submitting}`);
                }
                if (allExcluded && deliverableCount === 0 && !submitting) disabledWhenZero = g;
                if (allExcluded && deliverableCount === 2 && !submitting) enabledWhenHasWords = g;
            }
        }
    }
    ok(cells === 12, 'B9a 矩阵格数对(2 × 3 × 2)', `${cells} 格`);
    ok(violated.length === 0,
        'B9b 🔴 全矩阵不变量:disabled 为真 ⇒ 原因非空(灰掉又不说为什么 = 死按钮)',
        violated.length ? '违反:' + violated.join(' | ') : '12/12 满足');
    ok(!!disabledWhenZero && disabledWhenZero.disabled === true && /加一条|报价方/.test(disabledWhenZero.reason),
        'B9c 零可交付 ⇒ 禁用且原因里给出**下一步**(不是"不可提交"这种废话)',
        disabledWhenZero ? disabledWhenZero.reason : 'n/a');
    const g2 = M.submitGateState({ allExcluded: true, deliverableCount: 2, submitting: false });
    ok(g2.disabled === false,
        'B9d 客户补了词 ⇒ 闸放开(否则出口①通到一个永远点不动的按钮)');
    const g3 = M.submitGateState({ allExcluded: false, deliverableCount: 0, submitting: false });
    ok(g3.disabled === false && g3.reason === '',
        'B9e 反臂:不在全排除态时本函数零影响(老路径不许被我改语义)');
}

// B10 那条绿条(说的和做的一致)
{
    const lie = M.shouldClaimSubmitted({ status: 'business_lines_submitted', allExcluded: true, businessLinesSubmitted: true });
    const truth = M.shouldClaimSubmitted({ status: 'business_lines_submitted', allExcluded: false, businessLinesSubmitted: true });
    ok(lie === false,
        'B10a 🔴 全排除时不许挂「已提交 · 正在准备方案」—— 没有人在准备方案;'
        + '挂着它比死胡同更糟(客户会安心去等一个永远不来的方案)');
    ok(truth === true, 'B10b 反臂:正常提交后该挂的还挂(没把好路径一起掐掉)');
}

// B11 出口②按钮态
{
    const a = M.notifyState({ notifySent: true });
    const b = M.notifyState({ notifySent: false });
    const c = M.notifyState({ notifySent: false, alreadySent: true });
    const d = M.notifyState({ notifySent: false, inFlight: true });
    ok(a.done === true && a.disabled === true, 'B11a 后端已推过 ⇒ 直接显示已通知,不给重复点');
    ok(b.done === false && b.disabled === false && b.label.length > 0, 'B11b 没推过 ⇒ 按钮可点且有字');
    ok(c.done === true, 'B11c 这次点完了 ⇒ 转已通知态');
    ok(d.disabled === true && d.done === false, 'B11d 在途 ⇒ 禁点但不谎称已通知');
}

// B13 标题按 reason 换(Review 09-12 预告的增量;现在没有后端在发这个值)
{
    const t1 = M.cardTitle({ reason: 'all_excluded', total: 3 });
    const t2 = M.cardTitle({ reason: 'no_keywords_for_lines', total: 0 });
    const t3 = M.cardTitle({ reason: 'something-we-never-heard-of', total: 3 });
    ok(/不会让 AI 推荐/.test(t1), 'B13a 全排除版标题');
    ok(/还没有配好的问法/.test(t2) && !/不会让 AI 推荐/.test(t2),
        'B13b 🔴 "这个方向压根没配词"那版**不许**说成"你的问法不会让 AI 推荐" —— 那是冤枉客户',
        t2);
    // 🔴 标题与解释必须同源换:只换标题、留着"它们是知识/百科类问法"那句解释,
    //    等于半句真话 —— 比整句假话更难发现。
    const sub1 = M.cardSubtitle({ reason: 'all_excluded' });
    const sub2 = M.cardSubtitle({ reason: 'no_keywords_for_lines' });
    ok(/知识\/百科类问法/.test(sub1), 'B13d 全排除版的解释讲清为什么不收费');
    ok(/不是你写错了/.test(sub2) && !/知识\/百科类问法/.test(sub2),
        'B13e 🔴 没配词那版的解释也换掉(留着旧解释就是在继续怪客户写错词)', sub2);
    ok(M.cardSubtitle({ reason: 'unknown-value' }) === sub1,
        'B13f 认不出的 reason 解释也落回默认版');
    ok(t3 === t1,
        'B13c 认不出的 reason 落回默认版(客户页上冒出一个英文枚举比说错话更糟)');
}

// B14 主出口按 reason 换(C #178-B 契约:两档的补救动作不同)
{
    ok(M.primaryExit({ reason: 'all_excluded' }) === 'add_keywords',
        'B14a 有词但全被排除 ⇒ 主出口是「自己换个问法」(客户自己就能救)');
    ok(M.primaryExit({ reason: 'no_keywords_for_lines' }) === 'notify',
        'B14b 🔴 一条候选词都没生成 ⇒ 主出口是「让报价方补充」——'
        + '把"自己写一条"摆成主出口等于把配词这件事推给客户');
    ok(M.primaryExit({ reason: 'weird' }) === 'add_keywords', 'B14c 认不出的 reason 落回默认');
    // 卡片必须真的用它排序,且父容器得是 flex(order 只对 flex 子元素生效)
    const card = decomment(rd(CARD));
    ok(/const primary = primaryExit\(state\)/.test(card), 'B14d 卡片调了 primaryExit');
    ok(/flex flex-col rounded-2xl/.test(card),
        'B14e 🔴 出口容器是 flex —— order-1/order-2 只对 flex 子元素生效,'
        + '挂在普通 block 父元素上是一对**什么都不做**的类名(主出口顺序静默失效)');
    const o1 = (card.match(/order-1/g) || []).length;
    const o2 = (card.match(/order-2/g) || []).length;
    ok(o1 === 2 && o2 === 2,
        'B14f 两条出口各自带 order-1/order-2 两种可能(少一处 ⇒ 有一档排不动)',
        `order-1 ${o1} 处 · order-2 ${o2} 处`);
    ok(/data-testid="exit-add-keywords"/.test(card) && /data-testid="exit-notify"/.test(card),
        'B14g 两条出口都可被行为臂定位(缺锚 ⇒ 浏览器臂只能靠文案猜)');
}

// B12 改写示例不许是空卡
{
    const ex = M.rewriteExamples('韵宝钢琴', '钢琴培训');
    ok(ex.length >= 2 && ex.length <= 3 && ex.some((x) => x.includes('韵宝钢琴')),
        'B12a 示例带品牌名(policy :340-345:文本含品牌名 ⇒ BRAND_DIRECT ⇒ 可交付)',
        ex.join(' / '));
    const ex2 = M.rewriteExamples('', '');
    ok(ex2.length >= 2, 'B12b 边界:品牌名/品类都空也给得出示例(空卡片等于没出口)', ex2.join(' / '));
}

// ══ C 接线边(「我验了 A 也验了 B,但 A 调 B 那行没人验」) ═══════════════
console.log('\nC 接线边');
{
    ok(/import \{\s*AllExcludedExitCard\s*\} from '\.\/components\/AllExcludedExitCard'/.test(page),
        'C1 页面 import 了出口卡');
    const mountCount = (page.match(/\{allExcludedCard\}/g) || []).length;
    ok(mountCount === 2,
        'C2 🔴 出口卡在**业务线步与选词步各挂一处**(工单 §2e 要两步都有;'
        + '只挂一处 ⇒ 另一步照旧死胡同)',
        `挂载 ${mountCount} 处`);
    ok(/state=\{allExcluded\}/.test(page),
        'C3 卡片吃的是判定函数的产物(不是页面另算一份)');

    // 🔴 承重边:全排除态下那个提交键必须打 submit-keywords ——
    //    打回 submit-business-lines 就是死循环(结果一模一样),而"卡片在不在"全绿
    // 🔴 切片锚必须是**代码**,不是注释:上一版锚在 `// ===== Business Line Selection =====`
    //    上,而这段文本先过了 decomment —— 注释被刷成空格,indexOf 返回 -1,
    //    `slice(-1)` 切出最后一个字符,C4/C5 对着正确代码报红。
    //    (这次是红的所以被看见;同一个错切到别处就是假绿。)
    const blAt = page.indexOf('if (data.business_lines?.length)');
    ok(blAt !== -1, 'C4a 切得到业务线步分支(切不到 = 锚失效,下面两条不算数)');
    const bl = blAt === -1 ? '' : page.slice(blAt);
    const gateAt = bl.indexOf('{allExcluded ? (');
    ok(gateAt !== -1, 'C4b 业务线步里找得到全排除分支');
    const gateBlock = gateAt === -1 ? '' : bl.slice(gateAt, gateAt + 900);
    ok(/onClick=\{handleSubmitKeywords\}/.test(gateBlock),
        'C4 🔴🔴 全排除态的提交键打的是 handleSubmitKeywords(submit-keywords 允许态含 '
        + 'business_lines_submitted)—— 打回 handleSubmitBusinessLines 会拿到一模一样的'
        + '全排除结果,从死胡同变成死循环');
    ok(/disabledReason=\{submitGate\.reason\}/.test(gateBlock) && /disabled=\{submitGate\.disabled\}/.test(gateBlock),
        'C5 闸与原因是同一个函数的两个返回字段,一起传进去(分开传必有一处漂)');

    ok(/shouldClaimSubmitted\(\{/.test(page) && !/\{businessLinesSubmitted && \(/.test(page),
        'C6 🔴 绿条的渲染门换成 shouldClaimSubmitted,且**旧的裸 businessLinesSubmitted 门已不在** '
        + '(只加新门不撤旧门 = 旧门照挂)');

    const card = decomment(rd(CARD));
    ok(/import \{ CustomKeywordInput \} from '\.\/CustomKeywordInput'/.test(card)
        && /<CustomKeywordInput/.test(card),
        'C7 出口①的输入框就在卡片里(工单 §2e:就地内嵌,不是让客户自己找下一步)');
    ok(/onAdd=\{onAddKeyword\}/.test(card),
        'C8 输入框的 onAdd 接到页面的 handleAddCustom(接线断了就是死输入框:能打字,加不进去)');
    ok(/onClick=\{onNotify\}/.test(card),
        'C9 出口②的按钮接到通知 handler');

    const notifyBlock = page.slice(page.indexOf('handleNotifyNoDeliverable'));
    ok(/fetch\(`\/api\/s\/\$\{token\}\/notify-no-deliverable`/.test(notifyBlock),
        'C10 出口②打的是 C 契约里那个端点(端点名对不上 ⇒ 404 ⇒ 又是一个死按钮)');

    const bar = decomment(rd(BAR));
    ok(/const isDisabled = disabled \|\| loading \|\| selectedCount === 0;/.test(bar)
        && /\{isDisabled && disabledReason && \(/.test(bar),
        'C11 🔴 原因行的显示条件与按钮的 disabled 是**同一个表达式**(各写一遍必有一天不一致:'
        + '按钮灰了而原因不显示,或反过来)');
    ok(/disabled=\{isDisabled\}/.test(bar),
        'C12 按钮自己也吃那个变量(否则 C11 锁的是个没人用的中间量)');
}

// ══ D 反臂:不在这一态时整页零变化 ══════════════════════════════════════
console.log('\nD 反臂(老路径零漂移)');
{
    ok(/allExcluded \? \(/.test(page),
        'D1 出口卡是条件渲染(allExcluded 为 null 时整页与改动前同形)');
    const s = M.readAllExcluded({});
    const g = M.submitGateState({ allExcluded: false, deliverableCount: 0, submitting: false });
    ok(s.allExcluded === false && g.disabled === false && g.reason === '',
        'D2 空响应 ⇒ 不进新态、闸不生效(我这一单不许把老路径连带改语义)');
    ok((page.match(/toast\.error\(/g) || []).length >= 5,
        'D3 其余 4xx / 网络错误的 toast 仍在(工单 §2f:保持现状,别顺手删了)',
        `${(page.match(/toast\.error\(/g) || []).length} 处`);
}

console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过:${failed.join(' ')}`
        + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('全部通过');
    process.exitCode = 0;
}
