"""
全局认证中间件
拦截所有 HTTP 请求，进行认证和权限检查

注意：WebSocket 不经过 HTTP 中间件，需在 endpoint 内手动校验
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from auth.jwt_utils import decode_jwt
from auth.module_mapping import resolve_permission, get_required_level
from auth.perm_cache import get_cached_permission_version
from cache.redis_client import redis_get_json, redis_set_json

import asyncio
import time
import logging
logger = logging.getLogger("GEO-Auth")

# 在线看板用：user_id → 上次写 DB 的时间戳（30 秒内存去重，每 worker 独立）
_last_active_cache: dict = {}
_LAST_ACTIVE_TTL = 30  # 秒


# [Deploy-CTO NO-GO finding 4 v3] 软刷新失败不再用"清空 permissions"的假 fail-closed(auth-only 路由挡不住),
# 改为在中间件里直接返回 503 AUTH_REFRESH_UNAVAILABLE(见下方 dispatch · 绝不 call_next)。原 _failclosed_fallback 已废弃删除。

# 白名单：不需要认证的精确路径
PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/login-sms",    # 短信登录（登录前调用）
    "/api/auth/registration-agreements/accept",  # 仅接受短期补签凭证，endpoint 内独立验签
    "/api/auth/register",
    # [refchain 2026-08-05 · Owner 授权] 扫码归因端点。注册【前】调用,此刻用户还没有 JWT。
    #   🔴 PUBLIC_PATHS 是精确匹配的 set —— 上面那条 "/api/auth/register" 覆盖不到
    #      "/api/auth/register/attribution",多一段路径就不相等,于是被中间件 401,
    #      端点代码一行都执行不到 → 扫码归因功能等于没上线(生产实证 2026-08-05)。
    #   🔴 同型事故本文件里已有一次:wechat-refund-callback「原缺此白名单致回调 401 退款不到账」。
    #   安全面 Review 已逐行核:绝不回推荐码原文 / display_name 缺失不裸手机号 /
    #      cookie 值消毒 alnum≤32 / 恒 200 不阻断。
    "/api/auth/register/attribution",
    "/api/auth/captcha",      # 图形验证码（登录前调用）
    "/api/auth/send-sms",     # 发送短信（注册/登录前调用）
    "/api/tv/dashboard",      # TV 大屏 token 模式（endpoint 内部有自己的 token 校验）
    "/api/extension/bindcode/verify",  # 扩展绑定码验证（未登录状态调用）
    "/api/extension/config",           # 扩展平台配置下发（扩展启动时无 token 也需拉取）
    "/api/wallet/wechat-callback",     # 微信支付异步回调（第三方 POST，无法带 JWT）
    "/api/wallet/wechat-refund-callback",  # [GEO-R1-CAN-061 老板批] 微信退款异步回调（第三方 POST 无 JWT · handler 内 V3 验签）· 原缺此白名单致回调 401 退款不到账
    "/api/wallet/xunhupay-callback",   # 虎皮椒聚合支付异步回调（第三方 POST，endpoint 内部 MD5 验签）
    "/api/subscription/plans",         # 订阅套餐公开价目表（仅 plans 公开，其它 subscription API 仍需 JWT）
    "/api/subscription/health",        # V3.1 部署自检 (endpoint docstring 明示 Deploy-CTO 一行 curl 验链路)
    "/api/subscription/addons",        # 增量包公开价目表 (endpoint docstring 明示公开 · OverrunDialog 未登录态调用)
    "/api/partner/flag",               # v1.1 审核制 feature flag 探测（未登录 UI 需用于判断是否隐藏老代理入口）
    "/api/m3/customer-events/public",  # M3 公开 token 链路埋点（endpoint 内部做 token/report 反查）
    "/api/operation-packages/public",  # 报价经营包客户公开视图（仅客户白名单字段·无成本/毛利/系数·登录态 /api/operation-packages 仍需 JWT）
    "/docs",
    "/openapi.json",
    "/redoc",
    "/health",
    "/",              # 前端静态资源
    "/favicon.ico",
}

# 白名单：不需要认证的路径前缀
PUBLIC_PREFIXES = (
    "/api/portal/",    # Portal 有独立 Token 体系
    "/api/s/",         # 选词报价互动页（客户通过 token 访问）
    "/api/sl/",        # 短链重定向（分享海报二维码）
    "/api/m/",         # 营销资料确认页（客户通过 token 访问）
    "/api/public/",    # 公开页面（报告分享等）
    "/static/",        # 静态资源
    "/assets/",        # 前端资源
)

# /api/portal/ 前缀里需要走 JWT 鉴权(代理操作端点 · 不是客户公开链路)
# CTO-15.9 commit 4770a3f 给 POST/GET /api/portal/tokens 加了
# require_quote_access · 但全局 PUBLIC_PREFIXES 仍把整个 /api/portal/ 放行
# 导致 request.state.user=None · 端点恒 401。这里把代理鉴权类 portal 端点
# 排除在公共前缀外 · 让 JWT 走完(by-brand 之前已单独排除 · 现统一收拢)
PORTAL_PROTECTED_PREFIXES = (
    "/api/portal/tokens",  # 代理生成/查询客户门户 token(POST · GET 含 by-brand · GET {quote_id})
)


def _is_portal_protected(path: str) -> bool:
    """portal/ 前缀内需要 JWT 的子路径(代理端点)"""
    return any(
        path == p or path.startswith(p + "/")
        for p in PORTAL_PROTECTED_PREFIXES
    )

# 白名单：基于后缀的免鉴权（常用于 <img>/<audio> 等无法携带 Bearer Token 的资源请求）
PUBLIC_SUFFIXES = (
    "/avatar-image",   # 顾问头像 (img 标签)
    "/avatar.jpg", "/avatar.png", "/avatar.webp",
)


def setup_auth_middleware(app):
    """
    将认证中间件注册到 FastAPI app
    
    调用方式（在 server.py 中 CORS 中间件之后）：
        from auth.middleware import setup_auth_middleware
        setup_auth_middleware(app)
    """

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method

        # 1. 白名单放行
        # /api/portal/tokens* 是代理操作端点(by-brand/ 含 quote_id 含本身 POST)
        # 走 JWT + require_quote_access/require_brand_access · 不能走 portal 公共前缀放行
        if path in PUBLIC_PATHS or (path.startswith(PUBLIC_PREFIXES) and not _is_portal_protected(path)):
            return await call_next(request)

        # 后缀白名单（资源类请求，<img>/<audio> 无法携带 Token）
        # [GEO-R1-CAN-031 老板批] 仅对 GET/HEAD 放行:原 method 无关 + 在鉴权前判,
        # 任意 mutating(POST/DELETE/PUT)到以 /avatar-image 等后缀结尾的路径可绕过全部鉴权。
        # 头像资源本就只需 GET/HEAD,收窄即堵 mutating 绕权。
        if method in ("GET", "HEAD") and any(path.endswith(sfx) for sfx in PUBLIC_SUFFIXES):
            return await call_next(request)

        # OPTIONS 预检请求放行（CORS）
        if method == "OPTIONS":
            return await call_next(request)

        # 静态资源放行（前端路由 fallback）
        # v1_3 (CTO-15.1 2026-04-19 Round 2 P1 修复):
        # /api/* 路径永远走鉴权，不当静态资源。原规则 "最后一段含点 → 放行"
        # 会误伤 /api/partner/agreement/v2.0 (路径含 v2.0 的点) 被跳过鉴权 → 端点返 401
        if not path.startswith("/api/") and "." in path.split("/")[-1]:
            return await call_next(request)

        # 2. 提取 Bearer Token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录", "code": "NOT_AUTHENTICATED"}
            )

        token = auth_header[7:]

        # 2.1 门户Token快速检查 —— 非JWT短Token走门户验证通道
        # [P0-2 fix 2026-05-23 老板授权] Codex 跨 AI 审计:
        #   原 PORTAL_ALLOWED_PREFIXES 以前缀匹配 · /api/reports 覆盖 /api/reports/all + /export + /generate + DELETE
        #   portal token 退化为"前缀通行证" · 可读全局 reports + 任意 keyword 趋势
        # 修法:
        #   1. 写方法 POST/PUT/DELETE/PATCH 一律拦(portal 只读)
        #   2. 黑名单优先:/api/reports/all + /export + /generate 即使前缀命中也拒绝
        #   3. 端点层 endpoint 仍需 require_brand_access(防 query 参数越界)
        PORTAL_ALLOWED_PREFIXES = (
            "/api/monitoring/clients/",
            "/api/monitoring/trend",
            "/api/publications/",
            "/api/reports",
            "/api/insights/",
        )
        PORTAL_BLOCKED_PATHS = (
            "/api/reports/all",
            "/api/reports/export",
            "/api/reports/generate",
        )
        if len(token) <= 20 and not token.startswith("eyJ"):
            # 短Token,可能是门户访问令牌
            # P0-2:写方法一律拦
            if request.method.upper() not in ("GET", "HEAD"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "门户 token 只读", "code": "PORTAL_READ_ONLY"}
                )
            # P0-2:黑名单优先
            if any(path.startswith(b) for b in PORTAL_BLOCKED_PATHS):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "门户 token 无权访问该资源", "code": "PORTAL_PATH_BLOCKED"}
                )
            if path.startswith(PORTAL_ALLOWED_PREFIXES):
                try:
                    from db.monitoring_db import verify_portal_token
                    portal_info = verify_portal_token(token)
                    if portal_info and portal_info.get("valid"):
                        # 门户Token有效，注入最小用户信息后放行
                        # 通过 quote_id 获取 brand_id，限制门户用户只能访问自己的品牌
                        portal_brand_ids = []
                        try:
                            from db.diagnosis_db import get_quote
                            quote = get_quote(portal_info["quote_id"])
                            if quote and quote.get("brand_id"):
                                portal_brand_ids = [quote["brand_id"]]
                        except Exception:
                            pass
                        request.state.user = {
                            "user_id": f"portal_{portal_info['quote_id']}",
                            "username": portal_info.get("brand_name", "portal_user"),
                            "is_admin": False,
                            "portal": True,
                            "quote_id": portal_info["quote_id"],
                            "client_brand_ids": portal_brand_ids,
                        }
                        return await call_next(request)
                except Exception as e:
                    logger.warning(f"门户Token验证异常: {e}")
            # 门户Token但路径不在允许列表 → 拒绝
            return JSONResponse(
                status_code=401,
                content={"detail": "Token 无效或已过期", "code": "INVALID_TOKEN"}
            )

        # 2.2 标准JWT验证
        payload = decode_jwt(token)
        if not payload:
            return JSONResponse(
                status_code=401,
                content={"detail": "Token 无效或已过期", "code": "INVALID_TOKEN"}
            )

        # 3. 校验 permission_version（60 秒缓存）+ 软刷新机制
        #
        # 修复 P1-3 (2026-04-08):
        # 原逻辑: perm_version 不匹配 → 直接 401 PERMISSION_CHANGED 踢下线
        # 问题: 诊断/品牌分配等后台任务会 bump perm_version,用户做完诊断 ~110s
        #      后缓存过期 → 被踢下线 → 前端 axios 能自动 refresh 但 CLI/脚本/
        #      未来 Agent 客户端全挂。L3 压测 68k 请求里 18k 都是这个原因的 401。
        #
        # 新逻辑: perm_version 不匹配 → "软刷新" 从 DB 重新加载用户数据注入
        #        request.state.user,token 本身不作废。权限变更的实际生效点是
        #        下游 RBAC 检查(基于 DB 里的真实数据),不是 JWT payload。
        #
        # 只有两种情况仍然硬 401:
        #  - 账户被禁用 (is_active=0) → 安全关键
        #  - /api/auth/refresh 本身的 perm check 被跳过(原有逻辑保留)
        skip_perm_check = path == "/api/auth/refresh"
        user_data_source = payload  # 默认用 JWT payload
        if not skip_perm_check:
            current_ver = get_cached_permission_version(payload["user_id"])
            if payload.get("perm_version") != current_ver:
                # Perm version mismatch — 软刷新: 从 DB 拉新用户数据注入 request.state
                try:
                    uid = payload["user_id"]
                    cache_key = f"user:{uid}"

                    # 先查 Redis 缓存
                    cached_user = redis_get_json(cache_key)
                    if cached_user and cached_user.get("permission_version") == current_ver:
                        fresh_user = cached_user
                    else:
                        from db.auth_db import get_user
                        fresh_user = get_user(uid)
                        if fresh_user:
                            # 缓存到 Redis（300s TTL）
                            redis_set_json(cache_key, dict(fresh_user), ex=300)
                    if not fresh_user:
                        return JSONResponse(
                            status_code=401,
                            content={"detail": "用户不存在", "code": "USER_NOT_FOUND"}
                        )
                    if not fresh_user.get("is_active", True):
                        return JSONResponse(
                            status_code=401,
                            content={"detail": "账户已被禁用", "code": "ACCOUNT_DISABLED"}
                        )
                    # 构造完整的 user dict 兼容原有 payload 结构
                    user_data_source = {
                        "user_id": fresh_user["id"],
                        "username": fresh_user["username"],
                        "display_name": fresh_user.get("display_name"),
                        "is_admin": fresh_user.get("is_admin", False),
                        "roles": fresh_user.get("roles", []),
                        "permissions": fresh_user.get("permissions", []),
                        "client_brand_ids": fresh_user.get("client_brand_ids", []),
                        "perm_version": fresh_user.get("permission_version", current_ver),
                        "must_change_password": fresh_user.get("must_change_password", 0),
                        # 保留 JWT 里的 team_context（如果有）
                        "team_context": payload.get("team_context"),
                    }
                    logger.debug(
                        f"[Auth] perm_version soft-refresh user={payload.get('username')} "
                        f"{payload.get('perm_version')}→{current_ver}"
                    )
                except Exception as e:
                    # v3.8 CTO-15.0 (2026-04-19)：软刷新失败兜底 — 不再降级 401 踢用户
                    # 老板反馈：PC 端 "用着用着就退出" 需重登录，但手机端同账号没事。
                    # 根因：Redis/DB 偶发抖动时软刷新抛异常 → 原逻辑降级 401 PERMISSION_CHANGED
                    # → 前端 interceptor refresh 走同路径再抖一次 → 跳 /login 踢人。
                    # 修：异常时用 JWT payload 继续（token 本身是有效的，权限变更下次
                    # 请求会命中缓存），高优先级 error 日志用来发现 Redis/DB 问题。
                    # 安全性不降：account_disabled 仍会在下次 cache miss 时被查出。
                    logger.error(
                        f"[Auth] soft-refresh 异常（兜底继续，不踢用户）: user={payload.get('username')} "
                        f"perm_version={payload.get('perm_version')}→{current_ver} err={e}"
                    )
                    # [GEO-R1-CAN-097 · Deploy-CTO NO-GO finding 4 v3 · 老板改判 fail-closed 2026-07-12]
                    # perm_version 已变但软刷新失败(Redis/DB 抖动)→ 旧 JWT 的 permissions/roles/
                    # client_brand_ids 已【全不可信】(可能刚被撤权)。
                    # 🔴 v2 曾靠 _failclosed_fallback 清空 permissions 假 fail-closed,但 required_module=None 的
                    #    【auth-only 路由】(/api/geo-plan、/api/managed、/api/subscription 等)只需登录不查
                    #    permission,permissions=[] 挡不住 → 仍放行 = 撤权后可用。
                    # 修:软刷新失败【直接返回 503,绝不 call_next】,挡住所有路由(含 auth-only)。
                    #    用 503(非 401)避免前端 interceptor refresh 重试环反复强制登出(保留会话);
                    #    下次请求 perm_version 若已刷新一致则正常通过,不一致再软刷新。
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": "权限校验暂不可用,请稍后重试",
                            "code": "AUTH_REFRESH_UNAVAILABLE",
                        },
                    )

        # 注意: 从此处往下用 user_data_source 而不是 payload
        # 因为如果发生了 soft-refresh, user_data_source 是从 DB 拉的新鲜数据

        # 4. 账户是否启用
        # （JWT 中不存 is_active，此处信任 JWT 有效期内账户状态不变）
        # 如需实时检查，可从 DB 查询，但会增加开销

        # 5. admin 直接放行
        if user_data_source.get("is_admin"):
            request.state.user = user_data_source
            return await call_next(request)

        # 6. 路径→模块映射 → 权限检查
        required_module = resolve_permission(path)

        # 未映射路由默认拒绝（只有 admin 可访问）
        if required_module == "__unmapped__":
            logger.warning(f"未映射路由被拒绝: {method} {path} (user={user_data_source.get('username')})")
            return JSONResponse(
                status_code=403,
                content={"detail": f"未映射路由，需要管理员权限: {path}", "code": "UNMAPPED_ROUTE"}
            )

        # None = 仅需认证，不需特定模块权限（如 /api/client-context）
        if required_module is None:
            request.state.user = user_data_source
            return await call_next(request)

        # 确定需要的权限级别
        required_level = get_required_level(method, path)
        required_perm = f"{required_module}:{required_level}"

        if required_perm not in user_data_source.get("permissions", []):
            logger.info(f"权限不足: {method} {path} 需要 {required_perm} (user={user_data_source.get('username')})")
            return JSONResponse(
                status_code=403,
                content={"detail": f"无权访问: {required_perm}", "code": "FORBIDDEN"}
            )

        # 7. 通过，注入用户信息到 request.state
        request.state.user = user_data_source

        # 7.5 在线看板：更新 last_active_at + current_path（30 秒内存去重，每 worker 独立）
        # ⚠️ 失败必须 rollback，否则脏连接归还池子污染所有查询
        try:
            uid = user_data_source.get("user_id")
            if uid:
                now_ts = time.time()
                if now_ts - _last_active_cache.get(uid, 0) > _LAST_ACTIVE_TTL:
                    from db.connection import get_connection
                    _conn = None
                    try:
                        _conn = get_connection()
                        _cur = _conn.cursor()
                        _cur.execute(
                            "UPDATE users SET last_active_at = NOW(), current_path = %s WHERE id = %s",
                            (path, uid),
                        )
                        _conn.commit()
                        _cur.close()
                        _last_active_cache[uid] = now_ts
                    except Exception as _inner_e:
                        # 字段不存在或其他 DB 错误：必须 rollback 避免脏连接
                        if _conn is not None:
                            try:
                                _conn.rollback()
                            except Exception:
                                pass
                        # 永久标记为"写失败"，避免每个请求都重试（用大 TTL）
                        _last_active_cache[uid] = now_ts + 3600
                        logger.debug(f"写 last_active_at 失败（已标记1小时不重试）: {_inner_e}")
                    finally:
                        if _conn is not None:
                            try:
                                _conn.close()
                            except Exception:
                                pass
        except Exception as _e:
            logger.debug(f"写 last_active_at 外层异常（忽略）: {_e}")

        # 8. 非管理员的 brand_id 数据隔离（全局安全网）
        # [audit P2 2026-06-10 红线批·老板已批] 判定升级为与 require_brand_access 同语义(分配列表 OR owner):
        #   旧版两缺陷:① 空 client_brand_ids 直接跳过(该字段是团队分配制,owner 直连用户常年为空 → 网形同虚设)
        #             ② 有分配的用户访问自己 owner 品牌被误 403(网只查分配不查 owner,与端点级语义打架)
        #   新版:提取到 brand 归属(query brand_id / quote_id / client_id 反查·见 _extract_brand_id)→
        #        分配列表内放行 → 否则 owner(60s 缓存·查询异常 fail-open 放行)→ 都不是才 403。
        #   网是第二道纵深(第一道=端点级 require_*_access),故 fail-open;不读 POST body(stream 消费风险)。
        #   ⚠️ [audit #6 返修 claim 更正] 本安全网只覆盖【query 参数】brand 向量(brand_id / query
        #     quote_id / client_id 反查);**不读 POST body,也不覆盖 path-id**(如 /api/publications/
        #     {quote_id}、/api/distill/{diagnosis_id} 的路径 id IDOR)。路径越权靠端点级 require_*_access
        #     兜底(已由 #2 geo-core 把 distill/publications 收紧为 NULL-brand fail-closed)。别误读为
        #     "路径越权已由本网全关"——本网是 query 向量 + owner 语义的纵深补强,非全量 IDOR 闸。
        allowed_brands = user_data_source.get("client_brand_ids", []) or []
        # [audit #6 返修] sync DB helper(quote→brand 反查 / owner 判定)包 to_thread:WORKERS=1 下
        #   同步 psycopg2 查询会阻塞 event loop → 拖慢全站并发。多数命中进程内缓存(无 DB),未命中走线程。
        requested_brand_id = await asyncio.to_thread(_extract_brand_id, request)
        if requested_brand_id is not None and requested_brand_id not in allowed_brands:
            _uid_for_net = user_data_source.get("user_id") or user_data_source.get("id")
            _net_is_owner = await asyncio.to_thread(_is_brand_owner_cached_failopen, _uid_for_net, requested_brand_id)
            if not _net_is_owner:
                # [D10 演示只读放行 · Owner 2026-07-26 授权的保护文件单点例外 · SSOT v2.2]
                # 双 demo 头同时在场才进入判定(避免给每个请求加 DB 开销);只读方法;
                # 权威校验/精确品牌/任何异常 fail-closed 全在 helper 内 —— 注意这里是
                # 跨租户读授权本体,**不得**沿用上面 owner 判定的 fail-open 语义。
                # 写方法不在此放行(演示写侧由 demo 中间件 409 围栏负责)。
                _demo_case_hdr = (request.headers.get("X-Demo-Case-ID") or "").strip()
                if _demo_case_hdr and (request.headers.get("X-Demo-Brand-ID") or "").strip():
                    _demo_ctx = await asyncio.to_thread(
                        _demo_readonly_grant_failclosed,
                        _uid_for_net, request.method, _demo_case_hdr, requested_brand_id,
                    )
                    if _demo_ctx is not None:
                        request.state.demo_access_context = _demo_ctx
                        return await call_next(request)
                logger.warning(
                    f"品牌访问被拒: user={user_data_source.get('username')} "
                    f"brand_id={requested_brand_id} allowed={allowed_brands}"
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": f"无权访问该客户数据",
                        "code": "BRAND_ACCESS_DENIED"
                    }
                )

        return await call_next(request)

    logger.info("✅ RBAC 全局认证中间件已注册")


def _demo_readonly_grant_failclosed(user_id, method, case_id, requested_brand_id):
    """[D10 · Owner 2026-07-26 授权的保护文件单点例外(SSOT v2.2)] 演示席位只读放行判定。

    返回可放行的 DemoAccessContext,否则 None(调用方保持原 403 路径)。
    逐条硬约束:
      - 只读:仅 GET/HEAD/OPTIONS,写方法一律 None(写侧由 demo 中间件 409 围栏负责);
      - 权威校验:走 services.demo_access.resolve_demo_case_access(已校验 grant 的
        status/有效期/capability),不自写 SQL;
      - 精确品牌:仅 requested_brand_id == context.brand_id 放行,跨品牌 None;
      - **fail-closed**:任何异常 → None。这里是跨租户读授权本体,与同段 owner
        判定的 fail-open(第二道网的取舍)方向相反,不得混用。
    """
    try:
        if str(method or "").upper() not in ("GET", "HEAD", "OPTIONS"):
            return None
        if not case_id or user_id is None or requested_brand_id is None:
            return None
        from services.demo_access import resolve_demo_case_access
        ctx = resolve_demo_case_access(int(user_id), str(case_id))
        if ctx is not None and int(ctx.brand_id) == int(requested_brand_id):
            return ctx
        return None
    except Exception:
        return None


# [audit P2 2026-06-10] 安全网缓存:quote→brand 映射不可变 → 进程内永久缓存(量级~千 · 50k 上限保险);
# owner 判定 60s TTL(brand 转移/删除低频)。查询异常一律 fail-open(网是第二道,DB 抖动不可炸全站)。
_quote_brand_cache: dict = {}
_owner_net_cache: dict = {}
_OWNER_NET_TTL_SECONDS = 60


def _resolve_quote_brand_cached(quote_id: int):
    """quote_id → brand_id(永久缓存 · 失败返 None=fail-open)"""
    if quote_id in _quote_brand_cache:
        return _quote_brand_cache[quote_id]
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT brand_id FROM quotes WHERE id = %s", (quote_id,))
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        brand_id = row.get("brand_id") if row else None
        if len(_quote_brand_cache) > 50000:
            _quote_brand_cache.clear()
        _quote_brand_cache[quote_id] = brand_id
        return brand_id
    except Exception as e:
        logger.debug(f"[BrandNet] quote→brand 反查失败(fail-open) quote={quote_id}: {e}")
        return None


def _is_brand_owner_cached_failopen(user_id, brand_id) -> bool:
    """owner 判定(60s TTL 缓存)· 查询异常返 True=fail-open 放行(端点级兜底)。"""
    if not user_id or not brand_id:
        return True  # 信息不足 fail-open(端点级兜底)
    import time as _time
    # [audit #6 返修] int() 转换纳入守卫:畸形 user_id/brand_id(非数字 JWT subject 等)→ ValueError
    #   原在 try 之外 → 抛 500 破坏"本函数一切异常 fail-open"承诺。畸形 id fail-open 放行(端点级兜底)。
    try:
        _uid_i = int(user_id)
        _bid_i = int(brand_id)
    except (ValueError, TypeError):
        return True
    key = (_uid_i, _bid_i)
    hit = _owner_net_cache.get(key)
    now = _time.time()
    if hit and now - hit[1] < _OWNER_NET_TTL_SECONDS:
        return hit[0]
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT owner_user_id FROM brands WHERE id = %s AND (is_deleted IS NULL OR is_deleted = FALSE)",
                (brand_id,),
            )
            row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        is_owner = bool(row and row.get("owner_user_id") == _uid_i)
        if len(_owner_net_cache) > 50000:
            _owner_net_cache.clear()
        _owner_net_cache[key] = (is_owner, now)
        return is_owner
    except Exception as e:
        logger.warning(f"[BrandNet] owner 查询异常(fail-open 放行) user={user_id} brand={brand_id}: {e}")
        return True


# [P0 IDOR 热修 2026-08-10] 尾段**确实是 brand_id** 的路径前缀白名单。
#   往这里加一条之前必须确认:该前缀之后的最后一段是 brands.id,不是 quote_id /
#   diagnosis_id / article_id 之类。加错的代价是把合法请求判成越权 403(fail-closed 方向的误伤)。
#   成对判据见 tests/mcidor_2026_08_10/test_middleware_path_brand_id.py:
#   白名单内必须命中、白名单外(尤其尾段是 quote_id 的路由)必须**不**命中。
_PATH_BRAND_ID_PREFIXES = (
    "/api/marketing-confirm/status/",
    "/api/marketing-confirm/resend/",
)


def _extract_brand_id(request: Request):
    """
    从请求中提取 brand 归属(query 参数 > path 参数)· 返回 int 或 None

    [audit P2 2026-06-10] 扩展:query quote_id / client_id(纯数字=quote_id 口径·全仓同款)反查 brand
    (带永久缓存·失败 fail-open)。不读 POST body(中间件消费 stream 需缓存重放·风险大,端点级兜底);
    不做路径通配(误匹配风险,/api/client-context 显式模式保留)。
    """
    # 1. 从 query 参数提取 brand_id
    brand_id_str = request.query_params.get("brand_id")
    if brand_id_str:
        try:
            return int(brand_id_str)
        except (ValueError, TypeError):
            pass

    # 1b. query quote_id / client_id(实为 quote_id·见 /api/reports 同款解析)→ 反查 brand
    for _qp in ("quote_id", "client_id"):
        _v = request.query_params.get(_qp)
        if _v:
            try:
                _qid = int(_v)
            except (ValueError, TypeError):
                continue
            _bid = _resolve_quote_brand_cached(_qid)
            if _bid is not None:
                return int(_bid)

    # 2. 从路径中提取 /api/client-context/{brand_id} 等模式
    path = request.url.path
    # 匹配 /api/client-context/123 格式
    if "/api/client-context/" in path:
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            try:
                return int(parts[-1])
            except (ValueError, TypeError):
                pass

    # 3. [P0 IDOR 热修 2026-08-10 · WO_P0_MARKETING_CONFIRM_IDOR 修法(c)]
    #    **显式白名单**的 path 参数 brand_id 形态。总册 §13.3 记载的"资源级归属校验缺失"
    #    是系统级弱点,这是它的第一块补丁 —— 不是全量修复,别当成"路径越权已全关"。
    #
    # 🔴🔴 为什么是白名单而不是"尾段是数字就当 brand_id":
    #    全仓大量路由的尾段数字**不是** brand_id —— `/api/publications/{quote_id}`、
    #    `/api/distill/{diagnosis_id}`、`/api/quotes/{quote_id}` …… 一旦通配,
    #    安全网会拿 quote_id 去比对 allowed_brands,把**合法请求**判成越权 403。
    #    上面第 1b 段之所以对 quote_id/client_id 先反查再比对,就是同一个道理。
    #    本文件头部原注释也明写过"不做路径通配(误匹配风险)"—— 那条判断至今成立,
    #    这里只是把"哪些路径的尾段确实是 brand_id"显式登记出来。
    #
    # 🔴 覆盖边界(诚实交代,别误读):`/api/marketing-confirm/generate-link` 的 brand_id
    #    在 **POST body** 里,中间件不读 body(消费 stream 需缓存重放),**本网覆盖不到它**;
    #    它靠端点级 `require_brand_access` 兜底。本网是纵深第二道,不是第一道。
    for _prefix in _PATH_BRAND_ID_PREFIXES:
        if path.startswith(_prefix):
            _tail = path[len(_prefix):].strip("/")
            if _tail and "/" not in _tail:
                try:
                    return int(_tail)
                except (ValueError, TypeError):
                    pass
            break

    return None
