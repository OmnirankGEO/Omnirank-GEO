"""
API 路径 → 权限模块映射
用于全局中间件的权限解析

核心规则：
1. ROUTE_PREFIX_MAP 按最长前缀优先排序
2. None = 仅需认证，不需特定模块权限
3. "__unmapped__" = 默认拒绝（只有 admin 可访问）
4. POST_READ_OVERRIDES = POST 方法但实际是只读操作的端点
"""

from typing import Optional

# 路径前缀 → 模块ID 映射表
# None 表示仅需认证（无模块权限要求）
ROUTE_PREFIX_MAP = [
    # (前缀, 模块ID) — 按最长前缀优先排序
    # None = 仅需认证，不需特定模块权限
    ("/api/auth/",          None),           # 认证 API
    ("/api/organization/",  None),           # 组织席位（端点内四层 capability + assignment 校验）
    ("/api/organization",   None),           # 兼容无斜杠
    ("/api/demo-cases/",    None),           # 演示客户（端点内逐请求 live grant + 404 防枚举）
    ("/api/demo-cases",     None),           # 兼容无斜杠
    # ===== 防御型 GEO v2(三个 router,全部映射 diagnosis)=====
    # [门三 G7 2026-08-22] 原来一条都没登记 → `resolve_permission` 落到
    # `__unmapped__` → **只有 admin 能用**。本仓记过这个形态:
    # 「未映射 auth/module_mapping.py = 付费功能只有 admin 能用」。
    #
    # 三个 router(判据从 router census 机械导出,不手抄):
    #   api/defensive_geo_api.py         prefix=/api/defensive-geo
    #   api/defensive_geo_report_api.py  prefix=/api/defensive-geo
    #   api/defensive_publish_api.py     prefix=/api/defensive-geo/publish
    #
    # 🔴 两条都要:`resolve_permission` 是**列表顺序首个 startswith 命中**
    #    (文件头那句"最长前缀优先"是人工约定,不是运行时行为),
    #    带斜杠那条覆盖全部子路径,不带斜杠那条兜住裸前缀。
    #    publish 前缀被 `/api/defensive-geo/` 包住,同映射 diagnosis,无需单列。
    ("/api/defensive-geo/",  "diagnosis"),
    ("/api/defensive-geo",   "diagnosis"),
    ("/api/admin/",         "users"),         # 用户管理
    ("/api/portal/",        None),           # Portal 独立体系
    ("/api/client-context/",None),           # 全局客户切换（已有数据隔离）
    # ===== C 端自助业务路由（数据隔离由 user_id / brand_id 保障）=====
    ("/api/wallet/",        None),            # 个人钱包（充值/扣费/余额查询/交易明细）
    ("/api/wallet",         None),            # 兼容无斜杠
    ("/api/referral/",      None),            # 个人推荐体系（推荐码/佣金/分成）
    ("/api/referral",       None),            # 兼容无斜杠
    ("/api/customer/",      None),            # 普通用户工具额度/购买额度（端点内按 user_id / binding 隔离）
    ("/api/customer",       None),            # 兼容无斜杠
    # 双价目表 SSOT：零售、进货、报价和管理端点均在 pricing_ssot_api 内按
    # 登录身份、经销商等级、买方归属或 admin 角色继续做细粒度校验。
    # 全局中间件只负责让已认证请求进入端点，避免合法经销商被 UNMAPPED_ROUTE 拦截。
    ("/api/pricing/",       None),
    ("/api/pricing",        None),
    # 逐级库存转售：端点内对 buyer/seller/admin 做一跳归属校验；全局层只放行已认证主体。
    ("/api/dealer-resale/", None),
    ("/api/dealer-resale",  None),
    # [P1-17 fix 2026-05-23 老板授权红线] L2 服务费 v3.3.1
    # 原 __unmapped__ → admin 才能用 · L2 代理 403 不能查服务费
    # 端点内 is_l2_agent() + is_v3_3_1_enabled() 已做 L2 + flag 双校验
    # 中间件层放行到 None 让认证用户进入 · 鉴权交端点
    ("/api/service-fee/",   None),            # L2 服务费(端点内 is_l2_agent 兜底)
    ("/api/service-fee",    None),            # 兼容无斜杠
    ("/api/share/",         None),            # 分享海报/二维码/短链
    ("/api/share",          None),            # 兼容无斜杠
    # [返工 R3 · 2026-07-05 老板批红线例外] 营销物料工厂用户端(用户花自己算力做物料)
    # 中间件层放行到 None 让认证用户进入 · 端点内 user_id 归属隔离 + brand owner 校验
    # admin 军师端点走 /api/admin/marketing(→ /api/admin/ 映射 users 模块 + 端点内 _require_admin 双闸)
    #
    # 🔴🔴🔴 [P0 IDOR 热修 2026-08-10] 下面这条**必须排在 `/api/marketing` 之前**。
    #   `resolve_permission()` 是 **列表顺序优先**(第一个 startswith 命中就 return),
    #   本文件头部那句"按最长前缀优先排序"是**人工维护的约定,不是运行时行为**。
    #   原来没有这一条时,`/api/marketing-confirm/status/{brand_id}` 被下面的
    #   `("/api/marketing", None)` 前缀吞掉 → 静默降级为"仅需登录",
    #   而端点函数体当时零鉴权 → 任意登录用户可枚举任意品牌的确认 token(生产实测 200)。
    #   现在显式登记:效果与今天一致(None = 仅需认证),但**是有意为之而不是被前缀捡漏**,
    #   且 `/api/marketing` 将来收紧/放宽都不会再意外改变本路由的判定。
    #   为什么是 None 而不是某个具体模块:同类品牌资源就是这个口径 ——
    #   `("/api/brands", None)` 注释原文"数据隔离由 get_user_brand_filter/require_brand_access 保障"。
    #   在 P0 热修里把它改成具体模块 = 一次活的授权变更,会把没有该模块的合法服务商挡在门外
    #   (本仓已有前科:"未映射进 module_mapping = 付费功能只有 admin 能用")。
    #   真正的闸在端点级 `_require_brand_owner` → `require_brand_access`(非归属抛 404)。
    #   顺序被改回去时会转红:tests/mcidor_2026_08_10 的映射元判据锁。
    ("/api/marketing-confirm/", None),        # 营销资料确认(销售端 3 端点 · 端点内 require_brand_access 归属隔离)
    ("/api/marketing-confirm",  None),        # 兼容无斜杠
    ("/api/marketing/",     None),            # 营销物料工厂(模板/生成/任务/物料库/举报)
    ("/api/marketing",      None),            # 兼容无斜杠
    ("/api/geo-observation/", None),           # GEO 统一观测(端点内品牌归属/k 匿名隔离)
    ("/api/geo-observation",  None),           # 兼容无斜杠
    ("/api/my-",            None),            # 所有 /api/my-* 个人资源 (my-stats, my-brand, my-*)
    ("/api/user/",          None),            # /api/user/* 个人资源 (home-stats, notifications, etc)
    ("/api/meijiehezi/",    "writing"),       # 媒介盒子全部（媒体列表/下单/同步）统一 writing 权限
    # [WO_273 · 2026-09-23] 浏览器插件后端整体退役,原「仅需认证」那一条随之删除:
    #   退役路径落回默认拒绝(非 admin 403 / admin 404),回来要重新映射 + 复审。
    ("/api/publish/",       "writing"),       # 发布中心全部（媒体/订单/记录）统一 writing 权限
    ("/api/publish",        "writing"),       # 兼容无斜杠
    ("/api/monitoring/",    "monitoring"),
    ("/api/notifications",  None),            # 站内通知（个人数据，数据隔离由 user_id 保障）
    ("/api/faq/",           None),            # 帮助中心 FAQ · 所有登录用户都能看 · admin 写
    ("/api/faq",            None),            # 兼容无斜杠 (GET /api/faq/items)
    ("/api/admin/faq/",     None),            # FAQ 后台管理 · faq_api.py 内部 _require_admin 把关
    ("/api/admin/faq",      None),            # 兼容无斜杠
    ("/api/xiaobang/",      None),            # 小榜 GEO 助手 · 所有登录用户都能用 · is_admin 在内部决定是否检索管理员节点
    ("/api/xiaobang",       None),            # 兼容无斜杠
    ("/api/help/",          None),            # 帮助中心文档正文 · faq_api 端点内按真实身份逐slug隔离(slug/acl 跨身份403)· Deploy-CTO 补漏 PR55
    ("/api/help",           None),            # 兼容无斜杠
    ("/api/publications",   "monitoring"),    # 发布管理
    ("/api/logs",           "monitoring"),    # 操作日志
    ("/api/alerts/",        "monitoring"),    # 告警检查
    ("/api/diagnosis",      "diagnosis"),
    ("/api/diagnos",        "diagnosis"),
    ("/api/opportunity/",   "diagnosis"),     # 商机管理
    ("/api/strategy/",      "diagnosis"),     # 策略生成
    # [Deploy-CTO 2026-06-02] 客户图库 image_asset_api(/api/brand-images/*)漏注册 → UNMAPPED_ROUTE 403
    #   非 admin 代理访问自己客户图库全 403(同 /api/products 2026-05-08 先例)· 1 行修 · 非红线 5 文件
    #   各端点(list/upload/asset/article-preview/image-action)内部均 require_brand_access 做 brand 隔离 → 此处 None 仅需认证
    ("/api/brand-images/",  None),            # 客户图库(图片素材)· 端点内 require_brand_access 隔离
    ("/api/brand-images",   None),            # 兼容无斜杠
    ("/api/brands",         None),            # 品牌列表/详情（数据隔离由 get_user_brand_filter/require_brand_access 保障）
    ("/api/brand/",         None),            # 品牌趋势（单数，同上）
    # [开源 E3 · B2 · 2026-09-28] 社媒资料三问 / 上传解析 / 画像记忆、/api/social、产品卡、调研、采访、AI 员工 / 会议 / 工作台 / 部门、
    #   竞品(复数)这些前缀下的在役路由已为 0(随 E3a/B1b/B3a/B3b 删除;对 import server 的全量路由实测),映射条目删。
    ("/api/profile",        None),             # 档案管理（社媒初始化需要，仅需认证；数据隔离由 brand_id 安全网保障）
    ("/api/content/",       "writing"),
    ("/api/articles",       "writing"),
    ("/api/knowledge",      "writing"),
    ("/api/material",       "writing"),
    ("/api/writing/",       "writing"),       # 写作大厅（含项目列表，数据隔离由 brand_filter 保障）
    # 🔴 [Deploy-CTO 2026-08-03] AI 创作中心「制作 GEO 图文」漏注册 → UNMAPPED_ROUTE 403。
    #   middleware 逻辑是「admin 直接放行 / 未映射一律拒」,所以这个 390 算力的付费功能
    #   **上线后只有 admin 能用**,而按用户定位铁律主用户是服务商。
    #   实测:qa_agent_2026 打 POST /api/geo-douyin/posts →
    #     {"detail":"未映射路由，需要管理员权限: /api/geo-douyin/posts","code":"UNMAPPED_ROUTE"}
    #   归到 writing 的依据:它就是 GEO 内容生产,亲兄弟是写文章链;
    #   019 定价即显式对齐 article_gen 390/3.00 与 article_rewrite 260/3.00 —— 计费同族,权限同族。
    #   不用 None(=登录即可):这是付费功能,最小权限应与另一付费内容生产功能同门。
    #   端点内已有隔离:require_brand_access + _require_post_access +
    #   list_posts 的 created_by=None if is_admin else user_id(外门=模块权限,内门=归属)。
    #   ⚠️ 已知局限:若某服务商持 GEO 类权限却无 writing 模块权限,此映射仍会挡住他。
    #      Owner 授权 Deploy 决定,但该边界请 Review 明确裁一次。
    ("/api/geo-douyin/",    "writing"),       # AI 创作中心 · GEO 图文制作
    ("/api/geo-douyin",     "writing"),       # 兼容无斜杠
    ("/api/placement/",     "writing"),       # 投放建议/文章列表（数据隔离由 require_quote_access 保障）
    ("/api/topics/",        "writing"),       # 选题管理
    ("/api/references/",    "writing"),       # 参考文章
    ("/api/references", "writing"),  # [WO_287] 无斜杠集合路由(GET/POST /api/references)原先只登记了带斜杠前缀 ⇒ __unmapped__ ⇒ 只 admin 能用
    ("/api/imitation-records/", "writing"),   # 仿写记录
    ("/api/imitated-articles/", "writing"),   # 仿写文章
    ("/api/keyword-selection/", "quote"),      # 选词报价互动链接（销售端）
    ("/api/keywords/",      "quote"),         # 关键词扩展
    ("/api/operation",      "social"),
    ("/api/quote",          "quote"),
    ("/api/reports",        "reports"),
    ("/api/report/",        "reports"),       # 报告查看（单数）
    ("/api/advisors",       "social"),         # 顾问系统（社媒操盘手核心功能）
    # ⚠️ [fix/security-audit-p0 #15] 老 bug 修复：
    #   line 旧写法 ("/api/competitor", "ai_agents") 没带斜杠，前缀匹配会
    #   抢占 /api/competitors（复数），导致下方 line ~102 的 ("/api/competitors/", "writing")
    #   永远轮不到，普通用户根本用不了竞品功能（即使有 writing 权限也被 ai_agents 拦）
    #   修复：改成带斜杠的精确前缀，跟 /api/competitors/* 完全分开
    ("/api/competitor/",    "ai_agents"),     # AI 员工里的竞品对话功能（单数 + 斜杠）
    ("/api/tasks/",         "ai_agents"),     # 任务执行
    ("/api/settings",       "settings"),
    ("/api/llm",            "settings"),
    ("/api/models",         "settings"),      # 模型列表
    ("/api/scheduler/",     "settings"),      # 调度器管理
    ("/api/statistics",     None),            # 仪表盘统计（着陆页，所有用户可访问）
    ("/api/dashboard",      None),            # 仪表盘数据（着陆页，所有用户可访问）
    ("/api/history",        None),            # 历史记录（着陆页需要，数据按品牌过滤）
    ("/api/insights",       "insights"),
    ("/api/distillation",   "insights"),
    ("/api/distill/",       "insights"),      # 蒸馏操作
    ("/api/upload",         None),            # 文件上传（仅需认证）
    ("/api/agent/",         None),            # AI 助手 Agent（仅需认证）
    ("/api/agent",          None),            # 兼容无斜杠
    ("/api/c-end/",         None),            # C 端对话 API（仅需认证，v3.2 C 端入口）
    ("/api/c-end",          None),            # 兼容无斜杠
    # [WO_286] WO_267 行业大类字典(只读 GET,内容是产品分类表、不含租户数据)漏登记 ⇒ 非管理员 403 UNMAPPED_ROUTE,
    #   服务商品牌页的大类下拉取不到。仅需认证;判据 tests/industry_taxonomy_2026_09_23/test_route_mapped_for_non_admin.py
    ("/api/industry-taxonomy", None),         # 行业大类字典(仅需认证)
    # v3.2/v3.3 新路由（2026-04-17 P0-L: 原先全部 __unmapped__ 导致非 admin 403 全炸）
    ("/api/managed/",       None),            # v3.3 GEO 托管（数据隔离: require_brand_access + owner 校验）
    ("/api/managed",        None),            # 兼容无斜杠
    ("/api/trial-pass/",    None),            # v3.2 代理试用通行证（数据隔离: user_id）
    ("/api/trial-pass",     None),            # 兼容无斜杠
    ("/api/partner/",       None),            # v1.1 代理审核制自助端点（/flag 走 PUBLIC_PATHS；其余 9 个端点数据隔离: user_id / app_id owner 校验）
    ("/api/partner",        None),            # 兼容无斜杠
    ("/api/geo-plan/",      None),            # C 端 GEO 方案工具（数据隔离: user_id）
    ("/api/geo-plan",       None),            # 兼容无斜杠
    ("/api/materials/",     None),            # v3.4 物料中心（数据隔离: require_profile_access）
    ("/api/materials",      None),            # 兼容无斜杠
    ("/api/pay/",           None),            # 支付回调（虎皮椒 webhook 无需权限，签名校验在端点）
    ("/api/pay",            None),
    ("/api/tv/",            None),            # TV 大屏监控（仅需认证，endpoint 内部校验权限）
    # ===== M3 BFF (CTO bugfix 2026-04-27) =====
    # 修 fix/integration-r3-browser-p1: M3 GET 端点对合法 agent_level=2 代理误返
    # 403 UNMAPPED_ROUTE。M3 所有端点已在端点内做 brand 隔离 (owner_user_id /
    # client_brand_ids / require_brand_access)，全局只需认证；跨品牌访问由
    # middleware._extract_brand_id 安全网 + 端点 require_brand_access 双保险拦截。
    ("/api/m3/",            None),            # M3 销售/交付/客户工作台 BFF
    ("/api/m3",             None),            # 兼容无斜杠
    ("/api/analytics/",     None),            # D.10 自然迁移率埋点 · 仅需认证
    ("/api/analytics",      None),            # 兼容无斜杠
    # 2026-05-14 老板真机报"未映射路由 /api/subscription/checkout":
    # V3.1 订阅系统整套子模块从未注册到 module_mapping → 非 admin 全部 403 UNMAPPED_ROUTE
    # 业务隔离均在 endpoint 内做(get_active_subscription_full + user_id 校验)· 此处仅需认证
    # [开源 E3 · B4 · 2026-09-28] 订阅产品面随社媒删除(E3_DELETION_MAP §5.3):/api/subscription 下只剩 /health,
    #   它走 auth/middleware.py PUBLIC_PATHS;订阅支付(/api/subscription-pay)整组删除 ⇒ 两个前缀的映射行都已无路由可配,去掉。
    #   原注释「微信 / 虎皮椒 callback 在 PUBLIC_PATHS」与实情不符(生产 05-15 两条微信通知全部 401),一并删。
]

# POST 方法但实际是只读操作的端点
# 这些端点虽然用 POST 发送请求体（搜索条件、文件上传等），但不修改数据
POST_READ_OVERRIDES = {
    "/api/knowledge/search",
    "/api/articles/content",
    "/api/articles/download-zip",
    "/api/profiles/parse-uploaded-file",
    "/api/diagnosis/autofill",
}

# 动态路径的 POST 只读模式（用后缀匹配）
POST_READ_SUFFIX_OVERRIDES = {
    "/knowledge/search",       # 客户知识库检索是只读
}

# DELETE 方法但只需 write 权限的端点（用户删除自己的数据）
# 这些端点后端已通过 user_id 校验确保只能删自己的资源，不需要管理员级 delete 权限
DELETE_WRITE_OVERRIDES = (
    "/api/knowledge/client/",           # 用户删除自己有权访问的客户私有知识库文件（端点内 require_brand_access 校验）
    # [开源 E3 · B2 · 2026-09-28] 社媒语料 / 顾问对话 / 采访会话 / 创意工坊对话 / 文案 / 选题 / 画像记忆 / 社媒任务 / 团队九条的 DELETE 端点已删,条目删。
    "/api/profiles/",                   # 用户删除自己的档案（回收站）
    "/api/keyword-selection/",          # CTO-15.23 2026-05-13: 代理删自己客户的报价方案 (端点内 require_brand_access 做品牌归属校验)
    "/api/writing/projects/",           # CTO-15.23 2026-05-14: 代理软删自己客户的写作项目 (server.py:14520 · 端点内 require_quote_access 做品牌归属校验)
    "/api/writing/topics/",             # CTO-15.23 2026-05-14: 代理删自己的选题 (server.py:15350 · 端点内 status='writing' 保护 + 登录校验)
    "/api/writing/competitors/",        # CTO-15.23 2026-05-14: 代理清空竞品列表切换编造模式 (server.py:17508)
    "/api/operation-packages/",         # [2026-06-17] 服务商删自己的经营包配置(端点内 _require_agent + owner_user_id 归属校验 + status='deleted' 软删)· 非系统级 delete · 全局 RBAC 只需 write
)


def resolve_permission(path: str) -> Optional[str]:
    """
    从 API 路径解析出所需的模块 ID
    
    Returns:
        - 模块 ID 字符串 (如 "writing") — 需要对应的 module:level 权限
        - None — 仅需认证，不需特定权限
        - "__unmapped__" — 未映射路由，默认拒绝（只有 admin 可访问）
    """
    for prefix, module in ROUTE_PREFIX_MAP:
        if path.startswith(prefix):
            return module
    return "__unmapped__"  # 默认拒绝


def is_post_read_override(path: str) -> bool:
    """判断该 POST 端点是否实际为只读操作"""
    if path in POST_READ_OVERRIDES:
        return True
    return any(path.endswith(suffix) for suffix in POST_READ_SUFFIX_OVERRIDES)


def get_required_level(method: str, path: str) -> str:
    """
    根据 HTTP 方法和路径确定所需的权限级别
    
    Returns: "read" | "write" | "delete"
    """
    # POST 只读例外
    if method == "POST" and is_post_read_override(path):
        return "read"

    # DELETE 但只需 write 权限（用户删除自己的资源）
    if method == "DELETE" and any(path.startswith(p) for p in DELETE_WRITE_OVERRIDES):
        return "write"

    return {
        "GET": "read",
        "POST": "write",
        "PUT": "write",
        "PATCH": "write",
        "DELETE": "delete",
    }.get(method, "read")
