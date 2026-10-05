"""
安全审计阶段 1 越权测试（fix/security-audit-p0 分支）

测试矩阵：
- 账号 A (uid=17, 晓川)：品牌 77 / 档案 WYK4k7pT
- 账号 B (uid=18, 阿飞哥)：品牌 78 / 档案 5b90dee0
- 账号 admin (uid=1)：管理员

每个端点测 3 路径：
- A 拿自己的 ID → 期望 200（不破坏现有功能）
- A 拿 B 的 ID → 期望 403（堵住越权）
- admin 拿任意 ID → 期望 200（管理员旁通）
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth.jwt_utils import create_jwt

BASE = "http://127.0.0.1:8001"
A_TOK = create_jwt(17)
B_TOK = create_jwt(18)
ADMIN_TOK = create_jwt(1)

A_BRAND = 77
A_PROFILE = "WYK4k7pT"
B_BRAND = 78
B_PROFILE = "5b90dee0"


def call(method, path, token, params=None, json_body=None, expect_one_of=None):
    """发请求，返回 (status, body 摘要, 是否符合期望)"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.request(method, BASE + path,
                             headers=headers, params=params, json=json_body, timeout=20)
        status = r.status_code
        try:
            body = r.json()
            if isinstance(body, dict):
                summary = (body.get("detail") or body.get("error") or
                           list(body.keys())[:3])
            else:
                summary = str(type(body))
        except Exception:
            summary = r.text[:80]
        ok = (status in expect_one_of) if expect_one_of else True
        return status, summary, ok
    except Exception as e:
        return -1, str(e)[:100], False


def check(label, expect, actual_status, body, ok):
    icon = "✅" if ok else "❌"
    print(f"  {icon} {label:60s} 期望 {expect}, 实际 {actual_status}, body={str(body)[:60]}")
    return ok


print("=" * 88)
print("安全审计阶段 1 越权测试矩阵")
print("=" * 88)

results = []

# ==================== #18 distillation_api ====================
print("\n[#18 distillation_api] 8 个洞察接口加 require_brand_access")

# A 拿自己品牌 → 200
s, b, _ = call("GET", "/api/insights/summary", A_TOK,
               params={"brand_id": A_BRAND}, expect_one_of=[200])
results.append(check("A 看 A 自己品牌的洞察 summary", "200", s, b, s == 200))

# A 拿 B 品牌 → 403（核心越权测试）
s, b, _ = call("GET", "/api/insights/summary", A_TOK,
               params={"brand_id": B_BRAND}, expect_one_of=[403])
results.append(check("A 越权看 B 品牌的洞察 summary（关键）", "403", s, b, s == 403))

# admin 拿 B 品牌 → 200（旁通）
s, b, _ = call("GET", "/api/insights/summary", ADMIN_TOK,
               params={"brand_id": B_BRAND}, expect_one_of=[200])
results.append(check("admin 看 B 品牌的洞察 summary（旁通）", "200", s, b, s == 200))

# 其他几个洞察端点
for ep in ["/api/insights/competitor-radar", "/api/insights/source-analysis",
           "/api/insights/keyword-insights-list"]:
    s, b, _ = call("GET", ep, A_TOK, params={"brand_id": B_BRAND}, expect_one_of=[403])
    results.append(check(f"A 越权 {ep}", "403", s, b, s == 403))

# ==================== #26 placement_api ====================
print("\n[#26 placement_api] /research/cleanup 和 industry 删除加 admin")

# 普通用户调 cleanup → 403
s, b, _ = call("POST", "/api/placement/research/cleanup", A_TOK,
               json_body={}, expect_one_of=[403])
results.append(check("普通用户调 /research/cleanup（关键）", "403", s, b, s == 403))

# 管理员可以
s, b, _ = call("POST", "/api/placement/research/cleanup", ADMIN_TOK,
               json_body={}, expect_one_of=[200, 422])
results.append(check("admin 调 /research/cleanup（旁通）", "200/422", s, b, s in (200, 422)))

# 普通用户调 DELETE /research/industry/{name} → 403
s, b, _ = call("DELETE", "/api/placement/research/industry/餐饮", A_TOK,
               expect_one_of=[403])
results.append(check("普通用户删除行业调研数据（关键）", "403", s, b, s == 403))

# ==================== #25 managed_campaign（资金线，最关键）====================
print("\n[#25 managed_campaign_api] 单关键词 confirm-recharge 加 require_brand_access")

# A 拿 B 的 brand_id 做 confirm-recharge → 必须 403，不能扣 A 的钱
body = {
    "keyword": "测试词不会真扣",
    "brand_id": B_BRAND,
    "target_sov_pct": 10,
    "target_display_label": "10%",
    "tier_label": "entry",
    "selected_amount_yuan": 100,
    "mode": "semi_auto",
    "max_per_article_yuan": 200,
    "check_frequency_per_day": 1,
    "estimate_quoted_at": "2026-05-07T00:00:00",
    "agreement_consent": True,
    "brand_voice_consent": True,
}
s, b, _ = call("POST", "/api/managed/confirm-recharge", A_TOK,
               json_body=body, expect_one_of=[403])
results.append(check("A 用 B 品牌 confirm-recharge（资金红线）", "403", s, b, s == 403))


# ==================== 汇总 ====================
print()
print("=" * 88)
total = len(results)
passed = sum(1 for x in results if x)
print(f"测试总数: {total}, 通过: {passed}, 失败: {total - passed}")
print("=" * 88)
sys.exit(0 if passed == total else 1)
