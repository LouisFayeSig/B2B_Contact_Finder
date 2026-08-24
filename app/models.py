from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompanyRow(BaseModel):
    """Representation d'une ligne entreprise lue depuis Excel."""

    model_config = ConfigDict(str_strip_whitespace=True)

    row_index: int
    siret: str
    company_name: str
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None


class CompanyResult(BaseModel):
    """Coordonnees retournees par le service LLM."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(default="Non trouvé")
    phone: str = Field(default="Non trouvé")
    website: str = Field(default="Non trouvé")

    @classmethod
    def not_found(cls) -> "CompanyResult":
        return cls(email="Non trouvé", phone="Non trouvé", website="Non trouvé")


class ProcessingStats(BaseModel):
    """Statistiques d'execution du pipeline."""

    total_rows_scanned: int = 0
    eligible_rows: int = 0
    processed_rows: int = 0
    skipped_rows: int = 0
    success_rows: int = 0
    failed_rows: int = 0
    saved_batches: int = 0
