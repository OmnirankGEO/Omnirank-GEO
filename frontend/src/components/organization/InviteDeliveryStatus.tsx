/**
 * InviteDeliveryStatus — 邀请送达状态徽章 + 失败三动作（W1 提供，W2 挂载）。
 *
 * Props 契约（W2 挂载约定）：
 *   - state:        必填。queued/sending/sent/failed/expired/revoked/superseded，
 *                   与后端 delivery_state 一一对应（owner 侧数据经
 *                   organizationInviteApi.getInviteDeliveryStates 获取）。
 *   - failureCode:  可选。state==='failed' 时的机读错误码（如
 *                   'isv.MOBILE_NUMBER_ILLEGAL'、'MAX_ATTEMPTS:Throttling'、
 *                   'EMAIL_UNAVAILABLE'），人话映射由本组件负责。
 *   - onResend:     可选。提供时渲染「重新发送」；single-flight + loading +
 *                   持久错误由组件内部保证，handler 抛错即展示。
 *   - onRecheck:    可选。提供时渲染「重新核验」（通常=重新拉取 delivery-status）。
 *   - onContactOwner: 可选。提供时渲染「联系团队负责人」。
 *   - hint:         可选补充说明（如邀请过期时间）。
 */
import { useCallback, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Loader2,
  MailWarning,
  MessageSquareText,
  RefreshCw,
  Send,
  ShieldX,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { InviteDeliveryState } from '@/lib/organizationInviteApi';

export interface InviteDeliveryStatusProps {
  state: InviteDeliveryState;
  failureCode?: string | null;
  onResend?: () => void | Promise<void>;
  onRecheck?: () => void | Promise<void>;
  onContactOwner?: () => void;
  hint?: string;
  className?: string;
  /**
   * [A27 2026-08-17] 这个组件被两处复用,而 `superseded` 在两处含义不同:
   *   - 受邀者页(OrganizationInviteAccept)= **验证码**被重新发过;
   *   - 老板邀请列表(OrganizationCenter)= **邀请链接**被重签。
   * 原来固定写「已重新发送新验证码」,老板侧读到的是一句不对的话。
   */
  subject?: 'verification_code' | 'invite_link';
}

const STATE_META: Record<
  InviteDeliveryState,
  { label: string; className: string; Icon: typeof Clock3 }
> = {
  queued: { label: '排队中', className: 'bg-slate-100 text-slate-700 border-slate-200', Icon: Clock3 },
  sending: { label: '发送中', className: 'bg-blue-50 text-blue-700 border-blue-200', Icon: Send },
  sent: { label: '已发送', className: 'bg-emerald-50 text-emerald-700 border-emerald-200', Icon: CheckCircle2 },
  failed: { label: '发送失败', className: 'bg-red-50 text-red-700 border-red-200', Icon: AlertTriangle },
  expired: { label: '已过期', className: 'bg-amber-50 text-amber-700 border-amber-200', Icon: Clock3 },
  revoked: { label: '已撤回', className: 'bg-slate-100 text-slate-600 border-slate-200', Icon: ShieldX },
  superseded: { label: '已重新发送', className: 'bg-slate-100 text-slate-600 border-slate-200', Icon: RefreshCw },
};

function failureCopy(failureCode: string | null | undefined): string {
  const code = String(failureCode || '');
  if (!code) return '短信发送失败，请重新发送；仍失败请联系团队负责人。';
  if (code.includes('MOBILE_NUMBER_ILLEGAL')) return '手机号无效，短信无法送达。请联系团队负责人核对手机号后重新邀请。';
  // [2026-07-23 R2] 公开面归类码（_public_failure_code）映射；owner 侧原文码映射保留在下方。
  if (code === 'PARAM_ERROR') return '手机号或邀请参数无效，短信无法送达。请联系团队负责人核对后重新邀请。';
  if (code === 'RATE_LIMITED') return '请求过于频繁，请稍后再试；仍失败请联系团队负责人。';
  if (code === 'DELIVERY_FAILED') return '短信发送失败，请重新发送；仍失败请联系团队负责人。';
  if (code.startsWith('MAX_ATTEMPTS')) return '多次发送仍未成功（运营商限流或对端异常）。请稍后重新发送；仍失败请联系团队负责人。';
  if (code.includes('EMAIL_UNAVAILABLE')) return '邮箱邀请暂不可用，请团队负责人改用手机号重新邀请。';
  if (code.includes('TEMPLATE') || code.includes('CONFIG') || code.includes('SIGN')) return '平台短信通道配置未完成，请联系平台管理员处理。';
  if (code.includes('AMOUNT_NOT_ENOUGH')) return '平台短信余额不足，请联系平台管理员充值后重新发送。';
  return '短信发送失败，请重新发送；仍失败请联系团队负责人。';
}

export default function InviteDeliveryStatus({
  state,
  failureCode,
  onResend,
  onRecheck,
  onContactOwner,
  hint,
  className,
  subject = 'verification_code',
}: InviteDeliveryStatusProps) {
  const [busyAction, setBusyAction] = useState<'resend' | 'recheck' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  const runAction = useCallback(
    (action: 'resend' | 'recheck', handler?: () => void | Promise<void>) => {
      if (!handler || inFlightRef.current) return;
      inFlightRef.current = true;
      setBusyAction(action);
      setActionError(null);
      void (async () => {
        try {
          await handler();
        } catch (caught) {
          setActionError(caught instanceof Error ? caught.message : '操作失败，请稍后重试。');
        } finally {
          inFlightRef.current = false;
          setBusyAction(null);
        }
      })();
    },
    [],
  );

  const meta = STATE_META[state] || STATE_META.queued;
  const { Icon } = meta;
  const busy = busyAction !== null;

  return (
    <div className={className} data-testid="invite-delivery-status" data-state={state}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">送达状态</span>
        <Badge variant="outline" className={meta.className} data-testid="invite-delivery-badge">
          <Icon className="mr-1 h-3.5 w-3.5" />
          {meta.label}
        </Badge>
      </div>

      {state === 'failed' && (
        <p className="mt-2 flex items-start gap-1.5 text-sm text-red-700" data-testid="invite-delivery-failure">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{failureCopy(failureCode)}</span>
        </p>
      )}
      {state === 'superseded' && (
        <p className="mt-2 text-sm text-muted-foreground">
          {subject === 'invite_link'
            ? '已重新生成邀请链接，请把最新的链接发给员工，之前那条已失效。'
            : '已重新发送新验证码，请使用最新收到的验证码。'}
        </p>
      )}
      {state === 'expired' && (
        <p className="mt-2 text-sm text-amber-700">
          {subject === 'invite_link'
            ? '邀请已过期，请重新发一份；仍不可用请联系平台客服。'
            : '邀请或验证码已过期，请重新发送；仍不可用请联系团队负责人。'}
        </p>
      )}
      {state === 'revoked' && (
        <p className="mt-2 text-sm text-muted-foreground">邀请已失效。请联系团队负责人重新邀请。</p>
      )}
      {hint && <p className="mt-2 text-xs text-muted-foreground">{hint}</p>}

      {(onResend || onRecheck || onContactOwner) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {onResend && (
            <Button
              size="sm"
              variant={state === 'failed' || state === 'expired' ? 'default' : 'outline'}
              disabled={busy}
              onClick={() => runAction('resend', onResend)}
              data-testid="invite-delivery-resend"
            >
              {busyAction === 'resend' && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              重新发送
            </Button>
          )}
          {onRecheck && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => runAction('recheck', onRecheck)}
              data-testid="invite-delivery-recheck"
            >
              {busyAction === 'recheck' && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              重新核验
            </Button>
          )}
          {onContactOwner && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={onContactOwner}
              data-testid="invite-delivery-contact-owner"
            >
              <MessageSquareText className="mr-1.5 h-3.5 w-3.5" />
              联系团队负责人
            </Button>
          )}
        </div>
      )}

      {actionError && (
        <p role="alert" className="mt-2 flex items-start gap-1.5 text-sm text-destructive" data-testid="invite-delivery-action-error">
          <MailWarning className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{actionError}</span>
        </p>
      )}
    </div>
  );
}

// W2 挂载契约：命名导出与默认导出指向同一组件
export { InviteDeliveryStatus };
