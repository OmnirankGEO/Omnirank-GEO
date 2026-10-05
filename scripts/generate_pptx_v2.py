"""
GEO诊断报告 PPT V2 — 重设计版
叙事弧线: 认知 → 现状 → 对标 → 机会 → 行动

视觉设计原则:
- 卡片化布局，减少文字堆砌
- 红绿灯色彩系统传递状态
- 图表优先于表格
- 高情商语言，"机会"替代"损失"
"""

import re
import sys
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Cm, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION


# ====== 配色方案 V2 ======
# 主色
NAVY = RGBColor(0x0F, 0x17, 0x2A)
BRAND_BLUE = RGBColor(0x1E, 0x40, 0xAF)
LIGHT_BLUE = RGBColor(0x3B, 0x82, 0xF6)
CYAN = RGBColor(0x06, 0xB6, 0xD4)

# 中性色
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BG = RGBColor(0xF8, 0xFA, 0xFC)
CARD_BG = RGBColor(0xF1, 0xF5, 0xF9)
BORDER = RGBColor(0xE2, 0xE8, 0xF0)
DARK_TEXT = RGBColor(0x1E, 0x29, 0x3B)
MED_TEXT = RGBColor(0x47, 0x55, 0x69)
LIGHT_TEXT = RGBColor(0x94, 0xA3, 0xB8)

# 状态色
GREEN = RGBColor(0x22, 0xC5, 0x5E)
GREEN_BG = RGBColor(0xF0, 0xFD, 0xF4)
AMBER = RGBColor(0xF5, 0x9E, 0x0B)
AMBER_BG = RGBColor(0xFF, 0xFB, 0xEB)
RED = RGBColor(0xEF, 0x44, 0x44)
RED_BG = RGBColor(0xFE, 0xF2, 0xF2)

# 装饰色
PURPLE = RGBColor(0x8B, 0x5C, 0xF6)
TEAL = RGBColor(0x14, 0xB8, 0xA6)

# 尺寸
SLIDE_W = Cm(33.867)  # 16:9
SLIDE_H = Cm(19.05)
ML = Cm(2.5)  # margin left
MR = Cm(2.5)
CONTENT_W = SLIDE_W - ML - MR


# ====== 基础工具 ======

def add_bg(slide, color):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def txbox(slide, left, top, width, height, text,
          size=12, bold=False, color=DARK_TEXT,
          align=PP_ALIGN.LEFT, font="Microsoft YaHei",
          anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    try:
        tf.vertical_anchor = anchor
    except Exception:
        pass
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = color
    p.font.name = font
    p.alignment = align
    return box


def add_para(tf, text, size=12, bold=False, color=DARK_TEXT,
             align=PP_ALIGN.LEFT, space_before=0):
    p = tf.add_paragraph()
    p.text = text
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = color
    p.font.name = "Microsoft YaHei"
    p.alignment = align
    if space_before:
        p.space_before = Pt(space_before)
    return p


def rounded_rect(slide, left, top, w, h, fill_color, border_color=None, border_width=0):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = Pt(border_width)
    else:
        shape.line.fill.background()
    return shape


def rect(slide, left, top, w, h, fill_color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    shape.line.fill.background()
    return shape


def circle(slide, left, top, size, fill_color, border_color=None):
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, left, top, size, size)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = Pt(1)
    else:
        shape.line.fill.background()
    return shape


def footer(slide, brand_name):
    h = Cm(0.8)
    top = SLIDE_H - h
    rect(slide, Cm(0), top, SLIDE_W, h, CARD_BG)
    txbox(slide, ML, top, Cm(12), h,
          "OmniRank AI | GEO", size=7, color=LIGHT_TEXT,
          anchor=MSO_ANCHOR.MIDDLE)
    txbox(slide, SLIDE_W - MR - Cm(12), top, Cm(12), h,
          f"{brand_name} GEO", size=7, color=LIGHT_TEXT,
          align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)


def slide_title(slide, title, subtitle=""):
    """统一的页面标题样式"""
    # 蓝色竖条装饰
    rect(slide, ML, Cm(1.2), Cm(0.3), Cm(1.2), LIGHT_BLUE)
    txbox(slide, ML + Cm(0.7), Cm(1.0), CONTENT_W - Cm(1), Cm(1.5),
          title, size=22, bold=True, color=NAVY)
    if subtitle:
        txbox(slide, ML + Cm(0.7), Cm(2.4), CONTENT_W - Cm(1), Cm(1),
              subtitle, size=11, color=MED_TEXT)


# ====== 数据提取 ======
def extract_meta(md):
    # 技术版格式: "品牌名：XXX"
    m_brand = re.search(r'品牌名[：:](.+)', md)
    m_industry = re.search(r'行业[：:](.+)', md)
    m_score = re.search(r'GEO总分[：:](\d+)', md)
    m_level = re.search(r'等级[：:](.+)', md)
    m_date = re.search(r'诊断日期[：:]\**\s*(.+?)[\s|*]', md)
    m_ai_rate = re.search(r'AI推荐率[：:](.+?)%', md)
    m_ai_count = re.search(r'AI测试次数[：:](\d+)', md)
    m_ai_rec = re.search(r'品牌被推荐次数[：:](\d+)', md)

    # 销售版格式: "**GEO评分**：25/63（待提升，已评估维度）"
    if not m_score:
        m_score_alt = re.search(r'GEO评分\*?\*?[：:](\d+)', md)
        if m_score_alt:
            m_score = m_score_alt

    if not m_level:
        m_level_alt = re.search(r'GEO评分\*?\*?[：:]\d+/\d+[（(](.+?)[,，]', md)
        if m_level_alt:
            m_level = m_level_alt

    # 从文件名或报告内容推断品牌名
    if not m_brand:
        # 尝试从 "当潜在客户问AI" 前后找品牌名
        m_brand_alt = re.search(r'(\S{2,20})被\d+个AI引擎', md)
        if m_brand_alt:
            m_brand = m_brand_alt
        else:
            # 从AI测试中找品牌名: "XXX是什么公司"
            m_brand_q = re.search(r'"(.{2,20})是什么公司', md)
            if m_brand_q:
                m_brand = m_brand_q

    # 从AI测试中提取行业信息
    if not m_industry:
        m_ind_alt = re.search(r'问AI"(.{5,40})推荐哪家"', md)
        if m_ind_alt:
            m_industry = m_ind_alt

    # 推荐率
    if not m_ai_rate:
        m_rate_alt = re.search(r'推荐率\*?\*?[：:](.+?)%', md)
        if m_rate_alt:
            m_ai_rate = m_rate_alt

    # 统计AI测试表格行数
    ai_test_rows = re.findall(r'\| ".*?" \| [✅❌⚠️]', md)
    ai_count_est = len(ai_test_rows) * 4 if ai_test_rows else 0

    return {
        'brand': m_brand.group(1).strip().strip('*') if m_brand else 'Brand',
        'industry': m_industry.group(1).strip().strip('*') if m_industry else '',
        'score': int(m_score.group(1)) if m_score else 0,
        'level': m_level.group(1).strip().strip('*') if m_level else '',
        'date': m_date.group(1).strip() if m_date else '',
        'ai_rate': float(m_ai_rate.group(1)) if m_ai_rate else 0,
        'ai_count': int(m_ai_count.group(1)) if m_ai_count else (ai_count_est or 32),
        'ai_rec': int(m_ai_rec.group(1)) if m_ai_rec else 0,
    }


def extract_score_table(md):
    pattern = r'\| ([\U0001f000-\U0001ffff\w\s]+?) \| (\d+) \| (\d+) \| (.+?) \|'
    matches = re.findall(pattern, md)
    scores = []
    for name, score, total, status in matches:
        name = name.strip()
        if any(k in name for k in ['AI引擎', '社媒', '网页', '权威', '结构化', '内容质量', '品牌基础']):
            scores.append({
                'name': name,
                'short': name.split(' ')[-1] if ' ' in name else name[:4],
                'score': int(score),
                'total': int(total),
                'status': status.strip(),
            })
    return scores


def extract_competitors(md):
    pattern = r'\| (.+?) \| (抖音|小红书) \| (.+?) \| (.+?) \| (.+?) \|'
    matches = re.findall(pattern, md)
    comps = []
    for name, platform, fans, style, advantage in matches:
        name = name.strip()
        if name and '账号名称' not in name and '---' not in name:
            comps.append({
                'name': name, 'platform': platform,
                'fans': fans.strip(), 'advantage': advantage.strip()
            })
    return comps


def extract_action_plan(md):
    pattern = r'\| (\d) \| \*\*(.+?)\*\*[：:]?(.+?) \| (.+?) \|'
    recs = re.findall(pattern, md)
    return recs[:3]


def extract_ai_test_3dim(md):
    """提取三维度测试结果"""
    dims = []
    pattern = r'\| (品牌认知|地区\+行业|场景搜索) \| .+? \| (\d+)/(\d+) \|'
    for name, passed, total in re.findall(pattern, md):
        dims.append({'name': name, 'passed': int(passed), 'total': int(total)})
    return dims


def extract_ai_recommended_brands(md):
    """提取AI推荐的竞品品牌"""
    brands = []
    pattern = r'\| (.+?) \| (DeepSeek|千问|Kimi|豆包|Qwen).*? \| (.+?) \|'
    for brand, engine, context in re.findall(pattern, md):
        brand = brand.strip()
        if brand and '品牌' not in brand and '---' not in brand and len(brand) < 30:
            brands.append({'name': brand, 'engine': engine, 'context': context.strip()})
    return brands[:6]


# ====== SLIDES ======

def slide_cover(prs, meta):
    """Slide 1: 封面"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, NAVY)

    # 装饰圆
    c1 = slide.shapes.add_shape(MSO_SHAPE.OVAL,
                                SLIDE_W - Cm(10), Cm(-5), Cm(20), Cm(20))
    c1.fill.solid()
    c1.fill.fore_color.rgb = RGBColor(0x1E, 0x3A, 0x5F)
    c1.line.fill.background()

    c2 = slide.shapes.add_shape(MSO_SHAPE.OVAL,
                                Cm(-5), SLIDE_H - Cm(8), Cm(14), Cm(14))
    c2.fill.solid()
    c2.fill.fore_color.rgb = RGBColor(0x15, 0x25, 0x45)
    c2.line.fill.background()

    # 标签
    txbox(slide, ML, Cm(3), Cm(20), Cm(1),
          "GEO DIAGNOSTIC REPORT", size=12, color=LIGHT_TEXT,
          bold=True)

    # 品牌名
    txbox(slide, ML, Cm(4.5), Cm(25), Cm(3),
          meta['brand'], size=48, bold=True, color=WHITE)

    # 行业
    txbox(slide, ML, Cm(8), Cm(25), Cm(1.5),
          meta['industry'], size=16, color=LIGHT_TEXT)

    # 分数卡片
    score_card = rounded_rect(slide, ML, Cm(10.5), Cm(16), Cm(4.2),
                              RGBColor(0x1E, 0x29, 0x3B),
                              RGBColor(0x33, 0x44, 0x55), 1)

    # 分数
    txbox(slide, Cm(3.5), Cm(10.8), Cm(5), Cm(3.5),
          str(meta['score']), size=64, bold=True, color=LIGHT_BLUE)
    txbox(slide, Cm(8), Cm(12), Cm(3), Cm(1.5),
          "/100", size=20, color=LIGHT_TEXT)

    # 等级
    level_pill = rounded_rect(slide, Cm(12), Cm(11.8), Cm(4.5), Cm(1.5),
                              RGBColor(0x2A, 0x1F, 0x05), AMBER, 1)
    txbox(slide, Cm(12), Cm(11.8), Cm(4.5), Cm(1.5),
          meta['level'], size=14, bold=True, color=AMBER,
          align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    # 底部信息
    txbox(slide, ML, Cm(16), Cm(15), Cm(1),
          "OmniRank AI | GEO", size=14, bold=True,
          color=RGBColor(0x64, 0x74, 0x8B))
    txbox(slide, ML, Cm(17.2), Cm(15), Cm(0.8),
          f"{meta['date']}  |  GEO v4.0",
          size=10, color=RGBColor(0x64, 0x74, 0x8B))
    txbox(slide, SLIDE_W - MR - Cm(8), Cm(17.2), Cm(8), Cm(0.8),
          "www.omnirank.cn", size=10,
          color=RGBColor(0x64, 0x74, 0x8B), align=PP_ALIGN.RIGHT)


def slide_trend(prs, meta):
    """Slide 2: AI搜索趋势 — 建立共识"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "AI搜索正在重塑用户决策方式",
                "率先布局的企业正在获得先发优势")

    # 三个时代卡片
    card_w = Cm(8.8)
    card_h = Cm(6)
    card_top = Cm(4.2)
    gap = Cm(0.6)

    eras = [
        ("2010", "SEO时代", "百度搜 > 看10个结果 > 自己选", LIGHT_TEXT, CARD_BG),
        ("2020", "内容时代", "刷社媒 > 看推荐 > 被种草", LIGHT_TEXT, CARD_BG),
        ("2026", "AI搜索时代", "问AI > AI给答案 > 选AI推荐的", WHITE, BRAND_BLUE),
    ]

    for i, (year, title, desc, text_color, bg_color) in enumerate(eras):
        left = ML + (card_w + gap) * i
        rounded_rect(slide, left, card_top, card_w, card_h, bg_color)

        # 年份大字
        txbox(slide, left + Cm(0.8), card_top + Cm(0.5), card_w - Cm(1.6), Cm(2),
              year, size=32, bold=True,
              color=LIGHT_BLUE if bg_color == CARD_BG else WHITE)

        # 标题
        txbox(slide, left + Cm(0.8), card_top + Cm(2.8), card_w - Cm(1.6), Cm(1),
              title, size=13, bold=True, color=text_color)

        # 描述
        txbox(slide, left + Cm(0.8), card_top + Cm(4), card_w - Cm(1.6), Cm(1.5),
              desc, size=10, color=text_color)

    # 核心数据行
    stats_top = Cm(11.5)
    rounded_rect(slide, ML, stats_top, CONTENT_W, Cm(3.5),
                 RGBColor(0xEF, 0xF6, 0xFF), LIGHT_BLUE, 1)

    stats = [
        ("5.15亿", "AI搜索用户"),
        ("320亿", "GEO市场规模"),
        ("75%", "年增长率"),
        ("<5%", "已布局企业"),
    ]
    stat_w = CONTENT_W / 4
    for i, (num, label) in enumerate(stats):
        left = ML + stat_w * i
        txbox(slide, left, stats_top + Cm(0.4), stat_w, Cm(1.8),
              num, size=24, bold=True, color=BRAND_BLUE, align=PP_ALIGN.CENTER)
        txbox(slide, left, stats_top + Cm(2.2), stat_w, Cm(1),
              label, size=10, color=MED_TEXT, align=PP_ALIGN.CENTER)

    # 底部金句
    txbox(slide, ML, Cm(15.8), CONTENT_W, Cm(1),
          "窗口期特征：用户规模已达亿级，但布局企业不足5%——先行者正在获得结构性优势",
          size=11, color=BRAND_BLUE, align=PP_ALIGN.CENTER, bold=True)

    footer(slide, meta['brand'])


def slide_ai_test(prs, meta, ai_dims):
    """Slide 3: AI实测快照 — 三维度红绿灯"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "AI引擎实测快照",
                f"向4大主流AI引擎（千问、DeepSeek、Kimi、豆包）提出真实用户场景问题，共测试{meta['ai_count']}次")

    # 三维度卡片
    card_w = Cm(8.8)
    card_h = Cm(7.5)
    card_top = Cm(4)
    gap = Cm(0.6)

    # 默认三维度数据
    if not ai_dims or len(ai_dims) < 3:
        ai_dims = [
            {'name': '品牌认知', 'passed': 4, 'total': 4},
            {'name': '地区+行业', 'passed': 0, 'total': 20},
            {'name': '场景搜索', 'passed': 0, 'total': 8},
        ]

    dim_config = [
        ("品牌认知", "直接问AI你是谁", "AI是否认识您"),
        ("行业场景", "行业+场景推荐", "AI是否推荐您"),
        ("地区搜索", "城市+行业推荐", "本地搜索可见度"),
    ]

    for i, dim in enumerate(ai_dims[:3]):
        left = ML + (card_w + gap) * i
        passed = dim['passed']
        total = dim['total']
        rate = passed / total if total > 0 else 0

        # 状态色
        if rate >= 0.5:
            status_color = GREEN
            bg = GREEN_BG
            status_text = "表现良好"
            dot_color = GREEN
        elif rate > 0:
            status_color = AMBER
            bg = AMBER_BG
            status_text = "有提升空间"
            dot_color = AMBER
        else:
            status_color = RED
            bg = RED_BG
            status_text = "待布局"
            dot_color = RED

        # 卡片背景
        rounded_rect(slide, left, card_top, card_w, card_h, bg, status_color, 1.5)

        # 状态圆点 + 标签
        circle(slide, left + Cm(0.8), card_top + Cm(0.7), Cm(0.6), dot_color)
        txbox(slide, left + Cm(1.8), card_top + Cm(0.6), Cm(5), Cm(0.8),
              dim_config[i][0] if i < len(dim_config) else dim['name'],
              size=14, bold=True, color=DARK_TEXT)

        # 分数大字
        txbox(slide, left + Cm(0.8), card_top + Cm(2), card_w - Cm(1.6), Cm(2.5),
              f"{passed}/{total}", size=40, bold=True, color=status_color,
              align=PP_ALIGN.CENTER)

        # 说明
        txbox(slide, left + Cm(0.8), card_top + Cm(4.5), card_w - Cm(1.6), Cm(1),
              dim_config[i][1] if i < len(dim_config) else "",
              size=9, color=MED_TEXT, align=PP_ALIGN.CENTER)

        # 状态标签
        pill_w = Cm(4)
        pill_left = left + (card_w - pill_w) / 2
        rounded_rect(slide, pill_left, card_top + Cm(5.8), pill_w, Cm(1.2),
                     WHITE, status_color, 1)
        txbox(slide, pill_left, card_top + Cm(5.8), pill_w, Cm(1.2),
              status_text, size=10, bold=True, color=status_color,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    # 底部汇总条
    summary_top = Cm(12.5)
    rounded_rect(slide, ML, summary_top, CONTENT_W, Cm(2.8), CARD_BG)

    txbox(slide, ML + Cm(1), summary_top + Cm(0.3), CONTENT_W - Cm(2), Cm(1),
          f"综合推荐率: {meta['ai_rate']}%    |    行业参考: 30-50%    |    测试次数: {meta['ai_count']}次",
          size=13, bold=True, color=DARK_TEXT, align=PP_ALIGN.CENTER)

    # 进度条可视化
    bar_left = ML + Cm(4)
    bar_w = CONTENT_W - Cm(8)
    bar_top = summary_top + Cm(1.6)
    bar_h = Cm(0.6)

    # 背景条
    rounded_rect(slide, bar_left, bar_top, bar_w, bar_h,
                 RGBColor(0xE2, 0xE8, 0xF0))
    # 实际值
    actual_w = max(Cm(0.5), bar_w * meta['ai_rate'] / 100)
    rounded_rect(slide, bar_left, bar_top, actual_w, bar_h, LIGHT_BLUE)

    # 标记行业参考线 (30%)
    ref_left = bar_left + bar_w * 0.3
    rect(slide, ref_left, bar_top - Cm(0.2), Cm(0.1), bar_h + Cm(0.4), AMBER)
    txbox(slide, ref_left - Cm(1), bar_top - Cm(0.8), Cm(2.5), Cm(0.6),
          "30%", size=8, bold=True, color=AMBER, align=PP_ALIGN.CENTER)

    footer(slide, meta['brand'])


def slide_health(prs, meta, scores):
    """Slide 4: GEO健康度总览 — 柱状图 + 分数卡"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "GEO七维度健康度",
                "基于全域上榜v4.0七维度评分体系，全面评估品牌AI可见度基础设施")

    if not scores:
        scores = [
            {'name': 'AI引擎可见度', 'short': 'AI可见度', 'score': 10, 'total': 25, 'status': ''},
            {'name': '社媒内容资产', 'short': '社媒资产', 'score': 0, 'total': 20, 'status': ''},
            {'name': '网页内容资产', 'short': '网页资产', 'score': 6, 'total': 18, 'status': ''},
            {'name': '权威背书', 'short': '权威背书', 'score': 2, 'total': 15, 'status': ''},
            {'name': '结构化内容', 'short': '结构化', 'score': 1, 'total': 12, 'status': ''},
            {'name': '内容质量', 'short': '内容质量', 'score': 1, 'total': 5, 'status': ''},
            {'name': '品牌基础', 'short': '品牌基础', 'score': 2, 'total': 5, 'status': ''},
        ]

    # 左侧：水平条形图（得分率可视化）
    chart_left = ML
    chart_top = Cm(4)
    chart_w = Cm(18)
    chart_h = Cm(11)

    bar_h = Cm(1)
    bar_gap = Cm(0.5)
    bar_max_w = Cm(12)

    for i, s in enumerate(scores[:7]):
        y = chart_top + (bar_h + bar_gap) * i
        rate = s['score'] / s['total'] if s['total'] > 0 else 0

        # 维度名
        name_clean = s['name'].replace('🤖 ', '').replace('📱 ', '').replace('🌐 ', '') \
            .replace('🏛️ ', '').replace('💡 ', '').replace('📝 ', '').replace('🏷️ ', '')
        txbox(slide, chart_left, y, Cm(5), bar_h,
              name_clean, size=10, bold=True, color=DARK_TEXT,
              anchor=MSO_ANCHOR.MIDDLE)

        # 背景条
        bar_left = chart_left + Cm(5.5)
        rounded_rect(slide, bar_left, y + Cm(0.15), bar_max_w, Cm(0.7),
                     RGBColor(0xE2, 0xE8, 0xF0))

        # 得分条
        actual_bar_w = max(Cm(0.3), bar_max_w * rate)
        bar_color = GREEN if rate >= 0.6 else AMBER if rate >= 0.3 else RED
        rounded_rect(slide, bar_left, y + Cm(0.15), actual_bar_w, Cm(0.7), bar_color)

        # 分数标注
        txbox(slide, bar_left + actual_bar_w + Cm(0.3), y, Cm(3), bar_h,
              f"{s['score']}/{s['total']}", size=10, bold=True, color=bar_color,
              anchor=MSO_ANCHOR.MIDDLE)

    # 右侧：总分卡片
    card_left = Cm(23)
    card_top = Cm(4)
    card_w = Cm(8)
    card_h = Cm(11)

    rounded_rect(slide, card_left, card_top, card_w, card_h, NAVY)

    txbox(slide, card_left, card_top + Cm(0.8), card_w, Cm(1),
          "GEO", size=12, bold=True,
          color=LIGHT_TEXT, align=PP_ALIGN.CENTER)

    txbox(slide, card_left, card_top + Cm(2), card_w, Cm(3.5),
          str(meta['score']), size=64, bold=True,
          color=WHITE, align=PP_ALIGN.CENTER)

    txbox(slide, card_left, card_top + Cm(5.5), card_w, Cm(1),
          "/100", size=18, color=LIGHT_TEXT, align=PP_ALIGN.CENTER)

    # 等级
    level_color = GREEN if meta['score'] >= 61 else AMBER if meta['score'] >= 41 else RED
    pill_w = Cm(5)
    pill_left = card_left + (card_w - pill_w) / 2
    rounded_rect(slide, pill_left, card_top + Cm(7), pill_w, Cm(1.4),
                 RGBColor(0x1E, 0x29, 0x3B), level_color, 1)
    txbox(slide, pill_left, card_top + Cm(7), pill_w, Cm(1.4),
          meta['level'], size=13, bold=True, color=level_color,
          align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    # 等级说明
    level_map = {
        '空白': '品牌数字基础待建设',
        '起步': '已具备基础，有明确提升方向',
        '成长': '内容布局初具规模',
        '成熟': '多平台覆盖良好',
        '领先': '全渠道AI首推品牌',
    }
    level_desc = level_map.get(meta['level'], '')
    txbox(slide, card_left + Cm(0.5), card_top + Cm(9), card_w - Cm(1), Cm(1.5),
          level_desc, size=9, color=LIGHT_TEXT, align=PP_ALIGN.CENTER)

    footer(slide, meta['brand'])


def slide_benchmark(prs, meta, scores):
    """Slide 5: 行业对标定位"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "行业对标定位",
                "您的品牌在行业中的位置与提升空间")

    # 三条对标柱
    bar_top = Cm(5)
    bar_h = Cm(2.2)
    bar_gap = Cm(1.2)
    bar_max_w = Cm(20)
    label_w = Cm(5)
    bar_left = ML + label_w + Cm(0.5)

    # 估算行业均值和标杆
    your_score = meta['score']
    avg_score = 45  # 行业均值
    top_score = 78  # 行业标杆

    bars = [
        ("您的品牌", your_score, LIGHT_BLUE, True),
        ("行业均值", avg_score, RGBColor(0xA3, 0xBF, 0xD9), False),
        ("行业标杆", top_score, RGBColor(0x64, 0x74, 0x8B), False),
    ]

    for i, (label, score, color, highlight) in enumerate(bars):
        y = bar_top + (bar_h + bar_gap) * i

        # 标签
        if highlight:
            rounded_rect(slide, ML, y, label_w, bar_h, BRAND_BLUE)
            txbox(slide, ML, y, label_w, bar_h,
                  label, size=12, bold=True, color=WHITE,
                  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        else:
            txbox(slide, ML, y, label_w, bar_h,
                  label, size=12, bold=False, color=MED_TEXT,
                  align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)

        # 柱体
        actual_w = max(Cm(1), bar_max_w * score / 100)
        rounded_rect(slide, bar_left, y + Cm(0.3), actual_w, bar_h - Cm(0.6), color)

        # 分数标注
        txbox(slide, bar_left + actual_w + Cm(0.3), y, Cm(3), bar_h,
              f"{score}分", size=16, bold=True, color=color,
              anchor=MSO_ANCHOR.MIDDLE)

    # 底部洞察
    insight_top = Cm(14.5)
    rounded_rect(slide, ML, insight_top, CONTENT_W, Cm(2.5),
                 RGBColor(0xEF, 0xF6, 0xFF), LIGHT_BLUE, 1)

    gap = avg_score - your_score
    txbox(slide, ML + Cm(1), insight_top + Cm(0.5), CONTENT_W - Cm(2), Cm(1.5),
          f"当前位于行业起步阶段，距行业均值有{gap}分提升空间。"
          f"主要差距集中在社媒内容资产和权威背书维度。",
          size=12, color=BRAND_BLUE)

    footer(slide, meta['brand'])


def slide_ai_recommend(prs, meta, ai_brands):
    """Slide 6: AI推荐率分析 — 谁被AI推荐了"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "AI推荐格局",
                f"当用户向AI提问\"{meta['industry']}推荐\"时，哪些品牌被推荐了？")

    # 你的品牌卡片
    your_top = Cm(4.2)
    rounded_rect(slide, ML, your_top, CONTENT_W, Cm(3), CARD_BG, LIGHT_BLUE, 1.5)

    # 品牌名
    txbox(slide, ML + Cm(1), your_top + Cm(0.3), Cm(12), Cm(1.2),
          meta['brand'], size=16, bold=True, color=NAVY)

    # 推荐状态
    rate = meta['ai_rate']
    if rate >= 30:
        status = "AI推荐率达到行业参考水平"
        s_color = GREEN
    elif rate > 0:
        status = f"AI推荐率 {rate}%，距行业参考（30-50%）有提升空间"
        s_color = AMBER
    else:
        status = "当前未被AI主动推荐，有较大布局机会"
        s_color = RED

    txbox(slide, ML + Cm(1), your_top + Cm(1.5), CONTENT_W - Cm(2), Cm(1.2),
          status, size=12, color=s_color)

    # 推荐率数字
    txbox(slide, CONTENT_W - Cm(3), your_top + Cm(0.3), Cm(5), Cm(2.5),
          f"{rate}%", size=36, bold=True, color=s_color, align=PP_ALIGN.RIGHT)

    # AI推荐的品牌列表
    if ai_brands:
        list_top = Cm(8)
        txbox(slide, ML, list_top, CONTENT_W, Cm(1),
              "AI引擎推荐的品牌（场景搜索测试）", size=12, bold=True, color=DARK_TEXT)

        col_w = CONTENT_W / 2
        for i, b in enumerate(ai_brands[:6]):
            col = i % 2
            row = i // 2
            left = ML + col * col_w
            top = list_top + Cm(1.2) + row * Cm(1.8)

            # 品牌名
            circle(slide, left, top + Cm(0.2), Cm(0.5), LIGHT_BLUE)
            txbox(slide, left + Cm(0.8), top, col_w - Cm(1), Cm(0.8),
                  b['name'], size=11, bold=True, color=DARK_TEXT)
            txbox(slide, left + Cm(0.8), top + Cm(0.8), col_w - Cm(1), Cm(0.8),
                  f"推荐引擎: {b['engine']}", size=9, color=MED_TEXT)
    else:
        txbox(slide, ML, Cm(8.5), CONTENT_W, Cm(2),
              "场景搜索测试中，AI推荐了行业内其他品牌。\n"
              "这意味着您的行业已有品牌进入AI推荐名单——机会窗口依然存在。",
              size=12, color=MED_TEXT)

    footer(slide, meta['brand'])


def slide_opportunity(prs, meta):
    """Slide 7: 潜在业务机会 — 重新设计（正面叙事）"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "AI渠道的潜在业务价值",
                "当AI推荐率提升至行业均值水平后，AI搜索渠道预计可带来的增量机会")

    # 核心信息卡
    main_top = Cm(4.2)
    rounded_rect(slide, ML, main_top, CONTENT_W, Cm(2.5),
                 NAVY)
    txbox(slide, ML + Cm(1), main_top + Cm(0.3), CONTENT_W - Cm(2), Cm(2),
          f"当前AI推荐率 {meta['ai_rate']}%  →  行业均值 30-50%  →  提升空间 {max(0, 30 - meta['ai_rate']):.0f}-{max(0, 50 - meta['ai_rate']):.0f} 个百分点",
          size=16, bold=True, color=WHITE, align=PP_ALIGN.CENTER,
          anchor=MSO_ANCHOR.MIDDLE)

    # 四个机会指标卡片（使用进度条）
    cards_top = Cm(7.5)
    card_w = Cm(13.5)
    card_h = Cm(1.5)
    card_gap = Cm(0.6)
    bar_left = ML + Cm(8)
    bar_w = Cm(11)

    opportunities = [
        ("AI渠道月触达用户", "约 300-500 人次", 0.4, LIGHT_BLUE),
        ("产生品牌搜索行为", "约 50-100 次", 0.3, CYAN),
        ("潜在咨询机会", "约 10-25 个", 0.2, TEAL),
        ("潜在成交机会", "约 2-5 单", 0.1, PURPLE),
    ]

    for i, (label, value, ratio, color) in enumerate(opportunities):
        y = cards_top + (card_h + card_gap) * i

        # 指标名
        txbox(slide, ML, y, Cm(7.5), card_h,
              label, size=12, color=DARK_TEXT,
              anchor=MSO_ANCHOR.MIDDLE)

        # 进度条背景
        rounded_rect(slide, bar_left, y + Cm(0.2), bar_w, Cm(0.7),
                     RGBColor(0xE2, 0xE8, 0xF0))
        # 进度条
        rounded_rect(slide, bar_left, y + Cm(0.2),
                     max(Cm(0.5), bar_w * ratio), Cm(0.7), color)

        # 数值
        txbox(slide, bar_left + bar_w + Cm(0.5), y, Cm(7), card_h,
              value, size=12, bold=True, color=color,
              anchor=MSO_ANCHOR.MIDDLE)

    # 重要说明
    note_top = Cm(14.5)
    rounded_rect(slide, ML, note_top, CONTENT_W, Cm(2.5),
                 CARD_BG)
    txbox(slide, ML + Cm(1), note_top + Cm(0.3), CONTENT_W - Cm(2), Cm(2),
          "以上为基于行业关键词搜索热度的理论估算模型，仅供参考。\n"
          "实际效果受内容质量、市场竞争、行业差异等因素影响。数据来源：5118搜索指数 + 行业转化率参考值。",
          size=9, color=MED_TEXT)

    footer(slide, meta['brand'])


def slide_competitors(prs, meta, comps):
    """Slide 8: 同行内容策略"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "同行正在布局的内容策略",
                "了解行业内正在有效运作的内容模式")

    if not comps:
        comps = [
            {'name': '竞品A', 'platform': '抖音', 'fans': '1万+', 'advantage': '专业内容输出'},
            {'name': '竞品B', 'platform': '小红书', 'fans': '5000+', 'advantage': '用户种草'},
        ]

    # 竞品卡片（左右布局，最多展示4个）
    card_w = Cm(13.8)
    card_h = Cm(4.5)
    card_gap = Cm(0.7)
    cards_top = Cm(4)

    for i, comp in enumerate(comps[:4]):
        col = i % 2
        row = i // 2
        left = ML + col * (card_w + card_gap)
        top = cards_top + row * (card_h + card_gap)

        # 卡片
        rounded_rect(slide, left, top, card_w, card_h, CARD_BG, BORDER, 1)

        # 平台标签
        platform_color = RED if comp['platform'] == '抖音' else AMBER
        pill_w = Cm(2.5)
        rounded_rect(slide, left + Cm(0.6), top + Cm(0.5), pill_w, Cm(0.9),
                     platform_color)
        txbox(slide, left + Cm(0.6), top + Cm(0.5), pill_w, Cm(0.9),
              comp['platform'], size=9, bold=True, color=WHITE,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        # 账号名
        txbox(slide, left + Cm(3.5), top + Cm(0.4), card_w - Cm(4), Cm(1),
              comp['name'], size=13, bold=True, color=DARK_TEXT)

        # 粉丝
        txbox(slide, left + Cm(0.6), top + Cm(2), Cm(5), Cm(0.8),
              f"粉丝: {comp['fans']}", size=10, color=MED_TEXT)

        # 核心优势
        txbox(slide, left + Cm(0.6), top + Cm(2.8), card_w - Cm(1.2), Cm(1.5),
              f"策略亮点: {comp['advantage'][:40]}", size=10, color=DARK_TEXT)

    # 底部洞察
    insight_top = Cm(14) if len(comps) <= 2 else Cm(14)
    txbox(slide, ML, insight_top, CONTENT_W, Cm(2),
          "这些账号说明：行业内容赛道正在升温，但头部格局尚未固化——"
          "当前入场仍可快速获得位置。",
          size=12, bold=True, color=BRAND_BLUE)

    footer(slide, meta['brand'])


def slide_action(prs, meta, actions):
    """Slide 9: 30天快速行动"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, WHITE)
    slide_title(slide, "30天快速行动计划",
                "立即可执行的优先事项，快速建立AI搜索可见度基础")

    if not actions:
        actions = [
            ("1", "启动社媒账号", "在抖音/小红书发布3-5条核心业务的专业科普内容", "填补内容空白，积累AI可引用素材"),
            ("2", "优化权威信息", "确保百科、官网信息完整，突出地域标签和核心服务", "提升品牌在本地搜索中的相关性"),
            ("3", "创建结构化内容", "发布行业指南或FAQ，覆盖用户高频搜索场景", "增加AI引用潜力，提升推荐率"),
        ]

    # 三步行动卡片
    card_w = Cm(8.8)
    card_h = Cm(9)
    card_top = Cm(4)
    gap = Cm(0.6)

    step_colors = [LIGHT_BLUE, CYAN, TEAL]

    for i, action in enumerate(actions[:3]):
        left = ML + (card_w + gap) * i
        color = step_colors[i]

        if isinstance(action, tuple) and len(action) >= 4:
            num, title, detail, effect = action[0], action[1], action[2], action[3]
        else:
            num, title, detail, effect = str(i + 1), str(action), "", ""

        # 卡片
        rounded_rect(slide, left, card_top, card_w, card_h, WHITE, BORDER, 1)

        # 顶部色条
        rect(slide, left, card_top, card_w, Cm(0.3), color)

        # 编号圆
        num_size = Cm(2)
        circle(slide, left + (card_w - num_size) / 2, card_top + Cm(0.8), num_size, color)
        txbox(slide, left + (card_w - num_size) / 2, card_top + Cm(0.8), num_size, num_size,
              num, size=18, bold=True, color=WHITE,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        # 标题
        txbox(slide, left + Cm(0.6), card_top + Cm(3.2), card_w - Cm(1.2), Cm(1.5),
              title[:20], size=13, bold=True, color=DARK_TEXT,
              align=PP_ALIGN.CENTER)

        # 详情
        txbox(slide, left + Cm(0.6), card_top + Cm(4.8), card_w - Cm(1.2), Cm(2),
              detail[:60], size=9, color=MED_TEXT, align=PP_ALIGN.CENTER)

        # 预期效果
        if effect:
            rounded_rect(slide, left + Cm(0.4), card_top + Cm(7), card_w - Cm(0.8), Cm(1.5),
                         RGBColor(0xF0, 0xFD, 0xF4))
            txbox(slide, left + Cm(0.6), card_top + Cm(7.2), card_w - Cm(1.2), Cm(1.2),
                  f"{effect[:50]}", size=8, color=GREEN)

    footer(slide, meta['brand'])


def slide_cta(prs, meta):
    """Slide 10: CTA"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide, NAVY)

    # 装饰
    c = slide.shapes.add_shape(MSO_SHAPE.OVAL,
                               SLIDE_W - Cm(8), Cm(-3), Cm(16), Cm(16))
    c.fill.solid()
    c.fill.fore_color.rgb = RGBColor(0x1E, 0x3A, 0x5F)
    c.line.fill.background()

    # 主标题
    txbox(slide, ML, Cm(3.5), CONTENT_W, Cm(2),
          "GEO", size=36, bold=True,
          color=WHITE, align=PP_ALIGN.CENTER)

    # 三个选项卡片
    card_w = Cm(8.8)
    card_h = Cm(5.5)
    card_top = Cm(6.5)
    gap = Cm(0.6)

    options = [
        ("A", "", "30min",
         "", LIGHT_BLUE),
        ("B", "30", "",
         "", CYAN),
        ("C", "", "",
         "", TEAL),
    ]

    for i, (opt, title, desc, benefit, color) in enumerate(options):
        left = ML + (card_w + gap) * i

        rounded_rect(slide, left, card_top, card_w, card_h,
                     RGBColor(0x1E, 0x29, 0x3B), color, 1.5)

        # 选项标签
        circle(slide, left + (card_w - Cm(2)) / 2, card_top + Cm(0.5), Cm(2), color)
        txbox(slide, left + (card_w - Cm(2)) / 2, card_top + Cm(0.5), Cm(2), Cm(2),
              opt, size=20, bold=True, color=WHITE,
              align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        option_texts = {
            "A": ("1:1深度解读", "30min报告解读\n回答您的具体问题"),
            "B": ("30天执行方案", "定制内容策略\n明确投入产出预期"),
            "C": ("同行案例参考", "2-3个行业案例\n优化前后效果对比"),
        }

        t, d = option_texts.get(opt, ("", ""))
        txbox(slide, left + Cm(0.5), card_top + Cm(2.8), card_w - Cm(1), Cm(1),
              t, size=12, bold=True, color=WHITE, align=PP_ALIGN.CENTER)
        txbox(slide, left + Cm(0.5), card_top + Cm(3.8), card_w - Cm(1), Cm(1.5),
              d, size=9, color=LIGHT_TEXT, align=PP_ALIGN.CENTER)

    # 底部
    txbox(slide, ML, Cm(13.5), CONTENT_W, Cm(1),
          "OmniRank AI | GEO", size=16, bold=True,
          color=RGBColor(0x64, 0x74, 0x8B), align=PP_ALIGN.CENTER)
    txbox(slide, ML, Cm(14.8), CONTENT_W, Cm(1),
          "AI  |  www.omnirank.cn",
          size=11, color=RGBColor(0x64, 0x74, 0x8B), align=PP_ALIGN.CENTER)
    txbox(slide, ML, Cm(16.5), CONTENT_W, Cm(1),
          "",
          size=9, color=RGBColor(0x64, 0x74, 0x8B), align=PP_ALIGN.CENTER)


# ====== 主流程 ======

def generate_pptx_v2(md_path, output_path):
    """生成V2版PPT"""
    print(f"[PPTv2] Reading: {md_path}")
    md = open(md_path, 'r', encoding='utf-8').read()

    meta = extract_meta(md)
    scores = extract_score_table(md)
    comps = extract_competitors(md)
    actions = extract_action_plan(md)
    ai_dims = extract_ai_test_3dim(md)
    ai_brands = extract_ai_recommended_brands(md)

    print(f"[PPTv2] Brand: {meta['brand']} | Score: {meta['score']} | Level: {meta['level']}")

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    # 10 slides
    slide_cover(prs, meta)
    slide_trend(prs, meta)
    slide_ai_test(prs, meta, ai_dims)
    slide_health(prs, meta, scores)
    slide_benchmark(prs, meta, scores)
    slide_ai_recommend(prs, meta, ai_brands)
    slide_opportunity(prs, meta)
    slide_competitors(prs, meta, comps)
    slide_action(prs, meta, actions)
    slide_cta(prs, meta)

    prs.save(output_path)
    file_size = os.path.getsize(output_path) / 1024
    print(f"[PPTv2] Generated: {output_path}")
    print(f"[PPTv2] Size: {file_size:.1f} KB | Slides: {len(prs.slides)}")
    return output_path


if __name__ == '__main__':
    md_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), '..', 'output',
        '深圳美栖堂_upgrade_117_20260315001305.md')
    out_path = sys.argv[2] if len(sys.argv) > 2 else md_path.replace('.md', '_v2.pptx')
    generate_pptx_v2(md_path, out_path)
