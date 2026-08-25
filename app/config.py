from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

from app.utils import parse_bool, parse_optional_float, parse_optional_int


@dataclass(slots=True)
class AppConfig:
    input_excel_path: Path
    sheet_name: str | None
    start_row: int
    max_rows: int | None
    batch_size: int
    save_every_batch: bool
    skip_if_filled: bool
    overwrite_existing: bool
    azure_foundry_endpoint: str
    azure_foundry_api_key: str
    azure_foundry_model_deployment: str
    azure_foundry_reasoning_effort: str
    web_search_context_size: str
    search_audit_enabled: bool
    max_workers: int
    request_timeout: float
    max_retries: int
    retry_wait_seconds: float
    sleep_between_calls: float
    sleep_between_batches: float
    log_level: str
    create_backup: bool
    azure_foundry_ca_bundle: Path | None
    log_file_path: Path
    journal_file_path: Path
    invalid_response_file_path: Path
    site_extraction_enabled: bool = True
    site_extraction_max_pages: int = 6
    site_extraction_timeout: float = 12.0
    site_extraction_max_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> AppConfig:
        load_dotenv(override=False)

        sheet_name = os.getenv("SHEET_NAME", "").strip() or None
        azure_foundry_ca_bundle = _resolve_ca_bundle_path(os.getenv("AZURE_FOUNDRY_CA_BUNDLE"))
        config = cls(
            input_excel_path=Path(os.getenv("INPUT_EXCEL_PATH", "data/20ksocietes.xlsx")).expanduser(),
            sheet_name=sheet_name,
            start_row=_env_int("START_ROW", 2),
            max_rows=parse_optional_int(os.getenv("MAX_ROWS")),
            batch_size=_env_int("BATCH_SIZE", 100),
            save_every_batch=parse_bool(os.getenv("SAVE_EVERY_BATCH"), default=True),
            skip_if_filled=parse_bool(os.getenv("SKIP_IF_FILLED"), default=True),
            overwrite_existing=parse_bool(os.getenv("OVERWRITE_EXISTING"), default=False),
            azure_foundry_endpoint=_normalize_foundry_endpoint(os.getenv("AZURE_FOUNDRY_ENDPOINT", "")),
            azure_foundry_api_key=os.getenv("AZURE_FOUNDRY_API_KEY", "").strip(),
            azure_foundry_model_deployment=os.getenv("AZURE_FOUNDRY_MODEL_DEPLOYMENT", "gpt-5.6-luna").strip(),
            azure_foundry_reasoning_effort=os.getenv("AZURE_FOUNDRY_REASONING_EFFORT", "none").strip().lower(),
            web_search_context_size=os.getenv("WEB_SEARCH_CONTEXT_SIZE", "default").strip().lower(),
            search_audit_enabled=parse_bool(os.getenv("SEARCH_AUDIT_ENABLED"), default=False),
            max_workers=_env_int("MAX_WORKERS", 4),
            request_timeout=_env_float("REQUEST_TIMEOUT", 90.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            retry_wait_seconds=_env_float("RETRY_WAIT_SECONDS", 5.0),
            sleep_between_calls=_env_float("SLEEP_BETWEEN_CALLS", 0.0),
            sleep_between_batches=_env_float("SLEEP_BETWEEN_BATCHES", 0.0),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            create_backup=parse_bool(os.getenv("CREATE_BACKUP"), default=True),
            azure_foundry_ca_bundle=azure_foundry_ca_bundle,
            log_file_path=Path("logs") / "enrichment.log",
            journal_file_path=Path(
                os.getenv(
                    "PROCESSING_JOURNAL_PATH",
                    "logs/enrichment.pending.jsonl",
                )
            ).expanduser(),
            invalid_response_file_path=Path(
                os.getenv(
                    "INVALID_RESPONSE_PATH",
                    "logs/invalid_foundry_responses.jsonl",
                )
            ).expanduser(),
            site_extraction_enabled=parse_bool(os.getenv("SITE_EXTRACTION_ENABLED"), default=True),
            site_extraction_max_pages=_env_int("SITE_EXTRACTION_MAX_PAGES", 6),
            site_extraction_timeout=_env_float("SITE_EXTRACTION_TIMEOUT", 12.0),
            site_extraction_max_bytes=_env_int("SITE_EXTRACTION_MAX_BYTES", 2_000_000),
        )
        config.validate()
        return config

    def apply_cli_overrides(self, args: argparse.Namespace) -> AppConfig:
        updated = self

        if getattr(args, "file", None):
            updated = replace(updated, input_excel_path=Path(args.file).expanduser())
        if getattr(args, "sheet", None) is not None:
            updated = replace(updated, sheet_name=str(args.sheet).strip() or None)
        if getattr(args, "start_row", None) is not None:
            updated = replace(updated, start_row=int(args.start_row))
        if getattr(args, "max_rows", None) is not None:
            updated = replace(updated, max_rows=int(args.max_rows))
        if getattr(args, "batch_size", None) is not None:
            updated = replace(updated, batch_size=int(args.batch_size))
        if getattr(args, "skip_if_filled", None) is not None:
            updated = replace(updated, skip_if_filled=bool(args.skip_if_filled))
        if getattr(args, "overwrite_existing", None) is not None:
            updated = replace(updated, overwrite_existing=bool(args.overwrite_existing))
        if getattr(args, "audit", None) is not None:
            updated = replace(updated, search_audit_enabled=bool(args.audit))
        if getattr(args, "workers", None) is not None:
            updated = replace(updated, max_workers=int(args.workers))
        if getattr(args, "search_context_size", None) is not None:
            updated = replace(
                updated,
                web_search_context_size=str(args.search_context_size).strip().lower(),
            )
        if getattr(args, "site_extraction", None) is not None:
            updated = replace(updated, site_extraction_enabled=bool(args.site_extraction))

        updated.validate()
        return updated

    def validate(self) -> None:
        if self.start_row < 2:
            raise ValueError("START_ROW doit etre >= 2.")
        if self.batch_size <= 0:
            raise ValueError("BATCH_SIZE doit etre > 0.")
        if self.max_rows is not None and self.max_rows <= 0:
            raise ValueError("MAX_ROWS doit etre > 0 si renseigne.")
        if self.request_timeout <= 0:
            raise ValueError("REQUEST_TIMEOUT doit etre > 0.")
        if self.max_retries <= 0:
            raise ValueError("MAX_RETRIES doit etre > 0.")
        if self.retry_wait_seconds < 0:
            raise ValueError("RETRY_WAIT_SECONDS doit etre >= 0.")
        if self.sleep_between_calls < 0:
            raise ValueError("SLEEP_BETWEEN_CALLS doit etre >= 0.")
        if self.sleep_between_batches < 0:
            raise ValueError("SLEEP_BETWEEN_BATCHES doit etre >= 0.")
        if not 1 <= self.max_workers <= 32:
            raise ValueError("MAX_WORKERS doit etre compris entre 1 et 32.")
        if not self.azure_foundry_endpoint:
            raise ValueError("AZURE_FOUNDRY_ENDPOINT est obligatoire.")
        if not self.azure_foundry_model_deployment:
            raise ValueError("AZURE_FOUNDRY_MODEL_DEPLOYMENT est obligatoire.")
        if self.azure_foundry_reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("AZURE_FOUNDRY_REASONING_EFFORT doit valoir none, low, medium, high, xhigh ou max.")
        if self.web_search_context_size not in {"default", "low", "medium", "high"}:
            raise ValueError("WEB_SEARCH_CONTEXT_SIZE doit valoir default, low, medium ou high.")
        if not 1 <= self.site_extraction_max_pages <= 12:
            raise ValueError("SITE_EXTRACTION_MAX_PAGES doit etre compris entre 1 et 12.")
        if self.site_extraction_timeout <= 0:
            raise ValueError("SITE_EXTRACTION_TIMEOUT doit etre > 0.")
        if not 100_000 <= self.site_extraction_max_bytes <= 10_000_000:
            raise ValueError("SITE_EXTRACTION_MAX_BYTES doit etre compris entre 100000 et 10000000.")
        if self.input_excel_path.suffix.lower() != ".xlsx":
            raise ValueError("INPUT_EXCEL_PATH doit pointer vers un fichier .xlsx.")
        if self.azure_foundry_ca_bundle is not None and not self.azure_foundry_ca_bundle.exists():
            raise ValueError(f"AZURE_FOUNDRY_CA_BUNDLE introuvable : {self.azure_foundry_ca_bundle}")


def build_config(args: argparse.Namespace | None = None) -> AppConfig:
    config = AppConfig.from_env()
    if args is None:
        return config
    return config.apply_cli_overrides(args)


def _resolve_ca_bundle_path(raw_value: str | None) -> Path | None:
    if raw_value and raw_value.strip():
        return Path(raw_value.strip()).expanduser()

    for candidate_name in ("Zscaler Root CA.crt", "zscaler_root_ra.crt"):
        candidate_path = Path(candidate_name)
        if candidate_path.exists():
            return candidate_path

    return None


def _normalize_foundry_endpoint(raw_value: str) -> str:
    endpoint = raw_value.strip().rstrip("/")
    if not endpoint:
        return ""
    if endpoint.endswith("/openai/v1"):
        return f"{endpoint}/"
    return f"{endpoint}/openai/v1/"


def _env_int(name: str, default: int) -> int:
    value = parse_optional_int(os.getenv(name))
    return default if value is None else value


def _env_float(name: str, default: float) -> float:
    value = parse_optional_float(os.getenv(name))
    return default if value is None else value
