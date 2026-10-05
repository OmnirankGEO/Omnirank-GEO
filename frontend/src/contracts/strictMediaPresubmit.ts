/**
 * strictMediaPresubmit — 严审媒体「发布前预审被拦」的前端出口契约(SSOT 单点)。
 *
 * 背景(2026-07-30 工单 T3):后端 `services/strict_media_presubmit.py` 早就返回了
 * 一份完整载荷 —— 三个可点动作、逐媒体 `hard_failures`、`repair_hint`、
 * `hard_failure_codes`。前端此前**只取了 `message` 弹一个 toast**,于是用户既不知道
 * 哪里不合格、也没有下一步,这才是老板说的「完全没出路」。
 *
 * 这里做两件事,且只做这两件:
 *   1. 把后端载荷适配成既有 §13 `GovernanceAlertContract`,复用 `GovernanceAlert`
 *      渲染器 —— **不另写一套告警 UI**;
 *   2. 提供 `hard_failure_codes → 人话` 的**唯一**映射表。后端 `hard_failures`
 *      只带 {code, detail?, evidence?},没有面向用户的措辞,所以这张表必须存在;
 *      存在一份就够,禁止在组件里再硬编码第二份。
 *
 * 🔴 红线(工单 §3.2):**不提供任何「人工审核放行 / 忽略继续投放」出口**。
 *    这道门拦的不是"我们觉得不合规",而是「搜狐系 52.2% 的拒稿正是按 standard 档
 *    写的稿投严审媒体造成的」。放行 = 明知大概率被拒还扣钱发 = 钱白花。
 *    正确出口只有两条:**修稿达标** 或 **换成非严审媒体**。
 *    `ALLOWED_ACTION_IDS` 是这条红线的可执行形态 —— 后端将来若误发一个覆盖类动作,
 *    这里也不会把它渲染出去。
 */
import type {
  GovernanceAlertAction,
  GovernanceAlertContract,
} from '@/contracts/governanceAlert';

export const STRICT_PRESUBMIT_REASON = 'strict_media_presubmit_failed' as const;
export const STRICT_PRESUBMIT_CODE = 'STRICT_MEDIA_PRESUBMIT_FAILED' as const;

/** 前端自造的第 4 个动作:把被拦媒体从本次选择里摘掉,改投非严审媒体。 */
export const SWITCH_MEDIA_ACTION_ID = 'switch_to_non_strict_media' as const;

/**
 * 允许出现在面板上的动作 id 白名单。
 * 三个来自后端合同 + 一个前端换档动作。**任何覆盖/放行类 id 都不在此列。**
 */
export const ALLOWED_ACTION_IDS: readonly string[] = [
  'ai_fix_this_span',
  'view_findings',
  'edit_manually',
  SWITCH_MEDIA_ACTION_ID,
];

/**
 * hard_failure code → 人话。**全站唯一一份。**
 * 取值域来自 `writing/platform_safety_profiles.py::review_for_platform` 的 hard 分支。
 */
export const STRICT_FAILURE_CODE_LABELS: Readonly<Record<string, string>> = {
  ad_law_absolute_term_in_title: '标题里有广告法禁止的绝对化用语',
  ad_law_absolute_term_about_client_brand: '正文里紧挨着客户品牌用了绝对化用语',
  geo_outcome_guarantee_detected: '出现了「保证上榜 / 保证被 AI 推荐」这类效果承诺',
  strict_media_body_contact_detected: '正文里留了联系方式',
  strict_media_body_external_link_detected: '正文里带了站外链接',
  strict_media_sales_call_to_action_detected: '正文里有直接的销售引导话术',
  sohu_client_image_placeholder_detected: '正文里还留着客户配图占位符',
  strict_media_below_corpus_minimum_length: '正文字数低于这家媒体的收稿底线',
};

/** 没登记的新码不硬编码兜底文案,直接把码亮出来,便于当场发现映射表漏项。 */
export function humanizeStrictFailureCode(code: string): string {
  return STRICT_FAILURE_CODE_LABELS[code] || `未登记的审核项(${code})`;
}

export interface StrictHardFailure {
  code: string;
  detail?: string;
  evidence?: string;
}

export interface StrictBlockedMedia {
  media_id?: number | null;
  media_name?: string;
  family_label?: string;
  observed_reject_rate?: number | null;
  hard_failures?: StrictHardFailure[];
}

export interface StrictPresubmitBlock {
  reason: typeof STRICT_PRESUBMIT_REASON;
  article_id?: number;
  message: string;
  repair_hint?: string;
  hard_failure_codes: string[];
  blocked_media: StrictBlockedMedia[];
  passed_media: StrictBlockedMedia[];
  actions: GovernanceAlertAction[];
  rule_version?: string;
}

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

/**
 * 从 409 响应体里解析预审拦截载荷,解析不到返回 null。
 *
 * 承载形状:FastAPI `HTTPException(status_code=409, detail=payload)`
 * → `{ detail: {...} }`;也接受直接传 payload 本身。
 */
export function parseStrictPresubmitBlock(input: unknown): StrictPresubmitBlock | null {
  if (!input || typeof input !== 'object') return null;
  const outer = input as Record<string, unknown>;
  const candidate = (outer.detail && typeof outer.detail === 'object')
    ? outer.detail as Record<string, unknown>
    : outer;
  if (candidate.reason !== STRICT_PRESUBMIT_REASON) return null;
  const presubmit = (candidate.presubmit && typeof candidate.presubmit === 'object')
    ? candidate.presubmit as Record<string, unknown>
    : {};
  return {
    reason: STRICT_PRESUBMIT_REASON,
    article_id: typeof candidate.article_id === 'number' ? candidate.article_id : undefined,
    message: typeof candidate.message === 'string' ? candidate.message : '本稿未通过发布前预审。',
    repair_hint: typeof candidate.repair_hint === 'string' ? candidate.repair_hint : undefined,
    hard_failure_codes: asArray<string>(candidate.hard_failure_codes).filter(c => typeof c === 'string'),
    blocked_media: asArray<StrictBlockedMedia>(presubmit.blocked_media),
    passed_media: asArray<StrictBlockedMedia>(presubmit.passed_media),
    actions: asArray<GovernanceAlertAction>(candidate.actions)
      .filter(a => a && typeof a.id === 'string' && typeof a.label === 'string'),
    rule_version: typeof candidate.rule_version === 'string' ? candidate.rule_version : undefined,
  };
}

/** 被拦媒体的 media_id 集合 —— 「换成非严审媒体」按这个从选择里摘。 */
export function blockedMediaIds(block: StrictPresubmitBlock): number[] {
  return block.blocked_media
    .map(m => m.media_id)
    .filter((v): v is number => typeof v === 'number');
}

/**
 * 🔴 刻意写成**结构最小约束的泛型**,不是一个带索引签名的具体接口。
 * 早先写成 `mediaList: Array<{ id: number; [extra: string]: unknown }>`,
 * `CartItem`/`CartMediaItem` 没有索引签名 → `tsc -b` 报 TS2345 两条,
 * 而 esbuild 只做语法转译看不出来。
 */
export interface StrictSwitchCartEntry {
  articleId: number;
  mediaList: Array<{ id: number }>;
}

/**
 * 「换成非严审媒体」的纯归约:把被拦媒体从**那一篇**的选择里摘掉。
 *
 * 只动 `articleId` 命中的那一条 —— 同一家媒体对别的文章可能是过的,
 * 一刀切会误伤。摘光媒体的条目直接从清单里去掉(留个空条目提交必报错)。
 */
export function applyStrictMediaSwitch<T extends StrictSwitchCartEntry>(
  cart: T[],
  articleId: number | undefined,
  dropMediaIds: Iterable<number>,
): T[] {
  const drop = new Set(dropMediaIds);
  return (cart || [])
    .map(entry => (entry.articleId === articleId
      ? { ...entry, mediaList: entry.mediaList.filter(m => !drop.has(m.id)) }
      : entry))
    .filter(entry => entry.mediaList.length > 0);
}

/**
 * 适配成 §13 合同。
 *
 * 后端载荷缺 `code` / `impact`(它不是按 §13 构造的),这里补齐 —— 补的是**呈现**,
 * 不是判据:`message` / `repair_hint` / `actions` 全部原样透传。
 * `impact` 说明「没扣钱」是必须讲清的事实(拦在扣费之前),不然用户会以为钱已经花了。
 *
 * `extraActions` 用于挂前端自造的换档动作;白名单之外的一律丢弃。
 */
export function toGovernanceContract(
  block: StrictPresubmitBlock,
  extraActions: GovernanceAlertAction[] = [],
): GovernanceAlertContract {
  const names = block.blocked_media
    .map(m => m.media_name || m.family_label || '')
    .filter(Boolean);
  const reason = block.hard_failure_codes.length > 0
    ? `未通过的审核项：${block.hard_failure_codes.map(humanizeStrictFailureCode).join('；')}`
    : '这些媒体的收稿标准比通用媒体严格，本稿按当前写法过不了它们的初审。';
  const passed = block.passed_media.length;
  const actions = [...block.actions, ...extraActions]
    .filter(a => ALLOWED_ACTION_IDS.includes(a.id));
  return {
    code: STRICT_PRESUBMIT_CODE,
    message: block.message,
    reason,
    impact: passed > 0
      ? `${names.length} 家被拦下，本次未产生任何费用；同批另有 ${passed} 家可正常投放。`
      : '本次未产生任何费用（拦在扣费之前）。',
    repair_hint: block.repair_hint,
    actions,
    rule_version: block.rule_version,
    severity: 'hard',
  } as GovernanceAlertContract;
}
