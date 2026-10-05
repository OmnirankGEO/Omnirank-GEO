import type { TutorialStage } from './tutorialStage';

export interface SandboxTutorialNavItem {
    to: '/diagnosis/new' | '/pricing' | '/writing' | '/monitoring';
    label: string;
    stepNumber: 1 | 2 | 3 | 4;
    stage: TutorialStage;
    nextStage: TutorialStage;
    featureId: string;
    stepId: 'first_diagnosis' | 'first_quote' | 'first_article_publish' | 'first_monitoring';
    title: string;
    content: string;
}

/**
 * 沙盒左栏的唯一合同。
 *
 * 教程不能再复用管理员/服务商/普通用户各自可折叠、可记忆的真实导航位置，
 * 否则账号身份和滚动状态会让 spotlight 漂到相邻分组。这里只固定教程外壳；
 * 右侧仍进入真实路由、真实组件，并由 sandbox interceptor 提供零副作用数据。
 */
export const SANDBOX_TUTORIAL_NAV_ITEMS: readonly SandboxTutorialNavItem[] = [
    {
        to: '/diagnosis/new',
        label: '品牌体检',
        stepNumber: 1,
        stage: 'sidebar',
        nextStage: 'page-intro',
        featureId: 'sandbox_sidebar_diagnosis',
        stepId: 'first_diagnosis',
        title: '第一步: 点这里进入品牌体检',
        content: '5 分钟看品牌在 AI 搜索里的可见度 · 教程会带你跑一次诊断',
    },
    {
        to: '/pricing',
        label: '报价方案',
        stepNumber: 2,
        stage: 'step2-sidebar',
        nextStage: 'step2-page',
        featureId: 'sandbox_sidebar_quote',
        stepId: 'first_quote',
        title: '第二步: 点这里进入报价方案',
        content: '基于诊断结果给客户生成报价单 · 教程会带你走完在线报价',
    },
    {
        to: '/writing',
        label: 'AI 写文章与发布',
        stepNumber: 3,
        stage: 'step3-sidebar',
        nextStage: 'step3-page',
        featureId: 'sandbox_sidebar_writing',
        stepId: 'first_article_publish',
        title: '第三步: 点这里进入 AI 写文章',
        content: '生成 GEO 文章并走完模拟发布 · 全程不调用真实模型、不扣算力',
    },
    {
        to: '/monitoring',
        label: '效果监测',
        stepNumber: 4,
        stage: 'step4-sidebar',
        nextStage: 'step4-page',
        featureId: 'sandbox_sidebar_monitoring',
        stepId: 'first_monitoring',
        title: '第四步: 点这里看效果',
        content: '看出现率、达标词和待补词 · 教程会带你完成一次零扣费补救',
    },
] as const;

export function getSandboxTutorialNavItem(
    stage: TutorialStage,
): SandboxTutorialNavItem | null {
    return SANDBOX_TUTORIAL_NAV_ITEMS.find(item => item.stage === stage) ?? null;
}

/**
 * 生产页面按项目状态决定是否展示标题生成按钮；教程阶段则必须保证真实按钮存在，
 * 否则持久化 stage 会落入“有说明、无目标”的死路。
 */
export function shouldRenderTitleGenerationAction(
    writingStatus: string | null | undefined,
    hasAnyKeywordWithoutTopics: boolean,
    tutorialStage: TutorialStage,
    sandboxActive: boolean,
): boolean {
    if (sandboxActive && tutorialStage === 'step3-gen-titles') return true;
    return writingStatus === 'pending' || hasAnyKeywordWithoutTopics;
}
