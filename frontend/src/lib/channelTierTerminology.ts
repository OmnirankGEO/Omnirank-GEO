export type ChannelTierKey = 'none' | 'certified' | 'preferred' | 'strategic';

export const CHANNEL_TIER_LABELS: Record<ChannelTierKey, string> = {
  none: '未评级',
  certified: '认证服务商',
  preferred: '优选服务商',
  strategic: '战略服务商',
};

export const CHANNEL_TIER_SHORT_LABELS: Record<ChannelTierKey, string> = {
  none: '未评级',
  certified: '认证',
  preferred: '优选',
  strategic: '战略',
};

export const AGENT_LEVEL_LABELS: Record<number, string> = {
  0: '普通用户',
  1: '服务方',
  2: '服务方',
};

function normalizeTier(value?: string | null): ChannelTierKey {
  const key = String(value || 'none').trim().toLowerCase();
  if (key === 'certified' || key === 'preferred' || key === 'strategic') return key;
  return 'none';
}

export function tierLabel(value?: string | null): string {
  return CHANNEL_TIER_LABELS[normalizeTier(value)];
}

export function tierShortLabel(value?: string | null): string {
  return CHANNEL_TIER_SHORT_LABELS[normalizeTier(value)];
}

export function agentLevelLabel(level?: number | string | null): string {
  const n = Number(level ?? 0);
  return AGENT_LEVEL_LABELS[n] || '普通用户';
}

export function bonusRateLabel(rate?: number | string | null): string {
  const n = Number(rate ?? 0);
  if (!Number.isFinite(n) || n <= 0) return '无额外配货';
  return `+${Math.round(n * 100)}% 配货`;
}

export function founderLabel(isFounder?: boolean, rank?: number | string | null): string {
  if (!isFounder) return '';
  const n = Number(rank ?? 0);
  return n > 0 ? `创始席 #${n}` : '创始席';
}
