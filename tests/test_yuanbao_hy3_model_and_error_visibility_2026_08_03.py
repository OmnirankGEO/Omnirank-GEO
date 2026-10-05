"""[工单 2026-08-03] 元宝模型改回 hy3 + 供应商错误可见性 · 锁。

生产实证(2026-08-03,omnirank-green 容器内,同一 key 同一端点只换模型名):
  · `hy3-preview` → HTTP 402 / error.code=401008(免费体验包耗尽且未开后付费);
    控制台标注该模型 **2026-08-31 下线**。
  · `hy3`         → HTTP 200,`search_results` 11 条真实 URL,
    `usage.tool_usage={"web_search_call":3}`,prompt_tokens 16039;
    成对对照(同模型不带 web_search_options)prompt_tokens 35 / search_results=None。

这组锁钉住三件事:①四处血缘同步 ②价目表两条并存 ③错误体不再被吞。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai_surface_monitoring.adapters.openai_compat import (  # noqa: E402
    _classify_provider_error,
    _redact_provider_error,
)
from services.ai_surface_monitoring.lineage import SURFACE_SPECS  # noqa: E402
from tools.llm_call_tracker import PRICING_TABLE as PRICING  # noqa: E402


TOKENHUB_402_BODY = {
    "error": {
        "type": "gateway_error",
        "code": "401008",
        "message": "The free trial quota for the service has been exhausted and postpaid "
                   "billing is not enabled, so the service cannot be accessed.",
        "message_zh": "服务免费体验额度已耗尽，且未开启后付费，无法正常访问。",
    }
}


# ---------------------------------------------------------------- 血缘同步
def test_lineage_uses_hy3_not_preview():
    spec = SURFACE_SPECS["yuanbao_hy3_tokenhub"]
    assert spec.default_model_key == "hy3"
    # 反向断言:preview 8/31 下线,任何再回退到它的改动都必须红
    assert spec.default_model_key != "hy3-preview"
    # 检索仍必须声明开启 —— 换模型不能顺手把联网检索关掉
    assert spec.default_search_enabled is True
    assert spec.search_provider == "tencent_tokenhub"
    assert spec.env_key_var == "HY3_API_KEY"


def test_all_four_lineage_sites_agree_on_model():
    """四处血缘必须同一个模型名。07-27 就是漏了第 4 处导致成本按旧模型算。"""
    from db.monitoring_db import _estimate_token_cost_placeholder  # noqa: F401
    import inspect

    from tools.monitoring import batch_monitor
    import db.monitoring_db as mdb

    mdb_src = inspect.getsource(mdb)
    bm_src = inspect.getsource(batch_monitor)
    # 执行层映射里,yuanbao 必须绑定 hy3
    assert '"yuanbao": ("tencent_tokenhub", "hy3")' in mdb_src
    assert '"yuanbao": ("tencent_tokenhub", "hy3", "ai_search", "tencent_tokenhub")' in bm_src
    # 反向对照:证明这两个断言不是"什么都搜得到"——旧串必须已经不在执行行里
    assert '"yuanbao": ("tencent_tokenhub", "hy3-preview")' not in mdb_src
    assert '"yuanbao": ("tencent_tokenhub", "hy3-preview", "ai_search"' not in bm_src


def test_pricing_keeps_both_models():
    """新模型必须有价(否则落 DEFAULT_PRICING 静默算错);旧模型条目**不许删**(历史行要算价)。"""
    assert ("tencent_tokenhub", "hy3") in PRICING
    assert ("tencent_tokenhub", "hy3-preview") in PRICING, "旧价目条目删了 → 历史数据算价失真"
    assert PRICING[("tencent_tokenhub", "hy3")]["input"] > 0


# ---------------------------------------------------------------- 错误可见性
def test_billing_error_is_classified_as_needing_human():
    got = _classify_provider_error(402, TOKENHUB_402_BODY)
    assert got.startswith("billing_or_quota")
    # 反向对照:限流不该被归成"需人工"(它会自愈),否则分类没有判别力
    assert not _classify_provider_error(429, {"error": "too many requests"}).startswith("billing_or_quota")


def test_classification_distinguishes_each_family():
    assert _classify_provider_error(401, {}).startswith("auth")
    assert _classify_provider_error(403, {}).startswith("auth")
    assert _classify_provider_error(429, {}).startswith("rate_limit")
    assert _classify_provider_error(404, {}).startswith("not_found")
    assert _classify_provider_error(400, {}).startswith("bad_request")
    assert _classify_provider_error(500, {}).startswith("provider_error")


def test_chinese_quota_wording_also_classified_without_402():
    """别的供应商可能用 200/400 + 中文说欠费 —— 只认状态码会漏。"""
    got = _classify_provider_error(400, {"msg": "账户余额不足，请充值"})
    assert got.startswith("billing_or_quota")


def test_redaction_keeps_reason_and_drops_credentials():
    body = dict(TOKENHUB_402_BODY)
    body["echo"] = "Authorization: Bearer sk-AAAAAAAAAAAAAAAAAAAAAAAA"
    out = _redact_provider_error(body)
    # 原因必须留下 —— 这正是当初缺的那句话
    assert "401008" in out
    assert "后付费" in out or "postpaid" in out
    # 凭据必须打掉
    assert "sk-AAAAAAAAAAAAAAAAAAAAAAAA" not in out
    assert "<redacted>" in out


def test_redaction_is_single_line_and_bounded():
    out = _redact_provider_error({"m": "行1\n行2\n" + "x" * 5000})
    assert "\n" not in out, "多行会破坏 grep/单行日志"
    assert len(out) <= 400
