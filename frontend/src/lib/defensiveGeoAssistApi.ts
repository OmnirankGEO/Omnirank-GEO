/**
 * 销售自救面三条链的前端调用面(包H · U-9 / Z-3.1 / Z-3.3)。
 *
 * 后端 = `api/defensive_geo_assist_api.py`,与 façade 共用同一个 typed 信封,
 * 所以错误一律交给 `DefGeoError`(`@/lib/defensiveGeoApi`)解析 ——
 * 不在这里另写一套错误翻译。
 *
 * 🔴 与 `pages/DefensivePublish/api.ts` 同一条纪律:每次请求**显式**带
 *    `validateStatus`,不指望全局默认。默认值是别人可以随手改的全局。
 */

import type { AxiosRequestConfig } from 'axios';
import api from '@/lib/api';

const BASE = '/api/defensive-geo';

const ACCEPT_ANY_2XX = (status: number): boolean => status >= 200 && status < 300;

function cfg(extra?: AxiosRequestConfig): AxiosRequestConfig {
    return { ...extra, validateStatus: ACCEPT_ANY_2XX };
}

export interface TypedAction {
    kind: string;
    label: string;
    actionRef: string;
    target: unknown;
}

// ── U-9 ────────────────────────────────────────────────────────────────
export interface CustomerLink {
    kind: string;
    kindLabel: string;
    /** 人话前缀,形如【诊断报告】。**服务端下发**,前端不拼第二份。 */
    prefix: string;
    url: string | null;
    status: 'available' | 'expired' | 'revoked' | 'not_ready';
    statusLabel: string;
    reissuable: boolean;
    /** 前缀 + 品牌名 + 链接,复制出去直接能发微信。 */
    wechatScript: string | null;
    blockedReason: string | null;
    /**
     * 🔴 [工单 V3-A · Codex 三审 P1-8] 面板下发的**对象引用**。
     * 重签必须把它原样带回去:服务端不再"自己再取一次最新的"。
     * 以前这一格在 DTO 里根本没声明 ⇒ 服务端发了、前端丢了、POST 只带
     * brandId+kind ⇒ 用户看 A、系统轮换 B。
     */
    objectRef: { kind: string; id?: number; token?: string } | null;
    linksVersion: string;
}

export interface CustomerLinksResponse {
    brandId: number;
    links: CustomerLink[];
    hint: string;
    copyRegistryVersion: string;
}

export async function fetchCustomerLinks(
    brandId: number, signal?: AbortSignal,
): Promise<CustomerLinksResponse> {
    const res = await api.get<CustomerLinksResponse>(
        `${BASE}/customer-links`, cfg({ params: { brandId }, signal }));
    return res.data;
}

export async function reissueCustomerLink(
    brandId: number, kind: string,
    objectRef: CustomerLink['objectRef'],
): Promise<{ brandId: number; kind: string; link: CustomerLink; publicExplanation: string }> {
    // 🔴 objectRef 是**必传**参数(不是可选):可选就等于把旧的那条
    //    "服务端自己取最新" 的不安全路径原样留着,而留着的那条不会有判据在守。
    //    对象漂移时服务端返回 409 SNAPSHOT_CHANGED,调用方应当刷新面板后重试。
    const res = await api.post(
        `${BASE}/customer-links/reissue`, { brandId, kind, objectRef }, cfg());
    return res.data;
}

// ── Z-3.1 ──────────────────────────────────────────────────────────────
export interface FactDraft {
    factKey: string;
    factLabel: string;
    targetField: string;
    suggested: unknown;
    suggestedDisplay: string;
    confident: boolean;
}

export interface AiAutofillResponse {
    brandId: number;
    drafts: FactDraft[];
    manualOnly: { key: string; label: string }[];
    publicExplanation: string;
    actions: TypedAction[];
    copyRegistryVersion: string;
}

/** 🔴 **扣算力**。调用点必须先把「要花多少」说清楚(U-4 必答时刻)。 */
export async function requestFactAutofill(
    brandId: number, requestedFactKeys: string[],
): Promise<AiAutofillResponse> {
    const res = await api.post<AiAutofillResponse>(
        `${BASE}/fact-collection/ai-autofill`,
        { brandId, requestedFactKeys },
        cfg(),
    );
    return res.data;
}

// ── Z-3.3 ──────────────────────────────────────────────────────────────
export interface LegalRepairCandidate {
    text: string;
    note: string;
}

export interface LegalRepairResponse {
    articleRevisionId: string;
    passageRef: string;
    candidates: LegalRepairCandidate[];
    publicExplanation: string;
    actions: TypedAction[];
    copyRegistryVersion: string;
}

export async function fetchLegalRepairCandidates(body: {
    articleRevisionId: string;
    ruleId: string;
    passageRef: string;
    passageExcerpt: string;
}): Promise<LegalRepairResponse> {
    const res = await api.post<LegalRepairResponse>(
        `${BASE}/legal-repair/candidates`, body, cfg());
    return res.data;
}

/**
 * [工单 C-5] 「用这一句」的**真落点**。
 *
 * 🔴 在这之前这一颗按钮只是 `navigate('/writing?revision=…&repaired=<句子>')`,
 *    而那个 `repaired` 查询参数**全仓零消费者** —— 点完什么都没发生:
 *    重新确认冻的还是同一份正文、同一个 articleHash,发布门再拦一次。
 *
 * 返回体里的 `articleHash` 与 `previousArticleHash` 不同,就是
 * 「重新确认消费的是新 revision」的端到端证据。
 */
export interface LegalRepairApplyResponse {
    articleRevisionId: string;
    passageRef: string;
    articleHash: string;
    previousArticleHash: string;
    publicExplanation: string;
    actions: TypedAction[];
    copyRegistryVersion: string;
}

export async function applyLegalRepair(body: {
    articleRevisionId: string;
    ruleId: string;
    passageRef: string;
    passageExcerpt: string;
    chosenText: string;
}): Promise<LegalRepairApplyResponse> {
    const res = await api.post<LegalRepairApplyResponse>(
        `${BASE}/legal-repair/apply`, body, cfg());
    return res.data;
}
