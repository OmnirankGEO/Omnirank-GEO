/**
 * 算力充值页(旧版 · 已废弃)
 * 5 个固定档位 + 自定义金额 + 虎皮椒微信支付(扫码/H5)+ 轮询订单状态
 *
 * @deprecated [2026-06-07] 此页面已废弃 · 不再用于创建订单
 *   · 走平台 RECHARGE_PACKAGES 硬编码价 · 违反铁律 feedback_invited_user_inherits_inviter_markup
 * @migration_path 新流量统一走 `/customer/recharge` → `BuyCredit.tsx`(V3.5 W3 SKU SSOT 路径)
 *   · 最终价格由新版购买页向平台报价接口获取
 *   · 未绑定客户/服务商 → direct_fallback 平台 SKU
 *   · 支付接入:微信内 JSAPI / PC Native / 手机外部 xunhupay 走后端统一 UA 路由
 * @kept_for 兼容老二维码 / 老分享链接 / 历史 scheduler 提醒消息 / 老路由收藏
 *   · 路由 `/wallet/recharge` 不删 · 进入页面 banner 强引导 + handleRecharge 禁创建新订单
 *   · 后端 RECHARGE_PACKAGES + /api/wallet/recharge 端点保留(不阻断老订单 callback)
 * @new_features_freeze 本文件冻结 · 新功能与新档位全部去 BuyCredit · 不在此处加
 */
import { useState, useRef, useEffect } from 'react';
import { emitAgentEvent } from '@/lib/agentEvents';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { ArrowLeft, Check, Loader2, Sparkles, CheckCircle2, Clock, QrCode, AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { copyToClipboard } from '@/lib/copyUtils';
import { useWallet } from '@/context/WalletContext';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
// 2026-04-25 (CTO-15.9 wxpay 切直连) · JSAPI helper
import {
  isInWechatBrowser, getCachedOpenid, exchangeOpenid,
  createJsapiOrder, invokeWxPay, startOauthFlow,
} from '@/lib/wechatJsapi';

// ========== 充值档位（ID 对齐后端 RECHARGE_PACKAGES）==========

interface RechargeTier {
  id: string;
  price: number;
  basePoints: number;
  bonusPoints: number;
  totalPoints: number;
  label?: string;
}

// 2026-04-16 CTO-10 重构：赠送比例 10%/15%/20%/25%/30% 严格单调递增
// ¥500 热门推荐（触发 L1 累消门槛）/ ¥2000 触发 L2 累消门槛 / 删除性价比倒挂的旧档
const RECHARGE_TIERS: RechargeTier[] = [
  { id: 'trial',    price: 0,    basePoints: 0,      bonusPoints: 3888,  totalPoints: 3888,   label: '🎁 体验包' },
  { id: 'starter',  price: 30,   basePoints: 3900,   bonusPoints: 390,   totalPoints: 4290 },
  { id: 'popular',  price: 100,  basePoints: 13000,  bonusPoints: 1950,  totalPoints: 14950 },
  { id: 'pro',      price: 500,  basePoints: 65000,  bonusPoints: 13000, totalPoints: 78000,  label: '🔥 热门 · 服务商' },
  { id: 'flagship', price: 1000, basePoints: 130000, bonusPoints: 32500, totalPoints: 162500 },
  { id: 'premium',  price: 2000, basePoints: 260000, bonusPoints: 78000, totalPoints: 338000, label: '服务商' },
];

// 自定义金额参数（与后端 CUSTOM_AMOUNT_* 保持一致）
const CUSTOM_MIN = 1;
const CUSTOM_MAX = 10000;
const POINTS_PER_YUAN = 130;

function calcBonusRatio(amountYuan: number): number {
  if (amountYuan < 1) return 0;
  if (amountYuan < 100) return 0.10;
  if (amountYuan < 500) return 0.15;
  if (amountYuan < 1000) return 0.20;
  if (amountYuan < 2000) return 0.25;
  return 0.30;
}

type PaymentMethod = 'wechat' | 'alipay';

// ========== 主组件 ==========

export default function RechargePage() {
  const navigate = useEmbeddedNavigate();
  const { bonusPoints, refreshBalance, status: walletStatus } = useWallet();

  // 兼容老收藏/二维码，但不再让旧页成为可交互的下单入口。
  useEffect(() => {
    navigate('/customer/recharge', { replace: true });
  }, [navigate]);

  const [selectedTier, setSelectedTier] = useState<string | null>(null);
  const [customAmount, setCustomAmount] = useState<string>('');  // 自定义金额字符串（允许空）
  const [paymentMethod, setPaymentMethod] = useState<PaymentMethod>('wechat');
  const [submitting, setSubmitting] = useState(false);

  // 自定义金额解析 + 实时换算
  // [Q12 2026-07-12] 以下 customBase/customBonus/customTotal 是浏览器端「预估」展示,不是权威值。
  //   本页已废弃(handleRecharge 不再创建订单 · 只 navigate 到新版 /customer/recharge),
  //   真实到账算力一律由后端在下单时计算(新版 BuyCredit + 后端 SKU/CUSTOM_AMOUNT_* SSOT),
  //   浏览器绝不把这里算出的数字当权威提交。UI 已明标「预估 · 以支付页为准」。
  const customAmountNum = parseFloat(customAmount) || 0;
  const customValid = customAmountNum >= CUSTOM_MIN && customAmountNum <= CUSTOM_MAX;
  const customBase = Math.floor(customAmountNum * POINTS_PER_YUAN);
  const customBonus = Math.floor(customBase * calcBonusRatio(customAmountNum));
  const customTotal = customBase + customBonus;
  const customBonusPct = Math.round(calcBonusRatio(customAmountNum) * 100);

  // 支付弹窗状态
  const [orderData, setOrderData] = useState<{
    order_id: string;
    payment_url: string | null;
    payment_type: 'qrcode' | 'h5_redirect';
    amount_yuan: number;
    total_points: number;
    expire_minutes: number;
  } | null>(null);
  const [showPaymentDialog, setShowPaymentDialog] = useState(false);
  const [paymentStatus, setPaymentStatus] = useState<'pending' | 'paid' | 'expired'>('pending');

  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const completionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 体验包领取判断：localStorage 标记 或 bonusPoints > 0
  const freeClaimed = walletStatus !== 'ready' && walletStatus !== 'stale'
    ? true
    : localStorage.getItem('omnirank_trial_claimed') === '1' || bonusPoints > 0;

  // 清理轮询
  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
      if (completionTimerRef.current) clearTimeout(completionTimerRef.current);
    };
  }, []);

  const startPolling = (orderId: string) => {
    if (pollingRef.current) clearInterval(pollingRef.current);

    let attempts = 0;
    // [Deploy-CTO 2026-04-25] 轮询 3 秒一次(原 10 秒)· 配合后端 5s 主动 query 兜底
    // 用户付款后 ~3-6 秒可见 paid · 接近 callback 5 秒体验
    const maxAttempts = 300; // 15分钟 / 3秒

    pollingRef.current = setInterval(async () => {
      attempts++;
      if (attempts > maxAttempts) {
        clearInterval(pollingRef.current!);
        setPaymentStatus('expired');
        toast.error('订单已过期');
        return;
      }

      try {
        const res = await authFetch(`/api/wallet/order-status/${orderId}`);
        if (res.ok) {
          const { data } = await res.json();
          if (data.status === 'paid') {
            clearInterval(pollingRef.current!);
            setPaymentStatus('paid');
            toast.success('支付成功！算力已到账');
            localStorage.setItem('omnirank_trial_claimed', '1');
            emitAgentEvent('recharge_completed', { amount: data.total_points });
            await refreshBalance();
            // v3.2: 如果在 C 端分屏打开（iframe 内），通知父窗口刷新 Header 余额
            if (window.parent !== window) {
              window.parent.postMessage({ type: 'wallet_changed' }, window.location.origin);
            }
            completionTimerRef.current = setTimeout(() => {
              setShowPaymentDialog(false);
              navigate('/wallet');
            }, 2000);
          }
        }
      } catch {
        // 网络错误不中断轮询
      }
    }, 3000);
  };

  const handleRecharge = async () => {
    // [2026-06-07 P2 废弃强引导] 老页禁止创建新订单 · 强引导新版 /customer/recharge
    //   · 防绕过服务商系数(老页 RECHARGE_TIERS 是平台硬编码 · 不读 agent_sku_overrides)
    //   · 老订单 callback 不依赖此函数(支付回调走后端 webhook 写 DB · 余额自动到账)
    //   · 原 V3.4 创建订单 + JSAPI/Native/H5/xunhupay 路由逻辑已删除 · 仅供 git history 回滚参考
    //     (commit a18acc04 之前版本完整保留)
    toast.info('请前往新版“购买算力”页面查看平台当前配置的最终价格', { duration: 3000 });
    navigate('/customer/recharge');
  };

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* 返回标题 */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={() => navigate('/wallet')}>
          <ArrowLeft className="size-5" />
        </Button>
        <h1 className="text-2xl font-bold text-foreground">算力充值</h1>
      </div>

      {/* [2026-06-07 P2 废弃 banner] 老页禁创建新订单 · 强引导新版「购买算力」(BuyCredit) ·
          新页会显示平台当前配置的最终价格 · 路由保留兼容老二维码/分享链接 */}
      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 flex items-start gap-3">
        <AlertTriangle className="size-5 text-amber-500 shrink-0 mt-0.5" />
        <div className="flex-1 space-y-2">
          <p className="font-semibold text-amber-700 dark:text-amber-300">
            ⚠️ 此页面已是旧版 · 这里的价格不可用于下单
          </p>
          <p className="text-sm text-muted-foreground leading-relaxed">
            新版「购买算力」已上线 · 最终价格由 OmniRank 平台提供 · 此页保留仅为兼容老二维码/分享链接 · 不能再创建新充值订单。
          </p>
          <Button
            size="sm"
            className="bg-amber-500 hover:bg-amber-600 text-white"
            onClick={() => navigate('/customer/recharge')}
          >
            前往新版「购买算力」→
          </Button>
        </div>
      </div>

      {/* 2026-04-25 (CTO-15.9) · 引导卡 · 手机非微信用户切到微信内更丝滑 */}
      {!isInWechatBrowser()
        && /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)
        && (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm flex items-start gap-2.5">
          <Sparkles className="size-4 text-emerald-500 shrink-0 mt-0.5" />
          <div className="flex-1 leading-relaxed">
            <p className="font-medium text-emerald-700 dark:text-emerald-400">
              💡 在微信内打开本页可使用微信直连付款 · 更快更稳
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              当前浏览器走兜底通道 · 仍能正常付款。复制链接到微信打开 → 自动弹付款层。
            </p>
            <button
              type="button"
              className="text-xs text-emerald-600 dark:text-emerald-400 underline mt-1.5"
              onClick={async () => {
                const ok = await copyToClipboard(window.location.href);
                if (ok) {
                  toast.success('链接已复制 · 微信内粘贴打开');
                } else {
                  toast.info('请手动复制地址栏链接到微信');
                }
              }}
            >
              复制本页链接
            </button>
          </div>
        </div>
      )}

      {/* 套餐选择 */}
      <div>
        <h2 className="text-sm font-medium text-muted-foreground mb-3">选择套餐</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {RECHARGE_TIERS.map((tier) => {
            const isSelected = selectedTier === tier.id;
            const isFreeDisabled = tier.price === 0 && freeClaimed;

            return (
              <Card
                key={tier.id}
                className={cn(
                  'cursor-pointer transition-all duration-200 relative',
                  isSelected && 'border-foreground ring-1 ring-foreground/20',
                  isFreeDisabled && 'opacity-50 cursor-not-allowed',
                  !isSelected && !isFreeDisabled && 'hover:border-foreground/40'
                )}
                onClick={() => {
                  if (!isFreeDisabled) setSelectedTier(tier.id);
                }}
              >
                <CardContent className="p-4">
                  {/* 选中标记 */}
                  {isSelected && (
                    <div className="absolute top-3 right-3 size-5 rounded-full bg-foreground flex items-center justify-center">
                      <Check className="size-3 text-background" />
                    </div>
                  )}

                  {/* 标签 */}
                  {tier.label && (
                    <div className="flex items-center gap-1 mb-2">
                      <Sparkles className="size-3 text-amber-400" />
                      <span className="text-xs font-medium text-amber-400">
                        {isFreeDisabled ? '体验包 (已领取)' : tier.label}
                      </span>
                    </div>
                  )}

                  {/* 价格 */}
                  <div className="mb-3">
                    {tier.price === 0 ? (
                      <span className="text-2xl font-bold text-foreground">免费</span>
                    ) : (
                      <span className="text-2xl font-bold text-foreground">
                        ¥{tier.price.toLocaleString()}
                      </span>
                    )}
                  </div>

                  {/* 积分详情 */}
                  <div className="space-y-1 text-sm">
                    {tier.basePoints > 0 && (
                      <div className="flex justify-between text-muted-foreground">
                        <span>基础算力</span>
                        <span className="tabular-nums">{tier.basePoints.toLocaleString()}</span>
                      </div>
                    )}
                    <div className="flex justify-between text-muted-foreground">
                      <span>赠送算力</span>
                      <span className="tabular-nums text-amber-400">+{tier.bonusPoints.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between font-medium text-foreground pt-1 border-t border-border/50">
                      <span>总计</span>
                      <span className="tabular-nums">{tier.totalPoints.toLocaleString()}</span>
                    </div>
                  </div>

                  {/* 单价 */}
                  {tier.price > 0 && (
                    <p className="text-xs text-muted-foreground mt-2">
                      约 {(tier.totalPoints / tier.price).toFixed(0)} 算力/元
                    </p>
                  )}
                </CardContent>
              </Card>
            );
          })}

          {/* 自定义金额卡片 */}
          <Card
            className={cn(
              'cursor-pointer transition-all duration-200 relative',
              selectedTier === 'custom' && 'border-foreground ring-1 ring-foreground/20',
              selectedTier !== 'custom' && 'hover:border-foreground/40'
            )}
            onClick={() => setSelectedTier('custom')}
          >
            <CardContent className="p-4">
              {selectedTier === 'custom' && (
                <div className="absolute top-3 right-3 size-5 rounded-full bg-foreground flex items-center justify-center">
                  <Check className="size-3 text-background" />
                </div>
              )}
              <div className="flex items-center gap-1 mb-2">
                <Sparkles className="size-3 text-sky-400" />
                <span className="text-xs font-medium text-sky-400">自定义金额</span>
              </div>
              <div className="mb-3 flex items-baseline gap-1">
                <span className="text-2xl font-bold text-foreground">¥</span>
                <input
                  type="number"
                  inputMode="decimal"
                  min={CUSTOM_MIN}
                  max={CUSTOM_MAX}
                  step="0.01"
                  value={customAmount}
                  onChange={(e) => {
                    setCustomAmount(e.target.value);
                    setSelectedTier('custom');
                  }}
                  onClick={(e) => e.stopPropagation()}
                  placeholder={`${CUSTOM_MIN}-${CUSTOM_MAX}`}
                  className="bg-transparent text-2xl font-bold text-foreground border-b border-border/50 focus:outline-none focus:border-foreground w-full min-w-0"
                />
              </div>
              <div className="space-y-1 text-sm min-h-[70px]">
                {customValid ? (
                  <>
                    <div className="flex justify-between text-muted-foreground">
                      <span>基础算力</span>
                      <span className="tabular-nums">{customBase.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between text-muted-foreground">
                      <span>赠送 {customBonusPct}%</span>
                      <span className="tabular-nums text-amber-400">+{customBonus.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between font-medium text-foreground pt-1 border-t border-border/50">
                      <span>总计</span>
                      <span className="tabular-nums">{customTotal.toLocaleString()}</span>
                    </div>
                    {/* [Q12 2026-07-12] 浏览器端预估 · 实际到账以支付页(后端计算)为准 */}
                    <p className="text-[11px] text-muted-foreground/70 pt-0.5">预估 · 以支付页为准</p>
                  </>
                ) : (
                  <p className="text-xs text-muted-foreground/70 leading-relaxed">
                    输入 ¥{CUSTOM_MIN} - ¥{CUSTOM_MAX} 任意金额<br />
                    按梯度送 10% - 30% 算力
                  </p>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>

      {/* 支付方式 */}
      {selectedTier && RECHARGE_TIERS.find(t => t.id === selectedTier)?.price !== 0 && (
        <div>
          <h2 className="text-sm font-medium text-muted-foreground mb-3">支付方式</h2>
          <div className="flex gap-3">
            <PaymentOption
              label="微信支付"
              value="wechat"
              selected={paymentMethod === 'wechat'}
              onSelect={() => setPaymentMethod('wechat')}
              color="text-emerald-400"
            />
          </div>
        </div>
      )}

      {/* 确认充值 */}
      <div className="flex justify-end">
        <Button
          className="bg-foreground text-background hover:bg-foreground/90 min-w-[120px]"
          size="lg"
          disabled={
            !selectedTier ||
            submitting ||
            (selectedTier === 'custom' && !customValid)
          }
          onClick={handleRecharge}
        >
          {submitting ? (
            <Loader2 className="size-4 animate-spin" />
          ) : selectedTier === 'custom' && customValid ? (
            `充值 ¥${customAmountNum.toFixed(2)}`
          ) : (
            '充值'
          )}
        </Button>
      </div>

      {/* 微信支付二维码弹窗 */}
      <Dialog open={showPaymentDialog} onOpenChange={(open) => {
        if (!open && paymentStatus === 'pending') {
          if (pollingRef.current) clearInterval(pollingRef.current);
        }
        setShowPaymentDialog(open);
      }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {paymentStatus === 'paid' ? '支付成功' : '微信扫码支付'}
            </DialogTitle>
          </DialogHeader>

          <div className="flex flex-col items-center gap-4 py-4">
            {paymentStatus === 'paid' ? (
              <>
                <CheckCircle2 className="size-16 text-green-400" />
                <p className="text-lg font-medium text-foreground">
                  充值成功，{orderData?.total_points.toLocaleString()} 算力已到账
                </p>
              </>
            ) : paymentStatus === 'expired' ? (
              <>
                <Clock className="size-16 text-muted-foreground" />
                <p className="text-lg font-medium text-foreground">订单已过期</p>
                <Button onClick={() => setShowPaymentDialog(false)}>关闭</Button>
              </>
            ) : (
              <>
                {orderData?.payment_url ? (
                  <div className="bg-white p-4 rounded-xl">
                    {/* 虎皮椒返的 url_qrcode 本身就是生成好的二维码图片 URL，直接展示不要再二次编码 */}
                    {/^https?:\/\//i.test(orderData.payment_url) ? (
                      <img
                        src={orderData.payment_url}
                        alt="微信支付二维码"
                        className="size-[200px] object-contain"
                        onError={(e) => {
                          // 兜底：如果图片加载失败，退化到用第三方服务把 URL 编码成二维码
                          (e.target as HTMLImageElement).src =
                            `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(orderData.payment_url!)}`;
                        }}
                      />
                    ) : (
                      /* 非 http 协议（例如 weixin://），用 qrserver 现场编码 */
                      <img
                        src={`https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(orderData.payment_url)}`}
                        alt="微信支付二维码"
                        className="size-[200px]"
                      />
                    )}
                  </div>
                ) : (
                  <div className="size-[200px] bg-card rounded-xl flex items-center justify-center">
                    <QrCode className="size-16 text-muted-foreground" />
                  </div>
                )}

                <div className="text-center space-y-2 w-full">
                  <p className="text-2xl font-bold text-foreground">
                    ¥{orderData?.amount_yuan}
                  </p>
                  {/* 2026-04-25 第 2 版 (Deploy-CTO) · 分清手机 / PC 引导 · 去误导按钮 */}
                  {(() => {
                    const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
                    const inWeixin = /MicroMessenger/i.test(navigator.userAgent);
                    const isIOS = /iPhone|iPad|iPod/i.test(navigator.userAgent);

                    if (isMobile && !inWeixin) {
                      // 手机非微信浏览器 · 4 步引导
                      return (
                        <div className="bg-muted/50 rounded-lg p-3 mt-2 text-left space-y-1.5">
                          <p className="text-sm font-medium text-foreground text-center mb-2">
                            📱 手机用户 · 4 步完成
                          </p>
                          <div className="flex items-start gap-2 text-xs text-foreground">
                            <span className="font-bold text-primary shrink-0">①</span>
                            <span>
                              <strong>长按上方二维码</strong>
                              {isIOS ? '(按住 1 秒)' : ''} → 选「保存图片」
                            </span>
                          </div>
                          <div className="flex items-start gap-2 text-xs text-foreground">
                            <span className="font-bold text-primary shrink-0">②</span>
                            <span>打开<strong>微信</strong> → 右上角「+」→「扫一扫」</span>
                          </div>
                          <div className="flex items-start gap-2 text-xs text-foreground">
                            <span className="font-bold text-primary shrink-0">③</span>
                            <span>点扫一扫页面右上角的「<strong>相册</strong>」图标</span>
                          </div>
                          <div className="flex items-start gap-2 text-xs text-foreground">
                            <span className="font-bold text-primary shrink-0">④</span>
                            <span>选刚保存的二维码图片 → 微信弹付款 → 输密码</span>
                          </div>
                        </div>
                      );
                    }
                    // PC 引导
                    return (
                      <div className="bg-muted/50 rounded-lg p-3 mt-2">
                        <p className="text-sm text-foreground">
                          💻 用<strong>手机微信</strong>「扫一扫」对准上方二维码
                        </p>
                        <p className="text-xs text-muted-foreground mt-1">
                          5 分钟内有效 · 付款后自动跳转
                        </p>
                      </div>
                    );
                  })()}
                </div>

                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3 animate-spin" />
                  <span>等待支付中...</span>
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ========== 子组件 ==========

function PaymentOption({
  label,
  value: _value,
  selected,
  onSelect,
  color,
  disabled,
}: {
  label: string;
  value: string;
  selected: boolean;
  onSelect: () => void;
  color: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onSelect}
      disabled={disabled}
      className={cn(
        'flex items-center gap-2 px-4 py-3 rounded-lg border transition-all text-sm font-medium',
        disabled && 'opacity-50 cursor-not-allowed',
        selected && !disabled
          ? 'border-foreground bg-foreground/5 text-foreground'
          : 'border-border bg-card text-muted-foreground hover:border-foreground/40'
      )}
    >
      <div className={cn('size-4 rounded-full border-2 flex items-center justify-center',
        selected && !disabled ? 'border-foreground' : 'border-muted-foreground/40'
      )}>
        {selected && !disabled && <div className="size-2 rounded-full bg-foreground" />}
      </div>
      <span className={cn(selected && !disabled && color)}>{label}</span>
    </button>
  );
}
