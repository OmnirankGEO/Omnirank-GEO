/**
 * 营销资料确认页 — 客户端公开页面
 * 路由: /m/:token
 * 设计: 精美专业的品牌资料展示，让客户一目了然看到推广内容
 */
import { useState, useEffect, useCallback } from 'react';
import { useForceLightMode } from '@/pages/Selection/hooks/useForceLightMode';
import { useSoftKeyboardOpen } from '@/hooks/useSoftKeyboardOpen';
import { useParams } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { useBranding, type WhitelabelBrand } from '@/hooks/useWhitelabel';
import { BrandLogo, BrandFooter } from '@/components/brand/BrandDisplay';
import { OssAttribution } from '@/components/common/OssAttribution';

// ========== Types ==========
interface MaterialsData {
  company: {
    name: string;
    brand_name: string;
    industry: string;
    intro: string;
    core_value: string;
    target_users: string;
    service_area: string;
  };
  selling_points: {
    summary: string;
    items: Array<{ point: string; evidence: string }>;
    usp: string;
    advantages: string[];
    killer_data: string[];
  };
  products: {
    name: string;
    features: string[];
    scenarios: string[];
    metrics: string;
  };
  customers: {
    segments: string[];
    needs: string[];
    barriers: string[];
    concerns: string[];
    pain_scenario: string;
    pain_triggers: string;
  };
  competitors: {
    names: string[];
  };
  cases: Array<{
    client?: string;
    background?: string;
    solution?: string;
    results?: string;
    quote?: string;
  }>;
  testimonials: Array<{
    name?: string;
    title?: string;
    company?: string;
    quote?: string;
  }>;
  contact?: {
    phone?: string;
    wechat?: string;
    website?: string;
    address?: string;
  };
  images?: Array<{
    id?: number | string;
    url?: string;
    thumbnail_url?: string;
    title?: string;
    caption?: string;
    image_type?: string;
    rights_confirmed?: boolean;
  }>;
  credentials: Array<{ type?: string; name?: string }>;
  methodology: string;
}

interface PageData {
  status: string;
  brand_name: string;
  materials: MaterialsData;
  confirmed_at?: string;
  customer_notes?: string;
  // v3.6 白标 · 客户页下发契约(B3 后端 /api/m/:token 已新增)
  // [audit #10 返修] owner_user_id 已从 /m 响应移除(改读内联 whitelabel,不暴露代理 user_id)
  whitelabel?: WhitelabelBrand | null;
  branding_status?: 'platform' | 'approved_whitelabel';
}

interface MaterialEdit {
  path: string;
  value: string;
}

interface EditingField {
  path: string;
  label: string;
  value: string;
  multiline?: boolean;
}

function cloneMaterials(materials: MaterialsData): MaterialsData {
  return JSON.parse(JSON.stringify(materials)) as MaterialsData;
}

function readMaterialPath(root: MaterialsData | null, path: string): string {
  if (!root) return '';
  const parts = path.split('.');
  let node: any = root;
  for (const part of parts) {
    if (node === null || node === undefined) return '';
    const key: string | number = /^\d+$/.test(part) ? Number(part) : part;
    node = node[key];
  }
  if (node === null || node === undefined) return '';
  if (Array.isArray(node)) return node.join('、');
  if (typeof node === 'object') return JSON.stringify(node);
  return String(node);
}

function writeMaterialPath(root: MaterialsData, path: string, value: string): MaterialsData {
  const next = cloneMaterials(root);
  const parts = path.split('.');
  let node: any = next;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i];
    const key: string | number = /^\d+$/.test(part) ? Number(part) : part;
    if (node[key] === null || node[key] === undefined) return next;
    node = node[key];
  }
  const last = parts[parts.length - 1];
  const key: string | number = /^\d+$/.test(last) ? Number(last) : last;
  node[key] = value;
  return next;
}

// ========== Section Components ==========

function SectionHeader({ icon, title, subtitle }: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <div className="w-10 h-10 rounded-2xl bg-gradient-to-br from-indigo-500 to-purple-500 flex items-center justify-center text-white shrink-0 shadow-lg shadow-indigo-200/50">
        {icon}
      </div>
      <div>
        <h2 className="text-lg font-bold text-gray-900">{title}</h2>
        {subtitle && <p className="text-xs text-gray-400 mt-0.5">{subtitle}</p>}
      </div>
    </div>
  );
}

function InfoCard({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`mc-card bg-white rounded-2xl border border-gray-100 shadow-xs p-5 md:p-6 ${className}`}>
      {children}
    </div>
  );
}

function TagList({ items, color = 'gray' }: { items: string[]; color?: 'gray' | 'indigo' | 'emerald' | 'amber' }) {
  const colors = {
    gray: 'bg-gray-50 text-gray-600 border-gray-100',
    indigo: 'bg-indigo-50 text-indigo-700 border-indigo-100',
    emerald: 'bg-emerald-50 text-emerald-700 border-emerald-100',
    amber: 'bg-amber-50 text-amber-700 border-amber-100',
  };
  if (!items?.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item, i) => (
        <span key={i} className={`text-xs px-3 py-1.5 rounded-full border font-medium ${colors[color]}`}>
          {item}
        </span>
      ))}
    </div>
  );
}

function DataPoint({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div>
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className="text-sm text-gray-800 leading-relaxed">{value}</p>
    </div>
  );
}

function EditInlineButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="shrink-0 rounded-full border border-indigo-100 bg-indigo-50 px-2.5 py-1 text-[11px] font-medium text-indigo-600 active:scale-95"
    >
      修改此处
    </button>
  );
}

// ========== Main Page ==========

export function MaterialConfirmPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<PageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notes, setNotes] = useState('');

  // v3.6 白标 · /m/:token 客户场景 · [audit #10 返修] 读 /api/m/:token 内联白标(不再按 owner_user_id 二次调枚举端点)
  const { brand } = useBranding({
    inlineWhitelabel: (data?.whitelabel as WhitelabelBrand | null | undefined) ?? null,
    surface: 'customer',
  });
  const [submitting, setSubmitting] = useState(false);
  const [submittingFeedback, setSubmittingFeedback] = useState(false);
  const [showSuccess, setShowSuccess] = useState(false);
  const [showFeedbackSent, setShowFeedbackSent] = useState(false);
  const [brokenImageKeys, setBrokenImageKeys] = useState<Record<string, boolean>>({});
  const [draftMaterials, setDraftMaterials] = useState<MaterialsData | null>(null);
  const [pendingEdits, setPendingEdits] = useState<Record<string, string>>({});
  const [editingField, setEditingField] = useState<EditingField | null>(null);

  // [2026-06-02 修复 · 客户公开页崩坏] 强制亮色 —— 本页硬编码亮色设计(bg-white/text-gray-900),
  // 若残留代理端 .dark class,白卡被暗色主题重映射成深色、深字变浅(标题看不清)、底部按钮条消失。
  // 复用项目标准 useForceLightMode(已升级 useLayoutEffect 防闪色 · unmount 恢复)。
  useForceLightMode();
  // [WO_IOS_TOUCH_UX 2026-08-05] 底部固定条里有备注 textarea → 键盘弹出要把整条顶上去
  const { inset: keyboardInset } = useSoftKeyboardOpen();

  const fetchData = useCallback(async () => {
    if (!token) return;
    try {
      const res = await fetch(`/api/m/${token}`);
      if (!res.ok) {
        if (res.status === 404) setError('链接不存在或已失效');
        else setError('加载失败');
        return;
      }
      const d: PageData = await res.json();
      setData(d);
      setDraftMaterials(d.materials);
      setPendingEdits({});
      if (d.customer_notes) setNotes(d.customer_notes);
      if (d.status === 'confirmed') setShowSuccess(true);
    } catch {
      setError('网络错误，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchData(); }, [fetchData]);

  // CTO-F · 写作资料确认链客户行为埋点(已存在 m3 events whitelist · raw_token 后端 hash · 失败静默)
  const trackEvent = useCallback(
    (eventType: 'opened' | 'material_confirmed' | 'material_feedback', eventKey?: string) => {
      if (!token) return;
      // 静默 fire-and-forget
      fetch('/api/m3/customer-events/public', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: 'material_confirm',
          event_type: eventType,
          event_key: eventKey,
          raw_token: token,
        }),
      }).catch(() => {
        /* 静默失败 · 不阻塞页面 */
      });
    },
    [token],
  );

  // opened 事件(每次进页面 · event_key 含 token hash 防爆量 · 后端 _sanitize_event_key 自动 hash 替换 token)
  useEffect(() => {
    if (!token || !data) return;
    if (data.status === 'expired') return;
    trackEvent('opened', `material_confirm:opened:${token}`);
  }, [token, data, trackEvent]);

  const getDraftValue = useCallback(
    (path: string) => readMaterialPath(draftMaterials, path),
    [draftMaterials],
  );

  const openInlineEdit = (path: string, label: string, multiline = true) => {
    setEditingField({
      path,
      label,
      multiline,
      value: getDraftValue(path),
    });
  };

  const saveInlineEdit = () => {
    if (!editingField || !draftMaterials) return;
    setDraftMaterials(writeMaterialPath(draftMaterials, editingField.path, editingField.value));
    setPendingEdits(prev => ({ ...prev, [editingField.path]: editingField.value }));
    setEditingField(null);
  };

  const materialEdits: MaterialEdit[] = Object.entries(pendingEdits).map(([path, value]) => ({ path, value }));
  const hasInlineEdits = materialEdits.length > 0;

  const submitMaterialsPatch = async (action: 'feedback' | 'confirm') => {
    if (!token) return false;
    const res = await fetch(`/api/m/${token}/materials`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action,
        note: notes,
        edits: materialEdits,
      }),
    });
    if (!res.ok) return false;
    const result = await res.json();
    if (result.materials) {
      setDraftMaterials(result.materials);
      setData(prev => (prev ? { ...prev, status: result.status || prev.status, materials: result.materials } : prev));
    }
    setPendingEdits({});
    return true;
  };

  const handleConfirm = async () => {
    if (!token) return;
    setSubmitting(true);
    try {
      if (hasInlineEdits) {
        const ok = await submitMaterialsPatch('confirm');
        if (ok) {
          setShowSuccess(true);
          trackEvent('material_confirmed', `material_confirm:confirmed:${token}`);
          fetchData();
        }
        return;
      }
      const res = await fetch(`/api/m/${token}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ customer_notes: notes }),
      });
      if (res.ok) {
        setShowSuccess(true);
        trackEvent('material_confirmed', `material_confirm:confirmed:${token}`);
        fetchData();
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleFeedback = async () => {
    if (!token || (!notes.trim() && !hasInlineEdits)) return;
    setSubmittingFeedback(true);
    try {
      if (hasInlineEdits) {
        const ok = await submitMaterialsPatch('feedback');
        if (ok) {
          setShowFeedbackSent(true);
          trackEvent('material_feedback', `material_confirm:feedback:${token}`);
          fetchData();
        }
        return;
      }
      const res = await fetch(`/api/m/${token}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback: notes }),
      });
      if (res.ok) {
        setShowFeedbackSent(true);
        trackEvent('material_feedback', `material_confirm:feedback:${token}`);
        fetchData();
      }
    } finally {
      setSubmittingFeedback(false);
    }
  };

  // Loading state
  if (loading) {
    return (
      <div className="min-h-screen pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-gradient-to-b from-slate-50 to-white flex items-center justify-center" style={{ colorScheme: 'light' }}>
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="text-center"
        >
          <div className="w-12 h-12 border-2 border-indigo-200 border-t-indigo-500 rounded-full animate-spin mx-auto mb-4" />
          <p className="text-sm text-gray-400">加载中...</p>
        </motion.div>
      </div>
    );
  }

  // Error state
  if (error || !data) {
    return (
      <div className="min-h-screen pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-gradient-to-b from-slate-50 to-white flex items-center justify-center px-6" style={{ colorScheme: 'light' }}>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center max-w-sm"
        >
          <div className="w-16 h-16 rounded-full bg-red-50 flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-gray-800 mb-2">{error || '页面不存在'}</h2>
          <p className="text-sm text-gray-400">如有疑问，请联系您的客户经理</p>
        </motion.div>
      </div>
    );
  }

  // Expired state
  if (data.status === 'expired') {
    return (
      <div className="min-h-screen pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-gradient-to-b from-slate-50 to-white flex items-center justify-center px-6" style={{ colorScheme: 'light' }}>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center max-w-sm"
        >
          <div className="w-16 h-16 rounded-full bg-amber-50 flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-gray-800 mb-2">链接已过期</h2>
          <p className="text-sm text-gray-400">请联系客户经理重新生成确认链接</p>
        </motion.div>
      </div>
    );
  }

  const m = draftMaterials || data.materials;
  const hasSellingPoints = m.selling_points?.items?.length > 0 || m.selling_points?.summary;
  const hasProducts = m.products?.features?.length > 0 || m.products?.scenarios?.length > 0;
  const hasCustomers = m.customers?.segments?.length > 0 || m.customers?.pain_scenario;
  const hasCases = m.cases?.length > 0;
  const hasTestimonials = m.testimonials?.length > 0;
  const contactItems = [
    { label: '联系电话', value: m.contact?.phone, path: 'contact.phone' },
    { label: '微信/企业微信', value: m.contact?.wechat, path: 'contact.wechat' },
    { label: '官网', value: m.contact?.website, path: 'contact.website' },
    { label: '地址', value: m.contact?.address, path: 'contact.address' },
  ].filter(item => item.value);
  const confirmedImages = (m.images || []).filter(img => img.url || img.thumbnail_url);

  return (
    <div className="material-confirm-page min-h-screen pt-[env(safe-area-inset-top)] bg-gradient-to-b from-slate-50 via-white to-slate-50 text-slate-900" style={{ colorScheme: 'light', paddingBottom: 'calc(260px + env(safe-area-inset-bottom, 0px))' }}>
      {/* ===== Hero Header ===== */}
      <div className="relative overflow-hidden">
        {/* Decorative background */}
        <div className="absolute inset-0 bg-gradient-to-b from-indigo-50/80 via-purple-50/40 to-transparent" />
        <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[800px] h-[400px] bg-gradient-to-b from-indigo-100/30 to-transparent rounded-full blur-3xl" />
        <div className="absolute -top-32 -right-32 w-64 h-64 bg-gradient-to-br from-purple-100/40 to-transparent rounded-full blur-3xl" />
        <div className="absolute -top-20 -left-20 w-48 h-48 bg-gradient-to-br from-cyan-100/30 to-transparent rounded-full blur-3xl" />

        <motion.div
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease: 'easeOut' }}
          className="relative text-center pt-12 pb-6 px-6 md:pt-16 md:pb-10"
        >
          {/* v3.6 白标 · 客户页代理品牌(external_only 不回退平台) */}
          <div className="flex justify-center mb-8 opacity-80">
            <BrandLogo brand={brand} size="lg" className="!h-10 md:!h-12" />
          </div>

          {data.status === 'confirmed' ? (
            <>
              <div className="w-16 h-16 rounded-full bg-emerald-100 flex items-center justify-center mx-auto mb-4">
                <svg className="w-8 h-8 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <h1 className="text-2xl md:text-3xl font-bold text-gray-900 mb-2">资料已确认</h1>
              <p className="text-sm text-gray-500">
                感谢您的确认，我们将基于以下资料为您创作优质推广内容
              </p>
            </>
          ) : data.status === 'feedback' ? (
            <>
              <div className="w-16 h-16 rounded-full bg-amber-100 flex items-center justify-center mx-auto mb-4">
                <svg className="w-8 h-8 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
                </svg>
              </div>
              <h1 className="text-2xl md:text-3xl font-bold text-gray-900 mb-2">修改意见已提交</h1>
              <p className="text-sm text-gray-500 max-w-md mx-auto">
                我们已收到您的意见，团队正在修改中。修改完成后会重新发送确认链接给您
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-indigo-500 font-medium mb-2 tracking-wide">营销资料确认</p>
              <h1 className="text-2xl md:text-3xl lg:text-4xl font-bold text-gray-900 mb-3 tracking-tight">
                「{data.brand_name}」
              </h1>
              <p className="text-sm md:text-base text-gray-500 max-w-md mx-auto leading-relaxed">
                请审阅以下推广资料，确认无误后我们将据此为您创作 AI 搜索优化内容
              </p>
            </>
          )}
        </motion.div>
      </div>

      {/* ===== Content Sections ===== */}
      <div className="material-confirm-content max-w-2xl mx-auto px-5 md:px-6 space-y-5" style={{ paddingBottom: 'calc(300px + env(safe-area-inset-bottom, 0px))' }}>

        {/* --- Section 1: Company Overview --- */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}>
          <InfoCard>
            <SectionHeader
              icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" /></svg>}
              title="企业概况"
              subtitle="Company Overview"
            />
            <div className="space-y-4">
              {m.company.intro && (
                <div className="flex items-start gap-3">
                  <p className="flex-1 text-sm text-gray-700 leading-relaxed whitespace-pre-line">{m.company.intro}</p>
                  <EditInlineButton onClick={() => openInlineEdit('company.intro', '企业概况')} />
                </div>
              )}
              {m.company.core_value && (
                <div className="mc-soft-panel bg-indigo-50 rounded-xl p-4 border border-indigo-100">
                  <div className="mb-1.5 flex items-center justify-between gap-3">
                    <p className="text-xs text-indigo-500 font-semibold">核心价值主张</p>
                    <EditInlineButton onClick={() => openInlineEdit('company.core_value', '核心价值主张')} />
                  </div>
                  <p className="text-sm font-medium text-gray-800 leading-relaxed">{m.company.core_value}</p>
                </div>
              )}
              <div className="grid grid-cols-2 gap-4">
                <DataPoint label="行业" value={m.company.industry} />
                <DataPoint label="服务区域" value={m.company.service_area} />
                <DataPoint label="目标客群" value={m.company.target_users} />
              </div>
            </div>
          </InfoCard>
        </motion.div>

        {/* --- Contact Confirmation --- */}
        {contactItems.length > 0 && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.12 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 002.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 01-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102A1.125 1.125 0 005.872 2.25H4.5A2.25 2.25 0 002.25 4.5v2.25z" /></svg>}
                title="联系方式确认"
                subtitle="Contact Details"
              />
              <p className="text-sm text-gray-500 mb-4">
                以下联系方式会作为推广内容的参考信息。若不希望展示某项，请在底部提交修改意见。
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {contactItems.map(item => (
                  <div key={item.label} className="mc-subcard rounded-xl border border-gray-100 bg-gray-50 px-4 py-3">
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <p className="text-xs text-gray-400">{item.label}</p>
                      <EditInlineButton onClick={() => openInlineEdit(item.path, item.label, false)} />
                    </div>
                    <p className="text-sm font-medium text-gray-800 break-words">{item.value}</p>
                  </div>
                ))}
              </div>
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 2: Core Selling Points --- */}
        {hasSellingPoints && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" /></svg>}
                title="核心卖点"
                subtitle="Key Selling Points"
              />
              {m.selling_points.usp && (
                <div className="mc-warm-panel bg-amber-50 rounded-xl p-4 border border-amber-100 mb-4">
                  <div className="mb-1 flex items-center justify-between gap-3">
                    <p className="text-xs text-amber-600 font-semibold">独特竞争优势</p>
                    <EditInlineButton onClick={() => openInlineEdit('selling_points.usp', '独特竞争优势')} />
                  </div>
                  <p className="text-sm font-medium text-gray-800">{m.selling_points.usp}</p>
                </div>
              )}
              {m.selling_points.items?.length > 0 && (
                <div className="space-y-3">
                  {m.selling_points.items.map((item, i) => (
                    <div key={i} className="flex gap-3">
                      <div className="w-7 h-7 rounded-lg bg-indigo-100 flex items-center justify-center shrink-0 mt-0.5">
                        <span className="text-xs font-bold text-indigo-600">{i + 1}</span>
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-start justify-between gap-3">
                          <p className="text-sm font-medium text-gray-800">{item.point}</p>
                          <EditInlineButton onClick={() => openInlineEdit(`selling_points.items.${i}.point`, `卖点 ${i + 1} 标题`, false)} />
                        </div>
                        {item.evidence && (
                          <div className="mt-1 flex items-start justify-between gap-3">
                            <p className="text-xs text-gray-400 leading-relaxed">{item.evidence}</p>
                            <EditInlineButton onClick={() => openInlineEdit(`selling_points.items.${i}.evidence`, `卖点 ${i + 1} 说明`)} />
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {!m.selling_points.items?.length && m.selling_points.summary && (
                <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-line">{m.selling_points.summary}</p>
              )}
              {m.selling_points.advantages?.length > 0 && (
                <div className="mt-4">
                  <p className="text-xs text-gray-400 mb-2">差异化优势</p>
                  <TagList items={m.selling_points.advantages} color="indigo" />
                </div>
              )}
              {m.selling_points.killer_data?.length > 0 && (
                <div className="mt-4">
                  <p className="text-xs text-gray-400 mb-2">关键数据</p>
                  <div className="space-y-1.5">
                    {m.selling_points.killer_data.map((d, i) => (
                      <div key={i} className="flex items-start gap-2">
                        <svg className="w-3.5 h-3.5 text-emerald-500 mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                        </svg>
                        <span className="text-xs text-gray-600">{d}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 3: Products & Scenarios --- */}
        {hasProducts && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" /></svg>}
                title="产品与服务"
                subtitle="Products & Services"
              />
              {m.products.name && <DataPoint label="产品/服务名称" value={m.products.name} />}
              {m.products.features?.length > 0 && (
                <div className="mt-3">
                  <p className="text-xs text-gray-400 mb-2">核心功能特性</p>
                  <TagList items={m.products.features} color="indigo" />
                </div>
              )}
              {m.products.scenarios?.length > 0 && (
                <div className="mt-4">
                  <p className="text-xs text-gray-400 mb-2">应用场景</p>
                  <TagList items={m.products.scenarios} color="emerald" />
                </div>
              )}
              {m.products.metrics && (
                <div className="mt-4">
                  <DataPoint label="效果指标" value={m.products.metrics} />
                </div>
              )}
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 4: Target Customers --- */}
        {hasCustomers && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" /></svg>}
                title="目标客户画像"
                subtitle="Customer Insights"
              />
              {m.customers.segments?.length > 0 && (
                <div className="mb-4">
                  <p className="text-xs text-gray-400 mb-2">客户细分</p>
                  <TagList items={m.customers.segments} color="amber" />
                </div>
              )}
              {m.customers.pain_scenario && (
                <div className="mc-danger-panel bg-red-50 rounded-xl p-4 border border-red-100 mb-4">
                  <p className="text-xs text-red-500 font-semibold mb-1.5">痛点场景</p>
                  <p className="text-sm text-gray-700 leading-relaxed">{m.customers.pain_scenario}</p>
                  {m.customers.pain_triggers && (
                    <p className="text-xs text-gray-500 mt-2">
                      <span className="font-medium text-red-400">购买触发：</span>{m.customers.pain_triggers}
                    </p>
                  )}
                </div>
              )}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {m.customers.needs?.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-400 mb-2">核心需求</p>
                    <ul className="space-y-1.5">
                      {m.customers.needs.map((n, i) => (
                        <li key={i} className="text-xs text-gray-600 flex items-start gap-1.5">
                          <span className="text-indigo-400 mt-0.5">&#8226;</span>{n}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {m.customers.concerns?.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-400 mb-2">关注问题</p>
                    <ul className="space-y-1.5">
                      {m.customers.concerns.map((c, i) => (
                        <li key={i} className="text-xs text-gray-600 flex items-start gap-1.5">
                          <span className="text-amber-400 mt-0.5">&#8226;</span>{c}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 5: Success Cases --- */}
        {hasCases && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.3 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M16.5 18.75h-9m9 0a3 3 0 013 3h-15a3 3 0 013-3m9 0v-3.375c0-.621-.503-1.125-1.125-1.125h-.871M7.5 18.75v-3.375c0-.621.504-1.125 1.125-1.125h.872m5.007 0H9.497m5.007 0a7.454 7.454 0 01-.982-3.172M9.497 14.25a7.454 7.454 0 00.981-3.172M5.25 4.236c-.982.143-1.954.317-2.916.52A6.003 6.003 0 007.73 9.728M5.25 4.236V4.5c0 2.108.966 3.99 2.48 5.228M5.25 4.236V2.721C7.456 2.41 9.71 2.25 12 2.25c2.291 0 4.545.16 6.75.47v1.516M18.75 4.236c.982.143 1.954.317 2.916.52A6.003 6.003 0 0016.27 9.728M18.75 4.236V4.5c0 2.108-.966 3.99-2.48 5.228m0 0a6.003 6.003 0 01-5.54 0" /></svg>}
                title="成功案例"
                subtitle="Case Studies"
              />
              <div className="space-y-4">
                {m.cases.map((c, i) => (
                  <div key={i} className="mc-subcard bg-gray-50 rounded-xl p-4 border border-gray-100">
                    {c.client && (
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <p className="text-sm font-semibold text-gray-800">{c.client}</p>
                        <EditInlineButton onClick={() => openInlineEdit(`cases.${i}.client`, `案例 ${i + 1} 客户类型`, false)} />
                      </div>
                    )}
                    {c.background && (
                      <div className="mt-2">
                        <div className="mb-1 flex items-center justify-between gap-3">
                          <p className="text-xs text-gray-400">背景</p>
                          <EditInlineButton onClick={() => openInlineEdit(`cases.${i}.background`, `案例 ${i + 1} 背景`)} />
                        </div>
                        <p className="text-sm text-gray-800 leading-relaxed">{c.background}</p>
                      </div>
                    )}
                    {c.solution && (
                      <div className="mt-2">
                        <div className="mb-1 flex items-center justify-between gap-3">
                          <p className="text-xs text-gray-400">方案</p>
                          <EditInlineButton onClick={() => openInlineEdit(`cases.${i}.solution`, `案例 ${i + 1} 方案`)} />
                        </div>
                        <p className="text-sm text-gray-800 leading-relaxed">{c.solution}</p>
                      </div>
                    )}
                    {c.results && (
                      <div className="mc-success-panel mt-2 bg-emerald-50 rounded-lg p-3 border border-emerald-100">
                        <div className="mb-1 flex items-center justify-between gap-3">
                          <p className="text-xs text-emerald-600 font-semibold">成果</p>
                          <EditInlineButton onClick={() => openInlineEdit(`cases.${i}.results`, `案例 ${i + 1} 成果`)} />
                        </div>
                        <p className="text-sm text-gray-700">{c.results}</p>
                      </div>
                    )}
                    {c.quote && (
                      <div className="mt-2 flex items-start justify-between gap-3 pl-3 border-l-2 border-gray-200">
                        <p className="text-xs text-gray-500 italic">"{c.quote}"</p>
                        <EditInlineButton onClick={() => openInlineEdit(`cases.${i}.quote`, `案例 ${i + 1} 引用`)} />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </InfoCard>
          </motion.div>
        )}

        {/* --- Image Material Confirmation --- */}
        {confirmedImages.length > 0 && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.33 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909m-18 3.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v12a1.5 1.5 0 001.5 1.5zm10.5-11.25h.008v.008h-.008V8.25z" /></svg>}
                title="图片素材确认"
                subtitle="Image Materials"
              />
              <p className="text-sm text-gray-500 mb-4">
                以下图片会作为推广素材候选，用于文章配图、案例展示或品牌内容参考。若有不适合使用的图片，请在底部提交修改意见。
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {confirmedImages.map((image, i) => {
                  const src = image.thumbnail_url || image.url || '';
                  const imageKey = String(image.id ?? image.url ?? image.thumbnail_url ?? i);
                  const isBroken = brokenImageKeys[imageKey];
                  return (
                    <div key={image.id ?? i} className="mc-subcard rounded-xl border border-gray-100 bg-gray-50 overflow-hidden">
                      <div className="mc-image-frame aspect-[4/3] bg-gray-100">
                        {src && !isBroken ? (
                          <img
                            src={src}
                            alt={image.title || image.caption || `图片素材 ${i + 1}`}
                            loading="lazy"
                            onError={() => setBrokenImageKeys(prev => ({ ...prev, [imageKey]: true }))}
                          />
                        ) : (
                          <div className="mc-image-fallback">
                            <svg className="w-6 h-6 mb-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M3.75 19.5h16.5A1.5 1.5 0 0021.75 18V6A1.5 1.5 0 0020.25 4.5H3.75A1.5 1.5 0 002.25 6v12a1.5 1.5 0 001.5 1.5z" />
                            </svg>
                            <span>图片暂不可预览</span>
                          </div>
                        )}
                      </div>
                      <div className="px-3 py-2">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="text-[11px] text-gray-400">图片名称</span>
                          <EditInlineButton onClick={() => openInlineEdit(`images.${i}.title`, `图片 ${i + 1} 名称`, false)} />
                        </div>
                        <p className="text-xs font-medium text-gray-700 truncate">
                          {image.title || image.caption || `图片素材 ${i + 1}`}
                        </p>
                        {image.image_type && (
                          <p className="text-[11px] text-gray-400 mt-0.5 truncate">{image.image_type}</p>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 6: Testimonials --- */}
        {hasTestimonials && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" /></svg>}
                title="客户评价"
                subtitle="Testimonials"
              />
              <div className="space-y-3">
                {m.testimonials.map((t, i) => (
                  <div key={i} className="mc-testimonial bg-indigo-50 rounded-xl p-4 border border-indigo-100">
                    {t.quote && (
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-sm text-gray-700 italic leading-relaxed">"{t.quote}"</p>
                        <EditInlineButton onClick={() => openInlineEdit(`testimonials.${i}.quote`, `客户评价 ${i + 1}`)} />
                      </div>
                    )}
                    <div className="flex items-center gap-1.5 mt-2">
                      <div className="w-6 h-6 rounded-full bg-indigo-200 flex items-center justify-center">
                        <span className="text-[10px] font-bold text-indigo-700">
                          {(t.name || '匿')[0]}
                        </span>
                      </div>
                      <span className="text-xs text-gray-500">
                        {t.name}{t.title ? ` · ${t.title}` : ''}{t.company ? ` · ${t.company}` : ''}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </InfoCard>
          </motion.div>
        )}

        {/* --- Section 7: Methodology --- */}
        {m.methodology && (
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.4 }}>
            <InfoCard>
              <SectionHeader
                icon={<svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" /></svg>}
                title="服务方法论"
                subtitle="Methodology"
              />
              <div className="flex items-start justify-between gap-3">
                <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-line">{m.methodology}</p>
                <EditInlineButton onClick={() => openInlineEdit('methodology', '服务方法论')} />
              </div>
            </InfoCard>
          </motion.div>
        )}
      </div>

      {/* ===== Bottom Action Bar ===== */}
      <AnimatePresence>
        {data.status === 'pending' && (
          <motion.div
            initial={{ y: 100, opacity: 0 }}
            // [WO_IOS_TOUCH_UX 2026-08-05] 🔴 这条底部固定条**里面就有一个备注 textarea**。
            //   iOS 弹软键盘时不改 layout viewport → 整条被键盘盖住,
            //   客户点进备注框打字时**看不见自己正在输入的框**,体感就是"卡住了"。
            //   用 visualViewport 实测的键盘高度把整条顶到键盘上方(不能改 static ——
            //   那会让正在打字的框跑到文档末尾,要滚动才看得见)。
            //   🔴 位移必须走 framer 的 `y`,**不能**写在 style.transform 里:
            //      motion.div 的 animate 会生成自己的 inline transform 把 style 里那份覆盖掉,
            //      写在 style 里等于没修。
            animate={{ y: keyboardInset > 0 ? -keyboardInset : 0, opacity: 1 }}
            exit={{ y: 100, opacity: 0 }}
            transition={{ delay: 0.5, type: 'spring', damping: 25 }}
            className="mc-bottom-bar fixed bottom-0 left-0 right-0 bg-white border-t border-gray-100 shadow-2xl shadow-black/10 z-[60]"
            style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}
          >
            <div className="max-w-2xl mx-auto px-5 py-4">
              {hasInlineEdits && (
                <div className="mb-3 rounded-xl border border-indigo-100 bg-indigo-50 px-4 py-3 text-xs text-indigo-700">
                  已修改 {materialEdits.length} 处。保存本页修改后，会同步更新客户档案和后续写作资料。
                </div>
              )}
              {/* Notes input */}
              <div className="mb-3">
                <textarea
                  value={notes}
                  onChange={e => setNotes(e.target.value)}
                  placeholder={hasInlineEdits ? '可补充修改原因或其他说明...' : '如需修改，请在此写明修改意见...'}
                  rows={2}
                  className="mc-feedback-input w-full text-sm text-gray-700 placeholder-gray-300 bg-gray-50 border border-gray-200 rounded-xl px-4 py-3 resize-none focus:outline-hidden focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300 transition-all"
                />
              </div>

              {/* Two action buttons */}
              <div className="flex gap-3">
                {/* Feedback button */}
                <button
                  onClick={handleFeedback}
                  disabled={submittingFeedback || (!notes.trim() && !hasInlineEdits)}
                  className="flex-1 py-3.5 rounded-2xl border-2 border-amber-300 bg-amber-50 text-amber-700 font-semibold text-sm hover:bg-amber-100 active:scale-[0.98] transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {submittingFeedback ? (
                    <>
                      <div className="w-4 h-4 border-2 border-amber-300 border-t-amber-600 rounded-full animate-spin" />
                      提交中...
                    </>
                  ) : (
                    <>
                      <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
                      </svg>
                      {hasInlineEdits ? '保存本页修改' : '提交修改意见'}
                    </>
                  )}
                </button>

                {/* Confirm button */}
                <button
                  onClick={handleConfirm}
                  disabled={submitting}
                  className="flex-1 py-3.5 rounded-2xl bg-gradient-to-r from-indigo-500 to-purple-500 text-white font-semibold text-sm shadow-lg shadow-indigo-200/50 hover:shadow-xl hover:shadow-indigo-300/50 active:scale-[0.98] transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {submitting ? (
                    <>
                      <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                      提交中...
                    </>
                  ) : (
                    <>
                      <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      {hasInlineEdits ? '保存修改并确认' : '确认无误'}
                    </>
                  )}
                </button>
              </div>
              <p className="text-center text-[11px] text-gray-300 mt-2">
                确认后将基于以上资料创作推广内容 · 页面修改会同步更新客户档案
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ===== Inline Edit Dialog ===== */}
      <AnimatePresence>
        {editingField && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 px-5 backdrop-blur-xs"
            onClick={() => setEditingField(null)}
          >
            <motion.div
              initial={{ scale: 0.92, opacity: 0, y: 20 }}
              animate={{ scale: 1, opacity: 1, y: 0 }}
              exit={{ scale: 0.92, opacity: 0, y: 20 }}
              transition={{ type: 'spring', damping: 22 }}
              onClick={e => e.stopPropagation()}
              className="w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl"
            >
              <h3 className="text-lg font-bold text-gray-900">修改此处</h3>
              <p className="mt-1 text-sm text-gray-400">{editingField.label}</p>
              {editingField.multiline === false ? (
                <input
                  value={editingField.value}
                  onChange={e => setEditingField(prev => (prev ? { ...prev, value: e.target.value } : prev))}
                  className="mt-4 w-full rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-800 outline-hidden focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                />
              ) : (
                <textarea
                  value={editingField.value}
                  onChange={e => setEditingField(prev => (prev ? { ...prev, value: e.target.value } : prev))}
                  rows={6}
                  className="mt-4 w-full resize-none rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-800 outline-hidden focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                />
              )}
              <p className="mt-3 text-xs text-gray-400">
                保存后先更新本页预览；点击底部按钮后才会同步更新客户档案。
              </p>
              <div className="mt-5 flex gap-3">
                <button
                  type="button"
                  onClick={() => setEditingField(null)}
                  className="flex-1 rounded-2xl border border-gray-200 bg-white py-3 text-sm font-semibold text-gray-500"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={saveInlineEdit}
                  className="flex-1 rounded-2xl bg-indigo-600 py-3 text-sm font-semibold text-white shadow-lg shadow-indigo-200/60"
                >
                  保存本页修改
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ===== Feedback Sent Overlay ===== */}
      <AnimatePresence>
        {showFeedbackSent && data.status === 'feedback' && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/20 backdrop-blur-xs z-60 flex items-center justify-center px-6"
            onClick={() => setShowFeedbackSent(false)}
          >
            <motion.div
              initial={{ scale: 0.8, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.8, opacity: 0 }}
              transition={{ type: 'spring', damping: 20 }}
              onClick={e => e.stopPropagation()}
              className="bg-white rounded-3xl shadow-2xl max-w-sm w-full p-8 text-center"
            >
              <div className="w-20 h-20 rounded-full bg-gradient-to-br from-amber-400 to-orange-500 flex items-center justify-center mx-auto mb-5 shadow-lg shadow-amber-200/50">
                <svg className="w-10 h-10 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.087.16 2.185.283 3.293.369V21l4.076-4.076a1.526 1.526 0 011.037-.443 48.282 48.282 0 005.68-.494c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
                </svg>
              </div>
              <h3 className="text-xl font-bold text-gray-900 mb-2">意见已提交！</h3>
              <p className="text-sm text-gray-500 mb-6 leading-relaxed">
                感谢您的反馈。我们的团队会尽快修改资料，修改完成后会重新发送确认链接给您。
              </p>
              <button
                onClick={() => setShowFeedbackSent(false)}
                className="px-8 py-2.5 rounded-xl bg-gray-100 text-sm font-medium text-gray-600 hover:bg-gray-200 transition-colors"
              >
                知道了
              </button>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ===== Success Overlay ===== */}
      <AnimatePresence>
        {showSuccess && data.status === 'confirmed' && !loading && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/20 backdrop-blur-xs z-60 flex items-center justify-center px-6"
            onClick={() => setShowSuccess(false)}
          >
            <motion.div
              initial={{ scale: 0.8, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.8, opacity: 0 }}
              transition={{ type: 'spring', damping: 20 }}
              onClick={e => e.stopPropagation()}
              className="bg-white rounded-3xl shadow-2xl max-w-sm w-full p-8 text-center"
            >
              <div className="w-20 h-20 rounded-full bg-gradient-to-br from-emerald-400 to-teal-500 flex items-center justify-center mx-auto mb-5 shadow-lg shadow-emerald-200/50">
                <svg className="w-10 h-10 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
              </div>
              <h3 className="text-xl font-bold text-gray-900 mb-2">确认成功！</h3>
              <p className="text-sm text-gray-500 mb-6 leading-relaxed">
                感谢您的确认。我们的内容团队将立即开始为您创作高质量的 AI 搜索优化内容。
              </p>
              <button
                onClick={() => setShowSuccess(false)}
                className="px-8 py-2.5 rounded-xl bg-gray-100 text-sm font-medium text-gray-600 hover:bg-gray-200 transition-colors"
              >
                查看资料详情
              </button>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ===== Footer · v3.6 白标 · 客户页显示代理品牌(external_only 不回退平台) ===== */}
      <div className="text-center py-8 px-6 opacity-50">
        <BrandFooter brand={brand} />
      </div>
      {/* WO_329 开源版署名位:放在上面 opacity-50 容器之外;开关关 ⇒ 不渲染 */}
      <OssAttribution className="pb-8" />
    </div>
  );
}
