from __future__ import annotations

import argparse
import csv
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import AppConfig, build_config
from app.excel_service import ExcelService
from app.logger import setup_logger
from app.models import NOT_FOUND_LABEL, CompanyResult, CompanyRow, IdentityMatchType, ProcessingStatus
from app.site_extractor import DeterministicSiteExtractor
from app.utils import clean_cell_value


@dataclass(frozen=True, slots=True)
class SiteBenchmarkReference:
    company: CompanyRow
    result: CompanyResult


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.site_benchmark",
        description="Mesure sans appel Azure les gains de l'extraction directe sur les sites deja trouves.",
    )
    parser.add_argument("--file", help="Classeur contenant les resultats existants.")
    parser.add_argument("--sheet", help="Nom de la feuille Excel.")
    parser.add_argument("--start-row", type=int, default=None, help="Premiere ligne candidate.")
    parser.add_argument("--max-rows", type=int, default=30, help="Nombre maximal de sites incomplets a tester.")
    parser.add_argument("--workers", type=int, default=4, help="Sites analyses simultanement.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks"),
        help="Repertoire du rapport CSV et de sa synthese JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logger("INFO", Path("logs") / "site_benchmark.log")
    try:
        base_config = build_config(args)
        config = replace(
            base_config,
            site_extraction_enabled=True,
            search_audit_enabled=True,
            max_workers=int(args.workers),
        )
        config.validate()
        references = _load_references(config, int(args.max_rows), logger)
        if not references:
            raise ValueError("Aucune ligne avec un site et un email ou telephone manquant.")
        rows = _run(config, references, logger)
        paths = write_report(rows, args.output_dir, source_file=config.input_excel_path)
    except Exception as exc:
        logger.exception("Echec du benchmark d'extraction directe : %s", exc)
        return 1

    summary = summarize_rows(rows)
    logger.info(
        "Extraction directe | lignes=%s | lignes_completees=%s | emails=%s | telephones=%s | erreurs=%s",
        summary["requested_rows"],
        summary["rows_with_gain"],
        summary["recovered_emails"],
        summary["recovered_phones"],
        summary["error_rows"],
    )
    logger.info("Rapport detaille : %s", paths["csv"])
    logger.info("Synthese JSON : %s", paths["json"])
    return 0


def _load_references(config: AppConfig, max_rows: int, logger: Any) -> list[SiteBenchmarkReference]:
    service = ExcelService(config.input_excel_path, config.sheet_name, logger)
    service.open()
    references: list[SiteBenchmarkReference] = []
    try:
        for row_index in service.get_row_indexes(config.start_row, None):
            if service.read_status(row_index) is not ProcessingStatus.SUCCESS:
                continue
            company = service.read_company_row(row_index)
            if not company.company_name or not company.has_valid_siret:
                continue
            email, phone, website = service.read_contact_values(row_index)
            if website == NOT_FOUND_LABEL or (email != NOT_FOUND_LABEL and phone != NOT_FOUND_LABEL):
                continue
            result = _existing_result(service, row_index, email, phone, website)
            references.append(SiteBenchmarkReference(company=company, result=result))
            if len(references) == max_rows:
                break
    finally:
        service.workbook.close()
    return references


def _existing_result(
    service: ExcelService,
    row_index: int,
    email: str,
    phone: str,
    website: str,
) -> CompanyResult:
    columns = service.columns
    match_value = _read_cell(service, row_index, columns.identity_match_type)
    try:
        match_type = IdentityMatchType(match_value) if match_value else IdentityMatchType.NAME_AND_CITY
    except ValueError:
        match_type = IdentityMatchType.NAME_AND_CITY
    if match_type is IdentityMatchType.NONE:
        match_type = IdentityMatchType.NAME_AND_CITY

    website_source = _read_cell(service, row_index, columns.website_source) or website
    identity_source = _read_cell(service, row_index, columns.identity_source) or website_source
    return CompanyResult(
        email=email,
        phone=phone,
        website=website,
        sources=service.read_sources(row_index),
        email_source=_read_cell(service, row_index, columns.email_source) or NOT_FOUND_LABEL,
        phone_source=_read_cell(service, row_index, columns.phone_source) or NOT_FOUND_LABEL,
        website_source=website_source,
        identity_verified=True,
        identity_match_type=match_type,
        identity_source=identity_source,
        email_extraction_method="baseline" if email != NOT_FOUND_LABEL else "",
        phone_extraction_method="baseline" if phone != NOT_FOUND_LABEL else "",
        identity_extraction_method="baseline",
    )


def _read_cell(service: ExcelService, row_index: int, column_index: int) -> str | None:
    return clean_cell_value(service.sheet.cell(row=row_index, column=column_index).value)


def _run(
    config: AppConfig,
    references: list[SiteBenchmarkReference],
    logger: Any,
) -> list[dict[str, Any]]:
    extractor = DeterministicSiteExtractor(config, logger)
    futures: dict[Future[tuple[CompanyResult, float]], SiteBenchmarkReference] = {}
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="site-extraction") as executor:
        for reference in references:
            futures[executor.submit(_timed_extract, extractor, reference)] = reference
        for future in as_completed(futures):
            reference = futures[future]
            try:
                result, latency = future.result()
                rows.append(_result_row(reference, result, latency))
            except Exception as exc:
                logger.error("Extraction site ligne %s : %s", reference.company.row_index, exc)
                rows.append(_error_row(reference, exc))
    return sorted(rows, key=lambda row: int(row["row_index"]))


def _timed_extract(
    extractor: DeterministicSiteExtractor,
    reference: SiteBenchmarkReference,
) -> tuple[CompanyResult, float]:
    started_at = time.perf_counter()
    result = extractor.enrich(reference.company, reference.result)
    return result, time.perf_counter() - started_at


def _result_row(
    reference: SiteBenchmarkReference,
    result: CompanyResult,
    latency_seconds: float,
) -> dict[str, Any]:
    return {
        "row_index": reference.company.row_index,
        "siret": reference.company.normalized_siret,
        "company_name": reference.company.company_name,
        "website": reference.result.website,
        "baseline_email": reference.result.email,
        "candidate_email": result.email,
        "email_source": result.email_source,
        "email_extraction_method": result.email_extraction_method,
        "baseline_phone": reference.result.phone,
        "candidate_phone": result.phone,
        "phone_source": result.phone_source,
        "phone_extraction_method": result.phone_extraction_method,
        "identity_match_type": result.identity_match_type.value,
        "identity_source": result.identity_source,
        "identity_extraction_method": result.identity_extraction_method,
        "deterministic_pages": json.dumps(result.deterministic_pages, ensure_ascii=False),
        "deterministic_fields_found": result.deterministic_fields_found,
        "legal_popup_detected": result.legal_popup_detected,
        "latency_seconds": round(latency_seconds, 3),
        "error_type": "",
        "error_message": "",
    }


def _error_row(reference: SiteBenchmarkReference, error: BaseException) -> dict[str, Any]:
    row = _result_row(reference, reference.result, 0.0)
    row["error_type"] = type(error).__name__
    row["error_message"] = str(error)[:500]
    return row


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if not row["error_type"]]
    return {
        "requested_rows": len(rows),
        "completed_rows": len(completed),
        "error_rows": len(rows) - len(completed),
        "rows_with_gain": sum(int(row["deterministic_fields_found"]) > 0 for row in completed),
        "recovered_emails": sum(
            row["baseline_email"] == NOT_FOUND_LABEL and row["candidate_email"] != NOT_FOUND_LABEL
            for row in completed
        ),
        "recovered_phones": sum(
            row["baseline_phone"] == NOT_FOUND_LABEL and row["candidate_phone"] != NOT_FOUND_LABEL
            for row in completed
        ),
        "site_identity_matches": sum(
            row["identity_extraction_method"] == "site_siret_or_siren" for row in completed
        ),
        "legal_popups_detected": sum(bool(row["legal_popup_detected"]) for row in completed),
        "pages_fetched": sum(len(json.loads(str(row["deterministic_pages"]))) for row in completed),
        "average_latency_seconds": (
            round(sum(float(row["latency_seconds"]) for row in completed) / len(completed), 3)
            if completed
            else 0.0
        ),
    }


def write_report(rows: list[dict[str, Any]], output_dir: Path, *, source_file: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"site_extraction_{timestamp}.csv"
    json_path = output_dir / f"site_extraction_{timestamp}.summary.json"
    if rows:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summary = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_file": str(source_file),
        **summarize_rows(rows),
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return {"csv": csv_path, "json": json_path}


if __name__ == "__main__":
    raise SystemExit(main())
