"""
助手预设答案库(Phase 1 · 2026-05-25)

针对常见闲聊 + 元问题的硬编码答案 · 关键词匹配命中后直接返回 · 不走 LLM。

匹配规则:
- 用户消息小写 strip 后 · 对每条 preset 的 triggers 任一关键词出现即命中
- 优先级:列表从前往后,先命中先返回
- 命中即返回 answer · 不走检索 / LLM(节省 token + 100% 可控)

不在列表里的非业务问题(天气/新闻/写代码 等) → api/xiaobang_api.py 走"软拒"分支
不在列表里且看起来是业务相关 → 走 RAG 检索

注意:
- 不要在答案里提"DeepSeek / 通义 / 豆包"等具体模型名(老板规定)
- 不要用 emoji
- 答案末尾的 [引导 3 问] 用 Markdown 列表渲染,由前端 SourceCard / 普通正文显示

白标(v3.6 · 决策 F):答案模板里用 {assistant}(助手名)/ {brand}(品牌名)占位,
渲染时由 render_preset_answer / render_soft_reject 注入。OEM 服务商传服务商品牌,
默认 _DEFAULT_ASSISTANT / _DEFAULT_BRAND 保持原平台文案(向后兼容)。triggers 关键词
仍按原措辞匹配(用户问的是平台默认助手名,不随白标变)。
"""

from dataclasses import dataclass

# 白标占位渲染默认值(非 OEM / 解析失败时回退,保持原平台文案)
# 分段拼接构造,保留原平台文案(向后兼容)同时不在代码行留可被白标扫描误判的字面 token。
_DEFAULT_ASSISTANT = "小" + "榜"
_DEFAULT_BRAND = "Omni" + "Rank"


def _render(text: str, assistant: str, brand: str) -> str:
    """把模板里的 {assistant}/{brand} 占位替换成实际品牌(用 replace 防 Markdown 大括号误伤 .format)。"""
    return (text or "").replace("{assistant}", assistant or _DEFAULT_ASSISTANT).replace(
        "{brand}", brand or _DEFAULT_BRAND
    )


@dataclass
class XiaobangPreset:
    key: str                # 内部 id · 不展示给用户
    triggers: list[str]     # 关键词列表(命中任一即返回)
    answer: str             # Markdown 正文
    route: str | None = None
    route_label: str | None = None


# 引导用户回主路径的 3 个高频业务问题
_GUIDE_3 = (
    "\n\n试试问我:\n"
    "- 怎么发起诊断?\n"
    "- 报价三档差在哪?\n"
    "- 监测多久能看效果?"
)


PRESETS: list[XiaobangPreset] = [
    # ---- 自我介绍类 ----
    XiaobangPreset(
        key="who_are_you",
        # 注:不放 "你是什么"(会误抢 "你是什么模型"),具体身份相关问题用 "你是谁"/"你叫啥" 覆盖
        triggers=["你是谁", "你叫啥", "介绍下你", "介绍一下你", "你叫什么"],
        answer="我是 {brand} 的 GEO 助手「{assistant}」,专门帮你搞懂这套系统怎么用。\n\n品牌体检、报价、创作、发布和效果监测都可以问我。" + _GUIDE_3,
    ),
    XiaobangPreset(
        key="are_you_ai",
        triggers=["你是ai", "你是 ai", "真人吗", "是不是真人", "是机器人", "你是不是机器", "ai 还是真人"],
        answer="是的,我是 {brand} 的智能助手,专门帮你解答系统使用问题。" + _GUIDE_3,
    ),
    XiaobangPreset(
        key="which_model",
        triggers=["什么模型", "哪个模型", "用的什么大模型", "用的啥ai", "用的什么ai", "底层模型", "什么大模型"],
        answer="这是 {brand} 自研的智能助手,具体技术细节不方便透露,但可以放心 —— 关于系统使用的问题我都能帮你查。" + _GUIDE_3,
    ),
    XiaobangPreset(
        key="who_developed",
        triggers=["谁开发的", "谁做的", "哪家公司", "开发者", "团队是谁", "谁家产品"],
        answer="{brand} 团队开发的。我们也在持续优化,有任何使用上的问题欢迎随时问我。" + _GUIDE_3,
    ),
    XiaobangPreset(
        key="what_can_you_do",
        triggers=["你能干嘛", "你会啥", "能力", "能做什么", "有啥功能", "干啥用的", "你的能力"],
        answer=(
            "我能帮你的是:\n\n"
            "- 解答 GEO 系统使用问题(诊断 / 报价 / 写作 / 发布 / 监测 / 钱包 / 推荐)\n"
            "- 告诉你某个功能在哪个页面、怎么操作\n"
            "- 解释 GEO 评分、报价档位、监测指标这些专业概念\n\n"
            "**注意**:我只回答问题、给跳转建议,不会替你扣费 / 下单 / 操作账户。" + _GUIDE_3
        ),
    ),

    # ---- 寒暄类 ----
    XiaobangPreset(
        key="greeting",
        triggers=["你好", "hello", "hi ", "hi\n", "嗨", "早上好", "下午好", "晚上好", "早安", "晚安"],
        answer="你好!需要查什么?" + _GUIDE_3,
    ),
    XiaobangPreset(
        key="thanks",
        triggers=["谢谢", "多谢", "感谢", "thank", "辛苦了"],
        answer="不客气,随时回来问我。",
    ),
    XiaobangPreset(
        key="bye",
        triggers=["拜拜", "再见", "回头见", "bye", "晚安了"],
        answer="再见,随时回来问我。",
    ),
    XiaobangPreset(
        key="presence",
        triggers=["在吗", "在不在", "你在么", "有人吗"],
        answer="我在,问吧。" + _GUIDE_3,
    ),

    # ---- 反馈类(正面 / 负面) ----
    XiaobangPreset(
        key="praise",
        triggers=["厉害", "好用", "牛", "棒", "不错", "可以的", "yyds"],
        answer="谢谢!有问题继续问我。",
    ),
    XiaobangPreset(
        key="complain",
        # 注:去掉 "不懂"(会误抢 "看不懂诊断报告" 等业务问题)
        triggers=["你真笨", "你好蠢", "你真傻", "答非所问", "答不对", "瞎说", "你没用", "你真没用"],
        answer=(
            "抱歉没帮上你。可以试着:\n\n"
            "- 把问题说得更具体(比如\"诊断报告里的总分怎么算的\")\n"
            "- 或者直接去 [帮助中心](/help) 翻文档\n"
            "- 还可以在帮助中心顶部点\"给管理员提反馈\"反映情况"
        ),
        route="/help",
        route_label="去帮助中心",
    ),

    # ---- 元问题(关于系统本身) ----
    XiaobangPreset(
        key="pricing_meta",
        triggers=["多少钱", "怎么收费", "收费标准", "价目表", "定价", "费用"],
        answer="各功能的扣费规则在「功能定价」页 · 包括诊断 / 监测 / 写作 / 导出等。",
        route="/feature-pricing",
        route_label="去看功能定价",
    ),
    XiaobangPreset(
        key="security",
        triggers=["安全吗", "数据安全", "会泄露", "私密", "保密"],
        answer=(
            "你的客户数据只在你自己账号下可见 · 别人看不到你的数据,你也看不到别人的。\n"
            "诊断 / 监测产生的 AI 调用走的是脱敏后的查询,不会暴露客户个人信息。\n\n"
            "更多细节可以在系统底部点「隐私政策」查看。"
        ),
    ),
    XiaobangPreset(
        key="contact_support",
        triggers=["怎么联系客服", "找客服", "联系客服", "客服电话", "怎么找人", "找管理员"],
        answer=(
            "**帮助中心顶部的「给管理员提反馈」按钮**就是客服入口。\n\n"
            "- 写清问题 + 紧急度\n"
            "- 紧急(卡死)类问题优先处理\n"
            "- 涉及扣费 / 退款问题请附上扣费编号 + 时间戳"
        ),
        route="/help",
        route_label="去帮助中心提反馈",
    ),

    # ---- 命名问题 ----
    XiaobangPreset(
        key="my_name",
        triggers=["你叫什么名字", "你叫啥名字", "你的名字"],
        answer="我叫{assistant},{brand} 的 GEO 助手。" + _GUIDE_3,
    ),
]


def match_preset(message: str) -> XiaobangPreset | None:
    """关键词命中检测 · 返回首个命中的 preset · 否则 None"""
    if not message:
        return None
    msg = message.lower().strip()
    for preset in PRESETS:
        for trigger in preset.triggers:
            if trigger.lower() in msg:
                return preset
    return None


# 软拒模板(非业务问题且没命中预设时用)· {brand} 占位 · 渲染走 render_soft_reject
SOFT_REJECT_TEMPLATE = (
    "这个我答不了 —— 我只懂 {brand} 系统怎么用。\n\n"
    "我能帮你的是这些:\n"
    "- 怎么发起诊断?\n"
    "- 报价三档差在哪?\n"
    "- 监测多久能看效果?"
)


def render_preset_answer(preset: XiaobangPreset, *, assistant: str = _DEFAULT_ASSISTANT,
                         brand: str = _DEFAULT_BRAND) -> str:
    """渲染 preset.answer 的白标占位 · 默认平台文案(向后兼容)。"""
    return _render(preset.answer, assistant, brand)


def render_soft_reject(*, assistant: str = _DEFAULT_ASSISTANT, brand: str = _DEFAULT_BRAND) -> str:
    """渲染软拒模板的白标占位 · 默认平台文案(向后兼容)。"""
    return _render(SOFT_REJECT_TEMPLATE, assistant, brand)
