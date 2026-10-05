/**
 * 开源版署名位(WO_329):从 GET /api/public/oss-attribution 取开关与文字(唯一来源 config/oss_attribution.py)。
 * 主仓 / 线上开关恒关 ⇒ 接口回 {enabled:false} ⇒ 什么都不渲染。前端源码里不写署名的字(字只来自后端)。
 * 用原生 fetch(免登录的客户门户页也能取),整个页面只请求一次。
 */
import { useEffect, useState } from 'react';

export interface OssAttribution {
  enabled: boolean;
  text?: string;
  text_en?: string;
  /** 打印 / 导出文档用(带网址,打印出来点不了链接) */
  report_text?: string;
  href?: string;
}

let pending: Promise<OssAttribution | null> | null = null;

export function loadOssAttribution(): Promise<OssAttribution | null> {
  if (!pending) {
    pending = fetch('/api/public/oss-attribution', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (d && d.enabled === true && typeof d.text === 'string' && typeof d.href === 'string' ? (d as OssAttribution) : null))
      .catch(() => null);
  }
  return pending;
}

export function useOssAttribution(): OssAttribution | null {
  const [value, setValue] = useState<OssAttribution | null>(null);
  useEffect(() => {
    let alive = true;
    loadOssAttribution().then((v) => { if (alive) setValue(v); });
    return () => { alive = false; };
  }, []);
  return value;
}
