/**
 * 发布记录：一个用户级只读接口覆盖代发、本地在途与自助发布。
 * 页面不触发供应商同步；失败时保留最后一次成功快照。
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';
import { lazyToast } from '@/lib/lazyToast';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  Activity, AlertCircle, ChevronDown, ChevronLeft, ChevronRight, ChevronUp,
  ExternalLink, Loader2, RefreshCw, RotateCcw, Search, Send,
  ShoppingCart, Undo2,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

type HistorySource = 'proxy' | 'self';
type HistoryView = 'order' | 'article';
// [R2 §②③] `reported_unverified` = 浏览器回报过成功、服务端还没核实过。
// 它既不是 completed 也不是 in_progress —— 少了这个值，那些行会掉进
// statusClass 的默认分支被涂成「处理中」蓝，等于换个说法继续骗人。
type StatusFamily = '' | 'pending' | 'in_progress' | 'completed' | 'reported_unverified' | 'rejected' | 'withdrawn' | 'refunded';
type PublicationAxis = 'verified_published' | 'reported_success_unverified' | 'not_published';

interface HistoryStats {
  total: number;
  completed: number;
  in_progress: number;
  pending: number;
  rejected: number;
  withdrawn: number;
  refunded: number;
  reported_unverified?: number;
}

interface HistoryFilters {
  brands: Array<{ id: number; name: string }>;
  media_types: string[];
}

interface HistoryRecord {
  record_key: string;
  source: HistorySource;
  action_id: string;
  order_sn?: string;
  article_id?: number;
  article_title: string;
  brand_id?: number;
  brand_name?: string;
  channel_name: string;
  media_type: string;
  status_key: string;
  status_label: string;
  status_family: Exclude<StatusFamily, ''>;
  publication_axis?: PublicationAxis;
  can_view?: boolean;
  points: number;
  public_url?: string;
  status_detail?: string;
  created_at?: string;
  updated_at?: string;
  published_at?: string;
  refund_status?: string;
  can_withdraw: boolean;
  can_refund: boolean;
  can_republish: boolean;
}

interface HistoryArticleGroup {
  record_key: string;
  source: HistorySource;
  article_id?: number;
  article_title: string;
  brand_id?: number;
  brand_name?: string;
  total: number;
  completed: number;
  in_progress: number;
  rejected: number;
  withdrawn: number;
  refunded: number;
  records: HistoryRecord[];
}

type HistoryItem = HistoryRecord | HistoryArticleGroup;

interface HistoryResponse {
  status: 'success';
  records: HistoryItem[];
  total: number;
  page: number;
  pages: number;
  stats: HistoryStats;
  filters: HistoryFilters;
}

const EMPTY_FILTERS: HistoryFilters = { brands: [], media_types: [] };

const STATUS_FILTERS: Array<{ value: StatusFamily; label: string }> = [
  { value: '', label: '全部' },
  { value: 'completed', label: '已完成' },
  { value: 'in_progress', label: '处理中' },
  { value: 'reported_unverified', label: '待核实' },
  { value: 'pending', label: '准备提交' },
  { value: 'rejected', label: '未完成' },
  { value: 'withdrawn', label: '已撤回' },
  { value: 'refunded', label: '已退款' },
];

const REFUND_LABELS: Record<string, string> = {
  pending: '退款审核中',
  approved: '退款已通过',
  rejected: '退款未通过',
};

const PLATFORM_NAMES: Record<string, string> = {
  zhihu: '知乎', csdn: 'CSDN', juejin: '掘金', baijiahao: '百家号',
  weibo: '微博', woshipm: '人人都是产品经理', toutiao: '今日头条',
  sohu: '搜狐号', douban: '豆瓣', cto51: '51CTO', cnblogs: '博客园',
  bilibili: 'B站', wechat: '微信公众号',
};

function retryAfterMilliseconds(response: Response): number {
  const raw = response.headers.get('Retry-After');
  if (!raw) return 60_000;
  if (/^\d+$/.test(raw)) return Math.max(1_000, Number(raw) * 1_000);
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? Math.max(1_000, parsed - Date.now()) : 60_000;
}

function formatDate(value?: string): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `${parsed.toLocaleDateString('zh-CN')} ${parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;
}

function statusClass(status: HistoryRecord): string {
  if (status.status_family === 'completed') return 'bg-emerald-500/15 text-emerald-400';
  if (status.status_family === 'rejected') return 'bg-red-500/15 text-red-400';
  if (status.status_family === 'withdrawn') return 'bg-zinc-500/15 text-zinc-400';
  if (status.status_family === 'refunded') return 'bg-orange-500/15 text-orange-400';
  // [R2 §③] 待核实用琥珀色描边，和「已完成」绿、「处理中」蓝都分开 ——
  // 颜色本身就是一次陈述，涂成绿的等于在说已发布。
  if (status.status_family === 'reported_unverified') return 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30';
  if (status.status_family === 'pending' || status.status_key === 'awaiting_confirmation') return 'bg-amber-500/15 text-amber-400';
  return 'bg-blue-500/15 text-blue-400';
}

function isArticleGroup(item: HistoryItem): item is HistoryArticleGroup {
  return Array.isArray((item as HistoryArticleGroup).records);
}

export function PublishHistory() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const navigate = useNavigate();
  const { user } = useAuth();
  const identityKey = user?.id ? String(user.id) : '';
  const [source, setSource] = useState<HistorySource>('proxy');
  const [view, setView] = useState<HistoryView>('order');
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [total, setTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState<StatusFamily>('');
  const [brandFilter, setBrandFilter] = useState<number | null>(null);
  const [mediaTypeFilter, setMediaTypeFilter] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [records, setRecords] = useState<HistoryItem[]>([]);
  const [stats, setStats] = useState<HistoryStats | null>(null);
  const [filters, setFilters] = useState<HistoryFilters>(EMPTY_FILTERS);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [retryUntil, setRetryUntil] = useState(0);
  const [clock, setClock] = useState(Date.now());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [withdrawingId, setWithdrawingId] = useState<string | null>(null);
  const [refundRecord, setRefundRecord] = useState<HistoryRecord | null>(null);
  const [refundReason, setRefundReason] = useState('');
  const [submittingRefund, setSubmittingRefund] = useState(false);
  // [WO_273] 「待核实」行原有两个动作(重新核实 / 人工证据),走的是浏览器插件那组接口;
  //   插件自助发布整块退役、接口由后端同单删除,两个动作随之下线。历史自助记录本身照常只读可见。

  const requestSeqRef = useRef(0);
  const inFlightRef = useRef<AbortController | null>(null);
  const hasSuccessRef = useRef(false);
  const successfulQueryKeyRef = useRef('');
  const retryUntilRef = useRef(0);
  const identityRef = useRef(identityKey);
  const queryKey = [identityKey, source, view, page, statusFilter, brandFilter ?? '', mediaTypeFilter, search].join('\u001f');
  const dataQueryKeyRef = useRef(queryKey);

  const retrySeconds = retryUntil > clock ? Math.ceil((retryUntil - clock) / 1000) : 0;
  const queryControlsDisabled = initialLoading || refreshing || retrySeconds > 0;

  useLayoutEffect(() => {
    if (identityRef.current === identityKey) return;
    identityRef.current = identityKey;
    requestSeqRef.current += 1;
    inFlightRef.current?.abort();
    inFlightRef.current = null;
    hasSuccessRef.current = false;
    successfulQueryKeyRef.current = '';
    dataQueryKeyRef.current = queryKey;
    retryUntilRef.current = 0;
    setRetryUntil(0);
    setRecords([]);
    setStats(null);
    setFilters(EMPTY_FILTERS);
    setTotal(0);
    setPages(0);
    setPage(1);
    setLoadError(false);
    setInitialLoading(Boolean(identityKey));
  }, [identityKey, queryKey]);

  useLayoutEffect(() => {
    if (dataQueryKeyRef.current === queryKey) return;
    dataQueryKeyRef.current = queryKey;
    requestSeqRef.current += 1;
    inFlightRef.current?.abort();
    inFlightRef.current = null;

    // A last-good response is valid only for the exact query that produced it.
    // Never display page/filter A under the controls for page/filter B.
    hasSuccessRef.current = successfulQueryKeyRef.current === queryKey;
    if (!hasSuccessRef.current) {
      setRecords([]);
      setStats(null);
      setTotal(0);
      setPages(0);
      setLoadError(false);
      setInitialLoading(Boolean(identityKey));
    }
  }, [identityKey, queryKey]);

  useEffect(() => {
    if (!retryUntil) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setClock(now);
      if (now >= retryUntil) {
        retryUntilRef.current = 0;
        setRetryUntil(0);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, [retryUntil]);

  const loadRecords = useCallback(async () => {
    if (!identityKey || inFlightRef.current || Date.now() < retryUntilRef.current) return;
    const seq = ++requestSeqRef.current;
    const controller = new AbortController();
    inFlightRef.current = controller;
    if (hasSuccessRef.current) setRefreshing(true);
    else setInitialLoading(true);

    const params = new URLSearchParams({
      source,
      view,
      page: String(page),
      limit: '20',
    });
    if (statusFilter) params.set('status', statusFilter);
    if (brandFilter) params.set('brand_id', String(brandFilter));
    if (mediaTypeFilter) params.set('media_type', mediaTypeFilter);
    if (search) params.set('search', search);

    try {
      const response = await authFetch(`/api/meijiehezi/publish-history?${params}`, { signal: controller.signal });
      const data = await response.json().catch(() => ({})) as Partial<HistoryResponse>;
      if (response.status === 429) {
        if (seq === requestSeqRef.current) {
          const until = Date.now() + retryAfterMilliseconds(response);
          retryUntilRef.current = until;
          setRetryUntil(until);
          setClock(Date.now());
        }
        throw new Error('rate-limited');
      }
      if (!response.ok || data.status !== 'success') throw new Error('unavailable');
      if (controller.signal.aborted || seq !== requestSeqRef.current) return;

      setRecords(Array.isArray(data.records) ? data.records : []);
      setTotal(Number(data.total || 0));
      setPages(Number(data.pages || 0));
      setStats(data.stats || null);
      setFilters(data.filters || EMPTY_FILTERS);
      setLoadError(false);
      hasSuccessRef.current = true;
      successfulQueryKeyRef.current = queryKey;
    } catch (error) {
      if (!controller.signal.aborted && seq === requestSeqRef.current) {
        setLoadError(true);
      }
    } finally {
      if (inFlightRef.current === controller) inFlightRef.current = null;
      if (seq === requestSeqRef.current) {
        setInitialLoading(false);
        setRefreshing(false);
      }
    }
  }, [identityKey, source, view, page, statusFilter, brandFilter, mediaTypeFilter, search, queryKey]);

  useEffect(() => {
    void loadRecords();
    return () => {
      requestSeqRef.current += 1;
      inFlightRef.current?.abort();
      inFlightRef.current = null;
    };
  }, [loadRecords]);

  const resetContext = (nextSource: HistorySource, nextView: HistoryView = 'order') => {
    if (nextSource === source && nextView === view) return;
    if (Date.now() < retryUntilRef.current) return;
    requestSeqRef.current += 1;
    inFlightRef.current?.abort();
    inFlightRef.current = null;
    hasSuccessRef.current = false;
    successfulQueryKeyRef.current = '';
    setSource(nextSource);
    setView(nextView);
    setPage(1);
    setStatusFilter('');
    setBrandFilter(null);
    setMediaTypeFilter('');
    setSearch('');
    setSearchInput('');
    setRecords([]);
    setStats(null);
    setFilters(EMPTY_FILTERS);
    setLoadError(false);
    setInitialLoading(true);
  };

  const changeView = (next: HistoryView) => {
    if (next === view || Date.now() < retryUntilRef.current) return;
    requestSeqRef.current += 1;
    inFlightRef.current?.abort();
    inFlightRef.current = null;
    hasSuccessRef.current = false;
    successfulQueryKeyRef.current = '';
    setView(next);
    setPage(1);
    setRecords([]);
    setStats(null);
    setLoadError(false);
    setInitialLoading(true);
  };

  const refresh = () => {
    if (retrySeconds > 0 || inFlightRef.current) return;
    void loadRecords();
  };

  const handleWithdraw = async (record: HistoryRecord) => {
    if (!(await askConfirm({ title: '确定撤回这条发布记录吗？符合条件的算力将按原规则退还。', danger: true }))) return;
    setWithdrawingId(record.record_key);
    try {
      const response = await authFetch(`/api/meijiehezi/orders/withdraw?order_id=${encodeURIComponent(record.action_id)}`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.status !== 'success') throw new Error('withdraw failed');
      lazyToast.success(data.message || '已撤回');
      await loadRecords();
    } catch {
      lazyToast.error('撤回暂时无法完成，请稍后重试');
    } finally {
      setWithdrawingId(null);
    }
  };

  const handleRefund = async () => {
    if (!refundRecord || submittingRefund) return;
    setSubmittingRefund(true);
    try {
      const response = await authFetch('/api/meijiehezi/refund/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order_id: refundRecord.action_id, reason: refundReason }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.status !== 'success') throw new Error('refund failed');
      lazyToast.success('退款申请已提交');
      setRefundRecord(null);
      setRefundReason('');
      await loadRecords();
    } catch {
      lazyToast.error('退款申请暂时无法提交，请稍后重试');
    } finally {
      setSubmittingRefund(false);
    }
  };

  const handleRepublish = async (record: HistoryRecord) => {
    if (!record.order_sn) return;
    try {
      const response = await authFetch(`/api/meijiehezi/mhz-orders/${encodeURIComponent(record.order_sn)}/article-info`);
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.status !== 'success' || !data.article_id) throw new Error('not found');
      const params = new URLSearchParams({ article_id: String(data.article_id), republish_from: record.order_sn });
      if (data.quote_id) params.set('quote_id', String(data.quote_id));
      if (data.brand_id) params.set('brand_id', String(data.brand_id));
      if (data.media_type) params.set('media_type', String(data.media_type));
      window.location.href = `/publish?${params}`;
    } catch {
      lazyToast.error('暂时找不到原始文章，无法重新发布');
    }
  };

  const renderRecord = (record: HistoryRecord, nested = false) => (
    <div
      key={record.record_key}
      data-testid="publish-history-record"
      data-record-key={record.record_key}
      className={cn('min-w-0 rounded-lg border border-border px-3 py-3 sm:px-4', nested && 'rounded-none border-0 border-t first:border-t-0')}
    >
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start">
        <span className={cn('w-fit shrink-0 rounded px-2 py-0.5 text-[11px] font-medium', statusClass(record))}>
          {record.status_label}
        </span>
        <div className="min-w-0 flex-1">
          <div className="break-words text-sm font-medium" title={record.article_title}>
            {record.article_title || '未命名内容'}
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span className="break-all text-foreground/70">
              {record.source === 'self' ? (PLATFORM_NAMES[record.channel_name] || record.channel_name) : record.channel_name}
            </span>
            {record.brand_name && <span className="max-w-full break-words">{record.brand_name}</span>}
            <span>{formatDate(record.created_at)}</span>
            {record.published_at && <span className="text-emerald-400">发布：{formatDate(record.published_at)}</span>}
            {record.public_url && (
              <a href={record.public_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                查看结果 <ExternalLink className="size-3" />
              </a>
            )}
          </div>
          {record.status_detail && <div className="mt-1.5 break-words text-[11px] text-red-400">{record.status_detail}</div>}
          {record.refund_status && (
            <div className="mt-1.5 text-[11px] text-orange-400">{REFUND_LABELS[record.refund_status] || '退款处理中'}</div>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 sm:flex-col sm:items-end">
          {record.source === 'proxy' && (
            <div className="mr-2 text-left sm:mr-0 sm:text-right">
              <div className="text-sm font-semibold">{Number(record.points || 0).toLocaleString()}</div>
              <div className="text-[10px] text-muted-foreground">算力</div>
            </div>
          )}
          {record.can_withdraw && (
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-amber-400" disabled={withdrawingId === record.record_key} onClick={() => handleWithdraw(record)}>
              {withdrawingId === record.record_key ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Undo2 className="mr-1 size-3" />}撤回
            </Button>
          )}
          {record.can_refund && !record.refund_status && (
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-orange-400" onClick={() => { setRefundRecord(record); setRefundReason(''); }}>
              <RotateCcw className="mr-1 size-3" />申请退款
            </Button>
          )}
          {record.can_republish && record.order_sn && (
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-amber-400" onClick={() => handleRepublish(record)}>
              <RotateCcw className="mr-1 size-3" />重新发布
            </Button>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden p-3 sm:p-4 md:p-6">
      <div className="mb-4 flex shrink-0 flex-col gap-2 border-b border-border pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <h1 className="shrink-0 text-lg font-bold">发布记录</h1>
          {/* [WP7/WP6 收尾 2026-08-17 · 规格 03 §10]
              本页列的是**每一次发布尝试**(同一交付单元的重试会各占一行),
              不是合同口径的"已发布在线"。后端已用 `records_basis: 'raw_attempts'`
              标注这一点,前端必须把它说出来 —— 否则代理会拿这里的行数当交付数,
              而两者在撤稿/重试之后必然对不上。合同数字在门户的六阶段元组里。 */}
          <span data-testid="history-basis"
                className="shrink-0 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
            按发布尝试逐条列出(未按合同去重)
          </span>
          <Button variant="ghost" size="sm" className="shrink-0 text-xs sm:hidden" onClick={() => navigate('/monitoring')}>
            <Activity className="mr-1 size-3.5" />去监测中心
          </Button>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex min-w-0 flex-1 rounded-lg bg-secondary p-0.5 sm:flex-none">
            <button type="button" disabled={queryControlsDisabled} onClick={() => resetContext('proxy')} className={cn('min-w-0 flex-1 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium sm:flex-none', source === 'proxy' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground')}>
              <ShoppingCart className="mr-1 inline size-3.5" />媒介代发
            </button>
            <button type="button" disabled={queryControlsDisabled} onClick={() => resetContext('self')} className={cn('min-w-0 flex-1 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium sm:flex-none', source === 'self' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground')}>
              <Send className="mr-1 inline size-3.5" />自助发布
            </button>
          </div>
          <Button variant="ghost" size="sm" className="hidden shrink-0 text-xs sm:inline-flex" onClick={() => navigate('/monitoring')}>
            <Activity className="mr-1 size-3.5" />去监测中心
          </Button>
        </div>
      </div>

      <div className="mb-3 flex shrink-0 flex-col gap-3 lg:flex-row lg:items-start">
        <div className="grid flex-1 grid-cols-4 gap-1.5 sm:gap-2 xl:grid-cols-7">
          {[
            ['总记录', stats?.total], ['已完成', stats?.completed], ['处理中', stats?.in_progress],
            ['准备提交', stats?.pending], ['未完成', stats?.rejected], ['已撤回', stats?.withdrawn], ['已退款', stats?.refunded],
          ].map(([label, value]) => (
            <Card key={String(label)}><CardContent className="p-2 text-center sm:p-3">
              <div className="text-base font-bold sm:text-lg" data-testid={`history-stat-${label}`}>{value ?? '—'}</div>
              <div className="whitespace-nowrap text-[9px] text-muted-foreground sm:text-[10px]">{label}</div>
            </CardContent></Card>
          ))}
        </div>
        <Button
          data-testid="publish-history-refresh"
          variant="outline"
          size="sm"
          className="w-full shrink-0 lg:w-auto"
          disabled={initialLoading || refreshing || retrySeconds > 0}
          onClick={refresh}
        >
          <RefreshCw className={cn('mr-1.5 size-3.5', (initialLoading || refreshing) && 'animate-spin')} />
          {retrySeconds > 0 ? `${retrySeconds} 秒后可重新读取` : refreshing ? '读取中…' : '重新读取'}
        </Button>
      </div>

      {loadError && (
        <div role="alert" className="mb-3 flex shrink-0 flex-col gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-sm sm:flex-row sm:items-center">
          <div className="flex min-w-0 flex-1 items-start gap-2 text-amber-200">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span className="break-words">{hasSuccessRef.current ? '暂时无法更新，当前显示上次结果。' : '暂时无法读取发布记录，请稍后重试。'}</span>
          </div>
          {retrySeconds > 0 && <span className="shrink-0 text-xs text-amber-200/80">{retrySeconds} 秒后可重试</span>}
        </div>
      )}

      <div className="mb-3 flex shrink-0 flex-col gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <div className="flex rounded-lg bg-secondary p-0.5">
            <button type="button" disabled={queryControlsDisabled} onClick={() => changeView('order')} className={cn('rounded-md px-3 py-1 text-xs font-medium', view === 'order' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground')}>按记录看</button>
            <button type="button" disabled={queryControlsDisabled} onClick={() => changeView('article')} className={cn('rounded-md px-3 py-1 text-xs font-medium', view === 'article' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground')}>按内容看</button>
          </div>
          <div className="flex max-w-full flex-wrap gap-1.5">
            {STATUS_FILTERS.map(option => (
              <button key={option.value} type="button" disabled={queryControlsDisabled} onClick={() => { setStatusFilter(option.value); setPage(1); }} className={cn('rounded-full px-3 py-1 text-xs font-medium', statusFilter === option.value ? 'bg-primary text-primary-foreground' : 'bg-secondary text-muted-foreground')}>
                {option.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:flex-wrap">
          {filters.brands.length > 0 && (
            <select disabled={queryControlsDisabled} value={brandFilter ?? ''} onChange={event => { setBrandFilter(event.target.value ? Number(event.target.value) : null); setPage(1); }} className="h-8 max-w-full rounded-md border border-border bg-background px-2 text-xs">
              <option value="">全部客户</option>
              {filters.brands.map(brand => <option key={brand.id} value={brand.id}>{brand.name}</option>)}
            </select>
          )}
          {source === 'proxy' && filters.media_types.length > 0 && (
            <select disabled={queryControlsDisabled} value={mediaTypeFilter} onChange={event => { setMediaTypeFilter(event.target.value); setPage(1); }} className="h-8 rounded-md border border-border bg-background px-2 text-xs">
              <option value="">全部类型</option>
              {filters.media_types.map(type => <option key={type} value={type}>{type === 'wemedia' ? '自媒体' : type === 'svideo' ? '短视频' : '软文'}</option>)}
            </select>
          )}
          <form className="flex min-w-0 flex-1 gap-2 sm:max-w-sm" onSubmit={event => { event.preventDefault(); setSearch(searchInput.trim()); setPage(1); }}>
            <label className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-2 size-3.5 text-muted-foreground" />
              <input disabled={queryControlsDisabled} value={searchInput} onChange={event => setSearchInput(event.target.value)} placeholder="搜索内容或发布渠道" className="h-8 w-full min-w-0 rounded-md border border-border bg-background pl-8 pr-2 text-xs" />
            </label>
            <Button type="submit" variant="outline" size="sm" disabled={queryControlsDisabled} className="h-8 shrink-0">搜索</Button>
          </form>
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto overscroll-contain pb-20 pr-16 sm:pb-0 sm:pr-0">
        {initialLoading && !hasSuccessRef.current ? (
          <div className="flex justify-center py-12"><Loader2 className="size-5 animate-spin text-muted-foreground" /></div>
        ) : !hasSuccessRef.current && loadError ? (
          <div className="py-12 text-center text-sm text-muted-foreground">发布记录暂时不可用</div>
        ) : records.length === 0 ? (
          <div className="py-12 text-center text-sm text-muted-foreground">{statusFilter || brandFilter || mediaTypeFilter || search ? '当前筛选下没有记录' : '暂无发布记录'}</div>
        ) : view === 'article' ? (
          <div className="space-y-2">
            {records.filter(isArticleGroup).map(group => {
              const expanded = expandedGroups.has(group.record_key);
              return (
                <div key={group.record_key} className="min-w-0 overflow-hidden rounded-lg border border-border">
                  <button type="button" className="w-full min-w-0 px-3 py-3 text-left hover:bg-secondary/30 sm:px-4" onClick={() => setExpandedGroups(previous => {
                    const next = new Set(previous);
                    if (next.has(group.record_key)) next.delete(group.record_key); else next.add(group.record_key);
                    return next;
                  })}>
                    <div className="flex min-w-0 items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="break-words text-sm font-medium">{group.article_title || '未命名内容'}</div>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                          {group.brand_name && <span className="break-words">{group.brand_name}</span>}
                          <span>共 {group.total} 条</span>
                        </div>
                        <div className="mt-1.5 flex flex-wrap gap-1.5 text-[10px]">
                          {group.completed > 0 && <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-400">已完成 {group.completed}</span>}
                          {group.in_progress > 0 && <span className="rounded bg-blue-500/15 px-1.5 py-0.5 text-blue-400">处理中 {group.in_progress}</span>}
                          {group.rejected > 0 && <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-400">未完成 {group.rejected}</span>}
                          {group.withdrawn > 0 && <span className="rounded bg-zinc-500/15 px-1.5 py-0.5 text-zinc-400">已撤回 {group.withdrawn}</span>}
                          {group.refunded > 0 && <span className="rounded bg-orange-500/15 px-1.5 py-0.5 text-orange-400">已退款 {group.refunded}</span>}
                        </div>
                      </div>
                      {expanded ? <ChevronUp className="size-4 shrink-0 text-muted-foreground" /> : <ChevronDown className="size-4 shrink-0 text-muted-foreground" />}
                    </div>
                  </button>
                  {expanded && <div className="bg-secondary/10">{group.records.map(record => renderRecord(record, true))}</div>}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="space-y-2">{records.filter(item => !isArticleGroup(item)).map(item => renderRecord(item as HistoryRecord))}</div>
        )}
      </div>

      {pages > 1 && (
        <div className="mt-3 flex shrink-0 items-center justify-between border-t border-border pt-3">
          <Button variant="ghost" size="sm" disabled={page <= 1 || refreshing || retrySeconds > 0} onClick={() => setPage(current => current - 1)}><ChevronLeft className="mr-1 size-4" />上一页</Button>
          <span className="text-xs text-muted-foreground">{page}/{pages}（共 {total} 条）</span>
          <Button variant="ghost" size="sm" disabled={page >= pages || refreshing || retrySeconds > 0} onClick={() => setPage(current => current + 1)}>下一页<ChevronRight className="ml-1 size-4" /></Button>
        </div>
      )}

      {refundRecord && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setRefundRecord(null)}>
          <div role="dialog" aria-modal="true" aria-label="申请退款" className="w-full max-w-md rounded-xl border border-border bg-card p-5 shadow-lg" onClick={event => event.stopPropagation()}>
            <h2 className="mb-3 text-sm font-bold">申请退款</h2>
            <div className="mb-3 min-w-0 text-xs text-muted-foreground">
              <div className="break-words text-foreground">{refundRecord.article_title}</div>
              <div className="mt-1">{refundRecord.channel_name} · {Number(refundRecord.points || 0).toLocaleString()} 算力</div>
            </div>
            <textarea value={refundReason} onChange={event => setRefundReason(event.target.value)} placeholder="请说明退款原因（可选）" className="h-24 w-full resize-none rounded-lg border border-border bg-secondary/50 px-3 py-2 text-sm" />
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setRefundRecord(null)}>取消</Button>
              <Button size="sm" disabled={submittingRefund} onClick={handleRefund}>{submittingRefund && <Loader2 className="mr-1 size-3 animate-spin" />}确认申请</Button>
            </div>
          </div>
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
