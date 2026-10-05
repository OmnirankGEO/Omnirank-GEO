/**
 * 调研数据月份权重 配置抽屉（仅管理员）
 *
 * 设计文档：docs/2026-04-30-调研数据月份权重-设计.md
 *
 * 功能：
 *  - 全局聚合配置（默认衰减系数 + 最小题数门槛）
 *  - 按行业 × 月份覆盖权重
 *  - 改完后端会自动重聚合，调用方应在 onAggregated 里刷新行业列表
 */
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/api";
import {
    Sheet,
    SheetContent,
    SheetHeader,
    SheetTitle,
    SheetDescription,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

interface AggConfig {
    decay_factor: number;
    min_queries_per_month: number;
}

interface MonthWeightRow {
    year_month: string;
    query_count: number;
    default_weight: number;
    override_weight: number | null;
    is_overridden: boolean;
    below_min_threshold: boolean;
}

interface Props {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    industries: string[];
    onAggregated?: () => void;
}

export function WeightConfigDrawer({ open, onOpenChange, industries, onAggregated }: Props) {
    const [decay, setDecay] = useState("");
    const [minQ, setMinQ] = useState("");
    const [savingConfig, setSavingConfig] = useState(false);

    const [selectedIndustry, setSelectedIndustry] = useState<string>(industries[0] || "");
    const [rows, setRows] = useState<MonthWeightRow[]>([]);
    const [loadingRows, setLoadingRows] = useState(false);
    const [draft, setDraft] = useState<Record<string, string>>({}); // ym -> 编辑中的字符串
    const [savingYm, setSavingYm] = useState<string | null>(null);

    // 抽屉打开时，拉一次全局配置
    useEffect(() => {
        if (!open) return;
        authFetch("/api/placement/research/config")
            .then((r) => r.json())
            .then((c: AggConfig) => {
                setDecay(String(c.decay_factor));
                setMinQ(String(c.min_queries_per_month));
            })
            .catch(() => toast.error("加载全局配置失败"));
    }, [open]);

    // 行业切换 / 抽屉重新打开 → 拉行业月份明细
    useEffect(() => {
        if (!open || !selectedIndustry) return;
        refreshRows();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, selectedIndustry]);

    async function refreshRows() {
        setLoadingRows(true);
        try {
            const r = await authFetch(
                `/api/placement/research/weights?industry=${encodeURIComponent(selectedIndustry)}`
            );
            if (!r.ok) throw new Error(await r.text());
            setRows(await r.json());
            setDraft({});
        } catch (e: any) {
            toast.error("加载月份权重失败：" + (e?.message || e));
        } finally {
            setLoadingRows(false);
        }
    }

    async function saveGlobalConfig() {
        setSavingConfig(true);
        try {
            const body: any = {};
            const d = parseFloat(decay);
            const m = parseInt(minQ);
            if (!Number.isFinite(d) || d <= 0 || d > 1) {
                throw new Error("默认衰减系数必须在 (0, 1] 之间");
            }
            if (!Number.isInteger(m) || m < 1) {
                throw new Error("最小题数门槛必须是 ≥ 1 的整数");
            }
            body.decay_factor = d;
            body.min_queries_per_month = m;

            const r = await authFetch("/api/placement/research/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (!r.ok) throw new Error(await r.text());
            toast.success("全局配置已保存，已重新聚合所有行业");
            onAggregated?.();
            await refreshRows();
        } catch (e: any) {
            toast.error("保存失败：" + (e?.message || e));
        } finally {
            setSavingConfig(false);
        }
    }

    async function saveOverride(ym: string) {
        const raw = draft[ym];
        if (raw === undefined) return;
        const w = parseFloat(raw);
        if (!Number.isFinite(w) || w < 0 || w > 10) {
            toast.error("权重必须在 [0, 10] 之间");
            return;
        }
        setSavingYm(ym);
        try {
            const r = await authFetch("/api/placement/research/weights", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    industry: selectedIndustry,
                    year_month: ym,
                    weight: w,
                }),
            });
            if (!r.ok) throw new Error(await r.text());
            toast.success(`${ym} 权重已覆盖为 ${w}`);
            onAggregated?.();
            await refreshRows();
        } catch (e: any) {
            toast.error("保存失败：" + (e?.message || e));
        } finally {
            setSavingYm(null);
        }
    }

    async function resetOverride(ym: string) {
        setSavingYm(ym);
        try {
            const r = await authFetch(
                `/api/placement/research/weights?industry=${encodeURIComponent(
                    selectedIndustry
                )}&year_month=${encodeURIComponent(ym)}`,
                { method: "DELETE" }
            );
            if (!r.ok) throw new Error(await r.text());
            toast.success(`${ym} 已恢复默认权重`);
            onAggregated?.();
            await refreshRows();
        } catch (e: any) {
            toast.error("恢复失败：" + (e?.message || e));
        } finally {
            setSavingYm(null);
        }
    }

    const decayNum = parseFloat(decay);
    const decayPreview = Number.isFinite(decayNum) ? decayNum : 0;

    return (
        <Sheet open={open} onOpenChange={onOpenChange}>
            <SheetContent
                className="overflow-y-auto p-6"
                style={{ width: 'min(1000px, 92vw)', maxWidth: 'min(1000px, 92vw)' }}
            >
                <SheetHeader>
                    <SheetTitle>调研数据权重配置</SheetTitle>
                    <SheetDescription>
                        控制新旧月份调研数据在历史来源曝光聚合中的占比。仅管理员可见。
                    </SheetDescription>
                </SheetHeader>

                {/* 全局配置 */}
                <section className="mt-6 space-y-4">
                    <h3 className="font-medium">全局聚合配置</h3>
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className="text-sm text-muted-foreground">默认衰减系数</label>
                            <Input value={decay} onChange={(e) => setDecay(e.target.value)} />
                            <p className="text-xs text-muted-foreground mt-1">
                                当月 1.0 → 上月 {decayPreview.toFixed(3)} → 上上月 {(decayPreview ** 2).toFixed(3)}
                            </p>
                        </div>
                        <div>
                            <label className="text-sm text-muted-foreground">单月最低有效题数</label>
                            <Input value={minQ} onChange={(e) => setMinQ(e.target.value)} />
                            <p className="text-xs text-muted-foreground mt-1">
                                某月调研题数少于此值（如采集失败、网络异常），该月数据自动剔除，避免样本不足拉偏整体来源曝光
                            </p>
                        </div>
                    </div>
                    <Button onClick={saveGlobalConfig} disabled={savingConfig}>
                        {savingConfig && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                        保存全局配置（重聚合所有行业）
                    </Button>
                </section>

                {/* 行业月份权重 */}
                <section className="mt-8 space-y-4">
                    <div className="flex items-center justify-between">
                        <h3 className="font-medium">行业月份权重</h3>
                        <select
                            className="border rounded px-2 py-1 text-sm"
                            value={selectedIndustry}
                            onChange={(e) => setSelectedIndustry(e.target.value)}
                        >
                            {industries.map((i) => (
                                <option key={i} value={i}>
                                    {i}
                                </option>
                            ))}
                        </select>
                    </div>

                    {loadingRows ? (
                        <div className="py-6 flex justify-center">
                            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                        </div>
                    ) : rows.length === 0 ? (
                        <p className="text-sm text-muted-foreground py-4">该行业暂无调研数据</p>
                    ) : (
                        <table className="w-full text-sm table-fixed">
                            <colgroup>
                                <col className="w-[16%]" />
                                <col className="w-[16%]" />
                                <col className="w-[18%]" />
                                <col className="w-[24%]" />
                                <col className="w-[26%]" />
                            </colgroup>
                            <thead className="text-muted-foreground border-b">
                                <tr>
                                    <th className="text-center py-2 font-normal">月份</th>
                                    <th className="text-center font-normal">题数</th>
                                    <th className="text-center font-normal">默认权重</th>
                                    <th className="text-center font-normal">实际权重</th>
                                    <th className="text-center font-normal">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((row) => {
                                    const editing = draft[row.year_month];
                                    const displayed =
                                        editing ??
                                        (row.is_overridden
                                            ? String(row.override_weight)
                                            : row.default_weight.toFixed(3));
                                    return (
                                        <tr key={row.year_month} className="border-b">
                                            <td className="py-2 text-center">{row.year_month}</td>
                                            <td className="text-center">
                                                <span>{row.query_count}</span>
                                                {row.below_min_threshold && (
                                                    <Badge variant="destructive" className="ml-1">
                                                        不足
                                                    </Badge>
                                                )}
                                            </td>
                                            <td className="text-center text-muted-foreground">
                                                {row.default_weight.toFixed(3)}
                                            </td>
                                            <td className="text-center">
                                                <Input
                                                    className="w-24 inline-block text-center h-8"
                                                    value={displayed}
                                                    onChange={(e) =>
                                                        setDraft({
                                                            ...draft,
                                                            [row.year_month]: e.target.value,
                                                        })
                                                    }
                                                />
                                                {row.is_overridden && (
                                                    <Badge className="ml-1">已覆盖</Badge>
                                                )}
                                            </td>
                                            <td className="text-center space-x-1">
                                                {editing !== undefined && (
                                                    <Button
                                                        size="sm"
                                                        onClick={() => saveOverride(row.year_month)}
                                                        disabled={savingYm === row.year_month}
                                                    >
                                                        保存
                                                    </Button>
                                                )}
                                                {row.is_overridden && (
                                                    <Button
                                                        size="sm"
                                                        variant="outline"
                                                        onClick={() => resetOverride(row.year_month)}
                                                        disabled={savingYm === row.year_month}
                                                    >
                                                        恢复默认
                                                    </Button>
                                                )}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    )}
                </section>
            </SheetContent>
        </Sheet>
    );
}
