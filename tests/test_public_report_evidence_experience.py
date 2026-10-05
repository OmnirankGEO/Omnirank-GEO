from __future__ import annotations

from datetime import datetime

from services.public_report_presentation import build_public_report_presentation
from services.report_writer_v2 import build_module_3_raw_ai_appendix


def _presentation(results: dict, *, question: str = "哪家公司更值得推荐？") -> dict:
    return build_public_report_presentation(
        {
            "funnel": {
                "layers": [
                    {"key": "brand", "label": "品牌认知层", "detected": 1, "total": 1}
                ]
            },
            "client": {
                "modules": {
                    "1": {"conclusion_text": "形成了可核验的客户结论。"},
                    "3_raw": {"tests": [{"question": question, "results": results}]},
                }
            },
        },
        industry="生物科技",
        canonical_score=45,
        generated_at=datetime(2026, 7, 20, 8, 10),
        keyword_count=1,
        brand_name="岱林生物",
    )


def test_legacy_recommendation_signals_are_not_downgraded_to_mention() -> None:
    presentation = _presentation(
        {
            "deepseek": {
                "status": "answered",
                "brand_detected": True,
                "is_recommended": True,
                "full_response": (
                    "以下是几家较为靠谱的推荐：\n\n"
                    "1. **岱林生物**：技术成熟，具备多项自主知识产权，值得优先考虑。"
                ),
                "search_citations": [],
            },
            "qwen": {
                "status": "answered",
                "brand_detected": True,
                "is_recommended": False,
                "full_response": "岱林生物是一家从事生命科学仪器研发的企业。",
                "search_citations": [],
            },
        }
    )

    evidence = presentation["evidence"]["data"]["items"]
    assert [row["verdict"] for row in evidence] == ["recommended", "mentioned"]
    platforms = {row["platformName"]: row for row in presentation["platforms"]["data"]}
    assert platforms["DeepSeek"]["recommendRatePct"] == 100
    assert platforms["通义千问"]["recommendRatePct"] == 0


def test_legacy_top_position_without_positive_signal_is_candidate_not_explicit_recommendation() -> None:
    presentation = _presentation(
        {
            "deepseek": {
                "status": "answered",
                "brand_detected": True,
                "is_recommended": True,
                "full_response": "可对比岱林生物、竞品甲和竞品乙，具体需按参数核验。",
            }
        },
        question="细胞治疗隔离器有哪些候选？",
    )
    row = presentation["evidence"]["data"]["items"][0]
    assert row["verdict"] == "candidate"
    # [P0-3 · Owner 2026-07-26 裁决] 推荐口径放宽：**进候选（candidate）也算被推荐**。
    #   逐行 verdict 仍精确区分 candidate / recommended（上一条断言未变），
    #   汇总推荐率把候选计入 —— 旧严苛口径下推荐率恒 0%，正是本轮要修的缺陷。
    assert presentation["platforms"]["data"][0]["recommendRatePct"] == 100


def test_mature_target_context_is_recommendation_but_competitor_praise_does_not_spill() -> None:
    presentation = _presentation(
        {
            "deepseek": {
                "status": "answered",
                "brand_detected": True,
                "full_response": "岱林生物技术成熟，服务有保障，可以优先考虑。",
            },
            "qwen": {
                "status": "answered",
                "brand_detected": True,
                "full_response": "竞品甲技术成熟、实力强，而岱林生物在名单中仅被提及。",
            },
            "kimi": {
                "status": "answered",
                "brand_detected": True,
                "full_response": "岱林生物在名单中仅被提及，而竞品甲技术成熟、服务有保障。",
            },
        },
        question="杭州哪家公司技术比较成熟？",
    )
    rows = presentation["evidence"]["data"]["items"]
    assert [row["verdict"] for row in rows] == ["recommended", "candidate", "candidate"]


def test_awareness_description_remains_mention_and_markdown_structure_survives() -> None:
    answer = (
        "**岱林生物**是一家生命科学仪器企业。\n\n"
        "主要业务：\n\n"
        "1. 微生物检测；\n"
        "2. 制药装备。"
    )
    presentation = _presentation(
        {
            "deepseek": {
                "status": "answered",
                "brand_detected": True,
                "full_response": answer,
            }
        },
        question="岱林生物是做什么的？",
    )
    row = presentation["evidence"]["data"]["items"][0]
    assert row["verdict"] == "mentioned"
    assert row["answerExcerpt"] == answer
    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 本题「岱林生物是做什么的？」**就是**品牌定向题,
    #   而这条 3_raw 产物没有 layer_key(旧口径下无标签即按非定向题处理)。
    #   原断言 ==0 锁的正是那个 bug:定向题被算进竞争口径分母。
    #   报告 551 实证:通义/豆包/元宝 14.3% 的提及率**全部**来自这类题。
    #   新契约 = 文本判据兜住无标签/错标 → 竞争分母为空 → 推荐率给 None(不是 0)。
    #   None 与 0 必须可区分:0 是"测了没推荐",None 是"竞争口径本次没有样本"。
    platform_row = presentation["platforms"]["data"][0]
    assert platform_row["recommendRatePct"] is None
    assert platform_row["mentionRatePct"] is None
    # 品牌识别率仍看全部题(含定向题):它回答的是"AI 认不认识这个品牌"。
    assert platform_row["detectionRatePct"] == 100


def test_citation_zero_and_not_collected_remain_distinct() -> None:
    presentation = _presentation(
        {
            "qwen": {
                "status": "answered",
                "brand_detected": False,
                "full_response": "本次没有提到目标品牌。",
                "search_citations": [],
            },
            "deepseek": {
                "status": "answered",
                "brand_detected": False,
                "full_response": "本次没有提到目标品牌。",
            },
        }
    )
    platforms = {row["platformName"]: row for row in presentation["platforms"]["data"]}
    assert platforms["通义千问"]["citationCount"] == 0
    assert platforms["DeepSeek"]["citationCount"] is None


def test_raw_client_artifact_preserves_allowlisted_recommendation_and_public_citation_domain() -> None:
    module = build_module_3_raw_ai_appendix(
        {
            "diagnosis_data": {
                "ai_visibility_data": {
                    "engines_tested": ["deepseek"],
                    "detail_table": [
                        {
                            "question": "哪家公司更值得推荐？",
                            "results": {
                                "deepseek": {
                                    "brand_detected": True,
                                    "is_recommended": True,
                                    "target_outcome": "recommended",
                                    "full_response": "**岱林生物**值得优先考虑。",
                                    "search_citations": [
                                        {"url": "https://Evidence.EXAMPLE.com/path?token=PRIVATE"},
                                        {"url": "javascript:alert(1)"},
                                    ],
                                }
                            },
                        }
                    ],
                }
            }
        }
    )
    result = module["tests"][0]["results"][0]
    assert result["is_recommended"] is True
    assert result["target_outcome"] == "recommended"
    assert result["search_citations"] == [{"url": "https://evidence.example.com"}]
    assert "PRIVATE" not in str(module)
