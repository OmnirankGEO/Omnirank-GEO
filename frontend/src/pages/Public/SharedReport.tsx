/**
 * SharedReport — 公开报告页面（无需登录 · 客户可见产物）
 * v3.6 白标：header/SEO/og/sr-only 读后端内联 report.whitelabel（已 customer-gate + 脱敏 · 无授权留白不回退平台）· [audit #13 2026-06-10]
 *
 * CTO-B 2026-04-26 W4 · 升级:
 *   · report.report_version === 'v2' 时显示 v2 客户版 + 完整度 banner
 *   · report.report_v2_error 不为空时顶部显示 "报告生成异常" 警示(决策点 5 透明化)
 *   · 默认 light 优先(决策点 4)· 不接 dark 主题
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import ReactMarkdown from '@/components/SafeMarkdown';
import { Loader2, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/lib/utils';
import { BrandLogo, BrandFooter } from '@/components/brand/BrandDisplay';
import { mountOpened } from '@/lib/customerEvents';
import { PrivacyNotice } from '@/components/customer/PrivacyNotice';
import { OssAttribution } from '@/components/common/OssAttribution';

// [2026-06-01 玩法B 防穿帮 · 老板拍板] 客户面诊断报告留资 CTA 已整组移除(原 CTA_ACTIONS/handleAction/action 状态)
//   理由:① 能做诊断 = 代理早有客户联系方式 · 留资冗余 ② 报告会被客户转给领导/甲方 · 留资入口=向甲方暴露"背后有可直接联系的供应商" → 甲方绕单 / 代理中间赚差价穿帮
//   客户面 = 纯交付物 · 品牌身份留 BrandLogo + BrandFooter · 不挂任何回连漏斗的入口 · 侧边栏「客户线索」入口同步隐藏(AppSidebar)
//   静默"已查看"埋点 mountOpened 保留(客户无感 · 不穿帮 · 老板 Q1 选:只删可见留资·留静默已读)

/**
 * D.8 SEO Meta 注入 · CTO-15.18 PM 干预
 * 用 useEffect 改 document.title + meta · 不引入 react-helmet 依赖
 */
function SeoMeta({ title, description }: { title: string; description: string }) {
    useEffect(() => {
        if (!title) return;
        const oldTitle = document.title;
        document.title = title;

        const metaSet = (name: string, content: string, useProperty = false) => {
            const attr = useProperty ? 'property' : 'name';
            let m = document.querySelector(`meta[${attr}="${name}"]`) as HTMLMetaElement | null;
            if (!m) {
                m = document.createElement('meta');
                m.setAttribute(attr, name);
                document.head.appendChild(m);
            }
            m.content = content;
        };

        metaSet('description', description);
        metaSet('og:title', title, true);
        metaSet('og:description', description, true);
        metaSet('og:type', 'article', true);
        metaSet('twitter:card', 'summary');
        metaSet('twitter:title', title);
        metaSet('twitter:description', description);

        return () => {
            document.title = oldTitle;
        };
    }, [title, description]);
    return null;
}


function normalizeReportError(detail: unknown): string {
    if (!detail) return '链接无效或已过期';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        const first = detail[0];
        if (typeof first === 'string') return first;
        if (first && typeof first === 'object' && 'msg' in first) {
            return String((first as { msg?: unknown }).msg || '链接无效或已过期');
        }
        return '链接无效或已过期';
    }
    if (typeof detail === 'object' && 'message' in detail) {
        return String((detail as { message?: unknown }).message || '链接无效或已过期');
    }
    return '链接无效或已过期';
}

export default function SharedReport() {
    const { id } = useParams<{ id: string }>();
    const [searchParams] = useSearchParams();
    // [self-review r4 2026-05-23] P0-1 闭环:URL 上的 st(share_token)必须透传给所有 API
    // 防 strict 模式(PUBLIC_REPORT_REQUIRE_TOKEN=true)下报告/v2 html/lead/action 全 404
    const shareToken = searchParams.get('st');

    const [report, setReport] = useState<any>(null);
    // [audit #13 2026-06-10] 白标改读后端内联 report.whitelabel(已 customer-gate + 联系方式脱敏)·
    //   不再按 user_id 二次调白标 —— 匿名面不暴露 brand_owner_user_id(防代理画像枚举)
    const brand = report?.whitelabel || null;
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    // [WJ-08 P0 2026-06-01 · Codex smoke] v2.html 加载失败标记 · v2 报告禁降级 Markdown
    // (慢网/失败时原始 markdown 会闪露折叠标签/模型名/原始测试数据给客户面 · 王姐最怕"发给客户丢脸")
    const [v2HtmlError, setV2HtmlError] = useState(false);

    // 客户行为埋点 · mount 即写 opened + 启动 dwell 计时器(静默已读 · 客户无感 · 留资 CTA 已移除见文件头说明)
    useEffect(() => {
        if (!id) return;
        const diagId = parseInt(id, 10);
        if (Number.isNaN(diagId)) return;
        const cleanup = mountOpened({
            source: 'public_report',
            diagnosisId: diagId,
        });
        return () => cleanup();
    }, [id]);

    // [WJ-08 P0] v2 咨询版式 HTML 加载 · 抽出复用(首次加载 + 失败重试)
    // 失败置 v2HtmlError(不再吞错降级 Markdown · 防客户面闪露原始内容)
    const loadV2Html = useCallback((reportId: string) => {
        setV2HtmlError(false);
        const htmlParams = new URLSearchParams({ theme: 'light_corporate' });
        if (shareToken) htmlParams.set('st', shareToken);
        fetch(`/api/public/report/${reportId}/v2.html?${htmlParams.toString()}`)
            .then(r2 => (r2.ok ? r2.text() : null))
            .then(htmlText => {
                if (htmlText) {
                    setReport((prev: any) => prev ? {
                        ...prev,
                        v2_html_content: htmlText,
                        report_v3_html: htmlText.includes('report-v3'),
                    } : prev);
                } else {
                    setV2HtmlError(true); // v2.html 404/空 · 走错误态 · 绝不回退原始 Markdown
                }
            })
            .catch(() => setV2HtmlError(true));
    }, [shareToken]);

    useEffect(() => {
        if (!id) return;
        if (!/^\d+$/.test(id)) {
            setError('链接无效或已过期');
            setLoading(false);
            return;
        }
        const reportParams = new URLSearchParams();
        if (shareToken) reportParams.set('st', shareToken);
        const qs = reportParams.toString() ? `?${reportParams.toString()}` : '';
        // 第一步：加载轻量数据（Markdown + 元信息）
        fetch(`/api/public/report/${id}${qs}`)
            .then(r => r.json().catch(() => ({ detail: '网络响应异常' })))
            .then(data => {
                if (data.status === 'success') {
                    setReport(data.report);

                    // CTO-B 2026-04-26 W5 · v2 优先 fetch 咨询版式 HTML(决策点 3:网页和 PDF 同一套组件)
                    // [WJ-08 P0] 失败不再吞错降级 markdown · 改走骨架/错误态(见 loadV2Html + 渲染区 isV2 守卫)
                    if (data.report.report_version === 'v2') {
                        loadV2Html(id);
                    } else if (data.report.has_html) {
                        // 老 v1 · 加载老 PDF 模板 HTML(~2MB)
                        const sep = qs ? '&' : '?';
                        fetch(`/api/public/report/${id}${qs}${sep}include_html=1`)
                            .then(r2 => r2.json())
                            .then(d2 => {
                                if (d2.report?.html_content) {
                                    setReport((prev: any) => prev ? { ...prev, html_content: d2.report.html_content } : prev);
                                }
                            })
                            .catch(() => {});
                    }
                } else {
                    setError(normalizeReportError(data.detail));
                }
            })
            .catch(() => setError('网络错误'))
            .finally(() => setLoading(false));
    }, [id, shareToken, loadV2Html]);

    // [2026-06-01 玩法B 防穿帮] handleAction(POST /report/{id}/action 留资)已随客户面 CTA 一并移除 · 见文件头说明

    if (loading) {
        return (
            <div className="min-h-screen bg-background flex items-center justify-center">
                <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
        );
    }

    if (error || !report) {
        return (
            <div className="min-h-screen bg-background flex items-center justify-center">
                <div className="text-center space-y-3">
                    <p className="text-lg font-medium text-foreground">报告不存在或已失效</p>
                    <p className="text-sm text-muted-foreground">{error}</p>
                </div>
            </div>
        );
    }

    // D.8 (CTO-15.18 · 2026-04-28):iframe SEO 修复 · meta + OG + hidden text 给爬虫
    // 老板红线:不嵌空 src iframe · 直接 SSR 出 HTML 文档 · 加 meta + OG
    // 实际是 srcDoc html · 但搜索引擎不索引 iframe 内容 → 加 hidden text 给爬虫
    // v3.6 白标 · SEO/og/sr-only 不再硬编码平台名 · 走 useBranding 的 brand（external_only 决策 C：永远非空，未填字段以 company_name 兜底）
    const brandLabel = brand?.product_name || brand?.company_name || '';
    const seoTitle = `${report.brand_name || brandLabel} GEO 诊断报告 · AI 搜索优化`;
    const seoDesc = `AI 搜索可见度评分 ${report.score ?? '—'}/100 · 4 大主流 AI 引擎实测 · ${report.brand_name || ''} 的真实表现`;
    // [P2-12 fix 2026-05-23] 先移除 style/script/svg/noscript 块 · 再 strip tags · 防 CSS 污染 SEO 文本
    const seoText = (report.html_content || report.v2_html_content || '')
        .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, '')
        .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
        .replace(/<noscript\b[^<]*(?:(?!<\/noscript>)<[^<]*)*<\/noscript>/gi, '')
        .replace(/<svg\b[^<]*(?:(?!<\/svg>)<[^<]*)*<\/svg>/gi, '')
        .replace(/<[^>]+>/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 500);

    // [WJ-08 P0] v2 报告标识 · v2 必须走 v2.html(就绪)/骨架(加载中)/错误态 · 永不降级原始 Markdown
    const isV2 = report.report_version === 'v2';
    // [CTO-15.23 2026-05-09] v2 模式 wrapper 用 dark bg 跟 V2HtmlEmbed 暗色一致 · 防视觉断层大片白
    // 老 v1 / Markdown 降级保持 bg-background 原样
    const wrapperBgClass = report.v2_html_content ? 'bg-zinc-950' : 'bg-background';
    return (
        <div className={`min-h-screen ${wrapperBgClass}`}>
            {/* D.8 SEO meta(set on mount via useEffect or react-helmet · 这里用 useEffect 直接改 document) */}
            <SeoMeta title={seoTitle} description={seoDesc} />
            {/* D.8 hidden 给爬虫的纯文本(屏幕阅读器 + 搜索引擎索引) · 可见用户不影响 */}
            <div aria-hidden="true" className="sr-only">
                <h1>{seoTitle}</h1>
                <p>{seoDesc}</p>
                <p>{seoText}</p>
            </div>
            {/* Header · v2 模式不显示(V2HtmlEmbed 自带 cdv2-topbar 品牌+快照标识)· 老路径保留 */}
            {!report.v2_html_content && (
                <div className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-10">
                    <div className="max-w-4xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between">
                        <BrandLogo brand={brand} size="md" />
                        <Link to="/login" className="text-xs text-muted-foreground hover:text-foreground transition-colors">
                            登录
                        </Link>
                    </div>
                </div>
            )}

            {/* CTO-B W5 · v2 咨询版式 HTML 优先(决策点 3 · light 默认) */}
            {report.v2_html_content && (
                <V2HtmlEmbed html={report.v2_html_content} />
            )}

            {/* [WJ-08 P0 2026-06-01 · Codex smoke] v2 报告 · v2.html 未就绪期严禁降级原始 Markdown
                原始 markdown 含折叠标签/模型名/原始测试数据,慢网会闪现给客户面(王姐最怕"发给客户丢脸")
                加载中 → 骨架屏 · 失败 → 人话错误 + 重试 · 绝不回退 ReactMarkdown */}
            {isV2 && !report.v2_html_content && (
                v2HtmlError
                    ? <V2ErrorState onRetry={() => id && loadV2Html(id)} hasGenError={Boolean(report.report_v2_error)} />
                    : <V2LoadingState />
            )}

            {/* 老 v1 · HTML 精美版 — 全宽展示(非 v2) */}
            {!isV2 && !report.v2_html_content && report.html_content && (
                <div className="py-4">
                    <HtmlReportViewer html={report.html_content} />
                </div>
            )}

            {/* Markdown 降级 · 仅非 v2 报告(v2 报告永不走此路 · 已由上方骨架/错误兜底) */}
            {!isV2 && !report.v2_html_content && !report.html_content && (
                <div className="max-w-4xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
                    <div className="mb-6 sm:mb-8">
                        <h1 className="text-xl sm:text-2xl font-bold text-foreground">{report.brand_name} — AI 搜索可见度报告</h1>
                        <div className="flex flex-wrap items-center gap-3 mt-2 text-sm text-muted-foreground">
                            {report.score != null && (
                                <span className="px-2 py-0.5 rounded bg-primary/10 text-primary font-medium">
                                    综合得分 {report.score}
                                </span>
                            )}
                            {report.level && <span>{report.level}</span>}
                            {report.keyword_count > 0 && <span>{report.keyword_count} 个关键词</span>}
                            <span>{new Date(report.created_at).toLocaleDateString('zh-CN')}</span>
                            {report.report_version === 'v2' && (
                                // B.9 (CTO-15.18 · 2026-04-28):去"客户版"标记 · 老板红线"暗示客户还有内部版"
                                <span className="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-700 font-medium text-xs">
                                    GEO 诊断报告
                                </span>
                            )}
                        </div>
                    </div>

                    {/* CTO-B W4 · 异常 banner(决策点 5 透明化) */}
                    {report.report_v2_error && (
                        <div className="mb-6 rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 flex items-start gap-3">
                            <AlertTriangle className="h-5 w-5 text-rose-600 shrink-0 mt-0.5" />
                            <div className="space-y-1 text-sm">
                                <p className="font-semibold text-rose-900">报告生成存在异常</p>
                                <p className="text-rose-800/80 text-xs break-words">{report.report_v2_error}</p>
                                <p className="text-rose-700/70 text-xs">
                                    建议联系您的顾问重新生成报告 · 当前内容仅供参考。
                                </p>
                            </div>
                        </div>
                    )}

                    {/* CTO-B W4 · 完整度 banner(决策点 5 · 不假装数据完整) */}
                    {report.report_version === 'v2' && typeof report.data_completeness_score === 'number' && (
                        <div
                            className={cn(
                                "mb-6 rounded-lg border px-4 py-3 flex items-start gap-3",
                                report.data_completeness_score >= 60
                                    ? "border-emerald-300 bg-emerald-50"
                                    : report.data_completeness_score >= 35
                                    ? "border-amber-300 bg-amber-50"
                                    : "border-rose-300 bg-rose-50"
                            )}
                        >
                            <Info
                                className={cn(
                                    "h-5 w-5 shrink-0 mt-0.5",
                                    report.data_completeness_score >= 60
                                        ? "text-emerald-600"
                                        : report.data_completeness_score >= 35
                                        ? "text-amber-600"
                                        : "text-rose-600"
                                )}
                            />
                            <div className="space-y-1 text-sm">
                                <p className="font-semibold text-foreground">
                                    本次诊断输入完整度 · {report.data_completeness_score}/100
                                    {report.data_completeness_breakdown?.level && (
                                        <span className="text-xs text-muted-foreground ml-2">
                                            ({report.data_completeness_breakdown.level})
                                        </span>
                                    )}
                                </p>
                                {report.data_completeness_breakdown?.missing_summary && (
                                    <p className="text-xs text-muted-foreground">
                                        {report.data_completeness_breakdown.missing_summary}
                                    </p>
                                )}
                                {report.data_completeness_score < 60 && (
                                    <p className="text-[11px] text-muted-foreground/80">
                                        提示:报告内若有"数据不足"标注 · 是真实情况 · 建议补全后重新生成。
                                    </p>
                                )}
                            </div>
                        </div>
                    )}

                    {report.content ? (
                        <div className="prose prose-neutral max-w-none dark:prose-invert prose-table:border-collapse prose-th:border prose-th:border-border prose-th:p-2 prose-th:bg-muted prose-td:border prose-td:border-border prose-td:p-2 overflow-x-auto">
                            <ReactMarkdown>{report.content}</ReactMarkdown>
                        </div>
                    ) : (
                        <div className="py-12 text-center text-muted-foreground">
                            <p>报告内容暂不可用</p>
                        </div>
                    )}
                </div>
            )}

            {/* [2026-06-01 玩法B 防穿帮 · 老板拍板] 客户面"对这份报告感兴趣?留资联系顾问"CTA 卡(含 3 按钮 + BrandContact)整块已移除
                报告 = 纯交付物 · 可干净转发给领导/甲方 · 不挂任何回连漏斗的入口 · 品牌身份由 header BrandLogo + 下方 BrandFooter 承载 */}

            {/* Footer · v2 自带完整 footer(cdv2-final-footer + sticky CTA)· 这里仅渲染 PrivacyNotice 隐私合规 */}
            {/* 老 v1 / Markdown 降级路径继续渲染 BrandFooter + PrivacyNotice */}
            <div className="mx-auto px-4 sm:px-6 pb-8 space-y-3" style={{ maxWidth: '1123px' }}>
                {!isV2 && !report.v2_html_content && <BrandFooter brand={brand} />}
                {/* WO_329 开源版署名位:v2 HTML 由服务端渲染,自带署名行;这里只补 v1 / Markdown 降级路径 */}
                {!isV2 && !report.v2_html_content && <OssAttribution />}
                <PrivacyNotice />
            </div>
        </div>
    );
}


/**
 * V2LoadingState — WJ-08 P0 · v2 咨询版式 HTML 加载中骨架屏
 * v2 报告必须等 v2.html 到达再渲染 · 加载期绝不露原始 Markdown(折叠标签/模型名/原始数据)
 */
function V2LoadingState() {
    return (
        <div className="min-h-[60vh] flex flex-col items-center justify-center gap-5 px-6 py-16 text-center" style={{ background: '#F8FAFC' }}>
            <Loader2 className="h-8 w-8 animate-spin text-slate-400" />
            <div className="space-y-1">
                <p className="text-base font-medium text-slate-700">报告正在打开,请稍等…</p>
                <p className="text-sm text-slate-400">正在为你加载完整诊断报告</p>
            </div>
            <div className="w-full max-w-2xl mt-2 space-y-3" aria-hidden="true">
                <div className="h-7 w-2/5 rounded bg-slate-200/80 animate-pulse" />
                <div className="h-4 w-full rounded bg-slate-200/60 animate-pulse" />
                <div className="h-4 w-5/6 rounded bg-slate-200/60 animate-pulse" />
                <div className="h-44 w-full rounded-xl bg-slate-200/50 animate-pulse" />
            </div>
        </div>
    );
}

/**
 * V2ErrorState — WJ-08 P0 · v2.html 加载失败的人话错误态
 * 绝不回退原始 Markdown · 只给"重新加载";report_v2_error 仅用其存在性(不渲染原始技术串,可能含模型名/黑话)
 */
function V2ErrorState({ onRetry, hasGenError }: { onRetry: () => void; hasGenError: boolean }) {
    return (
        <div className="min-h-[60vh] flex flex-col items-center justify-center gap-4 px-6 py-16 text-center" style={{ background: '#F8FAFC' }}>
            <AlertTriangle className="h-9 w-9 text-amber-500" />
            <div className="space-y-1.5 max-w-md">
                <p className="text-base font-medium text-slate-700">报告暂时没打开</p>
                <p className="text-sm text-slate-500">网络不太稳定 · 点下面重新加载一下就好</p>
                {hasGenError && (
                    <p className="text-xs text-slate-400 pt-1">若多次打不开 · 可联系发你这条链接的人重新生成报告</p>
                )}
            </div>
            <button
                onClick={onRetry}
                className="min-h-11 px-6 inline-flex items-center justify-center rounded-xl bg-slate-900 text-white font-medium text-sm hover:bg-slate-800 transition-colors"
            >
                重新加载
            </button>
        </div>
    );
}


/**
 * V2HtmlEmbed — CTO-B W5 · 咨询版式 HTML 嵌入(决策点 3 网页+PDF 同套 · 决策点 4 light)
 *
 * 直接 inject 后端 render_report_html 产出的 HTML(含 <style>)
 * 不缩放 · 不转 PDF 缩略图 · 网页本身就是终态咨询报告版式
 * iframe 隔离样式 · 避免 SharedReport 父级 dark 主题污染 light 报告
 */
function V2HtmlEmbed({ html }: { html: string }) {
    // [2026-05-30 根治 · 老板报"分享链接大片空白 · 分辨率不同白多少不同"]
    // 真根因:旧实现用 <iframe srcDoc> + JS 测 body.scrollHeight 动态设 iframe 高度。
    //   srcDoc + 异步内容(雷达 SVG/字体/图片)+ 跨分辨率 → scrollHeight 永远有测量竞态,
    //   算大了 → iframe 下半露出 iframe 内 body 的浅底 #F8FAFC = 大片白(分辨率越大误差越大)。
    //   前 4 任都在调"测高参数"(initialH/polling/only-increase)治标,没根治。
    // 根治:废 iframe,改 Shadow DOM 直插。内容在文档流里由浏览器原生撑开高度 = 不可能空白/裁切。
    //   Shadow DOM 隔离样式,避免报告 light 主题 ↔ 父页 dark 主题互相污染(替代原 iframe 的隔离作用)。
    const hostRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const host = hostRef.current;
        if (!host) return;

        // 1) 解析后端整份 HTML 文档,取 <style> 与 <body> 内容
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const styleCss = Array.from(doc.querySelectorAll('style')).map((s) => s.textContent || '').join('\n');
        const bodyInner = doc.body ? doc.body.innerHTML : html;

        // 2) Shadow DOM 内 document.getElementById/querySelector 查不到 shadow 内元素(边界不被穿透),
        //    而报告的 inline onclick + <script> 全用 document.xxx → 必须重定向到 shadowRoot。
        //    window.__cdv2Root 由下方注入前赋值;rewrite 把 document.(getElementById|querySelector...) 指过去。
        const rewriteDocScope = (code: string) =>
            code.replace(/document\.(getElementById|querySelector|querySelectorAll)\(/g,
                '(window.__cdv2Root||document).$1(');

        const bodyRewritten = rewriteDocScope(bodyInner);

        // 3) attach shadow root(已存在则复用)
        const shadow = host.shadowRoot || host.attachShadow({ mode: 'open' });
        // body 选择器在 shadow 内无 <body> 元素 → 用 :host + 根 wrapper 承接 body 的视觉样式
        // (背景/字体/行高);丢弃 body{padding-bottom:64px}(那是旧 iframe sticky 占位,正是多余空白来源之一)
        shadow.innerHTML =
            `<style>
:host{display:block;background:#F8FAFC;}
.cdv2-doc-root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;color:#0F172A;line-height:1.6;-webkit-font-smoothing:antialiased;}
${styleCss}
</style>
<div class="cdv2-doc-root">${bodyRewritten}</div>`;

        // 4) 指 window.__cdv2Root 到本 shadowRoot,再手动重执行 <script>(innerHTML 注入的 script 不会自动跑)
        (window as unknown as { __cdv2Root?: ShadowRoot }).__cdv2Root = shadow;
        doc.querySelectorAll('script').forEach((old) => {
            try {
                const s = document.createElement('script');
                s.textContent = rewriteDocScope(old.textContent || '');
                shadow.appendChild(s);
            } catch {
                // 单个脚本失败不阻断渲染
            }
        });

        return () => {
            // 清理:卸载/重渲染时清掉 shadow 内容(window.__cdv2Root 指向最后一个挂载实例,单页仅一个 V2 报告)
            try { shadow.innerHTML = ''; } catch { /* noop */ }
        };
    }, [html]);

    // 高度天然由 shadow 内容撑开 = 不再有 iframe 测高 = 永不空白/裁切;外层 bg 跟报告浅底一致防任何瞬时断层
    return <div ref={hostRef} className="w-full" style={{ background: '#F8FAFC' }} />;
}


/**
 * HtmlReportViewer — PDF 模板 HTML 以"缩略图"模式在网页展示(老 v1 路径)
 *
 * 原理：保留 A4 横版原始尺寸不动（1123×794px），用 transform:scale
 * 整体等比缩放到屏幕宽度内，居中显示。像一个内嵌的 PDF 阅读器。
 *
 * 不修改任何 inline style — 页面内部的绝对定位布局依赖固定尺寸。
 */
function HtmlReportViewer({ html }: { html: string }) {
    const wrapRef = useRef<HTMLDivElement>(null);
    const innerRef = useRef<HTMLDivElement>(null);
    const [scale, setScale] = useState(1);
    const [innerH, setInnerH] = useState(0);
    const [offsetLeft, setOffsetLeft] = useState(0);

    const PAGE_W = 1123; // 297mm

    const recalc = useCallback(() => {
        const wrap = wrapRef.current;
        const inner = innerRef.current;
        if (!wrap || !inner) return;
        const cw = wrap.clientWidth;
        const s = Math.min(1, cw / PAGE_W);
        setScale(s);
        setInnerH(inner.scrollHeight);
        // 桌面端居中：缩放后实际宽度 < 容器宽度时，左移居中
        const scaledW = PAGE_W * s;
        setOffsetLeft(scaledW < cw ? (cw - scaledW) / 2 : 0);
    }, []);

    useEffect(() => {
        recalc();
        const t = setTimeout(recalc, 300);
        const inner = innerRef.current;
        const recalcSoon = () => {
            recalc();
            window.setTimeout(recalc, 50);
            window.setTimeout(recalc, 250);
        };
        let ro: ResizeObserver | null = null;
        if (inner) {
            inner.addEventListener('toggle', recalcSoon, true);
            inner.addEventListener('click', recalcSoon, true);
            if ('ResizeObserver' in window) {
                ro = new ResizeObserver(recalcSoon);
                ro.observe(inner);
            }
        }
        window.addEventListener('resize', recalc);
        return () => {
            clearTimeout(t);
            inner?.removeEventListener('toggle', recalcSoon, true);
            inner?.removeEventListener('click', recalcSoon, true);
            ro?.disconnect();
            window.removeEventListener('resize', recalc);
        };
    }, [recalc, html]);

    return (
        <div ref={wrapRef} className="w-full">
            <div style={{ height: innerH ? `${innerH * scale}px` : 'auto', overflow: 'hidden' }}>
                <div
                    ref={innerRef}
                    style={{
                        width: `${PAGE_W}px`,
                        transformOrigin: 'top left',
                        transform: `scale(${scale})`,
                        marginLeft: `${offsetLeft}px`,
                    }}
                    dangerouslySetInnerHTML={{ __html: html }}
                />
            </div>
        </div>
    );
}
