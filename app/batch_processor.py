from __future__ import annotations

from typing import Any

from app.config import AppConfig
from app.excel_service import ExcelService
from app.llm_service import CompanySearchService
from app.models import CompanyResult, CompanyRow, ProcessingStats
from app.utils import chunked, safe_sleep


class BatchProcessor:
    def __init__(
        self,
        config: AppConfig,
        excel_service: ExcelService,
        llm_service: CompanySearchService,
        logger: Any,
    ) -> None:
        self.config = config
        self.excel_service = excel_service
        self.llm_service = llm_service
        self.logger = logger

    def run(self) -> ProcessingStats:
        stats = ProcessingStats()

        self.excel_service.open()
        if self.config.create_backup:
            self.excel_service.create_backup()

        row_indexes = self.excel_service.get_row_indexes(
            start_row=self.config.start_row,
            max_rows=self.config.max_rows,
        )
        stats.total_rows_scanned = len(row_indexes)

        companies_to_process: list[CompanyRow] = []
        for row_index in row_indexes:
            if self._should_skip_row(row_index):
                stats.skipped_rows += 1
                self.logger.info("Ligne %s : skip (deja renseignee).", row_index)
                continue
            companies_to_process.append(self.excel_service.read_company_row(row_index))

        stats.eligible_rows = len(companies_to_process)
        self.logger.info("Nombre total de lignes analysees : %s", stats.total_rows_scanned)
        self.logger.info("Nombre de lignes eligibles : %s", stats.eligible_rows)

        batches = list(chunked(companies_to_process, self.config.batch_size))
        total_batches = len(batches)

        for batch_index, batch in enumerate(batches, start=1):
            self.logger.info("Debut batch %s/%s (%s ligne(s)).", batch_index, total_batches, len(batch))

            for company in batch:
                self._process_single_company(company, stats)
                safe_sleep(
                    self.config.sleep_between_calls,
                    logger=self.logger,
                    reason=f"pause entre deux appels ligne {company.row_index}",
                )

            if self.config.save_every_batch:
                self.excel_service.save()
                stats.saved_batches += 1
                self.logger.info("Sauvegarde batch effectuee (%s/%s).", batch_index, total_batches)

            safe_sleep(
                self.config.sleep_between_batches,
                logger=self.logger,
                reason=f"pause apres batch {batch_index}",
            )

        if not self.config.save_every_batch and companies_to_process:
            self.excel_service.save()
            stats.saved_batches += 1
            self.logger.info("Sauvegarde finale effectuee.")

        self.logger.info("Traitement termine.")
        return stats

    def _process_single_company(self, company: CompanyRow, stats: ProcessingStats) -> None:
        self.logger.info("Ligne %s : entreprise en cours '%s'.", company.row_index, company.company_name)

        if not company.siret or not company.company_name:
            self.logger.warning(
                "Ligne %s : SIRET ou raison sociale manquant, ecriture de Non trouvé.",
                company.row_index,
            )
            self.excel_service.write_result(company.row_index, CompanyResult.not_found())
            stats.processed_rows += 1
            stats.failed_rows += 1
            return

        try:
            result = self.llm_service.search_company_contact(company)
        except Exception:
            self.excel_service.write_result(company.row_index, CompanyResult.not_found())
            stats.processed_rows += 1
            stats.failed_rows += 1
            self.logger.exception("Erreur ligne %s.", company.row_index)
            return

        self.excel_service.write_result(company.row_index, result)
        stats.processed_rows += 1

        if (
            result.email == "Non trouvé"
            and result.phone == "Non trouvé"
            and result.website == "Non trouvé"
        ):
            stats.failed_rows += 1
            self.logger.info("Ligne %s : aucun resultat exploitable.", company.row_index)
            return

        stats.success_rows += 1
        self.logger.info("Ligne %s : enrichie.", company.row_index)

    def _should_skip_row(self, row_index: int) -> bool:
        if self.config.overwrite_existing:
            return False
        if self.config.skip_if_filled and self.excel_service.are_result_cells_filled(row_index):
            return True
        return False
