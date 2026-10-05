/**
 * BrandDisplay — 统一白标品牌展示组件
 *
 * 接受 resolveBrand()/useBranding() 的 brand 输出。brand 永远有值（最差是 OmniRank），始终渲染。
 * v3.6 新增 BrandName（纯品牌名）+ BrandTitle（运行时 document.title/favicon 注入 · OEM 决策 B）。
 */
import { useEffect } from 'react';
import type { ResolvedBrand } from '@/hooks/useWhitelabel';

interface BrandLogoProps {
  brand: ResolvedBrand | null;
  className?: string;
  size?: 'sm' | 'md' | 'lg';
}

/** 品牌 Logo — 有 logo_url 显示图片，否则显示公司名文字 */
export function BrandLogo({ brand, className, size = 'md' }: BrandLogoProps) {
  if (!brand) return null; // 加载中

  const sizeClass = size === 'sm' ? 'h-6' : size === 'lg' ? 'h-12' : 'h-8';
  const textClass = size === 'sm' ? 'text-sm' : size === 'lg' ? 'text-xl' : 'text-base';

  return brand.logo_url ? (
    <img
      src={brand.logo_url}
      alt={brand.company_name}
      className={`${sizeClass} w-auto object-contain ${className || ''}`}
    />
  ) : (
    <span className={`font-bold text-foreground ${textClass} ${className || ''}`}>
      {brand.company_name}
    </span>
  );
}

interface BrandFooterProps {
  brand: ResolvedBrand | null;
  className?: string;
}

/** 品牌页脚 — 显示公司名 + slogan */
export function BrandFooter({ brand, className }: BrandFooterProps) {
  if (!brand) return null;

  return (
    <div className={`text-center text-xs text-muted-foreground ${className || ''}`}>
      {brand.company_name}
      {brand.slogan && <span className="ml-1">· {brand.slogan}</span>}
    </div>
  );
}

interface BrandContactProps {
  brand: ResolvedBrand | null;
  className?: string;
}

/** 品牌联系方式 — 有联系人信息时显示 */
export function BrandContact({ brand, className }: BrandContactProps) {
  if (!brand) return null;
  if (!brand.contact_name && !brand.contact_phone && !brand.contact_wechat) return null;

  return (
    <div className={`text-sm text-muted-foreground space-y-0.5 ${className || ''}`}>
      {brand.contact_name && <p>联系人: {brand.contact_name}</p>}
      {brand.contact_phone && <p>电话: {brand.contact_phone}</p>}
      {brand.contact_wechat && <p>微信: {brand.contact_wechat}</p>}
    </div>
  );
}

interface BrandNameProps {
  brand: ResolvedBrand | null;
  className?: string;
}

/** 品牌名（纯文本）— OEM 优先 product_name，否则 company_name */
export function BrandName({ brand, className }: BrandNameProps) {
  if (!brand) return null;
  return <span className={className}>{brand.product_name || brand.company_name}</span>;
}

interface BrandTitleProps {
  brand: ResolvedBrand | null;
  suffix?: string;
}

/**
 * BrandTitle — 运行时注入 document.title + favicon（OEM · 决策 B）。无可视渲染（返回 null）。
 * 用于 OEM 档代理工作台浏览器标签品牌化；阶段 2 接入工作台时使用。
 *
 * P1(Codex T5)：缓存原始 title/favicon，在 cleanup / brand 切换时恢复，避免 OEM favicon
 * 残留到平台/admin 页面（切回平台 brand 无 favicon_url 时图标仍是服务商的）。
 * 只恢复本组件改过的内容；本组件自建的 <link> 在 cleanup 时移除。
 */
export function BrandTitle({ brand, suffix }: BrandTitleProps) {
  useEffect(() => {
    if (!brand) return;
    const name = brand.product_name || brand.company_name;

    const prevTitle = document.title;
    let titleChanged = false;
    if (name) {
      document.title = suffix ? `${name} · ${suffix}` : name;
      titleChanged = true;
    }

    let link: HTMLLinkElement | null = null;
    let prevFavicon = '';
    let createdLink = false;
    if (brand.favicon_url) {
      link = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
      if (!link) {
        link = document.createElement('link');
        link.rel = 'icon';
        document.head.appendChild(link);
        createdLink = true;
      }
      prevFavicon = link.getAttribute('href') || '';
      link.setAttribute('href', brand.favicon_url);
    }

    return () => {
      if (titleChanged) document.title = prevTitle;
      if (link) {
        if (createdLink) link.remove();
        else link.setAttribute('href', prevFavicon);
      }
    };
  }, [brand, suffix]);
  return null;
}
