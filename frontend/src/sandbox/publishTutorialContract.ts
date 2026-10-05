import type { TutorialStage } from './tutorialStage';

export type SandboxPublishAudience = 'standard' | 'provider';

export interface SandboxPublishUserLike {
  is_admin?: boolean | null;
  agent_level?: number | null;
}

export interface SandboxPublishGuide {
  audience: SandboxPublishAudience;
  title: string;
  description: string;
  nextAction: string;
  activeStep: number;
  steps: readonly string[];
}

const PUBLISH_STEPS = ['选文章', '选渠道', '加入清单', '模拟发布'] as const;

const STAGE_GUIDES: Partial<Record<TutorialStage, { activeStep: number; nextAction: string }>> = {
  'step3-pub-pick-article': {
    activeStep: 1,
    nextAction: '先勾选左侧第一篇文章。',
  },
  'step3-pub-pick-media': {
    activeStep: 2,
    nextAction: '再选择右侧第一个发布渠道。',
  },
  'step3-pub-add-cart': {
    activeStep: 3,
    nextAction: '点击“加入发布清单”，其余示例文章会自动配好渠道。',
  },
  'step3-pub-batch-send': {
    activeStep: 4,
    nextAction: '点击底部“模拟发布”，完成零算力操作演练。',
  },
  'step4-opt-batch': {
    activeStep: 4,
    nextAction: '点击底部“模拟发布”，完成补发演练并返回效果监测。',
  },
};

export function resolveSandboxPublishAudience(
  user: SandboxPublishUserLike | null | undefined,
): SandboxPublishAudience {
  return user?.is_admin === true || Number(user?.agent_level ?? 0) >= 1
    ? 'provider'
    : 'standard';
}

export function getSandboxPublishGuide(
  stage: TutorialStage,
  user: SandboxPublishUserLike | null | undefined,
): SandboxPublishGuide {
  const audience = resolveSandboxPublishAudience(user);
  const stageGuide = STAGE_GUIDES[stage] ?? {
    activeStep: 1,
    nextAction: '跟随页面高亮完成当前操作。',
  };

  return {
    audience,
    title: audience === 'provider'
      ? '发布演练 · 服务商工作流'
      : '发布演练 · 普通用户视角',
    description: audience === 'provider'
      ? '在真实发布页面练习为客户选文章和渠道。教程不会外发、扣算力或创建审批任务。'
      : '在真实发布页面完成一次模拟发布。你只需选择内容和渠道，不需要扮演管理员或处理审批。',
    nextAction: stageGuide.nextAction,
    activeStep: stageGuide.activeStep,
    steps: PUBLISH_STEPS,
  };
}

export function canShowSandboxManualGovernance(
  sandboxActive: boolean,
  user: SandboxPublishUserLike | null | undefined,
): boolean {
  return !sandboxActive || resolveSandboxPublishAudience(user) === 'provider';
}
