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
from urllib.parse import urlsplit

from app.config import AppConfig, build_config
from app.excel_service import ExcelService
from app.llm_service import AzureFoundryWebSearchService
from app.logger import setup_logger
from app.models import NOT_FOUND_LABEL, CompanyResult, CompanyRow, ProcessingStatus
from app.utils import safe_sleep


@dataclass(frozen=True, slots=True)
class BenchmarkReference:
    company: CompanyRow
    baseline_status: ProcessingStatus
    baseline_email: str
    baseline_phone: str
    baseline_website: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.benchmark",
        description="Compare des deploiements Foundry sans modifier le classeur Excel.",
    )
    parser.add_argument(
        "--deployment",
        action="append",
        required=True,
        help="Nom d'un deploiement Azure a tester. Repeter l'option pour plusieurs modeles.",
    )
    parser.add_argument("--file", help="Classeur contenant les resultats de reference.")
    parser.add_argument("--sheet", help="Nom de la feuille Excel.")
    parser.add_argument("--start-row", type=int, default=None, help="Premiere ligne candidate.")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=20,
        help="Nombre de lignes de reference terminees a comparer.",
    )
    parser.add_argument("--workers", type=int, default=2, help="Appels Azure simultanes.")
    parser.add_argument(
        "--search-context-size",
        choices=("default", "low", "medium", "high"),
        default="low",
        help="Contexte transmis par la recherche web au modele candidat.",
    )
    parser.add_argument(
        "--audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Applique la validation stricte des sources pendant le benchmark.",
    )
    parser.add_argument(
        "--site-extraction",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Active la seconde passe deterministe sur le site trouve.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks"),
        help="Repertoire des rapports CSV et JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logger("INFO", Path("logs") / "benchmark.log")

    try:
        base_config = build_config(args)
        if not base_config.input_excel_path.exists():
            raise FileNotFoundError(f"Fichier Excel introuvable : {base_config.input_excel_path}")
        references = _load_references(base_config, int(args.max_rows), logger)
        if not references:
            raise ValueError("Aucune ligne success/not_found valide trouvee dans la plage demandee.")

        all_rows: list[dict[str, Any]] = []
        for deployment in _unique_deployments(args.deployment):
            config = replace(
                base_config,
                azure_foundry_model_deployment=deployment,
                web_search_context_size=args.search_context_size,
                search_audit_enabled=bool(args.audit),
                site_extraction_enabled=bool(args.site_extraction),
            )
            config.validate()
            logger.info(
                "Benchmark %s : %s ligne(s), contexte=%s, audit=%s, extraction_site=%s.",
                deployment,
                len(references),
                config.web_search_context_size,
                config.search_audit_enabled,
                config.site_extraction_enabled,
            )
            all_rows.extend(_run_deployment(config, references, logger))

        report_paths = write_benchmark_report(
            all_rows,
            args.output_dir,
            source_file=base_config.input_excel_path,
        )
    except Exception as exc:
        logger.exception("Echec du benchmark : %s", exc)
        return 1

    summary = summarize_benchmark_rows(all_rows)
    for deployment, values in summary["deployments"].items():
        logger.info(
            "Resultat %s | lignes=%s | succes=%s | concordance_champs=%s%% | "
            "tokens=%s | recherches_web=%s | erreurs=%s",
            deployment,
            values["completed_rows"],
            values["candidate_success_rows"],
            values["exact_match_rate_on_baseline_found_pct"],
            values["total_tokens"],
            values["web_search_calls"],
            values["error_rows"],
        )
    logger.info("Rapport detaille : %s", report_paths["csv"])
    logger.info("Synthese JSON : %s", report_paths["json"])
    return 0


def _load_references(
    config: AppConfig,
    max_rows: int,
    logger: Any,
) -> list[BenchmarkReference]:
    excel_service = ExcelService(config.input_excel_path, config.sheet_name, logger)
    excel_service.open()
    references: list[BenchmarkReference] = []

    try:
        for row_index in excel_service.get_row_indexes(config.start_row, None):
            status = excel_service.read_status(row_index)
            if status not in {ProcessingStatus.SUCCESS, ProcessingStatus.NOT_FOUND}:
                continue
            company = excel_service.read_company_row(row_index)
            if not company.company_name or not company.has_valid_siret:
                continue
            email, phone, website = excel_service.read_contact_values(row_index)
            references.append(
                BenchmarkReference(
                    company=company,
                    baseline_status=status,
                    baseline_email=email,
                    baseline_phone=phone,
                    baseline_website=website,
                )
            )
            if len(references) == max_rows:
                break
    finally:
        excel_service.workbook.close()

    return references


def _run_deployment(
    config: AppConfig,
    references: list[BenchmarkReference],
    logger: Any,
) -> list[dict[str, Any]]:
    search_service = AzureFoundryWebSearchService(config, logger)
    futures: dict[Future[tuple[CompanyResult, float]], BenchmarkReference] = {}
    rows: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="benchmark-search") as executor:
        for position, reference in enumerate(references, start=1):
            futures[executor.submit(_timed_search, search_service, reference.company)] = reference
            if position < len(references):
                safe_sleep(config.sleep_between_calls)

        for future in as_completed(futures):
            reference = futures[future]
            try:
                result, latency_seconds = future.result()
                rows.append(_build_benchmark_row(config, reference, result, latency_seconds))
            except Exception as exc:
                rows.append(_build_error_row(config, reference, exc))
                logger.error(
                    "Benchmark %s ligne %s : %s",
                    config.azure_foundry_model_deployment,
                    reference.company.row_index,
                    exc,
                )

    return sorted(rows, key=lambda row: int(row["row_index"]))


def _timed_search(
    search_service: AzureFoundryWebSearchService,
    company: CompanyRow,
) -> tuple[CompanyResult, float]:
    started_at = time.perf_counter()
    result = search_service.search_company_contact(company)
    return result, time.perf_counter() - started_at


def _build_benchmark_row(
    config: AppConfig,
    reference: BenchmarkReference,
    result: CompanyResult,
    latency_seconds: float,
) -> dict[str, Any]:
    row = _base_row(config, reference)
    row.update(
        {
            "candidate_status": "not_found" if result.is_not_found else "success",
            "candidate_email": result.email,
            "candidate_phone": result.phone,
            "candidate_website": result.website,
            "email_exact_match": _contact_matches("email", reference.baseline_email, result.email),
            "phone_exact_match": _contact_matches("phone", reference.baseline_phone, result.phone),
            "website_exact_match": _contact_matches("website", reference.baseline_website, result.website),
            "identity_match_type": result.identity_match_type.value,
            "identity_source": result.identity_source,
            "email_source": result.email_source,
            "phone_source": result.phone_source,
            "website_source": result.website_source,
            "sources": json.dumps(result.sources, ensure_ascii=False),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "web_search_calls": result.web_search_calls,
            "deterministic_pages": json.dumps(result.deterministic_pages, ensure_ascii=False),
            "deterministic_fields_found": result.deterministic_fields_found,
            "email_extraction_method": result.email_extraction_method,
            "phone_extraction_method": result.phone_extraction_method,
            "identity_extraction_method": result.identity_extraction_method,
            "legal_popup_detected": result.legal_popup_detected,
            "latency_seconds": round(latency_seconds, 3),
            "error_type": "",
            "error_message": "",
        }
    )
    return row


def _build_error_row(
    config: AppConfig,
    reference: BenchmarkReference,
    error: BaseException,
) -> dict[str, Any]:
    row = _base_row(config, reference)
    row.update(
        {
            "candidate_status": "technical_error",
            "candidate_email": NOT_FOUND_LABEL,
            "candidate_phone": NOT_FOUND_LABEL,
            "candidate_website": NOT_FOUND_LABEL,
            "email_exact_match": False,
            "phone_exact_match": False,
            "website_exact_match": False,
            "identity_match_type": "none",
            "identity_source": NOT_FOUND_LABEL,
            "email_source": NOT_FOUND_LABEL,
            "phone_source": NOT_FOUND_LABEL,
            "website_source": NOT_FOUND_LABEL,
            "sources": "[]",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "web_search_calls": 0,
            "deterministic_pages": "[]",
            "deterministic_fields_found": 0,
            "email_extraction_method": "",
            "phone_extraction_method": "",
            "identity_extraction_method": "",
            "legal_popup_detected": False,
            "latency_seconds": 0.0,
            "error_type": type(error).__name__,
            "error_message": str(error)[:500],
        }
    )
    return row


def _base_row(config: AppConfig, reference: BenchmarkReference) -> dict[str, Any]:
    return {
        "deployment": config.azure_foundry_model_deployment,
        "search_context_size": config.web_search_context_size,
        "audit_enabled": config.search_audit_enabled,
        "site_extraction_enabled": config.site_extraction_enabled,
        "row_index": reference.company.row_index,
        "siret": reference.company.normalized_siret,
        "company_name": reference.company.company_name,
        "baseline_status": reference.baseline_status.value,
        "baseline_email": reference.baseline_email,
        "baseline_phone": reference.baseline_phone,
        "baseline_website": reference.baseline_website,
    }


def summarize_benchmark_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    deployments: dict[str, dict[str, Any]] = {}
    for deployment in dict.fromkeys(str(row["deployment"]) for row in rows):
        model_rows = [row for row in rows if row["deployment"] == deployment]
        completed_rows = [row for row in model_rows if not row["error_type"]]
        baseline_found = 0
        candidate_found = 0
        exact_matches = 0
        new_contacts = 0

        for row in completed_rows:
            for field in ("email", "phone", "website"):
                baseline_value = str(row[f"baseline_{field}"])
                candidate_value = str(row[f"candidate_{field}"])
                if _is_found(baseline_value):
                    baseline_found += 1
                    if bool(row[f"{field}_exact_match"]):
                        exact_matches += 1
                if _is_found(candidate_value):
                    candidate_found += 1
                    if not _is_found(baseline_value):
                        new_contacts += 1

        deployments[deployment] = {
            "requested_rows": len(model_rows),
            "completed_rows": len(completed_rows),
            "error_rows": len(model_rows) - len(completed_rows),
            "baseline_success_rows": sum(row["baseline_status"] == "success" for row in model_rows),
            "candidate_success_rows": sum(row["candidate_status"] == "success" for row in completed_rows),
            "baseline_found_fields": baseline_found,
            "candidate_found_fields": candidate_found,
            "exact_matches_on_baseline_found": exact_matches,
            "exact_match_rate_on_baseline_found_pct": _percentage(exact_matches, baseline_found),
            "new_candidate_contacts": new_contacts,
            "input_tokens": sum(int(row["input_tokens"]) for row in completed_rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in completed_rows),
            "total_tokens": sum(int(row["total_tokens"]) for row in completed_rows),
            "web_search_calls": sum(int(row["web_search_calls"]) for row in completed_rows),
            "deterministic_fields_found": sum(
                int(row.get("deterministic_fields_found", 0)) for row in completed_rows
            ),
            "rows_with_deterministic_gain": sum(
                int(row.get("deterministic_fields_found", 0)) > 0 for row in completed_rows
            ),
            "average_latency_seconds": round(
                sum(float(row["latency_seconds"]) for row in completed_rows) / len(completed_rows),
                3,
            )
            if completed_rows
            else 0.0,
        }

    return {"deployments": deployments}


def write_benchmark_report(
    rows: list[dict[str, Any]],
    output_dir: Path,
    *,
    source_file: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"model_benchmark_{timestamp}.csv"
    json_path = output_dir / f"model_benchmark_{timestamp}.summary.json"

    if rows:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_file": str(source_file),
        **summarize_benchmark_rows(rows),
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    return {"csv": csv_path, "json": json_path}


def _contact_matches(field: str, baseline: str, candidate: str) -> bool:
    if not _is_found(baseline) or not _is_found(candidate):
        return False
    return _canonical_contact(field, baseline) == _canonical_contact(field, candidate)


def _canonical_contact(field: str, value: str) -> str:
    if field == "email":
        return value.casefold().strip()
    if field == "phone":
        return "".join(character for character in value if character.isdigit())
    if field == "website":
        candidate = value if "://" in value else f"https://{value}"
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        return f"{host}{parsed.path.rstrip('/')}"
    return value.casefold().strip()


def _is_found(value: str) -> bool:
    return bool(value.strip()) and value.strip().casefold() != NOT_FOUND_LABEL.casefold()


def _percentage(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def _unique_deployments(values: list[str]) -> list[str]:
    deployments: list[str] = []
    for raw_value in values:
        deployment = raw_value.strip()
        if not deployment:
            raise ValueError("Le nom du deploiement ne peut pas etre vide.")
        if deployment not in deployments:
            deployments.append(deployment)
    return deployments


if __name__ == "__main__":
    raise SystemExit(main())
