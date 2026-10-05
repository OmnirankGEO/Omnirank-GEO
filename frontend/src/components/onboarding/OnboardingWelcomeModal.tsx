/**
 * OnboardingWelcomeModal - Stage 1 Task 2 (v2)
 *
 * 首次进工作台的欢迎弹窗 · v2 版本
 *
 * 设计要点 (v1 被反馈"难看,没有学习欲望",v2 重构):
 *   - 顶部 hero banner (琥珀→橙渐变) 替代纯文字标题
 *   - 大标题 + 数字钩子: "跑通 1 个客户,就上手 80%"
 *   - 个性化分支 3 张卡片: 跑诊断 / 出报价 / 跑全流程 (代替单一 CTA)
 *   - 完整 4 步流程做成默认折叠,降低视觉负担
 *   - 社会证明小字 + 居中 "稍后再说" 兜底按钮
 *
 * 显示条件 (与 v1 一致, 全部满足才弹):
 *   1) localStorage 可用 (隐私模式 / 配额满会跳过)
 *   2) state.welcome_choice === undefined (未做过选择)
 *   3) 已登录 (user 不为空)
 *   4)(#222 a1b 起不再要求身份 —— 原为「是服务商」)
 *   5) 桌面端 (window.innerWidth >= 768) · 移动端不弹
 *
 * 行为矩阵:
 *   点 "跑诊断" 卡  → setWelcomeChoice('start') · 跳 /diagnosis/new
 *   点 "出报价" 卡  → setWelcomeChoice('start') · 跳 /pricing
 *   点 "跑全流程" 卡 → setWelcomeChoice('start') · 跳 /my-clients
 *   点 "稍后再说"   → setWelcomeChoice('later')   · 关闭弹窗 · 下次刷新还会弹
 *   点 右上角 X     → setWelcomeChoice('never')   · 永远不再弹
 *   点 弹窗外蒙层    → setWelcomeChoice('later')   · 等同 "稍后"
 *
 * 无障碍: hero 视觉占位了标题位, 用 sr-only 的 DialogTitle / DialogDescription 兜屏幕阅读器
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Dialog, DialogContent, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  Collapsible, CollapsibleContent, CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  X, ChevronDown, Stethoscope, Calculator, ListChecks,
} from 'lucide-react';
import { useOnboarding } from '@/context/OnboardingContext';
import { useAuth } from '@/context/AuthContext';
import { enterSandbox } from '@/sandbox/sandboxState';
// 四步业务主流程定义集中在共享文件，欢迎页与任务卡共用。
// Welcome Modal 折叠区 + 右下角 Checklist 共享同一份, 单点维护
import { ONBOARDING_STEPS } from './OnboardingStepDefinitions';

// 3 张分支卡片 · 个性化路径
const PATH_CARDS = [
  {
    key: 'diagnosis',
    icon: Stethoscope,
    title: '跑诊断',
    timeHint: '5 分钟',
    target: '/diagnosis/new',
  },
  {
    key: 'pricing',
    icon: Calculator,
    title: '出报价',
    timeHint: '10 分钟',
    target: '/pricing',
  },
  {
    key: 'full-flow',
    icon: ListChecks,
    title: '跑全流程',
    timeHint: '1-2 小时',
    target: '/my-clients',
  },
] as const;

export function OnboardingWelcomeModal() {
  const navigate = useNavigate();
  const { state, setWelcomeChoice, isAvailable } = useOnboarding();
  const { user } = useAuth();

  // 折叠区开关 · 默认收起,降低首屏视觉密度
  const [stepsOpen, setStepsOpen] = useState(false);

  // 关闭来源标记 · 用来区分 "X / 稍后 / 蒙层" 三条路径
  // - 'never'  右上角 X 触发的关闭 (永远不再弹)
  // - 其他场景 (蒙层点击 / "稍后" 按钮) 默认走 'later'
  const [closeIntent, setCloseIntent] = useState<'never' | 'later'>('later');

  /*
   * 🔴 [#222 a1b] 去掉了 `&& isAgent`(理由同 WelcomeChoiceModal):
   *    引导走的是主流程四步,不含经营后台,没有理由只给一部分人看。
   */
  const shouldShow = (
    isAvailable
    && state.welcome_choice === undefined
    && !!user
    && typeof window !== 'undefined'
    && window.innerWidth >= 768
  );

  if (!shouldShow) return null;

  const handlePickPath = (_target: string) => {
    // Stage 1 Batch 3 (2026-05-08) · 沙盒模式
    // 任何路径卡都进沙盒, 强制路由到 /my-clients 让用户从 step 1 开始
    // 沙盒数据按 4 步主流程线性预设，中途路径卡只负责选择意图，不跳过业务步骤。
    setWelcomeChoice('start');
    enterSandbox();
    // reload 让 SandboxBanner 在新 mount 周期里显示 + 拦截器立即生效
    window.location.href = '/my-clients';
  };

  const handleLater = () => {
    setWelcomeChoice('later');
  };

  const handleNever = () => {
    setCloseIntent('never');
    setWelcomeChoice('never');
  };

  // Dialog 自带的 X 已被 [&>button]:hidden 隐藏 · 关闭路径全部走 onOpenChange
  const handleOpenChange = (open: boolean) => {
    if (open) return;
    if (closeIntent === 'never') {
      // handleNever 已经写过状态了, 这里只重置标志
      setCloseIntent('later');
      return;
    }
    handleLater();
  };

  return (
    <Dialog open={true} onOpenChange={handleOpenChange}>
      {/*
        max-w-2xl: 比 v1 的 lg 宽一档, 容纳 3 张并排卡片
        p-0 + overflow-hidden: hero banner 需要贴边, 不能被默认 p-5 切掉
        [&>button]:hidden: 隐藏 DialogContent 内置的右上角 X, 由我们自己的 X 接管 (避免行为不可控)
      */}
      <DialogContent className="max-w-2xl gap-0 [&>button]:hidden">
        {/* 屏幕阅读器用的隐藏标题 */}
        <DialogTitle className="sr-only">新手引导</DialogTitle>
        <DialogDescription className="sr-only">
          选择一条最适合你的开始路径,或稍后再说
        </DialogDescription>

        {/* 自定义右上角 X · 行为 = 'never' (永远不再弹) · 替代被隐藏的内置 X */}
        <Button
          variant="ghost"
          size="icon"
          onClick={handleNever}
          aria-label="不再显示此引导"
          className="absolute right-3 top-3 h-7 w-7"
        >
          <X className="h-4 w-4" />
        </Button>

        {/* 主体内容区 (无 hero · 直接顶天) */}
        <div className="space-y-5">
          {/* 标题块 */}
          <div className="space-y-1">
            <h2 className="text-2xl font-bold tracking-tight">
              跑通 1 个客户,就上手 80%
            </h2>
            <p className="text-sm text-muted-foreground">
              第一份诊断报告,只要 5 分钟
            </p>
          </div>

          {/* 个性化分支提问 */}
          <div className="space-y-3">
            <p className="text-sm font-medium">你今天最想先做哪件事?</p>

            <div className="grid grid-cols-3 gap-3">
              {PATH_CARDS.map((card) => (
                <button
                  key={card.key}
                  type="button"
                  onClick={() => handlePickPath(card.target)}
                  className="
                    group flex flex-col items-start gap-2 text-left
                    border rounded-xl p-4 cursor-pointer
                    transition-all hover:scale-[1.02]
                    hover:border-amber-500 hover:shadow-lg
                    focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-2
                  "
                >
                  <card.icon className="h-8 w-8 text-amber-500" />
                  <div>
                    <div className="text-base font-semibold">{card.title}</div>
                    <div className="text-xs text-muted-foreground">{card.timeHint}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* 完整 4 步流程 · 默认折叠 */}
          <Collapsible open={stepsOpen} onOpenChange={setStepsOpen}>
            <CollapsibleTrigger
              className="
                flex items-center gap-1 text-sm text-muted-foreground
                hover:text-foreground transition-colors cursor-pointer
                focus:outline-none
              "
            >
              <ChevronDown
                className={`h-4 w-4 transition-transform ${stepsOpen ? 'rotate-180' : ''}`}
              />
              <span>完整 4 步流程</span>
            </CollapsibleTrigger>
            <CollapsibleContent className="pt-3">
              <div className="grid grid-cols-1 gap-1.5">
                {ONBOARDING_STEPS.map((step) => (
                  <div
                    key={step.step_id}
                    className="flex items-center gap-3 px-3 py-2 rounded-lg bg-muted/30"
                  >
                    <span
                      className="
                        flex h-6 w-6 items-center justify-center
                        rounded-full bg-primary/10 text-primary text-xs font-medium
                      "
                    >
                      {step.index}
                    </span>
                    <step.icon className="h-4 w-4 text-muted-foreground" />
                    <span className="text-sm">{step.label}</span>
                  </div>
                ))}
              </div>
            </CollapsibleContent>
          </Collapsible>

          {/* 社会证明 */}
          <p className="text-xs text-muted-foreground text-center pt-2">
            100+ 服务商已用这套流程跑出方案
          </p>

          {/* 分隔线 + 兜底按钮 */}
          <div className="border-t pt-4 flex justify-center">
            <Button
              variant="outline"
              size="sm"
              onClick={handleLater}
              className="min-w-32"
            >
              稍后再说
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default OnboardingWelcomeModal;
