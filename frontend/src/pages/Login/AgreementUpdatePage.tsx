import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { CheckCircle2, FileText, LogOut, ShieldCheck } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import logoDark from '@/assets/logo-dark.png';
import { useAuth } from '@/context/AuthContext';
import {
  clearPendingAgreementSession,
  loadPendingAgreementSession,
  savePendingAgreementSession,
  type PendingAgreementSession,
} from '@/lib/agreementSupplement';
import { determineTargetRoute } from '@/utils/postLoginRedirect';

export default function AgreementUpdatePage() {
  const navigate = useNavigate();
  const { logout, updateToken } = useAuth();
  const [session, setSession] = useState<PendingAgreementSession | null>(loadPendingAgreementSession);
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [privacyAccepted, setPrivacyAccepted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  // 没有凭证 / 凭证过期 → 就地自助重新验证身份,**不再把用户弹回登录页**。
  // 弹回去正是死循环的那一环:登录页拿不出 session,补签页又要 session。
  const [recoveryReason, setRecoveryReason] = useState<'missing' | 'expired' | null>(
    loadPendingAgreementSession() ? null : 'missing',
  );

  useEffect(() => {
    if (!session) return;
    const timer = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [session]);

  useEffect(() => {
    if (session && now >= session.expires_at) {
      clearPendingAgreementSession();
      setSession(null);
      setRecoveryReason('expired');
    }
  }, [now, session]);

  const adoptSession = (payload: {
    agreement_session_token: string;
    expires_at: number;
    agreements: PendingAgreementSession['agreements'];
  }) => {
    const next: PendingAgreementSession = { ...payload, return_to: '/' };
    try {
      savePendingAgreementSession(payload, '/');
    } catch {
      // 存不进 sessionStorage 也要能签 —— 内存里这一份就够走完本次补签。
    }
    setSession(next);
    setRecoveryReason(null);
    setNow(Math.floor(Date.now() / 1000));
  };

  const documents = useMemo(() => {
    const byType = new Map((session?.agreements || []).map(item => [item.agreement_type, item]));
    return {
      terms: byType.get('user_terms'),
      privacy: byType.get('privacy'),
    };
  }, [session]);
  const secondsLeft = Math.max(0, (session?.expires_at || 0) - now);
  const expired = Boolean(session) && secondsLeft <= 0;

  const leave = () => {
    clearPendingAgreementSession();
    logout();
    navigate('/login', { replace: true });
  };

  const accept = async () => {
    if (!session || !documents.terms || !documents.privacy) {
      setError('协议信息不完整，请重新验证身份');
      return;
    }
    if (!termsAccepted || !privacyAccepted) {
      setError('请分别阅读并勾选两份协议');
      return;
    }
    if (expired) {
      clearPendingAgreementSession();
      setSession(null);
      setRecoveryReason('expired');
      return;
    }

    setSubmitting(true);
    setError('');
    try {
      const response = await fetch('/api/auth/registration-agreements/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agreement_session_token: session.agreement_session_token,
          terms_accepted: true,
          privacy_accepted: true,
          terms_version: documents.terms.agreement_version,
          privacy_version: documents.privacy.agreement_version,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data?.success || !data?.token) {
        const detail = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
        throw new Error(detail || '协议确认失败，请重新验证身份');
      }
      await updateToken(data.token);
      clearPendingAgreementSession();
      const target = data.must_change_password
        ? '/change-password'
        : await determineTargetRoute(session.return_to);
      navigate(target, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : '协议确认失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  if (!session) {
    return (
      <AgreementSessionRecovery
        reason={recoveryReason ?? 'missing'}
        onRecovered={adoptSession}
        onLeave={() => { clearPendingAgreementSession(); logout(); navigate('/login', { replace: true }); }}
      />
    );
  }

  return (
    <main className="min-h-screen bg-muted px-4 py-6 sm:py-10">
      <div className="mx-auto w-full max-w-2xl overflow-hidden rounded-md border border-border bg-card">
        <header className="border-b border-border px-5 py-6 sm:px-8">
          <img src={logoDark} alt="全域上榜 OmniRank" className="h-12 w-auto" />
          <div className="mt-6 flex items-start gap-3">
            <span className="flex size-11 shrink-0 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-400">
              <ShieldCheck className="size-6" aria-hidden="true" />
            </span>
            <div>
              <h1 className="text-xl font-semibold text-foreground sm:text-2xl">确认最新服务协议</h1>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                您的账号、余额、客户、品牌和历史数据不会变化。确认后将按原身份继续使用。
              </p>
            </div>
          </div>
        </header>

        <section className="space-y-4 px-5 py-6 sm:px-8">
          <AgreementChoice
            id="supplement-terms"
            checked={termsAccepted}
            onChange={setTermsAccepted}
            title={documents.terms?.title || '用户服务协议'}
            version={documents.terms?.agreement_version || ''}
            url={documents.terms?.url || '/terms'}
          />
          <AgreementChoice
            id="supplement-privacy"
            checked={privacyAccepted}
            onChange={setPrivacyAccepted}
            title={documents.privacy?.title || '隐私政策'}
            version={documents.privacy?.agreement_version || ''}
            url={documents.privacy?.url || '/privacy'}
          />

          <div className="rounded-md border border-border bg-secondary/50 p-4 text-sm leading-6 text-muted-foreground">
            <div className="flex items-start gap-2">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-400" aria-hidden="true" />
              <span>这里只补充当前协议确认，不会改变账号、余额、客户、品牌或历史数据。</span>
            </div>
          </div>

          {error && (
            <div role="alert" className="rounded-md border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            {expired ? '确认已超时，请返回重新验证身份。' : `本次身份确认还可使用 ${Math.ceil(secondsLeft / 60)} 分钟`}
          </p>

          <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
            <button
              type="button"
              onClick={accept}
              disabled={submitting || expired || !termsAccepted || !privacyAccepted}
              className="min-h-12 rounded-md bg-foreground px-5 font-semibold text-background transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
            >
              {submitting ? '正在确认...' : '同意并继续'}
            </button>
            <button
              type="button"
              onClick={leave}
              className="flex min-h-12 items-center justify-center gap-2 rounded-md border border-border px-5 text-sm text-muted-foreground hover:bg-secondary"
            >
              <LogOut className="size-4" aria-hidden="true" />
              暂不使用并退出
            </button>
          </div>
        </section>
      </div>
    </main>
  );
}

/**
 * 补签凭证自助重取(工单 §1.3.2 · 锁 3)。
 *
 * 这里**不是**第二个登录页:校验成功也只拿到一份 10 分钟的协议确认凭证,拿不到任何
 * 业务权限。它存在的唯一理由,是让"凭证丢了"不再等于"这个账号永远进不来"。
 */
function AgreementSessionRecovery({
  reason,
  onRecovered,
  onLeave,
}: {
  reason: 'missing' | 'expired';
  onRecovered: (payload: {
    agreement_session_token: string;
    expires_at: number;
    agreements: PendingAgreementSession['agreements'];
  }) => void;
  onLeave: () => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError('请输入登录手机号和密码');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/auth/registration-agreements/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data?.success && data?.agreement_session_token) {
        onRecovered({
          agreement_session_token: data.agreement_session_token,
          expires_at: data.expires_at,
          agreements: data.agreements,
        });
        return;
      }
      if (response.ok && data?.code === 'AGREEMENTS_ALREADY_CURRENT') {
        setError(data.message || '该账号的协议已经是最新版本，直接登录即可。');
        return;
      }
      const detail = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
      setError(detail || '身份验证失败，请检查手机号和密码');
    } catch {
      setError('网络异常，请稍后重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen bg-muted px-4 py-6 sm:py-10" data-testid="agreement-session-recovery">
      <div className="mx-auto w-full max-w-md overflow-hidden rounded-md border border-border bg-card">
        <header className="border-b border-border px-5 py-6">
          <img src={logoDark} alt="全域上榜 OmniRank" className="h-10 w-auto" />
          <h1 className="mt-5 text-lg font-semibold text-foreground">请先验证身份</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {reason === 'expired'
              ? '上一次身份确认已超时。重新验证一次即可继续确认协议，账号数据不受影响。'
              : '需要先确认你本人，才能继续确认最新版协议。验证只用于本次协议确认。'}
          </p>
        </header>
        <form onSubmit={submit} className="flex flex-col gap-4 px-5 py-6">
          <input
            type="tel"
            value={username}
            onChange={event => setUsername(event.target.value)}
            placeholder="登录手机号"
            autoComplete="username"
            className="min-h-11 rounded-md border border-border bg-background px-3 text-sm text-foreground"
          />
          <input
            type="password"
            value={password}
            onChange={event => setPassword(event.target.value)}
            placeholder="密码"
            autoComplete="current-password"
            className="min-h-11 rounded-md border border-border bg-background px-3 text-sm text-foreground"
          />
          {error && (
            <div role="alert" className="rounded-md border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={busy}
            className="min-h-11 rounded-md bg-foreground font-semibold text-background disabled:opacity-45"
          >
            {busy ? '正在验证...' : '验证身份并继续'}
          </button>
          <button
            type="button"
            onClick={onLeave}
            className="min-h-11 rounded-md border border-border text-sm text-muted-foreground hover:bg-secondary"
          >
            返回登录页
          </button>
        </form>
      </div>
    </main>
  );
}

function AgreementChoice({
  id,
  checked,
  onChange,
  title,
  version,
  url,
}: {
  id: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  title: string;
  version: string;
  url: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-md border border-border p-4">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={event => onChange(event.target.checked)}
        className="mt-1 size-5 shrink-0 cursor-pointer accent-emerald-500"
      />
      <div className="min-w-0 flex-1">
        <label htmlFor={id} className="block cursor-pointer">
          <span className="flex items-start gap-2 font-medium leading-6 text-foreground">
            <FileText className="mt-1 size-4 shrink-0" aria-hidden="true" />
            <span>我已阅读并同意《{title}》</span>
          </span>
        </label>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="text-muted-foreground">当前版本 {version}</span>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-emerald-400 hover:underline"
          >
            查看全文
          </a>
        </div>
      </div>
    </div>
  );
}
