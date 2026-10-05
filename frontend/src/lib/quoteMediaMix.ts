/**
 * 投放组合拆分 · TS 侧镜像（WO_QUOTE_MEDIA_MIX_DYNAMIC_2026-08-12 v2 · P0）
 *
 * 与 `services/quote_media_mix.py` **同一套公式**。工单 §4.3 原文：
 *   「Python 与 TypeScript 必须共享 round_half_up 夹具，禁止一端 banker rounding、
 *     一端 Math.round 造成漂移。」
 *
 * 🔴 两端漂移的真实后果:同一个交付条数在后端算出 6/11/4、前端算出 7/10/4,
 *    客户看到的三行加起来还是 21,但和后端存的组合对不上 —— 这种错不会有人报 bug,
 *    只会在对账时才发现。所以 `frontend/scripts/test-quote-media-mix-parity.mjs`
 *    用**同一份 fixture** 同时喂两端,数值不一致就转红。
 *
 * 🔴 本模块只做「一个已确定的数怎么拆」。总条数不在这里产生,也不许在这里改
 *    （总条数 SSOT = `tools/pricing_bands.py`,本包对它零差异）。
 */

/** 比例夹紧:只防小分母失真,**不是**合格线,也不是效果区间（裁定书 v2 §1.3） */
export const RATIO_CLAMP_MIN = 1.5;
export const RATIO_CLAMP_MAX = 4.0;
/** 锚点至少 2 篇,否则「锚点」这层名存实亡 */
export const MIN_ANCHOR_COUNT = 2;
/**
 * 全局回退比例 = 已分类引用池的 (垂直 + 自媒体 − 抖音) / (权威 + 门户)
 * = (10,916 + 8,931 − 1,724) / (479 + 8,088) = 18,123 / 8,567
 * 🔴 未分类长尾 8,566 条**不进**任何一侧（v1 把它算进覆盖,被判红）。
 */
export const GLOBAL_RATIO_RAW = 2.1154429789;

export interface MediaMixRatio {
  /** 未夹紧的原始比例 */
  raw: number;
  /** 实际参与计算的比例（已 clamp） */
  used: number;
  /** 取自哪一级:industry_engine / industry / engine / global */
  source: string;
}

export interface MediaMix {
  /** 重点媒体锚点 */
  focusMediaAnchor: number;
  /** 行业与平台覆盖 */
  industryPlatformCoverage: number;
  /** 抖音图文（豆包专项） */
  douyinDoubaoOnly: number;
}

/**
 * 四舍五入到整数,`.5` 一律进位。
 *
 * 🔴 `Math.round` 在**负数**上不是 half-up（`Math.round(-2.5) === -2`,而 half-up 应为 -3）。
 *    篇数不会为负,但这个函数被跨端对拍,语义必须与 Python 的
 *    `Decimal.quantize(ROUND_HALF_UP)` 严格一致,不能靠"业务上不会出现"糊过去。
 */
export function roundHalfUp(value: number): number {
  return value < 0 ? -Math.round(-value) : Math.round(value);
}

export function clampRatio(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return RATIO_CLAMP_MIN;
  return Math.min(RATIO_CLAMP_MAX, Math.max(RATIO_CLAMP_MIN, raw));
}

/**
 * 把已确定的交付条数拆成三类。
 *
 * @param capacityTotal 已冻结的交付条数（本函数**不改**它）
 * @param ratioUsed     已 clamp 的覆盖/锚点比
 * @param douyinShare   抖音专项占总条数的比例;不满足启用条件时传 0
 */
export function planMediaMix(
  capacityTotal: number,
  ratioUsed: number = clampRatio(GLOBAL_RATIO_RAW),
  douyinShare = 0,
): MediaMix {
  const total = Math.max(0, Math.floor(capacityTotal || 0));
  let douyin = roundHalfUp(total * (douyinShare > 0 ? douyinShare : 0));
  douyin = Math.max(0, Math.min(douyin, total));

  const remaining = total - douyin;
  let anchor = remaining > 0 ? Math.max(MIN_ANCHOR_COUNT, roundHalfUp(remaining / (1 + ratioUsed))) : 0;
  anchor = Math.min(anchor, remaining);
  const coverage = remaining - anchor;

  return {
    focusMediaAnchor: anchor,
    industryPlatformCoverage: coverage,
    douyinDoubaoOnly: douyin,
  };
}
