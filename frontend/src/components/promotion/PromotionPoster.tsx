/**
 * PromotionPoster — 代理获客推广海报(纯前端模板渲染)
 *
 * 老板 2026-06-03 需求:
 *   - 不用 AI 生图 · 不做展架 · 前端模板动态渲染 · html2canvas 导出 PNG
 *   - 自动套服务方白牌(品牌名 / Logo / 主题色) · 绝不出现 OmniRank / 全域上榜 / 平台技术服务
 *   - 二维码必须真实推广链接生成(qrcode 包客户端生成的 dataURL · 非图片模型)
 *
 * 设计尺寸固定 540×720(3:4) · 导出时由调用方用 html2canvas scale 放大到:
 *   朋友圈版 1080×1440(scale 2) / 微信私聊版 900×1200(scale 1.667)
 * 两个模板同一设计,仅导出分辨率不同。forwardRef 指向海报根 div,
 * 调用方对该 ref 跑 html2canvas 即得整张海报。
 *
 * 白标缺省(未授权代理 → /api/referral/whitelabel data:null):
 *   companyName='' → 顶部占位「你的品牌服务」/ 底部「由你的品牌提供服务」· 永不显示平台名。
 * 主题色缺省 → 系统绿 #6CBE1E。
 * Logo: 有 logoUrl 渲染图(crossOrigin · 加载/CORS 失败 onError 回退首字圆) · 无则首字圆。
 *
 * 注:本组件为导出型图片产物,用 inline style 控制像素级布局 + 运行时主题色
 *    (主题色无法用 Tailwind 静态类表达) · 与现有 components/share/SharePosterDialog 先例一致。
 */
import { forwardRef, useState } from 'react';

export type PosterTemplate = 'moments' | 'private';

export interface PosterBrandInfo {
  /** 代理白标公司名 · '' 表示未设置 → 用中性占位,绝不回退平台名 */
  companyName: string;
  logoUrl?: string;
  /** 主题色 hex · 调用方已兜底为系统绿 */
  brandColor: string;
}

export interface PromotionPosterProps {
  brand: PosterBrandInfo;
  /** 推广链接(ref_link)生成的真实二维码 PNG dataURL · '' 表示尚未生成 */
  qrDataUrl: string;
  /** 一句话卖点(标题下方展示) */
  sellingPoint: string;
  /** 是否在海报上展示服务点列表 */
  showServicePoints: boolean;
  // [推广页统一 2026-07-28] 普通用户"邀请朋友"复用同一张海报,仅换口吻。
  // 三项全 optional,默认 = 服务商版原文案 —— agent 页零改动零感知。
  /** 大标题两行(默认服务商版"让客户在 AI 里/更容易被找到") */
  title?: [string, string];
  /** 标题下副行(默认"AI体检 / 报价方案 / 写文章 / 看效果") */
  subline?: string;
  /** 二维码下 CTA(默认"扫码了解服务") */
  ctaText?: string;
  /** 底部品牌条(默认"由{品牌}提供服务") */
  footerText?: string;
}

export const POSTER_DESIGN_W = 540;
export const POSTER_DESIGN_H = 720;

const SERVICE_POINTS = [
  '洞察客户在各大 AI 平台的曝光表现',
  '生成专业报价方案，提升成交效率',
  '高质量内容创作，持续提升搜索可见',
];

/** 仅当标准 6 位 hex 时拼 alpha 后缀,否则返回原色(防 rgb()/3 位 hex 拼坏) */
function withAlpha(hex: string, alphaHex: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex + alphaHex : hex;
}

export const PromotionPoster = forwardRef<HTMLDivElement, PromotionPosterProps>(
  function PromotionPoster(
    { brand, qrDataUrl, sellingPoint, showServicePoints, title, subline, ctaText, footerText },
    ref,
  ) {
    const [logoBroken, setLogoBroken] = useState(false);
    const color = brand.brandColor || '#6CBE1E';
    const trimmedName = (brand.companyName || '').trim();
    const headerName = trimmedName || '你的品牌服务';
    const footerName = trimmedName || '你的品牌';
    const firstChar = (trimmedName || '品').charAt(0);
    const showLogoImg = !!brand.logoUrl && !logoBroken;
    const sp = (sellingPoint || '').trim();

    return (
      <div
        ref={ref}
        style={{
          width: POSTER_DESIGN_W,
          height: POSTER_DESIGN_H,
          background: '#ffffff',
          borderRadius: 24,
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
          color: '#1f2329',
          position: 'relative',
          border: '1px solid #ececec',
        }}
      >
        {/* 顶部品牌条 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '34px 40px 0' }}>
          {showLogoImg ? (
            <img
              src={brand.logoUrl}
              crossOrigin="anonymous"
              alt=""
              onError={() => setLogoBroken(true)}
              style={{ width: 46, height: 46, borderRadius: 23, objectFit: 'cover', flexShrink: 0 }}
            />
          ) : (
            <div
              style={{
                width: 46,
                height: 46,
                borderRadius: 23,
                background: color,
                color: '#ffffff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 22,
                fontWeight: 700,
                flexShrink: 0,
              }}
            >
              {firstChar}
            </div>
          )}
          <div style={{ fontSize: 19, fontWeight: 600, color: '#1f2329' }}>{headerName}</div>
        </div>

        {/* 标题 + 卖点 + 服务点 tags */}
        <div style={{ padding: '26px 40px 0' }}>
          <div style={{ fontSize: 40, fontWeight: 800, lineHeight: 1.18, color: '#15171c' }}>
            <div>{(title || ['让客户在 AI 里', '更容易被找到'])[0]}</div>
            <div>{(title || ['让客户在 AI 里', '更容易被找到'])[1]}</div>
          </div>
          {sp && (
            <div style={{ marginTop: 14, fontSize: 16, lineHeight: 1.5, color: '#5b616e' }}>{sp}</div>
          )}
          <div style={{ marginTop: 14, fontSize: 15, color: '#8a9099', fontWeight: 500 }}>
            {subline || 'AI体检 / 报价方案 / 写文章 / 看效果'}
          </div>
        </div>

        {/* 服务点列表(开关控制) */}
        {showServicePoints && (
          <div
            style={{
              margin: '22px 40px 0',
              padding: '18px 20px',
              borderRadius: 16,
              background: withAlpha(color, '14'),
              display: 'flex',
              flexDirection: 'column',
              gap: 13,
            }}
          >
            {SERVICE_POINTS.map((t) => (
              <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: 11,
                    background: color,
                    flexShrink: 0,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <span style={{ width: 8, height: 8, borderRadius: 4, background: '#ffffff' }} />
                </span>
                <span style={{ fontSize: 15, color: '#3d424d' }}>{t}</span>
              </div>
            ))}
          </div>
        )}

        {/* 二维码(真实推广链接生成) */}
        <div
          style={{
            marginTop: 'auto',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            paddingBottom: 18,
          }}
        >
          <div style={{ background: '#ffffff', padding: 10, borderRadius: 14, border: '1px solid #eeeeee' }}>
            {qrDataUrl ? (
              <img src={qrDataUrl} alt="推广二维码" style={{ width: 150, height: 150, display: 'block' }} />
            ) : (
              <div style={{ width: 150, height: 150, background: '#f4f4f5', borderRadius: 8 }} />
            )}
          </div>
          <div style={{ marginTop: 12, fontSize: 14, color: '#6b7280' }}>{ctaText || '扫码了解服务'}</div>
        </div>

        {/* 底部品牌条 */}
        <div
          style={{
            background: color,
            color: '#ffffff',
            textAlign: 'center',
            padding: '16px 0',
            fontSize: 16,
            fontWeight: 600,
          }}
        >
          {footerText || `由${footerName}提供服务`}
        </div>
      </div>
    );
  },
);

export default PromotionPoster;
