"""P2 · 写作大厅三处(A 不跳转 / C Modal / D 切免费端点)的锁。

判据口径(工单 §5 P2 锁)——刻意断在**结构与请求 URL**上,不断在文案串上:
  · 文案会随产品迭代改,断它 = 换个说法就红(假红),也挡不住"换皮不换行为"(假绿);
  · 请求 URL 是这一包真正要改变的东西:免费修复必须打 repair-finding / auto-repair,
    绝不能回到收费的 /api/writing/rewrite。

⚠️ 覆盖边界(如实交代,不假装):本仓前端**没有** vitest/jsdom 任何 DOM 测试设施
(frontend/package.json 只有一条 playwright 的 obs:test),本机也没有可用的 PG。
所以下面是**静态结构锁**,不是浏览器行为验证。它们能钉住的是"代码走哪条路",
钉不住的是"渲染出来长什么样"。真机 UI 复核仍需人过一遍。
"""

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SERVER = ROOT / "server.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    """取两个标记之间的源码块。找不到任一标记直接 fail —— 结构变了就该重新看,
    不能静默退化成"在全文件里找"(那样锁就没有作用域,别处的同名串会喂假绿)。"""
    start = source.find(start_marker)
    assert start >= 0, f"找不到起始标记: {start_marker}"
    end = source.find(end_marker, start + len(start_marker))
    assert end > start, f"找不到结束标记: {end_marker}"
    return source[start:end]


def _strip_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释。

    🔴 必要性:本包的注释里**刻意讨论**了"不要再用折叠元素""不要打 rewrite",
    否定说法同样会被 grep 命中 —— 不剥注释,锁 2/锁 3 会被自己的说明文字喂成假绿或假红。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _advisory_component(source: str) -> str:
    return _block(
        source,
        "function ArticleEvidenceAdvisory(props: {",
        "// 写作进度类型",
    )


def _continue_handler(source: str) -> str:
    return _block(
        source,
        "const continueEvidenceAdvisory = async (topic: Topic) => {",
        "// 批量重写选中的已完成文章",
    )


# ---------------------------------------------------------------- 锁 1

def test_lock1_confirm_does_not_navigate_away():
    """锁 1 · 确认后 URL 不变(→ 滚动/展开/筛选/已选自然保留)。

    判据:证据提示的「确认继续」处理函数体内**不存在任何 navigate(...) 调用**。
    变异「恢复 navigate(...)」→ 本条红。

    为什么这样断就够:loadProjectDetail 只 setKeywords/setTopics,不碰 selectedTopics /
    expandedKeywords / 滚动位置(见同文件 loadProjectDetail 实现),所以在这条链路上
    唯一会毁掉现场的动作就是路由跳转。
    """
    handler = _strip_comments(_continue_handler(_read(WRITING_HALL)))
    assert "navigate(" not in handler, "确认证据提示后仍会跳转,现场(滚动/展开/已选)会丢"


def test_lock1_confirm_judges_only_on_response_payload():
    """锁 1 反向面 · 判断只许用本次响应 payload。

    改前是 `payload.article_human_review_status === 'approved' || topic.publication_eligible`
    —— 后半截读的是闭包里上一次 loadProjectDetail 的旧值,而"确认"本身会改变它。
    判据:处理函数体内不出现 `topic.publication_eligible`,且确实读了 payload 字段。
    """
    handler = _strip_comments(_continue_handler(_read(WRITING_HALL)))
    assert "topic.publication_eligible" not in handler, "仍在读闭包里的旧 publication_eligible"
    assert "payload.article_human_review_status" in handler
    assert "payload.evidence_advisory_acknowledged" in handler


def test_lock1_row_level_publish_button_still_exists():
    """锁 1 的补充反向面:去掉跳转不等于去掉发布入口。

    单篇「去投放」按钮仍在,仍按 publication_eligible 决定可点性 ——
    否则"不跳转"就变成了"没法发布",那是把 A 修成了新 bug。
    """
    source = _read(WRITING_HALL)
    assert "disabled={!topic.publication_eligible}" in source
    assert "navigate(`/publish?article_id=${topic.article_id}" in source


# ---------------------------------------------------------------- 锁 2

def test_lock2_advisory_panel_is_a_modal_not_an_inline_disclosure():
    """锁 2 · 点「查看质量参考」只开 Modal,列表行高不变。

    判据:ArticleEvidenceAdvisory 组件体内
      · 折叠元素标签出现 0 次(它内联在文章行里,展开就撑高整行);
      · 出现 Dialog/DialogContent(Radix Dialog 走 Portal 挂 body,与行高无关)。
    变异「把 Modal 改回折叠元素」→ 本条红。
    """
    component = _strip_comments(_advisory_component(_read(WRITING_HALL)))
    tag = "<" + "details"
    assert component.count(tag) == 0, "质量参考面板又变回内联折叠结构,会撑高文章行"
    assert "<Dialog " in component and "<DialogContent" in component


def test_lock2_unrelated_contact_form_disclosure_is_untouched():
    """锁 2 的边界:🔴 工单说本文件有"3 处折叠元素"、要求"不许再用它撑开文章行"。

    实测订正:3 处里只有 2 处属于质量参考面板(面板根 + 位置清单);
    第 3 处是品牌资料表单里的「可选联系方式」折叠区,与文章行毫无关系,
    是折叠元素的正当用法。本包**不动它** —— 照"3 处全改"字面执行会顺手删掉
    一个无关的正常交互。这条锁把这个边界钉住,防后续有人"补全"。
    """
    source = _read(WRITING_HALL)
    tag = "<" + "details"
    assert _strip_comments(source).count(tag) == 1, "文件内折叠元素应只剩「可选联系方式」这一处"
    assert "可选联系方式" in source


# ---------------------------------------------------------------- 锁 3

FREE_REPAIR_URLS = (
    "`/api/articles/${topic.article_id}/repair-finding`",
    "`/api/articles/${topic.article_id}/auto-repair`",
)


def test_lock3_free_repair_calls_hit_repair_finding_not_rewrite():
    """锁 3 · 修复调用打到 repair-finding,而不是 rewrite(断请求 URL,不断源码串)。

    判据:质量参考面板组件体内
      · 出现 repair-finding 的请求 URL;
      · **完全不出现** /api/writing/rewrite。
    变异「切回 rewrite」→ 本条红。
    """
    component = _strip_comments(_advisory_component(_read(WRITING_HALL)))
    assert FREE_REPAIR_URLS[0] in component, "面板内的修复动作没有打到免费的 repair-finding"
    assert "/api/writing/rewrite" not in component, "面板内又出现了收费的整篇重写端点"


def test_lock3_legal_findings_panel_also_uses_free_endpoints_only():
    """锁 3 的邻居面:hard findings 面板(ArticleLegalFindings)同样只走两个免费端点。

    它改前就是对的,这里钉住不被本包顺手改坏。
    """
    component = _strip_comments(_block(
        _read(WRITING_HALL),
        "function ArticleLegalFindings(props: {",
        "// [工单 C-3 T2/T3 2026-07-27]",
    ))
    assert FREE_REPAIR_URLS[0] in component
    assert FREE_REPAIR_URLS[1] in component
    assert "/api/writing/rewrite" not in component


# ---------------------------------------------------------------- 锁 4

BILLING_CALL_NAMES = {
    "deduct_points",
    "deduct_points_with_preference",
    "charge_on_success",
    "freeze_points",
    "commit_freeze",
    "consume_points",
    "charge_points",
}


def _endpoint_ast(func_name: str) -> ast.FunctionDef:
    tree = ast.parse(_read(SERVER))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"server.py 里找不到函数 {func_name}")


def _called_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_lock4_free_endpoints_never_call_any_billing_function():
    """锁 4 · 用户扣费为 0,重复请求仍为 0。

    判据(AST 而非 grep):两个免费端点的函数体内,**不存在**对任何扣费函数的调用。
    AST 的意义:字符串匹配会被注释、日志文案、变量名喂假;AST 只认真正的调用节点。

    "重复请求仍为 0" 的依据:两端点根本不含扣费调用 —— 调多少次都是 0,
    与调用次数无关。(端点另有免费额度:同一 finding 首修 + 1 次重试,
    第 3 次直接拒绝并返回 quota_exhausted,仍然不扣费。)
    """
    for func_name in ("api_repair_article_finding", "api_auto_repair_article"):
        called = _called_names(_endpoint_ast(func_name))
        leaked = called & BILLING_CALL_NAMES
        assert not leaked, f"{func_name} 调用了扣费函数: {sorted(leaked)}"


ZERO_CHARGE_ENDPOINT_ALLOWLIST = {
    "/api/articles/${topic.article_id}/repair-finding",   # 零扣费(charged: False)
    "/api/articles/${topic.article_id}/auto-repair",      # 零扣费
    "/api/articles/${topic.article_id}",                  # GET 读正文,不扣费
}


def test_lock4_every_network_call_from_the_two_panels_is_zero_charge():
    """锁 4 的前端面 · 「用户扣费为 0」必须能被前端侧的变异打红。

    锁 4 的后端 AST 断言只证明"那两个端点不扣费",证不了"前端打的是那两个端点"——
    单靠它,变异「切回 rewrite」只会打红锁 3,锁 4 照绿(工单要求 ③④ 同时红)。
    这条补上缺口:枚举两个 findings 面板里**全部** authFetch 的 URL,
    逐个必须落在零扣费白名单内。出现 /api/writing/rewrite 之类计费端点 → 红。
    """
    source = _read(WRITING_HALL)
    panels = _strip_comments(_advisory_component(source)) + _strip_comments(_block(
        source,
        "function ArticleLegalFindings(props: {",
        "// [工单 C-3 T2/T3 2026-07-27]",
    ))
    urls = re.findall(r"authFetch\(`([^`]+)`", panels)
    assert urls, "没抓到任何 authFetch 调用 —— 提取口径失效了,这条锁等于没跑"
    unexpected = sorted(set(urls) - ZERO_CHARGE_ENDPOINT_ALLOWLIST)
    assert not unexpected, f"findings 面板打到了不在零扣费白名单里的端点: {unexpected}"


def test_lock4_free_endpoints_declare_zero_charge_in_response():
    """锁 4 的正面证据:repair-finding 的返回体自带 charged: False。

    不只"没调扣费函数"(可能只是还没写),而是**显式对外声明零计费**。
    """
    block = _block(
        _read(SERVER),
        '@app.post("/api/articles/{article_id}/repair-finding")',
        '@app.post("/api/articles/{article_id}/auto-repair")',
    )
    assert '"charged": False' in block


# ---------------------------------------------------------------- 锁 5(反向)

def test_lock5_whole_article_rewrite_stays_billed_and_is_labelled():
    """锁 5 · 反向:整篇创意重写**仍然计费**,且 UI 明确标注。

    这一条是防"修过头":把所有东西都改成免费端点,会顺手删掉一项付费能力,
    也会让用户以为整篇重写不要钱。判据三项都要成立:
      · 仍打 /api/writing/rewrite;
      · 仍走 articleRewriteCost + confirmLarge(价目未加载则拒绝,不默认 0 造免费假象);
      · 触发它的 UI 块带"计费"标注(data-testid 钉住那一块的存在)。
    """
    source = _read(WRITING_HALL)
    handler = _strip_comments(_block(
        source,
        "const rewriteWholeArticleForAdvisory = (topic: Topic) => {",
        "const continueEvidenceAdvisory",
    ))
    assert "authFetch(`/api/writing/rewrite/${topic.id}`" in handler, "整篇重写不再走计费端点"
    assert "articleRewriteCost == null" in handler, "价目未加载时没有拒绝,会造成免费假象"
    assert "confirmLarge(" in handler and "articleRewriteCost," in handler

    component = _advisory_component(source)
    assert 'data-testid="advisory-whole-rewrite-section"' in component
    assert 'data-testid="advisory-whole-rewrite-btn"' in component
    assert "整篇重写(计费)" in component, "付费入口没有明示这是整篇重写且计费"


def test_lock5_paid_and_free_actions_are_in_separate_blocks():
    """锁 5 的分离面:计费入口与免费修复入口不能混在同一块里。

    判据:付费块在「概览」标签页的独立容器内,免费的整类修复按钮在另一个容器;
    两者的 data-testid 不同,且付费块的容器串里不含免费按钮的 testid。
    """
    component = _advisory_component(_read(WRITING_HALL))
    paid_block = _block(
        component,
        'data-testid="advisory-whole-rewrite-section"',
        "</TabsContent>",
    )
    assert 'data-testid="repair-class-btn"' not in paid_block
    assert 'data-testid="advisory-repair-all-free-btn"' not in paid_block


# ------------------------------------------------- 语义一致性(工单点名的矛盾)

def test_advisory_panel_never_claims_pending_human_confirmation():
    """工单 §5:文案写"不影响发布"就不能同时显示"待人工确认"。

    判据:面板组件体内不出现"待确认/待人工确认/待处理"这类制造义务感的字样,
    同时"不影响发布"确实在。红线「不得用普通质量提示制造'必须确认'的视觉假象」。
    """
    component = _strip_comments(_advisory_component(_read(WRITING_HALL)))
    for forbidden in ("待人工确认", "待确认", "待处理", "必须确认"):
        assert forbidden not in component, f"质量参考面板出现了制造义务感的字样:{forbidden}"
    assert "不影响发布" in component


def test_advisory_action_set_is_complete():
    """工单 §5 指定的 A1 动作集四项齐全(按 data-testid 钉,不按标签文案钉)。

    一键免费修复 / 只修这一处 / 忽略并继续 / 全部忽略本类。
    注:"只修这一处"沿用既有标签「让 AI 改这一处」—— 它语义相同,且被
    test_article_evidence_advisory_flow.py 既有断言钉着(工单 §1.1 未把那条列入
    须改清单),改标签会无谓地打红一条仍然正确的测试。
    """
    component = _advisory_component(_read(WRITING_HALL))
    assert 'data-testid="advisory-repair-all-free-btn"' in component      # 一键免费修复
    assert 'data-testid="span-ai-repair-link"' in component               # 只修这一处
    assert 'data-testid="advisory-continue-btn"' in component             # 忽略并继续
    assert 'data-testid="ignore-class-btn"' in component                  # 全部忽略本类
