/**
 * 防御型 GEO v2 façade 客户端 —— 规格 §15.2 / §15.3 / §15.8。
 *
 * 三条铁律,都在本文件里兑现:
 *
 * 1. **前端不造话**(§0.5.5 U-1/U-2)。服务端每个响应都带 `*UserLabel` /
 *    `publicExplanation` / `nextAction.label`,前端**只渲染**它们。
 *    本文件不含任何把 code 翻成中文的映射表 —— 有映射表就意味着有第二套口径。
 * 2. **`IDEMPOTENCY_CONFLICT` 永不上屏**(U-2 逐字)。服务端靠"copy_registry
 *    里没有它"来实现;前端这边把它归类成 `silent`,由调用方静默重放。
 * 3. **Idempotency-Key 由前端签发并在重试间保持不变**(§15.3)。
 *    每次重试换一个 key = 每次都是新命令,幂等就没了。
 */

/** §15.8 错误信封。`publicExplanation` 可能**缺席** —— 缺席即"这条不该给用户看"。 */
export interface DefGeoErrorEnvelope {
    code: string;
    publicExplanation?: string;
    nextAction?: { kind: string; label: string; actionRef: string; target: unknown };
    details?: Record<string, unknown>;
}

export class DefGeoError extends Error {
    readonly code: string;
    readonly httpStatus: number;
    readonly envelope: DefGeoErrorEnvelope;

    constructor(httpStatus: number, envelope: DefGeoErrorEnvelope) {
        super(envelope.code);
        this.name = 'DefGeoError';
        this.code = envelope.code;
        this.httpStatus = httpStatus;
        this.envelope = envelope;
    }

    /**
     * 这条错误能不能给用户看?
     *
     * 🔴 [G5 门二返修 2026-08-22] 判据从「服务端给没给 publicExplanation」
     *    收窄成「**是不是 IDEMPOTENCY_CONFLICT**」。
     *
     *    原设计把两件事混成了一件:
     *      ① 这条错误**不该**给用户看(只有幂等冲突属于此类 —— 同一条命令已在跑,
     *         弹框只会让她以为自己搞砸了);
     *      ② 服务端**还没给**这条 code 配译文(registry 漏配、或新 code 先上线)。
     *    ②被当成①的后果是:**错误静默消失**,她点了按钮什么都没发生,
     *    连"失败了"都不知道 —— 比显示一句笼统的话糟糕得多。
     *    现在②走 `userSentence` 的兜底人话,只有①真正静默。
     */
    get isSilent(): boolean {
        return this.code === 'IDEMPOTENCY_CONFLICT';
    }

    /**
     * 上屏用的人话。永远不回落成 code —— 回落 = 内部枚举裸串上屏 = 验收红。
     *
     * 兜底句同时把钱说清楚:失败路径上"钱动没动"是她最关心的事,
     * 而没有译文的那条 code 恰恰是我们最不了解的一条,更要把话说死。
     */
    get userSentence(): string {
        return this.envelope.publicExplanation
            ?? '没能完成,请再试一次;没有扣除任何算力';
    }

    /** 出口。§0.5.6「任何阻塞与错误必须自带解决方案」。 */
    get nextActionLabel(): string | null {
        return this.envelope.nextAction?.label ?? null;
    }
}

export type QuestionSource = 'system' | 'user' | 'ai';

export interface DraftQuestion {
    text: string;
    modeSide: 'defensive' | 'offensive';
    familyKey: string;
    brandExposure: 'named' | 'unnamed' | 'comparison';
    /** 仅前端用的来源标签(§9.2「以来源标签区分」),**不**发给服务端。 */
    source: QuestionSource;
    /**
     * 仅前端:这道题是 hybrid 缺侧时**系统补位**加进来的(WO 2026-09-02 §3.1.5)。
     * 必须在列表里看得见 —— 用户看不见的题不能进题数与计价。
     */
    isFiller?: boolean;
    /**
     * 🔴 [#143③ 2026-09-07] 仅前端:这一行**还在打字,尚未提交**。
     *
     * 缺了它的后果(Owner 线上亲报):编辑器每敲一个字符就 `onManualChange`,
     * 题数当场变 1、算价请求当场发出 —— 输入「s」屏幕就说「已有 1 道问题」。
     * 计数与计价的**触发时机**必须是「提交」不是「输入」。
     *
     * 语义刻意是**否定式且可缺省**:`undefined` = 已提交。
     * 这样一来历史数据(从诊断记录回填的题、AI 预填的题)一个字不用改就仍然计数,
     * 只有编辑器新建的空行才带 `draft: true`,提交后清掉。
     */
    draft?: boolean;
    /**
     * 🔴 [#149] 漏斗层,由后端出题接口带来(`SuggestedQuestion.layer`)。
     *
     * **自由字符串,不是枚举** —— 取值域归生成器所有(`super_tier1` / `brand_awareness` / …)。
     * 前端写死枚举校验它,会在生成器新增一层时把整页打成 500(C 明确警告,#139 那种炸法)。
     * 带着它一路送回 `question_meta`,后端就不必对已知层的题再花一次 LLM 归层。
     */
    layer?: string;
}

export interface PlannedQuestion {
    questionIdentityKey: string;
    questionRevision: number;
    globalOrdinal: number;
    text: string;
    modeSide: string;
    familyKey: string;
    brandExposure: string;
}

export interface QuestionPlanResponse {
    planId: string;
    planRevision: number;
    canonicalHash: string;
    canonicalHashVersion: string;
    brandId: number;
    profileRevisionId: string;
    mode: string;
    modeUserLabel: string;
    questions: PlannedQuestion[];
    counts: { defensive: number; offensive: number; total: number };
    /**
     * 计价**规则**(不是本次报价)。服务端在 preview 成功时下发,
     * `basePoints` 与真实算价走同一个 `_price_for_plan`(C 的判据钉住了同源)。
     * 🔴 前端**只渲染**它,不用它做任何乘法 —— 总价仍只能来自 run-previews。
     */
    pricingRule?: {
        basePoints: number;
        freeQuestions: number;
        extraPerQuestion: number;
        ruleVersion: string;
    };
    expiresAt: string;
    copyRegistryVersion: string;
    idempotentReplay: boolean;
}

export interface RunPreviewResponse {
    previewId: string;
    canonicalHash: string;
    lifecycle: string;
    lifecycleUserLabel: string;
    fundingPolicy: string;
    fundingPolicyUserLabel: string;
    campaignMode: string;
    modeUserLabel: string;
    questionPlanId: string;
    questionPlanRevision: number;
    plannedCells: number;
    basePoints: number;
    extraPoints: number;
    exactTotalPoints: number;
    costUserLabel: string;
    expiresAt: string;
    canConfirm: boolean;
    nextAction: { kind: string; label: string; actionRef: string; target: unknown };
    copyRegistryVersion: string;
    runStatusProjectionVersion: string;
    idempotentReplay: boolean;
}

export interface RunConfirmResponse {
    diagnosisCommandId: string;
    runId: string;
    /**
     * 🔴 [门三 G8 · 门八第二发现] legacy 进度端点/WS 真正校验的那把键。
     *
     * `runId` 是 **run_token**;`auth.session_access.authorize_session` 查的是
     * `diagnosis_runs.session_id`,而 `session_id = "defgeo_" + run_token` ——
     * 两者**不相等**。拿 `runId` 去拼 `/diagnosis/progress/:id`,
     * 归属查询一条都命中不到,fail-closed 直接 403:
     * 她刚付完钱,看到的是「无权访问该诊断进度」。
     *
     * 后端 `RunConfirmResponse.progressSessionId` 早就把这把键交出来了
     * (`api/defensive_geo_api.py` 那段注释写得很清楚),
     * 是这个**前端类型**没收 —— 于是没人能用它。跳进度页只许用这个字段。
     */
    progressSessionId: string;
    statusUrl: string;
    idempotentReplay: boolean;
    fundingPolicy: string;
    principalKind: string;
    billingModeProjection: string;
    fundingState: string;
    fundingHandle: { kind: string; ref: string; approvalRef?: string | null };
    sponsorPolicyRef?: string | null;
    exactTotalPoints: number;
    runStateUserLabel: string;
    fundingStateUserLabel: string;
    costUserLabel: string;
    nextAction: { kind: string; label: string; actionRef: string; target: unknown };
    copyRegistryVersion: string;
    runStatusProjectionVersion: string;
}

const BASE = '/api/defensive-geo';

function authHeaders(): Record<string, string> {
    const token = localStorage.getItem('omnirank_token');
    return token ? { Authorization: `Bearer ${token}` } : {};
}

async function call<T>(
    path: string,
    init: RequestInit & { idempotencyKey?: string } = {},
): Promise<T> {
    const { idempotencyKey, ...rest } = init;
    const res = await fetch(BASE + path, {
        ...rest,
        headers: {
            'Content-Type': 'application/json',
            ...authHeaders(),
            ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
            ...(rest.headers as Record<string, string> | undefined),
        },
    });
    const raw = await res.text();
    let body: unknown = null;
    try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }

    if (!res.ok) {
        // FastAPI 把 _safe_error 的 payload 放在 detail 里。
        const detail = (body as { detail?: DefGeoErrorEnvelope })?.detail;
        const envelope: DefGeoErrorEnvelope =
            detail && typeof detail === 'object' && 'code' in detail
                ? detail
                : { code: 'INTERNAL_ERROR' };
        throw new DefGeoError(res.status, envelope);
    }
    return body as T;
}

/** §15.2 题单预览。`clientRequestId` 稳定 ⇒ 同一份编辑不会造出两个 plan。 */
export function previewQuestionPlan(input: {
    clientRequestId: string;
    brandId: number;
    profileRevisionId: string;
    mode: 'defensive' | 'offensive' | 'hybrid';
    questions: DraftQuestion[];
}): Promise<QuestionPlanResponse> {
    return call<QuestionPlanResponse>('/question-plans/preview', {
        method: 'POST',
        body: JSON.stringify({
            clientRequestId: input.clientRequestId,
            brandId: input.brandId,
            profileRevisionId: input.profileRevisionId,
            mode: input.mode,
            // source 是前端标签,**不发给服务端** —— 发过去会被 extra=forbid 打成 422。
            questions: input.questions.map((q) => ({
                text: q.text,
                modeSide: q.modeSide,
                familyKey: q.familyKey,
                brandExposure: q.brandExposure,
            })),
        }),
    });
}

export function getQuestionPlan(planId: string): Promise<QuestionPlanResponse> {
    return call<QuestionPlanResponse>(`/question-plans/${encodeURIComponent(planId)}`);
}

/** §15.3 第一阶段:算价但**不扣费**。 */
export function createRunPreview(
    input: {
        questionPlanId: string;
        questionPlanRevision: number;
        profileRevisionId: string;
        platformKeys: string[];
    },
    /**
     * 🔴 [G4 门二返修 2026-08-22] preview 也要幂等键。
     *
     * 算价本身零资金副作用,但它会**建一条 preview 行**;没有幂等键时,
     * 网络抖动重试会造出第二条 preview —— 于是「她看到的那一版」与
     * 「她确认的那一版」可能不是同一条,所见即所签就断了。
     *
     * 与 confirm 的键**分开**(两个不同的命令,不该互相顶掉),
     * 但各自在重试之间**保持不变** —— 与 LaunchPanel 里
     * `useState(newIdempotencyKey)` 同款纪律。
     */
    idempotencyKey?: string,
): Promise<RunPreviewResponse> {
    return call<RunPreviewResponse>('/run-previews', {
        method: 'POST',
        body: JSON.stringify(input),
        idempotencyKey,
    });
}

export function getRunPreview(previewId: string): Promise<RunPreviewResponse> {
    return call<RunPreviewResponse>(`/run-previews/${encodeURIComponent(previewId)}`);
}

/**
 * §15.3 第二阶段:所见即所签。
 *
 * `expectedHash` 必须是**用户看到的那一版** preview 的 hash ——
 * 传 latest 就等于"她确认的是 A,系统跑的是 B"。
 * `idempotencyKey` 在重试之间**必须不变**。
 */
export function confirmRunPreview(
    previewId: string,
    expectedHash: string,
    idempotencyKey: string,
): Promise<RunConfirmResponse> {
    return call<RunConfirmResponse>(
        `/run-previews/${encodeURIComponent(previewId)}/confirm`,
        { method: 'POST', body: JSON.stringify({ expectedHash }), idempotencyKey },
    );
}

/** 一次会话内稳定的幂等键。重试复用同一个,换一次就是换一条命令。 */
export function newIdempotencyKey(): string {
    const c = globalThis.crypto;
    if (c && 'randomUUID' in c) return `defgeo-${c.randomUUID()}`;
    return `defgeo-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
