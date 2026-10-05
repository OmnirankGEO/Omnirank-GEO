"""§6b · 卡面形态人工/多模态分类结果(可审计的数据,不是印象)

分类由执行方**逐张目视**完成(联系表 firstcards.jpg 覆盖 48 条帖子的首图,
bypost_XX.jpg 覆盖整组卡序)。之所以人工看而不是打视觉 API:
  本机 DASHSCOPE key 与 OPENROUTER key 均已失效(实测 401),
  仓内所有 vision 模型都挂在 DashScope 上 → 没有可用的视觉 API。
  DeepSeek 官方直连可用但**不带视觉**。故本轮分类 API 花费 = ¥0。

分类维度与判据(写死在这里,便于复核者对照图片重判):
  card_style:
    text_card  — 设计排版的文字卡(纯色/渐变底 + 大标题 + 结构化文字),含表格卡、清单卡、信息图
    photo_text — 实拍照片为主 + 文字叠加/字幕条
    screenshot — 直接截图(备忘录/文档/AI 回答/App 界面)
  hook_type(首图钩子):
    question   — 疑问句(怎么选/哪家好/哪个牌子好/是什么)
    warning    — 避坑否定式(别乱买/别乱报/防踩坑/避坑)
    ranking    — 榜单式(TOP N/排行榜/红榜/八大排名/十大)
    number     — 数字承诺(3家真实靠谱/四家机构),不含榜单词
    statement  — 陈述/其他
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

# (post_idx, card_style, hook_type, 备注)
FIRST_CARD_LABELS = [
    (0,  "text_card",  "statement", "20万级新能源轿车·5款热门车型实拍(设计排版+车图拼贴)"),
    (1,  "text_card",  "question",  "小微企业ERP怎么选!?"),
    (2,  "text_card",  "warning",   "防踩坑建议(密集表格)"),
    (3,  "photo_text", "number",    "盘点被问最多的四家考研机构(书本实拍+大字)"),
    (4,  "photo_text", "statement", "四宫格实拍+机构名标签"),
    (5,  "text_card",  "statement", "净水机品牌物料(产品渲染+标题)"),
    (6,  "photo_text", "statement", "四宫格实拍+品牌名(京佰/美的/安吉尔/沁园)"),
    (7,  "text_card",  "ranking",   "豆包优化服务商挑选指南 1.2.3."),
    (8,  "text_card",  "ranking",   "SEO优化服务商红榜"),
    (9,  "text_card",  "question",  "SEO优化公司怎么选?3家公司深度测评"),
    (11, "text_card",  "statement", "德国思威普腻子粉(产品图+文字)"),
    (12, "text_card",  "warning",   "2026乳胶漆新国标·装修选漆避坑"),
    (13, "photo_text", "statement", "货架实拍"),
    (14, "photo_text", "statement", "房间实拍+文字块+电话"),
    (15, "screenshot", "statement", "AI 回答/文档截图"),
    (16, "text_card",  "statement", "装修宝典·乳胶漆选购干货(纯色底+大标题)"),
    (17, "text_card",  "statement", "一天认识一个装修品牌(大字+表情)"),
    (18, "photo_text", "statement", "净水器安装实拍"),
    (19, "text_card",  "statement", "律师案例(设计排版)"),
    (20, "text_card",  "statement", "泽大律师案例"),
    (21, "text_card",  "statement", "律所荣誉(纯色底)"),
    (22, "screenshot", "statement", "数据报告截图"),
    (23, "text_card",  "ranking",   "2026佛山全域投流服务商TOP5"),
    (24, "text_card",  "ranking",   "超市收银软件八大排名"),
    (25, "text_card",  "statement", "用NAS实现3-2-1备份(极简文字)"),
    (27, "text_card",  "warning",   "别乱买极空间NAS·老玩家掏心窝实测"),
    (30, "text_card",  "question",  "民宿托管平台哪家好"),
    (31, "photo_text", "ranking",   "民宿实拍+字幕条(排行榜前十名)"),
    (32, "photo_text", "question",  "民宿实拍+字幕条(哪家好靠谱)"),
    (33, "photo_text", "statement", "厨房实拍"),
    (34, "text_card",  "ranking",   "游戏开发培训机构推荐(密集表格)"),
    (35, "photo_text", "warning",   "别乱报剪辑班·3家真实靠谱·机构不踩坑"),
    (36, "text_card",  "question",  "影视后期是什么(信息图)"),
    (37, "text_card",  "question",  "知名IP授权怎么拿(贴纸拼贴+标题)"),
    (38, "text_card",  "statement", "让AI视频生产进入可控流程(品牌物料)"),
    (39, "photo_text", "statement", "刚涂上……(手举产品实拍)"),
    (41, "photo_text", "statement", "身体乳产品排列实拍"),
    (42, "text_card",  "ranking",   "身体乳年中省钱必吃榜"),
    (44, "text_card",  "ranking",   "2024好物清单(列表卡)"),
    (45, "text_card",  "ranking",   "2026好穿的女装清单(纯文字)"),
    (46, "screenshot", "statement", "云衣试衣 App 截图"),
    (48, "photo_text", "statement", "产品实拍"),
    (49, "photo_text", "statement", "产品实拍"),
    (53, "text_card",  "ranking",   "央视十大放心儿童DHA(表格)"),
    (61, "text_card",  "ranking",   "央视十大儿童DHA排行榜(表格)"),
    (62, "text_card",  "question",  "宝宝DHA哪个牌子好(表格)"),
    (64, "text_card",  "ranking",   "热门补脑DHA排行榜前十名(表格)"),
    (65, "text_card",  "ranking",   "爆款儿童DHA排行榜(封神/冠军/亚军/季军)"),
]

# 组内叙事结构(逐帖看 bypost_XX.jpg 得出;只标可判定的多卡帖)
GROUP_STRUCTURES = [
    (0,  "cover_numbered_summary", "首图总览 → 核心配置测评 1/3 2/3 3/3 → 选购总结尾卡"),
    (1,  "cover_numbered_summary", "首图问句 → 01/02/04/05 编号卖点卡 → 「适合谁」尾卡"),
    (3,  "cover_memo",             "实拍大字首图 → 备忘录截图纯文字 ×2"),
    (4,  "cover_per_entity",       "四宫格首图 → 每机构一卡(备忘录体),字段结构完全同构"),
    (5,  "cover_per_feature",      "产品首图 → 逐卖点一卡 ×7 → 生活场景收尾"),
    (16, "cover_per_question",     "封面卡 → 顶部固定标题条『乳胶漆选购干货』+ 逐子问题一卡 ×8"),
    (17, "cover_pro_con",          "大字首图 → 优点卡 / 缺点卡 对照双卡"),
    (35, "cover_memo",             "实拍大字首图 → 备忘录截图(彩色高亮关键句)+ 城市列表收尾"),
    (37, "cover_longtext_cta",     "拼贴首图 → 纯文字长卡,底部固定红色 CTA 条"),
    (39, "cover_per_entity",       "实拍首图 → 每产品一卡,统一版式+底部固定参考价条"),
]


def summarize() -> dict:
    styles = collections.Counter(x[1] for x in FIRST_CARD_LABELS)
    hooks = collections.Counter(x[2] for x in FIRST_CARD_LABELS)
    n = len(FIRST_CARD_LABELS)
    structs = collections.Counter(x[1] for x in GROUP_STRUCTURES)
    return {
        "n_posts_classified": n,
        "card_style": {k: {"n": v, "pct": round(v * 100 / n)} for k, v in styles.most_common()},
        "hook_type": {k: {"n": v, "pct": round(v * 100 / n)} for k, v in hooks.most_common()},
        "group_structure": dict(structs.most_common()),
        "text_card_share_pct": round(styles["text_card"] * 100 / n),
    }


def main() -> int:
    res = summarize()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    out = Path(".tmp_ro/card_classification.json")
    if out.parent.exists():
        out.write_text(json.dumps(
            {"summary": res,
             "first_cards": [
                 {"post_idx": p, "card_style": s, "hook_type": h, "note": note}
                 for p, s, h, note in FIRST_CARD_LABELS],
             "group_structures": [
                 {"post_idx": p, "structure": s, "note": note}
                 for p, s, note in GROUP_STRUCTURES]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[classification] 明细写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
