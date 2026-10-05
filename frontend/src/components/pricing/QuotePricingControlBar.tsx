/**
 * QuotePricingControlBar — 在线报价页顶部「报价设置」紧凑控制栏
 *
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11]
 *
 * 事故现场:服务商当面向客户演示报价流程时,
 *   ① 「审核与发送」正文里一整块「本次报价系数」卡(售价系数 / 调整原因 / 套餐换算 /
 *      关键词标准价 / 冻结说明)客户全看得见 → 内部加价逻辑当场暴露;
 *   ② 顶部眼睛按钮只把**数值**换成 `••`,「默认报价系数」「毛利」「单篇成本」
 *      「平台设定 / 你设定的」这些**字段名**照旧渲染 → 不是真的隐私模式;
 *   ③ 显示态存 localStorage(`omnirank_pricing_reveal`),上次点过显示,
 *      下次进页面/刷新仍然是公开态 → 演示前根本没有"默认安全"。
 *
 * 本组件把两类系数收进同一条紧凑栏的两个标签,并落工单 §6 的隐私显示合同:
 *   · 默认隐藏 —— 纯 React state,**零持久化**(刷新 / 新标签页 / 重开浏览器都从隐藏开始);
 *   · 隐藏时敏感区渲染 `********`,不是遮数值留字段名;
 *   · 隐藏时敏感文字、数字、输入框**不在可见 DOM**(不用 opacity / blur / visibility 遮挡),
 *     本次系数预览请求也不发(不预加载);
 *   · 切报价 / 保存成功 / 标签页离开后返回 / 刷新 → 自动恢复隐藏。
 *
 * 🔴 两类系数不得混淆(工单 §4):
 *   「默认报价设置」= QuoteMarkupCard,PUT /api/auth/profile,只影响**以后新生成**的报价;
 *   「修改本次报价系数」= QuoteCoefficientEditor,POST /api/quotes/:id/coefficient,
 *     只影响**当前这一份**报价,走既有 CAS + 审计 + 不可变快照。
 *
 * 🔴 当前报价上下文由 OnlineQuoteFlow 单点提供(工单 §7):本组件**不自己维护**
 *    一套 session / quote_id 状态,否则切客户后会显示上一客户的系数。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Eye, EyeOff, Percent, Settings2 } from 'lucide-react';
import QuoteMarkupCard from './QuoteMarkupCard';
import QuoteCoefficientEditor from './QuoteCoefficientEditor';
import { PRICING_MASK, usePricingPrivacy } from './pricingPrivacy';

export { PRICING_MASK };

type ControlTab = 'default' | 'current';

/** 审核阶段之前 —— 报价还没算出来,没有"本次系数"可调 */
const PRE_REVIEW_STATUSES = new Set(['selecting', 'business_lines_submitted', 'keywords_submitted']);
/** 后端 services/quote_pricing_snapshot.save_coefficient_snapshot 唯一放行的状态 */
const EDITABLE_STATUS = 'pricing_pending_review';

export interface CurrentQuoteContext {
  /** 不可变报价单 id;null = 没选中报价 */
  quoteId: number | null;
  /** 选词会话状态 */
  status: string | null;
  /** 侧边栏里由历史报价补出来的占位行 —— 还没有真实选词会话 */
  isPlaceholder?: boolean;
  /** pricingData.generated_at,重算后触发重新预览 */
  pricingGeneratedAt?: string | null;
  /** 现有报价编辑权限(quote.create) */
  canEditQuote: boolean;
  /** 演示只读 / 沙盒禁止写入 */
  readOnly?: boolean;
}

export type CoefficientGate =
  | { editable: true }
  | { editable: false; message: string };

/**
 * 本次系数标签的出现条件(工单 §5)· 纯函数,便于反向核验。
 * 🔴 只是 UX 出口:后端 `save_coefficient_snapshot` 仍然独立校验状态,
 *    前端放宽不会让已发送报价被改(工单 §9 后端边界不动)。
 */
export function resolveCoefficientGate(ctx: CurrentQuoteContext): CoefficientGate {
  if (!ctx.quoteId || ctx.isPlaceholder) {
    return { editable: false, message: '请先选择一份报价' };
  }
  if (ctx.readOnly) {
    return { editable: false, message: '演示只读模式下不能修改本次报价系数' };
  }
  if (!ctx.status || PRE_REVIEW_STATUSES.has(ctx.status)) {
    return { editable: false, message: '当前报价尚未进入审核阶段' };
  }
  if (ctx.status !== EDITABLE_STATUS) {
    return { editable: false, message: '报价已发送，如需调整请先按现有流程撤回' };
  }
  if (!ctx.canEditQuote) {
    return { editable: false, message: '你的账号没有报价编辑权限，请联系管理员开通' };
  }
  return { editable: true };
}

export interface QuotePricingControlBarProps {
  context: CurrentQuoteContext;
  /** 本次系数保存成功 → 刷新报价数据(套餐/关键词报价立即刷新) */
  onQuoteRefresh: () => void;
  /** 预览/保存在途 → 上报给 OnlineQuoteFlow,用于禁用「发送报价给客户」 */
  onCoefficientBusyChange?: (busy: boolean) => void;
}

export default function QuotePricingControlBar({
  context, onQuoteRefresh, onCoefficientBusyChange,
}: QuotePricingControlBarProps) {
  const [activeTab, setActiveTab] = useState<ControlTab | null>(null);
  // 🔴 隐私态不是本组件的局部 state —— 关键词展开区的「为什么是这个价」也要跟着遮
  //    (返修 R2:首轮就是因为关在这里,眼睛按钮对那块完全无效)。
  //    切报价回隐藏 / 标签页返回回隐藏 / 零持久化,全在 PricingPrivacyProvider 里。
  const { revealed, setRevealed, toggleRevealed } = usePricingPrivacy();

  /* 忙态汇总:preview 与 save 分开记(返修 R1)。
     save 在途时用户点眼睛隐藏会卸载编辑器,那一刻**不许**把锁松开 ——
     编辑器只在 POST 的 finally 里发 save=false,这里如实转发即可。 */
  const [busy, setBusy] = useState({ preview: false, save: false });
  const handleEditorBusy = useCallback((kind: 'preview' | 'save', value: boolean) => {
    setBusy(prev => (prev[kind] === value ? prev : { ...prev, [kind]: value }));
  }, []);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  useEffect(() => {
    onCoefficientBusyChange?.(busy.preview || busy.save);
  }, [busy, onCoefficientBusyChange]);

  const gate = resolveCoefficientGate(context);

  const handleSaved = useCallback(() => {
    setRevealed(false);   // 保存成功 → 自动恢复隐藏(工单 §6)
    onQuoteRefresh();
  }, [onQuoteRefresh, setRevealed]);

  const tabClass = (tab: ControlTab) =>
    `flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
      activeTab === tab
        ? 'bg-background text-foreground shadow-sm'
        : 'text-muted-foreground hover:text-foreground'
    }`;

  const toggleTab = (tab: ControlTab) => setActiveTab(prev => (prev === tab ? null : tab));

  return (
    <div
      className="mb-3 shrink-0 rounded-lg border border-border bg-card text-card-foreground"
      data-testid="quote-pricing-control-bar"
    >
      {/* 紧凑栏:只有两个功能标签 + 眼睛。
          🔴 这里**不许**再放系数/毛利/成本/来源徽章 —— 那正是工单 §2 点名的泄露面。 */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <div className="flex min-w-0 items-center gap-1 rounded-lg bg-secondary/60 p-0.5">
          <button
            type="button"
            className={tabClass('default')}
            aria-pressed={activeTab === 'default'}
            onClick={() => toggleTab('default')}
            data-testid="quote-pricing-tab-default"
          >
            <Settings2 className="size-3.5 shrink-0" />
            <span className="truncate">默认报价设置</span>
          </button>
          <button
            type="button"
            className={tabClass('current')}
            aria-pressed={activeTab === 'current'}
            onClick={() => toggleTab('current')}
            data-testid="quote-pricing-tab-current"
          >
            <Percent className="size-3.5 shrink-0" />
            <span className="truncate">修改本次报价系数</span>
          </button>
        </div>

        {!revealed && (
          <span
            className="font-mono text-xs tracking-widest text-muted-foreground"
            data-testid="quote-pricing-mask-inline"
          >
            {PRICING_MASK}
          </span>
        )}

        <button
          type="button"
          onClick={() => toggleRevealed()}
          className="ml-auto shrink-0 text-muted-foreground hover:text-foreground"
          title={revealed ? '隐藏报价隐私信息' : '显示报价隐私信息'}
          aria-label={revealed ? '隐藏报价隐私信息' : '显示报价隐私信息'}
          data-testid="quote-pricing-reveal-toggle"
        >
          {revealed ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>
      </div>

      {activeTab && (
        <div className="border-t border-border px-3 py-3" data-testid="quote-pricing-panel">
          {!revealed ? (
            /* 🔴 隐藏态整块只有星号 + 一个开关。
               敏感字段名(默认报价系数/毛利/单篇成本/系数来源/调整原因/快照…)
               和输入框在这一支里**根本没被渲染**,不是被 CSS 盖住。 */
            <div className="flex flex-wrap items-center gap-3" data-testid="quote-pricing-masked">
              <span className="font-mono text-sm tracking-widest text-muted-foreground">{PRICING_MASK}</span>
              <button
                type="button"
                onClick={() => setRevealed(true)}
                className="text-xs text-foreground underline underline-offset-2"
                data-testid="quote-pricing-masked-reveal"
              >
                显示报价隐私信息
              </button>
            </div>
          ) : activeTab === 'default' ? (
            <QuoteMarkupCard />
          ) : gate.editable ? (
            <QuoteCoefficientEditor
              key={context.quoteId ?? 'none'}
              quoteId={context.quoteId as number}
              pricingGeneratedAt={context.pricingGeneratedAt}
              onSaved={handleSaved}
              onBusyChange={handleEditorBusy}
            />
          ) : (
            <p className="text-xs text-muted-foreground" data-testid="quote-coefficient-blocked">
              {gate.message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
