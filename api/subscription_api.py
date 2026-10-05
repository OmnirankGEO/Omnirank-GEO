"""
Social Studio 订阅 API V3.1

来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
计划: .planning/phases/07-social-studio-subscription/PLAN.md B05

Endpoints:
  GET  /api/subscription/health             — 部署自检(Deploy 用)

[开源 E3 · B4 · 2026-09-28] 订阅产品面随社媒删除(Review 09-27 裁定,E3_DELETION_MAP §5.3):
  套餐 / 我的订阅 / 下单 / 首月资格 / 退订 / 升降级 / 配额预检 / 加量包列表与购买 / 配额用量 /
  ¥9.9 白名单 / 反扣挂账 13 条端点删除;订阅支付与订阅佣金两个模块整文件删除。
  数据层与读路径保留:订阅相关表不动、middleware/subscription_billing.get_active_subscription_full 照常读。
"""

import logging

from fastapi import APIRouter

from db.connection import get_connection

logger = logging.getLogger("GEO-Subscription-API-V31")

router = APIRouter(prefix="/api/subscription", tags=["订阅系统 V3.1"])


# ==================== 部署自检 ====================

@router.get("/health")
async def subscription_health():
    """V3.1 订阅系统部署自检(Deploy-CTO 一行 curl 验完整链路)

    2026-05-12 老板"代码写好让 Deploy AI 填 ENV":
    本 endpoint 列出 V3.1 + 微信/虎皮椒支付所有配置项的实证状态,
    Deploy-CTO 部署后跑一次 curl 就知道缺什么。

    用法:
      curl https://omnirank.top/api/subscription/health | jq

    返回 200 + JSON 含:
      · subscription_plans:5 套餐是否就位
      · subscription_tables:7 张表是否建好
      · wechat_pay:5 ENV + 2 证书文件
      · xunhupay:3 ENV
      · cron:5 个订阅 cron 是否注册
      · overall_ready:总体可付费状态

    完整 ready → 用户可付费走 V3.1 月卡 + 充值
    部分缺失 → 列出缺什么,Deploy-CTO 对照补
    """
    import os
    import logging
    _logger = logging.getLogger("GEO-SubHealth")

    result = {
        "ok": True,
        "checks": {},
        "missing": [],
        "warnings": [],
    }

    # ---- DB schema 自检 ----
    db_ok = False
    plans_count = 0
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 7 张 V3.1 表
            v31_tables = [
                "subscription_plans", "user_social_subscriptions", "user_social_entitlements",
                "subscription_usage_events", "subscription_referral_records",
                "commission_clawback_pending", "first_month_special_whitelist",
            ]
            missing_tables = []
            for t in v31_tables:
                cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                row = cur.fetchone()
                exists = row and row.get("to_regclass") is not None
                if not exists:
                    missing_tables.append(t)
            result["checks"]["v31_tables_present"] = len(missing_tables) == 0
            if missing_tables:
                result["missing"].append({"category": "db_tables", "items": missing_tables})

            # 5 套餐 seed
            try:
                cur.execute("SELECT COUNT(*) AS c FROM subscription_plans WHERE is_active = TRUE")
                row = cur.fetchone()
                plans_count = int(row["c"]) if row else 0
                result["checks"]["plans_seed_count"] = plans_count
                if plans_count < 5:
                    result["missing"].append({
                        "category": "db_seed",
                        "items": [f"subscription_plans 仅 {plans_count} 行,期望 5"],
                    })
            except Exception:
                result["checks"]["plans_seed_count"] = "error"

            db_ok = len(missing_tables) == 0 and plans_count == 5
        finally:
            conn.close()
    except Exception as e:
        _logger.error(f"[health] DB check failed: {e}")
        result["checks"]["db_connection"] = False
        result["missing"].append({"category": "db", "items": [f"DB 连接失败: {type(e).__name__}"]})

    # ---- 微信支付 ENV ----
    # WX_PUB_KEY_ID 是未来"公钥模式"预留 · GEO 现走 V3 platform_cert
    # (SERIAL_NO + apiclient_key.pem 私钥签名 + pub_key.pem 回调验签)
    # · 缺失只 warning 不 block ready
    wx_env_keys = ["WX_MCH_ID", "WX_APPID", "WX_SERIAL_NO", "WX_APIV3_KEY"]
    wx_env_missing = []
    for k in wx_env_keys:
        v = os.getenv(k, "")
        if not v:
            wx_env_missing.append(k)
    if not os.getenv("WX_PUB_KEY_ID", ""):
        result["warnings"].append({
            "category": "wechat_pay_env_optional",
            "items": ["WX_PUB_KEY_ID (公钥模式预留 · GEO 走 platform_cert 不需要)"],
        })
    result["checks"]["wechat_pay_env"] = {
        "configured": len(wx_env_keys) - len(wx_env_missing),
        "total": len(wx_env_keys),
        "missing": wx_env_missing,
    }
    if wx_env_missing:
        result["missing"].append({"category": "wechat_pay_env", "items": wx_env_missing})

    # ---- 微信支付证书文件 ----
    wx_key_path = os.getenv("WX_KEY_PATH", "/app/apiclient_key.pem")
    wx_pub_key_path = os.getenv("WX_PUB_KEY_PATH", "/app/pub_key.pem")
    wx_certs_missing = []
    for label, path in [("apiclient_key.pem", wx_key_path), ("pub_key.pem", wx_pub_key_path)]:
        if not os.path.exists(path):
            # 尝试 fallback 路径(对齐 services/wechat_pay.py 内部 fallback 逻辑)
            fallback_exists = False
            for fb in [label, f"/app/{label}", f"./{label}"]:
                if os.path.exists(fb):
                    fallback_exists = True
                    break
            if not fallback_exists:
                wx_certs_missing.append({"file": label, "expected_path": path})
    result["checks"]["wechat_pay_certs"] = {
        "found": 2 - len(wx_certs_missing),
        "total": 2,
        "missing": wx_certs_missing,
    }
    if wx_certs_missing:
        result["missing"].append({
            "category": "wechat_pay_certs",
            "items": [f"{c['file']} 缺(查 {c['expected_path']})" for c in wx_certs_missing],
            "hint": "微信支付证书不入 git · Deploy-CTO 从老板拿到后放进 docker volume / 容器内",
        })

    # ---- 虎皮椒 fallback ENV(非必填,缺失只 warning)----
    xunhu_keys = ["XUNHUPAY_APPID", "XUNHUPAY_APPSECRET"]
    xunhu_missing = [k for k in xunhu_keys if not os.getenv(k, "")]
    result["checks"]["xunhupay_env"] = {
        "configured": len(xunhu_keys) - len(xunhu_missing),
        "total": len(xunhu_keys),
        "missing": xunhu_missing,
    }
    if xunhu_missing:
        result["warnings"].append({
            "category": "xunhupay_env",
            "items": xunhu_missing,
            "hint": "虎皮椒是微信支付失败时的 fallback,不配置不影响主流程(但 V3 失败无后路)",
        })

    # ---- Cron 注册自检 ----
    cron_registered = []
    try:
        from api.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            cron_registered = [j.id for j in scheduler.get_jobs() if "subscription" in j.id]
        # WO_308(2026-09-27):订阅佣金 3 个 cron 与 03:00 自动续费扣款 cron 已摘,只剩月度重置 1 个
        expected_crons = [
            "subscription_monthly_reset",
        ]
        missing_crons = [c for c in expected_crons if c not in cron_registered]
        result["checks"]["subscription_crons"] = {
            "registered": len(cron_registered),
            "expected": len(expected_crons),
            "missing": missing_crons,
        }
        if missing_crons:
            role = os.getenv("ROLE", "").lower()
            if role == "backup":
                # backup 实例本来就不应注册 cron(ROLE gate),不算 missing
                result["checks"]["subscription_crons"]["note"] = "ROLE=backup · cron 不注册符合设计"
            else:
                result["missing"].append({
                    "category": "cron",
                    "items": missing_crons,
                    "hint": "primary 实例订阅 cron(月度重置)应注册 · 检查 server.py R5 patch 是否 applied",
                })
    except Exception as e:
        result["checks"]["subscription_crons"] = {"error": str(e)}

    # ---- V3.1 business code 接入 SOCIAL_FEATURES 白名单 ----
    try:
        from middleware.subscription_billing import SOCIAL_FEATURES
        result["checks"]["social_features_count"] = len(SOCIAL_FEATURES)
        # 2026-05-12 老板 P0 修复:9 个新 feature 必须在白名单
        required_new = [
            "hook_gen", "deep_analyze", "industry_brief_rerun", "content_review",
            "comment_gen", "dm_gen", "scenario_gen", "team_analysis", "ai_coach",
        ]
        missing_features = [f for f in required_new if f not in SOCIAL_FEATURES]
        if missing_features:
            result["missing"].append({
                "category": "social_features",
                "items": missing_features,
                "hint": "V3.1 业务代码接入需要这 9 个 feature 在白名单",
            })
    except Exception as e:
        result["checks"]["social_features_count"] = f"error: {e}"

    # ---- 微信 oauth 配置自检(2026-05-13 复用 GEO oauth · 不再依赖独立表/router)----
    # V3.1 订阅 oauth 通过 services/wechat_pay.build_oauth_authorize_url + GEO
    # /api/wallet/wechat-jsapi/oauth-url + /exchange-openid 端点完成 · 不入库 即用即扔
    wechat_oauth_missing = []
    if not os.getenv("WX_APP_SECRET"):
        wechat_oauth_missing.append("WX_APP_SECRET(公众号 AppSecret · oauth 换 openid 必需)")
    if not os.getenv("WX_APPID"):
        wechat_oauth_missing.append("WX_APPID(公众号 AppID · oauth 重定向必需)")
    # GEO oauth helper 是否可用(复用 services/wechat_pay)
    try:
        from services.wechat_pay import build_oauth_authorize_url, fetch_openid_by_code  # noqa: F401
        result["checks"]["wechat_oauth_helpers_importable"] = True
    except Exception as e:
        result["checks"]["wechat_oauth_helpers_importable"] = False
        wechat_oauth_missing.append(f"services.wechat_pay oauth helper 导入失败: {type(e).__name__}")
    # GEO oauth API 端点是否在(复用 wallet_api 路由)
    try:
        from api.wallet_api import router as _wallet_router
        oauth_routes = [
            r for r in _wallet_router.routes
            if hasattr(r, "path") and "wechat-jsapi" in str(r.path)
        ]
        result["checks"]["geo_oauth_endpoints"] = len(oauth_routes)
        if len(oauth_routes) < 2:
            wechat_oauth_missing.append(
                "GEO wallet wechat-jsapi/* 端点 < 2(应有 oauth-url + exchange-openid)"
            )
    except Exception as e:
        result["checks"]["geo_oauth_endpoints"] = f"error: {e}"
        wechat_oauth_missing.append(f"GEO wallet_api 导入失败: {type(e).__name__}")

    result["checks"]["wechat_oauth"] = {
        "configured": 4 - len(wechat_oauth_missing),
        "total": 4,
        "missing": wechat_oauth_missing,
    }
    if wechat_oauth_missing:
        result["missing"].append({
            "category": "wechat_oauth",
            "items": wechat_oauth_missing,
            "hint": (
                "微信内浏览器(MicroMessenger UA)付款必须走 jsapi · 需 openid · "
                "V3.1 订阅复用 GEO 钱包已 prod 跑通的 oauth 流程 · "
                "需配置 WX_APP_SECRET · 并在公众号后台「网页授权域名」里加上本站对外域名(PUBLIC_BASE_URL 的主机名)"
            ),
        })

    # ---- 总体 ready 判定 ----
    result["overall_ready"] = (
        db_ok
        and len(wx_env_missing) == 0
        and len(wx_certs_missing) == 0
        and len(result["missing"]) == 0
    )
    result["ok"] = True  # endpoint 自身 OK(返 200)· overall_ready 才是付费就绪信号

    if not result["overall_ready"]:
        result["status_text"] = (
            f"❌ NOT READY · {len(result['missing'])} 类配置缺失 · "
            f"Deploy-CTO 对照 missing 数组补,然后再 curl 一次本 endpoint"
        )
    else:
        result["status_text"] = "✅ READY · V3.1 订阅 + 微信支付全链路配置就绪"

    return result
