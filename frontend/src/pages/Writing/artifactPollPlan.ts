/**
 * #196 · 素材准备的**轮询计划**。**零 import**(判据要能 transpile 后真调,用假时钟)。
 *
 * ## 现场(0913b · QA owner · 629 · 作品 40)
 *
 * 选账号 ⇒ `POST …/prepare-publish-media-v2` **200** ⇒ 面板「正在准备发布素材…」,
 * **3 分钟零后续请求**,状态不动,发布键一直灰着。
 *
 * ## 机理(核到 5784baa9e 的后端源码,不是照工单抄的)
 *
 * prepare-v2 是**异步**的:它只 `claim_artifact` 落一行 `state='preparing'` 就返回
 * (`api/geo_image_note_api.py` 那一段自记:「claim 只落身份,**不在请求线程里跑逐图上传**
 * —— 那是耗时外调,由 durable worker 领」)。真正的上传与 `mark_ready` 由 cron 容器的
 * artifact lane 20s 一轮做。
 *
 * 而前端只在展开时调**一次**,`artifact.state` 就此定格在 `preparing`。
 * 🔴 **这不是"慢",是"永远不会变"** —— 没有第二次读,状态就没有第二次机会。
 *
 * ## 怎么读第二次:没有 GET,就用幂等 POST
 *
 * 这条链**没有** artifact 的 GET 端点。但同 `Idempotency-Key` + 同 request_hash 再 POST,
 * `claim_artifact` 走回放分支、`_LOCK_EXISTING` 读**当前那一行**并返回它的 `state`
 * (`services/geo_douyin/artifact_prepare.py:90-103`)。所以**再 POST 一次就是一次读**,
 * 且不会重复 claim、不会重复外调。这一层的全部设计都建立在这个事实上。
 *
 * ## 取值域(逐个写入点枚举,不是猜)
 *
 * `geo_douyin_publish_artifacts.state` 恰好四个:
 *   · `preparing` —— claim 时的初值
 *   · `ready`     —— `mark_ready`(全部图就位)
 *   · `failed`    —— `mark_failed`
 *   · `unknown`   —— `mark_unknown`(远端已接受、本地 ack/URL 丢失)
 * 🔴 后端 `RETRYABLE_STATES = {failed}`,`unknown` **一律拒绝重试**,
 *    它自己的注释写着「不自动重传是硬约束,不是保守选择」——
 *    因为远端可能已经收了,重传等于可能发两次。这一层照搬这个边界,不自己放宽。
 */

/** 后端会写出的四个状态。认不出的一律当作**还没结束**(宁可多读一次,不谎称结束)。 */
export type ArtifactPollState = 'preparing' | 'ready' | 'failed' | 'unknown';

/** 轮询节奏与两道时间闸。都写在这里,别散进组件。 */
export const POLL_INTERVAL_MS = 3000;
/** 超过这条线仍在 preparing ⇒ 说一句"比平时慢",但**继续读**。 */
export const SLOW_NOTICE_MS = 120_000;
/** 超过这条线 ⇒ **停**,并说清楚为什么停、接下来能做什么。 */
export const GIVE_UP_MS = 300_000;

export interface PollPlan {
    /**
     * 这个计划是**哪个状态**算出来的(认不出的一律归 `preparing`)。
     *
     * 🔴 [a3 附注] 加它是为了让下游按**状态**判事,不要去 `includes` 文案。
     *    第一版 `artifactLine` 判「这句话里有没有『不会重复扣算力』」——
     *    那是把行为拴在一串中文上:改一个字(甚至只加个标点)锁就静默失效,
     *    而"钱的承诺被挤掉"这件事没有任何一格会红。
     */
    state: ArtifactPollState;
    /** 还要不要再读一次 */
    keepPolling: boolean;
    /** 距下一次读多少毫秒(keepPolling 为假时无意义) */
    delayMs: number;
    /** 到终态了没(ready/failed/unknown 都算) */
    terminal: boolean;
    /** 这一格能不能给「重新准备」——只有 failed 能 */
    retryable: boolean;
    /** 屏幕上那一行字。**不含裸 state 串。** */
    line: string;
    /** 停下来的原因(keepPolling 为假且非终态时才有) */
    stoppedReason: string;
}

const int = (v: unknown): number => {
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 ? n : 0;
};

/**
 * 算这一刻该干什么。
 *
 * @param state    服务端刚返回的 state(认不出的按 preparing 处理)
 * @param elapsedMs 从第一次 prepare 到现在过了多久(**由调用方传**,
 *                  这样判据能用假时钟把 120s / 300s 两条线直接跑到)
 * @param attempts 已经读过几次(含第一次 prepare)
 *
 * 🔴 这个函数**不碰时钟、不碰网络**:它只是把"现在什么状态、过了多久"翻译成
 *    "还读不读、屏幕上说什么"。时间从参数进来,判据才测得了那两条线 ——
 *    把 `Date.now()` 写在里面,120s/300s 这两格就只能靠等,等于没法测。
 */
export function planNextPoll(
    state: unknown, elapsedMs: unknown, attempts: unknown,
): PollPlan {
    const raw = typeof state === 'string' ? state.trim() : '';
    const ms = int(elapsedMs);
    const n = Math.max(1, Math.floor(int(attempts)) || 1);

    if (raw === 'ready') {
        return {
            state: 'ready',
            keepPolling: false, delayMs: 0, terminal: true, retryable: false,
            line: '素材已就绪', stoppedReason: '',
        };
    }
    if (raw === 'failed') {
        return {
            state: 'failed',
            keepPolling: false, delayMs: 0, terminal: true, retryable: true,
            line: '这篇素材准备失败', stoppedReason: '',
        };
    }
    if (raw === 'unknown') {
        /* 🔴 不给重试:远端可能已经收了,重传就是可能发两次(后端硬约束)。 */
        return {
            state: 'unknown',
            keepPolling: false, delayMs: 0, terminal: true, retryable: false,
            line: '结果未知,人工核对中,不会重复扣算力', stoppedReason: '',
        };
    }

    /* 到这儿都算"还在准备"(含认不出的状态)。 */
    if (ms >= GIVE_UP_MS) {
        return {
            state: 'preparing',
            keepPolling: false, delayMs: 0, terminal: false, retryable: false,
            line: '素材还没准备好',
            /* 🔴 停下来要说清楚**它没失败**、也说清楚下一步 ——
               只说"超时"会被读成"这次白花钱了"。 */
            stoppedReason: '已经等了 5 分钟还没就绪,先不继续查了。素材准备还在后台进行,'
                + '收起这个面板、过一会儿再打开就会重新查。',
        };
    }
    const slow = ms >= SLOW_NOTICE_MS;
    return {
        state: 'preparing',
        keepPolling: true,
        delayMs: POLL_INTERVAL_MS,
        terminal: false,
        retryable: false,
        /* 🔴 「第 N 次检查」是给用户看的**进度证据**:只写"正在准备"的话,
           卡住和正常进行长得一模一样 —— 这一单的现场就是那个样子。 */
        line: slow
            ? `素材准备比平时慢 · 已检查 ${n} 次,还在等后台上传`
            : `正在准备发布素材 · 第 ${n} 次检查`,
        stoppedReason: '',
    };
}

/* ── failed 的出口:下一次展开换一把钥匙 ────────────────────────────
 *
 * 🔴 Review 2026-09-13 裁定(WO_196 §4):**不加**「重新准备」按钮(N7d 那条极简锁不放宽),
 *    但 `failed` 不能没有出口 ⇒ `state=failed` 时,面板**下一次展开**换一把新的
 *    Idempotency-Key 重新 claim(后端 `RETRYABLE_STATES` 本就允许 failed 重来);
 *    `unknown` 保持终态、同 key 不换(远端可能已经收了,换 key 就是可能发两次)。
 *
 * 🔴 **换钥匙不能靠加后缀**。后端 `normalize_uuid(request_id)` 会把非 UUID 形状
 *    直接 400(`api/geo_image_note_api.py:1569`;那段自记「`prep-<id>-<rev>` 这种老形态
 *    会在 INSERT 那一刻 500」)。所以换的是**派生输入**,不是在 UUID 后面接字符串 ——
 *    同一只 `stableRequestId` 换一个 seed,出来的还是合法 UUID。
 *    (我上一版删掉的 `retryKeySuffix` 正是"接后缀"那种写法,它一上线就是 400。)
 */

/** 第 N 次尝试用的派生种子。N=1 与老行为完全一致(收起再打开、刷新都是同一把)。 */
export function prepSeed(postId: unknown, revisionId: unknown, attempt: unknown): string {
    const n = Math.max(1, Math.floor(int(attempt)) || 1);
    const base = `${int(postId)}-${int(revisionId)}`;
    return n <= 1 ? base : `${base}#${n}`;
}

/** 这一格的尝试次数存哪儿(每标签页一份:换钥匙是这一次会话里的补救,不该跨设备)。 */
export function prepAttemptStorageKey(postId: unknown, revisionId: unknown): string {
    return `imgnote_prep_attempt:${int(postId)}-${int(revisionId)}`;
}

/**
 * 看到这个状态之后,**下一次展开**该用第几次尝试。
 *
 * 🔴 只有 `failed` 递增。`ready` 不用换(已经成了),`unknown` **一定不能换** ——
 *    换 key = 再 claim 一条 = 远端可能收两次;`preparing` 还没结束,换了就是
 *    每次展开都 claim 一条新的(幂等根要防的正是这个)。
 */
export function nextAttemptAfter(state: unknown, current: unknown): number {
    const now = Math.max(1, Math.floor(int(current)) || 1);
    return String(state || '').trim() === 'failed' ? now + 1 : now;
}

/*
 * 🔴 `retryKeySuffix` **已删**。它是给「重新准备」按钮用的,而那颗按钮按
 *    `verify-image-note-publish` N7d(#186/#188 极简裁定)不许存在。
 *    留一个没有消费方的导出,判据可以对着空壳报绿 —— #186 的
 *    `IMAGE_NOTE_PUBLISH_CLOSED_NOTE` 刚因为同一个理由被删过。
 *    `PollPlan.retryable` 保留:它说的是**后端允不允许**(事实),
 *    与「界面上给不给按钮」(产品裁定)是两件事,别混成一件。
 */

/* ── 屏幕上那一行到底显示什么(一处谓词一处实现)───────────────────
 *
 * 🔴 [#196 a3] 优先级:**服务端原话 > 前端 catch 合成的话 > 计划层的兜底句**。
 *    改前只读 `user_message`,而那是前端自己在 catch 分支造的;
 *    C 在 200 回包里发的 `failure_reason` **没有任何人读** —— 前后端各自全绿,
 *    字段到了却没上屏。(同族:一处谓词写两遍,必有一处没验。)
 * 🔴 原话**原样显示、不再加工**:后端已保证它是人话且脱敏(196-c1b)。
 *
 * 🔴 **但"原话优先"不能一刀切** —— 这是写判据时当场撞出来的:
 *    `unknown` 那句「结果未知,人工核对中,**不会重复扣算力**」里带着一个**关于钱的承诺**,
 *    而服务端原话只说"第 2 张图读取为空"之类的**原因**。让原话把它整句挤掉,
 *    等于把"不会重复扣你的钱"这句话删了 —— 那是用户此刻最想知道的一件事。
 *    ⇒ `unknown` 时两句**都给**:先承诺,再原因。`failed` 时原话替掉那句泛泛的
 *    「这篇素材准备失败」(原话说的是同一件事,而且更具体)。
 */
export function artifactLine(
    artifact: { failure_reason?: string | null; user_message?: string | null } | null | undefined,
    plan: PollPlan | string | null | undefined,
): string {
    const planLine = typeof plan === 'string' ? plan : (plan?.line || '');
    /*
     * 🔴 [a3 附注订正] 按**状态**判,不按文案判。
     *    `unknown` 是唯一"计划那句话里有服务端原话给不出的东西(钱的承诺)"的状态;
     *    判它就是判这件事本身。改前写的是 `planLine.includes('不会重复扣算力')` ——
     *    文案改一个字,承诺就被静默挤掉,没有一格会红。
     */
    const keepPlanLine = typeof plan !== 'string' && plan?.state === 'unknown';
    const server = typeof artifact?.failure_reason === 'string' ? artifact.failure_reason.trim() : '';
    const local = typeof artifact?.user_message === 'string' ? artifact.user_message.trim() : '';
    const reason = server || local;
    if (!reason) return planLine;
    /* 承诺句留着,原因接在后面 */
    return keepPlanLine ? `${planLine} · ${reason}` : reason;
}
