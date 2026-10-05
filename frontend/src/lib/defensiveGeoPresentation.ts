/**
 * 防御型 GEO 呈现层 —— 前端**只渲染服务端下发的口径**,不自己算。
 *
 * 规格 §9.1 逐字:「前端只渲染服务端版本化 DTO,不计算 outcome、等级、价格和推荐理由」。
 * 判据 = CUR-03 / MET-40 / UI-24 / §0.5.5 U-1、U-2。
 *
 * 🔴 这个文件解决的是一个**实测存在**的口径漂移(CUR-03)
 * ------------------------------------------------------
 * 改造前实测:
 *   后端 SSOT `tools/scoring/scoring_levels.py`  领先>=85(emerald) 成熟70-84(green)
 *                                               成长55-69 起步40-54 待提升20-39 空白0-19
 *   前端 `BrandList.tsx:80-96`                   领先>=81(**green**) 成熟>=61(**emerald**)
 *                                               成长>=41 起步>=21 —— 且**没有**「待提升」这一档
 * 即:阈值全错、领先/成熟两档的颜色**互换**、六档少一档。
 * 一个 82 分的品牌在列表里显示「领先」,在别处是「成熟」。
 * `HistoryList.tsx:130` 又是第三套(按字符串 `level.includes('领先')` 判色)。
 *
 * 修法不是"把前端阈值改对" —— 那只是把三套变成三套一致,下次还会漂。
 * 修法是**前端不再拥有阈值**:等级、标签、色调一律由服务端 `level_meta` 下发。
 *
 * 🔴 拿不到 level_meta 时**显示「暂无结论」,不猜**
 * 用分数反推等级正是 CUR-03 的病根;缺数据就如实说缺,不要编一个出来。
 */

/** 服务端下发的等级元数据(与 `services/defensive_geo/presentation/registries.py` 同源)。 */
export interface LevelMeta {
    key: 'guarded' | 'needs_strengthening' | 'priority_fix' | 'unknown';
    label: string;
    tone: 'positive' | 'warning' | 'critical' | 'neutral';
    definitionVersion: string;
}

/** 四态色调 → Tailwind class。**只映射 tone,不映射分数** —— 前端不碰阈值。 */
const TONE_BADGE: Record<LevelMeta['tone'], string> = {
    positive: 'bg-emerald-50 text-emerald-700 border border-emerald-200',
    warning: 'bg-amber-50 text-amber-700 border border-amber-200',
    critical: 'bg-orange-50 text-orange-700 border border-orange-200',
    neutral: 'bg-muted text-muted-foreground border border-border',
};

const TONE_DOT: Record<LevelMeta['tone'], string> = {
    positive: 'bg-emerald-500',
    warning: 'bg-amber-500',
    critical: 'bg-orange-500',
    neutral: 'bg-muted-foreground/40',
};

/** 服务端没给等级时的确定性兜底。**不猜等级**,如实说暂无结论。 */
export const UNKNOWN_LEVEL: LevelMeta = {
    key: 'unknown',
    label: '暂无结论',
    tone: 'neutral',
    definitionVersion: 'client_fallback_no_server_level',
};

/**
 * 读服务端 level_meta。缺失/形态不对 → `UNKNOWN_LEVEL`。
 *
 * 注意**没有** `score` 参数 —— 签名里根本不给分数,
 * 就没人能在这里偷偷用分数反推等级。
 */
export function readLevelMeta(raw: unknown): LevelMeta {
    if (!raw || typeof raw !== 'object') return UNKNOWN_LEVEL;
    const m = raw as Partial<LevelMeta>;
    if (
        (m.key === 'guarded' || m.key === 'needs_strengthening' ||
         m.key === 'priority_fix' || m.key === 'unknown') &&
        typeof m.label === 'string' && m.label.length > 0 &&
        (m.tone === 'positive' || m.tone === 'warning' ||
         m.tone === 'critical' || m.tone === 'neutral')
    ) {
        return {
            key: m.key,
            label: m.label,
            tone: m.tone,
            definitionVersion: typeof m.definitionVersion === 'string'
                ? m.definitionVersion : 'unversioned',
        };
    }
    return UNKNOWN_LEVEL;
}

export function levelBadgeClass(meta: LevelMeta): string {
    return TONE_BADGE[meta.tone];
}

export function levelDotClass(meta: LevelMeta): string {
    return TONE_DOT[meta.tone];
}

/**
 * 🔴 null 与 0 严格分开(CUR-02)。
 *
 * 改造前实测 `BrandDetail.tsx` 有 16 处 `value || 0`,包括八维雷达的
 * 全部八个维度 —— **没测过**被画成 0 分,和**真的 0 分**长得一模一样。
 * 对销售来说这两件事的下一步动作完全不同(去测 vs 去修),
 * 混在一起等于把「不知道」谎报成「很差」。
 */
export function measured(value: number | null | undefined): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 未测量时的显示文本。**不显示 0**(§9.7「不显示 0 分」)。 */
export function formatMeasured(
    value: number | null | undefined,
    opts: { suffix?: string; notMeasured?: string } = {},
): string {
    const v = measured(value);
    if (v === null) return opts.notMeasured ?? '未测';
    return `${v}${opts.suffix ?? ''}`;
}

/**
 * U-1 / U-2:上屏前的最后一道闸 —— 内部枚举裸串不许出现在界面上。
 *
 * 判形态不判词表(补一条词漏三条):`snake_case` 全 ASCII 小写下划线串
 * 是内部名的形态,人话文案里不会长这样。
 * 与后端 `copy_registry.looks_like_internal_enum` 同一判据,两边都要有 ——
 * 后端挡下发,前端挡渲染。
 */
export function looksLikeInternalEnum(text: string): boolean {
    if (!text) return false;
    const s = text.trim();
    if (!s || s.includes(' ')) return false;
    return /^[a-z0-9]+(?:[._][a-z0-9]+)+$/.test(s);
}

/**
 * 渲染一个可能来自服务端枚举的字符串。
 * 命中内部枚举形态 → 返回兜底人话,**绝不把 raw 画到屏幕上**。
 */
export function safeLabel(text: string | null | undefined, fallback = '暂无结论'): string {
    if (!text) return fallback;
    return looksLikeInternalEnum(text) ? fallback : text;
}

/** §0.5.5 U-1 四态词。前端不得自造第二套(废「稳定/有缺口/未建立/未测」)。 */
export const LEVEL_LABELS: Record<LevelMeta['key'], string> = {
    guarded: '已守住',
    needs_strengthening: '待加强',
    priority_fix: '优先修复',
    unknown: '暂无结论',
};

/**
 * §0.5.5 U-3 五格商业进度条的**格数**。
 *
 * 🔴 [包H 2026-08-23] 这里原本是五个中文标签的数组,且全仓零消费点 ——
 *    它是一份**没人用的第二 SSOT**:后端 `commercial_milestones._PROGRESS_STEPS`
 *    哪天改了措辞,它不会跟着变,也不会有任何东西变红。
 *    真正渲染的 `CommercialProgressBar` 画的是**服务端下发的 steps**。
 *    留下格数是因为它是个结构常量(五格),标签不留。
 */
export const MILESTONE_STEP_COUNT = 5;

/** §8.2 五张客户结果卡的主问题(顺序即呈现顺序,MET-29 禁换序)。 */
export const CUSTOMER_CARDS = [
    { key: 'identity', question: 'AI 认得我吗?' },
    { key: 'recommendation', question: 'AI 会推荐我吗?' },
    { key: 'scenario', question: '客户换种问法还能找到我吗?' },
    { key: 'competition', question: 'AI 把我和谁放在一起?' },
    { key: 'evidence', question: 'AI 的说法有依据吗?' },
] as const;
