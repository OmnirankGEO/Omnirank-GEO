/**
 * ModeSelectDialog — 首次登录 C 端模式选择（Phase 1 · CTO-13.0 2026-04-19）
 *
 * 触发条件：localStorage 无 'omnirank_c_end_mode' 值且用户已登录
 * 位置：旧 C 端布局 和 SEndLayout 顶层 mount 时检测并弹出
 * 完成后：写 localStorage → 如果选的和当前端不同则 navigate
 */
import { useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { Loader2 } from 'lucide-react';

export type CEndMode = 'c' | 's';

interface ModeSelectDialogProps {
  open: boolean;
  onSelect: (mode: CEndMode) => void;
  /** 用户点×关闭时触发（不提供则×无效果保持原行为） */
  onClose?: () => void;
}

const OPTIONS: {
  mode: CEndMode;
  emoji: string;
  title: string;
  slogan: string;
  desc: string;
}[] = [
  {
    mode: 'c',
    emoji: '🔍',
    title: 'AI 搜索优化',
    slogan: '搜得到',
    desc: '让你的品牌在 DeepSeek / 豆包 / Kimi / 通义 里被 AI 推荐',
  },
  {
    mode: 's',
    emoji: '🎬',
    title: '短视频创作',
    slogan: '刷得到',
    desc: 'AI 帮你写抖音/小红书/视频号脚本，30 秒就能开干',
  },
];

export function ModeSelectDialog({ open, onSelect, onClose }: ModeSelectDialogProps) {
  const [submitting, setSubmitting] = useState<CEndMode | null>(null);

  const handleSelect = (mode: CEndMode) => {
    if (submitting) return;
    setSubmitting(mode);
    // 延迟一点让用户看到选中态
    setTimeout(() => onSelect(mode), 200);
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose?.(); }}>
      <DialogContent
        className="max-w-2xl p-6"
        // 点击外部和 ESC 仍不可关闭（强制选择），× 按钮走 onClose
        onEscapeKeyDown={(e) => e.preventDefault()}
        onPointerDownOutside={(e) => e.preventDefault()}
      >
        <DialogHeader className="space-y-2">
          <DialogTitle className="text-center text-xl">
            欢迎来到 OmniRank · 全域上榜
          </DialogTitle>
          <DialogDescription className="text-center text-sm">
            你今天主要想做哪一个？（以后可以在右上角随时切换）
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4">
          {OPTIONS.map(opt => (
            <button
              key={opt.mode}
              onClick={() => handleSelect(opt.mode)}
              disabled={!!submitting}
              className={cn(
                'relative flex flex-col items-center gap-3 p-6 rounded-xl border-2',
                'text-center transition-all',
                submitting === opt.mode
                  ? 'border-foreground bg-accent'
                  : 'border-border hover:border-foreground/50 hover:bg-accent/30',
                submitting && submitting !== opt.mode && 'opacity-40',
              )}
            >
              <span className="text-4xl">{opt.emoji}</span>
              <div>
                <div className="text-base font-semibold">{opt.title}</div>
                <div className="text-xs text-muted-foreground mt-0.5">{opt.slogan}</div>
              </div>
              <p className="text-xs text-muted-foreground leading-relaxed">
                {opt.desc}
              </p>
              {submitting === opt.mode && (
                <Loader2 className="absolute top-3 right-3 h-4 w-4 animate-spin" />
              )}
            </button>
          ))}
        </div>

        <p className="text-[11px] text-muted-foreground text-center mt-4">
          都想用也没关系 — 选一个开始，另一个右上角一点就到。
        </p>
      </DialogContent>
    </Dialog>
  );
}
