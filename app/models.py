from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NOT_FOUND_LABEL = "Non trouvé"

_MISSING_TOKENS = {
    "",
    "none",
    "null",
    "unknown",
    "n/a",
    "na",
    "not found",
    "introuvable",
    "aucun",
    "aucune",
    "non disponible",
    "non renseigne",
    "non renseigné",
    "non trouve",
    "non trouvé",
}
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProcessingStatus(StrEnum):
    """Etat persistant distinguant résultat métier et incident technique."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    TECHNICAL_ERROR = "technical_error"
    INVALID_INPUT = "invalid_input"


class IdentityMatchType(StrEnum):
    """Niveau de preuve utilise pour rattacher les contacts a l'entreprise."""

    SIRET = "siret"
    NAME_AND_ADDRESS = "name_and_address"
    NAME_AND_CITY = "name_and_city"
    NONE = "none"


class WebsiteType(StrEnum):
    """Nature de la page conservee dans la colonne Site Web."""

    OFFICIAL_SITE = "official_site"
    GOOGLE_MAPS = "google_maps"
    DIRECTORY = "directory"
    SOCIAL_NETWORK = "social_network"
    MARKETPLACE = "marketplace"
    OTHER = "other"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


class CompanyRow(BaseModel):
    """Representation d'une ligne entreprise lue depuis Excel."""

    model_config = ConfigDict(str_strip_whitespace=True)

    row_index: int
    siret: str
    company_name: str
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None

    @property
    def normalized_siret(self) -> str:
        return "".join(character for character in self.siret if character.isdigit())

    @property
    def has_valid_siret(self) -> bool:
        return len(self.normalized_siret) == 14


class CompanyResult(BaseModel):
    """Coordonnees validées et sources retournées par le service de recherche."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(default=NOT_FOUND_LABEL)
    phone: str = Field(default=NOT_FOUND_LABEL)
    website: str = Field(default=NOT_FOUND_LABEL)
    website_type: WebsiteType = WebsiteType.UNKNOWN
    sources: list[str] = Field(default_factory=list, max_length=30)
    email_source: str = Field(default=NOT_FOUND_LABEL)
    phone_source: str = Field(default=NOT_FOUND_LABEL)
    website_source: str = Field(default=NOT_FOUND_LABEL)
    identity_verified: bool = False
    identity_match_type: IdentityMatchType = IdentityMatchType.NONE
    identity_source: str = Field(default=NOT_FOUND_LABEL)
    searched_at_utc: datetime | None = None
    model_deployment: str = ""
    model_snapshot: str = ""
    response_id: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    web_search_calls: int = Field(default=0, ge=0)
    deterministic_pages: list[str] = Field(default_factory=list, max_length=12)
    deterministic_fields_found: int = Field(default=0, ge=0, le=3)
    email_extraction_method: str = ""
    phone_extraction_method: str = ""
    identity_extraction_method: str = ""
    legal_popup_detected: bool = False

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, value: Any) -> str:
        return normalize_email(value)

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, value: Any) -> str:
        return normalize_fr_phone(value)

    @field_validator("website", mode="before")
    @classmethod
    def validate_website(cls, value: Any) -> str:
        return _normalize_http_url(value)

    @field_validator("sources", mode="before")
    @classmethod
    def validate_sources(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raw_sources = [value]
        elif isinstance(value, list | tuple | set):
            raw_sources = list(value)
        else:
            return []
        sources: list[str] = []
        for raw_source in raw_sources:
            source = _normalize_http_url(raw_source)
            if source != NOT_FOUND_LABEL and source not in sources:
                sources.append(source)
            if len(sources) == 30:
                break
        return sources

    @field_validator("deterministic_pages", mode="before")
    @classmethod
    def validate_deterministic_pages(cls, value: Any) -> list[str]:
        if value is None:
            return []
        raw_pages = [value] if isinstance(value, str) else list(value) if isinstance(value, list | tuple | set) else []
        pages: list[str] = []
        for raw_page in raw_pages:
            page = _normalize_http_url(raw_page)
            if page != NOT_FOUND_LABEL and page not in pages:
                pages.append(page)
            if len(pages) == 12:
                break
        return pages

    @field_validator(
        "email_source",
        "phone_source",
        "website_source",
        "identity_source",
        mode="before",
    )
    @classmethod
    def validate_evidence_url(cls, value: Any) -> str:
        return _normalize_http_url(value)

    @model_validator(mode="after")
    def enforce_identity_consistency(self) -> CompanyResult:
        if not self.identity_verified or self.identity_match_type is IdentityMatchType.NONE:
            self.identity_verified = False
            self.identity_match_type = IdentityMatchType.NONE
            self.email = NOT_FOUND_LABEL
            self.phone = NOT_FOUND_LABEL
            self.website = NOT_FOUND_LABEL
            self.website_type = WebsiteType.NOT_FOUND
            self.email_source = NOT_FOUND_LABEL
            self.phone_source = NOT_FOUND_LABEL
            self.website_source = NOT_FOUND_LABEL
            self.email_extraction_method = ""
            self.phone_extraction_method = ""

        for value_field, source_field in (
            ("email", "email_source"),
            ("phone", "phone_source"),
            ("website", "website_source"),
        ):
            if getattr(self, value_field) == NOT_FOUND_LABEL:
                setattr(self, source_field, NOT_FOUND_LABEL)
                method_field = f"{value_field}_extraction_method"
                if hasattr(self, method_field):
                    setattr(self, method_field, "")
        if self.website == NOT_FOUND_LABEL:
            self.website_type = WebsiteType.NOT_FOUND
        elif self.website_type is WebsiteType.NOT_FOUND:
            self.website_type = WebsiteType.UNKNOWN
        return self

    @property
    def is_not_found(self) -> bool:
        return all(value == NOT_FOUND_LABEL for value in (self.email, self.phone, self.website))

    @classmethod
    def not_found(cls, *, sources: list[str] | None = None) -> CompanyResult:
        return cls(
            email=NOT_FOUND_LABEL,
            phone=NOT_FOUND_LABEL,
            website=NOT_FOUND_LABEL,
            sources=sources or [],
            identity_verified=False,
            identity_match_type=IdentityMatchType.NONE,
        )


class ProcessingStats(BaseModel):
    """Statistiques d'execution du pipeline."""

    total_rows_scanned: int = 0
    eligible_rows: int = 0
    processed_rows: int = 0
    skipped_rows: int = 0
    success_rows: int = 0
    not_found_rows: int = 0
    technical_error_rows: int = 0
    invalid_input_rows: int = 0
    failed_rows: int = 0
    saved_batches: int = 0
    recovered_rows: int = 0


def _normalize_text(value: Any) -> str:
    if value is None:
        return NOT_FOUND_LABEL
    text = " ".join(str(value).strip().split())
    if text.lower() in _MISSING_TOKENS:
        return NOT_FOUND_LABEL
    return text


def normalize_email(value: Any) -> str:
    text = _normalize_text(value)
    if text == NOT_FOUND_LABEL:
        return text
    if text.lower().startswith("mailto:"):
        text = text[7:].strip()
    text = text.strip(" <>[](){}.,;:\"'").casefold()
    if len(text) > 254 or _EMAIL_PATTERN.fullmatch(text) is None:
        return NOT_FOUND_LABEL
    local_part, domain = text.rsplit("@", 1)
    if len(local_part) > 64 or ".." in text or domain.startswith("-") or domain.endswith("-"):
        return NOT_FOUND_LABEL
    return text


def normalize_fr_phone(value: Any) -> str:
    text = _normalize_text(value)
    if text == NOT_FOUND_LABEL:
        return text
    if text.lower().startswith("tel:"):
        text = text[4:].strip()
    text = re.split(r"(?i)\b(?:poste|post|ext|extension)\b", text, maxsplit=1)[0]
    compact = re.sub(r"[^+\d]", "", text)
    if compact.startswith("0033"):
        compact = f"+33{compact[4:]}"
    if compact.startswith("+330"):
        compact = f"+33{compact[4:]}"
    if compact.startswith("+33"):
        national = compact[3:]
        if len(national) != 9 or national[0] == "0":
            return NOT_FOUND_LABEL
        compact = f"0{national}"
    if len(compact) != 10 or not compact.isdigit() or compact[0] != "0" or compact[1] == "0":
        return NOT_FOUND_LABEL
    return " ".join((compact[:2], compact[2:4], compact[4:6], compact[6:8], compact[8:10]))


def _normalize_http_url(value: Any) -> str:
    text = _normalize_text(value)
    if text == NOT_FOUND_LABEL:
        return text
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return NOT_FOUND_LABEL
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return NOT_FOUND_LABEL
    if "." not in parsed.hostname:
        return NOT_FOUND_LABEL
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
