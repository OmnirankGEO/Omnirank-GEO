/**
 * #182 · 效果监测「不断刷新」—— 轮询节奏与停滞判定。**零 import**。
 *
 * Owner 09-12:「效果监测这里还是会不断刷新,这是什么机制?」
 *
 * 🔴 机制是:`IdentityReviewPanel` **挂载即每 30 秒轮询**,不看有没有待审、
 *    也不看有没有任务在跑(`:136-138`)。08-09 那次客户反馈⑤只加了
 *    `identityItemsEqual` 去重(内容不变不重绘),**网络轮询照旧** ——
 *    于是"闪"不见了,但每个开着监测页的人仍然每分钟两次打同一个端点。
 *    Deploy 09-12 读数:该端点今天 165 次、p50 0.073s(不慢,但没必要)。
 *
 * 🔴 所以本模块回答的是「**这一刻还该不该再问一次**」,而不是"隔多久问一次"：
 *    · 没有待审、也没有任务在跑 ⇒ **不再问**(首次加载之后就停);
 *    · 页面不可见 ⇒ 不问(回到前台再立刻问一次,那一下由调用点做);
 *    · 连续几次没变化 ⇒ 退避;一有变化立刻回到最快档。
 *
 * 判据要能**真调**它,所以它不碰 React、不碰计时器、不碰 document。
 */

export const POLL_FAST_MS = 60_000;
export const POLL_SLOW_MS = 120_000;
export const POLL_IDLE_MS = 300_000;
/** 监测流多久没有任何帧就算停滞(工单 §1.4)。 */
export const STREAM_STALL_MS = 120_000;

export interface PollInput {
    /** 有待审条目(面板上真有事要人处理)。 */
    hasPending: boolean;
    /** 本页有监测任务在跑(进度还在动)。 */
    taskActive: boolean;
    /** 连续多少次取回来内容没变。 */
    unchangedStreak: number;
    /** 页面此刻可见吗。 */
    visible: boolean;
}

/**
 * 下一次轮询隔多久。**返回 null = 不再安排**。
 *
 * 🔴 null 与"隔很久"是两件事:前者让定时器彻底停下,后者仍会在后台醒来。
 *    老代码没有 null 这一档 —— 那正是"没事也一直刷"的来源。
 */
export function nextPollDelayMs(input: PollInput): number | null {
    if (!input.visible) return null;
    if (!input.hasPending && !input.taskActive) return null;
    const streak = Math.max(0, Number(input.unchangedStreak) || 0);
    if (streak >= 3) return POLL_IDLE_MS;
    if (streak >= 2) return POLL_SLOW_MS;
    return POLL_FAST_MS;
}

/** 取回来之后更新"连续没变"的计数。一有变化立刻归零(下一次就回到最快档)。 */
export function nextUnchangedStreak(prev: number, changed: boolean): number {
    if (changed) return 0;
    return Math.max(0, Number(prev) || 0) + 1;
}

/**
 * 这一次取数要不要显示 loading 骨架。
 *
 * 🔴 **后台重取不进 loading**(工单 §1.3):那正是"闪"的观感来源 ——
 *    内容没变,却因为 loading 态切了一下而整块重绘。
 *    只有首次加载与用户**自己点刷新**时才显示。
 */
export function shouldShowLoading(input: { manual: boolean; firstLoad: boolean }): boolean {
    return !!input.manual || !!input.firstLoad;
}

/**
 * 监测流是不是停滞了。
 *
 * 🔴 `reader.read()` 没有客户端超时:后端某格卡在 running 不发 complete,
 *    cell 就**永远 pulse**(`ProgressPanel.tsx:134`)—— 屏幕上是"还在跑",
 *    真相是"没人再说话了"。超过 `STREAM_STALL_MS` 没有任何帧就判停滞,
 *    并给重试入口(重试 = 重新发起 run-stream,幂等由后端 generation 机制保证)。
 */
export function isStreamStalled(input: {
    lastFrameAt: number | null;
    now: number;
    stallMs?: number;
}): boolean {
    const last = Number(input?.lastFrameAt);
    if (!Number.isFinite(last) || last <= 0) return false;   // 还没开始就不谈停滞
    const now = Number(input?.now);
    if (!Number.isFinite(now)) return false;
    const limit = Number(input?.stallMs) > 0 ? Number(input.stallMs) : STREAM_STALL_MS;
    return now - last >= limit;
}
