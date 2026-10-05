/**
 * 注册页
 * 手机号 + 密码注册，注册成功直接登录进入系统
 */
import React, { useState, useEffect } from 'react';
import { useNavigate, Link, useSearchParams } from 'react-router-dom';
import logo from '@/assets/logo.png';
import { determineTargetRoute } from '@/utils/postLoginRedirect';
import { useAuth } from '@/context/AuthContext';

export default function RegisterPage() {
  const { updateToken } = useAuth();
  const [searchParams] = useSearchParams();
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [referral, setReferral] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  // 从 URL 参数读取不透明推荐码
  const urlRef = searchParams.get('ref') || '';
  const redirectUrl = searchParams.get('redirect') || '';

  const refCode = urlRef;

  useEffect(() => {
    try { localStorage.removeItem('omnirank_locked_ref'); } catch { /* legacy cleanup */ }
    if (urlRef) {
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete('ref');
      window.history.replaceState(window.history.state, '', `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
    }
    if (refCode && !referral) {
      setReferral(refCode);
    }
  }, [urlRef, refCode]);

  const inputCls =
    'px-4 py-3 border border-border rounded-md text-sm outline-hidden bg-secondary transition-colors focus:ring-2 focus:ring-brand text-foreground w-full';
  const labelCls = 'text-xs font-semibold text-muted-foreground';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (!phone.trim() || !/^1\d{10}$/.test(phone.trim())) {
      setError('请输入正确的手机号');
      return;
    }
    if (password.length < 6) {
      setError('密码至少 6 位');
      return;
    }
    if (password !== confirmPwd) {
      setError('两次密码不一致');
      return;
    }

    setLoading(true);
    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone: phone.trim(),
          password,
          referral_code: referral.trim() || undefined,
        }),
      });

      const data = await res.json();

      if (data.success && data.token) {
        // 注册成功 → 保存 token → 清理锁定推荐码 → 按 agent_level 跳转
        // [CTO-15.3 2026-04-20] 修 bug: 原默认跳 /social 代理工作台, L0 新用户被错误带入代理端
        //   改走 determineTargetRoute 读后端 recommended_route(L0 → /c/chat, L1+ → /)
        await updateToken(data.token);
        localStorage.removeItem('omnirank_locked_ref');
        const target = await determineTargetRoute(redirectUrl || '/');
        navigate(target, { replace: true });
      } else if (res.status === 409) {
        setError('该手机号已注册，请直接登录');
      } else {
        setError(data.detail || data.message || '注册失败');
      }
    } catch {
      setError('网络错误，请稍后重试');
    }
    setLoading(false);
  };

  return (
    <div className="flex justify-center items-center min-h-screen bg-muted px-4">
      <div className="w-full max-w-md bg-card rounded-xl border border-border p-4 sm:p-6 md:p-8">
        <div className="text-center mb-9">
          <img
            src={logo}
            alt="全域上榜 OmniRank"
            className="h-16 mx-auto mb-4 rounded-lg"
          />
          <h1 className="text-2xl font-bold text-foreground m-0">注册账号</h1>
          <p className="text-sm text-muted-foreground mt-1">创建你的 AI 创作账号</p>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* 手机号 */}
          <div className="flex flex-col gap-1.5">
            <label className={labelCls}>手机号</label>
            <input
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="请输入手机号"
              maxLength={11}
              autoComplete="tel"
              className={inputCls}
            />
          </div>

          {/* 密码 */}
          <div className="flex flex-col gap-1.5">
            <label className={labelCls}>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 6 位"
              autoComplete="new-password"
              className={inputCls}
            />
          </div>

          {/* 确认密码 */}
          <div className="flex flex-col gap-1.5">
            <label className={labelCls}>确认密码</label>
            <input
              type="password"
              value={confirmPwd}
              onChange={(e) => setConfirmPwd(e.target.value)}
              placeholder="再次输入密码"
              autoComplete="new-password"
              className={inputCls}
            />
          </div>

          {/* 推荐码 */}
          <div className="flex flex-col gap-1.5">
            <label className={labelCls}>
              推荐码 <span className="font-normal text-muted-foreground/60">(选填)</span>
            </label>
            <input
              type="text"
              value={referral}
              onChange={(e) => !refCode && setReferral(e.target.value)}
              readOnly={!!refCode}
              placeholder="如有推荐码请填写"
              autoComplete="off"
              className={`${inputCls}${refCode ? ' opacity-70 cursor-not-allowed' : ''}`}
            />
          </div>

          {error && (
            <div className="px-3.5 py-2.5 rounded-xl bg-red-950/40 text-red-400 text-xs border border-red-900/50">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="py-3.5 rounded-md border-none bg-foreground text-background text-base font-semibold tracking-widest mt-1 transition-all hover:bg-foreground/90 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {loading ? '注册中...' : '注 册'}
          </button>
        </form>

        <p className="text-center text-sm text-muted-foreground mt-6">
          已有账号?{' '}
          <Link to="/login" className="text-foreground font-medium hover:underline">
            去登录
          </Link>
        </p>

        <p className="text-center text-xs text-muted-foreground/60 mt-6">
          &copy; 2026 OmniRank &middot; 全域上榜
        </p>
      </div>
    </div>
  );
}
