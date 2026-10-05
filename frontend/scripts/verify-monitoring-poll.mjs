#!/usr/bin/env node
/**
 * 判据 · #182 效果监测「不断刷新」。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 Owner 09-12:「效果监测这里还是会不断刷新,这是什么机制?」
 *    机制是 `IdentityReviewPanel:136-138` **挂载即每 30 秒轮询**,
 *    不看有没有待审、也不看有没有任务在跑。08-09 客户反馈⑤那次只加了
 *    `identityItemsEqual` 去重(内容不变不重绘)—— "闪"没了,**网络轮询照旧**。
 *    这是一个典型的「症状修了、机制没修」:去重让它看不见,而不是让它不发生。
 *
 * 本文件是 build 链那一半(纯函数真调 + 接线边);
 * 真计时那几条在 `test-monitoring-poll-timing.mjs`(真 chromium + 假时钟,不进 build 链)。
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
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

const PANEL = 'src/pages/Monitoring/components/IdentityReviewPanel.tsx';
const INDEX = 'src/pages/Monitoring/index.tsx';
const MOD = 'src/pages/Monitoring/monitoringPollSchedule.ts';

let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd(MOD), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a182-'));
    const f = join(tmp, 'poll.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL M0 🔴 判定模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}
ok(['nextPollDelayMs', 'nextUnchangedStreak', 'shouldShowLoading', 'isStreamStalled']
    .every((k) => typeof M[k] === 'function'), 'M1 四个判定函数都导出了');

const panel = decomment(rd(PANEL));
const index = decomment(rd(INDEX));

// ══ A 「有事才轮」的完整状态空间 ═══════════════════════════════════════
console.log('\nA 轮询调度(32 格真跑)');
{
    let cells = 0;
    const violations = [];
    let idleCell = null;
    for (const hasPending of [false, true]) {
        for (const taskActive of [false, true]) {
            for (const unchangedStreak of [0, 1, 2, 5]) {
                for (const visible of [false, true]) {
                    cells += 1;
                    const d = M.nextPollDelayMs({ hasPending, taskActive, unchangedStreak, visible });
                    const shouldStop = !visible || (!hasPending && !taskActive);
                    if (shouldStop && d !== null) {
                        violations.push(`停不下来: pending=${hasPending} task=${taskActive} vis=${visible} ⇒ ${d}`);
                    }
                    if (!shouldStop && (d === null || d < 60_000)) {
                        violations.push(`该轮却没给间隔/太快: streak=${unchangedStreak} ⇒ ${d}`);
                    }
                    if (!hasPending && !taskActive && visible && unchangedStreak === 0) idleCell = d;
                }
            }
        }
    }
    ok(cells === 32, 'A0 矩阵格数对(2×2×4×2)', `${cells} 格`);
    ok(violations.length === 0,
        'A1 🔴🔴 全矩阵不变量:**没待审、没任务** 或 **页面不可见** ⇒ 返回 null(彻底不再安排);'
        + '其余情况间隔 ≥60 秒。老代码是无条件 30 秒 —— 每个开着监测页的人每分钟两次',
        violations.length ? violations.slice(0, 3).join(' | ') : '32/32 满足');
    ok(idleCell === null,
        'A2 🔴 闲着的那一格返回的是 **null 而不是"很久"** ——'
        + 'null 让定时器彻底停下,"很久"仍会在后台醒来', String(idleCell));
    // 退避梯度
    const base = { hasPending: true, taskActive: false, visible: true };
    const d0 = M.nextPollDelayMs({ ...base, unchangedStreak: 0 });
    const d2 = M.nextPollDelayMs({ ...base, unchangedStreak: 2 });
    const d3 = M.nextPollDelayMs({ ...base, unchangedStreak: 3 });
    ok(d0 === 60_000 && d2 === 120_000 && d3 === 300_000,
        'A3 退避梯度 60s → 120s → 300s(工单 §1.1)', `${d0}/${d2}/${d3}`);
    ok(M.nextUnchangedStreak(5, true) === 0,
        'A4 🔴 一有变化立刻回到最快档(不是慢慢降回来 —— 有事发生时用户在等)');
    ok(M.nextUnchangedStreak(2, false) === 3, 'A5 没变化就累加');
}

// ══ B 后台重取不进 loading ═════════════════════════════════════════════
console.log('\nB loading 只给该给的');
{
    ok(M.shouldShowLoading({ manual: true, firstLoad: false }) === true, 'B1 手动刷新 ⇒ 显示');
    ok(M.shouldShowLoading({ manual: false, firstLoad: true }) === true, 'B2 首次加载 ⇒ 显示');
    ok(M.shouldShowLoading({ manual: false, firstLoad: false }) === false,
        'B3 🔴 后台重取 ⇒ **不显示**。内容没变却因为 loading 态切一下而整块重绘,'
        + '那正是"每 30 秒闪一下"的观感来源(08-09 那次只治了数据引用,没治这个)');
    ok(/const showSpinner = shouldShowLoading\(\{/.test(panel)
        && /if \(showSpinner\) setLoading\(true\)/.test(panel)
        && /if \(showSpinner\) setLoading\(false\)/.test(panel),
        'B4 接线:开和关**用的是同一个判断** —— 各写一遍必有一天不对称(开了关不掉)');
}

// ══ C 监测流停滞 ═══════════════════════════════════════════════════════
console.log('\nC 流停滞');
{
    ok(M.isStreamStalled({ lastFrameAt: 1000, now: 1000 + 120_000 }) === true, 'C1 满 120 秒 ⇒ 停滞');
    ok(M.isStreamStalled({ lastFrameAt: 1000, now: 1000 + 119_000 }) === false, 'C2 不到就不算');
    ok(M.isStreamStalled({ lastFrameAt: null, now: 999999 }) === false,
        'C3 🔴 还没开始就不谈停滞(否则一进页面就报"超时")');
    ok(M.isStreamStalled({ lastFrameAt: 1000, now: 1000 + 5000, stallMs: 3000 }) === true,
        'C4 阈值可注入(判据才跑得动)');
    ok(/Promise\.race\(\[[\s\S]{0,200}reader\.read\(\)/.test(index),
        'C5 🔴 每次读都配一个闸 —— `reader.read()` 本身没有客户端超时,'
        + '后端卡住时格子会**永远 pulse**(ProgressPanel:134):屏幕说"还在跑",'
        + '真相是没人再说话了');
    ok(/setStreamStalled\(true\)/.test(index) && /setStreamStalled\(false\)/.test(index),
        'C6 停滞会进态,也会在又有帧时解除(只进不出 = 一旦误判就永远卡在那)');
    ok(/data-testid="monitoring-stream-retry"/.test(index)
        && /handleRunMonitoring\(\)/.test(index.slice(index.indexOf('monitoring-stream-retry') - 400,
            index.indexOf('monitoring-stream-retry') + 400)),
        'C7 🔴 停滞态给的是**能点的出口**(重试真的重新发起监测),不是一句话就完');
    ok(/这次没有多扣算力/.test(rd(INDEX)),
        'C8 并且说清钱的事 —— 用户看到"超时"第一反应是"我是不是被扣了"');
}

// ══ D 接线边 ═══════════════════════════════════════════════════════════
console.log('\nD 接线边');
{
    ok(!/window\.setTimeout\(poll, 30_000\)/.test(panel) && !/30_000/.test(panel),
        'D1 🔴 无条件 30 秒那一行已不在(病根本身)');
    ok(/nextPollDelayMs\(\{/.test(panel) && /if \(delay === null\) return;/.test(panel),
        'D2 下一次的间隔由纯函数给,且 null 就不安排');
    /**
     * 🔴 D3 第一版只查 `visibilitychange` 这个词在不在 —— 那是**存在锁**:
     *    把 handler 改成"回到前台也只是重新排期"照样绿,而用户看到的仍是旧快照。
     *    改成钉 handler 里那一支的**行为**:visible ⇒ 立刻 poll()。
     */
    ok(/visibilitychange/.test(panel), 'D3a 装了 visibilitychange 监听');
    ok(/visibilityState === 'visible'\) \{ void poll\(\); \}/.test(panel),
        'D3 🔴 回到前台**立刻取一次**(不是等下一个周期 ——'
        + '用户切回来要看到现在的状态,不是他离开那一刻的快照)');
    ok(/document\.removeEventListener\('visibilitychange'/.test(panel),
        'D4 监听器有拆(不拆就是每次切品牌泄一个)');
    ok(/hasPending: itemsRef\.current\.length > 0/.test(panel),
        'D5 🔴 「有没有待审」读的是 ref 的**当前值** —— 读闭包里的 items 会永远是挂载那一刻的空数组');
    ok(/taskActive/.test(panel) && /taskActive=\{showProgressPanel && !streamStalled\}/.test(index),
        'D6 「有没有任务在跑」由页面传进来,且**停滞之后不再算在跑**(否则永远轮下去)');
    const decisionBlock = panel.slice(panel.indexOf('onDecided?.({ resultId'), panel.indexOf('onDecided?.({ resultId') + 500);
    ok(/void load\(\);/.test(decisionBlock),
        'D7 🔴 裁决完立刻重取一次 —— 等下一个周期(最快 60 秒)才更新,看起来就像"点了没反应"');
    ok(/data-testid="identity-review-refresh"/.test(panel) && /aria-label="刷新待确认列表"/.test(panel),
        'D8 刷新按钮有 testid 与 aria-label(图标按钮必须有无障碍名字)');
    ok(/manual: true/.test(panel),
        'D9 🔴 刷新按钮走 manual 档 —— 它是**唯一**该显示 loading 的用户动作'
        + '(存在理由:轮询变慢/停下之后,用户要有办法立刻问一次)');
    ok(!/setItems\(prev => \{[\s\S]{0,200}unchangedStreak/.test(panel),
        'D10 🔴 计数不借 setItems 的 updater 做(StrictMode 下 updater 会跑两次,计数会凭空多加)');
}

console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('全部通过');
    process.exitCode = 0;
}
