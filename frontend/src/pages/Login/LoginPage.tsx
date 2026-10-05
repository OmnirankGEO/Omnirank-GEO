/**
 * 统一认证页 — 密码登录 / 验证码登录 / 注册
 */
import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { determineTargetRoute } from '@/utils/postLoginRedirect';
import logoDark from '@/assets/logo-dark.png';
import {
  PRIVACY_URL,
  PRIVACY_VERSION,
  USER_TERMS_URL,
  USER_TERMS_VERSION,
} from '@/lib/legalAgreements';
import { savePendingAgreementSession } from '@/lib/agreementSupplement';
import type { AgreementRequirement } from '@/lib/agreementSupplement';

type AuthMode = 'password' | 'sms' | 'register';
type EntryMode = 'geo' | 'social';

const LOGIN_ENTRY_STORAGE_KEY = 'omnirank_login_entry';

function normalizeEntryMode(raw: string | null): EntryMode | null {
  if (!raw) return null;
  const v = raw.trim().toLowerCase();
  if (['social', 's', 'ip', 'social-ip', 'media', 'douyin'].includes(v)) return 'social';
  if (['geo', 'g', 'omnirank', 'rank', 'm3', 'agent'].includes(v)) return 'geo';
  return null;
}

function getInitialEntryMode(searchParams: URLSearchParams): EntryMode {
  const fromUrl = normalizeEntryMode(
    searchParams.get('entry') ||
    searchParams.get('app') ||
    searchParams.get('product') ||
    searchParams.get('source')
  );
  if (fromUrl) return fromUrl;
  try {
    return normalizeEntryMode(localStorage.getItem(LOGIN_ENTRY_STORAGE_KEY)) || 'geo';
  } catch {
    return 'geo';
  }
}

/** 从 API 错误响应中安全提取字符串错误信息（防 Pydantic 422 detail 为数组导致 React error #31） */
function extractErrorMsg(data: Record<string, unknown>, fallback: string): string {
  const raw = data.detail || data.message;
  if (!raw) return fallback;
  if (typeof raw === 'string') return raw;
  if (Array.isArray(raw)) {
    const first = raw[0];
    if (first?.msg) return String(first.msg);
  }
  return fallback;
}

function safeInternalTarget(raw: unknown, fallback = '/'): string {
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return fallback;
  if (raw.includes('\\') || /[\u0000-\u001f]/.test(raw)) return fallback;
  try {
    const parsed = new URL(raw, window.location.origin);
    if (parsed.origin !== window.location.origin || parsed.pathname.startsWith('/login')) return fallback;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}

export default function LoginPage() {
  const [searchParams] = useSearchParams();
  const _locForRef = useLocation();  // 早读 · 用于解析 state.from.search
  const initMode = searchParams.get('mode') === 'register' ? 'register' : 'password';
  // Legacy URL/state attribution is held in component memory only, never browser storage.
  const _fromSearchStr = ((_locForRef.state as any)?.from?.search || '') as string;
  const _fromSearchParams = new URLSearchParams(_fromSearchStr.startsWith('?') ? _fromSearchStr.slice(1) : _fromSearchStr);
  const stateRef = ((_locForRef.state as any)?.inviteCode || '') as string;
  const urlRef = searchParams.get('ref') || _fromSearchParams.get('ref') || '';
  const redirectUrl = safeInternalTarget(searchParams.get('redirect'), '');
  const refCode = urlRef || stateRef;

  const [mode, setMode] = useState<AuthMode>(initMode);
  const [entryMode, setEntryMode] = useState<EntryMode>(() => getInitialEntryMode(searchParams));
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [displayName, setDisplayName] = useState('');
  const [referral, setReferral] = useState(refCode);
  // [WO_REFERRAL_CHAIN §2.1] 扫码/短链归因回显:后端读归因 cookie 返回邀请人显示名。
  // recognized=true 时推荐码免填(注册请求由服务端从同一 cookie 解析,码不经前端)。
  const [attribution, setAttribution] = useState<{ recognized: boolean; inviterName: string | null } | null>(null);
  // 已识别但用户想改用别的推荐码 → 手动展开输入框
  const [overrideReferral, setOverrideReferral] = useState(false);
  const [smsCode, setSmsCode] = useState('');
  const [error, setError] = useState('');
  // 428 协议门禁已触发、但 payload 没能解析成可用 session 时置真:
  // 页面必须显式给出补签入口(工单 §1.3.2 · 锁 3),不能只剩一句红字。
  const [agreementGateBlocked, setAgreementGateBlocked] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const agreementState = searchParams.get('agreement');
    if (agreementState === 'expired') setError('协议确认已超时，请重新验证身份');
    if (agreementState === 'missing') setError('请先验证身份，再确认最新协议');
  }, [searchParams]);

  // 图形验证码
  const [captchaId, setCaptchaId] = useState('');
  const [captchaImage, setCaptchaImage] = useState('');
  const [captchaAnswer, setCaptchaAnswer] = useState('');

  const [agreedToTerms, setAgreedToTerms] = useState(false);
  const [agreedToPrivacy, setAgreedToPrivacy] = useState(false);

  // 短信倒计时
  const [cooldown, setCooldown] = useState(0);

  const { login, loginSms, updateToken } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const fromLocation = (location.state as any)?.from;
  const from = safeInternalTarget(
    fromLocation
      ? `${fromLocation.pathname || '/'}${fromLocation.search || ''}${fromLocation.hash || ''}`
      : '/',
  );

  const persistEntryMode = useCallback((next: EntryMode) => {
    setEntryMode(next);
    try {
      localStorage.setItem(LOGIN_ENTRY_STORAGE_KEY, next);
      // [WO_260] 原先这里还写 `omnirank_c_end_mode`(社媒入口 = 's')给登录后落地用;落地已统一回 `/`
      //   (postLoginRedirect 文件头),那个键只剩废弃的 C 端壳在读,写入一并撤掉。
    } catch { /* ignore */ }
  }, []);

  const resolveLoginTarget = useCallback(async () => {
    if (redirectUrl) return redirectUrl;
    // [WO_260] 社媒入口原先直接落社媒首页;社媒随 E3 删域,入口文案不动,落地与 GEO 入口一致。
    return determineTargetRoute(from);
  }, [redirectUrl, from]);

  const pendingAgreementReturnTo = useCallback(() => {
    if (redirectUrl) return redirectUrl;
    return from;
  }, [redirectUrl, from]);

  /**
   * 落 session 不许把用户的出路一起吞掉。
   * `sessionStorage.setItem` 在隐私模式 / 配额满时会抛,抛了就退到 fail-safe 入口,
   * 而不是让异常冒泡把 handler 打断在半路(那会既不跳转也不显示任何东西)。
   */
  const saveAgreementSessionSafely = useCallback((requirement: AgreementRequirement): boolean => {
    try {
      savePendingAgreementSession(requirement, pendingAgreementReturnTo());
      return true;
    } catch {
      return false;
    }
  }, [pendingAgreementReturnTo]);

  /** 协议门禁统一出口:能带 session 就直接进补签页,带不了也必须留一个可点击入口。 */
  const enterAgreementSupplement = useCallback((requirement?: AgreementRequirement): boolean => {
    if (requirement && saveAgreementSessionSafely(requirement)) {
      navigate('/agreement-update', { replace: true });
      return true;
    }
    return false;
  }, [navigate, saveAgreementSessionSafely]);

  // 清理旧缓存和旧 URL 参数；内存中的 referral state 不受 replaceState 影响。
  useEffect(() => {
    try { localStorage.removeItem('omnirank_locked_ref'); } catch { /* legacy cleanup */ }
    if (urlRef) {
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete('ref');
      window.history.replaceState(window.history.state, '', `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
    }
    if (refCode && !referral) setReferral(refCode);
  }, [urlRef, refCode]);

  // [WO_REFERRAL_CHAIN §2.1] 注册模式下查归因回显(cookie 随同源请求自动携带)。
  // 识别不到 / 请求失败 → 保持 null,走手填推荐码路径,不阻断任何流程。
  useEffect(() => {
    if (mode !== 'register') return;
    let cancelled = false;
    void fetch('/api/auth/register/attribution', { headers: { Accept: 'application/json' } })
      .then(r => (r.ok ? r.json() : null))
      .then(json => {
        if (cancelled || !json?.data) return;
        setAttribution({
          recognized: json.data.recognized === true,
          inviterName: json.data.inviter_display_name || null,
        });
      })
      .catch(() => { /* 静默:识别失败等同未识别 */ });
    return () => { cancelled = true; };
  }, [mode]);

  // 归因已识别、且用户没有主动改码 → 推荐码免填
  const attributionActive = Boolean(attribution?.recognized) && !overrideReferral && !referral.trim();

  useEffect(() => {
    persistEntryMode(entryMode);
  }, [entryMode, persistEntryMode]);

  // 倒计时
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setTimeout(() => setCooldown(c => c - 1), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  const inputCls = 'px-4 py-3 border border-border rounded-md text-sm outline-hidden bg-secondary transition-colors focus:ring-2 focus:ring-brand text-foreground w-full';
  const labelCls = 'text-xs font-semibold text-muted-foreground';

  // ========== 图形验证码 ==========
  const [captchaLoading, setCaptchaLoading] = useState(false);

  const refreshCaptcha = useCallback(async () => {
    setCaptchaLoading(true);
    try {
      const res = await fetch('/api/auth/captcha', { cache: 'no-store' });
      if (!res.ok) throw new Error('captcha failed');
      const data = await res.json();
      setCaptchaId(data.captcha_id || '');
      setCaptchaImage(data.image || '');
      setCaptchaAnswer('');
    } catch {
      setCaptchaImage('');
      setCaptchaId('');
    }
    setCaptchaLoading(false);
  }, []);

  // 注册模式自动加载图形验证码
  useEffect(() => {
    if (mode === 'register') refreshCaptcha();
  }, [mode, refreshCaptcha]);

  // ========== 发送短信验证码 ==========
  const handleSendSms = useCallback(async () => {
    if (!/^1\d{10}$/.test(phone.trim())) {
      setError('请输入正确的手机号');
      return;
    }
    setError('');

    const purpose = mode === 'register' ? 'register' : 'login';
    const body: Record<string, string> = { phone: phone.trim(), purpose };

    // 注册模式需要图形验证码
    if (mode === 'register') {
      if (!captchaAnswer.trim()) {
        setError('请先填写图形验证码');
        return;
      }
      body.captcha_id = captchaId;
      body.captcha_answer = captchaAnswer.trim();
    }

    // 先立即启动倒计时，不等网络响应
    setCooldown(60);

    try {
      const res = await fetch('/api/auth/send-sms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();

      if (!data.success) {
        setCooldown(0); // 发送失败，解锁按钮
        setError(extractErrorMsg(data, '发送失败'));
        if (data.redirect === 'login') setMode('password');
        if (mode === 'register') refreshCaptcha();
      }
    } catch {
      setCooldown(0);
      setError('网络错误');
    }
  }, [phone, mode, captchaId, captchaAnswer, refreshCaptcha]);

  // ========== 密码登录 ==========
  const handlePasswordLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!phone.trim() || !password.trim()) {
      setError('请输入手机号和密码');
      return;
    }
    setError('');
    setAgreementGateBlocked(false);
    setLoading(true);

    const result = await login(phone.trim(), password);
    if (result.success) {
      if (result.mustChangePassword) {
        navigate('/change-password', { replace: true });
      } else {
        // v3.2: 按 agent_level + preferred_mode 智能跳转（C 端 / 代理端）
        const target = await resolveLoginTarget();
        navigate(target, { replace: true });
      }
    } else if (result.requiresAgreement) {
      if (!enterAgreementSupplement(result.agreementRequirement)) {
        // 门禁确实触发了,但 session 没拿到 —— 给入口,不给死路。
        setAgreementGateBlocked(true);
        setError(result.error || '为继续使用，请确认当前《用户服务协议》和《隐私政策》');
      }
    } else {
      setError(result.error || '登录失败');
    }
    setLoading(false);
  };

  // ========== 验证码登录 ==========
  const handleSmsLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!phone.trim() || !smsCode.trim()) {
      setError('请输入手机号和验证码');
      return;
    }
    setError('');
    setAgreementGateBlocked(false);
    setLoading(true);

    const result = await loginSms(phone.trim(), smsCode.trim());
    if (result.success) {
      if (result.mustChangePassword) {
        navigate('/change-password', { replace: true });
      } else {
        // AuthContext 已先从 /api/auth/me 获取 agent_level，再允许决定业务入口。
        const target = await resolveLoginTarget();
        navigate(target, { replace: true });
      }
    } else if (result.requiresAgreement) {
      if (!enterAgreementSupplement(result.agreementRequirement)) {
        // 门禁确实触发了,但 session 没拿到 —— 给入口,不给死路。
        setAgreementGateBlocked(true);
        setError(result.error || '为继续使用，请确认当前《用户服务协议》和《隐私政策》');
      }
    } else {
      setError(result.error || '登录失败');
    }
    setLoading(false);
  };

  // ========== 注册 ==========
  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!phone.trim() || !/^1\d{10}$/.test(phone.trim())) {
      setError('请输入正确的手机号');
      return;
    }
    if (!smsCode.trim()) {
      setError('请输入短信验证码');
      return;
    }
    if (password.length < 6) {
      setError('密码至少 6 位');
      return;
    }
    // 纯邀请制:必须有推荐来源 —— 已识别邀请(归因 cookie · 服务端解析)或手填推荐码,二选一
    if (!referral.trim() && !attribution?.recognized) {
      setError('请输入推荐码（本平台目前为邀请注册），可向邀请你的人索取');
      return;
    }
    // v3.2: 必须勾选协议
    if (!agreedToTerms || !agreedToPrivacy) {
      setError('请分别阅读并同意《用户服务协议》和《隐私政策》');
      return;
    }
    setError('');
    setAgreementGateBlocked(false);
    setLoading(true);

    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone: phone.trim(),
          password,
          sms_code: smsCode.trim(),
          display_name: displayName.trim() || undefined,
          referral_code: referral.trim() || undefined,
          terms_accepted: agreedToTerms,
          privacy_accepted: agreedToPrivacy,
          terms_version: USER_TERMS_VERSION,
          privacy_version: PRIVACY_VERSION,
        }),
      });
      const data = await res.json();

      if (data.success && data.token) {
        await updateToken(data.token);
        localStorage.removeItem('onboarding_state');
        localStorage.removeItem('social_guide_dismissed');
        localStorage.removeItem('omnirank_locked_ref');
        // [CTO-15.3 2026-04-20] 修 bug: 原默认 /social 代理端, L0 新用户错位
        //   走 determineTargetRoute([WO_260] 起各身份统一落首页,见 utils/postLoginRedirect 文件头)
        const target = await resolveLoginTarget();
        window.location.href = target;
        return;
      } else if (res.status === 409) {
        setError('该手机号已注册，请直接登录');
        setMode('password');
      } else {
        setError(extractErrorMsg(data, '注册失败'));
      }
    } catch {
      setError('网络错误');
    }
    setLoading(false);
  };

  // ========== Tab 切换 ==========
  const tabs: { key: AuthMode; label: string }[] = [
    { key: 'password', label: '密码登录' },
    { key: 'sms', label: '验证码登录' },
    { key: 'register', label: '注册' },
  ];

  return (
    <main role="main" data-testid="login-page-main" className="flex justify-center items-center min-h-screen bg-muted px-4">
      <div className="w-full max-w-md bg-card rounded-xl border border-border p-4 sm:p-6 md:p-8">
        {/* Logo */}
        <div className="text-center mb-6">
          <img src={logoDark} alt="全域上榜 OmniRank" className="h-20 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">
            {entryMode === 'social' ? '先写出一条能发的内容' : 'AI 会推荐你吗?'}
          </p>
        </div>

        {/* Tabs */}
        <div className="flex rounded-lg bg-muted p-1 mb-6">
          {tabs.map(t => (
            <button
              key={t.key}
              onClick={() => { setMode(t.key); setError(''); setSmsCode(''); }}
              className={`flex-1 py-2 text-sm font-medium rounded-md transition-colors cursor-pointer border-none ${
                mode === t.key
                  ? 'bg-card text-foreground shadow-sm'
                  : 'bg-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* ===== 密码登录 ===== */}
        {mode === 'password' && (
          <form onSubmit={handlePasswordLogin} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>手机号 / 用户名</label>
              <input
                type="text"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="请输入手机号或用户名"
                autoFocus
                autoComplete="username"
                className={inputCls}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>密码</label>
              <div className="relative">
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="请输入密码"
                  autoComplete="current-password"
                  className={inputCls + ' pr-10'}
                />
                <button type="button" onClick={() => setShowPwd(v => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors bg-transparent border-none cursor-pointer p-0">
                  {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>
            <div className="flex justify-end">
              <button type="button" onClick={() => setMode('sms')}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors bg-transparent border-none cursor-pointer p-0">
                忘记密码？使用验证码登录
              </button>
            </div>
            {error && <ErrorBox msg={error} />}
            {agreementGateBlocked && (
              <AgreementGateExit onGo={() => navigate('/agreement-update')} />
            )}
            <SubmitButton loading={loading} text="登 录" />
          </form>
        )}

        {/* ===== 验证码登录 ===== */}
        {mode === 'sms' && (
          <form onSubmit={handleSmsLogin} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>手机号</label>
              <input
                type="tel"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="请输入手机号"
                maxLength={11}
                autoComplete="tel"
                className={inputCls}
              />
            </div>
            <SmsCodeField
              smsCode={smsCode}
              setSmsCode={setSmsCode}
              cooldown={cooldown}
              onSend={handleSendSms}
              inputCls={inputCls}
              labelCls={labelCls}
            />
            {error && <ErrorBox msg={error} />}
            {agreementGateBlocked && (
              <AgreementGateExit onGo={() => navigate('/agreement-update')} />
            )}
            <SubmitButton loading={loading} text="登 录" />
          </form>
        )}

        {/* ===== 注册 ===== */}
        {mode === 'register' && (
          <form onSubmit={handleRegister} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>手机号</label>
              <input
                type="tel"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="请输入手机号"
                maxLength={11}
                autoComplete="tel"
                className={inputCls}
              />
            </div>

            {/* 图形验证码 */}
            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>图形验证码</label>
              <div className="flex items-center gap-2">
                {captchaLoading ? (
                  <div className="h-[42px] w-[140px] rounded-md bg-muted flex items-center justify-center text-xs text-muted-foreground border border-border shrink-0">
                    加载中...
                  </div>
                ) : captchaImage ? (
                  <div
                    onClick={refreshCaptcha}
                    className="h-[42px] rounded-md cursor-pointer border border-border shrink-0 overflow-hidden"
                    title="点击刷新"
                    dangerouslySetInnerHTML={{ __html: atob(captchaImage.replace('data:image/svg+xml;base64,', '')) }}
                  />
                ) : (
                  <div
                    onClick={refreshCaptcha}
                    className="h-[42px] w-[140px] rounded-md bg-muted flex items-center justify-center text-xs text-muted-foreground cursor-pointer border border-border shrink-0"
                  >
                    点击加载验证码
                  </div>
                )}
                <input
                  type="text"
                  value={captchaAnswer}
                  onChange={e => setCaptchaAnswer(e.target.value)}
                  placeholder="请输入答案"
                  className={inputCls}
                />
              </div>
            </div>

            <SmsCodeField
              smsCode={smsCode}
              setSmsCode={setSmsCode}
              cooldown={cooldown}
              onSend={handleSendSms}
              inputCls={inputCls}
              labelCls={labelCls}
            />

            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>设置密码</label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="至少 6 位"
                autoComplete="new-password"
                className={inputCls}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <label className={labelCls}>
                昵称 <span className="font-normal text-muted-foreground/60">(选填)</span>
              </label>
              <input
                type="text"
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                placeholder="您的昵称"
                className={inputCls}
              />
            </div>

            {/* 推荐码(纯邀请制)· [WO_REFERRAL_CHAIN §2.1/§2.3]
                归因已识别 → 显示邀请人 + 免填(码由服务端解析,不经前端);
                未识别 → 必填输入 + 「向邀请你的人索取」出口,不许只留空框。 */}
            {attributionActive ? (
              <div className="flex flex-col gap-1.5" data-testid="register-attribution-recognized">
                <label className={labelCls}>推荐码</label>
                <div className="flex items-center justify-between gap-2 rounded-md border border-brand/40 bg-brand/5 px-4 py-3">
                  <span className="text-sm text-foreground">
                    已通过 <span className="font-semibold">{attribution?.inviterName || '你的邀请人'}</span> 的邀请进入,注册后自动关联,无需填写推荐码
                  </span>
                  <button
                    type="button"
                    onClick={() => setOverrideReferral(true)}
                    className="shrink-0 text-xs text-muted-foreground underline hover:text-foreground"
                  >
                    改用其他推荐码
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                <label className={labelCls}>
                  推荐码 <span className="font-normal text-brand">（必填）</span>
                </label>
                <input
                  type="text"
                  value={referral}
                  onChange={e => !refCode && setReferral(e.target.value)}
                  readOnly={!!refCode}
                  placeholder="请输入推荐码"
                  autoComplete="off"
                  className={`${inputCls}${refCode ? ' opacity-70 cursor-not-allowed' : ''}`}
                />
                {!refCode && (
                  <p className="text-xs text-muted-foreground">
                    没有推荐码?向邀请你注册的人索取,粘贴到上方即可。
                    {attribution?.recognized && overrideReferral && ' 留空则仍按已识别的邀请关系注册。'}
                  </p>
                )}
              </div>
            )}

            {/* 协议证据分开确认，避免一个总勾选无法证明分别同意。 */}
            <div className="flex items-start gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                id="agree-terms"
                checked={agreedToTerms}
                onChange={e => setAgreedToTerms(e.target.checked)}
                className="mt-0.5 shrink-0 cursor-pointer"
              />
              <label htmlFor="agree-terms" className="cursor-pointer leading-relaxed">
                我已阅读并同意
                <a href={USER_TERMS_URL} target="_blank" rel="noopener noreferrer"
                   className="text-primary hover:underline mx-0.5">《用户服务协议》</a>
                <span className="ml-1 text-muted-foreground/70">({USER_TERMS_VERSION})</span>
              </label>
            </div>
            <div className="flex items-start gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                id="agree-privacy"
                checked={agreedToPrivacy}
                onChange={e => setAgreedToPrivacy(e.target.checked)}
                className="mt-0.5 shrink-0 cursor-pointer"
              />
              <label htmlFor="agree-privacy" className="cursor-pointer leading-relaxed">
                我已阅读并同意
                <a href={PRIVACY_URL} target="_blank" rel="noopener noreferrer"
                   className="text-primary hover:underline mx-0.5">《隐私政策》</a>
                <span className="ml-1 text-muted-foreground/70">({PRIVACY_VERSION})</span>
              </label>
            </div>

            {error && <ErrorBox msg={error} />}
            <SubmitButton loading={loading} text="注 册" />
          </form>
        )}

        <p className="text-center text-xs text-muted-foreground/60 mt-6">
          &copy; 2026 OmniRank &middot; 全域上榜
        </p>
        {/* 页脚备案号:构建时读 VITE_ICP_BEIAN(写在 .env 里,Docker 构建经 docker-compose 的 build.args 传入);不设就不显示 */}
        {(import.meta.env.VITE_ICP_BEIAN || '').trim() && (
          <p className="text-center text-xs text-muted-foreground/60 mt-1">
            <a
              href="https://beian.miit.gov.cn/"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline"
              data-testid="login-icp-beian"
            >
              {import.meta.env.VITE_ICP_BEIAN}
            </a>
          </p>
        )}
      </div>
    </main>
  );
}

// ========== 子组件 ==========

/**
 * 协议门禁的**兜底出口**(工单锁 3)。
 *
 * 只在 428 已确认触发、但补签 session 没能从响应体里取出来时出现。它存在的唯一意义:
 * 无论 payload 形态怎么漂,用户都还有一个可点击的下一步 —— 补签页在没有 session 时
 * 会自助重新验证身份,不会白屏。
 */
function AgreementGateExit({ onGo }: { onGo: () => void }) {
  return (
    <div
      data-testid="agreement-gate-exit"
      className="flex flex-col gap-2 px-3.5 py-3 rounded-xl bg-secondary/60 border border-border"
    >
      <span className="text-xs leading-5 text-muted-foreground">
        你的账号需要先确认最新版《用户服务协议》和《隐私政策》才能继续使用。
        账号、余额、客户和历史数据都不会变化。
      </span>
      <button
        type="button"
        onClick={onGo}
        className="self-start min-h-9 px-4 rounded-lg bg-foreground text-background text-xs font-semibold"
      >
        去确认协议
      </button>
    </div>
  );
}

function ErrorBox({ msg }: { msg: string }) {
  return (
    <div className="px-3.5 py-2.5 rounded-xl bg-red-950/40 text-red-400 text-xs border border-red-900/50">
      {msg}
    </div>
  );
}

function SubmitButton({ loading, text }: { loading: boolean; text: string }) {
  return (
    <button
      type="submit"
      disabled={loading}
      className="py-3.5 rounded-md border-none bg-foreground text-background text-base font-semibold tracking-widest mt-1 transition-all hover:bg-foreground/90 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
    >
      {loading ? '请稍候...' : text}
    </button>
  );
}

function SmsCodeField({
  smsCode, setSmsCode, cooldown, onSend, inputCls, labelCls,
}: {
  smsCode: string;
  setSmsCode: (v: string) => void;
  cooldown: number;
  onSend: () => void;
  inputCls: string;
  labelCls: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className={labelCls}>短信验证码</label>
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={smsCode}
          onChange={e => setSmsCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="请输入6位验证码"
          maxLength={6}
          inputMode="numeric"
          className={inputCls}
        />
        <button
          type="button"
          onClick={onSend}
          disabled={cooldown > 0}
          className="shrink-0 px-4 py-3 rounded-md border border-border bg-muted text-sm text-foreground font-medium cursor-pointer transition-colors hover:bg-muted/70 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {cooldown > 0 ? `${cooldown}s` : '获取验证码'}
        </button>
      </div>
    </div>
  );
}
