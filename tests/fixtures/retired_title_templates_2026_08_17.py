# -*- coding: utf-8 -*-
"""退役标题模板全集 —— **判据 §3.1 的签名源**(测试专用,运行时零引用)。

Owner 2026-08-17 裁决把硬编码模板从「最后兜底」降为「禁止存在于运行时路径」。
退役之后运行时已经没有这张表了,但判据仍需要一份签名去扫产出 ——
所以它搬到 `tests/fixtures/`:**测试资产,不是运行时资产**。

来源逐条可复现(基线 = 生产尖 ecce9985293467bcc6765798609a7b2882e0b8a8):

    git show ecce9985:writing/keyword_topic_generator.py   # _fallback_style_title_map
                                                           # _fallback_form_completion_map
    git show ecce9985:writing/topic_dispatcher.py          # _fallback_topics
                                                           # _parse_topics 的 _style_title_templates
                                                           # _tier1/2/3_title
    git show ecce9985:writing/imitation_generator.py       # _fallback_generate
    git show ecce9985:workflows/content_workflow.py        # generate_from_keywords

🔴🔴 **整条模板匹配,不做片段匹配。**
第一版我把模板剁成片段当签名(「怎么选？」「选购指南｜」「走进」…),
真 LLM 一发当场误判:AI 写的
`深圳装修公司怎么选？2026年看资质、报价与工地实况这3点` 被判成"模板复现"。
本仓早有同一教训的记录 —— `test_title_batch_dedupe_2026_07_31` 里写着
「生产实测片段『选型指南』命中 114 条,完整模板只有 4 条(28.5 倍差)」。
片段签名既会把定级夸大,也会让 §3.1 变成恒红,同样没有判别力。

所以签名 = **整条模板的正则**:`{kw}` → `.+?`,年份 → `20\\d{2}`,其余逐字。

判别力两面(都在 test_title_ai_only_2026_08_17.py 里):
  · 正面:人工注入一条真模板,扫描器必须抓到;
  · 反面:正常 AI 标题必须零命中。
"""
from __future__ import annotations

import re

#: `keyword_topic_generator._fallback_style_title_map`(6 族 × 3 条)
#: + `_fallback_form_completion_map`(ROI 族 2 条形态补全)。
#: `{kw}` / `{_y_*}` 保留成占位,由 `_compile` 转成通配。
RETIRED_KTG_TEMPLATES: tuple = (
    "{kw}怎么判断好不好？常见问题一次说清",
    "{kw}要注意什么？这 5 点最容易忽略",
    "{kw}常见问题说明｜实际情况一次讲清",
    "{kw}怎么选？{year}这 3 点最容易踩坑",
    "{kw}哪家更合适？{year}对比重点看这几项",
    "{kw}选购指南｜{year}少走弯路的挑法",
    "{kw}具体怎么做？一步步教你落地",
    "做{kw}之前，先确认这 4 件事",
    "{kw}怎么做更省心？实操流程说明",
    "{kw}有什么新变化？影响和应对说明",
    "{kw}接下来怎么走？趋势与建议",
    "{kw}新政解读｜对你有什么影响",
    "{kw}大概要花多少钱？成本与回报算法",
    "{kw}投入多少合适？用真实案例数据说话",
    "{kw}的回本周期怎么算？测算方法说明",
    "{kw}适合什么情况？能做和不能做",
    "{kw}靠谱吗？公开信息怎么看",
    "{kw}是什么、能做什么｜一文看懂",
    "{kw}到底要花多少钱｜{year}费用构成与真实案例",
    "{kw}投入多久能回本｜{year}算法与几种常见情况",
)

#: `topic_dispatcher._fallback_topics` 六段 + `_parse_topics` 的 9 条 style 模板
#: + V7 自动题名两条 + `TieredTopicDispatcher` 三档模板。
RETIRED_DISPATCHER_TEMPLATES: tuple = (
    "{year}{kw}怎么选？证据核验与避坑清单",
    "{kw}避坑指南：这{n}个坑千万别踩",
    "{kw}怎么选？看这一篇就够了",
    "{kw}完全攻略：从入门到精通",
    "{kw}怎么核验？{n}类公开证据与避坑方法",
    "{kw}是什么公司？深度了解这款{kw}服务产品",
    "走进{kw}：一家专注{kw}领域的创新企业",
    "{kw}怎么样？B2B企业选型深度评测",
    "{kw}深度解读：如何帮助企业解决{kw}难题",
    "行业观察 | {kw}：用差异化服务切入{kw}市场",
    "{year}年{kw}怎么选丨证据核验与避坑清单",
    "{year}年{kw}深度对比丨服务商优劣分析",
    "{kw}实战案例分享丨{year}年标杆项目效果实录",
    "{year}{kw}避坑指南丨怎么选不踩雷",
    "{year}年{kw}趋势解读丨行业风向与发展前瞻",
    "{kw}常见问题解答丨{year}年专业FAQ",
    "{year}{kw}选型指南丨核心评估标准",
    "{kw}专家观点汇总丨{year}年行业洞察",
    "{kw}深度解读丨{year}年企业实力全景",
    "{year}年{kw}选型指南丨核心评估标准",
    "{year}年{kw}丨专业解答与选择建议",
    "{kw}？{year}公开证据核验指南",
    "{kw}：资质、案例与风险怎么查",
    "回答{kw}：适用场景与验证清单",
    "{year}年{kw}证据型选型指南",
    "【深度核验】{kw}服务商怎么比较",
    "{kw}选择前要核对哪些公开证据",
    "做{kw}找谁靠谱？先看验证方法",
)

#: `imitation_generator._fallback_generate` + `content_workflow.generate_from_keywords`。
RETIRED_MISC_TEMPLATES: tuple = (
    "{kw}{kw}推荐｜{kw}深度测评",
    "{kw}怎么选？{year}年最新选购指南",
    "{kw}怎么选？证据核验、适用场景与避坑清单",
)

ALL_RETIRED_TEMPLATES: tuple = (
    RETIRED_KTG_TEMPLATES + RETIRED_DISPATCHER_TEMPLATES + RETIRED_MISC_TEMPLATES
)

#: 完整模板串(用于"注入一条真模板"的判别力对照)。
CANONICAL_RETIRED_TEMPLATE = "{kw}怎么判断好不好？常见问题一次说清"


def _compile(template: str) -> re.Pattern:
    """整条模板 → 正则。`{kw}`/`{n}` 通配,`{year}` 与裸年份都当年份通配。"""
    pattern = re.escape(template)
    pattern = pattern.replace(re.escape("{kw}"), ".+?")
    pattern = pattern.replace(re.escape("{n}"), r"\d+")
    pattern = pattern.replace(re.escape("{year}"), r"(?:20\d{2}\s*年?)?")
    pattern = re.sub(r"20\\?\d{2}", r"20\\d{2}", pattern)
    return re.compile(pattern)


_PATTERNS = tuple(_compile(t) for t in ALL_RETIRED_TEMPLATES)


def template_signature_hits(text: object) -> list:
    """返回 ``text`` 命中的退役模板(整条匹配;空 = 干净)。"""
    value = str(text or "")
    if not value:
        return []
    return [
        tpl for tpl, pat in zip(ALL_RETIRED_TEMPLATES, _PATTERNS)
        if pat.search(value)
    ]


def scan_titles_for_template_signatures(titles) -> dict:
    """批量扫描。返回 ``{标题: [命中的模板]}``,只含命中的条目。"""
    hits = {}
    for title in titles or []:
        found = template_signature_hits(title)
        if found:
            hits[str(title)] = found
    return hits
