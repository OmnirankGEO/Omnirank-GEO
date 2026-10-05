/**
 * [推广页统一 2026-07-28] 普通用户「推荐有礼」· 按 /agent/promotion 新推广中心骨架重做。
 *
 * 身份路线铁律:
 * - 普通用户只见「推荐送算力(15% 即时到账)」路线,零任何服务商收益体系字样
 *   (判别锁扫描本文件,连注释也不许出现那三个词);
 * - 保留「升级服务商后历史返利不补差」提示;服务商收益体系(/agent/promotion + 结算)一字不动;
 * - 分身份取数:本页零 agent 专属 API(全部走 /api/referral/*,登录即可用);
 * - 旧 /referral 页(ReferralCenter.tsx)归档不删,路由已切到本页。
 */
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  Copy, Download, Gift, Image as ImageIcon, Loader2, QrCode, RefreshCw, Users,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import PromotionPoster, {
  POSTER_DESIGN_H,
  POSTER_DESIGN_W,
} from '@/components/promotion/PromotionPoster';
import { authFetch, formatApiErrorForDisplay } from '@/lib/api';
import { copyToClipboard } from '@/lib/copyUtils';
import { ManualCopyDialog } from '@/components/common/ManualCopyDialog';

interface BonusRecord {
  id: number;
  recharge_yuan?: number;
  rate?: number;
  bonus_points?: number;
  referred_name?: string;
  created_at?: string;
}

const PREVIEW_SCALE = 0.5;

export default function InviteCenter(): JSX.Element {
  const [refCode, setRefCode] = useState('');
  const [refLink, setRefLink] = useState('');
  const [qrDataUrl, setQrDataUrl] = useState('');
  const [referredCount, setReferredCount] = useState(0);
  const [records, setRecords] = useState<BonusRecord[]>([]);
  const [totalBonus, setTotalBonus] = useState(0);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  // [WO_IOS_TOUCH_UX 2026-08-05] 复制被拒 → 亮出可长按选中的内容(不许只说"请手动复制")
  const [manualCopyText, setManualCopyText] = useState<string | null>(null);
  const exportRef = useRef<HTMLDivElement>(null);

  const reload = async () => {
    setLoading(true);
    try {
      // 推荐码:注册即有,普通用户可用(非 agent 专属)
      const res = await authFetch('/api/referral/code');
      const json = await res.json();
      const code = json?.data?.code || json?.code || '';
      setRefCode(code);
      if (code) setRefLink(`${window.location.origin}/register?ref=${code}`);
    } catch (e: unknown) {
      toast.error(formatApiErrorForDisplay(e, '推荐码加载失败 · 请刷新重试', 'customer'));
    }
    try {
      // 只取"推荐了几个人";本页不消费任何收益类字段
      const res = await authFetch('/api/referral/stats');
      const json = await res.json();
      const d = json?.data || json || {};
      setReferredCount((Number(d.l1_count) || 0) + (Number(d.l2_count) || 0));
    } catch {
      /* 统计失败保持 0,不阻断 */
    }
    try {
      // 15% 即时奖励明细(按笔)
      const res = await authFetch('/api/referral/bonus-history?limit=50');
      const json = await res.json();
      const d = json?.data || json || {};
      setRecords((d.records || d.items || []) as BonusRecord[]);
      setTotalBonus(Number(d.total_bonus_points) || 0);
    } catch {
      /* 明细失败保持空态 */
    }
    setLoading(false);
  };

  useEffect(() => {
    void reload();
  }, []);

  // 推荐链接 → 真实二维码(客户端生成,与新推广中心同法)
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!refLink) return;
      try {
        const QRCode = (await import('qrcode')).default;
        const url = await QRCode.toDataURL(refLink, { width: 300, margin: 1 });
        if (!cancelled) setQrDataUrl(url);
      } catch {
        /* 二维码生成失败 → 海报用占位 */
      }
    })();
    return () => { cancelled = true; };
  }, [refLink]);

  // [WO_IOS_TOUCH_UX 2026-08-05] 这里的手势链本来就是好的(writeText 是函数体第一句,前面没 await),
  //   坏在**失败兜底**:原写法直连 navigator.clipboard(连 copyUtils 的 execCommand 降级都没有),
  //   失败只丢一句「请手动选择复制」—— 而推荐码在页面上是不可选中的展示元素,
  //   用户只能照着屏幕念给客户。2026-08-05 服务商 #133 把推荐码转录错(字形混淆)就是这么来的。
  //   铁律 feedback_hint_must_help_or_hide:提示要么能帮人解决,要么别显示。
  const copyText = (text: string, label: string) => {
    void copyToClipboard(text).then((ok) => {
      if (ok) toast.success(`${label}已复制`);
      // 复制不了 → 把内容原样亮出来给用户长按选中,而不是让他照着念
      else setManualCopyText(text);
    });
  };

  const exportPoster = async () => {
    if (!exportRef.current) return;
    setExporting(true);
    try {
      const html2canvas = (await import('html2canvas')).default;
      const canvas = await html2canvas(exportRef.current, {
        width: POSTER_DESIGN_W,
        height: POSTER_DESIGN_H,
        scale: 2,
        backgroundColor: '#ffffff',
      });
      const a = document.createElement('a');
      a.href = canvas.toDataURL('image/png');
      a.download = '邀请海报.png';
      a.click();
      toast.success('海报已保存');
    } catch (e: unknown) {
      toast.error(formatApiErrorForDisplay(e, '海报生成失败 · 请重试', 'customer'));
    } finally {
      setExporting(false);
    }
  };

  const posterProps = {
    brand: { companyName: '', brandColor: '#6CBE1E' },
    qrDataUrl,
    sellingPoint: '注册就送体验算力,AI 帮你做品牌曝光。',
    showServicePoints: false,
    title: ['邀请你一起', '用 AI 做推广'] as [string, string],
    subline: '品牌体检 / AI 写文章 / 效果监测',
    ctaText: '扫码注册领体验算力',
    footerText: '朋友邀请你加入',
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4" data-testid="invite-center-page">
      {/* 页头 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted">
            <Gift className="h-5 w-5" />
          </div>
          <h1 className="text-xl font-bold sm:text-2xl">推荐有礼</h1>
          <Badge variant="outline" className="hidden sm:inline-flex">
            朋友充值 · 你得 15% 奖励算力 · 即时到账
          </Badge>
        </div>
        <Button variant="ghost" size="sm" onClick={() => void reload()}>
          <RefreshCw className={loading ? 'mr-1 h-4 w-4 animate-spin' : 'mr-1 h-4 w-4'} /> 刷新
        </Button>
      </div>

      {/* 顶部:推荐码 + 二维码 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <QrCode className="h-4 w-4" /> 我的推荐码
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              朋友用你的链接注册并充值,你即时获得充值额 15% 的奖励算力(自动到账,可抵扣平台工具)。
            </p>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2">
              <code className="rounded-md bg-muted px-3 py-1.5 font-mono text-lg">{refCode || '…'}</code>
              <Button size="sm" variant="outline" disabled={!refCode} onClick={() => void copyText(refCode, '推荐码')}>
                <Copy className="mr-1 h-3.5 w-3.5" /> 复制
              </Button>
            </div>
            <div className="flex items-center gap-2">
              <span className="truncate rounded-md bg-muted px-3 py-1.5 text-xs text-muted-foreground">
                {refLink || '…'}
              </span>
              <Button size="sm" variant="outline" disabled={!refLink} onClick={() => void copyText(refLink, '推荐链接')}>
                <Copy className="mr-1 h-3.5 w-3.5" /> 复制链接
              </Button>
            </div>
            {qrDataUrl && (
              <img src={qrDataUrl} alt="推荐二维码" className="h-36 w-36 rounded-md border border-border" />
            )}
          </CardContent>
        </Card>

        {/* 海报卡(复用新推广中心海报组件 · 邀请口吻 · 中性品牌) */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ImageIcon className="h-4 w-4" /> 邀请海报
            </CardTitle>
            <p className="text-xs text-muted-foreground">生成带你推荐二维码的海报,发朋友圈或私聊都行。</p>
          </CardHeader>
          <CardContent className="space-y-3">
            <div
              className="overflow-hidden rounded-lg border border-border"
              style={{ width: POSTER_DESIGN_W * PREVIEW_SCALE, height: POSTER_DESIGN_H * PREVIEW_SCALE }}
            >
              <div style={{ transform: `scale(${PREVIEW_SCALE})`, transformOrigin: 'top left' }}>
                <PromotionPoster {...posterProps} />
              </div>
            </div>
            <Button size="sm" disabled={exporting || !qrDataUrl} onClick={() => void exportPoster()}>
              {exporting ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Download className="mr-1 h-4 w-4" />}
              保存海报
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* 底部:推荐奖励列表 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Users className="h-4 w-4" /> 我的推荐({referredCount} 位朋友)
            <span className="text-xs font-normal text-muted-foreground">
              累计获赠 {totalBonus.toLocaleString()} 算力
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {loading && <p className="p-4 text-xs text-muted-foreground">加载中…</p>}
          {!loading && records.length === 0 && (
            <p className="p-6 text-center text-sm text-muted-foreground">
              还没有奖励记录 · 把推荐码分享给朋友,朋友充值后你即时获得 15% 奖励算力。
            </p>
          )}
          {!loading && records.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="p-3 text-left">朋友</th>
                    <th className="p-3 text-right">充值(元)</th>
                    <th className="p-3 text-right">奖励比例</th>
                    <th className="p-3 text-right">获赠算力</th>
                    <th className="p-3 text-left">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id} className="border-b last:border-b-0">
                      <td className="p-3">{r.referred_name || '朋友'}</td>
                      <td className="p-3 text-right font-mono">{r.recharge_yuan ?? '-'}</td>
                      <td className="p-3 text-right" data-testid="bonus-rate">
                        {r.rate != null ? `${Math.round(Number(r.rate) * 100)}%` : '15%'}
                      </td>
                      <td className="p-3 text-right font-mono text-emerald-600 dark:text-emerald-300">
                        +{(r.bonus_points ?? 0).toLocaleString()}
                      </td>
                      <td className="p-3 text-xs">
                        {r.created_at ? new Date(r.created_at).toLocaleDateString() : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {/* 身份路线提示:升级不补差(铁律保留 · 不出现资金类字样) */}
          <p className="border-t border-border/60 p-3 text-xs leading-relaxed text-muted-foreground">
            💡 升级服务商后(通过「合作伙伴计划」实名审核):新充值按服务商规则结算;
            历史朋友的首次充值按当时身份结算,不补差额。
          </p>
        </CardContent>
      </Card>

      <ManualCopyDialog
        text={manualCopyText}
        onClose={() => setManualCopyText(null)}
        title="自动复制没成功"
        hint="下面就是要发给客户的内容。请长按全选复制，或点「再试一次」——别照着屏幕手打，容易看错字。"
      />

      {/* 隐藏:海报导出真身(原尺寸 · html2canvas 捕获此节点) */}
      <div style={{ position: 'fixed', left: -99999, top: 0, pointerEvents: 'none' }} aria-hidden>
        <PromotionPoster ref={exportRef} {...posterProps} />
      </div>
    </div>
  );
}
