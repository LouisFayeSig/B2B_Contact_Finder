from __future__ import annotations

import html as html_module
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.config import AppConfig
from app.models import (
    NOT_FOUND_LABEL,
    CompanyResult,
    CompanyRow,
    IdentityMatchType,
    normalize_email,
    normalize_fr_phone,
)

_EMAIL_SCAN_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+(?:@|%40)[A-Z0-9.-]+\.[A-Z]{2,63}",
    re.IGNORECASE,
)
_PHONE_SCAN_PATTERN = re.compile(
    r"(?<![\d.])(?:\+33(?:\s*\(0\))?|0033|0)[1-9](?:[ .-]?\d{2}){4}(?![\d.])"
)
_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:siret|siren|r\.?\s*c\.?\s*s\.?)\b[^\d]{0,40}((?:\d[\s.\-]?){9,14})(?!\d)",
    re.IGNORECASE,
)
_VAT_PATTERN = re.compile(
    r"\b(?:tva|intracommunautaire)\b.{0,50}?\bfr\s*[0-9a-z]{2}\s*((?:\d[\s.\-]?){9})(?!\d)",
    re.IGNORECASE | re.DOTALL,
)
_CONTACT_WORDS = (
    "contact",
    "contactez",
    "telephone",
    "tel",
    "appeler",
    "joindre",
    "email",
    "courriel",
    "devis",
)
_CONTACT_LINK_WORDS = (*_CONTACT_WORDS, "nous contacter", "contact us")
_LEGAL_LINK_WORDS = (
    "mentions legales",
    "mention legale",
    "legal notice",
    "informations legales",
    "impressum",
)
_COMMON_EMAIL_TLDS = {
    "com",
    "fr",
    "net",
    "org",
    "eu",
    "io",
    "pro",
    "info",
    "biz",
    "co",
    "me",
    "paris",
    "alsace",
    "bzh",
    "corsica",
    "re",
    "yt",
    "gp",
    "mq",
    "gf",
}
_LEGAL_FORMS = {
    "sa",
    "sas",
    "sasu",
    "sarl",
    "eurl",
    "ei",
    "eirl",
    "scop",
    "scea",
    "selarl",
}
_ADDRESS_STOP_WORDS = {
    "de",
    "du",
    "des",
    "la",
    "le",
    "les",
    "a",
    "au",
    "aux",
    "rue",
    "route",
    "avenue",
    "boulevard",
    "chemin",
    "impasse",
}
_SKIPPED_TEXT_TAGS = {"script", "style", "svg", "noscript"}
_STATIC_CONTACT_PATHS = ("/contact", "/nous-contacter", "/contactez-nous")
_STATIC_LEGAL_PATHS = ("/mentions-legales", "/mentions-legales/")


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    url: str
    html: str


@dataclass(frozen=True, slots=True)
class _Link:
    href: str
    label: str
    attributes: Mapping[str, str]
    region: str


@dataclass(frozen=True, slots=True)
class _ValueCandidate:
    value: str
    source_url: str
    method: str
    context: str


@dataclass(frozen=True, slots=True)
class _IdentifierCandidate:
    value: str
    context: str


@dataclass(slots=True)
class _ParsedPage:
    url: str
    text: str
    links: list[_Link]
    json_ld: list[Any]
    emails: list[_ValueCandidate] = field(default_factory=list)
    phones: list[_ValueCandidate] = field(default_factory=list)
    identifiers: list[_IdentifierCandidate] = field(default_factory=list)
    identity_match_type: IdentityMatchType = IdentityMatchType.NONE
    name_matches: bool = False
    location_matches: bool = False
    conflicting_identifier: bool = False
    legal_popup_detected: bool = False


@dataclass(slots=True)
class _AnchorState:
    href: str
    attributes: dict[str, str]
    region: str
    text_parts: list[str] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[_Link] = []
        self.json_ld_blocks: list[str] = []
        self._skip_stack: list[str] = []
        self._anchor: _AnchorState | None = None
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []
        self._regions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        if tag == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._json_ld_depth = 1
            self._json_ld_parts = []
            return
        if self._json_ld_depth:
            self._json_ld_depth += 1
            return
        if tag in _SKIPPED_TEXT_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag in {"header", "main", "footer"}:
            self._regions.append(tag)
        if tag == "a":
            self._anchor = _AnchorState(
                href=attributes.get("href", ""),
                attributes=attributes,
                region=self._regions[-1] if self._regions else "body",
            )
        if tag in {"br", "p", "div", "li", "section", "footer", "header", "address"}:
            self.text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._json_ld_depth:
            self._json_ld_depth -= 1
            if self._json_ld_depth == 0:
                block = "".join(self._json_ld_parts).strip()
                if block:
                    self.json_ld_blocks.append(block)
            return
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag == "a" and self._anchor is not None:
            self.links.append(
                _Link(
                    href=self._anchor.href,
                    label=" ".join("".join(self._anchor.text_parts).split()),
                    attributes=self._anchor.attributes,
                    region=self._anchor.region,
                )
            )
            self._anchor = None
        if tag in {"header", "main", "footer"} and self._regions and self._regions[-1] == tag:
            self._regions.pop()
        if tag in {"p", "div", "li", "section", "footer", "header", "address"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_parts.append(data)
            return
        if self._skip_stack:
            return
        self.text_parts.append(data)
        if self._anchor is not None:
            self._anchor.text_parts.append(data)


class DeterministicSiteExtractor:
    """Complete les champs oublies par le modele a partir des pages deja decouvertes."""

    def __init__(
        self,
        config: AppConfig,
        logger: Any,
        *,
        fetch_page: Callable[[str], FetchedDocument | None] | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._fetch_page_override = fetch_page

    def enrich(self, company: CompanyRow, result: CompanyResult) -> CompanyResult:
        if not self._config.site_extraction_enabled or result.website == NOT_FOUND_LABEL:
            return result
        if result.email != NOT_FOUND_LABEL and result.phone != NOT_FOUND_LABEL:
            return result

        pages = self._crawl(company, result)
        if not pages:
            return result

        site_verified = self._site_is_verified(pages)
        email_candidate = self._best_candidate("email", pages, company, result, site_verified)
        phone_candidate = self._best_candidate("phone", pages, company, result, site_verified)
        payload = result.model_dump()
        fields_found = 0

        if result.email == NOT_FOUND_LABEL and email_candidate is not None:
            payload["email"] = email_candidate.value
            payload["email_source"] = email_candidate.source_url
            payload["email_extraction_method"] = email_candidate.method
            fields_found += 1
        if result.phone == NOT_FOUND_LABEL and phone_candidate is not None:
            payload["phone"] = phone_candidate.value
            payload["phone_source"] = phone_candidate.source_url
            payload["phone_extraction_method"] = phone_candidate.method
            fields_found += 1

        exact_identity_page = next(
            (page for page in pages if page.identity_match_type is IdentityMatchType.SIRET),
            None,
        )
        if exact_identity_page is not None:
            payload["identity_verified"] = True
            payload["identity_match_type"] = IdentityMatchType.SIRET
            payload["identity_source"] = exact_identity_page.url
            payload["identity_extraction_method"] = "site_siret_or_siren"

        fetched_urls = list(dict.fromkeys(page.url for page in pages))[:12]
        payload["deterministic_pages"] = fetched_urls
        payload["deterministic_fields_found"] = fields_found
        payload["legal_popup_detected"] = any(page.legal_popup_detected for page in pages)
        if self._config.search_audit_enabled:
            payload["sources"] = list(dict.fromkeys([*fetched_urls, *result.sources]))[:30]

        enriched = CompanyResult.model_validate(payload)
        if fields_found:
            self._logger.info(
                "Ligne %s : extraction directe du site, %s champ(s) complete(s) sur %s page(s).",
                company.row_index,
                fields_found,
                len(fetched_urls),
            )
        return enriched

    def _crawl(self, company: CompanyRow, result: CompanyResult) -> list[_ParsedPage]:
        queue = [_canonical_url(result.website)]
        queued = set(queue)
        pages: list[_ParsedPage] = []
        root_host = ""
        client = self._build_http_client() if self._fetch_page_override is None else None

        try:
            while queue and len(pages) < self._config.site_extraction_max_pages:
                requested_url = queue.pop(0)
                document = self._fetch_document(requested_url, client)
                if document is None:
                    continue
                if not root_host:
                    root_host = _normalized_host(document.url)
                if _normalized_host(document.url) != root_host:
                    continue

                page = self._parse_page(document, company)
                pages.append(page)
                contact_links, legal_links = self._page_links(page, root_host)
                missing_email = result.email == NOT_FOUND_LABEL and not page.emails
                missing_phone = result.phone == NOT_FOUND_LABEL and not page.phones

                discovered = [*legal_links, *(contact_links if missing_email or missing_phone else [])]
                for link in discovered:
                    if link not in queued and len(queued) < self._config.site_extraction_max_pages * 3:
                        queue.append(link)
                        queued.add(link)

                if len(pages) == 1:
                    fallback_paths: Iterable[str] = ()
                    if (missing_email or missing_phone) and not contact_links:
                        fallback_paths = (*fallback_paths, *_STATIC_CONTACT_PATHS)
                    if not legal_links:
                        fallback_paths = (*fallback_paths, *_STATIC_LEGAL_PATHS)
                    origin = _origin_url(document.url)
                    for path in fallback_paths:
                        fallback_url = _canonical_url(urljoin(origin, path))
                        if fallback_url not in queued:
                            queue.append(fallback_url)
                            queued.add(fallback_url)
        finally:
            if client is not None:
                client.close()

        return pages

    def _build_http_client(self) -> httpx.Client:
        verify: ssl.SSLContext | bool = True
        ca_bundle = self._config.azure_foundry_ca_bundle
        if ca_bundle is not None:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(ca_bundle))
            verify = context
        return httpx.Client(
            verify=verify,
            timeout=self._config.site_extraction_timeout,
            follow_redirects=False,
            headers={
                "User-Agent": "B2BContactFinder/0.1 (+deterministic public-page extraction)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
            },
        )

    def _fetch_document(self, url: str, client: httpx.Client | None) -> FetchedDocument | None:
        if self._fetch_page_override is not None:
            return self._fetch_page_override(url)
        if client is None:
            return None

        current_url = url
        for _ in range(4):
            if not _is_public_http_url(current_url):
                self._logger.warning("Extraction directe ignore une URL non publique : %s", current_url)
                return None
            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current_url = _canonical_url(urljoin(str(response.url), location))
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        return None
                    content_type = response.headers.get("content-type", "").casefold()
                    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                        return None
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > self._config.site_extraction_max_bytes:
                            self._logger.warning("Page trop volumineuse ignoree : %s", current_url)
                            return None
                    encoding = response.encoding or response.charset_encoding or "utf-8"
                    return FetchedDocument(
                        url=_canonical_url(str(response.url)),
                        html=bytes(content).decode(encoding, errors="replace"),
                    )
            except (httpx.HTTPError, OSError, UnicodeError) as exc:
                self._logger.debug("Extraction directe impossible pour %s : %s", current_url, exc)
                return None
        return None

    def _parse_page(self, document: FetchedDocument, company: CompanyRow) -> _ParsedPage:
        parser = _DocumentParser()
        try:
            parser.feed(document.html)
            parser.close()
        except Exception as exc:
            self._logger.debug("HTML partiellement invalide pour %s : %s", document.url, exc)

        text = "\n".join(line.strip() for line in "".join(parser.text_parts).splitlines() if line.strip())
        json_ld = [_parse_json_ld(block) for block in parser.json_ld_blocks]
        json_ld = [value for value in json_ld if value is not None]
        page = _ParsedPage(
            url=document.url,
            text=text,
            links=parser.links,
            json_ld=json_ld,
        )
        page.emails = self._extract_emails(page)
        page.phones = self._extract_phones(page)
        page.identifiers = _extract_identifiers(page.text)
        (
            page.identity_match_type,
            page.name_matches,
            page.location_matches,
            page.conflicting_identifier,
        ) = _match_page_identity(page, company)
        page.legal_popup_detected = any(_is_legal_popup(link) for link in page.links)
        return page

    def _extract_emails(self, page: _ParsedPage) -> list[_ValueCandidate]:
        candidates: list[_ValueCandidate] = []
        for link in page.links:
            if not link.href.casefold().startswith("mailto:"):
                continue
            raw_value = unquote(link.href[7:].split("?", 1)[0]).strip()
            value, method = _decode_email(raw_value, "mailto")
            if value != NOT_FOUND_LABEL:
                candidates.append(
                    _ValueCandidate(value, page.url, method, _link_context(page.text, link))
                )

        decoded_text = unquote(html_module.unescape(page.text))
        for match in _EMAIL_SCAN_PATTERN.finditer(decoded_text):
            value, method = _decode_email(match.group(0), "visible_text")
            if value != NOT_FOUND_LABEL:
                candidates.append(
                    _ValueCandidate(value, page.url, method, _surrounding_text(decoded_text, match.start()))
                )

        for record in _walk_json_ld(page.json_ld):
            json_email = record.get("email")
            if isinstance(json_email, str):
                value = normalize_email(json_email)
                if value != NOT_FOUND_LABEL:
                    candidates.append(
                        _ValueCandidate(value, page.url, "json_ld", json.dumps(record, ensure_ascii=False))
                    )
        return _deduplicate_candidates(candidates)

    def _extract_phones(self, page: _ParsedPage) -> list[_ValueCandidate]:
        candidates: list[_ValueCandidate] = []
        for link in page.links:
            if not link.href.casefold().startswith("tel:"):
                continue
            value = normalize_fr_phone(unquote(link.href[4:].split("?", 1)[0]))
            if value != NOT_FOUND_LABEL:
                candidates.append(
                    _ValueCandidate(value, page.url, "tel_link", _link_context(page.text, link))
                )

        for match in _PHONE_SCAN_PATTERN.finditer(page.text):
            value = normalize_fr_phone(match.group(0))
            if value != NOT_FOUND_LABEL:
                candidates.append(
                    _ValueCandidate(value, page.url, "visible_text", _surrounding_text(page.text, match.start()))
                )

        for record in _walk_json_ld(page.json_ld):
            json_phone = record.get("telephone")
            if isinstance(json_phone, str):
                value = normalize_fr_phone(json_phone)
                if value != NOT_FOUND_LABEL:
                    candidates.append(
                        _ValueCandidate(value, page.url, "json_ld", json.dumps(record, ensure_ascii=False))
                    )
        return _deduplicate_candidates(candidates)

    def _page_links(self, page: _ParsedPage, root_host: str) -> tuple[list[str], list[str]]:
        contact_links: list[str] = []
        legal_links: list[str] = []
        for link in page.links:
            if not link.href or link.href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            target = _canonical_url(urljoin(page.url, link.href))
            if _normalized_host(target) != root_host:
                continue
            descriptor = _normalize_search_text(f"{link.label} {link.href}")
            if _contains_keyword(descriptor, _CONTACT_LINK_WORDS):
                contact_links.append(target)
            if _contains_keyword(descriptor, _LEGAL_LINK_WORDS):
                legal_links.append(target)
        return list(dict.fromkeys(contact_links)), list(dict.fromkeys(legal_links))

    def _site_is_verified(self, pages: list[_ParsedPage]) -> bool:
        for page in pages:
            if page.identity_match_type is not IdentityMatchType.SIRET:
                continue
            parsed = urlsplit(page.url)
            is_root = parsed.path in {"", "/"}
            is_legal = _contains_keyword(parsed.path, _LEGAL_LINK_WORDS)
            if is_legal or (is_root and page.name_matches and page.location_matches):
                return True
        return False

    def _best_candidate(
        self,
        field_name: str,
        pages: list[_ParsedPage],
        company: CompanyRow,
        result: CompanyResult,
        site_verified: bool,
    ) -> _ValueCandidate | None:
        ranked: list[tuple[int, _ValueCandidate]] = []
        for page in pages:
            if page.conflicting_identifier:
                continue
            page_is_eligible = (
                page.identity_match_type is not IdentityMatchType.NONE
                or site_verified
                or (result.identity_verified and page.name_matches)
            )
            if not page_is_eligible:
                continue
            values = page.emails if field_name == "email" else page.phones
            for candidate in values:
                if field_name == "email" and _looks_like_platform_email(
                    candidate,
                    company,
                    result.website,
                ):
                    continue
                score = _candidate_score(candidate, page, company)
                if score >= 65:
                    ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], len(item[1].source_url)))
        return ranked[0][1]


def _decode_email(raw_value: str, base_method: str) -> tuple[str, str]:
    decoded = unquote(html_module.unescape(raw_value)).strip()
    normal = normalize_email(decoded)
    if normal != NOT_FOUND_LABEL and _email_tld(normal) in _COMMON_EMAIL_TLDS:
        return normal, base_method

    reversed_value = normalize_email(decoded[::-1])
    if reversed_value != NOT_FOUND_LABEL and _email_tld(reversed_value) in _COMMON_EMAIL_TLDS:
        return reversed_value, f"reversed_{base_method}"
    return normal, base_method


def _email_tld(value: str) -> str:
    return value.rsplit(".", 1)[-1].casefold() if "." in value else ""


def _parse_json_ld(value: str) -> Any | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _walk_json_ld(values: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    for value in values:
        if isinstance(value, Mapping):
            yield value
            yield from _walk_json_ld(value.values())
        elif isinstance(value, list | tuple):
            yield from _walk_json_ld(value)


def _deduplicate_candidates(candidates: list[_ValueCandidate]) -> list[_ValueCandidate]:
    unique: list[_ValueCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.value.casefold(), candidate.source_url)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _extract_identifiers(text: str) -> list[_IdentifierCandidate]:
    candidates: list[_IdentifierCandidate] = []
    for pattern in (_IDENTIFIER_PATTERN, _VAT_PATTERN):
        for match in pattern.finditer(text):
            value = "".join(character for character in match.group(1) if character.isdigit())
            if len(value) not in {9, 14}:
                continue
            candidates.append(_IdentifierCandidate(value=value, context=_surrounding_text(text, match.start())))
    return candidates


def _match_page_identity(
    page: _ParsedPage,
    company: CompanyRow,
) -> tuple[IdentityMatchType, bool, bool, bool]:
    expected_siret = company.normalized_siret
    expected_siren = expected_siret[:9]
    identifiers = page.identifiers
    exact_identifier = any(item.value in {expected_siret, expected_siren} for item in identifiers)
    name_matches = _company_name_matches(page.text, company.company_name)
    address_matches = _address_matches(page.text, company.address, company.postal_code)
    city_matches = _value_is_present(page.text, company.city)

    conflicting_identifier = any(
        item.value not in {expected_siret, expected_siren}
        and _company_name_matches(item.context, company.company_name)
        for item in identifiers
    )
    if exact_identifier:
        return IdentityMatchType.SIRET, name_matches, address_matches or city_matches, False
    if name_matches and address_matches:
        return IdentityMatchType.NAME_AND_ADDRESS, True, True, conflicting_identifier
    if name_matches and city_matches:
        return IdentityMatchType.NAME_AND_CITY, True, True, conflicting_identifier
    return IdentityMatchType.NONE, name_matches, address_matches or city_matches, conflicting_identifier


def _candidate_score(candidate: _ValueCandidate, page: _ParsedPage, company: CompanyRow) -> int:
    score = {
        "mailto": 45,
        "reversed_mailto": 50,
        "tel_link": 45,
        "visible_text": 25,
        "reversed_visible_text": 30,
        "json_ld": 55,
    }.get(candidate.method, 20)
    context = _normalize_search_text(candidate.context)
    compact_context = context.replace(" ", "")
    company_name = _normalized_company_name(company.company_name).replace(" ", "")
    if company_name and company_name in compact_context:
        score += 25
    if _value_is_present(candidate.context, company.city) or _value_is_present(
        candidate.context,
        company.postal_code,
    ):
        score += 15
    if _contains_keyword(context, _CONTACT_WORDS):
        score += 15
    path = _normalize_search_text(urlsplit(candidate.source_url).path)
    if _contains_keyword(path, (*_CONTACT_LINK_WORDS, *_LEGAL_LINK_WORDS)):
        score += 10
    if urlsplit(candidate.source_url).path in {"", "/"}:
        score += 10
    if page.identity_match_type is IdentityMatchType.SIRET:
        score += 5
    if candidate.context.startswith("[region=header]") and any(
        word in context for word in ("support", "service client", "assistance", "annuaire")
    ):
        score -= 50
    return score


def _company_name_matches(text: str, company_name: str | None) -> bool:
    expected = _normalized_company_name(company_name)
    if len(expected.replace(" ", "")) < 4:
        return False
    return expected.replace(" ", "") in _normalize_search_text(text).replace(" ", "")


def _normalized_company_name(value: str | None) -> str:
    tokens = _normalize_search_text(value).split()
    while tokens and tokens[0] in _LEGAL_FORMS:
        tokens.pop(0)
    while tokens and tokens[-1] in _LEGAL_FORMS:
        tokens.pop()
    return " ".join(tokens)


def _address_matches(text: str, address: str | None, postal_code: str | None) -> bool:
    if not address:
        return False
    normalized_text = _normalize_search_text(text)
    address_tokens = [
        token
        for token in _normalize_search_text(address).split()
        if token not in _ADDRESS_STOP_WORDS and (token.isdigit() or len(token) >= 4)
    ]
    if not address_tokens:
        return False
    required = min(2, len(address_tokens))
    token_matches = sum(token in normalized_text.split() for token in address_tokens)
    postal_matches = not postal_code or _value_is_present(text, postal_code)
    return token_matches >= required and postal_matches


def _value_is_present(text: str, value: str | None) -> bool:
    normalized_value = _normalize_search_text(value)
    if not normalized_value:
        return False
    return normalized_value in _normalize_search_text(text)


def _normalize_search_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents.casefold()).split())


def _link_context(text: str, link: _Link) -> str:
    probes = [link.label, unquote(link.href.split(":", 1)[-1].split("?", 1)[0])]
    folded_text = text.casefold()
    for probe in probes:
        if not probe:
            continue
        index = folded_text.find(probe.casefold())
        if index >= 0:
            return f"[region={link.region}] {_surrounding_text(text, index)}"
    return f"[region={link.region}] {link.label}"


def _surrounding_text(text: str, index: int, radius: int = 300) -> str:
    return text[max(0, index - radius) : index + radius]


def _is_legal_popup(link: _Link) -> bool:
    descriptor = _normalize_search_text(f"{link.label} {link.href}")
    return (
        _contains_keyword(descriptor, _LEGAL_LINK_WORDS)
        and (
            link.attributes.get("data-action", "").casefold() == "open-popup"
            or bool(link.attributes.get("data-popup-anchor"))
        )
    )


def _contains_keyword(value: str, keywords: Iterable[str]) -> bool:
    normalized = _normalize_search_text(value)
    tokens = set(normalized.split())
    for keyword in keywords:
        normalized_keyword = _normalize_search_text(keyword)
        if not normalized_keyword:
            continue
        if " " in normalized_keyword and normalized_keyword in normalized:
            return True
        if " " not in normalized_keyword and normalized_keyword in tokens:
            return True
    return False


def _looks_like_platform_email(
    candidate: _ValueCandidate,
    company: CompanyRow,
    discovered_website: str,
) -> bool:
    if candidate.method != "json_ld":
        return False
    local_part, _, email_domain = candidate.value.partition("@")
    website = urlsplit(discovered_website)
    website_host = (website.hostname or "").casefold().removeprefix("www.")
    if email_domain.casefold().removeprefix("www.") != website_host:
        return False
    if local_part not in {"contact", "info", "support", "bonjour", "hello", "admin"}:
        return False
    company_key = _normalized_company_name(company.company_name).replace(" ", "")
    host_key = _normalize_search_text(website_host.split(".", 1)[0]).replace(" ", "")
    is_non_root_page = website.path not in {"", "/"}
    return is_non_root_page and bool(company_key) and company_key not in host_key and host_key not in company_key


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, parsed.query, ""))


def _normalized_host(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold().removeprefix("www.")


def _origin_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _is_public_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return False
        host = parsed.hostname.casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
            return False
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443)}
        except (OSError, ValueError):
            return False
        return bool(addresses) and all(address.is_global for address in addresses)
    except ValueError:
        return False
