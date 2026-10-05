"""小榜唯一操作注册表(v3)。

路径、按钮名和可导航动作只能从本模块签发。FAQ、帮助文档和模型可以解释，
但不能覆盖这里的 route_template、help_target 或权限合同。

## v2 → v3 增量(规格 §8,不建第二份能力清单)

* 每个 operation 可选挂一份 :class:`~services.xiaobang_command_contract.CommandContract`
  —— 描述它作为**可执行命令**时的 side_effect、确认档、幂等与 adapter。
  没挂的条目仍然只是导航项,行为一个字不变。
* 注册期对**每一条**条目跑社媒域负向枚举锁(Owner 2026-08-17「员工席位永久剔除
  社媒」)。锁在 ``OperationRegistry.__init__`` 里,不是在某个新增函数里 ——
  绕过它的唯一办法是不用这个注册表,而不用它就没有 route 可签。
* 版本协商:``OPERATION_MAP_VERSION`` 升到 v3,同时给出
  :data:`OPERATION_REGISTRY_COMPATIBLE_VERSIONS`。前端不再硬编码整版相等
  (债务 §4.1-7:后端一升版,动作会被整体过滤掉)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urlsplit

from services.xiaobang_command_contract import (
    ALL_PHASES,
    CommandContract,
    assert_no_social_domain,
    compute_only_confirmation_policy,
    external_confirmation_policy,
    standard_idempotency,
    SIDE_EFFECT_COMPUTE_ONLY,
    SIDE_EFFECT_EXTERNAL,
)

OPERATION_MAP_VERSION = "operation-registry-v3"
OPERATION_REGISTRY_VERSION = OPERATION_MAP_VERSION

#: 前端按这张表做协议协商。旧值留着不删(与部署脚本 sha256 同规矩:
#: 单值判据在下一次合法升版时必然变红,而长期红的判据等于没有判据)。
OPERATION_REGISTRY_COMPATIBLE_VERSIONS: tuple[str, ...] = (
    "operation-registry-v2",
    "operation-registry-v3",
)

#: 导航动作卡的**线上形状**版本。v2→v3 注册表升级没有改动作卡字段,
#: 所以这里不动 —— 前端要判的是"这张卡我认不认得",不是"注册表构建到第几版"。
NAV_ACTION_SCHEMA_VERSION = "1"

ROLE_ADMIN = "admin"
ROLE_AGENT = "agent"
ROLE_NORMAL = "normal_user"
ROLE_MEMBER = "member"
ALL_OPERATOR_ROLES = (ROLE_ADMIN, ROLE_AGENT, ROLE_NORMAL, ROLE_MEMBER)
DEFAULT_OPERATOR_ROLES = (ROLE_ADMIN, ROLE_AGENT, ROLE_NORMAL)
MEMBER_OPERATOR_ROLES = (*DEFAULT_OPERATOR_ROLES, ROLE_MEMBER)

_APP_TSX = Path(__file__).resolve().parent.parent / "frontend" / "src" / "App.tsx"
_ROUTE_RE = re.compile(r'<Route\s+path="([^"]+)"')
_PLACEHOLDER_NAMES = {
    "brand_id", "quote_id", "article_id", "plan_item_id",
    "publication_id", "monitoring_task_id",
}


def help_target_for_route(route: str) -> str:
    """Route → DOM target 的稳定规则；前端用同一算法绑定现役页面 ``main``。"""
    path = urlsplit(str(route or "")).path.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", path).strip("-") or "root"
    return f"route-{slug}"


@dataclass(frozen=True)
class OperationEntry:
    operation_id: str
    display_name: str
    synonyms: tuple[str, ...]
    allowed_roles: tuple[str, ...]
    permissions: tuple[str, ...]
    route_template: str
    help_target: str
    prerequisites: tuple[str, ...]
    action_type: str
    version: str = OPERATION_REGISTRY_VERSION
    breadcrumb: tuple[str, ...] = ()
    required_module: str | None = None
    actions: tuple[str, ...] = ("go_to_signed_route", "highlight_signed_target")
    until_version: str | None = None
    notes: str = ""
    #: v3 增量。``None`` = 这条只是导航项,没有可执行命令合同。
    command_contract: CommandContract | None = None

    @property
    def route(self) -> str:
        """v1 薄适配：无占位 route_template 与旧 ``entry.route`` 等价。"""
        return self.route_template

    @property
    def since_version(self) -> str:
        return self.version


def _nav(
    operation_id: str,
    display_name: str,
    route: str,
    *,
    synonyms: tuple[str, ...] = (),
    roles: tuple[str, ...] = DEFAULT_OPERATOR_ROLES,
    module: str | None = None,
    prerequisites: tuple[str, ...] = ("authenticated",),
    action_type: str = "navigation",
    breadcrumb: tuple[str, ...] | None = None,
    help_target: str | None = None,
    notes: str = "",
    contract: CommandContract | None = None,
) -> OperationEntry:
    return OperationEntry(
        operation_id=operation_id,
        display_name=display_name,
        synonyms=tuple(dict.fromkeys((display_name, f"{display_name}在哪里", *synonyms))),
        allowed_roles=roles,
        permissions=((f"{module}:*",) if module else ("authenticated",)),
        route_template=route,
        help_target=help_target or help_target_for_route(route),
        prerequisites=prerequisites,
        action_type=action_type,
        breadcrumb=breadcrumb or (display_name,),
        required_module=module,
        notes=notes,
        command_contract=contract,
    )


# ── v3 命令合同(规格 §8.1)────────────────────────────────────────────────
# WP1 只登记**两条**,刻意不铺满:
#   · 一条 external(发布域,P0-A④ 指定的确认门金路径);
#   · 一条 compute_only(写作生成,静默扣档的真实样本)。
# [WO-B ② 2026-08-20] 发布那一条已 executable=True(图文合同链随 34 班并车上线,
# §16.1 的原子 claim/kill-window 判据在 geo_image_note 包里);写作那一条仍是 False ——
# compute_only 档没有确认回执可以当幂等键,开它之前要先给它一个别的幂等身份。
# 「不同时铺三张空协议」是 WP1 的原话,所以监测等能力这一轮仍不登记。
_PUBLISH_IMAGE_NOTE_CONTRACT = CommandContract(
    required_capability="publish.execute",
    resource_kind="geo_image_post",
    side_effect=SIDE_EFFECT_EXTERNAL,
    supported_phases=ALL_PHASES,
    adapters={
        "query": "publish_image_note_query",
        "prepare": "publish_image_note_prepare",
        # [WO-B ② 2026-08-20] 解锁前置已满足(图文合同链随 34 班并车上线),
        # execute adapter 登记 ⇒ 五阶段真的能跑到最后一跳。
        # 派发表在 api/xiaobang_operations_api._execute_adapters(),
        # 接线锁 test_every_executable_contract_has_a_dispatchable_adapter。
        "execute": "publish_image_note_execute",
        "status": "publish_image_note_status",
    },
    confirmation_policy=external_confirmation_policy(),
    idempotency=standard_idempotency(),
    # 🔴 [窗G 段二③ · Owner 2026-08-24 批] 与产线**真正扣费**那个码统一。
    #    在这之前这里写的是 "media_publish",而这条链末端
    #    (api/geo_image_note_api.PUBLISH_FEATURE_CODE)冻的是 "media_proxy_publish"。
    #
    #    ⚠️ 口径更正(2026-08-24 · 撤回本包早先写的说法):后果**不是**
    #       「get_feature_pricing 永远 ValueError ⇒ 报价档恒 unavailable」。
    #       生产 feature_pricing 里**两个码都在**(db/wallet_db.seed_feature_pricing
    #       PRICING_DATA 同时种了两行;Review 2026-08-24 生产只读实证:两者
    #       cost_points 均 = 0)⇒ 生产上查得到,不会 ValueError。
    #       「恒 unavailable」只在**没种那一行的测试库**里成立 —— 那是夹具产物,
    #       不是生产缺陷。当时那条根因是我写错的,已就地更正。
    #
    #    真正的后果(才是收敛的理由):prepare 查的是**另一条链**的目录条目,
    #    而不是这条链末端真正冻结的那个码。两行今天恰好都是 0,所以数字上看不出
    #    差别;哪天有人给其中一个码配了真价,prepare 上屏的数字与 execute 实扣的
    #    数字就会静默劈叉 —— 而用户是按上屏那个数字按下确认的。
    #    资金 SSOT:docs/SYSTEM_TRUTH/08_billing.md §3.2(feature_pricing 只定义
    #    「某功能消耗多少算力」)、§6.3(异步长任务 freeze→commit/release)。
    billing_feature="media_proxy_publish",
    executable=True,
    notes=(
        "execute 走 services/xiaobang_publish_execute.execute_publish_image_note,"
        "内部调 services/geo_douyin/publish_batch_core.materialize_publish_batch ——"
        "与 POST /api/meijiehezi/image-notes/publish-batch **同一条产线**:"
        "同一份原子 claim、同一个 resolve_settlement_authority、"
        "同一个 freeze_per_item 逐项冻结。幂等键 = intent 确认回执 receipt_id。"
    ),
)

# [WO-B2 ② 2026-08-20 · Owner 批] 素材页(GEO 获客内容)登记合同。
#
# 🔴 **只读/低危档,不新增扣费点**:
#   · ``side_effect=compute_only`` —— 两档枚举里没有"零副作用"这一档,
#     compute_only 是**唯一**不要求真实用户点击确认的那一档(§8.1),
#     语义上"这一步只消耗算力、可撤销";
#   · ``billing_feature=None`` —— **关键的一格**。没有它,
#     ``_resolve_compute_quote`` 直接返回 ``not_applicable``,报价链一个字都不走,
#     也就不可能凭空长出一个扣费点。配套判据
#     ``test_geo_content_center_declares_no_billing_point`` 钉死这一位;
#   · ``executable=False`` + **不登记 execute adapter** —— 它只是让
#     ``/marketing-materials`` 的深链 intent 产生得出来(§12.3 那一格),
#     五阶段的执行段与它无关。
#
# 🔴 capability 用已登记的 ``materials.`` 命名空间(``_MEMBER_CAPABILITY_PREFIXES``
#    里本来就有 ``materials``),不新开一个前缀 —— 新前缀等于给员工席位开一道
#    没人复核过的门。
_GEO_CONTENT_CENTER_CONTRACT = CommandContract(
    required_capability="materials.produce",
    resource_kind="geo_image_post",
    side_effect=SIDE_EFFECT_COMPUTE_ONLY,
    supported_phases=ALL_PHASES,
    adapters={
        "query": "geo_content_center_query",
        "prepare": "geo_content_center_prepare",
        "status": "geo_content_center_status",
    },
    confirmation_policy=compute_only_confirmation_policy(),
    idempotency=standard_idempotency(),
    billing_feature=None,
    executable=False,
    notes=(
        "只读/低危登记(Owner 2026-08-20 批):唯一目的是让 /marketing-materials 的"
        "深链 intent 产生得出来(规格 §12.3「GEO 图文页需补真实 intent 解析」)。"
        "billing_feature=None ⇒ 报价链不走 ⇒ 不新增扣费点;"
        "无 execute adapter ⇒ 五阶段执行段对它一律 409 NOT_EXECUTABLE。"
    ),
)

_WRITING_GENERATE_CONTRACT = CommandContract(
    required_capability="writing.generate",
    resource_kind="article",
    side_effect=SIDE_EFFECT_COMPUTE_ONLY,
    supported_phases=ALL_PHASES,
    adapters={
        "query": "writing_generate_query",
        "prepare": "writing_generate_prepare",
        "status": "writing_generate_status",
    },
    confirmation_policy=compute_only_confirmation_policy(),
    idempotency=standard_idempotency(),
    billing_feature="article_gen",
    executable=False,
    notes=(
        "纯算力消耗、可撤销 → 静默扣 + 事后通知(已签发 DP-A6.1)。"
        "A6.1 唯一边界仍适用:本档只覆盖用户在本次交互中自己提出的目标,"
        "系统不得替用户开启他没开的收费项。"
    ),
)


_COMMON = DEFAULT_OPERATOR_ROLES
_MEMBER = MEMBER_OPERATOR_ROLES
_OWNER = (ROLE_ADMIN, ROLE_AGENT)
_ADMIN = (ROLE_ADMIN,)
_NORMAL = (ROLE_ADMIN, ROLE_NORMAL)

# 现役三身份可见导航 + 四步主流程。管理员专属项也在同一注册表里，
# 但匹配后仍需 ``is_operation_allowed`` 二次校验，不能只靠前端隐藏。
_ENTRIES: tuple[OperationEntry, ...] = (
    _nav("today_workspace", "今日工作台", "/dashboard/today", roles=_MEMBER,
         synonyms=("今日看板", "工作台入口")),
    _nav("diagnosis_new", "品牌体检", "/diagnosis/new", roles=_MEMBER, module="diagnosis",
         synonyms=("AI搜索诊断", "AI 搜索诊断", "发起诊断", "做一次诊断", "诊断入口"),
         breadcrumb=("主流程", "品牌体检"), action_type="prefill"),
    _nav("quote_center", "客户报价", "/pricing", roles=_MEMBER, module="quote",
         synonyms=("报价方案", "在线报价", "报价中心", "出报价", "选词"),
         breadcrumb=("主流程", "报价方案")),
    _nav("writing_center", "AI 创作中心", "/writing", roles=_MEMBER, module="writing",
         synonyms=("AI写文章", "写文章", "写作中心", "写作大厅", "开始写"),
         breadcrumb=("主流程", "AI 创作中心"), action_type="prefill",
         contract=_WRITING_GENERATE_CONTRACT),
    _nav("geo_content_center", "制作 GEO 图文", "/marketing-materials",
         roles=_MEMBER, module="materials",
         synonyms=("GEO 获客内容", "做 GEO 图文", "制作图文", "营销物料"),
         action_type="prefill", contract=_GEO_CONTENT_CENTER_CONTRACT),
    _nav("publish_center", "发布投放", "/publish", roles=_MEMBER, module="publish",
         synonyms=("发布中心", "发布管理", "投放入口", "去哪发布", "媒体代发"),
         breadcrumb=("主流程", "发布投放"), action_type="prefill",
         contract=_PUBLISH_IMAGE_NOTE_CONTRACT),
    _nav("monitoring_center", "效果监测", "/monitoring", roles=_MEMBER, module="monitoring",
         synonyms=("排名监测", "监测入口", "看效果", "监测词", "AI 引用")),
    _nav("client_list", "我的客户", "/my-clients", roles=_MEMBER,
         synonyms=("客户列表", "客户档案", "客户资料", "选择客户", "添加客户")),
    _nav("brand_center", "我的品牌", "/my-brand",
         synonyms=("品牌列表", "品牌档案", "品牌资料", "品牌设置")),
    _nav("team_seats", "团队与席位", "/organization/team",
         synonyms=("团队管理", "成员席位", "邀请成员", "组织团队")),
    _nav("wallet", "我的钱包", "/wallet",
         synonyms=("钱包", "账务", "余额", "消费明细", "扣费记录", "退款")),
    _nav("feature_pricing", "算力价格表", "/feature-pricing",
         synonyms=("功能定价", "算力定价", "价格表", "收费标准")),
    # 🔴 2026-08-21(包 B · 工单截图的**真因**):这里原本有一条同义词 `"怎么用"`。
    #    `OperationRegistry.match` 是**裸子串**匹配(:398 `candidate in text`),
    #    于是**任何**含「怎么用」的问题都命中 help_center,并在
    #    `api/xiaobang_api.py` 的 `deterministic_direct` 分支**先于一切层短路**:
    #      · 「员工席位这个该怎么用」 → help_center → 答「在「帮助中心」」(截图第 2 幕)
    #      · 「你除了引导我去帮助中心还能做什么」 → 命中「帮助中心」→ 又指回帮助中心(第 3 幕)
    #    工单把这归因于 current_page 关闸 + 提示词甩锅;那两条是真的,但**这一条才是主因** ——
    #    它比另外两条更早短路,前两条改完也照样甩锅。
    #    「怎么用」表达的是「<某功能>怎么用」,不是「带我去帮助中心」,本就不该当它的同义词。
    #    (对照:"帮助中心" / "帮助文档" / "使用说明" / "找帮助" 都是真的在找帮助中心,保留。)
    _nav("help_center", "帮助中心", "/help", roles=_MEMBER,
         synonyms=("使用说明", "帮助文档", "找帮助")),
    _nav("personal_settings", "个人设置", "/account/profile", roles=_MEMBER,
         synonyms=("修改密码", "改密码", "账号设置", "个人资料")),
    _nav("feedback", "问题反馈", "/feedback", roles=_MEMBER,
         synonyms=("反馈问题", "联系客服")),
    _nav("whitelabel", "对外品牌", "/agent/whitelabel",
         synonyms=("白标", "品牌授权", "对外品牌设置")),
    _nav("customer_compute", "我的算力", "/customer/wallet", roles=_NORMAL,
         synonyms=("算力余额", "剩余算力")),
    _nav("customer_recharge", "购买算力", "/customer/recharge", roles=_NORMAL,
         synonyms=("购买额度", "充值算力"), action_type="prefill"),
    _nav("referral", "推荐有礼", "/referral", roles=_NORMAL,
         synonyms=("推荐码", "推荐链接", "邀请好友")),

    _nav("agent_overview", "经营总览", "/agent/profit", roles=_OWNER,
         synonyms=("利润总览", "服务商经营")),
    _nav("agent_inventory", "算力库存", "/agent/inventory", roles=_OWNER,
         synonyms=("进货", "划拨算力", "库存中心")),
    _nav("agent_pricing", "客户售价", "/agent/pricing", roles=_OWNER,
         synonyms=("客户定价", "销售价格")),
    _nav("agent_settlement", "提现结算", "/agent/settlement", roles=_OWNER,
         synonyms=("收益提现", "服务商结算")),
    _nav("agent_promotion", "推广获客", "/agent/promotion", roles=_OWNER,
         synonyms=("推广中心", "获客推广")),
    _nav("agent_agreement", "合作协议", "/agent/agreement", roles=_OWNER),

    _nav("admin_tv", "大屏监控", "/tv", roles=_ADMIN, module="users"),
    _nav("admin_inventory_audit", "资金与算力对账", "/admin/inventory-audit",
         roles=_ADMIN, module="users", synonyms=("资金对账", "算力对账")),
    # [WP0 · 规格 §3.3 两条漏项] 覆盖测试早就把它们指出来了,但注册表一直没补,
    # 于是 test_visible_sidebar_routes_are_covered_by_the_registry 在生产尖上是**红的**。
    # 补进来的同时不动侧栏和路由本身 —— 漏的是注册,不是页面。
    _nav("admin_agent_inventory", "服务商库存与关系", "/admin/agent-inventory",
         roles=_ADMIN, module="users",
         synonyms=("服务商库存", "入库划拨", "渠道关系管理", "库存与关系")),
    _nav("admin_media_directory", "媒体目录分档", "/admin/media-directory",
         roles=_ADMIN, module="users",
         synonyms=("媒体分档", "媒体目录", "媒体档位管理")),
    _nav("admin_marketing", "营销中心", "/admin/marketing", roles=_ADMIN, module="users"),
    _nav("admin_finance", "财务中心", "/admin/finance", roles=_ADMIN, module="users",
         synonyms=("平台账务", "财务报表")),
    _nav("admin_orders", "订单管理", "/admin/orders", roles=_ADMIN, module="settings"),
    _nav("admin_withdrawals", "提现审核", "/admin/withdrawals", roles=_ADMIN, module="users"),
    _nav("admin_settlements", "代理打款审核", "/admin/settlements", roles=_ADMIN, module="users"),
    _nav("admin_refunds", "充值退款工单中心", "/admin/refunds", roles=_ADMIN, module="users"),
    _nav("admin_diagnosis_funds", "诊断资金异常处置", "/admin/diagnosis-fund-exceptions",
         roles=_ADMIN, module="users"),
    _nav("admin_pricing", "算力定价中心", "/admin/pricing-center", roles=_ADMIN, module="users"),
    _nav("admin_channel_tier", "渠道等级后台", "/admin/channel-tier", roles=_ADMIN, module="users"),
    _nav("admin_tax", "代理税务档案", "/admin/tax-profiles", roles=_ADMIN, module="users"),
    _nav("admin_settings", "系统设置", "/settings", roles=_ADMIN, module="settings"),
    _nav("admin_users", "用户管理", "/admin/users", roles=_ADMIN, module="users"),
    _nav("admin_team_policy", "团队席位策略", "/admin/organization-product-config",
         roles=_ADMIN, module="users"),
    _nav("admin_audit", "审计日志", "/admin/audit", roles=_ADMIN, module="users"),
    _nav("admin_ai_ops", "AI 运维控制塔", "/admin/ai-ops", roles=_ADMIN, module="users"),
    _nav("admin_research", "GEO 调研监测", "/admin/research-monitor", roles=_ADMIN, module="users"),
    _nav("admin_flywheel", "GEO 数据飞轮", "/admin/geo-placement-flywheel",
         roles=_ADMIN, module="users"),
    _nav("admin_observation", "观测治理中心", "/admin/geo-observation-center",
         roles=_ADMIN, module="users"),
    _nav("admin_industry", "行业素材矫正", "/admin/industry-corrections",
         roles=_ADMIN, module="users"),
    _nav("admin_whitelabel", "对外品牌授权", "/admin/whitelabel", roles=_ADMIN, module="users"),
    _nav("admin_binding", "客户归属争议", "/admin/binding-disputes", roles=_ADMIN, module="users"),
    _nav("admin_help", "帮助中心管理", "/admin/help-center", roles=_ADMIN, module="settings"),

    _nav("gap_plan_block", "交付计划", "/pricing", roles=_MEMBER, module="quote",
         synonyms=("缺口作战计划", "作战计划", "今天写什么", "任务卡"),
         breadcrumb=("客户报价", "打开词包", "交付计划"),
         help_target="gap-plan-section", prerequisites=("authenticated", "quote_context")),
    _nav("gap_plan_publication_link", "填发布链接", "/pricing", roles=_MEMBER, module="quote",
         synonyms=("发布链接填哪里", "发完了在哪填", "已发布怎么登记"),
         breadcrumb=("客户报价", "交付计划", "任务卡"),
         help_target="gap-plan-section", prerequisites=("authenticated", "quote_context"),
         action_type="prefill"),
    _nav("media_library", "媒体渠道", "/publish", roles=_MEMBER, module="publish",
         synonyms=("媒体库", "去哪查渠道", "发布落点"),
         help_target=help_target_for_route("/publish")),
)


class OperationRegistry:
    """只读版本化注册表。"""

    def __init__(self, entries: tuple[OperationEntry, ...] = _ENTRIES):
        active = tuple(e for e in entries if e.until_version is None)
        ids = [e.operation_id for e in active]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate operation_id")
        for entry in active:
            # 社媒域负向枚举锁(Owner 2026-08-17)。放在构造函数里而不是某个
            # 「校验函数」里 —— 校验函数可以不调用,构造函数不能不走。
            assert_no_social_domain(
                identifier=entry.operation_id,
                route=entry.route_template,
                capability=(
                    entry.command_contract.required_capability
                    if entry.command_contract is not None else ""
                ),
                modules=(entry.required_module or "",),
                where="operation_registry",
            )
        self._entries = active
        self._by_id = {e.operation_id: e for e in active}

    def all(self) -> tuple[OperationEntry, ...]:
        return self._entries

    def resolve(self, operation_id: str) -> OperationEntry | None:
        return self._by_id.get(str(operation_id or ""))

    def match(self, question: str) -> OperationEntry | None:
        text = str(question or "").strip().lower().replace(" ", "")
        if not text:
            return None
        best: tuple[int, OperationEntry] | None = None
        for entry in self._entries:
            for phrase in entry.synonyms:
                candidate = phrase.lower().replace(" ", "")
                if candidate and candidate in text:
                    score = len(candidate)
                    if best is None or score > best[0]:
                        best = (score, entry)
        return best[1] if best else None

    def render_route(self, entry: OperationEntry, context: dict | None = None) -> str | None:
        values = context or {}
        fields = [name for _, name, _, _ in Formatter().parse(entry.route_template) if name]
        if any(name not in _PLACEHOLDER_NAMES for name in fields):
            return None
        if any(values.get(name) in (None, "") for name in fields):
            return None
        try:
            route = entry.route_template.format_map(values)
        except (KeyError, ValueError):
            return None
        return route if route.startswith("/") and not route.startswith("//") else None


@lru_cache(maxsize=1)
def get_operation_registry() -> OperationRegistry:
    return OperationRegistry()


def all_operations() -> tuple[OperationEntry, ...]:
    return get_operation_registry().all()


def resolve_operation(operation_id: str) -> OperationEntry | None:
    return get_operation_registry().resolve(operation_id)


def match_operation(question: str) -> OperationEntry | None:
    return get_operation_registry().match(question)


def actor_role(user: dict | None, *, identity: Any = None) -> str:
    user = user or {}
    if user.get("is_admin"):
        return ROLE_ADMIN
    # 组织身份只能取 middleware/organization_guard.py 每请求重验后注入的
    # IdentityContext。不能相信浏览器字段，也不能从 user dict 猜员工身份。
    if getattr(identity, "is_member", False) or identity == ROLE_MEMBER:
        return ROLE_MEMBER
    if getattr(identity, "is_owner", False) or identity == "agent" or int(user.get("agent_level") or 0) >= 1:
        return ROLE_AGENT
    return ROLE_NORMAL


_MEMBER_CAPABILITY_PREFIXES: dict[str, tuple[str, ...]] = {
    "clients": ("clients.",),
    "diagnosis": ("diagnosis.",),
    "quote": ("quote.",),
    "writing": ("writing.",),
    "materials": ("materials.",),
    "publish": ("publish.",),
    "monitoring": ("monitoring.",),
}
_MEMBER_OPERATION_CAPABILITY_PREFIXES: dict[str, tuple[str, ...]] = {
    "client_list": ("clients.",),
}


def is_operation_allowed(entry: OperationEntry, user: dict | None, *, identity: Any = None) -> bool:
    user = user or {}
    role = actor_role(user, identity=identity)
    if role not in entry.allowed_roles:
        return False
    if role == ROLE_ADMIN or getattr(identity, "is_owner", False):
        return True
    if role == ROLE_MEMBER:
        if not (getattr(identity, "is_member", False) or identity == ROLE_MEMBER):
            return False
        capabilities = frozenset(str(value) for value in getattr(identity, "capabilities", ()))
        prefixes = _MEMBER_OPERATION_CAPABILITY_PREFIXES.get(
            entry.operation_id,
            _MEMBER_CAPABILITY_PREFIXES.get(entry.required_module or "", ()),
        )
        if not prefixes:
            return entry.required_module is None
        return any(capability.startswith(prefix) for capability in capabilities for prefix in prefixes)
    if entry.required_module is None:
        return True
    permissions = tuple(str(p) for p in (user.get("permissions") or ()))
    return any(p.startswith(f"{entry.required_module}:") for p in permissions)


_ACTION_TO_OPERATION: dict[str, str] = {
    "open_delivery_plan": "gap_plan_block",
    "open_media_library": "media_library",
    "view_checkback_schedule": "gap_plan_block",
    "show_adjacent_queries": "gap_plan_block",
}


def action_operation_ids() -> dict[str, str]:
    return dict(_ACTION_TO_OPERATION)


def route_for_action(action_id: str, context: dict | None = None) -> dict[str, str] | None:
    operation_id = _ACTION_TO_OPERATION.get(str(action_id or ""), "")
    entry = resolve_operation(operation_id)
    if entry is None:
        return None
    route = get_operation_registry().render_route(entry, context)
    if not route:
        return None
    return {
        "operation_id": operation_id,
        "target_route": route,
        "help_target": entry.help_target,
        "registry_version": OPERATION_REGISTRY_VERSION,
    }


def commandable_operations() -> tuple[OperationEntry, ...]:
    """挂了 command_contract 的条目(v3 能力发现的候选集)。"""
    return tuple(e for e in all_operations() if e.command_contract is not None)


def resolve_command_contract(operation_id: str):
    entry = resolve_operation(operation_id)
    return None if entry is None else entry.command_contract


def discover_commands(user: dict | None, *, identity: Any = None) -> list[dict]:
    """按当前服务端身份过滤后的可发现命令目录(规格 §8.2)。

    🔴 无权 operation 的**名称、schema 和存在性**都不下发 —— 所以这里是
    先过 :func:`is_operation_allowed`,再过 capability,最后才吐 public dict;
    公共 dict 里没有 adapter 名、内部 URL、HTTP method 和供应商信息。
    """
    out: list[dict] = []
    capabilities = frozenset(
        str(value) for value in getattr(identity, "capabilities", ()) or ()
    )
    is_admin = bool((user or {}).get("is_admin"))
    is_member = bool(getattr(identity, "is_member", False))
    for entry in commandable_operations():
        if not is_operation_allowed(entry, dict(user or {}), identity=identity):
            continue
        contract = entry.command_contract
        if is_member and not is_admin:
            # 员工席位再过一次 capability:注册表可见 ≠ 该员工持有该能力。
            if contract.required_capability not in capabilities:
                continue
        payload = contract.as_public_dict()
        payload["operation_id"] = entry.operation_id
        payload["display_name"] = entry.display_name
        payload["registry_version"] = OPERATION_REGISTRY_VERSION
        out.append(payload)
    return out


def describe(entry: OperationEntry, context: dict | None = None) -> dict:
    route = get_operation_registry().render_route(entry, context)
    return {
        "operation_id": entry.operation_id,
        "display_name": entry.display_name,
        "target_route": route,
        "breadcrumb": list(entry.breadcrumb),
        "help_target": entry.help_target,
        "allowed_roles": list(entry.allowed_roles),
        "permissions": list(entry.permissions),
        "prerequisites": list(entry.prerequisites),
        "action_type": entry.action_type,
        "operation_map_version": OPERATION_MAP_VERSION,
    }


def declared_frontend_routes() -> frozenset[str]:
    if not _APP_TSX.exists():
        return frozenset()
    out: set[str] = set()
    for raw in _ROUTE_RE.findall(_APP_TSX.read_text(encoding="utf-8", errors="replace")):
        out.add(raw if raw.startswith("/") else "/" + raw)
    return frozenset(out)


#: [WO-B2 ① 2026-08-20] 路由 → **有合同的** operation id。深链 producer 用它。
#:
#: 🔴 从注册表**机械派生**,不手写映射表:手写的那一份会在下一次加/删合同时
#:    悄悄漂 —— 而漂的表现是"这张卡不再签发深链",没有任何东西会变红。
#: 🔴 只收**有 command_contract** 的条目:没有合同的 operation 连 intent 都
#:    prepare 不出来(``_resolve_entry`` 对无合同一律 404),给它签发深链
#:    等于给用户一个必然 404 的链接。
@lru_cache(maxsize=1)
def _route_to_commandable_operation() -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in commandable_operations():
        path = urlsplit(entry.route_template).path
        # 同一路径挂两条合同是注册表配置错误,这里取先登记的那一条并保持确定性
        out.setdefault(path, entry.operation_id)
    return out


def commandable_operation_for_route(route: str) -> str | None:
    """这个路由背后有没有一条**可 prepare** 的 operation。拿不到返回 ``None``。"""
    path = urlsplit(str(route or "")).path
    if not path:
        return None
    return _route_to_commandable_operation().get(path)


def registry_route_paths() -> frozenset[str]:
    """供 route parity 使用；模板占位先去掉 query，仅验证注册的页面骨架。"""
    return frozenset(urlsplit(e.route_template).path for e in all_operations())
