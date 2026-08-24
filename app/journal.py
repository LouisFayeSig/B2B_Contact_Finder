from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.excel_service import ExcelService
from app.models import CompanyResult, CompanyRow, ProcessingStatus


class JournalRecoveryError(RuntimeError):
    """Le journal ne peut pas etre rejoue sans risque sur le classeur courant."""


class JournalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    recorded_at_utc: datetime
    row_index: int
    siret: str
    company_name: str
    result: CompanyResult | None
    status: ProcessingStatus
    overwrite_existing: bool
    error_type: str = ""
    error_message: str = ""


class ProcessingJournal:
    """Journal append-only vide uniquement apres une sauvegarde Excel reussie."""

    def __init__(self, path: Path, logger: Any) -> None:
        self.path = path
        self.logger = logger

    def append(
        self,
        company: CompanyRow,
        result: CompanyResult | None,
        status: ProcessingStatus,
        *,
        overwrite_existing: bool,
        error: BaseException | None = None,
    ) -> None:
        record = JournalRecord(
            recorded_at_utc=datetime.now(UTC),
            row_index=company.row_index,
            siret=company.siret,
            company_name=company.company_name,
            result=result,
            status=status,
            overwrite_existing=overwrite_existing,
            error_type=type(error).__name__ if error is not None else "",
            error_message=str(error)[:500] if error is not None else "",
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def recover(self, excel_service: ExcelService) -> int:
        records = self._read_records()
        if not records:
            return 0

        for record in records:
            company = excel_service.read_company_row(record.row_index)
            if not self._same_company(company, record):
                raise JournalRecoveryError(f"Le journal ne correspond plus a la ligne {record.row_index}.")
            if record.result is not None:
                excel_service.write_result(
                    record.row_index,
                    record.result,
                    overwrite_existing=record.overwrite_existing,
                )
            excel_service.write_status(record.row_index, record.status)

        excel_service.save()
        self.clear()
        self.logger.info("Journal rejoue et sauvegarde : %s ligne(s).", len(records))
        return len(records)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _read_records(self) -> list[JournalRecord]:
        if not self.path.exists():
            return []

        records: list[JournalRecord] = []
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(JournalRecord.model_validate_json(line))
                except Exception as exc:
                    raise JournalRecoveryError(f"Journal invalide a la ligne {line_number}: {self.path}") from exc
        return records

    def _same_company(self, company: CompanyRow, record: JournalRecord) -> bool:
        record_siret = "".join(character for character in record.siret if character.isdigit())
        return (
            company.normalized_siret == record_siret
            and company.company_name.casefold() == record.company_name.casefold()
        )
