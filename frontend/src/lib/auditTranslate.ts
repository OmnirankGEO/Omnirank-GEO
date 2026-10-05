import { agentLevelLabel, bonusRateLabel, tierLabel } from './channelTierTerminology';

const TIER_FIELDS = new Set([
  'channel_tier',
  'effective_tier',
  'tier_override',
  'natural_tier',
  'from_tier',
  'to_tier',
  'old_tier',
  'new_tier',
]);

const FIELD_LABELS: Record<string, string> = {
  agent_level: '代理身份',
  from_agent_level: '原代理身份',
  to_agent_level: '新代理身份',
  channel_tier: '渠道等级',
  effective_tier: '当前渠道等级',
  tier_override: '人工指定等级',
  from_tier: '原渠道等级',
  to_tier: '新渠道等级',
  bonus_rate: '配货比例',
  bonus_rate_used: '本次配货比例',
  reason: '原因',
  note: '备注',
  summary: '摘要',
  updated_by: '操作人',
  rolling_12m_yuan: '近 12 个月进货额',
  paid_points: '充值算力',
  bonus_points: '赠送算力',
  commission_points: '佣金算力',
  frozen_points: '冻结算力',
};

function parseSnapshot(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return '';
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function labelFor(key: string): string {
  return FIELD_LABELS[key] || key.replace(/_/g, ' ');
}

function formatValue(key: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (key.includes('agent_level')) return agentLevelLabel(value as number | string);
  if (key.includes('bonus_rate')) return bonusRateLabel(value as number | string);
  if (TIER_FIELDS.has(key)) return tierLabel(String(value));
  if (key.endsWith('_yuan') && typeof value !== 'object') return `${Number(value || 0).toLocaleString()} 元`;
  if (key.endsWith('_points') && typeof value !== 'object') return `${Number(value || 0).toLocaleString()} 算力`;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map((item) => String(item)).join('、') || '—';
  if (typeof value === 'object') return humanizeAuditSnapshot(value);
  return String(value);
}

export function humanizeAuditSnapshot(value: unknown): string {
  const parsed = parseSnapshot(value);
  if (parsed === null || parsed === undefined || parsed === '') return '—';
  if (typeof parsed !== 'object') return String(parsed);
  if (Array.isArray(parsed)) return parsed.map((item) => humanizeAuditSnapshot(item)).join('；') || '—';
  const obj = parsed as Record<string, unknown>;
  const lines = Object.entries(obj)
    .filter(([, v]) => v !== undefined)
    .map(([key, val]) => `${labelFor(key)}: ${formatValue(key, val)}`);
  return lines.join('；') || '—';
}

export function formatRawAuditSnapshot(value: unknown): string {
  const parsed = parseSnapshot(value);
  if (parsed === null || parsed === undefined || parsed === '') return '—';
  if (typeof parsed === 'string') return parsed;
  try {
    return JSON.stringify(parsed, null, 2);
  } catch {
    return String(parsed);
  }
}
