import React, { useState, useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Activity, TrendingUp, TrendingDown, FileText, Download, LogOut, ChevronDown, ChevronRight, CheckCircle2, Circle, CalendarClock } from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { useBranding } from '@/hooks/useWhitelabel';
import { BrandLogo } from '@/components/brand/BrandDisplay';
import { mountOpened, trackRenewedInterest } from '@/lib/customerEvents';
import { PrivacyNotice } from '@/components/customer/PrivacyNotice';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { awaitConfirmedSessionToken } from '@/lib/authoritativeSession';
import { OssAttribution } from '@/components/common/OssAttribution';
// [P1-11 fix 2026-05-23 老板授权] 元指令 11:面向非代理 UI 禁止裸露 SOV / AI 出现率百分比
// CTO-15.23 2026-05-25 · 老板订正:话术仅报价页用 · 客户门户(交付)用精确数据
// describeProbability 已从所有 UI 显示移除 · import 留着仅 commit 历史可查(不再调用)
// import { describeProbability } from '@/lib/probability';

const PORTAL_AUTHORITY_LOST_EVENT = 'omnirank-portal-authority-lost';
const portalRequests = new Set<AbortController>();
const INTERNAL_DEMO_ENTRY_PATTERN = /^D[A-Z2-7]{32}$/;

// [板块 C · Owner 2026-07-22 D2] portal_owner_user_id 全局 key 已废除（audit #10 后改用
// portal_quote_id 经后端解析白标）。历史上该 key 以全局名或按 token 变体
// （portal_owner_user_id:{token}）写入过 → 登出/换号/登录成功时统一前缀清扫，杜绝跨账号残留。
export function sweepLegacyPortalOwnerKeys(): void {
    try {
        const doomed: string[] = [];
        for (let i = 0; i < localStorage.length; i += 1) {
            const key = localStorage.key(i);
            if (key && key.startsWith('portal_owner_user_id')) doomed.push(key);
        }
        doomed.forEach((key) => localStorage.removeItem(key));
    } catch { /* localStorage 不可用时静默 */ }
}

function clearPortalSession(): void {
    localStorage.removeItem('portal_token');
    localStorage.removeItem('portal_quote_id');
    localStorage.removeItem('portal_brand_id');
    localStorage.removeItem('portal_brand_name');
    localStorage.removeItem('portal_access_mode');
    localStorage.removeItem('portal_demo_transport_entry');
    sweepLegacyPortalOwnerKeys();
}

function failPortalAuthority(): void {
    clearPortalSession();
    for (const controller of portalRequests) controller.abort();
    portalRequests.clear();
    window.dispatchEvent(new CustomEvent(PORTAL_AUTHORITY_LOST_EVENT));
}

// Portal 专用 fetch。demo 模式只能使用内部 transport；缺失或撤权绝不回退到 live Bearer。
async function portalFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const isDemo = localStorage.getItem('portal_access_mode') === 'demo';
    const demoTransportEntry = localStorage.getItem('portal_demo_transport_entry');
    const storedPortalToken = localStorage.getItem('portal_token');
    if (
        (isDemo && (!demoTransportEntry || !INTERNAL_DEMO_ENTRY_PATTERN.test(demoTransportEntry)))
        || (!isDemo && storedPortalToken !== null && INTERNAL_DEMO_ENTRY_PATTERN.test(storedPortalToken))
    ) {
        failPortalAuthority();
        throw new Error('PORTAL_DEMO_AUTHORITY_LOST');
    }
    const controller = new AbortController();
    const upstreamSignal = init?.signal;
    if (upstreamSignal) {
        if (upstreamSignal.aborted) controller.abort();
        else upstreamSignal.addEventListener('abort', () => controller.abort(), { once: true });
    }
    portalRequests.add(controller);
    try {
        if (isDemo) {
            const rawTarget = typeof input === 'string'
                ? input
                : input instanceof URL ? input.toString() : input.url;
            const targetUrl = new URL(rawTarget, window.location.origin);
            const target = `${targetUrl.pathname}${targetUrl.search}`;
            const response = await fetch(`/api/portal/demo/${encodeURIComponent(demoTransportEntry!)}/transport?target=${encodeURIComponent(target)}`, {
                ...init,
                headers: new Headers(init?.headers),
                signal: controller.signal,
            });
            let authorityLost = response.status === 401 || response.status === 403;
            if (response.status === 404) {
                authorityLost = response.headers.get('X-Error-Code') === 'DEMO_PORTAL_AUTHORITY_LOST';
                if (!authorityLost) {
                    try {
                        const errorPayload = await response.clone().json();
                        authorityLost = errorPayload?.detail?.code === 'DEMO_PORTAL_AUTHORITY_LOST';
                    } catch {
                        // An ordinary resource 404 may be non-JSON; it is not an
                        // authority revocation and remains local to that module.
                    }
                }
            }
            if (authorityLost) {
                failPortalAuthority();
                throw new Error('PORTAL_DEMO_AUTHORITY_LOST');
            }
            return response;
        }
        // [BUG-3 · 事故重灾区] 走 token 链接的客户有 portal_token 不受影响;
        // 走登录态的客户原来在这里抛错 → 整个门户崩 → 客户报"数据全丢失"。改成等待。
        const token = localStorage.getItem('portal_token') || await awaitConfirmedSessionToken();
        const headers = new Headers(init?.headers);
        if (token && !headers.has('Authorization')) {
            headers.set('Authorization', `Bearer ${token}`);
        }
        return await fetch(input, { ...init, headers, signal: controller.signal });
    } finally {
        portalRequests.delete(controller);
    }
}

/** 每个词独立的服务履约进度：显示已达标天数 / 还需达标天数
 *
 * [2026-06-04 纯履约口径 · 老板订正] 客户门户同步履约口径
 *   主显「还需达标 X 天」= service_days − compliant_days(后端 remaining_compliant 字段)
 *   副显「已达标 N/M 天」= 履约进度
 *
 * [CTO-15.23 2026-05-10 老板 A 方案] 加 isStable Badge 区分"刚达标 vs 稳定达标"
 *   isStable=true → "稳定达标" Badge (最近 7 天 ≥ 5 天 is_compliant=TRUE)
 *   isStable=false + isCompliant=true → "刚达标" Badge
 *   isCompliant=false → 不显 Badge
 */
function KeywordCountdown({
    compliantDays, remainingCompliant, serviceDays, isCompliant, isStable,
}: {
    compliantDays: number;
    remainingCompliant: number;
    /** 履约达标天数配额 · [服务期 SSOT 2026-08-06] 调用方不许再 `|| 365` 兜底 */
    serviceDays: number | null;
    isCompliant: boolean;
    isStable?: boolean;
}) {
    if (remainingCompliant <= 0) {
        return <span className="text-xs text-red-500 font-medium">已完成</span>;
    }

    // 2026-05-15 老板报"列内挤" · KeywordCountdown 拆 2 行 · Badge 挪到第 2 行跟"已达标 X/Y 天"并排
    return (
        <div className="text-xs font-mono tabular-nums leading-tight text-left">
            <div className="flex items-center gap-1 whitespace-nowrap">
                {isCompliant ? (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse shrink-0" title="计时中" />
                ) : (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-gray-400 shrink-0" title="已暂停" />
                )}
                <span className="font-semibold">还需达标 {remainingCompliant}天</span>
            </div>
            <div className="flex items-center gap-1.5 mt-1 whitespace-nowrap">
                {isCompliant && (
                    isStable ? (
                        <span
                            className="px-1 py-0 rounded bg-emerald-100 text-emerald-700 text-[9px] font-medium border border-emerald-200 shrink-0"
                            title="最近 7 天 ≥ 5 天达标"
                        >
                            稳定达标
                        </span>
                    ) : (
                        <span
                            className="px-1 py-0 rounded bg-amber-100 text-amber-700 text-[9px] font-medium border border-amber-200 shrink-0"
                            title="今天达标 · 最近 7 天达标天数 < 5 · 还在观察"
                        >
                            刚达标
                        </span>
                    )
                )}
                <span className="text-[10px] text-muted-foreground">
                    {serviceDays ? `已达标 ${compliantDays}/${serviceDays}天` : `已达标 ${compliantDays}天`}
                </span>
            </div>
        </div>
    );
}

interface Citation {
    title: string;
    url: string;
}

interface DetectionDetail {
    platform: string;
    is_detected: boolean;
    mention_type: string;
    tested_at: string;
    citations: Citation[];
    snippet: string;
    pending?: boolean;  // [2026-06-06 P1-1] 该引擎尚无本词监测记录 → 「待监测」灰态占位(不隐藏)
}

interface KeywordStat {
    keyword: string;
    target_brand: string;
    detection_rate: number;
    effective_rate?: number;  // 后端 v2 平滑值 · 跟代理端 KeywordTable 对齐(老板 2026-05-06 要求一致)
    display_rate?: number;  // CTO-15.23 2026-05-11 · 客户门户 + AI 洞察统一展示用值 = effective_rate ?? detection_rate
    is_today_dropped?: boolean;  // [CTO-15.23 2026-05-12] 累计已达标但今日实时跌出目标 · UI 加 ⚠️ 警告
    rate_change: number;
    last_tested: string;
    detection_details: DetectionDetail[];
    lifecycle?: 'pending' | 'deploying' | 'monitoring';
    // 达标倒计时
    target_rate?: number;
    compliant_days?: number;
    remaining_days?: number | null;
    // [2026-06-04 纯履约口径] 还需达标天数 = service_days − compliant_days(后端注入)
    remaining_compliant?: number | null;
    // [Deploy-CTO 2026-05-30] 服务期日历剩余天数(后端 api/monitoring_api.py 注入 · 客户门户走同一端点)
    service_remaining_days_natural?: number | null;
    service_days?: number;
    is_compliant?: boolean;
    is_stable?: boolean;  // CTO-15.23 2026-05-10 · 稳定达标(最近 7 天 ≥ 5 天)
    compliance_progress?: number;
}

interface Publication {
    id: number;
    platform_name: string;
    article_title: string;
    publish_date: string;
    platform_url: string;
}

interface TrendDataPoint {
    period_date: string;
    detection_rate: number;
    tests_count: number;
}

interface Report {
    id: number;
    report_type: string;
    period_start: string;
    period_end: string;
    status?: string;
    summary_data: {
        period_label: string;
        avg_detection_rate: number;
        total_keywords: number;
        total_publications: number;
    };
    created_at: string;
}

// [2026-06-07 批B 门户] 同义覆盖词(顺带覆盖·不单独监测·不承诺达标)· 仅展示「相关搜索参考」
interface CoveredPortalKeyword {
    keyword: string;
    final_price?: number | null;
    coverage_relation?: string;
    parent_core?: string | null;
    note?: string;
}

interface DashboardData {
    brand_name: string;
    total_keywords: number;
    avg_detection_rate: number;
    total_publications: number;
    keywords: KeywordStat[];
    publications: Publication[];
    trend_data: TrendDataPoint[];
    /** 履约达标天数配额 · 单位不是日历天 · 绝不允许加到日期上算到期 */
    service_days?: number;
    service_start_date?: string;
    /** [服务期 SSOT 2026-08-06] 后端算好的自然日剩余（带符号）= service_end_date − today */
    service_remaining_days_natural_signed?: number | null;
    /** 服务期至 · quotes.service_end_date */
    contract_end_date?: string | null;
    covered_keywords?: CoveredPortalKeyword[];
}

type InsightType = 'positive' | 'warning' | 'tip';
type InsightItem = string | { text: string; type?: InsightType };

// [CTO-15.23 2026-05-05] 删手写 renderMarkdown · 改用 ReactMarkdown + remark-gfm
// 老板反馈"HTML 展开回答里很多 markdown 符号没渲染干净"
// 原手写简易版只支持 ## ### ** * - 1. --- · 缺 # / `code` / > 引用 / [link] / ~~strike~~ / | 表格 / 嵌套列表
// 留 snippetPreview 用于预览(只清符号显纯文本)

/** 截取纯文本摘要 */
function snippetPreview(text: string, max: number = 60): string {
    const plain = text.replace(/[#*\-_>`]/g, '').replace(/\n/g, ' ').trim();
    return plain.length > max ? plain.slice(0, max) + '...' : plain;
}

export default function PortalDashboard() {
    const navigate = useNavigate();
    const [data, setData] = useState<DashboardData | null>(null);
    const [loading, setLoading] = useState(true);
    const [exporting, setExporting] = useState(false);
    const [reports, setReports] = useState<Report[]>([]);
    const [generatingReport, setGeneratingReport] = useState(false);
    const [snapshotMissing, setSnapshotMissing] = useState(false);
    const [dashboardSnapshotMissing, setDashboardSnapshotMissing] = useState(false);
    const [expandedKeywords, setExpandedKeywords] = useState<Set<number>>(new Set());
    const [expandedSnippets, setExpandedSnippets] = useState<Set<string>>(new Set());
    const [selectedReport, setSelectedReport] = useState<any>(null);
    const [insights, setInsights] = useState<InsightItem[]>([]);
    const [loadingInsights, setLoadingInsights] = useState(false);
    const reportRef = useRef<HTMLDivElement>(null);
    const brandName = localStorage.getItem('portal_brand_name') || '客户';
    const isDemoPortal = localStorage.getItem('portal_access_mode') === 'demo';

    useEffect(() => {
        const onAuthorityLost = () => {
            setData(null);
            setReports([]);
            setSelectedReport(null);
            setInsights([]);
            setExpandedKeywords(new Set());
            setExpandedSnippets(new Set());
            setLoadingInsights(false);
            setSnapshotMissing(false);
            setDashboardSnapshotMissing(false);
            navigate('/portal?reason=demo-authority-lost', { replace: true });
        };
        window.addEventListener(PORTAL_AUTHORITY_LOST_EVENT, onAuthorityLost);
        return () => window.removeEventListener(PORTAL_AUTHORITY_LOST_EVENT, onAuthorityLost);
    }, [navigate]);

    // v3.6 白标 · 客户门户 · [audit #10 返修] 只用 quote_id 经后端解析 owner 白标
    //   (不再依赖 portal_owner_user_id —— 后端 verify 已去该字段防 brand→agent 串联枚举)。
    const portalQuoteIdStr = localStorage.getItem('portal_quote_id') || '';
    const { brand } = useBranding({
        quoteId: portalQuoteIdStr || undefined,
        surface: 'customer',
        inlineWhitelabel: isDemoPortal ? null : undefined,
    });

    useEffect(() => {
        const token = localStorage.getItem('portal_token');
        const quoteId = localStorage.getItem('portal_quote_id');
        if (isDemoPortal && !localStorage.getItem('portal_demo_transport_entry')) {
            failPortalAuthority();
            return;
        }
        if (!token || !quoteId) {
            navigate('/portal');
            return;
        }
        fetchDashboard(parseInt(quoteId));
        fetchReports(parseInt(quoteId));
    }, [isDemoPortal, navigate]);

    // 客户行为埋点 (CTO-C 2026-04-26 · feat/m3-customer-signals)
    // mount 即写 portal opened + 启动 dwell 计时器
    useEffect(() => {
        const token = localStorage.getItem('portal_token');
        const quoteIdStr = localStorage.getItem('portal_quote_id');
        const brandIdStr = localStorage.getItem('portal_brand_id');
        if (!token || isDemoPortal) return;
        const cleanup = mountOpened({
            source: 'portal',
            rawToken: token,
            quoteId: quoteIdStr ? parseInt(quoteIdStr, 10) : undefined,
            brandId: brandIdStr ? parseInt(brandIdStr, 10) : undefined,
        });
        return () => cleanup();
    }, [isDemoPortal]);

    // 获取报告列表（优先用 brand_id 查询，与管理端一致）
    const fetchReports = async (_quoteId: number) => {
        try {
            const brandId = localStorage.getItem('portal_brand_id');
            const url = brandId
                ? `/api/reports?brand_id=${brandId}&limit=10`
                : `/api/reports?client_id=${_quoteId}&limit=10`;
            const res = await portalFetch(url);
            const data = await res.json();
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success') {
                // 只显示已发送给客户的报告
                const sentReports = (data.reports || []).filter((r: Report) => r.status === 'sent');
                setReports(sentReports);
            }
        } catch (e) {
            console.error('获取报告列表失败', e);
        }
    };

    // 打开报告详情（列表数据已包含 content 等全部字段，无需额外请求）
    const openReportDetail = (report: Report) => {
        setSelectedReport(report);
    };

    // 生成报告
    const handleGenerateReport = async (reportType: string) => {
        if (isDemoPortal) {
            toast.info('演示案例为只读');
            return;
        }
        const quoteId = localStorage.getItem('portal_quote_id');
        if (!quoteId) return;

        setGeneratingReport(true);
        try {
            const res = await portalFetch(`/api/reports/generate?client_id=${quoteId}&report_type=${reportType}`, {
                method: 'POST'
            });
            const data = await res.json();
            if (data.status === 'success') {
                toast.success(`${reportType === 'weekly' ? '周报' : '月报'}生成成功！`);
                fetchReports(parseInt(quoteId));
            } else {
                toast.error('报告生成失败：' + (data.error || '未知错误'));
            }
        } catch (e) {
            console.error('生成报告失败', e);
        } finally {
            setGeneratingReport(false);
        }
    };

    // 导出数据
    // [#5 2026-06-07] 改客户端本地生成 CSV:门户短 token 无权后端 /api/reports/export(被中间件拦 → 旧版恒"导出失败")
    //   监测词条数据本就只来自当前门户 token 自己的 quote(data.keywords)· 客户端生成天然不可能导出到别人的数据,安全边界自封
    //   "导出 Excel" 旧版本就是返回 CSV 改后缀(无 xlsx 依赖),这里统一产 .csv(Excel 可直接打开),行为不变
    const handleExportData = (_format: string) => {
        if (isDemoPortal) {
            toast.info('演示案例为只读');
            return;
        }
        const rows = data?.keywords || [];
        if (rows.length === 0) {
            toast.error('现在还没有可导出的监测数据');
            return;
        }
        const statusText = (lc?: string) =>
            lc === 'deploying' ? '铺量中' : lc === 'pending' ? '待启动' : '监测中';
        const cell = (v: string | number) => `"${String(v ?? '').replace(/"/g, '""')}"`;
        const lines = [
            ['监测词条', '目标品牌', 'AI 出现率(%)', '状态'].join(','),
            ...rows.map((kw) => {
                const rate = kw.lifecycle === 'monitoring'
                    ? Number(kw.display_rate ?? kw.effective_rate ?? kw.detection_rate ?? 0)
                    : '';
                return [kw.keyword, kw.target_brand || '-', rate, statusText(kw.lifecycle)]
                    .map(cell).join(',');
            }),
        ];
        try {
            const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const today = new Date().toISOString().slice(0, 10);
            a.download = `监测报表_${today}.csv`;
            a.click();
            URL.revokeObjectURL(url);
            toast.success('报表已下载到你的设备');
        } catch (e) {
            console.error('导出失败', e);
            toast.error('报表生成遇到点小问题,稍后再试一次');
        }
    };

    // AI洞察状态
    // [CTO-15.23 2026-05-10 老板报"AI 洞察 vs 词条监测打脸"根治]
    //   后端从此输出 {text, type} dict (type ∈ positive|warning|tip)
    //   兼容渐进部署期 string 形态(prod 旧响应缓存)→ insightToView 同时处理两种
    const insightToView = (item: InsightItem): { text: string; emoji: string } => {
        const EMOJI: Record<InsightType, string> = {
            positive: '✅',
            warning: '⚠️',
            tip: '💡',
        };
        if (typeof item === 'string') {
            return { text: item, emoji: EMOJI.tip };
        }
        // 2026-05-15 React #31 防御:item.text 必须强转 string · 防后端 nested object 漂移
        if (!item || typeof item !== 'object') {
            return { text: String(item ?? ''), emoji: EMOJI.tip };
        }
        const rawText: unknown = (item as { text?: unknown }).text;
        const safeText = typeof rawText === 'string' ? rawText : (rawText == null ? '' : JSON.stringify(rawText));
        const t = ((item as { type?: string }).type || 'tip') as InsightType;
        return { text: safeText, emoji: EMOJI[t] || EMOJI.tip };
    };

    // 2026-05-15 React #31 防御:入口 normalize · 任何 schema 漂移都不让 object 进 state
    const normalizeInsights = (raw: unknown): InsightItem[] => {
        if (!Array.isArray(raw)) return [];
        return raw.map((item): InsightItem => {
            if (typeof item === 'string') return item;
            if (item && typeof item === 'object') {
                const rec = item as Record<string, unknown>;
                const t = rec.text;
                if (typeof t === 'string') {
                    const type = typeof rec.type === 'string' ? (rec.type as InsightType) : undefined;
                    return type ? { text: t, type } : { text: t };
                }
                return { text: JSON.stringify(item) };
            }
            return String(item ?? '');
        }).filter(it => typeof it === 'string' ? it.trim() : it.text.trim());
    };

    // 获取AI洞察
    const fetchInsights = async (quoteId: number) => {
        setLoadingInsights(true);
        try {
            const res = await portalFetch(`/api/insights/${quoteId}?period=weekly`);
            const data = await res.json();
            if (data.snapshot_missing) setSnapshotMissing(true);
            if (data.status === 'success' && data.insights) {
                setInsights(normalizeInsights(data.insights));
            }
        } catch (e) {
            console.error('获取洞察失败', e);
        } finally {
            setLoadingInsights(false);
        }
    };

    // 在useEffect中调用
    useEffect(() => {
        const quoteId = localStorage.getItem('portal_quote_id');
        if (quoteId) {
            fetchInsights(parseInt(quoteId));
        }
    }, []);

    const fetchDashboard = async (quoteId: number) => {
        setLoading(true);
        try {
            // 获取关键词统计
            const kwRes = await portalFetch(`/api/monitoring/clients/${quoteId}/keywords`);
            const kwData = await kwRes.json();

            // 获取媒体投放
            const pubRes = await portalFetch(`/api/publications/${quoteId}`);
            const pubData = await pubRes.json();
            if (kwData.snapshot_missing || pubData.snapshot_missing) {
                setSnapshotMissing(true);
                setDashboardSnapshotMissing(true);
            }

            const keywords = kwData.status === 'success' ? kwData.keywords : [];
            const publications = pubData.status === 'success' ? pubData.publications : [];

            // [Deploy-CTO 2026-05-30 老板拍板] 趋势对齐当前客户:按 client_id(当前 quote)· 不按 brand 聚合
            // 旧 brand_id 会把同品牌其他 campaign(0 词/陈旧 quote)混进来稀释当前客户出现率(罗平 100% 被稀释成 12.5%)
            let trendData: TrendDataPoint[] = [];
            try {
                const trendRes = await portalFetch(`/api/monitoring/trend?client_id=${quoteId}&days=14`);
                const trendJson = await trendRes.json();
                if (trendJson.snapshot_missing) setSnapshotMissing(true);
                if (trendJson.status === 'success' && trendJson.trend) {
                    trendData = trendJson.trend
                        .filter((t: any) => t.rate !== null)
                        .map((t: any) => ({
                            period_date: t.date,
                            detection_rate: t.rate,
                            tests_count: 1,
                        }));
                }
            } catch (e) {
                console.error('获取趋势数据失败', e);
            }

            // 计算平均检测率（只算已生效的词条，排除铺量中和待测试）
            const activeKeywords = keywords.filter((k: KeywordStat) => k.lifecycle === 'monitoring');
            const avgRate = activeKeywords.length > 0
                ? Math.round(activeKeywords.reduce((sum: number, k: KeywordStat) => sum + (k.effective_rate ?? k.detection_rate ?? 0), 0) / activeKeywords.length)
                : 0;

            setData({
                brand_name: brandName,
                total_keywords: keywords.length,
                avg_detection_rate: avgRate,
                total_publications: publications.length,
                keywords,
                publications,
                trend_data: trendData,
                service_days: kwData.service_days,
                service_start_date: kwData.service_start_date,
                // [2026-06-07 批B 门户] 同义覆盖词(后端 covered_keywords)· 单独「相关搜索参考」区展示
                covered_keywords: kwData.status === 'success' ? (kwData.covered_keywords || []) : [],
            });
        } catch (e) {
            console.error('加载仪表盘数据失败', e);
        } finally {
            setLoading(false);
        }
    };

    const handleLogout = () => {
        clearPortalSession();
        navigate('/portal');
    };

    // 导出PDF报告
    const handleExportPDF = async () => {
        if (isDemoPortal) {
            toast.info('演示案例为只读');
            return;
        }
        setExporting(true);
        try {
            // 生成报告内容
            const reportContent = `
# ${brandName} - AI搜索优化监测报告

生成时间: ${new Date().toLocaleString('zh-CN')}

## 📊 数据概览
- 监测词条: ${data?.total_keywords || 0} 个
- AI 出现率: ${Number(data?.avg_detection_rate || 0)}%
- 媒体投放: ${data?.total_publications || 0} 篇

## 📈 词条监测详情
${data?.keywords?.map(kw => {
    if (kw.lifecycle === 'deploying') return `- ${kw.keyword}: 铺量中 (目标: ${kw.target_brand || '-'})`;
    if (kw.lifecycle === 'pending') return `- ${kw.keyword}: 待启动 (目标: ${kw.target_brand || '-'})`;
    // CTO-15.23 2026-05-25 · 老板订正:交付环节用精确数据 · 话术仅报价用
    const rate = Number(kw.effective_rate ?? kw.detection_rate ?? 0);
    return `- ${kw.keyword}: ${rate}% 出现率 (目标: ${kw.target_brand || '-'})`;
}).join('\n') || '暂无数据'}

## 📰 媒体投放记录
${data?.publications?.map(pub => `- [${pub.platform_name}] ${pub.article_title || '未命名'} (${pub.publish_date})`).join('\n') || '暂无数据'}

---
报告由系统自动生成
            `.trim();

            // 创建并下载文件
            const blob = new Blob([reportContent], { type: 'text/markdown;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${brandName}_监测报告_${new Date().toISOString().split('T')[0]}.md`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (e) {
            console.error('导出报告失败', e);
        } finally {
            setExporting(false);
        }
    };

    if (loading) {
        return (
            <div className="min-h-screen bg-muted flex items-center justify-center">
                <div className="text-center">
                    <Activity className="h-12 w-12 animate-spin text-brand mx-auto" />
                    <p className="mt-4 text-muted-foreground">加载中...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-muted">
            {/* 顶部导航 - Premium Glassmorphism */}
            <header className="sticky top-0 z-50 backdrop-blur-xl bg-card/80 border-b border-border">
                <div className="max-w-7xl mx-auto px-3 sm:px-6 py-3 sm:py-4 flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 sm:gap-4 min-w-0 flex-1">
                        {/* Logo区域 · v3.6 白标 · 客户门户显示代理品牌(external_only 不回退平台) */}
                        <div className="shrink-0 sm:-my-5 flex items-center">
                            <BrandLogo brand={brand} size="lg" className="!h-10 sm:!h-[83px] w-auto object-contain" />
                        </div>
                        <div className="h-8 sm:h-10 w-px bg-border/60 shrink-0 hidden sm:block"></div>
                        <div className="min-w-0">
                            <h1 className="text-sm sm:text-xl font-bold bg-linear-to-r from-[#2B4C7E] to-[#1E3A5F] bg-clip-text text-transparent truncate">
                                {brandName}
                            </h1>
                            <p className="text-[10px] sm:text-xs text-muted-foreground truncate">AI搜索可见度监测报告</p>
                        </div>
                    </div>

                    {/* 右侧操作 */}
                    <div className="flex items-center gap-1.5 sm:gap-3 shrink-0">
                        <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-50 border border-emerald-200">
                            <div className="w-2 h-2 bg-[#7AC943] rounded-full animate-pulse"></div>
                            <span className="text-xs text-emerald-700 font-medium">实时监测中</span>
                        </div>
                        {/* [CTO-15.3 2026-04-21] 老板要求:客户门户加主题切换(暗黑/浅色/跟随系统) */}
                        <ThemeToggle />
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={handleLogout}
                            className="text-muted-foreground hover:text-foreground hover:bg-muted px-2 sm:px-3"
                        >
                            <LogOut className="h-4 w-4 sm:mr-2" />
                            <span className="hidden sm:inline">退出</span>
                        </Button>
                    </div>
                </div>
            </header>

            <main className="max-w-7xl mx-auto px-3 py-4 sm:p-6 space-y-4 sm:space-y-6">
                {isDemoPortal && (
                    <div className="border-y border-brand/30 bg-brand/5 px-3 py-2 text-sm text-foreground" role="status">
                        演示案例 · 只读
                    </div>
                )}
                {snapshotMissing && (
                    <div className="border-y border-amber-500/30 bg-amber-500/5 px-3 py-2 text-sm text-amber-700 dark:text-amber-300" role="status">
                        快照未包含部分数据；页面保留正常布局与查看交互。
                    </div>
                )}
                {/* 统计概览 - Glassmorphism Cards */}
                <div className="grid grid-cols-2 md:grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-5">
                    {/* 监测词条 */}
                    <div className="group relative overflow-hidden rounded-xl bg-card border border-border transition-all duration-300 p-3 sm:p-6">
                        <div className="absolute top-0 right-0 w-16 sm:w-24 h-16 sm:h-24 bg-linear-to-br from-[#2B4C7E]/20 to-transparent rounded-bl-full"></div>
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-xs sm:text-sm font-medium text-muted-foreground mb-1">监测词条</p>
                                <p className="text-2xl sm:text-4xl font-bold text-[#2B4C7E]">{dashboardSnapshotMissing ? '--' : (data?.total_keywords ?? 0)}</p>
                            </div>
                            <div className="w-10 h-10 sm:w-14 sm:h-14 rounded-xl sm:rounded-2xl bg-linear-to-br from-[#2B4C7E] to-[#1E3A5F] flex items-center justify-center shadow-lg group-hover:scale-110 transition-transform">
                                <Activity className="h-5 w-5 sm:h-7 sm:w-7 text-white" />
                            </div>
                        </div>
                    </div>

                    {/* 平均出现率 — CTO-15.23 2026-05-25 老板订正:交付环节用精确数据 · 话术仅报价用
                        原 describeProbability 话术 "问 2 次约出现 1 次" 模糊无法验收 · 改 N% 数据驱动 */}
                    <div className="group relative overflow-hidden rounded-xl bg-card border border-border transition-all duration-300 p-3 sm:p-6">
                        <div className="absolute top-0 right-0 w-16 sm:w-24 h-16 sm:h-24 bg-linear-to-br from-[#7AC943]/30 to-transparent rounded-bl-full"></div>
                        <div className="flex items-center justify-between">
                            <div>
                                {/* [Owner 2026-08-02 拍板挂] 泛化文案:AI 平台普遍个性化推荐,
                                    客户自己去 APP 实测很可能与本报告不一致 —— 提前明示,别等客户
                                    随手一测才产生"报告失真"的误解。挂 HelpHint 不占版面。 */}
                                <p className="text-xs sm:text-sm font-medium text-emerald-600 mb-1 inline-flex items-center gap-1">
                                    平均出现率
                                    <HelpHint title="为什么我自己测,结果可能不一样?">
                                        AI 平台普遍存在个性化推荐,<b>不同用户实际看到的结果可能有所差异</b>。
                                        <br />这里的数字是我们在统一口径下批量检测得出的平均值,用来看<b>趋势和相对变化</b>；
                                        你自己单次提问看到的结果受帐号历史、地区、提问措辞等影响,与平均值有出入是正常的。
                                    </HelpHint>
                                </p>
                                <p className="text-2xl sm:text-4xl font-bold text-[#2B4C7E]">
                                    {dashboardSnapshotMissing ? '--' : Number(data?.avg_detection_rate ?? 0)}{!dashboardSnapshotMissing && <span className="text-base sm:text-xl font-normal text-muted-foreground">%</span>}
                                </p>
                            </div>
                            <div className="w-10 h-10 sm:w-14 sm:h-14 rounded-xl sm:rounded-2xl bg-linear-to-br from-[#7AC943] to-emerald-600 flex items-center justify-center shadow-lg group-hover:scale-110 transition-transform">
                                <TrendingUp className="h-5 w-5 sm:h-7 sm:w-7 text-white" />
                            </div>
                        </div>
                    </div>

                    {/* 达标词条 */}
                    <div className="group relative overflow-hidden rounded-xl bg-card border border-border transition-all duration-300 p-3 sm:p-6">
                        <div className="absolute top-0 right-0 w-16 sm:w-24 h-16 sm:h-24 bg-linear-to-br from-amber-500/20 to-transparent rounded-bl-full"></div>
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-xs sm:text-sm font-medium text-amber-600 mb-1">达标词条</p>
                                <p className="text-2xl sm:text-4xl font-bold text-[#2B4C7E]">
                                    {dashboardSnapshotMissing ? '--' : (data?.keywords?.filter(k => k.is_compliant).length ?? 0)}
                                    {!dashboardSnapshotMissing && <span className="text-base sm:text-xl font-normal text-muted-foreground">/{data?.total_keywords ?? 0}</span>}
                                </p>
                            </div>
                            <div className="w-10 h-10 sm:w-14 sm:h-14 rounded-xl sm:rounded-2xl bg-linear-to-br from-amber-500 to-orange-600 flex items-center justify-center shadow-lg group-hover:scale-110 transition-transform">
                                <CheckCircle2 className="h-5 w-5 sm:h-7 sm:w-7 text-white" />
                            </div>
                        </div>
                    </div>

                    {/* 服务开始(CTO-15.23 2026-05-25 老板拍 · 原"服务到期")
                        · 真因:上榜才算完成 · 服务实际到期日不确定 · 显示"到期"误导用户
                        · 改显示:已服务 N 天 + 开始日期 · 续费 CTA 保留(用 service_days 计算) */}
                    <div className="group relative overflow-hidden rounded-xl bg-card border border-border transition-all duration-300 p-3 sm:p-6">
                        <div className="absolute top-0 right-0 w-16 sm:w-24 h-16 sm:h-24 bg-linear-to-br from-sky-500/20 to-transparent rounded-bl-full"></div>
                        <div className="flex items-center justify-between gap-3">
                            <div className="min-w-0 flex-1">
                                <p className="text-xs sm:text-sm font-medium text-sky-600 mb-1">服务开始</p>
                                {data?.service_start_date ? (() => {
                                    const start = new Date(data.service_start_date!);
                                    const today = new Date();
                                    const daysElapsed = Math.max(0, Math.floor((today.getTime() - start.getTime()) / (1000 * 60 * 60 * 24)));
                                    // 续费 CTA 触发 · [服务期 SSOT 2026-08-06] 只读后端算好的自然日剩余
                                    // · 旧版在这里 `start + service_days` 自己算到期日 —— 那是【履约达标天数配额】,
                                    //   不是日历天,库默认 365 且无人写入 → 客户门户的续费提醒按"一年后到期"排,
                                    //   而自动监测早在 service_end_date(通常 start + 1 个月)那天就停了。
                                    // · 剩余 ≤ 30 天显示申请续费 · 元指令 16(客户不直接下单)
                                    // · 后端没给剩余天数(没设服务期)→ 不显示 CTA(不猜)
                                    const calendarRemaining = data?.service_remaining_days_natural_signed;
                                    const showRenewCTA =
                                        calendarRemaining != null && calendarRemaining > 0 && calendarRemaining <= 30;
                                    return (
                                        <>
                                            <p className="text-2xl sm:text-4xl font-bold text-[#2B4C7E]">
                                                已 {daysElapsed}<span className="text-base sm:text-xl font-normal text-muted-foreground">天</span>
                                            </p>
                                            <p className="text-xs text-muted-foreground mt-1">
                                                {/* CTO-15.23 2026-05-25 · 原"YYYY-MM-DD 到期"改为"YYYY-MM-DD 开始"
                                                    · 上榜才算完成 · 不知何时结束 · 显示开始日期更准确 */}
                                                {start.toISOString().slice(0, 10)} 开始
                                            </p>
                                            {showRenewCTA && (
                                                <Button
                                                    size="sm"
                                                    className="mt-2 bg-amber-500 hover:bg-amber-600 text-white text-xs h-7 px-3"
                                                    disabled={isDemoPortal}
                                                    title={isDemoPortal ? '演示案例为只读' : undefined}
                                                    onClick={async () => {
                                                        if (isDemoPortal) return;
                                                        const quoteId = localStorage.getItem('portal_quote_id');
                                                        if (!quoteId) return;
                                                        // CTO-C 客户行为埋点 · 续费意向
                                                        const portalToken = localStorage.getItem('portal_token');
                                                        const brandIdStr = localStorage.getItem('portal_brand_id');
                                                        trackRenewedInterest({
                                                            source: 'portal',
                                                            rawToken: portalToken || undefined,
                                                            quoteId: parseInt(quoteId, 10),
                                                            brandId: brandIdStr ? parseInt(brandIdStr, 10) : undefined,
                                                        });
                                                        try {
                                                            const res = await portalFetch('/api/portal/renew-request', {
                                                                method: 'POST',
                                                                headers: { 'Content-Type': 'application/json' },
                                                                body: JSON.stringify({ quote_id: parseInt(quoteId) }),
                                                            });
                                                            const r = await res.json();
                                                            if (r?.success) {
                                                                toast.success('已通知您的服务方 · 我们将尽快与您联系并准备续费方案');
                                                            } else {
                                                                toast.error(r?.detail || '申请失败 · 请稍后重试');
                                                            }
                                                        } catch {
                                                            toast.error('申请失败 · 请稍后重试');
                                                        }
                                                    }}
                                                >
                                                    申请续费
                                                </Button>
                                            )}
                                        </>
                                    );
                                })() : (
                                    <p className="text-2xl font-bold text-muted-foreground">--</p>
                                )}
                            </div>
                            <div className="w-10 h-10 sm:w-14 sm:h-14 rounded-xl sm:rounded-2xl bg-linear-to-br from-sky-500 to-blue-600 flex items-center justify-center shadow-lg group-hover:scale-110 transition-transform">
                                <CalendarClock className="h-5 w-5 sm:h-7 sm:w-7 text-white" />
                            </div>
                        </div>
                    </div>
                </div>

                {/* 趋势图 */}
                <Card className="bg-card border border-border rounded-xl">
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <TrendingUp className="h-5 w-5 text-green-500" />
                            AI出现率趋势（近14天）
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        {data?.trend_data && data.trend_data.length > 0 ? (
                            // CTO-15.23 2026-05-25 · 老板报 chart 超出容器(移动端 14 bar × 等分挤压标签错位)
                            // · 修法:overflow-x-auto 横向滚动 + min-w-max 让内容按需展开 + 每 bar 固定 w-10(40px)
                            // · touch-pan-x 让移动端单指横向滑动不触发垂直滚动
                            <div className="overflow-x-auto -mx-2 px-2 touch-pan-x">
                                <div className="h-48 flex items-end gap-1.5 min-w-max">
                                    {data.trend_data.slice(-14).map((point, i) => (
                                        <div key={i} className="w-10 shrink-0 flex flex-col items-center gap-1">
                                            <div
                                                className="w-full bg-blue-500 rounded-t transition-all hover:bg-blue-600"
                                                style={{ height: `${Math.max(point.detection_rate * 1.5, 10)}px` }}
                                                title={`${point.period_date}: ${point.detection_rate}%`}
                                            />
                                            <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                                                {point.period_date.slice(-5)}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <div className="h-48 flex items-center justify-center text-muted-foreground">
                                暂无趋势数据，请先执行监测任务
                            </div>
                        )}
                    </CardContent>
                </Card>

                {/* AI洞察 */}
                <Card className="bg-card border border-border rounded-xl">
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <div className="w-8 h-8 rounded-lg bg-linear-to-br from-purple-500 to-indigo-600 flex items-center justify-center">
                                <Activity className="h-4 w-4 text-white" />
                            </div>
                            <span className="text-[#2B4C7E]">AI智能洞察</span>
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        {loadingInsights ? (
                            <div className="text-center py-4 text-muted-foreground">
                                <Activity className="h-5 w-5 animate-spin inline mr-2" />
                                正在分析数据...
                            </div>
                        ) : insights.length > 0 ? (
                            <div className="space-y-3">
                                {insights.map((insight, i) => {
                                    const { text, emoji } = insightToView(insight);
                                    return (
                                        <div key={i} className="flex items-start gap-3 p-3 bg-linear-to-r from-purple-50 to-blue-50 rounded-lg">
                                            <span className="text-lg">{emoji}</span>
                                            <span className="text-sm">{text}</span>
                                        </div>
                                    );
                                })}
                            </div>
                        ) : (
                            <div className="text-center py-4 text-muted-foreground">
                                暂无洞察数据
                            </div>
                        )}
                    </CardContent>
                </Card>

                {/* 词条监测表格 */}
                <Card ref={reportRef} className="bg-card border border-border rounded-xl">
                    <CardHeader className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2">
                        <CardTitle className="flex items-center gap-2">
                            <div className="w-8 h-8 rounded-lg bg-linear-to-br from-[#2B4C7E] to-[#1E3A5F] flex items-center justify-center shrink-0">
                                <Activity className="h-4 w-4 text-white" />
                            </div>
                            <span className="text-[#2B4C7E]">词条监测详情</span>
                        </CardTitle>
                        <Button variant="outline" size="sm" onClick={handleExportPDF} disabled={exporting || isDemoPortal} title={isDemoPortal ? '演示案例为只读' : undefined}>
                            <Download className="h-4 w-4 mr-2" />
                            {exporting ? '导出中...' : '导出报告'}
                        </Button>
                    </CardHeader>
                    <CardContent className="space-y-2 p-4">
                        {data?.keywords?.length === 0 ? (
                            <div className="text-center py-8 text-muted-foreground">{dashboardSnapshotMissing ? '快照未包含' : '暂无监测数据'}</div>
                        ) : (
                            // CTO-15.23 2026-05-25 · 老板报"客户门户无法左右滑动"
                            // · 移动端 grid-cols-[16+1fr+48+48+96]≈208+1fr · 服务进度 96px 不够装"未达标 · 剩余 N 已达标 M/X"
                            // · 修法:外层 overflow-x-auto + min-w-[480px] · 让 5/6 列完整显示 + 移动端触摸横滑
                            <div className="overflow-x-auto -mx-2 px-2 touch-pan-x">
                            <div className="space-y-2 min-w-[480px]">
                            {/* 表头行 */}
                            <div
                                className="grid items-center px-2 sm:px-4 py-2 text-xs text-muted-foreground font-medium border-b grid-cols-[16px_1fr_48px_48px_96px] sm:grid-cols-[20px_1fr_60px_65px_180px_90px]"
                            >
                                <div />
                                <div>关键词</div>
                                <div className="text-center">出现率</div>
                                <div className="text-center">变化</div>
                                <div className="text-center">服务进度</div>
                                <div className="text-right hidden sm:block">检测日期</div>
                            </div>
                            {data?.keywords?.map((kw, i) => {
                                const isExpanded = expandedKeywords.has(i);
                                // 2026-05-15 React #31 防御:snippet 强转 string · 防后端 schema 漂移导致 ReactMarkdown 接 object 立爆
                                // [2026-06-06 老板 P1-1:真正 4 引擎全展示] 不再筛 is_detected · 也不筛 snippet ——
                                //   后端已补齐 4 引擎槽位(豆包/通义千问/DeepSeek/Kimi)· 提到=绿✓ / 未提到=灰○ / 待监测=灰 全显;
                                //   snippet 为空时显"暂无回答片段"不隐藏(0 命中是真实数据·不代表服务失败·数据驱动)。
                                const detectedDetails = (kw.detection_details || [])
                                    .map(d => ({ ...d, snippet: typeof d.snippet === 'string' ? d.snippet : String(d.snippet ?? '') }));
                                const hasDetails = detectedDetails.length > 0;
                                const platformNames: Record<string, string> = {
                                    'dashscope': '通义千问', 'deepseek': 'DeepSeek', 'kimi': 'Kimi', 'doubao': '豆包',
                                };

                                return (
                                    <div key={i} className="rounded-lg border border-border overflow-hidden">
                                        {/* 词条概览行 - 固定列宽grid布局 */}
                                        <div
                                            className={`grid items-center px-2 sm:px-4 py-3 grid-cols-[16px_1fr_48px_48px_96px] sm:grid-cols-[20px_1fr_60px_65px_180px_90px] ${hasDetails ? 'cursor-pointer hover:bg-blue-50/40' : ''} transition-colors`}
                                            onClick={() => {
                                                if (!hasDetails) return;
                                                setExpandedKeywords(prev => {
                                                    const next = new Set(prev);
                                                    if (next.has(i)) next.delete(i); else next.add(i);
                                                    return next;
                                                });
                                            }}
                                        >
                                            {hasDetails ? (
                                                isExpanded
                                                    ? <ChevronDown className="h-4 w-4 text-muted-foreground" />
                                                    : <ChevronRight className="h-4 w-4 text-muted-foreground" />
                                            ) : <div className="w-4" />}

                                            <span className="font-medium text-sm min-w-0 truncate">{kw.keyword}</span>

                                            <div className="text-center">
                                                {kw.lifecycle === 'deploying' || kw.lifecycle === 'pending' ? (
                                                    <Badge variant="outline" className="border-amber-400 text-amber-600 bg-amber-50">
                                                        {kw.lifecycle === 'deploying' ? '铺量中' : '待启动'}
                                                    </Badge>
                                                ) : (() => {
                                                    // [CTO-15.23 2026-05-11] display_rate 是后端新字段 · UI + AI 洞察同源
                                                    //   防 LLM 看 detection_rate(0%) 但 UI 看 effective_rate(75%) 打架
                                                    //   fallback 链:display_rate → effective_rate → detection_rate → 0
                                                    const displayRate = Number(kw.display_rate ?? kw.effective_rate ?? kw.detection_rate ?? 0);
                                                    const todayDropped = kw.is_today_dropped === true;
                                                    // CTO-15.23 2026-05-25 · 老板订正商业逻辑:
                                                    //   "话术只在报价页帮用户理解大概效果 · 监测/交付/客户门户用精确数据驱动"
                                                    //   原 describeProbability 话术 "问 2 次约出现 1 次" = 50% 等效模糊 · 无法验收交付
                                                    //   改:Badge 直接显示精确 N% · 跟代理后台 effective_rate 同源同显
                                                    //   memory feedback_data_driven_after_quote(2026-05-25 新)取代老 feedback_sov_translate_occurrence_rate
                                                    const tooltipText = todayDropped
                                                        ? `今日实时低于目标 · 但累计已达标`
                                                        : undefined;
                                                    return (
                                                        <Badge
                                                            variant={displayRate >= 60 ? 'default' : 'secondary'}
                                                            title={tooltipText}
                                                        >
                                                            {displayRate}%{todayDropped && ' ⚠️'}
                                                        </Badge>
                                                    );
                                                })()}
                                            </div>

                                            <div className="text-center">
                                                {kw.rate_change !== undefined && kw.rate_change !== null ? (
                                                    <span className={`text-xs ${kw.rate_change >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                                        {kw.rate_change >= 0 ? <TrendingUp className="h-3.5 w-3.5 inline mr-0.5" /> : <TrendingDown className="h-3.5 w-3.5 inline mr-0.5" />}
                                                        {Math.abs(kw.rate_change)}%
                                                    </span>
                                                ) : <span className="text-xs text-muted-foreground">-</span>}
                                            </div>

                                            {/* 达标状态 + 每词独立倒计时
                                                2026-05-15 老板报"挤" · 改 Badge + Countdown 横向 align-start 不 justify-center · 防 wrap */}
                                            <div className="text-center">
                                                {kw.lifecycle === 'monitoring' && kw.is_compliant !== undefined ? (
                                                    <div className="flex items-start gap-2">
                                                        <Badge variant="outline" className={`text-[11px] px-1.5 py-0 shrink-0 ${kw.is_compliant ? 'border-green-400 text-green-600 bg-green-50' : 'border-red-400 text-red-600 bg-red-50'}`}>
                                                            {kw.is_compliant ? '达标' : '未达标'}
                                                        </Badge>
                                                        {/* [2026-06-04 纯履约口径] 倒计时 = 还需达标天数(remaining_compliant)· 跟代理端 KeywordTable 同口径 */}
                                                        {kw.remaining_compliant != null && kw.remaining_compliant > 0 && (
                                                            <KeywordCountdown
                                                                compliantDays={kw.compliant_days || 0}
                                                                remainingCompliant={kw.remaining_compliant}
                                                                serviceDays={kw.service_days ?? null}
                                                                isCompliant={!!kw.is_compliant}
                                                                isStable={!!kw.is_stable}
                                                            />
                                                        )}
                                                    </div>
                                                ) : <span className="text-xs text-muted-foreground">-</span>}
                                            </div>

                                            <div className="text-right text-xs text-muted-foreground hidden sm:block">
                                                {kw.last_tested ? kw.last_tested.slice(0, 10) : '-'}
                                            </div>
                                        </div>

                                        {/* 展开区域：各平台AI回复 · [2026-06-06] 4 引擎全展示(提到/未提到均如实) */}
                                        {isExpanded && hasDetails && (
                                            <div className="border-t border-border bg-muted/60 px-2 sm:px-4 py-3 space-y-2">
                                                <p className="text-[11px] text-muted-foreground">4 大 AI 引擎实测结果如实展示(提到 / 未提到)。个别引擎暂未提到属正常,不代表服务失败,优化持续生效中。</p>
                                                {detectedDetails.map((detail, j) => {
                                                    const snippetKey = `${i}-${j}`;
                                                    const isSnippetExpanded = expandedSnippets.has(snippetKey);
                                                    const isLong = detail.snippet.length > 150;

                                                    return (
                                                        <div key={j} className="rounded-md border border-border bg-card p-3">
                                                            {/* 平台 + 时间 · [2026-06-06] 提到=绿✓ / 未提到=灰○(真实数据·不隐藏) */}
                                                            <div className="flex items-center justify-between mb-2">
                                                                <div className="flex items-center gap-1.5">
                                                                    {detail.is_detected
                                                                        ? <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                                                                        : <Circle className="h-3.5 w-3.5 text-muted-foreground" />}
                                                                    <span className="text-xs font-medium text-foreground">{platformNames[detail.platform] || detail.platform}</span>
                                                                    <span className={`text-[10px] px-1.5 py-0 rounded-full border ${detail.is_detected ? 'border-green-300 text-green-600' : 'border-border text-muted-foreground'}`}>
                                                                        {detail.is_detected ? '提到你' : (detail.pending ? '待监测' : '未提到')}
                                                                    </span>
                                                                </div>
                                                                <span className="text-[11px] text-muted-foreground">
                                                                    {detail.tested_at ? detail.tested_at.replace('T', ' ').slice(0, 16) : ''}
                                                                </span>
                                                            </div>

                                                            {/* 内容：[P1-1] 空片段显占位不隐藏·否则收起摘要/展开Markdown */}
                                                            {!detail.snippet ? (
                                                                <div className="text-xs text-muted-foreground italic">
                                                                    {detail.pending ? '该引擎本词暂未监测 · 下一轮出结果' : '本轮该引擎未返回可展示片段'}
                                                                </div>
                                                            ) : !isSnippetExpanded ? (
                                                                <div
                                                                    className={`text-xs text-muted-foreground ${isLong ? 'cursor-pointer hover:text-foreground' : ''}`}
                                                                    onClick={(e) => {
                                                                        if (!isLong) return;
                                                                        e.stopPropagation();
                                                                        setExpandedSnippets(prev => { const n = new Set(prev); n.add(snippetKey); return n; });
                                                                    }}
                                                                >
                                                                    {snippetPreview(detail.snippet, 100)}
                                                                    {isLong && <span className="text-blue-500 ml-1">展开</span>}
                                                                </div>
                                                            ) : (
                                                                <div>
                                                                    <div
                                                                        className="max-h-[60vh] overflow-y-auto overscroll-contain pr-1"
                                                                        style={{ WebkitOverflowScrolling: 'touch' }}
                                                                        onClick={(e) => e.stopPropagation()}
                                                                    >
                                                                        <div className="prose prose-sm dark:prose-invert max-w-none text-[13px] leading-relaxed text-foreground [&_h1]:text-sm [&_h2]:text-sm [&_h3]:text-sm [&_h4]:text-sm [&_strong]:text-foreground [&_hr]:my-2 [&_li]:text-muted-foreground [&_code]:text-xs [&_a]:text-blue-500 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground">
                                                                        <ReactMarkdown>
                                                                            {detail.snippet}
                                                                        </ReactMarkdown>
                                                                        </div>
                                                                    </div>
                                                                    <div
                                                                        className="text-xs text-blue-500 cursor-pointer mt-2 hover:text-blue-700"
                                                                        onClick={(e) => {
                                                                            e.stopPropagation();
                                                                            setExpandedSnippets(prev => { const n = new Set(prev); n.delete(snippetKey); return n; });
                                                                        }}
                                                                    >
                                                                        收起
                                                                    </div>
                                                                </div>
                                                            )}
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}

                            {/* [2026-06-07 批B 门户] 相关搜索参考(同义覆盖词)· 原价划线→免费 · 不单独监测/不计达标 · 监测列显 — */}
                            {data?.covered_keywords && data.covered_keywords.length > 0 && (
                                <div className="mt-3 pt-3 border-t border-dashed border-border">
                                    <p className="text-xs text-muted-foreground font-medium mb-2">
                                        相关搜索参考 {data.covered_keywords.length} 个 · 发核心词内容时顺带覆盖
                                    </p>
                                    <div className="space-y-1">
                                        {data.covered_keywords.map((cov, ci) => (
                                            <div
                                                key={`cov-${ci}`}
                                                className="grid items-center px-2 sm:px-4 py-2 rounded-lg bg-muted/40 grid-cols-[16px_1fr_48px_48px_96px] sm:grid-cols-[20px_1fr_60px_65px_180px_90px]"
                                            >
                                                <span className="w-1 h-1 rounded-full bg-gray-300" />
                                                <div className="min-w-0 flex items-center gap-2">
                                                    <span className="text-sm text-muted-foreground truncate">{cov.keyword}</span>
                                                    {cov.final_price != null && cov.final_price > 0 && (
                                                        <span className="text-[11px] whitespace-nowrap shrink-0">
                                                            <span className="text-gray-300 line-through mr-1">¥{cov.final_price}</span>
                                                            <span className="text-green-600 font-semibold">免费</span>
                                                        </span>
                                                    )}
                                                </div>
                                                <div className="text-center text-xs text-muted-foreground">—</div>
                                                <div className="text-center text-xs text-muted-foreground">—</div>
                                                <div className="text-center text-xs text-muted-foreground">—</div>
                                                <div className="text-right text-xs text-muted-foreground hidden sm:block">—</div>
                                            </div>
                                        ))}
                                    </div>
                                    <p className="mt-1.5 text-[10px] text-muted-foreground">顺带覆盖 · 不单独监测 · 不承诺达标</p>
                                </div>
                            )}
                            </div>
                            </div>
                        )}
                    </CardContent>
                </Card>

                {/* 报告中心 */}
                <Card>
                    <CardHeader className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2">
                        <CardTitle className="flex items-center gap-2">
                            <FileText className="h-5 w-5 text-blue-500 shrink-0" />
                            报告中心
                        </CardTitle>
                        <div className="flex gap-2">
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => handleExportData('csv')}
                                disabled={isDemoPortal}
                                title={isDemoPortal ? '演示案例为只读' : undefined}
                                className="border-green-300 text-green-700 hover:bg-green-50"
                            >
                                <Download className="h-4 w-4 sm:mr-1" />
                                <span className="hidden sm:inline">导出</span>CSV
                            </Button>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => handleExportData('excel')}
                                disabled={isDemoPortal}
                                title={isDemoPortal ? '演示案例为只读' : undefined}
                                className="border-blue-300 text-blue-700 hover:bg-blue-50"
                            >
                                <Download className="h-4 w-4 sm:mr-1" />
                                <span className="hidden sm:inline">导出</span>Excel
                            </Button>
                        </div>
                    </CardHeader>
                    <CardContent>
                        {reports.length === 0 ? (
                            <div className="text-center py-8 text-muted-foreground">
                                暂无报告
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {reports.map(report => (
                                    <div
                                        key={report.id}
                                        className={`flex flex-col sm:flex-row sm:justify-between sm:items-center p-3 rounded-lg cursor-pointer transition-all gap-2 ${report.report_type === 'monthly' ? 'bg-muted border border-border' : 'bg-muted hover:bg-muted/80'}`}
                                        onClick={() => openReportDetail(report)}
                                    >
                                        <div className="min-w-0 flex-1">
                                            <div className="flex items-center gap-2 flex-wrap">
                                                <Badge className={report.report_type === 'monthly' ? 'bg-linear-to-r from-[#2B4C7E] to-indigo-600 text-white hover:from-[#1E3A5F] hover:to-indigo-700' : ''} variant={report.report_type === 'weekly' ? 'default' : undefined}>
                                                    {report.report_type === 'weekly' ? '周报' : '月报'}
                                                </Badge>
                                                <span className="font-medium text-sm sm:text-base truncate">
                                                    {typeof report.summary_data?.period_label === 'string' && report.summary_data.period_label
                                                        ? report.summary_data.period_label
                                                        : `${report.period_start} ~ ${report.period_end}`}
                                                </span>
                                            </div>
                                            <div className="text-xs sm:text-sm text-muted-foreground mt-1">
                                                {/* CTO-15.23 2026-05-25 · 老板订正:交付环节数据驱动 · 话术仅报价用 */}
                                                AI 出现率: {Number(report.summary_data?.avg_detection_rate ?? 0)}% |
                                                词条数: {report.summary_data?.total_keywords || 0}
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-2 self-end sm:self-auto shrink-0">
                                            <span className="text-xs text-muted-foreground">
                                                {report.created_at?.split('T')[0] || '-'}
                                            </span>
                                            <ChevronRight className="h-4 w-4 text-muted-foreground" />
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </CardContent>
                </Card>

                {/* 报告详情弹窗 */}
                {selectedReport && (
                    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-xs" onClick={() => setSelectedReport(null)}>
                        <div className="bg-card rounded-t-xl sm:rounded-xl border border-border w-full sm:max-w-3xl max-h-[90vh] sm:max-h-[85vh] overflow-y-auto sm:m-4" onClick={e => e.stopPropagation()}>
                                <>
                                    {/* 弹窗头部 */}
                                    <div className="sticky top-0 bg-card border-b border-border px-4 sm:px-6 py-3 sm:py-4 rounded-t-xl flex items-center justify-between gap-2">
                                        <div className="min-w-0">
                                            <div className="flex items-center gap-2 flex-wrap">
                                                <Badge className={selectedReport.report_type === 'monthly' ? 'bg-linear-to-r from-[#2B4C7E] to-indigo-600 text-white' : ''} variant={selectedReport.report_type === 'weekly' ? 'default' : undefined}>
                                                    {selectedReport.report_type === 'weekly' ? '周报' : selectedReport.report_type === 'monthly' ? '月报' : selectedReport.report_type}
                                                </Badge>
                                                <span className="font-semibold text-base sm:text-lg truncate">
                                                    {typeof selectedReport.summary_data?.period_label === 'string' && selectedReport.summary_data.period_label
                                                        ? selectedReport.summary_data.period_label
                                                        : `${selectedReport.period_start} ~ ${selectedReport.period_end}`}
                                                </span>
                                            </div>
                                            <p className="text-xs text-muted-foreground mt-1">
                                                {selectedReport.period_start} ~ {selectedReport.period_end}
                                            </p>
                                        </div>
                                        <Button variant="ghost" size="sm" onClick={() => setSelectedReport(null)}>
                                            ✕
                                        </Button>
                                    </div>

                                    {/* 核心指标 */}
                                    <div className="px-4 sm:px-6 py-4">
                                        <div className="grid grid-cols-3 gap-2 sm:gap-4 mb-6">
                                            <div className="rounded-lg border bg-blue-50 border-blue-200 p-2.5 sm:p-4 text-center">
                                                {/* CTO-15.23 2026-05-25 · 老板订正:报告交付用精确数据 · 话术仅报价用 */}
                                                <div className="text-2xl sm:text-3xl font-bold text-blue-700 leading-snug">
                                                    {Number(selectedReport.summary_data?.detection_rate ?? selectedReport.summary_data?.avg_detection_rate ?? 0)}
                                                    <span className="text-base sm:text-xl font-normal text-blue-600">%</span>
                                                </div>
                                                <div className="text-[10px] sm:text-xs text-muted-foreground mt-1">AI 出现率</div>
                                            </div>
                                            <div className="rounded-lg border bg-muted p-2.5 sm:p-4 text-center">
                                                <div className="text-lg sm:text-2xl font-bold text-foreground">
                                                    {selectedReport.summary_data?.total_keywords || 0}
                                                </div>
                                                <div className="text-[10px] sm:text-xs text-muted-foreground mt-1">监测关键词</div>
                                            </div>
                                            <div className="rounded-lg border bg-muted p-2.5 sm:p-4 text-center">
                                                <div className="text-lg sm:text-2xl font-bold text-foreground">
                                                    {selectedReport.summary_data?.task_count || selectedReport.summary_data?.total_tests || 0}
                                                </div>
                                                <div className="text-[10px] sm:text-xs text-muted-foreground mt-1">监测次数</div>
                                            </div>
                                        </div>

                                        {/* 主题包表现 */}
                                        {(selectedReport.summary_data?.cluster_stats || []).length > 0 && (
                                            <div className="mb-6">
                                                <h3 className="font-semibold text-sm mb-3">📦 主题包表现</h3>
                                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                                    {(selectedReport.summary_data.cluster_stats as any[]).map((cs: any, i: number) => (
                                                        <div key={i} className="flex items-center justify-between p-2.5 rounded-lg bg-muted">
                                                            <div>
                                                                <span className="text-sm font-medium">{cs.cluster_name}</span>
                                                                <span className="text-xs text-muted-foreground ml-1.5">{cs.keyword_count}词</span>
                                                            </div>
                                                            <span className={`text-sm font-semibold ${cs.avg_rate >= 60 ? 'text-green-600' : cs.avg_rate >= 30 ? 'text-amber-600' : 'text-red-600'}`}>
                                                                {cs.avg_rate?.toFixed(1)}%
                                                            </span>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {/* 关键词表现 */}
                                        {(selectedReport.summary_data?.keyword_stats || []).length > 0 && (
                                            <div className="mb-6">
                                                <h3 className="font-semibold text-sm mb-3">关键词表现</h3>
                                                <div className="space-y-2">
                                                    {(selectedReport.summary_data.keyword_stats as any[]).map((kw: any, i: number) => (
                                                        <div key={i} className="flex items-center justify-between p-2.5 rounded-lg bg-muted">
                                                            <span className="text-sm">{kw.keyword}</span>
                                                            <div className="flex items-center gap-3">
                                                                <div className="w-24 bg-muted rounded-full h-1.5">
                                                                    <div
                                                                        className={`h-1.5 rounded-full ${kw.avg_rate >= 60 ? 'bg-green-500' : kw.avg_rate >= 30 ? 'bg-yellow-500' : 'bg-red-500'}`}
                                                                        style={{ width: `${Math.min(kw.avg_rate, 100)}%` }}
                                                                    />
                                                                </div>
                                                                <span className="text-sm font-medium w-14 text-right">{kw.avg_rate?.toFixed(1)}%</span>
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        {/* 报告内容(Markdown 渲染) */}
                                        {/* [CTO-15.3 2026-04-21] dark:prose-invert 修暗色 bold/li/heading 看不清 */}
                                        {/* [CTO-15.23 2026-05-05] dangerouslySetInnerHTML+renderMarkdown → ReactMarkdown
                                              · 修"展开回答里很多 markdown 符号没渲染干净" · 支持 # 1 级标题/code/blockquote/链接/表格 */}
                                        {typeof selectedReport.content === 'string' && selectedReport.content && (
                                            <div
                                                className="border rounded-lg p-5 bg-muted/50 max-h-[70vh] overflow-y-auto overscroll-contain"
                                                style={{ WebkitOverflowScrolling: 'touch' }}
                                            >
                                                <h3 className="font-semibold text-sm mb-3">报告详情</h3>
                                                <div className="prose prose-sm dark:prose-invert max-w-none text-sm leading-relaxed text-foreground">
                                                    <ReactMarkdown>
                                                        {selectedReport.content}
                                                    </ReactMarkdown>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                </>
                        </div>
                    </div>
                )}

                {/* 社媒内容展示 - 暂未对接，后续开放 */}

                {/* 隐私轻提示 (CTO-C 2026-04-26 老板拍板文案) */}
                <div className="mt-8 mb-4 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                    <PrivacyNotice />
                    {/* WO_329 开源版署名位;开关关 ⇒ 不渲染 */}
                    <OssAttribution />
                </div>
            </main >
        </div >
    );
}
