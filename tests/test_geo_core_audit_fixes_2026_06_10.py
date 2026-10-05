# -*- coding: utf-8 -*-
"""GEO 独立批(audit 5×P1 · 2026-06-10):
A 批量重写 LLM 失败计成 success → 全失败退费永不触发
B start-articles 全失败不退费(gather 吞异常 · 线程退费路径死)
C SSE 监测断连 settle 整块被跳过(GeneratorExit/CancelledError 接不住)→ 冻结悬挂
D lite 诊断无总超时 → 卡死成永久 NULL + 650 算力悬挂
E _run_diagnosis_impl 吞异常不 re-raise → 失败也 commit_freeze 扣费"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

# batch_rewrite_articles 函数体内 `from db.diagnosis_db import get_connection`(实际未使用的死 import)
# 会触发 db.connection 模块级建池连真 DB → 本机 mock 环境用假模块替身(函数逻辑不依赖它)
_FAKE_DIAG_DB = MagicMock()


# ---------- A 批量重写计数分流(值级) ----------

def _make_service():
    from writing.article_generator_service import ArticleGeneratorService
    return ArticleGeneratorService(quote_id=1, brand_name="测试品牌", industry="测试行业")


def test_batch_rewrite_error_dict_counts_failed():
    """rewrite_article 失败 return {'error':...}(不 raise)→ 必须计 failed(旧版计 success=回归核心)。"""
    svc = _make_service()

    async def _fake_rewrite(topic_id):
        return {"error": "LLM 失败", "topic_id": topic_id}

    with patch.dict(sys.modules, {"db.diagnosis_db": _FAKE_DIAG_DB}), \
         patch.object(svc, "rewrite_article", side_effect=_fake_rewrite):
        results = asyncio.run(svc.batch_rewrite_articles([11, 12, 13], max_concurrent=2))
    assert results["success"] == 0
    assert results["failed"] == 3  # 端点全失败退费判据 failed==N 现在可成立
    assert len(results["errors"]) == 3


def test_batch_rewrite_success_and_raise_mixed():
    """成功 dict 计 success;raise 计 failed;混合各归各。"""
    svc = _make_service()

    async def _fake_rewrite(topic_id):
        if topic_id == 1:
            return {"id": 999, "content": "ok"}
        if topic_id == 2:
            raise RuntimeError("硬异常")
        return {"error": "软失败", "topic_id": topic_id}

    with patch.dict(sys.modules, {"db.diagnosis_db": _FAKE_DIAG_DB}), \
         patch.object(svc, "rewrite_article", side_effect=_fake_rewrite):
        results = asyncio.run(svc.batch_rewrite_articles([1, 2, 3], max_concurrent=3))
    assert results["success"] == 1
    assert results["failed"] == 2


# ---------- B start-articles 全失败 raise(source-scan) ----------

def test_start_articles_all_failed_raises():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "gen_results = await service.generate_articles(" in src
    # [audit #2 返修] 真失败仍 raise 退费;并发接管 skipped 是良性并发,不能误判失败退费。
    assert "if _ok == 0:" in src, "0 产出仍必须集中判定"
    assert "批量生成全部失败" in src
    assert "0 产出(全部跳过" in src, "非接管 skipped 的 0 产出须有退费分支"
    assert "_takeover_skip_reasons" in src
    assert "全部 topic 已被其他写作批次接管" in src
    assert "r.get('error')" in src and "r.get('id')" in src


# ---------- #2 返修(2026-06-10)----------

def _load_strip():
    """exec 提取 _strip_internal_pricing_fields(selection_api import 会 eager 连库,不能直接 import)。"""
    import re
    import textwrap
    sel = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    m = re.search(r"(def _strip_internal_pricing_fields\(payload\)[\s\S]*?)\n\ndef ", sel)
    assert m, "未定位 _strip_internal_pricing_fields"
    ns = {"_CUSTOMER_INTERNAL_KW_FIELDS": {"competitor_count", "effective_competition", "target_share"}}
    exec(textwrap.dedent(m.group(1)), ns)
    return ns["_strip_internal_pricing_fields"]


def test_2_strip_removes_tier_target_share():
    """[#2 返修 行为] tier 级 target_share(内部 SOV · 违元指令#11)必须从客户面出口剥除。"""
    strip = _load_strip()
    payload = {
        "tiers": {
            "entry": {"label": "入门版", "target_share": 0.10, "ai_probability": "50%"},
            "standard": {"label": "标准版", "target_share": 0.20, "ai_probability": "65%"},
            "flagship": {"label": "旗舰版", "target_share": 0.30, "ai_probability": "75%"},
        },
        "keywords": [{"keyword": "x", "target_share": 0.20, "competitor_count": 9}],
    }
    strip(payload)
    for tk, tv in payload["tiers"].items():
        assert "target_share" not in tv, f"tier {tk} 仍裸露 target_share"
        assert tv.get("ai_probability"), "ai_probability 话术须保留(客户渲染用)"
        assert tv.get("label"), "label 须保留"
    # 词级内部字段也脱敏(原有行为不回退)
    assert "target_share" not in payload["keywords"][0]
    assert "competitor_count" not in payload["keywords"][0]


def test_2_distill_publications_null_brand_fail_closed():
    """[#2 返修 E4] distill(LLM 重写)/publications(跨租户读)端点 NULL-brand fail-closed。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "require_diagnosis_access(request, diagnosis_id, allow_null=False)" in src, "distill 须 allow_null=False"
    assert "require_quote_access(request, quote_id, allow_null=False)" in src, "publications 须 allow_null=False"
    ba = (ROOT / "auth" / "brand_access.py").read_text(encoding="utf-8")
    assert "def require_diagnosis_access(request: Request, diagnosis_id: int, allow_null: bool = True)" in ba
    assert "def require_quote_access(request: Request, quote_id: int, allow_null: bool = True)" in ba


# ---------- C SSE settle finally 化(source-scan) ----------

def test_sse_settle_in_finally_with_shield():
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    # settle 在 finally(断连 BaseException 也必经)
    i_finally = src.find("finally:\n        # ==========================================")
    assert i_finally > 0, "settle 必须在 finally 块"
    tail = src[i_finally:i_finally + 4000]
    assert "release_freeze" in tail and "commit_freeze" in tail
    # 完整跑完才 commit:_sse_completed 判据
    assert "execution_error is None and _sse_completed" in tail
    # 断连走 release + 区分 reason
    assert "disconnected" in tail
    # shield 防 settle 自身被二次取消
    assert "asyncio.shield(_settle_task)" in tail
    # 断连止损:cancel 未完成引擎任务
    assert "_et.cancel()" in tail
    # completed 标志设在统一 save + 标 completed 之后(蒸馏/通知/达标前)
    i_completed_mark = src.find('_sse_completed = True')
    i_update_completed = src.find('update_task_status(task_id, "completed"')
    i_distill = src.find("DDS: 异步触发数据蒸馏")
    assert i_update_completed < i_completed_mark < i_distill


# ---------- D 诊断总超时 + lite wait_for(source-scan) ----------

def test_diagnosis_total_timeout_wrapped():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "_DIAGNOSIS_TOTAL_TIMEOUT_SECONDS = 1800" in src
    assert "asyncio.wait_for(\n                _run_diagnosis_impl(request, session_id, creator_user_id),\n                timeout=_DIAGNOSIS_TOTAL_TIMEOUT_SECONDS,\n            )" in src


def test_lite_report_has_timeout():
    src = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")
    # lite 路径 generate_enhanced_report 必须包 wait_for(600) 对齐 full
    i_lite = src.find("enhanced_report = await asyncio.wait_for(\n                generate_enhanced_report(")
    assert i_lite > 0, "lite 路径报告生成必须包 asyncio.wait_for"
    assert "timeout=600" in src[i_lite:i_lite + 1400]
    # 写盘 IO 包 try(磁盘满不致整体 raise 错扣费)
    assert "JSON 导出写盘失败" in src
    assert "Markdown 导出写盘失败" in src


# ---------- E 吞异常 re-raise(source-scan) ----------

def test_diagnosis_impl_reraises():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    # _run_diagnosis_impl 的 except 块末尾必须 raise(让外层走 release_freeze)
    i = src.find("manager._set_status(session_id, error_status)")
    assert i > 0
    block = src[i:i + 700]
    assert "\n        raise" in block, "_run_diagnosis_impl except 末尾必须 re-raise"
    # 外层 release 分支(修活)参数与 freeze 对称:task_ref=session_id + user_id
    i_rel = src.find("from middleware.billing import release_freeze\n                        r = await release_freeze(")
    assert i_rel > 0
    rel_block = src[i_rel:i_rel + 400]
    assert "task_ref=session_id" in rel_block and "user_id=creator_user_id" in rel_block
