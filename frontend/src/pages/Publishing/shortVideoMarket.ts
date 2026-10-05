/** One read-only directory contract for video and image-note publishers. Prices are server points. */
export interface MarketAccount {
  id: number;
  media_name: string;
  platform: string;
  price_points: number | null;
  location?: string;
  industry?: string;
  occupation?: string;
  fans_num?: number;
  fans_num_text?: string;
  avg_likes_num?: number;
  avg_publish_time?: string;
  account_auth?: string;
  authority_media?: number;
  can_modify?: number;
  remark?: string;
}
export interface MarketScope { platform?: string; imageNote?: boolean }
export interface MarketFilters {
  page: number; search: string; platform: string; location: string; industry: string;
  pointsMin: string; pointsMax: string; fansMin: string; fansMax: string;
  accountAuth: string; authorityMedia: string; canModify: string; sort: string;
}
export interface MarketViewState { filters: MarketFilters; draft: MarketFilters }
export interface MarketRangePreset { value: string; label: string; min: string; max: string }
/** Closed integer ranges. These are account-search shortcuts, not packages or prices. */
export const MARKET_POINT_PRESETS: readonly MarketRangePreset[] = [
  { value: 'low', label: '6,500 及以下', min: '', max: '6500' },
  { value: 'medium', label: '6,501–13,000', min: '6501', max: '13000' },
  { value: 'high', label: '13,001–26,000', min: '13001', max: '26000' },
  { value: 'higher', label: '超过 26,000', min: '26001', max: '' },
];
export const MARKET_FAN_PRESETS: readonly MarketRangePreset[] = [
  { value: 'small', label: '1,000 及以下', min: '', max: '1000' },
  { value: 'growing', label: '1,001–5,000', min: '1001', max: '5000' },
  { value: 'medium', label: '5,001–1万', min: '5001', max: '10000' },
  { value: 'large', label: '10,001–5万', min: '10001', max: '50000' },
  { value: 'larger', label: '50,001–10万', min: '50001', max: '100000' },
  { value: 'largest', label: '超过 10万', min: '100001', max: '' },
];
export const MARKET_PAGE_SIZE = 20;
export function readMarketPage(value: unknown): { media: MarketAccount[]; total: number } {
  const page = value as { media?: unknown; total?: unknown } | null;
  if (!page || !Array.isArray(page.media) || !Number.isSafeInteger(page.total) || Number(page.total) < 0
    || page.media.some(a => !a || !Number.isSafeInteger(a.id) || a.id <= 0 || typeof a.media_name !== 'string')) {
    throw new Error('账号目录数据不完整，请重试。');
  }
  return { media: page.media, total: Number(page.total) };
}
export const emptyMarketFilters = (): MarketFilters => ({
  page: 1, search: '', platform: '', location: '', industry: '', pointsMin: '', pointsMax: '',
  fansMin: '', fansMax: '', accountAuth: '', authorityMedia: '', canModify: '', sort: 'id:asc',
});
export function changeMarketFilters(state: MarketFilters, changes: Partial<MarketFilters>): MarketFilters {
  return { ...state, ...changes, page: changes.page ?? 1 };
}
/** Apply one user action without reviving an old range when a later search is submitted. */
export function changeMarketView(state: MarketViewState, changes: Partial<MarketFilters>): MarketViewState {
  const filters = changeMarketFilters(state.filters, changes);
  return { filters, draft: { ...state.draft, ...changes, page: filters.page } };
}
export function marketRangeLabel(min: string, max: string): string {
  const number = (value: string) => Number(value).toLocaleString('zh-CN');
  return min && max ? `${number(min)}–${number(max)}` : min ? `${number(min)} 及以上` : `${number(max)} 及以下`;
}
export function marketActiveFilters(filters: MarketFilters, scope: MarketScope = {}) {
  const conditions: { key: string; label: string; clear: Partial<MarketFilters> }[] = [];
  for (const [field, label] of [['platform', '平台'], ['industry', '行业'], ['location', '地区'], ['accountAuth', '认证'], ['search', '名称']] as const) {
    if (field === 'platform' && scope.platform) continue;
    if (filters[field]) conditions.push({ key: field, label: `${label}：${filters[field]}`, clear: { [field]: '' } });
  }
  for (const [min, max, key, label] of [['pointsMin', 'pointsMax', 'points', '算力'], ['fansMin', 'fansMax', 'fans', '粉丝']] as const) {
    if (filters[min] || filters[max]) conditions.push({ key, label: `${label}：${marketRangeLabel(filters[min], filters[max])}`, clear: { [min]: '', [max]: '' } });
  }
  if (filters.authorityMedia !== '') conditions.push({ key: 'authorityMedia', label: filters.authorityMedia === '1' ? '官方媒体' : '非官方媒体', clear: { authorityMedia: '' } });
  if (filters.canModify !== '') conditions.push({ key: 'canModify', label: filters.canModify === '1' ? '可改稿' : '不可改稿', clear: { canModify: '' } });
  return conditions;
}
export function marketRangeError(state: MarketFilters): string {
  for (const [min, max, label] of [[state.pointsMin, state.pointsMax, '算力'], [state.fansMin, state.fansMax, '粉丝数']]) {
    if ([min, max].some(v => v !== '' && !/^\d+$/.test(v))) return `${label}请填写非负整数，或清空表示不限。`;
    if (min && max && Number(min) > Number(max)) return `${label}最低值不能大于最高值，请调整范围。`;
  }
  return '';
}
export function marketQuery(state: MarketFilters, scope: MarketScope = {}, facets = false): string {
  const q = new URLSearchParams();
  const fields = {
    search: state.search.trim(), platform: scope.platform || state.platform, location: state.location,
    account_auth: state.accountAuth, authority_media: state.authorityMedia, can_modify: state.canModify,
    points_min: state.pointsMin, points_max: state.pointsMax, fans_min: state.fansMin, fans_max: state.fansMax,
  };
  for (const [key, value] of Object.entries(fields)) if (value !== '') q.set(key, value);
  if (scope.imageNote) q.set('can_tuwen', '1');
  if (!facets) {
    if (state.industry) q.set('industry', state.industry);
    const [by, dir] = state.sort.split(':');
    q.set('sort_by', by); q.set('sort_dir', dir);
    q.set('page', String(state.page)); q.set('limit', String(MARKET_PAGE_SIZE));
  }
  return q.toString();
}
/** Selection is caller-owned, never reconstructed from a paginated response. */
export function chooseMarketAccount(selected: readonly MarketAccount[], account: MarketAccount,
  multiple: boolean, locked = false, excluded: readonly number[] = []): MarketAccount[] {
  if (locked || excluded.includes(account.id)) return [...selected];
  if (selected.some(a => a.id === account.id)) return selected.filter(a => a.id !== account.id);
  return multiple ? [...selected, account] : [account];
}
export function marketFans(account: MarketAccount): string {
  if (account.fans_num_text) return account.fans_num_text;
  return typeof account.fans_num === 'number' && account.fans_num >= 0 ? account.fans_num.toLocaleString() : '暂无数据';
}
export function marketPrice(account: MarketAccount): string {
  return typeof account.price_points === 'number' && Number.isFinite(account.price_points) && account.price_points > 0
    ? `${account.price_points.toLocaleString()} 算力` : '待询价';
}
export function receiptAccountName(item: { media_id?: number | null; media_name?: string }, hint: MarketAccount | null): string {
  return item.media_name?.trim() || (hint?.id === item.media_id ? hint?.media_name : '') || '发布账号资料暂未读取';
}
