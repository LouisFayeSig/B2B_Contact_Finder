from __future__ import annotations

import unittest

from app.models import NOT_FOUND_LABEL, CompanyResult, WebsiteType


class CompanyResultTests(unittest.TestCase):
    def test_valid_contacts_are_preserved_and_url_is_normalized(self) -> None:
        result = CompanyResult(
            email="contact@example.com",
            phone="+33 1 23 45 67 89",
            website="example.com/contact",
            website_type="official_site",
            identity_verified=True,
            identity_match_type="siret",
        )

        self.assertEqual(result.email, "contact@example.com")
        self.assertEqual(result.phone, "01 23 45 67 89")
        self.assertEqual(result.website, "https://example.com/contact")
        self.assertEqual(result.website_type, WebsiteType.OFFICIAL_SITE)
        self.assertFalse(result.is_not_found)

    def test_invalid_contacts_are_rejected(self) -> None:
        result = CompanyResult(
            email="pas-un-email",
            phone="abc",
            website="javascript:alert(1)",
        )

        self.assertEqual(result.email, NOT_FOUND_LABEL)
        self.assertEqual(result.phone, NOT_FOUND_LABEL)
        self.assertEqual(result.website, NOT_FOUND_LABEL)
        self.assertEqual(result.website_type, WebsiteType.NOT_FOUND)
        self.assertTrue(result.is_not_found)

    def test_sources_are_validated_deduplicated_and_limited(self) -> None:
        result = CompanyResult(
            sources=[
                "https://example.com/contact#form",
                "https://example.com/contact#other",
                "javascript:alert(1)",
            ]
        )

        self.assertEqual(result.sources, ["https://example.com/contact"])

    def test_invalid_source_container_is_ignored(self) -> None:
        result = CompanyResult(sources=123)  # type: ignore[arg-type]

        self.assertEqual(result.sources, [])

    def test_unverified_identity_rejects_all_contacts(self) -> None:
        result = CompanyResult(
            email="contact@example.com",
            phone="01 23 45 67 89",
            website="example.com",
            identity_verified=False,
            identity_match_type="none",
        )

        self.assertTrue(result.is_not_found)
        self.assertEqual(result.website_type, WebsiteType.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
