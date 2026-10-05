/**
 * PendingUserActions —— 发布任务 §13 合同的用户可达面(通用渲染器)。
 *
 * ## 为什么是一个通用渲染器,不是两个专用页
 *
 * 后端两条出口(内容漂移 PUBLISH_CONTENT_DRIFT / 代发卡单
 * PUBLISH_AWAITING_SYNC_UNRESOLVED)的合同**形状完全一致**,而读面
 * `/api/meijiehezi/pending-user-actions` 只按 status='awaiting_action' 过滤、
 * **不看 reject_code**。所以一个渲染器同时覆盖两条,且以后任何新增的 §13 合同
 * 自动就有 UI —— 这才是根治"写了合同没人渲染"的做法。
 *
 * 本文件**没有任何 reject_code 分支**。谁要往里加 if (code === 'XXX'),
 * 就意味着下一条出口又得改一次前端,回到原点。
 *
 * ## 为什么动作必须由合同的 target 驱动
 *
 * 前端绝不另写一份 URL 常量。后端换路径时合同会自己更新;硬编码则会**静默失效** ——
 * 按钮还在、点了 404,比没有按钮更糟(用户以为自己已经处理过了)。
 * 同理 fields[].name 直接作提交体的键,不做映射表。
 *
 * ## 「确认未发布 · 退还算力」是申报,不是退款
 *
 * 后端在这条路径上零退款调用(有断言守死)。媒体方当时回执过"已接收",稿件可能
 * 真发出去了 —— 自动退款 = 既退钱又发稿。用户点它只是**表态**,真正动钱要人工
 * 核实后由管理员执行。所以本组件的文案一律说"等待人工核实",绝不暗示"点了就到账"。
 */
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Inbox, Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { GovernanceAlert } from '@/components/ui/governance-alert';
import type { GovernanceAlertAction } from '@/contracts/governanceAlert';
import {
  usePendingUserActions,
  type PendingUserActionItem,
} from '@/hooks/usePendingUserActions';
// 纯逻辑拆到同目录的 .ts 里 —— 仓库没有前端单测框架，这是唯一能被脚本**真的执行**
// 并断言行为的形态（scripts/test-publication-contract-logic.mjs），而不是静态字符串匹配。
import {
  buildSubmitBody,
  fieldIsMissing,
  messageForStatus,
  normalizeContract,
} from './pendingUserActionsLogic';

async function readDetail(res: Response): Promise<string> {
  try {
    const data = await res.json();
    const detail = data?.detail ?? data?.message;
    if (typeof detail === 'string') return detail;
    // 后端有时把 §13 合同整个塞进 detail
    if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
    return '';
  } catch {
    return '';
  }
}

interface FormState {
  action: GovernanceAlertAction;
  itemId: number;
  values: Record<string, string>;
  errors: Record<string, string>;
}

export function PendingUserActions({ className }: { className?: string }) {
  const { items, loading, error, refresh } = usePendingUserActions();
  const navigate = useNavigate();
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const submit = async (itemId: number, action: GovernanceAlertAction, body: Record<string, string>) => {
    const target = action.target;
    if (!target) return;
    const key = `${itemId}:${action.id}`;
    setBusy(key);
    try {
      const res = await authFetch(target, {
        method: (action.method || 'POST').toUpperCase(),
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json().catch(() => null);
        lazyToast.success(String(data?.message || '已提交'));
        setForm(null);
        await refresh();
        return;
      }
      const { text, stale } = messageForStatus(res.status, await readDetail(res));
      lazyToast.error(text);
      if (stale) {
        setForm(null);
        await refresh();
      }
    } catch {
      lazyToast.error('网络不太稳，请稍后重试');
    } finally {
      setBusy(null);
    }
  };

  /**
   * 动作派发：只按 action.type 分派，绝不按 reject_code 分派。
   * 返回 true = 本组件已处理，GovernanceAlert 不再走它的内置逻辑
   * （内置 api 分支提交空 body，带 fields 的动作会被后端 400 挡回来）。
   */
  const dispatch = (item: PendingUserActionItem, action: GovernanceAlertAction): boolean => {
    const target = action.target;
    if (action.type === 'nav' && target) {
      navigate(target);
      return true;
    }
    if (action.type === 'api' && target) {
      const fields = Array.isArray(action.fields) ? action.fields : [];
      if (fields.length > 0) {
        setForm({
          action,
          itemId: item.item_id,
          values: Object.fromEntries(fields.map((f) => [f.name, ''])),
          errors: {},
        });
        return true;
      }
      void submit(item.item_id, action, {});
      return true;
    }
    return false;
  };

  const cards = useMemo(
    () => items.map((item) => ({ item, contract: normalizeContract(item) })),
    [items],
  );

  if (loading && cards.length === 0) {
    return (
      <div className={cn('flex items-center gap-2 px-1 py-2 text-xs text-muted-foreground', className)}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在看看有没有需要你确认的发布任务…
      </div>
    );
  }
  if (error && cards.length === 0) {
    return (
      <div className={cn('px-1 py-2 text-xs text-muted-foreground', className)}>
        {error}
        <Button variant="link" size="sm" className="h-auto px-1 py-0 text-xs" onClick={() => void refresh()}>
          重试
        </Button>
      </div>
    );
  }
  if (cards.length === 0) return null;

  const activeFields = Array.isArray(form?.action.fields) ? form!.action.fields : [];
  // [2026-07-27 Review-CTO · Owner 生产实测] 已表过态的条目是"等平台人工核实",
  // 不是"等你"——继续算进"待你确认"并保持警告黄框,用户会以为点了没生效。
  const actionableCards = cards.filter(({ item }) => !item.user_exit_claim);
  const claimedCards = cards.filter(({ item }) => Boolean(item.user_exit_claim));

  return (
    <div className={cn('space-y-2', className)} data-testid="pending-user-actions">
      {actionableCards.length > 0 && (
        <div className="flex items-center gap-2 text-sm font-medium text-foreground">
          <Inbox className="h-4 w-4 text-amber-600" />
          {actionableCards.length} 条发布任务待你确认
        </div>
      )}
      {actionableCards.length === 0 && claimedCards.length > 0 && (
        <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <Inbox className="h-4 w-4" />
          {claimedCards.length} 条已提交,平台人工核实中 · 你这边不用再操作
        </div>
      )}

      {cards.map(({ item, contract }) => {
        const claimed = Boolean(item.user_exit_claim);
        const claimedAt = item.user_exit_claim_at
          ? new Date(item.user_exit_claim_at).toLocaleString('zh-CN', { hour12: false })
          : '';
        // 已表过态 → api 动作全部置灰（防重复提交），nav 仍可用（还得让人能去看任务）
        const runningId = busy?.startsWith(`${item.item_id}:`) ? busy.split(':')[1] : '';
        const disabledActionIds = claimed
          ? contract.actions.filter((a) => a.type === 'api').map((a) => a.id)
          : runningId ? [runningId] : [];

        // 已表态 → 从警告黄框降级为一行"处理中"回执:不再催、不再摆动作按钮,
        // 只留一个"查看发布任务"的去处。合同与声明都在库里,admin 核完这行自然消失。
        if (claimed) {
          const navAction = contract.actions.find((a) => a.type === 'nav');
          return (
            <div
              key={item.item_id}
              data-item-id={item.item_id}
              data-contract-code={contract.code}
              data-testid="claimed-row"
              className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
            >
              <span className="font-medium text-foreground">{item.media_name || '发布任务'}</span>
              <span data-testid="claimed-hint">
                已提交，平台人工核实中{claimedAt ? `（${claimedAt}）` : ''}
                {item.user_exit_claim === 'not_published' ? ' · 核实通过后按原扣数退还算力' : ''}
              </span>
              {navAction && (
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto px-1 py-0 text-xs"
                  onClick={() => dispatch(item, navAction)}
                >
                  {navAction.label}
                </Button>
              )}
            </div>
          );
        }

        return (
          <div key={item.item_id} data-item-id={item.item_id} data-contract-code={contract.code}>
            <GovernanceAlert
              tone="warning"
              dismissible={false}
              contract={contract}
              disabledActionIds={disabledActionIds}
              onAction={(action) => dispatch(item, action)}
            />
          </div>
        );
      })}

      <Dialog open={Boolean(form)} onOpenChange={(open) => { if (!open) setForm(null); }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{form?.action.label}</DialogTitle>
            <DialogDescription>填好之后提交，我们会据此把这条发布任务结掉。</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            {activeFields.map((field) => {
              const value = form?.values[field.name] ?? '';
              const err = form?.errors[field.name] || '';
              const Comp = field.type === 'textarea' ? Textarea : Input;
              return (
                <div key={field.name} className="space-y-1">
                  <Label htmlFor={`contract-field-${field.name}`}>
                    {field.label || field.name}
                    {field.required && <span className="ml-0.5 text-destructive">*</span>}
                  </Label>
                  <Comp
                    id={`contract-field-${field.name}`}
                    value={value}
                    placeholder={field.placeholder}
                    onChange={(e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
                      setForm((prev) => prev && ({
                        ...prev,
                        values: { ...prev.values, [field.name]: e.target.value },
                        errors: { ...prev.errors, [field.name]: '' },
                      }))
                    }
                  />
                  {err && <p className="text-xs text-destructive">{err}</p>}
                </div>
              );
            })}
            <div className="space-y-1">
              <Label htmlFor="contract-field-note">补充说明（选填）</Label>
              <Textarea
                id="contract-field-note"
                value={form?.values.note ?? ''}
                placeholder="有什么要告诉我们的，写在这里"
                onChange={(e) =>
                  setForm((prev) => prev && ({ ...prev, values: { ...prev.values, note: e.target.value } }))
                }
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setForm(null)}>取消</Button>
            <Button
              disabled={Boolean(busy)}
              onClick={() => {
                if (!form) return;
                const errors: Record<string, string> = {};
                activeFields.forEach((f) => {
                  const msg = fieldIsMissing(f, form.values[f.name] ?? '');
                  if (msg) errors[f.name] = msg;
                });
                if (Object.keys(errors).length > 0) {
                  setForm({ ...form, errors });
                  return;
                }
                // 提交体的键直接来自合同 fields[].name，外加统一的 note
                void submit(form.itemId, form.action, buildSubmitBody(activeFields, form.values));
              }}
            >
              {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
              提交
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
