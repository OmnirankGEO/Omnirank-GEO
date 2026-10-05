/** Shared live account marketplace. This component reads the directory; it never publishes or charges. */
import { useEffect, useId, useState, type Dispatch, type SetStateAction } from 'react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { publicationTime } from './publicationTime';
import {
  MARKET_PAGE_SIZE, MARKET_POINT_PRESETS, MARKET_FAN_PRESETS, changeMarketView, chooseMarketAccount, emptyMarketFilters,
  marketActiveFilters, marketFans, marketPrice, marketQuery, marketRangeError, marketRangeLabel, readMarketPage,
  type MarketAccount, type MarketFilters, type MarketScope, type MarketViewState, type MarketRangePreset,
} from './shortVideoMarket';

interface Facets { platforms: string[]; locations: string[]; industries: { key: string; count: number }[]; account_auths: string[] }
export type { MarketViewState } from './shortVideoMarket';
export function newMarketViewState(): MarketViewState {
  return { filters: emptyMarketFilters(), draft: emptyMarketFilters() };
}
export interface MarketViewProps {
  viewState?: MarketViewState;
  setViewState?: Dispatch<SetStateAction<MarketViewState>>;
}
interface Props extends MarketViewProps {
  scope?: MarketScope;
  selected: readonly MarketAccount[];
  onChange: (accounts: MarketAccount[]) => void;
  multiple?: boolean;
  locked?: boolean;
  excludedIds?: readonly number[];
}
const CONTROL = 'min-h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 py-2 text-sm';
const blankFacets = (): Facets => ({ platforms: [], locations: [], industries: [], account_auths: [] });

interface FilterChoice { value: string; label: string; count?: number }
/** Shared presentational row: visible categories are real facets, not guessed account names. */
export function MarketFilterRow({ label, choices, value, onChange, expanded = false, onExpandedChange }: {
  label: string; choices: readonly FilterChoice[]; value: string; onChange: (value: string) => void;
  expanded?: boolean; onExpandedChange?: (expanded: boolean) => void;
}) {
  const current = choices.find(choice => choice.value === value);
  const visible = expanded || !onExpandedChange ? [...choices] : choices.slice(0, 8);
  if (value && !visible.some(choice => choice.value === value)) visible.push(current || { value, label: value });
  return <div role="group" aria-label={`${label}筛选`} className="flex min-w-0 flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
    <span className="shrink-0 text-xs leading-6 text-muted-foreground sm:w-20 sm:pt-1">{label}</span>
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1 gap-y-1">
      {[{ value: '', label: '不限' }, ...visible].map(choice => <button key={choice.value} type="button"
        aria-label={`${label}：${choice.label}`} aria-pressed={value === choice.value}
        onClick={() => onChange(value === choice.value ? '' : choice.value)}
        className={`inline-flex min-h-11 max-w-full items-center gap-1 rounded-md px-2.5 py-1 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-8 ${value === choice.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground'}`}>
        <span className="break-words">{choice.label}</span>
        {'count' in choice && Number.isSafeInteger(choice.count) && Number(choice.count) >= 0 && <span className="text-xs opacity-70">{choice.count}</span>}
      </button>)}
      {onExpandedChange && choices.length > 8 && <button type="button" aria-expanded={expanded} aria-label={`${expanded ? '收起' : '展开全部'}${label}`}
        onClick={() => onExpandedChange(!expanded)} className="min-h-11 rounded-md px-2 text-xs text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-8">
        {expanded ? '收起' : '展开全部'}</button>}
    </div>
  </div>;
}

export function AccountMarketResults({ rows, selected, onChange, multiple = false, locked = false,
  excludedIds = [] }: Omit<Props, 'scope'> & { rows: MarketAccount[] }) {
  const name = useId();
  return <div className="max-h-[28rem] max-w-full overflow-auto rounded-lg border border-border" data-testid="account-market-table-scroll">
    <table className="w-full min-w-[1000px] text-left text-sm" data-testid="account-market-table">
      <caption className="sr-only">发布账号目录，{multiple ? '可选择多个账号' : '一条图文选择一个账号'}</caption>
      <thead className="sticky top-0 z-10 bg-card text-xs text-muted-foreground"><tr>
        {['选择', '账号名称', '平台 / 行业', '地区', '粉丝', '平均点赞', '价格', '出稿时间', '认证 / 官媒', '改稿', '备注'].map(h => <th key={h} scope="col" className="whitespace-nowrap px-3 py-3 font-normal">{h}</th>)}
      </tr></thead>
      <tbody>{rows.map(a => {
        const on = selected.some(s => s.id === a.id);
        const excluded = excludedIds.includes(a.id);
        return <tr key={a.id} className={`border-t border-border ${on ? 'bg-primary/10' : 'hover:bg-muted/30'}`}>
          <td className="px-3"><label className="flex min-h-11 min-w-11 cursor-pointer items-center justify-center">
            <input id={`${name}-${a.id}`} type={multiple ? 'checkbox' : 'radio'} name={name} checked={on}
              disabled={locked || excluded} aria-label={`选择 ${a.media_name}`} data-testid={`inp-account-${a.id}`}
              onChange={() => onChange(chooseMarketAccount(selected, a, multiple, locked, excludedIds))}
              className="size-4 accent-primary" />
          </label></td>
          <td className="min-w-40 px-3 py-3"><label htmlFor={`${name}-${a.id}`} className="flex min-h-11 cursor-pointer items-center font-medium">{a.media_name}</label>
            {excluded && <span className="text-xs text-amber-500">上次失败，请换其他账号</span>}</td>
          <td className="px-3 py-3"><div>{a.platform || '暂无数据'}</div><div className="mt-1 text-xs text-muted-foreground">{a.industry || a.occupation || '行业未提供'}</div></td>
          <td className="whitespace-nowrap px-3 py-3">{a.location || '暂无数据'}</td>
          <td className="whitespace-nowrap px-3 py-3">{marketFans(a)}</td>
          <td className="whitespace-nowrap px-3 py-3">{typeof a.avg_likes_num === 'number' ? a.avg_likes_num.toLocaleString() : '暂无数据'}</td>
          <td className="whitespace-nowrap px-3 py-3 font-medium text-primary">{marketPrice(a)}</td>
          <td className="whitespace-nowrap px-3 py-3">{publicationTime(a.avg_publish_time)}</td>
          <td className="px-3 py-3"><div>{a.account_auth || '未提供认证信息'}</div>{a.authority_media === 1 && <div className="mt-1 text-xs text-muted-foreground">官方媒体</div>}</td>
          <td className="whitespace-nowrap px-3 py-3">{a.can_modify === 1 ? '可改稿' : a.can_modify === 0 ? '不可改稿' : '未提供'}</td>
          <td className="max-w-64 px-3 py-3">{a.remark ? <details><summary className="flex min-h-11 cursor-pointer items-center">查看备注</summary><p className="whitespace-pre-wrap break-words pb-2">{a.remark}</p></details> : '暂无备注'}</td>
        </tr>;
      })}</tbody>
    </table>
  </div>;
}

export function ShortVideoAccountMarket({ scope = {}, selected, onChange, multiple = false,
  locked = false, excludedIds = [], viewState, setViewState }: Props) {
  const [localView, setLocalView] = useState(newMarketViewState);
  const { filters, draft } = viewState || localView;
  const updateView = setViewState || setLocalView;
  const setDraft = (next: SetStateAction<MarketFilters>) => updateView(prev => ({ ...prev,
    draft: typeof next === 'function' ? next(prev.draft) : next }));
  const [facets, setFacets] = useState(blankFacets);
  const [facetIdentity, setFacetIdentity] = useState('');
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({});
  const [rows, setRows] = useState<MarketAccount[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [facetError, setFacetError] = useState('');
  const [inputError, setInputError] = useState('');
  const [reload, setReload] = useState(0);
  const listQuery = marketQuery(filters, scope);
  const facetsQuery = marketQuery(filters, scope, true);
  const change = (changes: Partial<MarketFilters>) => { updateView(prev => changeMarketView(prev, changes)); setInputError(''); };
  const clear = () => { updateView(newMarketViewState()); setInputError(''); };

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setTotal(null);
    void authFetch(`/api/meijiehezi/short-video?${listQuery}`, { signal: controller.signal })
      .then(async r => { const d = await r.json(); if (!r.ok || d.status !== 'success') throw new Error('directory'); return readMarketPage(d); })
      .then(d => {
        if (controller.signal.aborted) return;
        setRows(d.media || []); setTotal(Number(d.total)); setLoading(false);
        const pages = Math.max(1, Math.ceil(Number(d.total) / MARKET_PAGE_SIZE));
        if (filters.page > pages) updateView(prev => changeMarketView(prev, { page: pages }));
      }).catch(() => { if (!controller.signal.aborted) { setRows([]); setError('账号目录暂时没读到，重试即可；已选账号会保留。'); setLoading(false); } });
    return () => controller.abort();
  }, [listQuery, reload]);
  useEffect(() => {
    const controller = new AbortController();
    setFacetError('');
    void authFetch(`/api/meijiehezi/short-video/filters?${facetsQuery}`, { signal: controller.signal })
      .then(async r => { const d = await r.json(); if (!r.ok || d.status !== 'success') throw new Error('facets'); return d; })
      .then(d => { if (!controller.signal.aborted) {
        setFacets({ platforms: d.platforms || [], locations: d.locations || [], industries: d.industries || [], account_auths: d.account_auths || [] });
        setFacetIdentity(facetsQuery);
      } })
      .catch(() => { if (!controller.signal.aborted) setFacetError('筛选选项暂未更新，可以重试；搜索和已选条件仍保留。'); });
    return () => controller.abort();
  }, [facetsQuery, reload]);

  const select = (label: string, field: keyof MarketFilters, options: { value: string; label: string }[], compact = false) => {
    const current = String(filters[field]);
    const visible = current && !options.some(o => o.value === current) ? [{ value: current, label: `${current}（当前条件）` }, ...options] : options;
    return <label className="block min-w-0 space-y-1.5 text-sm"><span className={compact ? 'sr-only' : 'text-muted-foreground'}>{label}</span>
      <select className={CONTROL} aria-label={label} value={current} onChange={e => change({ [field]: e.target.value })}>
        {visible.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select></label>;
  };
  const opts = (items: string[]) => [{ value: '', label: '不限' }, ...items.map(s => ({ value: s, label: s }))];
  const categories = (label: string, field: 'industry' | 'location' | 'platform', choices: FilterChoice[]) => <MarketFilterRow label={label}
    choices={choices} value={filters[field]} onChange={value => change({ [field]: value })} expanded={expandedRows[field] || false}
    onExpandedChange={expanded => setExpandedRows(prev => ({ ...prev, [field]: expanded }))} />;
  const presets = (label: string, min: 'pointsMin' | 'fansMin', max: 'pointsMax' | 'fansMax', options: readonly MarketRangePreset[]) => {
    const current = options.find(option => option.min === filters[min] && option.max === filters[max]);
    const custom = !current && !!(filters[min] || filters[max]);
    return <MarketFilterRow label={label} choices={[...options, ...(custom ? [{ value: 'custom', label: `自定义 ${marketRangeLabel(filters[min], filters[max])}` }] : [])]}
      value={current?.value || (custom ? 'custom' : '')} onChange={value => {
        const option = options.find(item => item.value === value);
        change({ [min]: option?.min || '', [max]: option?.max || '' });
      }} />;
  };
  const activeFilters = marketActiveFilters(filters, scope);
  const range = (label: string, min: 'pointsMin' | 'fansMin', max: 'pointsMax' | 'fansMax') => <fieldset className="min-w-0 space-y-1.5">
    <legend className="text-sm text-muted-foreground">{label}</legend>
    <div className="flex min-w-0 items-center gap-2">
      <input className={CONTROL} type="number" min="0" step="1" aria-label={`${label}最低`} placeholder="最低" value={draft[min]} onChange={e => setDraft(prev => ({ ...prev, [min]: e.target.value }))} />
      <span className="text-muted-foreground">—</span>
      <input className={CONTROL} type="number" min="0" step="1" aria-label={`${label}最高`} placeholder="最高" value={draft[max]} onChange={e => setDraft(prev => ({ ...prev, [max]: e.target.value }))} />
    </div></fieldset>;

  return <section className={scope.imageNote ? 'min-w-0 space-y-3' : 'min-w-0 space-y-4'} data-testid="short-video-account-market">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">{scope.imageNote ? '选择抖音发布账号' : '短视频账号市场'}</h3>
      <span className="text-xs text-muted-foreground">{multiple ? '可选择多个账号' : '这条图文选择一个发布账号'}</span></div>
    <div className={scope.imageNote ? 'space-y-2 border-y border-border py-3' : 'space-y-3 rounded-xl border border-border bg-muted/20 p-3 sm:p-4'}>
      <div className="min-w-0 space-y-1" data-testid="account-market-quick-filters">
        {!scope.platform && categories('平台', 'platform', facets.platforms.map(value => ({ value, label: value })))}
        {categories('行业', 'industry', facets.industries.map(item => ({ value: item.key, label: item.key, count: facetIdentity === facetsQuery ? item.count : undefined })))}
        {categories('地区', 'location', facets.locations.map(value => ({ value, label: value })))}
        {presets('价格（算力）', 'pointsMin', 'pointsMax', MARKET_POINT_PRESETS)}
        {presets('粉丝数', 'fansMin', 'fansMax', MARKET_FAN_PRESETS)}
      </div>
      {activeFilters.length > 0 && <div className="flex min-w-0 flex-wrap items-center gap-1 border-t border-border pt-2" aria-label="已选筛选条件">
        <span className="mr-1 text-xs text-muted-foreground">已选</span>
        {activeFilters.map(condition => <button key={condition.key} type="button" aria-label={`取消${condition.label}`}
          className="inline-flex min-h-11 max-w-full items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-2 text-xs text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-8"
          onClick={() => change(condition.clear)}><span className="break-words">{condition.label}</span><span aria-hidden="true">×</span></button>)}
        <Button type="button" variant="ghost" className="min-h-11 px-2 text-xs sm:min-h-8 sm:h-8" onClick={clear}>清空筛选</Button>
      </div>}
      <details className="border-t border-border"><summary className="flex min-h-11 cursor-pointer items-center text-xs text-muted-foreground hover:text-foreground">更多条件与自定义范围</summary>
        <form className="space-y-3 pb-3" onSubmit={e => {
          e.preventDefault(); const validation = marketRangeError(draft); setInputError(validation);
          if (!validation) change({ pointsMin: draft.pointsMin, pointsMax: draft.pointsMax, fansMin: draft.fansMin, fansMax: draft.fansMax });
        }}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {range('价格（算力）', 'pointsMin', 'pointsMax')}
            {range('粉丝数', 'fansMin', 'fansMax')}
            {select('账号认证', 'accountAuth', opts(facets.account_auths))}
            {select('官方媒体', 'authorityMedia', [{ value: '', label: '不限' }, { value: '1', label: '官方媒体' }, { value: '0', label: '非官方媒体' }])}
            {select('内容修改', 'canModify', [{ value: '', label: '不限' }, { value: '1', label: '可改稿' }, { value: '0', label: '不可改稿' }])}
          </div>
          <Button type="submit" variant="outline" className="min-h-11">应用自定义范围</Button>
        </form>
      </details>
      <form className="flex min-w-0 flex-wrap items-center gap-2" onSubmit={e => { e.preventDefault(); change({ search: draft.search.trim() }); }}>
        <label className="min-w-0 flex-1 text-sm"><span className="sr-only">账号名称（选填）</span>
          <input className={`${CONTROL} sm:min-h-9`} aria-label="账号名称（选填）" placeholder="知道账号名称？在这里搜索（选填）"
            data-testid="inp-account-search" value={draft.search} onChange={e => setDraft(prev => ({ ...prev, search: e.target.value }))} /></label>
        <Button type="submit" variant="outline" className="min-h-11 sm:min-h-9 sm:h-9" data-testid="inp-account-search-btn">搜索账号</Button>
        {activeFilters.length === 0 && <Button type="button" variant="ghost" className="min-h-11 px-2 text-xs sm:min-h-9 sm:h-9" onClick={clear}>清空筛选</Button>}
      </form>
      {inputError && <p role="alert" className="text-sm text-amber-500">{inputError}</p>}
      {facetError && <p role="status" className="text-sm text-amber-500">{facetError}<button type="button" className="ml-2 min-h-11 underline" onClick={() => setReload(v => v + 1)}>重试筛选选项</button></p>}
    </div>
    <div className="flex flex-wrap items-end justify-between gap-3"><p className="text-sm" data-testid="inp-account-total">
      {scope.imageNote ? '可发布图文的抖音账号' : '符合条件的账号'}{total === null ? (error ? ' · 暂未读取到数量' : ' · 正在读取') : ` · ${total.toLocaleString()} 个`}</p>
      <div className="flex items-end gap-2">{select('排序', 'sort', [
        { value: 'id:asc', label: '默认排序' }, { value: 'price_points:asc', label: '价格从低到高' }, { value: 'price_points:desc', label: '价格从高到低' },
        { value: 'fans_num:desc', label: '粉丝从多到少' }, { value: 'avg_likes_num:desc', label: '平均点赞从多到少' },
      ], scope.imageNote)}<Button type="button" variant="outline" className="min-h-11" onClick={() => setReload(v => v + 1)}>刷新</Button></div>
    </div>
    {selected.length > 0 && <div className="space-y-2 rounded-lg border border-primary/30 bg-primary/5 p-3" data-testid="account-market-selection">
      <p className="text-sm font-medium">{locked ? '本次提交账号' : `已选 ${selected.length} 个账号`}<span className="ml-2 text-xs font-normal text-muted-foreground">翻页或筛选不会清除</span></p>
      {selected.map(a => <div key={a.id} className="flex flex-wrap items-center justify-between gap-2 text-sm"><span>{a.media_name} · {a.platform || '平台未提供'} · {marketPrice(a)}</span>
        {!locked && <Button type="button" variant="ghost" className="min-h-11" onClick={() => onChange(selected.filter(s => s.id !== a.id))}>移除 {a.media_name}</Button>}</div>)}
    </div>}
    {loading ? <p role="status" className="py-8 text-center text-sm text-muted-foreground">正在读取账号目录…</p> : error ? <div role="status" className="space-y-2 rounded-lg border p-4 text-sm"><p>{error}</p><Button type="button" variant="outline" className="min-h-11" onClick={() => setReload(v => v + 1)}>重试账号目录</Button></div>
      : rows.length === 0 ? <div className="space-y-2 rounded-lg border p-6 text-sm"><p>当前条件没有匹配账号。取消上方某个条件，或清空筛选看看其他账号。</p><Button type="button" variant="outline" className="min-h-11" onClick={clear}>清空筛选</Button></div>
        : <AccountMarketResults rows={rows} selected={selected} onChange={onChange} multiple={multiple} locked={locked} excludedIds={excludedIds} />}
    {total !== null && total > 0 && <nav aria-label="账号分页" className="flex flex-wrap items-center justify-between gap-2" data-testid="inp-account-pager">
      <Button type="button" variant="outline" className="min-h-11" disabled={loading || filters.page <= 1} onClick={() => change({ page: filters.page - 1 })}>上一页</Button>
      <span className="text-sm">第 {filters.page} / {Math.max(1, Math.ceil(total / MARKET_PAGE_SIZE))} 页 · 共 {total} 个</span>
      <Button type="button" variant="outline" className="min-h-11" disabled={loading || filters.page >= Math.ceil(total / MARKET_PAGE_SIZE)} onClick={() => change({ page: filters.page + 1 })}>下一页</Button>
    </nav>}
  </section>;
}
