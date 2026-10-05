/**
 * 出现率 → 话术翻译(客户/代理 UI 用)
 *
 * 阈值跟后端 transparent_pricing.py::describe_probability 严格对齐:
 *   后端(0-1 概率):0.95 / 0.85 / 0.70 / 0.50 / 0.30
 *   前端(0-100 百分比):95 / 85 / 70 / 50 / 30
 *
 * 出现率 → 话术映射:
 *   ≥ 95 → 几乎每次都出现(垄断级)
 *   ≥ 85 → 问 4 次约出现 3 次(旗舰版)
 *   ≥ 70 → 问 3 次约出现 2 次(进阶版)
 *   ≥ 50 → 问 2 次约出现 1 次(基础版/入门版)
 *   ≥ 30 → 问 3 次约出现 1 次(覆盖较少)
 *   < 30 → 偶尔出现
 *
 * [V3.5 v6 工厂模式 2026-05-26] 占有率类术语铁律见 docs/AI-CONTEXT/SOV_ALLOWLIST.md
 * 仅算法/admin 后台保留 · 客户 + 代理 UI 永远只用"出现率"
 */

export interface ProbabilityDescription {
    /** 自然语言话术(给客户看) */
    label: string;
    /** 套餐档位(基础版/进阶版/旗舰版/垄断级) */
    tier: string;
    /** 百分比数值(仅给代理后台 · 客户页不展示) */
    rawPct?: number;
    /** [r13 圆点可视化] 总问询次数(分母 N) · 用于"问 N 次出现 M 次" */
    askCount: number;
    /** [r13 圆点可视化] 出现次数(分子 M) */
    appearCount: number;
    /** [r13 圆点可视化] 垄断级 · 渲染时画满 5 点 + 显示"几乎每次" */
    isUbiquitous: boolean;
    /** [r13 圆点可视化] 极低覆盖 · 渲染时画 5 空点 + "偶尔" */
    isMinimal: boolean;
}

export function describeProbability(occurrenceRate: number, options?: { showRawForAgent?: boolean; quoteMode?: boolean }): ProbabilityDescription {
    // [P1 2026-06-05 老板定稿] quoteMode = 报价套餐/套餐卡片/确认页 强制口径:永不出「90%+ 几乎每次都出现」
    //   即使旧缓存/异常 payload 带 90%+,也钳到 <90(最高 问4次3次 旗舰档)· 只显三档目标口径(50/65/75)。
    //   90%+ ubiquitous 仅限内部/历史监测结果展示(非 quoteMode)。
    const _raw = Math.max(0, Math.min(100, occurrenceRate));
    const pct = options?.quoteMode ? Math.min(_raw, 89) : _raw;
    let label: string;
    let tier: string;
    let askCount: number;
    let appearCount: number;
    let isUbiquitous = false;
    let isMinimal = false;
    // [2026-06-05] 阈值比例对齐(前后端统一)· 让三档目标出现率 50/65/75 话术各异(消除旧 50/65 撞同档):
    //   50% → 问 2 次约 1 次 · 65% → 问 3 次约 2 次 · 75% → 问 4 次约 3 次 · 与「目标出现率 X%」并存展示(老板定稿)
    // [老板订正 2026-06-05] ≥90「几乎每次都出现」仅限【内部/历史监测结果】兜底描述 ·
    //   禁出现在报价套餐/套餐卡片/合同交付口径。报价唯二调用点 TierSelector + ConfirmCelebration
    //   只传固定 50/65/75(≤75 < 90)→ 永远命中不到本 ubiquitous 分支(调用场景已隔离)。监测面已改精确%。
    if (pct >= 90) {
        label = "几乎每次都出现";
        tier = "垄断级";
        askCount = 5; appearCount = 5;
        isUbiquitous = true;
    } else if (pct >= 73) {
        label = "问 4 次约出现 3 次";
        tier = "旗舰版";
        askCount = 4; appearCount = 3;
    } else if (pct >= 58) {
        label = "问 3 次约出现 2 次";
        tier = "进阶版";
        askCount = 3; appearCount = 2;
    } else if (pct >= 43) {
        label = "问 2 次约出现 1 次";
        tier = "基础版";
        askCount = 2; appearCount = 1;
    } else if (pct >= 25) {
        label = "问 4 次约出现 1 次";
        tier = "覆盖较少";
        askCount = 4; appearCount = 1;
    } else {
        label = "偶尔出现";
        tier = "暂无曝光";
        askCount = 5; appearCount = 0;
        isMinimal = true;
    }
    return {
        label,
        tier,
        askCount,
        appearCount,
        isUbiquitous,
        isMinimal,
        ...(options?.showRawForAgent ? { rawPct: _raw } : {}),
    };
}

/** 内部算法用 · 占有比 → 出现率近似映射(饱和曲线 · 同 transparent_pricing.py)
 * ⚠️ 仅 algorithm/admin 内部用 · UI 永禁暴露此映射的输入数据
 * 见 docs/AI-CONTEXT/SOV_ALLOWLIST.md(allowlist 第 1 条)
 */
export function sovToOccurrenceRate(sov: number): number {
    if (sov >= 0.50) return 97;
    if (sov >= 0.30) return 86;
    if (sov >= 0.20) return 76;
    if (sov >= 0.10) return 56;
    return Math.round(sov * 100 * 1.5);
}
