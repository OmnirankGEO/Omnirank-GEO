/**
 * UI-34 statusVersion 单调守卫 —— **纯函数**,零 React、零网络。
 *
 * 🔴 它防的是一个真会发生的形态,不是理论风险
 * ------------------------------------------------
 * 轮询是并发的:第 N 次请求可能比第 N+1 次**晚**回来(网络抖动 / 429 Retry-After
 * 重放 / 浏览器 tab 挂起后一起回)。如果 UI 无脑用「最后到达的那份」,
 * 用户会看到 `completed`(v7)被一份迟到的 `running`(v5)顶回去 ——
 * 对一个刚被扣了算力的销售来说,那等于「钱扣了、活又回去跑了」。
 *
 * 所以判定轴是 **statusVersion**,不是到达顺序、不是 updatedAt(时间戳会因
 * 时钟漂移打平),也不是 commandState 的语义先后(状态机可以合法回退,
 * 版本号不会)。
 *
 * 🔴 为什么单独成一个文件
 * ------------------------------------------------
 * 仓里没有 vitest / jest。把守卫抽成不依赖 React 的纯函数,
 * 才能被 `scripts/test-defgeo-publish-status-version.mjs` 用 esbuild 转译后
 * **真的跑一遍**(与 `test-publication-contract-logic.mjs` 同一套办法)。
 * 判据看不见的逻辑等于没有判据。
 */

/** 守卫只关心这两格;整份响应类型见 `contracts.ts`。 */
export interface StatusVersionCarrier {
    publishCommandId: string;
    statusVersion: number;
}

export type StatusUpdateVerdict =
    /** 接纳:同一条命令 + 版本号严格更高(或首份)。 */
    | 'accepted'
    /** 丢弃:同一条命令,但版本号不比手上这份高(慢响应)。 */
    | 'stale_version'
    /** 丢弃:不是这一条命令的响应(路由切换后旧请求回来)。 */
    | 'other_command'
    /** 丢弃:形状不对(缺 id / 版本号不是非负整数)。 */
    | 'malformed';

export interface StatusUpdateResult<T extends StatusVersionCarrier> {
    verdict: StatusUpdateVerdict;
    /** 该不该换掉 state。`false` 时 `next` **就是传进来的 current 本身**(引用不变,不触发重渲染)。 */
    changed: boolean;
    next: T | null;
}

function isNonNegativeInteger(value: unknown): value is number {
    return typeof value === 'number'
        && Number.isFinite(value)
        && Number.isInteger(value)
        && value >= 0;
}

/**
 * 决定一份新到的状态响应要不要采纳。
 *
 * @param current           手上正在显示的那份(首次轮询时为 null)
 * @param incoming          刚到的响应(**未经信任**,故形参类型是 unknown)
 * @param expectedCommandId 当前页面正在看的命令号
 *
 * 判定顺序是有意的:先判「是不是这条命令」再判「版本高不高」——
 * 反过来会让另一条命令的高版本号把本条顶掉。
 */
export function acceptStatusUpdate<T extends StatusVersionCarrier>(
    current: T | null,
    incoming: unknown,
    expectedCommandId: string,
): StatusUpdateResult<T> {
    const keep = (verdict: StatusUpdateVerdict): StatusUpdateResult<T> => ({
        verdict, changed: false, next: current,
    });

    if (!incoming || typeof incoming !== 'object') return keep('malformed');
    const candidate = incoming as Partial<StatusVersionCarrier>;

    if (typeof candidate.publishCommandId !== 'string'
        || candidate.publishCommandId.length === 0) {
        return keep('malformed');
    }
    if (candidate.publishCommandId !== expectedCommandId) return keep('other_command');
    if (!isNonNegativeInteger(candidate.statusVersion)) return keep('malformed');

    // 🔴 严格大于。相等也丢 —— 同版本内容按契约就是同一份,
    //    重新 setState 只会制造一次无意义重渲染(并让「闪一下」变成可复现现象)。
    if (current !== null && candidate.statusVersion <= current.statusVersion) {
        return keep('stale_version');
    }
    return { verdict: 'accepted', changed: true, next: incoming as T };
}
