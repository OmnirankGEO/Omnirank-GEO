"""WO_MEDIA_BOARD_UX_CLOSURE_2026-08-05 · 三件套用例(§1 域名人话化 / §2 提示出口 / §3 调研报表)。

写这份用例时的两条自我约束(都是本仓踩过的坑):
  1. **不把断言钉在"这个词在文件里出现过"** —— 词在别处也有 = 弱锁(2026-08-04 同型第二次)。
     结构锁一律先 `inspect.getsource` 把**那一个函数**抠出来再断言。
  2. **每条"必须命中"配一条"必须不命中"** —— 恒真的锁和恒红的锁一样废。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ===========================================================================
# §1 · 域名人话化
# ===========================================================================


def test_normalize_directory_domain_forms():
    from services.media_domain_directory import normalize_directory_domain as nd

    assert nd("https://WWW.Foo.com/a?b=1") == "foo.com"
    assert nd("  ITHome.com  ") == "ithome.com"
    # 🔴 子域名**不剥**:news.koolearn.com 不是 koolearn.com 的别名,
    #    拿父域名的中文名套子站就是编造(与 _match_domain_map 的逐级剥离刻意不同)。
    assert nd("news.koolearn.com") == "news.koolearn.com"
    # 反向对照:非域名形态一律 ''(不是"随便返回点什么")
    assert nd("这不是域名") == ""
    assert nd("localhost") == ""
    assert nd("") == ""
    assert nd(None) == ""


def test_is_bare_domain_label_only_matches_domain_fallback():
    from services.media_effectiveness_board import _is_bare_domain_label as bare

    # 命中:空 / 与域名逐字相同 / 只差 www.
    assert bare("", "licai.cofool.com") is True
    assert bare("licai.cofool.com", "licai.cofool.com") is True
    assert bare("www.licai.cofool.com", "licai.cofool.com") is True
    # 必须不命中:任何真名都不许被目录覆盖(平台公认名 / 媒体库绑定真名 / canonical)
    assert bare("IT之家", "ithome.com") is False
    assert bare("百度百家号", "mbd.baidu.com") is False


def _fake_directory(monkeypatch, entries, *, raise_on_read=False, sink=None):
    """替换 services.media_domain_directory 的三个读写口(不碰 DB)。"""
    import services.media_domain_directory as mod

    def _get(domains):
        if raise_on_read:
            raise RuntimeError("directory down")
        return entries

    def _req(domains):
        if sink is not None:
            sink.extend(domains)
        return len(domains)

    monkeypatch.setattr(mod, "get_directory_entries", _get)
    monkeypatch.setattr(mod, "request_distillation", _req)


def test_apply_domain_directory_fills_only_bare_domain_rows(monkeypatch):
    from services.media_effectiveness_board import _apply_domain_directory

    queued: list[str] = []
    _fake_directory(
        monkeypatch,
        {
            "licai.cofool.com": {"zh_name": "财富赢家网", "one_liner": "理财资讯站", "source": "llm"},
            "ithome.com": {"zh_name": "蒸馏给的名字", "one_liner": "科技媒体", "source": "llm"},
        },
        sink=queued,
    )

    rows = [
        # 裸域名行 → 该被补成中文名
        {"domain": "licai.cofool.com", "display_name": "licai.cofool.com"},
        # 已有平台公认名 → **不许**被蒸馏名覆盖(归属纠偏不能退回老 bug)
        {"domain": "ithome.com", "display_name": "IT之家"},
        # 目录没覆盖到 → 保持域名 + 进蒸馏队列
        {"domain": "05wang.com", "display_name": "05wang.com"},
    ]
    general = [{"domain": "chinapp.com", "display_name": "chinapp.com"}]

    _apply_domain_directory(rows, general)

    assert rows[0]["display_name"] == "财富赢家网"
    assert rows[0]["one_liner"] == "理财资讯站"
    # 反向对照:硬信源不动,但 one_liner 照样挂上(hover 用)
    assert rows[1]["display_name"] == "IT之家"
    assert rows[1]["one_liner"] == "科技媒体"
    assert rows[2]["display_name"] == "05wang.com"
    assert general[0]["display_name"] == "chinapp.com"
    # 仍缺名的两个进队列;已有名字的两个不进
    assert set(queued) == {"05wang.com", "chinapp.com"}


def test_apply_domain_directory_is_fail_soft(monkeypatch):
    """目录查询炸掉 → 榜完全退化为裸域名现状,**绝不抛**给出榜路径。"""
    from services.media_effectiveness_board import _apply_domain_directory

    _fake_directory(monkeypatch, {}, raise_on_read=True)
    rows = [{"domain": "licai.cofool.com", "display_name": "licai.cofool.com"}]
    _apply_domain_directory(rows, [])          # 不抛
    assert rows[0]["display_name"] == "licai.cofool.com"
    assert "one_liner" not in rows[0]


def test_publish_board_is_wired_to_directory():
    """接线锁:`get_publish_media_board` 必须真的调 `_apply_domain_directory`。

    (本仓 2026-08-02 栽过"能力全就绪但没接线"= 功能等于不存在。)
    """
    from services.media_effectiveness_board import get_publish_media_board, _apply_domain_directory

    src = inspect.getsource(get_publish_media_board)
    assert "_apply_domain_directory(rows, general_rows)" in src
    # 反向对照:E1 admin 主榜**不**接目录(它是品牌点名口径,没有 domain 列)
    from services.media_effectiveness_board import get_industry_media_effectiveness_board

    assert "_apply_domain_directory" not in inspect.getsource(get_industry_media_effectiveness_board)
    assert callable(_apply_domain_directory)


@pytest.mark.asyncio
async def test_distill_domains_drops_unasked_and_negative_caches(monkeypatch):
    """模型自行发挥补出来的域名一律丢弃;漏答的补成负缓存(否则永远排队永远漏答)。"""
    import json

    import services.media_domain_directory as mod

    class _Resp:
        @staticmethod
        def json():
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({"items": [
                            {"domain": "licai.cofool.com", "zh_name": "财富赢家网", "one_liner": "理财资讯"},
                            # 我们没问过的域名 —— 必须被丢弃
                            {"domain": "evil-inject.com", "zh_name": "臆造站", "one_liner": "x"},
                        ]}, ensure_ascii=False)
                    }
                }]
            }

    async def _fake_post(body, **kwargs):
        return _Resp()

    import services.llm.deepseek_key_pool as pool

    monkeypatch.setattr(pool, "adeepseek_post_with_failover", _fake_post)

    out = await mod.distill_domains(["licai.cofool.com", "05wang.com"])
    by_domain = {o["domain"]: o for o in out}

    assert by_domain["licai.cofool.com"]["zh_name"] == "财富赢家网"
    # 必须不命中:没问过的域名不进目录
    assert "evil-inject.com" not in by_domain
    # 漏答的那个补成负缓存行(空名),让 attempts 照常累加
    assert by_domain["05wang.com"]["zh_name"] == ""


@pytest.mark.asyncio
async def test_distill_domains_fail_soft_on_llm_error(monkeypatch):
    import services.media_domain_directory as mod
    import services.llm.deepseek_key_pool as pool

    async def _boom(body, **kwargs):
        raise RuntimeError("no key")

    monkeypatch.setattr(pool, "adeepseek_post_with_failover", _boom)
    assert await mod.distill_domains(["licai.cofool.com"]) == []


def test_upsert_never_overwrites_admin_rows():
    """人工订正(source='admin')的行,蒸馏永不覆盖 —— 写在 SQL 的 WHERE 里。"""
    from services.media_domain_directory import upsert_entries

    src = inspect.getsource(upsert_entries)
    assert "ON CONFLICT (domain) DO UPDATE" in src            # 幂等
    assert "media_domain_directory.source <> 'admin'" in src  # 人工优先
    assert "attempts  = media_domain_directory.attempts + 1" in src


def test_migration_026_registered():
    """迁移必须登记进 manifest,否则 prestart 不 glob = 表永远不会建。"""
    from db.migration_manifest import MIGRATIONS

    path = "db/migration_026_media_domain_directory_2026_08_05.sql"
    assert path in MIGRATIONS
    assert (ROOT / path).is_file()
    sql = (ROOT / path).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS media_domain_directory" in sql
    # 反向对照:回滚脚本**不许**登记(登记 = 上线即把本次建表撤掉)
    assert not any("rollback_026" in m for m in MIGRATIONS)


# ===========================================================================
# §2 · 「N 条提示」明细出口
# ===========================================================================


def _py_runtime_strings(src: str) -> str:
    """把一段 Python 源码里**非注释、非 docstring** 的字符串字面量拼起来。

    只有这些才可能出现在响应/日志/用户面 —— 语义红线只该管这些。
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(src))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            parts.append(node.value)
    return "\n".join(parts)


def _ts_runtime_source(src: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释(含 JSX 里的 `{/* */}`)。理由同上。"""
    import re

    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


def _server_function_source(name: str) -> str:
    """把 server.py 里那**一个**端点函数的源码抠出来(不做全文件 grep = 不写弱锁)。"""
    import ast

    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment((ROOT / "server.py").read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"server.py 里找不到函数 {name}")


def test_advisory_endpoint_exists_and_reuses_ssot():
    src = _server_function_source("api_get_article_advisory")
    # 鉴权与文章详情同一道边界
    assert "_require_article_access(request, article_id)" in src
    # 🔴 计数与列表徽章**同一个函数**:前端显示 N 条、点开看到 M 条从源头不可能。
    #    锚必须钉在**调用表达式**上 —— 只断言 "_advisory_state" 出现过,
    #    把这一行整个换成写死值也能绿(import 行还在),那是弱锁(变异 M8 实测跑掉过)。
    assert "advisory_state, advisory_open_count = _advisory_state(quality_warning)" in src
    # 明细与写作大厅同一份聚合
    assert "aggregate_article_findings" in src
    # 高风险判定与 repair-finding 端点同源(两层同源,不是两份口径)
    assert "article_ai_repair_blocked" in src


def test_advisory_endpoint_never_claims_review_passed():
    """语义红线(工单点名):明细如实说"提示",不许包装成"审核通过"。

    🔴 判据只看**会出网的字符串字面量**,不看注释/docstring —— 第一版没分开,
       抓住的是我自己那句"不许包装成审核通过"的注释(判据自己坏,同型第 N 次)。
    """
    src = _server_function_source("api_get_article_advisory")
    runtime = _py_runtime_strings(src)
    assert "审核通过" not in runtime
    # 反向对照 ①:判据认得出这四个字 —— 同文件别处**确实**在说"审核通过"
    whole = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "审核通过" in whole
    # 反向对照 ②:证明 _py_runtime_strings 真的取到了本函数的字面量(不是恒空)
    assert "文章不存在" in runtime


def test_advisory_route_registered_in_member_seat_contract():
    from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES

    paths = {(p.method, p.path_template) for p in MEMBER_GEO_ROUTE_POLICIES}
    assert ("GET", "/api/articles/{article_id}/advisory") in paths
    # 反向对照:没登记的路径确实查不到(证明这张表不是"什么都在里面")
    assert ("GET", "/api/articles/{article_id}/never-registered") not in paths


def test_advisory_badge_has_an_entry_in_publish_center():
    """UI 接线锁:徽章必须真的变成可点入口,并把动作接到抽屉上。

    (工单的病根就是"看得到点不开";只加组件不接线 = 病没修。)
    """
    page = _ts_runtime_source((ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8"))
    assert "ArticleAdvisoryDrawer" in page
    # [WO_273 · 2026-09-23 重锚:冻结数 2 → 1] 原注释写「两处文章列表(未分发 / 已分发分组)」,与代码不符 ——
    #   改前那两处实为「代发 · 未分发列表」+「自助 · 未代发列表」;后者随浏览器插件自助发布退役
    #   (WO_273-A 21afc6084)整块删除(那一半的「不许回来」由 build 链第 69 步
    #   frontend/scripts/test-self-publish-retired.mjs 的 S1/S5 守:PublishCenter 自助模式键 0、界面无「自助」)。
    #   现役只剩代发未分发列表这 1 处:少接这一处 = 这个列表的徽章点不开。
    #   「已分发」分组要不要也接入口是产品问题,不在本锁(A 的 WO_273 交付单 §7 已记)。
    assert page.count("onOpenAdvisory={openAdvisoryFor}") == 1
    assert 'data-testid="advisory-badge-entry"' in page

    raw_drawer = (ROOT / "frontend/src/components/publishing/ArticleAdvisoryDrawer.tsx").read_text(encoding="utf-8")
    # 🔴 去注释再断言:头部注释里**也**写着这两个端点路径,拿原文当判据 →
    #    把代码里的 URL 改掉、注释没改,锁照样绿(变异 M12 实测跑掉过)。
    drawer = _ts_runtime_source(raw_drawer)
    assert "/repair-finding" in drawer
    assert "/api/articles/batch-advisory-continue" in drawer
    assert "onEdit" in drawer
    # 反向对照:证明去注释确实把注释吃掉了(否则上面三条又变回原文断言)
    assert "不新造第二套口径" in raw_drawer and "不新造第二套口径" not in drawer
    # 语义红线同样管前端 —— 同样只看去注释后的代码(注释不渲染)
    drawer_runtime = _ts_runtime_source(drawer)
    assert "审核通过" not in drawer_runtime
    # 反向对照:去注释没把正文一起吃掉
    assert "不影响发布" in drawer_runtime


# ===========================================================================
# §3 · 本轮调研结果报表
# ===========================================================================


class _FakeCursor:
    """按 SQL 片段派发假数据的只读游标。"""

    def __init__(self, plan):
        self._plan = plan
        self._rows: list[dict] = []

    def execute(self, sql, params=None):
        for needle, rows in self._plan:
            if needle in sql:
                self._rows = list(rows)
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, plan):
        self._plan = plan

    def cursor(self):
        return _FakeCursor(self._plan)

    def close(self):
        pass


def _install_fake_conn(monkeypatch, plan):
    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn(plan))


def test_build_round_report_shape(monkeypatch):
    from datetime import datetime, timezone

    t0 = datetime(2026, 8, 5, 10, 29, 56, tzinfo=timezone.utc)
    plan = [
        ("FROM geo_research_round WHERE round_id", [
            {"round_id": "round_x", "status": "completed", "started_at": t0,
             "finished_at": None, "created_at": t0},
        ]),
        ("GROUP BY platform, status", [
            {"platform": "doubao", "status": "success", "cnt": 1},
            {"platform": "deepseek", "status": "success", "cnt": 1},
            {"platform": "kimi", "status": "success", "cnt": 1},
            {"platform": "qwen", "status": "failed", "cnt": 1},
        ]),
        ("SELECT DISTINCT prompt_id, prompt_text", [
            {"prompt_id": 7, "prompt_text": "深圳GEO交付系统哪家靠谱"},
        ]),
        ("status = 'failed'", [
            {"prompt_text": "深圳GEO交付系统哪家靠谱", "platform": "qwen", "error_message": "timeout"},
        ]),
        ("GROUP BY a.domain", [
            {"domain": "licai.cofool.com", "citations": 20, "articles": 5, "platforms": ["doubao"]},
            {"domain": "ithome.com", "citations": 9, "articles": 3, "platforms": ["kimi", "qwen"]},
        ]),
        # 本轮之前被引用过的域名(→ 只有 licai 是"本轮新出现")
        ("c.cited_at < %s", [{"domain": "ithome.com"}]),
    ]
    _install_fake_conn(monkeypatch, plan)

    import services.media_domain_directory as dirmod

    monkeypatch.setattr(dirmod, "get_directory_entries",
                        lambda domains: {"licai.cofool.com": {"zh_name": "财富赢家网", "one_liner": "理财资讯", "source": "llm"}})
    monkeypatch.setattr(dirmod, "request_distillation", lambda domains: 0)

    from services.research_monitor.round_report import build_round_report

    rep = build_round_report("round_x")

    assert rep["has_data"] is True
    assert rep["prompt_count"] == 1
    assert rep["engine_count"] == 4
    assert rep["call_stats"]["total"] == 4
    assert rep["call_stats"]["success"] == 3
    assert rep["call_stats"]["failed"] == 1
    assert rep["citation_count"] == 29           # 生产实证那一轮就是 29 条
    assert rep["new_media_domains"] == ["licai.cofool.com"]
    assert [m["display_name"] for m in rep["media"]] == ["财富赢家网", "IT之家"]
    assert rep["media"][0]["is_new"] is True
    assert rep["media"][1]["is_new"] is False    # 反向对照:老媒体不许被标"新出现"
    assert rep["failed_calls"][0]["platform"] == "qwen"


def test_round_report_never_fabricates_score_delta(monkeypatch):
    """分值变化无数据源 → 如实标不可得,**绝不给一个数**。"""
    from datetime import datetime, timezone

    t0 = datetime(2026, 8, 5, tzinfo=timezone.utc)
    _install_fake_conn(monkeypatch, [
        ("FROM geo_research_round WHERE round_id", [
            {"round_id": "r", "status": "completed", "started_at": t0, "finished_at": None, "created_at": t0},
        ]),
    ])
    from services.research_monitor.round_report import build_round_report

    rep = build_round_report("r")
    assert rep["score_delta_available"] is False
    assert rep["score_delta_reason"]
    # 必须不命中:报表里不许出现任何 delta 数字字段
    assert not any(k for k in rep if "delta" in k and k not in ("score_delta_available", "score_delta_reason"))


def test_build_round_report_fail_soft(monkeypatch):
    """取数炸掉 → 空报表(has_data=False),不抛 —— 报表不许把主流程带崩。"""
    import db.connection as conn_mod

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(conn_mod, "get_connection", _boom)
    from services.research_monitor.round_report import build_round_report

    rep = build_round_report("round_x")
    assert rep["has_data"] is False
    assert rep["media"] == []


def test_round_report_endpoint_is_owner_scoped():
    """round_id 是可推测的时间戳串,**不能当凭证** —— 必须先反查任务行判属主。"""
    import api.research_selfserve_api as mod

    src = inspect.getsource(mod.round_report)
    assert "get_selfserve_task_by_round" in src
    assert 'int(task.get("user_id") or 0) != agent["user_id"]' in src
    assert "status_code=404" in src
    # 反向对照:非属主与不存在必须**同一个** 404(不给探测存在性的旁路)
    assert "status_code=403" not in src


def test_rounds_list_is_user_scoped():
    """历轮列表按 user_id 过滤 —— 行业是共享的,别人花的钱不该出现在我的列表里。"""
    from db.research_selfserve_db import list_user_selfserve_rounds

    src = inspect.getsource(list_user_selfserve_rounds)
    assert "WHERE user_id = %s AND industry_key = %s" in src
    # 反向对照:证明不是"只按行业查"
    assert "WHERE industry_key = %s\n" not in src


def test_round_report_flywheel_note_is_stated():
    """[§3.2] 反哺是自动的(生产已实证),用户缺的是**被告知它发生了**。"""
    import api.research_selfserve_api as mod

    src = inspect.getsource(mod.round_report)
    assert "flywheel_note" in src
    assert "AI 真实引用媒体榜" in src


def test_completion_toast_leads_to_the_report():
    """[§3.5] toast 是入口不是终点:完成那条 toast 必须能点进报表。"""
    page = _ts_runtime_source((ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8"))
    assert "ResearchRoundReportDialog" in page
    assert "setRoundReportId(rid); setRoundReportOpen(true);" in page
    # 历轮入口(花过的算力找得回凭证)
    assert "onOpenRounds=" in page
    panel = (ROOT / "frontend/src/components/publishing/MediaEffectivenessPanel.tsx").read_text(encoding="utf-8")
    assert 'data-testid="media-board-rounds-entry"' in panel


def test_board_click_dead_end_has_explicit_empty_state():
    """[§1 点击闭环] 从榜点过来搜不到时,给显式空态,不是通用的"无匹配媒体"。"""
    raw = (ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8")
    # 🔴 同样去注释:这两句文案在 state 声明处的注释里**也**写了一遍,
    #    拿原文当判据 → 空态被换回通用文案也抓不到(变异 M19 实测跑掉过)。
    page = _ts_runtime_source(raw)
    assert "renderMediaEmpty" in page
    assert "AI 常引用头部站点" in page
    assert "暂无对应可购渠道" in page
    assert "在媒体库里暂无对应可购渠道" in page
    # 反向对照:非榜来源的搜索仍保留原文案(不是把所有空态都改掉)
    assert "无匹配媒体" in page
