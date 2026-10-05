"""开源 E3a · 删掉整文件社媒 router 之后的锁:E3a 六个(竞品 / 团队 / 语料 / 灵感 / 智能建议 / 敏感词)
+ B1b-2a 四个(研究 / 运营 / 人设 / 数据复盘)+ B1b-2b 两个(社媒主路径 / 采访)。

守三件事(全是静态扫描,不起应用,单包 < 2s):
1. 6 个模块文件不在;全仓已跟踪 .py 里没有任何一处 import 它们(含函数体内的延迟导入与 `from api import x` 形);
2. `server.py` 不再注册它们;
3. 管理员唯一处置面(OSS_09 E3 专条:资金补偿 / 大额服务费审批 / 下级服务商申请 / 飞轮心跳)在 `server.py` 里的注册都还在。
对照臂:同一个扫描器对一个仍在役、确实被导入的模块(`api.advisor_api`)必须报出导入 —— 证明扫描器没瞎。
真 app 读数(openapi 管理员处置面 4/4、已删前缀 0 条、/api 路由 1856 → 1792)在交付单里,不进本包(起 app 要 12s)。
"""
import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RETIRED = ["competitor_api", "team_api", "corpus_api", "inspiration_api", "intelligence_api", "sensitive_api",
           # [B1b-2a · 2026-09-28] 研究 / 运营 / 人设 / 数据复盘四个社媒 router
           "research_api", "operation_api", "personality_api", "review_api",
           # [B1b-2b · 2026-09-28] 社媒主路径 / 采访两个 router
           "social_mainpath_api", "interview_api",
           # [B3c G4 · 2026-09-28] C 端 GEO 方案任务 API(5 条全删:生产只读核 0 在途 / 0 冻结)
           "geo_plan_task_api",
           # [B4 · 2026-09-28] 订阅支付(下单 / 续费 / 两个支付回调)与订阅佣金(结算 / 反扣)整文件删
           "subscription_renewal", "subscription_referral"]
ADMIN_ONLY = ["admin_fund_recovery_api", "service_fee_review_api", "channel_partner_api", "flywheel_health_api"]


def _importers(mod: str) -> list[str]:
    """已跟踪 .py 里 import `api.<mod>` 的文件(先 git grep 按名字粗筛,再 AST 判定是不是 import)。"""
    hits = subprocess.run(["git", "grep", "-l", "-z", "-w", mod, "--", "*.py"], cwd=str(REPO),
                          capture_output=True).stdout.decode("utf-8").split("\0")
    out = []
    for rel in (h for h in hits if h):
        try:
            tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import) and any(a.name == f"api.{mod}" or a.name.startswith(f"api.{mod}.")
                                                 for a in n.names):
                out.append(rel)
            elif isinstance(n, ast.ImportFrom) and n.module and (
                    n.module == f"api.{mod}" or (n.module == "api" and any(a.name == mod for a in n.names))):
                out.append(rel)
    return sorted(set(out))


def test_retired_router_files_are_gone_and_nothing_imports_them():
    for m in RETIRED:
        assert not (REPO / "api" / f"{m}.py").exists(), m
        assert _importers(m) == [], (m, _importers(m))


def test_scanner_control_arm_sees_a_live_import():
    # 对照:server.py 真的 import 了 api.advisor_api(在役);扫不到 ⇒ 扫描器坏了,上一格的「0」不可信
    assert "server.py" in _importers("advisor_api")


LITERAL_GLOBS = ("*.py", "*.json", "*.ts", "*.tsx", "*.mjs", "*.sh", "*.conf")


def _literal_mentions(name: str) -> list[str]:
    """非测试的代码 / 配置文件里按词边界提到 name 的文件(字面量,不看是不是 import)。
    docs/ 排除(与普查门同口径):那里只有生成的普查快照 JSON,没有任何运行时或门禁读它。"""
    r = subprocess.run(["git", "grep", "-n", "-z", "-w", "-F", name, "--", *LITERAL_GLOBS, ":(exclude)tests",
                        ":(exclude)docs"], cwd=str(REPO), capture_output=True)
    allowed = _comment_allowlist()
    hits = set()
    for rec in r.stdout.decode("utf-8").splitlines():
        parts = rec.split("\0")
        if len(parts) < 3:
            continue
        path, text = parts[0], parts[2]
        # 与普查门 ⑦ 同一张注释行白名单:保护文件里登记过的纯注释行不算(改保护文件要先点头)
        if (path, text.strip()) in allowed:  # 类型(comment / e2_token_spec)与形状由普查门逐条核
            continue
        hits.add(path)
    return sorted(hits)


def _comment_allowlist() -> set[tuple[str, str]]:
    p = REPO / "tests" / "REFERENCER_COMMENT_ALLOW.txt"
    out = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                cols = line.split("\t")
                if len(cols) == 4:  # 文件 · 类型 · 行原文 · 理由(普查门同一张表;类型合规由普查门核)
                    out.add((cols[0].strip(), cols[2].strip()))
    return out


def test_no_literal_mention_survives_in_code_or_config():
    """[Review 09-28 补] 本仓还有按字符串动态导入(`__import__` 名单、「模块:函数」任务表):
    只认 import 语句的那格会被 `_X = "api.team_api:router"` 这种字面量骗过 ⇒ 按词边界查字面量。"""
    for m in RETIRED:
        assert _literal_mentions(m) == [], (m, _literal_mentions(m))


def test_literal_scanner_control_arm_sees_a_live_module():
    # 对照:仍在役的 advisor_api 必须在非测试代码里扫得到(server.py 至少一处)
    assert "server.py" in _literal_mentions("advisor_api")


def test_server_registers_none_of_them_and_keeps_the_admin_only_surfaces():
    src = (REPO / "server.py").read_text(encoding="utf-8")
    for m in RETIRED:
        assert f"api.{m}" not in src, m
    for m in ADMIN_ONLY:
        assert f"api.{m}" in src, f"管理员唯一处置面 {m} 的注册没了(OSS_09 E3 专条:不得删、删完必须仍可达)"


# ── [开源 E3 · B3a · 2026-09-28] server.py 内联端点:122 条删掉的不许回来 ──
# [开源 E3 · WO_323 G3a · 2026-10-02] 原「G3a 四条报价动作待裁、必须还在」:Review 10-02 取证全 0 后准删,随 WO_322 同班删 ⇒ 倒过来锁成「必须不在」;
#   同班删的 GET /api/brands/{brand_id}/social-project 一并钉住
import functools

G3A_RETIRED = [
    ("POST", "/api/quotes/{quote_id}/agent-activate-service"),
    ("PATCH", "/api/quotes/{quote_id}/adjust-price"),
    ("POST", "/api/quotes/{quote_id}/append-keywords"),
    ("PATCH", "/api/quotes/{quote_id}/replace-keyword"),
    ("GET", "/api/brands/{brand_id}/social-project"),
]


@functools.lru_cache(maxsize=1)
def _server_routes() -> frozenset:
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                        and d.func.value.id == "app" and d.args and isinstance(d.args[0], ast.Constant)):
                    out.add((d.func.attr.upper(), d.args[0].value))
    return frozenset(out)


def _retired_inline() -> list[tuple[str, str]]:
    rows = []
    for line in (REPO / "tests" / "oss_e3a_routers_2026_09_28" / "retired_inline_routes.tsv").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            m, p, _h = line.split("\t")
            rows.append((m, p))
    return rows


def test_retired_inline_routes_are_not_registered_in_server():
    retired = _retired_inline()
    assert len(retired) == 122, len(retired)  # 尺子自证:清单没被悄悄改短
    back = sorted(set(retired) & _server_routes())
    assert back == [], back


def test_g3a_quote_actions_are_retired_and_a_live_route_is_still_registered():
    # G3a 四条 + social-project 不许回来;对照臂:另挑一条在役内联路由证明扫描器看得见 server.py 的注册
    routes = _server_routes()
    for k in G3A_RETIRED:
        assert k not in routes, f"{k} 随 WO_322 / WO_323 删了,不许回来"
    assert ("POST", "/api/quotes/{quote_id}/offline-confirm") in routes


def test_article_contract_lists_no_retired_route():
    src = (REPO / "services" / "article_closed_loop_contract.py").read_text(encoding="utf-8")
    for m, p in _retired_inline() + G3A_RETIRED + [("POST", p) for _m, p in G3A_RETIRED]:
        assert f'"{m} {p}"' not in src, (m, p)


# ── [开源 E3 · B3b · 2026-09-28] api/content_api.py:58 条删掉的不许回来;9 条在役(行业简报 8 + deep-analyze)必须还在 ──
CONTENT_KEPT = [
    ("POST", "/api/content/deep-analyze"),
    ("POST", "/api/content/industry-brief/confirm"),
    ("PATCH", "/api/content/industry-brief/edit"),
    ("POST", "/api/content/industry-brief/rerun"),
    ("GET", "/api/content/industry-brief/history/{profile_id}"),
    ("GET", "/api/content/industry-brief/history/{profile_id}/{version}"),
    ("POST", "/api/content/industry-brief/rollback"),
    ("GET", "/api/content/industry-brief/pool/{profile_id}"),
    ("PATCH", "/api/content/industry-brief/correct"),
]


def _content_routes() -> list:
    tree = ast.parse((REPO / "api" / "content_api.py").read_text(encoding="utf-8"))
    prefix = None
    for n in tree.body:
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "router" for t in n.targets)
                and isinstance(n.value, ast.Call)):
            for kw in n.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value
    assert prefix == "/api/content", prefix  # 尺子自证:前缀取不到就别拼路径
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                        and d.func.value.id == "router" and d.args and isinstance(d.args[0], ast.Constant)):
                    out.append((d.func.attr.upper(), prefix + d.args[0].value))
    return out


def _retired_content() -> list:
    rows = []
    for line in (REPO / "tests" / "oss_e3a_routers_2026_09_28" / "retired_content_routes.tsv").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            m, p, _h = line.split("\t")
            rows.append((m, p))
    return rows


def test_retired_content_routes_are_not_registered():
    retired = _retired_content()
    assert len(retired) == 58, len(retired)  # 尺子自证:清单没被悄悄改短
    back = sorted(set(retired) & set(_content_routes()))
    assert back == [], back


def test_content_router_keeps_exactly_the_live_nine():
    # 对照臂:在役 9 条一条不少,且 content 路由只剩这 9 条(多一条 = 有人加了新路由或删漏,都要人过一眼)
    assert sorted(_content_routes()) == sorted(CONTENT_KEPT)


# ── [开源 E3 · B3c · 2026-09-28] 8 个混合文件(顾问 / 小帮旧对话 / C 端 / 档案 G1 / M3 G2 / 导出 G6 / 品牌 G6 / 钱包):
#    55 条删掉的不许回来;同文件里的在役路由(含在役充值)必须还在 ──
MIXED_KEPT = [
    ("api/wallet_api.py", "POST", "/api/wallet/recharge"),                       # 在役充值,Review 明令保留
    ("api/advisor_api.py", "GET", "/api/advisors"),                              # 设置页选写手 / 品牌 AI 补全
    ("api/advisor_api.py", "GET", "/api/advisors/config/default-writer"),
    ("api/advisor_api.py", "PUT", "/api/advisors/config/default-writer"),
    ("api/c_end_api.py", "GET", "/api/c-end/settings/mode"),                     # 登录后落地判定
    ("api/agent_api.py", "GET", "/api/agent/draft-workspace"),                   # 品牌详情页
]


def _file_routes(rel: str) -> set:
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    pre = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "APIRouter":
            p = ""
            for kw in n.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    p = kw.value.value
            for t in n.targets:
                if isinstance(t, ast.Name):
                    pre[t.id] = p
    assert pre, f"{rel} 里一个 APIRouter 都扫不到 —— 尺子坏了"
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                        and d.func.value.id in pre and d.args and isinstance(d.args[0], ast.Constant)):
                    out.add((d.func.attr.upper(), pre[d.func.value.id] + d.args[0].value))
    return out


def _retired_mixed() -> list:
    rows = []
    for line in (REPO / "tests" / "oss_e3a_routers_2026_09_28" / "retired_mixed_routes.tsv").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            m, p, _h, rel = line.split("\t")
            rows.append((rel, m, p))
    return rows


def test_retired_mixed_routes_are_not_registered():
    retired = _retired_mixed()
    assert len(retired) == 55, len(retired)  # 尺子自证:清单没被悄悄改短
    back = []
    for rel in sorted({r for r, _, _ in retired}):
        live = _file_routes(rel)
        back += [(rel, m, p) for r, m, p in retired if r == rel and (m, p) in live]
    assert back == [], back


def _kept_mixed() -> dict:
    out = {}
    for line in (REPO / "tests" / "oss_e3a_routers_2026_09_28" / "kept_mixed_routes.tsv").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            m, p, rel = line.split("\t")
            out.setdefault(rel, set()).add((m, p))
    return out


def test_mixed_files_keep_their_live_routes():
    # [B3c-3 · Review 09-28 裁] 只钉 6 条名不副实(删 GET /api/m3/customers 锁照绿)⇒ 8 个文件尖上的**全部**保留路由
    #   冻结成集合,逐文件判**相等**:少一条 = 顺手删多了;多一条 = 有人加了路由,要回来改这张表(人过一眼)
    kept = _kept_mixed()
    assert sum(len(v) for v in kept.values()) == 69 and len(kept) == 8, {k: len(v) for k, v in kept.items()}  # 尺子自证
    for rel, want in sorted(kept.items()):
        got = _file_routes(rel)
        assert got == want, (rel, "少了", sorted(want - got), "多了", sorted(got - want))
    # 点名的几条(含在役充值)必须在冻结集里 —— 挡「改表时把它们一起划掉」
    for rel, m, p in MIXED_KEPT:
        assert (m, p) in kept.get(rel, set()), (rel, m, p)


# ── [开源 E3 · B4 · 2026-09-28] 订阅产品面:subscription_api 只许剩 /health;scheduler 只剩月初重置 ──
#    /cancel 也删(Review 09-28 改口:05-15 两条被 401 的微信通知是内部 admin 测试付款,真客户受影响 0;
#    有效期内有流水的订阅 0、退款案 0 —— Deploy 读数 .deploy_runtime/b4_subscription_inflight_prod_20260928.out)。
SUBSCRIPTION_KEPT = {("GET", "/api/subscription/health")}
SCHED_REMOVED = ("daily_subscription_commission_settle_job", "daily_clawback_resolve_check_job",
                 "hourly_commission_reconciliation_job", "subscription_grace_period_check",
                 "_try_auto_renew", "_add_grace_period", "_expire_and_downgrade")


def test_subscription_api_keeps_exactly_health():
    got = _file_routes("api/subscription_api.py")
    assert got == SUBSCRIPTION_KEPT, ("少了", sorted(SUBSCRIPTION_KEPT - got), "多了", sorted(got - SUBSCRIPTION_KEPT))


def test_subscription_scheduler_registers_only_the_monthly_reset():
    tree = ast.parse((REPO / "api" / "subscription_scheduler.py").read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    back = sorted(set(SCHED_REMOVED) & defined)
    assert back == [], f"已删的订阅 job 本体 / 辅助回来了:{back}"
    reg = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register_subscription_jobs")
    ids = sorted({k.value for d in ast.walk(reg) if isinstance(d, ast.Dict)
                  for key, k in zip(d.keys, d.values)
                  if isinstance(key, ast.Constant) and key.value == "id" and isinstance(k, ast.Constant)})
    assert ids == ["subscription_monthly_reset"], ids  # 分母活性:扫得到这一条,「只剩一条」才可信
