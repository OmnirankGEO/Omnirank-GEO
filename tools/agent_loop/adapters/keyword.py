from __future__ import annotations

from tools.keyword_expander import expand_keywords_for_client


async def explore(
    seed: str,
    industry: str = "",
    platform: str = "",
    limit: int = 20,
) -> dict:
    result = await expand_keywords_for_client(
        core_keywords=[seed],
        industry=industry,
        target_count=max(1, min(int(limit or 20), 30)),
        profile_data={"platform": platform} if platform else None,
    )
    keywords = result.get("keywords") if isinstance(result, dict) else []
    return {"source": "keyword_expander", "seed": seed, "keywords": (keywords or [])[:limit], "raw": result}
