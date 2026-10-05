/**
 * /agent/leads · 客户线索后台 (PR-B 前端)
 *
 * CTO-15.23 2026-05-03 · Codex backend final sign-off (7c51b8c) 之上接前端
 *
 * 消费 backend 4 endpoints:
 *   GET  /api/agent/leads           · 列表 + stats 全量(P1-1) + 排除 action(P1-3)
 *   GET  /api/agent/leads/:id       · 详情
 *   GET  /api/agent/leads/:id/timeline · 行为时间线 + decision_window_text
 *   PATCH /api/agent/leads/:id/status  · 标 contacted/closed/lost · 写 status_updated_at
 *
 * 5 操作按钮(Codex 拍):
 *   1. 拨号(tel:) · 浏览器 native
 *   2. 复制微信(剪贴板)
 *   3. 复制客户信息(公司+手机+诊断 ID)
 *   4. 跳客户工作台(/my-clients/:brand_id)
 *   5. 标完成(PATCH status='contacted')
 *
 * UX:
 *   · 列表 + status filter tabs
 *   · 点行 → Sheet drawer 详情 + 时间线
 *   · N < 10 → "样本不足" (D2 防失真)
 *   · RBAC 已在 backend(代理只看自己 / admin 全部) · 前端不需 hard-code
 */
import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Phone, MessageCircle, Copy, ExternalLink, Check,
  Loader2, RefreshCw, Inbox, AlertCircle,
} from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle,
} from '@/components/ui/sheet';
import { authApi } from '@/context/AuthContext';
import { cn, formatDateTime, formatRelativeTime } from '@/lib/utils';
import { copyToClipboard as clipboardCopy } from '@/lib/copyUtils';

// ============ 类型 ============

type LeadStatus = 'new' | 'contacted' | 'closed' | 'lost';

interface Lead {
  id: number;
  diagnosis_id: number;
  phone: string;
  company_name: string;
  status: LeadStatus;
  created_at: string;
  status_updated_at: string | null;
  brand_name: string;
  industry: string;
  brand_id: number | null;
}

interface LeadDetail extends Lead {
  total_score: number | null;
  level: string | null;
}

interface TimelineEvent {
  time: string | null;
  kind: string;
  label: string;
  meta: Record<string, unknown>;
}

interface ListResponse {
  success: boolean;
  leads: Lead[];
  page_count: number;
  stats: {
    total: number;
    by_status: Record<LeadStatus, number>;
  };
}

interface TimelineResponse {
  success: boolean;
  timeline: TimelineEvent[];
  decision_window_seconds: number | null;
  decision_window_text: string | null;
  raw_event_count: number;
}

// ============ 常量 ============

const STATUS_LABEL: Record<LeadStatus, string> = {
  new: '未跟进',
  contacted: '已联系',
  closed: '已成交',
  lost: '已流失',
};

const STATUS_COLOR: Record<LeadStatus, string> = {
  new: 'bg-blue-500/10 text-blue-700 border-blue-200',
  contacted: 'bg-amber-500/10 text-amber-700 border-amber-200',
  closed: 'bg-emerald-500/10 text-emerald-700 border-emerald-200',
  lost: 'bg-zinc-500/10 text-zinc-600 border-zinc-200',
};

const FILTER_TABS: Array<{ key: 'all' | LeadStatus; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'new', label: '未跟进' },
  { key: 'contacted', label: '已联系' },
  { key: 'closed', label: '已成交' },
  { key: 'lost', label: '已流失' },
];

const SAMPLE_THRESHOLD = 10; // D2 · N<10 显示"样本不足"

// ============ 工具 ============
// PR-B frontend.1 (Codex P3) · 复用 lib/utils.ts 统一北京时区时间格式化
// 不再本地实现 · 跨浏览器/跨页面口径一致(同 通知/客户工作台/payment 等)

async function copyToClipboard(text: string, successMsg: string): Promise<void> {
  const ok = await clipboardCopy(text);
  if (ok) {
    toast.success(successMsg);
  } else {
    toast.error('复制失败 · 请手动选择文本');
  }
}

// ============ 主页面 ============

export default function LeadsPage() {
  const navigate = useNavigate();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [stats, setStats] = useState<ListResponse['stats'] | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState<'all' | LeadStatus>('all');
  const [error, setError] = useState<string>('');

  // Drawer state
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerLead, setDrawerLead] = useState<LeadDetail | null>(null);
  const [drawerTimeline, setDrawerTimeline] = useState<TimelineResponse | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [statusUpdating, setStatusUpdating] = useState(false);

  const fetchList = useCallback(async () => {
    try {
      setRefreshing(true);
      const qs = filter === 'all' ? '?limit=200' : `?status=${filter}&limit=200`;
      const res = await authApi.get<ListResponse>(`/api/agent/leads${qs}`);
      const data = res.data;
      if (data.success) {
        setLeads(data.leads || []);
        setStats(data.stats);
        setPageCount(data.page_count || 0);
        setError('');
      } else {
        setError('加载失败 · 请刷新重试');
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg || '网络错误');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [filter]);

  useEffect(() => {
    fetchList();
  }, [fetchList]);

  const openDrawer = useCallback(async (leadId: number) => {
    setDrawerOpen(true);
    setDrawerLoading(true);
    setDrawerLead(null);
    setDrawerTimeline(null);
    try {
      const [detailRes, tlRes] = await Promise.all([
        authApi.get<{ success: boolean; lead: LeadDetail }>(`/api/agent/leads/${leadId}`),
        authApi.get<TimelineResponse>(`/api/agent/leads/${leadId}/timeline`),
      ]);
      if (detailRes.data.success) setDrawerLead(detailRes.data.lead);
      if (tlRes.data.success) setDrawerTimeline(tlRes.data);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`加载详情失败: ${msg}`);
    } finally {
      setDrawerLoading(false);
    }
  }, []);

  const updateStatus = useCallback(async (leadId: number, newStatus: LeadStatus) => {
    if (statusUpdating) return;
    setStatusUpdating(true);
    try {
      const res = await authApi.patch<{
        success: boolean;
        new_status: LeadStatus;
        status_updated_at: string | null;
      }>(`/api/agent/leads/${leadId}/status`, { status: newStatus });
      const data = res.data;
      if (data.success) {
        toast.success(`已标记为「${STATUS_LABEL[newStatus]}」`);
        // 更新 drawer
        setDrawerLead((prev) => prev ? {
          ...prev,
          status: newStatus,
          status_updated_at: data.status_updated_at,
        } : prev);
        // 列表也同步刷
        fetchList();
        // timeline 重拉看 status_changed
        authApi.get<TimelineResponse>(`/api/agent/leads/${leadId}/timeline`)
          .then((r) => { if (r.data.success) setDrawerTimeline(r.data); })
          .catch(() => {});
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`状态更新失败: ${msg}`);
    } finally {
      setStatusUpdating(false);
    }
  }, [fetchList, statusUpdating]);

  // ============ 5 操作按钮 ============

  const handlePhone = useCallback((phone: string) => {
    window.location.href = `tel:${phone}`;
  }, []);

  const handleWechat = useCallback((phone: string) => {
    copyToClipboard(phone, '手机号已复制 · 打开微信粘贴搜索');
  }, []);

  const handleCopyInfo = useCallback((lead: LeadDetail) => {
    const lines = [
      `公司: ${lead.company_name || lead.brand_name || '(未填)'}`,
      `手机: ${lead.phone}`,
      `行业: ${lead.industry || '—'}`,
      `诊断 ID: #${lead.diagnosis_id} · ${lead.level || '—'}级 · ${lead.total_score ?? '—'}/100`,
      `留资时间: ${formatDateTime(lead.created_at)}`,
    ];
    copyToClipboard(lines.join('\n'), '客户信息已复制 · 可粘贴到 CRM/微信');
  }, []);

  const handleJumpWorkbench = useCallback((brandId: number | null) => {
    if (!brandId) {
      toast.error('该线索无关联品牌 · 无法跳客户工作台');
      return;
    }
    navigate(`/my-clients/${brandId}`);
  }, [navigate]);

  // ============ 派生 ============

  const sampleInsufficient = useMemo(
    () => (stats?.total ?? 0) < SAMPLE_THRESHOLD,
    [stats]
  );

  const displayLeads = leads;

  // ============ Render ============

  return (
    <div className="bg-background">
      <div className="max-w-6xl mx-auto p-4 sm:p-6">
        {/* Header */}
        <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
          <div>
            <h1 className="text-xl sm:text-2xl font-bold text-foreground">客户线索</h1>
            <p className="text-xs sm:text-sm text-muted-foreground mt-1">
              客户在诊断报告页留资 · 顾问按意向度跟进
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={fetchList}
            disabled={refreshing}
            className="gap-2"
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            刷新
          </Button>
        </div>

        {/* Stats row */}
        {stats && (
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-6">
            <StatCard label="全部" value={stats.total} sample={sampleInsufficient} />
            <StatCard label="未跟进" value={stats.by_status.new} sample={sampleInsufficient} accent="blue" />
            <StatCard label="已联系" value={stats.by_status.contacted} sample={sampleInsufficient} accent="amber" />
            <StatCard label="已成交" value={stats.by_status.closed} sample={sampleInsufficient} accent="emerald" />
            <StatCard label="已流失" value={stats.by_status.lost} sample={sampleInsufficient} accent="zinc" />
          </div>
        )}

        {/* Filter tabs */}
        <div className="flex flex-wrap gap-2 mb-4 border-b border-border pb-3">
          {FILTER_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setFilter(t.key)}
              className={cn(
                'px-3 py-1.5 rounded-md text-xs sm:text-sm font-medium transition-colors',
                filter === t.key
                  ? 'bg-foreground text-background'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              )}
            >
              {t.label}
              {stats && t.key !== 'all' && stats.by_status[t.key] > 0 && (
                <span className="ml-1.5 text-[10px] opacity-70">
                  {stats.by_status[t.key]}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* List */}
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : error ? (
          <div className="flex items-center justify-center py-16 text-rose-600 gap-2">
            <AlertCircle className="h-5 w-5" />
            {error}
          </div>
        ) : displayLeads.length === 0 ? (
          <EmptyState filter={filter} />
        ) : (
          <div className="space-y-2">
            {displayLeads.map((lead) => (
              <LeadRow
                key={lead.id}
                lead={lead}
                onClick={() => openDrawer(lead.id)}
              />
            ))}
            {/* PR-B frontend.1 (Codex P2) · filter 模式下用 by_status[filter] 不是 stats.total */}
            {(() => {
              const currentTotal = filter === 'all'
                ? (stats?.total ?? 0)
                : (stats?.by_status[filter] ?? 0);
              if (pageCount >= currentTotal) return null;
              return (
                <p className="text-xs text-muted-foreground text-center pt-3">
                  显示前 {pageCount} 条 · {filter === 'all' ? '全量' : `「${STATUS_LABEL[filter]}」共`} {currentTotal} 条
                </p>
              );
            })()}
          </div>
        )}
      </div>

      {/* Drawer */}
      <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
        <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
          <SheetHeader>
            <SheetTitle>客户线索详情</SheetTitle>
          </SheetHeader>

          {drawerLoading ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : drawerLead ? (
            <div className="px-4 pb-6 space-y-5">
              {/* 头部信息 */}
              <div className="space-y-1.5">
                <h3 className="text-base font-semibold text-foreground">
                  {drawerLead.company_name || drawerLead.brand_name || '(未填公司)'}
                </h3>
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge variant="outline" className={STATUS_COLOR[drawerLead.status]}>
                    {STATUS_LABEL[drawerLead.status]}
                  </Badge>
                  {drawerLead.level && (
                    <span className="text-xs text-muted-foreground">
                      诊断 #{drawerLead.diagnosis_id} · {drawerLead.level}级 · {drawerLead.total_score ?? '—'}/100
                    </span>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  留资时间: {formatDateTime(drawerLead.created_at)} · {formatRelativeTime(drawerLead.created_at)}
                </p>
                {drawerLead.status_updated_at && (
                  <p className="text-xs text-muted-foreground">
                    状态更新: {formatDateTime(drawerLead.status_updated_at)}
                  </p>
                )}
              </div>

              {/* 联系信息 */}
              <div className="grid grid-cols-2 gap-2 p-3 bg-muted/30 rounded-md">
                <div>
                  <div className="text-[10px] text-muted-foreground uppercase tracking-wide">手机号</div>
                  <div className="text-sm font-mono font-semibold text-foreground mt-0.5">
                    {drawerLead.phone}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground uppercase tracking-wide">行业</div>
                  <div className="text-sm text-foreground mt-0.5">
                    {drawerLead.industry || '—'}
                  </div>
                </div>
              </div>

              {/* 5 操作按钮 */}
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">操作</div>
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    variant="default"
                    size="sm"
                    className="gap-2"
                    onClick={() => handlePhone(drawerLead.phone)}
                  >
                    <Phone className="h-4 w-4" />
                    拨号联系
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => handleWechat(drawerLead.phone)}
                  >
                    <MessageCircle className="h-4 w-4" />
                    复制微信号
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => handleCopyInfo(drawerLead)}
                  >
                    <Copy className="h-4 w-4" />
                    复制客户信息
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    onClick={() => handleJumpWorkbench(drawerLead.brand_id)}
                    disabled={!drawerLead.brand_id}
                  >
                    <ExternalLink className="h-4 w-4" />
                    跳客户工作台
                  </Button>
                </div>
                {drawerLead.status === 'new' && (
                  <Button
                    variant="default"
                    className="w-full gap-2 mt-2"
                    onClick={() => updateStatus(drawerLead.id, 'contacted')}
                    disabled={statusUpdating}
                  >
                    {statusUpdating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                    标记为已联系
                  </Button>
                )}
              </div>

              {/* 状态切换 */}
              {drawerLead.status !== 'new' && (
                <div className="space-y-2">
                  <div className="text-xs font-medium text-muted-foreground">改状态</div>
                  <div className="flex flex-wrap gap-1.5">
                    {(['new', 'contacted', 'closed', 'lost'] as LeadStatus[]).map((s) => (
                      <button
                        key={s}
                        disabled={s === drawerLead.status || statusUpdating}
                        onClick={() => updateStatus(drawerLead.id, s)}
                        className={cn(
                          'px-2.5 py-1 text-xs rounded-md border transition-colors',
                          s === drawerLead.status
                            ? cn(STATUS_COLOR[s], 'opacity-60 cursor-default')
                            : 'border-border hover:bg-muted'
                        )}
                      >
                        {STATUS_LABEL[s]}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* 时间线 */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="text-xs font-medium text-muted-foreground">客户行为时间线</div>
                  {drawerTimeline?.decision_window_text && (
                    <Badge variant="outline" className="bg-emerald-500/10 text-emerald-700 border-emerald-200 text-xs">
                      {drawerTimeline.decision_window_text}
                    </Badge>
                  )}
                </div>
                {drawerTimeline && drawerTimeline.timeline.length > 0 ? (
                  <div className="space-y-2 pl-3 border-l-2 border-border">
                    {drawerTimeline.timeline.map((evt, i) => (
                      <div key={i} className="relative">
                        <div className="absolute -left-[15px] top-1.5 w-2.5 h-2.5 rounded-full bg-blue-500" />
                        <div className="text-sm text-foreground">{evt.label}</div>
                        <div className="text-[10px] text-muted-foreground">
                          {formatDateTime(evt.time)} · {evt.kind}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground py-2">暂无客户行为数据</p>
                )}
                {drawerTimeline && drawerTimeline.raw_event_count > 0 && (
                  <p className="text-[10px] text-muted-foreground pt-1">
                    原始事件: {drawerTimeline.raw_event_count} 条
                    {drawerTimeline.decision_window_seconds === null && drawerTimeline.raw_event_count > 0 && (
                      <span className="ml-2 text-amber-600">· 决策窗口不可用(数据不全)</span>
                    )}
                  </p>
                )}
              </div>
            </div>
          ) : (
            <div className="px-4 py-16 text-center text-muted-foreground">
              线索不存在或无权访问
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

// ============ 子组件 ============

function StatCard({
  label, value, sample, accent,
}: {
  label: string;
  value: number;
  sample: boolean;
  accent?: 'blue' | 'amber' | 'emerald' | 'zinc';
}) {
  const accentColor = {
    blue: 'text-blue-600',
    amber: 'text-amber-600',
    emerald: 'text-emerald-600',
    zinc: 'text-zinc-600',
    undefined: 'text-foreground',
  }[accent ?? 'undefined'];

  return (
    <div className="border border-border rounded-md p-3 bg-card">
      <div className="text-[10px] text-muted-foreground uppercase tracking-wide font-semibold">
        {label}
      </div>
      <div className={cn('text-xl sm:text-2xl font-bold mt-1 leading-none', accentColor)}>
        {value}
      </div>
      {sample && (
        <div className="text-[10px] text-amber-600 mt-1">样本不足</div>
      )}
    </div>
  );
}

function LeadRow({
  lead, onClick,
}: {
  lead: Lead;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left border border-border bg-card hover:bg-muted/40 hover:border-foreground/20 transition-colors rounded-md p-3 sm:p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm sm:text-base font-semibold text-foreground truncate">
              {lead.company_name || lead.brand_name || '(未填公司)'}
            </span>
            <Badge variant="outline" className={cn('text-[10px]', STATUS_COLOR[lead.status])}>
              {STATUS_LABEL[lead.status]}
            </Badge>
          </div>
          <div className="text-xs text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">
            <span className="font-mono">{lead.phone}</span>
            {lead.industry && <span className="hidden sm:inline">· {lead.industry}</span>}
            <span>· {formatRelativeTime(lead.created_at)}</span>
          </div>
        </div>
        <div className="text-xs text-muted-foreground shrink-0">
          诊断 #{lead.diagnosis_id}
        </div>
      </div>
    </button>
  );
}

function EmptyState({ filter }: { filter: 'all' | LeadStatus }) {
  const msg = filter === 'all'
    ? '还没有客户线索 · 把诊断报告分享给客户后,客户留资会出现在这里'
    : `没有「${STATUS_LABEL[filter as LeadStatus]}」状态的线索`;
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <Inbox className="h-10 w-10 text-muted-foreground/50 mb-3" />
      <p className="text-sm text-muted-foreground max-w-sm">{msg}</p>
    </div>
  );
}
