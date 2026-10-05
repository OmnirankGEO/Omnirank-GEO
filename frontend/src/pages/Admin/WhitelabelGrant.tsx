/**
 * AdminWhitelabelGrant — admin 对外品牌授权页 · v3.6
 *
 * 路由: /admin/whitelabel(管理分组 · adminOnly)
 * 对接:
 *   GET  /api/referral/admin/whitelabel?q=   列服务商对外品牌状态(q 空=已有行 · q 非空=搜 users 授权新服务商)
 *   PUT  /api/referral/admin/whitelabel/{id}  写授权位 {whitelabel_mode, whitelabel_status}
 *
 * admin 可改:whitelabel_mode(none/external_only/oem)+ whitelabel_status(locked/active/suspended)。
 * 规则(忠实后端):
 *   - mode=none → 撤销(后端强制 status=locked, unlocked=false)
 *   - mode∈{external_only,oem} & status=active → 后端校验 company_name 非空,缺失 400
 *     → 本页对 has_company_name=false 的行禁用"激活"并提示"待服务商补品牌资料"
 * 服务商危险字段(unlocked / approved_by / approved_at / hide_platform_branding)不在本页 · 后端 hard-403。
 * 价格不在对外品牌(老板约束5)· 本页不涉及任何价格/系数。
 */
import { useState, useEffect, useCallback, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Loader2, Search, RefreshCw, ShieldCheck, AlertTriangle, Palette, Save, Pencil, UploadCloud } from "lucide-react";
import { authFetch } from "@/lib/api";
import { toast } from "sonner";

type WlMode = "none" | "external_only" | "oem";
type WlStatus = "locked" | "active" | "suspended";

interface Row {
  user_id: number;
  username: string | null;
  display_name: string | null;
  whitelabel_mode: WlMode;
  whitelabel_status: WlStatus;
  company_name: string | null;
  has_company_name: boolean;
  approved_at: string | null;
  // [板块 C · Owner 2026-07-22 D1/D2] 后台换肤独立授权位 + 品牌版本号
  backoffice_brand_unlocked?: boolean;
  /** [客户反馈④ 2026-08-09] 该账号被 2026-07-22 白标作用域裁决钉死为「仅客户页面」 */
  backoffice_brand_pinned?: boolean;
  brand_version?: number;
  // [V3.6 2026-05-30] admin 代填弹窗预填用 · 品牌字段
  logo_url?: string | null;
  slogan?: string | null;
  brand_color?: string | null;
  contact_name?: string | null;
  contact_phone?: string | null;
  contact_wechat?: string | null;
  contact_email?: string | null;
  product_name?: string | null;
  favicon_url?: string | null;
}

const MODE_LABEL: Record<WlMode, string> = {
  none: "未授权",
  external_only: "仅客户页面",
  oem: "含后台品牌",
};
const STATUS_LABEL: Record<WlStatus, string> = {
  locked: "待激活",
  active: "已生效",
  suspended: "已暂停",
};

function modeBadgeClass(mode: WlMode): string {
  if (mode === "oem") return "bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30";
  if (mode === "external_only") return "bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30";
  return "bg-muted text-muted-foreground border-border";
}
function statusBadgeClass(status: WlStatus): string {
  if (status === "active") return "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30";
  if (status === "suspended") return "bg-destructive/15 text-destructive border-destructive/30";
  return "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30";
}

function RowEditor({ row, onApplied, flagEnabled }: { row: Row; onApplied: () => void; flagEnabled: boolean | null }) {
  const [mode, setMode] = useState<WlMode>(row.whitelabel_mode);
  const [status, setStatus] = useState<WlStatus>(row.whitelabel_status);
  // [板块 C · Owner 2026-07-22 D1/D2] 后台换肤独立授权（与客户页档位 external_only/oem 解耦）
  const [backoffice, setBackoffice] = useState<boolean>(row.backoffice_brand_unlocked === true);
  // [客户反馈④ 2026-08-09] 裁决钉死 = 这一位不可授予(后端同样会 400,前端只是别让人白点)
  const pinned = row.backoffice_brand_pinned === true;
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  // mounted 守卫：apply 在途时行被卸载（搜索/过滤换页）不回写 state
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // mode=none 时 status 无意义(后端强制 locked)
  const activateBlocked =
    mode !== "none" && status === "active" && !row.has_company_name;
  const dirty =
    mode !== row.whitelabel_mode ||
    status !== row.whitelabel_status ||
    backoffice !== (row.backoffice_brand_unlocked === true);

  const apply = async () => {
    if (activateBlocked) {
      toast.error("该服务方尚未填写公司名称,无法激活(待服务方补品牌资料)");
      return;
    }
    setSaving(true);
    try {
      const res = await authFetch(`/api/referral/admin/whitelabel/${row.user_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          whitelabel_mode: mode,
          whitelabel_status: status,
          backoffice_brand_unlocked: backoffice,
          reason: reason.trim() || undefined,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error((err as Record<string, string>).detail || "保存失败");
      }
      toast.success(`已更新 ${row.display_name || row.username || row.user_id} 的对外品牌授权`);
      if (mountedRef.current) setReason(""); // 保存成功后清空理由输入框
      onApplied();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  const selCls =
    "h-8 rounded-md border border-border bg-secondary px-2 text-xs text-foreground";

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        className={selCls}
        value={mode}
        onChange={(e) => {
          const next = e.target.value as WlMode;
          setMode(next);
          // [客户反馈④(b) 2026-08-09] 选"含后台品牌(oem)"时**联动打开**第三个开关。
          //   2026-07-22 D2 把后台换肤拆成独立授权位之后,oem 不再隐含它:那次迁移
          //   §3 一次性把当时的 oem+active+unlocked 全部映射成已授权,而**之后新授的
          //   oem 不会自动带上** —— 管理员按老习惯只改前两个下拉,第三个就一直是关的。
          //   联动把"记得点"从人身上挪到界面上;想只给客户页不给后台的,联动后手动关掉即可。
          //
          //   🔴 顺带纠正一条到处流传的说法:用户 132(翔玉咨询)那一格**不是**漏点。
          //     审计 id=21 显示 admin 在 2026-07-31 19:57:43 就点过了,是系统按裁决
          //     把它还原的(见下方 pinned 分支与 api/referral_api.py 的 PIN 守卫)。
          if (next === "oem" && !pinned) setBackoffice(true);
          if (next === "none") setBackoffice(false);
        }}
        title="客户触达页面档位（D2：仅映射 customer-facing surfaces）"
      >
        <option value="none">未授权</option>
        <option value="external_only">仅客户页面</option>
        <option value="oem">含后台品牌</option>
      </select>
      <select
        className={selCls}
        value={status}
        disabled={mode === "none"}
        onChange={(e) => setStatus(e.target.value as WlStatus)}
        title="已暂停 = emergency suppression（D3 · 冒用/安全/违法时立即全量压制品牌）"
      >
        <option value="locked">待激活</option>
        <option value="active">已生效</option>
        <option value="suspended">已暂停</option>
      </select>
      {/* [板块 C · D2] 后台换肤授权/撤权（独立位 · 与上方客户页档位分开） */}
      {/* [客户反馈④ 2026-08-09] 被裁决钉死的账号置灰:
          它以前可点、点了库里真写进去、下次部署被系统无声还原,而且写进去的那段时间
          库处于 readiness 合同定义的非法态。现在点不了,并直说原因。 */}
      <select
        className={selCls}
        value={pinned ? "0" : (backoffice ? "1" : "0")}
        disabled={pinned}
        onChange={(e) => setBackoffice(e.target.value === "1")}
        title={pinned
          ? "该服务商按 2026-07-22 白标作用域裁决固定为「仅客户页面」· 后台换肤不可在此授予"
          : "后台换肤独立授权（D2 · 服务商经营后台显示其自有品牌；撤权即回 OmniRank）"}
      >
        <option value="0">后台换肤:未授权</option>
        <option value="1">后台换肤:已授权</option>
      </select>
      <Input
        className="h-8 w-36 bg-secondary border-border text-xs"
        placeholder="操作理由(写审计·可空)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      <Button size="sm" variant={dirty ? "default" : "outline"} disabled={saving || !dirty} onClick={apply}>
        {saving ? <Loader2 className="h-3.5 w-5 animate-spin" /> : "应用"}
      </Button>
      {activateBlocked && (
        <span className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
          <AlertTriangle className="h-3 w-3" /> 需先补公司名
        </span>
      )}
      {/* [客户反馈④(b) 2026-08-09] 联动之外再加一条**看得见的**警示:
          存量行(以及管理员手动关掉联动后)仍可能停在"看着已生效、后台其实没换肤"。 */}
      {pinned && (
        <span
          data-testid="backoffice-pinned-note"
          className="flex items-center gap-1 text-xs text-muted-foreground"
        >
          <AlertTriangle className="h-3 w-3" /> 该服务商按裁决固定为「仅客户页面」· 后台换肤不可授予
        </span>
      )}
      {!pinned && mode === "oem" && status === "active" && !backoffice && (
        <span
          data-testid="backoffice-unlocked-warning"
          className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400"
        >
          <AlertTriangle className="h-3 w-3" /> 后台换肤未授权 · 该服务商后台仍显示 OmniRank
        </span>
      )}
      {/* 总闸没开时,三个开关怎么点都不生效 —— 说清楚是环境变量的事,别让人反复点开关。 */}
      {flagEnabled === false && backoffice && (
        <span
          data-testid="backoffice-flag-off-warning"
          className="flex items-center gap-1 text-xs text-destructive"
        >
          <AlertTriangle className="h-3 w-3" /> 后台换肤总闸未开启 · 本项授权暂不会生效(需运维开启后台换肤总开关)
        </span>
      )}
    </div>
  );
}

// [V3.6 2026-05-30 · 老板拍 admin 代填] admin 代服务商填品牌资料弹窗
// 后端 PUT /admin/whitelabel/{id}/brand 只写品牌不碰授权位 · 弹窗预填 row 现值(WYSIWYG · 无意外清空)
function BrandFillDialog({ row, onClose, onSaved }: { row: Row; onClose: () => void; onSaved: () => void }) {
  const logoInputRef = useRef<HTMLInputElement | null>(null);
  const [form, setForm] = useState({
    company_name: row.company_name || "",
    logo_url: row.logo_url || "",
    slogan: row.slogan || "",
    brand_color: row.brand_color || "",
    contact_name: row.contact_name || "",
    contact_phone: row.contact_phone || "",
    contact_wechat: row.contact_wechat || "",
    contact_email: row.contact_email || "",
    product_name: row.product_name || "",
    favicon_url: row.favicon_url || "",
  });
  const [saving, setSaving] = useState(false);
  const [uploadingLogo, setUploadingLogo] = useState(false);
  const isOem = row.whitelabel_mode === "oem";
  const set = (k: keyof typeof form, v: string) => setForm((p) => ({ ...p, [k]: v }));
  const fld = "bg-secondary border-border";

  const uploadLogo = async (file?: File | null) => {
    if (!file) return;
    setUploadingLogo(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const res = await authFetch(`/api/referral/admin/whitelabel/${row.user_id}/logo`, {
        method: "POST",
        body,
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || "上传失败");
      const logoUrl = json?.data?.logo_url || json?.logo_url;
      if (!logoUrl) throw new Error("上传成功但没有返回图片地址");
      set("logo_url", logoUrl);
      toast.success("Logo 已上传，请保存品牌资料后生效");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "上传失败");
    } finally {
      setUploadingLogo(false);
      if (logoInputRef.current) logoInputRef.current.value = "";
    }
  };

  const save = async () => {
    if (!form.company_name.trim()) {
      toast.error("公司名称必填");
      return;
    }
    setSaving(true);
    try {
      const res = await authFetch(`/api/referral/admin/whitelabel/${row.user_id}/brand`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error((err as Record<string, string>).detail || "保存失败");
      }
      toast.success(`已代填 ${row.display_name || row.username || row.user_id} 的对外品牌资料`);
      onSaved();
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>代填对外品牌资料 · {row.display_name || row.username || `用户 ${row.user_id}`}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            管理员可代服务商填写对外展示的品牌资料；这里只写品牌内容，不改授权档位或状态。
          </p>
          <div className="space-y-1.5">
            <Label>公司名称 <span className="text-destructive">*</span></Label>
            <Input className={fld} value={form.company_name} onChange={(e) => set("company_name", e.target.value)} placeholder="服务方公司名称" />
          </div>
          <div className="space-y-1.5">
            <Label>Logo</Label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input className={fld} value={form.logo_url} onChange={(e) => set("logo_url", e.target.value)} placeholder="可粘贴图片地址，也可直接上传" />
              <input
                ref={logoInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={(e) => uploadLogo(e.target.files?.[0])}
              />
              <Button type="button" variant="outline" onClick={() => logoInputRef.current?.click()} disabled={uploadingLogo}>
                {uploadingLogo ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <UploadCloud className="h-4 w-4 mr-1" />}
                上传 Logo
              </Button>
            </div>
            {form.logo_url && (
              <div className="mt-2 flex items-center gap-3 rounded-md border bg-secondary/40 p-2">
                <img src={form.logo_url} alt="Logo 预览" className="h-10 max-w-[160px] object-contain" />
                <span className="text-xs text-muted-foreground">预览；保存品牌资料后生效</span>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>品牌主色</Label>
              <Input className={fld} value={form.brand_color} onChange={(e) => set("brand_color", e.target.value)} placeholder="#6CBE1E" />
            </div>
            <div className="space-y-1.5">
              <Label>联系人</Label>
              <Input className={fld} value={form.contact_name} onChange={(e) => set("contact_name", e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>联系电话</Label>
              <Input className={fld} value={form.contact_phone} onChange={(e) => set("contact_phone", e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>微信号</Label>
              <Input className={fld} value={form.contact_wechat} onChange={(e) => set("contact_wechat", e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>联系邮箱</Label>
            <Input className={fld} value={form.contact_email} onChange={(e) => set("contact_email", e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>品牌标语</Label>
            <Textarea className={`${fld} resize-none`} rows={2} value={form.slogan} onChange={(e) => set("slogan", e.target.value)} />
          </div>
          {isOem && (
            <div className="rounded-lg border border-border bg-secondary/40 p-3 space-y-3">
              <p className="text-xs font-medium text-foreground">后台品牌字段(高级档)</p>
              <div className="space-y-1.5">
                <Label>产品名(浏览器标签)</Label>
                <Input className={fld} value={form.product_name} onChange={(e) => set("product_name", e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label>网站图标地址</Label>
                <Input className={fld} value={form.favicon_url} onChange={(e) => set("favicon_url", e.target.value)} />
              </div>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
          <Button onClick={save} disabled={saving || !form.company_name.trim()}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <><Save className="h-4 w-4 mr-1" />保存品牌</>}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function AdminWhitelabelGrant() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | WlStatus | "none_mode">("all");
  const [brandRow, setBrandRow] = useState<Row | null>(null); // [V3.6] admin 代填品牌弹窗目标行
  // [客户反馈④ 2026-08-09] 后台换肤**总闸**运行时真值(env WHITELABEL_BACKOFFICE_BRAND_ENABLED)。
  //   null = 后端没给(老后端)→ 不做任何断言。
  const [flagEnabled, setFlagEnabled] = useState<boolean | null>(null);

  const load = useCallback(async (query: string) => {
    setLoading(true);
    try {
      const url = query.trim()
        ? `/api/referral/admin/whitelabel?q=${encodeURIComponent(query.trim())}`
        : "/api/referral/admin/whitelabel";
      const res = await authFetch(url);
      const json = await res.json();
      if (!res.ok) throw new Error(json?.detail || "加载失败");
      setRows(json.items || []);
      setFlagEnabled(
        typeof json.backoffice_brand_flag_enabled === "boolean"
          ? json.backoffice_brand_flag_enabled
          : null,
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "加载失败");
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load("");
  }, [load]);

  const filtered = rows.filter((r) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "none_mode") return r.whitelabel_mode === "none";
    return r.whitelabel_mode !== "none" && r.whitelabel_status === statusFilter;
  });

  return (
    <div className="p-4 sm:p-6 max-w-6xl mx-auto space-y-5">
      <div className="flex items-center gap-2">
        <Palette className="h-6 w-6" />
        <div>
          <h1 className="text-2xl font-bold text-foreground">对外品牌授权</h1>
          <p className="text-sm text-muted-foreground">
            管理服务商的对外展示品牌和授权状态。激活前服务商需先填公司名称。
          </p>
        </div>
      </div>

      {/* [客户反馈④ 2026-08-09] 总闸未开的全局横幅。
          没有它,管理员会一直以为是自己开关点错了 —— 而实际上三个开关全点对也不生效。 */}
      {flagEnabled === false && (
        <div
          data-testid="backoffice-flag-off-banner"
          className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            后台换肤总开关当前是关闭的 —— 本页"后台换肤:已授权"对<b>所有</b>服务商暂时都不会生效
            （服务商自己的经营后台仍显示 OmniRank）。客户触达页面的品牌不受影响，照常生效。
            需要运维在服务端开启后台换肤总开关后，本页授权才会真正起作用。
          </span>
        </div>
      )}

      {/* 搜索 + 过滤 */}
      <div className="flex flex-wrap items-center gap-2">
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            load(q);
          }}
        >
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜服务商:用户名 / 名称 / 用户ID(回车)"
              className="pl-8 w-72 bg-secondary border-border"
            />
          </div>
          <Button type="submit" variant="outline" size="sm">搜索</Button>
        </form>
        <Button variant="ghost" size="sm" onClick={() => { setQ(""); load(""); }}>
          <RefreshCw className="h-4 w-4 mr-1" />重置
        </Button>
        <div className="ml-auto flex items-center gap-1">
          {(["all", "active", "locked", "suspended", "none_mode"] as const).map((f) => (
            <Button
              key={f}
              size="sm"
              variant={statusFilter === f ? "default" : "outline"}
              onClick={() => setStatusFilter(f)}
            >
              {f === "all" ? "全部" : f === "none_mode" ? "未授权" : STATUS_LABEL[f as WlStatus]}
            </Button>
          ))}
        </div>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            {q.trim() ? `搜索结果(${filtered.length})` : `已有对外品牌记录的服务商(${filtered.length})`}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : filtered.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">
              {q.trim() ? "没有匹配的服务商 · 换个关键词" : "暂无对外品牌记录 · 用上方搜索找服务商并授权"}
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50 text-left">
                  <th className="p-3">服务方</th>
                  <th className="p-3">当前档位</th>
                  <th className="p-3">状态</th>
                  <th className="p-3">品牌资料</th>
                  <th className="p-3">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={r.user_id} className="border-b align-top">
                    <td className="p-3">
                      <div className="font-medium text-foreground">
                        {r.display_name || r.username || `用户 ${r.user_id}`}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        ID {r.user_id}{r.username ? ` · ${r.username}` : ""}
                      </div>
                    </td>
                    <td className="p-3">
                      <Badge variant="outline" className={modeBadgeClass(r.whitelabel_mode)}>
                        {MODE_LABEL[r.whitelabel_mode]}
                      </Badge>
                      {r.backoffice_brand_unlocked && (
                        <Badge
                          variant="outline"
                          className="ml-1 bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30"
                          title="后台换肤独立授权（D2 · 2026-07-22）"
                        >
                          后台换肤
                        </Badge>
                      )}
                    </td>
                    <td className="p-3">
                      {r.whitelabel_mode === "none" ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : (
                        <Badge variant="outline" className={statusBadgeClass(r.whitelabel_status)}>
                          {STATUS_LABEL[r.whitelabel_status]}
                        </Badge>
                      )}
                    </td>
                    <td className="p-3">
                      <div className="flex flex-col items-start gap-1">
                        {r.has_company_name ? (
                          <span className="text-xs text-emerald-600 dark:text-emerald-400">
                            ✓ {r.company_name}
                          </span>
                        ) : (
                          <span className="text-xs text-muted-foreground">未填公司名</span>
                        )}
                        <Button size="sm" variant="ghost" className="h-6 px-1.5 text-xs" onClick={() => setBrandRow(r)}>
                          <Pencil className="h-3 w-3 mr-1" />{r.has_company_name ? "改品牌" : "代填品牌"}
                        </Button>
                      </div>
                    </td>
                    <td className="p-3">
                      <RowEditor row={r} onApplied={() => load(q)} flagEnabled={flagEnabled} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {brandRow && (
        <BrandFillDialog row={brandRow} onClose={() => setBrandRow(null)} onSaved={() => load(q)} />
      )}
    </div>
  );
}
