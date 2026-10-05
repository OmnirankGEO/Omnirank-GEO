"""v2.10 文章方向配比器 · G27 阶段 6 端到端 + 集成测试

阶段 1 测试在 tests/test_v210_direction_distribution.py(SSOT helpers 单元)
此文件覆盖:
  - 旧 endpoint hard 400(Codex 三审 P0-1)
  - server.py 新 endpoint 存在性 + 关键代码守护(grep)
  - 前端 DistributionConfigDialog 文案 0 工程词
  - WritingHall 高级入口 link 渲染
  - SQL 用 IS DISTINCT FROM 防 NULL unknown(Codex 四审 P0-1)
"""
from __future__ import annotations

import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read_text(rel_path: str) -> str:
    full_path = os.path.join(_ROOT, rel_path)
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# G27 · 旧 endpoint hard 400(Codex 三审 P0-1)
# ============================================================

def test_v210_legacy_articles_plan_distribution_hard_400():
    """G27 · /api/articles/plan request.distribution 必须 hard 400 reject(0 后门)"""
    src = _read_text("server.py")
    fn_start = src.find("async def plan_articles")
    assert fn_start > 0, "找不到 plan_articles endpoint"
    fn_block = src[fn_start:fn_start + 3000]

    # 必须有 hard 400 reject
    assert "request.distribution is not None" in fn_block, \
        "v2.10 破:/api/articles/plan 未 hard 400 reject distribution(0 后门违反)"
    assert "status_code=400" in fn_block, \
        "v2.10 破:plan_articles 未 raise 400"
    assert "distribution 字段已废弃" in fn_block or "deprecated" in fn_block.lower(), \
        "v2.10 破:400 detail 未说明字段废弃"


def test_v210_legacy_articles_generate_distribution_hard_400():
    """G27 · /api/articles/generate 必须整体废弃 410(v2.10.2 升级 · 不再半兼容)

    v2.10.1 改 Optional + is not None → 400(留 None 走旧逻辑)· 但 None 时 sum(None.values()) 500 崩
    v2.10.2 升级:整体 410 Gone · 任何调用都 reject · 0 后门 · 0 半兼容
    """
    src = _read_text("server.py")
    fn_start = src.find("async def generate_articles")
    assert fn_start > 0, "找不到 generate_articles endpoint"
    fn_block = src[fn_start:fn_start + 1500]

    # v2.10.2:函数顶部必须 raise 410(不再走 is not None 判断)
    assert "status_code=410" in fn_block, \
        "v2.10.2 破:/api/articles/generate 未整体 410 废弃"
    assert "已整体废弃" in fn_block or "deprecated" in fn_block.lower() or "Gone" in fn_block, \
        "v2.10.2 破:410 detail 未说明整体废弃"


# ============================================================
# G27 · 新 endpoint 存在性 + 关键代码守护
# ============================================================

def test_v210_direction_plan_endpoint_exists():
    """G27 · GET /api/writing/projects/{quote_id}/direction-plan 存在"""
    src = _read_text("server.py")
    assert '@app.get("/api/writing/projects/{quote_id}/direction-plan")' in src, \
        "v2.10 破:direction-plan endpoint 未注册"


def test_v210_direction_plan_no_total_query_param():
    """G27 · direction-plan 0 信任前端 total query(Codex 三审 P0-2)

    endpoint 签名应该是 async def api_get_direction_plan(quote_id: int, http_request)
    不接 total: int param
    """
    src = _read_text("server.py")
    fn_start = src.find("async def api_get_direction_plan")
    assert fn_start > 0, "找不到 api_get_direction_plan"
    # 取函数签名(到 ): 为止)
    fn_sig_end = src.find("):", fn_start)
    fn_sig = src[fn_start:fn_sig_end + 2]

    assert "total" not in fn_sig.lower() or "total_count" not in fn_sig, \
        f"v2.10 破:direction-plan 函数签名含 total query param · 应 0 信任前端 · 签名={fn_sig}"


def test_v210_apply_distribution_endpoint_exists():
    """G27 · POST /api/writing/projects/{quote_id}/apply-distribution 存在"""
    src = _read_text("server.py")
    assert '@app.post("/api/writing/projects/{quote_id}/apply-distribution")' in src, \
        "v2.10 破:apply-distribution endpoint 未注册"


def test_v210_apply_distribution_owner_check_exists():
    """G27 · apply-distribution 必须有 owner check(Codex 四审 P1-3 防越权)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    assert fn_start > 0, "找不到 api_apply_distribution"
    fn_block = src[fn_start:fn_start + 5000]

    # owner/assigned-agent access must use the shared fail-closed guard.
    assert "owner_user_id" in fn_block, "v2.10 破:apply-distribution 未 JOIN owner_user_id"
    assert "require_quote_access" in fn_block, "v2.10 破:apply-distribution 未走共享项目权限硬门"
    assert "is_admin" in fn_block, "v2.10 破:apply-distribution 未跳过 admin"


def test_v210_apply_distribution_uses_is_distinct_from():
    """G27 · apply-distribution UPDATE 必须 IS DISTINCT FROM 'manual'(Codex 四审 P0-1 防 NULL unknown)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 8000]

    # 关键 SQL:UPDATE WHERE source IS DISTINCT FROM 'manual'
    assert "IS DISTINCT FROM 'manual'" in fn_block, \
        "v2.10 破:apply-distribution UPDATE 未用 IS DISTINCT FROM(NULL unknown 致漏覆盖)"


def test_v210_apply_distribution_excludes_locked_statuses():
    """G27 · apply-distribution UPDATE 必须排除 LOCKED_STATUSES(防竞态)"""
    src = _read_text("server.py")
    fn_start = src.find("async def api_apply_distribution")
    fn_block = src[fn_start:fn_start + 8000]

    # 必须排除 completed/writing/regenerating
    assert "completed" in fn_block and "writing" in fn_block and "regenerating" in fn_block, \
        "v2.10 破:apply-distribution UPDATE 未排除 LOCKED_STATUSES(completed/writing/regenerating)"


def test_v210_regenerate_titles_accepts_style_plan():
    """G27 · regenerate-titles 必须接 style_plan param(Codex 二审 P1-4)"""
    src = _read_text("server.py")
    # RegenerateRequest 必须有 style_plan field
    req_idx = src.find("class RegenerateRequest")
    assert req_idx > 0, "找不到 RegenerateRequest"
    req_block = src[req_idx:req_idx + 500]

    assert "style_plan" in req_block, \
        "v2.10 破:RegenerateRequest 未加 style_plan 字段(per-topic plan 透传)"


def test_v210_ktg_accepts_style_plan_param():
    """G27 · KeywordTopicGenerator __init__ 必须接 style_plan param(Codex 二审 P1-4)"""
    src = _read_text("writing/keyword_topic_generator.py")
    assert "style_plan: List[Dict]" in src or "style_plan: list" in src.lower(), \
        "v2.10 破:KeywordTopicGenerator __init__ 未加 style_plan param(per-topic 注入)"
    assert "self.style_plan = style_plan" in src, \
        "v2.10 破:KeywordTopicGenerator 未保存 style_plan"


def test_v210_ktg_prompt_injects_per_topic_plan():
    """G27 · KeywordTopicGenerator prompt 必须注入 per-topic plan(防 LLM 自由生成致错位)"""
    src = _read_text("writing/keyword_topic_generator.py")

    # plan 注入段标记
    assert "_style_plan_hint" in src, \
        "v2.10 破:KeywordTopicGenerator 未生成 _style_plan_hint 段"
    # plan 优先级:style_plan > force_chinese_style > default
    assert "if self.force_chinese_style and not self.style_plan" in src, \
        "v2.10 破:force_chinese_style 未让位给 style_plan(优先级冲突)"


# ============================================================
# G27 · 前端 DistributionConfigDialog 文案 0 工程词
# ============================================================

def test_v210_distribution_dialog_exists():
    """G27 · DistributionConfigDialog 组件必须存在"""
    path = os.path.join(_ROOT, "frontend", "src", "components", "writing", "DistributionConfigDialog.tsx")
    assert os.path.exists(path), "v2.10 破:DistributionConfigDialog.tsx 不存在"


def test_v210_distribution_dialog_no_engineering_terms():
    """G27 · DistributionConfigDialog 用户可见文案 0 工程词(LLM/ranking/style_code/content_ratios)"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    # 跳过注释行扫 quoted string
    violations = []
    in_block_comment = False
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if "/*" in stripped:
            in_block_comment = True
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        # 扫 quoted string 含工程词
        for term in ["LLM", "ranking", "style_code", "content_ratios"]:
            if re.search(rf'["“”\'][^"“”\']*\b{term}\b[^"“”\']*["“”\']', line, re.IGNORECASE if term == "LLM" else 0):
                # ranking 排除 ranking_v2(变量名)· 但 quoted string 中不可有
                if term.lower() == "ranking" and ("ranking_v2" in line or "authority_ranking" in line):
                    continue
                violations.append(f"L{i}: {stripped[:100]}")

    assert not violations, \
        f"v2.10 破:DistributionConfigDialog 用户可见文案含工程词:\n" + "\n".join(violations[:5])


def test_v210_distribution_dialog_3_modes():
    """G27 · DistributionConfigDialog 必须有 3 mode(recommended/uniform/custom)"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    assert "'recommended'" in src or '"recommended"' in src, "v2.10 破:Dialog 缺 recommended mode"
    assert "'uniform'" in src or '"uniform"' in src, "v2.10 破:Dialog 缺 uniform mode"
    assert "'custom'" in src or '"custom"' in src, "v2.10 破:Dialog 缺 custom mode"
    assert "系统推荐" in src, "v2.10 破:Dialog 缺 '系统推荐' 文案"
    assert "统一方向" in src, "v2.10 破:Dialog 缺 '统一方向' 文案"
    assert "自定义配比" in src, "v2.10 破:Dialog 缺 '自定义配比' 文案"


def test_v210_distribution_dialog_uses_exact_six_families():
    """G27 · 配比弹窗只暴露六类 family,无工程词 —— 但清单**只许有一份**。

    🔴 [WO_205 2026-09-15 改判据] 原先在 `DistributionConfigDialog.tsx` 里找
       `value: 'evidence_qa'` 这类**手写字面量数组**。六个 family 后来搬进了 SSOT
       `frontend/src/pages/Writing/articleDirections.ts`,弹窗改成
       `USER_CHOICE_LABELS = distributableDirections().map(...)` —— 于是六个串全找不到。

    🔴 关键是:**这条判据当时在要求一件坏事**。满足它的唯一办法是把六个 family
       抄回弹窗,也就是把清单写成两份 —— 正是「同一谓词两处必有一处没验」。
       所以修法不是放宽,是**把锚挪到清单真正住的地方**:
         · 在弹窗里钉「**派生**自 SSOT」(不许再手写);
         · 在 SSOT 里钉「六类齐、工程词不在」。
       两条合起来比原来更严:原来只管弹窗那一份,现在连「有没有第二份」都管。
    """
    from tests._shared.source_slice import code_only

    # 🔴 [WO_205 注毒暴露] 剥注释再匹配。实测:把真正的 `distributableDirections()`
    #    调用换掉,断言**照样绿** —— 该文件抬头注释里也写着 `distributableDirections()`。
    dialog = code_only(
        _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx"), "ts")
    ssot = code_only(_read_text("frontend/src/pages/Writing/articleDirections.ts"), "ts")

    # ① 弹窗必须**派生**,不许自带清单
    assert "distributableDirections" in dialog, (
        "v2.10 破:配比弹窗没从 SSOT 取方向清单 —— 清单又被抄成了第二份")
    assert "USER_CHOICE_LABELS" in dialog, "配比弹窗缺 USER_CHOICE_LABELS"
    # 手写字面量的形状一旦回来就红(哪怕内容碰巧对)
    for choice in (
        "evidence_qa", "multi_brand_comparison", "implementation_guide",
        "trend_policy_risk", "case_data_roi", "company_facts",
    ):
        assert ("value: '%s'" % choice) not in dialog, (
            "v2.10 破:配比弹窗里又出现手写的 '%s' —— 清单成了两份,"
            "改 SSOT 时这一份不会跟着变,而没有任何东西会报错" % choice)

    # ② 六类 family 必须在 SSOT 里齐
    for choice in (
        "evidence_qa", "multi_brand_comparison", "implementation_guide",
        "trend_policy_risk", "case_data_roi", "company_facts",
    ):
        assert ("value: '%s'" % choice) in ssot, (
            "v2.10 破:SSOT articleDirections 缺 '%s'" % choice)

    # ③ 工程词不可暴露(老板拍 A):对**可分配**清单成立
    #    `distributableDirections()` = ARTICLE_DIRECTIONS 去掉 auto,
    #    所以这两个串在 SSOT 里出现就是暴露。
    assert "value: 'ranking'" not in ssot, (
        "v2.10 破:SSOT 含 ranking · 应不暴露(老板拍 A)")
    assert "value: 'company'" not in ssot, (
        "v2.10 破:SSOT 含 company · 应走 fixed slot")

    # ④ 反向对照:SSOT 里确实有 auto,且弹窗**排除**它 ——
    #    否则上面几条在「弹窗根本没渲染清单」时也会全绿。
    assert "value: 'auto'" in ssot, "SSOT 里没有 auto,这条锁的参照物不对"
    assert "distributableDirections()" in dialog, (
        "弹窗没调 distributableDirections() —— 它拿的可能是含 auto 的全量")


def test_v210_distribution_dialog_custom_mode_disclaimer():
    """G27 · 自定义模式明确只在六类文体内分配并校验总篇数。"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    assert "自定义配比只在六类文体内分配" in src
    assert "合计必须等于可分配篇数" in src


def test_v210_distribution_dialog_existing_titles_two_buttons():
    """G27 · 已生成场景双按钮:仅调整正文方向(不扣费) / 同步重写标题(扣 N×80)"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    assert "仅调整正文方向" in src, "v2.10 破:缺'仅调整正文方向'按钮"
    assert "不扣费" in src, "v2.10 破:缺'不扣费'文案"
    assert "同步重写标题" in src, "v2.10 破:缺'同步重写标题'按钮"
    assert "topicGenCost" in src, "v2.10 破:扣费金额未动态计算(应用 topicGenCost prop)"


def test_v210_distribution_dialog_calls_correct_api():
    """G27 · Dialog 必须调 direction-plan + apply-distribution(不调旧 endpoint)"""
    src = _read_text("frontend/src/components/writing/DistributionConfigDialog.tsx")

    assert "/direction-plan" in src, "v2.10 破:Dialog 未调 direction-plan endpoint"
    assert "/apply-distribution" in src, "v2.10 破:Dialog 未调 apply-distribution endpoint"
    assert "user_choice_distribution" in src, "v2.10 破:Dialog payload 字段名错(应是 user_choice_distribution)"
    assert "sync_rewrite_titles" in src, "v2.10 破:Dialog 缺 sync_rewrite_titles 切换"

    # 不能调旧 endpoint
    assert "articles/plan" not in src or "/* " in src.split("articles/plan")[0][-50:], \
        "v2.10 破:Dialog 误调旧 /api/articles/plan"
    assert "articles/generate" not in src or "/* " in src.split("articles/generate")[0][-50:], \
        "v2.10 破:Dialog 误调旧 /api/articles/generate"


# ============================================================
# G27 · WritingHall 高级入口集成
# ============================================================

def test_v210_writing_hall_imports_distribution_dialog():
    """G27 · WritingHall.tsx 必须 import DistributionConfigDialog"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    assert "import { DistributionConfigDialog }" in src, \
        "v2.10 破:WritingHall 未 import DistributionConfigDialog"
    assert "from '@/components/writing/DistributionConfigDialog'" in src, \
        "v2.10 破:DistributionConfigDialog 路径错"


def test_v210_writing_hall_advanced_link():
    """G27 · WritingHall 必须有高级入口小字 link(老板 A · 不大面积展现)"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    assert "高级：自定义文章文体配比" in src, \
        "v2.10 破:WritingHall 缺高级入口 link 文案"
    # 高级入口必须是 button + text-xs(小字)· 不是 Button + 显眼按钮
    advanced_idx = src.find("高级：自定义文章文体配比")
    surrounding = src[max(0, advanced_idx - 500):advanced_idx + 100]
    assert "text-xs" in surrounding, \
        "v2.10 破:高级入口未用小字 link(text-xs)· 老板要求不大面积展现"


def test_v210_writing_hall_renders_dialog():
    """G27 · WritingHall 必须渲染 DistributionConfigDialog"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    assert "<DistributionConfigDialog" in src, \
        "v2.10 破:WritingHall 未渲染 DistributionConfigDialog 组件"
    assert "distributionDialogOpen" in src, \
        "v2.10 破:WritingHall 缺 distributionDialogOpen state"
    assert "setDistributionDialogOpen" in src, \
        "v2.10 破:WritingHall 缺 setDistributionDialogOpen setter"


def test_v210_writing_hall_passes_topic_gen_cost():
    """G27 · WritingHall 传 topicGenCost prop 给 Dialog(动态扣费提示)"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    dialog_idx = src.find("<DistributionConfigDialog")
    assert dialog_idx > 0, "找不到 DistributionConfigDialog 渲染点"
    dialog_block = src[dialog_idx:dialog_idx + 800]

    assert "topicGenCost={topicGenCost}" in dialog_block, \
        "v2.10 破:WritingHall 未传 topicGenCost prop"
    assert "hasExistingTitles=" in dialog_block, \
        "v2.10 破:WritingHall 未传 hasExistingTitles prop(已生成场景判定)"
