/**
 * 三步进度条的纯逻辑(工单 §1.1)。
 *
 * 单独一个无 React 依赖的模块,好让锁能直接 import 真跑 ——
 * 塞在 .tsx 里的话,锁要么去 bundle 整棵 React 依赖树,要么只能退回读源码串。
 */
export const LAUNCH_STEPS = [
    { id: 1, label: '基础信息' },
    { id: 2, label: '搜索问题' },
    { id: 3, label: '启动检查' },
] as const;

export interface LaunchStepInput {
    /** 品牌名称(必填) */
    brandName: string;
    /** 所属行业(必填) */
    industry: string;
    /** 核心搜索问题的有效条数 */
    questionCount: number;
}

/**
 * 当前处在第几步。
 * - 品牌名 / 行业 还没填齐 → 第 1 步
 * - 齐了但一条搜索问题都没有 → 第 2 步
 * - 都有了 → 第 3 步(可以启动了)
 *
 * 🔴 只是**位置指示**:它不拦提交、不禁用字段、点它也不跳步。
 */
export function resolveLaunchStep(input: LaunchStepInput): number {
    if (!input.brandName.trim() || !input.industry.trim()) return 1;
    if (input.questionCount < 1) return 2;
    return 3;
}
