import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  AlertTriangle, CalendarClock, Check, Copy, Mail, MoreVertical, Phone,
  RefreshCw, ShieldCheck, SlidersHorizontal, UserPlus, X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useOrganization } from '@/context/OrganizationContext';
import {
  organizationRequest, requestId,
  getInviteLink, getPayerPolicy, putPayerPolicy,
  type AutomaticPlan, type OrganizationApproval, type OrganizationApprovalPolicy, type OrganizationInvite,
  type OrganizationLimit, type OrganizationMember, type OrganizationPayerPolicyView, type OrganizationRole,
} from '@/lib/organizationApi';
import {
  actorKindLabel, approvalActionLabel, approvalStatusLabel, artifactScopeLabel,
  auditActionLabel, entityTypeLabel, featureCodeLabel, inviteTargetKindLabel,
  limitKindLabel, limitStatusLabel, memberStatusLabel, planStatusLabel,
} from '@/lib/organizationLabels';
import { getInviteDeliveryStates, type InviteDeliveryStateItem } from '@/lib/organizationInviteApi';
import { InviteDeliveryStatus } from '@/components/organization/InviteDeliveryStatus';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { cn } from '@/lib/utils';

type TabKey = 'none' | 'billing' | 'approvals' | 'plans' | 'audit';

interface ClientOption {
  id: number;
  name: string;
}

interface AuditEvent {
  id: number;
  action: string;
  actor_kind: string;
  actor_user_id?: number | null;
  entity_type: string;
  entity_id?: string | null;
  reason?: string | null;
  created_at: string;
}

const CAPABILITY_LABELS: Record<string, string> = {
  'clients.read_assigned': '查看分配客户',
  'clients.profile_edit': '编辑客户资料',
  'diagnosis.read_own': '查看自己的诊断',
  'diagnosis.run': '发起诊断',
  'diagnosis.export': '导出诊断',
  'quote.read_own': '查看自己的报价',
  'quote.create': '创建报价',
  'quote.submit_for_approval': '提交报价审批',
  'quote.send_external': '对外发送报价',
  'writing.read_own': '查看自己的文章',
  'writing.generate': '生成文章',
  'writing.review': '审核文章',
  'writing.share_internal': '团队内分享文章',
  'publish.plan': '制作发布计划',
  'publish.submit_for_approval': '提交发布审批',
  'publish.execute': '执行真实发布',
  'monitoring.read_assigned': '查看客户监测',
  'monitoring.run': '执行监测',
  'monitoring.configure': '配置监测',
  'monitoring.retry': '重试失败监测',
  'reports.read_own': '查看自己的报告',
  'reports.generate': '生成客户报告',
  'reports.export': '导出报告',
  'reports.share_external': '对外分享报告',
  'materials.read_assigned': '查看客户材料',
  'materials.write': '编辑客户材料',
  'approvals.review': '审批团队申请',
  'team.output_read': '查看团队做的内容',
  'team.output_handoff': '交接团队的客户和内容',
};

/**
 * [C1 2026-08-17] 29 个权限勾选框原来平铺两列、无分组、无「角色默认自带」标识,
 * 老板根本判断不出哪些该动。按**业务模块**分组折叠,默认收起,只显示改动数。
 * 顺序 = 代理的真实工作顺序(客户 → 诊断 → 报价 → 写作 → 发布 → 监测 → 报告 → 材料 → 团队)。
 * 完备性由 `verify-organization-enum-labels.mjs` 上锁:CAPABILITY_LABELS 的每个 key
 * 必须且只能出现在一个分组里,漏一个红。
 */
const CAPABILITY_GROUPS: Array<{ title: string; codes: string[] }> = [
  { title: '客户', codes: ['clients.read_assigned', 'clients.profile_edit'] },
  { title: '诊断', codes: ['diagnosis.read_own', 'diagnosis.run', 'diagnosis.export'] },
  { title: '报价', codes: ['quote.read_own', 'quote.create', 'quote.submit_for_approval', 'quote.send_external'] },
  { title: '文章', codes: ['writing.read_own', 'writing.generate', 'writing.review', 'writing.share_internal'] },
  { title: '发布', codes: ['publish.plan', 'publish.submit_for_approval', 'publish.execute'] },
  { title: '监测', codes: ['monitoring.read_assigned', 'monitoring.run', 'monitoring.configure', 'monitoring.retry'] },
  { title: '报告', codes: ['reports.read_own', 'reports.generate', 'reports.export', 'reports.share_external'] },
  { title: '客户材料', codes: ['materials.read_assigned', 'materials.write'] },
  { title: '团队协作', codes: ['approvals.review', 'team.output_read', 'team.output_handoff'] },
];

/** [C7] 高风险权限一句话说清后果 —— 老板要判断的是「会发生什么」,不是权限名。 */
const CAPABILITY_CONSEQUENCES: Record<string, string> = {
  'quote.send_external': '员工可以直接把报价发给客户',
  'publish.execute': '员工可以直接对外真实发布',
  'reports.share_external': '员工可以把客户报告链接发给外部',
  'writing.generate': '员工可以消耗算力生成文章',
  'diagnosis.run': '员工可以消耗算力发起诊断',
  'monitoring.run': '员工可以消耗算力跑监测',
  'monitoring.configure': '员工可以改监测词和监测设置',
  'team.output_handoff': '员工可以把客户和内容交接给别人',
  'approvals.review': '员工可以审批别人的申请',
};

const DEFAULT_ROLE_CAPABILITIES = [
  'clients.read_assigned', 'clients.profile_edit', 'materials.read_assigned',
  'diagnosis.read_own', 'diagnosis.run', 'diagnosis.export',
  'quote.read_own', 'quote.create', 'quote.submit_for_approval',
  'monitoring.read_assigned', 'monitoring.run', 'monitoring.retry', 'reports.read_own',
];

const DELIVERY_ROLE_CAPABILITIES = [
  'clients.read_assigned', 'materials.read_assigned', 'materials.write',
  'writing.read_own', 'writing.generate', 'writing.review', 'writing.share_internal',
  'publish.plan', 'publish.submit_for_approval', 'publish.execute',
  'monitoring.read_assigned', 'monitoring.run', 'monitoring.retry',
  'reports.read_own', 'reports.generate', 'reports.export',
];

const READONLY_ROLE_CAPABILITIES = [
  'clients.read_assigned', 'materials.read_assigned', 'diagnosis.read_own',
  'quote.read_own', 'writing.read_own', 'monitoring.read_assigned',
  'reports.read_own', 'team.output_read',
];

function formatDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : '操作失败，请稍后重试';
}

async function mutate<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  return await organizationRequest<T>(path, { method, body: JSON.stringify(body) });
}

function EmptyState({ text }: { text: string }) {
  return <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">{text}</div>;
}

export default function OrganizationCenter() {
  const { overview, loading: overviewLoading, error: overviewError, isOwner, isMember, refresh: refreshOverview } = useOrganization();
  const [tab, setTab] = useState<TabKey>('none');
  const [members, setMembers] = useState<OrganizationMember[]>([]);
  const [roles, setRoles] = useState<OrganizationRole[]>([]);
  const [invites, setInvites] = useState<OrganizationInvite[]>([]);
  const [limits, setLimits] = useState<OrganizationLimit[]>([]);
  const [approvals, setApprovals] = useState<OrganizationApproval[]>([]);
  const [approvalPolicies, setApprovalPolicies] = useState<OrganizationApprovalPolicy[]>([]);
  const [plans, setPlans] = useState<AutomaticPlan[]>([]);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [clients, setClients] = useState<ClientOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  const refreshData = useCallback(async () => {
    if (!overview) return;
    setPageError(null);
    try {
      const requests: Promise<unknown>[] = [
        organizationRequest<OrganizationMember[]>('/members').then(setMembers),
        organizationRequest<OrganizationRole[]>('/roles').then(setRoles),
        organizationRequest<OrganizationLimit[]>('/limits').then(setLimits),
        organizationRequest<OrganizationApproval[]>('/approvals').then(setApprovals),
      ];
      if (isOwner) {
        requests.push(
          organizationRequest<OrganizationInvite[]>('/invites').then(setInvites),
          organizationRequest<AutomaticPlan[]>('/automatic-plans').then(setPlans),
          organizationRequest<AuditEvent[]>('/audit?limit=100').then(setAudit),
          organizationRequest<OrganizationApprovalPolicy[]>('/approval-policies').then(setApprovalPolicies),
        );
      }
      await Promise.all(requests);
    } catch (caught) {
      setPageError(errorMessage(caught));
    }
  }, [overview, isOwner]);

  useEffect(() => { void refreshData(); }, [refreshData]);

  useEffect(() => {
    if (!overview) {
      setClients([]);
      return;
    }
    if (!isOwner) {
      // [C2 2026-08-17] 员工没有 /api/my-clients 权限,客户名字由 overview 直接带下来
      //   (services/organization_service.get_overview → assigned_brands)。
      //   在此之前员工端只能看到内部客户编号拼接,如「3、17、42」。
      setClients((overview.assigned_brands || []).map(item => ({ id: Number(item.id), name: String(item.name) })));
      return;
    }
    const controller = new AbortController();
    authFetch('/api/my-clients?page_size=500', { signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject(new Error('客户列表加载失败')))
      .then(body => {
        const values = Array.isArray(body?.clients) ? body.clients : [];
        setClients(values.map((item: Record<string, unknown>) => ({
          id: Number(item.id),
          name: String(item.name || item.brand_name || `客户 #${item.id}`),
        })).filter((item: ClientOption) => item.id > 0));
      })
      .catch(caught => {
        if (!controller.signal.aborted) setPageError(errorMessage(caught));
      });
    return () => controller.abort();
  }, [overview, isOwner]);

  const run = useCallback(async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await action();
      lazyToast.success(success);
      await Promise.all([refreshOverview(), refreshData()]);
    } catch (caught) {
      const message = errorMessage(caught);
      lazyToast.error(message);
      setPageError(message);
    } finally {
      setBusy(false);
    }
  }, [refreshData, refreshOverview]);

  // [B14 2026-08-17] 内部权限版本号(组织授权:成员:能力:分配)从 hover title 移到这里。
  // 它是排查用的一致性令牌,对老板没有可操作含义 —— 客服要对版本时看 console 即可。
  useEffect(() => {
    if (overview?.identity?.authority_version) {
      console.debug('[organization] authority_version =', overview.identity.authority_version);
    }
  }, [overview?.identity?.authority_version]);

  if (overviewLoading && !overview) {
    return <div className="mx-auto max-w-7xl p-4 sm:p-6"><div className="h-48 animate-pulse rounded-2xl bg-muted" /></div>;
  }
  if (!overview) {
    return <CreateOrJoinOrganization error={overviewError} onCreated={refreshOverview} />;
  }

  // [C6 2026-08-17] 原来「算力上限」和「员工费用」两个 tab **渲染同一个 LimitsPanel**
  //   (291/295 两行),同一份内容两个入口,用户分不清区别。合并成一个。
  const tabs: Array<{ key: Exclude<TabKey, 'none'>; label: string; ownerOnly?: boolean }> = [
    { key: 'billing', label: '员工费用与算力上限' },
    { key: 'approvals', label: '审批队列' },
    { key: 'plans', label: '自动计划', ownerOnly: true },
    { key: 'audit', label: '操作记录', ownerOnly: true },
  ].filter(item => !item.ownerOnly || isOwner) as Array<{ key: Exclude<TabKey, 'none'>; label: string; ownerOnly?: boolean }>;

  return (
    <main className="mx-auto w-full max-w-[1600px] space-y-5 p-3 pb-24 sm:p-6 md:pr-20 lg:p-8" data-testid="organization-center">
      <section className="overflow-hidden rounded-2xl border bg-card">
        <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between sm:p-7">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{isOwner ? '我是老板' : '我是员工'}</Badge>
              <Badge variant={overview.status === 'active' ? 'default' : 'destructive'}>{overview.status === 'active' ? '团队正常' : '团队已停用'}</Badge>
            </div>
            <h1 className="break-words text-2xl font-semibold tracking-tight sm:text-3xl">{overview.name}</h1>
            {/* [A7] 原文一句话塞了三个否定(「不是余额、赠送算力或可提现资产」),
                谁也读不完。拆成两句正面陈述:钱从哪出、员工能看到什么。 */}
            <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
              员工花的算力都从你的钱包扣；给员工设上限只是限制他最多能花多少，不是给他充值。
              员工只能看到你分配给他的客户，和他自己做的内容。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {isOwner && <Button onClick={() => document.getElementById('owner-invite-contact')?.focus()}>
              <UserPlus className="mr-1 h-4 w-4" />邀请员工
            </Button>}
            <Button variant="outline" onClick={() => void Promise.all([refreshOverview(), refreshData()])} disabled={busy} aria-label="刷新团队数据">
              <RefreshCw className={cn('mr-1 h-4 w-4', busy && 'animate-spin')} />刷新
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-2 border-t sm:grid-cols-4">
          {[
            ['员工席位', `${overview.entitlement.occupied_seats}/${overview.entitlement.entitled_seats}`, ''],
            ['可邀请', String(overview.entitlement.available_seats), ''],
            // [§1-4 口径修正 2026-08-17] 这格的值是 `overview.assigned_brand_ids`,
            //   而后端对**老板**返回的是「你名下的全部客户」(db/organization_db.py
            //   assigned_brand_ids 的 owner 分支 = brands.owner_user_id 全量),
            //   **不是**「已分配给员工的客户」。QA 现场 0 员工却显示 4,就是这个原因
            //   (其中 2 个是测试客户,而下方分配客户用的 /api/my-clients 默认过滤测试客户,
            //   所以两个数字对不上)。
            //   这个数组同时是权限作用域的投影,**不能为了好看去改它**;
            //   按工单「口径写进 label」把标签改准。
            [
              isOwner ? '我的客户' : '我负责的客户',
              String(overview.assigned_brand_ids.length),
              isOwner
                ? '你名下的全部客户（含测试客户）。下方“分配客户”默认只列出正式客户，所以两处数字可能不同。'
                : '老板分配给你的客户数量。',
            ],
            // [2026-07-28] 原来这格是「权限版本 4:1:1:1」—— 那是内部一致性令牌
            //   (组织授权:成员:能力:分配 四个版本号),对老板没有任何可操作含义。
            // [B14 2026-08-17] hover 里那句「内部权限版本 4:1:1:1(排查用)」还是把调试
            //   信息端到用户面前了。彻底移出 DOM:版本号只进 console.debug,
            //   客服排查照样拿得到(且不占用户认知)。
            ['权限生效状态', overview.status === 'active' ? '已是最新' : '团队已停用', ''],
          ].map(([label, value, hint]) => (
            <div key={label} className="min-w-0 border-b p-4 even:border-l sm:border-b-0 sm:border-l sm:first:border-l-0">
              <div className="text-xs text-muted-foreground">{label}</div>
              <div className="mt-1 truncate text-lg font-semibold" title={hint || value}>{value}</div>
            </div>
          ))}
        </div>
      </section>

      {(pageError || overviewError) && (
        <div role="alert" className="flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="break-words">{pageError || overviewError}</span>
        </div>
      )}

      {isOwner ? (
        <OwnerOperationsPanel
          members={members}
          roles={roles}
          invites={invites}
          limits={limits}
          clients={clients}
          busy={busy}
          run={run}
        />
      ) : (
        <TeamPanel overviewOwner={false} members={members} clients={clients} busy={busy} run={run} />
      )}

      <section className="rounded-2xl border bg-card p-3 sm:p-4" aria-label="团队高级治理">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold">高级治理</h2>
            <p className="mt-1 text-sm text-muted-foreground">算力上限、审批、自动计划和操作记录都在这里，需要时点开；日常邀请和成员管理在上方就能做完。</p>
          </div>
          <nav className="flex max-w-full gap-1 overflow-x-auto rounded-xl bg-muted/40 p-1" aria-label="高级治理栏目">
            {tabs.map(item => (
              <button key={item.key} type="button" onClick={() => setTab(current => current === item.key ? 'none' : item.key)}
                className={cn('min-h-10 shrink-0 rounded-lg px-3 text-sm transition-colors', tab === item.key ? 'bg-background font-medium shadow-sm' : 'text-muted-foreground hover:text-foreground')}>
                {item.label}
              </button>
            ))}
          </nav>
        </div>
      </section>

      {tab === 'billing' && (
        <div className="space-y-5">
          <PayerPolicyPanel isOwner={isOwner} busy={busy} run={run} />
          <LimitsPanel isOwner={isOwner} members={members} limits={limits} busy={busy} run={run} />
        </div>
      )}
      {tab === 'approvals' && <ApprovalPanel approvals={approvals} policies={approvalPolicies} members={members} isOwner={isOwner} canReview={overview.identity.capabilities.includes('approvals.review')} busy={busy} run={run} />}
      {tab === 'plans' && isOwner && <PlansPanel plans={plans} clients={clients} busy={busy} run={run} />}
      {tab === 'audit' && isOwner && <AuditPanel events={audit} />}
    </main>
  );
}

function CreateOrJoinOrganization({ error, onCreated }: { error: string | null; onCreated: () => Promise<void> }) {
  const [name, setName] = useState('');
  const [inviteToken, setInviteToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const createRequestId = useRef(requestId('create-organization'));
  const acceptRequestId = useRef(requestId('accept-invite'));
  const submit = async (mode: 'create' | 'join') => {
    setBusy(true);
    setActionError(null);
    try {
      if (mode === 'create') {
        await mutate('', { name, request_id: createRequestId.current });
        lazyToast.success('团队已创建');
      } else {
        await mutate('/invites/accept', { token: inviteToken, request_id: acceptRequestId.current });
        lazyToast.success('已加入团队');
      }
      await onCreated();
    } catch (caught) {
      const message = errorMessage(caught);
      setActionError(message);
      lazyToast.error(message);
    } finally { setBusy(false); }
  };
  return (
    <main className="mx-auto grid min-h-[70vh] w-full max-w-5xl items-center gap-5 p-4 md:grid-cols-2 md:p-8">
      <Card>
        <CardHeader><CardTitle>创建团队</CardTitle><CardDescription>所有正常账号都可以建自己的团队。费用都由你支付、客户都归你；你自己不占员工名额。</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div><Label htmlFor="organization-name">团队名称</Label><Input id="organization-name" value={name} onChange={event => setName(event.target.value)} maxLength={120} placeholder="例如：华东品牌增长服务中心" /></div>
          <Button type="button" className="w-full" disabled={busy || !name.trim()} onClick={() => void submit('create')}>{busy ? '正在处理…' : '创建团队'}</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>接受团队邀请</CardTitle><CardDescription>请优先打开老板发给你的邀请链接；还没有账号时，可以在链接里验证手机号并直接创建员工账号。</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div><Label htmlFor="invite-token">邀请口令</Label><Textarea id="invite-token" value={inviteToken} onChange={event => setInviteToken(event.target.value)} placeholder="粘贴老板发给你的邀请口令" className="min-h-24 break-all" /></div>
          <Button type="button" variant="secondary" className="w-full" disabled={busy || inviteToken.length < 20} onClick={() => void submit('join')}>{busy ? '正在处理…' : '接受邀请'}</Button>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
      {actionError && <div role="alert" data-testid="organization-action-error" className="rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive md:col-span-2">{actionError}</div>}
    </main>
  );
}

function TeamPanel({ overviewOwner, members, clients, busy, run }: { overviewOwner: boolean; members: OrganizationMember[]; clients: ClientOption[]; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const [confirmDialogTeam, askConfirmTeam] = useConfirmDialog();
  const current = members.find(member => !overviewOwner && !member.is_owner);
  if (!overviewOwner) {
    // [C2 2026-08-17] 「分配客户」原来显示的是内部客户编号拼接(「3、17、42」),
    //   员工看不出那是谁。有客户名就用名字,取不到名字才回落成「客户 #3」。
    const assignedText = (current?.assigned_brand_ids || [])
      .map(id => clients.find(client => client.id === id)?.name || `客户 #${id}`)
      .join('、') || '暂未分配';
    return <div className="grid gap-4 lg:grid-cols-2">
      <Card><CardHeader><CardTitle>我的员工席位</CardTitle><CardDescription>你的花费都从老板的钱包扣，老板给你设了使用上限。</CardDescription></CardHeader><CardContent className="space-y-3 text-sm">
        {/* [B4] 原来是 `value={current?.status}` 直出英文 active —— 讽刺的是同文件里
            早就有映射函数,只是这里没用上。 */}
        <InfoRow label="角色" value={current?.role_name || '加载中'} /><InfoRow label="状态" value={current ? statusLabel(current.status) : '—'} /><InfoRow label="分配客户" value={assignedText} />
      </CardContent></Card>
      <Card><CardHeader><CardTitle>退出团队</CardTitle><CardDescription>退出后立刻不能再新建、外发或花算力；你做过的内容和操作记录会留给老板。</CardDescription></CardHeader><CardContent><Button variant="destructive" disabled={busy} onClick={async () => { if (await askConfirmTeam({ title: '确认退出当前团队？', confirmLabel: '退出', danger: true })) void run(() => mutate('/leave', { reason: '员工主动退出' }), '已退出团队'); }}>退出团队</Button></CardContent></Card>
      {confirmDialogTeam}
    </div>;
  }
  return <>{members.length ? <div className="grid gap-3 xl:grid-cols-2">{members.map(member => (
    <Card key={member.id}><CardContent className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
      {/* [B12] 这里原来是 `{member.status}` 原值直出。当前 owner 分支不可达
          (调用点恒 overviewOwner={false}),但留着就是一颗复活即英文的雷。 */}
      <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="break-words font-medium">{member.display_name || `成员 ${member.user_id}`}</span>{member.is_owner && <Badge>老板</Badge>}<Badge variant="secondary">{statusLabel(member.status)}</Badge></div><p className="mt-1 text-sm text-muted-foreground">{member.role_name} · 客户 {member.assigned_brand_ids.length} 个</p></div>
      {!member.is_owner && <div className="flex flex-wrap gap-2">{member.status === 'suspended' ? <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => mutate(`/members/${member.id}/status`, { action: 'resume', reason: '老板恢复员工席位' }), '员工已恢复')}>恢复</Button> : <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => mutate(`/members/${member.id}/status`, { action: 'suspend', reason: '老板暂停员工席位' }), '员工已暂停')}>暂停</Button>}<Button size="sm" variant="destructive" disabled={busy} onClick={async () => { if (await askConfirmTeam({ title: '移除后会立即撤销客户权限，确认继续？', confirmLabel: '移除', danger: true })) void run(() => mutate(`/members/${member.id}/status`, { action: 'remove', reason: '老板移除员工' }), '员工已移除'); }}>移除</Button></div>}
    </CardContent></Card>
  ))}</div> : <EmptyState text="还没有员工，去“邀请员工”添加第一位成员。" />}{confirmDialogTeam}</>;
}

const ROLE_SUMMARIES: Record<string, string> = {
  sales: '我的客户 · 诊断 · 报价 · 监测',
  delivery: '我的客户 · 写作 · 发布 · 监测',
  readonly: '只看已分配客户和授权结果',
};

const FEATURE_OPTIONS = [
  ['geo_diagnosis', '品牌诊断', 'diagnosis.run'],
  ['quote_generate', '报价方案', 'quote.create'],
  ['article_gen', 'AI 写文章', 'writing.generate'],
  ['topic_gen', '文章标题', 'writing.generate'],
  ['monitor_single', '效果监测', 'monitoring.run'],
  ['media_publish', '发布投放', 'publish.execute'],
] as const;

type RunAction = (action: () => Promise<unknown>, success: string) => Promise<void>;

/**
 * [C1 2026-08-17] 权限勾选:按模块分组 + 标出「角色自带」vs「额外增加」。
 *
 * 原来是 29 个勾选框平铺两列,没有分组、没有「这项本来就有」的提示,只有一个黄色
 * 「高风险」标。老板打开这一栏的第一反应是「我该动哪个」——答案是通常一个都不用动,
 * 但界面完全没有传达这一点。现在:默认全部收起,每组标题上直接写「本组已有 N 项 ·
 * 你额外加了 M 项」,老板扫一眼就知道哪一组被自己动过。
 */
function CapabilityPicker({ testId, isChecked, isRoleDefault, onToggle }: {
  testId: string;
  isChecked: (code: string) => boolean;
  isRoleDefault: (code: string) => boolean;
  onToggle: (code: string, checked: boolean) => void;
}) {
  return (
    <div data-testid={testId}>
      <Label>模块与动作</Label>
      <p className="mt-1 text-xs text-muted-foreground">
        标「角色自带」的是这个角色本来就有的权限，通常不用动；勾上没有「角色自带」标记的项，就是额外授权。
      </p>
      <div className="mt-2 space-y-2">
        {CAPABILITY_GROUPS.map(group => {
          const own = group.codes.filter(code => isRoleDefault(code)).length;
          const extra = group.codes.filter(code => !isRoleDefault(code) && isChecked(code)).length;
          return (
            <details key={group.title} className="rounded-lg border" data-testid={`capability-group-${group.title}`} open={extra > 0}>
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm font-medium">
                <span>{group.title}</span>
                <span className="text-xs font-normal text-muted-foreground">
                  角色自带 {own} 项{extra > 0 && <span className="ml-2 text-amber-600">额外增加 {extra} 项</span>}
                </span>
              </summary>
              <div className="grid gap-1 border-t p-2 sm:grid-cols-2">
                {group.codes.map(code => {
                  const label = CAPABILITY_LABELS[code];
                  const isExtra = !isRoleDefault(code) && isChecked(code);
                  return (
                    <label key={code} className={cn('flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm', isExtra ? 'bg-amber-500/10' : 'hover:bg-muted')}>
                      <input type="checkbox" checked={isChecked(code)} onChange={event => onToggle(code, event.target.checked)} />
                      <span>
                        {label}
                        {isRoleDefault(code) && <span className="ml-1 text-xs text-muted-foreground">角色自带</span>}
                        {isExtra && <span className="ml-1 text-xs text-amber-600">额外增加</span>}
                      </span>
                    </label>
                  );
                })}
              </div>
            </details>
          );
        })}
      </div>
    </div>
  );
}

/**
 * [A1/A2/C7 2026-08-17] 「跨域扩大」确认块。
 *
 * 原文案:「跨域扩大：对外发送报价、生成文章、真实发布」+「我确认弱化默认职责隔离，
 * 并接受审计。」—— 三个内部词(跨域/扩大/弱化默认职责隔离)堆在一句里,而且所有权限名
 * 用「、」串成一堵字墙,权限一多完全读不动。改成逐行列表 + 每项一句后果。
 */
function HighRiskConfirm({ codes, confirmed, reason, onConfirm, onReason }: {
  codes: string[];
  confirmed: boolean;
  reason: string;
  onConfirm: (value: boolean) => void;
  onReason: (value: string) => void;
}) {
  return (
    <div role="alert" data-testid="high-risk-confirm" className="space-y-3 rounded-xl border border-amber-500/50 bg-amber-500/10 p-4">
      <p className="text-sm font-semibold">新增了 {codes.length} 项超出角色的权限：</p>
      <ul className="space-y-1.5 text-sm">
        {codes.map(code => (
          <li key={code} className="flex flex-col">
            <span className="font-medium">{CAPABILITY_LABELS[code] || code}</span>
            {CAPABILITY_CONSEQUENCES[code] && (
              <span className="text-xs text-muted-foreground">{CAPABILITY_CONSEQUENCES[code]}</span>
            )}
          </li>
        ))}
      </ul>
      <label className="flex gap-2 text-sm">
        <input type="checkbox" checked={confirmed} onChange={event => onConfirm(event.target.checked)} />
        <span>我确认要额外给他这些权限，此操作会被记录。</span>
      </label>
      <Input value={reason} onChange={event => onReason(event.target.value)} placeholder="说明为什么要给他这些额外权限" />
    </div>
  );
}

function roleBadgeVariant(code: string): 'default' | 'secondary' | 'outline' {
  return code === 'sales' ? 'default' : code === 'delivery' ? 'secondary' : 'outline';
}

// [B9 2026-08-17] 原来这里是 `{...}[status] || status` —— 那个 `|| status` 就是英文出口:
// 后端只要返回 leaving/left/removed 之类漏网状态,屏幕上立刻是英文。映射表挪到
// `@/lib/organizationLabels` 统一维护(那里覆盖 DB CHECK 的**全部**取值,并且
// 兜底是「未知状态（xxx）」而不是裸值),这里只保留一个别名。
const statusLabel = memberStatusLabel;

function memberUsage(memberId: number, limits: OrganizationLimit[]): string {
  const monthly = limits.filter(limit => limit.membership_id === memberId && limit.limit_kind === 'monthly_total')
    .sort((a, b) => Date.parse(b.period_start) - Date.parse(a.period_start))[0];
  if (!monthly) return '未设置';
  const used = Math.max(0, monthly.reserved_points + monthly.consumed_points - monthly.refunded_points);
  return used.toLocaleString() + ' / ' + monthly.limit_points.toLocaleString();
}

function OwnerOperationsPanel({ members, roles, invites, limits, clients, busy, run }: {
  members: OrganizationMember[];
  roles: OrganizationRole[];
  invites: OrganizationInvite[];
  limits: OrganizationLimit[];
  clients: ClientOption[];
  busy: boolean;
  run: RunAction;
}) {
  const contactRef = useRef<HTMLInputElement>(null);
  const employees = members.filter(member => !member.is_owner);
  const inviteRoles = useMemo(() => ['sales', 'delivery', 'readonly']
    .map(code => roles.find(role => role.code === code))
    .filter((role): role is OrganizationRole => Boolean(role)), [roles]);
  // [2026-07-28] 短信/邮箱投递通道尚未开通(投递队列建好了,但没有真实发送方),
  //   所以这两种入口置灰标"即将开放",默认且唯一可用的是用户名式邀请。
  const [targetKind, setTargetKind] = useState<'phone' | 'email' | 'username'>('username');
  // [P0-B ①②④] 团队短代码 + 登录名实时校验:让团队长在**填的时候**就看到最终登录名,
  // 并且当场知道能不能用 —— 而不是等员工点开链接设密码那一刻才炸。
  const [shortCode, setShortCode] = useState<string>('');
  const [nameCheck, setNameCheck] = useState<{ status: string; message: string; login_name: string | null } | null>(null);
  const [contact, setContact] = useState('');
  const [roleId, setRoleId] = useState('');
  const [brandIds, setBrandIds] = useState<number[]>([]);
  const [dailyLimit, setDailyLimit] = useState('');
  const [monthlyLimit, setMonthlyLimit] = useState('');
  const [artifactScope, setArtifactScope] = useState<'own' | 'assigned_team'>('own');
  const [overrides, setOverrides] = useState<Record<string, 'allow' | 'deny'>>({});
  const [featureCode, setFeatureCode] = useState('');
  const [featureDaily, setFeatureDaily] = useState('');
  const [featureMonthly, setFeatureMonthly] = useState('');
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [riskReason, setRiskReason] = useState('');
  const [lastInviteUrl, setLastInviteUrl] = useState('');
  const [mobileInviteOpen, setMobileInviteOpen] = useState(false);
  const [editingMember, setEditingMember] = useState<OrganizationMember | null>(null);
  // W2 挂载点：W1 的 InviteDeliveryStatus（components/organization/InviteDeliveryStatus）。
  // 后端 /invites/delivery-states 由 W1 提供；未交付/失败时静默降级为无状态徽章，
  // 不影响重发/撤销主流程。集成者收尾时确认 props 与数据形态。
  const [deliveryItems, setDeliveryItems] = useState<InviteDeliveryStateItem[]>([]);
  const pendingInviteKey = invites.filter(invite => invite.status === 'pending').map(invite => invite.id).join(',');
  useEffect(() => {
    const ids = pendingInviteKey ? pendingInviteKey.split(',').map(Number).filter(id => id > 0) : [];
    if (!ids.length) {
      setDeliveryItems([]);
      return;
    }
    const controller = new AbortController();
    getInviteDeliveryStates(ids)
      .then(items => { if (!controller.signal.aborted) setDeliveryItems(items); })
      .catch(() => { if (!controller.signal.aborted) setDeliveryItems([]); });
    return () => controller.abort();
  }, [pendingInviteKey]);
  const selectedRole = inviteRoles.find(role => String(role.id) === roleId) || inviteRoles[0];

  useEffect(() => { if (!roleId && inviteRoles[0]) setRoleId(String(inviteRoles[0].id)); }, [inviteRoles, roleId]);
  useEffect(() => {
    setOverrides({});
    setRiskConfirmed(false);
    setRiskReason('');
    setArtifactScope(selectedRole?.code === 'readonly' ? 'assigned_team' : 'own');
    setFeatureCode('');
  }, [selectedRole?.id]);
  useEffect(() => {
    if (members.length > 0 && !employees.length) {
      setMobileInviteOpen(true);
      window.setTimeout(() => contactRef.current?.focus(), 50);
    }
  }, [members.length, employees.length]);

  const roleAllows = (code: string) => Boolean(selectedRole?.capabilities.includes(code));
  const effectiveAllows = (code: string) => overrides[code] ? overrides[code] === 'allow' : roleAllows(code);
  const highRisk = Object.entries(overrides).filter(([code, effect]) => effect === 'allow' && !roleAllows(code)).map(([code]) => code);
  const toggleCapability = (code: string, checked: boolean) => setOverrides(current => {
    const next = { ...current };
    if (checked === roleAllows(code)) delete next[code];
    else next[code] = checked ? 'allow' : 'deny';
    return next;
  });
  useEffect(() => {
    if (targetKind !== 'username') return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await organizationRequest<{ short_code: string }>('/short-code');
        if (!cancelled) setShortCode(data.short_code || '');
      } catch { /* 取不到就退化成不显示预览,不阻断填写 */ }
    })();
    return () => { cancelled = true; };
  }, [targetKind]);

  useEffect(() => {
    if (targetKind !== 'username' || !contact.trim()) { setNameCheck(null); return; }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const data = await organizationRequest<{ status: string; message: string; login_name: string | null }>(
            '/invites/check-login-name?member_name=' + encodeURIComponent(contact.trim().toLowerCase()),
          );
          if (!cancelled) setNameCheck(data);
        } catch { if (!cancelled) setNameCheck(null); }
      })();
    }, 300);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [targetKind, contact]);

  const toggleBrand = (id: number) => setBrandIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id].sort((a, b) => a - b));
  const validContact = targetKind === 'email'
    ? /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(contact.trim())
    : targetKind === 'username'
      ? /^[a-z0-9._-]{3,32}$/.test(contact.trim().toLowerCase())
      : contact.replace(/\D/g, '').length >= 11;

  // [2026-07-28] 两处改动:
  //   ① **先展示后复制**。原来是 `await writeText()` 成功才 `setLastInviteUrl`,
  //      于是剪贴板一失败(非安全上下文/权限被拒/浏览器差异)整个邀请就被判成失败 ——
  //      而服务端其实已经建好了邀请,链接却再也拿不到。现在链接先落到状态里显示出来,
  //      复制只是锦上添花,失败也只降级成"请手动复制"。
  //   ② 复制成功要**出提示**。原来是静默复制,老板根本不知道"链接已经在剪贴板里、
  //      需要自己发给员工",这是本次反馈的核心。
  const copyInviteUrl = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      lazyToast.success('邀请链接已复制,发给员工即可');
      return true;
    } catch {
      lazyToast.error('自动复制失败,请手动选中下方链接复制');
      return false;
    }
  };
  const rememberToken = async (token: string | null) => {
    if (!token) return;
    const url = window.location.origin + '/organization/invite#token=' + encodeURIComponent(token);
    setLastInviteUrl(url);
    await copyInviteUrl(url);
  };
  const sendInvite = async () => {
    if (!selectedRole) return;
    const featureLimits = featureCode && (featureDaily || featureMonthly) ? {
      [featureCode]: {
        ...(featureDaily ? { daily: Number(featureDaily) } : {}),
        ...(featureMonthly ? { monthly: Number(featureMonthly) } : {}),
      },
    } : {};
    const result = await mutate<{ delivery_token: string | null }>('/invites', {
      target_kind: targetKind,
      // [P0-B ②] 登录名式邀请提交的是**完整登录名**(短代码-员工名);
      // 服务端仍会再拼一次做权威校验,这里只是让请求自解释。
      target: targetKind === 'username' && nameCheck?.login_name
        ? nameCheck.login_name
        : contact.trim(),
      role_id: selectedRole.id,
      brand_ids: brandIds,
      capability_overrides: overrides,
      artifact_scope: artifactScope,
      daily_limit_points: dailyLimit === '' ? null : Number(dailyLimit),
      monthly_limit_points: monthlyLimit === '' ? null : Number(monthlyLimit),
      feature_limits: featureLimits,
      high_risk_confirmed: highRisk.length ? riskConfirmed : false,
      high_risk_reason: highRisk.length ? riskReason.trim() : null,
      request_id: requestId('invite'),
    });
    await rememberToken(result.delivery_token);
    setContact('');
    setBrandIds([]);
    setMobileInviteOpen(false);
  };
  const resend = async (id: number) => {
    const result = await mutate<{ delivery_token: string | null }>('/invites/' + id + '/resend', { request_id: requestId('resend') });
    await rememberToken(result.delivery_token);
  };
  // [§1-2 修复 2026-08-17] 找回邀请链接。**只读**:同一条邀请永远派生出同一个口令,
  //   不会像「重发」那样把已经发给员工的链接作废。
  //   在此之前生成/重发只写剪贴板,老板一丢剪贴板(微信里粘错窗口、换台机器)就没了。
  const revealInviteLink = async (id: number) => {
    const result = await getInviteLink(id);
    if (!result.delivery_token) throw new Error('这条邀请的链接无法找回，请撤销后重新发一份邀请');
    const url = window.location.origin + '/organization/invite#token=' + encodeURIComponent(result.delivery_token);
    setLastInviteUrl(url);
    await copyInviteUrl(url);
  };
  // [§1-3 修复 2026-08-17] 邀请行原来显示「用户名邀请 #16」——`#16` 是内部邀请 ID。
  //   老板真正需要看到的是最终登录名(要连同链接一起发给员工)。
  const inviteTitle = (invite: OrganizationInvite) => (
    invite.login_name || `${inviteTargetKindLabel(invite.target_kind)}（待接受）`
  );

  const invitePanel = <section data-testid="owner-invite-wizard" aria-label="邀请员工" className={cn(
    'border bg-card p-5 md:static md:block md:rounded-none md:border-0 md:shadow-none',
    mobileInviteOpen ? 'fixed inset-x-0 bottom-0 z-50 block max-h-[88vh] overflow-y-auto rounded-t-2xl shadow-2xl' : 'hidden',
  )}>
    <div className="mb-5 flex items-start justify-between gap-3">
      <div><h2 className="text-xl font-semibold">邀请员工</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">三步完成，员工用自己的账号登录。</p></div>
      <Button className="md:hidden" size="icon" variant="ghost" aria-label="关闭邀请面板" onClick={() => setMobileInviteOpen(false)}><X className="h-5 w-5" /></Button>
    </div>
    <div className="space-y-6">
      <section><h3 className="mb-3 flex items-center gap-2 text-base font-semibold"><span className="grid h-7 w-7 place-items-center rounded-full bg-primary text-sm text-primary-foreground">1</span>员工账号</h3>
        {/* [2026-07-28] 用户名调到第一位(唯一可用);手机号/邮箱置灰 + 标「即将开放」。
            禁用而不是隐藏:让老板知道这两条路存在、只是还没开,少一轮客服提问。 */}
        <div className="grid grid-cols-3 gap-2">
          <Button variant={targetKind === 'username' ? 'default' : 'outline'} data-testid="invite-kind-username" onClick={() => setTargetKind('username')}><UserPlus className="mr-1 h-4 w-4" />用户名</Button>
          {([
            ['phone', '手机号', Phone],
            ['email', '邮箱', Mail],
          ] as const).map(([kind, label, Icon]) => (
            <Button
              key={kind}
              variant="outline"
              disabled
              data-testid={'invite-kind-' + kind}
              title="即将开放"
              aria-label={label + '（即将开放）'}
              className="relative flex-col gap-0 opacity-60"
            >
              <span className="flex items-center"><Icon className="mr-1 h-4 w-4" />{label}</span>
              <span className="text-[10px] font-normal leading-none text-muted-foreground">即将开放</span>
            </Button>
          ))}
        </div>
        {targetKind === 'username' && <div className="mt-2 space-y-2">
          <p className="text-xs text-muted-foreground">用户名方式:你为员工设定登录名,把邀请链接连同登录名发给他;员工打开链接自设密码即可开户(无需短信)。</p>
          {shortCode && <p className="text-xs text-muted-foreground">团队代码 <span className="font-mono font-medium text-foreground">{shortCode}</span> · 员工名只需团队内唯一,不同团队可以重名。</p>}
          {contact.trim() && nameCheck && <div
            data-testid="login-name-preview"
            className={cn('rounded-md border px-3 py-2 text-xs',
              nameCheck.status === 'taken' || nameCheck.status === 'invalid'
                ? 'border-destructive/40 bg-destructive/5'
                : nameCheck.status === 'existing_account'
                  ? 'border-amber-400/50 bg-amber-50/60 dark:bg-amber-500/10'
                  : 'border-border bg-muted/40')}>
            {nameCheck.login_name && <div className="mb-1">最终登录名:<span className="font-mono font-medium text-foreground">{nameCheck.login_name}</span></div>}
            <div className="text-muted-foreground">{nameCheck.message}</div>
          </div>}
        </div>}
        <Label className="sr-only" htmlFor="owner-invite-contact">员工手机号、邮箱或用户名</Label><Input ref={contactRef} id="owner-invite-contact" className="mt-3 h-11 text-base" inputMode={targetKind === 'phone' ? 'tel' : targetKind === 'email' ? 'email' : 'text'} placeholder={targetKind === 'phone' ? '输入员工手机号' : targetKind === 'email' ? '输入员工邮箱' : '为员工设定登录用户名(3-32 位小写字母/数字)'} value={contact} onChange={event => setContact(event.target.value)} />
      </section>
      <section><h3 className="mb-3 flex items-center gap-2 text-base font-semibold"><span className="grid h-7 w-7 place-items-center rounded-full bg-primary text-sm text-primary-foreground">2</span>选择角色与权限</h3>
        <div className="space-y-2">{inviteRoles.map(role => <button key={role.id} type="button" data-testid={'invite-role-' + role.code} onClick={() => setRoleId(String(role.id))} className={cn('w-full rounded-xl border p-4 text-left', String(role.id) === String(selectedRole?.id) ? 'border-primary bg-primary/5' : 'hover:bg-muted/40')}><span className="flex justify-between gap-3"><span className="font-semibold">{role.name}</span><span className={cn('h-4 w-4 rounded-full border', String(role.id) === String(selectedRole?.id) && 'border-4 border-primary')} /></span><span className="mt-1 block text-sm leading-6 text-muted-foreground">{ROLE_SUMMARIES[role.code] || String(role.capabilities.length) + ' 项权限'}</span></button>)}</div>
        <p className="mt-3 flex gap-2 rounded-lg bg-muted/40 p-3 text-sm leading-6 text-muted-foreground"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />销售和交付各管各的，单个员工不能一个人从谈单做到交付。</p>
      </section>
      <section><h3 className="mb-3 flex items-center gap-2 text-base font-semibold"><span className="grid h-7 w-7 place-items-center rounded-full bg-primary text-sm text-primary-foreground">3</span>分配客户和算力</h3>
        <Label>负责客户（不勾选 = 暂不分配客户）</Label><div className="mt-2 max-h-44 space-y-1 overflow-y-auto rounded-xl border p-2">{clients.length ? clients.map(client => <label key={client.id} className="flex min-h-11 items-center gap-3 rounded-lg px-3 hover:bg-muted"><input type="checkbox" checked={brandIds.includes(client.id)} onChange={() => toggleBrand(client.id)} /><span className="truncate text-sm">{client.name}</span></label>) : <p className="p-3 text-sm text-muted-foreground">暂无客户；仍可邀请，员工接受后暂时没有客户。</p>}</div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2"><Field label="每日算力上限（可选）"><Input data-testid="invite-daily-limit" inputMode="numeric" value={dailyLimit} onChange={event => setDailyLimit(event.target.value.replace(/\D/g, ''))} placeholder="例如 200" /></Field><Field label="每月算力上限（可选）"><Input data-testid="invite-monthly-limit" inputMode="numeric" value={monthlyLimit} onChange={event => setMonthlyLimit(event.target.value.replace(/\D/g, ''))} placeholder="例如 3000" /></Field></div>
        <details className="mt-4 rounded-xl border p-3" data-testid="invite-advanced-permissions"><summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium"><SlidersHorizontal className="h-4 w-4" />高级权限（默认建议只减不加）</summary><div className="mt-4 space-y-4">
          <Field label="能看到谁做的内容"><select className="h-11 w-full rounded-md border bg-background px-3 text-sm" value={artifactScope} onChange={event => setArtifactScope(event.target.value as 'own' | 'assigned_team')}><option value="own">{artifactScopeLabel('own')}</option><option value="assigned_team">{artifactScopeLabel('assigned_team')}</option></select></Field>
          <CapabilityPicker
            testId="invite-capability-picker"
            isChecked={effectiveAllows}
            isRoleDefault={roleAllows}
            onToggle={toggleCapability}
          />
          <div className="grid gap-3 sm:grid-cols-3"><Field label="单独限制某个功能"><select className="h-10 w-full rounded-md border bg-background px-3 text-sm" value={featureCode} onChange={event => setFeatureCode(event.target.value)}><option value="">不单独限制</option>{FEATURE_OPTIONS.filter(option => selectedRole?.capabilities.includes(option[2])).map(option => <option key={option[0]} value={option[0]}>{option[1]}</option>)}</select></Field><Field label="该功能每日算力上限"><Input inputMode="numeric" disabled={!featureCode} value={featureDaily} onChange={event => setFeatureDaily(event.target.value.replace(/\D/g, ''))} /></Field><Field label="该功能每月算力上限"><Input inputMode="numeric" disabled={!featureCode} value={featureMonthly} onChange={event => setFeatureMonthly(event.target.value.replace(/\D/g, ''))} /></Field></div>
          {highRisk.length > 0 && <HighRiskConfirm
            codes={highRisk}
            confirmed={riskConfirmed}
            reason={riskReason}
            onConfirm={setRiskConfirmed}
            onReason={setRiskReason}
          />}
        </div></details>
      </section>
      <Button className="h-12 w-full text-base" disabled={busy || !selectedRole || !validContact || (highRisk.length > 0 && (!riskConfirmed || riskReason.trim().length < 3))} onClick={() => void run(sendInvite, '邀请已生成并复制链接')}><UserPlus className="mr-2 h-5 w-5" />生成邀请并复制链接</Button>
      {/* [2026-07-28] 邀请链接必须**看得见**。原来只有一个"再次复制"按钮、还是静默复制,
          老板不知道链接长什么样、也不知道要自己发给员工。现在给一个只读输入框把链接摊开,
          右边配复制按钮(带成功提示),下面一句话说清楚"要自己发出去"。 */}
      <p className="flex gap-2 text-sm leading-6 text-muted-foreground"><ShieldCheck className="mt-1 h-4 w-4 shrink-0" />员工自己验证手机号、设置密码；员工账号没有独立钱包，也不会有推荐奖励、分成或客户归属。</p>
    </div>
  </section>;

  const pending = invites.filter(invite => invite.status === 'pending');
  const memberCard = (member: OrganizationMember) => {
    const role = member.is_owner ? undefined : roles.find(item => item.id === member.role_id);
    const roleCode = member.is_owner ? 'owner' : member.role_code;
    const clientsText = member.is_owner ? '全部客户' : member.assigned_brand_ids.length ? String(member.assigned_brand_ids.length) + ' 个客户' : '零客户';
    const permissionText = member.is_owner ? '全部客户 · 全部功能 · 最终审批' : ROLE_SUMMARIES[member.role_code] || String(member.effective_capabilities?.length || role?.capabilities.length || 0) + ' 项权限';
    return <Card key={member.id}><CardContent className="space-y-3 p-4"><div className="flex items-center justify-between gap-3"><span className="font-medium">{member.display_name || '成员 ' + member.user_id}{member.is_owner && '（我）'}</span><div className="flex shrink-0 items-center gap-1.5"><Badge variant={roleBadgeVariant(roleCode)}>{member.is_owner ? '老板' : member.role_name}</Badge>{!member.is_owner && <Badge variant="secondary">{statusLabel(member.status)}</Badge>}</div></div><div className="grid grid-cols-2 gap-2 text-sm"><Metric label="负责客户" value={clientsText} /><Metric label="每月算力" value={member.is_owner ? '不限制' : memberUsage(member.id, limits)} /></div><p className="text-sm text-muted-foreground">{permissionText}</p>{!member.is_owner && <Button className="w-full" variant="outline" onClick={() => setEditingMember(member)}>编辑成员</Button>}</CardContent></Card>;
  };

  // [§1-2 2026-08-17] 链接面板必须**在抽屉外面**。
  //   原来它长在 `invitePanel` 里 —— 窄视口下 `invitePanel` 是底部抽屉,
  //   而「生成邀请」成功后代码会 `setMobileInviteOpen(false)` 把抽屉关掉,
  //   于是手机上刚生成的链接连同面板一起消失,只剩一句复制 toast。
  //   工单点名的「老板丢了剪贴板(微信/手机场景必然)」恰恰就是手机场景。
  //   现在挪到组件顶层常驻:桌面在长向导顶部就能看到,手机上抽屉开关都不影响。
  const linkPanel = lastInviteUrl ? (
    <div data-testid="invite-link-panel" className="space-y-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3 sm:p-4">
      <p className="text-sm font-medium">邀请链接已生成</p>
      <div className="flex items-center gap-2">
        <Input
          readOnly
          data-testid="invite-link-input"
          aria-label="邀请链接"
          value={lastInviteUrl}
          onFocus={event => event.currentTarget.select()}
          className="h-9 flex-1 font-mono text-xs"
        />
        <Button size="sm" variant="outline" className="shrink-0" data-testid="invite-link-copy"
          onClick={() => void copyInviteUrl(lastInviteUrl)}>
          <Copy className="mr-1 h-4 w-4" />复制
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        系统<strong className="text-foreground">不会自动发送</strong>,请把这条链接
        {targetKind === 'username' ? '连同登录名一起' : ''}发给员工(微信/钉钉均可)。
        员工打开链接即可自设密码开户。
      </p>
    </div>
  ) : null;

  return <>{linkPanel}<div className="grid min-h-[560px] overflow-hidden rounded-2xl border bg-card lg:grid-cols-[minmax(0,1.45fr)_minmax(340px,0.75fr)]"><section className="min-w-0 lg:border-r"><div className="flex items-center justify-between border-b p-5 sm:p-6"><div><h2 className="text-xl font-semibold">团队成员</h2><p className="mt-1 text-sm text-muted-foreground">角色、客户、权限和算力集中查看。</p></div><Badge variant="outline">{employees.length} 位员工</Badge></div>
    <div className="hidden overflow-x-auto lg:block"><table className="w-full min-w-[860px] text-left text-sm"><thead className="bg-muted/30 text-muted-foreground"><tr><th className="p-4">成员</th><th className="p-4">角色</th><th className="p-4">负责客户</th><th className="p-4">访问权限</th><th className="p-4">每月算力</th><th className="p-4">状态 / 操作</th></tr></thead><tbody>{members.map(member => { const roleCode = member.is_owner ? 'owner' : member.role_code; return <tr key={member.id} className="border-t"><td className="p-4 font-medium"><div>{member.display_name || '成员 ' + member.user_id}{member.is_owner && '（我）'}</div>{member.login_name && <button type="button" data-testid={'login-name-' + member.id} title="点击复制登录名" onClick={() => { void navigator.clipboard.writeText(member.login_name || ''); lazyToast.success('登录名已复制:' + member.login_name); }} className="mt-1 flex items-center gap-1 font-mono text-xs font-normal text-muted-foreground hover:text-foreground"><Copy className="h-3 w-3" />{member.login_name}</button>}</td><td className="p-4"><Badge variant={roleBadgeVariant(roleCode)}>{member.is_owner ? '老板' : member.role_name}</Badge></td><td className="p-4">{member.is_owner ? '全部客户' : member.assigned_brand_ids.length ? String(member.assigned_brand_ids.length) + ' 个客户' : '零客户'}</td><td className="max-w-[240px] p-4 text-muted-foreground">{member.is_owner ? '全部功能 · 最终审批' : ROLE_SUMMARIES[member.role_code] || String(member.effective_capabilities?.length || 0) + ' 项权限'}</td><td className="p-4">{member.is_owner ? '不限制' : memberUsage(member.id, limits)}</td><td className="p-4">{member.is_owner ? '—' : <div className="flex items-center gap-2"><Badge variant="secondary">{statusLabel(member.status)}</Badge><Button size="icon" variant="ghost" aria-label={'管理 ' + member.display_name} onClick={() => setEditingMember(member)}><MoreVertical className="h-4 w-4" /></Button></div>}</td></tr>; })}{pending.map(invite => { const role = roles.find(item => item.id === invite.role_id); return <tr key={'invite-' + invite.id} className="border-t bg-amber-500/5"><td className="p-4 font-medium" data-testid={'invite-title-' + invite.id}><div className={cn(invite.login_name && 'font-mono')}>{inviteTitle(invite)}</div><div className="mt-1 text-xs font-normal text-muted-foreground">{inviteTargetKindLabel(invite.target_kind)} · 待接受</div></td><td className="p-4"><Badge variant={roleBadgeVariant(role?.code || '')}>{role?.name || '员工'}</Badge></td><td className="p-4">{invite.access_policy?.brand_ids.length || 0} 个客户</td><td className="p-4 text-muted-foreground">{role ? ROLE_SUMMARIES[role.code] : '等待接受'}</td><td className="p-4">{invite.access_policy?.monthly_limit_points == null ? '未设置' : '0 / ' + invite.access_policy.monthly_limit_points}</td><td className="p-4"><InviteDeliveryHint inviteId={invite.id} items={deliveryItems} expiresAt={invite.expires_at} /><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" data-testid={'invite-reveal-' + invite.id} onClick={() => void run(() => revealInviteLink(invite.id), '邀请链接已找回并复制')}><Copy className="mr-1 h-3.5 w-3.5" />查看链接</Button><Button size="sm" variant="ghost" onClick={() => void run(() => resend(invite.id), '已重新生成并复制邀请链接')}>重发</Button><Button size="sm" variant="ghost" onClick={() => void run(() => mutate('/invites/' + invite.id + '/revoke', { reason: '老板撤销邀请' }), '邀请已撤销')}>撤销</Button></div><p className="mt-1 text-xs text-amber-700">到期：{formatDate(invite.expires_at)}</p></td></tr>; })}</tbody></table></div>
    <div className="space-y-3 p-3 lg:hidden">{members.map(memberCard)}{pending.map(invite => { const role = roles.find(item => item.id === invite.role_id); return <Card key={invite.id} className="border-amber-500/40"><CardContent className="space-y-3 p-4"><div className="flex justify-between gap-2" data-testid={'invite-title-' + invite.id}><span className={cn('min-w-0 break-all font-medium', invite.login_name && 'font-mono')}>{inviteTitle(invite)}</span><Badge variant="outline" className="shrink-0">待接受</Badge></div><p className="text-sm text-muted-foreground">{inviteTargetKindLabel(invite.target_kind)} · {role?.name || '员工'} · {invite.access_policy?.brand_ids.length || 0} 个客户</p><InviteDeliveryHint inviteId={invite.id} items={deliveryItems} expiresAt={invite.expires_at} /><div className="grid grid-cols-3 gap-2"><Button variant="outline" data-testid={'invite-reveal-' + invite.id} onClick={() => void run(() => revealInviteLink(invite.id), '邀请链接已找回并复制')}>查看链接</Button><Button variant="ghost" onClick={() => void run(() => resend(invite.id), '已重新生成并复制邀请链接')}>重发</Button><Button variant="ghost" onClick={() => void run(() => mutate('/invites/' + invite.id + '/revoke', { reason: '老板撤销邀请' }), '邀请已撤销')}>撤销</Button></div></CardContent></Card>; })}</div>
  </section><div className="hidden md:block">{invitePanel}</div></div>
  {mobileInviteOpen && <button type="button" aria-label="关闭邀请遮罩" className="fixed inset-0 z-40 bg-black/50 md:hidden" onClick={() => setMobileInviteOpen(false)} />}<div className="md:hidden">{invitePanel}</div><Button className="fixed inset-x-4 bottom-4 z-30 h-12 shadow-xl md:hidden" onClick={() => setMobileInviteOpen(true)}><UserPlus className="mr-2 h-5 w-5" />邀请员工</Button>
  {editingMember && <MemberAccessDrawer member={editingMember} role={roles.find(role => role.id === editingMember.role_id)} clients={clients} busy={busy} run={run} onClose={() => setEditingMember(null)} />}</>;
}

function MemberAccessDrawer({ member, role, clients, busy, run, onClose }: {
  member: OrganizationMember;
  role?: OrganizationRole;
  clients: ClientOption[];
  busy: boolean;
  run: RunAction;
  onClose: () => void;
}) {
  const [confirmDialogDrawer, askConfirmDrawer] = useConfirmDialog();
  const [brandIds, setBrandIds] = useState<number[]>(member.assigned_brand_ids);
  const [desired, setDesired] = useState<string[]>(member.effective_capabilities || role?.capabilities || []);
  const [touched, setTouched] = useState(false);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [riskReason, setRiskReason] = useState('');
  const baseline = role?.capabilities || [];
  const expansions = desired.filter(code => !baseline.includes(code));
  const save = async () => {
    const assignment = await mutate<{ version: number }>('/members/' + member.id + '/assignments', { brand_ids: brandIds, request_id: requestId('member-assignment'), reason: '老板在成员抽屉更新客户范围' }, 'PUT');
    if (touched) {
      const overrides: Record<string, 'allow' | 'deny'> = {};
      Object.keys(CAPABILITY_LABELS).forEach(code => { if (desired.includes(code) !== baseline.includes(code)) overrides[code] = desired.includes(code) ? 'allow' : 'deny'; });
      await mutate('/members/' + member.id + '/overrides', { expected_membership_version: assignment.version, overrides, reason: '老板在成员抽屉更新模块与动作权限', high_risk_confirmed: expansions.length ? riskConfirmed : false, high_risk_reason: expansions.length ? riskReason.trim() : null }, 'PUT');
    }
    onClose();
  };
  return <div className="fixed inset-0 z-[60] bg-black/50" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside role="dialog" aria-modal="true" aria-labelledby="member-drawer-title" className="absolute inset-y-0 right-0 w-full max-w-xl overflow-y-auto bg-background p-5 shadow-2xl sm:p-7"><div className="flex justify-between gap-3"><div><h2 id="member-drawer-title" className="text-xl font-semibold">编辑成员</h2><p className="mt-1 text-sm text-muted-foreground">{member.display_name} · {member.role_name}</p></div><Button size="icon" variant="ghost" aria-label="关闭成员编辑" onClick={onClose}><X className="h-5 w-5" /></Button></div><div className="mt-6 space-y-6">
    <section><Label>负责客户（不勾选 = 暂不分配客户）</Label><div className="mt-2 max-h-56 space-y-1 overflow-y-auto rounded-xl border p-2">{clients.length ? clients.map(client => <label key={client.id} className="flex min-h-11 items-center gap-3 rounded-lg px-3 hover:bg-muted"><input type="checkbox" checked={brandIds.includes(client.id)} onChange={() => setBrandIds(current => current.includes(client.id) ? current.filter(id => id !== client.id) : [...current, client.id])} /><span>{client.name}</span></label>) : <p className="p-3 text-sm text-muted-foreground">暂无可分配客户</p>}</div></section>
    <details className="rounded-xl border p-3"><summary className="cursor-pointer text-sm font-medium">高级权限</summary><div className="mt-3">
      <CapabilityPicker
        testId="drawer-capability-picker"
        isChecked={code => desired.includes(code)}
        isRoleDefault={code => baseline.includes(code)}
        onToggle={(code, checked) => { setTouched(true); setDesired(current => checked ? [...current, code] : current.filter(item => item !== code)); }}
      />
    </div>{expansions.length > 0 && <div className="mt-3"><HighRiskConfirm codes={expansions} confirmed={riskConfirmed} reason={riskReason} onConfirm={setRiskConfirmed} onReason={setRiskReason} /></div>}</details>
    <div className="grid grid-cols-2 gap-3"><Button variant="outline" onClick={onClose}>取消</Button><Button disabled={busy || (expansions.length > 0 && (!riskConfirmed || riskReason.trim().length < 3))} onClick={() => void run(save, '成员权限与客户范围已更新')}>保存变更</Button></div>
    <div className="grid grid-cols-2 gap-3 border-t pt-5"><Button variant="outline" onClick={() => void run(() => mutate('/members/' + member.id + '/status', { action: member.status === 'suspended' ? 'resume' : 'suspend', reason: member.status === 'suspended' ? '老板恢复员工席位' : '老板暂停员工席位' }), member.status === 'suspended' ? '员工已恢复' : '员工已暂停')}>{member.status === 'suspended' ? '恢复席位' : '暂停席位'}</Button><Button variant="destructive" onClick={async () => { if (await askConfirmDrawer({ title: '移除后会立即撤销客户和新付费权限，确认继续？', confirmLabel: '移除', danger: true })) void run(() => mutate('/members/' + member.id + '/status', { action: 'remove', reason: '老板移除员工' }), '员工已移除'); }}>移除员工</Button></div>
  </div></aside>{confirmDialogDrawer}</div>;
}

function LimitsPanel({ isOwner, members, limits, busy, run }: { isOwner: boolean; members: OrganizationMember[]; limits: OrganizationLimit[]; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const employees = members.filter(member => !member.is_owner && ['active', 'suspended'].includes(member.status));
  const [membershipId, setMembershipId] = useState('');
  const [daily, setDaily] = useState('');
  const [monthly, setMonthly] = useState('');
  useEffect(() => { if (!membershipId && employees[0]) setMembershipId(String(employees[0].id)); }, [employees, membershipId]);
  const visible = isOwner ? limits : limits.filter(limit => limit.membership_id === Number(membershipId) || employees.some(member => member.id === limit.membership_id));
  const save = async () => { const id = Number(membershipId); if (daily !== '') await mutate('/limits', { membership_id: id, limit_kind: 'daily_total', limit_points: Number(daily), reason: '老板设置员工日上限' }); if (monthly !== '') await mutate('/limits', { membership_id: id, limit_kind: 'monthly_total', limit_points: Number(monthly), reason: '老板设置员工月上限' }); };
  return <div className="space-y-5">{isOwner && <Card><CardHeader><CardTitle>设置总使用上限</CardTitle><CardDescription>至少配置日上限和月上限，员工才能发起消耗算力的操作。如需对单个功能单独设上限，在邀请员工时的“高级权限”里配置。</CardDescription></CardHeader><CardContent className="grid gap-3 sm:grid-cols-4"><div><Label>员工</Label><select className="h-10 w-full rounded-md border bg-background px-3 text-sm" value={membershipId} onChange={event => setMembershipId(event.target.value)}>{employees.map(member => <option key={member.id} value={member.id}>{member.display_name}</option>)}</select></div><div><Label htmlFor="daily-limit">每日上限</Label><Input id="daily-limit" inputMode="numeric" value={daily} onChange={event => setDaily(event.target.value.replace(/\D/g, ''))} placeholder="例如 2000" /></div><div><Label htmlFor="monthly-limit">每月上限</Label><Input id="monthly-limit" inputMode="numeric" value={monthly} onChange={event => setMonthly(event.target.value.replace(/\D/g, ''))} placeholder="例如 30000" /></div><div className="flex items-end"><Button className="w-full" disabled={busy || !membershipId || (!daily && !monthly)} onClick={() => void run(save, '使用上限已保存')}>保存上限</Button></div></CardContent></Card>}
    {/* [B1] 卡片标题原来直出 `monthly_total · monitor_single` —— 本模块最高频的英文露出。
        [B13] 右上角原来是「v3」策略版本徽章,对用户零含义。
        [C3] 卡片不显示归属员工,多员工时老板面对一排卡片分不清是谁的。 */}
    <div className="grid gap-3 lg:grid-cols-2">{visible.length ? visible.map(limit => { const net = limit.consumed_points - limit.refunded_points; const remaining = limit.limit_points - limit.reserved_points - net; const owner = members.find(member => member.id === limit.membership_id); return <Card key={limit.id}><CardContent className="p-4"><div className="flex items-center justify-between gap-3"><div className="min-w-0"><div className="truncate font-medium">{isOwner && <span>{owner?.display_name || `员工 #${limit.membership_id}`} · </span>}{limitKindLabel(limit.limit_kind)}{limit.feature_code ? ` · ${featureCodeLabel(limit.feature_code)}` : ''}</div><div className="mt-1 text-xs text-muted-foreground">{formatDate(limit.period_start)} 至 {formatDate(limit.period_end)}</div></div><Badge variant="outline" className="shrink-0">{limitStatusLabel(limit.status)}</Badge></div><div className="mt-4 grid grid-cols-4 gap-2 text-center text-xs"><Metric label="上限" value={limit.limit_points} /><Metric label="进行中占用" value={limit.reserved_points} /><Metric label="已用" value={net} /><Metric label="可用" value={Math.max(remaining, 0)} /></div></CardContent></Card>; }) : <EmptyState text={isOwner ? '尚未设置员工使用上限。' : '老板尚未为你设置使用上限。'} />}</div>
  </div>;
}

function ApprovalPanel({ approvals, policies, members, isOwner, canReview, busy, run }: { approvals: OrganizationApproval[]; policies: OrganizationApprovalPolicy[]; members: OrganizationMember[]; isOwner: boolean; canReview: boolean; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const canDecide = isOwner || canReview;
  const [threshold, setThreshold] = useState('');
  const currentHighCost = policies.find(policy => policy.status === 'active' && policy.action_type === 'billing.execute_high_cost' && !policy.membership_id && !policy.role_id && !policy.feature_code);
  useEffect(() => { if (threshold === '' && currentHighCost?.threshold_points != null) setThreshold(String(currentHighCost.threshold_points)); }, [currentHighCost, threshold]);
  // [发布审批开关] 没有策略行 = 默认「员工直接发」(后端同口径:approval_required_with_default)
  const publishPolicy = policies.find(policy => policy.status === 'active' && policy.action_type === 'publish.execute' && !policy.membership_id && !policy.role_id && !policy.feature_code);
  const publishNeedsApproval = Boolean(publishPolicy?.always_require_approval);
  return <div className="space-y-5">
    {isOwner && <Card>
      <CardHeader>
        <CardTitle>发布需要我审批</CardTitle>
        <CardDescription>
          {publishNeedsApproval
            ? '现在:员工发文章前要你点头。他提交后会出现在下面的审批队列里,批准后才真正发布、才扣算力。'
            : '现在:员工写完可以直接发,不用等你。发布消耗的算力从你的余额扣。'}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <span className="text-sm text-muted-foreground">
          {publishNeedsApproval ? '关掉后员工可直接发布' : '打开后员工发布须先经你批准'}
        </span>
        <Button
          variant={publishNeedsApproval ? 'destructive' : 'default'}
          disabled={busy}
          onClick={() => void run(() => mutate('/approval-policies', {
            request_id: requestId('approval-policy'),
            action_type: 'publish.execute',
            always_require_approval: !publishNeedsApproval,
            expected_version: publishPolicy?.version,
            reason: publishNeedsApproval ? '老板关闭发布审批,员工可直接发布' : '老板开启发布审批',
          }), publishNeedsApproval ? '已改为员工直接发布' : '已改为发布需你审批')}
        >
          {publishNeedsApproval ? '改为员工直接发布' : '改为发布需我审批'}
        </Button>
      </CardContent>
    </Card>}
    {isOwner && <Card><CardHeader><CardTitle>高额算力审批策略</CardTitle><CardDescription>修改后，之前批过但还没执行的申请需要重新审批；已经扣掉的算力不受影响。</CardDescription></CardHeader><CardContent className="flex flex-col gap-3 sm:flex-row sm:items-end"><Field label="需要审批的算力阈值"><Input inputMode="numeric" value={threshold} onChange={event => setThreshold(event.target.value.replace(/\D/g, ''))} placeholder="例如 500" /></Field><Button disabled={busy || threshold === ''} onClick={() => void run(() => mutate('/approval-policies', { request_id: requestId('approval-policy'), action_type: 'billing.execute_high_cost', always_require_approval: false, threshold_points: Number(threshold), expected_version: currentHighCost?.version, reason: '老板调整高额算力审批阈值' }), '审批策略已更新')}>保存</Button></CardContent></Card>}
    {/* [B2/B3] 徽章原来直出 pending/approved,标题原来直出 billing.execute_high_cost。
        [C5] 「申请人 #7」是 user_id,审批人不知道那是谁 —— 有姓名就显示姓名。 */}
    {approvals.length ? <div className="space-y-3">{approvals.map(approval => { const requester = members.find(member => member.id === approval.requested_by_membership_id); return <Card key={approval.id}><CardContent className="flex flex-col gap-4 p-4 lg:flex-row lg:items-center lg:justify-between"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Badge variant={approval.status === 'pending' ? 'default' : 'secondary'}>{approvalStatusLabel(approval.status)}</Badge><span className="break-all font-medium">{approvalActionLabel(approval.action_type)}</span></div><p className="mt-2 text-sm text-muted-foreground" title={`申请人账号编号 ${approval.requested_by_user_id}`}>申请人 {requester?.display_name || `员工 #${approval.requested_by_user_id}`} · 预计 {approval.estimated_points} 算力 · 到期 {formatDate(approval.expires_at)}</p></div>{approval.status === 'pending' && canDecide && <div className="flex gap-2"><Button disabled={busy} onClick={() => void run(() => mutate(`/approvals/${approval.id}/decision`, { decision: 'approve', expected_version: approval.version, reason: '审批通过' }), '审批已通过')}><Check className="mr-1 h-4 w-4" />通过</Button><Button variant="destructive" disabled={busy} onClick={() => void run(() => mutate(`/approvals/${approval.id}/decision`, { decision: 'reject', expected_version: approval.version, reason: '审批人拒绝' }), '审批已拒绝')}><X className="mr-1 h-4 w-4" />拒绝</Button></div>}</CardContent></Card>; })}</div> : <EmptyState text="当前没有审批记录。员工第一次对外发报价、发客户报告链接或真实发布时，会先到这里等你批准。" />}
  </div>;
}

function PlansPanel({ plans, clients, busy, run }: { plans: AutomaticPlan[]; clients: ClientOption[]; busy: boolean; run: (action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const [feature, setFeature] = useState('monitor_single');
  const [brandId, setBrandId] = useState('');
  const [cadenceHours, setCadenceHours] = useState('24');
  const [occurrences, setOccurrences] = useState('30');
  const [ceiling, setCeiling] = useState('130');
  const [budget, setBudget] = useState('3900');
  const create = () => { const start = new Date(); const end = new Date(start.getTime() + Number(cadenceHours) * Number(occurrences) * 3600_000 + 3600_000); return mutate('/automatic-plans', { request_id: requestId('automatic-plan'), feature_code: feature, work_kind: 'monitoring.run', payload: { brand_id: brandId ? Number(brandId) : null }, brand_id: brandId ? Number(brandId) : null, cadence_seconds: Number(cadenceHours) * 3600, max_occurrences: Number(occurrences), total_budget_points: Number(budget), max_occurrence_points: Number(ceiling), starts_at: start.toISOString(), ends_at: end.toISOString() }); };
  // [B6/C4] 「功能」原来是自由文本框,要求老板手输英文功能代码 monitor_single;
  //   「客户」原来是手输数字 ID。两个都改成中文下拉。
  //   [A4] 描述里的「系统 occurrence 使用稳定幂等键」整句删掉 —— 那是给工程看的。
  //   [C4] 「单次上限 / 总预算」补上单位(算力)。
  const planFeatures = FEATURE_OPTIONS.filter(option => option[0] === 'monitor_single');
  return <div className="space-y-5"><Card><CardHeader><CardTitle>创建自动计划</CardTitle><CardDescription>创建后按你设定的频次和预算自动执行，不会重复扣费，也不占用员工的算力上限。</CardDescription></CardHeader><CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6"><Field label="功能"><select className="h-10 w-full rounded-md border bg-background px-3 text-sm" value={feature} onChange={event => setFeature(event.target.value)} aria-label="功能">{planFeatures.map(option => <option key={option[0]} value={option[0]}>{option[1]}</option>)}</select></Field><Field label="客户"><select className="h-10 w-full rounded-md border bg-background px-3 text-sm" value={brandId} onChange={event => setBrandId(event.target.value)} aria-label="客户"><option value="">请选择客户</option>{clients.map(client => <option key={client.id} value={String(client.id)}>{client.name}</option>)}</select></Field><Field label="间隔小时"><Input inputMode="numeric" value={cadenceHours} onChange={event => setCadenceHours(event.target.value.replace(/\D/g, ''))} /></Field><Field label="执行次数"><Input inputMode="numeric" value={occurrences} onChange={event => setOccurrences(event.target.value.replace(/\D/g, ''))} /></Field><Field label="单次上限（算力）"><Input inputMode="numeric" value={ceiling} onChange={event => setCeiling(event.target.value.replace(/\D/g, ''))} /></Field><Field label="总预算（算力）"><Input inputMode="numeric" value={budget} onChange={event => setBudget(event.target.value.replace(/\D/g, ''))} /></Field><div className="sm:col-span-2 lg:col-span-6"><Button disabled={busy || !feature || !cadenceHours || !occurrences || !ceiling || !budget} onClick={() => void run(create, '自动计划已创建')}><CalendarClock className="mr-1 h-4 w-4" />创建自动计划</Button></div></CardContent></Card><div className="grid gap-3 lg:grid-cols-2">{plans.length ? plans.map(plan => <Card key={plan.id}><CardContent className="p-4"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="break-all font-medium">{featureCodeLabel(plan.feature_code)}{plan.brand_id ? ` · ${clients.find(client => client.id === plan.brand_id)?.name || `客户 #${plan.brand_id}`}` : ''}</div><div className="mt-1 text-xs text-muted-foreground">下次：{formatDate(plan.next_occurrence_at)}</div></div><Badge variant="secondary" className="shrink-0">{planStatusLabel(plan.status)}</Badge></div><div className="mt-4 grid grid-cols-3 gap-2 text-center text-xs"><Metric label="执行" value={`${plan.settled_occurrences}/${plan.max_occurrences}`} /><Metric label="已用预算" value={plan.consumed_budget_points - plan.refunded_budget_points} /><Metric label="总预算" value={plan.total_budget_points} /></div>{['active', 'paused'].includes(plan.status) && <div className="mt-4 flex gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => mutate(`/automatic-plans/${plan.id}/status`, { status: plan.status === 'active' ? 'paused' : 'active', expected_version: plan.version }), plan.status === 'active' ? '计划已暂停' : '计划已恢复')}>{plan.status === 'active' ? '暂停' : '恢复'}</Button><Button size="sm" variant="destructive" disabled={busy} onClick={() => void run(() => mutate(`/automatic-plans/${plan.id}/status`, { status: 'cancelled', expected_version: plan.version }), '计划已取消')}>取消</Button></div>}</CardContent></Card>) : <EmptyState text="暂无自动计划。" />}</div></div>;
}

// [B10] 审计表原来三列全是机器码:event.action（org.invite.create）、
//   event.actor_kind（owner/member/system）、event.entity_type（表名)。整表英文。
//   现在三列全走映射;原始码进 title,客服 hover 就能拿到。
function AuditPanel({ events }: { events: AuditEvent[] }) {
  return events.length ? <div className="overflow-hidden rounded-xl border"><div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-sm"><thead className="bg-muted/50 text-xs text-muted-foreground"><tr><th className="p-3">时间</th><th className="p-3">动作</th><th className="p-3">操作者</th><th className="p-3">对象</th><th className="p-3">原因</th></tr></thead><tbody>{events.map(event => <tr key={event.id} className="border-t"><td className="whitespace-nowrap p-3">{formatDate(event.created_at)}</td><td className="p-3 font-medium" title={event.action}>{auditActionLabel(event.action)}</td><td className="p-3" title={event.actor_kind}>{actorKindLabel(event.actor_kind)}{event.actor_user_id ? ` #${event.actor_user_id}` : ''}</td><td className="p-3 break-all" title={event.entity_type}>{entityTypeLabel(event.entity_type)}{event.entity_id ? ` #${event.entity_id}` : ''}</td><td className="max-w-xs break-words p-3 text-muted-foreground">{event.reason || '—'}</td></tr>)}</tbody></table></div></div> : <EmptyState text="暂无团队操作记录。" />;
}

function InfoRow({ label, value }: { label: string; value: string }) { return <div className="flex items-start justify-between gap-4 border-b pb-2 last:border-0"><span className="text-muted-foreground">{label}</span><span className="break-words text-right font-medium">{value}</span></div>; }
function Metric({ label, value }: { label: string; value: string | number }) { return <div className="rounded-lg bg-muted/50 p-2"><div className="truncate text-muted-foreground">{label}</div><div className="mt-1 truncate font-semibold" title={String(value)}>{value}</div></div>; }
function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block"><span className="mb-1.5 block text-sm font-medium leading-none">{label}</span>{children}</label>;
}

function PayerPolicyPanel({ isOwner, busy, run }: { isOwner: boolean; busy: boolean; run: RunAction }) {
  const [view, setView] = useState<OrganizationPayerPolicyView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [shared, setShared] = useState(false);
  const [overage, setOverage] = useState(false);
  const [perAction, setPerAction] = useState('');
  const [dailyCap, setDailyCap] = useState('');
  const [monthlyCap, setMonthlyCap] = useState('');
  const [reason, setReason] = useState('');
  const [confirmDialog, confirm] = useConfirmDialog();

  const load = useCallback(async () => {
    try {
      const data = await getPayerPolicy();
      setView(data);
      setLoadError(null);
      if (data.viewer === 'owner') {
        setShared(data.policy.shared_payer_enabled);
        setOverage(data.policy.overage_enabled);
        setPerAction(data.policy.per_action_limit_points == null ? '' : String(data.policy.per_action_limit_points));
        setDailyCap(data.policy.daily_limit_points == null ? '' : String(data.policy.daily_limit_points));
        setMonthlyCap(data.policy.monthly_limit_points == null ? '' : String(data.policy.monthly_limit_points));
      }
    } catch (caught) {
      setLoadError(errorMessage(caught));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (loadError && !view) {
    return <div role="alert" className="rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">{loadError}</div>;
  }
  if (!view) {
    return <div className="h-32 animate-pulse rounded-2xl bg-muted" />;
  }

  if (view.viewer === 'member') {
    return (
      <Card data-testid="my-billing-view">
        <CardHeader>
          <CardTitle>我的算力上限</CardTitle>
          <CardDescription>你的可用算力上限由老板设置；用超了是否自动继续走老板钱包，也由老板决定。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={view.shared_payer_enabled ? 'default' : 'secondary'}>
              老板代付{view.shared_payer_enabled ? '已开启' : '未开启'}
            </Badge>
            {view.shared_payer_enabled && (
              <Badge variant={view.overage_enabled ? 'outline' : 'secondary'}>
                超额代付{view.overage_enabled ? '已开启' : '未开启'}
              </Badge>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 text-center text-xs sm:grid-cols-4">
            <Metric label="今日代付已用" value={view.my_usage.daily_overage_used_points} />
            <Metric label="本月代付已用" value={view.my_usage.monthly_overage_used_points} />
            <Metric label="今日周期" value={formatDate(view.my_usage.daily_period_end)} />
            <Metric label="本月周期" value={formatDate(view.my_usage.monthly_period_end)} />
          </div>
          <p className="flex gap-2 text-sm leading-6 text-muted-foreground">
            <ShieldCheck className="mt-1 h-4 w-4 shrink-0" />
            你的可用算力见下方“算力上限”卡片；你看不到老板的钱包余额，也看不到其他员工的账。
          </p>
        </CardContent>
      </Card>
    );
  }

  // [C6 收口 2026-08-17] 「员工费用」和「算力上限」合并成一个页签后,这两块变成兄弟节点。
  //   原来这里是 `const policy = view.policy` 直接解引用:后端一旦回了既不是 owner 形态、
  //   也不是 member 形态的响应(降级/灰度/字段缺失),这一行就抛,整个页签连同下方的
  //   「算力上限」一起白掉 —— 合并之前它俩在两个页签里,崩一个不影响另一个。
  //   现在缺 policy 就降级成一句提示,不拖累同页签的其它内容。
  if (!view.policy) {
    return (
      <div role="alert" className="rounded-xl border border-amber-500/50 bg-amber-500/10 p-4 text-sm">
        员工费用代付设置暂时读不到，请刷新页面重试；下方的算力上限不受影响。
      </div>
    );
  }
  const policy = view.policy;
  const enabling = (shared && !policy.shared_payer_enabled) || (overage && !policy.overage_enabled);
  const capsReady = perAction !== '' && dailyCap !== '' && monthlyCap !== ''
    && Number(perAction) > 0 && Number(dailyCap) > 0 && Number(monthlyCap) > 0;
  const needsCaps = shared || overage;

  const save = async () => {
    if (enabling) {
      const ok = await confirm({
        title: '确认开启员工费用代付？',
        description: '开启后，员工用超自己的算力上限时，会自动从你的钱包继续扣，并受你设置的单次、每日、每月上限约束。每次改动都会留记录，之前批过但还没执行的授权会失效。',
        confirmLabel: '确认开启',
        cancelLabel: '再想想',
        danger: true,
      });
      if (!ok) return;
    }
    await putPayerPolicy({
      shared_payer_enabled: shared,
      overage_enabled: overage,
      per_action_limit_points: perAction === '' ? null : Number(perAction),
      daily_limit_points: dailyCap === '' ? null : Number(dailyCap),
      monthly_limit_points: monthlyCap === '' ? null : Number(monthlyCap),
      reason: reason.trim(),
      expected_version: policy.policy_version,
      request_id: requestId('payer-policy'),
    });
    await load();
  };

  return (
    <Card data-testid="payer-policy-panel">
      <CardHeader>
        <CardTitle>员工费用代付</CardTitle>
        <CardDescription>
          员工花的算力始终从老板钱包扣；这里控制“员工用超了上限，是否自动继续走老板钱包”，以及单次、每日、每月最多帮员工垫付多少。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {!view.global_shared_payer_flag && (
          <div role="alert" className="rounded-xl border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            平台暂未开放员工代付功能：此处设置会先保存，等平台开放后生效。
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={cn('flex min-h-14 items-center justify-between gap-3 rounded-xl border p-4', !isOwner && 'opacity-60')}>
            <span className="text-sm"><span className="block font-medium">员工的花费由我的钱包结算</span><span className="text-muted-foreground">关闭后员工不能发起任何消耗算力的操作</span></span>
            <input type="checkbox" className="h-5 w-5" checked={shared} disabled={!isOwner || busy}
              onChange={event => setShared(event.target.checked)} data-testid="shared-payer-toggle" />
          </label>
          <label className={cn('flex min-h-14 items-center justify-between gap-3 rounded-xl border p-4', !isOwner && 'opacity-60')}>
            <span className="text-sm"><span className="block font-medium">员工用超上限时，自动继续走我的钱包</span><span className="text-muted-foreground">超出员工上限的部分，按下方三个上限约束</span></span>
            <input type="checkbox" className="h-5 w-5" checked={overage} disabled={!isOwner || busy}
              onChange={event => setOverage(event.target.checked)} data-testid="overage-toggle" />
          </label>
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="单次代付上限"><Input inputMode="numeric" value={perAction} disabled={!isOwner} onChange={event => setPerAction(event.target.value.replace(/\D/g, ''))} placeholder="例如 100" /></Field>
          <Field label="每日代付上限"><Input inputMode="numeric" value={dailyCap} disabled={!isOwner} onChange={event => setDailyCap(event.target.value.replace(/\D/g, ''))} placeholder="例如 500" /></Field>
          <Field label="每月代付上限"><Input inputMode="numeric" value={monthlyCap} disabled={!isOwner} onChange={event => setMonthlyCap(event.target.value.replace(/\D/g, ''))} placeholder="例如 2000" /></Field>
        </div>
        <div className="grid grid-cols-2 gap-2 text-center text-xs sm:grid-cols-4">
          <Metric label="今日代付已用" value={view.usage.daily_overage_used_points} />
          <Metric label="今日代付剩余" value={view.usage.daily_overage_remaining_points == null ? '—' : view.usage.daily_overage_remaining_points} />
          <Metric label="本月代付已用" value={view.usage.monthly_overage_used_points} />
          <Metric label="本月代付剩余" value={view.usage.monthly_overage_remaining_points == null ? '—' : view.usage.monthly_overage_remaining_points} />
        </div>
        {isOwner && (
          <div className="space-y-3">
            <Field label="本次调整原因（必填，写入审计）"><Input value={reason} onChange={event => setReason(event.target.value)} placeholder="例如：开放交付组员工诊断代付" /></Field>
            {enabling && (
              <p role="alert" className="flex gap-2 rounded-xl border border-amber-500/50 bg-amber-500/10 p-3 text-sm leading-6">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                即将开启代付：员工用超上限后的花费会从你的钱包扣。保存时会再确认一次。
              </p>
            )}
            <Button
              disabled={busy || reason.trim().length === 0 || (needsCaps && !capsReady)}
              onClick={() => void run(save, '员工费用代付策略已保存')}
              data-testid="payer-policy-save"
              title={`当前设置版本 ${policy.policy_version}`}
            >
              保存代付设置
            </Button>
          </div>
        )}
        {policy.disabled_at && !policy.shared_payer_enabled && !policy.overage_enabled && (
          <p className="text-xs text-muted-foreground">上次关闭时间：{formatDate(policy.disabled_at)}</p>
        )}
      </CardContent>
      {confirmDialog}
    </Card>
  );
}

// W2 挂载辅助：按邀请 id 从 W1 的 delivery-states 中取 invite_link 送达状态。
// W1 未交付或该邀请尚无送达记录时不渲染任何徽章（占位降级）。
function InviteDeliveryHint({ inviteId, items, expiresAt }: { inviteId: number; items: InviteDeliveryStateItem[]; expiresAt?: string }) {
  const delivery = items.find(item => Number(item.invite_id) === Number(inviteId))?.deliveries?.invite_link;
  if (!delivery || !delivery.state) return null;
  return (
    <div className="mb-2">
      <InviteDeliveryStatus
        state={delivery.state}
        failureCode={delivery.failure_code}
        subject="invite_link"
        hint={expiresAt ? `邀请到期：${formatDate(expiresAt)}` : undefined}
      />
    </div>
  );
}
