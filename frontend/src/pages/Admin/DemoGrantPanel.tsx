import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckSquare, Eye, Loader2, RefreshCw, Search, ShieldX, Square } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

interface CatalogCase {
  case_id: string;
  brand_id: number;
  diagnosis_id: number;
  brand_name: string;
  industry?: string | null;
  owner_user_id?: number | null;
  owner_label?: string | null;
  diagnosis_created_at?: string | null;
  frozen: boolean;
}

interface DemoGrant {
  id: number;
  case_id: string;
  brand_id: number;
  diagnosis_id: number;
  grantee_kind: 'user' | 'organization';
  grantee_user_id?: number | null;
  grantee_organization_id?: number | null;
  grantee_label?: string | null;
  case_label?: string | null;
  industry?: string | null;
  capability: 'demo.customer.preview';
  status: 'active' | 'revoked' | 'expired';
  expires_at: string;
  note: string;
  version: number;
}

function errorText(caught: unknown): string {
  const error = caught as { response?: { data?: { detail?: string | { message?: string } } }; message?: string };
  const detail = error.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && detail.message) return detail.message;
  return error.message || '演示客户操作失败';
}

export default function DemoGrantPanel({ userId }: { userId: number }) {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [kind, setKind] = useState<'user' | 'organization'>('user');
  const [organizationId, setOrganizationId] = useState('');
  const [search, setSearch] = useState('');
  const [catalog, setCatalog] = useState<CatalogCase[]>([]);
  const [selectedCases, setSelectedCases] = useState<Set<string>>(new Set());
  const [selectedGrants, setSelectedGrants] = useState<Set<number>>(new Set());
  const [days, setDays] = useState('30');
  const [note, setNote] = useState('');
  const [reason, setReason] = useState('');
  const [grants, setGrants] = useState<DemoGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const authorityGenerationRef = useRef(0);
  const authorityRef = useRef('');
  const loadAbortRef = useRef<AbortController | null>(null);
  const mutationAbortRef = useRef<AbortController | null>(null);
  const catalogAbortRef = useRef<AbortController | null>(null);
  const catalogSequenceRef = useRef(0);
  const granteeId = kind === 'user' ? userId : Number(organizationId || 0);
  const authority = `${kind}:${granteeId || 0}`;
  authorityRef.current = authority;

  const stillCurrent = (generation: number, expectedAuthority: string) => (
    generation === authorityGenerationRef.current && expectedAuthority === authorityRef.current
  );

  const load = useCallback(async () => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    const generation = authorityGenerationRef.current;
    const expectedAuthority = authority;
    setLoading(true); setError(null); setGrants([]); setSelectedGrants(new Set());
    if (!granteeId) { if (stillCurrent(generation, expectedAuthority)) setLoading(false); return; }
    try {
      const response = await authApi.get(`/api/admin/cross-tenant-governance/demo-grants?page=1&page_size=100&grantee_kind=${kind}&grantee_id=${granteeId}`, {
        headers: { 'X-Request-ID': `demo-grants-list-${safeRandomUUID()}`, 'X-Governance-Reason': 'admin-demo-grants-list' },
        signal: controller.signal,
      });
      if (stillCurrent(generation, expectedAuthority)) setGrants(response.data.grants as DemoGrant[]);
    } catch (caught) {
      if (stillCurrent(generation, expectedAuthority) && !controller.signal.aborted) setError(errorText(caught));
    } finally {
      if (stillCurrent(generation, expectedAuthority)) setLoading(false);
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
    }
  }, [authority, granteeId, kind]);

  const searchCatalog = useCallback(async () => {
    const sequence = ++catalogSequenceRef.current;
    catalogAbortRef.current?.abort();
    const controller = new AbortController();
    catalogAbortRef.current = controller;
    setCatalogLoading(true); setError(null); setSelectedCases(new Set());
    try {
      const response = await authApi.get('/api/admin/cross-tenant-governance/demo-cases/catalog', {
        params: { page: 1, page_size: 100, search: search.trim() || undefined },
        headers: { 'X-Request-ID': `demo-catalog-${safeRandomUUID()}`, 'X-Governance-Reason': 'admin-demo-case-catalog-search' },
        signal: controller.signal,
      });
      if (sequence === catalogSequenceRef.current) setCatalog(response.data.cases as CatalogCase[]);
    } catch (caught) {
      if (sequence === catalogSequenceRef.current && !controller.signal.aborted) setError(errorText(caught));
    } finally {
      if (sequence === catalogSequenceRef.current) setCatalogLoading(false);
      if (catalogAbortRef.current === controller) catalogAbortRef.current = null;
    }
  }, [search]);

  useEffect(() => {
    authorityGenerationRef.current += 1;
    loadAbortRef.current?.abort();
    mutationAbortRef.current?.abort();
    setSaving(false); setError(null); setSelectedGrants(new Set()); setGrants([]);
    void load();
    return () => {
      authorityGenerationRef.current += 1;
      loadAbortRef.current?.abort();
      mutationAbortRef.current?.abort();
    };
  }, [load]);
  useEffect(() => { void searchCatalog(); }, [searchCatalog]);
  useEffect(() => () => {
    catalogSequenceRef.current += 1;
    catalogAbortRef.current?.abort();
  }, []);

  const chosen = useMemo(() => catalog.filter(item => selectedCases.has(item.case_id)), [catalog, selectedCases]);
  const activeSelectedGrants = useMemo(
    () => grants.filter(item => item.status === 'active' && selectedGrants.has(item.id)),
    [grants, selectedGrants],
  );
  const toggleCase = (caseId: string) => setSelectedCases(current => {
    const next = new Set(current); if (next.has(caseId)) next.delete(caseId); else next.add(caseId); return next;
  });
  const toggleGrant = (grantId: number) => setSelectedGrants(current => {
    const next = new Set(current); if (next.has(grantId)) next.delete(grantId); else next.add(grantId); return next;
  });

  const create = async () => {
    const parsedDays = Number(days);
    if (!granteeId || !chosen.length || !Number.isInteger(parsedDays) || parsedDays < 1 || parsedDays > 366 || reason.trim().length < 2) return;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    const generation = authorityGenerationRef.current;
    const expectedAuthority = authority;
    setSaving(true); setError(null);
    try {
      await authApi.post('/api/admin/cross-tenant-governance/demo-grants', {
        grantee_kind: kind,
        grantee_user_id: kind === 'user' ? userId : null,
        grantee_organization_id: kind === 'organization' ? granteeId : null,
        selections: chosen.map(item => ({ case_id: item.case_id, brand_id: item.brand_id, diagnosis_id: item.diagnosis_id })),
        capability: 'demo.customer.preview',
        expires_at: new Date(Date.now() + parsedDays * 86400_000).toISOString(),
        note: note.trim(), reason: reason.trim(), confirmation: 'GRANT_DEMO_CUSTOMER_PREVIEW',
      }, { headers: { 'X-Request-ID': `demo-grant-create-${safeRandomUUID()}` }, signal: controller.signal });
      if (!stillCurrent(generation, expectedAuthority)) return;
      setSelectedCases(new Set()); setNote(''); await load();
    } catch (caught) {
      if (stillCurrent(generation, expectedAuthority) && !controller.signal.aborted) setError(errorText(caught));
    } finally {
      if (stillCurrent(generation, expectedAuthority)) setSaving(false);
      if (mutationAbortRef.current === controller) mutationAbortRef.current = null;
    }
  };

  const revokeBatch = async (items: DemoGrant[]) => {
    if (!items.length) return;
    if (!(await askConfirm({
      title: `确认立即撤回 ${items.length} 份演示客户授权？`,
      description: '所有 URL、列表、详情、WS 和缓存会即时失效。',
      confirmLabel: '立即撤回',
      danger: true,
    }))) return;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    const generation = authorityGenerationRef.current;
    const expectedAuthority = authority;
    setSaving(true); setError(null);
    try {
      await authApi.post('/api/admin/cross-tenant-governance/demo-grants/batch-revoke', {
        grants: items.map(item => ({ grant_id: item.id, expected_version: item.version })),
        reason: reason.trim() || '管理员立即撤回演示客户访问',
        confirmation: 'REVOKE_DEMO_ACCESS',
      }, { headers: { 'X-Request-ID': `demo-grant-revoke-${safeRandomUUID()}` }, signal: controller.signal });
      if (!stillCurrent(generation, expectedAuthority)) return;
      await load();
    } catch (caught) {
      if (stillCurrent(generation, expectedAuthority) && !controller.signal.aborted) setError(errorText(caught));
    } finally {
      if (stillCurrent(generation, expectedAuthority)) setSaving(false);
      if (mutationAbortRef.current === controller) mutationAbortRef.current = null;
    }
  };

  return <div className="space-y-5" data-testid="demo-grant-panel">
    <section className="rounded-xl border p-4 sm:p-5">
      <div className="mb-4"><h3 className="font-semibold">演示客户授权</h3><p className="mt-1 text-xs leading-5 text-muted-foreground">按不可变 brand_id + case_id 授权；与真实客户归属、付款、代理关系和组织客户分配完全分离。动作可讲解，到副作用边界统一返回零写入流程预览。</p></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="space-y-1.5 text-sm"><span>授权对象</span><Select value={kind} onValueChange={value => setKind(value as typeof kind)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="user">当前账号 #{userId}</SelectItem><SelectItem value="organization">指定组织</SelectItem></SelectContent></Select></label>
        {kind === 'organization' && <label className="space-y-1.5 text-sm"><span>组织 ID</span><Input aria-label="组织 ID" inputMode="numeric" value={organizationId} onChange={event => setOrganizationId(event.target.value.replace(/\D/g, ''))} /></label>}
        <label className="space-y-1.5 text-sm"><span>有效天数（1-366）</span><Input inputMode="numeric" value={days} onChange={event => setDays(event.target.value.replace(/\D/g, ''))} /></label>
        <label className="space-y-1.5 text-sm"><span>备注（选填）</span><Input value={note} maxLength={500} onChange={event => setNote(event.target.value)} placeholder="销售场景/审批单" /></label>
      </div>
      <label className="mt-3 block space-y-1.5 text-sm"><span>授权原因（必填）</span><Input value={reason} maxLength={500} onChange={event => setReason(event.target.value)} placeholder="例如：Owner 批准明日产品演示" /></label>
      <div className="mt-4 flex gap-2"><div className="relative flex-1"><Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" /><Input className="pl-9" value={search} onChange={event => setSearch(event.target.value)} placeholder="按客户名、行业、owner 或 brand_id 搜索" /></div><Button variant="outline" onClick={() => void searchCatalog()} disabled={catalogLoading}>{catalogLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : '搜索'}</Button></div>
      <div className="mt-3 max-h-80 space-y-2 overflow-y-auto rounded-lg border p-2" data-testid="demo-case-catalog">
        {catalogLoading ? <div className="grid min-h-24 place-items-center"><Loader2 className="h-5 w-5 animate-spin" /></div> : catalog.length ? catalog.map(item => {
          const selected = selectedCases.has(item.case_id);
          return <button type="button" key={item.case_id} onClick={() => toggleCase(item.case_id)} className={`flex w-full items-start gap-3 rounded-md border p-3 text-left ${selected ? 'border-brand bg-brand/5' : 'hover:bg-muted/50'}`}>
            {selected ? <CheckSquare className="mt-0.5 h-4 w-4 shrink-0 text-brand" /> : <Square className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />}
            <span className="min-w-0"><span className="block font-medium">{item.brand_name}</span><span className="mt-1 block break-words text-xs text-muted-foreground">brand #{item.brand_id} · case {item.case_id} · diagnosis #{item.diagnosis_id}</span><span className="mt-1 block text-xs text-muted-foreground">owner {item.owner_label || `#${item.owner_user_id}`} · {item.industry || '行业未填'} {item.frozen ? '· 已冻结安全快照' : ''}</span></span>
          </button>;
        }) : <div className="p-6 text-center text-sm text-muted-foreground">没有匹配且已发布诊断的客户。</div>}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2"><Button onClick={() => void create()} disabled={saving || !granteeId || !chosen.length || reason.trim().length < 2}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}批量授权 {chosen.length || ''}</Button><Badge variant="outline"><Eye className="mr-1 h-3.5 w-3.5" />demo.customer.preview</Badge><Badge variant="outline"><ShieldX className="mr-1 h-3.5 w-3.5" />零写入 / 零资金 / 零 provider</Badge></div>
    </section>
    {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div>}
    <section><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">授权记录</h3><div className="flex gap-2">{activeSelectedGrants.length > 0 && <Button variant="destructive" size="sm" onClick={() => void revokeBatch(activeSelectedGrants)} disabled={saving}>批量撤回 {activeSelectedGrants.length}</Button>}<Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}><RefreshCw className="mr-1 h-4 w-4" />刷新</Button></div></div>{loading ? <div className="grid min-h-32 place-items-center"><Loader2 className="h-5 w-5 animate-spin" /></div> : grants.length ? <div className="space-y-2">{grants.map(grant => <article key={grant.id} className="flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between"><div className="flex min-w-0 items-start gap-3">{grant.status === 'active' ? <button onClick={() => toggleGrant(grant.id)} aria-label="选择授权">{selectedGrants.has(grant.id) ? <CheckSquare className="h-4 w-4 text-brand" /> : <Square className="h-4 w-4 text-muted-foreground" />}</button> : <span className="w-4" />}<div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{grant.case_label || `客户 #${grant.brand_id}`}</span><Badge variant={grant.status === 'active' ? 'default' : 'outline'}>{grant.status}</Badge></div><div className="mt-1 break-words text-xs text-muted-foreground">brand #{grant.brand_id} · case {grant.case_id} · diagnosis #{grant.diagnosis_id}</div><div className="mt-1 text-xs text-muted-foreground">{grant.industry || '行业未填'} · 到期 {new Date(grant.expires_at).toLocaleString('zh-CN', { hour12: false })}{grant.note ? ` · ${grant.note}` : ''}</div></div></div>{grant.status === 'active' && <Button variant="destructive" size="sm" disabled={saving} onClick={() => void revokeBatch([grant])}>立即撤回</Button>}</article>)}</div> : <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">当前对象没有演示客户授权。</div>}</section>
    {confirmDialog}
  </div>;
}
