/**
 * QualityWarningDialog — 品牌资料完善度软拦截 Dialog
 * CTO-15.5 Phase 4 PLAN 05 Task 5.2 (2026-04-20)
 *
 * 触发: 旧 C 端对话页 "出方案"按钮检到 brand.completeness_percent < 60 时弹出
 *
 * 3 按钮:
 *   1. 去补齐 (推荐) — 主按钮 → onGoFillProfile (跳 /my-brand)
 *   2. 用行业公共数据跑 -130 分 — 次按钮 → onConfirmFull (start-task data_mode='l1l2_fallback')
 *      * 根据 /api/geo-plan/industry-l1l2-status?industry=X has_l1=false 时 disabled
 *   3. 取消 — 轻按钮 → onClose
 *
 * 对齐老板元指令:
 *   - 禁出现"合并等待"类第 4 按钮 (Q7 拍板)
 *   - 永远不中断对话: error 走 toast 不 alert
 *   - 资料 <60% 才弹, >=60% 直接 start-task (由 旧 C 端对话页 判断)
 */

import { useEffect, useState } from 'react';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { authFetch } from '@/lib/api';
import { Loader2, Wand2, Database, X as XIcon } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface QualityWarningDialogProps {
  open: boolean;
  onClose: () => void;
  brandId: number;
  brandName: string;
  industry: string | null;
  completenessPercent: number; // 0-100
  onGoFillProfile: () => void;
  onConfirmFull: () => Promise<void> | void;
}

interface L1L2Status {
  has_l1: boolean;
  has_l2: boolean;
  layers_detail?: Record<string, any>;
}

export function QualityWarningDialog({
  open, onClose, brandName, industry, completenessPercent,
  onGoFillProfile, onConfirmFull,
}: QualityWarningDialogProps) {
  const [l1l2, setL1L2] = useState<L1L2Status | null>(null);
  const [l1l2Loading, setL1L2Loading] = useState(false);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (!open) {
      setL1L2(null);
      return;
    }
    if (!industry) {
      setL1L2({ has_l1: false, has_l2: false, layers_detail: { reason: 'industry_unknown' } });
      return;
    }
    setL1L2Loading(true);
    (async () => {
      try {
        const res = await authFetch(`/api/geo-plan/industry-l1l2-status?industry=${encodeURIComponent(industry)}`);
        if (res.ok) {
          const data = await res.json();
          setL1L2({
            has_l1: !!data.has_l1,
            has_l2: !!data.has_l2,
            layers_detail: data.layers_detail || {},
          });
        } else {
          setL1L2({ has_l1: false, has_l2: false, layers_detail: { reason: 'api_error' } });
        }
      } catch {
        setL1L2({ has_l1: false, has_l2: false, layers_detail: { reason: 'network_error' } });
      } finally {
        setL1L2Loading(false);
      }
    })();
  }, [open, industry]);

  const handleConfirmFull = async () => {
    setConfirming(true);
    try {
      await onConfirmFull();
    } finally {
      setConfirming(false);
    }
  };

  const button2Disabled = l1l2Loading || !l1l2?.has_l1 || confirming;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">
            您的品牌资料只填了 {completenessPercent}%
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            建议先补齐「{brandName || '品牌'}」的资料,AI 方案质量会好很多。
            {industry ? ` 当前行业: ${industry}。` : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2 py-2">
          {/* 按钮 1: 去补齐(推荐) */}
          <Button
            className="w-full justify-start gap-2"
            onClick={() => {
              onGoFillProfile();
              onClose();
            }}
          >
            <Wand2 className="h-4 w-4" />
            <span className="flex-1 text-left">去补齐资料</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/15">推荐</span>
          </Button>

          {/* 按钮 2: 用行业公共数据跑 -130 分 */}
          <div className="w-full">
            <Button
              variant="outline"
              className={cn(
                'w-full justify-start gap-2',
                button2Disabled && 'opacity-50 cursor-not-allowed',
              )}
              disabled={button2Disabled}
              onClick={() => void handleConfirmFull()}
              title={
                l1l2Loading
                  ? '查询公共数据可用性...'
                  : !l1l2?.has_l1
                  ? (industry
                      ? `「${industry}」行业暂无公共数据 · 请先补齐品牌资料`
                      : '未填行业 · 无法查询公共数据')
                  : '用行业公共数据跑方案(精度打折)'
              }
            >
              {confirming ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Database className="h-4 w-4" />
              )}
              <span className="flex-1 text-left">
                {confirming ? '创建任务中...' : '用行业公共数据跑'}
              </span>
              <span className="text-[10px] text-amber-400">-130 算力</span>
            </Button>
            {/* 灰掉时的说明行(hover tooltip 不稳,用副标更直观) */}
            {!l1l2Loading && !l1l2?.has_l1 && (
              <div className="mt-1 text-[10px] text-muted-foreground/60 pl-7">
                {industry ? `该行业暂无公共数据` : '未填行业'}
              </div>
            )}
          </div>

          {/* 按钮 3: 取消 */}
          <Button
            variant="ghost"
            className="w-full justify-start gap-2 text-muted-foreground"
            onClick={onClose}
          >
            <XIcon className="h-4 w-4" />
            <span>取消</span>
          </Button>
        </div>

        <DialogFooter>
          <div className="w-full text-[10px] text-muted-foreground/60 text-center">
            基于行业公共数据生成(精度会打折)
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default QualityWarningDialog;
