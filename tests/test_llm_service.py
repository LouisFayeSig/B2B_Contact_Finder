from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.llm_service import AzureFoundryWebSearchService, ModelResponseError
from app.models import CompanyResult, CompanyRow


class AzureFoundryWebSearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(AzureFoundryWebSearchService)
        self.service._logger = logging.getLogger("tests.llm")
        self.service._config = SimpleNamespace(
            azure_foundry_model_deployment="gpt-5.6-luna",
            azure_foundry_reasoning_effort="none",
            web_search_context_size="default",
            search_audit_enabled=True,
            site_extraction_enabled=False,
        )
        self.company = CompanyRow(
            row_index=2,
            siret="12345678901234",
            company_name="Entreprise Test",
        )

    def test_request_includes_official_web_search_sources(self) -> None:
        request = self.service._build_request(self.company, max_output_tokens=600)

        self.assertEqual(request["include"], ["web_search_call.action.sources"])
        self.assertEqual(request["tools"], [{"type": "web_search"}])
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertEqual(request["tool_choice"], "required")
        self.assertIsInstance(request["input"], str)
        self.assertIn("Entreprise Test", request["input"])
        self.assertIn("instructions", request)
        self.assertNotIn("temperature", request)
        self.assertIn("website_type", request["text"]["format"]["schema"]["required"])

    def test_request_omits_audit_payload_when_audit_is_disabled(self) -> None:
        self.service._config.search_audit_enabled = False

        request = self.service._build_request(self.company, max_output_tokens=600)

        self.assertNotIn("include", request)

    def test_request_can_reduce_web_search_context(self) -> None:
        self.service._config.web_search_context_size = "low"

        request = self.service._build_request(self.company, max_output_tokens=600)

        self.assertEqual(
            request["tools"],
            [{"type": "web_search", "search_context_size": "low"}],
        )

    def test_search_result_contains_validated_contacts_and_sources(self) -> None:
        response = {
            "id": "resp_test",
            "model": "gpt-5.6-luna-2026-07-09",
            "usage": {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
            "output_text": (
                '{"email":"contact@example.com","phone":"abc","website":"example.com",'
                '"email_source":"https://example.com/contact",'
                '"phone_source":"https://example.com/contact",'
                '"website_source":"https://example.com/contact",'
                '"identity_verified":true,"identity_match_type":"siret",'
                '"identity_source":"https://example.com/contact"}'
            ),
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"sources": [{"type": "url", "url": "https://example.com/contact#form"}]},
                }
            ],
        }

        with patch.object(self.service, "_request_with_token_retry", return_value=response):
            result = self.service.search_company_contact(self.company)

        self.assertEqual(result.email, "contact@example.com")
        self.assertEqual(result.phone, "Non trouvé")
        self.assertEqual(result.website, "https://example.com")
        self.assertEqual(result.sources, ["https://example.com/contact"])
        self.assertEqual(result.email_source, "https://example.com/contact")
        self.assertEqual(result.phone_source, "Non trouvé")
        self.assertEqual(result.response_id, "resp_test")
        self.assertEqual(result.model_snapshot, "gpt-5.6-luna-2026-07-09")
        self.assertEqual(result.total_tokens, 165)
        self.assertEqual(result.web_search_calls, 1)

    def test_search_result_omits_sources_when_audit_is_disabled(self) -> None:
        self.service._config.search_audit_enabled = False
        response = {
            "output_text": (
                '{"email":"contact@example.com","phone":"Non trouvé",'
                '"website":"Non trouvé","email_source":"https://example.com",'
                '"phone_source":"Non trouvé","website_source":"Non trouvé",'
                '"identity_verified":true,"identity_match_type":"siret",'
                '"identity_source":"https://example.com"}'
            ),
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"sources": [{"type": "url", "url": "https://example.com"}]},
                }
            ],
        }

        with patch.object(self.service, "_request_with_token_retry", return_value=response):
            result = self.service.search_company_contact(self.company)

        self.assertEqual(result.sources, [])

    def test_audit_accepts_a_page_fetched_by_deterministic_extraction(self) -> None:
        self.service._config.site_extraction_enabled = True

        class SiteExtractorStub:
            def enrich(self, company: CompanyRow, result: CompanyResult) -> CompanyResult:
                payload = result.model_dump()
                payload.update(
                    {
                        "phone": "01 23 45 67 89",
                        "phone_source": "https://example.com/contact",
                        "phone_extraction_method": "tel_link",
                        "deterministic_pages": ["https://example.com/contact"],
                        "deterministic_fields_found": 1,
                        "sources": [*result.sources, "https://example.com/contact"],
                    }
                )
                return CompanyResult.model_validate(payload)

        self.service._site_extractor = SiteExtractorStub()
        response = {
            "output_text": (
                '{"email":"Non trouvé","phone":"Non trouvé","website":"https://example.com",'
                '"email_source":"Non trouvé","phone_source":"Non trouvé",'
                '"website_source":"https://example.com","identity_verified":true,'
                '"identity_match_type":"siret","identity_source":"https://example.com"}'
            ),
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"sources": [{"type": "url", "url": "https://example.com"}]},
                }
            ],
        }

        with patch.object(self.service, "_request_with_token_retry", return_value=response):
            result = self.service.search_company_contact(self.company)

        self.assertEqual(result.phone, "01 23 45 67 89")
        self.assertIn("https://example.com/contact", result.sources)

    def test_invalid_json_raises_recoverable_processing_error(self) -> None:
        with self.assertRaises(ModelResponseError):
            self.service._parse_json_response("pas du json")

    def test_invalid_json_is_saved_then_retried_once(self) -> None:
        invalid_response = {
            "id": "resp_bad",
            "output_text": "pas du json",
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }
        valid_response = {
            "id": "resp_ok",
            "usage": {"input_tokens": 110, "output_tokens": 30, "total_tokens": 140},
            "output_text": (
                '{"email":"Non trouvé","phone":"Non trouvé","website":"Non trouvé",'
                '"website_type":"not_found","email_source":"Non trouvé",'
                '"phone_source":"Non trouvé","website_source":"Non trouvé",'
                '"identity_verified":false,"identity_match_type":"none",'
                '"identity_source":"Non trouvé"}'
            ),
        }
        self.service._invalid_response_journal = Mock()

        with patch.object(
            self.service,
            "_request_with_token_retry",
            side_effect=[invalid_response, valid_response],
        ) as request:
            response, result, attempted_responses = self.service._request_valid_result(self.company)

        self.assertEqual(response["id"], "resp_ok")
        self.assertTrue(result.is_not_found)
        self.assertEqual(len(attempted_responses), 2)
        self.assertEqual(
            self.service._aggregate_usage(attempted_responses),
            {"input_tokens": 210, "output_tokens": 50, "total_tokens": 260},
        )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["max_output_tokens"], 600)
        self.assertEqual(request.call_args_list[1].kwargs["max_output_tokens"], 1200)
        self.service._invalid_response_journal.append.assert_called_once()

    def test_second_invalid_json_is_saved_then_raised(self) -> None:
        self.service._invalid_response_journal = Mock()
        invalid_response = {"output_text": "toujours invalide"}

        with (
            patch.object(
                self.service,
                "_request_with_token_retry",
                side_effect=[invalid_response, invalid_response],
            ),
            self.assertRaises(ModelResponseError),
        ):
            self.service._request_valid_result(self.company)

        self.assertEqual(self.service._invalid_response_journal.append.call_count, 2)

    def test_audit_rejects_contact_with_unconsulted_evidence(self) -> None:
        result = CompanyResult(
            email="contact@example.com",
            email_source="https://untrusted.example/contact",
            identity_verified=True,
            identity_match_type="siret",
            identity_source="https://untrusted.example/legal",
        )

        filtered = self.service._apply_audit_evidence_policy(
            result,
            ["https://consulted.example/page"],
        )

        self.assertTrue(filtered.is_not_found)
        self.assertFalse(filtered.identity_verified)


if __name__ == "__main__":
    unittest.main()
