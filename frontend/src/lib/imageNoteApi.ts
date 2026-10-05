/**
 * 图文发布链 · 前端唯一取数口(#186 极简版)。
 *
 * 本文件是 `6b491ab23^:frontend/src/lib/imageNoteApi.ts`(453 行)的**减法恢复**:
 * 只留发布这一步用得到的四个端点 + 两张人话表 + 幂等 id 生成。
 * 制作链那半(delivery plan / production preview / draft / batch)**没有恢复** ——
 * 那条路当时归 `pages/Writing/imageNoteStudioApi.ts`(#150/#179),两处都留就是同一个谓词
 * 两份实现。[WO_271] 那个文件随 #204 a2 制作台退役删了(df9c142c5);现在整批下单的请求体
 * 由 `pages/Writing/imageNoteProduction.ts` 的 productionBatch 拼,本文件仍只管发布这一步。
 *
 * 🔴 本模块**一个数字都不算**。价格、容量、资格全部原样透传服务端字段;
 *    没有换算函数,就没有地方可以偷偷换算 —— 这是结构保证,不是纪律要求。
 *
 * 🔴 错误一律走后端统一错误合同(code / message / reason / impact /
 *    repair_hint / actions / next_action / retryable),前端不自造文案:
 *    自造文案会在后端改口径时静默变成谎话。
 */
import { authFetch } from '@/lib/api';

/** 后端统一错误合同。 */
export interface ContractError {
    code: string;
    message: string;
    reason?: string;
    impact?: string;
    repair_hint?: string;
    actions?: { id: string; label: string; type: string }[];
    next_action?: string;
    retryable?: boolean;
    command_id?: string;
}

export class ImageNoteError extends Error {
    contract: ContractError;
    httpStatus: number;
    constructor(contract: ContractError, httpStatus: number) {
        super(contract.message || '操作未完成');
        this.contract = contract;
        this.httpStatus = httpStatus;
    }
}

async function call<T>(path: string, init?: RequestInit & { headers?: Record<string, string> }): Promise<T> {
    const res = await authFetch(path, init as RequestInit);
    let body: unknown = null;
    try {
        body = await res.json();
    } catch {
        body = null;
    }
    if (!res.ok) {
        // FastAPI 的 HTTPException(detail=dict) 会包一层 detail;两种形态都接。
        const b = (body || {}) as Record<string, unknown>;
        const raw = (b.detail && typeof b.detail === 'object' ? b.detail : b) as Record<string, unknown>;
        throw new ImageNoteError({
            code: String(raw.code || `HTTP_${res.status}`),
            message: String(raw.message
                || (typeof b.detail === 'string' ? b.detail : '')
                || '操作未完成'),
            reason: raw.reason as string | undefined,
            impact: raw.impact as string | undefined,
            repair_hint: raw.repair_hint as string | undefined,
            actions: raw.actions as ContractError['actions'],
            next_action: raw.next_action as string | undefined,
            retryable: raw.retryable as boolean | undefined,
            command_id: typeof raw.command_id === 'string' ? raw.command_id : undefined,
        }, res.status);
    }
    return body as T;
}

// ── 素材准备 ────────────────────────────────────────────────────────
export type ArtifactState = 'preparing' | 'ready' | 'failed' | 'unknown';

export interface PreparedArtifact {
    geo_post_id: number;
    post_revision_id: number;
    state: ArtifactState;
    prepared_artifact_id?: number | string | null;
    manifest_hash?: string | null;
    /**
     * [#196 c1] 服务端给的**失败原话**(`state` 为 failed/unknown 时非空,其余恒空串)。
     * 🔴 与下面那个 `user_message` **不是一回事**:`user_message` 是**前端自己**
     *    在 catch 分支合成的(HTTP 错误合同的 message);`failure_reason` 是服务端
     *    200 回包里的原话(worker 写在 `card_statuses[].reason` 上,C 的 f038c54f1)。
     *    改前这里只有 `user_message`,于是**字段到了没人读** —— 两边各自全绿。
     */
    failure_reason?: string | null;
    user_message?: string | null;
}

export function preparePublishMedia(postId: number, idempotencyKey: string) {
    return call<PreparedArtifact>(
        `/api/geo-douyin/posts/${postId}/prepare-publish-media-v2`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
            body: JSON.stringify({ idempotency_key: idempotencyKey }),
        });
}

// ── 预览(**唯一**价格来源)─────────────────────────────────────────
export interface PublishPreviewItem {
    item_request_id: string;
    geo_post_id: number;
    post_revision_id: number;
    media_id: number;
    /** 🔴 服务端权威逐项价。前端只显示,不参与任何加减乘除。 */
    final_price_points: number;
    publish_price_fingerprint: string;
    eligibility?: { eligible: boolean; reason?: string | null; today_remaining?: number | null };
}

export interface PublishPreview {
    status: string;
    items: PublishPreviewItem[];
    total_price_points: number;
}

export function previewPublish(payload: {
    quote_id?: number | null;
    brand_id: number;
    items: { item_request_id: string; geo_post_id: number; post_revision_id: number;
             prepared_artifact_id: string; manifest_hash: string; media_id: number }[];
}) {
    return call<PublishPreview>('/api/meijiehezi/image-notes/publish-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

// ── 提交 ────────────────────────────────────────────────────────────
/**
 * 🔴 每项**恰好七个键**,内容(标题/正文/图)**不在请求体里** ——
 *    它来自冻结版本(`post_revision_id` + `prepared_artifact_id` + `manifest_hash`)。
 *    往请求体里塞 title 就等于给了一条"绕过冻结版本改内容"的路,判据 N1 钉死这一点。
 *
 * 🔴 旧版本(`6b491ab23^`)还发了第八个键 `expected_price_points`。**本版不发**:
 *    2026-09-13 实测全后端**零读取**(只有模型声明与一条 pytest 夹具命中),
 *    它头顶那句「服务端据此逐项比对」是假注释;真正的逐项校验走
 *    `expected_price_fingerprint`(`_recompute_items` + `assert_fingerprint_operation`)。
 *    发一个没人读的字段,只会让下一个人以为它是承重的。
 */
export function submitPublish(payload: {
    request_id: string;
    expected_total_price_points: number;
    items: { item_request_id: string; geo_post_id: number; post_revision_id: number;
             prepared_artifact_id: string; manifest_hash: string; media_id: number;
             expected_price_fingerprint: string }[];
}) {
    return call<{ status: string; command_id?: string; command_status?: string;
                  items?: { item_request_id: string; state: string; user_message?: string }[] }>(
        '/api/meijiehezi/image-notes/publish-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': payload.request_id },
            body: JSON.stringify(payload),
        });
}

// ── 提交之后的动态状态 ──────────────────────────────────────────────
export interface PublishCommandItem {
    item_request_id: string;
    geo_post_id?: number | null;
    media_id?: number | null;
    media_name?: string;
    /** 机器态;界面文案由 PUBLISH_ITEM_COPY 翻译。 */
    state: string;
    /** 🔴 服务端派生。前端**不许**自己按 state 推 —— 推错就是诱导用户重复外调。 */
    retryable: boolean;
    next_action?: string;
    failure_reason?: string | null;
    published_url?: string | null;
}

export interface PublishCommandState {
    status: string;
    command_id: string;
    command_status: string;
    items: PublishCommandItem[];
    summary?: { total: number; retryable: number };
}

/** 🔴 提交后靠它轮询。没有它页面只会显示第一次返回的 `accepted`,部分失败根本看不见。 */
export function fetchPublishCommand(commandId: string) {
    return call<PublishCommandState>(
        `/api/meijiehezi/image-notes/commands/${encodeURIComponent(commandId)}`);
}

// ── 机器态 → 人话 ───────────────────────────────────────────────────
export interface StateCopy { label: string; action?: string }

export const PUBLISH_ITEM_COPY: Record<string, StateCopy> = {
    queued: { label: '排队中,等待提交发布' },
    submitting: { label: '正在提交给发布渠道' },
    submitted: { label: '已提交，等待渠道回执' },
    pending: { label: '已提交,等待渠道接单' },
    publishing: { label: '渠道正在发布' },
    published: { label: '已发布' },
    failed: { label: '发布失败' },
    rejected: { label: '渠道未接单，请查看原因' },
    awaiting_sync: { label: '正在核对渠道结果，请勿重复提交' },
    needs_action: { label: '需要人工处理，请查看发布记录中的说明' },
    cancelled: { label: '已取消' },
    unknown: { label: '结果未知,人工核对中,不会重复扣算力' },
};

export const ARTIFACT_COPY: Record<ArtifactState, StateCopy> = {
    preparing: { label: '正在准备发布素材…' },
    ready: { label: '素材已就绪' },
    failed: { label: '这篇素材准备失败' },
    // 🔴 未知态**没有** action:给重试按钮等于诱导用户重复外调。
    unknown: { label: '结果未知,人工核对中,不会重复扣算力' },
};

/**
 * 由一串文字**确定性**派生一个 UUID 形状的 id。
 *
 * 🔴 这几个键都落进 uuid 列(`mhz_publish_order_items.item_request_id`、
 *    `geo_douyin_publish_artifacts.request_id` …)—— 「模型放行 ≠ 库放行」:
 *    形状不对时 Pydantic 全放行,**INSERT 那一刻 InvalidTextRepresentation ⇒ 500**。
 * 🔴 为什么**确定性**而不是随机:这几个键的意义就是幂等根。
 *    随机 UUID 会把"重试不重复外调"这条保证直接删掉。
 * 🔴 不用 `crypto.randomUUID`:浏览器底线 Safari iOS 16 上是 undefined。
 */
export function stableRequestId(namespace: string, key: string): string {
    const input = `${namespace}::${key}`;
    const round = (seed: number) => {
        let h = seed >>> 0;
        for (let i = 0; i < input.length; i++) {
            h ^= input.charCodeAt(i);
            h = Math.imul(h, 0x01000193) >>> 0;
        }
        return h.toString(16).padStart(8, '0');
    };
    const a = round(0x811c9dc5), b = round(0x9e3779b9);
    const c = round(0x85ebca6b), d = round(0xc2b2ae35);
    const variant = '89ab'[parseInt(c[0], 16) & 3];
    return `${a}-${b.slice(0, 4)}-4${b.slice(5, 8)}-${variant}${c.slice(1, 4)}-${c.slice(4, 8)}${d}`;
}
