"""
intake_flow — 客户公开 intake 分步采访流程 (P0-7 · 2026-05-04)

核心:
  - 把"22 字段长表"拆成 8-10 步 · 每步 1-2 个字段
  - 每步配人话问句 (不是字段名)
  - 已知字段自动跳过 (compute_flow_for_brand 算 skip)
  - 社媒补充 8 字段固定追加 (永远不跳)

红线:
  - 步骤纯静态定义 + 简单 skip 判定 · LLM 失败也能完整跑完
  - 不引入新依赖 · 不动 FIELD_MAP 主权威
  - 社媒字段不进 FIELD_MAP · 走 social_fields JSONB
  - schema 与前端 IntakeFillPage 共享 (前端只渲染 backend 给的步骤)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# 8 个社媒补充字段 · 与 db.intake_db.SOCIAL_FIELD_KEYS 对齐
SOCIAL_FIELD_KEYS = (
    "persona_tone",
    "speaking_style",
    "customer_faq",
    "real_cases",
    "content_taboo",
    "target_audience",
    "product_selling_points",
    "closing_method",
)


# ---- 步骤定义 ----
# 每步包含:
#   id            : 唯一 id (前端 step_index 顺序参考)
#   title         : 步骤短标题 (头部展示)
#   question      : 人话问句 (主对话气泡)
#   helper        : 给客户的辅助说明 (灰字小字)
#   fields        : 该步要采集的字段列表 (1-3 个)
#   namespace     : 'profile' (canonical 进 FIELD_MAP) | 'social' (进 social_fields JSONB)
#   skip_if_filled: 该步若所有字段已知则跳过 (社媒新字段恒 False)
#   max_fields    : 该步最多回答字段数 (UI 安全护栏)
#
# field 子结构:
#   key     : payload key (canonical 字段对齐 FIELD_MAP form_key · social 字段用 SOCIAL_FIELD_KEYS)
#   label   : 短标签
#   type    : 'text' | 'textarea' | 'tag_list' | 'scope_radio' | 'tel'
#   placeholder
#   optional: True = 该字段允许跳过 (默认 False)

INTAKE_FLOW_STEPS: List[Dict[str, Any]] = [
    {
        "id": "intro",
        "title": "先认识下你",
        "question": "你好,先简单介绍下你的品牌或公司:叫什么名字、做哪行的?",
        "helper": "如果链接里已经写了, 直接确认就行。",
        "fields": [
            {"key": "brand_name", "label": "品牌/门店/公司名称", "type": "text",
             "placeholder": "客户在大众点评/微信里看到的名字"},
            {"key": "industry", "label": "行业", "type": "text",
             "placeholder": "如餐饮 / 装修 / SaaS / 教育"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 2,
    },
    {
        "id": "location",
        "title": "服务范围",
        "question": "你主要在哪个城市做生意?是只服务本地, 还是全国都接?",
        "helper": "我们要把内容投到对的地方。",
        "fields": [
            {"key": "cities", "label": "所在城市/服务区域", "type": "text",
             "placeholder": "如深圳南山 / 全国线上"},
            {"key": "service_scope", "label": "服务范围", "type": "scope_radio"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 2,
    },
    {
        "id": "business",
        "title": "你做的事",
        "question": "你具体做什么生意, 用一两句话说清楚就行?",
        "helper": "比如\"做新房软装的, 给中产家庭打全套配饰方案\"。",
        "fields": [
            {"key": "business", "label": "主营服务", "type": "textarea",
             "placeholder": "一句话说清你做什么"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 1,
    },
    {
        "id": "audience",
        "title": "目标客户",
        "question": "你最想吸引哪类客户?他们通常因为什么问题来找你?",
        "helper": "客户画像越具体, 我们写的内容越能打中他。",
        "fields": [
            {"key": "target_users", "label": "目标客户", "type": "textarea",
             "placeholder": "主要服务谁, 描述他们的特征"},
            {"key": "pain_points", "label": "客户最常问的 3-5 个问题", "type": "tag_list",
             "placeholder": "一行一个"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 2,
    },
    {
        "id": "value",
        "title": "你的优势",
        "question": "客户为什么选你不选别家?说 2-3 个最有说服力的点。",
        "helper": "可以是技术/团队/案例/价格/服务任何独特之处。",
        "fields": [
            {"key": "core_value", "label": "为什么选你", "type": "textarea",
             "placeholder": "核心理由"},
            {"key": "selling_points", "label": "主要卖点", "type": "textarea",
             "placeholder": "3-5 个有特色的点"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 2,
    },
    {
        "id": "cases",
        "title": "真实案例",
        "question": "举 1-2 个最近的成功案例 — 客户是什么背景, 你帮他解决了什么?",
        "helper": "案例越具体, 内容越有说服力。",
        "fields": [
            {"key": "success_cases", "label": "典型案例", "type": "textarea",
             "placeholder": "故事化描述 1-2 个最有代表性的成单"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 1,
    },
    {
        "id": "competitors",
        "title": "主要竞品",
        "question": "你日常会和哪几家同行做对比? 列 2-3 家就行。",
        "helper": "我们会避免和他们撞内容, 也帮你找差异化角度。",
        "fields": [
            {"key": "competitors", "label": "主要竞品", "type": "tag_list",
             "placeholder": "一行一个"},
        ],
        "namespace": "profile",
        "skip_if_filled": True,
        "max_fields": 1,
    },
    # ---- 社媒补充 8 字段 (永远问 · 不 skip) ----
    {
        "id": "social_persona",
        "title": "镜头前的你",
        "question": "如果让别人形容你, 是\"专业冷静\"还是\"亲切搞笑\"? 说话偏\"书面正式\"还是\"口语自然\"?",
        "helper": "这两条决定我们写脚本的语气和分寸。",
        "fields": [
            {"key": "persona_tone", "label": "人设语气", "type": "text",
             "placeholder": "如专业不严肃 / 理性冷静 / 亲切热情"},
            {"key": "speaking_style", "label": "说话风格", "type": "text",
             "placeholder": "如口语化 / 书面正式 / 带方言 / 年轻化"},
        ],
        "namespace": "social",
        "skip_if_filled": False,
        "max_fields": 2,
    },
    {
        "id": "social_target_close",
        "title": "目标客户 + 成交方式",
        "question": "你最想被谁刷到? 他们看完最后通常通过什么方式找你成交?",
        "helper": "这两条决定脚本钩子和 CTA。",
        "fields": [
            {"key": "target_audience", "label": "目标客户画像", "type": "textarea",
             "placeholder": "年龄 / 职业 / 痛点画像"},
            {"key": "closing_method", "label": "成交方式", "type": "text",
             "placeholder": "如线上预约 / 线下到店 / 电话沟通 / 直播带单"},
        ],
        "namespace": "social",
        "skip_if_filled": False,
        "max_fields": 2,
    },
    {
        "id": "social_faq_cases",
        "title": "客户最常问的事 + 真实案例",
        "question": "客户最常问你哪 3-5 件事? 有没有 1-2 个能讲的真实成交故事?",
        "helper": "这两块是脚本最常被使用的素材源。",
        "fields": [
            {"key": "customer_faq", "label": "客户常问", "type": "tag_list",
             "placeholder": "一行一条"},
            {"key": "real_cases", "label": "真实案例", "type": "textarea",
             "placeholder": "客户背景 + 解决了什么问题 + 结果"},
        ],
        "namespace": "social",
        "skip_if_filled": False,
        "max_fields": 2,
    },
    {
        "id": "social_taboo_selling",
        "title": "内容边界 + 卖点",
        "question": "有什么内容是绝对不能讲的? 你最想被记住的 3 个产品卖点是什么?",
        "helper": "禁区帮我们避免踩雷, 卖点是脚本反复强调的锚点。",
        "fields": [
            {"key": "content_taboo", "label": "内容禁区", "type": "textarea",
             "placeholder": "如不能承诺效果 / 不能比价 / 不能露脸等"},
            {"key": "product_selling_points", "label": "产品卖点", "type": "tag_list",
             "placeholder": "3 条最想被记住的"},
        ],
        "namespace": "social",
        "skip_if_filled": False,
        "max_fields": 2,
    },
    # ---- 收尾 (可选) ----
    {
        "id": "context",
        "title": "更多线索 (可选)",
        "question": "有没有官网或主阵地链接? 客单价大概多少? 有什么不能宣传的东西?",
        "helper": "这一步可以全部跳过, 但填了我们方案会更准。",
        "fields": [
            {"key": "main_link", "label": "官网/大众点评/抖音/小红书任一链接",
             "type": "text", "placeholder": "贴一个就行", "optional": True},
            {"key": "price_range", "label": "客单价 / 预算范围", "type": "text",
             "placeholder": "如 ¥3000-5000", "optional": True},
            {"key": "constraints", "label": "禁止宣传 / 不能承诺", "type": "textarea",
             "placeholder": "让我们避免踩雷", "optional": True},
        ],
        "namespace": "profile",
        "skip_if_filled": False,
        "max_fields": 3,
    },
    {
        "id": "contact",
        "title": "联系方式 (可选)",
        "question": "方便留个称呼和联系方式吗? 顾问审核完会通过这个回你。",
        "helper": "纯可选, 不填也能提交。",
        "fields": [
            {"key": "submitted_by_name", "label": "你的称呼", "type": "text",
             "placeholder": "如 王经理", "optional": True},
            {"key": "submitted_by_phone", "label": "联系方式", "type": "tel",
             "placeholder": "手机号 / 微信", "optional": True},
        ],
        "namespace": "profile",
        "skip_if_filled": False,
        "max_fields": 2,
    },
]


def _is_value_filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    return True


def _resolve_known_value(field_key: str, brand: Dict[str, Any], profile: Dict[str, Any]) -> Any:
    """从已有 brand/profile 取该字段当前值 · 用于 skip 判定 + 前端展示."""
    # canonical FIELD_MAP 字段查表 (form_key -> table.column)
    from services.intake_diff import FIELD_MAP
    spec = FIELD_MAP.get(field_key)
    if spec:
        if spec["table"] == "brands":
            return brand.get(spec["column"])
        return profile.get(spec["column"])
    # NOTES_KEYS / 社媒字段当前值无法回查 (社媒走 social_fields JSONB)
    if field_key in SOCIAL_FIELD_KEYS:
        sf = profile.get("social_fields") or {}
        if isinstance(sf, dict):
            return sf.get(field_key)
    return None


def compute_flow_for_brand(
    brand: Dict[str, Any],
    profile: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """根据 brand/profile 当前已知字段决定步骤跳过.
    返每步:
      {
        id, title, question, helper, namespace,
        fields: [{ key, label, type, placeholder, optional, prefilled, current_value }],
        all_filled: bool,
        skipped: bool,
      }
    """
    p = profile or {}
    out: List[Dict[str, Any]] = []
    for step in INTAKE_FLOW_STEPS:
        fields_view: List[Dict[str, Any]] = []
        all_required_filled = True
        any_filled = False
        for f in step["fields"]:
            current = _resolve_known_value(f["key"], brand, p)
            filled = _is_value_filled(current)
            if filled:
                any_filled = True
            elif not f.get("optional"):
                all_required_filled = False
            fields_view.append({
                "key": f["key"],
                "label": f["label"],
                "type": f["type"],
                "placeholder": f.get("placeholder", ""),
                "optional": bool(f.get("optional", False)),
                "prefilled": filled,
                "current_value": current if filled else None,
            })
        skipped = bool(step.get("skip_if_filled")) and all_required_filled and any_filled
        out.append({
            "id": step["id"],
            "title": step["title"],
            "question": step["question"],
            "helper": step.get("helper", ""),
            "namespace": step["namespace"],
            "fields": fields_view,
            "all_filled": all_required_filled,
            "skipped": skipped,
            "max_fields": step.get("max_fields", len(fields_view)),
        })
    return out


def split_payload_by_namespace(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """前端把所有答案放一个 flat dict · 后端按 namespace 切回 profile / social.
    返:
      {
        profile_payload: {form_key: value},  # 走 FIELD_MAP / NOTES_KEYS · 现有 normalize_payload 处理
        social_fields:   {social_key: value} # 走 client_profiles.social_fields JSONB
      }
    """
    profile_payload: Dict[str, Any] = {}
    social_fields: Dict[str, Any] = {}
    for k, v in (payload or {}).items():
        if k in SOCIAL_FIELD_KEYS:
            social_fields[k] = v
        else:
            profile_payload[k] = v
    return {"profile_payload": profile_payload, "social_fields": social_fields}
