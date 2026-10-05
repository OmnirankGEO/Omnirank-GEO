/**
 * #186 · 图文发布面板的判定层。**零 import**(判据要能 transpile 后真调)。
 *
 * Owner 09-12:「图文尽快重做上线……不要弄非必要的按钮,选项尽量简单」。
 * 所以这一层只回答四个问题,多一个都不加:
 *   ① 哪些作品能勾(有版本才行);② 哪些账号能选;
 *   ③ 提交体长什么样(**七个键,一个不多**);④ 轮询什么时候停。
 *
 * 🔴 **前端零价格算术**:价与合计只从 `publish-preview` 的回包里取字段显示,
 *    这里没有一个加号乘号。#150 撤下旧页的原因之一就是它自己算价
 *    (`totalPrice = firstPrice + over × extraCardPrice`),规则一变前后端就分家。
 */

/** `/api/geo-douyin/posts?status=ready` 的一行(只取本层用得到的)。 */
/*
 * 🔴 [#188] `ReadyPostRow` / `SelectableRow` / `toSelectableRows` **已删除**。
 *
 *    它们是 #186 批量面板的行模型。面板退役之后**零调用** —— 而它们实现的谓词
 *    (「没有 active_revision_id ⇒ 发不出去」)当时已经由区 E 的
 *    `imageNoteStudioApi.zoneEState / canPublishCard` 承担(那一份也随 #204 a2 删了,df9c142c5)。
 *
 *    留着的后果不是"多几行死代码",是**判据在证一个没人调用的函数**:
 *    N2a–N2e 当时全绿,而屏幕上那件事由另一份实现决定 —— 注毒 V3 恰好把这点照出来了:
 *    毒它,纯函数臂红、**浏览器臂纹丝不动**。那不是"锁没牙",是「毒够不着」,
 *    因为被毒的那份根本不在被测路径上。
 *    ⇒ 删掉死的那份,判据搬到活的那份上(见 verify-image-note-studio 的 Z 段)。
 */
const int = (v: unknown): number => {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
};
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
/** Bigint DTOs may arrive as JSON numbers or decimal strings; never truncate IDs. */
export function artifactIdentity(value: unknown): string {
    if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? String(value) : '';
    if (typeof value === 'string' && /^[1-9]\d*$/.test(value)) return value;
    return '';
}

export interface AccountRow {
    id?: unknown;
    media_id?: unknown;
    media_name?: unknown;
    name?: unknown;
    can_tuwen?: unknown;
    blacklist?: unknown;
    is_active?: unknown;
    /* [#192] 选账号时要显示的三样。字段名照 db/meijiehezi_db.SHORT_VIDEO_PUBLIC_COLUMNS
       与 api 的 `_project_rows`(它把成本侧字段删掉、加上 price_points)。 */
    platform?: unknown;
    price_points?: unknown;
    fans_num?: unknown;
    fans_num_text?: unknown;
}

export interface AccountOption {
    mediaId: number;
    name: string;
    /**
     * [#192 a1] 选账号要看得见的三样。都是**服务端原值直显**,前端不算、不猜。
     * 🔴 `pricePoints` 用 `null` 表示"服务端没给",不是 0 ——
     *    0 会被读成"免费",而那是我们反复在修的那类谎。
     */
    platform: string;
    pricePoints: number | null;
    fans: string;
}

/**
 * 账号下拉里该出现谁。
 *
 * 🔴 **这是"身份侧"的严格子集,不是资格判定的第二份实现。**
 *    资格的 SSOT 是 `services/geo_douyin/account_eligibility.py`:
 *    抖音平台 + `can_tuwen=1` + active + `blacklist=0` + **真实频控**(当日额度)。
 *    前端只做得到前三条(目录里有这三个字段),**当日额度看不到也不猜** ——
 *    它由 `publish-preview` 逐项回的 `eligibility` 说了算。
 *
 *    为什么仍然要在前端滤:一个 `can_tuwen=0` 的号**永远**不可能发图文,
 *    把它列进下拉就是一个死选项(用户选了必被拒,还以为是自己点错)。
 *    这与旧面板注释里那句「不在前端判资格」不矛盾 —— 那句说的是**频控/黑名单
 *    这类动态资格**,这里滤掉的是**静态能力**。判据 N3 钉的也是这个边界:
 *    只许滤这三项,多滤一项(比如自己算当日额度)就是造第二个判官。
 */
export function eligibleAccounts(rows: readonly AccountRow[] | null | undefined): AccountOption[] {
    const out: AccountOption[] = [];
    for (const r of rows || []) {
        const mediaId = int(r?.id) || int(r?.media_id);
        if (!mediaId) continue;
        if (int(r?.can_tuwen) !== 1) continue;          // 不能发图文 —— 永远不会变可用
        if (int(r?.blacklist) === 1) continue;          // 拉黑
        if (r?.is_active === false || int(r?.is_active) === 0) continue;
        const pp = Number(r?.price_points);
        out.push({
            mediaId,
            name: str(r?.media_name) || str(r?.name) || `账号 ${mediaId}`,
            platform: str(r?.platform),
            pricePoints: Number.isFinite(pp) && pp > 0 ? pp : null,
            /* 粉丝数服务端给的是已经格式化好的 `fans_num_text`(如「12.3w」);
               没有就退回裸数;都没有就空串 —— 不编。 */
            fans: str(r?.fans_num_text) || (Number(r?.fans_num) > 0 ? String(r.fans_num) : ''),
        });
    }
    return out;
}

/** Narrow shortcut scope uses the catalog's platform field, never account names. */
export function accountCatalogQuery(page: number, search: string, platform: string, platformScope?: '抖音'): string {
    const params = new URLSearchParams({ can_tuwen: '1', limit: '50', page: String(page) });
    if (search.trim()) params.set('search', search.trim());
    if (platformScope || platform) params.set('platform', platformScope || platform);
    return params.toString();
}

export function accountsForPlatform(rows: readonly AccountRow[], platformScope?: '抖音'): AccountOption[] {
    return eligibleAccounts(rows).filter(a => !platformScope || a.platform.trim() === platformScope);
}

export function commandMediaId(command: { items?: { media_id?: number | null }[] } | null, selected: number): number {
    return command?.items?.find(i => Number.isSafeInteger(i.media_id) && Number(i.media_id) > 0)?.media_id || selected;
}

/** 已准备好的素材(`prepare-publish-media-v2` 的回包)。 */
export interface PreparedArtifactLike {
    state?: unknown;
    prepared_artifact_id?: unknown;
    manifest_hash?: unknown;
    post_revision_id?: unknown;
}

/** 提交体里的一项。🔴 **恰好七个键** —— 内容不在请求体里(它来自冻结版本)。 */
export interface BatchItem {
    item_request_id: string;
    geo_post_id: number;
    post_revision_id: number;
    prepared_artifact_id: string;
    manifest_hash: string;
    media_id: number;
    expected_price_fingerprint: string;
}

export interface PreviewItemLike {
    item_request_id?: unknown;
    geo_post_id?: unknown;
    publish_price_fingerprint?: unknown;
    final_price_points?: unknown;
}

/**
 * 拼提交体。
 *
 * 🔴 **只拼这七个键**。契约:`api/geo_image_note_api.py:285-312`
 *    (`PublishPreviewItem` 六键 + `expected_price_fingerprint`)。
 *    模型上还有一个 `expected_price_points`,但**全后端零读取**
 *    (2026-09-13 实测:只有它自己的声明与一条 pytest 夹具命中)——
 *    它头顶那句「服务端据此逐项比对」是**假注释**;真正的逐项校验走指纹。
 *    所以不发它:发一个没人读的字段,只会让下一个人以为它是承重的。
 *
 * 🔴 指纹**只从 preview 回包里取**,前端不构造、不猜、不复用制作链的指纹
 *    (制作链与投放链的指纹不可互换,后端 `assert_fingerprint_operation` 会拒)。
 */
export function buildBatchItems(input: {
    chosen: readonly { postId: number; revisionId: number; mediaId: number; itemRequestId: string }[];
    artifacts: Readonly<Record<number, PreparedArtifactLike>>;
    previewItems: readonly PreviewItemLike[];
}): BatchItem[] {
    const byRequestId = new Map<string, PreviewItemLike>();
    for (const p of input.previewItems || []) {
        const k = str(p?.item_request_id);
        if (k) byRequestId.set(k, p);
    }
    const out: BatchItem[] = [];
    for (const c of input.chosen || []) {
        const art = input.artifacts?.[c.postId];
        const fp = str(byRequestId.get(c.itemRequestId)?.publish_price_fingerprint);
        const artifactId = artifactIdentity(art?.prepared_artifact_id);
        const manifest = str(art?.manifest_hash);
        // 任一缺失就**不发这一项** —— 宁可少发一条,也不发一个必被拒的项
        if (!c.postId || !c.revisionId || !c.mediaId || !artifactId || !manifest || !fp) continue;
        out.push({
            item_request_id: c.itemRequestId,
            geo_post_id: c.postId,
            post_revision_id: c.revisionId,
            prepared_artifact_id: artifactId,
            manifest_hash: manifest,
            media_id: c.mediaId,
            expected_price_fingerprint: fp,
        });
    }
    return out;
}

/** 提交体每项应有的键(判据与实现共用一份,免得两处各写一遍)。 */
export const BATCH_ITEM_KEYS: readonly string[] = [
    'item_request_id', 'geo_post_id', 'post_revision_id',
    'prepared_artifact_id', 'manifest_hash', 'media_id', 'expected_price_fingerprint',
];

/**
 * 轮询该不该停。
 *
 * 🔴 终态还接着轮 = 白打接口;终态判错成"还在跑" = 用户永远看不到结果。
 *    终态集合照后端 command_status 的取值,认不出的一律**当作还没结束**
 *    (宁可多轮一次,也不谎称结束)。
 */
const TERMINAL = new Set(['succeeded', 'failed', 'partial', 'cancelled', 'completed', 'done']);
export function isCommandTerminal(commandStatus: unknown): boolean {
    const s = str(commandStatus).toLowerCase();
    if (!s) return false;
    return TERMINAL.has(s);
}

export function publishCommandLabel(status: unknown): string {
    const labels: Record<string, string> = { accepted: '已提交，等待渠道回执', processing: '已提交，等待渠道回执',
        queued: '已提交，正在排队', succeeded: '本次发布处理完成，请查看逐条结果', completed: '本次发布处理完成，请查看逐条结果',
        done: '本次发布处理完成，请查看逐条结果', failed: '本次发布未成功，请查看原因', partial: '部分处理完成，请查看逐条结果', cancelled: '本次发布已取消' };
    return labels[str(status)] || '正在核对本次提交结果，请稍后刷新';
}

/** Follow the server's released-failure contract, never infer retryability from a timeout. */
export function failedCommandAccounts(command: { command_status?: string; items?: { media_id?: unknown; state?: string; retryable?: boolean }[] } | null): number[] {
    if (command?.command_status !== 'failed' || !command.items?.length) return [];
    if (!command.items.every(item => item.retryable === true && ['failed', 'rejected'].includes(item.state || '')
        && Number.isSafeInteger(Number(item.media_id)) && Number(item.media_id) > 0)) return [];
    return command.items.map(item => Number(item.media_id));
}

/**
 * 合计怎么显示。**原样取服务端的数**,一个算术都不做。
 * 取不到就返回 null,由界面说「还没算出价」——**不许**显示 0(那等于告诉用户免费)。
 */
export function totalPointsFromPreview(preview: unknown): number | null {
    const o = (preview && typeof preview === 'object' ? preview : {}) as Record<string, unknown>;
    const v = o.total_price_points;
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
