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
    openai_api_key: str
    openai_model: str
    request_timeout: float
    max_retries: int
    retry_wait_seconds: float
    sleep_between_calls: float
    sleep_between_batches: float
    log_level: str
    create_backup: bool
    openai_ca_bundle: Path | None
    log_file_path: Path

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv(override=False)

        sheet_name = os.getenv("SHEET_NAME", "").strip() or None
        openai_ca_bundle = _resolve_ca_bundle_path(os.getenv("OPENAI_CA_BUNDLE"))
        config = cls(
            input_excel_path=Path(os.getenv("INPUT_EXCEL_PATH", "20ksocietes.xlsx")).expanduser(),
            sheet_name=sheet_name,
            start_row=parse_optional_int(os.getenv("START_ROW")) or 2,
            max_rows=parse_optional_int(os.getenv("MAX_ROWS")),
            batch_size=parse_optional_int(os.getenv("BATCH_SIZE")) or 20,
            save_every_batch=parse_bool(os.getenv("SAVE_EVERY_BATCH"), default=True),
            skip_if_filled=parse_bool(os.getenv("SKIP_IF_FILLED"), default=True),
            overwrite_existing=parse_bool(os.getenv("OVERWRITE_EXISTING"), default=False),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            request_timeout=parse_optional_float(os.getenv("REQUEST_TIMEOUT")) or 90.0,
            max_retries=parse_optional_int(os.getenv("MAX_RETRIES")) or 3,
            retry_wait_seconds=parse_optional_float(os.getenv("RETRY_WAIT_SECONDS")) or 5.0,
            sleep_between_calls=parse_optional_float(os.getenv("SLEEP_BETWEEN_CALLS")) or 0.0,
            sleep_between_batches=parse_optional_float(os.getenv("SLEEP_BETWEEN_BATCHES")) or 0.0,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            create_backup=parse_bool(os.getenv("CREATE_BACKUP"), default=True),
            openai_ca_bundle=openai_ca_bundle,
            log_file_path=Path("logs") / "enrichment.log",
        )
        config.validate()
        return config

    def apply_cli_overrides(self, args: argparse.Namespace) -> "AppConfig":
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
        if self.input_excel_path.suffix.lower() != ".xlsx":
            raise ValueError("INPUT_EXCEL_PATH doit pointer vers un fichier .xlsx.")
        if self.openai_ca_bundle is not None and not self.openai_ca_bundle.exists():
            raise ValueError(f"OPENAI_CA_BUNDLE introuvable : {self.openai_ca_bundle}")


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
