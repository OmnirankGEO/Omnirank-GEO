/**
 * 交付计划 · 客户售前预览(WO_GAPPLAN_RELOCATION_A 2026-08-10 · 阶段 2)
 *
 * 落点:客户选词报价页 `/s/:token`,紧接词/价格块之后 —— 与服务商侧在报价详情里的
 * 原落点在客户侧对称。回答客户掏钱前最想知道的那件事:**你们拿到钱以后要做什么**。
 *
 * 🔴 本组件**不发任何请求**。数据随 `GET /api/s/{token}` 一起下发
 *    (api/selection_api.py::_build_customer_delivery_plan)。
 *    客户是 token-only 会话,`/api/quotes/` 在 auth 中间件层就 401 ——
 *    在这里写 fetch 只会得到一个必然失败的请求。
 *
 * 🔴 本组件**不接 onAction、不 import GapTaskCard**。
 *    GapTaskCard 内建「去写这篇 / 已发布填链接 / 渠道不可用」三个写操作,
 *    整体复用等于把执行入口渲染给客户。要共用的只有文案块 → `GapTaskRationale`。
 *
 * 🔴 屏蔽「能否进入」话术是**服务端**做的(present_snapshot audience=customer),
 *    不是这里挑着不渲染:前端不渲染只是没画出来,接口照样把它发到了浏览器。
 *    生产实测 media_outlets.entry_assessment 27,004 行全 NULL —— 客户在掏钱决策
 *    阶段看到「这个网站还没有人核实过能否进入」,读到的是"你们连能不能发都没搞清楚"。
 *
 * 🔴 客户页是**亮色强制**(SelectionPage useForceLightMode),所以这里用的是
 *    该页一致的 gray/blue 直接色阶,不是报价页那套语义 token —— 两套页面各自成体系,
 *    混用会在客户页出现半深半浅的色块。
 */
import type { GapPlanCustomerPreview } from '@/lib/gapPlanApi';
import { GapTaskRationale } from './GapTaskRationale';

interface Props {
  plan?: GapPlanCustomerPreview | null;
}

export function GapPlanPreview({ plan }: Props) {
  // 没算出来(还没有观察数据 / 服务暂时不可用)→ 整块不出现。
  // 🔴 刻意不显示"暂时无法加载":客户不认识这个模块,一个报错块只会制造疑虑。
  if (!plan || !plan.items?.length) return null;

  return (
    <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-3">
      <div className="bg-white rounded-2xl border border-gray-100 shadow-xs overflow-hidden">
        <div className="px-5 md:px-6 py-4 border-b border-gray-100">
          <p className="text-sm md:text-base font-semibold text-gray-700">
            确认后我们会做什么
          </p>
          {plan.summary.headline && (
            <p className="text-xs md:text-sm text-gray-500 mt-1.5 leading-relaxed">
              {plan.summary.headline}
            </p>
          )}
          {plan.summary.next_step && (
            <p className="text-xs md:text-sm text-gray-500 mt-1 leading-relaxed">
              {plan.summary.next_step}
            </p>
          )}
          {plan.query_family.display_query && (
            <p className="text-xs text-gray-400 mt-2">
              目标问题：{plan.query_family.display_query}
            </p>
          )}
        </div>

        <ol className="divide-y divide-gray-100">
          {plan.items.map((item) => (
            <li key={item.plan_item_id} className="px-5 md:px-6 py-4">
              <div className="flex items-start gap-3">
                <span className="shrink-0 mt-0.5 text-[11px] text-gray-400 tabular-nums">
                  {String(item.ordinal).padStart(2, '0')}
                </span>
                <div className="min-w-0 flex-1">
                  <GapTaskRationale item={item} />
                </div>
              </div>
            </li>
          ))}
        </ol>

        <p className="px-5 md:px-6 py-3 text-[11px] text-gray-400 border-t border-gray-100">
          以上为当前观察结果下的内容安排，实际篇目会随 AI 回答的变化调整。
        </p>
      </div>
    </div>
  );
}

export default GapPlanPreview;
