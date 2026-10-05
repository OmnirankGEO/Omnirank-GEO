import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, ArrowRight, CheckCircle2, Loader2, MailWarning, ShieldCheck, UsersRound } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import InviteDeliveryStatus from '@/components/organization/InviteDeliveryStatus';
import {
  OrganizationApiError,
  organizationRequest,
  requestId,
} from '@/lib/organizationApi';
import {
  createVerificationChallenge,
  getChallengeDeliveryStatus,
  inspectPublicInvite,
  onboardInviteOperator,
  onboardInviteOperatorCredential,
  verifyInviteChallenge,
  type ChallengeDeliveryStatus,
  type InviteChallenge,
  type InviteVerification,
  type PublicInviteInspect,
} from '@/lib/organizationInviteApi';

// [F-3 · Owner 裁决]操作员开户密码最短长度。**必须与后端唯一权威常量一致**
// (services/organization_onboarding.MIN_OPERATOR_PASSWORD_LENGTH = 6),
// 否则会出现「前端放行、后端 422」的死胡同。
const MIN_OPERATOR_PASSWORD_LENGTH = 6;

interface ExistingInvitationStatus {
  status: 'pending' | 'accepted' | 'revoked' | 'expired';
  organization_id: number;
  organization_name: string;
  role_name: string;
  target_kind: 'phone' | 'email';
  expires_at: string;
  account_matched: boolean;
  target_verified: boolean;
  can_accept: boolean;
  blocker_code?: string | null;
}

const blockerCopy: Record<string, string> = {
  ORG_INVITE_ACCEPTED: '这份邀请已经被接受，不能重复使用。',
  ORG_INVITE_REVOKED: '邀请已由团队创建者撤回。',
  ORG_INVITE_EXPIRED: '邀请已经过期，请联系团队创建者重新发送。',
  ORG_INACTIVE: '该团队当前不可加入。',
  ORG_INVITEE_TARGET_UNVERIFIED: '当前账号尚未验证邀请指定的手机号或邮箱。',
  ORG_USER_ALREADY_MEMBER_THIS_ORG: '当前账号已经加入这个团队。',
  ORG_USER_ALREADY_MEMBER_OTHER_ORG: '当前账号已属于其他团队，不能跨团队接受邀请。',
  ORG_SEAT_LIMIT_REACHED: '团队员工席位已用完，请联系团队创建者调整席位。',
  ORG_INVITE_ROLE_CHANGED: '邀请角色已变化，请团队创建者重新发送邀请。',
  ORG_INVITE_CAPABILITIES_CHANGED: '邀请权限已变化，请团队创建者重新发送邀请。',
};

function messageFor(error: unknown): string {
  if (error instanceof OrganizationApiError) {
    if (error.detail.code === 'ORG_INVITEE_TARGET_MISMATCH') return '当前登录账号与邀请指定的手机号/邮箱不一致，请换个账号登录后重试。';
    if (error.detail.code === 'ORG_INVITEE_ACCOUNT_EXISTS') return '该联系方式已有账号，请用已有账号登录。';
    if (error.detail.code === 'ORG_PRODUCT_CONFIG_GOVERNANCE_ONLY') return '员工席位功能待平台开通，请联系平台客服。';
    if (error.detail.code === 'ORG_INVITE_EMAIL_UNAVAILABLE') return '邮箱邀请暂不可用，请让老板改用手机号邀请你。';
    if (error.detail.code === 'ORG_INVITE_DELIVERY_NOT_CONFIGURED') return '短信验证暂不可用，请联系老板或平台客服。';
    // [B8 2026-08-17] 这里原来是无条件 `return error.detail.message` —— 后端任何未汉化
    //   的 message(含 FastAPI 默认英文)都直接上屏。现在 message 已在
    //   `organizationApi.responseDetail` 统一过滤:非中文 / 夹带变量名的一律换成
    //   按 code 的中文兜底,所以这里透传的**一定**是中文人话。
    return error.detail.message || '邀请处理失败，请稍后重试。';
  }
  return error instanceof Error ? error.message : '邀请处理失败，请稍后重试。';
}

export default function OrganizationInviteAccept() {
  const navigate = useNavigate();
  const { user, login, updateToken } = useAuth();
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const tokenRef = useRef(fragment.get('token') || '');
  const queryTokenRejectedRef = useRef(new URLSearchParams(window.location.search).has('token'));
  const generationRef = useRef(0);
  const inspectRequestRef = useRef(requestId('public-invite-inspect'));
  const acceptRequestRef = useRef(requestId('accept-invite'));
  const challengeRequestRef = useRef(requestId('invite-code'));
  const verifyRequestRef = useRef(requestId('verify-invite-code'));
  const onboardRequestRef = useRef(requestId('onboard-invite-operator'));
  const deliveryRequestRef = useRef(requestId('invite-delivery-status'));
  const autoPollRef = useRef(0);

  const [publicStatus, setPublicStatus] = useState<PublicInviteInspect | null>(null);
  const [existingStatus, setExistingStatus] = useState<ExistingInvitationStatus | null>(null);
  const [challenge, setChallenge] = useState<InviteChallenge | null>(null);
  const [delivery, setDelivery] = useState<ChallengeDeliveryStatus | null>(null);
  const [verification, setVerification] = useState<InviteVerification | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [contactNotice, setContactNotice] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [loginPassword, setLoginPassword] = useState('');
  const [code, setCode] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [privacyAccepted, setPrivacyAccepted] = useState(false);

  const inspectPublic = useCallback(async () => {
    const token = tokenRef.current;
    const generation = ++generationRef.current;
    if (!token) {
      setError('邀请链接不完整，请重新打开老板发给你的完整链接。');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const next = await inspectPublicInvite(token, inspectRequestRef.current);
      if (generation !== generationRef.current) return;
      setPublicStatus(next);
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  }, []);

  const inspectExisting = async () => {
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const next = await organizationRequest<ExistingInvitationStatus>('/invites/inspect', {
        method: 'POST',
        body: JSON.stringify({ token: tokenRef.current, request_id: requestId('inspect-existing-invite') }),
      });
      if (generation !== generationRef.current) return;
      setExistingStatus(next);
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  useEffect(() => {
    // Bearer stays only in component memory. It never enters query strings,
    // storage, analytics or referrers.
    if (tokenRef.current || queryTokenRejectedRef.current) window.history.replaceState(window.history.state, '', '/organization/invite');
    void inspectPublic();
    return () => { generationRef.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (publicStatus?.account_mode === 'sign_in' && user && !existingStatus) void inspectExisting();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publicStatus?.account_mode, user?.id]);

  const recheckDelivery = useCallback(async () => {
    if (!challenge) return;
    const next = await getChallengeDeliveryStatus(
      challenge.challenge_id,
      tokenRef.current,
      deliveryRequestRef.current,
    );
    if (generationRef.current <= 0) return;
    setDelivery(next);
  }, [challenge]);

  //  queued/sending 时低频自动轮询送达状态（最多 6 次，人工可继续重新核验）
  useEffect(() => {
    if (!challenge || verification) return undefined;
    const state = delivery?.delivery_state;
    if (state && state !== 'queued' && state !== 'sending') return undefined;
    if (autoPollRef.current >= 6) return undefined;
    const timer = window.setTimeout(() => {
      autoPollRef.current += 1;
      void recheckDelivery().catch(() => undefined);
    }, 5000);
    return () => window.clearTimeout(timer);
  }, [challenge, delivery, verification, recheckDelivery]);

  const signInHere = async () => {
    if (busy || !username.trim() || !loginPassword) return;
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const result = await login(username.trim(), loginPassword);
      if (generation !== generationRef.current) return;
      if (!result.success) {
        if (result.requiresAgreement) throw new Error('该账号需要先完成协议更新，请在新标签页登录处理后再回到本页。');
        throw new Error(result.error || '登录失败');
      }
      if (result.mustChangePassword) throw new Error('该账号需要先修改初始密码，请在新标签页处理后再回到本页。');
      await inspectExisting();
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  const acceptExisting = async () => {
    if (!existingStatus?.can_accept || busy) return;
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      await organizationRequest('/invites/accept', {
        method: 'POST',
        body: JSON.stringify({ token: tokenRef.current, request_id: acceptRequestRef.current }),
      });
      if (generation !== generationRef.current) return;
      setAccepted(true);
      window.dispatchEvent(new CustomEvent('organization-authority-changed'));
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  const sendVerification = async () => {
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const next = await createVerificationChallenge(tokenRef.current, challengeRequestRef.current);
      if (generation !== generationRef.current) return;
      setChallenge(next);
      setDelivery(null);
      autoPollRef.current = 0;
      if (next.test_code) setCode(next.test_code);
      // 立即查一次送达状态（通常为排队中）
      try {
        const status = await getChallengeDeliveryStatus(next.challenge_id, tokenRef.current, deliveryRequestRef.current);
        if (generation === generationRef.current) setDelivery(status);
      } catch {
        // 状态查询失败不阻断；可手动重新核验
      }
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
      throw caught;
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  const resendVerification = async () => {
    // 新 request_id = 服务端作废旧 challenge/旧 outbox 并签发新验证码。
    // 注意：不在飞行中清空 challenge——否则状态组件卸载、single-flight ref
    // 丢失，连击会绕过守卫发出第二次请求。
    challengeRequestRef.current = requestId('invite-code');
    setVerification(null);
    setCode('');
    await sendVerification();
  };

  const verifyCode = async () => {
    if (!challenge || !/^[0-9]{6}$/.test(code)) return;
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const next = await verifyInviteChallenge(challenge.challenge_id, tokenRef.current, code, verifyRequestRef.current);
      if (generation !== generationRef.current) return;
      setVerification(next);
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  const createOperator = async () => {
    if (!publicStatus || !challenge || !verification || busy) return;
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const created = await onboardInviteOperator({
        token: tokenRef.current,
        challenge_id: challenge.challenge_id,
        verification_receipt: verification.verification_receipt,
        password: newPassword,
        display_name: displayName.trim(),
        request_id: onboardRequestRef.current,
        terms_accepted: termsAccepted,
        privacy_accepted: privacyAccepted,
        terms_version: publicStatus.agreements.user_terms_version,
        privacy_version: publicStatus.agreements.privacy_version,
      });
      if (generation !== generationRef.current) return;
      if (created.auto_login && created.token) {
        // 开户即登录：直接使用服务端签发的 JWT
        await updateToken(created.token);
      } else {
        const signedIn = await login(created.login_username, newPassword);
        if (generation !== generationRef.current) return;
        if (!signedIn.success) throw new Error('员工账号已创建并加入团队，请使用邀请联系方式和刚设置的密码登录。');
      }
      if (generation !== generationRef.current) return;
      setAccepted(true);
      window.dispatchEvent(new CustomEvent('organization-authority-changed'));
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  // [WP6] 用户名式邀请:owner 已设登录用户名,被邀请人凭 token 直接自设密码开户,不走短信。
  const createOperatorCredential = async () => {
    if (!publicStatus || busy) return;
    const generation = ++generationRef.current;
    setBusy(true);
    setError(null);
    try {
      const created = await onboardInviteOperatorCredential({
        token: tokenRef.current,
        password: newPassword,
        display_name: displayName.trim(),
        request_id: onboardRequestRef.current,
        terms_accepted: termsAccepted,
        privacy_accepted: privacyAccepted,
        terms_version: publicStatus.agreements.user_terms_version,
        privacy_version: publicStatus.agreements.privacy_version,
      });
      if (generation !== generationRef.current) return;
      if (created.auto_login && created.token) {
        await updateToken(created.token);
      } else {
        const signedIn = await login(created.login_username, newPassword);
        if (generation !== generationRef.current) return;
        if (!signedIn.success) throw new Error('员工账号已创建并加入团队，请使用团队负责人给你的用户名和刚设置的密码登录。');
      }
      if (generation !== generationRef.current) return;
      setAccepted(true);
      window.dispatchEvent(new CustomEvent('organization-authority-changed'));
    } catch (caught) {
      if (generation === generationRef.current) setError(messageFor(caught));
    } finally {
      if (generation === generationRef.current) setBusy(false);
    }
  };

  const emailUnavailable = publicStatus?.target_kind === 'email';
  const isUsernameInvite = publicStatus?.target_kind === 'username';

  return (
    <main className="mx-auto grid min-h-screen w-full max-w-3xl place-items-center p-4 sm:p-8" data-testid="organization-invite-accept">
      <Card className="w-full max-w-xl overflow-hidden">
        <CardHeader className="border-b bg-muted/25">
          <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary"><UsersRound className="h-6 w-6" /></div>
          <CardTitle>接受团队邀请</CardTitle>
          <CardDescription>
            不用先注册：验证手机号后就能直接加入团队。员工账号只用于在团队里干活，费用和客户都归老板；员工账号没有推荐奖励、分成或提现功能。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5 p-5 sm:p-7">
          {loading ? (
            <div className="grid min-h-44 place-items-center text-sm text-muted-foreground"><Loader2 className="mb-2 h-6 w-6 animate-spin" />正在核验团队邀请</div>
          ) : accepted ? (
            <div className="space-y-4 text-center">
              <CheckCircle2 className="mx-auto h-12 w-12 text-emerald-500" />
              <div><h2 className="font-semibold">已成功加入 {publicStatus?.organization_name}</h2><p className="mt-1 text-sm text-muted-foreground">现在能看到老板分配给你的客户，和你自己做的内容。</p></div>
              <Button onClick={() => navigate('/organization/team')} className="w-full">进入团队中心 <ArrowRight className="ml-1 h-4 w-4" /></Button>
            </div>
          ) : publicStatus ? (
            <>
              <div className="space-y-3 rounded-xl border p-4 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-muted-foreground">团队</span><span className="break-words text-right font-medium">{publicStatus.organization_name}</span></div>
                <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">员工角色</span><Badge variant="secondary">{publicStatus.role_name}</Badge></div>
                <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">{publicStatus.target_kind === 'username' ? '登录方式' : '联系方式'}</span><span>{publicStatus.target_kind === 'phone' ? '邀请指定的手机号（出于保护未显示）' : publicStatus.target_kind === 'email' ? '邀请指定的邮箱（出于保护未显示）' : '老板为你设定的登录用户名'}</span></div>
                <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">有效期至</span><span>{new Date(publicStatus.expires_at).toLocaleString('zh-CN', { hour12: false })}</span></div>
              </div>

              {emailUnavailable ? (
                <div className="space-y-3 rounded-xl border border-amber-200 bg-amber-50 p-4" data-testid="email-invite-unavailable">
                  <p className="flex items-start gap-2 text-sm text-amber-800"><MailWarning className="mt-0.5 h-4 w-4 shrink-0" />邮箱邀请暂不可用。请团队负责人改用手机号重新邀请你；短信验证后无需预先注册即可加入。</p>
                  <Button size="sm" variant="outline" onClick={() => setContactNotice('请直接联系发送邀请链接给你的团队负责人，请其改用手机号重新邀请。')}>联系团队负责人</Button>
                </div>
              ) : publicStatus.account_mode === 'sign_in' ? (
                existingStatus ? (
                  <div className="space-y-4">
                    <div className="rounded-lg border p-3 text-sm">
                      <p>{existingStatus.account_matched && existingStatus.target_verified ? '当前账号与已验证联系方式匹配。' : '当前账号尚未通过联系方式核验。'}</p>
                      {!existingStatus.can_accept && <p className="mt-2 text-amber-700">{blockerCopy[existingStatus.blocker_code || ''] || '当前账号暂不能接受邀请。'}</p>}
                    </div>
                    <Button className="w-full" disabled={!existingStatus.can_accept || busy} onClick={() => void acceptExisting()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}接受团队邀请</Button>
                  </div>
                ) : user ? (
                  <Button className="w-full" disabled={busy} onClick={() => void inspectExisting()}>核验当前登录账号</Button>
                ) : (
                  <div className="space-y-4 rounded-xl border p-4">
                    <p className="text-sm text-muted-foreground">该联系方式已有账号，登录后即可接受邀请。</p>
                    <div><Label htmlFor="invite-login">已有账号</Label><Input id="invite-login" autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="手机号、邮箱或用户名" /></div>
                    <div><Label htmlFor="invite-login-password">密码</Label><Input id="invite-login-password" type="password" autoComplete="current-password" value={loginPassword} onChange={event => setLoginPassword(event.target.value)} /></div>
                    <Button className="w-full" disabled={busy || !username.trim() || !loginPassword} onClick={() => void signInHere()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}登录并核验邀请</Button>
                  </div>
                )
              ) : isUsernameInvite ? (
                <div className="space-y-4 rounded-xl border p-4" data-testid="username-onboard-form">
                  <p className="text-sm text-muted-foreground">团队负责人已为你设置好登录用户名，你只需填写姓名并设置登录密码，即可开户并自动登录。</p>
                  <div><Label htmlFor="operator-name-u">你的姓名</Label><Input id="operator-name-u" autoComplete="name" maxLength={80} value={displayName} onChange={event => setDisplayName(event.target.value)} /></div>
                  <div><Label htmlFor="operator-password-u">设置登录密码</Label><Input id="operator-password-u" type="password" autoComplete="new-password" minLength={MIN_OPERATOR_PASSWORD_LENGTH} maxLength={128} value={newPassword} onChange={event => setNewPassword(event.target.value)} /><p className="mt-1 text-xs text-muted-foreground">至少 {MIN_OPERATOR_PASSWORD_LENGTH} 位。这个账号只用于在该团队内干活，创建后会自动登录。</p></div>
                  <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={termsAccepted} onChange={event => setTermsAccepted(event.target.checked)} /><span>我已阅读并同意 <a className="underline" href="/terms" target="_blank" rel="noreferrer">用户协议</a></span></label>
                  <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={privacyAccepted} onChange={event => setPrivacyAccepted(event.target.checked)} /><span>我已阅读并同意 <a className="underline" href="/privacy" target="_blank" rel="noreferrer">隐私政策</a></span></label>
                  <Button className="w-full" data-testid="username-onboard-submit" disabled={busy || !displayName.trim() || newPassword.length < MIN_OPERATOR_PASSWORD_LENGTH || !termsAccepted || !privacyAccepted} onClick={() => void createOperatorCredential()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}创建员工账号并加入团队</Button>
                </div>
              ) : !challenge ? (
                <div className="space-y-3">
                  <p className="text-sm text-muted-foreground">点击下方按钮获取短信验证码；验证通过后只需填写姓名并设置密码，即可开户并自动登录。</p>
                  <Button className="w-full" disabled={busy || !publicStatus.account_available} onClick={() => void sendVerification().catch(() => undefined)}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}验证联系方式并创建员工账号</Button>
                </div>
              ) : !verification ? (
                <div className="space-y-4 rounded-xl border p-4">
                  <InviteDeliveryStatus
                    state={delivery?.delivery_state || 'queued'}
                    failureCode={delivery?.failure_code}
                    onResend={() => resendVerification().catch(() => undefined)}
                    onRecheck={() => recheckDelivery()}
                    onContactOwner={() => setContactNotice('请直接联系发送邀请链接给你的团队负责人。')}
                    hint={`验证码只用于本次员工开户，${new Date(challenge.expires_at).toLocaleTimeString('zh-CN', { hour12: false })} 前有效。`}
                  />
                  {challenge.test_code && <p className="rounded-md bg-muted p-2 text-xs" data-testid="local-verification-code">测试环境验证码：{challenge.test_code}</p>}
                  <div><Label htmlFor="invite-code">6 位验证码</Label><Input id="invite-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={event => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} /></div>
                  <Button className="w-full" disabled={busy || code.length !== 6} onClick={() => void verifyCode()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}确认验证码</Button>
                </div>
              ) : (
                <div className="space-y-4 rounded-xl border p-4">
                  <div><Label htmlFor="operator-name">你的姓名</Label><Input id="operator-name" autoComplete="name" maxLength={80} value={displayName} onChange={event => setDisplayName(event.target.value)} /></div>
                  <div><Label htmlFor="operator-password">设置登录密码</Label><Input id="operator-password" type="password" autoComplete="new-password" minLength={MIN_OPERATOR_PASSWORD_LENGTH} maxLength={128} value={newPassword} onChange={event => setNewPassword(event.target.value)} /><p className="mt-1 text-xs text-muted-foreground">至少 {MIN_OPERATOR_PASSWORD_LENGTH} 位。这个账号只用于在该团队内干活，创建后会自动登录。</p></div>
                  <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={termsAccepted} onChange={event => setTermsAccepted(event.target.checked)} /><span>我已阅读并同意 <a className="underline" href="/terms" target="_blank" rel="noreferrer">用户协议</a></span></label>
                  <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={privacyAccepted} onChange={event => setPrivacyAccepted(event.target.checked)} /><span>我已阅读并同意 <a className="underline" href="/privacy" target="_blank" rel="noreferrer">隐私政策</a></span></label>
                  <Button className="w-full" disabled={busy || !displayName.trim() || newPassword.length < MIN_OPERATOR_PASSWORD_LENGTH || !termsAccepted || !privacyAccepted} onClick={() => void createOperator()}>{busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}创建员工账号并加入团队</Button>
                </div>
              )}
            </>
          ) : null}

          {contactNotice && (
            <div className="rounded-lg border bg-muted/40 p-3 text-sm text-muted-foreground" data-testid="contact-owner-notice">{contactNotice}</div>
          )}
          {error && (
            <div role="alert" className="space-y-3 rounded-lg border border-destructive/35 bg-destructive/5 p-4 text-sm text-destructive">
              <div className="flex items-start gap-2"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /><span>{error}</span></div>
              <Button size="sm" variant="outline" onClick={() => void inspectPublic()}>重新核验</Button>
            </div>
          )}
          <p className="flex items-start gap-1.5 text-xs leading-5 text-muted-foreground"><ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />邀请链接只在本页有效，不会被保存；链接被撤回或过期后将无法使用。</p>
        </CardContent>
      </Card>
    </main>
  );
}
