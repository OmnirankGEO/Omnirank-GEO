"""C-4 判别锁中的**纯静态断言**(从 test_c4_review_autopilot_2026_07_27.py 拆出)。

拆分理由(backlog `BACKLOG_C4_STATIC_ASSERTIONS_SPLIT`,复审 REVIEW_VERDICT_P2_WRITING_HALL
§ backlog 立项 · P3 并入项 2):

  test_c4 里有一个 `@pytest.fixture(scope="module", autouse=True)` 的 `_preload_server_module`
  —— autouse + module 作用域意味着它对**该文件里每一条**用例生效,包括那些只读源码文本、
  压根不碰 server / DB 的静态断言。于是在没有 PG 的环境里整个文件 ERROR,静态断言
  **一条都没被执行过**。

  这不是理论风险,已经真实咬过一次:`"质量记录(仅供参考" in advisory` 这条断言在
  2026-07-30 文案改名后就恒为 False,却一直"没红",直到 P2(2026-07-31)才被发现 ——
  因为它从来没跑到过。死断言就是这么积累起来的。

本文件的硬约束(拆出来的意义全在这里):
  · **不得引入任何 fixture**(module 级、autouse、conftest 依赖都不行);
  · **不得 import server / db / 任何需要 DATABASE_URL 的模块**;
  · 只允许 `Path.read_text` + 字符串断言。
  → 任何环境(无 PG、无 .env、CI 冷机)都能跑,红了就是真红。
  这三条由 tests/test_p3_batch_audit_2026_08_01.py::test_c4_static_split_* 上锁。

🔴 **只搬运,不改语义**:下面四条用例的函数名、docstring、断言全部与拆分前逐字相同,
本次不借机"顺手优化"。要改语义请另开包,别混在搬运里(混在一起就没人能判断
"红了是搬坏了还是本来就该红")。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"


def test_auto_repair_endpoint_refreshes_machine_review():
    """修好→审核通过的管道:两个修复端点落盘后都必须刷新机审结论。

    旧缺陷:只改 content+quality_warning,article_review_status 仍是 blocked
    → "修好了发布门还拦"。变异(去 refresh)转红。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    repair_seg = src[src.index("async def api_repair_article_finding"):src.index("@app.post(\"/api/articles/{article_id}/auto-repair\")")]
    auto_seg = src[src.index("async def api_auto_repair_article"):src.index("@app.put(\"/api/articles/{article_id}\")")]
    assert "refresh_article_review(article_id, cursor=c)" in repair_seg
    assert "refresh_article_review(article_id, cursor=c)" in auto_seg
    # 一键端点的服务端轮数硬闸
    assert "one_click_rounds_left" in auto_seg
    assert "AUTO_REPAIR_ROUNDS_EXHAUSTED" in auto_seg


def test_frontend_soft_is_background_reference_not_confirmation():
    """soft/advisory 从确认流移除:入口是"质量记录(仅供参考)",不再"需要你确认"。"""
    tsx = _TSX.read_text(encoding="utf-8")
    start = tsx.index("function ArticleEvidenceAdvisory")
    end = tsx.index("// 写作进度类型", start)
    advisory = tsx[start:end]
    # 🔴 [P2 2026-07-31 发现] 这条断言在本包之前就已经是**死的**:
    #    入口文案在 2026-07-30「误报治理 T3」里已从「质量记录(仅供参考)」改成
    #    「质量参考(不影响发布)」,`"质量记录(仅供参考" in advisory` 在生产尖
    #    eac2200b 上就已经是 False。它一直没红,只是因为本文件的其它用例依赖
    #    PG 而在无库环境里整体 ERROR —— 断言从来没被执行到。
    #    (复现:`git show eac2200b:frontend/.../WritingHall.tsx | grep -c "质量记录(仅供参考"` → 0)
    #    现按当前口径订正,并改成钉"入口存在 + 不制造确认负担",不再钉具体措辞前半段。
    assert "不影响发布" in advisory
    assert 'data-testid="article-advisory-findings"' in advisory
    assert "需要你确认" not in advisory, "soft 不得再制造确认负担"
    # 记录可达面还在(想看的能看):类型卡与位置清单未拆
    assert 'data-testid="findings-type-card"' in advisory
    assert "展开位置清单" in advisory


def test_frontend_hard_summary_card_replaces_flat_cards():
    """hard 列表入口只出一张汇总卡:N 处 + 一键修复(轮数) + 查看详情;
    详情弹窗有逐项高亮定位([AI 修复][手动改][忽略]三出口 + 失败治理)。"""
    tsx = _TSX.read_text(encoding="utf-8")
    start = tsx.index("function ArticleLegalFindings")
    end = tsx.index("// [工单 C-3 T2/T3", start)
    legal = tsx[start:end]
    assert 'data-testid="hard-summary-card"' in legal
    assert "本篇有 {findings.length} 处需要处理" in legal
    assert 'data-testid="auto-repair-btn"' in legal
    assert "auto-repair`" in legal or "auto-repair'" in legal or "/auto-repair" in legal
    assert 'data-testid="view-findings-btn"' in legal
    # 详情弹窗三出口 + 高亮 + 失败治理
    assert 'data-testid="hard-detail-dialog"' in legal
    assert 'data-testid="locate-finding-btn"' in legal
    assert 'data-testid="finding-highlight"' in tsx  # locateAndHighlight 的 mark
    assert "手动改" in legal and "忽略" in legal
    assert 'data-testid="span-repair-failure"' in legal and "重试这一处" in legal
    # 修干净后的可追溯徽章
    assert 'data-testid="auto-repaired-badge"' in legal
    assert "审核通过 · 已自动修复" in legal
    # 旧的逐条平铺(上限 6 + 溢出提示)必须绝迹
    assert "MAX_LEGAL_FINDINGS" not in tsx
    assert "legal-findings-overflow" not in tsx


def test_admin_quality_panel_hard_only_uses_real_path():
    """admin 后台 hard_only 修正:查 evidence_legal.hard(旧顶层 'hard' 恒空)。"""
    src = (ROOT / "api" / "admin_articles_api.py").read_text(encoding="utf-8")
    assert "quality_warning->'evidence_legal'->'hard'" in src
