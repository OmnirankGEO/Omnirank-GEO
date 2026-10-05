/**
 * ConfidenceBadge — M1c A.6 · AI 推断置信度徽章
 *
 * CTO-15.9 2026-04-25 · PRD M1c §M3:
 *   suggester confidence "强/中/弱" → UI 展示 + 提示代理"建议核对"
 *
 * 用法:
 *   <ConfidenceBadge confidence="强" />  // 绿
 *   <ConfidenceBadge confidence="中" />  // 黄
 *   <ConfidenceBadge confidence="弱" />  // 橙红 + "建议核对"
 *   <ConfidenceBadge confidence={null} />  // 不渲染
 */
import { Sparkles, AlertCircle, ShieldCheck } from 'lucide-react';
import { cn } from '@/lib/utils';

interface Props {
  confidence?: '强' | '中' | '弱' | string | null;
  /** 字段名 · 用于 title 提示 */
  fieldLabel?: string;
  className?: string;
}

const CONFIDENCE_META: Record<string, { color: string; icon: React.ReactNode; label: string; hint: string }> = {
  强: {
    color: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/40',
    icon: <ShieldCheck className="h-3 w-3" />,
    label: '高置信',
    hint: 'AI 强信号推断 · 一般可直接采用',
  },
  中: {
    color: 'bg-amber-500/10 text-amber-600 border-amber-500/40',
    icon: <Sparkles className="h-3 w-3" />,
    label: '中置信',
    hint: 'AI 中等推断 · 建议代理快速扫视',
  },
  弱: {
    color: 'bg-rose-500/10 text-rose-500 border-rose-500/40',
    icon: <AlertCircle className="h-3 w-3" />,
    label: '低置信',
    hint: 'AI 弱信号 · 建议代理核对后保存',
  },
};

export function ConfidenceBadge({ confidence, fieldLabel, className }: Props) {
  if (!confidence) return null;
  const meta = CONFIDENCE_META[confidence];
  if (!meta) return null;
  const title = fieldLabel ? `${fieldLabel} · ${meta.hint}` : meta.hint;
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] border font-medium',
        meta.color,
        className,
      )}
    >
      {meta.icon}
      {meta.label}
    </span>
  );
}

export default ConfidenceBadge;
