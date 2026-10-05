/**
 * 全部规定状态视图（copy 合同 required_states）：
 * loading / empty / error / 403 / 409 / 423 / 503 / 样本不足 / 模型升级 / 洞察失败。
 * 每个 disabled/错误态都给人能理解的原因；pending 尺寸稳定防双击。
 */

import { type ReactNode } from 'react';
import {
  Loader2,
  Inbox,
  Lock,
  RefreshCw,
  AlertTriangle,
  Hourglass,
  ServerCrash,
  Settings2,
} from 'lucide-react';
import { PAGE } from '../copy';
import type { ObservationErrorCode } from '../types';

function Frame({
  icon,
  title,
  desc,
  action,
  tone = 'neutral',
  compact = false,
}: {
  icon: ReactNode;
  title: string;
  desc?: string;
  action?: ReactNode;
  tone?: 'neutral' | 'bad' | 'warn' | 'info';
  compact?: boolean;
}) {
  const border =
    tone === 'bad'
      ? 'border-red-500/30'
      : tone === 'warn'
        ? 'border-amber-500/30'
        : tone === 'info'
          ? 'border-blue-500/30'
          : 'border-border';
  return (
    <div
      className={`flex flex-col items-center justify-center rounded-lg border ${border} bg-card px-6 text-center ${compact ? 'py-8' : 'py-14'}`}
      role="status"
    >
      <div className="mb-3">{icon}</div>
      <p className="text-sm font-medium text-foreground">{title}</p>
      {desc ? <p className="mt-1.5 max-w-md text-xs leading-relaxed text-muted-foreground">{desc}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

function RetryButton({ onRetry }: { onRetry: () => void }) {
  return (
    <button
      type="button"
      onClick={onRetry}
      className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 py-2 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
    >
      <RefreshCw className="h-3.5 w-3.5" />
      {PAGE.states.retry}
    </button>
  );
}

export function LoadingBlock({ compact = false }: { compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      icon={<Loader2 className="h-7 w-7 animate-spin text-brand" aria-hidden />}
      title={PAGE.states.loading}
    />
  );
}

/** 骨架行（列表/表格加载态，尺寸稳定不跳动）。 */
export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-lg bg-muted" />
      ))}
    </div>
  );
}

export function EmptyBlock({ title, desc, compact }: { title?: string; desc?: string; compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      icon={<Inbox className="h-8 w-8 text-muted-foreground/40" aria-hidden />}
      title={title || PAGE.states.empty}
      desc={desc}
    />
  );
}

export function InsufficientBlock({ compact }: { compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      tone="warn"
      icon={<Hourglass className="h-8 w-8 text-amber-500" aria-hidden />}
      title={PAGE.states.insufficient}
      desc={PAGE.states.insufficientHint}
    />
  );
}

/**
 * 数据口径断点：内联横幅。标题中性（"数据口径已更新"），具体说明**来自后端字段**
 * （summary.stability_explanation / trend.comparison_note），不永久硬编码"模型升级"。
 * 仅在后端确认口径断点（stability='shifted' 或 trend 有 comparison_note）时由调用方渲染。
 */
export function ModelShiftBanner({ explanation }: { explanation?: string | null }) {
  return (
    <div
      className="flex items-start gap-2 rounded-lg border border-blue-500/30 bg-blue-500/5 px-3.5 py-2.5"
      role="note"
    >
      <Settings2 className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" aria-hidden />
      <p className="text-xs leading-relaxed text-foreground">
        <span className="font-medium">{PAGE.states.dataCaliberUpdated}</span>
        {explanation ? <span className="text-muted-foreground">：{explanation}</span> : null}
      </p>
    </div>
  );
}

export function ForbiddenBlock({ compact }: { compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      tone="bad"
      icon={<Lock className="h-8 w-8 text-red-500" aria-hidden />}
      title={PAGE.states.forbidden}
    />
  );
}

export function EnvOverrideBlock({ compact }: { compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      tone="warn"
      icon={<Settings2 className="h-8 w-8 text-amber-500" aria-hidden />}
      title={PAGE.states.envOverride}
    />
  );
}

export function ConflictBlock({ onRetry, compact }: { onRetry?: () => void; compact?: boolean }) {
  return (
    <Frame
      compact={compact}
      tone="warn"
      icon={<AlertTriangle className="h-8 w-8 text-amber-500" aria-hidden />}
      title={PAGE.states.conflict}
      action={onRetry ? <RetryButton onRetry={onRetry} /> : undefined}
    />
  );
}

export function ErrorBlock({
  onRetry,
  message,
  unavailable = false,
  compact,
}: {
  onRetry?: () => void;
  message?: string;
  unavailable?: boolean;
  compact?: boolean;
}) {
  return (
    <Frame
      compact={compact}
      tone="bad"
      icon={<ServerCrash className="h-8 w-8 text-red-500" aria-hidden />}
      title={unavailable ? PAGE.states.unavailable : PAGE.states.error}
      desc={message}
      action={onRetry ? <RetryButton onRetry={onRetry} /> : undefined}
    />
  );
}

/**
 * 统一分发：把 ResourceState 的 (status,errorCode) 映射到正确状态视图。
 * 返回 null 表示"成功且有数据"，由调用方渲染真实内容。
 */
export function ResourceStateView({
  status,
  errorCode,
  httpStatus,
  message,
  isEmpty,
  onRetry,
  compact,
  emptyTitle,
  emptyDesc,
  loadingSkeleton,
}: {
  status: 'idle' | 'loading' | 'success' | 'error';
  errorCode?: ObservationErrorCode | null;
  httpStatus?: number | null;
  message?: string | null;
  isEmpty?: boolean;
  onRetry?: () => void;
  compact?: boolean;
  emptyTitle?: string;
  emptyDesc?: string;
  loadingSkeleton?: ReactNode;
}): ReactNode | null {
  if (status === 'idle' || status === 'loading') {
    return loadingSkeleton ?? <LoadingBlock compact={compact} />;
  }
  if (status === 'error') {
    switch (errorCode) {
      case 'FORBIDDEN':
        return <ForbiddenBlock compact={compact} />;
      case 'VERSION_CONFLICT':
        return <ConflictBlock onRetry={onRetry} compact={compact} />;
      case 'ENV_OVERRIDE_ACTIVE':
        return <EnvOverrideBlock compact={compact} />;
      case 'INSUFFICIENT_SAMPLES':
        return <InsufficientBlock compact={compact} />;
      case 'OBSERVATION_UNAVAILABLE':
        return <ErrorBlock onRetry={onRetry} unavailable compact={compact} />;
      default:
        // 403/409/423 也可能只由 httpStatus 表达
        if (httpStatus === 403) return <ForbiddenBlock compact={compact} />;
        if (httpStatus === 409) return <ConflictBlock onRetry={onRetry} compact={compact} />;
        if (httpStatus === 423) return <EnvOverrideBlock compact={compact} />;
        if (httpStatus === 503) return <ErrorBlock onRetry={onRetry} unavailable compact={compact} />;
        return <ErrorBlock onRetry={onRetry} message={message || undefined} compact={compact} />;
    }
  }
  if (status === 'success' && isEmpty) {
    return <EmptyBlock title={emptyTitle} desc={emptyDesc} compact={compact} />;
  }
  return null;
}
