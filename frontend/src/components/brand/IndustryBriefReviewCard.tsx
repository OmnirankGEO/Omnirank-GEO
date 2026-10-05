/**
 * IndustryBriefReviewCard — v3.7 知识库审核工作流（CTO-15.0 2026-04-19）
 *
 * 4 phase 一波交付，全部放在此组件：
 *   Phase 1 · 5 组分类展开 + 确认入库（绿色徽章）
 *   Phase 2 · 行内编辑（免费 · 铅笔图标 + Dialog）
 *   Phase 3 · 字段重跑（🔄 按钮 + 重跑 Dialog · 静默扣费 · 页面不前置告知额度）
 *   Phase 4 · 版本历史（列表 / 对比 / 回滚）
 *
 * 父组件（BrandDetailPage）只需传 profileId 和 reloadProfile 回调，
 * 所有状态 + API 调用都在组件内部处理。
 */

import { useState, useEffect, useCallback } from 'react';
import {
  Sparkles, Check, Edit3, RefreshCw, History, Save, X, ChevronDown, ChevronRight,
  AlertTriangle, Users, Target, Lightbulb, MapPin, Flame, Database, Globe,
} from 'lucide-react';
import { toast } from 'sonner';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// ============ 字段分组（5 组 · Phase 1 UI 核心） ============

interface FieldDef {
  key: string;
  label: string;
  valueType: 'string' | 'list';
  placeholder?: string;
}

interface GroupDef {
  id: string;
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;  // tailwind 色相前缀（如 'emerald', 'amber'）
  fields: FieldDef[];
}

const GROUPS: GroupDef[] = [
  {
    id: 'audience',
    title: '目标人群',
    icon: Users,
    color: 'sky',
    fields: [
      { key: 'my_audience', label: '目标客群', valueType: 'string' },
      { key: 'target_users', label: '目标用户画像', valueType: 'string' },
    ],
  },
  {
    id: 'competition',
    title: '竞争格局',
    icon: Target,
    color: 'rose',
    fields: [
      { key: 'my_differentiation', label: '差异化定位', valueType: 'string' },
      { key: 'differentiation_hints', label: '差异化素材', valueType: 'list' },
      { key: 'local_competitors', label: '本地竞品', valueType: 'list' },
    ],
  },
  {
    id: 'content',
    title: '内容策略',
    icon: Lightbulb,
    color: 'amber',
    fields: [
      { key: 'content_strategy', label: '内容策略建议', valueType: 'string' },
      { key: 'top_content_formats', label: '爆款内容形态', valueType: 'list' },
      { key: 'content_pain_points', label: '内容痛点', valueType: 'list' },
    ],
  },
  {
    id: 'local',
    title: '本地洞察',
    icon: MapPin,
    color: 'violet',
    fields: [
      { key: 'city_context', label: '本地市场上下文', valueType: 'string' },
      { key: 'local_platform_tips', label: '本地平台玩法', valueType: 'string' },
      { key: 'regional_kw_ideas', label: '地域关键词点子', valueType: 'list' },
    ],
  },
  {
    id: 'acquisition',
    title: '获客爆款',
    icon: Flame,
    color: 'emerald',
    fields: [
      { key: 'acquisition_paths', label: '获客路径', valueType: 'list' },
      { key: 'top_cases', label: 'Top 案例', valueType: 'list' },
    ],
  },
];

const ALL_FIELD_KEYS = GROUPS.flatMap(g => g.fields.map(f => f.key));

// ============ 类型 ============

interface Props {
  profileId: number | string;
  brief: Record<string, any> | null;
  confirmed: boolean;
  partialFields: string[] | null;  // null = 全量入库 / array = 部分入库
  version: number;
  onRefresh: () => Promise<void> | void;  // 父组件重拉 profile
  // [CTO-13.0 2026-04-19 S3.3] C 端用户 (mode='c-end') 应设 true
  // 效果：隐藏 PoolFieldBlock 编辑/删除按钮 + 不触发矫正反哺 Dialog
  // 原因：矫正反哺对全行业生效，C 端用户不懂影响面，防误操作和恶意污染
  // 默认 false 不影响代理端现有行为
  readonly?: boolean;
}

interface HistoryVersion {
  id: number;
  version: number;
  confirmed: boolean;
  partial_fields: string[] | null;
  source: string;
  created_at: string;
}

// ============ 工具 ============

function formatValue(v: any): string {
  if (v == null) return '';
  if (Array.isArray(v)) return v.map(String).join(' · ');
  if (typeof v === 'object') return JSON.stringify(v, null, 2);
  return String(v);
}

function parseListInput(text: string): string[] {
  return text.split(/\n+|、|；|;/).map(s => s.trim()).filter(Boolean);
}

// ============ v3.6 L1/L2 池单字段块组件 ============

interface PoolFieldBlockProps {
  label: string;
  items: any[];
  previewLimit: number;
  level: 'industry' | 'category';
  field_name: string;
  renderItem: (item: any) => React.ReactNode;
  onEdit: (params: any) => void;
  // [CTO-13.0 2026-04-19 S3.3] true 时隐藏 ✏️/🗑 按钮（C 端只读）
  readonly?: boolean;
}

function PoolFieldBlock({ label, items, previewLimit, level, field_name, renderItem, onEdit, readonly }: PoolFieldBlockProps) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? items : items.slice(0, previewLimit);
  return (
    <div className="mb-2">
      <div className="text-muted-foreground mb-0.5 flex items-center gap-2">
        <span>{label} · {items.length} 条</span>
        {items.length > previewLimit && (
          <button
            onClick={() => setShowAll(v => !v)}
            className="text-[10px] text-sky-400 hover:text-sky-300"
          >
            {showAll ? '折叠' : `展开全部 (${items.length})`}
          </button>
        )}
      </div>
      <div className="space-y-0.5">
        {visible.map((item: any, i: number) => (
          <div key={i} className="text-foreground/80 group flex items-start gap-1">
            <span className="flex-1">· {renderItem(item)}</span>
            {/* S3.3: readonly 时不渲染矫正按钮 */}
            {!readonly && (
              <span className="opacity-0 group-hover:opacity-100 transition-opacity flex gap-1 shrink-0">
                <button
                  onClick={() => onEdit({ level, field_name, item_index: i, label, original: item, editType: 'update' })}
                  className="text-sky-400 hover:text-sky-300 px-1 text-[10px]"
                  title="修改素材"
                >
                  ✏️
                </button>
                <button
                  onClick={() => onEdit({ level, field_name, item_index: i, label, original: item, editType: 'delete' })}
                  className="text-red-400 hover:text-red-300 px-1 text-[10px]"
                  title="移除素材"
                >
                  🗑
                </button>
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ============ 主组件 ============

export function IndustryBriefReviewCard({ profileId, brief, confirmed, partialFields, version, onRefresh, readonly = false }: Props) {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  // 每组默认展开/折叠
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    GROUPS.forEach(g => { init[g.id] = true; });
    return init;
  });

  // 部分入库勾选态（仅未 confirmed 时交互；已 confirmed 后展示当前 partialFields）
  const [selectedFields, setSelectedFields] = useState<Set<string>>(() => {
    if (partialFields && Array.isArray(partialFields)) return new Set(partialFields);
    return new Set(ALL_FIELD_KEYS);  // 默认全选
  });

  useEffect(() => {
    if (partialFields && Array.isArray(partialFields)) {
      setSelectedFields(new Set(partialFields));
    } else if (confirmed) {
      setSelectedFields(new Set(ALL_FIELD_KEYS));
    }
  }, [partialFields, confirmed]);

  const [submitting, setSubmitting] = useState(false);

  // ----- [CTO-13.0 2026-04-19 S1.4] brief → profile 一键应用 -----
  const [applyingBrief, setApplyingBrief] = useState(false);
  const handleApplyBrief = async () => {
    if (!confirmed) {
      toast.error('请先「确认入库」再应用到品牌信息');
      return;
    }
    if (applyingBrief) return;
    setApplyingBrief(true);
    try {
      const res = await authApi.patch(`/api/profiles/${profileId}/apply-brief`, {
        fields: null,  // null = 全量应用全部 7 个映射字段
        conflict_strategy: 'replace',
      });
      const d = res.data || {};
      if (d.success) {
        const cnt = d.applied_count ?? (d.applied_fields?.length ?? 0);
        const skCnt = d.skipped_fields?.length ?? 0;
        toast.success(
          skCnt > 0
            ? `已应用 ${cnt} 个字段到品牌信息（跳过 ${skCnt} 个 brief 无值）`
            : `已应用 ${cnt} 个字段到品牌信息，请切到「基本信息」Tab 检查`
        );
        // [S1.4] 通知主页面应用了哪些字段 → BrandDetailPage 监听此事件设 aiFilledFields
        // 让 apply-brief 也触发 S1.3 的琥珀高亮，用户一眼看到 brief 填了什么
        try {
          window.dispatchEvent(new CustomEvent('brief-applied', {
            detail: { fields: d.applied_fields || [] },
          }));
        } catch { /* ignore */ }
        await onRefresh();
      } else {
        toast.error(d.detail || d.error || '应用失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || e?.message || '应用失败');
    } finally {
      setApplyingBrief(false);
    }
  };

  // ----- Phase 1: 确认入库 -----
  const handleConfirm = async () => {
    if (!brief) { toast.error('尚无可入库的知识库'); return; }
    const chosen = Array.from(selectedFields);
    const isFullLike = chosen.length === ALL_FIELD_KEYS.length;
    setSubmitting(true);
    try {
      const res = await authApi.post('/api/content/industry-brief/confirm', {
        profile_id: profileId,
        partial_fields: isFullLike ? null : chosen,
      });
      if (res.data?.success) {
        toast.success(isFullLike ? '已全量入库，AI 写文章会自动融合' : `已入库 ${chosen.length} 项`);
        await onRefresh();
      } else {
        toast.error(res.data?.detail || '入库失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '入库失败');
    } finally {
      setSubmitting(false);
    }
  };

  // ----- Phase 2: 字段编辑 -----
  const [editingField, setEditingField] = useState<FieldDef | null>(null);
  const [editValue, setEditValue] = useState('');

  const openEdit = (f: FieldDef) => {
    setEditingField(f);
    const raw = brief?.[f.key];
    setEditValue(f.valueType === 'list' && Array.isArray(raw) ? raw.join('\n') : formatValue(raw));
  };

  const handleSaveEdit = async () => {
    if (!editingField) return;
    const newValue = editingField.valueType === 'list' ? parseListInput(editValue) : editValue.trim();
    setSubmitting(true);
    try {
      const res = await authApi.patch('/api/content/industry-brief/edit', {
        profile_id: profileId,
        field_name: editingField.key,
        new_value: newValue,
      });
      if (res.data?.success) {
        toast.success('字段已更新，请重新点击「确认入库」生效');
        setEditingField(null);
        await onRefresh();
      } else {
        toast.error(res.data?.detail || '保存失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '保存失败');
    } finally {
      setSubmitting(false);
    }
  };

  // ----- Phase 3: 字段重跑（静默扣费 · 页面不前置告知额度） -----
  const [rerunFields, setRerunFields] = useState<string[] | null>(null);  // null = Dialog 关闭

  const openRerun = (fields: string[]) => setRerunFields(fields);

  const handleRerun = async () => {
    if (!rerunFields || rerunFields.length === 0) return;
    setSubmitting(true);
    try {
      const res = await authApi.post('/api/content/industry-brief/rerun', {
        profile_id: profileId,
        fields: rerunFields,
      });
      if (res.data?.success) {
        const updated = res.data.updated_fields?.length || rerunFields.length;
        const empty: string[] = res.data.empty_fields || [];
        toast.success(`已重跑 ${updated} 个字段，请审核后确认入库`);
        // [CTO-13.3 2026-04-19 P0 R9] AI 对部分字段返空 → 提示用户 (不退费因至少 1 个字段更新成功)
        if (empty.length) {
          toast.warning(
            `以下字段 AI 未生成新内容，保留旧值：${empty.join('、')}`,
            { duration: 6000 },
          );
        }
        setRerunFields(null);
        await onRefresh();
      } else {
        toast.error(res.data?.detail || '重跑失败');
      }
    } catch (e: any) {
      const msg = e?.response?.data?.detail_contract ?? e?.response?.data?.detail;
      const status = e?.response?.status;
      if (status === 402) {
        toast.error(`算力不足：${msg || '请先补充算力'}`);
      } else if (status === 422) {
        // [CTO-13.3 2026-04-19 P0 R9] 所有请求字段全返空 → 后端已自动还回 · detail 含业务解释
        toast.warning(msg || 'AI 未能生成新内容，已自动还回', { duration: 8000 });
        setRerunFields(null);
      } else {
        toast.error(msg || '重跑失败');
      }
    } finally {
      setSubmitting(false);
    }
  };

  // ----- Phase 4: 历史版本 -----
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyList, setHistoryList] = useState<HistoryVersion[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [compareVersion, setCompareVersion] = useState<number | null>(null);
  const [compareData, setCompareData] = useState<any>(null);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const res = await authApi.get(`/api/content/industry-brief/history/${profileId}`);
      if (res.data?.success) {
        setHistoryList(res.data.versions || []);
      }
    } catch {
      toast.error('加载历史失败');
    } finally {
      setHistoryLoading(false);
    }
  }, [profileId]);

  useEffect(() => {
    if (historyOpen) loadHistory();
  }, [historyOpen, loadHistory]);

  const loadCompare = async (targetVersion: number) => {
    setCompareVersion(targetVersion);
    try {
      const res = await authApi.get(`/api/content/industry-brief/history/${profileId}/${targetVersion}`);
      if (res.data?.success) setCompareData(res.data.brief_data);
    } catch {
      toast.error('加载版本详情失败');
    }
  };

  const handleRollback = async (targetVersion: number) => {
    if (!(await askConfirm({ title: `回滚到 v${targetVersion}？回滚后需再次点击"确认入库"才会对生产链路生效。`, danger: true }))) return;
    setSubmitting(true);
    try {
      const res = await authApi.post('/api/content/industry-brief/rollback', {
        profile_id: profileId,
        target_version: targetVersion,
      });
      if (res.data?.success) {
        toast.success(`已回滚到 v${targetVersion}`);
        setHistoryOpen(false);
        setCompareVersion(null);
        setCompareData(null);
        await onRefresh();
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '回滚失败');
    } finally {
      setSubmitting(false);
    }
  };

  // ----- v3.6 L1/L2 公共素材池(读 + 矫正反哺) -----
  const [poolData, setPoolData] = useState<any>(null);
  const [poolExpanded, setPoolExpanded] = useState(false);
  const [poolReloadKey, setPoolReloadKey] = useState(0);

  useEffect(() => {
    if (!profileId) return;
    authApi.get(`/api/content/industry-brief/pool/${profileId}`)
      .then(res => { if (res.data?.success) setPoolData(res.data); })
      .catch(() => { /* 静默,池数据是增强非必需 */ });
  }, [profileId, poolReloadKey]);

  // L1/L2 矫正 Dialog state
  const [editingPool, setEditingPool] = useState<{
    level: 'industry' | 'category';
    field_name: string;
    item_index: number;
    label: string;        // 给用户看的字段名(如"行业术语")
    original: any;        // 原值(JSON 对象)
    editType: 'update' | 'delete';
  } | null>(null);
  const [poolEditValue, setPoolEditValue] = useState('');
  const [poolEditReason, setPoolEditReason] = useState('');

  // [CTO-13.3 2026-04-19 v3.8 C4] 质量门控拒绝 Dialog · 给用户重写机会
  const [gateRejection, setGateRejection] = useState<{
    message: string;
    reasons: string[];
    field: string;
    rewriteValue: string;
  } | null>(null);

  const openPoolEdit = (params: typeof editingPool & object) => {
    setEditingPool(params);
    if (params.editType === 'update') {
      setPoolEditValue(typeof params.original === 'string' ? params.original : JSON.stringify(params.original, null, 2));
    } else {
      setPoolEditValue('');
    }
    setPoolEditReason('');
  };

  const submitCorrection = async (valueStr: string, reason?: string) => {
    if (!editingPool) return;
    let new_value: any = null;
    if (editingPool.editType === 'update') {
      try {
        new_value = JSON.parse(valueStr);
      } catch {
        new_value = valueStr;
      }
    }
    setSubmitting(true);
    try {
      const res = await authApi.patch('/api/content/industry-brief/correct', {
        profile_id: profileId,
        level: editingPool.level,
        field_name: editingPool.field_name,
        item_index: editingPool.item_index,
        new_value,
        action: editingPool.editType,
        reason: reason || undefined,
      });
      if (res.data?.success) {
        toast.success(`素材已${editingPool.editType === 'delete' ? '移除' : '更新'} · 下次解析自动应用`);
        setEditingPool(null);
        setGateRejection(null);
        setPoolReloadKey(k => k + 1);
      } else {
        toast.error(res.data?.detail || '矫正失败');
      }
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail_contract ?? e?.response?.data?.detail;
      // [CTO-13.3 2026-04-19 v3.8 C4] 422 质量门控拒绝 · 弹 Dialog 给用户重写机会
      if (status === 422 && detail && typeof detail === 'object' && detail.code === 'QUALITY_GATE_REJECTED') {
        setGateRejection({
          message: detail.message || 'AI 觉得这条修改：相关性低 / 跟现有冲突 / 内容空泛 — 是否要重写后再提交？',
          reasons: Array.isArray(detail.reasons_summary) ? detail.reasons_summary : [],
          field: detail.field || editingPool.field_name,
          rewriteValue: valueStr,
        });
      } else {
        toast.error(typeof detail === 'string' ? detail : '矫正失败');
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handlePoolSave = async () => {
    if (!editingPool) return;
    await submitCorrection(poolEditValue, poolEditReason);
  };

  const handleRewriteSubmit = async () => {
    if (!gateRejection) return;
    // 重写后再提交一次 · 再次走门控（给 AI 审多一轮）
    await submitCorrection(gateRejection.rewriteValue, poolEditReason || '重写后提交');
  };

  // ----- 无 brief 兜底 -----
  if (!brief || typeof brief !== 'object') {
    return null;
  }

  const hasAny = GROUPS.some(g => g.fields.some(f => brief[f.key]));
  if (!hasAny) return null;

  // ============ 渲染 ============

  return (
    <div className="mb-5 rounded-xl border border-amber-500/20 bg-amber-500/[0.03] p-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-2 flex-wrap">
          <Sparkles className="h-4 w-4 text-amber-400" />
          <h3 className="text-sm font-medium text-amber-300">AI 解析报告（待审核入库）</h3>
          {confirmed ? (
            <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">
              <Check className="h-3 w-3" />
              已入库 v{version}{partialFields && partialFields.length > 0 ? ` · ${partialFields.length} 项` : ''}
            </span>
          ) : (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400">
              待确认 · 未入库前 AI 写文章不会引用
            </span>
          )}
          {brief?.service_scope && (
            <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-400" title={brief.service_scope_reasoning || '业务范围判定'}>
              <Globe className="h-3 w-3" />
              {brief.service_scope === 'local' ? '本地服务' : brief.service_scope === 'national' ? '全国线上' : '全国 + 本地'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {/* [CTO-13.0 2026-04-19 S1.4] brief 一键应用到品牌信息字段
              仅 confirmed=true 时显示（未审核不让用户用到生产字段）*/}
          {confirmed && (
            <Button
              size="sm"
              className="h-7 text-xs bg-amber-600 hover:bg-amber-700 text-white"
              onClick={handleApplyBrief}
              disabled={applyingBrief}
              title="把深度行业分析的结果应用到「基本信息 / IP 人设」表单（可编辑后保存）"
            >
              {applyingBrief ? <RefreshCw className="h-3 w-3 mr-1 animate-spin" /> : <Sparkles className="h-3 w-3 mr-1" />}
              应用到品牌信息
            </Button>
          )}
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setHistoryOpen(true)}>
            <History className="h-3 w-3 mr-1" /> 历史
          </Button>
        </div>
      </div>

      {/* 5 组展开 */}
      <div className="space-y-2">
        {GROUPS.map(group => {
          const Icon = group.icon;
          const expanded = expandedGroups[group.id];
          const fieldsWithValue = group.fields.filter(f => brief[f.key] != null && brief[f.key] !== '');
          if (fieldsWithValue.length === 0) return null;

          const groupFieldKeys = group.fields.map(f => f.key);
          const groupSelectedCount = groupFieldKeys.filter(k => selectedFields.has(k)).length;
          const groupAllSelected = groupSelectedCount === groupFieldKeys.length;

          const toggleGroup = () => {
            setExpandedGroups(s => ({ ...s, [group.id]: !s[group.id] }));
          };
          const toggleGroupSelect = () => {
            if (confirmed) return;  // 已确认后不能改部分勾选
            setSelectedFields(s => {
              const n = new Set(s);
              if (groupAllSelected) groupFieldKeys.forEach(k => n.delete(k));
              else groupFieldKeys.forEach(k => n.add(k));
              return n;
            });
          };

          return (
            <div key={group.id} className={cn('rounded-lg border border-border/30 bg-card/30')}>
              {/* 组 header */}
              <div className="flex items-center gap-2 px-3 py-2">
                {!confirmed && (
                  <input
                    type="checkbox"
                    checked={groupAllSelected}
                    onChange={toggleGroupSelect}
                    className="h-3.5 w-3.5 rounded border-border"
                  />
                )}
                <button onClick={toggleGroup} className="flex-1 flex items-center gap-2 text-left">
                  <Icon className={`h-3.5 w-3.5 text-${group.color}-400`} />
                  <span className="text-xs font-medium">{group.title}</span>
                  <span className="text-[10px] text-muted-foreground">· {fieldsWithValue.length} 项</span>
                  {expanded ? <ChevronDown className="h-3 w-3 text-muted-foreground ml-auto" /> : <ChevronRight className="h-3 w-3 text-muted-foreground ml-auto" />}
                </button>
                <Button
                  size="sm" variant="ghost"
                  className="h-6 px-2 text-[10px] text-muted-foreground hover:text-amber-400"
                  onClick={() => openRerun(groupFieldKeys)}
                  disabled={submitting}
                  title="重跑本组字段"
                >
                  <RefreshCw className="h-3 w-3" />
                </Button>
              </div>

              {/* 组内字段 */}
              {expanded && (
                <div className="border-t border-border/30 px-3 py-2 space-y-1.5">
                  {fieldsWithValue.map(f => {
                    const v = brief[f.key];
                    const checked = selectedFields.has(f.key);
                    return (
                      <div key={f.key} className="flex items-start gap-2 text-[11px]">
                        {!confirmed && (
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => {
                              setSelectedFields(s => {
                                const n = new Set(s);
                                if (n.has(f.key)) n.delete(f.key); else n.add(f.key);
                                return n;
                              });
                            }}
                            className="h-3 w-3 mt-1 rounded border-border"
                          />
                        )}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 mb-0.5">
                            <span className="text-[10px] text-muted-foreground">{f.label}</span>
                          </div>
                          <div className="text-foreground/85 leading-relaxed whitespace-pre-wrap break-words">
                            {formatValue(v) || '—'}
                          </div>
                        </div>
                        <Button
                          size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]"
                          onClick={() => openEdit(f)}
                          title="手动编辑（免费）"
                        >
                          <Edit3 className="h-3 w-3" />
                        </Button>
                        <Button
                          size="sm" variant="ghost" className="h-6 px-1.5 text-[10px]"
                          onClick={() => openRerun([f.key])}
                          disabled={submitting}
                          title="重跑此字段"
                        >
                          <RefreshCw className="h-3 w-3" />
                        </Button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* v3.6 L1/L2 公共素材池(只读 · 折叠展示) */}
      {poolData && (poolData._pool_summary?.l1_count > 0 || poolData._pool_summary?.l2_count > 0) && (
        <div className="mt-3 rounded-lg border border-sky-500/20 bg-sky-500/[0.03] p-3">
          <button
            onClick={() => setPoolExpanded(v => !v)}
            className="flex items-center gap-2 text-xs text-sky-300 hover:text-sky-200 w-full"
          >
            {poolExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            <Database className="h-3 w-3" />
            <span className="font-medium">行业知识库</span>
            <span className="text-[10px] text-sky-400/70">
              {poolData._pool_summary.l1_count + poolData._pool_summary.l2_count} 条参考素材 · AI 写作时自动调用
            </span>
          </button>
          {poolExpanded && (
            <div className="mt-3 space-y-3 text-[11px]">
              {/* L1 行业层 */}
              {(poolData.l1?.industry_jargon?.length > 0 || poolData.l1?.authority_sources?.length > 0 || poolData.l1?.counter_consensus?.length > 0) && (
                <div>
                  <div className="text-[10px] text-sky-400 mb-1">行业通识</div>
                  {/* 复用 PoolItemRow 渲染单条素材 + 编辑/删除按钮 */}
                  {poolData.l1.industry_jargon?.length > 0 && (
                    <PoolFieldBlock
                      label="行业术语"
                      items={poolData.l1.industry_jargon}
                      previewLimit={10}
                      level="industry"
                      field_name="industry_jargon"
                      renderItem={(j: any) => <><span className="text-sky-300">{j.term}</span>{j.meaning && <span className="text-muted-foreground"> · {j.meaning}</span>}</>}
                      onEdit={openPoolEdit}
                      readonly={readonly}
                    />
                  )}
                  {poolData.l1.authority_sources?.length > 0 && (
                    <PoolFieldBlock
                      label="权威信源"
                      items={poolData.l1.authority_sources}
                      previewLimit={5}
                      level="industry"
                      field_name="authority_sources"
                      renderItem={(a: any) => (
                        <>
                          <span className="text-sky-300">{a.source}</span>: {a.conclusion?.slice(0, 80)}
                          {a.url && <a href={a.url} target="_blank" rel="noreferrer" className="text-sky-400 ml-1">↗</a>}
                        </>
                      )}
                      onEdit={openPoolEdit}
                      readonly={readonly}
                    />
                  )}
                  {poolData.l1.counter_consensus?.length > 0 && (
                    <PoolFieldBlock
                      label="行业反共识"
                      items={poolData.l1.counter_consensus}
                      previewLimit={5}
                      level="industry"
                      field_name="counter_consensus"
                      renderItem={(c: any) => <>{c.insight?.slice(0, 100)}</>}
                      onEdit={openPoolEdit}
                      readonly={readonly}
                    />
                  )}
                </div>
              )}
              {/* L2 品类层 */}
              {(poolData.l2?.user_voices_pool?.length > 0 || poolData.l2?.case_evidence_pool?.length > 0) && (
                <div>
                  <div className="text-[10px] text-sky-400 mb-1">细分品类</div>
                  {poolData.l2.user_voices_pool?.length > 0 && (
                    <PoolFieldBlock
                      label="真实用户原话"
                      items={poolData.l2.user_voices_pool}
                      previewLimit={5}
                      level="category"
                      field_name="user_voices_pool"
                      renderItem={(v: any) => <>"{v.quote?.slice(0, 120)}"</>}
                      onEdit={openPoolEdit}
                      readonly={readonly}
                    />
                  )}
                  {poolData.l2.case_evidence_pool?.length > 0 && (
                    <PoolFieldBlock
                      label="真实案例数据"
                      items={poolData.l2.case_evidence_pool}
                      previewLimit={5}
                      level="category"
                      field_name="case_evidence_pool"
                      renderItem={(c: any) => <><span className="text-sky-300">{c.subject}</span>: {c.result?.slice(0, 80)}</>}
                      onEdit={openPoolEdit}
                      readonly={readonly}
                    />
                  )}
                </div>
              )}
              <div className="text-[10px] text-muted-foreground pt-1 border-t border-sky-500/10">
                💡 hover 单条素材右侧 ✏️ 修改 / 🗑 移除,下次解析将使用最新版本
              </div>
            </div>
          )}
        </div>
      )}

      {/* 确认入库 / 已入库状态按钮 */}
      <div className="mt-4 flex items-center justify-between flex-wrap gap-2">
        <div className="text-[10px] text-muted-foreground">
          {confirmed
            ? '✓ 已审核入库 · AI 写文章会融合此知识库'
            : `已选 ${selectedFields.size} / ${ALL_FIELD_KEYS.length} 项 · 确认后 AI 才会使用`}
        </div>
        <Button
          size="sm"
          className={cn('h-8', confirmed ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-amber-600 hover:bg-amber-700')}
          onClick={handleConfirm}
          disabled={submitting || selectedFields.size === 0}
        >
          {confirmed ? (
            <><Check className="h-3.5 w-3.5 mr-1" />重新确认</>
          ) : (
            <><Check className="h-3.5 w-3.5 mr-1" />确认入库（{selectedFields.size} 项）</>
          )}
        </Button>
      </div>

      {/* -------- v3.6 L1/L2 矫正反哺 Dialog （S3.3: readonly 时不渲染）-------- */}
      {!readonly && <Dialog open={!!editingPool} onOpenChange={(o) => !o && setEditingPool(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editingPool?.editType === 'delete' ? '移除' : '修改'} · {editingPool?.label} #{(editingPool?.item_index ?? 0) + 1}
            </DialogTitle>
            <DialogDescription className="text-xs space-y-1">
              <div className="flex items-center gap-1 text-amber-400">
                <AlertTriangle className="h-3 w-3" />
                提交后将更新到行业知识库,下次解析使用最新版本
              </div>
              <div className="text-muted-foreground">
                {editingPool?.level === 'industry' ? '行业通识素材' : `${poolData?.category || ''} 细分品类素材`}
              </div>
            </DialogDescription>
          </DialogHeader>
          {editingPool?.editType === 'update' ? (
            <>
              <div className="text-[11px] text-muted-foreground mb-1">原值(JSON):</div>
              <pre className="text-[11px] p-2 bg-muted/30 rounded max-h-32 overflow-auto whitespace-pre-wrap break-all">
                {typeof editingPool.original === 'string' ? editingPool.original : JSON.stringify(editingPool.original, null, 2)}
              </pre>
              <div className="text-[11px] text-muted-foreground mb-1 mt-2">新值(JSON 或字符串均可):</div>
              <textarea
                value={poolEditValue}
                onChange={(e) => setPoolEditValue(e.target.value)}
                className="w-full text-xs p-2 bg-background border border-input rounded h-32 font-mono"
                placeholder="编辑 JSON 或纯文本"
              />
            </>
          ) : (
            <>
              <div className="text-[11px] text-muted-foreground mb-1">将删除以下条目:</div>
              <pre className="text-[11px] p-2 bg-red-500/10 rounded max-h-32 overflow-auto whitespace-pre-wrap break-all border border-red-500/20">
                {typeof editingPool?.original === 'string' ? editingPool.original : JSON.stringify(editingPool?.original, null, 2)}
              </pre>
            </>
          )}
          <div className="text-[11px] text-muted-foreground mb-1 mt-2">修改原因(可选):</div>
          <input
            type="text"
            value={poolEditReason}
            onChange={(e) => setPoolEditReason(e.target.value)}
            className="w-full text-xs p-2 bg-background border border-input rounded"
            placeholder="如:数据已过时 / 信源不准确 / 内容不相关"
          />
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditingPool(null)} disabled={submitting}>
              <X className="h-3 w-3 mr-1" />取消
            </Button>
            <Button
              size="sm"
              className={editingPool?.editType === 'delete' ? 'bg-red-600 hover:bg-red-700' : ''}
              onClick={handlePoolSave}
              disabled={submitting || (editingPool?.editType === 'update' && !poolEditValue.trim())}
            >
              <Save className="h-3 w-3 mr-1" />
              {editingPool?.editType === 'delete' ? '确认移除' : '保存修改'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>}

      {/* -------- v3.8 C4 · 质量门控拒绝 · 重写 Dialog (CTO-13.3 2026-04-19) -------- */}
      {!readonly && <Dialog open={!!gateRejection} onOpenChange={(o) => !o && setGateRejection(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-amber-400" />
              AI 审核未通过这条修改
            </DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground">
              {gateRejection?.message}
            </DialogDescription>
          </DialogHeader>
          {gateRejection?.reasons && gateRejection.reasons.length > 0 && (
            <div className="rounded bg-amber-500/10 border border-amber-500/20 p-2 space-y-1">
              <div className="text-[11px] font-medium text-amber-400">AI 反馈：</div>
              <ul className="text-[11px] text-amber-200/90 list-disc pl-4 space-y-0.5">
                {gateRejection.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="text-[11px] text-muted-foreground mt-2">重写内容（JSON 或纯文本均可）：</div>
          <textarea
            value={gateRejection?.rewriteValue || ''}
            onChange={(e) => setGateRejection(prev => prev ? { ...prev, rewriteValue: e.target.value } : prev)}
            className="w-full text-xs p-2 bg-background border border-input rounded h-32 font-mono"
            placeholder="改写后再提交, AI 会重新审核"
          />
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setGateRejection(null)} disabled={submitting}>
              <X className="h-3 w-3 mr-1" />取消
            </Button>
            <Button
              size="sm"
              onClick={handleRewriteSubmit}
              disabled={submitting || !(gateRejection?.rewriteValue || '').trim()}
            >
              <Save className="h-3 w-3 mr-1" />重写后提交
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>}

      {/* -------- Phase 2 · 编辑 Dialog -------- */}
      <Dialog open={!!editingField} onOpenChange={(o) => !o && setEditingField(null)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>编辑 · {editingField?.label}</DialogTitle>
            <DialogDescription className="text-xs">
              手动编辑免费。保存后需重新点击「确认入库」才对 AI 写文章生效。
              {editingField?.valueType === 'list' && <span className="block mt-1">多项用换行分隔</span>}
            </DialogDescription>
          </DialogHeader>
          <textarea
            value={editValue}
            onChange={e => setEditValue(e.target.value)}
            className="w-full min-h-[180px] text-sm bg-background border border-border rounded-lg px-3 py-2 outline-none focus:ring-1 focus:ring-foreground/20"
            placeholder={editingField?.placeholder}
          />
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditingField(null)} disabled={submitting}>
              <X className="h-3.5 w-3.5 mr-1" />取消
            </Button>
            <Button size="sm" onClick={handleSaveEdit} disabled={submitting}>
              <Save className="h-3.5 w-3.5 mr-1" />保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* -------- Phase 3 · 重跑 Dialog（静默扣费口径 · 不前置告知算力）-------- */}
      <Dialog open={!!rerunFields} onOpenChange={(o) => !o && setRerunFields(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>AI 重跑字段</DialogTitle>
            <DialogDescription className="text-xs">
              AI 会联网重新搜索并覆盖以下字段，重跑后需再次点击「确认入库」才对生产链路生效。
            </DialogDescription>
          </DialogHeader>
          <div className="text-xs space-y-1 py-2">
            <div className="text-muted-foreground mb-1">将重跑 {rerunFields?.length} 个字段：</div>
            {rerunFields?.map(k => {
              const field = GROUPS.flatMap(g => g.fields).find(f => f.key === k);
              return (
                <div key={k} className="flex items-center gap-1 text-foreground/80">
                  <RefreshCw className="h-3 w-3 text-amber-400" />
                  <span>{field?.label || k}</span>
                </div>
              );
            })}
          </div>
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300/90">
            <AlertTriangle className="h-3 w-3 inline mr-1" />
            没生成出新内容会自动还回，请放心。
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setRerunFields(null)} disabled={submitting}>取消</Button>
            <Button size="sm" className="bg-amber-600 hover:bg-amber-700" onClick={handleRerun} disabled={submitting}>
              {submitting ? <><RefreshCw className="h-3.5 w-3.5 mr-1 animate-spin" />重跑中…</> : '开始重跑'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* -------- Phase 4 · 历史 Dialog -------- */}
      <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>版本历史</DialogTitle>
            <DialogDescription className="text-xs">
              每次 AI 生成 / 用户编辑 / 重跑 / 回滚都会记录一条。点击版本可对比或回滚。
            </DialogDescription>
          </DialogHeader>
          {historyLoading ? (
            <div className="text-center text-xs text-muted-foreground py-4">加载中...</div>
          ) : historyList.length === 0 ? (
            <div className="text-center text-xs text-muted-foreground py-4">暂无历史版本</div>
          ) : (
            <div className="grid grid-cols-2 gap-3 text-xs">
              {/* 左：版本列表 */}
              <div className="space-y-1.5 max-h-[50vh] overflow-auto">
                {historyList.map(v => (
                  <button
                    key={v.id}
                    onClick={() => loadCompare(v.version)}
                    className={cn(
                      'w-full text-left rounded-lg border px-2.5 py-2 transition-colors',
                      compareVersion === v.version
                        ? 'border-amber-500/60 bg-amber-500/10'
                        : 'border-border/40 hover:bg-accent/30',
                    )}
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium">v{v.version}</span>
                      {v.version === version && <span className="text-[9px] px-1 rounded bg-emerald-500/15 text-emerald-400">当前</span>}
                      {v.confirmed && <Check className="h-3 w-3 text-emerald-400" />}
                    </div>
                    <div className="text-[10px] text-muted-foreground mt-0.5">
                      {sourceLabel(v.source)} · {v.created_at ? new Date(v.created_at).toLocaleString('zh-CN') : ''}
                    </div>
                    {v.partial_fields && Array.isArray(v.partial_fields) && v.partial_fields.length > 0 && (
                      <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                        入库 {v.partial_fields.length} 项
                      </div>
                    )}
                  </button>
                ))}
              </div>
              {/* 右：对比详情 */}
              <div className="space-y-2 max-h-[50vh] overflow-auto">
                {compareVersion == null ? (
                  <div className="text-center text-[11px] text-muted-foreground py-8">
                    ← 选择左侧版本查看
                  </div>
                ) : compareData ? (
                  <>
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] font-medium">v{compareVersion} 内容</span>
                      {compareVersion !== version && (
                        <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => handleRollback(compareVersion)} disabled={submitting}>
                          回滚到此版本
                        </Button>
                      )}
                    </div>
                    <div className="space-y-2">
                      {GROUPS.flatMap(g => g.fields).map(f => {
                        const oldV = compareData[f.key];
                        const curV = brief[f.key];
                        const same = JSON.stringify(oldV) === JSON.stringify(curV);
                        if (oldV == null && curV == null) return null;
                        return (
                          <div key={f.key} className={cn('rounded border p-2', same ? 'border-border/30' : 'border-amber-500/30 bg-amber-500/5')}>
                            <div className="text-[10px] text-muted-foreground mb-1">{f.label} {!same && '· 已变更'}</div>
                            <div className="text-[11px] text-foreground/85 whitespace-pre-wrap break-words line-clamp-4">
                              {formatValue(oldV) || '—'}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </>
                ) : (
                  <div className="text-center text-[11px] text-muted-foreground py-8">加载中...</div>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </div>
  );
}

function sourceLabel(s: string): string {
  return {
    'ai_full': 'AI 全量解析',
    'ai_rerun': 'AI 重跑',
    'user_edit': '用户编辑',
    'user_confirm': '用户确认入库',
    'rollback': '回滚',
  }[s] || s;
}
