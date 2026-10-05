/**
 * 报价中心 — 统一入口
 *
 * 2026-05-25 产品决策:砍掉"快速录单"+"报告页一键报价" 2 个通道 · 只保留"在线报价"单路径
 * 之前的双模式 toggle / SandboxQuotePathPicker / completedPaths 状态机全部移除
 *
 * CTO-15.21 v5 收口:接 ?brand_id 深链 · 自动切客户(其它深链已移除)
 */
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import OnlineQuoteFlow from './OnlineQuoteFlow';
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
import { useClientContext } from '@/context/ClientContext';
import { useSandboxState } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
import { useOnboarding } from '@/context/OnboardingContext';
import { toast } from 'sonner';

export default function PricingCenter() {
  const [searchParams] = useSearchParams();
  const { currentBrandId, switchClient } = useClientContext();
  const { isSandbox } = useSandboxState();
  const tutorialStage = useTutorialStage();
  const { markStepCompleted, isStepCompleted } = useOnboarding();

  const urlBrandId = searchParams.get('brand_id');
  // 报价设置栏的 portal 落点 · 用 state 而不是 useRef:ref 变化不会触发重渲染,
  // OnlineQuoteFlow 就永远收不到这个 DOM 节点(拿到 null 会退化成内联渲染)。
  const [barSlot, setBarSlot] = useState<HTMLDivElement | null>(null);

  // 沙盒 step 2 · 进 /pricing · 推进 stage 到 online-form(让 OnlineQuoteFlow 内部 spotlight 接力)
  useEffect(() => {
    if (!isSandbox) return;
    if (tutorialStage === 'step2-sidebar' || tutorialStage === 'step2-page') {
      setTutorialStage('step2-online-form');
    }
  }, [isSandbox, tutorialStage]);

  // 沙盒里在线报价流程走完(OnlineQuoteFlow 在收款成功后 dispatch 该事件)→ 标 step 2 完成 + 推进到 step 3
  useEffect(() => {
    if (!isSandbox) return;
    const handler = () => {
      const wasCompleted = isStepCompleted('first_quote');
      markStepCompleted('first_quote');
      if (wasCompleted) {
        window.dispatchEvent(new CustomEvent('sandbox:step-celebrate', { detail: { stepId: 'first_quote' } }));
      }
      setTutorialStage('step3-sidebar');
      toast.success('Step 2 完成 ✓ · 接下来去 AI 写文章 + 发布');
    };
    window.addEventListener('sandbox:quote-path-done', handler);
    return () => window.removeEventListener('sandbox:quote-path-done', handler);
  }, [isSandbox, markStepCompleted, isStepCompleted]);

  // ?brand_id 自动切客户(全站 ClientContext 联动)
  useEffect(() => {
    if (!urlBrandId) return;
    const bid = parseInt(urlBrandId, 10);
    if (Number.isFinite(bid) && bid > 0 && bid !== currentBrandId) {
      switchClient(bid);
    }
  }, [urlBrandId, currentBrandId, switchClient]);

  return (
    <div className="h-full min-h-0 flex flex-col overflow-hidden p-3 sm:p-4 md:p-6">
      {/* CTO-15.20 桥接 banner · URL ?brand_id=X 时显示 */}
      <BridgeBanner />
      {/* 报价设置栏的**固定槽位**(2026-08-11 R3)。
          🔴 位置必须在这里 —— 与 OnlineQuoteFlow 的滚动区同级且在其之上,所以它**不随内容滚动**,
             这正是老板要的「和之前一样」:原来的 QuoteMarkupCard 就挂在这个位置,常驻可见;
             R1/R2 那版把它挪进了 OnlineQuoteFlow 的 overflow-y-auto 里,往下翻就看不见了。
          🔴 只放空槽、不在这里渲染组件:当前报价上下文(session / quote_id / pricingData)
             只有 OnlineQuoteFlow 有。组件仍在它的 React 树里渲染,通过 portal 把 DOM 投到这个槽 ——
             于是既拿到了同步的报价上下文(切客户不会慢一帧显示上一客户系数),
             又拿到了「不随内容滚动」的位置。**这里不许改成直接挂组件。** */}
      <div ref={setBarSlot} className="shrink-0" data-testid="quote-pricing-bar-slot" />
      <div className="flex-1 min-h-0 overflow-hidden">
        <OnlineQuoteFlow topBarSlot={barSlot} />
      </div>
    </div>
  );
}
