/**
 * BatchUpgradeDialog — M1c A2 · 批量升级客户资料
 *
 * CTO-15.9 2026-04-25 · PRD M1c Epic 3:
 *   代理 /my-clients 页顶部 banner "N 个客户资料不全 · 一键升级"
 *   点击进本 Dialog · 勾选客户 · 批量调 /api/profiles/ai-fill
 *
 * 并发:Semaphore(3) 控制 DashScope API 限流
 * 每个客户失败独立 toast · 不 block 其他
 * 完成后回调 onAllDone 刷新 /my-clients
 *
 * 前置条件:
 *   · clients 必须带 completeness(父组件 fetchClients 时并行拉 /api/brands/{id}/completeness)
 *   · 客户知识库为空会 400 · UI 提示代理先上传资料
 */
import { useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { authApi } from '@/context/AuthContext';
import { usePricing } from '@/context/PricingContext';
import { FeatureCostBadge } from '@/components/FeatureCostBadge';
import { Loader2, Sparkles, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';

interface UpgradeClient {
  id: number;
  name: string;
  industry?: string;
  completeness: number;  // 0-100
}

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** 所有 completeness < 80 的客户 · 由父组件过滤好传入 */
  candidates: UpgradeClient[];
  /** 每个客户 upgrade 完成后调(批量场景可多次调)· 用于父组件刷新单个 */
  onClientUpgraded?: (clientId: number, ok: boolean) => void;
  /** 全部完成后调 · 用于刷新列表 */
  onAllDone?: () => void;
}

const CONCURRENCY = 3;

export function BatchUpgradeDialog({ open, onOpenChange, candidates, onClientUpgraded, onAllDone }: Props) {
  // [#65] 价目取自 feature_pricing SSOT;为空 = 该 code 还没配 ⇒ 不显示任何数字。
  const { getCost } = usePricing();
  const autofillCost = getCost('autofill_brand');
  const [selected, setSelected] = useState<Set<number>>(() => new Set(candidates.map(c => c.id)));
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ total: number; done: number; ok: number; failed: number }>({
    total: 0, done: 0, ok: 0, failed: 0,
  });
  const [results, setResults] = useState<Record<number, { status: 'pending' | 'running' | 'ok' | 'failed'; message?: string }>>({});

  // 静默扣费口径(2026-06-03)· 后端 _bill_ctx('autofill_brand') 真扣 · 页面不前置告知额度

  const toggleSelect = (id: number) => {
    if (running) return;
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (running) return;
    setSelected(prev =>
      prev.size === candidates.length ? new Set() : new Set(candidates.map(c => c.id))
    );
  };

  // ========== 批量升级核心 ==========
  const handleBatchUpgrade = async () => {
    if (running) return;
    const ids = Array.from(selected);
    if (ids.length === 0) {
      toast.error('请至少选择 1 个客户');
      return;
    }
    setRunning(true);
    setProgress({ total: ids.length, done: 0, ok: 0, failed: 0 });
    setResults(Object.fromEntries(ids.map(id => [id, { status: 'pending' as const }])));

    // 简易并发控制(Semaphore pattern)
    const queue = [...ids];
    const workers: Promise<void>[] = [];

    // CTO-15.16 P1 (Codex 复核) · 完成 toast 用本地累加器 · 不读 stale `progress` state
    // 原因:setProgress 是异步的 · Promise.all 解析时 state 可能还没刷到
    // 老代码 `progress.ok + selected.size - progress.failed` 是错的(还混 selected.size)
    let okCount = 0;
    let failedCount = 0;

    const runOne = async (clientId: number) => {
      setResults(prev => ({ ...prev, [clientId]: { status: 'running' } }));
      try {
        // CTO-15.16 M1c · persist=true 是关键 · 老行为返 success 但数据不进库 · 完整度永不动
        // 服务端会 update_profile 业务字段 + 合成部分 industry_brief JSONB(authority/format/diff)
        // D 组(industry_brief_status='done' + confirmed)仍需用户单独跑 deep-analyze + confirm
        const res = await authApi.post('/api/profiles/ai-fill', {
          brand_id: clientId,
          persist: true,
        });
        const ok = res.data?.success === true;
        const persistInfo = res.data?.persisted;
        const persistedFields: string[] = persistInfo?.updated_fields || [];
        const briefKeys: string[] = persistInfo?.synthesized_brief_keys || [];
        const persistFailed = ok && persistInfo && (persistInfo.errors?.length || 0) > 0;
        const finalOk = ok && !persistFailed;
        const detail = persistFailed
          ? `落库异常: ${persistInfo.errors[0]}`
          : ok
            ? `已更新 ${persistedFields.length} 字段` + (briefKeys.length ? ` + brief ${briefKeys.length}` : '')
            : (res.data?.error || '未知失败');
        setResults(prev => ({
          ...prev,
          [clientId]: finalOk
            ? { status: 'ok', message: detail }
            : { status: 'failed', message: detail },
        }));
        if (finalOk) okCount += 1; else failedCount += 1;
        setProgress(p => ({
          ...p,
          done: p.done + 1,
          ok: p.ok + (finalOk ? 1 : 0),
          failed: p.failed + (finalOk ? 0 : 1),
        }));
        onClientUpgraded?.(clientId, finalOk);
      } catch (e: unknown) {
        const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '请求失败';
        setResults(prev => ({ ...prev, [clientId]: { status: 'failed', message: String(msg).slice(0, 80) } }));
        failedCount += 1;
        setProgress(p => ({ ...p, done: p.done + 1, failed: p.failed + 1 }));
        onClientUpgraded?.(clientId, false);
      }
    };

    const worker = async () => {
      while (queue.length > 0) {
        const id = queue.shift();
        if (id === undefined) return;
        await runOne(id);
      }
    };

    for (let i = 0; i < Math.min(CONCURRENCY, ids.length); i++) {
      workers.push(worker());
    }
    await Promise.all(workers);

    setRunning(false);
    // 用本地累加器 · 不依赖 setProgress 的异步刷新
    if (failedCount === 0) {
      toast.success(`批量升级完成 · 全部 ${okCount} 个成功`);
    } else if (okCount === 0) {
      toast.error(`批量升级失败 · 全部 ${failedCount} 个失败 · 看每行详情`);
    } else {
      toast(`批量升级完成 · 成功 ${okCount} / 失败 ${failedCount}`);
    }
    onAllDone?.();
  };

  return (
    <Dialog open={open} onOpenChange={running ? undefined : onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            批量升级客户资料
          </DialogTitle>
        </DialogHeader>

        {candidates.length === 0 ? (
          <div className="py-8 text-center">
            <CheckCircle2 className="h-12 w-12 text-emerald-500 mx-auto mb-2" />
            <p className="text-sm text-muted-foreground">所有客户资料都 ≥ 80 分 · 无需升级</p>
          </div>
        ) : (
          <>
            {/* Banner */}
            <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-start gap-2 text-xs">
              <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium text-amber-700 dark:text-amber-300">
                  {candidates.length} 个客户资料 &lt; 80 分 · 影响诊断 / 拓词 / 报告质量
                </div>
                <div className="text-amber-700/80 dark:text-amber-300/80 mt-0.5">
                  AI 会读取每个客户的知识库 · 补齐 11+ 字段 · 30 秒左右/个 · 并发 {CONCURRENCY} · 失败自动跳过
                </div>
              </div>
            </div>

            {/* 选择汇总 */}
            <div className="flex items-center justify-between py-2 px-3 rounded-md bg-muted/40 text-xs">
              <div className="flex items-center gap-2">
                <Checkbox
                  checked={selected.size === candidates.length}
                  onCheckedChange={toggleAll}
                  disabled={running}
                />
                <span className="text-muted-foreground">
                  已选 <span className="text-foreground font-medium">{selected.size}</span> / {candidates.length}
                </span>
              </div>
            </div>

            {/* 客户列表 */}
            <div className="max-h-80 overflow-y-auto border border-border/60 rounded-md divide-y divide-border/30">
              {candidates.map(c => {
                const r = results[c.id];
                return (
                  <div
                    key={c.id}
                    className="flex items-center gap-3 px-3 py-2.5 hover:bg-muted/30 cursor-pointer text-xs"
                    onClick={() => toggleSelect(c.id)}
                  >
                    <Checkbox
                      checked={selected.has(c.id)}
                      onCheckedChange={() => toggleSelect(c.id)}
                      disabled={running}
                      onClick={(e) => e.stopPropagation()}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-foreground truncate">{c.name}</div>
                      {c.industry && (
                        <div className="text-[10px] text-muted-foreground/80">{c.industry}</div>
                      )}
                    </div>
                    <div className="shrink-0 flex items-center gap-2">
                      <span
                        className={
                          c.completeness < 40
                            ? 'text-rose-500'
                            : c.completeness < 60
                              ? 'text-amber-600'
                              : 'text-orange-500'
                        }
                      >
                        {c.completeness}%
                      </span>
                      {r?.status === 'running' && <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-500" />}
                      {r?.status === 'ok' && <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />}
                      {r?.status === 'failed' && (
                        <span className="flex items-center gap-1 text-rose-500" title={r.message}>
                          <XCircle className="h-3.5 w-3.5" />
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* 进度条 */}
            {running && (
              <div className="space-y-1">
                <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>进度 {progress.done} / {progress.total}</span>
                  <span>
                    ✅ {progress.ok} · ❌ {progress.failed}
                  </span>
                </div>
                <div className="h-1.5 bg-muted rounded-full overflow-hidden">
                  <div
                    className="h-full bg-emerald-500 transition-all"
                    style={{ width: `${(progress.done / Math.max(progress.total, 1)) * 100}%` }}
                  />
                </div>
              </div>
            )}
          </>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={running}>
            {running ? '升级中(请勿关闭)' : '取消'}
          </Button>
          {candidates.length > 0 && (
          <>
            {/* 🔴 [#65 2026-09-05] 2026-06-03 拍板第 ② 段:废除前置确认,但**价格必须看得见**。
                  用 getCost 判空门控 —— FeatureCostBadge 拿不到价会渲染「价目待配置」
                  (给开发看的调试串),在 C 补上 feature_pricing 行之前那句会漏给用户。
                  取不到价就**一个字都不显示**,同 #64:宁可少说,不可说错。 */}
            {autofillCost != null && selected.size > 0 && (
              <span data-testid="batch-upgrade-cost" className="mr-2 self-center">
                <FeatureCostBadge featureCode="autofill_brand" multiplier={selected.size} variant="inline" />
              </span>
            )}
            <Button onClick={handleBatchUpgrade} disabled={running || selected.size === 0}>
              {running ? (
                <><Loader2 className="h-4 w-4 mr-1 animate-spin" />升级中...</>
              ) : (
                <><Sparkles className="h-4 w-4 mr-1" />一键批量升级 ({selected.size})</>
              )}
            </Button>
          </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default BatchUpgradeDialog;
