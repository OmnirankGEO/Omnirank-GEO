/**
 * 🗄️ [归档 2026-07-28 · 推广页统一工单] 本页已由 pages/Referral/InviteCenter.tsx
 * (普通用户版,按 /agent/promotion 新推广中心骨架)取代,路由 /referral 已切走。
 * 按回滚参考惯例**保留不删**;CommissionPanel 仍被本目录引用与测试锁定,勿动。
 *
 * ReferralCenter - 推荐中心主页面
 * 包含推荐概览和收益明细两个 Tab
 */
import { useState, useEffect } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { copyToClipboard } from "@/lib/copyUtils";
import {
  Users,
  Coins,
  TrendingUp,
  Copy,
  Share2,
  Link as LinkIcon,
  Loader2,
  AlertCircle,
  Gift,
  CheckCircle2,
  QrCode,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { authFetch } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { usePartnerFlag } from "@/hooks/usePartnerFlag";
import { CommissionPanel } from "./CommissionPanel";
import { SharePosterDialog } from "@/components/share/SharePosterDialog";

// ========== 类型定义 ==========

interface ReferralInfo {
  code: string;
  link: string;
  total_referred: number;
  total_commission: number;
  monthly_commission?: number;
}

// ========== 推荐码卡片 ==========

function ReferralCodeCard({ info, isAgent }: { info: ReferralInfo; isAgent: boolean }) {
  const [copied, setCopied] = useState<"code" | "link" | null>(null);

  // [WO_IOS_TOUCH_UX 2026-08-05] ⚠️ 本页已归档(路由 /referral 已切到 InviteCenter),
  //   这处改动**没有用户价值**,只为让手势链门禁能断言 0 —— 留着它门禁就永远红一条。
  //   病灶是 `await import()`:文本本来就在手里,纯粹因为动态导入把手势耗掉了。改静态 import。
  const handleCopy = (text: string, type: "code" | "link") => {
    void copyToClipboard(text).then((ok) => {
      if (ok) { setCopied(type); setTimeout(() => setCopied(null), 2000); }
    });
  };

  return (
    <Card className="bg-card border-border rounded-xl">
      <CardHeader>
        <CardTitle className="text-foreground flex items-center gap-2">
          <Gift className="h-5 w-5" />
          我的推荐码
        </CardTitle>
        <CardDescription className="text-muted-foreground">
          {isAgent
            ? '把你的链接发给客户。客户购买算力包后，你的收益会进入「提现结算」。'
            : '邀请朋友使用平台，你会获得奖励算力。奖励自动到账，可用于平台工具。'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 推荐码 */}
        <div className="space-y-2">
          <label className="text-xs text-muted-foreground">推荐码</label>
          <div className="flex items-center gap-2">
            <div className="flex-1 rounded-lg bg-muted px-4 py-2.5 font-mono text-lg text-foreground tracking-wider">
              {info.code}
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleCopy(info.code, "code")}
              className="rounded-lg shrink-0"
            >
              {copied === "code" ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        {/* 推荐链接 */}
        <div className="space-y-2">
          <label className="text-xs text-muted-foreground">推荐链接</label>
          <div className="flex items-center gap-2">
            <div className="flex-1 rounded-lg bg-muted px-4 py-2.5 text-sm text-muted-foreground truncate">
              {info.link}
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleCopy(info.link, "link")}
              className="rounded-lg shrink-0"
            >
              {copied === "link" ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              ) : (
                <LinkIcon className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ========== 统计卡片 ==========

function StatsCards({ info, isAgent }: { info: ReferralInfo; isAgent: boolean }) {
  // 普通用户:只看推荐人数(奖励额度在下方「奖励明细」按额度展示,不在此处露「元」)
  // 代理:可看收益金额(元)
  const stats = [
    {
      label: "推荐人数",
      value: info.total_referred,
      suffix: "人",
      icon: Users,
    },
    ...(isAgent
      ? [
          {
            label: "累计收益",
            value: (info.total_commission ?? 0).toFixed(2),
            suffix: "元",
            icon: Coins,
          },
          {
            label: "本月收益",
            value: (info.monthly_commission ?? 0).toFixed(2),
            suffix: "元",
            icon: TrendingUp,
          },
        ]
      : []),
  ];

  return (
    <div className={cn("grid grid-cols-1 gap-4", isAgent && "sm:grid-cols-3")}>
      {stats.map((s) => (
        <Card key={s.label} className="bg-card border-border rounded-xl">
          <CardContent className="p-4 flex items-center gap-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted shrink-0">
              <s.icon className="h-5 w-5 text-muted-foreground" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{s.label}</p>
              <p className="text-xl font-semibold text-foreground">
                {s.value}
                <span className="text-sm font-normal text-muted-foreground ml-1">
                  {s.suffix}
                </span>
              </p>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ========== 推荐规则说明 ==========

function ReferralRules({ isAgent }: { isAgent: boolean }) {
  const { enabled: partnerFlagEnabled } = usePartnerFlag();
  const rules = isAgent
    ? [
        { step: "1", title: "把链接发给客户", desc: "将你的专属推荐码 / 链接发给客户" },
        { step: "2", title: "客户购买算力包", desc: "客户通过你的链接注册并购买算力包" },
        {
          step: "3",
          title: "获得服务收益",
          desc: "客户成交后，系统按你设置的售价和服务成本计算收益，进入「提现结算」（可转算力 / 提现）",
        },
      ]
    : [
        { step: "1", title: "分享推荐码", desc: "把推荐码 / 海报分享给朋友" },
        { step: "2", title: "朋友注册使用", desc: "朋友通过你的链接注册并使用平台" },
        {
          step: "3",
          title: "获得奖励算力",
          desc: "朋友充值后你获得奖励算力，自动到账，可用于平台工具",
        },
      ];

  return (
    <Card className="bg-card border-border rounded-xl">
      <CardHeader>
        <CardTitle className="text-foreground text-base">
          {isAgent ? '服务收益规则' : '邀请奖励规则'}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {rules.map((r) => (
            <div
              key={r.step}
              className="flex items-start gap-3 rounded-lg bg-muted p-4"
            >
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-foreground text-background text-sm font-semibold shrink-0">
                {r.step}
              </div>
              <div>
                <p className="text-sm font-medium text-foreground">
                  {r.title}
                </p>
                <p className="text-xs text-muted-foreground mt-1">{r.desc}</p>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-4 rounded-lg bg-muted p-3 space-y-1">
          {isAgent ? (
            <>
              <p className="text-xs text-muted-foreground">
                收益进入 <span className="text-emerald-400">「提现结算」</span>，可转算力或提现
              </p>
              <p className="text-xs text-muted-foreground">
                推荐关系长期绑定，客户后续购买持续产生收益
              </p>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              奖励直接发到赠送算力，不可提现但可抵扣平台工具（注册即送体验算力，朋友充值再叠加）
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ========== 主组件 ==========

// ============================================
// L0 普通用户返利明细 Tab
// ============================================

interface BonusRecord {
  id: number;
  recharge_yuan: number;
  rate: number;
  bonus_points: number;
  referred_name: string;
  created_at: string;
}

function BonusHistoryPanel() {
  const { enabled: partnerFlagEnabled } = usePartnerFlag();
  const [loading, setLoading] = useState(true);
  const [records, setRecords] = useState<BonusRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [count, setCount] = useState(0);
  const [suspicious, setSuspicious] = useState(0);

  useEffect(() => {
    authFetch('/api/referral/bonus-history?limit=30')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.success) {
          setRecords(data.data.records || []);
          setTotal(data.data.total_bonus_points || 0);
          setCount(data.data.total_count || 0);
          setSuspicious(data.data.suspicious_count || 0);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* 汇总 */}
      <Card>
        <CardContent className="py-5">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-xs text-muted-foreground mb-1">累计获得返利</div>
              <div className="text-2xl font-bold text-amber-400">
                {total.toLocaleString()} 赠送算力
              </div>
              <div className="text-[11px] text-muted-foreground mt-1">
                可直接消费平台 AI 功能
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground mb-1">朋友充值次数</div>
              <div className="text-2xl font-bold text-foreground">{count} 笔</div>
              {suspicious > 0 && (
                <div className="text-[11px] text-amber-400 mt-1">
                  另有 {suspicious} 笔被退款已标记
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 说明 */}
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-muted-foreground">
        💡 <span className="text-amber-400 font-medium">
          {partnerFlagEnabled ? '成为服务商后' : '升级服务商后'}
        </span>：
        {partnerFlagEnabled
          ? '（通过「合作伙伴计划」申请身份证实名审核）'
          : ''}
        新充值产生可提现的服务收益；历史朋友的首次充值按当时身份结算，不补差额。
      </div>

      {/* 明细列表 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">返利明细</CardTitle>
        </CardHeader>
        <CardContent>
          {records.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              还没有返利记录。<br />
              复制上面推荐码分享给朋友，朋友充值后你即时获得奖励算力。
            </div>
          ) : (
            <div className="space-y-2">
              {records.map(r => (
                <div
                  key={r.id}
                  className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-foreground truncate">{r.referred_name}</div>
                    <div className="text-[11px] text-muted-foreground">
                      {new Date(r.created_at).toLocaleString('zh-CN')} · 充值 ¥{r.recharge_yuan.toFixed(0)}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-sm font-medium text-amber-400">
                      +{r.bonus_points.toLocaleString()} 奖励算力
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ============================================

export function ReferralCenter() {
  const { user } = useAuth();
  const isAgent = (user?.agent_level ?? 0) >= 1;
  const [info, setInfo] = useState<ReferralInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [isPlaceholder, setIsPlaceholder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPoster, setShowPoster] = useState(false);
  const [posterType, setPosterType] = useState<"register" | "register_social">("register");

  useEffect(() => {
    const fetchReferralInfo = async () => {
      setLoading(true);
      setError(null);
      try {
        // 并行请求推荐码和统计数据
        const [codeRes, statsRes] = await Promise.all([
          authFetch("/api/referral/code"),
          authFetch("/api/referral/stats"),
        ]);
        if (codeRes.status === 404 && statsRes.status === 404) {
          setIsPlaceholder(true);
          return;
        }
        const codeData = codeRes.ok ? await codeRes.json() : { success: false };
        const statsData = statsRes.ok ? await statsRes.json() : { success: false };

        const code = codeData?.data?.code || "";
        const stats = statsData?.data || {};
        const origin = window.location.origin;

        setInfo({
          code,
          link: code ? `${origin}/register?ref=${code}` : "",
          total_referred: (stats.l1_count || 0) + (stats.l2_count || 0),
          total_commission: stats.total_commission || 0,
          monthly_commission: stats.month_commission || 0,
        });
      } catch (err: unknown) {
        setError(
          err instanceof Error ? err.message : "加载失败，请稍后重试"
        );
      } finally {
        setLoading(false);
      }
    };
    fetchReferralInfo();
  }, []);

  // 加载状态
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // 占位 UI
  if (isPlaceholder) {
    return (
      <div className="p-4 sm:p-6 max-w-4xl mx-auto">
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
            <Gift className="h-8 w-8 text-muted-foreground" />
          </div>
          <h2 className="text-xl font-semibold text-foreground mb-2">
            推荐中心即将上线
          </h2>
          <p className="text-sm text-muted-foreground max-w-md">
            推荐好友注册并使用平台，即可获得奖励。推荐功能正在紧锣密鼓开发中，敬请期待。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-4 sm:p-6 max-w-5xl mx-auto space-y-6">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">推荐有礼</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {isAgent
              ? '把链接发给客户，客户购买算力包后你获得服务收益（可结算 / 提现）'
              : '邀请朋友使用平台，获得奖励算力（可消费）'}
          </p>
        </div>
        {/* [CTO-13.3 2026-04-20] 两按钮包 flex gap-2 整体作为 justify-between 右侧 child,
            避免三 flex child 被平均撑开 */}
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => { setPosterType("register"); setShowPoster(true); }}
            className="rounded-lg gap-2"
          >
            <QrCode className="h-4 w-4" />
            推给品牌主
          </Button>
          <Button
            onClick={() => { setPosterType("register_social"); setShowPoster(true); }}
            className="rounded-lg gap-2"
          >
            <QrCode className="h-4 w-4" />
            推给创作者
          </Button>
        </div>
      </div>

      <SharePosterDialog
        open={showPoster}
        onClose={() => setShowPoster(false)}
        type={posterType}
      />

      {/* 错误提示 */}
      {error && (
        <div className="flex items-center gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Tabs — 身份感知：代理看"收益明细"，普通用户看"奖励明细" */}
      <Tabs defaultValue="overview" className="space-y-6">
        <TabsList className="bg-muted rounded-lg">
          <TabsTrigger value="overview" className="rounded-md">
            推广概览
          </TabsTrigger>
          <TabsTrigger value="detail" className="rounded-md">
            {isAgent ? '收益明细' : '奖励明细'}
          </TabsTrigger>
        </TabsList>

        {/* 推广概览 */}
        <TabsContent value="overview" className="space-y-6">
          {info && (
            <>
              <ReferralCodeCard info={info} isAgent={isAgent} />
              <StatsCards info={info} isAgent={isAgent} />
              <ReferralRules isAgent={isAgent} />
            </>
          )}
        </TabsContent>

        {/* 明细 — 按身份切换组件 */}
        <TabsContent value="detail">
          {isAgent ? <CommissionPanel /> : <BonusHistoryPanel />}
        </TabsContent>
      </Tabs>
    </div>
  );
}
