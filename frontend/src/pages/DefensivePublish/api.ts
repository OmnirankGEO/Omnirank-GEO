/**
 * 防御型 GEO 发布 v2 façade 的前端调用面。
 *
 * 前缀 `/api/defensive-geo/publish/**` —— **不是** `/api/publish/**`。
 * (后端 docstring 已实证碰撞:`api/publish_api.py:1168` 有一条同名但
 *  `snapshot_id: int` 的 legacy 路由;两条注册在一起会互相遮蔽。)
 *
 * ══════════════════════════════════════════════════════════════════════
 * 🔴 API-02 / UI-34:2xx 的 pending 响应**不许**被 error 拦截器吃掉
 * ══════════════════════════════════════════════════════════════════════
 * census 实测(2026-08-21,本树):
 *   · `frontend/src/lib/api.ts:677` 的 `axios.create()` **没有** `validateStatus`;
 *   · 全仓 `grep -rn "validateStatus" frontend/src/` = 0 命中。
 * 也就是说当前走 axios 默认 `status >= 200 && status < 300` —— 202 落在 success 分支。
 *
 * 但「今天默认值是对的」不是判据。默认值是**别人可以随手改的全局**,
 * 而这条链上 202 被当成错误的后果是:命令其实已经建了、算力其实已经冻结了,
 * 前端却给用户报一个红色失败,用户再点一次 —— 幂等键不同就是第二条命令。
 *
 * 所以本文件的每一次请求都**显式**带上 `validateStatus`,把这条保证钉在调用点上:
 * 全局默认再怎么被改,`ACCEPT_ANY_2XX` 仍然让 200/201/202/204 走 data 分支。
 * 这也让变异有地方打:把它改成 `s === 200`,pending 用例当场转红。
 */

import type { AxiosRequestConfig } from 'axios';
import api from '@/lib/api';
import type { DeliveryTodoResponse } from './deliveryTodo';
import type {
    CommandStatusResponse,
    ConfirmResponse,
    ReviewActionResponse,
    ReviewQueueResponse,
    SnapshotResponse,
} from './contracts';

/** 🔴 API-02 的实现点。含 202 Accepted 在内的全部 2xx = 成功,交给业务层判 pending。 */
export const ACCEPT_ANY_2XX = (status: number): boolean => status >= 200 && status < 300;

const BASE = '/api/defensive-geo/publish';

function cfg(extra?: AxiosRequestConfig): AxiosRequestConfig {
    return { ...extra, validateStatus: ACCEPT_ANY_2XX };
}

/**
 * 幂等键。**一次用户意图一个键**,重试同一个意图必须复用同一个键 ——
 * 所以它由调用方在「打开确认弹窗」时生成并存进 state,而不是在每次
 * `await` 之前现生成(现生成 = 用户点两次就是两条命令)。
 */
export function newIdempotencyKey(prefix: string): string {
    // 🔴 不用 `crypto.randomUUID()` —— 它要 Chrome 92,而本仓
    //    `package.json` 的 browserslist 底线是 `chrome >= 90`,
    //    build 链里的 `verify-browser-baseline.mjs` 会当场判红。
    //    `getRandomValues` 在底线内(Chrome 11+ / Safari 6.1+)。
    let rand: string;
    const c = typeof crypto !== 'undefined' ? crypto : undefined;
    if (c && typeof c.getRandomValues === 'function') {
        const buf = new Uint8Array(16);
        c.getRandomValues(buf);
        rand = Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
    } else {
        rand = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
    }
    return `${prefix}-${rand}`.slice(0, 200);
}

// ══════════════════════════════════════════════════════════════════════════
// UI-35:决策快照 —— 恢复走 exact GET,**永不重 POST preview**
// ══════════════════════════════════════════════════════════════════════════

/**
 * 🔴 刷新 / 后退 / 换设备都走这一条。
 *
 * 为什么不能退回去重 POST `preview`:preview 会**签发一份新的冻结面**并把旧的
 * CAS 成 superseded(`_build_new_preview` → `_store.supersede_open`)。
 * 也就是说「刷新一下页面」会让用户刚才看的那份报价当场作废、价格重算。
 * GET 则明确「不重新推荐、不读 latest、不延长有效期」。
 */
export async function fetchDecisionSnapshot(snapshotId: string): Promise<SnapshotResponse> {
    const res = await api.get<SnapshotResponse>(
        `${BASE}/decision-snapshots/${encodeURIComponent(snapshotId)}`, cfg());
    return res.data;
}

export async function confirmDecisionSnapshot(
    snapshotId: string,
    body: { expectedHash: string; expectedVersion: number },
    idempotencyKey: string,
): Promise<ConfirmResponse> {
    const res = await api.post<ConfirmResponse>(
        `${BASE}/decision-snapshots/${encodeURIComponent(snapshotId)}/confirm`,
        body,
        cfg({ headers: { 'Idempotency-Key': idempotencyKey } }),
    );
    return res.data;
}

/** MED-10:只允许送 opaque option id + expected hash/version + 理由。 */
export async function overrideDecisionSnapshot(
    snapshotId: string,
    body: {
        expectedHash: string;
        expectedVersion: number;
        selectedPublicMediaOptionId: string;
        actorReason: string;
    },
    idempotencyKey: string,
): Promise<SnapshotResponse> {
    const res = await api.post<SnapshotResponse>(
        `${BASE}/decision-snapshots/${encodeURIComponent(snapshotId)}/override`,
        body,
        cfg({ headers: { 'Idempotency-Key': idempotencyKey } }),
    );
    return res.data;
}

/** 「先不选这家媒体」。零 command / 零冻结 / 零外调 —— U-4 那句「没有扣除任何算力」的依据。 */
export async function cancelDecisionSnapshot(
    snapshotId: string,
    body: { expectedHash: string; expectedVersion: number },
    idempotencyKey: string,
): Promise<SnapshotResponse> {
    const res = await api.post<SnapshotResponse>(
        `${BASE}/decision-snapshots/${encodeURIComponent(snapshotId)}/cancel`,
        body,
        cfg({ headers: { 'Idempotency-Key': idempotencyKey } }),
    );
    return res.data;
}

// ══════════════════════════════════════════════════════════════════════════
// UI-34 / UI-36:命令状态
// ══════════════════════════════════════════════════════════════════════════

export async function fetchCommandStatus(
    commandId: string,
    signal?: AbortSignal,
): Promise<CommandStatusResponse> {
    const res = await api.get<CommandStatusResponse>(
        `${BASE}/commands/${encodeURIComponent(commandId)}`, cfg({ signal }));
    return res.data;
}

export async function retryChild(
    commandId: string,
    body: { expectedStatusVersion: number; expectedCommandHash: string },
    idempotencyKey: string,
): Promise<ConfirmResponse> {
    const res = await api.post<ConfirmResponse>(
        `${BASE}/commands/${encodeURIComponent(commandId)}/retry-child`,
        body,
        cfg({ headers: { 'Idempotency-Key': idempotencyKey } }),
    );
    return res.data;
}

/** Z-1:服务商提交线下核实凭证。**零资金副作用**。 */
export async function submitVerificationEvidence(
    commandId: string,
    body: { evidenceKind: string; evidenceText: string },
): Promise<ReviewActionResponse> {
    const res = await api.post<ReviewActionResponse>(
        `${BASE}/commands/${encodeURIComponent(commandId)}/verification-evidence`,
        body,
        cfg(),
    );
    return res.data;
}

// ══════════════════════════════════════════════════════════════════════════
// Z-1:平台 admin 资金核验队列
// ══════════════════════════════════════════════════════════════════════════

export async function fetchSettlementReviewQueue(
    params: { limit?: number; offset?: number } = {},
): Promise<ReviewQueueResponse> {
    const res = await api.get<ReviewQueueResponse>(
        `${BASE}/admin/settlement-review`,
        cfg({ params: { limit: params.limit ?? 50, offset: params.offset ?? 0 } }),
    );
    return res.data;
}

export async function applySettlementReviewAction(
    commandId: string,
    body: { action: string; reason?: string },
): Promise<ReviewActionResponse> {
    const res = await api.post<ReviewActionResponse>(
        `${BASE}/admin/settlement-review/${encodeURIComponent(commandId)}`,
        body,
        cfg(),
    );
    return res.data;
}

// ══════════════════════════════════════════════════════════════════════════
// U-5:交付待办清单(聚合页的取数)
// ══════════════════════════════════════════════════════════════════════════

/**
 * 🔴 **只读**。这个端点不签出任何 preview、不冻任何算力 ——
 *    聚合的是呈现,不是资金(后端 docstring 同一口径)。
 *    如果哪天它开始有副作用,「打开待办页」就会变成「把这一单全签了」。
 */
export async function fetchDeliveryTodo(
    acceptedSnapshotId: number,
    signal?: AbortSignal,
): Promise<DeliveryTodoResponse> {
    const res = await api.get<DeliveryTodoResponse>(
        `${BASE}/delivery-todo`,
        cfg({ params: { acceptedSnapshotId }, signal }),
    );
    return res.data;
}
