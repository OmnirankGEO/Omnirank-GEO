/**
 * v3.6 行业知识库矫正记录管理 (CTO-15.2 · 2026-04-19)
 *
 * /admin/industry-corrections 路由
 *
 * 功能:
 * - 看所有用户对 L1/L2 公共素材的矫正记录(默认仅未回滚)
 * - 一键回滚某条矫正(恢复 industry_knowledge 该字段为 old_value)
 * - 飞轮监控指标(顶部 stats 卡片)
 *
 * 关联 endpoints:
 * - GET /api/admin/industry-knowledge/corrections (查询)
 * - POST /api/admin/industry-knowledge/rollback/{correction_id} (回滚)
 * - GET /api/admin/industry-knowledge/stats (飞轮指标)
 */

import { useState, useEffect, useCallback } from 'react';
import { authApi } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { toast } from 'sonner';
import { RotateCcw, Filter, Database, AlertTriangle, CheckCircle2, RefreshCw, ExternalLink, Recycle } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

interface Correction {
  id: number;
  level: 'industry' | 'category';
  industry: string;
  category: string | null;
  field_name: string;
  item_index: number | null;
  action: 'update' | 'add' | 'delete';
  old_value: any;
  new_value: any;
  corrector_user_id: number | null;
  corrector_profile_id: number | null;
  reason: string | null;
  created_at: string | null;
  rolled_back_at: string | null;
  rollback_by_admin_id: number | null;
}

interface Stats {
  by_level: Array<{
    level: string;
    entries: number;
    total_hits: number;
    total_corrections: number;
    oldest: string | null;
    newest: string | null;
  }>;
  top_hits: Array<{
    level: string;
    industry: string;
    category: string | null;
    search_count: number;
    correction_count: number;
    is_expired: boolean;
  }>;
  corrections_summary: {
    total: number;
    active: number;
    rolled_back: number;
  };
  flywheel: {
    l1_avg_reuse: number;
    l2_avg_reuse: number;
    interpretation: string;
  };
}

const FIELD_LABELS: Record<string, string> = {
  industry_jargon: '行业术语',
  authority_sources: '权威信源',
  counter_consensus: '行业反共识',
  user_voices_pool: '用户原话',
  case_evidence_pool: '真实案例',
};

const ACTION_LABELS: Record<string, string> = {
  update: '矫正',
  add: '新增',
  delete: '删除',
};

const ACTION_COLORS: Record<string, string> = {
  update: 'text-amber-400 bg-amber-500/10',
  add: 'text-emerald-400 bg-emerald-500/10',
  delete: 'text-red-400 bg-red-500/10',
};

export default function IndustryCorrectionsAdmin() {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [corrections, setCorrections] = useState<Correction[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  // 过滤
  const [filterIndustry, setFilterIndustry] = useState('');
  const [filterCategory, setFilterCategory] = useState('');
  const [filterOnlyActive, setFilterOnlyActive] = useState(true);

  // v3.7 行业洗牌
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [rebuildIndustry, setRebuildIndustry] = useState('');
  const [rebuildCategory, setRebuildCategory] = useState('');
  const [rebuildReason, setRebuildReason] = useState('');
  const [rebuilding, setRebuilding] = useState(false);

  const handleRebuild = async () => {
    if (!rebuildIndustry.trim() || !rebuildReason.trim()) return;
    if (!(await askConfirm({
      title: `⚠ 确认对【${rebuildIndustry}${rebuildCategory ? '/' + rebuildCategory : ''}】行业洗牌?`,
      description: `所有 L1/L2 数据会清空(归档到历史),所有用户下次解析触发会全量重采。理由: ${rebuildReason}`,
      confirmLabel: '确认洗牌',
      danger: true,
    }))) return;
    setRebuilding(true);
    try {
      const res = await authApi.post('/api/admin/industry-knowledge/rebuild', {
        industry: rebuildIndustry,
        category: rebuildCategory || undefined,
        reason: rebuildReason,
      });
      if (res.data?.success) {
        toast.success(`已重建 ${res.data.rebuilt_count} 条 · ${res.data.warning?.slice(0, 80)}`);
        setRebuildOpen(false);
        setRebuildIndustry('');
        setRebuildCategory('');
        setRebuildReason('');
        await Promise.all([loadCorrections(), loadStats()]);
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '重建失败');
    } finally {
      setRebuilding(false);
    }
  };

  const loadCorrections = useCallback(async () => {
    setLoading(true);
    try {
      const params: any = { limit: 100, only_active: filterOnlyActive };
      if (filterIndustry) params.industry = filterIndustry;
      if (filterCategory) params.category = filterCategory;
      const res = await authApi.get('/api/admin/industry-knowledge/corrections', { params });
      if (res.data?.success) {
        setCorrections(res.data.items || []);
      } else {
        toast.error(res.data?.detail || '加载失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [filterIndustry, filterCategory, filterOnlyActive]);

  const loadStats = useCallback(async () => {
    try {
      const res = await authApi.get('/api/admin/industry-knowledge/stats');
      if (res.data?.success) setStats(res.data);
    } catch {
      /* 静默,stats 是辅助 */
    }
  }, []);

  useEffect(() => {
    loadCorrections();
    loadStats();
  }, [loadCorrections, loadStats]);

  const handleRollback = async (correctionId: number) => {
    if (!(await askConfirm({ title: `一键回滚矫正 #${correctionId}？`, description: `回滚后该字段对全行业用户立即恢复为 old_value(下次 get_industry_knowledge 命中新版本)。`, danger: true }))) {
      return;
    }
    setRollingBack(correctionId);
    try {
      const res = await authApi.post(`/api/admin/industry-knowledge/rollback/${correctionId}`);
      if (res.data?.success) {
        toast.success(`已回滚 #${correctionId} · ${res.data.restored_field} 字段已恢复`);
        await Promise.all([loadCorrections(), loadStats()]);
      } else {
        toast.error(res.data?.detail || '回滚失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || '回滚失败');
    } finally {
      setRollingBack(null);
    }
  };

  const formatValue = (v: any): string => {
    if (v == null) return '-';
    if (typeof v === 'string') return v;
    return JSON.stringify(v);
  };

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Database className="h-5 w-5 text-sky-400" />
            行业知识库矫正记录 (v3.6)
          </h1>
          <p className="text-xs text-muted-foreground mt-1">
            用户对 L1/L2 公共素材池的矫正立即对全行业生效 · admin 后台可一键回滚
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setRebuildOpen(true)}>
            <Recycle className="h-3 w-3 mr-1" />行业洗牌重建
          </Button>
          <Button variant="outline" size="sm" onClick={() => { loadCorrections(); loadStats(); }} disabled={loading}>
            <RefreshCw className={cn('h-3 w-3 mr-1', loading && 'animate-spin')} />刷新
          </Button>
        </div>
      </div>

      {/* v3.7 行业洗牌 Dialog */}
      {rebuildOpen && (
        <Card className="p-4 border-amber-500/40 bg-amber-500/[0.03]">
          <div className="flex items-center gap-2 mb-3 text-sm font-medium text-amber-300">
            <Recycle className="h-4 w-4" />
            行业洗牌一键重建 (v3.7)
          </div>
          <div className="text-[11px] text-muted-foreground mb-3 space-y-1">
            <div>使用场景: 行业重大变革时(如 GPT-5 发布 / AI 搜索引擎重新洗牌 / 政策大改)需要清空整个行业的道法术器重建。</div>
            <div className="text-amber-400">⚠ 该行业下所有 L1+L2 数据会清空(归档到历史),所有用户下次解析触发会全量重采(类似首次拓荒成本)。</div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2 mb-3">
            <input
              type="text"
              value={rebuildIndustry}
              onChange={(e) => setRebuildIndustry(e.target.value)}
              className="px-3 py-2 bg-background border border-input rounded text-xs"
              placeholder="行业名(必填,如 GEO 服务)"
            />
            <input
              type="text"
              value={rebuildCategory}
              onChange={(e) => setRebuildCategory(e.target.value)}
              className="px-3 py-2 bg-background border border-input rounded text-xs"
              placeholder="品类(可选,只重建该 L2)"
            />
            <input
              type="text"
              value={rebuildReason}
              onChange={(e) => setRebuildReason(e.target.value)}
              className="px-3 py-2 bg-background border border-input rounded text-xs"
              placeholder="重建理由(必填,审计用)"
            />
          </div>
          <div className="flex items-center gap-2 justify-end">
            <Button variant="outline" size="sm" onClick={() => setRebuildOpen(false)} disabled={rebuilding}>
              取消
            </Button>
            <Button
              size="sm"
              className="bg-amber-600 hover:bg-amber-700"
              onClick={handleRebuild}
              disabled={!rebuildIndustry.trim() || !rebuildReason.trim() || rebuilding}
            >
              {rebuilding ? <RefreshCw className="h-3 w-3 mr-1 animate-spin" /> : <Recycle className="h-3 w-3 mr-1" />}
              确认重建
            </Button>
          </div>
        </Card>
      )}

      {/* 飞轮监控 */}
      {stats && (
        <Card className="p-4">
          <div className="text-sm font-medium mb-3 flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            飞轮起飞情况
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {stats.by_level.map(l => (
              <div key={l.level} className="rounded border bg-muted/20 p-3">
                <div className="text-xs text-muted-foreground">L{l.level === 'industry' ? '1 行业' : '2 品类'}</div>
                <div className="text-lg font-semibold">{l.entries} <span className="text-xs text-muted-foreground font-normal">条目</span></div>
                <div className="text-[11px] text-muted-foreground">命中 {l.total_hits} · 矫正 {l.total_corrections}</div>
              </div>
            ))}
            <div className="rounded border bg-sky-500/[0.03] border-sky-500/20 p-3">
              <div className="text-xs text-muted-foreground">L1 平均复用</div>
              <div className="text-lg font-semibold text-sky-400">{stats.flywheel.l1_avg_reuse}<span className="text-xs font-normal"> 次/条</span></div>
              <div className="text-[11px] text-muted-foreground">L2: {stats.flywheel.l2_avg_reuse}</div>
            </div>
            <div className="rounded border bg-amber-500/[0.03] border-amber-500/20 p-3">
              <div className="text-xs text-muted-foreground">矫正概况</div>
              <div className="text-lg font-semibold text-amber-400">{stats.corrections_summary.active}<span className="text-xs font-normal"> 生效中</span></div>
              <div className="text-[11px] text-muted-foreground">回滚 {stats.corrections_summary.rolled_back} / 总 {stats.corrections_summary.total}</div>
            </div>
          </div>
          <div className="text-[11px] text-muted-foreground mt-3 pt-3 border-t">
            💡 {stats.flywheel.interpretation}
          </div>
          {stats.top_hits.length > 0 && (
            <div className="mt-3">
              <div className="text-xs text-muted-foreground mb-2">
                Top 10 热门行业(按复用次数) · <span className="text-amber-400">点击 chip 可自动填入「行业洗牌」表单</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {stats.top_hits.map((h, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => {
                      // [CTO-15.3 2026-04-20] 老板要求: 点 chip 自动填入洗牌表单,避免手输错 industry/category
                      setRebuildIndustry(h.industry);
                      setRebuildCategory(h.category || '');
                      setRebuildReason('');
                      setRebuildOpen(true);
                    }}
                    className={cn(
                      'text-[11px] px-2 py-0.5 rounded border cursor-pointer transition-colors',
                      h.is_expired
                        ? 'border-muted bg-muted/20 text-muted-foreground hover:bg-muted/40'
                        : 'border-sky-500/30 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20'
                    )}
                    title={`点击填入洗牌表单: ${h.industry}${h.category ? '/' + h.category : ''}`}
                  >
                    {h.industry}{h.category ? `/${h.category}` : ''}: {h.search_count}{h.is_expired && ' (已过期)'}
                  </button>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      {/* 过滤 */}
      <Card className="p-4">
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <Filter className="h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={filterIndustry}
            onChange={(e) => setFilterIndustry(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && loadCorrections()}
            className="px-3 py-1.5 bg-background border border-input rounded text-xs w-48"
            placeholder="行业过滤(如 GEO 服务)"
          />
          <input
            type="text"
            value={filterCategory}
            onChange={(e) => setFilterCategory(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && loadCorrections()}
            className="px-3 py-1.5 bg-background border border-input rounded text-xs w-48"
            placeholder="品类过滤(可选)"
          />
          <label className="flex items-center gap-1 text-xs text-muted-foreground cursor-pointer">
            <input
              type="checkbox"
              checked={filterOnlyActive}
              onChange={(e) => setFilterOnlyActive(e.target.checked)}
              className="rounded"
            />
            仅显示生效中
          </label>
          <Button size="sm" onClick={loadCorrections} disabled={loading}>查询</Button>
        </div>
      </Card>

      {/* 矫正列表 */}
      <Card className="overflow-hidden">
        <div className="px-4 py-3 border-b flex items-center justify-between">
          <div className="text-sm font-medium">矫正记录 ({corrections.length})</div>
        </div>
        {loading ? (
          <div className="p-8 text-center text-sm text-muted-foreground">加载中...</div>
        ) : corrections.length === 0 ? (
          <div className="p-8 text-center text-sm text-muted-foreground">暂无矫正记录</div>
        ) : (
          <div className="divide-y">
            {corrections.map(c => (
              <div key={c.id} className="p-4 hover:bg-muted/20">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap mb-1">
                      <span className="text-xs font-semibold">#{c.id}</span>
                      <span className={cn('text-[10px] px-1.5 py-0.5 rounded font-medium', ACTION_COLORS[c.action])}>
                        {ACTION_LABELS[c.action]}
                      </span>
                      <span className="text-[11px] text-muted-foreground">
                        L{c.level === 'industry' ? '1' : '2'}
                      </span>
                      <span className="text-xs text-foreground/80">
                        {c.industry}{c.category ? ` · ${c.category}` : ''}
                      </span>
                      <span className="text-[11px] text-sky-400">
                        {FIELD_LABELS[c.field_name] || c.field_name}
                        {c.item_index !== null && ` #${c.item_index + 1}`}
                      </span>
                      {c.rolled_back_at ? (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                          已回滚
                        </span>
                      ) : (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">
                          生效中
                        </span>
                      )}
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[11px] mt-2">
                      <div>
                        <div className="text-muted-foreground mb-0.5">原值:</div>
                        <pre className="bg-muted/20 rounded p-2 text-[11px] overflow-x-auto whitespace-pre-wrap break-all max-h-24">
                          {formatValue(c.old_value)}
                        </pre>
                      </div>
                      {c.action !== 'delete' && (
                        <div>
                          <div className="text-muted-foreground mb-0.5">新值:</div>
                          <pre className="bg-amber-500/[0.03] border border-amber-500/20 rounded p-2 text-[11px] overflow-x-auto whitespace-pre-wrap break-all max-h-24">
                            {formatValue(c.new_value)}
                          </pre>
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-3 text-[10px] text-muted-foreground mt-2">
                      <span>用户 #{c.corrector_user_id ?? '?'}</span>
                      {c.corrector_profile_id && (
                        <a href={`/brands/${c.corrector_profile_id}`} className="text-sky-400 hover:text-sky-300 inline-flex items-center gap-0.5">
                          profile #{c.corrector_profile_id}
                          <ExternalLink className="h-2.5 w-2.5" />
                        </a>
                      )}
                      <span>{c.created_at?.replace('T', ' ').slice(0, 19)}</span>
                      {c.reason && <span className="italic">"{c.reason}"</span>}
                      {c.rolled_back_at && (
                        <span className="text-amber-400">
                          回滚于 {c.rolled_back_at.replace('T', ' ').slice(0, 19)} by admin #{c.rollback_by_admin_id}
                        </span>
                      )}
                    </div>
                  </div>
                  {!c.rolled_back_at && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => handleRollback(c.id)}
                      disabled={rollingBack === c.id}
                      className="shrink-0"
                    >
                      {rollingBack === c.id ? (
                        <RefreshCw className="h-3 w-3 animate-spin" />
                      ) : (
                        <><RotateCcw className="h-3 w-3 mr-1" />回滚</>
                      )}
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="text-[11px] text-muted-foreground p-4 rounded border bg-amber-500/[0.03] border-amber-500/20 flex items-start gap-2">
        <AlertTriangle className="h-3 w-3 text-amber-400 shrink-0 mt-0.5" />
        <span>
          矫正立即对全行业生效(老板拍板:不要 N 人共识阈值,避免冷启动 0 矫正)。
          回滚后该字段恢复为 old_value,industry_knowledge.version + 1。
          所有操作完整审计在 industry_knowledge_corrections 表。
        </span>
      </div>
      {confirmDialog}
    </div>
  );
}
