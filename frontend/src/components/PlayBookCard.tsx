/**
 * PlayBookCard — WJ-45 玩法B 说明卡
 *
 * 新代理首次进工作台时显示,用 4 步图示讲清"这生意怎么跑、钱怎么赚":
 *   ① 你进货(充额度) → ② 用工具服务客户 → ③ 客户线下付钱给你 → ④ 你赚差价
 *
 * 玩法B 红线:平台只提供工具和数据,不碰代理和客户之间的钱(不代运营、不收客户款)。
 * 文案口语化,不裸露成本/毛利/markup 黑话,不露供应商名。
 *
 * 可关闭:✕ 关闭后写 localStorage,下次不再弹。
 */

import { useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Wallet, Wrench, HandCoins, TrendingUp, X, ArrowRight } from 'lucide-react';
import { cn } from '@/lib/utils';

const DISMISS_KEY_PREFIX = 'playbook_card_dismissed';

// 4 步闭环(标题 ≤6 字 · 一句 ≤28 字)
const STEPS = [
  { icon: Wallet, title: '你进货', desc: '先充算力,备好工具' },
  { icon: Wrench, title: '做服务', desc: '用工具给客户做优化' },
  { icon: HandCoins, title: '收客户款', desc: '客户线下把钱付给你' },
  { icon: TrendingUp, title: '赚差价', desc: '收的减进货,就是你赚的' },
];

export function PlayBookCard({ className, userId }: { className?: string; userId?: number | string }) {
  // Codex P1:按 user_id 隔离 dismiss · 一个账号关闭不影响另一代理
  const DISMISS_KEY = `${DISMISS_KEY_PREFIX}_${userId ?? 'anon'}`;
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  if (dismissed) return null;

  const handleDismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, '1');
    } catch {
      /* localStorage 不可用时静默,仅本次会话隐藏 */
    }
    setDismissed(true);
  };

  return (
    <Card className={cn('relative min-w-0 overflow-hidden border-primary/20 bg-primary/5', className)}>
      <CardContent className="p-4 sm:p-5">
        {/* 右上角关闭 */}
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="关闭说明"
          onClick={handleDismiss}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
        >
          <X className="size-4" />
        </Button>

        {/* 标题 */}
        <div className="mb-3 pr-8">
          <h2 className="text-base font-semibold text-foreground">你的生意怎么跑</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">4 步看懂,钱从哪来</p>
        </div>

        {/* 4 步图示 · 手机 2 列 / 桌面 4 列 · 步与步之间带箭头 */}
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4 sm:items-stretch">
          {STEPS.map((step, i) => {
            const Icon = step.icon;
            return (
              <div key={i} className="relative flex min-w-0">
                <div className="min-w-0 flex-1 rounded-xl border border-border bg-card p-3">
                  <div className="flex items-center gap-1.5">
                    <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">
                      {i + 1}
                    </span>
                    <Icon className="size-4 text-primary" />
                  </div>
                  <div className="mt-2 truncate text-sm font-medium text-foreground">{step.title}</div>
                  <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{step.desc}</div>
                </div>
                {/* 桌面端步骤间箭头(最后一步不显示) */}
                {i < STEPS.length - 1 && (
                  <ArrowRight className="absolute -right-[7px] top-1/2 z-10 hidden size-3.5 -translate-y-1/2 text-primary/50 sm:block" />
                )}
              </div>
            );
          })}
        </div>

        {/* 玩法B 红线一句话 */}
        <p className="mt-3 rounded-lg bg-secondary/40 px-3 py-2 text-xs leading-snug text-muted-foreground">
          平台只给你工具和数据,不碰你和客户之间的钱。
        </p>
      </CardContent>
    </Card>
  );
}
