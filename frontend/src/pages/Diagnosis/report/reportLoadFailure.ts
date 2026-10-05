/**
 * 报告页取数失败态的**纯逻辑**(门四 UX 返修)。
 *
 * 🔴 这个文件存在的理由(门四真浏览器实录):
 *    服务商打开 `/diagnosis/report/3`,页面并发 6 个请求打在 WORKERS=1 的后端上,
 *    nginx 把慢的几条打成 503。前端把这一次 503 当**终态**,整页画成
 *    「加载报告失败 · 返回首页」。同一端点紧接着连打 3 次全 200 ——
 *    是瞬时饱和,不是坏。刷新即好。
 *
 *    她付了 7800 算力,第一次点开报告看到的是「失败」,唯一出口是「返回首页」。
 *    这是**把瞬时态画成终态**:系统没坏,但用户被告知坏了,而且没有原地恢复的路。
 *
 * 无 React 依赖是刻意的:判据要能直接 import 真跑这两个谓词,
 * 而不是去 bundle 整棵 React 依赖树、或者退回读源码串。
 */

/** 重试前的短退避。一次就够 —— 瞬时饱和通常几百毫秒就过去了。 */
export const REPORT_RETRY_DELAY_MS = 2000;

/**
 * 这次失败是不是**瞬时**的(值得自动重试一次)?
 *
 * · 5xx = 服务端/网关侧,重试有意义(503 正是本次的实录);
 * · 4xx = 终态。403/404 重试一百次也还是那样,重试只会让她多等 2 秒;
 * · 拿不到 status = 请求根本没走完网络层(断网 / 超时 / CORS / DNS),同样值得再试一次。
 *
 * 🔴 只重试 GET。本页首屏两条主请求(getDetail / getContent)都是只读,
 *    重试无副作用;这个函数**不该**被拿去包任何写操作。
 */
export function isTransientLoadFailure(err: unknown): boolean {
    const e = err as {
        response?: { status?: number };
        status?: number;
    } | null | undefined;
    // axios 走 err.response.status;fetch wrapper 直接挂 err.status
    const status = e?.response?.status ?? e?.status;
    if (typeof status === 'number') {
        return status >= 500 && status <= 599;
    }
    return true;
}

/**
 * 跑一次;瞬时失败就退避后**再跑一次**。第二次仍失败就把错误抛出去走失败态。
 *
 * 🔴 只重试一次是有意的:重试次数堆上去 = 用户对着骨架屏干等更久,
 *    而她其实更想要一个「重试」按钮自己掌握节奏。自动那一次是为了让
 *    **绝大多数瞬时 503 根本不上屏**;剩下的交给按钮。
 */
export async function loadWithOneRetry<T>(
    run: () => Promise<T>,
    sleep: (ms: number) => Promise<void> = defaultSleep,
): Promise<T> {
    try {
        return await run();
    } catch (err) {
        if (!isTransientLoadFailure(err)) throw err;
        await sleep(REPORT_RETRY_DELAY_MS);
        return run();
    }
}

function defaultSleep(ms: number): Promise<void> {
    return new Promise((resolve) => { setTimeout(resolve, ms); });
}

export interface ReportLoadFailure {
    /** 上屏第一句。不吓人、不让她以为报告丢了。 */
    headline: string;
    /** 第二句:说清楚怎么办 + 钱有没有动。 */
    body: string;
    isTransient: boolean;
}

/**
 * 瞬时失败的固定文案。
 *
 * 三条约束都在这一句里兑现:
 * 1. **不说"失败"以外的吓人话** —— 不出现"错误/异常/无法/丢失/不存在"这类词;
 * 2. **带行动指引** —— 明写点「重试」;
 * 3. **把钱说死** —— 她刚花了几千算力,失败页上最该回答的问题是"钱白花了吗"。
 *    「重新加载不额外扣算力」是可核验的:`GET /api/diagnosis/{id}/content`
 *    的 handler 体内无任何计费调用,`services/organization_route_contract.py:222`
 *    该条也没有 `billing=` 标注(反向对照:同文件 generate-quote 那条有)。
 */
export const TRANSIENT_FAILURE_COPY = {
    headline: '刚才没连上,报告还在',
    body: '服务器忙了一下,报告已经生成好了。点「重试」就能接着看,重新加载不额外扣算力。',
} as const;

/**
 * 把一个异常翻成上屏用的失败态。
 *
 * 终态(4xx)仍走 `formatApiErrorForDisplay` 的人话 —— 那些消息是有信息量的
 * (没权限 / 报告不存在),换成笼统句反而更糟。瞬时态则**不上原始 detail**:
 * 503 的 body 是 nginx 的 HTML,翻出来只会是一句她看不懂又吓人的话。
 */
export function describeReportLoadFailure(
    err: unknown,
    formattedMessage: string,
): ReportLoadFailure {
    if (isTransientLoadFailure(err)) {
        return { ...TRANSIENT_FAILURE_COPY, isTransient: true };
    }
    return {
        headline: formattedMessage,
        body: '如果这是刚做完的体检,稍等一下再点「重试」;还是不行就返回首页从客户列表进来。',
        isTransient: false,
    };
}
