"""
T6 白标泄漏扫描器 · 纯分类逻辑单测（scripts/whitelabel_leak_scan.py）。

只测 classify() / scan_text() / summarize() / admin_internal_keeps_omnirank() 纯逻辑，
不触真实树扫描、不连 DB（conftest 仅模块级要求 TEST_DATABASE_URL，本测试不真连）。
"""
import importlib.util
import pathlib

_SCANNER = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "whitelabel_leak_scan.py"
_spec = importlib.util.spec_from_file_location("whitelabel_leak_scan", _SCANNER)
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)


def _c(relpath, line, token="OmniRank"):
    """便捷：在 line 中定位 token 的真实起点再分类。"""
    start = line.find(token)
    assert start >= 0, f"token {token!r} not in line"
    return wl.classify(relpath, line, token, start)


# ---------- KEEP / 防火墙 ----------

def test_docker_infra_name_keep():
    assert _c("docker-compose.yml", "  image: omnirank-blue:latest", "omnirank") == wl.CLASS_KEEP
    assert _c("start.sh", "docker restart omnirank-ai", "omnirank") == wl.CLASS_KEEP


def test_oss_bucket_hyphen_keep():
    # 任意 .py 里出现 omnirank-kyc（token 后紧接 '-'）→ 基础设施名
    assert _c("services/oss_uploader.py", 'BUCKET = "omnirank-kyc"', "omnirank") == wl.CLASS_KEEP


def test_lock_file_keep():
    assert _c("scripts/deploy_helper.py", 'LOCK = "/tmp/omnirank-deploy.lock"', "omnirank") == wl.CLASS_KEEP


def test_code_symbol_keep():
    assert _c("frontend/src/pages/Publishing/PublishCenter.tsx",
              "  if (window.OmniRankExtension) connect();", "OmniRank") == wl.CLASS_KEEP
    assert _c("utils/di.py", "deps: OmniRankDeps = get_deps()", "OmniRank") == wl.CLASS_KEEP


def test_payment_subject_keep():
    assert _c("tools/payment/wechat_pay.py", 'desc = "OmniRank AI 积分充值"') == wl.CLASS_KEEP
    assert _c("tools/payment/xunhupay.py", 'title = "OmniRank 充值"') == wl.CLASS_KEEP


def test_sms_signature_keep():
    assert _c("auth/sms_service.py", "sign_name='全域上榜示例'", "全域上榜") == wl.CLASS_KEEP


def test_llm_caller_header_keep():
    # report_enhancement_agent 既有 customer 报告又有 LLM caller header；header 行必须 KEEP
    assert _c("agents/report_enhancement_agent.py",
              '"HTTP-Referer": "https://omnirank.cn",', "omnirank") == wl.CLASS_KEEP
    assert _c("advisors/base_advisor.py", '"X-Title": "OmniRank"') == wl.CLASS_KEEP


def test_crawler_ua_keep():
    assert _c("tools/agent_loop/adapters/web.py",
              'headers = {"User-Agent": "OmniRank-AgentLoop/1.0"}') == wl.CLASS_KEEP


def test_python_comment_keep():
    assert _c("api/share_api.py", "    # fallback 回 OmniRank 平台品牌") == wl.CLASS_KEEP


def test_js_comment_keep():
    assert _c("frontend/src/pages/Dashboard.tsx", "  // 旧 OmniRank 文案，已废弃") == wl.CLASS_KEEP


def test_localstorage_underscore_key_keep():
    # omnirank_* 下划线键 = localStorage/鉴权/缓存代码符号(改名破登录)→ KEEP
    assert _c("frontend/src/pages/Portal/PortalDashboard.tsx",
              "  const t = localStorage.getItem('omnirank_token');", "omnirank") == wl.CLASS_KEEP
    assert _c("frontend/src/pages/Publishing/PublishCenter.tsx",
              "  localStorage.setItem('omnirank_publish_cart', x)", "omnirank") == wl.CLASS_KEEP


def test_block_comment_single_line_keep():
    found = wl.scan_text("frontend/src/pages/Dashboard.tsx", "  /* 旧 OmniRank 文案 */ const x = 1;")
    assert [f["class"] for f in found if f["token"] == "OmniRank"] == [wl.CLASS_KEEP]


def test_jsx_block_comment_keep():
    found = wl.scan_text("frontend/src/components/layout/Layout.tsx", "  {/* OmniRank · 刷得到 */}")
    assert found and all(f["class"] == wl.CLASS_KEEP for f in found)


def test_multiline_block_comment_keep_but_not_over_mask():
    text = ("/**\n"
            " * 这是 OmniRank 的 JSDoc\n"
            " * 全域上榜 说明\n"
            " */\n"
            "const brand = '全域上榜';")  # 块注释外的真泄漏(代理后台页面→agent)
    found = wl.scan_text("frontend/src/pages/Brands/X.tsx", text)  # [开源 E3 · 前端 · 2026-10-01 · WO_322] 合成路径换成在役代理后台页目录
    by = {(f["line"], f["token"]): f["class"] for f in found}
    assert by[(2, "OmniRank")] == wl.CLASS_KEEP        # JSDoc 内
    assert by[(3, "全域上榜")] == wl.CLASS_KEEP          # JSDoc 内
    assert by[(5, "全域上榜")] == wl.CLASS_AGENT          # 注释外真泄漏 · 不被过度掩盖


def test_docs_archive_keep():
    assert _c("docs/AI-CONTEXT/HANDOFF.md", "OmniRank 交接") == wl.CLASS_KEEP
    assert _c("frontend/src/pages/x_LEGACY.md", "OmniRank legacy") == wl.CLASS_KEEP


def test_url_in_customer_page_not_comment():
    # https://omnirank.top 的 '//' 前导 ':' 不能误判为 JS 注释 → 仍是 customer 泄漏
    line = '  <meta property="og:url" content="https://omnirank.top/r/abc" />'
    assert _c("frontend/src/pages/Public/SharedReport.tsx", line, "omnirank") == wl.CLASS_CUSTOMER


# ---------- admin-internal（必须保留 OmniRank） ----------

def test_admin_backend_is_admin_internal():
    assert _c("api/admin_api.py", 'title = "OmniRank 后台"') == wl.CLASS_ADMIN


def test_admin_frontend_is_admin_internal():
    assert _c("frontend/src/pages/Admin/UserManagement.tsx",
              '<h1>OmniRank 用户管理</h1>') == wl.CLASS_ADMIN


# ---------- customer（external_only） ----------

def test_report_renderer_is_customer():
    assert _c("services/report_html_renderer.py", 'eyebrow = "OmniRank"') == wl.CLASS_CUSTOMER


def test_ranking_prompt_is_customer():
    assert _c("writing/templates/ranking_list_template.py",
              '"提及 OmniRank 不超过 5 次"') == wl.CLASS_CUSTOMER


def test_pdf_theme_js_is_customer():
    assert _c("scripts/pptx_workspace/generate_pdf_themes.js",
              "const brand = '全域上榜';", "全域上榜") == wl.CLASS_CUSTOMER


# ---------- agent（oem 工作台） ----------

def test_sidebar_layout_is_agent():
    assert _c("frontend/src/components/layout/AppSidebar.tsx",
              '<img alt="OmniRank" src={logo} />') == wl.CLASS_AGENT


def test_xiaobang_prompt_is_agent():
    assert _c("api/xiaobang_api.py", 'system = "你是小榜助手"', "小榜") == wl.CLASS_AGENT


# ---------- other（SSOT / 平台默认常量 · 不断言） ----------

def test_ssot_default_constant_is_other():
    # 前端 hooks 下的平台默认品牌常量，既非 admin 也非 customer/agent → other（信息项）
    assert _c("frontend/src/hooks/useWhitelabel.ts",
              "  company_name: 'OmniRank · 全域上榜',") == wl.CLASS_OTHER
    assert _c("services/public_whitelabel.py",
              '_PLATFORM_BRAND = {"company_name": "OmniRank · 全域上榜"}') == wl.CLASS_OTHER


# ---------- scan_text / summarize / 硬断言 ----------

def test_scan_text_multi_token_in_line():
    found = wl.scan_text("services/report_html_renderer.py",
                         "OmniRank 与 全域上榜 同一行")
    assert len(found) == 2
    assert {f["token"] for f in found} == {"OmniRank", "全域上榜"}
    assert all(f["class"] == wl.CLASS_CUSTOMER for f in found)


def test_scan_text_no_token():
    assert wl.scan_text("api/foo.py", "no brand here") == []


def test_admin_assertion_true_when_admin_present():
    findings = [
        {"class": wl.CLASS_ADMIN}, {"class": wl.CLASS_CUSTOMER}, {"class": wl.CLASS_AGENT},
    ]
    summary, _ = wl.summarize(findings)
    assert wl.admin_internal_keeps_omnirank(summary) is True


def test_admin_assertion_false_when_admin_stripped():
    # 过度替换：admin 类被清空 → 硬断言必须 False（阶段 0 唯一会失败的条件）
    findings = [{"class": wl.CLASS_CUSTOMER}, {"class": wl.CLASS_KEEP}]
    summary, _ = wl.summarize(findings)
    assert wl.admin_internal_keeps_omnirank(summary) is False


def test_summarize_counts():
    findings = [
        {"class": wl.CLASS_CUSTOMER}, {"class": wl.CLASS_CUSTOMER},
        {"class": wl.CLASS_AGENT}, {"class": wl.CLASS_KEEP},
    ]
    summary, by_class = wl.summarize(findings)
    assert summary[wl.CLASS_CUSTOMER] == 2
    assert summary["total"] == 4
    assert len(by_class[wl.CLASS_CUSTOMER]) == 2
