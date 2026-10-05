/**
 * duplicateOrderConflict — 批量投放「组合已被占用」的前端出口契约(SSOT 单点)。
 *
 * 背景(2026-08-04 工单 WO-BATCH-CONFLICT):后端预检一次就能把**全部**冲突组合算出来,
 * 前端此前**只取 message 弹一个 toast** —— 于是整批卡死,用户既不知道是哪几个组合、
 * 也没有"去掉冲突继续投其余"的出路,只能整批放弃。生产实证:item 414
 * (article 1393 × 博客园)是 `published` 且有真实单号 `1120260730151700…`,
 * 却被说成"进行中",用户以为在排队,一直等。
 *
 * 这里只做三件:
 *   1. 把 409 载荷解析成结构化冲突清单(每条带 article_id + media_id —— **能回到具体组合**);
 *   2. 提供 `冲突类型 → 人话` 的**唯一**映射表(禁止在组件里再硬编码第二份);
 *   3. 提供「剔除冲突组合」的纯归约,让购物车可以精确摘掉那几对。
 *
 * 🔴 红线:本文件**不做任何判定**。谁该拦、谁不该拦,全部由后端
 *    `db.meijiehezi_db.find_active_orders_for_media` 决定(那道闸刚随僵尸包改过,
 *    本单一个字不碰)。这里只负责"把后端已经算出来的结论展示出来 + 让用户能摘掉"。
 */

export const DUPLICATE_ORDER_CODE = 'DUPLICATE_ORDER' as const;

/** 冲突类型。取值域 = 后端 `db/meijiehezi_db.py` 的 BLOCK_REASON_* 常量。 */
export type ConflictReason = 'already_published' | 'in_flight' | 'other';

/**
 * 类型 → 人话。**全站唯一一份。**
 *
 * 🔴 `already_published` 与 `in_flight` 必须给**不同**措辞:
 *    前者是"早就发完了,别重发"(生产最老 95 天前发的还在拦人),
 *    后者才是"正在发,等着"。老文案把两者混成一句"已有进行中的发布订单",
 *    这是本单要修的核心错误。
 */
export const CONFLICT_REASON_LABELS: Readonly<Record<ConflictReason, string>> = {
  already_published: '已在该媒体发布过',
  in_flight: '有进行中的订单',
  other: '该组合当前不可提交',
};

/** 每类冲突下面那句解释,回答"那我该怎么办"。 */
export const CONFLICT_REASON_HINTS: Readonly<Record<ConflictReason, string>> = {
  already_published: '这几篇早就在对应媒体发出去了，重复投放不会有新效果，也不会计费。',
  in_flight: '这几篇正在对应媒体的发布流程里，等它出结果就好，不用重复提交。',
  other: '这几个组合暂时不能提交，去掉后其余可以照常投放。',
};

/**
 * 后端理由码 → 本文件的取值域。
 * 大小写不敏感:后端常量是 `ALREADY_PUBLISHED`/`IN_FLIGHT`(僵尸包已定并被锁钉住,
 * 本单不改它的拼写),这里做规范化而不是要求后端迁就前端。
 * 认不出的码一律落 `other` —— 不猜,但也不把用户卡死。
 */
export function normalizeConflictReason(raw: unknown): ConflictReason {
  const s = String(raw ?? '').trim().toLowerCase();
  if (s === 'already_published') return 'already_published';
  if (s === 'in_flight') return 'in_flight';
  return 'other';
}

export interface OrderConflict {
  articleId: number;
  mediaId: number;
  articleTitle: string;
  mediaName: string;
  reason: ConflictReason;
}

export interface DuplicateOrderBlock {
  code: typeof DUPLICATE_ORDER_CODE;
  message: string;
  conflicts: OrderConflict[];
}

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

function asNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * 从 409 响应体解析冲突清单;解析不到返回 null(调用方回落到老 toast)。
 *
 * 承载形状:FastAPI `HTTPException(409, detail=payload)` → `{ detail: {...} }`;
 * 也接受直接传 payload 本身。
 *
 * 🔴 缺 article_id / media_id 的条目**丢弃**而不是补 0:
 *    补 0 会让"剔除"去摘一个不存在的组合 —— 剔了个寂寞,而 UI 还显示成功。
 */
export function parseDuplicateOrderBlock(input: unknown): DuplicateOrderBlock | null {
  if (!input || typeof input !== 'object') return null;
  const outer = input as Record<string, unknown>;
  const candidate = (outer.detail && typeof outer.detail === 'object')
    ? outer.detail as Record<string, unknown>
    : outer;
  if (candidate.code !== DUPLICATE_ORDER_CODE) return null;

  const conflicts: OrderConflict[] = [];
  for (const raw of asArray<Record<string, unknown>>(candidate.conflicts)) {
    if (!raw || typeof raw !== 'object') continue;
    const articleId = asNum(raw.article_id);
    const mediaId = asNum(raw.media_id);
    if (articleId === null || mediaId === null) continue;
    conflicts.push({
      articleId,
      mediaId,
      articleTitle: typeof raw.article_title === 'string' ? raw.article_title : '',
      mediaName: typeof raw.media_name === 'string' ? raw.media_name : '',
      reason: normalizeConflictReason(raw.reason_code),
    });
  }
  if (conflicts.length === 0) return null;   // 没有可操作的组合 = 给不出处理窗口,回落 toast

  return {
    code: DUPLICATE_ORDER_CODE,
    message: typeof candidate.message === 'string' ? candidate.message : '有组合无法提交。',
    conflicts,
  };
}

/** 按类型分组,保持 `CONFLICT_REASON_LABELS` 的键序,空组不返回。 */
export function groupConflictsByReason(
  conflicts: OrderConflict[],
): Array<{ reason: ConflictReason; items: OrderConflict[] }> {
  const order: ConflictReason[] = ['already_published', 'in_flight', 'other'];
  return order
    .map(reason => ({ reason, items: (conflicts || []).filter(c => c.reason === reason) }))
    .filter(g => g.items.length > 0);
}

/**
 * 🔴 与 `StrictSwitchCartEntry` 同理:写成结构最小约束的泛型,不带索引签名。
 * (带索引签名会让 `CartItem` 传不进来,而 esbuild 只做语法转译看不出,`tsc -b` 才报。)
 */
export interface ConflictCartEntry {
  articleId: number;
  mediaList: Array<{ id: number }>;
}

/**
 * 「去掉冲突组合」的纯归约。
 *
 * 🔴 必须按 **(articleId, mediaId) 成对**摘,不能只按 mediaId:
 *    同一家媒体对**别的**文章是完全合法的组合,只按 mediaId 一刀切会误删用户想投的。
 * 摘光媒体的条目整条去掉(留个空条目提交必报错)。
 */
export function applyConflictRemoval<T extends ConflictCartEntry>(
  cart: T[],
  conflicts: OrderConflict[],
): T[] {
  const drop = new Set((conflicts || []).map(c => `${c.articleId}:${c.mediaId}`));
  if (drop.size === 0) return cart || [];
  return (cart || [])
    .map(entry => ({
      ...entry,
      mediaList: entry.mediaList.filter(m => !drop.has(`${entry.articleId}:${m.id}`)),
    }))
    .filter(entry => entry.mediaList.length > 0);
}

/**
 * 「现在能不能按提交」的**唯一**判定。
 *
 * 🔴 抽成纯函数是刻意的:它是资金相关的闸(按下去就扣费),必须能被行为断言直接打,
 *    而不是只能靠渲染快照或源码串去猜。弹窗只负责把它接到 `disabled` 上。
 *
 * 两种情况禁提交:
 *   · 还有没处理的冲突 —— 直接提交只会再撞同一批,白跑一趟;
 *   · 剔除后一个组合都不剩 —— 那是空单。
 */
export function isSubmitBlockedByConflicts(
  block: DuplicateOrderBlock | null | undefined,
): boolean {
  return !!block && block.conflicts.length > 0;
}

/** 购物车里还剩几个「文章×媒体」组合 —— 0 就禁止提交(空单)。 */
export function countCombinations<T extends ConflictCartEntry>(cart: T[]): number {
  return (cart || []).reduce((sum, entry) => sum + entry.mediaList.length, 0);
}

/**
 * 剔除后还剩几个组合。给按钮文案「去掉这 N 个,继续投放其余 M 个」用。
 *
 * 🔴 M 必须**从剔除后的购物车真算**,不能拿 `总数 - 冲突数` 减:
 *    后端可能对同一组合返回多条(不同订单各一条),减法会把 M 算小。
 */
export function remainingAfterRemoval<T extends ConflictCartEntry>(
  cart: T[],
  conflicts: OrderConflict[],
): number {
  return countCombinations(applyConflictRemoval(cart, conflicts));
}
