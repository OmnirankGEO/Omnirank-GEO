/**
 * SharePosterDialog - 通用分享海报弹窗
 * 支持 5 种海报类型：register/interview/team/profile/report
 * 左侧海报预览 + 右侧操作按钮
 */
import { useState, useEffect, useRef, useCallback } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Copy,
  Download,
  Link as LinkIcon,
  Loader2,
  CheckCircle2,
  Share2,
  QrCode,
  X,
} from "lucide-react";
import { cn, extractErrorMessage } from "@/lib/utils";
import { copyToClipboard } from "@/lib/copyUtils";
import { authFetch } from "@/lib/api";
import { toast } from "sonner";

// ========== 类型定义 ==========

export type PosterType = "register" | "register_social" | "interview" | "team" | "profile" | "report";

export interface SharePosterDialogProps {
  open: boolean;
  onClose: () => void;
  type: PosterType;
  params?: Record<string, any>;
}

interface PosterData {
  type: PosterType;
  title: string;
  subtitle?: string;
  features?: string[];
  cta?: string;
  qr_hint: string;
  qr_url: string;
  qr_image_base64: string;
  sharer_name?: string;
  sharer_code?: string;
  brand_name?: string;
  member_count?: number;
  team_code?: string;
  // profile 类型
  creator_type?: string;
  mbti?: string;
  radar?: Record<string, number>;
  soul_tags?: string[];
  ip_declaration?: string;
  // report 类型
  score?: number;
  level?: string;
  dimensions?: Record<string, any>;
  findings?: string[];
}

interface PosterResponse {
  success: boolean;
  poster_data: PosterData;
  share_url: string;
  short_code: string;
  whitelabel?: {
    company_name?: string;
    company_logo_url?: string;
    contact_name?: string;
  } | null;
  agent_level?: number;
}

// ========== 海报标题映射 ==========

const POSTER_TITLES: Record<PosterType, string> = {
  register: "推荐注册海报（GEO版）",
  register_social: "推荐注册海报（社媒版）",
  interview: "AI面试邀请",
  team: "团队邀请",
  profile: "懂你度",
  report: "诊断报告分享",
};

// ========== 海报预览组件 ==========

function PosterPreview({
  data,
  whitelabel,
  posterRef,
}: {
  data: PosterData;
  whitelabel?: PosterResponse["whitelabel"];
  posterRef: React.RefObject<HTMLDivElement | null>;
}) {
  const brandName = whitelabel?.company_name || "";

  return (
    <div
      ref={posterRef as React.RefObject<HTMLDivElement>}
      data-poster="true"
      className="relative w-full max-w-[375px] min-h-[460px] rounded-2xl overflow-hidden shrink-0 bg-[#0c0e14]"
    >
      <div className="p-5 sm:p-6 flex flex-col h-full">
        {/* 顶部品牌区 */}
        <div className="text-center mb-5 sm:mb-6">
          <p className="text-xs text-white/40 tracking-widest uppercase mb-1">
            {brandName}
          </p>
          <h2 className="text-lg sm:text-xl font-bold text-white leading-tight px-1 whitespace-pre-line">
            {data.subtitle || data.title}
          </h2>
        </div>

        {/* 核心内容区 — 根据类型渲染 */}
        <div className="flex-1 space-y-4">
          {(data.type === "register" || data.type === "register_social") && <RegisterContent data={data} />}
          {data.type === "interview" && <InterviewContent data={data} />}
          {data.type === "team" && <TeamContent data={data} />}
          {data.type === "profile" && <ProfileContent data={data} />}
          {data.type === "report" && <ReportContent data={data} />}
        </div>

        {/* CTA 行动号召 */}
        {data.cta && (
          <p className="text-center text-sm text-emerald-400 font-medium mt-4 mb-2">
            {data.cta}
          </p>
        )}

        {/* 二维码区 */}
        <div className="flex flex-col items-center mt-4 mb-3">
          <div className="bg-white rounded-xl p-2.5">
            <img
              src={data.qr_image_base64}
              alt="QR Code"
              className="w-28 h-28"
              crossOrigin="anonymous"
            />
          </div>
          <p className="text-xs text-white/50 mt-2">{data.qr_hint}</p>
        </div>

        {/* 底部信息 */}
        <div className="text-center space-y-0.5 pt-2 border-t border-white/5">
          {!whitelabel && data.sharer_name && (
            <p className="text-xs text-white/40">
              {data.type === "report" ? "诊断师" : "推荐人"}：{data.sharer_name}
            </p>
          )}
          {!whitelabel && data.sharer_code && (
            <p className="text-xs text-white/30 font-mono">{data.sharer_code}</p>
          )}
          {data.team_code && (
            <p className="text-xs text-white/30 font-mono">
              团队码：{data.team_code}
            </p>
          )}
          {whitelabel?.contact_name && (
            <p className="text-xs text-white/40">
              联系人：{whitelabel.contact_name}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// ========== 各类型内容组件 ==========

function RegisterContent({ data }: { data: PosterData }) {
  return (
    <div className="space-y-3">
      <p className="text-white/70 text-sm text-center">{data.title}</p>
      <div className="space-y-2 flex flex-col items-center">
        {data.features?.map((f, i) => (
          <div key={i} className="flex items-start gap-2">
            <span className="text-emerald-400 text-xs mt-0.5">&#10003;</span>
            <span className="text-sm text-white/80">{f}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function InterviewContent({ data }: { data: PosterData }) {
  return (
    <div className="space-y-3">
      <p className="text-white/70 text-sm text-center">{data.title}</p>
      {data.brand_name && (
        <p className="text-center text-xs text-white/40">
          品牌：{data.brand_name}
        </p>
      )}
      <p className="text-white/50 text-xs text-center">AI 会根据你的回答：</p>
      <div className="space-y-2 flex flex-col items-center">
        {data.features?.map((f, i) => (
          <div key={i} className="flex items-start gap-2">
            <span className="text-emerald-400 text-xs mt-0.5">&#10003;</span>
            <span className="text-sm text-white/80">{f}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TeamContent({ data }: { data: PosterData }) {
  return (
    <div className="space-y-3">
      <div className="text-center">
        <p className="text-lg font-semibold text-white">{data.title}</p>
        <p className="text-sm text-white/60 mt-1">{data.subtitle}</p>
      </div>
      <div className="flex justify-center gap-6 text-center">
        <div>
          <p className="text-xl font-bold text-white">{data.member_count || 0}</p>
          <p className="text-xs text-white/40">团队成员</p>
        </div>
        {data.brand_name && (
          <div>
            <p className="text-sm font-medium text-white truncate max-w-[120px]">
              {data.brand_name}
            </p>
            <p className="text-xs text-white/40">服务品牌</p>
          </div>
        )}
      </div>
      <p className="text-white/50 text-xs text-center">加入后你可以：</p>
      <div className="space-y-2 flex flex-col items-center">
        {data.features?.map((f, i) => (
          <div key={i} className="flex items-start gap-2">
            <span className="text-emerald-400 text-xs mt-0.5">&#10003;</span>
            <span className="text-sm text-white/80">{f}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProfileContent({ data }: { data: PosterData }) {
  const RADAR_LABELS: Record<string, string> = {
    professional: '专业性', expression: '表达力', storytelling: '故事力',
    empathy: '共情力', business: '商业思维', creativity: '创造力',
  };

  // radar 值可能是 number 或 {score, reason}，统一提取数字
  const radarItems = data.radar
    ? Object.entries(data.radar).slice(0, 6).map(([key, val]) => {
        const score = typeof val === 'object' && val !== null ? ((val as any).score ?? 0) : (Number(val) || 0);
        return [key, score] as [string, number];
      })
    : [];

  // mbti 可能是 string 或 {type, label}
  const mbtiStr = typeof data.mbti === 'object' && data.mbti !== null ? (data.mbti as any).type || '' : (data.mbti || '');

  return (
    <div className="space-y-3">
      <div className="text-center">
        {data.creator_type && (
          <p className="text-sm text-emerald-400">{String(data.creator_type)}</p>
        )}
        {mbtiStr && (
          <p className="text-xs text-white/50 mt-1">MBTI: {mbtiStr}</p>
        )}
      </div>

      {/* 简化雷达图为进度条 */}
      {radarItems.length > 0 && (
        <div className="rounded-xl bg-white/5 p-3 space-y-2">
          {radarItems.map(([key, val]) => (
            <div key={key} className="flex items-center gap-2">
              <span className="text-xs text-white/50 w-14 text-right shrink-0">
                {RADAR_LABELS[key] || key}
              </span>
              <div className="flex-1 h-2 rounded-full bg-white/10 overflow-hidden">
                <div
                  className="h-full rounded-full bg-emerald-400/70"
                  style={{ width: `${Math.min(100, val)}%` }}
                />
              </div>
              <span className="text-xs text-white/60 w-7 shrink-0">
                {val}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* 灵魂标签 */}
      {data.soul_tags && data.soul_tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5 justify-center">
          {data.soul_tags.map((tag, i) => (
            <span
              key={i}
              className="text-xs px-2 py-0.5 rounded-full bg-white/10 text-white/70"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* IP 宣言 */}
      {data.ip_declaration && (
        <p className="text-center text-sm text-white/60 italic px-4">
          &ldquo;{data.ip_declaration}&rdquo;
        </p>
      )}
    </div>
  );
}

function ReportContent({ data }: { data: PosterData }) {
  return (
    <div className="space-y-3">
      <div className="text-center">
        <p className="text-sm text-white/60">{data.brand_name}</p>
      </div>

      {/* 评分 */}
      <div className="flex items-center justify-center gap-4">
        <div className="text-center">
          <p className="text-4xl font-bold text-white">
            {data.score ?? "—"}
          </p>
          <p className="text-xs text-white/40">/100</p>
        </div>
        {data.level && (
          <div className="px-3 py-1 rounded-lg bg-emerald-400/10 text-emerald-400 text-sm font-medium">
            {data.level}
          </div>
        )}
      </div>

      {/* 关键发现 */}
      {data.findings && data.findings.length > 0 && (
        <div className="space-y-1.5 px-2">
          <p className="text-xs text-white/50 font-medium">关键发现：</p>
          {data.findings.map((f, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-amber-400 text-xs mt-0.5">&#9888;</span>
              <span className="text-xs text-white/70">{f}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ========== 主组件 ==========

export function SharePosterDialog({
  open,
  onClose,
  type,
  params = {},
}: SharePosterDialogProps) {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<PosterResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [saving, setSaving] = useState(false);
  const posterRef = useRef<HTMLDivElement>(null);

  // 请求海报数据
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    setData(null);

    authFetch("/api/share/poster", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, params }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const text = await res.text();
          let detail = "生成海报失败";
          try {
            const json = JSON.parse(text);
            detail = extractErrorMessage(json, "生成海报失败");
          } catch {}
          setError(detail);
          return;
        }
        const json = await res.json();
        if (json.success) {
          setData(json);
        } else {
          setError(extractErrorMessage(json, "生成海报失败"));
        }
      })
      .catch((err) => {
        console.error("[SharePoster] fetch error:", err);
        setError("网络连接失败，请检查网络后重试");
      })
      .finally(() => setLoading(false));
  }, [open, type, JSON.stringify(params)]);

  // 复制链接
  const handleCopy = useCallback(async () => {
    if (!data?.share_url) return;
    const ok = await copyToClipboard(data.share_url);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      toast.error("复制失败");
    }
  }, [data?.share_url]);

  // 保存图片
  const isMobile = /iPhone|iPad|iPod|Android|HarmonyOS|Huawei/i.test(navigator.userAgent);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const handleSave = useCallback(async () => {
    if (!posterRef.current) return;
    setSaving(true);
    try {
      const html2canvas = (await import("html2canvas")).default;
      const canvas = await html2canvas(posterRef.current, {
        scale: 2,
        backgroundColor: "#0c0e14",
        useCORS: true,
        allowTaint: true,
        logging: false,
        onclone: (clonedDoc: Document) => {
          // html2canvas 对某些 CSS 支持差，在克隆 DOM 里简化样式
          const el = clonedDoc.querySelector('[data-poster]') as HTMLElement;
          if (el) el.style.background = '#0c0e14';
        },
      });
      const dataUrl = canvas.toDataURL("image/png");

      if (isMobile) {
        // 移动端：展示图片让用户长按保存
        setPreviewUrl(dataUrl);
        toast.success("长按下方图片即可保存");
      } else {
        // 桌面端：直接下载
        const link = document.createElement("a");
        link.download = `${POSTER_TITLES[type]}.png`;
        link.href = dataUrl;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        toast.success("海报已保存");
      }
    } catch (err) {
      console.error("保存海报失败:", err);
      toast.error("保存失败，请长按海报图片手动保存");
    }
    setSaving(false);
  }, [type, isMobile]);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-[720px] sm:max-w-[720px] max-sm:max-w-[calc(100vw-2rem)] p-0 gap-0 bg-card border-border overflow-hidden">
        <DialogHeader className="p-4 pb-0">
          <DialogTitle className="text-foreground flex items-center gap-2">
            <Share2 className="h-5 w-5 shrink-0" />
            {POSTER_TITLES[type]}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col sm:flex-row gap-4 p-4">
          {/* 左侧：海报预览 */}
          <div className="flex justify-center overflow-auto max-h-[60vh] sm:max-h-[70vh]">
            {loading ? (
              <div className="w-full max-w-[375px] min-h-[400px] flex items-center justify-center rounded-2xl bg-muted">
                <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
              </div>
            ) : error ? (
              <div className="w-full max-w-[375px] min-h-[300px] flex items-center justify-center rounded-2xl bg-muted">
                <p className="text-sm text-destructive px-4 text-center">{error}</p>
              </div>
            ) : data?.poster_data ? (
              <PosterPreview
                data={data.poster_data}
                whitelabel={data.whitelabel}
                posterRef={posterRef}
              />
            ) : null}
          </div>

          {/* 右侧/下方：操作区 */}
          <div className="flex flex-col gap-3 min-w-[180px] sm:pt-8">
            {/* 复制链接 */}
            <Button
              variant="outline"
              className="justify-start gap-2 rounded-lg"
              onClick={handleCopy}
              disabled={!data?.share_url}
            >
              {copied ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
              {copied ? "已复制" : "复制链接"}
            </Button>

            {/* 保存图片 */}
            <Button
              variant="outline"
              className="justify-start gap-2 rounded-lg"
              onClick={handleSave}
              disabled={!data?.poster_data || saving}
            >
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Download className="h-4 w-4" />
              )}
              保存图片
            </Button>

            {/* 短链展示 */}
            {data?.share_url && (
              <div className="rounded-lg bg-muted p-3 mt-2">
                <p className="text-xs text-muted-foreground mb-1">分享链接</p>
                <p className="text-xs text-foreground font-mono break-all">
                  {data.share_url}
                </p>
              </div>
            )}
          </div>

          {/* 移动端：生成的图片预览（长按保存） */}
          {previewUrl && (
            <div className="px-4 pb-4">
              <p className="text-xs text-muted-foreground text-center mb-2">长按图片保存到相册</p>
              <img
                src={previewUrl}
                alt="海报"
                className="w-full rounded-xl border border-border"
              />
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
