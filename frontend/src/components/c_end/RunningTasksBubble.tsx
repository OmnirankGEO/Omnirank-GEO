/**
 * RunningTasksBubble — 旧 C 端布局 右下角运行中任务气泡
 * CTO-15.5 Phase 4 PLAN 05 Task 5.3 (2026-04-20)
 *
 * 功能:
 *   - fixed bottom-4 right-4 z-50 浮层
 *   - 10s 轮询 GET /api/geo-plan/tasks?status=running&limit=10
 *   - 同时跟踪最近 5min 内 done 的任务(localStorage 记已查看)
 *   - 有 running → 橙色 + running 数字 badge
 *   - 有 done 未查看 → 蓝色 + done 数字 badge
 *   - 都是 0 → 隐藏
 *   - Popover 精简列表 · Drawer 完整详情
 *   - 点卡片 → panel.openInPanel('/c/geo-plan?task_id=X') 并标已查看
 *   - done 5 分钟后或手动查看后自动从 Bubble 移除
 *
 * 同时维护 localStorage key 'viewed_done_task_ids' → Set<number>
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { Zap, CheckCircle2, Loader2, ChevronRight, X as XIcon, ArrowRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { authFetch } from '@/lib/api';
import { useCEndPanel } from '@/context/CEndPanelContext';

const POLL_INTERVAL_MS = 10_000;
const DONE_SHOW_WINDOW_MS = 5 * 60 * 1000; // 5min
const VIEWED_KEY = 'viewed_geo_plan_done_task_ids';
const RECENT_KEY = 'recent_geo_plan_done_tasks';

interface TaskSummary {
  id: number;
  brand_id: number;
  brand_name?: string | null;
  brand_industry?: string | null;
  status: string;
  progress_stage?: string | null;
  progress_percent?: number;
  progress_message?: string | null;
  queued_at?: string | null;
  started_at?: string | null;
  done_at?: string | null;
  data_mode?: string;
}

function getViewedIds(): Set<number> {
  try {
    const raw = localStorage.getItem(VIEWED_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return new Set(arr.map(Number).filter(Number.isFinite));
  } catch { /* ignore */ }
  return new Set();
}

function addViewedId(id: number) {
  const s = getViewedIds();
  s.add(id);
  try {
    localStorage.setItem(VIEWED_KEY, JSON.stringify([...s].slice(-200)));
  } catch { /* ignore */ }
}

// 记录最近 done 任务 (用 localStorage 撑过"从 running 列表消失到用户查看"的空白)
function getRecentDone(): TaskSummary[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return arr;
  } catch { /* ignore */ }
  return [];
}

function upsertRecentDone(tasks: TaskSummary[]) {
  try {
    const cutoff = Date.now() - DONE_SHOW_WINDOW_MS;
    const byId = new Map<number, TaskSummary>();
    for (const t of getRecentDone()) {
      if (t.done_at && new Date(t.done_at).getTime() > cutoff) byId.set(t.id, t);
    }
    for (const t of tasks) {
      if (t.status === 'done' && t.done_at && new Date(t.done_at).getTime() > cutoff) {
        byId.set(t.id, t);
      }
    }
    localStorage.setItem(RECENT_KEY, JSON.stringify([...byId.values()]));
  } catch { /* ignore */ }
}

function relativeTime(iso?: string | null): string {
  if (!iso) return '';
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return '';
  const diff = Date.now() - d;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Date(iso).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

export function RunningTasksBubble() {
  const panel = useCEndPanel();
  const [running, setRunning] = useState<TaskSummary[]>([]);
  const [recentDone, setRecentDone] = useState<TaskSummary[]>([]);
  const [viewedIds, setViewedIds] = useState<Set<number>>(() => getViewedIds());
  const [open, setOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      // 拉 running
      const rRes = await authFetch('/api/geo-plan/tasks?status=running&limit=10');
      const rData = rRes.ok ? await rRes.json() : null;
      const rList: TaskSummary[] = (rData?.tasks || []) as TaskSummary[];
      setRunning(rList);
      // queued 也当 running 处理(用户视角"在跑")
      const qRes = await authFetch('/api/geo-plan/tasks?status=queued&limit=10');
      const qData = qRes.ok ? await qRes.json() : null;
      const qList: TaskSummary[] = (qData?.tasks || []) as TaskSummary[];
      setRunning([...rList, ...qList].sort((a, b) =>
        (a.queued_at || '').localeCompare(b.queued_at || '')
      ));
      // 拉最近 done (5 分钟内)
      const dRes = await authFetch('/api/geo-plan/tasks?status=done&limit=10');
      const dData = dRes.ok ? await dRes.json() : null;
      const dList: TaskSummary[] = (dData?.tasks || []) as TaskSummary[];
      const cutoff = Date.now() - DONE_SHOW_WINDOW_MS;
      const recent = dList.filter(t => t.done_at && new Date(t.done_at).getTime() > cutoff);
      upsertRecentDone(recent);
      // 合并 localStorage 里的(防抖通过轮询间隙 done 消失)
      const merged = new Map<number, TaskSummary>();
      for (const t of getRecentDone()) merged.set(t.id, t);
      for (const t of recent) merged.set(t.id, t);
      setRecentDone([...merged.values()]);
    } catch {
      // 网络错误静默, UI 保持前一轮状态
    }
  }, []);

  useEffect(() => {
    void refresh();
    const tid = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => window.clearInterval(tid);
  }, [refresh]);

  // 点击外部关闭 popover
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const runningCount = running.length;
  const unviewedDone = recentDone.filter(t => !viewedIds.has(t.id));
  const unviewedDoneCount = unviewedDone.length;

  // 都是 0 → 隐藏(保留空占位 · 容错用户调试时不懵)
  if (runningCount === 0 && unviewedDoneCount === 0) {
    return null;
  }

  const hasRunning = runningCount > 0;
  const buttonColor = hasRunning
    ? 'bg-amber-500 text-white hover:bg-amber-600 shadow-amber-500/30'
    : 'bg-blue-500 text-white hover:bg-blue-600 shadow-blue-500/30';
  const badgeColor = hasRunning ? 'bg-white text-amber-600' : 'bg-white text-blue-600';
  const Icon = hasRunning ? Loader2 : CheckCircle2;

  const handleClickTask = (task: TaskSummary) => {
    if (panel) {
      panel.openInPanel(`/c/geo-plan?task_id=${task.id}`);
    }
    // 标已查看
    addViewedId(task.id);
    setViewedIds(prev => {
      const n = new Set(prev);
      n.add(task.id);
      return n;
    });
    setOpen(false);
    setDrawerOpen(false);
  };

  return (
    <>
      <div ref={popoverRef} className="fixed bottom-4 right-4 z-40">
        {/* 主按钮 */}
        <button
          onClick={() => setOpen(v => !v)}
          className={cn(
            'relative flex items-center gap-1.5 rounded-full px-3 py-2 shadow-lg transition-all',
            buttonColor,
          )}
          aria-label={hasRunning ? `${runningCount} 个任务运行中` : `${unviewedDoneCount} 个任务完成`}
        >
          <Icon className={cn('h-4 w-4', hasRunning && 'animate-spin')} />
          <span className={cn(
            'min-w-[18px] h-5 rounded-full text-xs flex items-center justify-center font-bold px-1.5',
            badgeColor,
          )}>
            {hasRunning ? runningCount : unviewedDoneCount}
          </span>
        </button>

        {/* Popover 精简列表 */}
        {open && (
          <div className="absolute bottom-full right-0 mb-2 w-72 rounded-xl border border-border bg-card shadow-2xl overflow-hidden">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <span className="text-xs font-medium">我的 GEO 方案任务</span>
              <button
                onClick={() => setOpen(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="关闭"
              >
                <XIcon className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="max-h-80 overflow-y-auto">
              {hasRunning && (
                <>
                  <div className="px-3 py-1.5 text-[10px] text-amber-400 font-medium bg-amber-500/5">
                    运行中 ({runningCount})
                  </div>
                  {running.slice(0, 5).map(t => (
                    <TaskRow key={t.id} task={t} onClick={() => handleClickTask(t)} kind="running" />
                  ))}
                </>
              )}
              {unviewedDoneCount > 0 && (
                <>
                  <div className="px-3 py-1.5 text-[10px] text-blue-400 font-medium bg-blue-500/5">
                    刚完成 ({unviewedDoneCount})
                  </div>
                  {unviewedDone.slice(0, 5).map(t => (
                    <TaskRow key={t.id} task={t} onClick={() => handleClickTask(t)} kind="done" />
                  ))}
                </>
              )}
            </div>
            {(runningCount + unviewedDoneCount) > 0 && (
              <button
                onClick={() => { setDrawerOpen(true); setOpen(false); }}
                className="w-full border-t px-3 py-2 text-xs text-muted-foreground hover:bg-accent/30 flex items-center justify-center gap-1"
              >
                查看全部 <ChevronRight className="h-3 w-3" />
              </button>
            )}
          </div>
        )}
      </div>

      {/* Drawer 详情 */}
      {drawerOpen && (
        <>
          <div
            className="fixed inset-0 z-50 bg-black/40"
            onClick={() => setDrawerOpen(false)}
          />
          <div className="fixed right-0 top-0 bottom-0 z-50 w-full sm:w-96 bg-card shadow-2xl flex flex-col">
            <div className="flex items-center justify-between border-b px-4 py-3 shrink-0">
              <h3 className="text-sm font-semibold flex items-center gap-2">
                <Zap className="h-4 w-4 text-amber-500" /> 我的任务
              </h3>
              <button
                onClick={() => setDrawerOpen(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="关闭"
              >
                <XIcon className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              {hasRunning && (
                <>
                  <div className="px-4 py-2 text-xs text-amber-400 font-medium bg-amber-500/5">
                    运行中 ({runningCount})
                  </div>
                  {running.map(t => (
                    <TaskRow key={t.id} task={t} onClick={() => handleClickTask(t)} kind="running" big />
                  ))}
                </>
              )}
              {recentDone.length > 0 && (
                <>
                  <div className="px-4 py-2 text-xs text-blue-400 font-medium bg-blue-500/5">
                    最近完成 ({recentDone.length})
                  </div>
                  {recentDone.map(t => (
                    <TaskRow key={t.id} task={t} onClick={() => handleClickTask(t)}
                      kind={viewedIds.has(t.id) ? 'viewed' : 'done'} big />
                  ))}
                </>
              )}
              {runningCount === 0 && recentDone.length === 0 && (
                <div className="flex-1 flex items-center justify-center p-6 text-sm text-muted-foreground">
                  暂无任务
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}

function TaskRow({ task, onClick, kind, big }: {
  task: TaskSummary;
  onClick: () => void;
  kind: 'running' | 'done' | 'viewed';
  big?: boolean;
}) {
  const percent = Math.min(100, Math.max(0, task.progress_percent || 0));
  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full text-left border-b border-border/30 hover:bg-accent/30 transition-colors flex items-center gap-2',
        big ? 'px-4 py-3' : 'px-3 py-2',
      )}
    >
      <div className="flex-1 min-w-0">
        <div className={cn('font-medium truncate flex items-center gap-1.5', big ? 'text-sm' : 'text-xs')}>
          {task.brand_name || '(未命名品牌)'}
          {kind === 'viewed' && (
            <span className="text-[9px] px-1 py-0.5 rounded bg-muted text-muted-foreground">已查看</span>
          )}
        </div>
        <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
          {kind === 'running' && (
            <>
              {percent}% · {relativeTime(task.started_at || task.queued_at)}
              {task.progress_message && <span className="ml-1 opacity-60">· {task.progress_message}</span>}
            </>
          )}
          {kind === 'done' && (
            <>✓ 已完成 · {relativeTime(task.done_at)}</>
          )}
          {kind === 'viewed' && (
            <>完成 · {relativeTime(task.done_at)}</>
          )}
        </div>
      </div>
      <ArrowRight className="h-3 w-3 text-muted-foreground/50 shrink-0" />
    </button>
  );
}

export default RunningTasksBubble;
