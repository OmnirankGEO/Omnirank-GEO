/**
 * 测试客户过滤 Toggle · CTO-15.18 PM 干预 A.7
 *
 * 老板红线(2026-04-28):
 * - 真/测试客户 filter 默认 ON 隐藏测试 brand
 * - localStorage 记忆代理偏好
 * - 真代理日常视图清洁 · admin 调试时手动开
 *
 * 用法:
 *   const [includeTest, setIncludeTest] = useTestClientFilter();
 *   <TestClientFilterToggle value={includeTest} onChange={setIncludeTest} />
 *
 * 显示规则(根因 #3 测试数据污染):
 * - 默认 OFF(隐藏测试)
 * - 点击 → 显示测试 + 真实客户(全部)
 * - localStorage 'omnirank_m3_show_test_clients'='1' 记忆
 */
import { useState, useEffect, useCallback } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/context/AuthContext';
import { cn } from '@/lib/utils';

const STORAGE_KEY = 'omnirank_m3_show_test_clients';

export function useTestClientFilter(): [boolean, (v: boolean) => void] {
    const { user } = useAuth();
    const isAdmin = user?.is_admin === true;
    const [includeTest, setIncludeTestState] = useState<boolean>(() => {
        try {
            return localStorage.getItem(STORAGE_KEY) === '1';
        } catch {
            return false;
        }
    });

    const setIncludeTest = useCallback((v: boolean) => {
        const nextValue = isAdmin ? v : false;
        setIncludeTestState(nextValue);
        try {
            if (nextValue) {
                localStorage.setItem(STORAGE_KEY, '1');
            } else {
                localStorage.removeItem(STORAGE_KEY);
            }
        } catch { /* ignore */ }
    }, [isAdmin]);

    return [isAdmin && includeTest, setIncludeTest];
}

export interface TestClientFilterToggleProps {
    value: boolean;
    onChange: (v: boolean) => void;
    className?: string;
    /** 数字徽章(测试客户数 · 可选 · 提示用户有几个被隐藏) */
    hiddenCount?: number;
}

export function TestClientFilterToggle({
    value,
    onChange,
    className,
    hiddenCount,
}: TestClientFilterToggleProps) {
    const { user } = useAuth();
    if (user?.is_admin !== true) return null;

    return (
        <Button
            type="button"
            variant={value ? 'default' : 'ghost'}
            size="sm"
            onClick={() => onChange(!value)}
            className={cn(
                'gap-1.5 text-xs h-8',
                !value && 'text-muted-foreground hover:text-foreground',
                className,
            )}
            aria-label={value ? '当前显示全部客户(包含测试) · 点击隐藏测试客户' : '当前隐藏测试客户 · 点击显示全部'}
            aria-pressed={value}
            title={value ? '已显示全部 · 含测试客户' : '已隐藏测试客户'}
        >
            {value ? <Eye className="h-3.5 w-3.5" aria-hidden /> : <EyeOff className="h-3.5 w-3.5" aria-hidden />}
            <span>{value ? '显示全部' : '隐藏测试'}</span>
            {!value && hiddenCount !== undefined && hiddenCount > 0 && (
                <span className="ml-0.5 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-muted text-[10px] tabular-nums">
                    {hiddenCount}
                </span>
            )}
        </Button>
    );
}

/** TestClientBadge · 客户卡上显示"测试"小标签(隔离 ON 时不显示 · OFF 时显示) */
export function TestClientBadge({ className }: { className?: string }) {
    return (
        <span
            className={cn(
                'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium',
                'bg-muted/60 text-muted-foreground border border-border/60',
                className,
            )}
            title="测试客户(数据隔离 · 不计入真实业务)"
        >
            测试
        </span>
    );
}
