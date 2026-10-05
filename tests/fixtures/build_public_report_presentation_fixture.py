"""Emit a synthetic V2 public response through the real presentation builder."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.public_report_presentation import build_public_report_presentation  # noqa: E402


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


generated_at = datetime(2026, 7, 19, 12, 10)
modules = {
    "funnel": {
        "total_score": 45,
        "level_meta": {"partial_sample": True, "level_capped": False},
        "layers": [{
            "key": "brand", "label": "品牌认知层", "weight": 20,
            "detected": 2, "total": 3, "rate_pct": 66.7, "score": 45,
            "data_sufficient": False, "confidence": "low",
        }],
    },
    "client": {
        "modules": {
            "1": {"conclusion_text": "品牌已被识别，但推荐证据仍需补齐。"},
            "1_interpretation": {
                "headline": "品牌已被识别，但推荐证据仍需补齐。",
                "business_translation": "提及不等于推荐，需要继续核对证据。",
            },
            "3_raw": {
                "tests": [
                    {
                        "question": "智能家居品牌有哪些候选？",
                        "results": {
                            "deepseek": {
                                "platform_key": "deepseek_metaso_proxy",
                                "status": "answered",
                                "brand_detected": True,
                                "target_outcome": "candidate_only",
                                "full_response": "澜川智能家居被列入候选。",
                            },
                            "doubao": {
                                "platform_key": "doubao_ark_api_search",
                                "status": "answered",
                                "brand_detected": False,
                                "full_response": "本次回答没有提到该品牌。",
                            },
                        },
                    }
                ]
            },
            "3_competition": {"top_brands": [], "client_detected_count": 1, "valid_total": 2},
            "6": {"todos": [{"priority": "P0", "issue": "案例证据不足", "action": "补齐可核验案例"}]},
        }
    },
}
presentation = build_public_report_presentation(
    modules,
    industry="智能家居",
    canonical_score=45,
    generated_at=generated_at,
    keyword_count=1,
)
body = {
    "status": "success",
    "report": {
        "brand_name": "澜川智能家居",
        "score": 45,
        "level": "边缘级",
        "content": "## 客户报告正文\n\n这是经过 Markdown 渲染的客户结论。",
        "keyword_count": 1,
        "report_version": "v2",
        "audience": "client",
        "data_completeness_score": 62,
        "data_completeness_breakdown": {"level": "可用", "missing_summary": None},
        "created_at": "2026-07-19T12:00:00",
        "report_v2_generated_at": generated_at.isoformat(),
        "branding_status": "platform",
        "presentation": presentation,
    },
}
print(json.dumps(body, ensure_ascii=False))
