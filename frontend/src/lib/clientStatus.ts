export type ClientStatus = 'active' | 'undecided' | 'won' | 'archived';

export const CLIENT_STATUS_OPTIONS: Array<{
  value: ClientStatus;
  label: string;
  shortLabel: string;
  description: string;
  badgeClass: string;
}> = [
  {
    value: 'active',
    label: '跟进中',
    shortLabel: '跟进',
    description: '正在服务或继续跟进的客户',
    badgeClass: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/25',
  },
  {
    value: 'undecided',
    label: '未确定',
    shortLabel: '未定',
    description: '还没确认成交，先放在观察区',
    badgeClass: 'bg-amber-500/15 text-amber-300 border-amber-500/25',
  },
  {
    value: 'won',
    label: '已成交',
    shortLabel: '成交',
    description: '已经成交或确认合作',
    badgeClass: 'bg-sky-500/15 text-sky-300 border-sky-500/25',
  },
  {
    value: 'archived',
    label: '已归档',
    shortLabel: '归档',
    description: '不再出现在日常客户选择里',
    badgeClass: 'bg-muted text-muted-foreground border-border',
  },
];

const CLIENT_STATUS_ALIAS: Record<string, ClientStatus> = {
  active: 'active',
  draft: 'active',
  follow: 'active',
  following: 'active',
  open: 'active',
  pending: 'undecided',
  undecided: 'undecided',
  unknown: 'undecided',
  confirmed: 'won',
  closed: 'won',
  closed_won: 'won',
  won: 'won',
  archive: 'archived',
  archived: 'archived',
};

export function normalizeClientStatus(status?: string | null): ClientStatus {
  const key = String(status || '').trim().toLowerCase();
  if (!key) return 'active';
  return CLIENT_STATUS_ALIAS[key] || 'active';
}

export function getClientStatusMeta(status?: string | null) {
  const normalized = normalizeClientStatus(status);
  return CLIENT_STATUS_OPTIONS.find((item) => item.value === normalized) || CLIENT_STATUS_OPTIONS[0];
}
