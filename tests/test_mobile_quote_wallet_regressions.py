"""Mobile GEO sales regressions reported on 2026-06-09.

These are mostly contract/static guards because the bugs crossed frontend
display code and backend SQL aggregation. They pin the business wording and
data-source choices that caused mobile users to see inconsistent balances,
wrong industries, missing quote history, and mismatched writing project totals.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_customer_wallet_uses_global_wallet_totals_and_scrollable_mobile_table():
    src = _read("frontend/src/pages/Customer/CreditWallet.tsx")

    assert "useWallet" in src
    assert "wallet.totalPoints" in src
    assert "wallet.paidPointsDisplayed" in src
    assert "wallet.paidPointsDisplayed + wallet.commissionPoints" not in src
    assert "wallet.bonusPointsDisplayed" in src
    assert 'label="充值算力"' in src
    assert 'label="赠送算力"' in src
    assert 'className="overflow-x-auto"' in src
    assert 'className="min-w-[640px] text-sm"' in src


def test_quote_industry_classifier_keeps_jade_jewelry_out_of_apparel():
    src = _read("frontend/src/pages/Quote/OnlineQuoteFlow.tsx")

    assert "label: '珠宝饰品'" in src
    assert "'翡翠'" in src
    assert "'玉石'" in src
    assert "preferIndustryFromBusinessText" in src
    apparel_line = next(line for line in src.splitlines() if "label: '服装纺织'" in line)
    assert "'珠宝'" not in apparel_line
    assert "'饰品'" not in apparel_line
    assert "'定制'" not in apparel_line


def test_online_quote_recovers_quotes_without_selection_sessions():
    src = _read("frontend/src/pages/Quote/OnlineQuoteFlow.tsx")

    assert "quote:" in src
    assert "/api/quotes" in src
    assert "missingQuoteSessions" in src
    assert "createSessionFromQuotePlaceholder" in src


def test_online_quote_has_local_keyword_fallback_for_network_breaks():
    src = _read("frontend/src/pages/Quote/OnlineQuoteFlow.tsx")

    assert "buildFallbackExpandedKeywords" in src
    assert "AI扩词连接中断" in src
    assert "本地兜底" in src


def test_online_quote_does_not_present_underfilled_or_stale_candidates_as_complete():
    src = _read("frontend/src/pages/Quote/OnlineQuoteFlow.tsx")

    assert "minimumExpansionCandidateCount" in src
    assert "mergeExpandedKeywordCandidates" in src
    assert "keywordRequestGenerationRef" in src
    assert "上游有效候选较少" in src
    assert "setExpanded([])" in src


def test_writing_project_list_is_per_quote_not_brand_rollup():
    src = _read("db/diagnosis_db.py")
    segment = src[src.index("def get_writing_projects"):src.index("def get_writing_project_detail")]

    assert "按 quote 展示" in segment
    assert "GROUP BY brand_id" not in segment
    assert "SUM(keyword_count)" not in segment
    assert "ARRAY[id]" in segment
