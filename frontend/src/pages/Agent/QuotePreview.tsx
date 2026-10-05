/**
 * QuotePreview - 白标报价单预览与导出
 * 代理专属，从报价中心生成后跳转到此页面
 * 预览区域使用白色背景（独立于暗色主题），不含 OmniRank 品牌信息
 */
import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Download,
  Link2,
  Printer,
  ArrowLeft,
  Loader2,
  AlertCircle,
  FileText,
  CheckCircle2,
  Phone,
  MessageCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { authFetch } from "@/lib/api";
import { copyAsyncText } from "@/lib/copyUtils";
import { ManualCopyDialog } from "@/components/common/ManualCopyDialog";

// ========== 类型定义 ==========

interface QuoteService {
  name: string;
  quantity: number;
  unit_price: number;
  subtotal: number;
}

interface WhitelabelSettings {
  company_name: string;
  logo_url?: string;
  phone?: string;
  wechat?: string;
  slogan?: string;
  contact_name?: string;
}

interface QuoteData {
  services: QuoteService[];
  total_price: number;
  whitelabel: WhitelabelSettings;
  quote_id?: string;
  created_at?: string;
}

// ========== Toast 简易实现 ==========

function useToast() {
  const [message, setMessage] = useState<string | null>(null);

  const show = useCallback((msg: string) => {
    setMessage(msg);
    setTimeout(() => setMessage(null), 3000);
  }, []);

  const Toast = message ? (
    <div className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 rounded-lg bg-foreground text-background px-4 py-2 text-sm shadow-lg lg:bottom-6">
      {message}
    </div>
  ) : null;

  return { show, Toast };
}

// ========== 白标报价单预览（白色背景） ==========

function QuotePreviewCard({
  data,
  previewRef,
}: {
  data: QuoteData;
  previewRef: React.Ref<HTMLDivElement>;
}) {
  const { whitelabel, services, total_price } = data;

  return (
    <div
      ref={previewRef}
      className="bg-card text-foreground rounded-xl shadow-sm border border-border overflow-hidden print:shadow-none print:border-none print:rounded-none"
    >
      {/* 报价单头部 */}
      <div className="px-6 py-8 sm:px-10 sm:py-10 border-b border-border">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-2">
            {whitelabel.logo_url ? (
              <img
                src={whitelabel.logo_url}
                alt={whitelabel.company_name}
                className="h-10 w-auto object-contain"
              />
            ) : (
              <h2 className="text-xl font-bold text-foreground">
                {whitelabel.company_name || "公司名称"}
              </h2>
            )}
            {whitelabel.slogan && (
              <p className="text-sm text-muted-foreground">{whitelabel.slogan}</p>
            )}
          </div>
          <div className="text-right shrink-0">
            <p className="text-sm font-medium text-muted-foreground">报价单</p>
            {data.created_at && (
              <p className="text-xs text-muted-foreground mt-1">
                {new Date(data.created_at).toLocaleDateString("zh-CN")}
              </p>
            )}
            {data.quote_id && (
              <p className="text-xs text-muted-foreground mt-0.5">
                编号: {data.quote_id}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* 服务明细表格 */}
      <div className="px-6 py-6 sm:px-10">
        <h3 className="text-sm font-semibold text-foreground mb-4">服务明细</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-3 pr-4 font-medium text-muted-foreground">
                  服务项目
                </th>
                <th className="text-center py-3 px-4 font-medium text-muted-foreground">
                  数量
                </th>
                <th className="text-right py-3 px-4 font-medium text-muted-foreground">
                  单价 (元)
                </th>
                <th className="text-right py-3 pl-4 font-medium text-muted-foreground">
                  小计 (元)
                </th>
              </tr>
            </thead>
            <tbody>
              {services.map((svc, idx) => (
                <tr
                  key={idx}
                  className="border-b border-border/50 last:border-0"
                >
                  <td className="py-3 pr-4 text-foreground">{svc.name}</td>
                  <td className="py-3 px-4 text-center text-foreground">
                    {svc.quantity}
                  </td>
                  <td className="py-3 px-4 text-right text-foreground">
                    {svc.unit_price.toFixed(2)}
                  </td>
                  <td className="py-3 pl-4 text-right font-medium text-foreground">
                    {svc.subtotal.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* 合计 */}
        <div className="mt-6 flex justify-end">
          <div className="w-48 border-t-2 border-foreground pt-3">
            <div className="flex justify-between items-center">
              <span className="text-sm font-medium text-foreground">合计</span>
              <span className="text-xl font-bold text-foreground">
                {total_price.toFixed(2)} 元
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* 联系方式 */}
      {(whitelabel.phone || whitelabel.wechat || whitelabel.contact_name) && (
        <div className="px-6 py-6 sm:px-10 bg-muted border-t border-border">
          <h3 className="text-sm font-semibold text-foreground mb-3">
            联系方式
          </h3>
          <div className="flex flex-wrap gap-4 text-sm text-muted-foreground">
            {whitelabel.contact_name && (
              <span>联系人: {whitelabel.contact_name}</span>
            )}
            {whitelabel.phone && (
              <span className="flex items-center gap-1">
                <Phone className="h-3.5 w-3.5" />
                {whitelabel.phone}
              </span>
            )}
            {whitelabel.wechat && (
              <span className="flex items-center gap-1">
                <MessageCircle className="h-3.5 w-3.5" />
                微信: {whitelabel.wechat}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ========== 内部主组件 ==========

function QuotePreviewInner() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const previewRef = useRef<HTMLDivElement>(null);
  const toast = useToast();

  const [quoteData, setQuoteData] = useState<QuoteData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"pdf" | "link" | null>(null);
  // [WO_WHITELABEL_COPY_UX 项2] 剪贴板被拒但链接已拿到 → 弹可选中链接框
  const [manualCopyText, setManualCopyText] = useState<string | null>(null);

  const fetchQuoteData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // 尝试从 URL 参数获取 quote_id
      const quoteId = searchParams.get("id");

      // 同时获取白标设置
      const [whitelabelRes, quoteRes] = await Promise.all([
        authFetch("/api/referral/whitelabel"),
        quoteId
          ? authFetch(`/api/referral/quote/${quoteId}`)
          : Promise.resolve(null),
      ]);

      let whitelabel: WhitelabelSettings = {
        company_name: "我的公司",
      };

      if (whitelabelRes.ok) {
        const wlJson = await whitelabelRes.json();
        // [CTO-15.23 2026-05-20] 后端 api/referral_api.py:1234 返 {success, data: {...}} 嵌套 envelope
        // 之前直读 wlData.company_name 全 undefined · 报价单永远显示 "我的公司" fallback · 白标失效影响代理出单
        const wlData = (wlJson?.success && wlJson?.data) ? wlJson.data : (wlJson?.data ?? wlJson) || {};
        // [CTO-15.23 2026-05-20 follow-up] 后端 whitelabel_settings 字段名是 contact_phone/contact_wechat
        // 之前 wlData.phone / wlData.wechat 永远 undefined · 报价单电话/微信不显
        // 兼容 legacy 旧字段 phone/wechat(如果 BE 后续改名也不破)
        whitelabel = {
          company_name: wlData.company_name || "我的公司",
          logo_url: wlData.logo_url,
          phone: wlData.phone || wlData.contact_phone,
          wechat: wlData.wechat || wlData.contact_wechat,
          slogan: wlData.slogan,
          contact_name: wlData.contact_name,
        };
      }

      // 如果有 quoteId 且请求成功，用 API 数据
      if (quoteRes && quoteRes.ok) {
        const qData = await quoteRes.json();
        setQuoteData({
          services: qData.services || [],
          total_price: qData.total_price || 0,
          whitelabel,
          quote_id: qData.quote_id,
          created_at: qData.created_at,
        });
      } else {
        // 从 URL searchParams 解析内联数据
        const servicesParam = searchParams.get("services");
        const pricesParam = searchParams.get("prices");

        if (servicesParam) {
          try {
            const services: QuoteService[] = JSON.parse(
              decodeURIComponent(servicesParam)
            );
            // 如果有 prices 覆盖
            if (pricesParam) {
              const prices: Record<string, number> = JSON.parse(
                decodeURIComponent(pricesParam)
              );
              services.forEach((s) => {
                if (prices[s.name] !== undefined) {
                  s.unit_price = prices[s.name];
                  s.subtotal = s.unit_price * s.quantity;
                }
              });
            }
            const totalPrice = services.reduce(
              (sum, s) => sum + s.subtotal,
              0
            );
            setQuoteData({
              services,
              total_price: totalPrice,
              whitelabel,
              created_at: new Date().toISOString(),
            });
          } catch {
            setError("报价数据解析失败");
          }
        } else {
          // 没有有效数据源
          setError("未找到报价数据，请从报价中心重新生成");
        }
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [searchParams]);

  useEffect(() => {
    fetchQuoteData();
  }, [fetchQuoteData]);

  const handleExportPDF = () => {
    window.print();
    toast.show('请在打印对话框中选择"保存为 PDF"');
  };

  // [WO_WHITELABEL_COPY_UX 项2 2026-08-05] 复制必须在手势同步栈发起(iOS/微信 webview):
  // handler 不再 async,保存/取链接的网络请求全部装进 copyAsyncText 的 getText。
  const handleShareLink = () => {
    if (!quoteData) return;
    setExporting("link");
    void copyAsyncText(async () => {
      let quoteId = quoteData.quote_id;

      // 如果还没有 quote_id（从 URL params 内联数据来的），先保存到 DB
      if (!quoteId) {
        const genRes = await authFetch("/api/referral/generate-quote", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            services: quoteData.services,
            total_price: quoteData.total_price,
          }),
        });
        const genData = await genRes.json();
        quoteId = genData.data?.quote_id;
        if (quoteId) {
          setQuoteData(prev => prev ? { ...prev, quote_id: quoteId } : prev);
        }
      }

      if (!quoteId) throw new Error("保存报价单失败");

      const res = await authFetch("/api/referral/share-quote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quote_id: quoteId }),
      });
      const data = await res.json();
      if (!data.url) throw new Error(data.error || "生成链接失败");
      return data.url as string;
    }).then(({ ok, text, errorMessage }) => {
      if (ok) toast.show("链接已复制到剪贴板");
      else if (text) setManualCopyText(text);
      else toast.show(errorMessage || "生成链接失败");
    }).finally(() => setExporting(null));
  };

  const handlePrint = () => {
    window.print();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 sm:p-6 max-w-3xl mx-auto space-y-4">
        <div className="flex items-center gap-2 rounded-lg bg-destructive/10 p-4 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
        <Button
          variant="outline"
          onClick={() => navigate("/pricing")}
          className="rounded-lg"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          返回报价方案
        </Button>
      </div>
    );
  }

  if (!quoteData) return null;

  return (
    <div className="p-4 sm:p-6 max-w-4xl mx-auto space-y-6 pb-28 lg:pb-6">
      {/* 页面标题 (暗色主题) */}
      <div className="flex items-center justify-between print:hidden">
        <div>
          <h1 className="text-2xl font-bold text-foreground">白标报价单预览</h1>
          <p className="text-sm text-muted-foreground mt-1">
            预览并导出报价单，发送给您的客户
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => navigate("/pricing")}
          className="rounded-lg hidden sm:flex"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          返回报价方案
        </Button>
      </div>

      {/* 提示 */}
      <div className="rounded-lg bg-muted p-3 print:hidden">
        <p className="text-xs text-muted-foreground">
          以下报价单不包含任何平台方品牌信息，仅展示您的公司信息。
          可下载 PDF 或生成网页链接发送给客户。
        </p>
      </div>

      {/* 白色预览区域 */}
      <div className="overflow-x-auto">
        <QuotePreviewCard data={quoteData} previewRef={previewRef} />
      </div>

      {/* 操作按钮 - 桌面端 */}
      <div className="hidden sm:flex items-center justify-end gap-3 print:hidden">
        <Button
          variant="outline"
          onClick={handlePrint}
          className="rounded-lg"
        >
          <Printer className="mr-2 h-4 w-4" />
          打印
        </Button>
        <Button
          variant="outline"
          onClick={handleShareLink}
          disabled={exporting === "link"}
          className="rounded-lg"
        >
          {exporting === "link" ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Link2 className="mr-2 h-4 w-4" />
          )}
          复制链接
        </Button>
        <Button
          onClick={handleExportPDF}
          disabled={exporting === "pdf"}
          className="bg-foreground text-background hover:bg-foreground/90 rounded-lg"
        >
          {exporting === "pdf" ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Download className="mr-2 h-4 w-4" />
          )}
          下载 PDF
        </Button>
      </div>

      {/* 操作按钮 - 移动端固定底部 */}
      <div className="fixed bottom-0 left-0 right-0 bg-card border-t border-border p-3 flex items-center gap-2 sm:hidden print:hidden z-40">
        <Button
          variant="outline"
          size="sm"
          onClick={() => navigate("/pricing")}
          className="rounded-lg shrink-0"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={handlePrint}
          className="rounded-lg shrink-0"
        >
          <Printer className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={handleShareLink}
          disabled={exporting === "link"}
          className="rounded-lg flex-1"
        >
          {exporting === "link" ? (
            <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
          ) : (
            <Link2 className="mr-1.5 h-4 w-4" />
          )}
          复制链接
        </Button>
        <Button
          size="sm"
          onClick={handleExportPDF}
          disabled={exporting === "pdf"}
          className="bg-foreground text-background hover:bg-foreground/90 rounded-lg flex-1"
        >
          {exporting === "pdf" ? (
            <Loader2 className="mr-1.5 h-4 w-4 animate-spin" />
          ) : (
            <Download className="mr-1.5 h-4 w-4" />
          )}
          下载 PDF
        </Button>
      </div>

      {toast.Toast}
      <ManualCopyDialog text={manualCopyText} onClose={() => setManualCopyText(null)} />
    </div>
  );
}

// ========== 导出组件 ==========

/*
 * [#222 a1b'' 2026-09-17] 这里原来包着 <AgentLevelGate requiredLevel={1}>,把白标报价导出整页挡掉。
 *
 * Owner 09-17 原话「白标放开所有人」。菜单侧 WHITELABEL_ITEM 本来就同时挂在
 * 经营后台组与普通用户「资料」组(AppSidebar),路由 /agent/whitelabel 也自 2026-06-06
 * P1 起对所有 operator 开放 —— 唯独这一页的**导出**还关着,自相矛盾:
 * 能设置对外品牌、却不能用它导出报价。
 *
 * 🔴 当初把这一格列进「经营后台」清单的依据是「白标只在普通账号菜单里、放反了」,
 *    而那句话查下来不成立(活菜单两组都有;它是从全仓零引用的 Sidebar.tsx 读来的)。
 *    清单错了,据清单派生的门控也跟着错 —— 所以这不是"放宽",是撤掉一个建在错事实上的闸。
 */
export function QuotePreview() {
  return <QuotePreviewInner />;
}
