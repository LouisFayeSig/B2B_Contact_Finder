from __future__ import annotations

import logging
from types import SimpleNamespace

from app.models import NOT_FOUND_LABEL, CompanyResult, CompanyRow, IdentityMatchType
from app.site_extractor import DeterministicSiteExtractor, FetchedDocument


def _config(*, audit: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        site_extraction_enabled=True,
        site_extraction_max_pages=6,
        site_extraction_timeout=5.0,
        site_extraction_max_bytes=500_000,
        azure_foundry_ca_bundle=None,
        search_audit_enabled=audit,
    )


def _company() -> CompanyRow:
    return CompanyRow(
        row_index=2,
        siret="98347936100013",
        company_name="VINCE AMENAGEMENT",
        address="912 route de Geovreissiat",
        postal_code="01460",
        city="Geovreissiat",
    )


def _result(website: str = "https://vince-amenagement.fr/") -> CompanyResult:
    return CompanyResult(
        website=website,
        website_source=website,
        identity_verified=True,
        identity_match_type="siret",
        identity_source="https://annuaire-entreprises.data.gouv.fr/entreprise/983479361",
    )


def test_vince_pattern_recovers_phone_and_reversed_email() -> None:
    html = """
    <html><body>
      <h1>VINCE AMENAGEMENT</h1>
      <p>Contactez-nous</p>
      <a href="tel:0672763498">06 72 76 34 98</a>
      <a href="mailto:rf.rfs%40tnemeganema.ecniv">rf.rfs@tnemeganema.ecniv</a>
      <address>912 route de Geovreissiat, 01460 Geovreissiat</address>
      <a href="#" data-action="open-popup" data-popup-anchor="legal-id">Mentions legales</a>
    </body></html>
    """

    def fetch(url: str) -> FetchedDocument | None:
        if url == "https://vince-amenagement.fr/":
            return FetchedDocument(url=url, html=html)
        return None

    extractor = DeterministicSiteExtractor(_config(), logging.getLogger("test.site"), fetch_page=fetch)
    result = extractor.enrich(_company(), _result())

    assert result.email == "vince.amenagement@sfr.fr"
    assert result.phone == "06 72 76 34 98"
    assert result.email_source == "https://vince-amenagement.fr/"
    assert result.phone_source == "https://vince-amenagement.fr/"
    assert result.email_extraction_method == "reversed_mailto"
    assert result.phone_extraction_method == "tel_link"
    assert result.deterministic_fields_found == 2
    assert result.legal_popup_detected is True


def test_contact_page_is_followed_and_legal_siret_upgrades_identity_source() -> None:
    pages = {
        "https://example.fr/": """
            <h1>Entreprise Test</h1><p>Lyon</p>
            <a href="/contact">Contactez-nous</a>
            <a href="/mentions-legales">Mentions legales</a>
        """,
        "https://example.fr/contact": """
            <h1>Entreprise Test</h1><p>Contact Lyon</p>
            <a href="mailto:contact@example.fr">contact@example.fr</a>
            <a href="tel:+33412345678">04 12 34 56 78</a>
        """,
        "https://example.fr/mentions-legales": """
            <h1>Mentions legales Entreprise Test</h1>
            <p>SIRET : 123 456 789 01234</p><p>Lyon</p>
        """,
    }

    def fetch(url: str) -> FetchedDocument | None:
        return FetchedDocument(url=url, html=pages[url]) if url in pages else None

    company = CompanyRow(
        row_index=3,
        siret="12345678901234",
        company_name="Entreprise Test",
        address="10 rue de la Paix",
        postal_code="69002",
        city="Lyon",
    )
    initial = CompanyResult(
        website="https://example.fr/",
        website_source="https://example.fr/",
        identity_verified=True,
        identity_match_type="name_and_city",
        identity_source="https://directory.example/entreprise-test",
    )
    extractor = DeterministicSiteExtractor(_config(), logging.getLogger("test.site"), fetch_page=fetch)

    result = extractor.enrich(company, initial)

    assert result.email == "contact@example.fr"
    assert result.phone == "04 12 34 56 78"
    assert result.identity_match_type is IdentityMatchType.SIRET
    assert result.identity_source == "https://example.fr/mentions-legales"
    assert result.identity_extraction_method == "site_siret_or_siren"


def test_directory_support_number_is_ignored_in_favor_of_company_block() -> None:
    url = "https://annuaire.example/entreprise-test-123456789"
    html = """
      <header>Support de l'annuaire : <a href="tel:0188998877">01 88 99 88 77</a></header>
      <main>
        <h1>Entreprise Test - Lyon</h1>
        <p>SIRET : 123 456 789 01234</p>
        <p>Telephone de l'entreprise : <a href="tel:0611223344">06 11 22 33 44</a></p>
      </main>
    """
    company = CompanyRow(
        row_index=4,
        siret="12345678901234",
        company_name="Entreprise Test",
        city="Lyon",
    )
    initial = CompanyResult(
        website=url,
        website_source=url,
        identity_verified=True,
        identity_match_type="siret",
        identity_source=url,
    )
    extractor = DeterministicSiteExtractor(
        _config(),
        logging.getLogger("test.site"),
        fetch_page=lambda requested: FetchedDocument(url=requested, html=html) if requested == url else None,
    )

    result = extractor.enrich(company, initial)

    assert result.phone == "06 11 22 33 44"


def test_conflicting_siret_rejects_contacts() -> None:
    html = """
      <h1>Entreprise Test</h1><p>10 rue de la Paix, 69002 Lyon</p>
      <p>SIRET Entreprise Test : 999 999 999 00019</p>
      <p>Contact : <a href="tel:0611223344">06 11 22 33 44</a></p>
    """
    company = CompanyRow(
        row_index=5,
        siret="12345678901234",
        company_name="Entreprise Test",
        address="10 rue de la Paix",
        postal_code="69002",
        city="Lyon",
    )
    initial = CompanyResult(
        website="https://wrong.example/",
        website_source="https://wrong.example/",
        identity_verified=True,
        identity_match_type="name_and_address",
        identity_source="https://directory.example/company",
    )
    extractor = DeterministicSiteExtractor(
        _config(),
        logging.getLogger("test.site"),
        fetch_page=lambda url: FetchedDocument(url=url, html=html),
    )

    result = extractor.enrich(company, initial)

    assert result.phone == NOT_FOUND_LABEL


def test_generic_json_ld_email_from_listing_platform_is_rejected() -> None:
    url = "https://www.enduiseur.fr/entreprise-cakir"
    html = """
      <h1>ENTREPRISE CAKIR</h1><p>39000 Lons-le-Saunier</p>
      <script type="application/ld+json">
        {"@type":"WebPage","name":"Entreprise Cakir - Enduiseur.fr","email":"contact@enduiseur.fr"}
      </script>
    """
    company = CompanyRow(
        row_index=6,
        siret="50752589700025",
        company_name="ENTREPRISE CAKIR",
        postal_code="39000",
        city="Lons-le-Saunier",
    )
    initial = CompanyResult(
        website=url,
        website_source=url,
        identity_verified=True,
        identity_match_type="siret",
        identity_source="https://directory.example/cakir",
    )
    extractor = DeterministicSiteExtractor(
        _config(),
        logging.getLogger("test.site"),
        fetch_page=lambda requested: FetchedDocument(url=requested, html=html) if requested == url else None,
    )

    result = extractor.enrich(company, initial)

    assert result.email == NOT_FOUND_LABEL


def test_svg_numbers_and_non_audited_sources_are_not_returned() -> None:
    html = """
      <h1>VINCE AMENAGEMENT</h1><p>01460 Geovreissiat, 912 route de Geovreissiat</p>
      <svg><path d="M0.463742998 0.634073669"></path></svg>
    """
    extractor = DeterministicSiteExtractor(
        _config(audit=False),
        logging.getLogger("test.site"),
        fetch_page=(
            lambda url: FetchedDocument(url=url, html=html)
            if url == "https://vince-amenagement.fr/"
            else None
        ),
    )

    result = extractor.enrich(_company(), _result())

    assert result.phone == NOT_FOUND_LABEL
    assert result.sources == []
    assert result.deterministic_pages == ["https://vince-amenagement.fr/"]
