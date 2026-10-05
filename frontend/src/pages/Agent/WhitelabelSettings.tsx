/**
 * WhitelabelSettings — 对外品牌(白标)设置 · v3.6 门面页
 *
 * 路由: /agent/whitelabel(sidebar"经营后台 · 对外品牌"常显入口 · 阶段0)
 *
 * 客户可见品牌资料完整后自助生效；后台 OEM 换肤仍由平台治理。
 * 暂停后立即回退 OmniRank，历史订单和结算不受影响。
 *
 * 价格边界(老板约束5):本页只配置品牌字段 · 绝不含价格/系数 ·
 *   报价系数走 /account/profile(users.quote_markup_ratio)· 额度售价走 /agent/pricing(retail_cents)。
 * 授权位只能由管理员维护，本页不接收或展示内部判断依据。
 */
import { useState, useEffect, useRef } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Building2,
  Image as ImageIcon,
  Phone,
  MessageCircle,
  Mail,
  Type,
  Save,
  Loader2,
  AlertCircle,
  Eye,
  Paintbrush,
  Palette,
  Monitor,
  Lock,
  Clock,
  CheckCircle2,
  ShieldAlert,
  UploadCloud,
} from "lucide-react";
import { authFetch } from "@/lib/api";

// ========== 类型 ==========

type AuthState = "loading" | "unauthorized" | "editable" | "locked" | "active" | "suspended";
type WlMode = "oem" | "none";

// [板块 C · Owner 2026-07-22 D1] 前端 URL 预校验（与后端 _validate_brand_image_url 同规则 · 人话错误）
const BRAND_IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"];

function validateBrandImageUrlClient(raw: string): string | null {
  const url = raw.trim();
  if (!url) return null; // 空 = 清空，允许
  if (url.startsWith("/uploads/")) return null; // 本站上传（上传时已校验）
  const lower = url.toLowerCase();
  if (
    lower.startsWith("javascript:") ||
    lower.startsWith("data:") ||
    lower.startsWith("vbscript:") ||
    lower.startsWith("file:")
  ) {
    return "仅支持 https 图片地址，脚本/数据协议已被拦截";
  }
  if (!lower.startsWith("https://")) {
    return "仅支持 https:// 安全链接（也可点「上传 Logo」使用本地上传）";
  }
  const path = lower.split("?")[0];
  const dot = path.lastIndexOf(".");
  const ext = dot >= 0 ? path.slice(dot) : "";
  if (!BRAND_IMAGE_EXTS.includes(ext)) {
    return `仅支持 ${BRAND_IMAGE_EXTS.join(" / ")} 图片后缀`;
  }
  return null; // 有效性（可达/类型/大小）由后端 HEAD 探测兜底，失败会返回 400 人话
}

function validateBrandTextClient(raw: string, label: string, maxLen: number): string | null {
  const text = raw.trim();
  if (!text) return null;
  if (text.length > maxLen) return `${label}过长（最多 ${maxLen} 字）`;
  if (/[<>{}]/.test(text) || /javascript:|data:/i.test(text)) {
    return `${label}包含不允许的字符（请勿使用 <> 括号或脚本协议）`;
  }
  return null;
}

interface WhitelabelConfig {
  company_name: string;
  logo_url: string;
  slogan: string;
  brand_color: string;
  contact_name: string;
  contact_phone: string;
  contact_wechat: string;
  contact_email: string;
  // OEM 专属(代理后台换肤)
  product_name: string;
  favicon_url: string;
}

const defaultConfig: WhitelabelConfig = {
  company_name: "",
  logo_url: "",
  slogan: "",
  brand_color: "",
  contact_name: "",
  contact_phone: "",
  contact_wechat: "",
  contact_email: "",
  product_name: "",
  favicon_url: "",
};

// ========== 客户侧预览(用未保存的表单值实时渲染 · 不依赖后端) ==========

// [WO_WHITELABEL_COPY_UX 项3 2026-08-05] 字段 → 客户可见位置 逐处对应表。
// 服务商 #133 实证教训:product_name 之前只标"后台标题",但客户面诊断报告顶部
// 的品牌名恰恰**优先用产品名**(没填才回落公司名称)—— 哪个字段管哪一处必须直说。
function FieldSourceTag({ label }: { label: string }) {
  return (
    <span className="rounded border border-border bg-background/80 px-1 py-px text-[10px] leading-none text-muted-foreground">
      {label}
    </span>
  );
}

function CustomerPreview({ config, mode, state }: { config: WhitelabelConfig; mode: WlMode; state: AuthState }) {
  const accent = config.brand_color || "#6CBE1E";
  // 与后端一致的品牌名回落顺序:产品名 → 公司名称(premium 报告 TopNav / BrandName 同序)
  const reportBrandName = config.product_name || config.company_name || "您的公司名称";
  return (
    <Card className="bg-card border-border rounded-xl">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Eye className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-base text-foreground">客户看到的样子</CardTitle>
        </div>
        <CardDescription className="text-muted-foreground">
          报价单 / 诊断报告 / 客户门户 / 充值页 顶部都会用以下品牌 · 每一处标了来自哪个字段
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* [项3 禁做2] 未生效时预览必须明示,不许让人以为已经生效 */}
        {state !== "active" && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              以下是<strong>生效后</strong>的效果预览。当前品牌尚未生效,客户现在看到的仍是平台品牌
              {state === "suspended" ? "(白标已暂停)" : "(补全公司名称 + Logo 并保存后生效)"}。
            </span>
          </div>
        )}
        {/* 客户侧头部模拟(报价单 / 客户门户 / 充值页 同一头部结构) */}
        <div className="rounded-xl border border-border bg-secondary p-6 space-y-4">
          <div
            className="flex items-center justify-between border-b pb-4"
            style={{ borderColor: accent + "55" }}
          >
            <div className="flex items-center gap-3">
              <div className="flex flex-col items-center gap-1">
                {config.logo_url ? (
                  <img
                    src={config.logo_url}
                    alt="Logo"
                    className="h-10 w-10 rounded-lg object-cover"
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = "none";
                    }}
                  />
                ) : (
                  <div
                    className="h-10 w-10 rounded-lg flex items-center justify-center"
                    style={{ backgroundColor: accent + "22" }}
                  >
                    <Building2 className="h-5 w-5" style={{ color: accent }} />
                  </div>
                )}
                <FieldSourceTag label="Logo" />
              </div>
              <div>
                <p className="text-sm font-semibold text-foreground">
                  {config.company_name || "您的公司名称"}{" "}
                  <FieldSourceTag label="公司名称" />
                </p>
                {config.slogan && (
                  <p className="text-xs text-muted-foreground">
                    {config.slogan} <FieldSourceTag label="品牌标语" />
                  </p>
                )}
              </div>
            </div>
            <Badge
              className="border-0 text-white"
              style={{ backgroundColor: accent }}
            >
              报价单 / 门户 / 充值页
            </Badge>
          </div>

          <div className="space-y-2">
            <div className="h-3 bg-muted rounded w-3/4" />
            <div className="h-3 bg-muted rounded w-1/2" />
          </div>
          <div className="space-y-1">
            <div className="h-8 rounded" style={{ backgroundColor: accent + "33" }} />
            <div className="h-6 bg-muted/60 rounded" />
          </div>
          <div className="border-t border-border pt-4 flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
            {config.contact_name && (
              <span className="flex items-center gap-1">
                <Building2 className="h-3 w-3" />
                {config.contact_name}
              </span>
            )}
            {config.contact_phone && (
              <span className="flex items-center gap-1">
                <Phone className="h-3 w-3" />
                {config.contact_phone}
              </span>
            )}
            {config.contact_wechat && (
              <span className="flex items-center gap-1">
                <MessageCircle className="h-3 w-3" />
                {config.contact_wechat}
              </span>
            )}
            {config.contact_email && (
              <span className="flex items-center gap-1">
                <Mail className="h-3 w-3" />
                {config.contact_email}
              </span>
            )}
            {(config.contact_name || config.contact_phone || config.contact_wechat || config.contact_email) && (
              <FieldSourceTag label="联系人 / 电话 / 微信 / 邮箱 · 对客户自动脱敏" />
            )}
          </div>
        </div>

        {/* [项3] 诊断报告顶部品牌条:品牌名优先用产品名,没填才用公司名称(#133 教训) */}
        <div className="rounded-lg border border-border bg-secondary/60 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">诊断报告 · 顶部品牌条</p>
          <div className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2 w-fit max-w-full">
            {config.logo_url ? (
              <img
                src={config.logo_url}
                alt="Logo"
                className="h-5 w-5 rounded-sm object-cover"
                onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
              />
            ) : (
              <div className="h-5 w-5 rounded-sm" style={{ backgroundColor: accent + "33" }} />
            )}
            <span className="text-xs font-semibold text-foreground truncate">{reportBrandName}</span>
            <FieldSourceTag label={config.product_name ? "产品名" : "公司名称(未填产品名时)"} />
          </div>
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            报告顶部与浏览器标题的品牌名:填了「产品名」用产品名,否则用「公司名称」。
          </p>
        </div>

        {/* OEM 档:浏览器标签预览 */}
        {mode === "oem" && (
          <div className="rounded-lg border border-border bg-secondary/60 p-3">
            <p className="text-xs text-muted-foreground mb-2">你自己的后台浏览器标签(仅含后台档)</p>
            <div className="flex items-center gap-2 rounded-md bg-background border border-border px-3 py-1.5 w-fit max-w-full">
              {config.favicon_url ? (
                <img src={config.favicon_url} alt="favicon" className="h-4 w-4 rounded-sm object-cover" />
              ) : (
                <div className="h-4 w-4 rounded-sm" style={{ backgroundColor: accent }} />
              )}
              <span className="text-xs text-foreground truncate">
                {config.product_name || config.company_name || "您的产品名"} · 工作台
              </span>
              <FieldSourceTag label="产品名 + 网站图标" />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ========== 状态条 ==========

function StateBanner({ state, mode }: { state: AuthState; mode: WlMode }) {
  const modeLabel = mode === "oem" ? "含后台换肤" : "仅客户可见侧";
  if (state === "editable") {
    return (
      <div className="flex items-start gap-2 rounded-lg bg-sky-500/10 border border-sky-500/30 p-3 text-sm">
        <Paintbrush className="h-4 w-4 shrink-0 text-sky-500 mt-0.5" />
        <div>
          <p className="font-medium text-sky-600 dark:text-sky-400">完善品牌资料后即可生效</p>
          <p className="text-xs text-muted-foreground mt-0.5">填写公司名称、上传 Logo 并同意使用条款，保存后客户页面立即使用你的品牌。</p>
        </div>
      </div>
    );
  }
  if (state === "active") {
    return (
      <div className="flex items-start gap-2 rounded-lg bg-emerald-500/10 border border-emerald-500/30 p-3 text-sm">
        <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500 mt-0.5" />
        <div>
          <p className="font-medium text-emerald-600 dark:text-emerald-400">白标已生效 · {modeLabel}</p>
          <p className="text-xs text-muted-foreground mt-0.5">客户在报价单/报告/门户/充值页看到的是你的品牌。修改资料后即时生效。</p>
        </div>
      </div>
    );
  }
  if (state === "locked") {
    return (
      <div className="flex items-start gap-2 rounded-lg bg-amber-500/10 border border-amber-500/30 p-3 text-sm">
        <Clock className="h-4 w-4 shrink-0 text-amber-500 mt-0.5" />
        <div>
          <p className="font-medium text-amber-600 dark:text-amber-400">品牌资料待完善 · {modeLabel}</p>
          <p className="text-xs text-muted-foreground mt-0.5">请补全公司名称和 Logo，保存后客户侧立即生效。</p>
        </div>
      </div>
    );
  }
  if (state === "suspended") {
    return (
      <div className="flex items-start gap-2 rounded-lg bg-destructive/10 border border-destructive/30 p-3 text-sm">
        <Lock className="h-4 w-4 shrink-0 text-destructive mt-0.5" />
        <div>
          <p className="font-medium text-destructive">白标已暂停</p>
          <p className="text-xs text-muted-foreground mt-0.5">客户侧已回退平台品牌,资料暂不可编辑。请联系平台恢复。</p>
        </div>
      </div>
    );
  }
  return null;
}

// ========== 白标范围表(WJ-28)· 开通后哪些页面会变成你的品牌 ==========

function WhitelabelScopeCard({ mode, state }: { mode: WlMode; state: AuthState }) {
  const isOem = mode === "oem";
  const isActive = state === "active";
  // WJ-28 修(Codex P1):是否真换品牌 = 仅 active 绿勾;未开通/待激活/暂停 显未生效状态,不误导代理
  const clientNote =
    state === "active" ? "换成你的品牌"
    : state === "suspended" ? "已暂停 · 当前回平台"
    : state === "editable" ? "补全并保存后生效"
    : state === "locked" ? "补资料后生效"
    : "开通后会换";  // unauthorized / none
  const backstageNote =
    !isOem ? "仅「含后台」档才换"
    : isActive ? "已换成你的品牌"
    : "开通后换(含后台档)";
  const rows: { page: string; note: string; on: boolean; pending?: boolean }[] = [
    { page: "报价单", note: clientNote, on: isActive },
    { page: "诊断报告", note: clientNote, on: isActive },
    { page: "客户门户", note: clientNote, on: isActive },
    { page: "物料", note: clientNote, on: isActive },
    { page: "经营后台", note: backstageNote, on: isActive && isOem },
    { page: "邀请页", note: "暂不变", on: false, pending: true },
  ];
  return (
    <Card className="bg-card border-border rounded-xl">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Palette className="h-4 w-4 text-muted-foreground" />
          <CardTitle className="text-base text-foreground">开通后哪些页面变成你的品牌</CardTitle>
        </div>
        <CardDescription className="text-muted-foreground">
          客户可见侧统一换成你的品牌;经营后台仅「含后台」档才换。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-border">
          {rows.map((r) => (
            <li key={r.page} className="flex items-center justify-between py-2.5 text-sm">
              <span className="flex items-center gap-2 text-foreground">
                {r.pending ? (
                  <Clock className="h-4 w-4 shrink-0 text-muted-foreground" />
                ) : r.on ? (
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
                ) : (
                  <Lock className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                {r.page}
              </span>
              <span
                className={
                  r.on
                    ? "text-foreground/80"
                    : "text-muted-foreground"
                }
              >
                {r.note}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

// ========== 主组件 ==========

function WhitelabelSettingsInner() {
  const [config, setConfig] = useState<WhitelabelConfig>(defaultConfig);
  const logoFileInputRef = useRef<HTMLInputElement | null>(null);
  const [authState, setAuthState] = useState<AuthState>("loading");
  const [mode, setMode] = useState<WlMode>("none");
  const [unauthorizedMsg, setUnauthorizedMsg] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [logoUploading, setLogoUploading] = useState(false);
  const [logoUploadError, setLogoUploadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  // [P1 finding1] 轻量白牌条款:termsAccepted=后端已记录同意 · termsChecked=本次勾选
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [termsChecked, setTermsChecked] = useState(false);
  // [板块 C · Owner 2026-07-22 D1] 品牌版本号 + 最近审计时间（只读展示）
  const [managedByOwner, setManagedByOwner] = useState(false);   // [白标继承] 员工席位 = 只读态
  const [brandVersion, setBrandVersion] = useState<number | null>(null);
  const [lastAuditAt, setLastAuditAt] = useState<string | null>(null);

  useEffect(() => {
    const fetchSettings = async () => {
      setAuthState("loading");
      setError(null);
      try {
        const res = await authFetch("/api/referral/whitelabel");
        if (!res.ok) {
          // P1:基础白标已对所有 operator 放开;非 200 多为网络/服务异常 → 加载失败重试态
          setUnauthorizedMsg("白标设置加载失败,请刷新页面重试");
          setAuthState("unauthorized");
          return;
        }
        const json = await res.json();
        const data = json?.data ?? {};
        const status: string = data.configuration_status || "draft";
        setTermsAccepted(!!data.brand_terms_accepted_at);  // [P1 finding1] 后端已记录《对外品牌使用条款》同意
        setMode(data.backoffice_branding_active === true ? "oem" : "none");
        setBrandVersion(typeof data.brand_version === "number" ? data.brand_version : null);  // [板块 C · D1 只读]
        setLastAuditAt(data.last_audit_at || null);  // [板块 C · D1 只读]
        // [白标继承] 对外品牌由团队长统一设置,员工席位读到的是团队长的品牌且不可改。
        setManagedByOwner(data.managed_by_owner === true);
        setConfig({
          company_name: data.company_name || "",
          logo_url: data.logo_url || data.company_logo_url || "",
          slogan: data.slogan || "",
          brand_color: data.brand_color || "",
          contact_name: data.contact_name || "",
          contact_phone: data.contact_phone || "",
          contact_wechat: data.contact_wechat || "",
          contact_email: data.contact_email || "",
          product_name: data.product_name || "",
          favicon_url: data.favicon_url || "",
        });
        if (status === "suspended") setAuthState("suspended");
        else if (status === "approved") setAuthState("active");
        else setAuthState("editable");
      } catch {
        setUnauthorizedMsg("加载白标设置失败,请稍后重试");
        setAuthState("unauthorized");
      }
    };
    fetchSettings();
  }, []);

  const isOem = mode === "oem";
  const readOnly = authState === "suspended";
  const canEdit = authState === "active" || authState === "locked" || authState === "editable";

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      // [板块 C · Owner 2026-07-22 D1] 保存前前端预校验（人话错误 · 后端仍强制复核+HEAD 探测）
      const preErrors = [
        validateBrandTextClient(config.company_name, "公司名称", 100),
        validateBrandImageUrlClient(config.logo_url),
        validateBrandTextClient(config.slogan, "品牌标语", 200),
        validateBrandTextClient(config.contact_name, "联系人", 50),
        validateBrandTextClient(config.contact_phone, "联系电话", 50),
        validateBrandTextClient(config.contact_wechat, "微信号", 100),
        isOem ? validateBrandTextClient(config.product_name, "产品名", 100) : null,
        isOem ? validateBrandImageUrlClient(config.favicon_url) : null,
      ].filter((msg): msg is string => !!msg);
      if (preErrors.length > 0) {
        throw new Error(preErrors[0]);
      }
      // Customer-facing display fields are self-serve; OEM authorization remains admin-only.
      const payload: Record<string, unknown> = {
        company_name: config.company_name,
        logo_url: config.logo_url,
        slogan: config.slogan,
        brand_color: config.brand_color,
        contact_name: config.contact_name,
        contact_phone: config.contact_phone,
        contact_wechat: config.contact_wechat,
        contact_email: config.contact_email,
      };
      if (isOem) {
        payload.product_name = config.product_name;
        payload.favicon_url = config.favicon_url;
      }
      // [P1 finding1] 首次保存带《对外品牌使用条款》同意(已同意则不重复带)
      if (!termsAccepted) payload.accept_brand_terms = true;
      const res = await authFetch("/api/referral/whitelabel", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const saved = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = saved;
        throw new Error((err as Record<string, string>).detail || "保存失败");
      }
      const savedData = saved?.data ?? {};
      if (savedData.configuration_status === "suspended") setAuthState("suspended");
      else if (savedData.configuration_status === "approved") setAuthState("active");
      else setAuthState("editable");
      setMode(savedData.backoffice_branding_active === true ? "oem" : "none");
      setSuccess(true);
      if (!termsAccepted) setTermsAccepted(true);  // [P1 finding1] 首次保存成功 → 标记已同意条款
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存失败,请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  const updateField = (field: keyof WhitelabelConfig, value: string) => {
    setConfig((prev) => ({ ...prev, [field]: value }));
  };

  const handleLogoUpload = async (file: File) => {
    setLogoUploadError(null);
    setSuccess(false);
    const allowedTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
    if (!allowedTypes.has(file.type)) {
      setLogoUploadError("仅支持 PNG、JPG、WebP 格式 Logo");
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      setLogoUploadError("Logo 图片不能超过 2MB");
      return;
    }

    setLogoUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await authFetch("/api/referral/whitelabel/logo", {
        method: "POST",
        body: formData,
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok || !json?.success) {
        throw new Error(json?.detail || json?.message || "Logo 上传失败");
      }
      const logoUrl = json?.data?.logo_url || json?.logo_url || json?.url;
      if (!logoUrl) throw new Error("Logo 上传成功,但未返回图片地址");
      updateField("logo_url", logoUrl);
    } catch (err: unknown) {
      setLogoUploadError(err instanceof Error ? err.message : "Logo 上传失败,请稍后重试");
    } finally {
      setLogoUploading(false);
    }
  };

  // ---- loading ----
  if (authState === "loading") {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // ---- 加载失败兜底(P1:基础白标已放开,正常不进此态;仅网络/服务异常时显示重试)----
  if (authState === "unauthorized") {
    return (
      <div className="p-4 sm:p-6 max-w-4xl mx-auto">
        <div>
          <h1 className="text-2xl font-bold text-foreground">对外品牌(白标)</h1>
          <p className="text-sm text-muted-foreground mt-1">让客户看到的是你的品牌,而不是平台品牌</p>
        </div>
        <div className="flex flex-col items-center justify-center py-16 text-center mt-6">
          <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
            <ShieldAlert className="h-8 w-8 text-muted-foreground" />
          </div>
          <h2 className="text-xl font-semibold text-foreground mb-2">设置加载失败</h2>
          <p className="text-sm text-foreground/80 max-w-md">{unauthorizedMsg || "请刷新页面重试"}</p>
          <Button onClick={() => window.location.reload()} variant="outline" className="mt-6 rounded-xl">
            刷新重试
          </Button>
        </div>
      </div>
    );
  }

  // ---- 已授权(active/locked/suspended)----
  return (
    <div className="p-4 sm:p-6 max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-foreground">对外品牌(白标)</h1>
        <p className="text-sm text-muted-foreground mt-1">
          配置品牌信息,客户看到的报价单 / 报告 / 门户 / 充值页将使用你的品牌
        </p>
        {/* [V3.6 价格边界说明 · 老板约束5] 白标只管品牌外观 · 价格在别处 */}
        <p className="text-xs text-muted-foreground/70 mt-1">
          提示:本页只配置品牌外观,不含价格。报价系数在「报价中心」· 给客户的算力售价在「客户售价」。
        </p>
        {/* [板块 C · Owner 2026-07-22 D1] 品牌版本 + 最近审计时间（只读 · 每次变更版本+1并落审计） */}
        {(brandVersion !== null || lastAuditAt) && (
          <p className="text-xs text-muted-foreground/70 mt-1">
            品牌版本 v{brandVersion ?? 1}
            {lastAuditAt ? ` · 最近变更审计 ${new Date(lastAuditAt).toLocaleString("zh-CN")}` : ""}
            （只读 · 每次保存自动记录）
          </p>
        )}
      </div>

      <StateBanner state={authState} mode={mode} />

      {/* [WJ-28 2026-05-31] 白标范围表 · 当前档位下哪些页面会变成你的品牌 */}
      <WhitelabelScopeCard mode={mode} state={authState} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 表单 */}
        <Card className="bg-card border-border rounded-xl">
          <CardHeader>
            <CardTitle className="text-foreground flex items-center gap-2">
              品牌信息
              <Badge variant="outline" className="text-xs font-normal">
                {isOem ? "含后台换肤" : "仅客户可见侧"}
              </Badge>
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              {isOem
                ? "客户可见侧 + 你自己的后台都会用这些品牌"
                : "这些信息会显示在客户看到的页面上"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="company_name" className="text-foreground">
                <Building2 className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                公司名称 <span className="text-destructive">*</span>
              </Label>
              <Input
                id="company_name"
                placeholder="您的公司名称"
                value={config.company_name}
                disabled={readOnly}
                onChange={(e) => updateField("company_name", e.target.value)}
                className="bg-secondary border-border"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="logo_url" className="text-foreground">
                <ImageIcon className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                Logo
              </Label>
              <div className="flex flex-col sm:flex-row gap-2">
                <Input
                  id="logo_url"
                  placeholder="可粘贴图片地址,也可直接上传"
                  value={config.logo_url}
                  disabled={readOnly}
                  onChange={(e) => updateField("logo_url", e.target.value)}
                  className="bg-secondary border-border flex-1"
                />
                <input
                  ref={logoFileInputRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  disabled={readOnly || logoUploading}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) void handleLogoUpload(file);
                    e.currentTarget.value = "";
                  }}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={readOnly || logoUploading}
                  onClick={() => logoFileInputRef.current?.click()}
                  className="rounded-lg shrink-0"
                >
                  {logoUploading ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <UploadCloud className="mr-2 h-4 w-4" />
                  )}
                  上传 Logo
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                支持 PNG/JPG/WebP,2MB 内。粘贴外链仅支持 https:// 图片地址（保存时后端会探测可达性）。上传后会自动填入地址,点击「保存设置」后生效。
              </p>
              {logoUploadError && (
                <p className="text-xs text-destructive">{logoUploadError}</p>
              )}
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="brand_color" className="text-foreground">
                  <Palette className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                  品牌主色
                </Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="brand_color"
                    type="color"
                    value={config.brand_color || "#6CBE1E"}
                    disabled={readOnly}
                    onChange={(e) => updateField("brand_color", e.target.value)}
                    className="bg-secondary border-border h-10 w-14 p-1"
                  />
                  <Input
                    placeholder="#6CBE1E"
                    value={config.brand_color}
                    disabled={readOnly}
                    onChange={(e) => updateField("brand_color", e.target.value)}
                    className="bg-secondary border-border flex-1"
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="contact_name" className="text-foreground">
                  <Building2 className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                  联系人
                </Label>
                <Input
                  id="contact_name"
                  placeholder="联系人姓名"
                  value={config.contact_name}
                  disabled={readOnly}
                  onChange={(e) => updateField("contact_name", e.target.value)}
                  className="bg-secondary border-border"
                />
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="contact_phone" className="text-foreground">
                  <Phone className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                  联系电话
                </Label>
                <Input
                  id="contact_phone"
                  placeholder="联系电话"
                  value={config.contact_phone}
                  disabled={readOnly}
                  onChange={(e) => updateField("contact_phone", e.target.value)}
                  className="bg-secondary border-border"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="contact_wechat" className="text-foreground">
                  <MessageCircle className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                  微信号
                </Label>
                <Input
                  id="contact_wechat"
                  placeholder="微信号"
                  value={config.contact_wechat}
                  disabled={readOnly}
                  onChange={(e) => updateField("contact_wechat", e.target.value)}
                  className="bg-secondary border-border"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="contact_email" className="text-foreground">
                <Mail className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                联系邮箱
              </Label>
              <Input
                id="contact_email"
                placeholder="contact@example.com"
                value={config.contact_email}
                disabled={readOnly}
                onChange={(e) => updateField("contact_email", e.target.value)}
                className="bg-secondary border-border"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="slogan" className="text-foreground">
                <Type className="inline h-4 w-4 mr-1.5 text-muted-foreground" />
                品牌标语
              </Label>
              <Textarea
                id="slogan"
                placeholder="一句话描述您的服务"
                value={config.slogan}
                disabled={readOnly}
                onChange={(e) => updateField("slogan", e.target.value)}
                rows={2}
                className="bg-secondary border-border resize-none"
              />
            </div>

            {/* OEM 专属字段 */}
            {isOem && (
              <div className="rounded-lg border border-border bg-secondary/40 p-4 space-y-4">
                <div className="flex items-center gap-2">
                  <Monitor className="h-4 w-4 text-muted-foreground" />
                  <p className="text-sm font-medium text-foreground">产品名与图标</p>
                  <Badge variant="outline" className="text-xs font-normal">含后台档专属</Badge>
                </div>
                <div className="space-y-2">
                  {/* [WO_WHITELABEL_COPY_UX 项3] 产品名不只管后台:客户面报告顶部品牌名优先用它(#133 教训) */}
                  <Label htmlFor="product_name" className="text-foreground">产品名</Label>
                  <Input
                    id="product_name"
                    placeholder="如:XX 营销云"
                    value={config.product_name}
                    disabled={readOnly}
                    onChange={(e) => updateField("product_name", e.target.value)}
                    className="bg-secondary border-border"
                  />
                  <p className="text-xs text-muted-foreground">
                    客户打开的诊断报告顶部、浏览器标题会<strong>优先显示产品名</strong>;不填则显示公司名称。也用于你自己后台的标签标题。
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="favicon_url" className="text-foreground">网站图标地址(你后台的浏览器标签小图标)</Label>
                  <Input
                    id="favicon_url"
                    placeholder="https://example.com/favicon.ico"
                    value={config.favicon_url}
                    disabled={readOnly}
                    onChange={(e) => updateField("favicon_url", e.target.value)}
                    className="bg-secondary border-border"
                  />
                </div>
              </div>
            )}

            {error && (
              <div className="flex items-center gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {error}
              </div>
            )}
            {/* [WO_WHITELABEL_COPY_UX 项3] 保存成功提示必须带生效状态:
                存在「已保存但客户还看不到」时(资料不完整)不许只说"已保存" */}
            {success && (
              <div
                className={
                  authState === "active"
                    ? "flex items-center gap-2 rounded-lg bg-emerald-500/10 p-3 text-sm text-emerald-500"
                    : "flex items-start gap-2 rounded-lg bg-amber-500/10 p-3 text-sm text-amber-600 dark:text-amber-400"
                }
              >
                <Save className="h-4 w-4 shrink-0 mt-0.5" />
                {authState === "active"
                  ? "设置已保存,客户侧已生效"
                  : "设置已保存,但品牌尚未生效(需公司名称 + Logo 齐全)。客户目前看到的仍是平台品牌。"}
              </div>
            )}

            {/* [P1 finding1] 轻量《对外品牌使用条款》· 首次开通须勾选(非代理协议·人人可签) */}
            {canEdit && !termsAccepted && (
              <label className="flex items-start gap-2 text-xs text-muted-foreground cursor-pointer rounded-lg border border-border bg-secondary/40 p-3">
                <input
                  type="checkbox"
                  checked={termsChecked}
                  onChange={(e) => setTermsChecked(e.target.checked)}
                  className="mt-0.5 shrink-0"
                />
                <span>
                  我已阅读并同意《对外品牌使用条款》:对外使用此品牌提供服务时,我承诺信息真实合规、不冒用他人商标、对外服务及交付责任由我自行承担;平台仅提供技术工具,不对我的对外经营行为承担责任。
                </span>
              </label>
            )}

            {managedByOwner && (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">
                对外品牌由团队长统一设置,已自动应用到你的账号 —— 你出的报价、报告、海报和
                代发文章都会使用下方品牌。如需调整,请联系团队长在其账号中修改。
              </div>
            )}

            {canEdit && (
              <Button
                onClick={handleSave}
                disabled={managedByOwner || saving || !config.company_name || (!termsAccepted && !termsChecked)}
                className="w-full bg-foreground text-background hover:bg-foreground/90 rounded-lg"
              >
                {saving ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Save className="mr-2 h-4 w-4" />
                )}
                保存设置
              </Button>
            )}
            {readOnly && (
              <p className="text-xs text-center text-muted-foreground">白标暂停期间资料不可编辑</p>
            )}
          </CardContent>
        </Card>

        {/* 预览 */}
        <CustomerPreview config={config} mode={mode} state={authState} />
      </div>
    </div>
  );
}

// ========== 导出(带等级门控:仅代理) ==========

export function WhitelabelSettings() {
  // 客户可见品牌自助生效；OEM 后台换肤仍由管理员治理。
  return <WhitelabelSettingsInner />;
}
