#!/usr/bin/env node
/**
 * 判据 · #196 素材准备要轮询到终态。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 现场(0913b):选账号 ⇒ prepare-v2 **200** ⇒「正在准备发布素材…」⇒ **3 分钟零后续请求**。
 * 机理:prepare-v2 只 claim 一行 `preparing` 就返回,上传与 mark_ready 由 cron worker 做;
 * 前端只调一次,`state` 就此定格。**不是慢,是永远不会变。**
 *
 * 🔴 这一层的两条时间闸(120s 慢提示 / 300s 停)**只能用假时钟测**:
 *    真等 5 分钟的判据没人会跑,不跑的判据等于不存在。
 *    所以节奏与判定全在纯函数 `artifactPollPlan.planNextPoll(state, elapsedMs, attempts)` 里
 *    —— 时间从参数进来,判据把它当刻度盘拨。
 *    组件里只负责"按计划办",另有浏览器臂证它真的按计划办了。
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
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};
const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/artifactPollPlan.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a196-'));
    const f = join(tmp, 'plan.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL Q0 🔴 轮询计划模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}

const INL = decomment(rd('src/pages/Writing/ImageNotePublishInline.tsx'));
console.log('Q #196 素材准备轮询');

/* ══ Q0 分母自证 ═══════════════════════════════════════════════════ */
{
    ok(M.POLL_INTERVAL_MS === 3000, 'Q0a 轮询间隔 3s(工单 a1)', `${M.POLL_INTERVAL_MS}ms`);
    ok(M.SLOW_NOTICE_MS === 120000 && M.GIVE_UP_MS === 300000,
        'Q0b 两条时间闸:120s 慢提示 / 300s 停',
        `${M.SLOW_NOTICE_MS} / ${M.GIVE_UP_MS}`);
}

/* ══ Q1 preparing ⇒ 接着读;ready ⇒ 停并放行 ═══════════════════════ */
{
    const p1 = M.planNextPoll('preparing', 0, 1);
    ok(p1.keepPolling && p1.delayMs === 3000 && !p1.terminal,
        'Q1a 🔴 `preparing` ⇒ **还要再读**(现场那一次就是读完第一次就不读了)',
        `keepPolling=${p1.keepPolling} delay=${p1.delayMs}`);
    const p2 = M.planNextPoll('ready', 6000, 3);
    ok(!p2.keepPolling && p2.terminal && !p2.retryable && p2.line === '素材已就绪',
        'Q1b `ready` ⇒ 停,且是终态', `${p2.line}`);
    /* 🔴 反向控制:如果 planNextPoll 永远说"别读了",Q1a 就抓不到东西;
       上面 p1/p2 一真一假才说明它真的在分状态。 */
    ok(p1.keepPolling !== p2.keepPolling,
        'Q1c 反向控制:两种状态给出**不同**结论(恒真/恒假的计划器抓不到任何缺陷)');
    /* 「第 N 次检查」要真的随次数变 —— 不变的话进度条就是装饰 */
    const l1 = M.planNextPoll('preparing', 0, 1).line;
    const l7 = M.planNextPoll('preparing', 0, 7).line;
    ok(l1 !== l7 && l7.includes('7'),
        'Q1d 🔴 进度行随检查次数变(a3)—— 不变的话"卡住"和"正常进行"长得一模一样',
        `${l1} / ${l7}`);
}

/* ══ Q3 终态:failed 可重试、unknown 一律不给 ═══════════════════════ */
{
    const f = M.planNextPoll('failed', 9000, 3);
    const u = M.planNextPoll('unknown', 9000, 3);
    ok(f.terminal && !f.keepPolling && f.retryable,
        'Q3a `failed` ⇒ 停,且**可以**重新准备(后端 RETRYABLE_STATES = {failed})');
    ok(u.terminal && !u.keepPolling && !u.retryable,
        'Q3b 🔴 `unknown` ⇒ 停,且**不给重试** —— 远端可能已经收了,'
        + '重传就是可能发两次(后端注释:不自动重传是硬约束,不是保守选择)');
    ok(!f.line.includes('failed') && !u.line.includes('unknown'),
        'Q3c 🔴 屏幕上不出现裸 state 串');
    /* 认不出的状态:当作还没结束,别谎称终态 */
    const x = M.planNextPoll('some_new_backend_state', 1000, 2);
    ok(x.keepPolling && !x.terminal,
        'Q3d 认不出的状态**当作还没结束**(宁可多读一次,不谎称结束)');
}

/* ══ Q3b [#196 a3] 失败原话:服务端 > 前端合成 > 兜底句 ══════════════ */
{
    /*
     * 🔴 **契约错位**:C 的 196-c1(f038c54f1)在 200 回包里发 `failure_reason`,
     *    而前端这两处读的一直是 `user_message` —— 那是**前端自己**在 catch 分支
     *    合成的字段。两边各自全绿,字段到了没人读。
     *    ⇒ 现在由 `artifactLine()` 一处决定,判据真调它。
     */
    const failedPlan = M.planNextPoll('failed', 9000, 3);
    const unknownPlan = M.planNextPoll('unknown', 9000, 3);
    ok(M.artifactLine({ failure_reason: '第 2 张图读取为空', user_message: '素材准备失败' }, failedPlan)
        === '第 2 张图读取为空',
        'Q3b1 🔴 服务端原话**优先** —— 它是唯一知道"这一篇到底怎么了"的那一个');
    ok(M.artifactLine({ failure_reason: '', user_message: 'HTTP 合同里的 message' }, failedPlan)
        === 'HTTP 合同里的 message',
        'Q3b2 原话为空 ⇒ 退到 catch 合成的那句(HTTP 错误那条路)');
    ok(M.artifactLine({ failure_reason: '', user_message: '' }, failedPlan) === failedPlan.line,
        'Q3b3 两个都空 ⇒ 退到计划层的兜底句');
    ok(M.artifactLine(null, failedPlan) === failedPlan.line, 'Q3b4 没有 artifact 也不炸');
    ok(M.artifactLine({ failure_reason: '  原话  ' }, failedPlan) === '原话',
        'Q3b5 两头空白去掉(服务端多打个空格不该让它变成"有原话")');
    /*
     * 🔴 写这条判据时当场撞出来的:`unknown` 那句里带着一个**关于钱的承诺**
     *    (「不会重复扣算力」),而原话只说原因。一刀切"原话优先"会把那句承诺**删掉** ——
     *    那是用户此刻最想知道的一件事。所以 unknown 两句都给。
     */
    const u1 = M.artifactLine({ failure_reason: '远端已接受但回执丢了' }, unknownPlan);
    ok(u1.includes('不会重复扣算力') && u1.includes('远端已接受但回执丢了'),
        'Q3b8 🔴 `unknown` 时**承诺句不许被原话挤掉**:两句都在', u1);
    ok(M.artifactLine({ failure_reason: '' }, unknownPlan) === unknownPlan.line,
        'Q3b9 `unknown` 没有原话时就只有那句承诺');
    /*
     * 🔴 [a2 · a3 附注订正] 上面那条"承诺句不许被挤掉"靠什么判?
     *
     *    第一版 `artifactLine` 判的是 `planLine.includes('不会重复扣算力')` ——
     *    把行为拴在一串中文上。文案改一个字(哪怕只加个标点),这个判断就静默变假,
     *    承诺句被原话挤掉,而**没有任何一格会红**:Q3b8 用的是同一串字面量,
     *    它跟着一起变,两边同时"看起来对"。
     *    ⇒ 改成按 `plan.state === 'unknown'` 判,并在这里钉住两件事:
     *       ① 计划带得出状态(四个状态各自标对);
     *       ② 实现里**不再**出现按文案判的写法。
     */
    /* 🔴 **去注释再查**:这一格问的是代码怎么判,不是文档里提到过什么。
       (不去的话,解释"改前是怎么错的"那段注释自己就会把这一格打红 —— 实测踩了一次。) */
    const SRC_PLAN = decomment(rd('src/pages/Writing/artifactPollPlan.ts'));
    ok(!/planLine\.includes\(/.test(SRC_PLAN),
        'Q3b10 🔴 `artifactLine` **不按文案判** —— 不许再出现 `planLine.includes(...)`');
    ok(/plan\?\.state === 'unknown'/.test(SRC_PLAN),
        'Q3b11 🔴 改成按**状态**判(`plan.state === \'unknown\'`)');
    const STATES = [
        ['ready', 'ready'], ['failed', 'failed'], ['unknown', 'unknown'],
        ['preparing', 'preparing'], ['zzq_没见过的状态', 'preparing'],
    ];
    const wrong = STATES.filter(([raw, want]) => M.planNextPoll(raw, 0, 1).state !== want);
    ok(wrong.length === 0,
        `Q3b12 五种输入(含认不出的一种)算出的 \`state\` 都标对了`,
        wrong.map(([r]) => r).join(',') || '全对');
    /* 反臂:承诺句只在 unknown 那一档保留 —— 别的终态不许也留着 */
    const keptElsewhere = ['ready', 'failed'].filter((st) => {
        const line = M.artifactLine({ failure_reason: '原话' }, M.planNextPoll(st, 0, 1));
        return line !== '原话';
    });
    ok(keptElsewhere.length === 0,
        'Q3b13 反臂:只有 `unknown` 保留计划句,别的终态原话照样替掉',
        keptElsewhere.join(',') || '无');
    /*
     * 🔴 兜底句也要**说清是哪一件事**。服务端没给原话时,屏幕上只剩它 ——
     *    写成「操作未完成」这种放之四海皆准的话,用户连"是这一篇的素材"都不知道。
     *    (a3 之前这一格由 W3 那发毒守着;a3 之后原话优先,兜底句反而没人守了,
     *     所以这条补上。)
     */
    ok(failedPlan.line.includes('素材') && failedPlan.line.includes('这篇'),
        'Q3c 🔴 失败的兜底句点名「这篇」「素材」,不是一句放之四海的「操作未完成」',
        failedPlan.line);

    /* 接线:两处读点都走这只函数,且不再出现裸 `user_message` 兜底 */
    /*
     * 🔴 传的必须是**计划对象**,不是 `artPlan.line`。
     *    传字符串时 `artifactLine` 看不到 `terminal/retryable`,`unknown` 的
     *    承诺句就会被原话挤掉 —— 纯函数臂喂的是对象所以全绿,**浏览器臂**才红。
     *    两层都要有,就是为了抓这种"函数对、接线错"。
     */
    ok((INL.match(/artifactLine\(artifact, artPlan\)/g) || []).length === 2,
        'Q3b6 🔴 **两处**读点都走同一只函数,且传的是**计划对象**',
        `${(INL.match(/artifactLine\(/g) || []).length} 处`);
    ok(!/artifact\?\.user_message \|\|/.test(INL),
        'Q3b7 🔴 组件里不再有"只读 user_message"那条路 ——'
        + '留着它就等于服务端原话可以被绕过去');
}

/* ══ Q4 两条时间闸(假时钟)══════════════════════════════════════ */
{
    const before = M.planNextPoll('preparing', 119_000, 40);
    const slow = M.planNextPoll('preparing', 121_000, 41);
    ok(!before.line.includes('比平时慢') && slow.line.includes('比平时慢'),
        'Q4a 🔴 120s 之后才说「比平时慢」,之前不说(假时钟拨过去的)',
        `${before.line} → ${slow.line}`);
    ok(slow.keepPolling, 'Q4b 慢提示之后**继续读**(慢不等于该放弃)');
    const give = M.planNextPoll('preparing', 301_000, 100);
    ok(!give.keepPolling && !give.terminal && give.stoppedReason.length > 10,
        'Q4c 🔴 300s 之后**停**,并说清为什么停 —— 一直读下去就是在用户的浏览器里空转',
        give.stoppedReason.slice(0, 40));
    ok(!/失败|白花|扣/.test(give.stoppedReason) && /还在|后台/.test(give.stoppedReason),
        'Q4d 🔴 停的话要说清**它没失败**(素材准备还在后台跑)——'
        + '只说"超时"会被读成"这次白花钱了"', give.stoppedReason.slice(0, 50));
}

/* ══ Q2 幂等钥匙:轮询期间**不换**,只有手动重试才换 ════════════════ */
{
    ok(typeof M.retryKeySuffix === 'undefined',
        'Q2a 🔴 没有留「换钥匙重试」的空壳 —— 那颗按钮按 N7d 不许存在,'
        + '留一个没有消费方的导出,判据可以对着空壳报绿');
    /*
     * 🔴 接线:轮询那一圈用的是**同一个 key 变量**(在 effect 外层算一次),
     *    而不是每次 tick 重算。每 tick 重算 = 每 3 秒 claim 一条新 artifact,
     *    正是幂等根要防的事。
     */
    const prepAt = INL.indexOf("const key = stableRequestId('prep'");
    const eff = INL.slice(INL.lastIndexOf('useEffect(() =>', prepAt), INL.indexOf(']);', prepAt));
    ok(/stableRequestId\('prep', prepSeed\(postId, revisionId, attempt\)\)/.test(INL),
        'Q2b 钥匙由 `(postId, revisionId, 第几次尝试)` **确定性**派生 ——'
        + '同一次尝试里收起再打开、刷新都是同一把');
    ok(M.prepSeed(40, 9001, 1) === '40-9001' && M.prepSeed(40, 9001, 2) === '40-9001#2',
        'Q2b2 第 1 次的种子与老行为**逐字相同**(不改已在用的那把钥匙)',
        `${M.prepSeed(40, 9001, 1)} / ${M.prepSeed(40, 9001, 2)}`);
    ok((eff.match(/stableRequestId\(/g) || []).length === 1,
        'Q2c 🔴 轮询那一圈里**只算一次**钥匙(每 tick 重算 = 每 3 秒 claim 一条新 artifact)',
        `${(eff.match(/stableRequestId\(/g) || []).length} 处`);
    ok(!/retryKeySuffix/.test(INL) && !/\+ *['\`]-retry/.test(INL),
        'Q2d 🔴 换钥匙**不是在 UUID 后面接后缀** —— 后端 `normalize_uuid` 对非 UUID 形状'
        + '直接 400(上一版删掉的 `retryKeySuffix` 正是那种写法,一上线就是 400)');
    ok(!/\[postId, revisionId, artifact\]/.test(INL),
        'Q2e 🔴 `artifact` 不在准备 effect 的依赖里 ——'
        + '进了就会每收到一次状态重建一次定时器,计时也就没意义了');
}

/* ══ Q5 收起面板 ⇒ 定时器清掉 ═══════════════════════════════════════ */
{
    ok(/window\.clearTimeout\(artPollRef\.current\)/.test(INL),
        'Q5a 有清定时器这件事');
    ok(/return \(\) => \{ cancelled = true; clear\(\); \};/.test(INL),
        'Q5b 🔴 cleanup 里**既停回调也清定时器** —— 只停一个,面板收起后仍在后台打接口');
    ok(/artPollRef\.current = window\.setTimeout\(tick, plan\.delayMs\)/.test(INL),
        'Q5c 下一次读用的是计划给的间隔(不是组件里另写一个数)');
}

/* ══ Q6 组件不写第二套状态判断 ═════════════════════════════════════ */
{
    /* 🔴 [#196 a3] 那一行字现在由 `artifactLine(artifact, artPlan)` 统一决定
       (服务端原话 > catch 合成 > 计划兜底;unknown 的承诺句不被挤掉),
       所以这里钉的是**两处读点都走它** + 灰不灰仍看 `artPlan.terminal`。 */
    ok(/artifactLine\(artifact, artPlan\)/.test(INL) && /artPlan\.terminal/.test(INL),
        'Q6a 屏幕上那行字、发布键为什么灰着,都来自同一处(组件里不写第二套)');
    ok(!/ARTIFACT_COPY\[/.test(INL),
        'Q6b 🔴 不再用那张静态文案表 —— 文案与「还读不读」必须同源,'
        + '两处各写一遍必有一天屏幕说「准备中」而轮询已经停了');
    ok(!/state === 'unknown'/.test(INL) && !/state === 'failed'/.test(INL),
        'Q6c 🔴 组件里不出现对 artifact state 的裸比较(状态判断只许在计划层一处)');
    ok(/data-testid="inp-artifact-stopped"/.test(INL)
        && /data-testid="inp-artifact-block"/.test(INL),
        'Q6d 准备态那一块与「停下来的说明」都能被行为臂定位');
    /*
     * 🔴 **这一格是本单交回 Review 的那个口子**:后端说 `failed` 可重试,
     *    但本面板的 N7d(#186/#188 极简裁定)禁「手动重新准备」。
     *    我按已经立着的那条锁做 ⇒ `failed` 在这个面板里**没有出口**。
     *    判据把这件事钉成**明说**的现状,而不是让它悄悄存在:
     *    哪天 Review 决定给出口,这条会红,提醒同一笔里把 N7d 一起改。
     */
    ok(!/inp-artifact-retry/.test(INL),
        'Q6e 🔴 没有「重新准备」按钮(N7d 不放宽)——'
        + '失败的出口不是按钮,是下一次展开换钥匙(见 Q6f)');
}

/* ══ Q6f failed 的出口:下一次展开换一把钥匙(Review 2026-09-13 裁定)═══ */
{
    ok(M.nextAttemptAfter('failed', 1) === 2,
        'Q6f1 🔴 看到 `failed` ⇒ 下一次展开用第 2 把钥匙(后端 RETRYABLE_STATES 允许 failed 重来)');
    ok(M.nextAttemptAfter('unknown', 1) === 1,
        'Q6f2 🔴 看到 `unknown` ⇒ **不换** —— 远端可能已经收了,换 key = 再 claim 一条 = 可能发两次');
    ok(M.nextAttemptAfter('preparing', 3) === 3 && M.nextAttemptAfter('ready', 3) === 3,
        'Q6f3 `preparing` / `ready` 都不递增(preparing 递增 = 每次展开都 claim 一条新的)');
    ok(/nextAttemptAfter\(a\?\.state, attempt\)/.test(INL)
        && /sessionStorage\.setItem\(/.test(INL),
        'Q6f4 接线:组件**真的**把下一次的尝试号记下来了(只写在 state 里的话,'
        + '收起时组件卸载就忘了)');
    ok(/sessionStorage\.getItem\(prepAttemptStorageKey/.test(INL),
        'Q6f5 接线:展开时**真的**把它读回来(不读 = 永远第 1 把,等于没换)');
    // Check actual try-block ancestry, not arbitrary character distance to the nearest `try`.
    const ts = require_('typescript');
    function storageSites(source) {
        const sf = ts.createSourceFile('inline.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
        const sites = [];
        function visit(node) {
            if (ts.isCallExpression(node) && /(?:^|\.)sessionStorage\.(get|set|remove)Item$/.test(node.expression.getText(sf))) {
                let guarded = false;
                for (let p = node.parent; p; p = p.parent) {
                    if (ts.isTryStatement(p) && node.pos >= p.tryBlock.pos && node.end <= p.tryBlock.end) { guarded = true; break; }
                    if (ts.isFunctionLike(p)) break;
                }
                sites.push({ guarded });
            }
            ts.forEachChild(node, visit);
        }
        visit(sf); return sites;
    }
    ok(storageSites("try { sessionStorage.getItem('x') } catch {}")[0]?.guarded, 'Q6f6a try 内的存储读取确实受保护');
    ok(!storageSites("try {} catch {} sessionStorage.getItem('x')")[0]?.guarded, 'Q6f6b 邻近但不在 try 内的读取必须被抓住');
    const sessSites = storageSites(INL);
    const unguarded = sessSites.filter(site => !site.guarded);
    ok(sessSites.length >= 2 && unguarded.length === 0,
        'Q6f6 sessionStorage 读写**都包在 try 里** —— 隐私模式下抛错会把整个面板炸掉',
        `${sessSites.length} 处,未保护 ${unguarded.length} 处`);
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
