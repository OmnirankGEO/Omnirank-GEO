/**
 * 代理获客推广中心(2026-06-03 重做 · 老板按参考图)
 *
 * 路由: /agent/promotion
 * 结构:
 *   顶部 — 推广二维码(真实推广链接客户端生成) + 推广链接与邀请码
 *   中部 — 生成推广海报(前端模板动态渲染 + html2canvas 导出 PNG · 朋友圈版/微信私聊版)
 *   底部 — 已绑定客户(脱敏 · 工具算力/发布算力/赠送算力)
 *
 * 红线: 海报/二维码绝不出现 OmniRank / 全域上榜 / 平台技术服务;
 *       海报品牌走代理自填白标(/api/referral/whitelabel),未授权 → 中性占位「你的品牌」。
 */
import { useEffect, useRef, useState } from 'react';
import { agentApi, formatPoints } from '@/lib/v35w2Api';
import { sourceLabel } from '@/lib/v35Terminology';
import { formatApiErrorForDisplay, authFetch } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Megaphone, Copy, RefreshCw, AlertCircle, Image as ImageIcon, MessageSquare, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import QRCode from 'qrcode';
import { PromotionPoster, type PosterTemplate } from '@/components/promotion/PromotionPoster';
import { copyToClipboard } from '@/lib/copyUtils';

interface QRInfo {
  qr_code_url: string;
  ref_link: string;
  invite_code: string;
}

interface CustomerItem {
  customer_user_id: number;
  display_name?: string | null;
  phone_masked?: string | null;
  binding_source: string;
  bound_at: string;
  dispute_status?: string | null;
  tool_credit: number;
  publish_credit: number;
  bonus_credit: number;
  // [微单 C-6] 门控三态字段随 UI 迁至客户售价页(ClientPurchaseGatePanel),此处不再消费。
}

interface WhitelabelBrand {
  company_name: string;
  logo_url: string;
  brand_color: string;
  slogan: string;
}

// [WO_REFERRAL_CHAIN §1.2] /api/referral/team 返回行(与 /stats 的 l1/l2 计数同源同表)
interface TeamMember {
  referred_id: number;
  level: number;
  created_at: string;
  display_name?: string | null;
  username?: string | null;
  total_recharged: number; // 分
}

/** 名单里绝不裸露手机号:display_name 缺失时用打码 username 兜底 */
function maskMemberName(m: TeamMember): string {
  const dn = (m.display_name || '').trim();
  if (dn) return dn;
  const un = (m.username || '').trim();
  if (/^1\d{10}$/.test(un)) return `${un.slice(0, 3)}****${un.slice(-4)}`;
  return un || `用户编号 ${m.referred_id}`;
}

const SOURCE_LABELS: Record<string, string> = {
  qrcode: '扫码',
  ref_link: '链接',
  invite_code: '邀请码',
  admin_manual: '人工绑定',
};

const DEFAULT_SELLING_POINT = '一站式 AI 营销服务，帮客户提升曝光与转化';
const DEFAULT_BRAND_COLOR = '#6CBE1E';

/** 海报设计宽 540 → 预览列缩放到 ~300px 宽 */
const PREVIEW_W = 300;
const PREVIEW_SCALE = PREVIEW_W / 540; // 0.5556
const PREVIEW_H = Math.round(720 * PREVIEW_SCALE);

const TEMPLATES: { key: PosterTemplate; label: string; size: string; icon: typeof ImageIcon }[] = [
  { key: 'moments', label: '朋友圈版', size: '1080 × 1440', icon: ImageIcon },
  { key: 'private', label: '微信私聊版', size: '900 × 1200', icon: MessageSquare },
];

export default function PromotionCenter() {
  const [qr, setQR] = useState<QRInfo | null>(null);
  const [items, setItems] = useState<CustomerItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  // [WO_REFERRAL_CHAIN §1.2] 我的下线(既有 /api/referral/team · L1/L2)
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [teamError, setTeamError] = useState(false);

  // 白标品牌(海报用 · 未授权 → null → 中性占位)
  const [wlBrand, setWlBrand] = useState<WhitelabelBrand | null>(null);

  // 二维码(真实推广链接 ref_link 客户端生成)
  const [qrDataUrl, setQrDataUrl] = useState('');

  // 海报控制项
  const [template, setTemplate] = useState<PosterTemplate>('moments');
  const [showServicePoints, setShowServicePoints] = useState(true);
  const [sellingPoint, setSellingPoint] = useState(DEFAULT_SELLING_POINT);
  const sellingPrefilled = useRef(false);
  const [saving, setSaving] = useState(false);
  const [posterPreviewUrl, setPosterPreviewUrl] = useState<string | null>(null);
  const exportRef = useRef<HTMLDivElement>(null);
  const isMobile = /iPhone|iPad|iPod|Android|HarmonyOS|Huawei/i.test(navigator.userAgent);

  const reload = async () => {
    setLoading(true);
    // 推广码/邀请码/链接(海报与二维码依赖此 · 优先且独立加载)
    try {
      const q = await agentApi.promotionQR();
      setQR(q);
    } catch (e: any) {
      toast.error(formatApiErrorForDisplay(e, '推广码加载失败 · 请刷新重试', 'agent'));
    }
    // 已绑定客户(独立加载 · 失败不拖垮二维码/海报 · 空态文案已兜底)
    try {
      const c = await agentApi.promotionCustomers(50, 0);
      setItems(c.items || []);
      setTotal(c.total || 0);
    } catch {
      /* 客户列表加载失败 · 保持空态 */
    }
    // [WO_REFERRAL_CHAIN §1.2] 我的下线名单(独立加载 · 失败给重试态不装空)
    try {
      const res = await authFetch('/api/referral/team');
      const json = await res.json();
      if (!res.ok || !json?.success) throw new Error('team load failed');
      setTeam(Array.isArray(json.data) ? json.data : []);
      setTeamError(false);
    } catch {
      setTeamError(true);
    }
    setLoading(false);
  };

  // 读代理自填白标(海报品牌源 · 未授权后端返 data:null)
  const loadWhitelabel = async () => {
    try {
      const res = await authFetch('/api/referral/whitelabel');
      if (!res.ok) return;
      const json = await res.json();
      const d = json?.data;
      if (!d) return; // 未授权 → 保持 null → 海报用中性占位
      const b: WhitelabelBrand = {
        company_name: d.company_name || '',
        logo_url: d.logo_url || d.company_logo_url || '',
        brand_color: d.brand_color || '',
        slogan: d.slogan || '',
      };
      setWlBrand(b);
      // 卖点未被用户改过时,用白标 slogan 预填(只填一次)
      if (!sellingPrefilled.current && b.slogan) {
        setSellingPoint(b.slogan);
        sellingPrefilled.current = true;
      }
    } catch {
      /* 静默 · 海报退回中性占位 */
    }
  };

  useEffect(() => {
    reload();
    loadWhitelabel();
  }, []);

  // ref_link → 真实二维码 dataURL(高分辨率 400px · 导出缩放后仍清晰可扫)
  useEffect(() => {
    const link = qr?.ref_link;
    if (!link) {
      setQrDataUrl('');
      return;
    }
    let cancelled = false;
    QRCode.toDataURL(link, {
      width: 400,
      margin: 1,
      errorCorrectionLevel: 'M',
      color: { dark: '#111111', light: '#ffffff' },
    })
      .then((url) => {
        if (!cancelled) setQrDataUrl(url);
      })
      .catch(() => {
        if (!cancelled) setQrDataUrl('');
      });
    return () => {
      cancelled = true;
    };
  }, [qr?.ref_link]);

  const copy = async (text: string, label: string) => {
    const ok = await copyToClipboard(text);
    if (ok) {
      toast.success(`${label} 已复制`);
    } else {
      toast.error('复制失败');
    }
  };

  const posterBrand = {
    companyName: wlBrand?.company_name || '',
    logoUrl: wlBrand?.logo_url || undefined,
    brandColor: wlBrand?.brand_color || DEFAULT_BRAND_COLOR,
  };

  const handleSavePoster = async () => {
    if (!exportRef.current) return;
    if (!qrDataUrl) {
      toast.error('二维码还没准备好 · 请稍候再试');
      return;
    }
    setSaving(true);
    setPosterPreviewUrl(null);
    try {
      const targetW = template === 'moments' ? 1080 : 900;
      const scale = targetW / 540;
      const html2canvas = (await import('html2canvas')).default;
      const canvas = await html2canvas(exportRef.current, {
        scale,
        useCORS: true,
        allowTaint: false,
        backgroundColor: '#ffffff',
        logging: false,
      });
      const dataUrl = canvas.toDataURL('image/png');
      const fname = `推广海报_${template === 'moments' ? '朋友圈版' : '微信私聊版'}.png`;
      if (isMobile) {
        // 移动端无法直接触发下载 · 展示成图让用户长按保存
        setPosterPreviewUrl(dataUrl);
        toast.success('长按下方海报图片即可保存');
      } else {
        const link = document.createElement('a');
        link.download = fname;
        link.href = dataUrl;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        toast.success('海报已保存 · 去发朋友圈/微信群吧');
      }
    } catch (e) {
      console.error('生成海报失败', e);
      toast.error('海报生成失败 · 请重试');
    } finally {
      setSaving(false);
    }
  };

  const currentTemplate = TEMPLATES.find((t) => t.key === template)!;

  return (
    <div className="container mx-auto py-6 space-y-6 max-w-7xl">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Megaphone className="w-6 h-6" />
          <h1 className="text-2xl font-bold">推广获客</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={reload}>
          <RefreshCw className="w-4 h-4 mr-1" /> 刷新
        </Button>
      </div>

      {/* 顶部:推广二维码 + 推广链接与邀请码 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">推广二维码</CardTitle>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="h-32 bg-muted animate-pulse rounded" />
            ) : qrDataUrl ? (
              <>
                <img src={qrDataUrl} alt="推广二维码" className="w-32 h-32 mx-auto" />
                <p className="text-center text-xs text-muted-foreground mt-2">客户扫码后，归到你的服务名下</p>
              </>
            ) : (
              <div className="flex flex-col items-center gap-2 py-3 text-center">
                <AlertCircle className="w-7 h-7 text-muted-foreground" />
                <p className="text-xs text-muted-foreground">二维码暂时没生成 · 先复制推广链接发客户</p>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!qr?.ref_link}
                    onClick={() => qr?.ref_link && copy(qr.ref_link, '推广链接')}
                  >
                    <Copy className="w-4 h-4 mr-1" />
                    复制推广链接
                  </Button>
                  <Button size="sm" variant="outline" onClick={reload}>
                    <RefreshCw className="w-4 h-4 mr-1" />
                    重新生成
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="md:col-span-2">
          <CardHeader>
            <CardTitle className="text-sm">推广链接与邀请码</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <div className="text-xs text-muted-foreground mb-1">邀请码</div>
              <div className="flex gap-2">
                <code className="flex-1 bg-muted px-3 py-2 rounded font-mono">{qr?.invite_code || '...'}</code>
                <Button size="sm" variant="outline" onClick={() => qr && copy(qr.invite_code, '邀请码')}>
                  <Copy className="w-4 h-4" />
                </Button>
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground mb-1">推广链接</div>
              <div className="flex gap-2">
                <code className="flex-1 bg-muted px-3 py-2 rounded text-xs truncate">{qr?.ref_link || '...'}</code>
                <Button size="sm" variant="outline" onClick={() => qr && copy(qr.ref_link, '推广链接')}>
                  <Copy className="w-4 h-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 中部:生成推广海报 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">生成推广海报</CardTitle>
          <p className="text-xs text-muted-foreground">
            自动套用你的品牌、Logo、主题色和推广二维码，方便发朋友圈和微信群
          </p>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* 左:控制项 */}
            <div className="space-y-5">
              <div>
                <div className="text-sm font-medium mb-2">选择海报模板</div>
                <div className="grid grid-cols-2 gap-3">
                  {TEMPLATES.map((t) => {
                    const active = template === t.key;
                    const Icon = t.icon;
                    return (
                      <button
                        key={t.key}
                        type="button"
                        onClick={() => setTemplate(t.key)}
                        className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
                          active ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40'
                        }`}
                      >
                        <div
                          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-md ${
                            active ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'
                          }`}
                        >
                          <Icon className="h-5 w-5" />
                        </div>
                        <div className="min-w-0">
                          <div className="text-sm font-medium">{t.label}</div>
                          <div className="text-xs text-muted-foreground">{t.size}</div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center justify-between rounded-lg border p-3">
                <div>
                  <div className="text-sm font-medium">显示服务点</div>
                  <div className="text-xs text-muted-foreground">在海报上展示服务点列表</div>
                </div>
                <Switch checked={showServicePoints} onCheckedChange={setShowServicePoints} />
              </div>

              <div>
                <div className="text-sm font-medium mb-1.5">一句话卖点</div>
                <Input
                  value={sellingPoint}
                  maxLength={40}
                  placeholder={DEFAULT_SELLING_POINT}
                  onChange={(e) => {
                    sellingPrefilled.current = true;
                    setSellingPoint(e.target.value);
                  }}
                />
                <div className="text-xs text-muted-foreground mt-1">将在海报标题下方展示</div>
              </div>

              <Button onClick={handleSavePoster} disabled={saving || !qrDataUrl} className="gap-2">
                {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImageIcon className="h-4 w-4" />}
                生成并保存海报
              </Button>
            </div>

            {/* 右:海报预览 */}
            <div className="flex flex-col items-center">
              <div className="text-xs text-muted-foreground mb-2">
                海报预览（{currentTemplate.label} {currentTemplate.size}）
              </div>
              <div style={{ width: PREVIEW_W, height: PREVIEW_H, overflow: 'hidden' }}>
                <div style={{ transform: `scale(${PREVIEW_SCALE})`, transformOrigin: 'top left' }}>
                  <PromotionPoster
                    brand={posterBrand}
                    qrDataUrl={qrDataUrl}
                    sellingPoint={sellingPoint}
                    showServicePoints={showServicePoints}
                  />
                </div>
              </div>

              {/* 移动端长按保存图 */}
              {posterPreviewUrl && (
                <div className="mt-4 flex flex-col items-center">
                  <img
                    src={posterPreviewUrl}
                    alt="推广海报"
                    style={{ width: PREVIEW_W }}
                    className="rounded-lg border"
                  />
                  <div className="text-xs text-muted-foreground mt-1">长按图片保存到相册</div>
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* [微单 C-6 2026-07-28] 客户线上购买门控已迁至「客户售价」页(唯一入口),
          此处旧卡与表格"线上购买"列一并撤除 —— 单一入口,不留第二处开关。 */}

      {/* [WO_REFERRAL_CHAIN §1.2] 我的下线(/api/referral/team · 与推荐统计同源) */}
      <Card data-testid="referral-team-section">
        <CardHeader>
          <CardTitle className="text-base">
            我的下线
            {team.length > 0 && (
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                直推 {team.filter((m) => m.level === 1).length} 人 · 间推 {team.filter((m) => m.level === 2).length} 人
              </span>
            )}
          </CardTitle>
          <p className="text-xs text-muted-foreground">通过你的推广链接 / 二维码 / 推荐码注册的用户</p>
        </CardHeader>
        <CardContent className="p-0">
          {teamError ? (
            <div className="flex flex-col items-center gap-2 p-6 text-center">
              <AlertCircle className="w-6 h-6 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">名单加载失败</p>
              <Button size="sm" variant="outline" onClick={reload}>
                <RefreshCw className="w-4 h-4 mr-1" /> 重新加载
              </Button>
            </div>
          ) : team.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">
              还没有人通过你的链接注册 · 把上方推广链接或海报发出去,注册的用户会出现在这里
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {team.map((m) => (
                <li
                  key={`${m.level}-${m.referred_id}`}
                  className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-3 sm:px-6"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <Badge variant={m.level === 1 ? 'default' : 'outline'} className="shrink-0">
                      {m.level === 1 ? '直推' : '间推'}
                    </Badge>
                    <span className="truncate text-sm text-foreground">{maskMemberName(m)}</span>
                  </div>
                  <div className="flex shrink-0 items-center gap-4 text-xs text-muted-foreground">
                    <span>注册 {new Date(m.created_at).toLocaleDateString('zh-CN')}</span>
                    <span className="font-mono">累计充值 ¥{((m.total_recharged || 0) / 100).toFixed(0)}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* 底部:已绑定客户 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">已绑定客户({total})</CardTitle>
          <p className="text-xs text-muted-foreground">手机号脱敏 · 客户归属冲突由平台裁决</p>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="text-left p-3">客户</th>
                  <th className="text-left p-3">手机</th>
                  <th className="text-left p-3">来源</th>
                  <th className="text-left p-3">绑定时间</th>
                  <th className="text-right p-3">算力</th>
                  <th className="text-right p-3">发布算力</th>
                  <th className="text-right p-3">赠送算力</th>
                  <th className="text-left p-3">状态</th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 && (
                  <tr>
                    <td colSpan={8} className="text-center p-6 text-muted-foreground">
                      暂无绑定客户 · 客户通过你的推广链接或二维码注册后将在这里显示
                    </td>
                  </tr>
                )}
                {items.map((c) => (
                  <tr key={c.customer_user_id} className="border-b">
                    <td className="p-3">
                      {c.display_name || `用户编号 ${c.customer_user_id}`}
                      <span className="text-xs text-muted-foreground ml-1">编号 {c.customer_user_id}</span>
                    </td>
                    <td className="p-3 text-xs">{c.phone_masked || '-'}</td>
                    <td className="p-3">
                      <Badge variant="outline">
                        {SOURCE_LABELS[c.binding_source] || sourceLabel(c.binding_source, 'agent')}
                      </Badge>
                    </td>
                    <td className="p-3 text-xs">{new Date(c.bound_at).toLocaleDateString()}</td>
                    <td className="p-3 text-right font-mono">{formatPoints(c.tool_credit)}</td>
                    <td className="p-3 text-right font-mono">{formatPoints(c.publish_credit)}</td>
                    <td className="p-3 text-right font-mono">{formatPoints(c.bonus_credit)}</td>
                    <td className="p-3">
                      {c.dispute_status === 'pending' && (
                        <Badge variant="destructive">
                          <AlertCircle className="w-3 h-3 mr-1" />
                          冲突待裁决
                        </Badge>
                      )}
                      {!c.dispute_status && <span className="text-xs text-green-600">正常</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* 隐藏:海报导出真身(540×720 原尺寸 · html2canvas 捕获此节点) */}
      <div style={{ position: 'fixed', left: -99999, top: 0, pointerEvents: 'none' }} aria-hidden>
        <PromotionPoster
          ref={exportRef}
          brand={posterBrand}
          qrDataUrl={qrDataUrl}
          sellingPoint={sellingPoint}
          showServicePoints={showServicePoints}
        />
      </div>
    </div>
  );
}
