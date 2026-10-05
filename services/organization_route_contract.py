"""Exact employee-seat route contract for live GEO handlers.

Member traffic is never admitted by a broad prefix.  Every entry binds an
HTTP method, an exact path shape, the capability checked by the organization
guard, and the resource whose handler must perform the second (object-level)
authorization check.  Routes absent from this table remain fail-closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Optional, Pattern


@dataclass(frozen=True)
class MemberRoutePolicy:
    method: str
    path_template: str
    capability: str
    resource_kind: str
    operation: str
    #: 🔴 **现无运行期消费方**(Review 2026-08-24 census,窗G 段二③ 收口时复核)。
    #:    这一格是**只写不读**的声明数据:``middleware/organization_guard`` 匹配到
    #:    policy 后只取 ``capability`` / ``capability_any_of``,然后把整个 policy
    #:    塞进 ``request.state.organization_route_policy`` —— 而那个 state 键
    #:    **全仓再无读取点**;组织预算实际走的是各 handler 显式传给
    #:    ``deduct_points`` 的 feature_code 参数。``route_contract_manifest()``
    #:    也只有判据在调。
    #:    ⇒ 改这一格**不改变任何运行期计费归属**。但正因为它迟早会被接线,
    #:    值必须与那条路由 handler **真正扣的**码一致,否则接线那天会带错值上线。
    #:    这条对齐由 test_the_declared_billing_matches_what_the_handler_charges
    #:    机械锁住(从 handler AST 取真值,不手写期望)。
    billing_feature: Optional[str] = None
    async_surface: Optional[str] = None
    #: [R3-P7 ④] 通用路由:这条路径**可达的 operation 集合**各自要求的 capability。
    #: 非空时外层守卫走 ``require_any``(持有任意一项即可达),精确校验交给
    #: handler 的 ``_authorize()``。留空 = 沿用单 capability 的老行为
    #: (所以这是**追加谓词**,不是把原谓词改写成三元式 —— 没传的老路一个字节没变)。
    capability_any_of: tuple[str, ...] = ()

    @property
    def compiled(self) -> Pattern[str]:
        pattern = re.escape(self.path_template)
        pattern = pattern.replace(r"\{session_id\}", r"(?P<session_id>[A-Za-z0-9_-]{8,200})")
        pattern = pattern.replace(r"\{profile_id\}", r"(?P<profile_id>[A-Za-z0-9_-]{1,200})")
        pattern = pattern.replace(r"\{request_id\}", r"(?P<request_id>[A-Za-z0-9._:-]{8,128})")
        pattern = pattern.replace(r"\{material_id\}", r"(?P<material_id>[A-Za-z0-9_-]{8,64})")
        pattern = pattern.replace(r"\{token\}", r"(?P<token>[A-Za-z0-9_-]{8,200})")
        pattern = pattern.replace(r"\{task_id\}", r"(?P<task_id>[1-9][0-9]*)")
        pattern = pattern.replace(r"\{cell_id\}", r"(?P<cell_id>[1-9][0-9]*)")
        # [xbvnext 2026-08-18] 小榜五阶段端点的三个占位是**非数字**的:
        #   operation_id 是注册表 id(snake_case)、intent_id/execution_id 是
        #   不可猜的 opaque 串。不加这三行的话,下面那条兜底正则会把它们当成
        #   数字主键 —— 结果是路由**永远匹配不上**,员工在守卫层直接 403,
        #   而且失败形态跟"没登记"一模一样(静默,查不出来)。
        pattern = pattern.replace(r"\{operation_id\}", r"(?P<operation_id>[a-z][a-z0-9_]{1,63})")
        pattern = pattern.replace(r"\{intent_id\}", r"(?P<intent_id>xint_[A-Za-z0-9_-]{8,64})")
        pattern = pattern.replace(r"\{execution_id\}", r"(?P<execution_id>xexe_[A-Za-z0-9_-]{8,64})")
        pattern = re.sub(r"\\\{[a-zA-Z_][a-zA-Z0-9_]*\\\}", r"(?P<resource_id>[1-9][0-9]*)", pattern)
        return re.compile(f"^{pattern}$")

    def as_contract(self) -> dict:
        return asdict(self)


def _p(
    method: str,
    path: str,
    capability: str,
    resource: str,
    operation: str,
    *,
    billing: Optional[str] = None,
    async_surface: Optional[str] = None,
    capability_any_of: tuple[str, ...] = (),
) -> MemberRoutePolicy:
    return MemberRoutePolicy(
        method, path, capability, resource, operation, billing, async_surface,
        capability_any_of,
    )


def commandable_capabilities() -> tuple[str, ...]:
    """[R3-P7 ④] 五阶段通用路由可达的 capability 全集 —— **从注册表取,不手写**。

    手写就会在新增一个 command_contract 时静默漂移,而漂移的表现是
    「能力发现下发了、外层守卫 403」——两边单看都对。
    延迟 import 是为了避开 ``gap_operation_map`` ↔ 本模块的潜在环。
    """
    from services.gap_operation_map import commandable_operations

    seen: list[str] = []
    for entry in commandable_operations():
        contract = entry.command_contract
        capability = getattr(contract, "required_capability", "")
        if capability and capability not in seen:
            seen.append(capability)
    return tuple(seen)


#: 只读规划档也要能进(query/prefill 这类)。与上面的能力集并起来做 any-of。
_PLAN_TIER_CAPABILITIES: tuple[str, ...] = ("publish.plan", "clients.read_assigned")


def five_phase_any_of() -> tuple[str, ...]:
    out = list(_PLAN_TIER_CAPABILITIES)
    for capability in commandable_capabilities():
        if capability not in out:
            out.append(capability)
    return tuple(out)


#: [R3-P7 ④] 求值一次。**从注册表取**,不手写 —— 手写会在新增 command_contract 时
#: 静默漂移,而漂移的表现是「能力发现下发了、外层守卫 403」,两边单看都对。
_FIVE_PHASE_ANY_OF: tuple[str, ...] = five_phase_any_of()

# This is deliberately conservative.  External/public actions are exclusively
# exposed through /api/organization approval + token endpoints and never by a
# legacy send/share/publish handler.
MEMBER_GEO_ROUTE_POLICIES: tuple[MemberRoutePolicy, ...] = (
    # 小榜只读取已分配客户的上下文并签发导航动作。员工身份仍由
    # OrganizationGuard 每请求重验，handler 内再按 brand/quote 做对象级校验；
    # 不把整个 /api/xiaobang namespace 放进宽白名单（其中还有管理端重建接口）。
    _p("POST", "/api/xiaobang/context", "clients.read_assigned", "brand", "list"),
    _p("POST", "/api/xiaobang/chat", "clients.read_assigned", "brand", "list"),

    # [xbvnext 2026-08-18 · 规格 §8.2/P1-1] 五阶段端点**逐条**登记:方法 + 精确
    # 路径 + capability + resource_kind。不在这张表里 = 员工席位 fail-closed 403。
    # 🔴 三点刻意:
    #   ① 不放 prefix。整个 /api/xiaobang/operations namespace 放宽 = 以后新增
    #      端点自动获得员工可达性,那正是这张表要防的事;
    #   ② capability 用 publish.plan(只读规划档)而不是 publish.execute ——
    #      WP1 的 execute 一律 409,给交付角色 execute 档等于提前发权限。
    #      handler 内部还会按 command_contract.required_capability 再问一次;
    #   ③ resource_kind 统一 xiaobang_intent:这些端点操作的是**协调层意图**,
    #      不是品牌/报价/文章本身;对象级校验由 resolve_authorized_context 做。
    # [R3-P6 2026-08-19] 能力发现 + 页面预填两条 GET,同样**逐条**登记。
    #   ① 能力发现是纯读目录 → clients.read_assigned(三档角色模板都持有);
    #      handler 里 discover_commands() 还会按 capability 再筛一遍,
    #      没有该能力的 operation 连名字都不下发(§8.2);
    #   ② 预填按 intent 反解 operation,路径里**没有** operation_id
    #      (deep link 只带 opaque xint_,§12.2)。capability 取 publish.plan
    #      这一档(只读规划),与 GET intent 同档 —— 它返回的是同一行的投影。
    _p("GET", "/api/xiaobang/operations/capabilities",
       "clients.read_assigned", "xiaobang_intent", "list",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("GET", "/api/xiaobang/operations/intents/{intent_id}/prefill",
       "publish.plan", "xiaobang_intent", "detail",
       capability_any_of=_FIVE_PHASE_ANY_OF),

    _p("POST", "/api/xiaobang/operations/{operation_id}/query",
       "clients.read_assigned", "xiaobang_intent", "list",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("POST", "/api/xiaobang/operations/{operation_id}/prepare",
       "publish.plan", "xiaobang_intent", "create",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("GET", "/api/xiaobang/operations/{operation_id}/intents/{intent_id}",
       "publish.plan", "xiaobang_intent", "detail",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("POST", "/api/xiaobang/operations/{operation_id}/intents/{intent_id}/confirm",
       "publish.plan", "xiaobang_intent", "update",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("POST", "/api/xiaobang/operations/{operation_id}/intents/{intent_id}/cancel",
       "publish.plan", "xiaobang_intent", "update",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("POST", "/api/xiaobang/operations/{operation_id}/intents/{intent_id}/execute",
       "publish.execute", "xiaobang_intent", "update",
       capability_any_of=_FIVE_PHASE_ANY_OF),
    _p("GET", "/api/xiaobang/operations/{operation_id}/executions/{execution_id}/status",
       "publish.plan", "xiaobang_intent", "status", async_surface="poll",
       capability_any_of=_FIVE_PHASE_ANY_OF),

    # Assigned clients and profiles.
    _p("GET", "/api/my-clients", "clients.read_assigned", "brand", "list"),
    _p("GET", "/api/my-clients/{brand_id}", "clients.read_assigned", "brand", "detail"),
    _p("PUT", "/api/my-clients/{brand_id}", "clients.profile_edit", "brand", "update"),
    # [F-1] 录入新客户。此前该路由**未被分类**,员工在守卫层就吃 403
    # ORG_ROUTE_NOT_CLASSIFIED —— 改上限也走不到。归到既有的 clients.profile_edit
    # (客户面写权限)而不是新造能力:销售角色本就持有它,交付/只读角色没有,
    # 于是"销售录客户、交付不录"的分工天然成立,也不需要给存量角色补授权。
    _p("POST", "/api/my-clients", "clients.profile_edit", "brand", "create"),
    # [白标继承] 员工要能**看到**团队长设置的对外品牌(否则整个工作台退回平台默认皮肤,
    # 客户面也无从预览)。只放 GET;PUT / logo 仍不分类 = 守卫层直接 403,
    # 加上端点内 WHITELABEL_OWNER_ONLY 硬闸,双层保证员工改不了。
    # 归到 clients.read_assigned:三档角色模板都持有,存量角色无需补授权即可生效。
    _p("GET", "/api/referral/whitelabel", "clients.read_assigned", "brand", "read"),
    _p("GET", "/api/brands", "clients.read_assigned", "brand", "list"),
    _p("GET", "/api/brands/{brand_id}", "clients.read_assigned", "brand", "detail"),
    _p("GET", "/api/brands/{brand_id}/diagnoses", "diagnosis.read_own", "diagnosis", "list"),
    _p("GET", "/api/brands/{brand_id}/latest-diagnosis-params", "diagnosis.read_own", "diagnosis", "list"),
    _p("GET", "/api/brands/{brand_id}/diagnosis-history", "diagnosis.read_own", "diagnosis", "list"),
    _p("GET", "/api/brands/{brand_id}/materials", "materials.read_assigned", "client_material", "list"),
    _p("GET", "/api/materials/{diagnosis_id}", "materials.read_assigned", "client_material", "detail"),
    _p("POST", "/api/materials/{diagnosis_id}", "materials.write", "client_material", "create"),
    _p("PUT", "/api/materials/{diagnosis_id}", "materials.write", "client_material", "update"),
    _p("GET", "/api/marketing/content-center/bootstrap", "materials.read_assigned", "marketing_material", "list"),
    _p("GET", "/api/marketing/teachers", "materials.read_assigned", "marketing_material", "list"),
    _p("GET", "/api/marketing/pricing", "materials.read_assigned", "marketing_material", "list"),
    _p("POST", "/api/marketing/interpret", "materials.write", "marketing_material", "create"),
    _p("GET", "/api/marketing/product-facts", "materials.read_assigned", "marketing_material", "list"),
    _p("POST", "/api/marketing/qr-reference", "materials.write", "marketing_material", "create"),
    _p("POST", "/api/marketing/content-packages", "materials.write", "marketing_material", "create", async_surface="task"),
    _p("GET", "/api/marketing/content-packages/by-request/{request_id}", "materials.read_assigned", "marketing_material", "status", async_surface="poll"),
    _p("GET", "/api/marketing/jobs/{job_id}", "materials.read_assigned", "marketing_material", "status", async_surface="poll"),
    _p("POST", "/api/marketing/jobs/{job_id}/retry", "materials.write", "marketing_material", "create", async_surface="task"),
    _p("GET", "/api/marketing/my-materials", "materials.read_assigned", "marketing_material", "list"),
    _p("GET", "/api/marketing/materials/{asset_id}/download", "materials.read_assigned", "marketing_material", "detail"),
    _p("POST", "/api/marketing/materials/{asset_id}/confirm", "materials.write", "marketing_material", "update"),
    _p("POST", "/api/marketing/materials/{asset_id}/report", "materials.write", "marketing_material", "update"),
    _p("PATCH", "/api/marketing/assets/{asset_id}", "materials.write", "marketing_material", "update"),
    _p("POST", "/api/marketing/deal-drafts", "materials.write", "marketing_material", "create"),
    _p("GET", "/api/marketing/deal-drafts/{draft_id}", "materials.read_assigned", "marketing_material", "detail"),
    _p("POST", "/api/marketing/deal-drafts/{draft_id}/materials", "materials.write", "marketing_material", "create"),
    _p("POST", "/api/marketing/deal-drafts/{draft_id}/voice", "materials.write", "marketing_material", "create"),
    _p("POST", "/api/marketing/deal-drafts/{draft_id}/analyze", "materials.write", "marketing_material", "create"),
    _p("PATCH", "/api/marketing/deal-drafts/{draft_id}", "materials.write", "marketing_material", "update"),
    _p("POST", "/api/marketing/deal-drafts/{draft_id}/redact", "materials.write", "marketing_material", "update"),
    _p("GET", "/api/marketing/deal-drafts/{draft_id}/materials/{material_id}/file", "materials.read_assigned", "marketing_material", "detail"),
    _p("GET", "/api/brands/{brand_id}/competitors", "clients.read_assigned", "brand", "detail"),
    _p("PUT", "/api/brands/{brand_id}/competitors", "clients.profile_edit", "brand", "update"),
    _p("GET", "/api/profiles", "clients.read_assigned", "client_profile", "list"),
    _p("GET", "/api/profiles/{profile_id}", "clients.read_assigned", "client_profile", "detail"),
    _p("PUT", "/api/profiles/{profile_id}", "clients.profile_edit", "client_profile", "update"),

    # Diagnosis: one pre-reserved live writer plus isolated reads/exports.
    _p("POST", "/api/diagnosis", "diagnosis.run", "diagnosis", "create", billing="geo_diagnosis", async_surface="task"),
    _p("POST", "/api/diagnosis/start", "diagnosis.run", "diagnosis", "create", billing="geo_diagnosis", async_surface="task"),
    _p("GET", "/api/diagnosis/session/{session_id}/status", "diagnosis.read_own", "diagnosis", "status", async_surface="poll"),
    _p("GET", "/api/diagnosis/{diagnosis_id}", "diagnosis.read_own", "diagnosis", "detail"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/type", "diagnosis.read_own", "diagnosis", "detail"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/retest-data", "diagnosis.read_own", "diagnosis", "detail"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/content", "diagnosis.read_own", "diagnosis", "detail"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/report-v2.pdf", "diagnosis.export", "diagnosis", "export"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/report-v2.html", "diagnosis.export", "diagnosis", "export"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/export/pdf", "diagnosis.export", "diagnosis", "export"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/export/pptx", "diagnosis.export", "diagnosis", "export"),

    # Quote proposal creation and owner/member-isolated reads.
    _p("POST", "/api/diagnosis/{diagnosis_id}/generate-quote", "quote.create", "quote", "create", billing="quote_generate"),
    _p("GET", "/api/diagnosis/{diagnosis_id}/quote", "quote.read_own", "quote", "detail"),
    _p("GET", "/api/quotes", "quote.read_own", "quote", "list"),
    _p("GET", "/api/quotes/{quote_id}", "quote.read_own", "quote", "detail"),
    _p("GET", "/api/quotes/{quote_id}/coefficient-preview", "quote.create", "quote", "detail"),
    _p("POST", "/api/quotes/{quote_id}/coefficient", "quote.create", "quote", "update"),
    _p("GET", "/api/quotes/{quote_id}/archive-preview", "quote.read_own", "quote", "detail"),
    _p("DELETE", "/api/quotes/{quote_id}", "team.output_handoff", "quote", "archive"),
    _p("POST", "/api/quotes/{quote_id}/restore", "team.output_handoff", "quote", "restore"),
    _p("GET", "/api/keyword-selection/list", "quote.read_own", "quote", "list"),
    _p("POST", "/api/keyword-selection/create", "quote.create", "quote", "create"),
    _p("POST", "/api/keyword-selection/{token}/generate-quote", "quote.create", "quote", "update", billing="quote_generate"),
    _p("DELETE", "/api/keyword-selection/{token}/keywords/{keyword_id}", "quote.create", "quote", "update"),
    _p("POST", "/api/keyword-selection/{token}/audit-keyword/{keyword_id}", "quote.create", "quote", "update"),
    _p("POST", "/api/keyword-selection/{token}/edit-cluster", "quote.create", "quote", "update"),
    _p("GET", "/api/keyword-selection/{token}/analysis", "quote.read_own", "quote", "detail"),
    _p("GET", "/api/keyword-selection/{token}/order-details", "quote.read_own", "quote", "detail"),

    # Writing output.  Mutation routes enter this table only with organization
    # pre-reservation and artifact stamping in their live handlers.
    _p("GET", "/api/writing/projects", "writing.read_own", "quote", "list"),
    _p("GET", "/api/writing/projects/{quote_id}", "writing.read_own", "quote", "detail"),
    _p("GET", "/api/writing/projects/{quote_id}/archive-preview", "writing.read_own", "quote", "detail"),
    _p("DELETE", "/api/writing/projects/{quote_id}", "team.output_handoff", "quote", "archive"),
    _p("POST", "/api/writing/projects/{quote_id}/restore", "team.output_handoff", "quote", "restore"),
    _p("GET", "/api/writing/projects/{quote_id}/knowledge-status", "writing.read_own", "quote", "detail"),
    _p("GET", "/api/writing/projects/{quote_id}/direction-plan", "writing.read_own", "quote", "detail"),
    _p("GET", "/api/writing/progress/{quote_id}", "writing.read_own", "article", "status", async_surface="poll"),
    _p("GET", "/api/articles/{article_id}", "writing.read_own", "article", "detail"),
    # [WO_MEDIA_BOARD_UX_CLOSURE §2] 「N 条提示」明细(只读)。与文章详情同一档能力:
    # 它读的就是这篇文章的机审提示,能看详情就能看提示。不在这张表里 = 员工席位
    # fail-closed 打不开抽屉,徽章又变回"点了没反应"。
    _p("GET", "/api/articles/{article_id}/advisory", "writing.read_own", "article", "detail"),
    _p("PUT", "/api/articles/{article_id}", "writing.generate", "article", "update"),
    _p("POST", "/api/topics/{topic_id}/mark-reviewed", "writing.read_own", "article", "update"),
    _p("PUT", "/api/writing/topics/{topic_id}", "writing.generate", "quote", "update"),
    _p("POST", "/api/writing/projects/{quote_id}/apply-distribution", "writing.generate", "quote", "update"),
    _p("POST", "/api/writing/projects/{quote_id}/reset-to-recommended", "writing.generate", "quote", "update"),
    _p("POST", "/api/writing/generate-titles", "writing.generate", "quote", "update", billing="topic_gen"),
    _p("POST", "/api/writing/start-articles", "writing.generate", "article", "create", billing="article_gen", async_surface="thread"),
    # [发布面 · Owner 2026-07-25 拍板"默认员工直接发,做成开关"]
    # 此前交付角色持有 publish.* 与 writing.review,却**没有任何可达路由** ——
    # 员工写得出文章却发不出去,交付岗等于干不了本职工作。
    # 选品/看榜是只读规划(publish.plan);真正下单发布走 publish.execute,
    # 端点内再问一次组织审批开关(organization_approval_policies)决定直发还是转审批。
    _p("GET", "/api/meijiehezi/media", "publish.plan", "media", "list"),
    _p("GET", "/api/meijiehezi/media/filters", "publish.plan", "media", "list"),
    _p("GET", "/api/meijiehezi/media/by-engine", "publish.plan", "media", "list"),
    _p("GET", "/api/meijiehezi/media/recommend", "publish.plan", "media", "list"),
    _p("GET", "/api/meijiehezi/media/recommend-v2", "publish.plan", "media", "list"),
    _p("GET", "/api/meijiehezi/orders", "publish.plan", "publish_order", "list"),
    _p("GET", "/api/meijiehezi/orders/{order_id}", "publish.plan", "publish_order", "detail"),
    # 🔴 [窗G 段二③ · Review 2026-08-24 裁定条件①] 声明值与 handler **真正扣的**码对齐。
    #    这两条路由的 handler(api/meijiehezi_api.py:939 与 :1723)扣的一直是
    #    ``media_proxy_publish``;这里原本声明的是另一个码,声明与实扣打架。
    #    ⚠️ 这是**零行为变更**:见 MemberRoutePolicy.billing_feature 的字段注释 ——
    #       这一格现无运行期消费方,组织预算走 handler 显式传参。改它是为了
    #       "以后有人接线时拿到的是真值",不是为了改变现在的任何一分钱。
    #    资金 SSOT:docs/SYSTEM_TRUTH/08_billing.md §3.2(功能消耗目录 ≠ 现金售价)。
    _p("POST", "/api/meijiehezi/publish", "publish.execute", "publish_order", "create", billing="media_proxy_publish"),
    _p("POST", "/api/meijiehezi/publish/batch", "publish.execute", "publish_order", "create", billing="media_proxy_publish"),
    # 写作审阅:交付角色持有 writing.review,同样此前无路由
    _p("POST", "/api/articles/{article_id}/review", "writing.review", "article", "update"),

    # ─────────────────────────────────────────────────────────────
    # GEO 图文(抖音图文笔记)交付面 · 裁定 2026-08-17 P0-7
    #
    # 未登记的路径在守卫层就是 fail-closed:交付操作员会吃 403
    # ORG_ROUTE_NOT_CLASSIFIED —— 界面做得再好也点不动(本表 [F-1] 那一格记过同样的病)。
    # 所以「新增端点同步登记」是交付的一部分,不是收尾工作。
    #
    # 能力归档:读计划 = writing.read_own(交付/销售三档模板都持有,存量角色无需补授权);
    # 真正产生制作/资金动作的写端点走 writing.generate,发布走 publish.execute
    # —— 与既有 /api/writing/start-articles、/api/meijiehezi/publish 同档,不新造能力名。
    #
    # 🔴 负向锁(Owner 2026-08-17 拍板 · memory:feedback_org_seats_forever_exclude_social):
    #    **员工席位永不含社媒板块**。本组端点全属 GEO 板块(图文制作与发布是服务商内部
    #    GEO 交付能力),登记进 MEMBER_GEO_ROUTE_POLICIES 合规;
    #    任何往这张表里塞 social/社媒工作台 路由的变更一律 NO-GO。
    # ─────────────────────────────────────────────────────────────
    _p("GET", "/api/geo-douyin/quotes/{quote_id}/delivery-plan",
       "writing.read_own", "quote", "detail"),
    # 制作侧(writing.generate)· billing 绑既有 feature code,不新造 SKU
    _p("POST", "/api/geo-douyin/production-preview",
       "writing.generate", "quote", "detail"),
    _p("PUT", "/api/geo-douyin/production-drafts/{draft_id}",
       "writing.generate", "batch", "update"),
    _p("POST", "/api/geo-douyin/batches",
       "writing.generate", "batch", "create",
       billing="geo_douyin_image_post", async_surface="task"),
    _p("GET", "/api/geo-douyin/batches/{batch_id}",
       "writing.read_own", "batch", "status", async_surface="poll"),
    # 投放侧(publish.execute)· 与既有 /api/meijiehezi/publish 同档,不新造能力
    _p("POST", "/api/geo-douyin/posts/{post_id}/prepare-publish-media-v2",
       "publish.execute", "post", "update"),
    _p("POST", "/api/meijiehezi/image-notes/publish-preview",
       "publish.execute", "publish_order", "detail"),
    _p("POST", "/api/meijiehezi/image-notes/publish-batch",
       "publish.execute", "publish_order", "create",
       billing="media_proxy_publish", async_surface="task"),

    # Monitoring configuration/read paths and the live pre-reserved runner.
    _p("GET", "/api/monitoring/platforms", "monitoring.read_assigned", "monitoring_task", "list"),
    _p("GET", "/api/monitoring/keywords", "monitoring.read_assigned", "monitoring_task", "list"),
    _p("GET", "/api/monitoring/keywords/{keyword_id}", "monitoring.read_assigned", "monitoring_task", "detail"),
    _p("GET", "/api/monitoring/tasks", "monitoring.read_assigned", "monitoring_task", "list"),
    _p("GET", "/api/monitoring/tasks/{task_id}", "monitoring.read_assigned", "monitoring_task", "detail"),
    _p("GET", "/api/monitoring/clients", "monitoring.read_assigned", "quote", "list"),
    _p("GET", "/api/monitoring/clients/{quote_id}/keywords", "monitoring.read_assigned", "quote", "detail"),
    _p("GET", "/api/monitoring/trend", "monitoring.read_assigned", "monitoring_report", "list"),
    _p(
        "GET", "/api/monitoring/identity-reviews",
        "monitoring.read_assigned", "monitoring_task", "list",
    ),
    _p(
        # [工单 2026-08-03 ①] 「查看完整回答」·只读,与列表同权限档(read_assigned)
        "GET", "/api/monitoring/identity-reviews/{result_id}/full-response",
        "monitoring.read_assigned", "monitoring_task", "detail",
    ),
    _p(
        "POST", "/api/monitoring/identity-reviews/{result_id}/decision",
        "monitoring.run", "monitoring_task", "update",
    ),
    _p("POST", "/api/monitoring/run", "monitoring.run", "monitoring_task", "create", billing="monitor_single"),
    _p(
        "POST",
        "/api/monitoring/tasks/{task_id}/cells/{cell_id}/retry",
        "monitoring.retry",
        "monitoring_task",
        "update",
    ),

    # Internal reports only. Legacy send remains unclassified; public issuance
    # must go through organization approval and server-owned white-label data.
    _p("POST", "/api/reports/generate", "reports.generate", "monitoring_report", "create"),
    _p("GET", "/api/reports", "reports.read_own", "monitoring_report", "list"),
    _p("GET", "/api/reports/all", "reports.read_own", "monitoring_report", "list"),
    _p("GET", "/api/reports/{report_id}", "reports.read_own", "monitoring_report", "detail"),
    _p("GET", "/api/reports/{report_id}/download", "reports.export", "monitoring_report", "export"),
    _p("GET", "/api/reports/{report_id}/export/pdf", "reports.export", "monitoring_report", "export"),
    _p("GET", "/api/reports/{report_id}/export/excel", "reports.export", "monitoring_report", "export"),
)


def match_member_geo_route(method: str, path: str) -> Optional[MemberRoutePolicy]:
    normalized_method = str(method).upper()
    for policy in MEMBER_GEO_ROUTE_POLICIES:
        if policy.method == normalized_method and policy.compiled.fullmatch(path):
            return policy
    return None


def route_contract_manifest() -> list[dict]:
    return [policy.as_contract() for policy in MEMBER_GEO_ROUTE_POLICIES]
