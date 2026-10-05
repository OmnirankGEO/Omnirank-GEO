/**
 * DiagnosisFundExceptions -- Admin 诊断资金异常处置台(WORKERS=4 · 返工4 P2)
 *
 * URL: /admin/diagnosis-fund-exceptions
 *
 * 两队列 + 证据 + 操作历史 + 受控按钮(替代"运营靠手工调 API"):
 *  1) 补发/退款处置(delivery_repair_pending):钱已扣但无产物 →
 *     - republish(产物已恢复→补发)/ writeoff(不可恢复→退款待确认)/
 *       confirm_refund(确认退款完成·强制真实退款流水编号+核对说明)
 *  2) 结算人工处置(settlement_manual):资金记录需人工核对 → 正常/重复记录逐笔处置
 *  每行可展开看**持久审计流水**(操作历史);顶部资金告警条。
 *  🔴 所有动作走后端受控端点(admin-only · operator 取认证上下文 · 全程审计)· 前端不碰余额。
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Loader2, RefreshCw, AlertTriangle, PackageCheck, Undo2, CheckCircle2, History, ChevronDown, Wrench,
} from 'lucide-react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';

// ---------- types ----------
interface V35RefundPreview {
  owner_user_id: number;
  freeze_id: number;
  total: number;
  tool: number;
  publish: number;
  bonus: number;
  freeze_status: string;
}
interface DeliveryRepairItem {
  run_token: string;
  session_id: string;
  owner_user_id: number;
  brand_id: number | null;
  billing_mode: string;
  run_status: string;
  last_settlement_error?: string | null;
  status_changed_at?: string;
  created_at?: string;
  has_product: boolean;
  resolved: boolean;
  refund_pending: boolean;
  repair_stage: 'awaiting' | 'refund_pending' | 'resolved';
  // [P1-2] v35 精确退款:内部类型区分系统执行与财务流程确认。
  freeze_backend?: string | null;
  v35_refund_preview?: V35RefundPreview | null;
  can_execute_v35_refund?: boolean;
  bonus_refund_requires_manual?: boolean;
}

interface FreezeLoc { backend: string; freeze_id: number; status?: string; }
interface SettlementManualItem {
  run_token: string;
  session_id: string;
  owner_user_id: number;
  brand_id: number | null;
  freeze_task_ref: string;
  run_status: string;
  settlement_attempts: number;
  last_settlement_error?: string | null;
  status_changed_at?: string;
  locate?: FreezeLoc;
  all_freezes?: FreezeLoc[];
  double_frozen?: boolean;
  // [返工5 P2] manual_resolving(处置租约在途/过期待接管)只读展示 · 禁直接 resolve
  is_resolving?: boolean;
  resolvable?: boolean;
  lease_state?: 'processing' | 'expired_await_takeover';
  manual_lease_until?: string | null;
}

interface AuditRow { id: number; operator: string; action: string; detail: string; created_at: string; }

// ---------- helpers ----------
function fmtTime(iso?: string): string {
  return iso?.slice(0, 19)?.replace('T', ' ') || '--';
}

function bonusRefundRequiresManual(item: DeliveryRepairItem): boolean {
  return item.bonus_refund_requires_manual === true
    || Number(item.v35_refund_preview?.bonus || 0) > 0;
}

function canExecuteV35Refund(item: DeliveryRepairItem): boolean {
  return item.billing_mode === 'paid'
    && item.run_status === 'delivery_repair_pending'
    && item.freeze_backend === 'v35'
    && item.repair_stage === 'refund_pending'
    && item.v35_refund_preview?.freeze_status === 'committed'
    && Number(item.v35_refund_preview?.bonus || 0) === 0
    && item.can_execute_v35_refund === true;
}

function canConfirmManualV35BonusRefund(item: DeliveryRepairItem): boolean {
  return item.billing_mode === 'paid'
    && item.run_status === 'delivery_repair_pending'
    && item.freeze_backend === 'v35'
    && item.repair_stage === 'refund_pending'
    && item.v35_refund_preview?.freeze_status === 'committed'
    && Number(item.v35_refund_preview?.bonus || 0) > 0
    && item.can_execute_v35_refund === false;
}

function fundAccountLabel(backend?: string): string {
  return backend === 'v35' ? '客户算力账户' : '标准算力账户';
}

function fundRecordStatusLabel(status?: string): string {
  if (status === 'committed') return '已扣费';
  if (status === 'released') return '已退款';
  if (status === 'frozen') return '待结算';
  return '状态待核对';
}

function auditActionLabel(action: string): string {
  const labels: Record<string, string> = {
    delivery_v35_refund_executed: '已执行算力退款',
    delivery_refund_confirmed: '已确认退款完成',
    delivery_writeoff_intent: '已发起退款核对',
    delivery_republish: '已补发报告',
    manual_resolve_single: '已处理单笔资金异常',
    manual_resolve_double: '已处理重复资金记录',
    dup_release: '已退回重复扣费',
    auto_to_manual: '已转人工核对',
  };
  return labels[action] || '资金操作记录';
}

function alertKindLabel(kind?: string): string {
  const labels: Record<string, string> = {
    delivery_refund_pending: '诊断退款待核对',
    settlement_stuck: '结算状态待核对',
    duplicate_freeze: '重复资金记录',
    delivery_missing: '交付记录待核对',
  };
  return (kind && labels[kind]) || '资金异常';
}

function humanizeAuditDetail(detail: string): string {
  return detail
    .replace(/\brun_token\b/gi, '任务编号')
    .replace(/\brun\b/gi, '任务')
    .replace(/\b(?:freeze_)?task_ref\b/gi, '任务资金编号')
    .replace(/\brefund_tx(?:_ref|_id)?\b/gi, '退款流水编号')
    .replace(/\b(?:freeze|other)_backend\b/gi, '账户类型')
    .replace(/\bbackend\b/gi, '账户类型')
    .replace(/\bfreeze_id\b/gi, '资金记录编号')
    .replace(/\bowner(?:_user_id)?\b/gi, '客户编号')
    .replace(/\bkeeper\b/gi, '保留记录')
    .replace(/\bdup_released\b/gi, '重复记录已退款')
    .replace(/\bdecision\b/gi, '处理方式')
    .replace(/\bCAS\b/gi, '状态确认')
    .replace(/\bledger\b/gi, '资金流水')
    .replace(/\bidempotent\b/gi, '重复请求保护')
    .replace(/\bops_resolved\b/gi, '已处理')
    .replace(/\brefund_pending\b/gi, '退款待核对')
    .replace(/\bpool_split\b/gi, '退款明细')
    .replace(/\bpool\b/gi, '资金明细')
    .replace(/\bsource\b/gi, '来源')
    .replace(/\blegacy\b/gi, '标准算力账户')
    .replace(/\bv35\b/gi, '客户算力账户')
    .replace(/\btool\b/gi, '工具算力')
    .replace(/\bpublish\b/gi, '发布算力')
    .replace(/\bbonus\b/gi, '赠送算力')
    .replace(/\brelease\b/gi, '退款')
    .replace(/\bcommit\b/gi, '交付结算');
}

const ACTION_ERROR_MESSAGES: Record<string, string> = {
  BONUS_REFUND_REQUIRES_MANUAL: '本次诊断包含赠送算力，需人工核对退款',
  V35_AGENT_OWNERSHIP_MISMATCH: '客户算力账户的服务商归属信息不一致，退款确认未完成，请人工核对',
  V35_CUSTOMER_OWNERSHIP_MISMATCH: '资金记录的客户归属信息不一致，退款确认未完成，请人工核对',
  V35_REFUND_FREEZE_NOT_COMMITTED: '本次诊断的扣费状态待核对，退款确认未完成',
  REFUND_CONFIRM_REQUIRES_PAID: '本次诊断没有已提交的付费扣款，无法确认退款完成',
  INVALID_CONSUME_LEDGER_SIGN: '扣费记录金额异常，退款确认未完成，请人工核对',
  INVALID_CONSUME_LEDGER_POOL: '扣费算力类型异常，退款确认未完成，请人工核对',
  INVALID_REFUND_LEDGER_SIGN: '退款记录金额异常，退款确认未完成，请人工核对',
  INVALID_REFUND_LEDGER_POOL: '退款算力类型异常，退款确认未完成，请人工核对',
  RUN_FREEZE_TASK_REF_MISMATCH: '任务与资金记录不一致，退款确认未完成，请人工核对',
  EXISTING_REFUND_REQUIRES_MANUAL: '已存在关联退款记录，为防止重复退款，请人工核对',
  V35_REFUND_TRANSACTION_FAILED: '自动退款执行失败且已回滚，请稍后重试或人工核对',
  V35_REFUND_VERIFICATION_FAILED: '退款记录核对未通过，自动退款已回滚，请人工核对',
  V35_REFUND_REJECTED: '自动退款未执行，请人工核对',
};

function operatorSafeActionError(detail: unknown): string {
  if (Array.isArray(detail)) {
    return detail.map((item) => (
      item && typeof item === 'object' ? String((item as { msg?: unknown }).msg || '') : String(item)
    )).filter(Boolean).join('; ') || '输入校验失败';
  }
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === 'string' && ACTION_ERROR_MESSAGES[code]) return ACTION_ERROR_MESSAGES[code];
    // 结构化服务端 message 可能含内部字段、状态机或账本术语；未知码一律转人工友好文案。
    return '操作未完成，请核对资金记录后重试或转人工处理';
  }
  // 普通字符串同样可能是底层异常详情，不直接展示给运营。
  return '操作未完成，请核对资金记录后重试或转人工处理';
}

const STAGE_BADGE: Record<DeliveryRepairItem['repair_stage'], { label: string; cls: string }> = {
  awaiting:       { label: '待处置',   cls: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
  refund_pending: { label: '退款待确认', cls: 'bg-orange-500/15 text-orange-400 border-orange-500/20' },
  resolved:       { label: '已核销',   cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20' },
};

// ---------- component ----------
export default function DiagnosisFundExceptions() {
  const [tab, setTab] = useState<'delivery' | 'settlement'>('delivery');
  const [delivery, setDelivery] = useState<DeliveryRepairItem[]>([]);
  const [settlement, setSettlement] = useState<SettlementManualItem[]>([]);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  // [返工5 P1] 已核销历史(与活跃队列分离 · 真分页)
  const HISTORY_PAGE = 20;
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<DeliveryRepairItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyHasMore, setHistoryHasMore] = useState(false);

  // dialogs
  const [repairDialog, setRepairDialog] = useState<{ item: DeliveryRepairItem; decision: string } | null>(null);
  const [note, setNote] = useState('');
  const [refundTxId, setRefundTxId] = useState('');   // [返工5 复审二] confirm_refund 引用真实退款流水编号。
  // [P1-2] v35 精确退款执行 dialog(系统原路退回 · 无需手填流水 id)
  const [v35RefundDialog, setV35RefundDialog] = useState<DeliveryRepairItem | null>(null);
  const [v35Note, setV35Note] = useState('');
  const [resolveDialog, setResolveDialog] = useState<SettlementManualItem | null>(null);
  const [rBackend, setRBackend] = useState('legacy');
  const [rFreezeId, setRFreezeId] = useState('');
  const [rDecision, setRDecision] = useState('release');
  const [rNote, setRNote] = useState('');
  const [rIsDouble, setRIsDouble] = useState(false);
  const [rOtherBackend, setROtherBackend] = useState('v35');
  const [rOtherFreezeId, setROtherFreezeId] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [dResp, sResp, aResp] = await Promise.all([
        authFetch('/api/admin/diagnosis-runs/delivery-repair?limit=100'),
        authFetch('/api/admin/diagnosis-runs/settlement-manual?limit=100'),
        authFetch('/api/admin/diagnosis-runs/settlement-alerts?limit=50'),
      ]);
      if (dResp.ok) { const d = await dResp.json(); setDelivery(d.runs || []); }
      if (sResp.ok) { const s = await sResp.json(); setSettlement(s.runs || []); }
      if (aResp.ok) { const a = await aResp.json(); setAlerts(a.alerts || []); }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const toggleAudit = async (runToken: string) => {
    if (expanded === runToken) { setExpanded(null); return; }
    setExpanded(runToken);
    setAudit([]);
    setAuditLoading(true);
    try {
      const resp = await authFetch(`/api/admin/diagnosis-runs/${runToken}/audit?limit=100`);
      if (resp.ok) { const d = await resp.json(); setAudit(d.audit || []); }
    } finally {
      setAuditLoading(false);
    }
  };

  const doAction = async (url: string, body: Record<string, unknown>) => {
    setSubmitting(true);
    setActionError(null);
    try {
      const resp = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        // [返工4 修复净增量] FastAPI 422 的 detail 是数组 [{loc,msg,type}],直接渲染会 React 崩溃(白屏)· 归一化成字符串
        const detail = (err as { detail?: unknown })?.detail;
        setActionError(operatorSafeActionError(detail));
        return false;
      }
      return true;
    } catch (e) {
      setActionError('网络连接异常，请稍后重试');
      return false;
    } finally {
      setSubmitting(false);
    }
  };

  const loadHistory = useCallback(async (offset: number) => {
    setHistoryLoading(true);
    try {
      // 多取 1 条探测是否还有下一页(hasMore),展示只取 HISTORY_PAGE 条
      const resp = await authFetch(`/api/admin/diagnosis-runs/delivery-repair?history=true&limit=${HISTORY_PAGE + 1}&offset=${offset}`);
      if (resp.ok) {
        const d = await resp.json();
        const rows: DeliveryRepairItem[] = d.runs || [];
        setHistoryHasMore(rows.length > HISTORY_PAGE);
        setHistory(rows.slice(0, HISTORY_PAGE));
        setHistoryOffset(offset);
      }
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const toggleHistory = () => {
    const next = !showHistory;
    setShowHistory(next);
    if (next && history.length === 0) loadHistory(0);
  };

  const openRepair = (item: DeliveryRepairItem, decision: string) => {
    setActionError(null); setNote(''); setRefundTxId('');
    setRepairDialog({ item, decision });
  };

  // [P1-2] v35 精确退款:仅工具/发布池可自动退；赠送池必须人工核对
  const openV35Refund = (item: DeliveryRepairItem) => {
    if (!canExecuteV35Refund(item)) {
      setActionError(
        bonusRefundRequiresManual(item)
          ? '本次扣费包含赠送算力，需人工核对退款'
          : '当前记录不满足自动退款条件，请人工核对',
      );
      return;
    }
    setActionError(null); setV35Note('');
    setV35RefundDialog(item);
  };

  const submitV35Refund = async () => {
    if (!v35RefundDialog) return;
    if (!canExecuteV35Refund(v35RefundDialog)) {
      setActionError(
        bonusRefundRequiresManual(v35RefundDialog)
          ? '本次扣费包含赠送算力，需人工核对退款'
          : '当前记录不满足自动退款条件，请刷新后人工核对',
      );
      return;
    }
    // [round1 P3] 与后端 refund_and_confirm_v35_delivery 的 note ≥ 4 字对齐(前端预挡 · 免一次 400 往返)
    if (v35Note.trim().length < 4) { setActionError('请填写核对说明/证据(≥ 4 字)'); return; }
    const ok = await doAction(`/api/admin/diagnosis-runs/${v35RefundDialog.run_token}/execute-v35-refund`, { note: v35Note.trim() });
    if (ok) { setV35RefundDialog(null); refresh(); }
  };

  const submitRepair = async () => {
    if (!repairDialog) return;
    const { item, decision } = repairDialog;
    if (decision === 'confirm_refund' && note.trim().length < 4) {
      setActionError('请填写退款核对说明 / 证据（至少 4 个字）'); return;
    }
    if (note.trim().length < 2) { setActionError('请填写核对说明/证据'); return; }
    const body: Record<string, unknown> = { decision, note: note.trim() };
    if (decision === 'confirm_refund') {
      // [返工5 复审二] 引用真实退款流水编号(整数)· 服务端只读核验任务绑定/逐资金池/金额。
      if (!refundTxId.trim() || !Number.isInteger(Number(refundTxId)) || Number(refundTxId) <= 0) {
        setActionError('请填写退款流水编号（由财务退款流程生成，需为正整数）'); return;
      }
      body.refund_tx_id = refundTxId.trim();
    }
    const ok = await doAction(`/api/admin/diagnosis-runs/${item.run_token}/repair`, body);
    if (ok) { setRepairDialog(null); refresh(); }
  };

  const openResolve = (item: SettlementManualItem) => {
    setActionError(null);
    const dbl = !!item.double_frozen;
    setRIsDouble(dbl);
    // 预填重复资金记录定位现场
    const legacy = (item.all_freezes || []).find(f => f.backend === 'legacy');
    const v35 = (item.all_freezes || []).find(f => f.backend === 'v35');
    setRBackend(legacy ? 'legacy' : (item.locate?.backend || 'legacy'));
    setRFreezeId(legacy ? String(legacy.freeze_id) : (item.locate?.freeze_id ? String(item.locate.freeze_id) : ''));
    setRDecision('release');
    setROtherBackend('v35');
    setROtherFreezeId(v35 ? String(v35.freeze_id) : '');
    setRNote('');
    setResolveDialog(item);
  };

  const submitResolve = async () => {
    if (!resolveDialog) return;
    if (rNote.trim().length < 2) { setActionError('请填写核对结果/理由/证据'); return; }
    // 资金记录编号必须是整数；前端先挡，避免提交无效值
    if (!rFreezeId.trim() || !Number.isInteger(Number(rFreezeId))) { setActionError('资金记录编号必须是数字'); return; }
    const body: Record<string, unknown> = {
      backend: rBackend, freeze_id: Number(rFreezeId), decision: rDecision, note: rNote.trim(),
    };
    if (rIsDouble) {
      if (!rOtherFreezeId.trim() || !Number.isInteger(Number(rOtherFreezeId))) { setActionError('重复资金记录编号必须是数字'); return; }
      body.other_backend = rOtherBackend;
      body.other_freeze_id = Number(rOtherFreezeId);
    }
    const ok = await doAction(`/api/admin/diagnosis-runs/${resolveDialog.run_token}/resolve`, body);
    if (ok) { setResolveDialog(null); refresh(); }
  };

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-6xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Wrench className="h-5 w-5 text-amber-400" />
          <h1 className="text-lg font-semibold">诊断资金异常处置台</h1>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          <span className="ml-1">刷新</span>
        </Button>
      </div>

      {alerts.length > 0 && (
        <Card className="border-amber-500/30">
          <CardContent className="p-3">
            <div className="flex items-center gap-2 text-amber-400 text-sm font-medium mb-1">
              <AlertTriangle className="h-4 w-4" /> 资金告警(最近 {alerts.length} 条)
            </div>
            <div className="max-h-28 overflow-y-auto text-xs text-muted-foreground space-y-0.5">
              {alerts.slice(0, 20).map((a, i) => (
                <div key={i} className="truncate">
                  <span className="text-amber-500">[{alertKindLabel(a.kind)}]</span> 任务 {a.run_token} · {humanizeAuditDetail(String(a.detail || ''))}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex gap-2 border-b">
        {([
          { k: 'delivery', label: `补发/退款处置 (${delivery.filter(d => d.repair_stage !== 'resolved').length})` },
          { k: 'settlement', label: `结算人工处置 (${settlement.length})` },
        ] as const).map(t => (
          <button key={t.k}
            className={`px-3 py-2 text-sm border-b-2 -mb-px ${tab === t.k ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground'}`}
            onClick={() => setTab(t.k)}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ---------- 补发/退款处置 ---------- */}
      {tab === 'delivery' && (
        <div className="space-y-2">
          {delivery.length === 0 && <div className="text-sm text-muted-foreground p-4 text-center">无待处置补发/退款异常</div>}
          {delivery.map(item => (
            <Card key={item.run_token}>
              <CardContent className="p-3">
                <div className="flex flex-wrap items-center gap-2 justify-between">
                  <div className="flex items-center gap-2 min-w-0">
                    <Badge variant="outline" className={STAGE_BADGE[item.repair_stage].cls}>{STAGE_BADGE[item.repair_stage].label}</Badge>
                    <span className="text-xs font-mono truncate">任务 {item.run_token}</span>
                    <span className="text-xs text-muted-foreground">客户 #{item.owner_user_id}{item.brand_id ? ` · 品牌 #${item.brand_id}` : ''}</span>
                    {item.has_product
                      ? <Badge variant="outline" className="bg-emerald-500/10 text-emerald-400 border-emerald-500/20 text-[10px]">产物已恢复</Badge>
                      : <Badge variant="outline" className="bg-red-500/10 text-red-400 border-red-500/20 text-[10px]">无完整产物</Badge>}
                  </div>
                  <div className="flex items-center gap-1.5">
                    {item.repair_stage === 'awaiting' && item.has_product && (
                      <Button size="sm" variant="outline" onClick={() => openRepair(item, 'republish')}>
                        <PackageCheck className="h-3.5 w-3.5 mr-1" />补发发布
                      </Button>
                    )}
                    {item.repair_stage === 'awaiting' && (
                      <Button size="sm" variant="outline" onClick={() => openRepair(item, 'writeoff')}>
                        <Undo2 className="h-3.5 w-3.5 mr-1" />核销走退款
                      </Button>
                    )}
                    {item.repair_stage === 'refund_pending' && item.freeze_backend === 'v35' && canExecuteV35Refund(item) && (
                      <Button size="sm" variant="outline" onClick={() => openV35Refund(item)}>
                        <CheckCircle2 className="h-3.5 w-3.5 mr-1" />执行并确认退款
                      </Button>
                    )}
                    {item.repair_stage === 'refund_pending' && item.freeze_backend === 'v35' && bonusRefundRequiresManual(item) && (
                      <Badge variant="outline" className="bg-orange-500/15 text-orange-400 border-orange-500/20">
                        包含赠送算力，需人工核对退款
                      </Badge>
                    )}
                    {canConfirmManualV35BonusRefund(item) && (
                      <Button size="sm" variant="outline" onClick={() => openRepair(item, 'confirm_refund')}>
                        <CheckCircle2 className="h-3.5 w-3.5 mr-1" />确认人工退款完成
                      </Button>
                    )}
                    {item.repair_stage === 'refund_pending' && item.freeze_backend === 'v35'
                      && !canExecuteV35Refund(item) && !bonusRefundRequiresManual(item) && (
                      <Badge variant="outline" className="bg-amber-500/15 text-amber-400 border-amber-500/20">
                        退款条件待人工核对
                      </Badge>
                    )}
                    {item.repair_stage === 'refund_pending' && item.freeze_backend !== 'v35' && (
                      <Button size="sm" variant="outline" onClick={() => openRepair(item, 'confirm_refund')}>
                        <CheckCircle2 className="h-3.5 w-3.5 mr-1" />确认退款完成
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => toggleAudit(item.run_token)}>
                      <History className="h-3.5 w-3.5" />
                      <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded === item.run_token ? 'rotate-180' : ''}`} />
                    </Button>
                  </div>
                </div>
                <div className="text-[11px] text-muted-foreground mt-1">
                  {fmtTime(item.status_changed_at)} · {
                    bonusRefundRequiresManual(item)
                      ? '本次扣费包含赠送算力，系统不会自动恢复，请人工核对原赠送算力及有效期'
                      : item.repair_stage === 'refund_pending'
                        ? '退款待核对'
                        : '钱已结算但产物异常'
                  }
                </div>
                {expanded === item.run_token && (
                  <AuditPanel loading={auditLoading} audit={audit} />
                )}
              </CardContent>
            </Card>
          ))}

          {/* [返工5 P1] 已核销历史(分页 · 与活跃队列分离 · 惰性加载 · 防 resolved 挤占活跃队列) */}
          <div className="pt-2">
            <button className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1" onClick={toggleHistory}>
              <History className="h-3.5 w-3.5" />
              {showHistory ? '收起已核销历史' : '查看已核销历史'}
              <ChevronDown className={`h-3.5 w-3.5 transition-transform ${showHistory ? 'rotate-180' : ''}`} />
            </button>
            {showHistory && (
              <div className="mt-2 space-y-1.5">
                {historyLoading && <Loader2 className="h-4 w-4 animate-spin" />}
                {!historyLoading && history.length === 0 && <div className="text-xs text-muted-foreground">无已核销记录</div>}
                {history.map(item => (
                  <div key={item.run_token} className="flex items-center gap-2 text-[11px] text-muted-foreground border rounded px-2 py-1.5">
                    <Badge variant="outline" className={STAGE_BADGE.resolved.cls}>{STAGE_BADGE.resolved.label}</Badge>
                    <span className="font-mono truncate">任务 {item.run_token}</span>
                    <span className="shrink-0">客户 #{item.owner_user_id}</span>
                    <span className="truncate">{fmtTime(item.status_changed_at)} · 退款处置已完成</span>
                  </div>
                ))}
                {/* [返工5 复审] 真分页:prev/next(offset 翻页 · 非固定 50 条) */}
                {(historyOffset > 0 || historyHasMore) && (
                  <div className="flex items-center gap-2 pt-1">
                    <Button size="sm" variant="outline" disabled={historyLoading || historyOffset === 0}
                      onClick={() => loadHistory(Math.max(0, historyOffset - HISTORY_PAGE))}>上一页</Button>
                    <span className="text-[11px] text-muted-foreground">第 {Math.floor(historyOffset / HISTORY_PAGE) + 1} 页</span>
                    <Button size="sm" variant="outline" disabled={historyLoading || !historyHasMore}
                      onClick={() => loadHistory(historyOffset + HISTORY_PAGE)}>下一页</Button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ---------- 结算人工处置 ---------- */}
      {tab === 'settlement' && (
        <div className="space-y-2">
          {settlement.length === 0 && <div className="text-sm text-muted-foreground p-4 text-center">无待人工结算</div>}
          {settlement.map(item => (
            <Card key={item.run_token}>
              <CardContent className="p-3">
                <div className="flex flex-wrap items-center gap-2 justify-between">
                  <div className="flex items-center gap-2 min-w-0">
                    {item.is_resolving
                      ? <Badge variant="outline" className="bg-blue-500/15 text-blue-400 border-blue-500/20">
                          {item.lease_state === 'processing' ? '处置处理中' : '处置超时，待接管'}
                        </Badge>
                      : item.double_frozen
                        ? <Badge variant="outline" className="bg-red-500/15 text-red-400 border-red-500/20">检测到重复资金记录</Badge>
                        : <Badge variant="outline" className="bg-amber-500/15 text-amber-400 border-amber-500/20">待核对</Badge>}
                    <span className="text-xs font-mono truncate">任务 {item.run_token}</span>
                    <span className="text-xs text-muted-foreground">客户 #{item.owner_user_id} · 已重试 {item.settlement_attempts} 次</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {item.is_resolving
                      ? <span className="text-[11px] text-muted-foreground px-2">处置中 · 禁重复操作</span>
                      : <Button size="sm" variant="outline" onClick={() => openResolve(item)}>处置</Button>}
                    <Button size="sm" variant="ghost" onClick={() => toggleAudit(item.run_token)}>
                      <History className="h-3.5 w-3.5" />
                      <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded === item.run_token ? 'rotate-180' : ''}`} />
                    </Button>
                  </div>
                </div>
                <div className="text-[11px] text-muted-foreground mt-1">
                  {fmtTime(item.status_changed_at)} · 资金状态需人工核对 · 相关记录:
                  {(item.all_freezes || []).map(f => ` ${fundAccountLabel(f.backend)} #${f.freeze_id}(${fundRecordStatusLabel(f.status)})`).join(' /') || ' 无'}
                </div>
                {expanded === item.run_token && (
                  <AuditPanel loading={auditLoading} audit={audit} />
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* ---------- 补发/退款 dialog ---------- */}
      <Dialog open={!!repairDialog} onOpenChange={(o) => !o && setRepairDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {repairDialog?.decision === 'republish' && '补发发布(产物已恢复)'}
              {repairDialog?.decision === 'writeoff' && '核销走退款(产物不可恢复)'}
              {repairDialog?.decision === 'confirm_refund'
                && (canConfirmManualV35BonusRefund(repairDialog.item) ? '确认人工退款完成' : '确认退款完成')}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            {repairDialog?.decision === 'writeoff' && (
              <p className="text-xs text-orange-400">核销后进入“退款待确认”且继续保留告警。请先通过财务退款流程完成退款，再回到这里确认。</p>
            )}
            {repairDialog?.decision === 'confirm_refund' && (
              <>
                <p className="text-xs text-emerald-400">须先通过财务退款流程完成实际退款，再填写生成的退款流水编号。系统将核对本任务、各算力池金额及该记录是否已被使用；没有有效退款记录时会保持“退款待确认”。</p>
                {canConfirmManualV35BonusRefund(repairDialog.item) && (
                  <p className="text-xs text-orange-400">原赠送算力来源和剩余有效期须已由财务人工核对；系统只确认已有退款记录，不会自动恢复或新建赠送算力。</p>
                )}
                <div className="space-y-1">
                  <Label className="text-xs">退款流水编号（必填 · 由财务退款流程生成）</Label>
                  <Input value={refundTxId} onChange={e => setRefundTxId(e.target.value)} placeholder="退款流水编号（正整数）" inputMode="numeric" maxLength={40} />
                </div>
              </>
            )}
            <div className="space-y-1">
              <Label className="text-xs">核对说明 / 证据{repairDialog?.decision === 'confirm_refund' ? '(退款凭据)' : ''}</Label>
              <Input value={note} onChange={e => setNote(e.target.value)} placeholder="核对的业务结果 / 证据" maxLength={2000} />
            </div>
            {actionError && <p className="text-xs text-red-400">{actionError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRepairDialog(null)} disabled={submitting}>取消</Button>
            <Button onClick={submitRepair} disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---------- [P1-2] v35 精确退款执行 dialog ---------- */}
      <Dialog open={!!v35RefundDialog} onOpenChange={(o) => !o && setV35RefundDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>执行并确认退款</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-xs text-emerald-400">
              系统将按已核对的扣费记录退回<b>工具 / 发布算力</b>到客户钱包，并同步完成退款确认。赠送算力不支持自动退款。
            </p>
            {v35RefundDialog?.v35_refund_preview ? (
              <div className="rounded-md border p-2 text-xs space-y-1">
                <div>客户 #{v35RefundDialog.v35_refund_preview.owner_user_id} · 资金记录 #{v35RefundDialog.v35_refund_preview.freeze_id}</div>
                <div>退款总额:<span className="text-emerald-400 font-medium">{v35RefundDialog.v35_refund_preview.total}</span> 算力</div>
                <div className="text-muted-foreground">
                  退回明细:工具 {v35RefundDialog.v35_refund_preview.tool} · 发布 {v35RefundDialog.v35_refund_preview.publish}
                </div>
                {v35RefundDialog.v35_refund_preview.bonus > 0 && (
                  <div className="text-orange-400">包含赠送算力 {v35RefundDialog.v35_refund_preview.bonus}，需人工核对退款</div>
                )}
                {!canExecuteV35Refund(v35RefundDialog) && (
                  <div className="text-red-400">当前记录不满足自动退款条件，请人工核对</div>
                )}
              </div>
            ) : (
              <div className="text-xs text-red-400">无法预览资金明细，请转人工核对</div>
            )}
            <div className="space-y-1">
              <Label className="text-xs">核对说明 / 证据</Label>
              <Input value={v35Note} onChange={e => setV35Note(e.target.value)} placeholder="核对结果 / 理由 / 证据" maxLength={2000} />
            </div>
            {actionError && <p className="text-xs text-red-400">{actionError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setV35RefundDialog(null)} disabled={submitting}>取消</Button>
            <Button onClick={submitV35Refund} disabled={submitting || !v35RefundDialog || !canExecuteV35Refund(v35RefundDialog)}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}执行退款
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---------- 结算处置 dialog ---------- */}
      <Dialog open={!!resolveDialog} onOpenChange={(o) => !o && setResolveDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>结算人工处置{rIsDouble ? '（重复资金记录逐笔核对）' : ''}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1">
                <Label className="text-xs">{rIsDouble ? '保留记录的' : ''}账户类型</Label>
                <select className="w-full h-9 rounded-md border bg-background px-2 text-sm" value={rBackend} onChange={e => setRBackend(e.target.value)}>
                  <option value="legacy">标准算力账户</option>
                  <option value="v35">客户算力账户</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">资金记录编号</Label>
                <Input value={rFreezeId} onChange={e => setRFreezeId(e.target.value)} placeholder="请输入记录编号" inputMode="numeric" maxLength={18} />
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">{rIsDouble ? '保留记录的' : ''}处理方式</Label>
              <select className="w-full h-9 rounded-md border bg-background px-2 text-sm" value={rDecision} onChange={e => setRDecision(e.target.value)}>
                <option value="release">退款</option>
                <option value="commit">确认交付并结算</option>
              </select>
            </div>
            {rIsDouble && (
              <div className="grid grid-cols-2 gap-2 border-t pt-2">
                <div className="space-y-1">
                  <Label className="text-xs">重复记录的账户类型(始终退款)</Label>
                  <select className="w-full h-9 rounded-md border bg-background px-2 text-sm" value={rOtherBackend} onChange={e => setROtherBackend(e.target.value)}>
                    <option value="v35">客户算力账户</option>
                    <option value="legacy">标准算力账户</option>
                  </select>
                </div>
                <div className="space-y-1">
                  <Label className="text-xs">重复资金记录编号</Label>
                  <Input value={rOtherFreezeId} onChange={e => setROtherFreezeId(e.target.value)} placeholder="请输入重复记录编号" inputMode="numeric" maxLength={18} />
                </div>
              </div>
            )}
            <div className="space-y-1">
              <Label className="text-xs">核对结果 / 理由 / 证据</Label>
              <Input value={rNote} onChange={e => setRNote(e.target.value)} placeholder="核对结果 / 理由 / 证据" maxLength={2000} />
            </div>
            {actionError && <p className="text-xs text-red-400">{actionError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResolveDialog(null)} disabled={submitting}>取消</Button>
            <Button onClick={submitResolve} disabled={submitting}>
              {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}提交处置
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AuditPanel({ loading, audit }: { loading: boolean; audit: AuditRow[] }) {
  return (
    <div className="mt-2 border-t pt-2">
      <div className="text-[11px] text-muted-foreground font-medium mb-1">操作历史</div>
      {loading && <Loader2 className="h-4 w-4 animate-spin" />}
      {!loading && audit.length === 0 && <div className="text-[11px] text-muted-foreground">无审计记录</div>}
      {!loading && audit.map(a => (
        <div key={a.id} className="text-[11px] text-muted-foreground flex gap-2">
          <span className="text-muted-foreground/70 shrink-0">{fmtTime(a.created_at)}</span>
          <span className="text-blue-400 shrink-0">{auditActionLabel(a.action)}</span>
          <span className="shrink-0">{a.operator}</span>
          <span className="truncate">{humanizeAuditDetail(a.detail)}</span>
        </div>
      ))}
    </div>
  );
}
