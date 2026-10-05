import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  BadgeCheck,
  BriefcaseBusiness,
  Building2,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  FileClock,
  History,
  KeyRound,
  Landmark,
  Link2,
  Loader2,
  Network,
  RefreshCw,
  Search,
  Shield,
  ShieldCheck,
  UserCog,
  Users,
  WalletCards,
} from 'lucide-react';
import { lazyToast } from '@/lib/lazyToast';
import ProviderDowngradeWizard from './ProviderDowngradeWizard';
import DemoGrantPanel from './DemoGrantPanel';
import { AgentOverridePanel } from './PricingCenter';
import { adminApi } from '@/lib/v35w2Api';
import { GovernanceAlert } from '@/components/ui/governance-alert';
import type { GovernanceAlertContract } from '@/contracts/governanceAlert';

import { authApi } from '@/context/AuthContext';
import { humanizeAuditSnapshot } from '@/lib/auditTranslate';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

type BusinessIdentity = 'ordinary_user' | 'service_provider';
type PlatformAccess = 'administrator' | 'standard';

interface Versions {
  business_identity: number;
  commercial_binding: number;
  channel_relationship: number;
  platform_access: number;
  password_security: number;
  wallet_adjustment: number;
  account_status: number;
}

interface UserListItem {
  user_id: number;
  username: string;
  display_name: string;
  phone?: string | null;
  is_active: boolean;
  business_identity: BusinessIdentity;
  platform_access: PlatformAccess;
  total_points: number;
  customer_count: number;
  brand_count: number;
  service_mode: 'service_provider' | 'platform_direct';
  /** [补充工单 2026-08-06] 账号来源 · 与详情页 registration.account_origin 同源同值 */
  account_origin: 'self_signup' | 'organization_member';
  /** 组织成员才有;列表行只需要团队名,完整 organization 对象在详情页 */
  organization_name?: string | null;
  needs_attention: boolean;
  attention_label?: string | null;
  created_at?: string | null;
  last_active_at?: string | null;
  versions: Versions;
}

interface ActorRef {
  user_id: number;
  username: string;
  display_name: string;
  is_active: boolean;
  company?: string | null;
  business_identity: BusinessIdentity;
  service_code?: string | null;
  channel_code?: string | null;
}

interface Evidence {
  status: 'complete' | 'related' | 'incomplete' | 'not_required';
  label: string;
  operator_user_id?: number | null;
  operator_name?: string | null;
  reason?: string | null;
  request_id?: string | null;
  happened_at?: string | null;
}

interface RelationshipNotice {
  code: string;
  severity: 'info' | 'warning' | 'critical';
  title: string;
  detail: string;
}

interface UserDetail {
  success: boolean;
  overview: {
    user_id: number;
    username: string;
    display_name: string;
    phone?: string | null;
    company?: string | null;
    is_active: boolean;
    business_identity: BusinessIdentity;
    business_identity_label: string;
    platform_access: PlatformAccess;
    platform_access_label: string;
    total_points: number;
    paid_points: number;
    bonus_points: number;
    total_recharged_points: number;
    customer_count: number;
    brand_count: number;
    created_at?: string | null;
    last_login_at?: string | null;
    last_active_at?: string | null;
    versions: Versions;
    /** [P0-C §9] 这条归属是否缺审计凭证 —— 与列表页同一段判定 SQL,不是另一套口径 */
    needs_attention?: boolean;
    attention_label?: string | null;
  };
  relationships: {
    registration: {
      present: boolean;
      inviter?: ActorRef | null;
      source: 'referral_links' | 'legacy_user_pointer' | 'organization_invite' | 'none';
      registered_at?: string | null;
      legacy_pointer_user_id?: number | null;
      evidence: Evidence;
      /** [工单 2026-08-06 §3] 账号是怎么来的 —— 决定「没有邀请归属」到底是正常还是异常 */
      account_origin: 'self_signup' | 'organization_member';
      organization?: {
        organization_id: number | null;
        name: string | null;
        owner?: ActorRef | null;
        membership_status?: string | null;
        role_id?: number | null;
        joined_at?: string | null;
      } | null;
    };
    commercial: {
      mode: 'service_provider' | 'platform_direct';
      provider?: ActorRef | null;
      binding_id?: number | null;
      binding_source?: string | null;
      binding_source_label: string;
      bound_at?: string | null;
      relationship_version: number;
      dispute_status?: string | null;
      evidence: Evidence;
    };
    channel: {
      mode: 'upstream_channel' | 'platform_root' | 'not_applicable';
      upstream?: ActorRef | null;
      relationship_version?: string | null;
      cost_multiplier_bps?: number | null;
      effective_from?: string | null;
      reason?: string | null;
    };
    dual_relationships_present: boolean;
    dual_relationships_label?: string | null;
    notices: RelationshipNotice[];
  };
  pricing_and_settlement: {
    customer_pricing_route: string;
    procurement_pricing_route: string;
    settlement_route: string;
    pricing_source: string;
    special_pricing_note?: string | null;
  };
  clients_and_brands: {
    clients: Array<{ customer_user_id: number; username: string; display_name: string; bound_at?: string | null }>;
    brands: Array<{ brand_id: number; name: string; industry?: string | null; status?: string | null }>;
  };
  wallet_and_billing: {
    paid_points: number;
    bonus_points: number;
    total_points: number;
    total_recharged_points: number;
    recent_orders: Array<{ order_id: string; amount_yuan: string; status_label: string; created_at?: string | null; paid_at?: string | null }>;
    recent_transactions: Array<{ transaction_id: number; direction_label: string; points: number; description?: string | null; created_at?: string | null }>;
  };
  permissions_and_security: {
    platform_access: PlatformAccess;
    account_status_label: string;
    must_change_password: boolean;
    permission_version: number;
    legacy_roles: Array<{
      role_id: number;
      internal_name: string;
      historical_label: string;
      compatibility_status: 'active_compatibility' | 'read_only_legacy';
    }>;
  };
  operation_logs: Array<{
    audit_id: number;
    scope: 'business_identity' | 'commercial_binding' | 'channel_relationship' | 'platform_access' | 'password_security' | 'wallet_adjustment';
    action_label: string;
    operator_user_id: number;
    operator_name?: string | null;
    request_id: string;
    reason: string;
    before_summary: string;
    after_summary: string;
    version_before: number;
    version_after: number;
    created_at: string;
  }>;
}

interface AgreementGateMetrics {
  available: boolean;
  days: number;
  total: number;
  distinct_users: number;
  daily: { day: string; triggers: number; users: number }[];
  /** 连续多天非零 = 有账号正卡在协议门禁外面出不来(正常形态是当天补签当天消失)。 */
  consecutive_days_with_triggers: number;
}

interface EmptyRetailScope {
  version_id: number;
  scope_key: string;
  version_code: string;
  effective_from: string | null;
  reason: string;
}

interface Readiness {
  configured: boolean;
  ready: boolean;
  status: 'missing' | 'invalid' | 'ready';
  label: string;
  service_user?: ActorRef | null;
  checks: string[];
  /** 已发布但零条目的零售目录 —— 这些服务方名下的客户当前买不了任何东西。 */
  empty_retail_scopes?: EmptyRetailScope[];
  empty_retail_alert?: { message?: string; repair_hint?: string } | null;
}

type ClientScopeKind = 'legacy_unrestricted' | 'legacy_selected' | 'organization_assigned';

interface AdminClientScope {
  success: boolean;
  user_id: number;
  scope_kind: ClientScopeKind;
  subject_kind: 'legacy_user' | 'organization_member' | 'organization_owner';
  editable: boolean;
  brand_ids: number[];
  effective_brand_ids: number[];
  available_brands: Array<{ id: number; name: string; owner_user_id: number }>;
  stale_brand_ids: number[];
  empty_semantics: 'legacy_owner_role_fallback' | 'selected_plus_owned' | 'zero_clients' | 'owner_identity_all';
  version: number;
  permission_version: number;
  organization_id?: number | null;
  membership_id?: number | null;
  principal_user_id: number;
  organization_authority_version?: number | null;
  membership_status?: string | null;
  etag: string;
}

function passwordPolicyError(password: string): string | null {
  if (password.length < 8) return '至少输入 8 个字符';
  if (password !== password.trim()) return '密码首尾不能包含空格';
  if (new TextEncoder().encode(password).length > 72) return '密码过长，请控制在 72 字节以内';
  return null;
}

type TabKey = 'overview' | 'relationships' | 'pricing' | 'clients' | 'demo' | 'wallet' | 'security' | 'logs';
type ChangeKind = 'business_identity' | 'commercial_binding' | 'channel_relationship' | 'platform_access' | 'account_status';

interface ChangeDraft {
  kind: ChangeKind;
  title: string;
  beforeLabel: string;
  afterLabel: string;
  impact: string;
  identity?: BusinessIdentity;
  providerUserId?: number | null;
  upstreamUserId?: number | null;
  costMultiplierBps?: number;
  administrator?: boolean;
  active?: boolean;
}

const TABS: Array<{ key: TabKey; label: string; icon: typeof Users }> = [
  { key: 'overview', label: '概览', icon: UserCog },
  { key: 'relationships', label: '服务关系', icon: Network },
  { key: 'pricing', label: '定价与结算', icon: CircleDollarSign },
  { key: 'clients', label: '客户与品牌', icon: Building2 },
  { key: 'demo', label: '演示客户', icon: BadgeCheck },
  { key: 'wallet', label: '钱包与账单', icon: WalletCards },
  { key: 'security', label: '权限与安全', icon: ShieldCheck },
  { key: 'logs', label: '操作日志', icon: History },
];

const IDENTITY_LABEL: Record<BusinessIdentity, string> = {
  ordinary_user: '普通用户',
  service_provider: '服务商',
};

function formatNumber(value: number | null | undefined) {
  return Number(value || 0).toLocaleString('zh-CN');
}

function formatTime(value: string | null | undefined) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function formatMultiplier(value: number) {
  return Number(value.toFixed(4)).toString();
}

function formatMultiplierBps(value: number) {
  return formatMultiplier(value / 10000);
}

function errorMessage(error: unknown) {
  const candidate = error as { response?: { data?: { detail?: string | { message?: string } } }; message?: string };
  const detail = candidate.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && detail.message) return detail.message;
  return candidate.message || '请求失败，请稍后重试';
}

function Initials({ name }: { name: string }) {
  return (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-primary/12 text-sm font-semibold text-primary">
      {(name || '?').slice(0, 2)}
    </span>
  );
}

function IdentityBadge({ identity }: { identity: BusinessIdentity }) {
  return (
    <Badge variant="outline" className={cn(
      'border-0 px-2 py-0.5 font-medium',
      identity === 'service_provider'
        ? 'bg-sky-500/12 text-sky-700 dark:text-sky-300'
        : 'bg-muted text-muted-foreground',
    )}>
      {IDENTITY_LABEL[identity]}
    </Badge>
  );
}

function FactRow({ label, value, hint }: { label: string; value: string; hint?: string | null }) {
  return (
    <div className="grid gap-1 border-b border-border/60 py-3 last:border-0 sm:grid-cols-[150px_minmax(0,1fr)] sm:gap-5">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="min-w-0 break-words text-sm text-foreground" title={value}>{value}</div>
      {hint && <div className="text-xs leading-5 text-muted-foreground sm:col-start-2">{hint}</div>}
    </div>
  );
}

function Section({ title, description, children, action }: {
  title: string;
  description?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="border-b border-border/70 py-5 first:pt-0 last:border-0 last:pb-0">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          {description && <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

// [WP2 §5.4] 用户详情内嵌的定价治理面板:服务商 → 预填 AgentOverridePanel(可操作入口,
// 不再只解释路径);非服务商 → §13 告警合同给"去调整为服务商"出口(避免事故#2:admin 被
// 只读页锁死)。走 catalog-versioned agent-override 单一写路径(§5.5 版本/来源)。
function PricingGovernancePanel({
  userId, displayName, isServiceProvider, onGoToIdentity, onSaved,
}: {
  userId: number;
  displayName: string;
  isServiceProvider: boolean;
  onGoToIdentity: () => void;
  onSaved?: () => Promise<void>;
}) {
  const [pointsPerYuan, setPointsPerYuan] = useState<number | null>(null);
  const [catalogVersion, setCatalogVersion] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [loadErr, setLoadErr] = useState(false);

  useEffect(() => {
    if (!isServiceProvider) return;
    let alive = true;
    setLoadErr(false);
    (async () => {
      try {
        const r = await adminApi.pricingCenter();
        if (!alive) return;
        setPointsPerYuan(r.default_rule?.points_per_yuan ?? null);
        setCatalogVersion(r.catalog_version || '');
      } catch { if (alive) setLoadErr(true); }
    })();
    return () => { alive = false; };
  }, [isServiceProvider, reloadKey]);

  if (!isServiceProvider) {
    const contract: GovernanceAlertContract = {
      code: 'PRICING_TARGET_NOT_SERVICE_PROVIDER',
      message: '该用户当前不是服务商，无法为其配置专属定价。',
      reason: '专属定价系数只对服务商生效；普通用户没有面向终端客户的报价链。',
      impact: '本页暂无法编辑该用户的报价系数与进货折扣。',
      repair_hint: '如需为其配置定价，请先在“概览”里把该用户调整为服务商，再回到本页。',
      actions: [{ id: 'go_adjust_identity', label: '去调整为服务商', type: 'nav' }],
      rule_version: 'pricing-governance-ui-v1',
    };
    return (
      <GovernanceAlert
        contract={contract}
        onAction={(a) => { if (a.id === 'go_adjust_identity') { onGoToIdentity(); return true; } }}
      />
    );
  }

  if (loadErr) {
    const contract: GovernanceAlertContract = {
      code: 'PRICING_CATALOG_LOAD_FAILED',
      message: '价格版本没能载入，暂时无法编辑专属定价。',
      reason: '读取当前算力定价版本失败，通常是短暂的网络或服务波动。',
      impact: '为避免在错误的价格版本上保存，本次未打开编辑。',
      repair_hint: '可重试载入；若持续失败请联系有权限的同事。',
      actions: [{ id: 'retry_pricing_catalog', label: '重试载入', type: 'retry' }],
      rule_version: 'pricing-governance-ui-v1',
    };
    return (
      <GovernanceAlert
        contract={contract}
        onAction={(a) => { if (a.id === 'retry_pricing_catalog') { setReloadKey((k) => k + 1); return true; } }}
      />
    );
  }

  if (!catalogVersion) {
    return <div className="py-6 text-center text-sm text-muted-foreground">价格版本载入中…</div>;
  }

  return (
    <AgentOverridePanel
      initialAgentUserId={userId}
      initialAgentName={displayName}
      pointsPerYuan={pointsPerYuan}
      catalogVersion={catalogVersion}
      onSaved={async () => { await onSaved?.(); }}
    />
  );
}

function EvidenceView({ evidence }: { evidence: Evidence }) {
  const incomplete = evidence.status === 'incomplete';
  const related = evidence.status === 'related';
  return (
    <div className={cn(
      'mt-3 rounded-lg border px-3 py-2.5 text-xs leading-5',
      incomplete && 'border-amber-500/30 bg-amber-500/8 text-amber-800 dark:text-amber-200',
      related && 'border-sky-500/25 bg-sky-500/8 text-sky-800 dark:text-sky-200',
      !incomplete && !related && 'border-border/70 bg-muted/35 text-muted-foreground',
    )}>
      <div className="flex items-start gap-2">
        {incomplete ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> : <FileClock className="mt-0.5 h-4 w-4 shrink-0" />}
        <div className="min-w-0 break-words">
          <div className="font-medium">{evidence.label}</div>
          {(evidence.operator_name || evidence.operator_user_id) && (
            <div>操作人：{evidence.operator_name || `用户 #${evidence.operator_user_id}`}</div>
          )}
          {evidence.reason && <div title={evidence.reason}>证据：{evidence.reason}</div>}
          {evidence.happened_at && <div>时间：{formatTime(evidence.happened_at)}</div>}
        </div>
      </div>
    </div>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">{children}</div>;
}

function ClientAccessAssignment({ userId }: { userId: number }) {
  const [scope, setScope] = useState<AdminClientScope | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [search, setSearch] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const requestSequence = useRef(0);

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setSaving(false);
    setLoadError(null);
    try {
      const response = await authApi.get(`/api/admin/users/${userId}/clients`);
      if (sequence !== requestSequence.current) return;
      const next = response.data as AdminClientScope;
      if (next.user_id !== userId) throw new Error('客户访问范围响应与当前账号不一致');
      setScope(next);
      setSelected(next.brand_ids);
      setReason('');
    } catch (error) {
      if (sequence === requestSequence.current) setLoadError(errorMessage(error));
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    void load();
    return () => { requestSequence.current += 1; };
  }, [load]);

  const baseline = useMemo(() => [...(scope?.brand_ids || [])].sort((a, b) => a - b), [scope]);
  const normalizedSelected = useMemo(() => [...new Set(selected)].sort((a, b) => a - b), [selected]);
  const dirty = JSON.stringify(baseline) !== JSON.stringify(normalizedSelected);
  const desiredScope: ClientScopeKind = scope?.scope_kind === 'organization_assigned'
    ? 'organization_assigned'
    : normalizedSelected.length > 0 ? 'legacy_selected' : 'legacy_unrestricted';
  const effectiveSet = new Set(scope?.subject_kind === 'organization_owner'
    ? scope.effective_brand_ids
    : normalizedSelected);
  const filteredBrands = (scope?.available_brands || []).filter((brand) => (
    !search.trim() || `${brand.name} ${brand.id}`.toLocaleLowerCase('zh-CN').includes(search.trim().toLocaleLowerCase('zh-CN'))
  ));

  const save = async () => {
    if (!scope || !scope.editable || !dirty || !reason.trim()) return;
    const sequence = ++requestSequence.current;
    const submittedUserId = userId;
    const submittedScope = scope;
    const submittedBrandIds = [...normalizedSelected];
    const submittedReason = reason.trim();
    setSaving(true);
    try {
      const response = await authApi.put(`/api/admin/users/${submittedUserId}/clients`, {
        scope_kind: desiredScope,
        expected_scope_kind: submittedScope.scope_kind,
        expected_version: submittedScope.version,
        etag: submittedScope.etag,
        brand_ids: submittedBrandIds,
        request_id: `admin-client-scope-${safeRandomUUID()}`,
        reason: submittedReason,
      });
      if (sequence !== requestSequence.current) return;
      const next = response.data as AdminClientScope;
      if (next.user_id !== submittedUserId) {
        lazyToast.error('保存响应与当前账号不一致，已重新加载');
        await load();
        return;
      }
      setScope(next);
      setSelected(next.brand_ids);
      setReason('');
      lazyToast.success('客户访问范围已更新，旧权限版本即时失效');
    } catch (error) {
      if (sequence !== requestSequence.current) return;
      lazyToast.error(errorMessage(error));
      if ((error as { response?: { status?: number } }).response?.status === 409) await load();
    } finally {
      if (sequence === requestSequence.current) setSaving(false);
    }
  };

  const modeLabel = scope?.scope_kind === 'organization_assigned'
    ? scope.subject_kind === 'organization_owner' ? '组织老板全客户' : '组织员工已分配客户'
    : scope?.scope_kind === 'legacy_selected' ? '旧账号指定客户' : '旧账号兼容回退';
  const emptyCopy = scope?.subject_kind === 'organization_owner'
    ? '老板的全客户视图来自 owner 身份，不使用空分配推导。'
    : scope?.scope_kind === 'organization_assigned'
      ? '当前未分配客户：该员工可访问 0 个客户。'
      : '当前未指定额外客户：仅保留本人名下客户与现役旧角色的兼容访问，不代表全部客户。';

  return (
    <Section
      title="客户访问分配"
      description="这里只调整账号可访问的客户，不改变商业归属、老板、付款人、邀请来源或上下游关系。"
      action={scope?.editable ? (
        <Button
          size="sm"
          disabled={!dirty || !reason.trim() || saving}
          onClick={() => void save()}
          data-testid="client-scope-save"
        >
          {saving ? <><Loader2 className="mr-1.5 h-4 w-4 animate-spin" />保存中</> : '保存分配'}
        </Button>
      ) : undefined}
    >
      {loading ? (
        <div className="flex items-center justify-center py-8 text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载访问范围</div>
      ) : loadError ? (
        <EmptyState><AlertTriangle className="mx-auto mb-2 h-5 w-5" />{loadError}<div><Button variant="link" onClick={() => void load()}>重试</Button></div></EmptyState>
      ) : scope ? (
        <div className="space-y-3" data-testid="client-scope-editor" data-scope-kind={scope.scope_kind}>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2.5 text-xs">
            <Badge variant="outline">{modeLabel}</Badge>
            <span className="text-muted-foreground">范围版本 {scope.version} · 权限版本 {scope.permission_version}</span>
          </div>
          {(scope.brand_ids.length === 0 || scope.subject_kind === 'organization_owner') && (
            <div className={cn(
              'rounded-lg border px-3 py-2.5 text-xs leading-5',
              scope.scope_kind === 'organization_assigned' && scope.subject_kind !== 'organization_owner'
                ? 'border-amber-500/30 bg-amber-500/8 text-amber-800 dark:text-amber-200'
                : 'border-sky-500/25 bg-sky-500/8 text-sky-800 dark:text-sky-200',
            )} data-testid="client-scope-empty-state">{emptyCopy}</div>
          )}
          {scope.stale_brand_ids.length > 0 && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/8 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-200">
              检测到 {scope.stale_brand_ids.length} 条已失效的历史分配；它们不会进入有效权限，保存后会清理。
            </div>
          )}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索客户名称或品牌 ID"
              className="h-9 pl-9"
              aria-label="搜索可分配客户"
            />
          </div>
          <div className="max-h-72 overflow-y-auto rounded-lg border border-border p-1" data-testid="client-scope-options">
            {filteredBrands.length === 0 ? (
              <div className="px-3 py-8 text-center text-sm text-muted-foreground">没有合法可分配客户</div>
            ) : filteredBrands.map((brand) => (
              <label key={brand.id} className={cn(
                'flex min-h-11 items-center gap-3 rounded-md px-3 py-2 text-sm',
                scope.editable ? 'cursor-pointer hover:bg-muted/50' : 'cursor-default opacity-80',
              )}>
                <input
                  type="checkbox"
                  className="h-4 w-4 shrink-0 accent-primary"
                  checked={effectiveSet.has(brand.id)}
                  disabled={!scope.editable}
                  onChange={(event) => setSelected((current) => event.target.checked
                    ? [...new Set([...current, brand.id])]
                    : current.filter((id) => id !== brand.id))}
                  aria-label={`分配客户 ${brand.name}`}
                />
                <span className="min-w-0 flex-1 break-words text-foreground" title={brand.name}>{brand.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">ID {brand.id}</span>
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>已选 {scope.subject_kind === 'organization_owner' ? scope.effective_brand_ids.length : normalizedSelected.length} · 可分配 {scope.available_brands.length}</span>
            {scope.membership_status && <span>席位状态：{scope.membership_status}</span>}
          </div>
          {scope.editable && (
            <Input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="填写调整原因（必填）"
              maxLength={500}
              aria-label="客户访问范围调整原因"
              data-testid="client-scope-reason"
            />
          )}
        </div>
      ) : null}
    </Section>
  );
}


export default function UserManagement() {
  const [users, setUsers] = useState<UserListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(30);
  const [totalPages, setTotalPages] = useState(0);
  const [search, setSearch] = useState('');
  const [identity, setIdentity] = useState<'all' | BusinessIdentity>('all');
  const [attentionOnly, setAttentionOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [gateMetrics, setGateMetrics] = useState<AgreementGateMetrics | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>('overview');
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [changeDraft, setChangeDraft] = useState<ChangeDraft | null>(null);
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [providerCandidates, setProviderCandidates] = useState<UserListItem[]>([]);
  const [providerChoice, setProviderChoice] = useState<string>('platform');
  const [channelMultiplier, setChannelMultiplier] = useState('1.0000');
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordReason, setPasswordReason] = useState('');
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [walletDialogOpen, setWalletDialogOpen] = useState(false);
  const [walletPointType, setWalletPointType] = useState<'paid' | 'bonus'>('paid');
  const [walletOperation, setWalletOperation] = useState<'add' | 'deduct' | 'set'>('add');
  const [walletAmount, setWalletAmount] = useState('');
  const [walletReason, setWalletReason] = useState('');
  const [walletSaving, setWalletSaving] = useState(false);
  const [downgradeWizardOpen, setDowngradeWizardOpen] = useState(false);
  // [P0-C §9] 补录凭证 —— 后端端点早就在,缺的一直是这个入口
  const [backfillOpen, setBackfillOpen] = useState(false);
  const [backfillReason, setBackfillReason] = useState('');
  const [backfillSaving, setBackfillSaving] = useState(false);
  const [listCollapsed, setListCollapsed] = useState(false);
  const listRequestSequence = useRef(0);
  const listInFlightRef = useRef<{ key: string; request: Promise<void> } | null>(null);
  const detailRequestSequence = useRef(0);

  const loadUsers = useCallback((): Promise<void> => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (search.trim()) params.set('search', search.trim());
    if (identity !== 'all') params.set('identity', identity);
    if (attentionOnly) params.set('attention_only', 'true');
    const requestKey = params.toString();
    if (listInFlightRef.current?.key === requestKey) return listInFlightRef.current.request;
    const requestSequence = ++listRequestSequence.current;
    setListLoading(true);
    setListError(null);
    const request = (async () => {
      try {
        const response = await authApi.get(`/api/admin/user-governance/users?${params}`);
        if (requestSequence !== listRequestSequence.current) return;
        const nextUsers = response.data.users as UserListItem[];
        const nextTotal = Number(response.data.total || 0);
        const nextTotalPages = Number(response.data.total_pages || 0);
        const clampedPage = Math.min(page, Math.max(1, nextTotalPages));
        if (clampedPage !== page) {
          setUsers([]); setSelectedId(null); setTotal(nextTotal); setTotalPages(nextTotalPages);
          setPage(clampedPage);
          return;
        }
        setUsers(nextUsers);
        setTotal(nextTotal);
        setTotalPages(nextTotalPages);
        setSelectedId((current) => (
          current && nextUsers.some((user) => user.user_id === current)
            ? current
            : nextUsers[0]?.user_id ?? null
        ));
      } catch (error) {
        if (requestSequence === listRequestSequence.current) setListError(errorMessage(error));
      } finally {
        if (requestSequence === listRequestSequence.current) setListLoading(false);
      }
    })();
    listInFlightRef.current = { key: requestKey, request };
    void request.finally(() => {
      if (listInFlightRef.current?.request === request) listInFlightRef.current = null;
    });
    return request;
  }, [attentionOnly, identity, page, pageSize, search]);

  const loadDetail = useCallback(async (userId: number) => {
    const requestSequence = ++detailRequestSequence.current;
    setDetail((current) => current?.overview.user_id === userId ? current : null);
    setDetailLoading(true);
    setDetailError(null);
    try {
      const response = await authApi.get(`/api/admin/user-governance/users/${userId}`);
      if (requestSequence === detailRequestSequence.current) setDetail(response.data as UserDetail);
    } catch (error) {
      if (requestSequence === detailRequestSequence.current) {
        setDetail(null);
        setDetailError(errorMessage(error));
      }
    } finally {
      if (requestSequence === detailRequestSequence.current) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadUsers(), 250);
    return () => window.clearTimeout(timer);
  }, [loadUsers]);

  useEffect(() => { setPage(1); }, [search, identity, attentionOnly]);

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1024px)');
    const restoreMobileList = (event: MediaQueryListEvent | MediaQueryList) => {
      if (!event.matches) setListCollapsed(false);
    };
    restoreMobileList(desktop);
    desktop.addEventListener('change', restoreMobileList);
    return () => desktop.removeEventListener('change', restoreMobileList);
  }, []);

  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    if (selectedId) void loadDetail(selectedId);
  }, [loadDetail, selectedId]);

  useEffect(() => {
    authApi.get('/api/admin/user-governance/platform-direct-readiness')
      .then((response) => setReadiness(response.data.readiness as Readiness))
      .catch(() => setReadiness(null));
    authApi.get('/api/admin/user-governance/agreement-gate-metrics')
      .then((response) => setGateMetrics(response.data.metrics as AgreementGateMetrics))
      .catch(() => setGateMetrics(null));
  }, []);

  const selectedListItem = useMemo(
    () => users.find((user) => user.user_id === selectedId) || null,
    [selectedId, users],
  );

  const selectUser = (userId: number) => {
    if (selectedId === userId) return;
    detailRequestSequence.current += 1;
    setDetail(null);
    setDetailError(null);
    setChangeDraft(null);
    setReason('');
    setPasswordDialogOpen(false); setNewPassword(''); setConfirmPassword(''); setPasswordReason('');
    setWalletDialogOpen(false); setWalletAmount(''); setWalletReason('');
    setDowngradeWizardOpen(false);
    setProviderCandidates([]);
    setSelectedId(userId);
  };

  const openBusinessIdentityChange = () => {
    if (!detail) return;
    const next: BusinessIdentity = detail.overview.business_identity === 'ordinary_user' ? 'service_provider' : 'ordinary_user';
    if (next === 'ordinary_user') {
      setDowngradeWizardOpen(true);
      return;
    }
    // [返修 F-preblock · Master SSOT v1.8 §4.1 / 手册 §1.6 事故#1]
    // 旧客户端预拦(有商业归属→toast+return)已废:后端 change_business_identity 已实现
    // 原子升级——旧归属在同事务转换为「原服务商→该用户」渠道进货关系(默认成本透传,
    // D2 裁决),服务商发展下级服务商是正常商业路径,前端不得抢先拦截。
    const hasProvider = Boolean(detail.relationships.commercial.provider);
    setChangeDraft({
      kind: 'business_identity',
      title: '确认调整业务身份',
      beforeLabel: detail.overview.business_identity_label,
      afterLabel: IDENTITY_LABEL[next],
      impact: next === 'service_provider'
        ? (hasProvider
          ? '保存后该账号原子升级为服务商：当前商业服务归属将同事务转换为「原服务商 → 该用户」的渠道进货关系（默认成本透传，可事后在渠道关系中调整系数）。注册来源、历史订单、组织关系均不变，失败整体回滚。'
          : '保存后该账号可进入服务商业务链；不会自动创建渠道关系、修改价格或改动历史订单。')
        : '仅在没有商业绑定客户、现役渠道关系、库存和待支付进货订单时允许降级；不会删除历史兼容角色或账务记录。',
      identity: next,
    });
    setReason('');
  };

  const openAccessChange = () => {
    if (!detail) return;
    const next = detail.overview.platform_access !== 'administrator';
    setChangeDraft({
      kind: 'platform_access',
      title: next ? '确认授予内部管理员权限' : '确认移除内部管理员权限',
      beforeLabel: detail.overview.platform_access_label,
      afterLabel: next ? '内部管理员' : '无管理员权限',
      impact: '该操作会立即推进权限版本并使旧登录凭证失效；业务身份、商业关系和历史角色不变。',
      administrator: next,
    });
    setReason('');
  };

  const openAccountStatusChange = () => {
    if (!detail) return;
    const next = !detail.overview.is_active;
    setChangeDraft({
      kind: 'account_status',
      title: next ? '确认恢复账号' : '确认停用账号',
      beforeLabel: detail.overview.is_active ? '正常' : '已停用',
      afterLabel: next ? '正常' : '已停用',
      impact: '该专门操作会立即推进权限版本并使旧会话失效；不会修改业务身份、客户归属、余额、库存或商业关系。',
      active: next,
    });
    setReason('');
  };

  const openBindingChange = () => {
    if (!detail) return;
    if (detail.overview.business_identity !== 'ordinary_user') {
      lazyToast.error('商业服务归属只适用于普通用户；服务商上下游请使用渠道关系治理');
      return;
    }
    const current = detail.relationships.commercial;
    setProviderChoice(current.provider ? String(current.provider.user_id) : 'platform');
    void loadServiceProviderCandidates();
    setChangeDraft({
      kind: 'commercial_binding',
      title: '预览商业服务归属变更',
      beforeLabel: current.provider
        ? `${current.provider.display_name}（用户 #${current.provider.user_id}）`
        : '平台直营',
      afterLabel: current.provider ? current.provider.display_name : '平台直营',
      impact: '只修改当前商业服务绑定；注册邀请归属、渠道层级、价格配置、余额和历史订单均不改变。',
      providerUserId: current.provider?.user_id ?? null,
    });
    setReason('');
  };

  const loadServiceProviderCandidates = async () => {
    try {
      const response = await authApi.get('/api/admin/user-governance/users?page=1&page_size=100&identity=service_provider');
      const candidates = response.data.users as UserListItem[];
      setProviderCandidates(candidates);
      return candidates;
    } catch {
      setProviderCandidates([]);
      return [];
    }
  };

  const openChannelRelationshipChange = () => {
    if (!detail || detail.overview.business_identity !== 'service_provider') return;
    const current = detail.relationships.channel;
    const choice = current.upstream ? String(current.upstream.user_id) : 'platform';
    const multiplierBps = current.cost_multiplier_bps ?? 10000;
    setProviderChoice(choice);
    setChannelMultiplier(formatMultiplierBps(multiplierBps));
    void loadServiceProviderCandidates();
    setChangeDraft({
      kind: 'channel_relationship',
      title: '预览直属渠道关系变更',
      beforeLabel: current.upstream
        ? `${current.upstream.display_name} · 系数 ${formatMultiplierBps(multiplierBps)}`
        : '平台根渠道',
      afterLabel: current.upstream
        ? `${current.upstream.display_name} · 系数 ${formatMultiplierBps(multiplierBps)}`
        : '平台根渠道',
      impact: '只调整该服务商今后新进货使用的直属上游和系数；不会改写邀请记录、普通客户归属、历史报价、待支付订单或已支付订单快照。',
      upstreamUserId: current.upstream?.user_id ?? null,
      costMultiplierBps: multiplierBps,
    });
    setReason('');
  };

  const updateProviderPreview = (value: string) => {
    setProviderChoice(value);
    setChangeDraft((draft) => {
      if (!draft || (draft.kind !== 'commercial_binding' && draft.kind !== 'channel_relationship')) return draft;
      const provider = providerCandidates.find((candidate) => String(candidate.user_id) === value);
      if (draft.kind === 'channel_relationship') {
        const bps = Math.round(Number(channelMultiplier) * 10000);
        return {
          ...draft,
          upstreamUserId: value === 'platform' ? null : Number(value),
          costMultiplierBps: bps,
          afterLabel: provider
            ? `${provider.display_name} · 系数 ${formatMultiplierBps(bps)}`
            : '平台根渠道',
        };
      }
      return {
        ...draft,
        providerUserId: value === 'platform' ? null : Number(value),
        afterLabel: provider ? `${provider.display_name}（用户 #${provider.user_id}）` : '平台直营',
      };
    });
  };

  const updateChannelMultiplier = (value: string) => {
    setChannelMultiplier(value);
    setChangeDraft((draft) => {
      if (!draft || draft.kind !== 'channel_relationship') return draft;
      const parsed = Number(value);
      const bps = Number.isFinite(parsed) ? Math.round(parsed * 10000) : 0;
      const provider = providerCandidates.find((candidate) => candidate.user_id === draft.upstreamUserId);
      return {
        ...draft,
        costMultiplierBps: bps,
        afterLabel: draft.upstreamUserId && provider
          ? `${provider.display_name} · 系数 ${Number.isFinite(parsed) ? formatMultiplier(parsed) : '无效'}`
          : '平台根渠道',
      };
    });
  };

  const submitChange = async () => {
    if (!changeDraft || !detail || !reason.trim()) return;
    const userId = detail.overview.user_id;
    const requestId = `admin-user-${changeDraft.kind}-${safeRandomUUID()}`;
    let url = '';
    let expectedVersion = 1;
    let payload: Record<string, unknown> = { reason: reason.trim() };
    if (changeDraft.kind === 'business_identity') {
      url = `/api/admin/user-governance/users/${userId}/business-identity`;
      expectedVersion = detail.overview.versions.business_identity;
      payload.business_identity = changeDraft.identity;
    } else if (changeDraft.kind === 'commercial_binding') {
      url = `/api/admin/user-governance/users/${userId}/commercial-service-binding`;
      expectedVersion = detail.overview.versions.commercial_binding;
      payload.provider_user_id = changeDraft.providerUserId ?? null;
    } else if (changeDraft.kind === 'channel_relationship') {
      url = `/api/admin/user-governance/users/${userId}/channel-relationship`;
      expectedVersion = detail.overview.versions.channel_relationship;
      payload.upstream_user_id = changeDraft.upstreamUserId ?? null;
      payload.cost_multiplier_bps = changeDraft.costMultiplierBps ?? 10000;
    } else if (changeDraft.kind === 'platform_access') {
      url = `/api/admin/user-governance/users/${userId}/platform-access`;
      expectedVersion = detail.overview.versions.platform_access;
      payload.administrator = changeDraft.administrator;
    } else {
      url = `/api/admin/cross-tenant-governance/users/${userId}/account-status`;
      expectedVersion = detail.overview.versions.account_status;
      payload.active = changeDraft.active;
      payload.confirmation = 'CHANGE_ACCOUNT_STATUS';
    }
    payload.expected_version = expectedVersion;
    setSaving(true);
    try {
      await authApi.put(url, payload, { headers: { 'X-Request-ID': requestId } });
      lazyToast.success('已保存，并记录完整操作证据');
      setChangeDraft(null);
      setReason('');
      await Promise.all([loadDetail(userId), loadUsers()]);
    } catch (error) {
      lazyToast.error(errorMessage(error));
      if ((error as { response?: { status?: number } }).response?.status === 409) {
        await loadDetail(userId);
      }
    } finally {
      setSaving(false);
    }
  };

  /** [P0-C §9] 补录归属凭证 —— 调**现成**端点,不新建后端实现。
   *  成功后重新拉详情与列表:灯该当场熄灭(判据侧与处置侧共用同一段 SQL)。 */
  const submitBackfill = async () => {
    if (!detail || backfillReason.trim().length < 2) return;
    const userId = detail.overview.user_id;
    setBackfillSaving(true);
    try {
      await authApi.post(
        `/api/admin/user-governance/users/${userId}/commercial-service-binding/audit-backfill`,
        {
          expected_version: detail.overview.versions.commercial_binding,
          reason: backfillReason.trim(),
        },
        { headers: { 'X-Request-ID': `admin-backfill-${safeRandomUUID()}` } },
      );
      lazyToast.success('凭证已补录 · 归属本身未改动');
      setBackfillOpen(false);
      setBackfillReason('');
      await Promise.all([loadDetail(userId), loadUsers()]);
    } catch (error) {
      lazyToast.error(errorMessage(error));
      if ((error as { response?: { status?: number } }).response?.status === 409) {
        await loadDetail(userId);
      }
    } finally {
      setBackfillSaving(false);
    }
  };

  const closePasswordDialog = () => {
    if (passwordSaving) return;
    setPasswordDialogOpen(false);
    setNewPassword('');
    setConfirmPassword('');
    setPasswordReason('');
  };

  const submitPasswordReset = async () => {
    if (!detail || newPassword.length < 8 || newPassword !== confirmPassword || passwordReason.trim().length < 2) return;
    const userId = detail.overview.user_id;
    setPasswordSaving(true);
    try {
      await authApi.put(
        `/api/admin/user-governance/users/${userId}/password`,
        {
          new_password: newPassword,
          expected_version: detail.overview.versions.password_security,
          reason: passwordReason.trim(),
        },
        { headers: { 'X-Request-ID': `admin-user-password-${safeRandomUUID()}` } },
      );
      lazyToast.success('密码已重置；该用户下次登录必须修改密码');
      setPasswordDialogOpen(false);
      setNewPassword('');
      setConfirmPassword('');
      setPasswordReason('');
      await loadDetail(userId);
    } catch (error) {
      lazyToast.error(errorMessage(error));
      if ((error as { response?: { status?: number } }).response?.status === 409) await loadDetail(userId);
    } finally {
      setPasswordSaving(false);
    }
  };

  const closeWalletDialog = (force = false) => {
    if (walletSaving && !force) return;
    setWalletDialogOpen(false);
    setWalletPointType('paid');
    setWalletOperation('add');
    setWalletAmount('');
    setWalletReason('');
  };

  const submitWalletAdjustment = async () => {
    if (!detail || walletSaving) return;
    const amount = Number(walletAmount);
    if (!Number.isSafeInteger(amount) || amount < 0 || walletReason.trim().length < 2) return;
    if ((walletOperation === 'add' || walletOperation === 'deduct') && amount === 0) return;
    const userId = detail.overview.user_id;
    setWalletSaving(true);
    try {
      await authApi.put(
        `/api/admin/user-governance/users/${userId}/wallet-adjustment`,
        {
          point_type: walletPointType,
          operation: walletOperation,
          amount,
          expected_version: detail.overview.versions.wallet_adjustment,
          reason: walletReason.trim(),
        },
        { headers: { 'X-Request-ID': `admin-user-wallet-${safeRandomUUID()}` } },
      );
      lazyToast.success('算力已校正，并记录操作人、时间、IP、原因和独立校正流水');
      closeWalletDialog(true);
      await Promise.all([loadDetail(userId), loadUsers()]);
    } catch (error) {
      lazyToast.error(errorMessage(error));
      if ((error as { response?: { status?: number } }).response?.status === 409) await loadDetail(userId);
    } finally {
      setWalletSaving(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1680px] px-3 py-4 sm:px-5 lg:px-6" data-testid="admin-user-governance-page">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-medium text-primary">
            <Shield className="h-3.5 w-3.5" /> 仅平台管理员可见
          </div>
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-foreground sm:text-2xl">用户管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">注册来源保留事实；合格服务商邀请可建立首次服务关系，后续调整统一通过治理流程留痕。</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void Promise.all([loadUsers(), selectedId ? loadDetail(selectedId) : Promise.resolve()])}>
          <RefreshCw className="mr-2 h-4 w-4" />刷新
        </Button>
      </header>

      <div className={cn(
        'grid min-h-[680px] items-start rounded-xl border border-border bg-card shadow-sm',
        listCollapsed ? 'lg:grid-cols-[64px_minmax(0,1fr)]' : 'lg:grid-cols-[330px_minmax(0,1fr)]',
      )}>
        <aside className={cn(
          'min-w-0 flex-col border-border bg-muted/15 lg:sticky lg:top-4 lg:flex lg:max-h-[calc(100vh-2rem)] lg:self-start lg:border-r',
          selectedId ? 'hidden lg:flex' : 'flex',
        )} aria-label="用户列表">
          <div className={cn('border-b border-border', listCollapsed ? 'hidden p-2 lg:block' : 'p-3')}>
            <div className={cn('mb-2 items-center', listCollapsed ? 'hidden lg:flex lg:justify-center' : 'flex justify-between')}>
              {!listCollapsed && <span className="text-xs font-medium text-muted-foreground">账号导航</span>}
              <Button variant="ghost" size="icon" className="hidden h-8 w-8 lg:inline-flex" onClick={() => setListCollapsed(value => !value)} aria-label={listCollapsed ? '展开用户导航' : '收起用户导航'}>
                {listCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
              </Button>
            </div>
            {!listCollapsed && <>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索姓名、账号或手机号"
                className="h-10 pl-9"
                aria-label="搜索用户"
              />
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Select value={identity} onValueChange={(value) => setIdentity(value as typeof identity)}>
                <SelectTrigger className="h-9" aria-label="业务身份筛选"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部身份</SelectItem>
                  <SelectItem value="ordinary_user">普通用户</SelectItem>
                  <SelectItem value="service_provider">服务商</SelectItem>
                </SelectContent>
              </Select>
              <Button
                variant={attentionOnly ? 'secondary' : 'outline'}
                size="sm"
                className="h-9"
                onClick={() => setAttentionOnly((value) => !value)}
                aria-pressed={attentionOnly}
              >
                <AlertTriangle className="mr-1.5 h-3.5 w-3.5" />待处理
              </Button>
            </div>
            <div className="mt-2 text-xs text-muted-foreground">共 {formatNumber(total)} 个账号</div>
            </>}
          </div>

          {!listCollapsed && <div className="min-h-0 flex-1 overflow-y-auto p-2" data-testid="user-list">
            {listLoading ? (
              <div className="grid place-items-center py-16 text-sm text-muted-foreground"><Loader2 className="mb-2 h-5 w-5 animate-spin" />加载中</div>
            ) : listError ? (
              <EmptyState><AlertTriangle className="mx-auto mb-2 h-5 w-5" />{listError}<Button variant="link" onClick={() => void loadUsers()}>重试</Button></EmptyState>
            ) : users.length === 0 ? (
              <EmptyState>没有符合条件的用户</EmptyState>
            ) : users.map((user) => (
              <button
                key={user.user_id}
                type="button"
                onClick={() => selectUser(user.user_id)}
                className={cn(
                  'mb-1 flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring',
                  selectedId === user.user_id && 'bg-primary/8 ring-1 ring-primary/20',
                )}
                data-testid={`user-row-${user.user_id}`}
              >
                <Initials name={user.display_name} />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="max-w-full break-words text-sm font-medium text-foreground" title={user.display_name}>{user.display_name}</span>
                    <IdentityBadge identity={user.business_identity} />
                    {user.platform_access === 'administrator' && <ShieldCheck className="h-3.5 w-3.5 text-primary" aria-label="内部管理员" />}
                  </span>
                  <span className="mt-1 block truncate text-xs text-muted-foreground" title={`${user.username} · ID ${user.user_id}`}>
                    {user.username} · ID {user.user_id}
                  </span>
                  {/* [补充工单 2026-08-06] 账号来源必须在**列表行**上就看得出来。
                      Owner 原话:「就是没有显示是否是用户在团队与席位里面自建的账号」——
                      他看的是列表,不是详情页。详情页改好之后两处仍然不一致:
                      点进去写着「组织操作员」,退出来又变回一个看不出来历的普通用户。
                      🔴 用**文字**不用纯图标/色块 —— 要的是一眼看出来,不是悬停才知道。 */}
                  <span className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="shrink-0">{user.business_identity === 'service_provider'
                        ? '渠道供货'
                        : user.service_mode === 'platform_direct' ? '平台直营' : '服务商承接'}</span>
                      <span className="shrink-0 text-muted-foreground/50">·</span>
                      {user.account_origin === 'organization_member' ? (
                        <span
                          className="truncate rounded bg-sky-500/10 px-1.5 py-0.5 font-medium text-sky-700 dark:text-sky-300"
                          title={`团队成员 · ${user.organization_name || '团队信息缺失'}`}
                        >
                          团队成员 · {user.organization_name || '团队信息缺失'}
                        </span>
                      ) : (
                        <span className="shrink-0">自助注册</span>
                      )}
                    </span>
                    {user.needs_attention && (
                      <span className="font-medium text-amber-700 dark:text-amber-300">
                        {user.attention_label || '旧人工归属待复核'}
                      </span>
                    )}
                  </span>
                </span>
                <ChevronRight className="mt-3 h-4 w-4 shrink-0 text-muted-foreground" />
              </button>
            ))}
          </div>}
          {!listCollapsed && totalPages > 1 && <div className="flex items-center justify-between gap-2 border-t p-2 text-xs text-muted-foreground"><Button variant="ghost" size="sm" disabled={page <= 1 || listLoading} onClick={() => setPage(value => Math.max(1, value - 1))}>上一页</Button><span>{page} / {totalPages}</span><Button variant="ghost" size="sm" disabled={page >= totalPages || listLoading} onClick={() => setPage(value => Math.min(totalPages, value + 1))}>下一页</Button></div>}
        </aside>

        <main className={cn('min-w-0 bg-card', selectedId ? 'block' : 'hidden lg:block')} aria-live="polite">
          {!selectedId ? (
            <div className="grid h-full min-h-[680px] place-items-center text-sm text-muted-foreground">从左侧选择一个用户</div>
          ) : detailLoading && !detail ? (
            <div className="grid h-full min-h-[680px] place-items-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-5 w-5 animate-spin" />加载用户详情</div>
          ) : detailError ? (
            <div className="grid h-full min-h-[680px] place-items-center px-6 text-center text-sm text-muted-foreground">
              <div><AlertTriangle className="mx-auto mb-2 h-6 w-6" />{detailError}<div><Button variant="link" onClick={() => void loadDetail(selectedId)}>重新加载</Button></div></div>
            </div>
          ) : detail ? (
            <>
              <div className="border-b border-border px-4 py-4 sm:px-6">
                <Button variant="ghost" size="sm" className="mb-2 -ml-2 lg:hidden" onClick={() => { setSelectedId(null); setDetail(null); }}>
                  <ArrowLeft className="mr-1.5 h-4 w-4" />返回用户列表
                </Button>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <Initials name={detail.overview.display_name} />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="max-w-full break-words text-lg font-semibold text-foreground" title={detail.overview.display_name}>{detail.overview.display_name}</h2>
                        <IdentityBadge identity={detail.overview.business_identity} />
                        <Badge variant={detail.overview.is_active ? 'outline' : 'destructive'}>{detail.overview.is_active ? '正常' : '已停用'}</Badge>
                      </div>
                      <div className="mt-1 break-words text-xs text-muted-foreground">
                        {detail.overview.username} · 用户 ID {detail.overview.user_id}
                        {detail.overview.company ? ` · ${detail.overview.company}` : ''}
                      </div>
                    </div>
                  </div>
                  {detail.relationships.notices.some((notice) => notice.severity !== 'info') && (
                    <Button variant="outline" size="sm" onClick={() => setActiveTab('relationships')}>
                      <AlertTriangle className="mr-1.5 h-4 w-4 text-amber-600" />需要核验
                    </Button>
                  )}
                </div>
              </div>

              <nav className="flex overflow-x-auto border-b border-border px-2 sm:px-4" aria-label="用户详情栏目">
                {TABS.map((tab) => {
                  const Icon = tab.icon;
                  return (
                    <button
                      key={tab.key}
                      type="button"
                      onClick={() => setActiveTab(tab.key)}
                      className={cn(
                        'flex h-12 shrink-0 items-center gap-1.5 border-b-2 px-3 text-sm outline-none transition focus-visible:ring-2 focus-visible:ring-ring',
                        activeTab === tab.key
                          ? 'border-primary font-medium text-foreground'
                          : 'border-transparent text-muted-foreground hover:text-foreground',
                      )}
                      aria-current={activeTab === tab.key ? 'page' : undefined}
                    >
                      <Icon className="h-4 w-4" />{tab.label}
                    </button>
                  );
                })}
              </nav>

              <div className="min-h-[520px] px-4 py-5 sm:px-6" data-testid={`tab-${activeTab}`}>
                {activeTab === 'overview' && (
                  <div className="space-y-5">
                    <div className="grid gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-4">
                      {[
                        ['可用算力', formatNumber(detail.overview.total_points)],
                        ['服务客户', formatNumber(detail.overview.customer_count)],
                        ['品牌数量', formatNumber(detail.overview.brand_count)],
                        ['累计充值算力', formatNumber(detail.overview.total_recharged_points)],
                      ].map(([label, value]) => (
                        <div key={label} className="bg-card px-4 py-4">
                          <div className="text-xs text-muted-foreground">{label}</div>
                          <div className="mt-1 text-lg font-semibold text-foreground">{value}</div>
                        </div>
                      ))}
                    </div>
                    <Section
                      title="业务身份"
                      description="经营身份只分普通用户与服务商，不由历史角色名称决定。"
                      action={<Button variant="outline" size="sm" onClick={openBusinessIdentityChange}>调整身份</Button>}
                    >
                      <FactRow label="当前身份" value={detail.overview.business_identity_label} />
                      <FactRow label="账号状态" value={detail.overview.is_active ? '正常使用' : '已停用'} />
                    </Section>
                    <Section title="平台权限" description="管理员权限与业务身份独立。">
                      <FactRow label="平台权限" value={detail.overview.platform_access_label} />
                      <FactRow label="最近登录" value={formatTime(detail.overview.last_login_at)} />
                    </Section>
                  </div>
                )}

                {activeTab === 'relationships' && (
                  <div className="space-y-5">
                    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-muted/35 px-4 py-3">
                      <div>
                        <div className="text-sm font-medium text-foreground">
                          {detail.overview.business_identity === 'service_provider' ? '直属供货关系管理' : '客户服务归属管理'}
                        </div>
                        <div className="mt-1 text-xs leading-5 text-muted-foreground">
                          {detail.overview.business_identity === 'service_provider'
                            ? '设置该服务商今后向谁进货及采用的价格系数。'
                            : '设置该客户当前由平台直营或指定服务商承接。'}
                        </div>
                      </div>
                      {detail.overview.business_identity === 'service_provider'
                        ? <Button variant="outline" size="sm" onClick={openChannelRelationshipChange}>调整直属上游</Button>
                        : <Button variant="outline" size="sm" onClick={openBindingChange}>修改归属</Button>}
                    </div>

                    {/* 🔴 [P0-C · WO_INVREL_P0_HOTFIX §9] 补录凭证入口。
                        后端服务层与端点 2026-08 就已完整实现,**前端零入口** ——
                        谁都点不到,所以"安排了几批一直没补上"。这是本仓记过的
                        「死函数 = 复审漏接线」同型复发,这里把线接上。
                        判据:删掉这个 onClick,浏览器测试必须转红。 */}
                    {detail.overview.needs_attention && (
                      <div
                        className="rounded-lg border border-amber-500/30 bg-amber-500/8 px-4 py-3 text-sm"
                        data-testid="backfill-card"
                      >
                        <div className="font-medium text-foreground">
                          {detail.overview.attention_label || '旧人工归属缺少直接审计'}
                        </div>
                        <p className="mt-1 text-xs leading-5 text-muted-foreground">
                          这条归属当时是人工设置的,系统里没有留下"为什么这么定"的凭证。
                          补录只新增一条凭证记录,<strong>不会改动归属本身</strong>,也不会影响对方现在的任何功能。
                        </p>
                        <Button
                          variant="outline"
                          size="sm"
                          className="mt-2"
                          data-testid="backfill-open"
                          onClick={() => { setBackfillReason(''); setBackfillOpen(true); }}
                        >
                          补录凭证
                        </Button>
                      </div>
                    )}
                    {detail.relationships.notices.map((notice) => (
                      <div key={notice.code} className={cn(
                        'rounded-lg border px-4 py-3 text-sm',
                        notice.severity === 'critical' && 'border-red-500/30 bg-red-500/8',
                        notice.severity === 'warning' && 'border-amber-500/30 bg-amber-500/8',
                        notice.severity === 'info' && 'border-sky-500/25 bg-sky-500/8',
                      )}>
                        <div className="font-medium text-foreground">{notice.title}</div>
                        <div className="mt-1 text-xs leading-5 text-muted-foreground">{notice.detail}</div>
                      </div>
                    ))}
                    {/* [工单 2026-08-06 §3] 这个面板此前对**组织操作员账号**也只写一句「无邀请记录」，
                        看的人分不清那是正常还是注册闸失守 —— 实际触发过一次误报 P0。
                        现在按账号来源分三态渲染，让人一眼看出「这是正常的」还是「这个要查」。 */}
                    {detail.relationships.registration.account_origin === 'organization_member' ? (
                      <Section
                        title="账号来源 · 组织操作员"
                        description="该账号由团队邀请创建，走的是组织邀请流程，不经过注册闸，因此本来就没有注册邀请归属记录。"
                      >
                        <FactRow
                          label="所属团队"
                          value={detail.relationships.registration.organization?.name
                            ? `${detail.relationships.registration.organization.name}${
                                detail.relationships.registration.organization.organization_id
                                  ? `（团队 #${detail.relationships.registration.organization.organization_id}）` : ''}`
                            : '团队信息缺失 · 需人工核实'}
                        />
                        <FactRow
                          label="邀请人（团队所有者）"
                          value={detail.relationships.registration.organization?.owner
                            ? `${detail.relationships.registration.organization.owner.display_name}（用户 #${detail.relationships.registration.organization.owner.user_id}）`
                            : '所有者账号不存在 · 需人工核实'}
                        />
                        <FactRow label="加入时间" value={formatTime(detail.relationships.registration.organization?.joined_at)} />
                        <FactRow label="席位状态" value={detail.relationships.registration.organization?.membership_status || '—'} />
                        <EvidenceView evidence={detail.relationships.registration.evidence} />
                      </Section>
                    ) : (
                      <Section title="注册邀请归属" description="记录账号从哪里注册；合格服务商邀请可建立首次服务关系，后续管理员调整不会改写这条来源记录。">
                        <FactRow
                          label="邀请人"
                          value={detail.relationships.registration.inviter
                            ? `${detail.relationships.registration.inviter.display_name}（用户 #${detail.relationships.registration.inviter.user_id}）`
                            : (detail.relationships.registration.evidence.status === 'incomplete'
                              ? '无邀请归属记录 · 自助注册账号，需人工核实来历'
                              : '无邀请归属记录（注册邀请闸上线前的老账号）')}
                        />
                        <FactRow label="记录时间" value={formatTime(detail.relationships.registration.registered_at)} />
                        <EvidenceView evidence={detail.relationships.registration.evidence} />
                      </Section>
                    )}
                    {detail.overview.business_identity === 'service_provider' ? (
                      <>
                        <Section
                          title="直属供货关系"
                          description="决定该服务商今后的进货上游和价格系数；历史报价与订单快照不会被改写。"
                        >
                          <FactRow
                            label="直属上游"
                            value={detail.relationships.channel.upstream
                              ? `${detail.relationships.channel.upstream.display_name}（用户 #${detail.relationships.channel.upstream.user_id}）`
                              : '平台直接供货'}
                          />
                          <FactRow label="关系版本" value={detail.relationships.channel.relationship_version || '—'} />
                          <FactRow
                            label="进货系数"
                            value={detail.relationships.channel.cost_multiplier_bps
                              ? formatMultiplierBps(detail.relationships.channel.cost_multiplier_bps)
                              : '平台直接供货，不叠加上游系数'}
                          />
                        </Section>
                        <Section
                          title="客户服务归属记录"
                          description="这项只记录普通客户的服务承接事实，不决定服务商进货。服务商当前供应关系以上方直属供货关系为准。"
                        >
                          <FactRow
                            label="记录状态"
                            value={detail.relationships.commercial.provider
                              ? `${detail.relationships.commercial.provider.display_name}（用户 #${detail.relationships.commercial.provider.user_id}）`
                              : '不适用（没有普通客户服务绑定）'}
                            hint={detail.relationships.commercial.provider?.company}
                          />
                          {detail.relationships.commercial.provider && (
                            <>
                              <FactRow label="记录来源" value={detail.relationships.commercial.binding_source_label} />
                              <FactRow label="记录时间" value={formatTime(detail.relationships.commercial.bound_at)} />
                              <EvidenceView evidence={detail.relationships.commercial.evidence} />
                            </>
                          )}
                        </Section>
                      </>
                    ) : (
                      <Section
                        title="当前商业服务归属"
                        description="决定该客户当前由平台直营还是指定服务商承接；注册邀请来源会单独保留。"
                      >
                        <FactRow
                          label="当前承接方"
                          value={detail.relationships.commercial.provider
                            ? `${detail.relationships.commercial.provider.display_name}（用户 #${detail.relationships.commercial.provider.user_id}）`
                            : '平台直营'}
                          hint={detail.relationships.commercial.provider?.company}
                        />
                        <FactRow label="关系来源" value={detail.relationships.commercial.binding_source_label} />
                        <FactRow label="绑定时间" value={formatTime(detail.relationships.commercial.bound_at)} />
                        <FactRow label="关系版本" value={`第 ${detail.relationships.commercial.relationship_version} 版`} />
                        <EvidenceView evidence={detail.relationships.commercial.evidence} />
                      </Section>
                    )}
                    <Section title="平台直营准备状态" description="专用服务账号只对管理员可见；不会向普通用户或服务商返回。">
                      <FactRow label="状态" value={readiness?.label || '未能读取准备状态'} />
                      {readiness?.service_user && <FactRow label="专用服务账号" value={`${readiness.service_user.display_name}（用户 #${readiness.service_user.user_id}）`} />}
                      {readiness?.checks.map((check) => <div key={check} className="text-xs leading-5 text-muted-foreground">• {check}</div>)}
                      {!!readiness?.empty_retail_scopes?.length && (
                        <div
                          data-testid="empty-retail-catalog-alert"
                          className="mt-3 rounded-md border border-red-900/60 bg-red-950/30 px-3 py-2.5"
                        >
                          <div className="text-xs font-semibold leading-5 text-red-300">
                            {readiness.empty_retail_alert?.message
                              || `有 ${readiness.empty_retail_scopes.length} 个服务方的在售目录当前是空的，这些客户买不了任何东西。`}
                          </div>
                          {readiness.empty_retail_scopes.map((scope) => (
                            <div key={scope.version_id} className="mt-1 text-xs leading-5 text-red-300/85">
                              • 服务编号 {scope.scope_key}（版本 {scope.version_code || scope.version_id}）无在售商品
                            </div>
                          ))}
                          <div className="mt-1.5 text-xs leading-5 text-muted-foreground">
                            {readiness.empty_retail_alert?.repair_hint
                              || '到定价中心为这些服务方重新上架至少一个商品并重新发布。'}
                          </div>
                        </div>
                      )}
                    </Section>
                    <Section
                      title="协议门禁触发情况"
                      description="缺当前版本协议的账号登录时会被拦下要求补签。正常形态是当天补签、当天消失。"
                    >
                      {gateMetrics?.available ? (
                        <>
                          <FactRow
                            label={`近 ${gateMetrics.days} 天被拦次数`}
                            value={`${gateMetrics.total} 次 · 涉及 ${gateMetrics.distinct_users} 个账号`}
                          />
                          {gateMetrics.consecutive_days_with_triggers >= 2 && (
                            <div
                              data-testid="agreement-gate-stuck-alert"
                              className="mt-2 rounded-md border border-red-900/60 bg-red-950/30 px-3 py-2.5 text-xs leading-5 text-red-300"
                            >
                              已连续 {gateMetrics.consecutive_days_with_triggers} 天有账号被协议门禁拦下。
                              连续多天不消失，通常说明补签流程本身走不通，请立刻核查。
                            </div>
                          )}
                        </>
                      ) : (
                        <div className="text-xs leading-5 text-muted-foreground">暂无可用计数</div>
                      )}
                    </Section>
                  </div>
                )}

                {activeTab === 'pricing' && (
                  <div className="space-y-6">
                    <Section title="生效中的定价与结算链" description="这里只解释业务路径，不展示配置键、比例底层字段或原始数据。">
                      <FactRow label="客户售价" value={detail.pricing_and_settlement.customer_pricing_route} />
                      <FactRow label="进货路径" value={detail.pricing_and_settlement.procurement_pricing_route} />
                      <FactRow label="结算路径" value={detail.pricing_and_settlement.settlement_route} />
                      <FactRow label="规则来源" value={detail.pricing_and_settlement.pricing_source} />
                      {detail.pricing_and_settlement.special_pricing_note && <FactRow label="专属规则" value={detail.pricing_and_settlement.special_pricing_note} />}
                    </Section>
                    {/* [WP2 §5.4] 可操作定价治理入口(非只读解释):服务商可就地改专属报价系数/进货折扣。 */}
                    <Section title="专属定价治理" description="在此为该服务商设置专属报价系数与进货折扣，保存即时生效于其后续新业务。">
                      <PricingGovernancePanel
                        key={`pricing-gov-${detail.overview.user_id}`}
                        userId={detail.overview.user_id}
                        displayName={detail.overview.display_name}
                        isServiceProvider={detail.overview.business_identity === 'service_provider'}
                        onGoToIdentity={() => setActiveTab('overview')}
                        onSaved={async () => { await loadDetail(detail.overview.user_id); }}
                      />
                    </Section>
                  </div>
                )}

                {activeTab === 'clients' && (
                  <div className="space-y-6">
                    <ClientAccessAssignment key={`client-access-${detail.overview.user_id}`} userId={detail.overview.user_id} />
                    <Section title={`服务客户（${detail.clients_and_brands.clients.length}）`}>
                      {detail.clients_and_brands.clients.length === 0 ? <EmptyState>当前没有商业绑定客户</EmptyState> : (
                        <div className="divide-y divide-border rounded-lg border border-border">
                          {detail.clients_and_brands.clients.map((client) => (
                            <div key={client.customer_user_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                              <div className="min-w-0"><div className="break-words font-medium" title={client.display_name}>{client.display_name}</div><div className="text-xs text-muted-foreground">{client.username} · ID {client.customer_user_id}</div></div>
                              <div className="text-xs text-muted-foreground">{formatTime(client.bound_at)}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </Section>
                    <Section title={`品牌（${detail.clients_and_brands.brands.length}）`}>
                      {detail.clients_and_brands.brands.length === 0 ? <EmptyState>当前没有品牌</EmptyState> : (
                        <div className="divide-y divide-border rounded-lg border border-border">
                          {detail.clients_and_brands.brands.map((brand) => (
                            <div key={brand.brand_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                              <div className="min-w-0"><div className="break-words font-medium" title={brand.name}>{brand.name}</div><div className="text-xs text-muted-foreground">品牌 ID {brand.brand_id}</div></div>
                              <Badge variant="outline">{brand.industry || '未填写行业'}</Badge>
                            </div>
                          ))}
                        </div>
                      )}
                    </Section>
                  </div>
                )}

                {activeTab === 'wallet' && (
                  <div className="space-y-6">
                    <div className="rounded-lg border border-sky-500/20 bg-sky-500/7 px-4 py-3 text-xs leading-5 text-muted-foreground">
                      最高管理员可处理经核验的数据校正。校正不计入充值收入，并会记录操作人、时间、IP、原因及前后余额。
                    </div>
                    <Section
                      title="钱包摘要"
                      action={<Button size="sm" onClick={() => setWalletDialogOpen(true)}>校正算力</Button>}
                    >
                      <FactRow label="付费算力" value={formatNumber(detail.wallet_and_billing.paid_points)} />
                      <FactRow label="赠送算力" value={formatNumber(detail.wallet_and_billing.bonus_points)} />
                      <FactRow label="合计可用" value={formatNumber(detail.wallet_and_billing.total_points)} />
                    </Section>
                    <Section title="最近订单">
                      {detail.wallet_and_billing.recent_orders.length === 0 ? <EmptyState>暂无充值订单</EmptyState> : (
                        <div className="divide-y divide-border rounded-lg border border-border">
                          {detail.wallet_and_billing.recent_orders.map((order) => (
                            <div key={order.order_id} className="grid gap-1 px-4 py-3 text-sm sm:grid-cols-[minmax(0,1fr)_100px_130px]">
                              <div className="truncate font-mono text-xs" title={order.order_id}>{order.order_id}</div>
                              <div>¥{order.amount_yuan}</div><div className="text-xs text-muted-foreground">{order.status_label} · {formatTime(order.created_at)}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </Section>
                    <Section title="最近算力流水">
                      {detail.wallet_and_billing.recent_transactions.length === 0 ? <EmptyState>暂无算力流水</EmptyState> : (
                        <div className="divide-y divide-border rounded-lg border border-border">
                          {detail.wallet_and_billing.recent_transactions.map((tx) => (
                            <div key={tx.transaction_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                              <div className="min-w-0"><div className="break-words">{tx.description || tx.direction_label}</div><div className="text-xs text-muted-foreground">{formatTime(tx.created_at)}</div></div>
                              <div className={cn('font-medium', tx.points < 0 ? 'text-red-600' : 'text-emerald-600')}>{tx.points > 0 ? '+' : ''}{formatNumber(tx.points)}</div>
                            </div>
                          ))}
                        </div>
                      )}
                    </Section>
                  </div>
                )}

                {activeTab === 'demo' && <DemoGrantPanel userId={detail.overview.user_id} />}

                {activeTab === 'security' && (
                  <div className="space-y-5">
                    <Section
                      title="账号状态"
                      description="账号启停是独立治理能力，不会联动覆盖业务或商业关系。"
                      action={<Button variant={detail.overview.is_active ? 'destructive' : 'outline'} size="sm" onClick={openAccountStatusChange}>{detail.overview.is_active ? '停用账号' : '恢复账号'}</Button>}
                    >
                      <FactRow label="当前状态" value={detail.overview.is_active ? '正常' : '已停用'} />
                      <FactRow label="状态版本" value={`第 ${detail.overview.versions.account_status} 版`} />
                    </Section>
                    <Section
                      title="内部管理员权限"
                      description="平台权限独立于普通用户/服务商身份。"
                      action={<Button variant="outline" size="sm" onClick={openAccessChange}>{detail.overview.platform_access === 'administrator' ? '移除权限' : '授予权限'}</Button>}
                    >
                      <FactRow label="当前权限" value={detail.overview.platform_access_label} />
                      <FactRow label="权限版本" value={`第 ${detail.permissions_and_security.permission_version} 版`} />
                      <FactRow label="登录安全" value={detail.permissions_and_security.must_change_password ? '下次登录必须修改密码' : '无需强制改密'} />
                    </Section>
                    <Section title="历史兼容信息" description="默认折叠、只读；系统继续按旧 RBAC 兼容登录与鉴权，但不能再分配。">
                      <details className="group rounded-lg border border-border">
                        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">
                          查看 {detail.permissions_and_security.legacy_roles.length} 条历史角色记录
                          <ChevronRight className="h-4 w-4 transition group-open:rotate-90" />
                        </summary>
                        <div className="border-t border-border px-4 py-2">
                          {detail.permissions_and_security.legacy_roles.length === 0 ? (
                            <div className="py-3 text-sm text-muted-foreground">没有历史角色记录</div>
                          ) : detail.permissions_and_security.legacy_roles.map((role) => (
                            <FactRow key={role.role_id} label={role.historical_label} value={role.compatibility_status === 'active_compatibility' ? '现役鉴权兼容' : '只读历史兼容'} />
                          ))}
                        </div>
                      </details>
                    </Section>
                    <Section title="密码与账号安全">
                      <div className="flex flex-wrap gap-2">
                        <Button variant="outline" size="sm" onClick={() => setPasswordDialogOpen(true)}><KeyRound className="mr-2 h-4 w-4" />重置登录密码</Button>
                      </div>
                      <div className="mt-3 text-xs leading-5 text-muted-foreground">重置后旧登录凭证立即失效，用户下次登录必须修改密码。密码内容不会进入通知、响应或操作日志。</div>
                    </Section>
                  </div>
                )}

                {activeTab === 'logs' && (
                  <Section title="治理操作日志" description="身份、客户归属、渠道关系、管理员权限和密码安全操作的前后状态、原因、操作人、请求编号与版本证据。">
                    {detail.operation_logs.length === 0 ? <EmptyState>暂无新治理操作记录；历史关系证据见“服务关系”。</EmptyState> : (
                      <div className="space-y-3">
                        {detail.operation_logs.map((log) => (
                          <article key={log.audit_id} className="rounded-lg border border-border px-4 py-3">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div className="text-sm font-medium text-foreground">{log.action_label}</div>
                              <div className="text-xs text-muted-foreground">{formatTime(log.created_at)}</div>
                            </div>
                            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                              <span className="rounded bg-muted px-2 py-1">{humanizeAuditSnapshot(log.before_summary)}</span>
                              <ChevronRight className="h-4 w-4 text-muted-foreground" />
                              <span className="rounded bg-primary/10 px-2 py-1 text-primary">{humanizeAuditSnapshot(log.after_summary)}</span>
                            </div>
                            <div className="mt-2 break-words text-xs leading-5 text-muted-foreground" title={log.reason}>原因：{log.reason}</div>
                            <div className="mt-1 break-all text-[11px] text-muted-foreground">操作人：{log.operator_name || `用户 #${log.operator_user_id}`} · 版本 {log.version_before} → {log.version_after} · 请求 {log.request_id}</div>
                          </article>
                        ))}
                      </div>
                    )}
                  </Section>
                )}
              </div>
            </>
          ) : null}
        </main>
      </div>

      <Dialog open={Boolean(changeDraft)} onOpenChange={(open) => { if (!open && !saving) setChangeDraft(null); }}>
        <DialogContent className="max-w-xl" data-testid="governance-change-dialog">
          <DialogHeader>
            <DialogTitle>{changeDraft?.title}</DialogTitle>
            <DialogDescription>请核对修改前后、影响对象和不可逆影响。保存采用版本锁，旧页面提交会被拒绝。</DialogDescription>
          </DialogHeader>
          {changeDraft && (
            <div className="space-y-4">
              {(changeDraft.kind === 'commercial_binding' || changeDraft.kind === 'channel_relationship') && (
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                    {changeDraft.kind === 'commercial_binding' ? '新的商业服务承接方' : '新的直属上游'}
                  </label>
                  {/* 可搜索:接口来源 · 生产 45 个 agent_level>=1 服务商 · 资金归属操作误选代价高 */}
                  <SearchableSelect
                    value={providerChoice}
                    onChange={updateProviderPreview}
                    placeholder="选择平台或服务商"
                    searchPlaceholder="搜索服务商名 / 用户号"
                    emptyText="没有匹配的服务商"
                    options={[
                      {
                        value: 'platform',
                        label: changeDraft.kind === 'commercial_binding' ? '平台直营（解除商业绑定）' : '平台根渠道（无直属上游）',
                      },
                      ...providerCandidates
                        .filter((provider) => provider.is_active && provider.user_id !== detail?.overview.user_id)
                        .map((provider) => ({
                          value: String(provider.user_id),
                          label: `${provider.display_name} · 用户 #${provider.user_id}`,
                          keywords: String(provider.user_id),
                        })),
                    ]}
                  />

                  {changeDraft.kind === 'channel_relationship' && providerChoice !== 'platform' && (
                    <div className="mt-3">
                      <label htmlFor="channel-multiplier" className="mb-1.5 block text-xs font-medium text-muted-foreground">进货系数</label>
                      <Input
                        id="channel-multiplier"
                        type="number"
                        min="1"
                        max="10"
                        step="0.0001"
                        value={channelMultiplier}
                        onChange={(event) => updateChannelMultiplier(event.target.value)}
                      />
                      <div className="mt-1 text-[11px] text-muted-foreground">例如 1.2 表示该服务商今后的新进货按直属上游基础的 1.2 倍计算。</div>
                    </div>
                  )}
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-[1fr_auto_1fr] sm:items-center">
                <div className="rounded-lg border border-border bg-muted/30 px-3 py-3">
                  <div className="text-xs text-muted-foreground">修改前</div>
                  <div className="mt-1 break-words text-sm font-medium">{changeDraft.beforeLabel}</div>
                </div>
                <ChevronRight className="mx-auto h-5 w-5 rotate-90 text-muted-foreground sm:rotate-0" />
                <div className="rounded-lg border border-primary/25 bg-primary/7 px-3 py-3">
                  <div className="text-xs text-muted-foreground">修改后</div>
                  <div className="mt-1 break-words text-sm font-medium text-primary">{changeDraft.afterLabel}</div>
                </div>
              </div>
              <div className="rounded-lg border border-amber-500/25 bg-amber-500/8 px-3 py-3 text-xs leading-5 text-muted-foreground">
                <div className="font-medium text-foreground">影响对象：{selectedListItem?.display_name || detail?.overview.display_name}</div>
                <div className="mt-1">{changeDraft.impact}</div>
                <div className="mt-1 font-medium text-amber-800 dark:text-amber-200">保存后会留下不可删除的审计证据；历史订单和账务数据不变。</div>
              </div>
              <div>
                <label htmlFor="governance-reason" className="mb-1.5 block text-xs font-medium text-muted-foreground">操作原因（必填）</label>
                <textarea
                  id="governance-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  maxLength={500}
                  rows={3}
                  className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none ring-offset-background placeholder:text-muted-foreground focus:ring-2 focus:ring-ring"
                  placeholder="说明业务背景、核验依据与预期结果"
                />
                <div className="mt-1 text-right text-[11px] text-muted-foreground">{reason.length}/500</div>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setChangeDraft(null)} disabled={saving}>取消</Button>
            <Button
              onClick={() => void submitChange()}
              disabled={
                saving
                || reason.trim().length < 2
                || changeDraft?.beforeLabel === changeDraft?.afterLabel
                || (changeDraft?.kind === 'channel_relationship'
                  && changeDraft.upstreamUserId != null
                  && ((changeDraft.costMultiplierBps ?? 0) < 10000 || (changeDraft.costMultiplierBps ?? 0) > 100000))
              }
            >
              {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}确认并保存证据
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* [P0-C §9] 补录凭证对话框 —— 必填理由,与其他治理动作同规矩 */}
      <Dialog open={backfillOpen} onOpenChange={(open) => { if (!backfillSaving) setBackfillOpen(open); }}>
        <DialogContent className="max-w-md" data-testid="backfill-dialog">
          <DialogHeader>
            <DialogTitle>补录归属凭证</DialogTitle>
            <DialogDescription>
              为 {detail?.overview.display_name || '当前用户'} 这条人工设置的归属补一条说明凭证。
              只新增记录，<strong>不修改归属本身</strong>，也不改变绑定时间。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <label htmlFor="backfill-reason" className="block text-xs font-medium text-muted-foreground">
              当时为什么这么定（必填）
            </label>
            <Input
              id="backfill-reason"
              data-testid="backfill-reason"
              value={backfillReason}
              onChange={(event) => setBackfillReason(event.target.value)}
              placeholder="例如：2026-07 双方线下确认由该服务商承接"
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setBackfillOpen(false)} disabled={backfillSaving}>取消</Button>
            <Button
              data-testid="backfill-submit"
              onClick={submitBackfill}
              disabled={backfillSaving || backfillReason.trim().length < 2}
            >
              确认补录
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={passwordDialogOpen} onOpenChange={(open) => { if (!open) closePasswordDialog(); }}>
        <DialogContent className="max-w-md" data-testid="password-reset-dialog">
          <DialogHeader>
            <DialogTitle>重置登录密码</DialogTitle>
            <DialogDescription>
              为 {detail?.overview.display_name || '当前用户'} 设置临时密码。保存后旧登录凭证失效，用户下次登录必须修改密码。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <label htmlFor="new-password" className="mb-1.5 block text-xs font-medium text-muted-foreground">临时密码</label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                aria-describedby="new-password-policy"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
              <div
                id="new-password-policy"
                className={`mt-1 text-xs ${newPassword && passwordPolicyError(newPassword) ? 'text-destructive' : 'text-muted-foreground'}`}
              >
                {passwordPolicyError(newPassword) || '密码格式符合要求'}
              </div>
            </div>
            <div>
              <label htmlFor="confirm-password" className="mb-1.5 block text-xs font-medium text-muted-foreground">再次输入</label>
              <Input id="confirm-password" type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} />
              {confirmPassword && newPassword !== confirmPassword && <div className="mt-1 text-xs text-destructive">两次输入不一致</div>}
            </div>
            <div>
              <label htmlFor="password-reason" className="mb-1.5 block text-xs font-medium text-muted-foreground">操作原因（必填）</label>
              <textarea
                id="password-reason"
                value={passwordReason}
                onChange={(event) => setPasswordReason(event.target.value)}
                maxLength={500}
                rows={3}
                className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none ring-offset-background placeholder:text-muted-foreground focus:ring-2 focus:ring-ring"
                placeholder="例如：用户完成身份核验后申请重置"
              />
            </div>
            <div className="rounded-lg border border-border bg-muted/30 px-3 py-2 text-xs leading-5 text-muted-foreground">
              系统只记录操作人、时间、IP、原因和状态变化，不记录密码明文或密码哈希。
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closePasswordDialog} disabled={passwordSaving}>取消</Button>
            <Button
              onClick={() => void submitPasswordReset()}
              disabled={passwordSaving || Boolean(passwordPolicyError(newPassword)) || newPassword !== confirmPassword || passwordReason.trim().length < 2}
            >
              {passwordSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}确认重置
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={walletDialogOpen} onOpenChange={(open) => { if (!open) closeWalletDialog(); }}>
        <DialogContent className="max-w-md" data-testid="wallet-adjustment-dialog">
          <DialogHeader>
            <DialogTitle>最高管理员算力校正</DialogTitle>
            <DialogDescription>
              校正 {detail?.overview.display_name || '当前用户'} 的钱包数据。本操作不形成充值收入，保存后不可删除审计证据。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">算力类型</label>
              <Select value={walletPointType} onValueChange={(value) => setWalletPointType(value as 'paid' | 'bonus')}>
                <SelectTrigger aria-label="算力类型"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="paid">付费算力</SelectItem><SelectItem value="bonus">赠送算力</SelectItem></SelectContent>
              </Select>
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">校正方式</label>
              <Select value={walletOperation} onValueChange={(value) => setWalletOperation(value as 'add' | 'deduct' | 'set')}>
                <SelectTrigger aria-label="校正方式"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="add">增加</SelectItem><SelectItem value="deduct">扣减</SelectItem><SelectItem value="set">设为指定余额</SelectItem></SelectContent>
              </Select>
            </div>
            <div>
              <label htmlFor="wallet-adjustment-amount" className="mb-1.5 block text-xs font-medium text-muted-foreground">算力数量</label>
              <Input id="wallet-adjustment-amount" inputMode="numeric" value={walletAmount} onChange={(event) => setWalletAmount(event.target.value.replace(/\D/g, ''))} placeholder="请输入整数" />
            </div>
            <div>
              <label htmlFor="wallet-adjustment-reason" className="mb-1.5 block text-xs font-medium text-muted-foreground">核验原因（必填）</label>
              <textarea id="wallet-adjustment-reason" value={walletReason} onChange={(event) => setWalletReason(event.target.value)} maxLength={500} rows={3} className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none ring-offset-background placeholder:text-muted-foreground focus:ring-2 focus:ring-ring" placeholder="例如：核对支付流水后修正历史到账差异" />
            </div>
            <div className="rounded-lg border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs leading-5 text-muted-foreground">
              这不是充值、赠送活动或收入确认。请仅在证据充分时操作，并再次核对用户与算力类型。
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => closeWalletDialog()} disabled={walletSaving}>取消</Button>
            <Button onClick={() => void submitWalletAdjustment()} disabled={walletSaving || !walletAmount || walletReason.trim().length < 2 || ((walletOperation === 'add' || walletOperation === 'deduct') && Number(walletAmount) <= 0)}>
              {walletSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}确认校正
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {detail && (
        <ProviderDowngradeWizard
          open={downgradeWizardOpen}
          onOpenChange={setDowngradeWizardOpen}
          providerUserId={detail.overview.user_id}
          providerName={detail.overview.display_name}
          identityVersion={detail.overview.versions.business_identity}
          onCompleted={async () => { await Promise.all([loadDetail(detail.overview.user_id), loadUsers()]); }}
        />
      )}
    </div>
  );
}
