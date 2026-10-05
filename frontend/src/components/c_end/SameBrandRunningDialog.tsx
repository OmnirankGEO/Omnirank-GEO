/**
 * SameBrandRunningDialog — 同品牌已有任务 Dialog
 * CTO-15.5 Phase 4 PLAN 05 Task 5.2 (2026-04-20)
 *
 * 触发: 旧 C 端对话页 调 /api/geo-plan/start-task 拿到 409 same_brand_running
 *
 * 3 按钮 (严禁第 4 按钮"合并等待" · Q7 老板拍板):
 *   1. 查看进度 — 主按钮 → onViewProgress (openInPanel existing task)
 *   2. 取消重跑 — 次按钮 → onCancelAndRestart (cancel + start-task)
 *   3. 关闭 — 轻按钮 → onClose
 */

import { useState } from 'react';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, Eye, RefreshCw, X as XIcon } from 'lucide-react';

export interface SameBrandRunningDialogProps {
  open: boolean;
  onClose: () => void;
  brandId: number;
  brandName: string;
  existingTaskId: number;
  progressPercent: number;
  onViewProgress: () => void;
  onCancelAndRestart: () => Promise<void> | void;
}

export function SameBrandRunningDialog({
  open, onClose, brandName, existingTaskId, progressPercent,
  onViewProgress, onCancelAndRestart,
}: SameBrandRunningDialogProps) {
  const [restarting, setRestarting] = useState(false);

  const handleRestart = async () => {
    setRestarting(true);
    try {
      await onCancelAndRestart();
      onClose();
    } finally {
      setRestarting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base">
            该品牌「{brandName}」已有方案任务在进行 (进度 {progressPercent}%)
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            同一品牌无法并行生成方案 · 任务 #{existingTaskId}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2 py-2">
          {/* 按钮 1: 查看进度 */}
          <Button
            className="w-full justify-start gap-2"
            onClick={() => {
              onViewProgress();
              onClose();
            }}
            disabled={restarting}
          >
            <Eye className="h-4 w-4" />
            <span>查看进度</span>
          </Button>

          {/* 按钮 2: 取消重跑 */}
          <Button
            variant="outline"
            className="w-full justify-start gap-2"
            onClick={() => void handleRestart()}
            disabled={restarting}
          >
            {restarting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            <span>{restarting ? '取消并重跑中...' : '取消重跑'}</span>
          </Button>

          {/* 按钮 3: 关闭 */}
          <Button
            variant="ghost"
            className="w-full justify-start gap-2 text-muted-foreground"
            onClick={onClose}
            disabled={restarting}
          >
            <XIcon className="h-4 w-4" />
            <span>关闭</span>
          </Button>
        </div>

        <DialogFooter>
          <div className="w-full text-[10px] text-muted-foreground/60 text-center">
            取消重跑会立即停止当前任务并创建新任务 · 已消耗的算力会退回
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default SameBrandRunningDialog;
