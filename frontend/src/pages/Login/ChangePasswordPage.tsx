/**
 * 修改密码页
 * 首登强制改密或主动修改密码
 */
import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/context/AuthContext';
import { authApi } from '@/context/AuthContext';

export default function ChangePasswordPage() {
    const [oldPassword, setOldPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const { user, updateToken, logout } = useAuth();
    const navigate = useNavigate();

    const isForced = user?.must_change_password === 1;

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');

        if (!oldPassword || !newPassword || !confirmPassword) {
            setError('请填写所有字段');
            return;
        }
        if (newPassword.length < 6) {
            setError('新密码至少 6 位');
            return;
        }
        if (newPassword !== confirmPassword) {
            setError('两次输入的新密码不一致');
            return;
        }

        setLoading(true);
        try {
            const res = await authApi.post('/api/auth/change-password', {
                old_password: oldPassword,
                new_password: newPassword,
            });

            if (res.data.success) {
                if (res.data.token) {
                    await updateToken(res.data.token);
                }
                window.location.href = '/';
                return;
            } else {
                setError(res.data.error || '密码修改失败');
            }
        } catch (err: any) {
            setError(err.response?.data?.detail || '网络错误');
        }
        setLoading(false);
    };

    return (
        <div className="flex justify-center items-center min-h-screen bg-muted px-4">
            <div className="w-full max-w-[400px] bg-card rounded-xl border border-border p-4 sm:p-6 md:p-8">
                <div className="text-center mb-8">
                    <div className="text-5xl mb-3">🔑</div>
                    <h1 className="text-[22px] font-bold text-foreground m-0">
                        {isForced ? '首次登录 · 请修改密码' : '修改密码'}
                    </h1>
                    {isForced && (
                        <p className="text-[13px] text-amber-600 mt-2 bg-amber-50 px-3 py-2 rounded-lg">
                            为了账户安全，请设置新密码后继续使用
                        </p>
                    )}
                </div>

                <form onSubmit={handleSubmit} className="flex flex-col gap-[18px]">
                    <div className="flex flex-col gap-1.5">
                        <label className="text-[13px] font-semibold text-muted-foreground">
                            {isForced ? '初始密码' : '原密码'}
                        </label>
                        <input
                            type="password"
                            value={oldPassword}
                            onChange={e => setOldPassword(e.target.value)}
                            placeholder={isForced ? '输入初始密码' : '输入原密码'}
                            autoFocus
                            className="px-4 py-3 border border-border rounded-md text-[15px] outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground"
                        />
                    </div>

                    <div className="flex flex-col gap-1.5">
                        <label className="text-[13px] font-semibold text-muted-foreground">新密码</label>
                        <input
                            type="password"
                            value={newPassword}
                            onChange={e => setNewPassword(e.target.value)}
                            placeholder="至少 6 位"
                            className="px-4 py-3 border border-border rounded-md text-[15px] outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground"
                        />
                    </div>

                    <div className="flex flex-col gap-1.5">
                        <label className="text-[13px] font-semibold text-muted-foreground">确认新密码</label>
                        <input
                            type="password"
                            value={confirmPassword}
                            onChange={e => setConfirmPassword(e.target.value)}
                            placeholder="再次输入新密码"
                            className="px-4 py-3 border border-border rounded-md text-[15px] outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground"
                        />
                    </div>

                    {error && (
                        <div className="px-3.5 py-2.5 rounded-xl bg-red-50 text-red-600 text-[13px] border border-red-200">
                            {error}
                        </div>
                    )}

                    <button
                        type="submit"
                        disabled={loading}
                        className="py-3.5 rounded-md border-none bg-brand text-white text-base font-semibold cursor-pointer mt-1 transition-all hover:bg-brand/90 disabled:opacity-60 disabled:cursor-not-allowed"
                    >
                        {loading ? '提交中...' : '确认修改'}
                    </button>

                    {!isForced && (
                        <button
                            type="button"
                            onClick={() => navigate(-1)}
                            className="py-3 rounded-md border border-border bg-transparent text-muted-foreground text-sm cursor-pointer hover:bg-secondary transition-colors"
                        >
                            取消
                        </button>
                    )}
                </form>
            </div>
        </div>
    );
}
