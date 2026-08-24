from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.llm_service import AzureFoundryWebSearchService, ModelResponseError
from app.models import CompanyResult, CompanyRow


class AzureFoundryWebSearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(AzureFoundryWebSearchService)
        self.service._logger = logging.getLogger("tests.llm")
        self.service._config = SimpleNamespace(
            azure_foundry_model_deployment="gpt-5.6-luna",
            azure_foundry_reasoning_effort="none",
            search_audit_enabled=True,
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
        self.assertIsInstance(request["input"], str)
        self.assertIn("Entreprise Test", request["input"])
        self.assertIn("instructions", request)
        self.assertNotIn("temperature", request)

    def test_request_omits_audit_payload_when_audit_is_disabled(self) -> None:
        self.service._config.search_audit_enabled = False

        request = self.service._build_request(self.company, max_output_tokens=600)

        self.assertNotIn("include", request)

    def test_search_result_contains_validated_contacts_and_sources(self) -> None:
        response = {
            "id": "resp_test",
            "model": "gpt-5.6-luna-2026-07-09",
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

    def test_invalid_json_raises_recoverable_processing_error(self) -> None:
        with self.assertRaises(ModelResponseError):
            self.service._parse_json_response("pas du json")

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
