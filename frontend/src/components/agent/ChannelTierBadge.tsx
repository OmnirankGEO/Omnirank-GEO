import { Crown, ShieldCheck, Sparkles } from 'lucide-react';
import { cn } from '@/lib/utils';
import { founderLabel, tierShortLabel, type ChannelTierKey } from '@/lib/channelTierTerminology';

const TIER_STYLES: Record<ChannelTierKey, string> = {
  none: 'border-slate-700 bg-slate-900/70 text-slate-300',
  certified: 'border-emerald-500/35 bg-emerald-500/10 text-emerald-200',
  preferred: 'border-sky-500/35 bg-sky-500/10 text-sky-200',
  strategic: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
};

function normalizeTier(value?: string | null): ChannelTierKey {
  const key = String(value || 'none').trim().toLowerCase();
  if (key === 'certified' || key === 'preferred' || key === 'strategic') return key;
  return 'none';
}

interface ChannelTierBadgeProps {
  tier?: string | null;
  enabled?: boolean;
  compact?: boolean;
  founder?: boolean;
  founderRank?: number | null;
  className?: string;
}

export function ChannelTierBadge({
  tier,
  enabled = true,
  compact = false,
  founder = false,
  founderRank = null,
  className,
}: ChannelTierBadgeProps) {
  const normalized = enabled ? normalizeTier(tier) : 'none';
  const label = enabled ? tierShortLabel(normalized) : '等级未启用';
  const founderText = founderLabel(founder, founderRank);
  const Icon = founder ? Crown : normalized === 'none' ? ShieldCheck : Sparkles;

  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1 rounded-full border font-medium',
        compact ? 'px-1.5 py-0.5 text-[10px]' : 'px-2.5 py-1 text-xs',
        TIER_STYLES[normalized],
        founder && 'ring-1 ring-amber-300/35',
        className,
      )}
      title={[label, founderText].filter(Boolean).join(' · ') || label}
    >
      <Icon className={cn(compact ? 'h-3 w-3' : 'h-3.5 w-3.5')} />
      <span className="truncate">{label}</span>
      {founderText && !compact && <span className="text-amber-200/90">{founderText}</span>}
    </span>
  );
}
