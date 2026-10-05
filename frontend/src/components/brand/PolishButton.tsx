/**
 * PolishButton — AI 顾问润色单字段（按 CTO-15.2 接入指南 §7）
 *
 * CTO-13.0 2026-04-19 S1.5 · 每字段旁小按钮触发
 *
 * 行为：
 *   1. 字段有内容才渲染（空字段无法润色）
 *   2. 点击 → POST /api/profiles/:id/polish-field（profile_polish · 静默扣费 · 页面不前置告知额度）
 *   3. 成功 → 弹 Dialog 显示原文 vs 润色后 · 用户选"用新版"或"保留原文"
 *   4. 选用新版 → onAccept(polishedText) 回调
 *
 * 禁止"一键全部润色"（接入指南明确要求）— 所以此组件只处理单字段
 */
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from '@/components/ui/dialog';
import { Sparkles, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { authApi } from '@/context/AuthContext';
import { useConfirmLargeDeduction } from '@/hooks/useConfirmLargeDeduction';

interface Props {
  profileId: string | number;
  field: string;         // e.g. 'business' / 'company_intro'
  currentValue: string;  // 当前字段文字
  onAccept: (polished: string) => void;
  /** 字段标签（用于 Dialog 标题）· 不传用 field key */
  label?: string;
}

export function PolishButton({ profileId, field, currentValue, onAccept, label }: Props) {
  const [loading, setLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [polished, setPolished] = useState('');
  const [advisorName, setAdvisorName] = useState('');
  const confirmLarge = useConfirmLargeDeduction(1000);  // 小额(< 1000)直接执行不弹 toast

  const doPolish = async () => {
    if (!currentValue.trim()) {
      toast.error('字段为空，无需润色');
      return;
    }
    if (loading) return;
    setLoading(true);
    try {
      const res = await authApi.post(`/api/profiles/${profileId}/polish-field`, {
        field,
        text: currentValue,
      }, { timeout: 60000 });
      const d = res.data || {};
      if (d.success && d.polished) {
        setPolished(d.polished);
        setAdvisorName(d.advisor_name || '');
        setDialogOpen(true);
      } else {
        toast.error(d.detail || d.error || '润色失败');
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || e?.message || '润色失败');
    } finally {
      setLoading(false);
    }
  };

  const handleClick = () => {
    // 静默扣费口径(2026-06-03)· confirmLarge 仅大额(>=1000)才弹 · 此处小额直接执行不前置告知额度
    confirmLarge(30, () => { void doPolish(); }, `顾问润色「${label || field}」字段`);
  };

  const handleAccept = () => {
    onAccept(polished);
    setDialogOpen(false);
    toast.success('已替换为润色版');
  };

  return (
    <>
      <button
        type="button"
        onClick={handleClick}
        disabled={loading || !currentValue.trim()}
        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] text-sky-400 hover:text-sky-300 hover:bg-sky-500/10 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        title="AI 顾问润色此字段"
      >
        {loading ? (
          <RefreshCw className="h-2.5 w-2.5 animate-spin" />
        ) : (
          <Sparkles className="h-2.5 w-2.5" />
        )}
        润色
      </button>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <Sparkles className="h-4 w-4 text-amber-400" />
              {advisorName || '顾问'}润色「{label || field}」
            </DialogTitle>
            <DialogDescription className="text-xs">
              对比原文和顾问润色版 · 选你喜欢的一个
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <div className="text-[11px] text-muted-foreground mb-1">原文</div>
              <div className="rounded-lg border border-border/60 bg-muted/30 p-2.5 text-sm whitespace-pre-wrap leading-relaxed">
                {currentValue}
              </div>
            </div>
            <div>
              <div className="text-[11px] text-amber-400 mb-1 flex items-center gap-1">
                <Sparkles className="h-3 w-3" />
                顾问润色版
              </div>
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-2.5 text-sm whitespace-pre-wrap leading-relaxed">
                {polished}
              </div>
            </div>
          </div>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setDialogOpen(false)} className="h-8 text-xs">
              保留原文
            </Button>
            <Button onClick={handleAccept} className="h-8 text-xs bg-amber-600 hover:bg-amber-700 text-white">
              <Sparkles className="h-3 w-3 mr-1" /> 用润色版
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default PolishButton;
