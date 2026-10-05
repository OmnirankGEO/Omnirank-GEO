/**
 * TrialPassBanner — 代理试用倒计时条（v3.2 Phase 3）
 *
 * 当 L0 用户有 active 状态的试用 → 顶部显示倒计时
 * 在 旧 C 端布局 的 header 下方显示
 * 剩余 < 1 小时时变红色警告
 */

import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { Clock, Crown, X } from 'lucide-react';

interface TrialData {
  has_active: boolean;
  trial_id?: number;
  status?: string;
  activated_at?: string;
  expires_at?: string;
}

function formatRemaining(ms: number): string {
  if (ms <= 0) return '已过期';
  const h = Math.floor(ms / (60 * 60 * 1000));
  const m = Math.floor((ms % (60 * 60 * 1000)) / (60 * 1000));
  const s = Math.floor((ms % (60 * 1000)) / 1000);
  if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function TrialPassBanner() {
  const [trial, setTrial] = useState<TrialData | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    (async () => {
      try {
        const res = await authFetch('/api/trial-pass/my-active');
        if (res.ok) {
          const data = await res.json();
          setTrial(data);
        }
      } catch {
        // ignore
      }
    })();
  }, []);

  // 倒计时 tick
  useEffect(() => {
    if (!trial?.has_active || trial.status !== 'active') return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [trial]);

  if (dismissed || !trial?.has_active) return null;
  if (trial.status !== 'active') {
    // pending 状态提示
    return (
      <div className="bg-amber-500/10 border-b border-amber-500/20 px-4 py-2 text-xs text-amber-700 dark:text-amber-300 flex items-center justify-between">
        <span className="flex items-center gap-1.5">
          <Clock className="h-3.5 w-3.5" />
          工作台试用申请审核中，请耐心等待审核
        </span>
        <button onClick={() => setDismissed(true)} aria-label="关闭">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  const expiresAt = trial.expires_at ? new Date(trial.expires_at).getTime() : 0;
  const remaining = expiresAt - now;
  const urgent = remaining < 60 * 60 * 1000;  // < 1 小时红色
  const text = formatRemaining(remaining);

  return (
    <div className={`${urgent ? 'bg-red-500/15 border-red-500/30' : 'bg-emerald-500/10 border-emerald-500/20'} border-b px-4 py-2 text-xs flex items-center justify-between`}>
      <span className={`flex items-center gap-1.5 ${urgent ? 'text-red-700 dark:text-red-300' : 'text-emerald-700 dark:text-emerald-300'}`}>
        <Crown className="h-3.5 w-3.5" />
        工作台试用中 · 剩余 <span className="font-mono font-semibold">{text}</span>
        {urgent && <span className="ml-2 font-medium">即将过期，尽快体验完整功能</span>}
      </span>
      <button
        onClick={() => { window.location.href = '/wallet?embedded=true&action=upgrade'; }}
        className="text-primary hover:underline font-medium"
      >
        立即升级为服务商 →
      </button>
    </div>
  );
}
