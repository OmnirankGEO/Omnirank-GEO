"""[Phase 2A 2026-06-07] DeepSeek(DashScope)引擎 search_citations 采集 · 函数级 mock

验证 query_dashscope_deepseek 把 API 返回的 output.search_info.search_results 如实写进
search_citations(此前硬编码 [] "引用由 qwen 统一采集"),使 deepseek 引擎来源能进入
诊断 detail_table.results.deepseek.search_citations → 经 Phase 1 聚合进权威背书。

不碰 prompt / brand_detected / 检出率 / 评分 / 扣费 —— 仅来源采集口径。
"""
import asyncio
import json


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _patch_deepseek_io(monkeypatch, ai_tester, payload, analyze=None):
    """mock 掉网络 + 可见度分析,只跑 query_dashscope_deepseek 的来源采集逻辑。"""
    async def _fake_tracked_post(*args, **kwargs):
        return _FakeResp(payload)

    async def _fake_analyze(ai_response, query, check_brand, engine_name):
        return analyze or {"brand_detected": False, "mentioned_brands": [], "answer_summary": ""}

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_tracked_post)
    monkeypatch.setattr(ai_tester, "_analyze_visibility", _fake_analyze)


def test_deepseek_captures_search_info_citations(monkeypatch):
    """search_info.search_results 有来源 → search_citations 如实采集(非空)。"""
    from tools.ai_visibility import ai_tester

    payload = {
        "output": {
            "choices": [{"message": {"content": "这是一段不含链接的回答正文。"}}],
            "search_info": {
                "search_results": [
                    {"index": 1, "title": "央视网报道", "url": "https://www.cctv.com/x"},
                    {"index": 2, "title": "新华网", "url": "https://www.xinhuanet.com/y"},
                ]
            },
        }
    }
    _patch_deepseek_io(monkeypatch, ai_tester, payload)

    resp = asyncio.run(ai_tester.query_dashscope_deepseek("测试问题", "某品牌"))
    data = json.loads(resp.content[0]["text"])

    cits = data.get("search_citations") or []
    assert len(cits) == 2, f"期望 2 条来源,实际 {len(cits)}"
    urls = {c.get("url") for c in cits}
    assert "https://www.cctv.com/x" in urls
    assert "https://www.xinhuanet.com/y" in urls
    assert data.get("web_search_enabled") is True


def test_deepseek_empty_citations_when_no_search_info(monkeypatch):
    """search_info 缺失 → search_citations = [](不抛 · 与 qwen 同口径降级)。"""
    from tools.ai_visibility import ai_tester

    payload = {"output": {"choices": [{"message": {"content": "纯文本回答,无来源无链接。"}}]}}
    _patch_deepseek_io(monkeypatch, ai_tester, payload)

    resp = asyncio.run(ai_tester.query_dashscope_deepseek("q", "brand"))
    data = json.loads(resp.content[0]["text"])
    assert data.get("search_citations") == []


def test_deepseek_does_not_alter_brand_detected(monkeypatch):
    """采集来源不改检出口径:_analyze_visibility 的 brand_detected/mentioned_brands 原样透传。"""
    from tools.ai_visibility import ai_tester

    payload = {
        "output": {
            "choices": [{"message": {"content": "回答正文"}}],
            "search_info": {"search_results": [{"index": 1, "title": "t", "url": "https://example.com/p"}]},
        }
    }
    _patch_deepseek_io(
        monkeypatch, ai_tester, payload,
        analyze={"brand_detected": True, "mentioned_brands": ["某品牌"], "answer_summary": "命中"},
    )

    resp = asyncio.run(ai_tester.query_dashscope_deepseek("q", "某品牌"))
    data = json.loads(resp.content[0]["text"])
    assert data.get("brand_detected") is True
    assert data.get("mentioned_brands") == ["某品牌"]
    assert len(data.get("search_citations") or []) == 1


def test_deepseek_citations_flow_into_diagnosis_citations():
    """闭环:deepseek 引擎来源经 Phase 1 _extract_diagnosis_citations 进入聚合
    (platform=deepseek · brand_detected → is_detected/mention_type=direct)。"""
    from services.source_authority_analyzer import _extract_diagnosis_citations

    detail_table = [
        {
            "question": "推荐这个行业的品牌",
            "results": {
                "deepseek": {
                    "brand_detected": True,
                    "search_citations": [
                        {"url": "https://www.cctv.com/x", "title": "央视"},
                    ],
                }
            },
        }
    ]
    flat = _extract_diagnosis_citations(detail_table)
    hit = [c for c in flat if c.get("platform") == "deepseek" and c.get("url") == "https://www.cctv.com/x"]
    assert hit, "deepseek 引擎来源未进入 diagnosis_citations"
    assert hit[0]["is_detected"] is True
    # [P0-3 2026-07-26] 词表归一：direct → mentioned（"出现在回答里"≠"被推荐"，
    #   绝不升成 recommended）。断言的仍是"命中即落提及档"这条语义。
    assert hit[0]["mention_type"] == "mentioned"
