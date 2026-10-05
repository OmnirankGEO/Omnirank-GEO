import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, CheckCircle2, Loader2, RefreshCw } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

type Strategy = 'transfer_upstream' | 'platform_managed' | 'settle_then_downgrade';

/** 降级后，原上游的商业归属去向。必须由管理员显式选，不给静默默认值。 */
type UpstreamDestination = 'rebind_upstream' | 'declare_released';

interface OutstandingItem {
  key: string;
  label: string;
  unit: 'rows' | 'points' | 'cents';
  value: number;
}

interface Snapshot {
  ready: boolean;
  schema_ready: boolean;
  blocker_categories: string[];
  missing_schema: string[];
  categories: Array<{
    key: string;
    label: string;
    resolved: boolean;
    outstanding: OutstandingItem[];
  }>;
}

interface Plan {
  id: number;
  provider_user_id: number;
  strategy: Strategy;
  target_provider_user_id?: number | null;
  status: string;
  dependency_snapshot: Snapshot;
  next_steps: Array<{ category: string; instruction: string }>;
  live_dependency_snapshot?: Snapshot;
  live_next_steps?: Array<{ category: string; instruction: string }>;
  version: number;
}

interface UpstreamRef { user_id: number; display_name: string }

interface GovernanceVersions {
  business_identity: number;
  commercial_binding: number;
  channel_relationship: number;
}

/**
 * 每一类阻塞项的「去哪里处理」。分类键与后端
 * services/admin_cross_tenant_governance.py provider_downgrade_snapshot 的
 * labels 一一对应；后端 ACTIVE_PROVIDER_DEPENDENCIES 也按同一套语义逐项回话。
 * 🔴 提示必须自带出路（feedback_hint_must_help_or_hide），只报数字不给出口 = 缺陷。
 */
const CATEGORY_HANDLING: Record<string, string> = {
  clients: '逐个把客户转交给其他服务商或转为平台直营（用户治理 → 关系 → 修改归属），再回本向导。',
  channel_relations: '先在本向导终结现役渠道关系。降级后该账号不再是服务商，渠道关系就永远改不动了，所以这一步必须排在降级之前。',
  inventory: '用现有库存退回 / 转售 / 结清原语把库存和退款准备金归零；向导不没收库存。',
  orders: '等待待支付、转售、消费者与 JIT 履约订单进入不可变终态；向导不代改订单。',
  earnings: '走现有结算 / 提现 / 收益冲销流程结清；向导不覆盖收益。',
  responsibilities: '完成争议裁决、退款工单与退款资金责任；责任不得转嫁给平台。',
  platform_manufacturer: '先把平台生产主体配置和平台直营专用服务身份迁到别的账号。',
  schema: '依赖表缺失，当前保持 fail-closed；补齐只读依赖 schema 后重试。',
};

const UNIT_SUFFIX: Record<OutstandingItem['unit'], string> = { rows: '项', points: '点', cents: '分' };

function apiMessage(error: unknown): string {
  const candidate = error as { response?: { data?: { detail?: string | { message?: string } } }; message?: string };
  const detail = candidate.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && detail.message) return detail.message;
  return candidate.message || '降级向导请求失败';
}

export default function ProviderDowngradeWizard({
  open, onOpenChange, providerUserId, providerName, identityVersion, onCompleted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providerUserId: number;
  providerName: string;
  identityVersion: number;
  onCompleted: () => Promise<void>;
}) {
  const [strategy, setStrategy] = useState<Strategy>('settle_then_downgrade');
  const [targetId, setTargetId] = useState('');
  const [reason, setReason] = useState('');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showDurablePlan, setShowDurablePlan] = useState(false);

  // 直属上游与三个 scope 的乐观锁版本都要现取：向导内部连做三步写入，
  // 用打开弹窗那一刻的快照会在第二步就撞 409。
  const [upstream, setUpstream] = useState<UpstreamRef | null>(null);
  const [versions, setVersions] = useState<GovernanceVersions>({
    business_identity: identityVersion, commercial_binding: 1, channel_relationship: 1,
  });
  const [destination, setDestination] = useState<UpstreamDestination | null>(null);
  /** 打开向导那一刻的直属上游。只要它存在，降级后就必须交代归属去向 ——
   *  不管上游是在本向导里终结的，还是管理员在别处先终结的。 */
  const [initialUpstream, setInitialUpstream] = useState<UpstreamRef | null>(null);
  const upstreamCaptured = useRef(false);
  const [downgraded, setDowngraded] = useState(false);

  const reasonText = reason.trim();
  const reasonReady = reasonText.length >= 2;

  const loadDetail = async (): Promise<GovernanceVersions | null> => {
    try {
      const response = await authApi.get(`/api/admin/user-governance/users/${providerUserId}`, {
        headers: { 'X-Request-ID': `downgrade-detail-read-${safeRandomUUID()}`, 'X-Governance-Reason': 'admin-provider-downgrade-detail-read' },
      });
      const detail = response.data as {
        overview: { versions: GovernanceVersions };
        relationships: { channel: { upstream: UpstreamRef | null } };
      };
      const current = detail.relationships.channel.upstream ?? null;
      setUpstream(current);
      if (!upstreamCaptured.current) {
        upstreamCaptured.current = true;
        setInitialUpstream(current);
      }
      setVersions(detail.overview.versions);
      return detail.overview.versions;
    } catch {
      // 版本读不到时不阻断只读评估；真正的写入会被后端 CAS 挡住。
      return null;
    }
  };

  const loadReadiness = async () => {
    setBusy(true); setError(null);
    try {
      await loadDetail();
      if (plan) {
        const response = await authApi.get(`/api/admin/cross-tenant-governance/providers/${providerUserId}/downgrade-plans/${plan.id}`, {
          // 🔴 HTTP header 值只接受 latin1(ISO-8859-1)。这里曾是中文,导致
          // setRequestHeader 当场抛异常 → 向导打开即死 → 管理员完全无法降级服务商。
          // 取值口径与 DemoGrantPanel 一致:ASCII slug。后端 _read_reason 只要求 len>=2,
          // 且未传时自带中文兜底(admin_cross_tenant_governance_api.py:_read_reason)。
          headers: { 'X-Request-ID': `downgrade-plan-read-${safeRandomUUID()}`, 'X-Governance-Reason': 'admin-provider-downgrade-plan-read' },
        });
        const next = response.data.plan as Plan;
        setPlan(next); setSnapshot(next.live_dependency_snapshot || next.dependency_snapshot);
      } else {
        const response = await authApi.get(`/api/admin/cross-tenant-governance/providers/${providerUserId}/downgrade-readiness`, {
          // 🔴 同上:latin1 限制。这一处在 useEffect(open) 里,是「打开向导即报错」的直接来源。
          headers: { 'X-Request-ID': `downgrade-readiness-${safeRandomUUID()}`, 'X-Governance-Reason': 'admin-provider-downgrade-readiness' },
        });
        setSnapshot(response.data.readiness as Snapshot);
      }
    } catch (caught) { setError(apiMessage(caught)); } finally { setBusy(false); }
  };

  useEffect(() => {
    if (open) void loadReadiness();
    if (!open) {
      setPlan(null); setSnapshot(null); setReason(''); setTargetId('');
      setStrategy('settle_then_downgrade'); setError(null); setShowDurablePlan(false);
      setUpstream(null); setDestination(null); setInitialUpstream(null); setDowngraded(false);
      upstreamCaptured.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, providerUserId]);

  const blockedCategories = useMemo(
    () => (snapshot?.categories || []).filter((item) => !item.resolved),
    [snapshot],
  );
  // 有上游就必须先说清归属去向，才允许推进降级 —— 归属不许静默消失。
  const destinationRequired = Boolean(initialUpstream);
  const destinationReady = !destinationRequired || Boolean(destination);

  /** 步骤 1：终结现役渠道关系。必须在降级之前——降级后 agent_level=0，
   *  change_channel_relationship 会以 CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER 永久拒绝。 */
  const terminateChannelRelationship = async () => {
    if (!upstream || !reasonReady || !destination) return;
    setBusy(true); setError(null);
    try {
      const fresh = await loadDetail();
      await authApi.put(
        `/api/admin/user-governance/users/${providerUserId}/channel-relationship`,
        {
          upstream_user_id: null,
          cost_multiplier_bps: 10000,
          expected_version: (fresh || versions).channel_relationship,
          reason: `${reasonText}（服务商降级前置：终结直属上游渠道关系；降级后归属去向=${destination === 'rebind_upstream' ? '回绑原上游为客户' : '显式解除归属'}）`,
        },
        { headers: { 'X-Request-ID': `downgrade-channel-end-${providerUserId}-${safeRandomUUID()}` } },
      );
      await loadReadiness();
    } catch (caught) { setError(apiMessage(caught)); } finally { setBusy(false); }
  };

  /** 步骤 2：十二项全零时的一步降级。不必先建耐久方案。 */
  const downgradeDirectly = async () => {
    if (!reasonReady || !destinationReady) return;
    setBusy(true); setError(null);
    try {
      const fresh = await loadDetail();
      await authApi.put(
        `/api/admin/user-governance/users/${providerUserId}/business-identity`,
        {
          business_identity: 'ordinary_user',
          expected_version: (fresh || versions).business_identity,
          reason: reasonText,
        },
        { headers: { 'X-Request-ID': `downgrade-direct-${providerUserId}-${safeRandomUUID()}` } },
      );
      setDowngraded(true);
      await onCompleted();
      if (!initialUpstream) onOpenChange(false);
    } catch (caught) { setError(apiMessage(caught)); } finally { setBusy(false); }
  };

  /** 步骤 3：交代上游归属去向。回绑走 customer_agent_bindings（商业归属 SSOT）。 */
  const settleAttribution = async () => {
    if (!initialUpstream || !destination) return;
    if (destination === 'declare_released') {
      // 显式声明解除：归属去向已写进步骤 1 的审计原因，无需再写一条空绑定。
      await onCompleted();
      onOpenChange(false);
      return;
    }
    setBusy(true); setError(null);
    try {
      const fresh = await loadDetail();
      await authApi.put(
        `/api/admin/user-governance/users/${providerUserId}/commercial-service-binding`,
        {
          provider_user_id: initialUpstream.user_id,
          expected_version: (fresh || versions).commercial_binding,
          reason: `${reasonText}（服务商降级后置：原上游 #${initialUpstream.user_id} 的归属回落为客户关系）`,
        },
        { headers: { 'X-Request-ID': `downgrade-rebind-${providerUserId}-${safeRandomUUID()}` } },
      );
      await onCompleted();
      onOpenChange(false);
    } catch (caught) { setError(apiMessage(caught)); } finally { setBusy(false); }
  };

  const createPlan = async () => {
    setBusy(true); setError(null);
    try {
      const response = await authApi.post(
        `/api/admin/cross-tenant-governance/providers/${providerUserId}/downgrade-plans`,
        {
          strategy,
          target_provider_user_id: strategy === 'transfer_upstream' ? Number(targetId) : null,
          expected_identity_version: versions.business_identity,
          reason: reasonText,
          confirmation: 'CREATE_PROVIDER_DOWNGRADE_PLAN',
        },
        { headers: { 'X-Request-ID': `downgrade-plan-create-${safeRandomUUID()}` } },
      );
      const next = response.data.plan as Plan;
      setPlan(next); setSnapshot(next.dependency_snapshot);
    } catch (caught) { setError(apiMessage(caught)); } finally { setBusy(false); }
  };

  const confirm = async () => {
    if (!plan) return;
    setBusy(true); setError(null);
    try {
      await authApi.post(
        `/api/admin/cross-tenant-governance/providers/${providerUserId}/downgrade-plans/${plan.id}/confirm`,
        { expected_plan_version: plan.version, reason: reasonText, confirmation: 'CONFIRM_PROVIDER_DOWNGRADE' },
        // The same plan/version is one durable attempt. A lost response must
        // reuse its idempotency key so the backend can close the narrow crash
        // window after the authoritative identity primitive has committed.
        { headers: { 'X-Request-ID': `downgrade-plan-confirm-${plan.id}-v${plan.version}` } },
      );
      setDowngraded(true);
      await onCompleted();
      if (!initialUpstream) onOpenChange(false);
    } catch (caught) {
      setError(apiMessage(caught));
      await loadReadiness();
    } finally { setBusy(false); }
  };

  const liveSteps = plan?.live_next_steps || plan?.next_steps || [];
  const awaitingAttribution = downgraded && Boolean(initialUpstream);

  return (
    <Dialog open={open} onOpenChange={value => { if (!busy) onOpenChange(value); }}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto" data-testid="provider-downgrade-wizard">
        <DialogHeader>
          <DialogTitle>服务商降级向导</DialogTitle>
          <DialogDescription>{providerName}（用户 #{providerUserId}）必须逐项处理客户、订单、库存、收益和责任。向导不会静默充公或直接覆盖资金事实。</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <label className="block space-y-1.5 text-sm"><span className="font-medium">治理原因（必填）</span><textarea value={reason} onChange={event => setReason(event.target.value)} rows={2} maxLength={500} className="w-full rounded-md border bg-background px-3 py-2" placeholder="说明业务申请、责任承接和核验依据" /></label>

          {awaitingAttribution ? (
            /* 步骤 3：降级已完成，但上游归属还没有去处 —— 不允许静默丢失。 */
            <div className="rounded-xl border border-sky-500/30 bg-sky-500/5 p-4 text-sm" data-testid="downgrade-attribution-step">
              <div className="font-medium text-foreground">最后一步：交代上游归属去向</div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">
                该账号已降为普通用户，原直属上游「{initialUpstream?.display_name}（#{initialUpstream?.user_id}）」的渠道关系已终结。
                归属不能凭空消失，请确认去向：
              </div>
              <div className="mt-3 text-sm font-medium text-foreground">
                {destination === 'rebind_upstream'
                  ? `回绑为 ${initialUpstream?.display_name}（#${initialUpstream?.user_id}）的客户`
                  : '显式声明归属已解除（转平台直营，不回绑任何服务商）'}
              </div>
            </div>
          ) : (
            <>
              {destinationRequired && (
                /* 归属去向必须在降级之前就定下来：降级后原上游关系已终结，
                   没有这一步归属就静默消失了。 */
                <div className="space-y-1.5 rounded-xl border border-sky-500/30 bg-sky-500/5 p-4" data-testid="downgrade-destination-block">
                  <div className="text-sm font-medium text-foreground">降级后，原上游的商业归属去哪里？（必选）</div>
                  <div className="text-xs leading-5 text-muted-foreground">
                    当前直属上游：{initialUpstream?.display_name}（#{initialUpstream?.user_id}）。
                    降级会终结渠道关系，归属必须有明确去处，不允许凭空消失。
                  </div>
                  <Select value={destination ?? ''} onValueChange={value => setDestination(value as UpstreamDestination)}>
                    <SelectTrigger data-testid="downgrade-destination-select"><SelectValue placeholder="请选择归属去向" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="rebind_upstream">
                        回绑为 {initialUpstream?.display_name}（#{initialUpstream?.user_id}）的客户
                      </SelectItem>
                      <SelectItem value="declare_released">显式声明归属已解除（转平台直营）</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              )}

              {snapshot && blockedCategories.length === 0 && (
                <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-sm" data-testid="downgrade-ready-banner">
                  <div className="flex items-center gap-2 font-medium text-foreground">
                    <CheckCircle2 className="h-4 w-4 text-emerald-500" />该服务商没有任何未结清依赖
                  </div>
                  <div className="mt-1 text-xs leading-5 text-muted-foreground">填写治理原因后可以直接降级，不必再建耐久处置方案。</div>
                </div>
              )}

              {snapshot && blockedCategories.length > 0 && (
                <div className="overflow-hidden rounded-xl border" data-testid="downgrade-blockers">
                  <div className="flex items-center justify-between border-b bg-muted/35 px-4 py-3">
                    <div className="font-medium">还需处理 {blockedCategories.length} 项</div>
                    <Button variant="ghost" size="sm" onClick={() => void loadReadiness()} disabled={busy}><RefreshCw className="mr-1 h-4 w-4" />刷新</Button>
                  </div>
                  <div className="divide-y">
                    {blockedCategories.map(item => (
                      <div key={item.key} className="px-4 py-3 text-sm">
                        <div className="flex items-center gap-2">
                          <AlertTriangle className="h-4 w-4 text-amber-500" />
                          <span className="font-medium">{item.label}</span>
                        </div>
                        {item.outstanding.length > 0 && (
                          <ul className="mt-2 space-y-1 pl-6 text-xs text-amber-700 dark:text-amber-300">
                            {item.outstanding.map(part => <li key={part.key}>{part.label}：{part.value} {UNIT_SUFFIX[part.unit]}</li>)}
                          </ul>
                        )}
                        <div className="mt-2 pl-6 text-xs leading-5 text-muted-foreground">
                          {CATEGORY_HANDLING[item.key] || '使用该领域现有原语处理后回本向导刷新。'}
                        </div>
                        {item.key === 'channel_relations' && (
                          <div className="mt-3 ml-6 space-y-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                            <Button
                              size="sm"
                              data-testid="downgrade-end-channel"
                              onClick={() => void terminateChannelRelationship()}
                              disabled={busy || !upstream || !reasonReady || !destination}
                            >
                              {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}终结本账号的直属上游
                              <ArrowRight className="ml-1 h-4 w-4" />
                            </Button>
                            <div className="text-xs leading-5 text-muted-foreground">
                              {upstream
                                ? '这一步只终结本账号「向谁进货」的关系，历史报价与订单快照不改写。'
                                : '本账号当前没有直属上游，剩下的现役关系是挂在它名下的下级服务商。'}
                              {' '}若刷新后计数仍未归零，说明还有下级服务商以该账号为上游——需先把下级的直属上游改到别处，本向导不代改别人的关系。
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {snapshot && !snapshot.schema_ready && snapshot.missing_schema.length > 0 && (
                <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
                  依赖 schema 缺失，保持 fail-closed：{snapshot.missing_schema.join('、')}
                </div>
              )}

              {liveSteps.length > 0 && <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-4"><div className="mb-2 text-sm font-medium">耐久方案下一步</div><ol className="space-y-2 text-sm text-muted-foreground">{liveSteps.map((step, index) => <li key={`${step.category}-${index}`}>{index + 1}. {step.instruction}</li>)}</ol></div>}

              <div className="rounded-xl border">
                <button
                  type="button"
                  className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium"
                  onClick={() => setShowDurablePlan(value => !value)}
                >
                  <span>高级：建立耐久处置方案（转交上级 / 平台托管）</span>
                  <span className="text-xs text-muted-foreground">{showDurablePlan ? '收起' : '展开'}</span>
                </button>
                {showDurablePlan && (
                  <div className="space-y-3 border-t px-4 py-3">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <label className="space-y-1.5 text-sm"><span className="font-medium">处理策略</span><Select value={strategy} onValueChange={value => setStrategy(value as Strategy)} disabled={Boolean(plan)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="transfer_upstream">转交上级服务商</SelectItem><SelectItem value="platform_managed">平台托管</SelectItem><SelectItem value="settle_then_downgrade">全部结清后降级</SelectItem></SelectContent></Select></label>
                      {strategy === 'transfer_upstream' && <label className="space-y-1.5 text-sm"><span className="font-medium">目标服务商用户 ID</span><Input inputMode="numeric" value={targetId} disabled={Boolean(plan)} onChange={event => setTargetId(event.target.value.replace(/\D/g, ''))} placeholder="合格上级服务商 ID" /></label>}
                    </div>
                    {!plan
                      ? <Button variant="outline" size="sm" onClick={() => void createPlan()} disabled={busy || !reasonReady || (strategy === 'transfer_upstream' && !targetId)}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}建立耐久方案</Button>
                      : <Button variant="outline" size="sm" onClick={() => void confirm()} disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}按方案确认并降级</Button>}
                  </div>
                )}
              </div>
            </>
          )}

          {error && <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error}</div>}
          <div className="rounded-lg border bg-muted/25 p-3 text-xs leading-5 text-muted-foreground">资金、库存、收益与退款责任只接现有领域原语；任一 schema、终态或责任证明不足，最终确认都会 fail-closed。</div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy || awaitingAttribution}>关闭</Button>
          {awaitingAttribution ? (
            <Button data-testid="downgrade-settle-attribution" onClick={() => void settleAttribution()} disabled={busy}>
              {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}确认归属去向并完成
            </Button>
          ) : (
            <Button
              data-testid="downgrade-direct"
              onClick={() => void downgradeDirectly()}
              disabled={busy || !snapshot?.ready || !reasonReady || !destinationReady}
            >
              {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}降级为普通用户
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
