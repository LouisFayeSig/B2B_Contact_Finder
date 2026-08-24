from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from math import ceil
from typing import Any

from app.config import AppConfig
from app.excel_service import ExcelService
from app.journal import ProcessingJournal
from app.llm_service import CompanySearchService
from app.models import CompanyResult, CompanyRow, ProcessingStats, ProcessingStatus
from app.utils import chunked, safe_sleep


class BatchProcessor:
    def __init__(
        self,
        config: AppConfig,
        excel_service: ExcelService,
        llm_service: CompanySearchService,
        logger: Any,
        journal: ProcessingJournal | None = None,
    ) -> None:
        self.config = config
        self.excel_service = excel_service
        self.llm_service = llm_service
        self.logger = logger
        self.journal = journal

    def run(self) -> ProcessingStats:
        stats = ProcessingStats()

        self.excel_service.open()
        if self.config.create_backup:
            self.excel_service.create_backup()
        if self.journal is not None:
            stats.recovered_rows = self.journal.recover(self.excel_service)

        row_indexes = self.excel_service.get_row_indexes(
            start_row=self.config.start_row,
            max_rows=self.config.max_rows,
        )
        stats.total_rows_scanned = len(row_indexes)

        eligible_row_indexes: list[int] = []
        for row_index in row_indexes:
            if self._should_skip_row(row_index):
                stats.skipped_rows += 1
                self.logger.info("Ligne %s : skip (deja renseignee).", row_index)
                continue
            eligible_row_indexes.append(row_index)

        stats.eligible_rows = len(eligible_row_indexes)
        self.logger.info("Nombre total de lignes analysees : %s", stats.total_rows_scanned)
        self.logger.info("Nombre de lignes eligibles : %s", stats.eligible_rows)

        total_batches = ceil(stats.eligible_rows / self.config.batch_size)

        with ThreadPoolExecutor(
            max_workers=self.config.max_workers,
            thread_name_prefix="foundry-search",
        ) as executor:
            for batch_index, batch in enumerate(
                chunked(eligible_row_indexes, self.config.batch_size),
                start=1,
            ):
                self.logger.info(
                    "Debut batch %s/%s (%s ligne(s), %s worker(s)).",
                    batch_index,
                    total_batches,
                    len(batch),
                    self.config.max_workers,
                )
                self._process_batch(batch, stats, executor)

                if self.config.save_every_batch:
                    self._save_checkpoint()
                    stats.saved_batches += 1
                    self.logger.info("Sauvegarde batch effectuee (%s/%s).", batch_index, total_batches)

                if batch_index < total_batches:
                    safe_sleep(
                        self.config.sleep_between_batches,
                        logger=self.logger,
                        reason=f"pause apres batch {batch_index}",
                    )

        if not self.config.save_every_batch and eligible_row_indexes:
            self._save_checkpoint()
            stats.saved_batches += 1
            self.logger.info("Sauvegarde finale effectuee.")

        self.logger.info("Traitement termine.")
        return stats

    def _process_single_company(self, company: CompanyRow, stats: ProcessingStats) -> None:
        self.logger.info("Ligne %s : entreprise en cours '%s'.", company.row_index, company.company_name)

        if not company.company_name or not company.has_valid_siret:
            self.logger.warning(
                "Ligne %s : SIRET invalide ou raison sociale manquante.",
                company.row_index,
            )
            self._commit_outcome(
                company,
                CompanyResult.not_found(),
                ProcessingStatus.INVALID_INPUT,
                stats,
            )
            return

        try:
            result = self.llm_service.search_company_contact(company)
        except Exception as exc:
            self._commit_outcome(
                company,
                None,
                ProcessingStatus.TECHNICAL_ERROR,
                stats,
                error=exc,
            )
            self.logger.exception(
                "Erreur technique ligne %s ; coordonnées conservées et ligne à retraiter.",
                company.row_index,
            )
            return

        status = self._result_status(company.row_index, result)
        self._commit_outcome(company, result, status, stats)

    def _process_batch(
        self,
        row_indexes: list[int],
        stats: ProcessingStats,
        executor: ThreadPoolExecutor,
    ) -> None:
        futures: dict[Future[CompanyResult], CompanyRow] = {}
        for position, row_index in enumerate(row_indexes, start=1):
            company = self.excel_service.read_company_row(row_index)
            self.logger.info("Ligne %s : soumise '%s'.", company.row_index, company.company_name)
            if not company.company_name or not company.has_valid_siret:
                self._commit_outcome(
                    company,
                    CompanyResult.not_found(),
                    ProcessingStatus.INVALID_INPUT,
                    stats,
                )
                continue
            futures[executor.submit(self.llm_service.search_company_contact, company)] = company
            if position < len(row_indexes):
                safe_sleep(
                    self.config.sleep_between_calls,
                    logger=self.logger,
                    reason=f"throttle apres soumission ligne {company.row_index}",
                )

        for future in as_completed(futures):
            company = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                self._commit_outcome(
                    company,
                    None,
                    ProcessingStatus.TECHNICAL_ERROR,
                    stats,
                    error=exc,
                )
                self.logger.error(
                    "Erreur technique ligne %s (%s) : %s",
                    company.row_index,
                    company.company_name,
                    exc,
                )
                continue

            status = self._result_status(company.row_index, result)
            self._commit_outcome(company, result, status, stats)

    def _result_status(self, row_index: int, result: CompanyResult) -> ProcessingStatus:
        existing_contact_survives = not self.config.overwrite_existing and self.excel_service.has_any_contact(row_index)
        if existing_contact_survives or not result.is_not_found:
            return ProcessingStatus.SUCCESS
        return ProcessingStatus.NOT_FOUND

    def _commit_outcome(
        self,
        company: CompanyRow,
        result: CompanyResult | None,
        status: ProcessingStatus,
        stats: ProcessingStats,
        *,
        error: BaseException | None = None,
    ) -> None:
        if self.journal is not None:
            self.journal.append(
                company,
                result,
                status,
                overwrite_existing=self.config.overwrite_existing,
                error=error,
            )

        if result is not None:
            self.excel_service.write_result(
                company.row_index,
                result,
                overwrite_existing=self.config.overwrite_existing,
            )
        self.excel_service.write_status(company.row_index, status)
        stats.processed_rows += 1

        if status is ProcessingStatus.SUCCESS:
            stats.success_rows += 1
            self.logger.info("Ligne %s : enrichie.", company.row_index)
        elif status is ProcessingStatus.NOT_FOUND:
            stats.not_found_rows += 1
            stats.failed_rows += 1
            self.logger.info("Ligne %s : aucun resultat exploitable.", company.row_index)
        elif status is ProcessingStatus.TECHNICAL_ERROR:
            stats.technical_error_rows += 1
            stats.failed_rows += 1
        elif status is ProcessingStatus.INVALID_INPUT:
            stats.invalid_input_rows += 1
            stats.failed_rows += 1

    def _save_checkpoint(self) -> None:
        self.excel_service.save()
        if self.journal is not None:
            self.journal.clear()

    def _should_skip_row(self, row_index: int) -> bool:
        if self.config.overwrite_existing:
            return False
        if not self.config.skip_if_filled:
            return False

        status = self.excel_service.read_status(row_index)
        if status is ProcessingStatus.TECHNICAL_ERROR:
            return False
        if status is ProcessingStatus.INVALID_INPUT:
            company = self.excel_service.read_company_row(row_index)
            return not company.company_name or not company.has_valid_siret
        if status in {ProcessingStatus.SUCCESS, ProcessingStatus.NOT_FOUND}:
            return True

        # Compatibilite avec les classeurs enrichis avant l'ajout de la colonne statut.
        return self.excel_service.are_result_cells_filled(row_index)
