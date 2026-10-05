/**
 * 交付计划 · 服务商执行面(B1 / B2 / B7)
 *
 * 唯一落点:现役 /pricing 报价详情内,紧接「客户已选词清单」之后。
 * 🔴 **不新增任何导航或菜单**(合同 §13.1)。设计包原型的左侧导航是为了让静态页
 *    可理解而搭的脚手架,不是 IA 方案。
 *
 * 三层渐进披露(合同 §4):
 *   第一层 一句话诊断条 + 状态灯汇总
 *   第二层 任务卡流
 *   第三层 依据抽屉(默认折叠,匿名化四平台卡)
 *
 * 🔴 快照取不到时**保留上次结果**,只在顶部显示「暂时无法获取最新计划 + 重新获取」,
 *    绝不清空成 0(合同 §5 / 05 文档空态表)。
 *
 * ─────────────────────────────────────────────────────────────
 * 【阶段 1 · WO_GAPPLAN_RELOCATION_A 2026-08-10 · Owner 拍板"按状态分档"】
 *
 * 执行闸没开(客户还没确认报价/还没付款)→ 这里**只渲染一句提示**,不渲染任务卡流。
 * 理由:销售正在做报价时,整段执行计划既帮不上忙又撑破布局;而客户一确认,
 * 同一个位置自动展开可执行的那一版 —— 提示语承诺的正是这件事。
 *
 * 🔴 开关来自服务端 `snapshot.execution_gate.open`(派生自 Capacity.executable),
 *    前端**不看** available_articles / display_status 自己判 —— 容量判断只在服务端,
 *    前端门禁有一条正则钉死这件事(合同判据 #10 的同一条边界)。
 * 🔴 分档时仍保留 `data-help-target="gap-plan-section"` 锚点:小榜「交付计划在哪」
 *    的自然语言向导按操作地图高亮这个 DOM 锚点,摘掉锚点会让向导指向不存在的元素。
 * 🔴 组件名从 GapPlanSection 改成 GapPlanExecution,是为了与客户售前版
 *    `GapPlanPreview` 成对:一个叫"区块"另一个叫"预览"读不出边界,
 *    叫"执行/预览"就读得出 —— 边界要写在名字里,不能只写在注释里。
 *
 * ─────────────────────────────────────────────────────────────
 * 【返工 · WO_GAPPLAN_REWORK_2026-08-11】
 * 本文件现在**只剩取数与副作用**;分档三态与渲染全在 `GapPlanExecutionView.tsx`。
 * 拆分的直接原因是上一轮那条假修复溜过去了 —— 详见 View 文件头。
 */
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  fetchDeliveryPlan, markDomainAccess, parseGapPlanError, submitPublicationLink,
  writingDeepLink,
  type GapAction, type GapPlanError, type GapPlanItem, type GapPlanSnapshot,
} from '@/lib/gapPlanApi';
import { GapPlanExecutionView } from './GapPlanExecutionView';

interface Props {
  quoteId: number;
}

export function GapPlanExecution({ quoteId }: Props) {
  const [snapshot, setSnapshot] = useState<GapPlanSnapshot | null>(null);
  const [error, setError] = useState<GapPlanError | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);

  const load = useCallback(async () => {
    if (!quoteId) return;
    setLoading(true);
    try {
      const data = await fetchDeliveryPlan(quoteId);
      setSnapshot(data);
      setError(null);
    } catch (err) {
      // 🔴 保留上次快照:setSnapshot 不动。运营宁可看到"稍旧但真"的计划,
      //    也不该看到一个凭空变成 0 的页面。
      setError(parseGapPlanError(err));
    } finally {
      setLoading(false);
    }
  }, [quoteId]);

  useEffect(() => { void load(); }, [load]);

  const handleAction = useCallback(
    async (action: GapAction, item: GapPlanItem, payload?: { url?: string }) => {
      if (!action.enabled) return;                    // 占位:不发写请求
      if (!snapshot) return;

      switch (action.action_id) {
        case 'open_writing_task':
          window.location.assign(
            writingDeepLink(quoteId, item.plan_item_id, snapshot.authority_generation),
          );
          return;
        case 'open_media_library':
          window.location.assign('/publish');
          return;
        case 'submit_publication_link': {
          if (!payload?.url) return;
          setBusy(true);
          try {
            const res = await submitPublicationLink(quoteId, item.plan_item_id, {
              publication_url: payload.url,
              // 幂等键:同一计划项 + 同一代际 + 同一链接 → 同一个 key,重试不重复建回查
              idempotency_key: `${quoteId}:${item.plan_item_id}:${snapshot.authority_generation}`,
              expected_authority_generation: snapshot.authority_generation,
            });
            setSnapshot({ ...snapshot, evidence: { ...snapshot.evidence, ...res.evidence } });
            toast.success(res.replayed ? '这条链接之前已经登记过了' : '已登记，系统会在第 7、14、30 天自动检查');
          } catch (err) {
            const parsed = parseGapPlanError(err);
            toast.error(parsed?.message || '暂时没能登记这条链接，请稍后再试');
          } finally {
            setBusy(false);
          }
          return;
        }
        case 'mark_domain_unavailable': {
          setBusy(true);
          try {
            const fresh = await markDomainAccess(quoteId, item.plan_item_id, {
              accessible: false, reason: '渠道当前无法安排',
            });
            setSnapshot(fresh);
            toast.success('已记录，系统重新安排了落点');
          } catch (err) {
            toast.error(parseGapPlanError(err)?.message || '暂时没能更新，请稍后再试');
          } finally {
            setBusy(false);
          }
          return;
        }
        case 'retry_snapshot':
          void load();
          return;
        default:
          // 其余动作(看邻近问题建议 / 请小榜给建议 / 保留建议 …)本包只做展示,
          // 不静默发请求 —— 宁可什么都不做,也不做用户没预期的事。
          return;
      }
    },
    [load, quoteId, snapshot],
  );

  if (!quoteId) return null;

  // 🔴 渲染全部交给纯展示层 GapPlanExecutionView(返工 2026-08-11 拆出)。
  //    容器只管取数与副作用;三分支分档语义与"旧后端 = 旧行为"的回退都在那边,
  //    并由 scripts/test-gap-plan-render.mjs 真渲染验证 —— 不再靠正则扫写法。
  return (
    <GapPlanExecutionView
      snapshot={snapshot}
      error={error}
      loading={loading}
      busy={busy}
      onReload={() => void load()}
      onAction={handleAction}
    />
  );
}

export default GapPlanExecution;
