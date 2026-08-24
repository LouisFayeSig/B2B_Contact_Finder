from __future__ import annotations

from app.benchmark import _contact_matches, summarize_benchmark_rows
from app.models import NOT_FOUND_LABEL


def test_summary_compares_candidate_with_existing_baseline() -> None:
    row = {
        "deployment": "cheap-model",
        "baseline_status": "success",
        "baseline_email": "contact@example.com",
        "baseline_phone": "01 23 45 67 89",
        "baseline_website": NOT_FOUND_LABEL,
        "candidate_status": "success",
        "candidate_email": "contact@example.com",
        "candidate_phone": NOT_FOUND_LABEL,
        "candidate_website": "https://example.com",
        "email_exact_match": True,
        "phone_exact_match": False,
        "website_exact_match": False,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "web_search_calls": 1,
        "latency_seconds": 2.0,
        "error_type": "",
    }

    summary = summarize_benchmark_rows([row])["deployments"]["cheap-model"]

    assert summary["candidate_success_rows"] == 1
    assert summary["exact_match_rate_on_baseline_found_pct"] == 50.0
    assert summary["new_candidate_contacts"] == 1
    assert summary["total_tokens"] == 120


def test_contact_comparison_normalizes_phone_and_website() -> None:
    assert _contact_matches("phone", "+33 1 23 45 67 89", "+33123456789")
    assert _contact_matches("website", "https://www.example.com/", "example.com")
