/**
 * BackToClientButton · 桥接通用组件(原 components/m3/workbench/BackToM3Button)
 *
 * CTO-15.20 桥接重设计:旧版工作面顶部 banner 用 · 跳回这个客户的工作台
 *
 * 用法:
 *   <BackToClientButton brandId={brand.id} />
 *
 * 🔴 [WO_260 · 2026-09-23] 改名 + 改目标:原先跳 `/m3/customer/:id`(M3 已废弃,那条路由只剩
 *    重定向到首页,E3 删域后会 404)。客户工作台的在役页是客户资料中心 `/my-clients/:id`
 *    (BrandDetailPage,决策条 / 工具卡 / 资料确认都在那儿)—— 不留任何指向 `/m3/*` 的默认值。
 *    唯一调用方 DecisionBarBridge 默认不显示它(showBackToClient 默认 false),可见文案不变。
 */

import { ChevronLeft } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useNavigate } from 'react-router-dom';
import { cn } from '@/lib/utils';

interface BackToClientButtonProps {
  brandId: number | string | null | undefined;
  className?: string;
  size?: 'default' | 'sm';
  variant?: 'outline' | 'ghost';
  /** 文案覆盖(默认"回客户工作台") */
  label?: string;
}

export function BackToClientButton({
  brandId,
  className,
  size = 'sm',
  variant = 'outline',
  label = '回客户工作台',
}: BackToClientButtonProps) {
  const navigate = useNavigate();

  if (!brandId) return null;

  const handleClick = () => {
    if (typeof window !== 'undefined') {
      const w = window as unknown as { __m3Analytics?: { track?: (e: string, p?: unknown) => void } };
      w.__m3Analytics?.track?.('back_to_client', { brand_id: brandId });
    }
    navigate(`/my-clients/${brandId}`);
  };

  return (
    <Button
      type="button"
      variant={variant}
      size={size}
      onClick={handleClick}
      className={cn('gap-1.5', className)}
      aria-label="返回客户工作台 · 看下一步任务"
    >
      <ChevronLeft className="h-4 w-4" aria-hidden />
      {label}
    </Button>
  );
}
