/**
 * CustomerIntakeDialog · Dialog 包装 + 同名冲突检测 + 提交协调
 *
 * Phase 06 · CTO-15.23 · 2026-05-03
 *
 * 流程:
 *   1. 用户填字段(CustomerIntakeForm)
 *   2. 点"创建"按钮
 *   3. 本地 validate
 *   4. 调 GET /api/my-clients/check-duplicate?name=xxx 检测同名
 *   5. 命中 → 弹 AlertDialog "复用 / 强制新建"
 *   6. 选"复用" → onSubmit(data, 'reuse', existing_brand_id)
 *   7. 选"强制新建" 或 没命中 → onSubmit(data, 'create')
 *
 * 上层(快速写作)根据 mode 决定后续动作:
 *   - 'create' · POST /api/my-clients/add 创建 brand → upload knowledge → POST /api/writing/projects/quick-create
 *   - 'reuse'  · 直接 POST /api/writing/projects/quick-create with existing brand_id(profile 不覆盖)
 */

import { useState, useCallback, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Loader2 } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { toast } from 'sonner';
import { CustomerIntakeForm, validateQuickWriteIntake } from './CustomerIntakeForm';
import { emitBrandUpdated } from '@/lib/brandProfileEvents';
import type { CustomerIntakeData, FieldBlock, ValidationErrors } from './types';
import { DEFAULT_INTAKE_DATA } from './types';

export type SubmitMode = 'create' | 'reuse';

// [CTO-15.23 2026-05-18 老板报"填好资料下次回来又重新创建"] 草稿 localStorage 持久化
// 关闭弹窗后字段保留 · 下次打开自动恢复 · 提交成功才清空 · 解决"操作很怪"用户体感
const DRAFT_KEY = 'customer-intake-draft-v1';

function loadDraft(): CustomerIntakeData | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // 防垃圾数据:必须是对象且含 name 字段
    if (parsed && typeof parsed === 'object' && 'name' in parsed) {
      return parsed as CustomerIntakeData;
    }
    return null;
  } catch {
    return null;
  }
}

function saveDraft(data: CustomerIntakeData) {
  try {
    // 仅在用户真填过(name 非空)才存 · 防空数据污染
    if (data.name && data.name.trim()) {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(data));
    }
  } catch {
    /* localStorage 满 / disabled · 静默 */
  }
}

function clearDraft() {
  try {
    localStorage.removeItem(DRAFT_KEY);
  } catch { /* noop */ }
}

interface DuplicateInfo {
  brand_id: number;
  name: string;
  industry: string | null;
  city: string | null;
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 显示的字段块 · 顺序 = 数组顺序 */
  blocks: FieldBlock[];
  /** Dialog 标题 */
  title?: string;
  /** Dialog 描述 */
  description?: string;
  /** 提交按钮文案 */
  submitText?: string;
  /** 默认值 */
  defaultValues?: Partial<CustomerIntakeData>;
  /** 提交回调 · 上层根据 mode 决定后续动作(create / reuse) */
  onSubmit: (
    data: CustomerIntakeData,
    mode: SubmitMode,
    existingBrandId?: number,
  ) => Promise<void>;

  // 校验配置(透传给 CustomerIntakeForm)
  showAiFill?: boolean;
  minKeywords?: number;
  maxKeywords?: number;
  keywordsRequired?: boolean;
  complexFields?: boolean;
}

export function CustomerIntakeDialog({
  open,
  onOpenChange,
  blocks,
  title = '快速写作',
  description = '填客户资料 → 自动建档 → 直接开写',
  submitText = '创建客户并开始写作',
  defaultValues,
  onSubmit,
  showAiFill = true,
  minKeywords = 1,
  maxKeywords = 15,
  keywordsRequired = true,
  complexFields = true,
}: Props) {
  const [data, setData] = useState<CustomerIntakeData>({ ...DEFAULT_INTAKE_DATA, ...(defaultValues || {}) });
  const [errors, setErrors] = useState<ValidationErrors>({});
  const [submitting, setSubmitting] = useState(false);

  // 同名冲突状态
  const [dupInfo, setDupInfo] = useState<DuplicateInfo | null>(null);
  const [showDupConfirm, setShowDupConfirm] = useState(false);
  // 2026-05-17 用户在 BasicBlock 选中已有客户时记 brand id · 提交时直接走 reuse 跳过同名检测
  const [selectedBrandId, setSelectedBrandId] = useState<number | null>(null);

  // [CTO-15.23 2026-05-18 修] open 切换时优先恢复 localStorage 草稿(老板报"填了又丢")
  // 1. 上层显式传 defaultValues(prefill 场景) → 直接用 defaultValues 不读草稿
  // 2. 否则 → 尝试读 localStorage · 有就恢复 + toast 提示 · 没有走 DEFAULT_INTAKE_DATA
  useEffect(() => {
    if (!open) return;
    const hasDefaults = defaultValues && Object.keys(defaultValues).length > 0;
    if (hasDefaults) {
      setData({ ...DEFAULT_INTAKE_DATA, ...defaultValues });
    } else {
      const draft = loadDraft();
      if (draft) {
        setData(draft);
        toast.info('已恢复上次未完成的草稿 · 右下角可"清空重填"', { duration: 4000 });
      } else {
        setData({ ...DEFAULT_INTAKE_DATA });
      }
    }
    setErrors({});
    setDupInfo(null);
    setShowDupConfirm(false);
    setSelectedBrandId(null);
  }, [open, defaultValues]);

  // [CTO-15.23 2026-05-18 修] 字段任何变化都写 localStorage · 防关闭后丢失
  useEffect(() => {
    if (!open) return;
    saveDraft(data);
  }, [data, open]);

  const checkDuplicate = useCallback(async (name: string): Promise<DuplicateInfo | null> => {
    try {
      const res = await authApi.get<{ success: boolean; duplicate: DuplicateInfo | null }>(
        `/api/my-clients/check-duplicate`,
        { params: { name } },
      );
      if (res.data.success) {
        return res.data.duplicate;
      }
      return null;
    } catch {
      return null;
    }
  }, []);

  const doSubmit = useCallback(
    async (mode: SubmitMode, existingBrandId?: number) => {
      setSubmitting(true);
      try {
        await onSubmit(data, mode, existingBrandId);
        // T8 · 提交成功后 emit · 让 my-clients 列表 / 其他读取点不 F5 刷新
        // brand_id 在 reuse 模式下用 existingBrandId · create 模式下上层应自行 emit
        // (因为 create 模式 brand_id 由后端创建后才知道 · 上层 onSubmit 内拿到 brand_id 时 emit)
        if (mode === 'reuse' && existingBrandId) {
          emitBrandUpdated(existingBrandId, 'customer-intake-dialog');
        }
        // [CTO-15.23 2026-05-18 修] 提交成功才清草稿 · 防下次打开恢复"已提交的"旧数据
        clearDraft();
        onOpenChange(false);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        toast.error(`提交失败:${msg}`);
      } finally {
        setSubmitting(false);
      }
    },
    [data, onSubmit, onOpenChange],
  );

  const handleSubmitClick = useCallback(async () => {
    // 1. 本地校验
    const errs = validateQuickWriteIntake(data, { keywordsRequired, minKeywords });
    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      toast.warning('请补全必填字段');
      return;
    }
    setErrors({});

    // 2026-05-17 用户在 BasicBlock 已选中已有客户 · 跳过同名检测直接 reuse
    if (selectedBrandId !== null) {
      await doSubmit('reuse', selectedBrandId);
      return;
    }

    // 2. 同名检测
    setSubmitting(true);
    const dup = await checkDuplicate(data.name.trim());
    setSubmitting(false);

    if (dup) {
      setDupInfo(dup);
      setShowDupConfirm(true);
      return;
    }

    // 3. 没命中 · 直接 create
    await doSubmit('create');
  }, [data, keywordsRequired, minKeywords, checkDuplicate, doSubmit, selectedBrandId]);

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>

          <div className="py-2">
            <CustomerIntakeForm
              blocks={blocks}
              data={data}
              onChange={setData}
              errors={errors}
              showAiFill={showAiFill}
              onSelectExisting={setSelectedBrandId}
              minKeywords={minKeywords}
              maxKeywords={maxKeywords}
              keywordsRequired={keywordsRequired}
              complexFields={complexFields}
            />
          </div>

          <DialogFooter className="gap-2 flex-wrap sm:flex-nowrap">
            {/* [CTO-15.23 2026-05-18 修] 清空重填按钮 · 仅在用户填过内容(name 非空)时显示
                老板诉求"填好资料下次回来又重新创建" · 现在草稿持久化 · 用户主动清才重头开始 */}
            {data.name && data.name.trim() && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setData({ ...DEFAULT_INTAKE_DATA, ...(defaultValues || {}) });
                  clearDraft();
                  setSelectedBrandId(null);
                  toast.success('已清空草稿 · 重新开始填');
                }}
                disabled={submitting}
                className="text-muted-foreground hover:text-foreground sm:mr-auto"
              >
                清空重填
              </Button>
            )}
            <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
              取消
            </Button>
            <Button onClick={handleSubmitClick} disabled={submitting}>
              {submitting ? <Loader2 className="h-4 w-4 animate-spin mr-1.5" /> : null}
              {submitText}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 同名冲突确认 */}
      <AlertDialog open={showDupConfirm} onOpenChange={setShowDupConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>已有同名客户</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>系统已有一个同名客户:</p>
                <div className="rounded-md border border-border p-2.5 bg-muted/30 text-sm space-y-0.5">
                  <p><span className="text-muted-foreground">名称:</span> {dupInfo?.name}</p>
                  {dupInfo?.industry && <p><span className="text-muted-foreground">行业:</span> {dupInfo.industry}</p>}
                  {dupInfo?.city && <p><span className="text-muted-foreground">城市:</span> {dupInfo.city}</p>}
                </div>
                <p className="text-xs text-muted-foreground">
                  · 复用 = 用现有客户档案 · 不覆盖原资料 · 仅创建新写作项目
                  <br />
                  · 强制新建 = 允许重名 · 创建独立的新客户档案
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>取消</AlertDialogCancel>
            <Button
              variant="outline"
              onClick={async () => {
                setShowDupConfirm(false);
                await doSubmit('create');
              }}
              disabled={submitting}
            >
              强制新建
            </Button>
            <AlertDialogAction
              onClick={async () => {
                setShowDupConfirm(false);
                if (dupInfo) await doSubmit('reuse', dupInfo.brand_id);
              }}
              disabled={submitting}
            >
              复用现有客户
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
