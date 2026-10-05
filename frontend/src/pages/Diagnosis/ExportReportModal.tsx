import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Loader2, FileText, FileSpreadsheet } from "lucide-react";
import { authFetch } from "@/lib/api";
import { isInWechatBrowser } from "@/lib/wechatJsapi";
import { safeRandomUUID } from '@/lib/safeRandomUUID';

interface ExportReportModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  diagnosisId: number;
  brandName: string;
}

interface ThemeOption {
  id: string;
  label: string;
  description: string;
  gradient: string;
}

const THEMES: ThemeOption[] = [
  {
    id: "dark_premium",
    label: "深色商务",
    description: "专业沉稳，适合高端客户",
    gradient: "linear-gradient(90deg, #3B82F6, #0891B2, #F59E0B)",
  },
  {
    id: "light_corporate",
    label: "浅色专业",
    description: "清爽简约，适合企业汇报",
    gradient: "linear-gradient(90deg, #1E40AF, #0891B2, #0F766E)",
  },
  {
    id: "warm_trust",
    label: "暖色信赖",
    description: "温暖亲切，适合服务行业",
    gradient: "linear-gradient(90deg, #B45309, #D97706, #059669)",
  },
  {
    id: "bold_gradient",
    label: "活力渐变",
    description: "现代活力，适合创新品牌",
    gradient: "linear-gradient(90deg, #2563EB, #7C3AED, #F97316)",
  },
];

const EXPORT_ERROR_MESSAGES: Record<string, string> = {
  CLIENT_REPORT_NOT_READY: "客户报告尚未就绪，请稍后重试。",
  CLIENT_REPORT_EXPORT_UNSUPPORTED: "PPTX 客户报告暂不支持安全导出，请先使用 PDF。",
};

const exportAttemptStorageKey = (diagnosisId: number) =>
  `omnirank:report-export-attempt:${diagnosisId}`;

// Browser privacy settings may disable sessionStorage. Keep a page-lifetime
// copy so an unknown billing result cannot turn a retry into a second charge.
const inMemoryExportAttemptKeys = new Map<number, string>();

const CLEARABLE_UNCHARGED_EXPORT_CODES = new Set([
  "CLIENT_REPORT_NOT_READY",
  "CLIENT_REPORT_EXPORT_UNSUPPORTED",
  "REPORT_EXPORT_FILE_PREPARATION_FAILED",
  "REPORT_EXPORT_IDEMPOTENCY_KEY_REQUIRED",
]);

function getExportErrorCode(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const body = payload as Record<string, unknown>;
  const detail =
    body.detail && typeof body.detail === "object"
      ? (body.detail as Record<string, unknown>)
      : null;
  return (
    (typeof detail?.code === "string" && detail.code) ||
    (typeof body.code === "string" && body.code) ||
    ""
  );
}

export function shouldClearExportAttemptAfterError(payload: unknown): boolean {
  const code = getExportErrorCode(payload);
  return (
    code === "IDEMPOTENCY_CHARGE_REFUNDED" ||
    CLEARABLE_UNCHARGED_EXPORT_CODES.has(code)
  );
}

function getOrCreateExportAttemptKey(diagnosisId: number): string {
  const storageKey = exportAttemptStorageKey(diagnosisId);
  const prefix = `report-export:${diagnosisId}:`;
  try {
    const existing = sessionStorage.getItem(storageKey);
    if (existing?.startsWith(prefix)) {
      inMemoryExportAttemptKeys.set(diagnosisId, existing);
      return existing;
    }
  } catch {
    // Storage may be blocked; fall through to the page-lifetime copy.
  }
  const inMemory = inMemoryExportAttemptKeys.get(diagnosisId);
  if (inMemory?.startsWith(prefix)) return inMemory;

  const attempt = safeRandomUUID();
  const key = `${prefix}${attempt}`;
  inMemoryExportAttemptKeys.set(diagnosisId, key);
  try {
    sessionStorage.setItem(storageKey, key);
  } catch {
    // Best effort only. The current request still remains idempotent.
  }
  return key;
}

function clearExportAttemptKey(diagnosisId: number): void {
  inMemoryExportAttemptKeys.delete(diagnosisId);
  try {
    sessionStorage.removeItem(exportAttemptStorageKey(diagnosisId));
  } catch {
    // Nothing else is required after a known terminal response.
  }
}

export function getExportErrorMessage(payload: unknown, status: number): string {
  if (!payload || typeof payload !== "object") {
    return `导出失败 (${status})`;
  }
  const body = payload as Record<string, unknown>;
  const detail =
    body.detail && typeof body.detail === "object"
      ? (body.detail as Record<string, unknown>)
      : null;
  const code = getExportErrorCode(payload);
  const message =
    (typeof detail?.message === "string" && detail.message) ||
    (typeof body.message === "string" && body.message) ||
    (typeof body.error === "string" && body.error) ||
    "";

  return EXPORT_ERROR_MESSAGES[code] || message || `导出失败 (${status})`;
}

export function ExportReportModal({
  open,
  onOpenChange,
  diagnosisId,
  brandName,
}: ExportReportModalProps) {
  const [selectedTheme, setSelectedTheme] = useState("dark_premium");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedThemeLabel =
    THEMES.find((t) => t.id === selectedTheme)?.label ?? selectedTheme;

  const handleGenerate = async () => {
    if (isInWechatBrowser()) {
      setError("微信内暂不支持直接下载文件。请点右上角「···」→「在浏览器打开」后再导出报告。");
      return;
    }
    setGenerating(true);
    setError(null);
    const idempotencyKey = getOrCreateExportAttemptKey(diagnosisId);

    try {
      const url = `/api/diagnosis/${diagnosisId}/report-v2.pdf?theme=${selectedTheme}`;

      const response = await authFetch(url, {
        headers: {
          "X-Report-Export-Idempotency-Key": idempotencyKey,
        },
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        // Unknown billing/generation results and refund_pending retain this
        // exact key. Only explicit no-charge or refunded terminal responses
        // may authorize a fresh attempt identity.
        if (shouldClearExportAttemptAfterError(errorData)) {
          clearExportAttemptKey(diagnosisId);
        }
        throw new Error(getExportErrorMessage(errorData, response.status));
      }

      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = `${brandName}_诊断报告_${selectedThemeLabel}.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(blobUrl);
      clearExportAttemptKey(diagnosisId);

      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出报告时发生未知错误");
    } finally {
      setGenerating(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl p-6">
        <DialogHeader>
          <DialogTitle className="text-xl">导出诊断报告</DialogTitle>
          <DialogDescription>选择 PDF 报告风格</DialogDescription>
        </DialogHeader>

        {/* Format Selection */}
        <div className="flex gap-3">
          <button
            type="button"
            aria-pressed="true"
            className="flex cursor-default items-center gap-2 rounded-lg border border-blue-500 bg-blue-50 px-4 py-2.5 text-sm font-medium text-blue-700"
          >
            <FileText className="h-4 w-4" />
            PDF
          </button>

          <button
            type="button"
            disabled
            aria-disabled="true"
            title="PPTX 即将开放"
            className="flex cursor-not-allowed items-center gap-2 rounded-lg border border-border bg-muted px-4 py-2.5 text-sm font-medium text-muted-foreground opacity-70"
          >
            <FileSpreadsheet className="h-4 w-4" />
            PPTX（即将开放）
          </button>
        </div>

        {/* Theme Selection Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {THEMES.map((theme) => (
            <button
              key={theme.id}
              type="button"
              onClick={() => setSelectedTheme(theme.id)}
              className={`flex flex-col items-start rounded-lg border p-4 text-left transition-all cursor-pointer hover:shadow-md ${
                selectedTheme === theme.id
                  ? "ring-2 ring-blue-500 border-blue-500 bg-blue-50/30"
                  : "border-border bg-card hover:border-border"
              }`}
            >
              {/* Gradient Preview Bar */}
              <div
                className="mb-3 h-2 w-full rounded-full"
                style={{ background: theme.gradient }}
              />
              <span className="text-sm font-semibold text-foreground">
                {theme.label}
              </span>
              <span className="mt-0.5 text-xs text-muted-foreground">
                {theme.description}
              </span>
            </button>
          ))}
        </div>

        {/* Error Message */}
        {error && (
          <p className="text-sm text-red-600">{error}</p>
        )}

        {/* Footer */}
        <DialogFooter className="flex sm:justify-between">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={generating}
            className="cursor-pointer"
          >
            取消
          </Button>
          <Button
            onClick={handleGenerate}
            disabled={generating}
            className="bg-blue-600 text-white hover:bg-blue-700 cursor-pointer"
          >
            {generating ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                生成中...
              </>
            ) : (
              "生成并下载"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
