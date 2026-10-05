import { useState, type ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  GOVERNANCE_ALERT_CONTRACT_VERSION,
  type GovernanceAlertAction,
  type GovernanceAlertContract,
} from '@/contracts/governanceAlert';

interface GovernanceAlertProps {
  contract: GovernanceAlertContract;
  /**
   * 动作派发。返回 true 表示"已处理"(用于 dismiss 后本地隐藏);其余交由调用方。
   * retry/nav/contact 这类 app 相关跳转由调用方按 action.id 决定(与 ActionableAlert 同哲学:
   * 组件不硬编码路由)。不传时,除内置 dismiss 外的按钮点击为 no-op。
   */
  onAction?: (action: GovernanceAlertAction) => void | boolean;
  /** 是否允许内置 dismiss 自行隐藏(默认 true)。 */
  dismissible?: boolean;
  className?: string;
  /** 覆盖标题图标。 */
  icon?: ReactNode;
  /**
   * [返修 P2-5] 视觉分级:'notice' = 状态说明/推荐类(中性样式,非必要不警告),
   * 'warning' = 需用户处理但不阻断,'error' = 已失败/被拦。缺省从合同 severity 推断。
   */
  tone?: 'notice' | 'warning' | 'error';
  /**
   * 需要置灰的动作 id（如用户已表过态、等人工核实中，禁止重复提交）。
   * 刻意保留按钮可见而不是抽走 —— 抽走会让用户以为出口消失了。
   */
  disabledActionIds?: string[];
}

/**
 * 治理 §13 用户可见告警渲染器 —— 前端第一个消费"真 §13 合同"
 * ({code,message,reason,impact,repair_hint,actions[].type,rule_version})的组件。
 *
 * 契约铁律:必须至少渲染一个下一步动作(合同构造侧已保证 actions 非空);
 * 只有红码没有下一步的告警在后端就被拒(事故#8)。这里把 reason(为什么)/impact
 * (影响了什么)/repair_hint(怎么修)都显式呈现,不让用户困在死胡同。
 */
export function GovernanceAlert({
  contract,
  onAction,
  dismissible = true,
  className,
  icon,
  tone,
  disabledActionIds,
}: GovernanceAlertProps) {
  const severity = String((contract as { severity?: string }).severity || '');
  const effectiveTone: 'notice' | 'warning' | 'error' =
    tone ?? (severity === 'hard' ? 'error' : severity ? 'warning' : 'notice');
  const toneClass = {
    notice: 'border-border bg-muted/40',
    warning: 'border-amber-400/50 bg-amber-50/60 dark:bg-amber-500/10',
    error: 'border-destructive/40 bg-destructive/5',
  }[effectiveTone];
  const iconClass = {
    notice: 'text-muted-foreground', warning: 'text-amber-600', error: 'text-destructive',
  }[effectiveTone];
  // [P0 崩溃修 2026-07-26] 这两个 useState 之间**不能**有 early return。
  // 原代码在 `if (dismissed) return null` 之后才声明 running：dismissed 一旦翻 true，
  // 这一次 render 只跑了 1 个 hook（上一次是 2 个）→ React 抛
  // "Rendered fewer hooks than expected"，整棵子树白屏。
  // 触发路径正是本组件的主路径：点 dismiss、或 api 动作成功后 setDismissed(true)。
  const [dismissed, setDismissed] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  if (dismissed) return null;

  const handle = (action: GovernanceAlertAction) => {
    const handled = onAction?.(action);
    if (handled) return;

    // 内置:dismiss 类动作在无 caller 处理时自行隐藏,保证"取消/暂不"始终有效。
    if (dismissible && action.type === 'dismiss') {
      setDismissed(true);
      return;
    }

    // 内置:nav 类动作直接跳,不需要每个调用方都写一遍。
    const target = (action as { target?: string }).target;
    if (action.type === 'nav' && target) {
      window.location.assign(target);
      return;
    }

    // 内置:api 类动作真的把接口调掉 —— 否则"出口按钮"点了没反应,
    // 等于把用户从一堵墙引到另一堵墙(§13 的下一步必须真能走通)。
    if (action.type === 'api' && target) {
      const method = ((action as { method?: string }).method || 'POST').toUpperCase();
      setRunning(action.id);
      void (async () => {
        try {
          const { authApi } = await import('@/context/AuthContext');
          const response = method === 'GET'
            ? await authApi.get(target)
            : await authApi.post(target, {});
          const { lazyToast } = await import('@/lib/lazyToast');
          lazyToast.success(String(response?.data?.message || '已处理'));
          setDismissed(true);
        } catch (error) {
          const { apiErrorText } = await import('@/lib/api');
          const { lazyToast } = await import('@/lib/lazyToast');
          lazyToast.error(apiErrorText(error, '操作失败,请稍后重试'));
        } finally {
          setRunning(null);
        }
      })();
    }
  };

  return (
    <div
      role="alert"
      data-governance-contract={GOVERNANCE_ALERT_CONTRACT_VERSION}
      data-governance-code={contract.code}
      data-governance-tone={effectiveTone}
      className={cn('rounded-lg border px-4 py-3 text-sm', toneClass, className)}
    >
      <div className="flex items-start gap-2">
        <span className={cn('mt-0.5 shrink-0', iconClass)} aria-hidden>
          {icon ?? <AlertTriangle className="h-4 w-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-medium text-foreground" data-governance-field="message">
            {contract.message}
          </p>
          {contract.reason && (
            <p className="mt-1 text-xs text-muted-foreground" data-governance-field="reason">
              {contract.reason}
            </p>
          )}
          {contract.impact && (
            <p className="mt-1 text-xs text-muted-foreground" data-governance-field="impact">
              影响：{contract.impact}
            </p>
          )}
          {contract.repair_hint && (
            <p
              className="mt-1 text-xs text-muted-foreground"
              data-governance-field="repair_hint"
              title={String(contract.repair_hint_full || contract.repair_hint)}
            >
              {contract.repair_hint}
            </p>
          )}
          {contract.actions.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {contract.actions.map((action) => (
                <Button
                  key={action.id}
                  size="sm"
                  variant={action.type === 'dismiss' ? 'ghost' : 'outline'}
                  disabled={running === action.id || Boolean(disabledActionIds?.includes(action.id))}
                  onClick={() => handle(action)}
                  data-governance-action={action.id}
                  data-governance-action-type={action.type ?? ''}
                >
                  {action.label}
                </Button>
              ))}
            </div>
          )}
          {/*
            [写作质量总工单 2026-07-29 · D-2] 规则 ID / 版本号(article-finding-v1、
            ad-law-art9-absolute-v9、EV-001 …)对代理零意义,此前平铺在卡片底部,
            属 CLAUDE.md B.6「工程术语全站翻译人话」点名的那类噪音。
            按工单要求:**不是删掉**(排障与复审要用),而是收进折叠区不占主视线。
          */}
          {contract.rule_version && (
            <details className="mt-2" data-governance-field="rule_version">
              <summary className="cursor-pointer text-[10px] text-muted-foreground select-none">
                开发者信息
              </summary>
              <p className="mt-1 text-[10px] text-muted-foreground">
                规则版本 {contract.rule_version}
              </p>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
