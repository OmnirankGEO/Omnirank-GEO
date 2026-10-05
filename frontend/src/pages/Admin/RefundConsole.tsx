import { useEffect, useMemo, useState } from 'react';
import type { ComponentType, ReactNode } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronRight,
  ClipboardCheck,
  FileSearch,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { cn } from '@/lib/utils';
import {
  approveRefundWorkOrder,
  completeRefundWorkOrder,
  createRefundWorkOrderDraft,
  executeRefundWorkOrder,
  executeRefundCashJob,
  fetchRefundOrderPreview,
  listRefundWorkOrders,
  listRefundCashJobs,
  submitRefundWorkOrder,
  uploadRefundAttachment,
  type RefundWorkOrderPayload,
} from './refunds/api';
import { EvidenceUploader } from './refunds/EvidenceUploader';
import { RefundImpactPanel } from './refunds/RefundImpactPanel';
import { RefundWorkOrderList } from './refunds/RefundWorkOrderList';
import type { PendingEvidence, RefundCashJob, RefundMethod, RefundOrderPreview, RefundWorkOrder } from './refunds/types';

const WORK_ORDER_API_BASE = '/api/wallet/refund-work-orders';
const PANEL_TITLES = ['退款影响预览', '工单时间线'];

const tabs = [
  { key: 'start', label: '发起退款' },
  { key: 'submitted', label: '待审核' },
  { key: 'payout_pending', label: '待打款' },
  { key: 'cash_jobs', label: '原路退款队列' },
  { key: 'completed', label: '已完成' },
  { key: 'rejected', label: '已驳回' },
];

const reasonOptions = [
  { value: 'customer_request', label: '客户申请' },
  { value: 'wrong_recharge', label: '误充值' },
  { value: 'duplicate_recharge', label: '重复充值' },
  { value: 'service_dispute', label: '服务纠纷' },
  { value: 'platform_process', label: '平台处理' },
  { value: 'other', label: '其他' },
];

const methodOptions: Array<{ value: RefundMethod; label: string }> = [
  { value: 'manual_wechat', label: '人工微信退款' },
  { value: 'offline_transfer', label: '线下转账' },
  { value: 'original_route_pending', label: '原路退款待接入' },
  { value: 'system_only', label: '仅系统冲账不打款' },
];

function centsToYuanInput(cents?: number) {
  if (!cents) return '';
  return (cents / 100).toFixed(2);
}

function yuanToCents(value: string) {
  const num = Number(value || '0');
  if (!Number.isFinite(num) || num < 0) return 0;
  return Math.round(num * 100);
}

function nowLocal() {
  const date = new Date();
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function applyRefundMethodToPreview(preview: RefundOrderPreview | null, refundMethod: RefundMethod) {
  if (!preview) return null;
  const needsManualPayout = refundMethod !== 'system_only';
  return {
    ...preview,
    impact: {
      ...preview.impact,
      needs_manual_payout: needsManualPayout,
    },
    checks: (preview.checks || []).map((check) => (
      check.key === 'payout_proof'
        ? {
          ...check,
          label: needsManualPayout ? '需要人工打款凭证' : '无需人工打款凭证',
          status: needsManualPayout ? 'warn' : 'pass',
        }
        : check
    )),
  };
}

export default function RefundConsole() {
  const [activeTab, setActiveTab] = useState('start');
  const [query, setQuery] = useState('');
  const [preview, setPreview] = useState<RefundOrderPreview | null>(null);
  const [currentWorkOrder, setCurrentWorkOrder] = useState<RefundWorkOrder | null>(null);
  const [items, setItems] = useState<RefundWorkOrder[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [pendingFiles, setPendingFiles] = useState<PendingEvidence[]>([]);
  const [busy, setBusy] = useState(false);
  const [executeTarget, setExecuteTarget] = useState<RefundWorkOrder | null>(null);
  const [cashJobs, setCashJobs] = useState<RefundCashJob[]>([]);
  const [cashJobAttentionCount, setCashJobAttentionCount] = useState(0);
  const [executingCashJobId, setExecutingCashJobId] = useState<string | null>(null);

  const [reasonCategory, setReasonCategory] = useState('customer_request');
  const [refundMethod, setRefundMethod] = useState<RefundMethod>('manual_wechat');
  const [refundAmount, setRefundAmount] = useState('');
  const [customerRequestedAt, setCustomerRequestedAt] = useState(nowLocal());
  const [agentConfirmedAt, setAgentConfirmedAt] = useState(nowLocal());
  const [processNote, setProcessNote] = useState('');
  const [confirmAmount, setConfirmAmount] = useState(false);
  const [confirmAgent, setConfirmAgent] = useState(false);
  const [confirmEvidence, setConfirmEvidence] = useState(false);
  const [confirmPayout, setConfirmPayout] = useState(false);

  const selectedPreview = useMemo(() => {
    if (currentWorkOrder?.impact_snapshot?.order) return currentWorkOrder.impact_snapshot as RefundOrderPreview;
    return applyRefundMethodToPreview(preview, refundMethod);
  }, [currentWorkOrder, preview, refundMethod]);

  const loadList = async (status = activeTab) => {
    if (status === 'start') return;
    setBusy(true);
    try {
      if (status === 'cash_jobs') {
        const data = await listRefundCashJobs();
        setCashJobs(data.items || []);
        setCashJobAttentionCount(data.attention_count || 0);
        return;
      }
      const data = await listRefundWorkOrders(status);
      setItems(data.items || []);
      setCounts(data.counts || {});
    } catch (error: any) {
      toast.error(error.message || '加载工单失败');
    } finally {
      setBusy(false);
    }
  };

  const handleExecuteCashJob = async (job: RefundCashJob) => {
    setExecutingCashJobId(job.cash_job_id);
    try {
      const result = await executeRefundCashJob(job.cash_job_id, '管理员从原路退款队列执行或重试');
      toast.success(
        result.refund_execution.status === 'completed'
          ? '原路退款已完成并同步内账'
          : '退款渠道处理中，稍后可再次主动查询',
      );
      await loadList('cash_jobs');
    } catch (error: any) {
      toast.error(error.message || '原路退款执行失败，工单已保留');
      await loadList('cash_jobs');
    } finally {
      setExecutingCashJobId(null);
    }
  };

  useEffect(() => {
    loadList(activeTab);
  }, [activeTab]);

  const handlePreview = async () => {
    if (!query.trim()) {
      toast.error('请输入订单号、手机号或客户名');
      return;
    }
    setBusy(true);
    setCurrentWorkOrder(null);
    try {
      const data = await fetchRefundOrderPreview(query.trim());
      setPreview(data);
      setRefundAmount(centsToYuanInput(data.impact?.estimated_refund_cents));
      toast.success('订单已载入');
    } catch (error: any) {
      setPreview(null);
      toast.error(error.message || '未找到订单');
    } finally {
      setBusy(false);
    }
  };

  const buildPayload = (): RefundWorkOrderPayload => {
    if (!preview?.order?.id) throw new Error('请先查询订单');
    return {
      source_order_id: preview.order.id,
      refund_reason_category: reasonCategory,
      refund_reason_detail: processNote,
      refund_method: refundMethod,
      requested_refund_cents: yuanToCents(refundAmount) || preview.impact.estimated_refund_cents,
      customer_requested_at: customerRequestedAt ? new Date(customerRequestedAt).toISOString() : undefined,
      agent_confirmed_at: agentConfirmedAt ? new Date(agentConfirmedAt).toISOString() : undefined,
      process_note: processNote,
    };
  };

  const ensureDraft = async () => {
    if (currentWorkOrder?.id) return currentWorkOrder;
    const created = await createRefundWorkOrderDraft(buildPayload());
    setCurrentWorkOrder(created.work_order);
    return created.work_order;
  };

  const uploadPending = async (workOrderId: number) => {
    let latest: RefundWorkOrder | null = null;
    for (const item of pendingFiles) {
      const res = await uploadRefundAttachment(workOrderId, item.file, item.evidence_type);
      latest = res.work_order;
    }
    if (latest) setCurrentWorkOrder(latest);
    setPendingFiles([]);
    return latest;
  };

  const handleSaveDraft = async () => {
    if (!preview) {
      toast.error('请先查询订单');
      return;
    }
    setBusy(true);
    try {
      const draft = await ensureDraft();
      if (pendingFiles.length) await uploadPending(draft.id);
      toast.success('草稿已保存');
    } catch (error: any) {
      toast.error(error.message || '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const hasEvidence = (currentWorkOrder?.attachments?.length || 0) + pendingFiles.length > 0;
  const canSubmit = Boolean(preview && hasEvidence && confirmAmount && confirmAgent && confirmEvidence && confirmPayout);

  const handleSubmit = async () => {
    if (!preview) {
      toast.error('请先查询订单');
      return;
    }
    if (!hasEvidence) {
      toast.error('请先上传至少 1 个退款凭证');
      return;
    }
    if (!confirmAmount || !confirmAgent || !confirmEvidence || !confirmPayout) {
      toast.error('请完成确认项');
      return;
    }
    setBusy(true);
    try {
      const draft = await ensureDraft();
      const latest = pendingFiles.length ? await uploadPending(draft.id) : draft;
      const submitted = await submitRefundWorkOrder((latest || draft).id, processNote);
      setCurrentWorkOrder(submitted.work_order);
      toast.success('已提交退款审核');
      setActiveTab('submitted');
    } catch (error: any) {
      toast.error(error.message || '提交失败');
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    if (activeTab === 'start') {
      if (query.trim()) await handlePreview();
      return;
    }
    await loadList(activeTab);
  };

  const handleApprove = async (item: RefundWorkOrder) => {
    setBusy(true);
    try {
      const res = await approveRefundWorkOrder(item.id, '审核通过');
      setCurrentWorkOrder(res.work_order);
      toast.success('工单已审核通过');
      await loadList(activeTab);
    } catch (error: any) {
      toast.error(error.message || '审核失败');
    } finally {
      setBusy(false);
    }
  };

  const handleExecute = async (item: RefundWorkOrder) => {
    setExecuteTarget(item);
  };

  const confirmExecute = async () => {
    if (!executeTarget) return;
    setBusy(true);
    try {
      const res = await executeRefundWorkOrder(executeTarget.id, '管理员二次确认执行系统冲账');
      setCurrentWorkOrder(res.work_order);
      toast.success('系统冲账已执行');
      setExecuteTarget(null);
      setActiveTab(res.work_order.status === 'completed' ? 'completed' : 'payout_pending');
    } catch (error: any) {
      toast.error(error.message || '系统冲账失败');
    } finally {
      setBusy(false);
    }
  };

  const handleComplete = async (item: RefundWorkOrder) => {
    setBusy(true);
    try {
      const res = await completeRefundWorkOrder(item.id, '凭证已核对，标记完成');
      setCurrentWorkOrder(res.work_order);
      toast.success('退款工单已完成');
      setActiveTab('completed');
    } catch (error: any) {
      toast.error(error.message || '标记完成失败');
      setCurrentWorkOrder(item);
    } finally {
      setBusy(false);
    }
  };

  const selectWorkOrder = (item: RefundWorkOrder) => {
    setCurrentWorkOrder(item);
    const snap = item.impact_snapshot as RefundOrderPreview | undefined;
    if (snap?.order) setPreview(snap);
  };

  const resetNew = () => {
    setActiveTab('start');
    setPreview(null);
    setCurrentWorkOrder(null);
    setPendingFiles([]);
    setQuery('');
    setProcessNote('');
    setRefundAmount('');
    setConfirmAmount(false);
    setConfirmAgent(false);
    setConfirmEvidence(false);
    setConfirmPayout(false);
  };

  return (
    <div
      className="bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.10),transparent_34%),linear-gradient(180deg,rgba(15,23,42,0.18),transparent_22%)] px-4 py-5 md:px-6"
      data-api={WORK_ORDER_API_BASE}
      data-panels={PANEL_TITLES.join(',')}
    >
      <div className="mx-auto max-w-[1500px] space-y-4">
        <header className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-start gap-3">
            <div className="grid h-11 w-11 place-items-center rounded-lg border border-white/10 bg-background/70">
              <RotateCcw className="h-6 w-6 text-foreground" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-normal text-foreground">充值退款工单中心</h1>
              <p className="mt-1 text-sm text-muted-foreground">核对订单、留存证据、审核后处理退款</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={refresh} disabled={busy}>
              <RefreshCw className={cn('mr-2 h-4 w-4', busy && 'animate-spin')} />
              刷新
            </Button>
            <Button onClick={resetNew}>
              <Plus className="mr-2 h-4 w-4" />
              新建工单
            </Button>
          </div>
        </header>

        <div className="overflow-x-auto">
          <div className="inline-flex min-w-full rounded-lg border border-white/10 bg-card/65 p-1 md:min-w-0">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  'min-h-[38px] min-w-[104px] rounded-md px-4 text-sm font-medium text-muted-foreground transition',
                  activeTab === tab.key && 'bg-muted text-foreground shadow-sm',
                )}
              >
                {tab.label}
                {tab.key !== 'start' && (tab.key === 'cash_jobs' ? cashJobs.length : counts[tab.key]) ? (
                  <span className="ml-2 rounded-full bg-background/70 px-1.5 py-0.5 text-xs">
                    {tab.key === 'cash_jobs' ? cashJobs.length : counts[tab.key]}
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
          <div className="flex gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-300" />
            <span>退款会影响客户算力、服务商收益和人工打款，请先核对订单并上传凭证。</span>
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_460px]">
          <main className="space-y-4">
            {activeTab === 'start' ? (
              <StartWorkOrderPanel
                query={query}
                setQuery={setQuery}
                preview={preview}
                busy={busy}
                onPreview={handlePreview}
                reasonCategory={reasonCategory}
                setReasonCategory={setReasonCategory}
                refundMethod={refundMethod}
                setRefundMethod={setRefundMethod}
                refundAmount={refundAmount}
                setRefundAmount={setRefundAmount}
                customerRequestedAt={customerRequestedAt}
                setCustomerRequestedAt={setCustomerRequestedAt}
                agentConfirmedAt={agentConfirmedAt}
                setAgentConfirmedAt={setAgentConfirmedAt}
                processNote={processNote}
                setProcessNote={setProcessNote}
                pendingFiles={pendingFiles}
                setPendingFiles={setPendingFiles}
                currentWorkOrder={currentWorkOrder}
                setCurrentWorkOrder={setCurrentWorkOrder}
                confirmAmount={confirmAmount}
                setConfirmAmount={setConfirmAmount}
                confirmAgent={confirmAgent}
                setConfirmAgent={setConfirmAgent}
                confirmEvidence={confirmEvidence}
                setConfirmEvidence={setConfirmEvidence}
                confirmPayout={confirmPayout}
                setConfirmPayout={setConfirmPayout}
                onSaveDraft={handleSaveDraft}
                onSubmit={handleSubmit}
                canSubmit={canSubmit}
              />
            ) : activeTab === 'cash_jobs' ? (
              <RefundCashJobQueue
                items={cashJobs}
                attentionCount={cashJobAttentionCount}
                busy={busy}
                executingId={executingCashJobId}
                onRefresh={() => loadList('cash_jobs')}
                onExecute={handleExecuteCashJob}
              />
            ) : (
              <section className="rounded-lg border border-white/10 bg-card/70 p-4">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-lg font-semibold text-foreground">{tabs.find((tab) => tab.key === activeTab)?.label}</h2>
                    <p className="mt-1 text-sm text-muted-foreground">选择工单后可在右侧查看影响预览和时间线</p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => loadList(activeTab)} disabled={busy}>
                    <RefreshCw className={cn('mr-2 h-4 w-4', busy && 'animate-spin')} />
                    刷新列表
                  </Button>
                </div>
                <RefundWorkOrderList
                  items={items}
                  selectedId={currentWorkOrder?.id}
                  onSelect={selectWorkOrder}
                  onApprove={handleApprove}
                  onExecute={handleExecute}
                  onComplete={handleComplete}
                />
                {currentWorkOrder && currentWorkOrder.status === 'payout_pending' && (
                  <div className="mt-4 rounded-lg border border-white/10 bg-background/35 p-4">
                    <h3 className="mb-3 text-base font-semibold text-foreground">补传打款凭证</h3>
                    <EvidenceUploader
                      workOrder={currentWorkOrder}
                      pendingFiles={pendingFiles}
                      onPendingChange={setPendingFiles}
                      onWorkOrderChange={setCurrentWorkOrder}
                    />
                  </div>
                )}
              </section>
            )}
          </main>

          {activeTab === 'cash_jobs' ? (
            <aside className="rounded-lg border border-white/10 bg-card/70 p-4 text-sm text-muted-foreground">
              <h2 className="text-base font-semibold text-foreground">资金执行护栏</h2>
              <ul className="mt-3 list-disc space-y-2 pl-5">
                <li>退款金额来自持久工单的整数分，不读取页面输入。</li>
                <li>微信退款发起后必须主动查询或等待验签回调，才形成现金终态。</li>
                <li>虎皮椒部分退款转人工资金处理，不会误发整单全额。</li>
                <li>外部成功、内账失败会留下补偿证据；只能主动查询对账，禁止重复打款。</li>
              </ul>
            </aside>
          ) : (
            <RefundImpactPanel preview={selectedPreview} workOrder={currentWorkOrder} />
          )}
        </div>
      </div>

      <AlertDialog open={!!executeTarget} onOpenChange={(open) => !open && setExecuteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认执行系统冲账</AlertDialogTitle>
            <AlertDialogDescription>
              这一步会扣减客户未用算力，并处理服务商收益影响。请确认订单、证据和审核记录都已核对。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={confirmExecute}>确认执行</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

const cashJobStatusLabels: Record<RefundCashJob['status'], string> = {
  queued: '待执行',
  provider_processing: '渠道处理中',
  completed: '已完成',
  failed: '可重试',
  manual_review: '人工资金处理',
};

function RefundCashJobQueue({
  items,
  attentionCount,
  busy,
  executingId,
  onRefresh,
  onExecute,
}: {
  items: RefundCashJob[];
  attentionCount: number;
  busy: boolean;
  executingId: string | null;
  onRefresh: () => void;
  onExecute: (job: RefundCashJob) => void;
}) {
  return (
    <section className="rounded-lg border border-white/10 bg-card/70 p-4">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">原支付路径退款队列</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            金额只读自持久工单；失败重试复用同一幂等键，人工输入不能改变退款金额。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={busy}>
          <RefreshCw className={cn('mr-2 h-4 w-4', busy && 'animate-spin')} />
          刷新队列
        </Button>
      </div>
      {attentionCount > 0 && (
        <div className="mb-4 rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
          有 {attentionCount} 笔工单需要执行、重试或主动查询。外部成功但内账待补偿的记录禁止重复打款。
        </div>
      )}
      <div className="space-y-3">
        {items.length === 0 && (
          <div className="rounded-md border border-dashed border-white/10 p-8 text-center text-sm text-muted-foreground">
            当前没有原路退款现金工单
          </div>
        )}
        {items.map((job) => {
          const manualWithoutProvider = job.status === 'manual_review' && !job.provider_refund_id;
          const canExecute = job.status !== 'completed' && !manualWithoutProvider;
          return (
            <div key={job.cash_job_id} className="rounded-md border border-white/10 bg-background/40 p-3">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0 space-y-1 text-sm">
                  <div className="font-medium text-foreground">订单 {job.source_order_id}</div>
                  <div className="text-muted-foreground">
                    应退 ¥{(job.amount_cents / 100).toFixed(2)} · {job.original_payment_route}
                    {' · '}尝试 {job.attempt_count} 次
                  </div>
                  <div className="text-xs text-muted-foreground">工单 {job.cash_job_id}</div>
                  {job.attention_reason && <div className="text-xs text-amber-200">{job.attention_reason}</div>}
                  {job.last_error && <div className="text-xs text-red-300">{job.last_error}</div>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="rounded-full border border-white/10 bg-background/70 px-2 py-1 text-xs text-muted-foreground">
                    {cashJobStatusLabels[job.status]}
                  </span>
                  {canExecute && (
                    <Button
                      size="sm"
                      onClick={() => onExecute(job)}
                      disabled={busy || executingId === job.cash_job_id}
                    >
                      <RefreshCw className={cn('mr-2 h-3.5 w-3.5', executingId === job.cash_job_id && 'animate-spin')} />
                      {job.status === 'failed'
                        ? '按原金额重试'
                        : job.status === 'provider_processing' || job.status === 'manual_review'
                          ? '主动查询对账'
                          : '执行原路退款'}
                    </Button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function StartWorkOrderPanel(props: {
  query: string;
  setQuery: (value: string) => void;
  preview: RefundOrderPreview | null;
  busy: boolean;
  onPreview: () => void;
  reasonCategory: string;
  setReasonCategory: (value: string) => void;
  refundMethod: RefundMethod;
  setRefundMethod: (value: RefundMethod) => void;
  refundAmount: string;
  setRefundAmount: (value: string) => void;
  customerRequestedAt: string;
  setCustomerRequestedAt: (value: string) => void;
  agentConfirmedAt: string;
  setAgentConfirmedAt: (value: string) => void;
  processNote: string;
  setProcessNote: (value: string) => void;
  pendingFiles: PendingEvidence[];
  setPendingFiles: (files: PendingEvidence[]) => void;
  currentWorkOrder: RefundWorkOrder | null;
  setCurrentWorkOrder: (workOrder: RefundWorkOrder) => void;
  confirmAmount: boolean;
  setConfirmAmount: (value: boolean) => void;
  confirmAgent: boolean;
  setConfirmAgent: (value: boolean) => void;
  confirmEvidence: boolean;
  setConfirmEvidence: (value: boolean) => void;
  confirmPayout: boolean;
  setConfirmPayout: (value: boolean) => void;
  onSaveDraft: () => void;
  onSubmit: () => void;
  canSubmit: boolean;
}) {
  const order = props.preview?.order;

  return (
    <section className="rounded-lg border border-white/10 bg-card/70 p-4 md:p-5">
      <div className="mb-5 flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">发起退款工单</h2>
          <p className="mt-1 text-sm text-muted-foreground">查询订单、核对影响、上传凭证后提交审核</p>
        </div>
        <div className="flex items-center gap-2 overflow-x-auto pb-1">
          {[
            ['1', '查订单'],
            ['2', '核影响'],
            ['3', '留证据'],
            ['4', '提交审核'],
          ].map(([index, label], i) => (
            <div key={index} className="flex items-center gap-2">
              <span className={cn(
                'grid h-8 w-8 place-items-center rounded-full border text-sm font-semibold',
                i === 0 || props.preview ? 'border-emerald-400/45 bg-emerald-500/12 text-emerald-200' : 'border-white/10 text-muted-foreground',
              )}>
                {index}
              </span>
              <span className="whitespace-nowrap text-sm text-muted-foreground">{label}</span>
              {i < 3 && <ChevronRight className="h-4 w-4 text-muted-foreground" />}
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-4">
        <StepBlock index="1" title="订单查询" icon={FileSearch}>
          <div className="flex flex-col gap-3 md:flex-row">
            <Input
              value={props.query}
              onChange={(event) => props.setQuery(event.target.value)}
              placeholder="订单号 / 手机号 / 客户名"
              className="h-11 border-white/10 bg-background/60"
            />
            <Button className="h-11 md:w-36" onClick={props.onPreview} disabled={props.busy}>
              查询订单
            </Button>
          </div>
          {order && (
            <div className="mt-3 grid gap-2 rounded-md border border-white/10 bg-background/45 p-3 text-sm text-muted-foreground md:grid-cols-2 xl:grid-cols-4">
              <Info label="客户" value={order.customer_name || '-'} />
              <Info label="服务商" value={order.agent_name || '-'} />
              <Info label="订单金额" value={`¥${((order.amount_cents || 0) / 100).toFixed(2)}`} />
              <Info label="支付时间" value={(order.paid_at || '-').slice(0, 16).replace('T', ' ')} />
              <Info label="订单状态" value={order.payment_status === 'paid' ? '已支付' : order.payment_status || '-'} />
              <Info label="支付方式" value={order.payment_method || '-'} />
              <Info label="订单类型" value={order.order_type || '-'} />
              <Info label="退款状态" value={order.refund_status || '无'} />
            </div>
          )}
        </StepBlock>

        <StepBlock index="2" title="退款信息" icon={ClipboardCheck}>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="退款原因分类" required>
              <Select value={props.reasonCategory} onValueChange={props.setReasonCategory}>
                <SelectTrigger className="h-11 border-white/10 bg-background/60">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {reasonOptions.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label="退款方式" required>
              <Select value={props.refundMethod} onValueChange={(value) => props.setRefundMethod(value as RefundMethod)}>
                <SelectTrigger className="h-11 border-white/10 bg-background/60">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {methodOptions.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label="预计退款金额">
              <Input
                value={props.refundAmount}
                onChange={(event) => props.setRefundAmount(event.target.value)}
                placeholder="0.00"
                inputMode="decimal"
                className="h-11 border-white/10 bg-background/60"
              />
            </Field>
            <Field label="客户申请时间" required>
              <Input
                type="datetime-local"
                value={props.customerRequestedAt}
                onChange={(event) => props.setCustomerRequestedAt(event.target.value)}
                className="h-11 border-white/10 bg-background/60"
              />
            </Field>
            <Field label="服务商确认时间" required>
              <Input
                type="datetime-local"
                value={props.agentConfirmedAt}
                onChange={(event) => props.setAgentConfirmedAt(event.target.value)}
                className="h-11 border-white/10 bg-background/60"
              />
            </Field>
            <div className="md:col-span-2">
              <Field label="退款原因与处理说明" required>
                <Textarea
                  value={props.processNote}
                  onChange={(event) => props.setProcessNote(event.target.value)}
                  maxLength={500}
                  placeholder="请输入退款原因、核实过程、协商结果等"
                  className="min-h-[96px] resize-none border-white/10 bg-background/60"
                />
                <div className="mt-1 text-right text-xs text-muted-foreground">{props.processNote.length} / 500</div>
              </Field>
            </div>
          </div>
        </StepBlock>

        <StepBlock index="3" title="证据材料" icon={ShieldCheck}>
          <EvidenceUploader
            workOrder={props.currentWorkOrder}
            pendingFiles={props.pendingFiles}
            onPendingChange={props.setPendingFiles}
            onWorkOrderChange={props.setCurrentWorkOrder}
            disabled={!props.preview}
          />
        </StepBlock>

        <StepBlock index="4" title="确认与提交" icon={Check}>
          <div className="grid gap-3 md:grid-cols-2">
            <ConfirmRow checked={props.confirmAmount} onChange={props.setConfirmAmount} label="已核对退款金额和算力扣减" />
            <ConfirmRow checked={props.confirmAgent} onChange={props.setConfirmAgent} label="已取得服务商确认，并已上传沟通凭证" />
            <ConfirmRow checked={props.confirmEvidence} onChange={props.setConfirmEvidence} label="已上传必要凭证" />
            <ConfirmRow checked={props.confirmPayout} onChange={props.setConfirmPayout} label="如需人工打款，完成后补传打款凭证" />
          </div>
          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={props.onSaveDraft} disabled={props.busy || !props.preview}>
              保存草稿
            </Button>
            <Button onClick={props.onSubmit} disabled={props.busy || !props.canSubmit} className="min-w-40">
              提交退款审核
            </Button>
          </div>
        </StepBlock>
      </div>
    </section>
  );
}

function StepBlock({
  index,
  title,
  icon: Icon,
  children,
}: {
  index: string;
  title: string;
  icon: ComponentType<{ className?: string }>;
  children: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-white/10 bg-background/30 p-4">
      <div className="mb-4 flex items-center gap-3">
        <span className="grid h-8 w-8 place-items-center rounded-full bg-foreground text-sm font-semibold text-background">{index}</span>
        <Icon className="h-5 w-5 text-emerald-300" />
        <h3 className="text-base font-semibold text-foreground">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function Field({ label, required, children }: { label: string; required?: boolean; children: ReactNode }) {
  return (
    <div>
      <Label className="mb-2 block text-sm text-muted-foreground">
        {label}{required && <span className="ml-1 text-amber-300">*</span>}
      </Label>
      {children}
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <p className="truncate text-sm font-medium text-foreground">{value}</p>
    </div>
  );
}

function ConfirmRow({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return (
    <label className="flex min-h-[44px] cursor-pointer items-center gap-3 rounded-md border border-white/10 bg-background/45 px-3 py-2 text-sm text-muted-foreground">
      <Checkbox checked={checked} onCheckedChange={(value) => onChange(Boolean(value))} />
      <span>{label}</span>
    </label>
  );
}
