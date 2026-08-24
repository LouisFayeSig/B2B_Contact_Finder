from __future__ import annotations

import argparse
import sys

from app.batch_processor import BatchProcessor
from app.config import AppConfig, build_config
from app.excel_service import ExcelService
from app.journal import ProcessingJournal
from app.llm_service import AzureFoundryWebSearchService
from app.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Enrichit un fichier Excel d'entreprises avec email, telephone et site web.",
    )
    parser.add_argument("--file", dest="file", help="Chemin du fichier Excel a traiter.")
    parser.add_argument("--sheet", dest="sheet", help="Nom de la feuille Excel a utiliser.")
    parser.add_argument("--start-row", dest="start_row", type=int, help="Premiere ligne de donnees.")
    parser.add_argument("--max-rows", dest="max_rows", type=int, help="Nombre maximum de lignes a traiter.")
    parser.add_argument("--batch-size", dest="batch_size", type=int, help="Taille des batches.")
    parser.add_argument(
        "--workers",
        dest="workers",
        type=int,
        help="Nombre maximum d'appels Foundry simultanes.",
    )
    parser.add_argument(
        "--audit",
        dest="audit",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Collecte les sources par champ et les metadonnees de recherche.",
    )
    parser.add_argument(
        "--search-context-size",
        choices=("default", "low", "medium", "high"),
        default=None,
        help="Volume de contexte retourne par la recherche web.",
    )
    parser.add_argument(
        "--skip-if-filled",
        dest="skip_if_filled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip les lignes deja remplies dans P/Q/R.",
    )
    parser.add_argument(
        "--overwrite-existing",
        dest="overwrite_existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force la reecriture des colonnes P/Q/R meme si elles sont deja remplies.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = build_config(args)
    except Exception as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 1

    logger = setup_logger(config.log_level, config.log_file_path)

    try:
        _validate_runtime_config(config)
        excel_service = ExcelService(
            config.input_excel_path,
            config.sheet_name,
            logger,
            audit_enabled=config.search_audit_enabled,
        )
        llm_service = AzureFoundryWebSearchService(config, logger)
        journal = ProcessingJournal(config.journal_file_path, logger)
        processor = BatchProcessor(
            config,
            excel_service,
            llm_service,
            logger,
            journal=journal,
        )
        stats = processor.run()
    except Exception as exc:
        logger.exception("Echec global du traitement : %s", exc)
        return 1

    logger.info(
        "Resume final | scannees=%s | eligibles=%s | traitees=%s | skip=%s | "
        "succes=%s | introuvables=%s | erreurs_techniques=%s | entrees_invalides=%s | "
        "echecs=%s | sauvegardes=%s | recuperees_journal=%s",
        stats.total_rows_scanned,
        stats.eligible_rows,
        stats.processed_rows,
        stats.skipped_rows,
        stats.success_rows,
        stats.not_found_rows,
        stats.technical_error_rows,
        stats.invalid_input_rows,
        stats.failed_rows,
        stats.saved_batches,
        stats.recovered_rows,
    )
    return 0


def _validate_runtime_config(config: AppConfig) -> None:
    if not config.input_excel_path.exists():
        raise FileNotFoundError(f"Fichier Excel introuvable : {config.input_excel_path}")
    if config.azure_foundry_ca_bundle is not None and not config.azure_foundry_ca_bundle.exists():
        raise FileNotFoundError(f"Certificat introuvable : {config.azure_foundry_ca_bundle}")


if __name__ == "__main__":
    raise SystemExit(main())
