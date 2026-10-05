/**
 * ReportV2View · A.5 (CTO-15.9 session 3 · 2026-04-25)
 *
 * 检测 v2 报告 markdown · 按 8 模块 ## heading 拆 Card 独立渲染
 *
 * v2 起点判定:content.trimStart().startsWith('## 1 分钟结论') 或旧版 '## 封面结论'
 * 模块标题(对齐 services/report_writer_v2.py):
 *   1. ## 1 分钟结论
 *   1a. ## 这份报告应该怎么读
 *   2. ## GEO 总分
 *   3. ## AI 实测证据
 *   4. ## 竞品分析(AI 搜索同频)
 *   5. ## 机会解读
 *   6. ## 行动优先级(Top 10)
 *   7. ## 报告解读提纲
 *   8. ## 报告局限说明 / 如需进一步沟通
 *
 * fallback:遇到非预期 heading · 仍按 ## 切分 · 顺序展示 · 不丢内容
 */
import ReactMarkdown from '@/components/SafeMarkdown'; // [2026-07-22 板块B] 全站 Markdown SSOT（verify-markdown-ssot 门禁）
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Sparkles, BarChart3, Search, Swords, TrendingUp, ListChecks, Calendar, Megaphone, FileText,
} from 'lucide-react';
// B3 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 2) · 真雷达图
import { ResponsiveContainer, RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar } from 'recharts';
// 板块 A(2026-07-22)· Module 3 结构化逐格判定 + 人工确认
import { BrandCellsSection } from './components/BrandVerdictCell';

const V2_MARKERS = ['## 1 分钟结论', '## 封面结论'];

export function isReportV2(content: string | null | undefined): boolean {
  if (!content) return false;
  const start = content.trimStart();
  return V2_MARKERS.some(marker => start.startsWith(marker));
}

interface Section {
  heading: string;
  body: string;
  /** 1-8 · 0=未识别(fallback) */
  moduleIndex: number;
}

const HEADING_ORDER: { match: RegExp; index: number; icon: typeof Sparkles; label: string }[] = [
  { match: /^(1\s*分钟结论|封面结论)/, index: 1, icon: Sparkles, label: '结论' },
  { match: /^这份报告应该怎么读/, index: 1, icon: FileText, label: '报告解读' },
  { match: /^(GEO\s*总分|评分雷达)/, index: 2, icon: BarChart3, label: '评分总览' },
  { match: /^(\d{1,2}[.、\s]\s*)?AI\s*实测证据/, index: 3, icon: Search, label: 'AI 实测证据' },
  { match: /^竞品分析/, index: 4, icon: Swords, label: '竞品分析' },
  { match: /^(机会解读|机会估算)/, index: 5, icon: TrendingUp, label: '机会解读' },
  { match: /^行动优先级/, index: 6, icon: ListChecks, label: '行动优先级' },
  { match: /^(报告解读提纲|30\s*天行动计划)/, index: 7, icon: Calendar, label: '解读提纲' },
  { match: /^(报告局限说明|如需进一步沟通|方案承接)/, index: 8, icon: Megaphone, label: '客观收束' },
];

function classify(heading: string): { index: number; icon: typeof Sparkles; label: string } {
  for (const { match, index, icon, label } of HEADING_ORDER) {
    if (match.test(heading)) return { index, icon, label };
  }
  return { index: 0, icon: FileText, label: heading };
}

/**
 * B3 (CTO-15.9 session 3) · 解析 Module 2 markdown 表 → recharts 数据
 * 输入示例:
 *   | 维度 | 分数 |
 *   | --- | --- |
 *   | 问题解决 | 70.5 |
 *   | 品牌契合 | — |   ← missing 维度
 */
function parseRadarFromBody(body: string): { dimension: string; score: number | null }[] {
  const out: { dimension: string; score: number | null }[] = [];
  const lines = body.split(/\r?\n/);
  for (const line of lines) {
    if (!line.trim().startsWith('|')) continue;
    if (line.includes('---')) continue;
    if (line.includes('维度') && line.includes('分数')) continue;
    const cells = line.split('|').map(s => s.trim()).filter(Boolean);
    if (cells.length < 2) continue;
    const dimension = cells[0];
    const scoreStr = cells[1];
    const score = scoreStr === '—' || scoreStr === '-' || !scoreStr ? null : parseFloat(scoreStr);
    if (dimension && (score === null || !Number.isNaN(score))) {
      out.push({ dimension, score });
    }
  }
  return out;
}

function splitV2(markdown: string): Section[] {
  // 按行扫 · 遇 `## ` 起新段 · 第一段如果不是 `## ` 起则归入"前言"段
  const lines = markdown.split(/\r?\n/);
  const sections: Section[] = [];
  let current: Section | null = null;

  for (const line of lines) {
    const m = /^##\s+(.+?)\s*$/.exec(line);
    if (m) {
      if (current) sections.push(current);
      const heading = m[1].trim();
      const { index } = classify(heading);
      current = { heading, body: '', moduleIndex: index };
    } else if (current) {
      current.body += (current.body ? '\n' : '') + line;
    }
  }
  if (current) sections.push(current);

  // 过滤完全空段(分隔符 ---)
  return sections.filter((s) => s.heading || s.body.trim());
}

interface Props {
  content: string;
  /** 板块 A(2026-07-22)· 提供时 Module 3「AI 实测证据」改渲染结构化逐格判定 + 人工确认单元格 */
  diagnosisId?: number;
  /** 确认「疑似提到」后回调最新漏斗总分/等级(评分 SSOT 重算结果) */
  onScoreUpdated?: (score: number | null, level: string | null) => void;
}

export function ReportV2View({ content, diagnosisId, onScoreUpdated }: Props) {
  const sections = splitV2(content);

  return (
    <div className="space-y-4">
      {/* v2 顶部 Badge */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <Badge className="bg-brand text-white">报告 2.0</Badge>
          {/* [Review-CTO 2026-07-26 修 · Owner 指出] 原文案「M2 8 模块装配版 ·
              Codex 0424 方案」把内部工程代号(里程碑号、承包 AI 名、方案批次)
              暴露给客户,对读者零信息量。只留对客户有意义的模块数。 */}
          <span className="text-xs text-muted-foreground">
            共 {sections.length} 个模块
          </span>
        </div>
      </div>

      {sections.map((s, i) => {
        const { icon: Icon, label } = classify(s.heading);
        const isUnclassified = s.moduleIndex === 0;
        // B3 · Module 2 雷达图 (M2 §Epic 2)
        const radarData = s.moduleIndex === 2 ? parseRadarFromBody(s.body) : [];
        const validRadar = radarData.filter(r => r.score !== null) as { dimension: string; score: number }[];
        return (
          <Card
            key={i}
            id={`module-${s.moduleIndex || `extra-${i}`}`}
            className={`border ${isUnclassified ? 'border-border' : 'border-brand/20'} rounded-xl shadow-none`}
          >
            <CardHeader className="p-4 pb-2">
              <CardTitle className="text-base flex items-center gap-2">
                {s.moduleIndex > 0 && (
                  <span className="inline-flex items-center justify-center h-6 w-6 rounded-full bg-brand/10 text-brand text-[11px] font-semibold tabular-nums">
                    {s.moduleIndex}
                  </span>
                )}
                <Icon className="h-4 w-4 text-brand" />
                <span>{s.heading}</span>
                {!isUnclassified && (
                  <Badge variant="secondary" className="text-[10px] font-normal ml-auto">
                    模块 {s.moduleIndex} · {label}
                  </Badge>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-2 space-y-3">
              {/* Module 2 真雷达图(覆盖原 markdown 表 · 仍保留下方表格作 fallback / 数据展示) */}
              {s.moduleIndex === 2 && validRadar.length >= 3 && (
                <div className="bg-muted/20 rounded-lg p-2">
                  <ResponsiveContainer width="100%" height={280}>
                    <RadarChart data={validRadar}>
                      <PolarGrid />
                      <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11 }} />
                      <PolarRadiusAxis angle={30} domain={[0, 100]} tick={{ fontSize: 10 }} />
                      <Radar
                        name="得分"
                        dataKey="score"
                        stroke="hsl(var(--primary))"
                        fill="hsl(var(--primary))"
                        fillOpacity={0.4}
                      />
                    </RadarChart>
                  </ResponsiveContainer>
                </div>
              )}
              {/* 板块 A(2026-07-22)· 仅 Module 3「AI 实测证据」改结构化逐格判定渲染 +
                  人工确认单元格;fetch 失败/无数据时降级回原 markdown 体。
                  其他模块渲染方式不动(markdown SSOT 属板块 B 范围)。 */}
              {s.moduleIndex === 3 && diagnosisId ? (
                <BrandCellsSection
                  diagnosisId={diagnosisId}
                  onScoreUpdated={onScoreUpdated}
                  fallback={(
                    <div className="prose prose-neutral max-w-none dark:prose-invert prose-table:border-collapse prose-th:border prose-th:border-border prose-th:p-2 prose-th:bg-muted prose-td:border prose-td:border-border prose-td:p-2 overflow-x-auto text-sm">
                      <ReactMarkdown>{s.body || '_(本模块暂无内容)_'}</ReactMarkdown>
                    </div>
                  )}
                />
              ) : (
                <div className="prose prose-neutral max-w-none dark:prose-invert prose-table:border-collapse prose-th:border prose-th:border-border prose-th:p-2 prose-th:bg-muted prose-td:border prose-td:border-border prose-td:p-2 overflow-x-auto text-sm">
                  <ReactMarkdown>{s.body || '_(本模块暂无内容)_'}</ReactMarkdown>
                </div>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

export default ReportV2View;
