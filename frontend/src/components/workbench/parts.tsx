/**
 * M3 共享小组件(parts)
 *
 * 基础 UI primitives · 多页面共用 · 单文件方便对照统一规范
 *
 * 设计规范(skill 三件套自审):
 *   - icon 全部 lucide(无 emoji)
 *   - touch target ≥ 44px(button size 默认 sm 也 ≥ 36px · 主按钮 ≥ 44px)
 *   - 颜色用 v7 HSL token(bg-card / bg-muted / text-foreground / text-muted-foreground)
 *   - transition-colors duration-200 微动画
 *   - aria-label 在 icon-only button
 */

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import type { LucideIcon } from 'lucide-react';
import type {
  LifecycleStage,
  RiskLevel,
  BusinessTag as BusinessTagValue,
} from '@/services/m3';
import { BUSINESS_TAG_LABEL, RISK_LABEL, getStageLabel } from '@/services/m3';

// ============================================================
// SectionHeader · 段落标题
// ============================================================

interface SectionHeaderProps {
  title: string;
  count?: number;
  hint?: string;
  action?: ReactNode;
  className?: string;
}

export function SectionHeader({ title, count, hint, action, className }: SectionHeaderProps) {
  return (
    <div className={cn('flex items-end justify-between mb-3', className)}>
      <div>
        <h2 className="text-sm font-semibold text-foreground tracking-tight">
          {title}
          {typeof count === 'number' && (
            <span className="ml-2 text-xs font-normal text-muted-foreground">{count}</span>
          )}
        </h2>
        {hint && <p className="text-xs text-muted-foreground mt-0.5">{hint}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

// ============================================================
// Chip · 筛选 chip(顶部横滑筛选条)
// ============================================================

interface ChipProps {
  label: string;
  count?: number;
  active?: boolean;
  tone?: 'default' | 'urgent';
  onClick?: () => void;
  className?: string;
}

export function Chip({ label, count, active, tone = 'default', onClick, className }: ChipProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-3.5 py-2 text-xs font-medium whitespace-nowrap',
        'min-h-[40px] transition-all duration-150 cursor-pointer',
        'border',
        active
          ? 'bg-foreground text-background border-foreground shadow-sm'
          : 'bg-card text-muted-foreground border-border hover:bg-muted hover:text-foreground hover:border-muted-foreground/30',
        tone === 'urgent' && !active && 'border-destructive/40 text-destructive',
        className,
      )}
    >
      <span>{label}</span>
      {typeof count === 'number' && (
        <span
          className={cn(
            'inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full text-[10px] font-semibold font-mono tabular-nums',
            active ? 'bg-background/15 text-background' : 'bg-muted text-muted-foreground',
            tone === 'urgent' && !active && 'bg-destructive/10 text-destructive',
          )}
        >
          {count}
        </span>
      )}
    </button>
  );
}

// ============================================================
// StagePill · 9 步阶段 pill(用色克制 · 不喧宾夺主)
// ============================================================

const STAGE_TONE: Record<LifecycleStage, string> = {
  1: 'bg-muted text-muted-foreground',
  2: 'bg-blue-500/10 text-blue-600 dark:text-blue-300',
  3: 'bg-amber-500/10 text-amber-600 dark:text-amber-300',
  4: 'bg-orange-500/10 text-orange-600 dark:text-orange-300',
  5: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
  6: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
  7: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
  8: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300',
  9: 'bg-rose-500/10 text-rose-600 dark:text-rose-300',
};

export function StagePill({
  stage,
  className,
}: {
  stage: LifecycleStage;
  className?: string;
}) {
  return (
    <span
      className={cn(
        'inline-flex max-w-full shrink-0 items-center whitespace-nowrap px-2 py-0.5 rounded-md text-[11px] font-medium',
        STAGE_TONE[stage],
        className,
      )}
    >
      {getStageLabel(stage)}
    </span>
  );
}

// ============================================================
// BusinessTag · VIP/大单/慢热/试水
// ============================================================

const BUSINESS_TAG_TONE: Record<BusinessTagValue, string> = {
  vip: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30',
  large: 'bg-violet-500/15 text-violet-700 dark:text-violet-300 border-violet-500/30',
  slow: 'bg-slate-500/15 text-slate-700 dark:text-slate-300 border-slate-500/30',
  trial: 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 border-cyan-500/30',
};

export function BusinessTagBadge({ tag }: { tag: BusinessTagValue }) {
  return (
    <span
      className={cn(
        'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold border',
        BUSINESS_TAG_TONE[tag],
      )}
    >
      {BUSINESS_TAG_LABEL[tag]}
    </span>
  );
}

// ============================================================
// RiskDot · 客户活跃度状态点
// ============================================================

const RISK_DOT_TONE: Record<RiskLevel, string> = {
  stalled: 'bg-destructive',
  renewal: 'bg-rose-500',
  ready: 'bg-amber-500',
  warming: 'bg-sky-500',
  ok: 'bg-emerald-500',
};

export function RiskDot({ risk, className }: { risk: RiskLevel; className?: string }) {
  return (
    <span
      role="img"
      aria-label={`风险等级:${RISK_LABEL[risk]}`}
      className={cn('inline-block h-2 w-2 rounded-full shrink-0', RISK_DOT_TONE[risk], className)}
    />
  );
}

// ============================================================
// TimerPill · "27h" / "T-5" 文本(displays "为什么紧迫")
// ============================================================

export function TimerPill({ value, urgent }: { value: string; urgent?: boolean }) {
  return (
    <span
      className={cn(
        'inline-flex items-center px-1.5 py-0.5 rounded font-mono text-[11px] tabular-nums font-semibold',
        urgent
          ? 'bg-destructive/15 text-destructive ring-1 ring-destructive/20'
          : 'bg-muted text-muted-foreground',
      )}
    >
      {value}
    </span>
  );
}

// ============================================================
// WhyNowTag · "为什么是现在"横排 mono 标签 + 文本
// ============================================================

export function WhyNowTag({ text, icon: Icon }: { text: string; icon?: LucideIcon }) {
  // Phase E.3 · v2 §6.2 · 删 "WHY NOW" 英文术语 · 直接讲事(代理不读 PM 黑话)
  return (
    <div className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-muted/60 max-w-full">
      {Icon && <Icon className="h-3.5 w-3.5 text-amber-600 dark:text-amber-300 shrink-0" aria-hidden />}
      <span className="text-xs text-foreground leading-relaxed">{text}</span>
    </div>
  );
}

// ============================================================
// MetaRow · "客户名 · 行业 · 城市"小元数据行
// ============================================================

export function MetaRow({ items }: { items: (string | undefined | null)[] }) {
  const filtered = (items.filter(Boolean) as string[]).filter((item) => !/^BRD-\d+$/.test(item));
  if (filtered.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
      {filtered.map((item, i) => {
        return (
          <span key={i} className="inline-flex items-center gap-1.5">
            {i > 0 && <span aria-hidden className="opacity-40">·</span>}
            <span>{item}</span>
          </span>
        );
      })}
    </div>
  );
}

// ============================================================
// CompletenessBadge · 客户资料评分徽章(老板拍板阈值 60/80)
// A.1 · 三态:
//   >=80  pass   绿  资料齐全
//   60-79 warn   黄  建议补完(允许继续 · 黄色风险提示)
//   <60   block  红  资料不足(阻断出报价)
// ============================================================

export type CompletenessTier = 'pass' | 'warn' | 'block';

/** 复用阈值判定 · 阻断 Dialog / M3 客户列表页 列表 / Workbench 共用 */
export function getCompletenessTier(score: number): CompletenessTier {
  if (score >= 80) return 'pass';
  if (score >= 60) return 'warn';
  return 'block';
}

const COMPLETENESS_TONE: Record<CompletenessTier, string> = {
  pass: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 ring-emerald-500/20',
  warn: 'bg-amber-500/10 text-amber-600 dark:text-amber-300 ring-amber-500/20',
  block: 'bg-rose-500/10 text-rose-600 dark:text-rose-300 ring-rose-500/20',
};

const COMPLETENESS_LABEL: Record<CompletenessTier, string> = {
  pass: '资料齐全',
  warn: '建议补完',
  block: '资料不足',
};

/** 缺失字段中文映射(给 tooltip / 阻断 Dialog 用 · 不和后端 missing key 强耦合) */
const MISSING_FIELD_LABEL: Record<string, string> = {
  name: '品牌名称',
  industry: '所属行业',
  company_name: '公司主体',
  business: '业务描述',
  target_users: '目标客户',
  cities: '服务城市',
  products: '产品/服务',
  company_intro: '公司简介',
  selling_points: '核心卖点',
  success_cases: '成功案例',
  core_value: '核心价值',
  testimonials: '客户证言',
  industry_brief: '行业洞察',
  industry_brief_confirm: '行业洞察签字',
  industry_brief_running: '行业洞察(分析中)',
  service_scope: '服务范围',
  local_competitors: '本地竞品',
  authority_sources: '权威来源',
  hot_formats: '热门内容形式',
  my_differentiation: '差异化主张',
};

export function getMissingLabel(key: string): string {
  return MISSING_FIELD_LABEL[key] || key;
}

export function CompletenessBadge({
  score,
  missing,
  brandId,
  className,
  showLabel = false,
}: {
  score: number;
  missing?: string[];
  /** 有它才给「去补资料」出口 —— 没有 id 的地方不假装能跳。 */
  brandId?: number;
  className?: string;
  showLabel?: boolean;
}) {
  const tier = getCompletenessTier(score);
  const tooltip =
    missing && missing.length > 0
      ? `${COMPLETENESS_LABEL[tier]} · 缺:${missing
          .slice(0, 5)
          .map(getMissingLabel)
          .join(' / ')}${missing.length > 5 ? `…等 ${missing.length} 项` : ''}`
      : `${COMPLETENESS_LABEL[tier]} · 客户资料评分 ${score}/100`;

  /*
   * 🔴 [#199] 改前这枚徽章可见的只有「85 /100」—— **什么分**、**满分怎么来的**、
   *    **缺哪几项**全在 `title` 里(hover 才看得到,手机上根本没有 hover)。
   *    现在:`showLabel` 时前面带「资料」二字(说清是什么分),后面带三档标签;
   *    有缺项时点一下展开缺项清单 + 「去补资料 →」。
   * 🔴 缺项**不自造**:来自 `/api/brands/{id}/completeness` 的 `missing`
   *    (`BrandCompleteness.missing`),与阻断 Dialog 同一个来源 —— 一处谓词一处数据。
   */
  const canExpand = !!(missing && missing.length > 0 && brandId);
  const [open, setOpen] = useState(false);
  const body = (
    <>
      {showLabel && <span className="mr-1 font-sans font-medium opacity-80">资料</span>}
      <span>{score}</span>
      <span className="opacity-60">/100</span>
      {showLabel && (
        <span className="ml-1 font-sans font-medium">{COMPLETENESS_LABEL[tier]}</span>
      )}
    </>
  );
  const shell = cn(
    'inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded ring-1 text-[10px] font-mono font-semibold tabular-nums',
    COMPLETENESS_TONE[tier],
    className,
  );

  if (!canExpand) {
    return (
      <span role="img" aria-label={`客户资料评分 ${score} 分 · ${COMPLETENESS_LABEL[tier]}`}
        title={tooltip} className={shell} data-testid="completeness-badge">
        {body}
      </span>
    );
  }

  /*
   * 🔴 [#199 a2] 弹层必须 **Portal 到 body**,不能用 `absolute`。
   *
   *    第一版是 `absolute left-0 top-full`,而这枚徽章最主要的挂载点
   *    `DecisionBarBridge` 的那张 Card 带着 `overflow-hidden`
   *    (5/22 为治"中文长串撑出 viewport"加的,不能去掉)。
   *    绝对定位的内容**不撑高父容器** ⇒ 弹层在父容器下边界处被裁,
   *    「去补资料 →」这条出口正好在最下面 —— **必被切掉**。
   *    也就是说:DOM 里有、判据查得到、用户点不到。
   *
   *    与 `InfoBadgeRow` 5/22 v3 同一个理由和同一套做法:Portal + `position: fixed`
   *    + viewport 坐标 clamp。坐标是运行期 px,只能走 inline style
   *    (设计系统禁 inline style 针对的是**样式**,这里是几何)。
   *
   *    伴随后果:fixed 坐标会随滚动失效 ⇒ 滚动/resize 一律收起(同 InfoBadgeRow)。
   */
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLSpanElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null);

  const POP_W = 224; // w-56
  const MARGIN = 8;

  const openAt = () => {
    const btn = btnRef.current;
    if (!btn || typeof window === 'undefined') return;
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth;
    const width = Math.min(POP_W, Math.max(160, vw - MARGIN * 2));
    const left = Math.max(MARGIN, Math.min(r.left, vw - width - MARGIN));
    setPos({ top: r.bottom + 6, left, width });
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || popRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onMove = () => setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('touchstart', onDown);
    window.addEventListener('scroll', onMove, true);
    window.addEventListener('resize', onMove);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('touchstart', onDown);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
    };
  }, [open]);

  return (
    <span className="relative inline-block" data-testid="completeness-badge-wrap">
      <button ref={btnRef} type="button" title={tooltip} className={cn(shell, 'cursor-pointer')}
        data-testid="completeness-badge"
        aria-expanded={open}
        aria-label={`客户资料评分 ${score} 分 · ${COMPLETENESS_LABEL[tier]} · 点开看缺哪几项`}
        onClick={() => { if (open) setOpen(false); else openAt(); }}>
        {body}
      </button>
      {open && pos && typeof document !== 'undefined' && createPortal(
        <span ref={popRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, width: pos.width, zIndex: 9999 }}
          className="block rounded-md border border-border bg-popover p-2 text-[11px]
          font-sans font-normal text-popover-foreground shadow-lg"
          role="dialog" data-testid="completeness-missing">
          <span className="mb-1 block font-medium">还差这几项:</span>
          <span className="block leading-5 text-muted-foreground">
            {missing!.slice(0, 8).map(getMissingLabel).join(' · ')}
            {missing!.length > 8 ? ` …等 ${missing!.length} 项` : ''}
          </span>
          {/* 🔴 出口:光说"缺什么"而不给去处,人还得自己找 */}
          <a className="mt-1.5 inline-block underline" href={`/my-clients/${brandId}`}
            data-testid="completeness-goto">去补资料 →</a>
        </span>,
        document.body,
      )}
    </span>
  );
}
